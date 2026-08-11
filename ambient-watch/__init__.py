"""ambient-watch: Claude-Tag-style ambient mode for Hermes Agent (Slack).

Loaded by the real Hermes loader as package ``hermes_plugins.<name>``
with ``__path__ = [plugin_dir]`` — hence relative imports and no
sys.path mutation (adversarial-review finding: the old sys.path hack
leaked flat module names process-wide).

Config-failure posture (review finding: "dormant" was fail-OPEN for
channels left in free_response_channels):
1. healthy config.json  -> normal wiring; persist a last-known-good copy
2. corrupt config.json  -> fall back to the LKG copy, log ERROR
3. nothing usable       -> register an emergency suppressor that skips
   un-mentioned traffic in whatever channels Hermes' own config.yaml
   lists under free_response_channels
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path

from .aw_config import hermes_home, load_config
from .aw_guard import check_tool_call
from .aw_recorder import Decision, decide
from .aw_store import AmbientStore
from .gate import install_gate

logger = logging.getLogger("ambient_watch")


def _config_paths():
    data_dir = hermes_home() / "plugin-data" / "ambient_watch"
    return data_dir / "config.json", data_dir / "config.json.lkg"


def _load_config_with_fallback():
    cfg_path, lkg_path = _config_paths()
    try:
        cfg = load_config(cfg_path)
        try:
            if not lkg_path.exists() or lkg_path.read_bytes() != cfg_path.read_bytes():
                shutil.copyfile(cfg_path, lkg_path)
        except OSError:
            logger.warning("ambient-watch: could not persist last-known-good config")
        return cfg
    except Exception:
        logger.exception("ambient-watch: config.json unusable, trying last-known-good")
    try:
        cfg = load_config(lkg_path)
        logger.error(
            "ambient-watch: RUNNING ON LAST-KNOWN-GOOD CONFIG — fix %s", cfg_path
        )
        return cfg
    except Exception:
        return None


def _read_free_response_channels(config_yaml: Path) -> set:
    """Best-effort read of slack.free_response_channels from Hermes config."""
    try:
        text = config_yaml.read_text(encoding="utf-8")
    except OSError:
        return set()
    try:
        import yaml  # available in the Hermes venv

        data = yaml.safe_load(text) or {}
        slack = data.get("slack") or (data.get("platforms") or {}).get("slack") or {}
        frc = slack.get("free_response_channels") or []
        if isinstance(frc, (list, tuple)):
            return {str(c).strip() for c in frc if str(c).strip()}
    except Exception:  # noqa: BLE001 — fall through to the regex parse
        pass
    m = re.search(r"free_response_channels:\s*\[([^\]]*)\]", text)
    if not m:
        return set()
    return {p.strip().strip("'\"") for p in m.group(1).split(",") if p.strip().strip("'\"")}


def _register_emergency_suppressor(ctx):
    frc = _read_free_response_channels(hermes_home() / "config.yaml")
    logger.error(
        "ambient-watch: NO USABLE CONFIG. Emergency suppressor active for "
        "free_response_channels=%s — un-mentioned traffic there is dropped so a "
        "broken ambient config cannot turn Hermes into answer-everything. "
        "Fix plugin-data/ambient_watch/config.json.",
        sorted(frc),
    )
    if not frc:
        return

    def on_pre_gateway_dispatch(event=None, **kwargs):
        try:
            if event is None:
                return None
            source = getattr(event, "source", None)
            platform = getattr(getattr(source, "platform", None), "value", None)
            if str(platform) != "slack":
                return None
            if getattr(source, "chat_type", "") == "dm":
                return None
            meta = getattr(event, "metadata", None) or {}
            channel = meta.get("slack_channel_id") or getattr(source, "chat_id", "")
            if channel not in frc:
                return None
            raw = getattr(event, "raw_message", None)
            raw = raw if isinstance(raw, dict) else {}
            text = f"{event.text or ''} {raw.get('text') or ''}"
            if "<@" in text:
                return None  # mention-shaped: better to over-answer than to eat it
            return {"action": "skip", "reason": "ambient-watch: emergency suppressor"}
        except Exception:
            return {"action": "skip", "reason": "ambient-watch: emergency suppressor"}

    ctx.register_hook("pre_gateway_dispatch", on_pre_gateway_dispatch)


def _warn_auth_posture():
    allow_all = os.environ.get("SLACK_ALLOW_ALL_USERS", "").lower() in ("1", "true", "yes")
    allowed = os.environ.get("SLACK_ALLOWED_USERS", "")
    if not allow_all and "*" not in allowed:
        logger.warning(
            "ambient-watch: Slack auth is allowlist-restricted — the adapter "
            "rejects non-allowlisted senders BEFORE pre_gateway_dispatch, so "
            "ambient recording only sees allowlisted users' messages. For full "
            "channel coverage set SLACK_ALLOW_ALL_USERS=true or '*' in "
            "SLACK_ALLOWED_USERS (mention-response auth still applies downstream)."
        )


def register(ctx):
    cfg = _load_config_with_fallback()
    if cfg is None:
        _register_emergency_suppressor(ctx)
        return

    store = AmbientStore(cfg.data_dir / "ambient.db")
    try:
        install_gate()
    except Exception:
        logger.exception("ambient-watch: could not install the cron gate shim")
    _warn_auth_posture()

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
