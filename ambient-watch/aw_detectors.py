"""Deterministic prefilter: which threads are worth PAYING to judge.

Zero-token stage. It settles only facts SQL and a wall clock can settle —
never "is this useful?", which is the judge's job (aw_judge.py). The regex
that used to decide who got nudged (a ``?`` plus an ask-language pattern) is
gone: it could not tell a blocked engineer from a joke, and it was the whole
of the plugin's "judgment".

What survives here, and why each one is not a crutch:

* channel allowlist, channel/thread mutes, quiet hours — operator intent
* root ``is_bot`` — never nudge a bot's thread (also the anti-feedback-loop
  rule now that nothing else is behind it: our own nudge is a channel
  message the recorder sees)
* ``is_engaged`` — the bot already converses there
* ``has_intervention`` — once per thread, forever
* ``is_shadow_seen`` — shadow dedupe, so a soak reports each thread once
* ``channel_self_quieted`` — N ignored nudges and we stop
* ``min_age_minutes`` — a thread that is still moving does not need us
* ``needs_judgment`` — the re-judge WATERMARK. This is what replaced the
  120-minute cooldown: a declined thread is not judged again until a human
  says something new, so no thread can bill us repeatedly for existing.

DELETED: ``cooldown_minutes``, ``caps_per_channel_per_day``,
``caps_global_per_day``. They were proxies for a spend limit that now exists
(aw_budget). Keeping them would cap nudges by count while the real resource
— money — went unmetered, which is exactly backwards.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

try:  # real loader: package-relative
    from . import aw_sanitize
except ImportError:  # cron shim: flat import after sys.path.insert(plugin_dir)
    import aw_sanitize

logger = logging.getLogger("ambient_watch")


@dataclass
class Candidate:
    channel: str
    thread_ts: str
    kind: str
    target: str
    excerpt: str            # compact export profile — stored for operator review
    judge_view: str = ""    # richer in-process profile — what the judge reads
    human_participants: int = 1
    idle_minutes: int = 0
    last_activity: float = 0.0
    messages: list = field(default_factory=list)


def _local_minutes(now: float, tz_name: str) -> int:
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(tz_name)
    except Exception:
        logger.warning("ambient-watch: timezone %r unavailable, using UTC", tz_name)
        tz = timezone.utc
    local = datetime.fromtimestamp(now, tz)
    return local.hour * 60 + local.minute


def _in_quiet_hours(cfg, now: float) -> bool:
    def parse(s):
        h, m = s.split(":")
        return int(h) * 60 + int(m)

    start, end = parse(cfg.quiet_start), parse(cfg.quiet_end)
    lm = _local_minutes(now, cfg.quiet_tz)
    if start == end:
        return False
    if start < end:
        return start <= lm < end
    return lm >= start or lm < end  # window wraps midnight


def find_candidates(store, cfg, now: float) -> list[Candidate]:
    """Nominate threads for judgment. Cheap, deterministic, zero tokens."""
    if _in_quiet_hours(cfg, now):
        return []

    shadow = cfg.mode != "live"
    out: list[Candidate] = []

    for channel in sorted(cfg.channels):
        if store.is_channel_muted(channel):
            continue  # someone muted the whole channel from Slack
        if store.channel_self_quieted(channel, cfg.self_quiet_after_ignored):
            continue

        best: Candidate | None = None
        for root in store.thread_roots(channel):
            root_ts = root["ts"]
            if root["is_bot"]:
                continue
            if store.is_muted(channel, root_ts):
                continue
            if store.has_intervention(channel, root_ts):
                continue
            if store.is_engaged(channel, root_ts):
                continue  # the bot already converses there — never nudge
            if shadow and store.is_shadow_seen(channel, root_ts):
                continue  # already reported in a shadow digest

            msgs = store.thread_messages(channel, root_ts)
            if not msgs:
                continue
            last_activity = max(float(m["ts"]) for m in msgs)
            if (now - last_activity) < cfg.min_age_minutes * 60:
                continue  # still moving — leave it alone
            if not store.needs_judgment(
                channel, root_ts, last_activity, cfg.judge_max_rejudge
            ):
                continue  # judged already, nothing new said since

            humans = {m["author"] for m in msgs if not m["is_bot"] and m["author"]}
            # `kind` is descriptive only now — it labels the audit trail and
            # the intervention row. It no longer decides anything.
            kind = (
                "unanswered_question"
                if not [m for m in msgs if m["ts"] != root_ts and not m["is_bot"]]
                else "stalled_thread"
            )
            cand = Candidate(
                channel=channel,
                thread_ts=root_ts,
                kind=kind,
                target=f"{channel}:{root_ts}",
                excerpt=aw_sanitize.build_excerpt(m["text"] for m in msgs[:6]),
                judge_view=aw_sanitize.build_judge_view(msgs),
                human_participants=len(humans),
                idle_minutes=int((now - last_activity) / 60),
                last_activity=last_activity,
                messages=msgs,
            )
            # At most ONE candidate per channel per sweep (throughput limit).
            # The most engaged, most recently active thread wins: a
            # three-day-dead thread should not outrank one that stalled an
            # hour ago with four people in it.
            if best is None or (cand.human_participants, cand.last_activity) > (
                best.human_participants, best.last_activity
            ):
                best = cand
        if best is not None:
            out.append(best)

    out.sort(key=lambda c: (-c.human_participants, -c.last_activity))
    return out[: cfg.candidates_per_run]
