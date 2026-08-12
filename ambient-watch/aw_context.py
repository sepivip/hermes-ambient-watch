"""P1 — context fidelity. What the judge is allowed to see.

THE GAP THIS CLOSES. Anthropic's spec says every Claude Tag session "reads its
own thread and the channel's history, including pinned items". Ours saw only
what our own sqlite ledger recorded since the plugin was installed: a short
sanitized excerpt from a handful of messages in one thread. It had never seen
channel history, never seen a pinned message, and could not see anything said
before the plugin started running.

FOUR SECTIONS, ONE FIXED PRIORITY ORDER, ONE CHARACTER CEILING. Ranked by
value-per-byte, which is the only ranking that matters when every character is
metered in USD:

1. ``[THIS THREAD]`` — the thread itself, from the ledger, backfilled from
   ``conversations.replies`` when the ledger cannot answer.
2. ``[CHANNEL]`` — name, topic, purpose (<=200 chars, one ``conversations.info``
   per channel per TTL). Highest value per byte in the design: it is the prior
   for "should an uninvited bot speak here at all". #incident-response and
   #watercooler produce opposite correct answers to identical text.
3. ``[RECENT CHANNEL ACTIVITY]`` — <=6 messages. Its job is not "ambient
   awareness" in the abstract, it is one specific false positive: *someone
   already answered in-channel instead of in-thread*, which
   ``answered-since-detection`` cannot see because it only looks inside the
   thread. Sourced from the LEDGER first and fetched only when the ledger holds
   fewer than ``context_channel_messages`` rows in the window. BUDGET WARNING,
   measured rather than assumed: a QUIET channel is below that threshold almost
   by definition, so the ledger-first rule saves the call in a BUSY channel and
   costs one ``conversations.history`` per judgment in a quiet one. Do not read
   it as "zero calls in steady state" — it is "zero calls where the answer was
   already free".
4. ``[PINNED]`` — OPTIONAL, OFF, scope-gated. ``pins:read`` is not granted on
   this install, so it costs a manifest edit plus a human reinstall.

THE ROOTLESS-THREAD FIX IS A CORRECTNESS FIX, NOT AN ENRICHMENT.
``store.thread_roots()`` selects ``WHERE ts=thread_root``, so a thread whose
root predates the plugin — or whose root was deleted by retention while replies
continued — was **structurally invisible to BOTH triggers, forever**. Not
"judged with poor context": never nominated at all. ``aw_detectors`` now admits
such a thread (gated on ``context_enabled``), and the rung that would be lost —
``if root["is_bot"]: continue``, the anti-feedback-loop rule — is re-established
here from the AUTHORITATIVE source: ``conversations.replies`` returns the root
with ``bot_id``/``subtype``, so a bot-authored root is dropped before the call.
**If the backfill fails the nominee is dropped, not judged.** That is why the
relaxation and the backfill sit behind ONE boolean.

WHERE IT SITS IN THE LADDER, and why that makes the rate-limit argument trivial:

    prefilter (ZERO network) -> budget -> take token -> ENRICH (0-4 fetches) -> judge

Steady state is ZERO fetches (a ledger-complete thread, a cached topic, enough
ledger rows for the channel window) and the DEFAULT ceiling is 2 — a thread
backfill plus a thin-ledger ``conversations.history``. The absolute worst case is
4: those two plus a cold ``conversations.info`` and, only if an operator turned
pins on, ``pins.list``. All of it inside ONE ``context_total_timeout_seconds``
budget for the whole enrichment, so the LATENCY bound does not grow with the
count.

Placing enrichment after ``TokenBuckets.take`` gives the invariant **at most one
enrichment per judgment**, so Slack call volume inherits the token buckets and
the USD caps exactly, and channel traffic appears in neither bound. It costs one
thing: a failed root backfill burns a bucket token without spending money, which
is the conservative direction (the buckets meter attempts by design).

CONTAINMENT. Every string fetched here is untrusted and is neutralized INSIDE
``SlackReader``, before it is assigned to anything a caller can read — there is
no code path on which a raw Slack body escapes this module, so a future caller
cannot forget. Nothing fetched is ever written to disk: ``ContextCache`` is a
process-local dict, deliberately NOT a ``flags`` row, because persisting it
would create a new permanent copy of untrusted text — the exact category the
2026-08-11 incident came from. The only durable writes are counters and
character counts (numbers, plus our own section labels).

BOTH TRIGGERS, ONE IMPLEMENTATION. ``enrich_for_judgment`` takes a LIST, so the
sweep (<=3 nominees, inline in a subprocess) and the arrival pump (1 nominee,
inside ``asyncio.to_thread``) share it. It never raises: every failure is
degradation to "judge with less context".
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

try:  # real loader: package-relative
    from . import aw_post, aw_sanitize
except ImportError:  # cron shim / bare script: flat import
    import aw_post
    import aw_sanitize

logger = logging.getLogger("ambient_watch")

_API_BASE = "https://slack.com/api/"

#: Rows requested from Slack. Both are ABOVE the prompt caps on purpose: bots
#: and join noise are filtered after the fetch, so asking for a few extra rows
#: is what keeps a CI-heavy channel from yielding an empty window. Neither
#: number can affect the prompt size, which the assembly ceiling fixes.
FETCH_REPLY_LIMIT = 30
FETCH_HISTORY_EXTRA = 8

#: Slack's own rate-limit signals. Tier 3 (~50/min) for conversations.history;
#: at our ceiling of ~58 judgments/day we use about four orders of magnitude
#: less, so a 429 means something else on the token — one bounded retry, then
#: degrade.
_RATE_LIMIT_ERRORS = ("ratelimited", "rate_limited", "http_429")
_MAX_RETRY_AFTER_SECONDS = 5.0

#: Errors that mean "this will never work until a human changes something".
#: Cached for the process so a permanent gap is reported ONCE, not per judgment.
_PERMANENT_ERRORS = (
    "missing_scope", "not_allowed_token_type", "invalid_auth",
    "account_inactive", "token_revoked", "no_permission",
)

#: Slack message subtypes that are pure byte-waste in a judgment prompt.
_SKIP_SUBTYPES = frozenset({
    "channel_join", "channel_leave", "channel_topic", "channel_purpose",
    "channel_name", "channel_archive", "channel_unarchive", "group_join",
    "group_leave", "group_topic", "group_purpose", "group_name",
    "pinned_item", "unpinned_item", "bot_message", "message_changed",
    "message_deleted", "tombstone", "reminder_add", "bot_add", "bot_remove",
})

#: Store flag names. Both carry NUMBERS and our own vocabulary only.
PINS_FLAG = "context_pins_scope"
LAST_FLAG = "context_last"

#: Fixed vocabulary for the ``context:`` note on a nominee. Our own strings, so
#: the note is never a channel-controlled value.
NOTE_THREAD = "thread history unavailable"
NOTE_CHANNEL = "channel history unavailable"
NOTE_TOPIC = "channel identity unavailable"
NOTE_PINS = "pinned items unavailable"


# ------------------------------------------------------------------- the reader


def _dig(node, *path, default=None):
    """Walk a possibly-malformed JSON body without raising."""
    for key in path:
        if not isinstance(node, dict):
            return default
        node = node.get(key)
    return node if node is not None else default


def _is_bot_row(row) -> bool:
    return bool(row.get("bot_id") or row.get("subtype") == "bot_message"
                or row.get("app_id"))


class SlackReader:
    """GET-only twin of ``aw_post.SlackTransport``.

    Same token resolution (``aw_post._bot_token``, which restores the
    cron-stripped ``.env`` via ``load_hermes_dotenv``), same ``urllib``, and the
    same "never raise, return a falsy dict" contract — because every caller of
    this class treats failure as *less context*, never as an error.

    SANITIZATION HAPPENS HERE, not in the caller. Each method maps its rows
    through ``aw_sanitize.neutralize`` with the section's cap before returning,
    so no raw Slack string is ever assigned to something a caller can read.
    """

    def __init__(self, *, token=None, home=None, fetch=None, sleep=None,
                 clock=None, fetch_timeout=4.0, total_timeout=8.0):
        self._token = token
        self._token_resolved = token is not None
        self._home = home
        self._fetch = fetch or self._http_fetch
        self._sleep = sleep or time.sleep
        self._clock = clock or time.monotonic
        self.fetch_timeout = float(fetch_timeout)
        self.total_timeout = float(total_timeout)
        self._deadline = None
        #: Method names only — never parameters, never bodies.
        self.calls: list = []
        self.stats = {
            "fetches": 0, "failures": 0, "rate_limited": 0, "budget_skipped": 0,
        }
        #: Permanent failures already reported, so a known gap is quiet after
        #: the first line.
        self.unavailable: dict = {}

    @classmethod
    def from_cfg(cls, cfg):
        return cls(
            fetch_timeout=float(getattr(cfg, "context_fetch_timeout_seconds", 4) or 4),
            total_timeout=float(getattr(cfg, "context_total_timeout_seconds", 8) or 8),
        )

    # -- budget -----------------------------------------------------------

    def start_budget(self, seconds=None):
        """Bound the WHOLE enrichment, not each call.

        A hung TCP connection must not park a worker thread on the gateway loop
        or stall a cron tick, and two fetches at the per-call timeout would
        already exceed what a judgment can afford to wait.
        """
        self._deadline = self._clock() + float(
            seconds if seconds is not None else self.total_timeout
        )

    def _remaining(self) -> float:
        if self._deadline is None:
            return self.total_timeout
        return self._deadline - self._clock()

    # -- transport --------------------------------------------------------

    def _bearer(self) -> str:
        if not self._token_resolved:
            try:
                self._token = aw_post._bot_token(self._home)
            except Exception:  # noqa: BLE001 — no token is a degradation
                self._token = ""
            self._token_resolved = True
        return self._token or ""

    def _http_fetch(self, method: str, params: dict, timeout: float) -> dict:
        """One GET against Slack. Maps every failure onto a falsy dict."""
        url = f"{_API_BASE}{method}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {self._bearer()}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode())
            return body if isinstance(body, dict) else {
                "ok": False, "error": "malformed_body"
            }
        except urllib.error.HTTPError as exc:  # noqa: PERF203
            retry_after = 0.0
            try:
                retry_after = float(exc.headers.get("Retry-After") or 0)
            except (TypeError, ValueError, AttributeError):
                retry_after = 0.0
            if exc.code == 429:
                return {"ok": False, "error": "http_429", "retry_after": retry_after}
            return {"ok": False, "error": f"http_{exc.code}"}
        except Exception as exc:  # noqa: BLE001 — a fetch must never raise
            return {"ok": False, "error": f"transport:{type(exc).__name__}"}

    def _call(self, method: str, params: dict) -> dict:
        """One Slack read, with exactly ONE bounded 429 retry. Never raises."""
        if not self._bearer():
            return {"ok": False, "error": "no_slack_bot_token"}
        known = self.unavailable.get(method)
        if known:
            return {"ok": False, "error": known, "cached": True}
        for attempt in (0, 1):
            remaining = self._remaining()
            if remaining <= 0:
                self.stats["budget_skipped"] += 1
                return {"ok": False, "error": "context_budget_exhausted"}
            self.calls.append(method)
            self.stats["fetches"] += 1
            body = self._fetch(
                method, dict(params), min(self.fetch_timeout, remaining)
            )
            if not isinstance(body, dict):
                self.stats["failures"] += 1
                return {"ok": False, "error": "malformed_body"}
            if body.get("ok"):
                return body
            error = str(body.get("error") or "unknown")
            self.stats["failures"] += 1
            if error in _PERMANENT_ERRORS:
                # Report once. A per-judgment log line about a permanent,
                # known configuration gap is noise that hides real failures.
                self.unavailable[method] = error
                logger.warning(
                    "ambient-watch: %s is unavailable (%s) — that context "
                    "section will be skipped for the life of this process. If "
                    "it is pins.list, the bot token has no pins:read scope: "
                    "add it at api.slack.com/apps, Reinstall to Workspace, "
                    "then re-copy the token into HERMES_HOME/.env.",
                    method, error,
                )
                return {"ok": False, "error": error}
            if error in _RATE_LIMIT_ERRORS and attempt == 0:
                self.stats["rate_limited"] += 1
                wait = min(
                    float(body.get("retry_after") or 1.0),
                    _MAX_RETRY_AFTER_SECONDS,
                    max(0.0, self._remaining()),
                )
                if wait <= 0:
                    return {"ok": False, "error": error}
                self._sleep(wait)
                continue
            return {"ok": False, "error": error}
        return {"ok": False, "error": "ratelimited"}

    # -- the four reads, each sanitizing its own payload -------------------

    def _rows(self, body, cap: int):
        """Slack messages -> sanitized rows. Bots and noise are dropped here.

        Anthropic filters other bots' replies out of the window; for us it is
        also budget defence — one chatty CI webhook would otherwise eat the
        whole section.
        """
        out, bots = [], 0
        for row in _dig(body, "messages", default=[]) or []:
            if not isinstance(row, dict):
                continue
            if row.get("subtype") in _SKIP_SUBTYPES:
                if row.get("subtype") == "bot_message":
                    bots += 1
                continue
            if _is_bot_row(row):
                bots += 1
                continue
            text = aw_sanitize.neutralize(row.get("text") or "", cap)
            acks = [
                str(r.get("name") or "")
                for r in (row.get("reactions") or [])
                if isinstance(r, dict)
            ]
            out.append({
                "ts": str(row.get("ts") or ""),
                # Identifiers, copied as strings exactly like ``ts``: Slack
                # generates them, nothing ever prints them, and the mute filter
                # in _activity_section needs to know which thread a fetched row
                # belongs to (conversations.history returns thread ROOTS, and a
                # thread_broadcast carries thread_ts).
                "thread_root": str(row.get("thread_ts") or row.get("ts") or ""),
                "author": row.get("user") or None,
                "is_bot": 0,
                "text": text,
                # Only names from our OWN allowlist survive, so no
                # attacker-authored emoji name can traverse.
                "acks": [n for n in aw_sanitize.ACK_REACTIONS if n in acks],
            })
        return out, bots

    def replies(self, channel: str, root: str, limit: int = FETCH_REPLY_LIMIT) -> dict:
        """``conversations.replies`` — the thread, including its root.

        ``root_is_bot`` is reported separately from the filtered rows because it
        is the authoritative answer to the anti-feedback-loop question, and the
        row itself is exactly the one the filter above removes.
        """
        body = self._call("conversations.replies", {
            "channel": channel, "ts": root, "limit": int(limit),
            "inclusive": "true",
        })
        if not body.get("ok"):
            return {"ok": False, "error": body.get("error", "unknown")}
        raw = [r for r in (_dig(body, "messages", default=[]) or [])
               if isinstance(r, dict)]
        root_row = next((r for r in raw if str(r.get("ts") or "") == str(root)), None)
        rows, bots = self._rows(body, aw_sanitize.JUDGE_MAX_MESSAGE_CHARS)
        return {
            "ok": True,
            "messages": rows,
            "bots_omitted": bots,
            "root_seen": root_row is not None,
            "root_is_bot": bool(root_row is not None and _is_bot_row(root_row)),
        }

    def history(self, channel: str, limit: int, oldest=None) -> dict:
        """``conversations.history`` — recent top-level channel activity."""
        params = {"channel": channel, "limit": int(limit) + FETCH_HISTORY_EXTRA}
        if oldest is not None:
            params["oldest"] = f"{float(oldest):.6f}"
        body = self._call("conversations.history", params)
        if not body.get("ok"):
            return {"ok": False, "error": body.get("error", "unknown")}
        rows, bots = self._rows(body, aw_sanitize.CTX_CHANNEL_MSG_CHARS)
        rows.sort(key=lambda r: r["ts"])
        return {"ok": True, "messages": rows, "bots_omitted": bots}

    def info(self, channel: str) -> dict:
        """``conversations.info`` — name, topic, purpose.

        A topic is writable by any member (and ``channels:manage`` is on the
        token), so it is untrusted text, not metadata.
        """
        body = self._call("conversations.info", {"channel": channel})
        if not body.get("ok"):
            return {"ok": False, "error": body.get("error", "unknown")}
        ch = _dig(body, "channel", default={}) or {}
        if not isinstance(ch, dict):
            return {"ok": False, "error": "malformed_body"}
        return {
            "ok": True,
            "name": aw_sanitize.neutralize(ch.get("name") or "", 60),
            "topic": aw_sanitize.neutralize(
                _dig(ch, "topic", "value", default="") or "",
                aw_sanitize.CTX_TOPIC_FIELD_CHARS,
            ),
            "purpose": aw_sanitize.neutralize(
                _dig(ch, "purpose", "value", default="") or "",
                aw_sanitize.CTX_TOPIC_FIELD_CHARS,
            ),
        }

    def pins(self, channel: str, limit: int) -> dict:
        """``pins.list`` — OPTIONAL. Needs a scope this install does not have."""
        body = self._call("pins.list", {"channel": channel})
        if not body.get("ok"):
            return {"ok": False, "error": body.get("error", "unknown")}
        items = []
        for item in _dig(body, "items", default=[]) or []:
            if len(items) >= int(limit):
                break
            if not isinstance(item, dict):
                continue
            message = item.get("message")
            text = ""
            if isinstance(message, dict):
                if _is_bot_row(message):
                    continue
                text = message.get("text") or ""
            body_text = aw_sanitize.neutralize(text, aw_sanitize.CTX_PIN_CHARS)
            if body_text:
                items.append(body_text)
        return {"ok": True, "items": items}


# -------------------------------------------------------------------- the cache


class ContextCache:
    """Per-key TTL cache. PROCESS-LOCAL, NEVER PERSISTED.

    Persisting the topic/pins result in the ``flags`` table was considered and
    rejected: the fetch is cheap enough that persistence buys nothing and would
    create a NEW permanent copy of untrusted text — the exact category the
    2026-08-11 incident came from. So the count of new persisted bytes stays
    zero and the containment claim is absolute rather than "sanitized before
    storage".

    One instance per ``run_gate()`` call; one instance held by
    ``ArrivalRuntime`` for the gateway process's lifetime.
    """

    def __init__(self, clock=None):
        self._clock = clock or time.monotonic
        self._data: dict = {}
        self.hits = 0
        self.misses = 0
        self._batch = 0

    def new_batch(self) -> int:
        """Open a fresh scope for entries that must NOT outlive one enrichment.

        Channel activity is the case this exists for. Its entire job is catching
        "somebody already answered in the channel", which is a fact about *now* —
        and the arrival runtime holds ONE cache for the whole gateway process, so
        an entry with no expiry would freeze that window at whatever it was the
        first time the process judged anything, in exactly the wrong direction
        (the answer usually arrives after the first fetch). Channel identity is
        the deliberate contrast: a topic does not change per message, so it keeps
        the 6h TTL. Entries from older batches are dropped here, so the dict
        cannot grow without bound in a long-lived process.
        """
        self._batch += 1
        self._data = {
            key: value for key, value in self._data.items()
            if not (isinstance(key, tuple) and key and key[0] == "batch")
        }
        return self._batch

    def batch_key(self, *parts) -> tuple:
        return ("batch", self._batch, *parts)

    def get_or(self, key, ttl, producer):
        entry = self._data.get(key)
        if entry is not None and (entry[0] is None or self._clock() < entry[0]):
            self.hits += 1
            return entry[1]
        self.misses += 1
        value = producer()
        expiry = None if ttl is None else self._clock() + float(ttl)
        self._data[key] = (expiry, value)
        return value

    def stats(self) -> dict:
        return {"hits": self.hits, "misses": self.misses, "keys": len(self._data)}


# ------------------------------------------------------------- the entry point


@dataclass
class EnrichResult:
    """What the enricher did. ``dropped`` is ``[(candidate, reason)]``."""

    keep: list = field(default_factory=list)
    dropped: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)


_SECTION_LABEL = re.compile(r"^\[([A-Z][A-Z ]*)\]$")


def section_lengths(block: str) -> dict:
    """Body length per section label. For tests and the ops surface only."""
    out: dict = {}
    label = None
    for line in (block or "").splitlines():
        match = _SECTION_LABEL.match(line)
        if match:
            label = match.group(1)
            out.setdefault(label, 0)
            continue
        if label is not None and not line.startswith("<"):
            out[label] = out.get(label, 0) + len(line) + 1
    return out


def _thread_section(cand, cfg, store, reader, notes, ceiling) -> str:
    """Fill ``[THIS THREAD]``, backfilling from Slack only when required.

    ONE trigger for a fetch, and no others: the root row is absent
    (``root_missing``). A thread the ledger holds a root for costs zero calls,
    which is the steady state.

    WHAT THIS DELIBERATELY DOES NOT REPAIR: a thread whose root we have but
    whose MIDDLE messages we never recorded — gateway downtime, senders the
    adapter filtered before ``SLACK_ALLOW_ALL_USERS``, pruned middles. There is
    no free signal for it. An earlier version of this function tested
    ``root_ts < store.channel_first_ts(channel)``, which reads plausibly and can
    never be true: ``channel_first_ts`` is ``MIN(ts)`` over the same channel's
    rows and a non-rootless candidate's root row is one of them, so the minimum
    is always <= the root. It fetched nothing, ever. Detecting a hole for real
    costs a ``conversations.replies`` on EVERY judgment, which is a money
    decision an operator has to make explicitly — so the honest state is: the
    judge may see a thread with a hole in it, and PARITY.md says so.

    Returns the reason to DROP the nominee, or "" to keep it.
    """
    root = cand.thread_ts
    msgs = list(cand.messages or [])
    need = bool(getattr(cand, "root_missing", False))
    if not (need and getattr(cfg, "context_thread_backfill", True)):
        return ""

    body = reader.replies(cand.channel, root)
    if not body.get("ok") or not body.get("root_seen"):
        # FAIL CLOSED, always: the only trigger is a MISSING root, so a failed
        # fetch leaves "is this thread bot-authored?" unanswered and guessing
        # would be fail-open on the anti-feedback-loop rule.
        notes.add(NOTE_THREAD)
        return "declined-root-unknown"
    if body.get("root_is_bot"):
        return "declined-bot-root"

    by_ts = {str(m.get("ts")): m for m in body.get("messages") or []}
    for row in msgs:  # the ledger wins: it is the raw, authoritative copy
        by_ts[str(row.get("ts"))] = row
    merged = sorted(by_ts.values(), key=lambda r: str(r.get("ts")))
    cand.messages = merged
    # The thread is spent from the SAME budget as the sections and is never
    # dropped, so it may not exceed the whole ceiling on its own — otherwise a
    # low `context_max_chars` produces a prompt LARGER than context-off while
    # deleting every section (measured: 3050 chars at a ceiling of 500).
    cand.judge_view = aw_sanitize.build_judge_view(
        merged, aw_sanitize.CTX_THREAD_MESSAGES,
        min(aw_sanitize.CTX_THREAD_VIEW_CHARS, max(0, int(ceiling))),
    )
    return ""


def _channel_section(cand, cfg, cache, reader, ttl, notes) -> str:
    if not getattr(cfg, "context_topic", True):
        return ""
    body = cache.get_or(("info", cand.channel), ttl,
                        lambda: reader.info(cand.channel))
    if not body.get("ok"):
        notes.add(NOTE_TOPIC)
        return ""
    parts = []
    if body.get("name"):
        parts.append(f"name: {body['name']}")
    if body.get("topic"):
        parts.append(f"topic: {body['topic']}")
    if body.get("purpose"):
        parts.append(f"purpose: {body['purpose']}")
    return "\n".join(parts)[:aw_sanitize.CTX_TOPIC_CHARS]


def _mute_check(store, channel):
    """Memoized "has a human muted this thread?", for one section build.

    ``ambient mute`` is the only in-Slack control a human has for "leave this
    thread alone", and the recorder deliberately keeps RECORDING a muted thread
    (mute gates nomination, not the ledger). Before context fidelity that was
    harmless, because a thread's text could only ever reach the prompt built for
    that same thread — and a muted thread is never nominated.
    ``[RECENT CHANNEL ACTIVITY]`` draws from the whole channel, so without this
    filter mute would still stop us NUDGING a thread while quietly failing to
    stop us reading it into a judgment about a different one and shipping it to
    a model provider. That is not what the human was told mute means.

    Bounded and cheap: at most ``want * 4`` plus the fetched rows' distinct
    roots, each an indexed lookup on ``muted``'s (channel, thread_ts) primary
    key, memoized per section build. Never raises — a ledger read must not break
    a judgment — but note the failure direction is INCLUDE, matching every other
    degradation here; the mute itself still blocks the nomination of that thread.
    """
    is_muted = getattr(store, "is_muted", None)
    seen: dict = {}

    def muted(root) -> bool:
        key = str(root or "")
        if not key or is_muted is None:
            return False
        if key not in seen:
            try:
                seen[key] = bool(is_muted(channel, key))
            except Exception:  # noqa: BLE001 — degrade, never block a judgment
                seen[key] = False
        return seen[key]

    return muted


def _activity_section(cand, cfg, store, cache, reader, now, notes) -> str:
    """Recent channel activity — the LEDGER first, Slack only if it is thin."""
    want = int(getattr(cfg, "context_channel_messages", 0) or 0)
    if not (want and getattr(cfg, "context_channel_history", True)):
        return ""
    hours = int(getattr(cfg, "context_channel_hours", 6) or 6)
    since = float(now) - hours * 3600
    muted = _mute_check(store, cand.channel)

    rows, bots = [], 0
    try:
        for row in store.recent_channel_messages(cand.channel, want * 4, since):
            if row.get("is_bot"):
                bots += 1
                continue
            if str(row.get("thread_root")) == str(cand.thread_ts):
                continue  # already in [THIS THREAD]
            if muted(row.get("thread_root")):
                continue  # a human said to leave that thread alone
            rows.append(row)
    except Exception:  # noqa: BLE001 — a ledger read must not break a judgment
        logger.debug("ambient-watch: ledger channel window failed", exc_info=True)

    if len(rows) < want:
        # Cold start, post-restart, or after quiet hours. Cached in the BATCH
        # scope: shared by every nominee in this one enrichment, and never reused
        # by the next one — see ContextCache.new_batch for why that direction of
        # staleness would defeat the section's whole purpose.
        body = cache.get_or(
            cache.batch_key("history", cand.channel), None,
            lambda: reader.history(cand.channel, want, since),
        )
        if body.get("ok"):
            bots += int(body.get("bots_omitted") or 0)
            seen = {str(r.get("ts")) for r in rows}
            for row in body.get("messages") or []:
                if str(row.get("ts")) in seen:
                    continue
                if str(row.get("ts")) == str(cand.thread_ts):
                    continue
                # Same mute rule on the OTHER source, or the filter would only
                # cover the steady state: conversations.history returns thread
                # ROOTS, which is exactly how a muted thread arrives here when
                # the ledger is thin.
                if muted(row.get("thread_root") or row.get("ts")):
                    continue
                rows.append(row)
        else:
            notes.add(NOTE_CHANNEL)

    rows.sort(key=lambda r: str(r.get("ts")))
    lines = aw_sanitize.neutralize_lines(
        [r.get("text") for r in rows[-want:]],
        aw_sanitize.CTX_CHANNEL_MSG_CHARS,
    )
    if bots:
        lines.append(f"({bots} bot message(s) omitted)")
    body_text = "\n".join(lines)
    return body_text[:aw_sanitize.CTX_CHANNEL_CHARS]


def _pins_section(cand, cfg, store, cache, reader, ttl, notes) -> str:
    if not getattr(cfg, "context_pins", False):
        return ""
    limit = int(getattr(cfg, "context_pin_items", 0) or 0)
    if not limit:
        return ""
    body = cache.get_or(("pins", cand.channel), ttl,
                        lambda: reader.pins(cand.channel, limit))
    if not body.get("ok"):
        notes.add(NOTE_PINS)
        error = str(body.get("error") or "")
        if error in _PERMANENT_ERRORS:
            # Our own vocabulary, not Slack's arbitrary string, and numbers-only
            # in spirit: this is what aw_status.py reports to the operator with
            # the remediation sentence.
            try:
                store.set_flag(PINS_FLAG, error)
            except Exception:  # noqa: BLE001 — reporting is never load-bearing
                pass
        return ""
    items = list(body.get("items") or [])[:limit]
    return "\n".join(f"- {t}" for t in items)[:aw_sanitize.CTX_PINS_CHARS]


def enrich_for_judgment(cands, cfg, store, cache=None, reader=None, now=None):
    """Add context to nominees that are ABOUT to be judged. Never raises.

    Called with a list so both triggers share one implementation. Mutates
    ``judge_view``, ``messages``, ``context_block`` and ``context_note`` in
    place, and returns which nominees survived: a rootless thread whose root
    cannot be verified is DROPPED rather than judged.
    """
    cands = list(cands or [])
    if not getattr(cfg, "context_enabled", False):
        return EnrichResult(keep=cands, stats={"enabled": False})

    now = time.time() if now is None else float(now)
    cache = cache if cache is not None else ContextCache()
    reader = reader if reader is not None else SlackReader.from_cfg(cfg)
    ttl = int(getattr(cfg, "context_cache_ttl_seconds", 21600) or 0) or None
    ceiling = min(
        int(getattr(cfg, "context_max_chars", aw_sanitize.CTX_TOTAL_CHARS) or 0),
        aw_sanitize.CTX_TOTAL_CHARS,
    )

    try:
        reader.start_budget(getattr(cfg, "context_total_timeout_seconds", 8))
    except Exception:  # noqa: BLE001
        pass

    # DELTAS, not totals. The gate builds a fresh reader per tick, but the
    # arrival runtime keeps one for the whole gateway process — bumping its
    # cumulative counters on every judgment would compound them quadratically.
    before = dict(getattr(reader, "stats", {}) or {})
    cache_before = cache.stats()
    cache.new_batch()  # scope for entries that must not outlive this enrichment

    keep, dropped, snapshot = [], [], {}
    for cand in cands:
        notes: set = set()
        try:
            drop = _thread_section(cand, cfg, store, reader, notes, ceiling)
            if drop:
                dropped.append((cand, drop))
                continue
            sections = [
                ("CHANNEL", _channel_section(cand, cfg, cache, reader, ttl, notes)),
                ("RECENT CHANNEL ACTIVITY",
                 _activity_section(cand, cfg, store, cache, reader, now, notes)),
                ("PINNED", _pins_section(cand, cfg, store, cache, reader, ttl, notes)),
            ]
            # ONE ceiling, over all four sections together and applied LAST.
            # The thread view is assembled (and capped) above, so what remains
            # is what the other three may spend — which is why pins are
            # structurally the first thing dropped under pressure and the thread
            # is structurally never dropped.
            budget = max(0, ceiling - len(cand.judge_view or ""))
            cand.context_block = aw_sanitize.build_context_block(sections, budget)
            cand.context_note = ", ".join(sorted(notes))
            measured = section_lengths(cand.context_block)
            snapshot = {
                "chars": len(cand.judge_view or "") + len(cand.context_block),
                "thread_msgs": len(cand.messages or []),
                "context_chars": len(cand.context_block),
                # Which sections actually SURVIVED the ceiling, and how big each
                # one ended up. This is the ops answer to "what did the judge
                # see?" that carries no text: a section that was clipped away
                # under budget simply is not here.
                "sections": [
                    label for label, _body in sections if label in measured
                ],
                "section_chars": {k: int(v) for k, v in measured.items()},
                "notes": sorted(notes),
                "fetches": int(
                    reader.stats.get("fetches", 0) - (before.get("fetches") or 0)
                ),
                "at": now,
            }
            keep.append(cand)
        except Exception:  # noqa: BLE001 — degrade, never block a judgment
            logger.debug("ambient-watch: enrichment failed", exc_info=True)
            cand.context_block = getattr(cand, "context_block", "") or ""
            cand.context_note = (
                getattr(cand, "context_note", "") or "context unavailable"
            )
            keep.append(cand)

    after, cache_after = dict(reader.stats), cache.stats()
    stats = {
        "enabled": True,
        "cache": {k: cache_after[k] - cache_before.get(k, 0)
                  for k in ("hits", "misses")},
        "reader": {k: after.get(k, 0) - (before.get(k) or 0) for k in after},
        "dropped": len(dropped),
        "totals": {"reader": after, "cache": cache_after},
    }
    _record(store, snapshot, stats, now)
    return EnrichResult(keep=keep, dropped=dropped, stats=stats)


def _record(store, snapshot, stats, now):
    """Durable ops surface: NUMBERS and our own section labels, never text.

    This is the only thing the context layer writes to disk, which is what
    makes "nothing new is persisted" checkable by diffing the data directory.
    """
    try:
        if snapshot:
            store.set_flag(LAST_FLAG, json.dumps(snapshot))
        reader = stats.get("reader") or {}
        cache = stats.get("cache") or {}
        store.bump_context_counters(
            now=now,
            judgments=1,
            fetches=int(reader.get("fetches") or 0),
            failures=int(reader.get("failures") or 0),
            rate_limited=int(reader.get("rate_limited") or 0),
            budget_skipped=int(reader.get("budget_skipped") or 0),
            cache_hits=int(cache.get("hits") or 0),
            cache_misses=int(cache.get("misses") or 0),
            dropped=int(stats.get("dropped") or 0),
        )
    except Exception:  # noqa: BLE001 — reporting is never load-bearing
        logger.debug("ambient-watch: context counters not written", exc_info=True)
