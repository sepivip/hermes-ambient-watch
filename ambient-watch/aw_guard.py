"""Tool guard: pre_tool_call target pinning.

While ambient intents are armed, any send_message aimed INTO a watched
channel must exactly match a pending intent target (C<chan>:<thread_ts>).
This structurally blocks wrong-thread posts (#15927 lineage) and
injection-directed posting into watched channels. Traffic to unwatched
channels and to the ops channel is not constrained.
"""

from __future__ import annotations


def check_tool_call(tool_name: str, args: dict, cfg, store):
    if tool_name != "send_message":
        return None
    if not store.any_intents():
        return None  # never armed -> dormant

    target = str((args or {}).get("target") or (args or {}).get("to") or "")
    channel_part = target.split(":", 1)[0].strip()

    if channel_part == cfg.ops_channel:
        return None  # shadow digests / ops alerts always allowed
    if channel_part not in cfg.channels:
        return None  # not ambient's business

    pending = set(store.pending_intents())
    if target in pending:
        return None
    return {
        "action": "block",
        "message": (
            f"ambient-watch: send_message target {target!r} is not an armed "
            "ambient intent for this watched channel. Allowed targets: "
            + (", ".join(sorted(pending)) if pending else "(none)")
        ),
    }
