"""Deterministic detectors: which threads deserve a nudge candidate.

Zero-token stage — pure SQL over the ledger plus wall-clock gates
(quiet hours, cooldowns, caps, self-quiet). The LLM judge only ever sees
what survives this file.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger("ambient_watch")

_ASK_LANGUAGE = re.compile(
    r"\?|(\b(decide|decision|should we|blocked|waiting on|waiting for|wdyt|"
    r"thoughts|need(s)? (a )?(review|approval|answer|decision))\b)",
    re.IGNORECASE,
)


@dataclass
class Candidate:
    channel: str
    thread_ts: str
    kind: str
    target: str
    excerpt: str


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


def _is_question(text: str) -> bool:
    return "?" in (text or "")


def _has_ask_language(text: str) -> bool:
    return bool(_ASK_LANGUAGE.search(text or ""))


def find_candidates(store, cfg, now: float) -> list[Candidate]:
    if _in_quiet_hours(cfg, now):
        return []
    day_ago = now - 86400

    # Live gates on real interventions; shadow gates on its own shadow_seen
    # ledger so a soak reproduces the digest volume live would actually post.
    # Without this, shadow silently had NO cooldown and NO caps (interventions
    # is empty by design), so the soak over-reported and precision tuning
    # would be done against a volume live could never produce.
    shadow = cfg.mode != "live"
    if shadow:
        global_since = store.global_shadow_seen_since
        channel_since = store.shadow_seen_since
        last_at = store.last_shadow_seen_at
    else:
        global_since = store.global_interventions_since
        channel_since = store.interventions_since
        last_at = store.last_intervention_at

    if global_since(day_ago) >= cfg.caps_global_per_day:
        return []

    out: list[Candidate] = []
    for channel in sorted(cfg.channels):
        if store.is_channel_muted(channel):
            continue  # someone muted the whole channel from Slack
        if store.channel_self_quieted(channel, cfg.self_quiet_after_ignored):
            continue
        last = last_at(channel)
        if last is not None and (now - last) < cfg.cooldown_minutes * 60:
            continue
        if channel_since(channel, day_ago) >= cfg.caps_per_channel_per_day:
            continue

        channel_found = False
        for root in store.thread_roots(channel):
            if channel_found:
                break  # at most ONE candidate per channel per sweep
            root_ts = root["ts"]
            if root["is_bot"]:
                continue
            if store.is_muted(channel, root_ts):
                continue
            if store.has_intervention(channel, root_ts):
                continue
            if store.is_engaged(channel, root_ts):
                continue  # the bot already converses there — never nudge
            if cfg.mode == "shadow" and store.is_shadow_seen(channel, root_ts):
                continue  # already reported in a shadow digest

            msgs = store.thread_messages(channel, root_ts)
            human_replies = [
                m for m in msgs if m["ts"] != root_ts and not m["is_bot"]
            ]
            last_activity = max(float(m["ts"]) for m in msgs)
            age_root = now - float(root_ts)
            age_last = now - last_activity

            kind = None
            if (
                _is_question(root["text"])
                and not human_replies
                and age_root >= cfg.unanswered_after_minutes * 60
            ):
                kind = "unanswered_question"
            elif (
                any(_has_ask_language(m["text"]) for m in msgs)
                and age_last >= cfg.stalled_after_minutes * 60
            ):
                kind = "stalled_thread"
            if kind is None:
                continue

            excerpt = " | ".join(
                (m["text"] or "")[:280] for m in msgs[:6]
            )
            out.append(
                Candidate(
                    channel=channel,
                    thread_ts=root_ts,
                    kind=kind,
                    target=f"{channel}:{root_ts}",
                    excerpt=excerpt,
                )
            )
            channel_found = True

    out.sort(key=lambda c: float(c.thread_ts))
    return out[: cfg.candidates_per_run]
