#!/usr/bin/env python3
"""ambient-watch status -- inspect the live ledger, config, budget and gate.

Read the ledger HERE, in a terminal, outside the agent: the data directory is
a containment boundary that no tool call may reference (see the README's L3
jail), and this is the sanctioned way in -- a human doing the reading.

But "in a terminal" is not proof of "by a human": the jail blocks a tool call
that NAMES the data directory, and `terminal: python aw_status.py` names
nothing, so an agent session can run this script. Everything printed out of
the `messages` ledger therefore goes through aw_sanitize's export profile
(see safe_text) -- the same L1 profile as the stored excerpt.

Usage:
    python aw_status.py                 # full status
    python aw_status.py --kill on|off   # flip the kill switch (no LLM in path)
    python aw_status.py --mode shadow|live
    python aw_status.py --prod          # restore production thresholds
    python aw_status.py --reset-ledger  # wipe recorded messages (keeps config)
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

PROD = {
    "min_age_minutes": 45,
    "quiet_start": "20:00",
    "quiet_end": "09:00",
}

# Config keys the decision path no longer reads (see aw_config.LEGACY_KEYS).
RETIRED_KEYS = (
    "cooldown_minutes",
    "caps_per_channel_per_day",
    "caps_global_per_day",
    "unanswered_after_minutes",
    "stalled_after_minutes",
)

DAY = 86400


def home() -> Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "hermes"
    return Path.home() / ".hermes"


DATA = home() / "plugin-data" / "ambient_watch"
CFG_PATH = DATA / "config.json"
DB_PATH = DATA / "ambient.db"

# L1 for everything this script prints out of the ledger. The `messages` table
# holds VERBATIM channel text on purpose (the detectors run SQL over it), and
# "printed to a terminal" is NOT the same as "read by a human": the L3 jail
# blocks a tool call that NAMES the data directory, but it cannot see
# `terminal: python aw_status.py`, which names nothing. An agent session --
# with the same full toolset as the 2026-08-11 incident -- can therefore run
# this file, and printing raw bodies would hand it the exact payload the jail
# exists to keep away from it. So bodies go through the same export profile as
# the stored excerpt: inert, capped, instruction-shaped text redacted.
sys.path.insert(0, str(Path(__file__).resolve().parent / "ambient-watch"))
try:
    from aw_sanitize import neutralize as _neutralize
except Exception:  # noqa: BLE001 — no plugin dir next to this script
    _neutralize = None

RAW_TEXT_WITHHELD = "[text withheld: aw_sanitize could not be imported]"


def safe_text(text, max_chars: int = 70) -> str:
    """Ledger text -> printable. Fails closed if the sanitizer is missing."""
    if _neutralize is None:
        return RAW_TEXT_WITHHELD
    return repr(_neutralize(text or "", max_chars))


def load_cfg() -> dict:
    return json.loads(CFG_PATH.read_text(encoding="utf-8"))


def save_cfg(cfg: dict):
    CFG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def ago(ts: float) -> str:
    m = (time.time() - float(ts)) / 60
    if m < 60:
        return f"{m:.0f}m ago"
    if m < 1440:
        return f"{m/60:.1f}h ago"
    return f"{m/1440:.1f}d ago"


def _sum(db, sql, args) -> float:
    try:
        row = db.execute(sql, args).fetchone()
    except sqlite3.OperationalError:
        return 0.0  # budget tables appear on the first sweep
    return float(row[0] or 0.0)


def _bar(spent: float, cap: float) -> str:
    if not cap:
        return f"${spent:.4f} / (no cap)  !! DECLINING EVERYTHING"
    ratio = spent / cap
    flag = ""
    if ratio >= 1.0:
        flag = "  !! EXCEEDED -- candidates are being DECLINED"
    elif ratio >= 0.95:
        flag = "  !  95% alert"
    elif ratio >= 0.75:
        flag = "  !  75% alert"
    return f"${spent:.4f} / ${cap:.2f}  ({ratio:.0%}){flag}"


def budget_section(db, cfg):
    print("\nSPEND (the limiter -- there are no cooldowns or daily nudge caps)")
    now = time.time()
    day, month = now - DAY, now - 30 * DAY
    caps = (
        float(cfg.get("daily_usd_global", 1.00) or 0),
        float(cfg.get("daily_usd_per_channel", 0.25) or 0),
        float(cfg.get("monthly_usd_global", 10.00) or 0),
    )
    if not any(caps):
        print("  !! NO CAP CONFIGURED -- Budget.decision() returns 'unconfigured',")
        print("     so every candidate is declined and ambient does nothing.")
    print(f"  global today      : {_bar(_sum(db, 'SELECT SUM(usd) FROM budget_usage WHERE created_at>?', (day,)), caps[0])}")
    print(f"  global this month : {_bar(_sum(db, 'SELECT SUM(usd) FROM budget_usage WHERE created_at>?', (month,)), caps[2])}")
    for channel in cfg.get("channels", []):
        spent = _sum(
            db,
            "SELECT SUM(usd) FROM budget_usage WHERE channel=? AND created_at>?",
            (channel, day),
        )
        print(f"  {channel} today : {_bar(spent, caps[1])}")
    try:
        alerts = db.execute(
            "SELECT scope, threshold, period_key FROM budget_alerts"
            " ORDER BY rowid DESC LIMIT 5"
        ).fetchall()
    except sqlite3.OperationalError:
        alerts = []
    if alerts:
        print("  alerts fired      : " + ", ".join(
            f"{a['scope']}@{a['threshold']:.0%}" for a in alerts
        ))


def _json_flag(db, key) -> dict:
    try:
        row = db.execute("SELECT value FROM flags WHERE key=?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return {}
    if not row or not row["value"]:
        return {}
    try:
        data = json.loads(row["value"])
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def arrival_section(db, cfg):
    """Arrival-time judging: config, durable counters, and the audit tail.

    THE HONEST CAVEAT, stated in the output itself: the pending map and the
    rate buckets live in memory inside the GATEWAY process, so this CLI cannot
    see them. What is visible here is only what the arrival path wrote down.
    """
    enabled = bool(cfg.get("arrival_enabled", False))
    print("\nARRIVAL-TIME JUDGING (judge on message arrival, debounced)")
    print(f"  arrival_enabled   : {'ON' if enabled else 'off (sweep-only)'}")
    print(f"  debounce / floor  : {cfg.get('arrival_debounce_seconds', 90)}s"
          "   (both the coalescing quiet period and the politeness floor)")
    print(f"  max wait          : {cfg.get('arrival_max_wait_seconds', 300)}s"
          "  (a never-quiet thread still gets judged once; 0 disables)")
    print(f"  rate buckets      : {cfg.get('arrival_judgments_per_channel_hour', 4)}"
          f"/hour per channel, {cfg.get('arrival_judgments_global_hour', 12)}/hour"
          f" global, burst {cfg.get('arrival_burst', 2)}")
    print(f"  pending cap       : {cfg.get('arrival_max_pending', 200)} thread(s)"
          f", pump every {cfg.get('arrival_pump_interval_seconds', 5)}s")
    print(f"  sweep window      : min_age_minutes="
          f"{cfg.get('min_age_minutes', 45)} min -- SWEEP-ONLY. The two triggers")
    print("                      partition by age: arrival owns [debounce,")
    print("                      min_age_minutes), the sweep owns the rest and")
    print("                      remains the stalled-thread trigger.")

    counters = _json_flag(db, "arrival_counters")
    reported = _json_flag(db, "arrival_reported")
    if not counters:
        print("  activity          : (none recorded)")
    else:
        keys = ("judged", "posted", "withheld", "declined", "throttled",
                "shadow", "post_failed", "errors")
        body = "  ".join(f"{k}={int(counters.get(k) or 0)}" for k in keys)
        print(f"  activity (total)  : {body}")
        print(f"  arrival spend     : ${float(counters.get('usd') or 0):.4f}"
              " (already included in the SPEND totals above)")
        if counters.get("updated_at"):
            print(f"  last arrival act. : {ago(counters['updated_at'])}")
        pending_report = {
            k: int(counters.get(k) or 0) - int(reported.get(k) or 0) for k in keys
        }
        if any(v > 0 for v in pending_report.values()):
            print("  not yet reported  : "
                  + "  ".join(f"{k}={v}" for k, v in pending_report.items() if v > 0)
                  + "  (the next sweep tick announces it)")

    print("  NOT VISIBLE HERE  : the pending map and the rate buckets are"
          " in-memory in the")
    print("                      gateway process. A gateway restart loses"
          " in-flight debounce")
    print("                      state; the sweep is what recovers those"
          " threads.")

    log = DATA / "arrival.log"
    if log.exists() and log.stat().st_size:
        print(f"  {log} (tail):")
        tail = log.read_text(encoding="utf-8", errors="replace").strip()
        for line in tail.splitlines()[-8:]:
            print(f"    {line}")
    elif enabled:
        print("  (no arrival.log yet -- nothing has been judged on arrival)")


def context_section(db, cfg):
    """Context fidelity: the effective settings, and what the LAST judgment
    actually saw — in COUNTS AND CHARACTERS, never the text.

    The fetched strings never reach this script at all: nothing persists them
    (``ContextCache`` is a process-local dict by design), so there is nothing
    here to sanitize. If that ever changes, whatever is added must go through
    ``safe_text`` like every other body — the same rule the RECORDED MESSAGES
    section follows.
    """
    enabled = bool(cfg.get("context_enabled", False))
    print("\nCONTEXT FIDELITY (what the judge is allowed to see)")
    print(f"  context_enabled   : {'ON' if enabled else 'off (ledger thread only)'}")
    print(f"  thread backfill   : "
          f"{'on' if cfg.get('context_thread_backfill', True) else 'off'}"
          "   (conversations.replies when the ledger has no root row --")
    print("                      such a thread is otherwise invisible to BOTH triggers)")
    print(f"  channel identity  : "
          f"{'on' if cfg.get('context_topic', True) else 'off'}"
          "   (conversations.info: name/topic/purpose, <=200 chars, cached "
          f"{int(cfg.get('context_cache_ttl_seconds', 21600)) // 3600}h)")
    print(f"  channel activity  : "
          f"{'on' if cfg.get('context_channel_history', True) else 'off'}"
          f"   ({cfg.get('context_channel_messages', 6)} msg / "
          f"{cfg.get('context_channel_hours', 6)}h window, LEDGER first --")
    print("                      conversations.history only when the ledger is thin)")
    pins_flag = None
    try:
        row = db.execute(
            "SELECT value FROM flags WHERE key='context_pins_scope'"
        ).fetchone()
        pins_flag = row["value"] if row else None
    except sqlite3.OperationalError:
        pins_flag = None
    if not cfg.get("context_pins", False):
        print("  pinned items      : off (default) -- pins:read is NOT in the bot")
        print("                      token's scopes on this install, so turning it on")
        print("                      needs an app-manifest change plus a human")
        print("                      Reinstall to Workspace first.")
    elif pins_flag:
        print(f"  pinned items      : unavailable ({pins_flag})")
        print("                      REMEDIATION: api.slack.com/apps -> your app ->")
        print("                      OAuth & Permissions -> add the pins:read BOT")
        print("                      scope -> Reinstall to Workspace -> re-copy the")
        print("                      bot token into %LOCALAPPDATA%\\hermes\\.env ->")
        print("                      restart the gateway. Do NOT hand-edit")
        print("                      slack-manifest.json: `hermes slack manifest")
        print("                      --write` regenerates it and drops the edit.")
    else:
        print(f"  pinned items      : on ({cfg.get('context_pin_items', 3)} item(s) max)")
    print(f"  ceiling           : {cfg.get('context_max_chars', 4400)} chars per "
          "nominee, over ALL sections, applied")
    print("                      AFTER assembly -- so a busy channel cannot inflate")
    print("                      one judge call by a single character.")
    print(f"  fetch timeouts    : {cfg.get('context_fetch_timeout_seconds', 4)}s per "
          f"call, {cfg.get('context_total_timeout_seconds', 8)}s for the whole "
          "enrichment")

    counters = _json_flag(db, "context_counters")
    if counters:
        print("  totals            : " + "  ".join(
            f"{k}={int(counters.get(k) or 0)}"
            for k in ("judgments", "fetches", "failures", "rate_limited",
                      "budget_skipped", "cache_hits", "cache_misses", "dropped")
        ))
        if counters.get("updated_at"):
            print(f"  last enrichment   : {ago(counters['updated_at'])}")
    elif enabled:
        print("  totals            : (nothing enriched yet)")

    last = _json_flag(db, "context_last")
    if last:
        print("  last judgment saw : "
              f"{int(last.get('chars') or 0)} chars total "
              f"({int(last.get('context_chars') or 0)} of them context), "
              f"{int(last.get('thread_msgs') or 0)} thread message(s), "
              f"{int(last.get('fetches') or 0)} Slack call(s)")
        print(f"                      sections: "
              f"{', '.join(last.get('sections') or ['(none)'])}")
        if last.get("notes"):
            print(f"                      degraded: {', '.join(last['notes'])}")
        print("                      (counts only -- no fetched text is stored"
              " anywhere,")
        print("                      which is why there is none to print here)")


def judgment_section(db):
    print("\nJUDGE OUTCOMES (the model decides; a '?' regex no longer does)")
    try:
        rows = db.execute(
            "SELECT * FROM judgments ORDER BY updated_at DESC LIMIT 10"
        ).fetchall()
    except sqlite3.OperationalError:
        print("  (no judgments yet)")
        return
    if not rows:
        print("  (none)")
    for r in rows:
        print(f"  {ago(r['updated_at']):>10}  {r['channel']}/{r['thread_ts']} "
              f"-> {r['verdict']} conf={r['confidence']:.2f} x{r['judge_count']}")
        if r["reason"]:
            print(f"              why: {r['reason']}")
        if r["nudge"]:
            print(f"            nudge: {r['nudge']}")
        if r["excerpt"]:
            # Sanitized L1 export profile -- safe to show a human in a terminal.
            print(f"           thread: {(r['excerpt'] or '')[:160]}")


def status():
    cfg = load_cfg()
    print("=" * 68)
    print("CONFIG")
    print(f"  mode              : {cfg.get('mode')}")
    print(f"  watched channels  : {cfg.get('channels')}")
    print(f"  ops channel       : {cfg.get('ops_channel')}")
    print(f"  min thread age    : {cfg.get('min_age_minutes', 45)} min (sweep only)")
    print(f"  nominees per sweep: {cfg.get('candidates_per_run', 3)}")
    print(f"  judge threshold   : {cfg.get('judge_confidence_threshold', 0.7)}")
    print(f"  judge model       : {cfg.get('judge_model') or 'auxiliary.ambient_watch_judge'}")
    print(f"  quiet hours       : {cfg.get('quiet_start','20:00')}-"
          f"{cfg.get('quiet_end','09:00')} {cfg.get('quiet_tz','UTC')}")
    is_prod = all(cfg.get(k, v) == v for k, v in PROD.items())
    print(f"  thresholds        : {'PRODUCTION' if is_prod else 'TEST (lowered)'}")
    stale = [k for k in RETIRED_KEYS if k in cfg]
    if stale:
        print(f"  retired keys      : {', '.join(stale)} (ignored -- safe to delete)")

    if not DB_PATH.exists():
        print("\n(no ledger yet)")
        return
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row

    row = db.execute("SELECT value FROM flags WHERE key='kill_switch'").fetchone()
    print(f"  kill switch       : {'ON (ambient halted)' if row and row['value']=='1' else 'off'}")

    budget_section(db, cfg)
    arrival_section(db, cfg)
    context_section(db, cfg)
    judgment_section(db)

    print("\nRECORDED MESSAGES (L1-sanitized: inert, capped, injections redacted)")
    msgs = db.execute("SELECT * FROM messages ORDER BY CAST(ts AS REAL) DESC LIMIT 15").fetchall()
    if not msgs:
        print("  (none)")
    for m in msgs:
        tag = "MENTION" if m["is_mention"] else ("BOT" if m["is_bot"] else "plain ")
        thread = "" if m["ts"] == m["thread_root"] else f" (reply in {m['thread_root']})"
        print(f"  [{tag}] {ago(m['ts']):>10}  {m['author']}: {safe_text(m['text'])}{thread}")

    print("\nENGAGED THREADS (bot is conversing -- never nudged)")
    eng = db.execute("SELECT * FROM engaged_threads").fetchall()
    print("  (none)" if not eng else "")
    for e in eng:
        print(f"  {e['channel']}/{e['thread_root']}")

    print("\nINTERVENTIONS (real nudges posted; shadow mode records none)")
    iv = db.execute("SELECT * FROM interventions ORDER BY created_at DESC LIMIT 10").fetchall()
    print("  (none)" if not iv else "")
    for i in iv:
        print(f"  {ago(i['created_at']):>10}  {i['channel']}/{i['thread_ts']} "
              f"[{i['kind']}] engaged={i['engaged']}")

    print("\nGATE OUTPUT")
    print("  The gate IS the cron job (--no-agent): it detects, judges and posts")
    print("  in one process. Its stdout is an EXCERPT-FREE audit line delivered")
    print("  to the ops channel, or {\"wakeAgent\": false} for a silent tick.")
    print("  Shadow digests: the ops channel, or ~/hermes/cron/output/.")
    stale_file = DATA / "candidates.json"
    if stale_file.exists():
        print(f"  !! stale pre-containment file present: {stale_file}")
        print("     it holds verbatim channel text; the next gate run or")
        print("     Hermes restart purges it (or delete it by hand now).")

    errs = DATA / "gate_errors.log"
    if errs.exists() and errs.stat().st_size:
        print(f"\n!! GATE ERRORS ({errs}):")
        print("   " + errs.read_text(encoding='utf-8', errors='replace').strip()[-800:])
    db.close()
    print("=" * 68)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kill", choices=["on", "off"])
    ap.add_argument("--mode", choices=["shadow", "live"])
    ap.add_argument("--prod", action="store_true", help="restore production thresholds")
    ap.add_argument("--reset-ledger", action="store_true")
    a = ap.parse_args()

    if a.kill:
        db = sqlite3.connect(str(DB_PATH))
        with db:
            db.execute("INSERT OR REPLACE INTO flags (key,value) VALUES ('kill_switch',?)",
                       ("1" if a.kill == "on" else "0",))
        db.close()
        print(f"kill switch -> {a.kill.upper()}")
    if a.mode:
        cfg = load_cfg(); cfg["mode"] = a.mode; save_cfg(cfg)
        print(f"mode -> {a.mode}")
    if a.prod:
        cfg = load_cfg(); cfg.update(PROD); save_cfg(cfg)
        print("thresholds -> PRODUCTION " + json.dumps(PROD))
    if a.reset_ledger:
        db = sqlite3.connect(str(DB_PATH))
        with db:
            for t in ("messages", "engaged_threads", "interventions", "judgments",
                      "shadow_seen", "budget_usage", "budget_alerts", "intents"):
                try:
                    db.execute(f"DELETE FROM {t}")
                except sqlite3.OperationalError:
                    pass  # table from an older/newer schema, or already gone
            try:
                # Arrival counters are ledger state too; the kill switch is NOT
                # (a reset must never silently re-arm a halted ambient mode).
                db.execute(
                    "DELETE FROM flags WHERE key IN"
                    " ('arrival_counters','arrival_reported','context_counters',"
                    "  'context_last','context_pins_scope')"
                )
            except sqlite3.OperationalError:
                pass
        db.close()
        (DATA / "candidates.json").unlink(missing_ok=True)
        (DATA / "arrival.log").unlink(missing_ok=True)
        print("ledger wiped (config kept; kill switch untouched)")

    if not any((a.kill, a.mode, a.prod, a.reset_ledger)):
        status()
    else:
        print()
        status()
    return 0


if __name__ == "__main__":
    sys.exit(main())
