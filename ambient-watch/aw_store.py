"""SQLite ledger for ambient-watch.

Thread-safety (adversarial-review finding): the recorder runs on the
gateway loop thread while pre_tool_call fires in tool-executor worker
threads — one connection, so ``check_same_thread=False`` plus an RLock
around every touch of the connection (Python's sqlite3 does not
serialize cross-thread use of a single connection by itself).

WAL mode, busy_timeout for the cron process's separate connection, one
file under the sanctioned per-plugin data dir (never the shared
state.db). Timestamps are Slack ts strings or bare epoch floats.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    channel     TEXT NOT NULL,
    ts          TEXT NOT NULL,
    thread_root TEXT NOT NULL,
    author      TEXT,
    is_bot      INTEGER NOT NULL DEFAULT 0,
    is_mention  INTEGER NOT NULL DEFAULT 0,
    text        TEXT,
    created_at  REAL NOT NULL,
    PRIMARY KEY (channel, ts)
);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages (channel, thread_root);
CREATE TABLE IF NOT EXISTS engaged_threads (
    channel    TEXT NOT NULL,
    thread_root TEXT NOT NULL,
    PRIMARY KEY (channel, thread_root)
);
CREATE TABLE IF NOT EXISTS interventions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    channel    TEXT NOT NULL,
    thread_ts  TEXT NOT NULL,
    kind       TEXT NOT NULL,
    created_at REAL NOT NULL,
    engaged    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS muted (
    channel   TEXT NOT NULL,
    thread_ts TEXT NOT NULL,
    PRIMARY KEY (channel, thread_ts)
);
-- NOTE: an `intents` table existed here to feed aw_guard's send_message
-- target pinning. Both are gone: no cron agent can call send_message
-- (cron/scheduler.py:182), the gate posts directly, and the outbound
-- invariant now lives in aw_post.post_nudge where it can actually fire. A
-- deployed database keeps the unused table; nothing reads or writes it.
CREATE TABLE IF NOT EXISTS flags (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- Shadow-mode "already reported" ledger. Kept separate from interventions
-- so a shadow digest never consumes a thread's real nudge budget, while
-- still preventing the same candidate being re-digested every sweep.
CREATE TABLE IF NOT EXISTS shadow_seen (
    channel    TEXT NOT NULL,
    thread_ts  TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (channel, thread_ts)
);
-- Judge outcomes, one row per thread. `last_activity_seen` is the re-judge
-- WATERMARK and it is what makes the deleted cooldown unnecessary: a thread
-- the judge declined is not judged again until a NEW human message arrives,
-- so a thread cannot cost repeated LLM calls merely by continuing to exist.
-- `judge_count` bounds even that (cfg.judge_max_rejudge).
CREATE TABLE IF NOT EXISTS judgments (
    channel            TEXT NOT NULL,
    thread_ts          TEXT NOT NULL,
    verdict            TEXT NOT NULL,
    confidence         REAL NOT NULL DEFAULT 0,
    reason             TEXT,
    nudge              TEXT,
    excerpt            TEXT,
    last_activity_seen REAL NOT NULL DEFAULT 0,
    judge_count        INTEGER NOT NULL DEFAULT 0,
    created_at         REAL NOT NULL,
    updated_at         REAL NOT NULL,
    PRIMARY KEY (channel, thread_ts)
);
"""


