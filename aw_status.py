#!/usr/bin/env python3
"""ambient-watch status — inspect the live ledger, config and gate state.

Usage:
    python aw_status.py                 # full status
    python aw_status.py --kill on|off   # flip the kill switch
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
from datetime import datetime
from pathlib import Path

PROD = {
    "unanswered_after_minutes": 45,
    "stalled_after_minutes": 240,
    "cooldown_minutes": 120,
    "quiet_start": "20:00",
    "quiet_end": "09:00",
}


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


def status():
    cfg = load_cfg()
    print("=" * 68)
    print("CONFIG")
    print(f"  mode              : {cfg.get('mode')}")
    print(f"  watched channels  : {cfg.get('channels')}")
    print(f"  ops channel       : {cfg.get('ops_channel')}")
    print(f"  unanswered after  : {cfg.get('unanswered_after_minutes', 45)} min")
    print(f"  stalled after     : {cfg.get('stalled_after_minutes', 240)} min")
    print(f"  channel cooldown  : {cfg.get('cooldown_minutes', 120)} min")
    print(f"  quiet hours       : {cfg.get('quiet_start','20:00')}-"
          f"{cfg.get('quiet_end','09:00')} {cfg.get('quiet_tz','UTC')}")
    is_prod = all(cfg.get(k) == v for k, v in PROD.items())
    print(f"  thresholds        : {'PRODUCTION' if is_prod else 'TEST (lowered)'}")

    if not DB_PATH.exists():
        print("\n(no ledger yet)")
        return
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row

    row = db.execute("SELECT value FROM flags WHERE key='kill_switch'").fetchone()
    print(f"  kill switch       : {'ON (ambient halted)' if row and row['value']=='1' else 'off'}")

    print("\nRECORDED MESSAGES")
    msgs = db.execute("SELECT * FROM messages ORDER BY CAST(ts AS REAL) DESC LIMIT 15").fetchall()
    if not msgs:
        print("  (none)")
    for m in msgs:
        tag = "MENTION" if m["is_mention"] else ("BOT" if m["is_bot"] else "plain ")
        thread = "" if m["ts"] == m["thread_root"] else f" (reply in {m['thread_root']})"
        print(f"  [{tag}] {ago(m['ts']):>10}  {m['author']}: {(m['text'] or '')[:70]!r}{thread}")

    print("\nENGAGED THREADS (bot is conversing — never nudged)")
    eng = db.execute("SELECT * FROM engaged_threads").fetchall()
    print("  (none)" if not eng else "")
    for e in eng:
        print(f"  {e['channel']}/{e['thread_root']}")

    print("\nINTERVENTIONS (nudges; shadow mode records none)")
    iv = db.execute("SELECT * FROM interventions ORDER BY created_at DESC LIMIT 10").fetchall()
    print("  (none)" if not iv else "")
    for i in iv:
        print(f"  {ago(i['created_at']):>10}  {i['channel']}/{i['thread_ts']} "
              f"[{i['kind']}] engaged={i['engaged']}")

    print("\nARMED INTENTS (tool-guard allowlist)")
    it = db.execute("SELECT * FROM intents ORDER BY created_at DESC LIMIT 10").fetchall()
    print("  (none — guard dormant)" if not it else "")
    for i in it:
        print(f"  {i['status']:>8}  {i['target']}  {ago(i['created_at'])}")

    cand = DATA / "candidates.json"
    print("\nLAST GATE OUTPUT")
    if cand.exists():
        c = json.loads(cand.read_text(encoding="utf-8"))
        print(f"  generated {ago(c['generated_at'])}, mode={c['mode']}, "
              f"{len(c['candidates'])} candidate(s)")
        for x in c["candidates"]:
            print(f"    [{x['kind']}] {x['target']}")
            print(f"       excerpt: {x['excerpt'][:90]!r}")
    else:
        print("  (gate has not produced candidates yet)")

    errs = DATA / "gate_errors.log"
    if errs.exists() and errs.stat().st_size:
        print(f"\n!! GATE ERRORS ({errs}):")
        print("   " + errs.read_text(encoding='utf-8', errors='replace').strip()[-500:])
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
            for t in ("messages", "engaged_threads", "interventions", "intents"):
                db.execute(f"DELETE FROM {t}")
        db.close()
        (DATA / "candidates.json").unlink(missing_ok=True)
        print("ledger wiped (config kept)")

    if not any((a.kill, a.mode, a.prod, a.reset_ledger)):
        status()
    else:
        print()
        status()
    return 0


if __name__ == "__main__":
    sys.exit(main())
