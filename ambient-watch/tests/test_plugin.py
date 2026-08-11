"""Plugin wiring: register(ctx) hooks the recorder and guard correctly."""

import importlib.util
import json
from pathlib import Path

from conftest import BOT_ID, PLUGIN_DIR, WATCHED, make_event


def _load_plugin(monkeypatch, tmp_path):
    home = tmp_path / "hermes-home"
    data = home / "plugin-data" / "ambient_watch"
    data.mkdir(parents=True)
    (data / "config.json").write_text(
        json.dumps(
            {
                "bot_user_id": BOT_ID,
                "channels": [WATCHED],
                "mode": "shadow",
                "ops_channel": "C0AMBOPS11",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    spec = importlib.util.spec_from_file_location(
        "ambient_watch_plugin", Path(PLUGIN_DIR) / "__init__.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeCtx:
    def __init__(self):
        self.hooks = {}

    def register_hook(self, name, cb):
        self.hooks[name] = cb


def test_register_wires_both_hooks(monkeypatch, tmp_path):
    mod = _load_plugin(monkeypatch, tmp_path)
    ctx = FakeCtx()
    mod.register(ctx)
    assert "pre_gateway_dispatch" in ctx.hooks
    assert "pre_tool_call" in ctx.hooks


def test_dispatch_hook_skips_plain_watched_traffic(monkeypatch, tmp_path):
    mod = _load_plugin(monkeypatch, tmp_path)
    ctx = FakeCtx()
    mod.register(ctx)
    verdict = ctx.hooks["pre_gateway_dispatch"](
        event=make_event(text="nobody mentioned the bot"),
        gateway=None,
        session_store=None,
    )
    assert verdict == {"action": "skip", "reason": "ambient-watch: recorded"}


def test_dispatch_hook_passes_mentions(monkeypatch, tmp_path):
    mod = _load_plugin(monkeypatch, tmp_path)
    ctx = FakeCtx()
    mod.register(ctx)
    verdict = ctx.hooks["pre_gateway_dispatch"](
        event=make_event(text=f"<@{BOT_ID}> hello"),
        gateway=None,
        session_store=None,
    )
    assert verdict is None
