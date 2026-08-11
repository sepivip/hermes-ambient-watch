"""SQLite ledger for ambient-watch.

WAL mode, short transactions, one file under the sanctioned per-plugin
data dir (never the shared state.db). All timestamps are Slack ts strings
(epoch-seconds with fractional part) or bare epoch floats.
"""

from __future__ import annotations

import sqlite3
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
"""


class AmbientStore:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self):
        self._db.close()

    # -- messages ---------------------------------------------------------
    def record_message(self, channel, ts, thread_ts, author, is_bot, is_mention, text):
        root = thread_ts or ts
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO messages"
                " (channel, ts, thread_root, author, is_bot, is_mention, text, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (channel, ts, root, author, int(is_bot), int(is_mention), text, time.time()),
            )

    def messages_in_channel(self, channel):
        cur = self._db.execute(
            "SELECT * FROM messages WHERE channel=? ORDER BY CAST(ts AS REAL)", (channel,)
        )
        return [dict(r) for r in cur.fetchall()]

    def thread_roots(self, channel):
        cur = self._db.execute(
            "SELECT * FROM messages WHERE channel=? AND ts=thread_root"
            " ORDER BY CAST(ts AS REAL)",
            (channel,),
        )
        return [dict(r) for r in cur.fetchall()]

    def thread_messages(self, channel, root):
        cur = self._db.execute(
            "SELECT * FROM messages WHERE channel=? AND thread_root=?"
            " ORDER BY CAST(ts AS REAL)",
            (channel, root),
        )
        return [dict(r) for r in cur.fetchall()]

    # -- engagement (mention-driven thread following) ---------------------
    def mark_engaged(self, channel, thread_root):
        with self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO engaged_threads (channel, thread_root) VALUES (?,?)",
                (channel, thread_root),
            )

    def is_engaged(self, channel, thread_root) -> bool:
        cur = self._db.execute(
            "SELECT 1 FROM engaged_threads WHERE channel=? AND thread_root=?",
            (channel, thread_root),
        )
        return cur.fetchone() is not None

    # -- interventions ----------------------------------------------------
    def record_intervention(self, channel, thread_ts, kind, now=None):
        with self._db:
            self._db.execute(
                "INSERT INTO interventions (channel, thread_ts, kind, created_at)"
                " VALUES (?,?,?,?)",
                (channel, thread_ts, kind, now if now is not None else time.time()),
            )

    def record_engagement(self, channel, thread_ts):
        with self._db:
            self._db.execute(
                "UPDATE interventions SET engaged=1 WHERE channel=? AND thread_ts=?",
                (channel, thread_ts),
            )

    def has_intervention(self, channel, thread_ts) -> bool:
        cur = self._db.execute(
            "SELECT 1 FROM interventions WHERE channel=? AND thread_ts=? LIMIT 1",
            (channel, thread_ts),
        )
        return cur.fetchone() is not None

    def interventions_since(self, channel, since) -> int:
        cur = self._db.execute(
            "SELECT COUNT(*) c FROM interventions WHERE channel=? AND created_at>=?",
            (channel, since),
        )
        return cur.fetchone()["c"]

    def global_interventions_since(self, since) -> int:
        cur = self._db.execute(
            "SELECT COUNT(*) c FROM interventions WHERE created_at>=?", (since,)
        )
        return cur.fetchone()["c"]

    def last_intervention_at(self, channel):
        cur = self._db.execute(
            "SELECT MAX(created_at) m FROM interventions WHERE channel=?", (channel,)
        )
        row = cur.fetchone()
        return row["m"]

    def channel_self_quieted(self, channel, threshold) -> bool:
        cur = self._db.execute(
            "SELECT engaged FROM interventions WHERE channel=?"
            " ORDER BY created_at DESC LIMIT ?",
            (channel, threshold),
        )
        rows = cur.fetchall()
        if len(rows) < threshold:
            return False
        return all(r["engaged"] == 0 for r in rows)

    # -- mutes ------------------------------------------------------------
    def mute_thread(self, channel, thread_ts):
        with self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO muted (channel, thread_ts) VALUES (?,?)",
                (channel, thread_ts),
            )

    def is_muted(self, channel, thread_ts) -> bool:
        cur = self._db.execute(
            "SELECT 1 FROM muted WHERE channel=? AND thread_ts=?", (channel, thread_ts)
        )
        return cur.fetchone() is not None

    # -- intents (tool-guard arming) --------------------------------------
    def arm_intent(self, target, channel, thread_ts, now=None):
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO intents (target, channel, thread_ts, status, created_at)"
                " VALUES (?,?,?,'pending',?)",
                (target, channel, thread_ts, now if now is not None else time.time()),
            )

    def pending_intents(self):
        cur = self._db.execute(
            "SELECT target FROM intents WHERE status='pending' ORDER BY created_at"
        )
        return [r["target"] for r in cur.fetchall()]

    def any_intents(self) -> bool:
        cur = self._db.execute("SELECT 1 FROM intents LIMIT 1")
        return cur.fetchone() is not None

    def mark_intent_done(self, target):
        with self._db:
            self._db.execute(
                "UPDATE intents SET status='done' WHERE target=?", (target,)
            )

    # -- kill switch ------------------------------------------------------
    def set_kill_switch(self, on: bool):
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO flags (key, value) VALUES ('kill_switch', ?)",
                ("1" if on else "0",),
            )

    def kill_switch(self) -> bool:
        cur = self._db.execute("SELECT value FROM flags WHERE key='kill_switch'")
        row = cur.fetchone()
        return bool(row) and row["value"] == "1"
