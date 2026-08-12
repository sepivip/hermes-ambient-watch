"""Live delivery — post one nudge into the originating Slack thread.

WHY NOT THE COMPOSE AGENT: cron/scheduler.py:182 hardcodes ``messaging``
into every cron session's disabled toolsets, and a user's
``agent.disabled_toolsets`` layers on top so a per-job ``enabled_toolsets``
cannot re-widen it. No cron agent can send a Slack message, ever. The
original design ("the compose agent calls send_message with a pinned
target") was impossible, and aw_guard's target pinning was a dormant no-op.

That constraint improves the architecture: because delivery happens here,
outside any agent, no tool-bearing session needs the untrusted excerpt at
all — the containment problem is retired rather than mitigated.

Every refusal is explicit and fails closed. The freshness re-check is the
one that protects trust: a nudge that lands seconds after a human already
answered is worse than no nudge at all.

THIS IS THE OUTBOUND INVARIANT'S REAL HOME. ``aw_guard`` used to express it
as ``send_message`` target pinning, on a tool no cron session can call — a
no-op dressed as a control. Here the checks sit in the actual send path, so
they can actually fire: watched channel only, thread must exist in our own
ledger, once per thread, not muted, not already answered, live mode only,
and the text must survive ``aw_sanitize.sanitize_nudge``.

Two transports, tried in order:

1. ``HermesTransport`` — Hermes' own ``_send_to_platform`` (the same
   function the cron scheduler calls at cron/scheduler.py:2178). It routes
   through the live in-process Slack adapter when the gateway hosts the
   tick and falls back to the plugin's standalone ``chat.postMessage``
   otherwise. Preferred: it is the supported seam and it inherits Slack's
   configured behaviour.
2. ``SlackTransport`` — a direct ``chat.postMessage`` for when Hermes is not
   importable (a bare script run, a test, a broken install).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from dataclasses import dataclass
from pathlib import Path

try:  # real loader: package-relative
    from . import aw_sanitize
except ImportError:  # cron shim: flat import after sys.path.insert(plugin_dir)
    import aw_sanitize

logger = logging.getLogger("ambient_watch")

_SLACK_POST_URL = "https://slack.com/api/chat.postMessage"


def _ts_float(value, default=None):
    """Slack ts string -> float, or ``default`` when it cannot be read.

    Used by the freshness re-check, where an unreadable timestamp must refuse
    the post rather than raise: post_nudge runs inside the sweep, and an
    exception here would fail the whole tick instead of one candidate.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class PostResult:
    posted: bool
    reason: str
    channel: str = ""
    thread_ts: str = ""


class SlackTransport:
    """Minimal chat.postMessage caller. Token is read from Hermes' .env."""

    def __init__(self, token: str | None = None, home: Path | None = None):
        self._token = token or _bot_token(home)

    def post(self, channel: str, thread_ts: str, text: str) -> dict:
        if not self._token:
            return {"ok": False, "error": "no_slack_bot_token"}
        body = json.dumps(
            {
                "channel": channel,
                "thread_ts": thread_ts,
                "text": text,
                # Keep the nudge in-thread; never fan it out to the channel.
                "reply_broadcast": False,
                "unfurl_links": False,
                "unfurl_media": False,
            }
        ).encode()
        req = urllib.request.Request(
            _SLACK_POST_URL,
            data=body,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode())
        except Exception as exc:  # noqa: BLE001 — a failed post must not raise
            return {"ok": False, "error": f"transport:{type(exc).__name__}"}


class HermesTransport:
    """Post via Hermes' own sender. Dual-mode by design (see module docstring)."""

    def post(self, channel: str, thread_ts: str, text: str) -> dict:
        import asyncio

        # A cron script's env has SLACK_BOT_TOKEN stripped (it is category
        # "messaging", hence in _HERMES_PROVIDER_ENV_BLOCKLIST). This is the
        # supported restore, and the same call the scheduler makes for
        # no_agent jobs at cron/scheduler.py:3222.
        try:
            from hermes_cli.env_loader import load_hermes_dotenv

            load_hermes_dotenv(hermes_home=str(_home()))
        except Exception:  # noqa: BLE001 — may already be populated
            pass

        from gateway.config import Platform, load_gateway_config
        from tools.send_message_tool import _send_to_platform

        config = load_gateway_config()
        pconfig = config.platforms.get(Platform.SLACK)
        if not pconfig or not getattr(pconfig, "enabled", False):
            return {"ok": False, "error": "slack_platform_not_enabled"}
        coro = _send_to_platform(
            Platform.SLACK, pconfig, channel, text, thread_id=thread_ts
        )
        try:
            result = asyncio.run(coro)
        except RuntimeError:
            # asyncio.run refuses inside a running loop and never started the
            # coroutine — close it, then run it on a thread with no loop.
            coro.close()
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(
                    asyncio.run,
                    _send_to_platform(
                        Platform.SLACK, pconfig, channel, text, thread_id=thread_ts
                    ),
                ).result(timeout=60)
        # _send_to_platform returns a truthy result on success; normalize.
        return {"ok": bool(result), "raw": str(result)[:200]}


