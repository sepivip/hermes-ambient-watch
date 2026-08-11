"""REAL-Hermes integration test for ambient-watch's pre_gateway_dispatch hook.

Everything below the plugin boundary is genuine Hermes code:

* ``gateway.platforms.base.MessageEvent`` (real dataclass)
* ``gateway.session.SessionSource`` (real dataclass, ``Platform.SLACK``)
* ``gateway.run.GatewayRunner._handle_message`` (real dispatch entry point,
  built with the ``object.__new__`` bare-runner pattern that
  ``tests/gateway/test_pre_gateway_dispatch.py`` and
  ``tests/gateway/test_gateway_command_dispatch_minimal.py`` use)
* ``hermes_cli.plugins.PluginManager.invoke_hook`` / ``PluginContext``
  (real hook registry + real per-callback isolation)
* the plugin is imported exactly the way ``PluginManager._load_directory_module``
  imports it: as ``hermes_plugins.ambient_watch`` with
  ``submodule_search_locations=[plugin_dir]`` and NO ``sys.path`` mutation.

The event fixtures mirror the real Slack adapter construction site at
``plugins/platforms/slack/adapter.py:6302`` field-for-field:

    metadata = {"slack_team_id", "slack_channel_id", "slack_thread_ts"}
    message_id = ts
    raw_message = the raw Slack Events API payload
    source.chat_type = "dm" if is_dm else "group"   # never "channel"

The Hermes venv has the hermes package but no pytest; this repo's venv has
pytest but not hermes' deps.  Both are CPython 3.11.15, so run this repo's
pytest with the Hermes venv's site-packages on PYTHONPATH (read-only use — it
never writes into the real install):

    PYTHONPATH="C:/Users/User/AppData/Local/hermes/hermes-agent;\
C:/Users/User/AppData/Local/hermes/hermes-agent/venv/Lib/site-packages" \
    ./.venv/Scripts/python.exe -m pytest tests/test_real_gateway_dispatch.py

Every test pins HERMES_HOME to a pytest ``tmp_path``; nothing touches the real
Hermes home.
"""

from __future__ import annotations

import asyncio
import functools
import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / "ambient-watch"
HERMES_AGENT = Path(r"C:\Users\User\AppData\Local\hermes\hermes-agent")

if not HERMES_AGENT.exists():  # pragma: no cover - environment guard
    pytest.skip("real hermes-agent tree not present", allow_module_level=True)

if str(HERMES_AGENT) not in sys.path:
    sys.path.insert(0, str(HERMES_AGENT))

try:
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.base import MessageEvent, MessageType
    from gateway.session import SessionSource
    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
except Exception as exc:  # pragma: no cover - environment guard
    pytest.skip(f"hermes imports unavailable: {exc}", allow_module_level=True)


def asyncio_test(fn):
    """Run an async test without pytest-asyncio (not a repo dependency).

    ``functools.wraps`` sets ``__wrapped__`` so pytest still sees the real
    signature and injects fixtures.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


BOT_ID = "U0BOTID99"
WATCHED = "C0WATCHED1"
UNWATCHED = "C0ELSEWHER"
TEAM = "T0TEAM0001"
HUMAN = "U0HUMAN001"


# ---------------------------------------------------------------------------
# Plugin loading — byte-for-byte the real loader's mechanism
# ---------------------------------------------------------------------------

def _load_plugin_module():
    """Import ambient-watch as ``hermes_plugins.ambient_watch``.

    Mirrors ``PluginManager._load_directory_module`` (hermes_cli/plugins.py
    :2042) including the ``hermes_plugins`` namespace-parent stub.  Crucially
    it does NOT put the plugin dir on ``sys.path`` — the real loader does not,
    so any flat ``import aw_config`` inside the plugin will fail here exactly
    as it fails in production.
    """
    ns_parent = "hermes_plugins"
    if ns_parent not in sys.modules:
        ns_pkg = types.ModuleType(ns_parent)
        ns_pkg.__path__ = []
        ns_pkg.__package__ = ns_parent
        sys.modules[ns_parent] = ns_pkg

    module_name = f"{ns_parent}.ambient_watch"
    if module_name in sys.modules:
        return sys.modules[module_name]

    spec = importlib.util.spec_from_file_location(
        module_name,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    module.__package__ = module_name
    module.__path__ = [str(PLUGIN_DIR)]
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _write_config(home: Path, *, channels=(WATCHED,), mode="shadow") -> Path:
    data_dir = home / "plugin-data" / "ambient_watch"
    data_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = data_dir / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "bot_user_id": BOT_ID,
                "channels": list(channels),
                "mode": mode,
                "ops_channel": "C0AMBOPS11",
                "data_dir": str(data_dir),
                "quiet_tz": "UTC",
            }
        ),
        encoding="utf-8",
    )
    return cfg_path


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """A throwaway HERMES_HOME.  Never the user's real install."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture
def plugin_manager(hermes_home, monkeypatch):
    """Register ambient-watch into a REAL PluginManager hook registry.

    Returns ``(manager, module)``.  ``hermes_cli.plugins.invoke_hook`` is
    monkeypatched to route into this manager, which is what
    ``gateway.run._handle_message`` reaches through
    ``hermes_cli.lifecycle.invoke_hook`` (lifecycle.py:20 does
    ``from hermes_cli import plugins; return plugins.invoke_hook(...)``,
    a module-attribute lookup, so the patch takes effect).
    """
    _write_config(hermes_home)
    module = _load_plugin_module()

    manager = object.__new__(PluginManager)
    manager._hooks = {}
    manager._middleware = {}

    manifest = PluginManifest(
        name="ambient-watch",
        key="ambient-watch",
        source="user",
        path=str(PLUGIN_DIR),
    )
    ctx = PluginContext(manifest, manager)
    module.register(ctx)

    import hermes_cli.plugins as _plugins_mod

    monkeypatch.setattr(
        _plugins_mod,
        "invoke_hook",
        lambda hook_name, **kwargs: manager.invoke_hook(hook_name, **kwargs),
    )
    return manager, module


