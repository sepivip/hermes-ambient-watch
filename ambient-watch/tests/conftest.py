"""Shared fixtures for ambient-watch tests.

Fakes mirror the contracts verified against hermes-agent v0.20.0
(commit c0106e5, 2026-08-11):

- MessageEvent: text, message_id (= Slack ts), user_id, source
  (SessionSource: platform, user_id, chat_id, user_name, chat_type),
  raw_message (raw Slack event dict), reply_to_message_id
  (= thread_ts when in a thread), metadata {slack_team_id,
  slack_channel_id, slack_thread_ts}.
- pre_gateway_dispatch: kwargs event/gateway/session_store, returns
  {"action": "skip"|"rewrite"|"allow"} or None.
- pre_tool_call: kwargs tool_name/args/..., returns
  {"action": "block", "message": str} to block.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Make the plugin modules importable without a Hermes install.
PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from aw_config import AmbientConfig  # noqa: E402
from aw_store import AmbientStore  # noqa: E402

BOT_ID = "U0BOTID99"
WATCHED = "C0WATCHED1"
UNWATCHED = "C0ELSEWHER"


@pytest.fixture
def cfg(tmp_path):
    return AmbientConfig(
        bot_user_id=BOT_ID,
        channels={WATCHED},
        mode="shadow",
        ops_channel="C0AMBOPS11",
        data_dir=tmp_path,
        unanswered_after_minutes=45,
        stalled_after_minutes=240,
        caps_per_thread=1,
        caps_per_channel_per_day=3,
        caps_global_per_day=8,
        cooldown_minutes=120,
        quiet_start="20:00",
        quiet_end="09:00",
        quiet_tz="Asia/Tbilisi",
    )


@pytest.fixture
def store(cfg):
    s = AmbientStore(cfg.data_dir / "ambient.db")
    yield s
    s.close()


def make_event(
    text="hello world",
    channel=WATCHED,
    ts="1754900000.000100",
    thread_ts=None,
    user="U0HUMAN001",
    platform="slack",
    chat_type="channel",
    bot_id=None,
    subtype=None,
):
    """Build a MessageEvent-shaped fake matching the Slack adapter's output."""
    raw = {"ts": ts, "channel": channel, "user": user, "text": text}
    if thread_ts:
        raw["thread_ts"] = thread_ts
    if bot_id:
        raw["bot_id"] = bot_id
    if subtype:
        raw["subtype"] = subtype
    return SimpleNamespace(
        text=text,
        message_id=ts,
        user_id=user,
        user_name="tester",
        internal=False,
        source=SimpleNamespace(
            platform=SimpleNamespace(value=platform),
            user_id=user,
            chat_id=channel,
            user_name="tester",
            chat_type=chat_type,
        ),
        raw_message=raw,
        reply_to_message_id=(thread_ts if thread_ts and thread_ts != ts else None),
        metadata={
            "slack_team_id": "T0TEAM0001",
            "slack_channel_id": channel,
            "slack_thread_ts": thread_ts or ts,
        },
    )
