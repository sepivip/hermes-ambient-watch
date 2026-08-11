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
CREATE TABLE IF NOT EXISTS intents (
    target     TEXT PRIMARY KEY,
    channel    TEXT NOT NULL,
    thread_ts  TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL
);
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

    # -- intents (tool-guard arming) --------------------------------------
    def arm_intent(self, target, channel, thread_ts, now=None):
        # target may arrive platform-prefixed from gate candidates; store bare.
        ref = target.partition(":")[2] if target.startswith("slack:") else target
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO intents (target, channel, thread_ts, status, created_at)"
                " VALUES (?,?,?,'pending',?)",
                (ref, channel, thread_ts, now if now is not None else time.time()),
            )

    def pending_intents(self):
        with self._lock:
            cur = self._db.execute(
                "SELECT target FROM intents WHERE status='pending' ORDER BY created_at"
            )
            return [r["target"] for r in cur.fetchall()]

    def any_intents(self) -> bool:
        with self._lock:
            cur = self._db.execute("SELECT 1 FROM intents LIMIT 1")
            return cur.fetchone() is not None

    def mark_intent_done(self, target):
        with self._lock, self._db:
            self._db.execute(
                "UPDATE intents SET status='done' WHERE target=?", (target,)
            )

    def expire_stale_intents(self, now: float, ttl_seconds: float) -> int:
        with self._lock, self._db:
            cur = self._db.execute(
                "UPDATE intents SET status='expired'"
                " WHERE status='pending' AND created_at < ?",
                (now - ttl_seconds,),
            )
            return cur.rowcount

    # -- retention --------------------------------------------------------
    def prune(self, now: float, retention_days: int) -> int:
        """Delete expired message bodies and stale bookkeeping rows."""
        cutoff = now - retention_days * 86400
        with self._lock, self._db:
            removed = self._db.execute(
                "DELETE FROM messages WHERE CAST(ts AS REAL) < ?", (cutoff,)
            ).rowcount
            removed += self._db.execute(
                "DELETE FROM intents WHERE status != 'pending' AND created_at < ?",
                (cutoff,),
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