class AmbientStore:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.executescript(_SCHEMA)
            self._db.commit()

    def close(self):
        with self._lock:
            self._db.close()

    # -- messages ---------------------------------------------------------
    def record_message(self, channel, ts, thread_ts, author, is_bot, is_mention, text):
        root = thread_ts or ts
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO messages"
                " (channel, ts, thread_root, author, is_bot, is_mention, text, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (channel, ts, root, author, int(is_bot), int(is_mention), text, time.time()),
            )

    def messages_in_channel(self, channel):
        with self._lock:
            cur = self._db.execute(
                "SELECT * FROM messages WHERE channel=? ORDER BY CAST(ts AS REAL)",
                (channel,),
            )
            return [dict(r) for r in cur.fetchall()]

    def thread_roots(self, channel):
        with self._lock:
            cur = self._db.execute(
                "SELECT * FROM messages WHERE channel=? AND ts=thread_root"
                " ORDER BY CAST(ts AS REAL)",
                (channel,),
            )
            return [dict(r) for r in cur.fetchall()]

    def thread_root(self, channel, ts):
        """One thread's ROOT row, or None — the narrow form of thread_roots().

        Exists for the arrival path. ``thread_roots`` returns every root in the
        channel WITH its text, and the arrival ladder considers exactly one
        thread, so filtering that scan in Python would make each arrival
        attempt O(roots in the channel) while holding this connection's RLock —
        the same RLock the recorder needs on the gateway loop thread. This hits
        the (channel, ts) primary key instead.
        """
        with self._lock:
            cur = self._db.execute(
                "SELECT * FROM messages WHERE channel=? AND ts=? AND ts=thread_root",
                (channel, ts),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def thread_messages(self, channel, root):
        with self._lock:
            cur = self._db.execute(
                "SELECT * FROM messages WHERE channel=? AND thread_root=?"
                " ORDER BY CAST(ts AS REAL)",
                (channel, root),
            )
            return [dict(r) for r in cur.fetchall()]

    # -- engagement (mention/nudge-driven thread following) ---------------
    def mark_engaged(self, channel, thread_root):
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO engaged_threads (channel, thread_root) VALUES (?,?)",
                (channel, thread_root),
            )

    def is_engaged(self, channel, thread_root) -> bool:
        with self._lock:
            cur = self._db.execute(
                "SELECT 1 FROM engaged_threads WHERE channel=? AND thread_root=?",
                (channel, thread_root),
            )
            return cur.fetchone() is not None

    # -- interventions ----------------------------------------------------
    def record_intervention(self, channel, thread_ts, kind, now=None):
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO interventions (channel, thread_ts, kind, created_at)"
                " VALUES (?,?,?,?)",
                (channel, thread_ts, kind, now if now is not None else time.time()),
            )

    def record_engagement(self, channel, thread_ts):
        with self._lock, self._db:
            self._db.execute(
                "UPDATE interventions SET engaged=1 WHERE channel=? AND thread_ts=?",
                (channel, thread_ts),
            )

    def has_intervention(self, channel, thread_ts) -> bool:
        with self._lock:
            cur = self._db.execute(
                "SELECT 1 FROM interventions WHERE channel=? AND thread_ts=? LIMIT 1",
                (channel, thread_ts),
            )
            return cur.fetchone() is not None

    def interventions_since(self, channel, since) -> int:
        with self._lock:
            cur = self._db.execute(
                "SELECT COUNT(*) c FROM interventions WHERE channel=? AND created_at>=?",
                (channel, since),
            )
            return cur.fetchone()["c"]

    def global_interventions_since(self, since) -> int:
        with self._lock:
            cur = self._db.execute(
                "SELECT COUNT(*) c FROM interventions WHERE created_at>=?", (since,)
            )
            return cur.fetchone()["c"]

    def last_intervention_at(self, channel):
        with self._lock:
            cur = self._db.execute(
                "SELECT MAX(created_at) m FROM interventions WHERE channel=?", (channel,)
            )
            return cur.fetchone()["m"]

    def channel_self_quieted(self, channel, threshold) -> bool:
        with self._lock:
            cur = self._db.execute(
                "SELECT engaged FROM interventions WHERE channel=?"
                " ORDER BY created_at DESC LIMIT ?",
                (channel, threshold),
            )
            rows = cur.fetchall()
        if len(rows) < threshold:
            return False
        return all(r["engaged"] == 0 for r in rows)

    # -- shadow-mode dedupe -----------------------------------------------
    def mark_shadow_seen(self, channel, thread_ts, now=None):
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO shadow_seen (channel, thread_ts, created_at)"
                " VALUES (?,?,?)",
                (channel, thread_ts, now if now is not None else time.time()),
            )

    def is_shadow_seen(self, channel, thread_ts) -> bool:
        with self._lock:
            cur = self._db.execute(
                "SELECT 1 FROM shadow_seen WHERE channel=? AND thread_ts=?",
                (channel, thread_ts),
            )
            return cur.fetchone() is not None

    # Shadow analogues of the intervention counters, so a soak reproduces the
    # exact digest volume live mode would post (see test_shadow_simulates_caps).
    def shadow_seen_since(self, channel, since) -> int:
        with self._lock:
            cur = self._db.execute(
                "SELECT COUNT(*) c FROM shadow_seen WHERE channel=? AND created_at>=?",
                (channel, since),
            )
            return cur.fetchone()["c"]

    def global_shadow_seen_since(self, since) -> int:
        with self._lock:
            cur = self._db.execute(
                "SELECT COUNT(*) c FROM shadow_seen WHERE created_at>=?", (since,)
            )
            return cur.fetchone()["c"]

    def last_shadow_seen_at(self, channel):
        with self._lock:
            cur = self._db.execute(
                "SELECT MAX(created_at) m FROM shadow_seen WHERE channel=?", (channel,)
            )
            return cur.fetchone()["m"]

    # -- judgments (model verdicts + the re-judge watermark) --------------
    def record_judgment(
        self, channel, thread_ts, verdict, *, confidence=0.0, reason="",
        nudge="", excerpt="", last_activity_seen=0.0, now=None,
    ):
        now = time.time() if now is None else now
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO judgments (channel, thread_ts, verdict, confidence,"
                " reason, nudge, excerpt, last_activity_seen, judge_count,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,1,?,?)"
                " ON CONFLICT(channel, thread_ts) DO UPDATE SET"
                "   verdict=excluded.verdict,"
                "   confidence=excluded.confidence,"
                "   reason=excluded.reason,"
                "   nudge=excluded.nudge,"
                "   excerpt=excluded.excerpt,"
                "   last_activity_seen=MAX(judgments.last_activity_seen,"
                "                          excluded.last_activity_seen),"
                "   judge_count=judgments.judge_count + 1,"
                "   updated_at=excluded.updated_at",
                (channel, thread_ts, verdict, float(confidence), reason, nudge,
                 excerpt, float(last_activity_seen), now, now),
            )

    def record_decline(self, channel, thread_ts, verdict, *, excerpt="", now=None):
        """Note a candidate that was DECLINED without being judged.

        A decline is not a verdict: no model saw the thread and no tokens were
        spent, so it must not consume the re-judge watermark or the
        ``judge_count``. Recording it as a judgment would permanently retire
        the thread — the spend cap resets the next day (and a misconfigured
        budget gets fixed by an operator) but the thread would never be looked
        at again, which is the "ambient silently does nothing" failure mode
        this plugin fears most.
        """
        now = time.time() if now is None else now
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO judgments (channel, thread_ts, verdict, confidence,"
                " reason, nudge, excerpt, last_activity_seen, judge_count,"
                " created_at, updated_at) VALUES (?,?,?,0,'','',?,0,0,?,?)"
                " ON CONFLICT(channel, thread_ts) DO UPDATE SET"
                "   verdict=excluded.verdict,"
                "   excerpt=excluded.excerpt,"
                "   updated_at=excluded.updated_at",
                (channel, thread_ts, verdict, excerpt, now, now),
            )

    def judgment(self, channel, thread_ts):
        with self._lock:
            cur = self._db.execute(
                "SELECT * FROM judgments WHERE channel=? AND thread_ts=?",
                (channel, thread_ts),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def needs_judgment(self, channel, thread_ts, last_activity, max_rejudge) -> bool:
        """True when this thread may be handed to the judge (again).

        This is the control that replaced the cooldown. A thread already
        judged is eligible only if a human said something NEW since, and only
        up to ``max_rejudge`` extra times — so the judge's cost is bounded by
        conversation, not by the sweep cadence.
        """
        row = self.judgment(channel, thread_ts)
        if row is None:
            return True
        if float(last_activity) <= float(row["last_activity_seen"]):
            return False  # nothing new since the last verdict
        return int(row["judge_count"]) <= int(max_rejudge)

    def recent_judgments(self, limit=10):
        with self._lock:
            cur = self._db.execute(
                "SELECT * FROM judgments ORDER BY updated_at DESC LIMIT ?", (limit,)
            )
            return [dict(r) for r in cur.fetchall()]

    # -- mutes ------------------------------------------------------------
    def mute_thread(self, channel, thread_ts):
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO muted (channel, thread_ts) VALUES (?,?)",
                (channel, thread_ts),
            )

    def is_muted(self, channel, thread_ts) -> bool:
        with self._lock:
            cur = self._db.execute(
                "SELECT 1 FROM muted WHERE channel=? AND thread_ts=?",
                (channel, thread_ts),
            )
            return cur.fetchone() is not None

    def unmute_thread(self, channel, thread_ts):
        with self._lock, self._db:
            self._db.execute(
                "DELETE FROM muted WHERE channel=? AND thread_ts=?",
                (channel, thread_ts),
            )

    # A channel-wide mute is stored as the sentinel thread_ts '*'.
    _CHANNEL_MUTE = "*"

    def mute_channel(self, channel):
        self.mute_thread(channel, self._CHANNEL_MUTE)

    def unmute_channel(self, channel):
        self.unmute_thread(channel, self._CHANNEL_MUTE)

    def is_channel_muted(self, channel) -> bool:
        return self.is_muted(channel, self._CHANNEL_MUTE)

    # -- retention --------------------------------------------------------
    def prune(self, now: float, retention_days: int) -> int:
        """Delete expired message bodies and stale bookkeeping rows."""
        cutoff = now - retention_days * 86400
        with self._lock, self._db:
            removed = self._db.execute(
                "DELETE FROM messages WHERE CAST(ts AS REAL) < ?", (cutoff,)
            ).rowcount
            # Judgments of threads whose messages are gone carry no signal,
            # but keep them while the thread is still in the ledger: the row
            # is the once-per-thread + watermark memory.
            removed += self._db.execute(
                "DELETE FROM judgments WHERE updated_at < ?", (cutoff,)
            ).rowcount
            removed += self._db.execute(
                "DELETE FROM engaged_threads WHERE NOT EXISTS ("
                " SELECT 1 FROM messages m WHERE m.channel=engaged_threads.channel"
                " AND m.thread_root=engaged_threads.thread_root)"
            ).rowcount
            return removed

    # -- flags (generic key/value; the kill switch is one of them) --------
    def set_flag(self, key: str, value):
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO flags (key, value) VALUES (?,?)",
                (str(key), str(value)),
            )

    def get_flag(self, key: str, default=None):
        with self._lock:
            cur = self._db.execute(
                "SELECT value FROM flags WHERE key=?", (str(key),)
            )
            row = cur.fetchone()
            return row["value"] if row else default

    # -- kill switch ------------------------------------------------------
    def set_kill_switch(self, on: bool):
        self.set_flag("kill_switch", "1" if on else "0")

    def kill_switch(self) -> bool:
        return self.get_flag("kill_switch", "0") == "1"

    #: Returned by ``kill_switch_nowait`` when the connection lock was busy.
    LOCK_BUSY = object()

    def kill_switch_nowait(self, default=LOCK_BUSY):
        """Read the kill switch WITHOUT ever waiting for the connection lock.

        This exists for exactly one caller: the arrival path's Tier A, which
        runs on the GATEWAY EVENT LOOP THREAD inside ``pre_gateway_dispatch``.
        Blocking there is not acceptable — a worker thread (an
        ``asyncio.to_thread`` DB write from the pump, or a tool-executor call)
        can hold this RLock for up to ``busy_timeout=5000`` ms while contending
        with the sweep process, which would stall dispatch for every message on
        every platform for five seconds.

        Returns ``default`` (``LOCK_BUSY`` by default) rather than waiting, so
        the caller keeps whatever answer it already had. That is safe because
        Tier A is only an optimisation: the pump re-reads the switch FRESH,
        off-thread, immediately before anything can be spent.
        """
        if not self._lock.acquire(blocking=False):
            return default
        try:
            cur = self._db.execute("SELECT value FROM flags WHERE key='kill_switch'")
            row = cur.fetchone()
            return bool(row) and row["value"] == "1"
        finally:
            self._lock.release()

    # -- arrival-mode counters --------------------------------------------
    # Judgments happen in the long-lived GATEWAY process; reporting happens on
    # the sweep's tick, in a different process. So arrival activity needs a
    # durable counter rather than an in-memory one — and it has to be a
    # counter, not a scan of `judgments`, because no row records WHICH trigger
    # produced it and a scan would re-report the sweep's own work as arrival
    # activity.
    #
    # Numbers only, by construction: this is the ops surface, so it must be
    # structurally incapable of carrying channel text.
    _ARRIVAL_COUNTER_KEY = "arrival_counters"
    _ARRIVAL_REPORTED_KEY = "arrival_reported"
    ARRIVAL_COUNTERS = (
        "judged", "posted", "withheld", "declined", "throttled", "shadow",
        "errors", "post_failed",
    )

    def _json_flag(self, key: str) -> dict:
        raw = self.get_flag(key, "")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def arrival_counters(self) -> dict:
        return self._json_flag(self._ARRIVAL_COUNTER_KEY)

    def arrival_reported(self) -> dict:
        return self._json_flag(self._ARRIVAL_REPORTED_KEY)

    def set_arrival_reported(self, counters: dict):
        self.set_flag(self._ARRIVAL_REPORTED_KEY, json.dumps(dict(counters or {})))

    def bump_arrival_counters(self, now=None, **deltas) -> dict:
        """Read-modify-write the arrival counters. Never raises.

        Cross-process this RMW can lose an increment if the sweep writes the
        same key in the same instant. The blast radius is one ops line being
        off by one, which is why it is not worth a cross-process lock on the
        gateway's event loop.
        """
        with self._lock, self._db:
            cur = self._db.execute(
                "SELECT value FROM flags WHERE key=?", (self._ARRIVAL_COUNTER_KEY,)
            )
            row = cur.fetchone()
            data = {}
            if row:
                try:
                    parsed = json.loads(row["value"])
                    data = parsed if isinstance(parsed, dict) else {}
                except ValueError:
                    data = {}
            for key, delta in deltas.items():
                try:
                    data[key] = (data.get(key) or 0) + delta
                except TypeError:
                    data[key] = delta
            data["updated_at"] = time.time() if now is None else float(now)
            self._db.execute(
                "INSERT OR REPLACE INTO flags (key, value) VALUES (?,?)",
                (self._ARRIVAL_COUNTER_KEY, json.dumps(data)),
            )
            return dict(data)
