"""Recorder: the pre_gateway_dispatch decision (Tier 0 — zero LLM/tools).

Adversarial-review corrections encoded here:
- Slash-command events PASS (second MessageEvent shape, adapter.py:7753).
- Mentions are detected in flat text AND Block Kit blocks (#52387),
  skipping rich_text_quote subtrees (quoted mentions are not mentions).
- Replies in threads we nudged PASS + record engagement + retire the
  intent (feedback loop for self-quiet; conversation continuation).

Fail-safe invariant: never fail open, never eat a genuine mention.
"""

from __future__ import annotations

import logging
import re
from enum import Enum

logger = logging.getLogger("ambient_watch")


class Decision(Enum):
    PASS = "pass"           # not ours — normal dispatch
    RECORD_SKIP = "skip"    # recorded; drop before auth/agent
    RECORD_PASS = "allow"   # recorded; continue to normal dispatch
    RECORD_REWRITE = "rewrite"  # recorded; replace text so the agent confirms


# In-channel control surface (Claude Tag parity). Anchored at the start of
# the message — after an optional bot mention or '!' — so ordinary prose that
# merely contains the word "mute" is never treated as a command.
_MUTE_CMD = re.compile(
    r"^\s*(?:<@[A-Z0-9]+>\s*|!)?\s*(?:hermes\s+)?ambient\s+(mute|unmute)\b",
    re.IGNORECASE,
)


def _mute_command(text: str):
    """Return 'mute' | 'unmute' | None for an ambient control message."""
    m = _MUTE_CMD.match(text or "")
    return m.group(1).lower() if m else None


def _channel_of(event) -> str:
    meta = getattr(event, "metadata", None) or {}
    return meta.get("slack_channel_id") or getattr(event.source, "chat_id", "") or ""


def _is_slash_command(event) -> bool:
    mt = getattr(event, "message_type", None)
    if getattr(mt, "name", "") == "COMMAND" or getattr(mt, "value", "") == "command":
        return True
    raw = getattr(event, "raw_message", None)
    return isinstance(raw, dict) and "command" in raw and "ts" not in raw


def _blocks_mention(blocks, bot_user_id: str) -> bool:
    """Recursive Block Kit walk; rich_text_quote subtrees are skipped."""
    if not isinstance(blocks, list):
        return False
    for el in blocks:
        if not isinstance(el, dict):
            continue
        if el.get("type") == "rich_text_quote":
            continue
        if el.get("type") == "user" and el.get("user_id") == bot_user_id:
            return True
        if _blocks_mention(el.get("elements"), bot_user_id):
            return True
    return False


def _is_mention(event, bot_user_id: str) -> bool:
    needle = f"<@{bot_user_id}>"
    raw = getattr(event, "raw_message", None)
    raw = raw if isinstance(raw, dict) else {}
    if needle in (event.text or "") or needle in (raw.get("text") or ""):
        return True
    return _blocks_mention(raw.get("blocks"), bot_user_id)


def _is_bot(event, bot_user_id: str) -> bool:
    raw = getattr(event, "raw_message", None)
    raw = raw if isinstance(raw, dict) else {}
    author = raw.get("user") or getattr(event, "user_id", None)
    return bool(raw.get("bot_id")) or raw.get("subtype") == "bot_message" or (
        author is not None and author == bot_user_id
    )


def arrival_key(event, cfg):
    """Tier A of the arrival path: ``(channel, thread_root, last_activity)``.

    Pure — no ledger, no clock, no I/O, no allocation beyond a tuple — because
    it runs inside ``pre_gateway_dispatch``, which Hermes invokes
    SYNCHRONOUSLY from ``async def _handle_message`` (``plugins.py``:
    ``ret = cb(**kwargs)``). Anything slow here delays every inbound message
    on every platform.

    Returns ``None`` for anything the arrival path must not enqueue.

    THE BOT RUNG IS LOAD-BEARING AND NOT REDUNDANT. ``decide()`` returns
    ``RECORD_SKIP`` for bot-authored messages too (see below), so enqueueing on
    ``RECORD_SKIP`` alone would let our OWN posted nudge re-trigger judgment on
    the thread it just landed in. ``has_intervention`` would stop the second
    post, so the symptom would be silent wasted spend rather than a visible
    loop — the worst shape of bug for this project. Hence an explicit rung
    here, and ``decide()``'s contract left byte-identical.
    """
    try:
        source = getattr(event, "source", None)
        platform = getattr(getattr(source, "platform", None), "value", None) or getattr(
            source, "platform", None
        )
        if str(platform) != "slack":
            return None
        if getattr(source, "chat_type", "") == "dm":
            return None
        if _is_slash_command(event):
            return None
        channel = _channel_of(event)
        if not channel or channel not in cfg.channels:
            return None
        if _is_bot(event, cfg.bot_user_id):
            return None
        if _is_mention(event, cfg.bot_user_id):
            return None
        if _mute_command(getattr(event, "text", "")):
            return None  # an in-channel control message, not a thread to judge
        raw = getattr(event, "raw_message", None)
        raw = raw if isinstance(raw, dict) else {}
        ts = getattr(event, "message_id", None) or raw.get("ts")
        if not ts:
            return None
        root = raw.get("thread_ts") or ts
        try:
            last_activity = float(ts)
        except (TypeError, ValueError):
            return None
        return str(channel), str(root), last_activity
    except Exception:  # noqa: BLE001 — never raise into the gateway loop
        return None


def decide(event, cfg, store) -> Decision:
    source = getattr(event, "source", None)
    platform = getattr(getattr(source, "platform", None), "value", None) or getattr(
        source, "platform", None
    )
    if str(platform) != "slack":
        return Decision.PASS
    if getattr(source, "chat_type", "") == "dm":
        return Decision.PASS
    if _is_slash_command(event):
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

        cmd = _mute_command(event.text)
        if cmd:
            scope = "thread" if thread_ts else "channel"
            if cmd == "mute":
                if thread_ts:
                    store.mute_thread(channel, thread_ts)
                else:
                    store.mute_channel(channel)
            else:
                if thread_ts:
                    store.unmute_thread(channel, thread_ts)
                else:
                    store.unmute_channel(channel)
            verb = "muted" if cmd == "mute" else "unmuted"
            return (
                Decision.RECORD_REWRITE,
                f"[ambient-watch] Ambient nudges are now {verb} for this {scope}. "
                f"Confirm this to the user in one short sentence and take no other action.",
            )

        if mention:
            return Decision.RECORD_PASS
        if thread_ts and store.is_engaged(channel, thread_ts):
            return Decision.RECORD_PASS
        if thread_ts and store.has_intervention(channel, thread_ts):
            # A human replied in a thread we nudged: engagement feedback for
            # self-quiet, and let the conversation flow — stock Hermes would
            # dispatch replies to bot-participated threads.
            store.record_engagement(channel, thread_ts)
            store.mark_engaged(channel, thread_ts)
            return Decision.RECORD_PASS
        return Decision.RECORD_SKIP
    except Exception:
        logger.exception("ambient-watch recorder failed; applying fail-safe")
        if mention and not is_bot:
            return Decision.RECORD_PASS
        return Decision.RECORD_SKIP