# ---------------------------------------------------------------------------
# Event fixtures — mirror adapter.py:6302 exactly
# ---------------------------------------------------------------------------

def _slack_source(*, channel=WATCHED, user=HUMAN, thread_ts=None, ts="1754900000.000100",
                  is_bot=False, chat_type="group") -> SessionSource:
    return SessionSource(
        platform=Platform.SLACK,
        chat_id=channel,
        chat_name="#watched",
        # adapter.py:6227 -> chat_type="dm" if is_dm else "group"
        chat_type=chat_type,
        user_id=user,
        user_name="tester",
        # adapter.py:6232 -> thread_id=thread_ts (the DERIVED value: for a
        # top-level channel message with reply_in_thread=True this is ts)
        thread_id=thread_ts or ts,
        scope_id=TEAM,
        is_bot=is_bot,
    )


def _slack_event(
    text="deploy looks stuck, anyone know why?",
    *,
    channel=WATCHED,
    ts="1754900000.000100",
    thread_ts=None,
    user=HUMAN,
    bot_id=None,
    subtype=None,
    blocks=None,
    message_type=MessageType.TEXT,
) -> MessageEvent:
    """Build the MessageEvent the real Slack adapter would build."""
    raw = {
        "type": "message",
        "channel": channel,
        "channel_type": "channel",
        "user": user,
        "text": text,
        "ts": ts,
        "team": TEAM,
    }
    if thread_ts:
        raw["thread_ts"] = thread_ts
    if bot_id:
        raw["bot_id"] = bot_id
        raw["user"] = None
    if subtype:
        raw["subtype"] = subtype
    if blocks is not None:
        raw["blocks"] = blocks

    derived_thread_ts = thread_ts if (thread_ts and thread_ts != ts) else ts

    return MessageEvent(
        text=text,
        message_type=message_type,
        source=_slack_source(
            channel=channel,
            user=user,
            thread_ts=thread_ts,
            ts=ts,
            is_bot=bool(bot_id) or subtype == "bot_message",
        ),
        raw_message=raw,
        message_id=ts,
        media_urls=[],
        media_types=[],
        reply_to_message_id=thread_ts if thread_ts and thread_ts != ts else None,
        metadata={
            "slack_team_id": TEAM,
            "slack_channel_id": channel,
            "slack_thread_ts": derived_thread_ts,
        },
    )


def _slash_event(channel=WATCHED, text="/hermes status") -> MessageEvent:
    """Mirror the SECOND construction site, adapter.py:7753 (slash commands)."""
    command = {
        "command": "/hermes",
        "text": "status",
        "channel_id": channel,
        "user_id": HUMAN,
        "team_id": TEAM,
        "response_url": "https://hooks.slack.test/x",
    }
    return MessageEvent(
        text=text,
        message_type=MessageType.COMMAND,
        source=SessionSource(
            platform=Platform.SLACK,
            chat_id=channel,
            chat_type="group",
            user_id=HUMAN,
            scope_id=TEAM,
        ),
        raw_message=command,
        # NOTE: adapter.py:7753 sets NO message_id and NO metadata.
    )


