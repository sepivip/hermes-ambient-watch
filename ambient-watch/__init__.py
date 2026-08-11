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
from .aw_guard import check_tool_call, data_dir_jail, looks_sensitive
from .aw_judge import AUX_TASK
from .aw_recorder import Decision, decide
from .aw_store import AmbientStore
from .gate import install_gate, purge_untrusted_artifacts, record_gate_error

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


def _register_jail_only(ctx):
    """Register the data-directory jail even with no usable config.

    Without this, a config failure would leave the ledger — which holds raw
    channel text — reachable from every tool call, i.e. "dormant" would be
    fail-OPEN for containment exactly as it once was for dispatch.
    ``data_dir`` is the only field the jail needs, and it is derivable
    without config.json.
    """
    from types import SimpleNamespace

    shim = SimpleNamespace(data_dir=hermes_home() / "plugin-data" / "ambient_watch")

    def on_pre_tool_call(tool_name="", args=None, **kwargs):
        try:
            return data_dir_jail(tool_name, args or {}, shim)
        except Exception:
            logger.exception("ambient-watch: jail error — failing closed")
            return {
                "action": "block",
                "message": (
                    "ambient-watch: data-directory jail errored; blocked "
                    "(fail closed)"
                ),
            }

    ctx.register_hook("pre_tool_call", on_pre_tool_call)


def _register_emergency_suppressor(ctx):
    _register_jail_only(ctx)
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
    # Remediate a payload left on disk by a pre-containment build without
    # waiting for the next sweep to come round.
    if purge_untrusted_artifacts(cfg):
        logger.warning(
            "ambient-watch: purged a legacy candidates.json holding verbatim "
            "channel text (it is no longer written; the sweep gets its "
            "payload on the gate's stdout)"
        )
    _warn_auth_posture()

    def on_pre_gateway_dispatch(event=None, **kwargs):
        if event is None:
            return None
        verdict = decide(event, cfg, store)
        if isinstance(verdict, tuple):
            # (RECORD_REWRITE, replacement_text) — an in-channel control
            # command; replace the text so the agent confirms it to the human.
            return {"action": "rewrite", "text": verdict[1]}
        if verdict is Decision.RECORD_SKIP:
            return {"action": "skip", "reason": "ambient-watch: recorded"}
        return None

    def on_pre_tool_call(tool_name="", args=None, session_id="", **kwargs):
        args = args or {}
        try:
            return check_tool_call(tool_name, args, cfg, store, session_id=session_id)
        except Exception:
            logger.exception("ambient-watch: tool guard error — failing closed")
            # A guard bug must not re-open the data-directory jail: re-check
            # with a cfg-free marker scan that cannot depend on the code that
            # just threw.
            try:
                sensitive = looks_sensitive(args)
            except Exception:
                sensitive = True
            if sensitive:
                return {
                    "action": "block",
                    "message": (
                        "ambient-watch: guard errored while checking a reference to "
                        "the ambient data directory; blocked (fail closed)"
                    ),
                }
            return None

    # Leak detector, not a meter. The sweep runs as a --no-agent cron job and
    # judges in its own process, so ambient must account for ZERO
    # agent-session tokens: anything this hook attributes to the sweep is a
    # bug (an agent session was created that should not exist). Armed only
    # when the operator sets sweep_job_id, because without it there is no way
    # to tell the sweep's session from any other cron job's.
    seen_anomalies: set = set()

    def on_post_api_request(usage=None, session_id="", platform="", model="", **kwargs):
        try:
            job_id = getattr(cfg, "sweep_job_id", "") or ""
            if not job_id or str(platform) != "cron":
                return None
            sid = str(session_id or "")
            if job_id not in sid or sid in seen_anomalies:
                return None
            if len(seen_anomalies) < 256:  # bounded: this is a long-lived process
                seen_anomalies.add(sid)
            total = 0
            if isinstance(usage, dict):
                total = int(usage.get("total_tokens") or 0)
            record_gate_error(
                f"ANOMALY: an agent session was billed for the ambient sweep "
                f"(session={sid}, model={model}, total_tokens={total}). The sweep "
                f"must run as --no-agent; ambient should spend zero agent tokens. "
                f"Check `hermes cron list` for a job that lost its no_agent flag.",
                data_dir=getattr(cfg, "data_dir", None),
            )
        except Exception:  # noqa: BLE001 — a detector must never break a call
            logger.debug("ambient-watch: leak detector failed", exc_info=True)
        return None

    ctx.register_hook("pre_gateway_dispatch", on_pre_gateway_dispatch)
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    try:
        ctx.register_hook("post_api_request", on_post_api_request)
    except Exception:  # noqa: BLE001 — older hosts may not know the hook
        logger.debug("ambient-watch: post_api_request hook unavailable", exc_info=True)

    # Declare the judge's auxiliary task so an operator can pin a cheap model
    # for ambient judgment in `hermes model -> Configure auxiliary models`,
    # independently of the main chat model. A duplicate/reserved key raises
    # ValueError; that must never take the whole plugin down with it.
    register_task = getattr(ctx, "register_auxiliary_task", None)
    if callable(register_task):
        try:
            register_task(
                key=AUX_TASK,
                display_name="Ambient judgment",
                description="ambient-watch nudge-worthiness + wording",
                defaults={
                    "provider": "auto",
                    "timeout": cfg.judge_timeout_seconds,
                    "max_tokens": cfg.judge_max_tokens,
                },
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "ambient-watch: could not register the %s auxiliary task; the "
                "judge will fall back to auto provider resolution", AUX_TASK,
                exc_info=True,
            )
    logger.info(
        "ambient-watch registered: %d watched channel(s), mode=%s",
        len(cfg.channels), cfg.mode,
    )
