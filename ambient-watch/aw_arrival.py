"""Arrival-time judging: judge a thread when a message arrives, debounced.

WHAT THIS CLOSES. Claude Tag replies to channel messages *as they arrive*; our
judge only ever saw a thread that had already been quiet for
``min_age_minutes``. The judgment was equivalent; the trigger was not. This
module moves the trigger onto ``pre_gateway_dispatch`` and keeps the sweep as
the ``stalled_thread`` trigger and the ops reporting surface.

WHAT IT DOES NOT DO, DELIBERATELY. There is no escalation to a tool-bearing
agent session, no ``ctx.inject_message``, and no toolset is widened anywhere.
The capability envelope of arrival mode is exactly the sweep's: ONE bounded
structured LLM call with no tools, whose only effect is a ≤200-character plain
text post into one Slack thread. Autonomously routing attacker-controllable
channel text into a session holding ``terminal``/``execute_code``/``browser_*``
is a self-triggering code-execution surface and it has not been consented to.

THREE FACTS FROM THE REAL SOURCE SHAPE EVERYTHING HERE.

1. ``pre_gateway_dispatch`` is invoked SYNCHRONOUSLY from inside
   ``async def _handle_message`` (gateway/run.py:14902-14923 ->
   hermes_cli/lifecycle.py -> hermes_cli/plugins.py:2126 ``ret = cb(**kwargs)``).
   There is no await, no timeout and no thread offload, and only ``dict``
   returns are inspected — so a coroutine returned from the hook is silently
   dropped unawaited, and any blocking work in the hook delays EVERY inbound
   message on EVERY platform. But the hook body runs on the loop thread, so
   ``asyncio.get_running_loop()`` succeeds and ``loop.create_task`` is the
   house pattern. ``create_task`` keeps only a weak reference
   (gateway/run.py:10494), hence the mandatory strong ref in ``self._task``.

2. ``ctx.llm.acomplete_structured`` is the wrong transport — see
   ``aw_judge.hermes_allm`` for the source citation. We call
   ``async_call_llm(task=AUX_TASK, ...)`` so the operator's pinned cheap model
   is actually used.

3. Every existing control is REUSED, not reimplemented. The arrival path
   reaches the model only through ``aw_detectors.find_candidates`` and posts
   only through ``aw_post.post_nudge``. Two eligibility ladders would drift,
   and the one that drifts is the one that spends money and posts.

THE DEBOUNCE IS THREE MECHANISMS AT THREE DIFFERENT JOBS.

* coalescing map (identity = the thread) — a 20-message burst is ONE judgment
* per-thread quiet timer — decides *when* one thread is ready, and doubles as
  the politeness floor: we get one post per thread ever, so replying five
  seconds in spends it on a thread a colleague was already answering
* per-channel + global token buckets — bound the *rate across* threads, which
  the map cannot: 200 threads with one message each are 200 legitimate entries

THE TRIGGER IS NOW ATTACKER-TIMED. The 15-minute cadence was an implicit rate
limit that no message volume could change (<=96 judge calls/day, whatever
anyone posted). At arrival time, whoever posts chooses when we spend. The token
buckets are the REPLACEMENT for that cadence, not an optimization: without them
the USD caps still bound the bill but not the exhaustion — one hostile actor
burns ``daily_usd_global`` in ninety seconds and ambient goes silent for the
rest of the day at full price. The buckets therefore meter ATTEMPTS, and a
token is never refunded on failure.

I/O DISCIPLINE. The hook does pure-memory work only. Everything the pump
touches that blocks — ``find_candidates``, budget rollups, ``record_judgment``,
the durable arrival counters and the whole post step — goes through
``asyncio.to_thread``. This is not tidiness: ``AmbientStore`` uses
``busy_timeout=5000``, so one write contended with the sweep process would
otherwise stall the GATEWAY EVENT LOOP for five seconds. The counters are the
one that hid: a bump looks like arithmetic and is a read-modify-write
transaction, and it sits on the throttle rung, which attacker volume drives once
per refused thread.

AUDIT TRAIL. ``plugin-data/ambient_watch/arrival.log`` — inside the L3 jail's
``plugin-data`` markers, which is strictly better than the sweep's
``cron/output/<job_id>/``. Ids, verdicts, confidences, dollars and the
model-authored nudge only: never a message body, never an excerpt, never a
Slack user id.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

try:  # real loader: package-relative
    from . import aw_context, aw_detectors
    from .aw_recorder import arrival_key
except ImportError:  # cron shim / bare script: flat import
    import aw_context
    import aw_detectors
    from aw_recorder import arrival_key

logger = logging.getLogger("ambient_watch")

ARRIVAL_LOG_NAME = "arrival.log"
ARRIVAL_LOG_MAX_BYTES = 64 * 1024  # same rotation as gate_errors.log

#: Tier A must not put a SQL read on the loop's hot path, but the kill switch
#: still has to be honoured there — so it is read at most once per this many
#: seconds. Tier B reads it FRESH: a 3am ``--kill on`` must stop a judgment
#: already sitting in the queue.
KILL_CACHE_TTL_SECONDS = 5.0

#: Pump lifetime. It exits after this many consecutive empty wakes so a
#: dormant gateway carries no task; ``ensure_pump`` recreates it on the next
#: arrival, which is also what covers a loop identity change.
PUMP_IDLE_TICKS = 12


# ---------------------------------------------------------------- pure core


@dataclass
class Pending:
    """One coalesced thread awaiting judgment. Monotonic times."""

    channel: str
    root: str
    first_seen: float      # start of the burst — drives the max-wait backstop
    last_seen: float       # most recent arrival — drives the quiet timer
    count: int = 1         # messages coalesced (audit only)
    last_activity: float = 0.0  # newest Slack ts seen: the freshness watermark


class Debouncer:
    """Coalescing map + quiet timer. Pure: dicts and an injected clock only.

    Mutated only on the gateway loop thread. The hook and the pump never
    interleave because neither awaits between a read and a write of this map,
    so no lock is needed — an assumption worth stating explicitly, because
    ``AmbientStore`` DOES hold an RLock for the tool-executor threads and a
    reader could reasonably assume the same is required here.
    """

    def __init__(self, debounce_seconds=90, max_wait_seconds=300, max_pending=200):
        self.debounce_seconds = float(debounce_seconds)
        self.max_wait_seconds = float(max_wait_seconds or 0)
        self.max_pending = int(max_pending)
        self._pending: dict = {}
        self.dropped = 0

    @classmethod
    def from_cfg(cls, cfg) -> "Debouncer":
        return cls(
            debounce_seconds=getattr(cfg, "arrival_debounce_seconds", 90),
            max_wait_seconds=getattr(cfg, "arrival_max_wait_seconds", 300),
            max_pending=getattr(cfg, "arrival_max_pending", 200),
        )

    def __len__(self):
        return len(self._pending)

    def upsert(self, channel: str, root: str, now: float, last_activity=0.0) -> bool:
        """Note an arrival. O(1). False when the pending cap dropped it."""
        key = (channel, root)
        entry = self._pending.get(key)
        if entry is None:
            if len(self._pending) >= self.max_pending:
                # DROP-NEW, not drop-oldest: the sweep is the backstop, so the
                # loss is latency rather than the judgment. There is no
                # eviction policy here that is not gameable; under a raid
                # arrival mode degrades to sweep behaviour, and that is the
                # honest statement.
                self.dropped += 1
                return False
            self._pending[key] = Pending(
                channel=channel, root=root, first_seen=float(now),
                last_seen=float(now), count=1, last_activity=float(last_activity),
            )
            return True
        entry.last_seen = float(now)
        entry.count += 1
        entry.last_activity = max(entry.last_activity, float(last_activity))
        return True

    def due(self, now: float) -> list:
        """Keys whose timer fired, oldest burst first. PURE — no mutation.

        Being a pure function over the map plus an injected clock is what makes
        burst/quiet/max-wait/eviction unit-testable with zero event loop, which
        matters because there is no ``pytest-asyncio`` in this venv and the
        project's dependency rules forbid adding one.
        """
        out = []
        for key, entry in self._pending.items():
            quiet = (now - entry.last_seen) >= self.debounce_seconds
            waited = (
                self.max_wait_seconds > 0
                and (now - entry.first_seen) >= self.max_wait_seconds
            )
            if quiet or waited:
                out.append(key)
        out.sort(key=lambda k: self._pending[k].first_seen)
        return out

    def get(self, key):
        return self._pending.get(key)

    def drop(self, key):
        return self._pending.pop(key, None)

    def clear(self):
        self._pending.clear()


@dataclass
class Bucket:
    tokens: float
    updated: float


class TokenBuckets:
    """Per-channel + one global token bucket. Pure, injected clock.

    METERS ATTEMPTS, NOT SUCCESSES. ``take`` happens immediately before the
    model call and the token is never refunded, because otherwise a broken
    provider becomes a free retry loop driven by whoever is posting — the
    arrival-time version of the outage hole ``estimate_prompt_tokens`` exists
    for.
    """

    GLOBAL = "*"

    def __init__(self, per_channel_hour=4, global_hour=12, burst=2):
        self.channel_rate = float(per_channel_hour) / 3600.0
        self.global_rate = float(global_hour) / 3600.0
        self.burst = float(max(0, burst))
        self._buckets: dict = {}
        self.refused = 0

    @classmethod
    def from_cfg(cls, cfg) -> "TokenBuckets":
        return cls(
            per_channel_hour=getattr(cfg, "arrival_judgments_per_channel_hour", 4),
            global_hour=getattr(cfg, "arrival_judgments_global_hour", 12),
            burst=getattr(cfg, "arrival_burst", 2),
        )

    def _bucket(self, key: str, now: float, rate: float) -> Bucket:
        entry = self._buckets.get(key)
        if entry is None:
            entry = Bucket(tokens=self.burst, updated=float(now))
            self._buckets[key] = entry
            return entry
        elapsed = max(0.0, float(now) - entry.updated)
        entry.tokens = min(self.burst, entry.tokens + elapsed * rate)
        entry.updated = float(now)
        return entry

    def _pair(self, channel: str, now: float):
        return (
            self._bucket(self.GLOBAL, now, self.global_rate),
            self._bucket(channel, now, self.channel_rate),
        )

    def available(self, channel: str, now: float) -> bool:
        """Ask, do not take. Rungs 9-10 of the ladder: an ineligible thread
        must not burn the channel's rate budget."""
        glob, chan = self._pair(channel, now)
        return glob.tokens >= 1.0 and chan.tokens >= 1.0

    def take(self, channel: str, now: float) -> bool:
        """Take one token from BOTH scopes, or neither."""
        glob, chan = self._pair(channel, now)
        if glob.tokens < 1.0 or chan.tokens < 1.0:
            self.refused += 1
            return False
        glob.tokens -= 1.0
        chan.tokens -= 1.0
        return True

    def snapshot(self, now: float) -> dict:
        return {
            key: round(self._bucket(
                key, now,
                self.global_rate if key == self.GLOBAL else self.channel_rate,
            ).tokens, 4)
            for key in sorted(self._buckets)
        }