def default_transport():
    """Hermes' sender when importable, a direct Slack call otherwise."""
    try:
        import tools.send_message_tool  # noqa: F401 — availability probe
    except Exception:  # noqa: BLE001
        return SlackTransport()
    return HermesTransport()


def _home() -> Path:
    try:
        from .aw_config import hermes_home
    except ImportError:
        from aw_config import hermes_home
    return hermes_home()


def _bot_token(home: Path | None = None) -> str:
    env = os.environ.get("SLACK_BOT_TOKEN")
    if env:
        return env
    # Prefer Hermes' own loader; only hand-parse .env if it is unavailable.
    try:
        from hermes_cli.env_loader import load_hermes_dotenv

        load_hermes_dotenv(hermes_home=str(home or _home()))
        env = os.environ.get("SLACK_BOT_TOKEN")
        if env:
            return env
    except Exception:  # noqa: BLE001
        pass
    try:
        home = home or _home()
        for line in (Path(home) / ".env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("SLACK_BOT_TOKEN=") and not line.startswith("#"):
                return line.partition("=")[2].strip()
    except OSError:
        pass
    return ""


def post_nudge(cfg, store, candidate, text: str, transport=None) -> PostResult:
    channel = candidate.channel
    thread_ts = candidate.thread_ts
    fail = lambda why: PostResult(False, why, channel, thread_ts)  # noqa: E731

    # Shadow mode must be structurally unable to write to a watched channel.
    if getattr(cfg, "mode", "shadow") != "live":
        return fail("shadow-mode")
    if channel not in cfg.channels:
        return fail("channel-not-watched")
    if not (text or "").strip():
        return fail("empty-text")
    # Final outbound gate: the wording is model-authored but
    # attacker-INFLUENCED, and the gate is not the only caller.
    safe = aw_sanitize.sanitize_nudge(text)
    if not safe:
        return fail("unsafe-text")
    if store.is_channel_muted(channel) or store.is_muted(channel, thread_ts):
        return fail("muted")
    # Once-per-thread, checked before the send so a retry cannot double-post.
    if store.has_intervention(channel, thread_ts):
        return fail("already-nudged")

    # The thread must be one WE recorded. Without this, any (channel,
    # thread_ts) pair reaching this function would post — the invariant
    # aw_guard's dead target pinning was supposed to express.
    msgs = store.thread_messages(channel, thread_ts)
    if not msgs:
        return fail("unknown-thread")

    # Freshness re-check: the ledger is fed live by the recorder, so a human
    # reply that landed between detection and now is already visible here.
    # It is measured against the activity the DETECTOR saw (candidate.
    # last_activity), not against "any reply at all": a stalled thread is a
    # thread that already has replies, and comparing against zero refused
    # every one of them -- the gate paid to judge them and then could never
    # post. A candidate carrying no watermark falls back to 0.0, i.e. the
    # strictest reading, so an unknown caller cannot post into a thread that
    # has been replied to.
    # An unreadable ts counts as newer (inf), and a malformed watermark counts
    # as zero, so both unknowns resolve to "refuse to post".
    seen_through = _ts_float(getattr(candidate, "last_activity", 0.0), default=0.0) or 0.0
    if any(
        m["ts"] != thread_ts
        and not m["is_bot"]
        and _ts_float(m["ts"], default=float("inf")) > seen_through
        for m in msgs
    ):
        return fail("answered-since-detection")

    transport = transport or default_transport()
    resp = transport.post(channel, thread_ts, safe)
    if not resp.get("ok"):
        # Deliberately no intervention row: a failed send must leave the
        # thread eligible, or a transient Slack error silently burns it.
        logger.warning(
            "ambient-watch: nudge failed for %s/%s: %s",
            channel, thread_ts, resp.get("error"),
        )
        return fail(f"post-failed:{resp.get('error')}")

    # Keep the ts Slack gave OUR message: it is the anchor reaction-gated
    # escalation compares against, so a reaction only counts as a human
    # invocation when it lands on something we actually posted.
    store.record_intervention(
        channel, thread_ts, kind=candidate.kind, nudge_ts=resp.get("ts")
    )
    logger.info("ambient-watch: nudged %s/%s [%s]", channel, thread_ts, candidate.kind)
    return PostResult(True, "posted", channel, thread_ts)
