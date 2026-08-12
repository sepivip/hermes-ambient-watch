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
-- Reaction-gated escalation audit. One row per thread handed to a
-- full-toolset Hermes session, recording WHICH HUMAN invoked it. The human
-- reaction is the entire security control, so who clicked is the single most
-- important fact to keep.
CREATE TABLE IF NOT EXISTS escalations (
    channel    TEXT NOT NULL,
    thread_ts  TEXT NOT NULL,
    reactor    TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (channel, thread_ts)
);
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
            self._migrate()
            self._db.commit()

    def _migrate(self):
        """Additive column migrations for already-deployed databases.

        CREATE TABLE IF NOT EXISTS never adds a column to a table that already
        exists, so a live ambient.db keeps its original interventions schema.
        Each step is idempotent and tolerates the column already being there.
        """
        for table, column, decl in (
            ("interventions", "nudge_ts", "TEXT"),
        ):
            cols = {
                r["name"]
                for r in self._db.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if column not in cols:
                self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

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

    # -- context fidelity (aw_context) ------------------------------------
    # Both queries below are deliberately NARROW. ``messages_in_channel()``
    # would answer them and must not be used: it returns every row in the
    # channel, text included, while holding this connection's single RLock —
    # the same lock the recorder needs on the GATEWAY LOOP THREAD. Same
    # reasoning that already produced ``thread_root()`` next to
    # ``thread_roots()``.
    #
    # ORDER BY ts, not CAST(ts AS REAL): the cast defeats the (channel, ts)
    # primary-key index. Slack ts strings are fixed-width zero-padded, so
    # lexicographic order equals numeric order until the epoch grows an 11th
    # digit (year 2286). ``since`` is formatted to the same width for the same
    # reason — and because comparing a TEXT column against a bare float in
    # SQLite is always true (NULL < numbers < TEXT), i.e. a silently absent
    # filter.
    def recent_channel_messages(self, channel, limit, since=None):
        """The newest ``limit`` rows in a channel, newest LAST. Bounded."""
        limit = max(0, int(limit))
        if not limit:
            return []
        sql = "SELECT * FROM messages WHERE channel=?"
        args = [channel]
        if since is not None:
            sql += " AND ts>=?"
            args.append(f"{float(since):.6f}")
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = [dict(r) for r in self._db.execute(sql, args).fetchall()]
        rows.reverse()
        return rows

    def explain_recent_channel_messages(self, channel) -> str:
        """The query plan for the above. Exists so a test can PROVE the index
        is used rather than asserting it in a comment."""
        with self._lock:
            rows = self._db.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM messages WHERE channel=?"
                " AND ts>=? ORDER BY ts DESC LIMIT ?",
                (channel, "0", 1),
            ).fetchall()
        return " | ".join(str(r["detail"]) for r in rows)

    def channel_first_ts(self, channel):
        """"When did we start watching this channel", derived not stored.

        NOT LOAD-BEARING, and deliberately kept anyway. `aw_context` used to
        compare a candidate's root against this to decide "the thread began
        before the ledger existed, so backfill it" — a comparison that can never
        be true, because a rooted candidate's own root row is one of the rows
        this MIN runs over. The enricher no longer consults it (see
        `_thread_section`); it remains as the honest primitive for anyone who
        later implements gap repair with a real signal and an explicit budget.
        Derived, so it needs no migration and cannot drift.
        """
        with self._lock:
            row = self._db.execute(
                "SELECT MIN(ts) m FROM messages WHERE channel=?", (channel,)
            ).fetchone()
        try:
            return float(row["m"])
        except (TypeError, ValueError):
            return None

    def orphan_threads(self, channel):
        """Threads with at least one HUMAN message and NO root row.

        The structural blind spot this closes: ``thread_roots()`` selects
        ``WHERE ts=thread_root``, so a thread whose root predates the plugin —
        or whose root was deleted by retention while replies continued — was
        never nominated by either trigger. Synthetic root rows are returned so
        the shared eligibility ladder can run unchanged; ``is_bot`` is 0 because
        it is UNKNOWN here, and the enricher re-establishes the real answer from
        ``conversations.replies`` before anything can be judged.
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT thread_root, MIN(ts) first_ts FROM messages m"
                " WHERE channel=? AND NOT EXISTS ("
                "   SELECT 1 FROM messages r WHERE r.channel=m.channel"
                "   AND r.ts=m.thread_root)"
                " GROUP BY thread_root"
                " HAVING SUM(CASE WHEN is_bot=0 THEN 1 ELSE 0 END) > 0"
                " ORDER BY thread_root",
                (channel,),
            ).fetchall()
        return [
            {
                "channel": channel,
                "ts": r["thread_root"],
                "thread_root": r["thread_root"],
                "author": None,
                "is_bot": 0,
                "is_mention": 0,
                "text": "",
                "created_at": 0.0,
                "root_missing": True,
            }
            for r in rows
        ]

    def orphan_thread(self, channel, root):
        """The narrow form of ``orphan_threads`` — one thread, by key.

        Narrow SQL rather than a filtered scan of ``orphan_threads``, for the
        same reason ``thread_root`` exists: the arrival path considers exactly
        one thread and must not walk the channel while holding this lock.
        """
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) n,"
                " SUM(CASE WHEN is_bot=0 THEN 1 ELSE 0 END) humans"
                " FROM messages WHERE channel=? AND thread_root=?",
                (channel, root),
            ).fetchone()
            if not row or not row["n"] or not (row["humans"] or 0):
                return None
            has_root = self._db.execute(
                "SELECT 1 FROM messages WHERE channel=? AND ts=?", (channel, root)
            ).fetchone()
        if has_root:
            return None
        return {
            "channel": channel, "ts": root, "thread_root": root, "author": None,
            "is_bot": 0, "is_mention": 0, "text": "", "created_at": 0.0,
            "root_missing": True,
        }

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
    def record_intervention(self, channel, thread_ts, kind, now=None, nudge_ts=None):
        """Record a posted nudge.

        ``nudge_ts`` is the ts Slack assigned to OUR message. It is the anchor
        reaction-gated escalation compares against: a reaction only counts as a
        human invocation when it lands on a message we actually posted, so
        "some message in this thread" is not good enough.
        """
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO interventions (channel, thread_ts, kind, created_at, nudge_ts)"
                " VALUES (?,?,?,?,?)",
                (channel, thread_ts, kind,
                 now if now is not None else time.time(), nudge_ts),
            )

    def nudge_ts_matches(self, channel, thread_ts, ts) -> bool:
        """True when *ts* is the ts of a nudge WE posted in this thread."""
        if not ts:
            return False
        with self._lock:
            cur = self._db.execute(
                "SELECT 1 FROM interventions"
                " WHERE channel=? AND thread_ts=? AND nudge_ts=? LIMIT 1",
                (channel, thread_ts, str(ts)),
            )
            return cur.fetchone() is not None

    # -- escalation ledger (reaction-gated; see aw_escalate) ---------------
    def record_escalation(self, channel, thread_ts, reactor, now=None):
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO escalations"
                " (channel, thread_ts, reactor, created_at) VALUES (?,?,?,?)",
                (channel, thread_ts, reactor,
                 now if now is not None else time.time()),
            )

    def has_escalation(self, channel, thread_ts) -> bool:
        with self._lock:
            cur = self._db.execute(
                "SELECT 1 FROM escalations WHERE channel=? AND thread_ts=?",
                (channel, thread_ts),
            )
            return cur.fetchone() is not None

    def escalations_since(self, since) -> int:
        with self._lock:
            cur = self._db.execute(
                "SELECT COUNT(*) c FROM escalations WHERE created_at>=?", (since,)
            )
            return cur.fetchone()["c"]

    def recent_escalations(self, limit=10):
        with self._lock:
            cur = self._db.execute(
                "SELECT * FROM escalations ORDER BY created_at DESC LIMIT ?", (limit,)
            )
            return [dict(r) for r in cur.fetchall()]

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
            # KEEP A ROOT WHOSE THREAD IS STILL ALIVE. Deleting the ts=thread_root
            # row while replies continue inside the window used to MANUFACTURE a
            # rootless thread every retention period: `thread_roots()` selects
            # WHERE ts=thread_root, so the thread became invisible to both
            # triggers forever. This is the cheap half of the backfill fix, and it
            # is what keeps steady-state conversations.replies volume near zero.
            removed = self._db.execute(
                "DELETE FROM messages WHERE CAST(ts AS REAL) < ?"
                " AND NOT (ts = thread_root AND EXISTS ("
                "   SELECT 1 FROM messages m2 WHERE m2.channel = messages.channel"
                "   AND m2.thread_root = messages.thread_root"
                "   AND CAST(m2.ts AS REAL) >= ?))",
                (cutoff, cutoff),
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

    #: Cumulative context-fidelity counters (fetches, failures, 429s, cache
    #: hits). Numbers only, by construction — same rule as the arrival
    #: counters: this is an ops surface, so it must be structurally incapable
    #: of carrying channel text.
    _CONTEXT_COUNTER_KEY = "context_counters"

    def context_counters(self) -> dict:
        return self._json_flag(self._CONTEXT_COUNTER_KEY)

    def bump_context_counters(self, now=None, **deltas) -> dict:
        return self._bump_json_counters(self._CONTEXT_COUNTER_KEY, now, deltas)

    def bump_arrival_counters(self, now=None, **deltas) -> dict:
        """Read-modify-write the arrival counters. Never raises.

        Cross-process this RMW can lose an increment if the sweep writes the
        same key in the same instant. The blast radius is one ops line being
        off by one, which is why it is not worth a cross-process lock on the
        gateway's event loop.
        """
        return self._bump_json_counters(self._ARRIVAL_COUNTER_KEY, now, deltas)

    def _bump_json_counters(self, key: str, now, deltas: dict) -> dict:
        with self._lock, self._db:
            cur = self._db.execute(
                "SELECT value FROM flags WHERE key=?", (key,)
            )
            row = cur.fetchone()
            data = {}
            if row:
                try:
                    parsed = json.loads(row["value"])
                    data = parsed if isinstance(parsed, dict) else {}
                except ValueError:
                    data = {}
            for name, delta in deltas.items():
                try:
                    data[name] = (data.get(name) or 0) + delta
                except TypeError:
                    data[name] = delta
            data["updated_at"] = time.time() if now is None else float(now)
            self._db.execute(
                "INSERT OR REPLACE INTO flags (key, value) VALUES (?,?)",
                (key, json.dumps(data)),
            )
            return dict(data)
