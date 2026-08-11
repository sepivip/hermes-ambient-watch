"""ambient-watch: Claude-Tag-style ambient mode for Hermes Agent (Slack).

Wiring (contracts verified against hermes-agent v0.20.0):
- pre_gateway_dispatch: records watched-channel traffic, returns
  {"action": "skip"} for un-mentioned messages so nothing is answered
  and no tokens are spent; passes mentions through untouched.
- pre_tool_call: pins ambient send_message calls to armed intent targets.

Requires the watched channels to ALSO be listed in the Slack platform's
``free_response_channels`` (that is what makes the adapter emit
un-mentioned messages at all). See README for the coupling check.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from aw_config import load_config  # noqa: E402
from aw_guard import check_tool_call  # noqa: E402
from aw_recorder import Decision, decide  # noqa: E402
from aw_store import AmbientStore  # noqa: E402

logger = logging.getLogger("ambient_watch")

_cfg = None
_store = None


def _state():
    global _cfg, _store
    if _cfg is None:
        _cfg = load_config()
        _store = AmbientStore(_cfg.data_dir / "ambient.db")
    return _cfg, _store


def register(ctx):
    try:
        cfg, store = _state()
    except Exception:
        logger.exception(
            "ambient-watch: config missing/invalid — plugin stays dormant "
            "(fail closed; no hooks registered)"
        )
        return

    def on_pre_gateway_dispatch(event=None, **kwargs):
        if event is None:
            return None
        verdict = decide(event, cfg, store)
        if verdict is Decision.RECORD_SKIP:
            return {"action": "skip", "reason": "ambient-watch: recorded"}
        return None

    def on_pre_tool_call(tool_name="", args=None, **kwargs):
        try:
            return check_tool_call(tool_name, args or {}, cfg, store)
        except Exception:
            logger.exception("ambient-watch: tool guard error — blocking send")
            if tool_name == "send_message":
                return {
                    "action": "block",
                    "message": "ambient-watch: guard errored; send blocked (fail closed)",
                }
            return None

    ctx.register_hook("pre_gateway_dispatch", on_pre_gateway_dispatch)
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    logger.info(
        "ambient-watch registered: %d watched channel(s), mode=%s",
        len(cfg.channels), cfg.mode,
    )
