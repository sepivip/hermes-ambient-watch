"""Shared fixtures for ambient-watch tests.

Fakes mirror the contracts verified against hermes-agent v0.20.0
(commit c0106e5, 2026-08-11), corrected per adversarial review:

- MessageEvent: text, message_type, message_id (= Slack ts), user_id,
  source (platform, user_id, chat_id, user_name, chat_type), raw_message
  (raw Slack event dict, may contain Block Kit "blocks"), metadata.
- chat_type is ONLY ever "dm" or "group" — the Slack adapter builds it as
  ``"dm" if is_dm else "group"`` (plugins/platforms/slack/adapter.py:6227,
  where is_dm covers both 1:1 IMs and MPIM group DMs), so it never emits
  "channel". make_event() therefore defaults to "group": a fake that said
  "channel" would let recorder logic keyed on that value pass the whole
  suite and suppress nothing in production.
- Slash commands produce a second shape: chat_type="group", no metadata,
  raw_message = slash payload with "command" key and NO "ts".
- send_message tool targets are "slack:C…:<thread_ts>" — PLATFORM PREFIX
  FIRST (tools/send_message_tool.py _handle_send splits platform:ref).
- Cron scripts are path-jailed to HERMES_HOME/scripts/.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "ambient-watch"
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from aw_config import AmbientConfig  # noqa: E402
from aw_store import AmbientStore  # noqa: E402

BOT_ID = "U0BOTID99"
WATCHED = "C0WATCHED1"
UNWATCHED = "C0ELSEWHER"


def _make_cfg(tmp_path, mode):
    return AmbientConfig(
        bot_user_id=BOT_ID,
        channels={WATCHED},
        mode=mode,
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
def cfg(tmp_path):
    return _make_cfg(tmp_path, "shadow")


@pytest.fixture
def live_cfg(tmp_path):
    return _make_cfg(tmp_path, "live")


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
    chat_type="group",
    bot_id=None,
    subtype=None,
    blocks=None,
    message_type=None,
):
    raw = {"ts": ts, "channel": channel, "user": user, "text": text}
    if thread_ts:
        raw["thread_ts"] = thread_ts
    if bot_id:
        raw["bot_id"] = bot_id
    if subtype:
        raw["subtype"] = subtype
    if blocks is not None:
        raw["blocks"] = blocks
    return SimpleNamespace(
        text=text,
        message_type=message_type,
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


def make_slash_event(channel=WATCHED, text="/hermes status"):
    """Mirror adapter.py:7753 — slash payload: no ts/thread_ts/user keys."""
    return SimpleNamespace(
        text=text,
        message_type=SimpleNamespace(name="COMMAND", value="command"),
        message_id=None,
        user_id="U0HUMAN001",
        user_name="tester",
        internal=False,
        source=SimpleNamespace(
            platform=SimpleNamespace(value="slack"),
            user_id="U0HUMAN001",
            chat_id=channel,
            user_name="tester",
            chat_type="group",
        ),
        raw_message={"command": "/hermes", "channel_id": channel, "text": text},
        reply_to_message_id=None,
        metadata={},
    )
