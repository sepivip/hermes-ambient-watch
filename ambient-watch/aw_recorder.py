"""Recorder: the pre_gateway_dispatch decision.

Tier 0 of the design: zero LLM, zero tools. Decides, per incoming
MessageEvent, whether ambient-watch records it, and whether normal
dispatch continues (PASS) or stops (SKIP).

Fail-safe invariant: an internal error must never fail open (dispatching
un-mentioned free_response traffic to the agent) and must never eat a
genuine mention.
"""

from __future__ import annotations

import logging
from enum import Enum

logger = logging.getLogger("ambient_watch")


class Decision(Enum):
    PASS = "pass"           # not ours — normal dispatch
    RECORD_SKIP = "skip"    # recorded; drop before auth/agent
    RECORD_PASS = "allow"   # recorded; continue to normal dispatch


def _channel_of(event) -> str:
    meta = getattr(event, "metadata", None) or {}
    return meta.get("slack_channel_id") or getattr(event.source, "chat_id", "") or ""


def _is_mention(event, bot_user_id: str) -> bool:
    needle = f"<@{bot_user_id}>"
    raw = getattr(event, "raw_message", None) or {}
    raw_text = raw.get("text") or "" if isinstance(raw, dict) else ""
    return needle in (event.text or "") or needle in raw_text


def _is_bot(event, bot_user_id: str) -> bool:
    raw = getattr(event, "raw_message", None) or {}
    if not isinstance(raw, dict):
        raw = {}
    author = raw.get("user") or getattr(event, "user_id", None)
    return bool(raw.get("bot_id")) or raw.get("subtype") == "bot_message" or (
        author is not None and author == bot_user_id
    )


def decide(event, cfg, store) -> Decision:
    source = getattr(event, "source", None)
    platform = getattr(getattr(source, "platform", None), "value", None) or getattr(
        source, "platform", None
    )
    if str(platform) != "slack":
        return Decision.PASS
    if getattr(source, "chat_type", "") == "dm":
        return Decision.PASS
    channel = _channel_of(event)
    if channel not in cfg.channels:
        return Decision.PASS

    mention = _is_mention(event, cfg.bot_user_id)
    is_bot = _is_bot(event, cfg.bot_user_id)

    try:
        raw = event.raw_message if isinstance(event.raw_message, dict) else {}
        ts = getattr(event, "message_id", None) or raw.get("ts")
        thread_ts = raw.get("thread_ts")
        author = raw.get("user") or getattr(event, "user_id", None)
        root = thread_ts or ts

        if mention and not is_bot:
            store.mark_engaged(channel, root)

        store.record_message(
            channel=channel,
            ts=ts,
            thread_ts=thread_ts,
            author=author,
            is_bot=is_bot,
            is_mention=mention,
            text=event.text,
        )

        if is_bot:
            return Decision.RECORD_SKIP
        if mention:
            return Decision.RECORD_PASS
        if thread_ts and store.is_engaged(channel, thread_ts):
            # Preserve stock thread-following: once the bot was mentioned in
            # a thread, later un-mentioned replies still reach the agent.
            return Decision.RECORD_PASS
        return Decision.RECORD_SKIP
    except Exception:
        logger.exception("ambient-watch recorder failed; applying fail-safe")
        # Fail safe: never fail open, never eat a real mention.
        if mention and not is_bot:
            return Decision.RECORD_PASS
        return Decision.RECORD_SKIP