# ------------------------------------------------------------ the audit trail


def _task_loop(task):
    """The loop a task is bound to, or None when it cannot be asked."""
    getter = getattr(task, "get_loop", None)
    if getter is None:
        return None
    try:
        return getter()
    except Exception:  # noqa: BLE001 — an unaskable task is a replaceable task
        return None


def append_arrival_log(data_dir, detail: str) -> Path | None:
    """Append one excerpt-free line to ``arrival.log``. Never raises.

    A detached task's unhandled exception is otherwise only visible as "Task
    exception was never retrieved" in the Hermes log, which is exactly the
    silent-failure mode ``gate_errors.log`` exists to prevent — so every
    swallowed exception on this path leaves a breadcrumb here.
    """
    try:
        folder = Path(data_dir)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / ARRIVAL_LOG_NAME
        try:
            if path.stat().st_size > ARRIVAL_LOG_MAX_BYTES:
                os.replace(path, path.with_suffix(path.suffix + ".1"))
        except OSError:
            pass
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{stamp}] arrival: {detail.strip()}\n")
        return path
    except Exception:  # noqa: BLE001 — breadcrumbs are never load-bearing
        return None


# --------------------------------------------------------------- the runtime


class ArrivalRuntime:
    """Tier A (the hook), the pump, and one judgment at a time.

    ONE PUMP, NOT A TASK PER MESSAGE. A single lazily-created task means a
    bounded task count under a raid, at most one judgment in flight
    process-wide (so the spend rate is a property of the code rather than an
    argument about it), and no per-message task churn on the loop. The pump
    awaits each judgment serially: a queued judgment fires late, never twice.
    """

    def __init__(self, cfg, store, *, judge_fn=None, transport=None,
                 reader=None, clock=None, wall_clock=None):
        self.cfg = cfg
        self.store = store
        self.debouncer = Debouncer.from_cfg(cfg)
        self.buckets = TokenBuckets.from_cfg(cfg)
        self._judge_fn = judge_fn
        self._transport = transport
        # Context fidelity. The cache and the reader live for the PROCESS: the
        # gateway is long-lived, so one conversations.info per channel per TTL
        # (6h) is the whole channel-identity cost. Both are process-local
        # memory; nothing here is ever persisted.
        self._cache = aw_context.ContextCache()
        self._reader = reader
        self._clock = clock or time.monotonic
        self._wall = wall_clock or time.time
        self._task = None          # STRONG ref: create_task holds only a weak one
        self._loop_id = None
        self._kill_until = 0.0
        self._kill_cached = False
        #: Durable counter deltas accumulated during one drain and written once,
        #: off the loop thread, when it ends. See _bump / _flush_counters.
        self._counters: dict = {}
        self.stats = {
            "noted": 0, "dropped": 0, "judged": 0, "posted": 0, "withheld": 0,
            "declined": 0, "throttled": 0, "shadow": 0, "errors": 0,
            "post_failed": 0, "ineligible": 0,
        }
        self.last_judgment = None  # excerpt-free dict for aw_status.py

    # -- Tier A: in the hook, loop thread, microseconds, pure memory -------

    def note(self, event) -> bool:
        """Enqueue an arrival. Returns True when the map took it.

        Wrapped so it cannot raise into the gateway: an arrival-path bug must
        never change the recorder's verdict.
        """
        try:
            cfg = self.cfg
            if not getattr(cfg, "arrival_enabled", False):
                return False
            key = arrival_key(event, cfg)
            if key is None:
                return False
            channel, root, last_activity = key
            wall = self._wall()
            # Quiet hours: pure wall clock, and it saves map churn overnight.
            # Everything said then is still RECORDED — the sweep judges it in
            # the morning, which is one of the reasons the sweep stays.
            if aw_detectors._in_quiet_hours(cfg, wall):
                return False
            if self._kill_switch_cached(wall):
                return False
            if not self.debouncer.upsert(channel, root, self._clock(), last_activity):
                self.stats["dropped"] += 1
                return False
            self.stats["noted"] += 1
            self.ensure_pump()
            return True
        except Exception as exc:  # noqa: BLE001 — never raise into the loop
            self.stats["errors"] += 1
            logger.debug("ambient-watch: arrival note failed", exc_info=True)
            append_arrival_log(
                getattr(self.cfg, "data_dir", None),
                f"note failed ({type(exc).__name__}); the recorder's verdict is "
                f"unaffected and the sweep remains the backstop",
            )
            return False

    def _kill_switch_cached(self, wall: float) -> bool:
        """The kill switch, from a short TTL cache. Never blocks, fails closed.

        Two hazards are being avoided here, not one.

        A SQL read per inbound message is the first: it must not go on the
        loop's hot path at all, hence the TTL.

        The second is subtler and is the reason for ``kill_switch_nowait``:
        even ONE read can block. ``AmbientStore`` serialises a single
        connection behind an RLock, and a worker thread doing an
        ``asyncio.to_thread`` write can hold that lock for up to
        ``busy_timeout=5000`` ms while contending with the sweep process. A
        blocking acquire here would therefore stall dispatch — the exact thing
        the whole to_thread discipline exists to prevent, reintroduced by the
        one call that looked too cheap to matter. On contention we keep the
        previous answer AND the previous expiry, so the next message retries
        immediately rather than caching a stale verdict for the full TTL.

        Staleness is safe in only one direction, and that is the direction it
        can go: Tier A is an optimisation, and the pump re-reads the switch
        fresh, off-thread, before anything can be spent.
        """
        if wall < self._kill_until:
            return self._kill_cached
        try:
            value = self.store.kill_switch_nowait()
        except Exception:  # noqa: BLE001 — cannot tell -> halt, do not spend
            self._kill_cached = True
            self._kill_until = wall + KILL_CACHE_TTL_SECONDS
            return True
        if value is getattr(self.store, "LOCK_BUSY", None):
            return self._kill_cached  # busy: keep the answer, retry next message
        self._kill_cached = bool(value)
        self._kill_until = wall + KILL_CACHE_TTL_SECONDS
        return self._kill_cached

    # -- the pump ---------------------------------------------------------

    def ensure_pump(self):
        """Create the pump task lazily, on the loop that is actually running.

        ``register()`` may run before any loop exists or on the background
        discovery thread (hermes_cli/plugins.py:2302), and there is no
        ``gateway_started`` hook in ``VALID_HOOKS`` — so the hook body is the
        only place with a guaranteed running loop. The ``(loop identity, task)``
        check is what survives multiplex profiles and in-process restarts: a
        task bound to a dead loop would silently never run again.

        Loop identity is taken from the TASK, not from ``id(loop)``. An id is an
        address and CPython reuses addresses: a loop created after the previous
        one is freed can land on the same id, and an id comparison would then
        hand back a task bound to a closed loop — arrival mode silently dead
        until someone restarts the gateway. ``task.get_loop()`` cannot alias.
        """
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None  # no loop here; the sweep is the backstop
        task = self._task
        if task is not None and not task.done() and _task_loop(task) is loop:
            return task
        self._task = loop.create_task(self._pump())
        self._loop_id = id(loop)
        return self._task

    async def _pump(self):
        import asyncio

        interval = max(1.0, float(getattr(
            self.cfg, "arrival_pump_interval_seconds", 5)))
        idle = 0
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    await self.drain()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self.stats["errors"] += 1
                    self._log(f"pump iteration failed ({type(exc).__name__})")
                if len(self.debouncer):
                    idle = 0
                else:
                    idle += 1
                    if idle >= PUMP_IDLE_TICKS:
                        return  # dormant: ensure_pump() revives it on demand
        except asyncio.CancelledError:
            raise  # shutdown is not an outage

    async def drain(self, now: float | None = None):
        """Judge everything whose timer fired, serially. Never raises."""
        import asyncio

        now = self._clock() if now is None else now
        try:
            for key in self.debouncer.due(now):
                pending = self.debouncer.drop(key)
                if pending is None:
                    continue
                try:
                    await self.judge_one(pending)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — one thread, not the pump
                    self.stats["errors"] += 1
                    self._log(
                        f"judgment failed for {pending.channel}/{pending.root} "
                        f"({type(exc).__name__})"
                    )
        finally:
            # ONE durable counter write per drain, on a worker thread. drain() is
            # the only caller of judge_one, so this is the whole flush point.
            await self._flush_counters()

    # -- Tier B and C: one thread, one bounded call ------------------------

    async def judge_one(self, pending: Pending) -> str:
        """The ladder, in evaluation order. Nothing before rung 13 can spend."""
        import asyncio

        try:  # real loader: package-relative
            from . import aw_budget, aw_judge, aw_post
        except ImportError:  # cron shim / bare script
            import aw_budget
            import aw_judge
            import aw_post

        cfg, store = self.cfg, self.store
        channel, root = pending.channel, pending.root
        wall = self._wall()

        # 8. kill switch, read FRESH — a 3am --kill on must stop a queued
        #    judgment, so the TTL cache is deliberately not consulted here.
        if await asyncio.to_thread(store.kill_switch):
            self._log(f"kill-switch {channel}/{root} (nothing judged, nothing posted)")
            return "kill-switch"

        # 9/10. rate: ask both buckets without consuming, so an ineligible
        #       thread does not burn the channel's budget.
        if not self.buckets.available(channel, self._clock()):
            self.stats["throttled"] += 1
            self._bump(throttled=1)
            self._log(
                f"throttled {channel}/{root} -- the arrival rate bucket is empty "
                f"(this is what replaced the 15-minute cadence)"
            )
            return "throttled"

        # 11. THE SHARED LADDER, inherited whole. Off the loop: several queries
        #     plus the judge-view build.
        candidates = await asyncio.to_thread(
            aw_detectors.find_candidates, store, cfg, wall,
            only=(channel, root),
            min_age_seconds=float(getattr(cfg, "arrival_debounce_seconds", 90)),
            limit=1,
        )
        if not candidates:
            self.stats["ineligible"] += 1
            return "not-eligible"
        cand = candidates[0]

        # 12. budget — the last free check, immediately before the call.
        def _budget():
            budget = aw_budget.Budget(store, cfg.budget_cfg())
            return budget, budget.decision(channel, wall)

        budget, decision = await asyncio.to_thread(_budget)
        if decision in ("exceeded", "unconfigured"):
            # record_decline, never record_judgment: nothing was judged, so the
            # re-judge watermark must not be consumed by a cap that resets
            # tomorrow (or by a misconfiguration an operator will fix).
            await asyncio.to_thread(
                store.record_decline, channel, cand.thread_ts,
                f"declined-{decision}", excerpt=cand.excerpt, now=wall,
            )
            self.stats["declined"] += 1
            self._bump(declined=1)
            self._log(
                f"DECLINED {channel}/{cand.thread_ts} [{cand.kind}] -- "
                f"{'spend cap reached' if decision == 'exceeded' else 'no spend cap configured'} "
                f"(not judged, not posted)"
            )
            return f"declined-{decision}"

        # 13. take one token from each bucket. NEVER refunded.
        if not self.buckets.take(channel, self._clock()):
            self.stats["throttled"] += 1
            self._bump(throttled=1)
            return "throttled"

        # 13.5. CONTEXT FIDELITY, off the loop thread like every other blocking
        #       thing here (0 GETs in steady state, 2 typically, 4 at absolute
        #       worst — replies + history + a cold info + pins if an operator
        #       enabled them — plus a few narrow ledger reads, and the whole
        #       enrichment shares ONE context_total_timeout_seconds budget, so
        #       the latency bound does not grow with the count. Every mute lookup
        #       is an indexed primary-key hit.) It sits BELOW buckets.take so
        #       at most one enrichment exists per judgment — that is what makes
        #       Slack call volume inherit the buckets and the USD caps rather
        #       than following channel traffic. A rootless thread whose root
        #       cannot be verified is dropped here: without the root we cannot
        #       answer "is this thread bot-authored?", and guessing would be
        #       fail-open on the anti-feedback-loop rule.
        if getattr(cfg, "context_enabled", False):
            dropped = await asyncio.to_thread(
                self._enrich, cand, wall
            )
            if dropped:
                await asyncio.to_thread(
                    store.record_decline, channel, cand.thread_ts, dropped,
                    excerpt=cand.excerpt, now=wall,
                )
                self.stats["declined"] += 1
                self._bump(declined=1)
                self._log(
                    f"DROPPED {channel}/{cand.thread_ts} [{cand.kind}] -- "
                    f"{dropped} (not judged, not posted)"
                )
                return dropped

        # 14. the call. One nominee, no tools, JSON out.
        self.stats["judged"] += 1
        result = await self._judge([cand])

        # 15. meter usage — including the estimate on failure.
        usd = 0.0
        if result.prompt_tokens or result.completion_tokens:
            usd = await asyncio.to_thread(
                budget.record_usage, channel, result.model,
                result.prompt_tokens, result.completion_tokens, wall,
            )
        self._bump(judged=1, usd=round(float(usd or 0.0), 6))

        if result.error:
            # Provider detail goes to the breadcrumb, never to an ops channel.
            charged = ""
            if getattr(result, "estimated", False):
                charged = (
                    f" [charged ~{result.prompt_tokens} ESTIMATED prompt tokens: "
                    f"the provider reported no usage]"
                )
            self._log(
                f"judge unavailable for {channel}/{cand.thread_ts}, staying "
                f"silent: {result.error}{charged}"
            )

        outcome = "no-verdict"
        for verdict in result.verdicts:
            if (verdict.channel, verdict.thread_ts) != (channel, cand.thread_ts):
                continue  # not the thread this judgment nominated
            # 16/17. validated verdict -> the watermark.
            await asyncio.to_thread(
                store.record_judgment, channel, cand.thread_ts,
                "post" if verdict.should_post else "skip",
                confidence=verdict.confidence, reason=verdict.reason,
                nudge=verdict.nudge, excerpt=cand.excerpt,
                last_activity_seen=cand.last_activity, now=wall,
            )
            self.last_judgment = {
                "channel": channel, "thread_ts": cand.thread_ts,
                "kind": cand.kind, "verdict": "post" if verdict.should_post else "skip",
                "confidence": round(float(verdict.confidence), 3),
                "usd": round(float(usd or 0.0), 6), "at": wall,
            }
            if not verdict.should_post:
                self.stats["withheld"] += 1
                self._bump(withheld=1)
                self._log(
                    f"withheld {channel}/{cand.thread_ts} [{cand.kind}] "
                    f"conf={verdict.confidence:.2f} (judge said no)"
                )
                outcome = "withheld"
                continue
            # 18. confidence threshold.
            if verdict.confidence < cfg.judge_confidence_threshold:
                self.stats["withheld"] += 1
                self._bump(withheld=1)
                self._log(
                    f"withheld {channel}/{cand.thread_ts} [{cand.kind}] "
                    f"conf={verdict.confidence:.2f} "
                    f"(below {cfg.judge_confidence_threshold})"
                )
                outcome = "withheld"
                continue
            # 19. deliver, or shadow-mark.
            outcome = await self._deliver(cand, verdict, wall, aw_post)
        return outcome

    def _enrich(self, cand, wall) -> str:
        """``enrich_for_judgment`` for one nominee, on a worker thread.

        Returns the drop reason, or "" to judge it. Never raises: a context
        failure must be "judge with less context", never a lost judgment.
        """
        if self._reader is None:
            self._reader = aw_context.SlackReader.from_cfg(self.cfg)
        try:
            result = aw_context.enrich_for_judgment(
                [cand], self.cfg, self.store, self._cache, self._reader, wall
            )
        except Exception:  # noqa: BLE001
            logger.debug("ambient-watch: arrival enrichment failed", exc_info=True)
            return ""
        return result.dropped[0][1] if result.dropped else ""

    async def _judge(self, nominees):
        """One bounded structured call. No tools, JSON out, text in one thread.

        The failure accounting is here as well as inside ``ajudge`` because the
        token has ALREADY been taken by the time we get here: a transport that
        raises (an injected one, or an import error reaching
        ``async_call_llm``) must still charge ``estimate_prompt_tokens``, or a
        permanently broken provider is a free retry loop that the day's caps
        never notice. Same helper, so there is one implementation of it.
        """
        import asyncio

        try:  # real loader: package-relative
            from . import aw_judge
        except ImportError:
            import aw_judge

        try:
            if self._judge_fn is not None:
                return await self._judge_fn(nominees, self.cfg)
            return await aw_judge.ajudge(nominees, self.cfg)
        except asyncio.CancelledError:
            raise  # shutdown is not an outage: no post, no charge
        except Exception as exc:  # noqa: BLE001
            return aw_judge.result_from_failure(exc, nominees, self.cfg)

    async def _deliver(self, cand, verdict, wall, aw_post) -> str:
        """Shadow marks; live posts through the ONE outbound gate."""
        import asyncio

        if getattr(self.cfg, "mode", "shadow") != "live":
            # Shadow judges and pays (that is the point of the soak) and can
            # never post. post_nudge would refuse anyway — this branch simply
            # never asks it to.
            await asyncio.to_thread(
                self.store.mark_shadow_seen, cand.channel, cand.thread_ts, wall
            )
            self.stats["shadow"] += 1
            self._bump(shadow=1)
            self._log(
                f"WOULD HAVE POSTED to {cand.channel}/{cand.thread_ts} "
                f"[{cand.kind}]: {verdict.nudge}"
            )
            return "shadow"

        result = await asyncio.to_thread(
            self._post, aw_post, cand, verdict.nudge
        )
        if result.posted:
            self.stats["posted"] += 1
            self._bump(posted=1)
            self._log(
                f"POSTED to {cand.channel}/{cand.thread_ts} [{cand.kind}]: "
                f"{verdict.nudge}"
            )
            return "posted"
        self.stats["post_failed"] += 1
        self._bump(post_failed=1)
        self._log(
            f"POST REFUSED for {cand.channel}/{cand.thread_ts} [{cand.kind}]: "
            f"{result.reason}"
        )
        return f"post-refused:{result.reason}"

    def _post(self, aw_post, cand, nudge):
        """``post_nudge``, whole, on a worker thread.

        NOT SPLIT, NOT COPIED, NOT BYPASSED. Its refusals — shadow,
        channel-not-watched, empty, unsafe-text, muted, already-nudged,
        unknown-thread, answered-since-detection — are the outbound gate for
        BOTH triggers, and ``answered-since-detection`` is what makes a human
        replying during the debounce or during the in-flight call produce
        silence rather than a stale nudge.

        ``SlackTransport`` deliberately, not ``HermesTransport``: the latter's
        ``asyncio.run`` path raises inside a running loop and falls back to a
        fresh loop in a ThreadPoolExecutor (aw_post.py:140-154), which would
        drive the live adapter's aiohttp client from a foreign loop. The
        adapter's formatting adds nothing to one <=200-char plain-text line that
        ``sanitize_nudge`` already guarantees.
        """
        transport = self._transport or aw_post.SlackTransport()
        return aw_post.post_nudge(self.cfg, self.store, cand, nudge, transport)

    # -- bookkeeping ------------------------------------------------------

    def _bump(self, **deltas):
        """Accumulate durable counters IN MEMORY. Pure, non-blocking.

        This used to call ``store.bump_arrival_counters`` directly, which is a
        sqlite READ-MODIFY-WRITE inside ``with self._lock, self._db`` — a
        blocking write on the GATEWAY EVENT LOOP THREAD, since ``judge_one``
        runs in the pump coroutine. That is precisely what the to_thread
        discipline in this module exists to prevent (``busy_timeout=5000``: one
        write contended with the sweep process parks the caller for up to five
        seconds), and it was on the one path an attacker drives directly — the
        throttle rung fires once per refused thread, i.e. up to ``max_pending``
        times per debounce window.

        Deferring also collapses a raid's refusals into ONE write. Reporting is
        explicitly best effort, so drain-granularity durability is the right
        trade: the worst case is that a crash mid-drain costs the ops line a few
        counts, while the sweep's own report and the ledger stay authoritative.
        """
        for key, delta in deltas.items():
            try:
                self._counters[key] = self._counters.get(key, 0) + delta
            except TypeError:  # noqa: PERF203 — a bad delta must not lose the rest
                self._counters[key] = delta

    async def _flush_counters(self):
        """Write the accumulated counters once, off the loop thread."""
        import asyncio

        if not self._counters:
            return
        deltas, self._counters = self._counters, {}
        try:
            await asyncio.to_thread(self.store.bump_arrival_counters, **deltas)
        except asyncio.CancelledError:
            raise  # shutdown: the counts are lost, which is what best-effort means
        except Exception:  # noqa: BLE001 — reporting is never load-bearing
            logger.debug("ambient-watch: arrival counters not written", exc_info=True)

    def _log(self, detail: str):
        append_arrival_log(getattr(self.cfg, "data_dir", None), detail)

    # -- operator surface -------------------------------------------------

    def status(self) -> dict:
        now = self._clock()
        return {
            "enabled": bool(getattr(self.cfg, "arrival_enabled", False)),
            "pending": len(self.debouncer),
            "dropped": self.debouncer.dropped,
            "buckets": self.buckets.snapshot(now),
            "stats": dict(self.stats),
            "last_judgment": self.last_judgment,
        }