# ---------------------------------------------------------------------------
# Runner fixtures — the two bare-runner shapes the real tests use
# ---------------------------------------------------------------------------

def _clear_auth_env(monkeypatch) -> None:
    for key in (
        "SLACK_ALLOWED_USERS",
        "GATEWAY_ALLOWED_USERS",
        "SLACK_ALLOW_ALL_USERS",
        "GATEWAY_ALLOW_ALL_USERS",
    ):
        monkeypatch.delenv(key, raising=False)


def _make_minimal_runner():
    """The shape used by tests/gateway/test_pre_gateway_dispatch.py."""
    from gateway.run import GatewayRunner

    config = GatewayConfig(platforms={Platform.SLACK: PlatformConfig(enabled=True)})
    runner = object.__new__(GatewayRunner)
    runner.config = config
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters = {Platform.SLACK: adapter}
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = False
    runner.pairing_store._is_rate_limited.return_value = False
    runner.session_store = MagicMock()
    runner._running_agents = {}
    runner._update_prompt_pending = {}
    return runner, adapter


def _make_dispatch_runner():
    """Fuller shape (tests/gateway/test_gateway_command_dispatch_minimal.py)
    so an @mention can travel all the way to _handle_message_with_agent."""
    from datetime import datetime
    from types import SimpleNamespace

    from gateway.run import GatewayRunner
    from gateway.session import SessionEntry, build_session_key

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.SLACK: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    adapter._pending_messages = {}
    runner.adapters = {Platform.SLACK: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(
        emit=AsyncMock(), emit_collect=AsyncMock(return_value=[]), loaded_hooks=False
    )
    src = _slack_source()
    entry = SessionEntry(
        session_key=build_session_key(src),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.SLACK,
        chat_type="group",
        total_tokens=0,
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = True
    runner.pairing_store._is_rate_limited.return_value = False
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._queued_events = {}
    runner._session_db = MagicMock()
    runner._session_db.get_session_title.return_value = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._should_send_voice_reply = lambda *a, **k: False
    runner._send_voice_reply = AsyncMock()
    runner._capture_gateway_honcho_if_configured = lambda *a, **k: None
    runner._emit_gateway_run_progress = AsyncMock()
    runner._update_prompt_pending = {}
    runner._busy_input_mode = "interrupt"
    runner._draining = False
    runner._session_run_generation = {}
    runner._session_sources = {}
    runner._pending_native_image_paths_by_session = {}
    runner._background_tasks = {}
    runner._background_task_counter = 0
    runner._session_model_overrides = {}
    runner._pending_model_notes = {}
    runner._service_tier = None
    runner._fast_mode_by_session = {}
    runner._goal_state_by_session = {}
    runner._goal_runs_in_progress = set()
    runner._goal_queued_by_session = set()
    runner._is_telegram_topic_root_lobby = lambda _source: False
    runner._should_send_telegram_lobby_reminder = lambda _source: False
    runner._check_slash_access = lambda _source, _command: None
    runner._begin_session_run_generation = lambda _key: 1
    runner._release_running_agent_state = lambda key: runner._running_agents.pop(key, None)
    return runner, adapter


def _capture_dispatch(runner):
    captured = {}

    async def _fake(event, source, key, generation):
        captured["event"] = event
        captured["source"] = source
        captured["key"] = key
        captured["generation"] = generation
        return {"final_response": "", "messages": []}

    runner._handle_message_with_agent = _fake
    return captured


def _spy_hook_returns(manager):
    """Record what our callback hands back to run.py, without changing it."""
    returns = []
    originals = list(manager._hooks["pre_gateway_dispatch"])

    def _wrap(cb):
        def _spy(**kwargs):
            out = cb(**kwargs)
            returns.append(out)
            return out

        return _spy

    manager._hooks["pre_gateway_dispatch"] = [_wrap(cb) for cb in originals]
    return returns


# ===========================================================================
# (a) un-mentioned watched-channel message is DROPPED
# ===========================================================================

@asyncio_test
async def test_unmentioned_watched_message_is_dropped(plugin_manager, monkeypatch):
    _clear_auth_env(monkeypatch)
    manager, _module = plugin_manager
    runner, adapter = _make_minimal_runner()
    captured = _capture_dispatch(runner)

    result = await runner._handle_message(_slack_event())

    assert result is None, "watched un-mentioned message must be dropped"
    assert captured == {}, "no agent dispatch may happen"
    adapter.send.assert_not_awaited()


@asyncio_test
async def test_dropped_message_is_actually_recorded(plugin_manager, hermes_home, monkeypatch):
    """The drop is a RECORD_SKIP, not a silent black hole."""
    _clear_auth_env(monkeypatch)
    runner, _adapter = _make_minimal_runner()
    _capture_dispatch(runner)

    await runner._handle_message(_slack_event(text="anyone seen the staging build?"))

    import sqlite3

    db = hermes_home / "plugin-data" / "ambient_watch" / "ambient.db"
    assert db.exists(), "recorder never opened its ledger"
    with sqlite3.connect(str(db)) as conn:
        rows = conn.execute(
            "SELECT channel, ts, thread_root, author, is_bot, is_mention "
            "FROM messages"
        ).fetchall()
    assert len(rows) == 1, rows
    channel, ts, thread_root, author, is_bot, is_mention = rows[0]
    assert channel == WATCHED
    assert ts == "1754900000.000100", "message_id (== Slack ts) not recorded"
    # raw_message has no 'thread_ts' for a top-level post, so the recorder must
    # root the message on its own ts — NOT on metadata['slack_thread_ts'],
    # which the adapter synthesises to ts and would silently work here but
    # break for genuine thread replies.
    assert thread_root == ts, "top-level message must root on its own ts"
    assert author == HUMAN, "raw_message['user'] not recorded"
    assert not is_bot
    assert not is_mention


@asyncio_test
async def test_thread_reply_roots_on_raw_thread_ts(plugin_manager, hermes_home, monkeypatch):
    """A real thread reply must root on ``raw_message['thread_ts']``.

    The adapter's ``metadata['slack_thread_ts']`` is the DERIVED session-scoping
    value; for top-level posts it equals ``ts``.  Reading it instead of the raw
    payload would make every top-level message look like a thread reply.
    """
    _clear_auth_env(monkeypatch)
    runner, _adapter = _make_minimal_runner()
    _capture_dispatch(runner)

    await runner._handle_message(
        _slack_event(
            text="still broken",
            ts="1754900500.000900",
            thread_ts="1754900000.000100",
        )
    )

    import sqlite3

    db = hermes_home / "plugin-data" / "ambient_watch" / "ambient.db"
    with sqlite3.connect(str(db)) as conn:
        rows = conn.execute("SELECT ts, thread_root FROM messages").fetchall()
    assert rows == [("1754900500.000900", "1754900000.000100")], rows


@asyncio_test
async def test_bot_message_in_watched_channel_is_dropped(plugin_manager, monkeypatch):
    _clear_auth_env(monkeypatch)
    runner, adapter = _make_minimal_runner()
    captured = _capture_dispatch(runner)

    event = _slack_event(
        text="Build #412 failed", bot_id="B0BUILDBOT", subtype="bot_message"
    )
    assert await runner._handle_message(event) is None
    assert captured == {}
    adapter.send.assert_not_awaited()


# ===========================================================================
# (b) @mention message PROCEEDS to dispatch
# ===========================================================================

@asyncio_test
async def test_mention_in_watched_channel_reaches_dispatch(plugin_manager, monkeypatch):
    _clear_auth_env(monkeypatch)
    runner, _adapter = _make_dispatch_runner()
    captured = _capture_dispatch(runner)

    event = _slack_event(text=f"<@{BOT_ID}> can you check the deploy?")
    result = await runner._handle_message(event)

    assert captured, "genuine @mention was eaten — the hook must return None"
    assert captured["event"].text == f"<@{BOT_ID}> can you check the deploy?"
    assert captured["source"].platform is Platform.SLACK
    assert captured["source"].chat_id == WATCHED
    assert result == {"final_response": "", "messages": []}


@asyncio_test
async def test_block_kit_mention_reaches_dispatch(plugin_manager, monkeypatch):
    """Slack often delivers the mention only in ``blocks`` (#52387)."""
    _clear_auth_env(monkeypatch)
    runner, _adapter = _make_dispatch_runner()
    captured = _capture_dispatch(runner)

    blocks = [
        {
            "type": "rich_text",
            "elements": [
                {
                    "type": "rich_text_section",
                    "elements": [
                        {"type": "user", "user_id": BOT_ID},
                        {"type": "text", "text": " ping"},
                    ],
                }
            ],
        }
    ]
    # Adapter strips the bot's own <@…> out of `text`, so text carries no needle.
    event = _slack_event(text="ping", blocks=blocks)
    event.raw_message["text"] = "ping"

    await runner._handle_message(event)
    assert captured, "Block Kit mention was suppressed"


@asyncio_test
async def test_quoted_mention_is_not_a_mention(plugin_manager, monkeypatch):
    """A mention inside rich_text_quote is a quote, not an address."""
    _clear_auth_env(monkeypatch)
    runner, adapter = _make_minimal_runner()
    captured = _capture_dispatch(runner)

    blocks = [
        {
            "type": "rich_text",
            "elements": [
                {
                    "type": "rich_text_quote",
                    "elements": [{"type": "user", "user_id": BOT_ID}],
                }
            ],
        }
    ]
    event = _slack_event(text="as I said above", blocks=blocks)
    event.raw_message["text"] = "as I said above"

    assert await runner._handle_message(event) is None
    assert captured == {}
    adapter.send.assert_not_awaited()


# ===========================================================================
# Non-watched / non-Slack / DM / slash traffic is untouched
# ===========================================================================

@asyncio_test
async def test_unwatched_channel_reaches_dispatch(plugin_manager, monkeypatch):
    _clear_auth_env(monkeypatch)
    runner, _adapter = _make_dispatch_runner()
    captured = _capture_dispatch(runner)

    await runner._handle_message(_slack_event(channel=UNWATCHED, text="hi there"))
    assert captured, "ambient-watch suppressed a channel it does not watch"


@asyncio_test
async def test_slack_dm_reaches_dispatch(plugin_manager, monkeypatch):
    """adapter.py:6227 gives DMs chat_type='dm' — never ambient-suppressed."""
    _clear_auth_env(monkeypatch)
    runner, _adapter = _make_dispatch_runner()
    captured = _capture_dispatch(runner)

    event = _slack_event(text="hey")
    event.source.chat_type = "dm"
    await runner._handle_message(event)
    assert captured, "a Slack DM was suppressed"


@asyncio_test
async def test_slash_command_is_permitted(plugin_manager, monkeypatch, caplog):
    """The adapter.py:7753 shape has no metadata and no message_id.

    A slash command never reaches ``_handle_message_with_agent`` (run.py routes
    it to the command dispatcher first), so the assertion here is that our hook
    PERMITS it — returns None — and that run.py got far enough to reply.
    """
    _clear_auth_env(monkeypatch)
    manager, _module = plugin_manager
    returns = _spy_hook_returns(manager)
    runner, _adapter = _make_dispatch_runner()
    _capture_dispatch(runner)

    with caplog.at_level("WARNING", logger="gateway.run"):
        await runner._handle_message(_slash_event())

    assert returns == [None], f"slash command was suppressed: {returns}"
    # run.py:16197 — proof the event travelled past our hook into the real
    # gateway command dispatcher rather than being dropped.
    assert any("Unrecognized slash command" in r.message for r in caplog.records)


@asyncio_test
async def test_internal_event_bypasses_hook(plugin_manager, monkeypatch):
    """run.py:14909 gates the hook on `not is_internal`."""
    _clear_auth_env(monkeypatch)
    manager, _module = plugin_manager
    runner, _adapter = _make_dispatch_runner()
    captured = _capture_dispatch(runner)

    event = _slack_event(text="background job finished")
    event.internal = True
    await runner._handle_message(event)
    assert captured, "internal event must never be ambient-suppressed"


# ===========================================================================
# Hook contract details against the REAL invoke_hook
# ===========================================================================

@asyncio_test
async def test_hook_receives_real_kwargs(plugin_manager, monkeypatch):
    """run.py passes event=, gateway=, session_store= (+ telemetry_schema_version
    injected by PluginManager.invoke_hook).  Our callback must tolerate all of
    them via **kwargs."""
    _clear_auth_env(monkeypatch)
    manager, _module = plugin_manager
    seen = {}

    original = manager._hooks["pre_gateway_dispatch"][0]

    def _spy(**kwargs):
        seen.update(kwargs)
        return original(**kwargs)

    manager._hooks["pre_gateway_dispatch"] = [_spy]

    runner, _adapter = _make_minimal_runner()
    _capture_dispatch(runner)
    await runner._handle_message(_slack_event())

    assert set(seen) >= {"event", "gateway", "session_store", "telemetry_schema_version"}
    assert seen["gateway"] is runner
    assert isinstance(seen["event"], MessageEvent)


@asyncio_test
async def test_hook_survives_missing_session_store(plugin_manager, monkeypatch):
    """Regression mirror of the real Hermes test: a runner without
    session_store must still deliver the event (session_store=None)."""
    _clear_auth_env(monkeypatch)
    runner, adapter = _make_minimal_runner()
    captured = _capture_dispatch(runner)
    del runner.session_store

    assert await runner._handle_message(_slack_event()) is None
    assert captured == {}
    adapter.send.assert_not_awaited()


def test_skip_payload_shape(plugin_manager):
    """The value we hand back must be exactly what run.py:14925 consumes."""
    manager, _module = plugin_manager
    cb = manager._hooks["pre_gateway_dispatch"][0]
    out = cb(event=_slack_event(), gateway=None, session_store=None)
    assert isinstance(out, dict)
    assert out["action"] == "skip"
    assert "reason" in out


# ===========================================================================
# Field-shape verification against the REAL adapter construction site
# ===========================================================================

def test_recorder_reads_only_fields_the_real_adapter_sets():
    """Guard the integration seam itself.

    Fields ambient-watch's recorder reads, and where the real Slack adapter
    sets them (plugins/platforms/slack/adapter.py:6302):

        event.metadata['slack_channel_id']   -> set (line 6317)
        event.message_id                     -> set to ts
        event.raw_message                    -> the raw Slack event dict
        event.source.chat_type               -> 'dm' | 'group'
        event.text / event.message_type      -> set
    """
    event = _slack_event()
    assert "slack_channel_id" in event.metadata
    assert "slack_team_id" in event.metadata
    assert "slack_thread_ts" in event.metadata
    assert event.message_id == "1754900000.000100"
    assert isinstance(event.raw_message, dict)
    assert {"ts", "user", "text"} <= set(event.raw_message)
    # The real adapter NEVER produces chat_type == "channel".
    assert event.source.chat_type in {"dm", "group"}
    # The real adapter does NOT populate MessageEvent.user_id (only source).
    assert event.user_id is None


def test_recorder_channel_resolution_matches_adapter():
    """_channel_of prefers metadata['slack_channel_id'], falls back to chat_id."""
    from hermes_plugins.ambient_watch.aw_recorder import _channel_of

    event = _slack_event()
    assert _channel_of(event) == WATCHED
    # Slash-command shape carries no metadata -> chat_id fallback must work.
    assert _channel_of(_slash_event()) == WATCHED


def test_recorder_handles_group_chat_type(plugin_manager, hermes_home):
    """A regression guard: the recorder must not depend on chat_type=='channel'
    (the FAKES in tests/conftest.py use 'channel'; the real adapter uses
    'group')."""
    from hermes_plugins.ambient_watch.aw_config import load_config
    from hermes_plugins.ambient_watch.aw_recorder import Decision, decide
    from hermes_plugins.ambient_watch.aw_store import AmbientStore

    cfg = load_config(hermes_home / "plugin-data" / "ambient_watch" / "config.json")
    store = AmbientStore(cfg.data_dir / "decide_probe.db")
    try:
        assert decide(_slack_event(), cfg, store) is Decision.RECORD_SKIP
        assert (
            decide(_slack_event(text=f"<@{BOT_ID}> hi", ts="1754900000.000200"), cfg, store)
            is Decision.RECORD_PASS
        )
    finally:
        store.close()


def test_install_gate_runs_under_the_real_package_import(hermes_home, monkeypatch, caplog):
    """gate.install_gate() must work when the plugin is imported as a package.

    The real loader does NOT add the plugin dir to sys.path, so any flat
    ``import aw_config`` inside the plugin fails in production.
    """
    monkeypatch.syspath_prepend  # noqa: B018 - documented no-op reference
    # Remove any flat plugin modules another test/conftest may have leaked in.
    for name in ("aw_config", "aw_store", "aw_detectors", "gate"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(
        sys, "path", [p for p in sys.path if Path(p).resolve() != PLUGIN_DIR.resolve()]
    )

    from hermes_plugins.ambient_watch.gate import install_gate

    shim = install_gate()
    assert shim.exists(), "cron gate shim was not written"
    assert shim.parent == hermes_home / "scripts"
