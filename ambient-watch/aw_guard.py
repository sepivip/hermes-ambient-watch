"""Tool guard: pre_tool_call target pinning.

Real send_message target grammar (tools/send_message_tool.py:221, :365):
``platform``, ``platform:chat_id``, or ``platform:chat_id:thread_id`` —
platform prefix FIRST. A Slack thread post is ``slack:C…:<thread_ts>``.
Armed intents are stored as bare refs ``C…:<thread_ts>``.

While intents are (or ever were) armed, sends INTO a watched channel
must match a pending intent; ambiguous name-form slack targets are
blocked outright (fail closed — a name can resolve into a watched
channel after directory lookup, evading an ID comparison).
"""

from __future__ import annotations

_ID_PREFIXES = ("C", "G")


def check_tool_call(tool_name: str, args: dict, cfg, store):
    if tool_name != "send_message":
        return None
    if not store.any_intents():
        return None  # never armed -> dormant

    target = str((args or {}).get("target") or (args or {}).get("to") or "")
    platform, _, ref = target.partition(":")
    if platform.strip().lower() != "slack":
        return None  # other platforms are not ambient's business
    ref = ref.strip()
    channel_part = ref.split(":", 1)[0].strip()

    if channel_part == cfg.ops_channel:
        return None  # shadow digests / ops alerts always allowed

    id_form = (
        len(channel_part) >= 9
        and channel_part[:1] in _ID_PREFIXES
        and channel_part.isalnum()
    )
    if not id_form:
        # "slack:#name" / "slack:@handle" / bare "slack" while armed:
        # cannot be compared against the channel allowlist -> fail closed.
        return {
            "action": "block",
            "message": (
                f"ambient-watch: ambiguous slack target {target!r} while ambient "
                "intents are armed. Use an explicit channel-ID target."
            ),
        }
    if channel_part not in cfg.channels:
        return None  # not a watched channel

    pending = set(store.pending_intents())
    if ref in pending:
        return None
    return {
        "action": "block",
        "message": (
            f"ambient-watch: send_message target {target!r} is not an armed "
            "ambient intent for this watched channel. Allowed targets: "
            + (", ".join(f"slack:{p}" for p in sorted(pending)) if pending else "(none)")
        ),
    }
