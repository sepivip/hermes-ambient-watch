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

    # -- kill switch ------------------------------------------------------
    def set_kill_switch(self, on: bool):
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO flags (key, value) VALUES ('kill_switch', ?)",
                ("1" if on else "0",),
            )

    def kill_switch(self) -> bool:
        with self._lock:
            cur = self._db.execute("SELECT value FROM flags WHERE key='kill_switch'")
            row = cur.fetchone()
            return bool(row) and row["value"] == "1"
