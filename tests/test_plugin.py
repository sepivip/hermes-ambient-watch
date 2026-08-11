"""Plugin wiring — updated per adversarial review.

New contracts encoded:
- The real loader imports plugins as package hermes_plugins.<name> with
  __path__ set; __init__ must work that way (relative imports, no
  sys.path pollution).
- Config failure must NOT leave free_response_channels unguarded: fall
  back to last-known-good config; with no LKG, register an emergency
  suppressor for the channels listed in Hermes' own config.yaml.
- install_gate() runs at register time (idempotent).
"""

import importlib.util
import json
import sys
from pathlib import Path

from conftest import BOT_ID, PLUGIN_DIR, WATCHED, make_event


def _write_config(home: Path, valid=True):
    data = home / "plugin-data" / "ambient_watch"
    data.mkdir(parents=True, exist_ok=True)
    if valid:
        body = json.dumps(
            {
                "bot_user_id": BOT_ID,
                "channels": [WATCHED],
                "mode": "shadow",
                "ops_channel": "C0AMBOPS11",
            }
        )
    else:
        body = "{ this is not json"
    (home / "plugin-data" / "ambient_watch" / "config.json").write_text(
        body, encoding="utf-8"
    )


def _load_plugin(monkeypatch, home: Path):
    """Load __init__.py the way the real loader does: as a package with
    __path__ = [plugin_dir] under hermes_plugins.<name>."""
    monkeypatch.setenv("HERMES_HOME", str(home))
    name = "hermes_plugins.ambient_watch_test"
    spec = importlib.util.spec_from_file_location(
        name, Path(PLUGIN_DIR) / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        # do not leak between tests
        for k in list(sys.modules):
            if k.startswith(name):
                sys.modules.pop(k, None)
    return mod


class FakeCtx:
    def __init__(self):
        self.hooks = {}

    def register_hook(self, name, cb):
        self.hooks[name] = cb


def test_register_wires_both_hooks_and_installs_gate(monkeypatch, tmp_path):
    _write_config(tmp_path)
    mod = _load_plugin(monkeypatch, tmp_path)
    ctx = FakeCtx()
    mod.register(ctx)
    assert "pre_gateway_dispatch" in ctx.hooks
    assert "pre_tool_call" in ctx.hooks
    assert (tmp_path / "scripts" / "ambient_watch_gate.py").exists()


def test_dispatch_hook_skips_plain_watched_traffic(monkeypatch, tmp_path):
    _write_config(tmp_path)
    mod = _load_plugin(monkeypatch, tmp_path)
    ctx = FakeCtx()
    mod.register(ctx)
    verdict = ctx.hooks["pre_gateway_dispatch"](
        event=make_event(text="nobody mentioned the bot"), gateway=None, session_store=None
    )
    assert verdict == {"action": "skip", "reason": "ambient-watch: recorded"}


def test_dispatch_hook_passes_mentions(monkeypatch, tmp_path):
    _write_config(tmp_path)
    mod = _load_plugin(monkeypatch, tmp_path)
    ctx = FakeCtx()
    mod.register(ctx)
    verdict = ctx.hooks["pre_gateway_dispatch"](
        event=make_event(text=f"<@{BOT_ID}> hello"), gateway=None, session_store=None
    )
    assert verdict is None


def test_corrupt_config_falls_back_to_last_known_good(monkeypatch, tmp_path):
    # First healthy load persists an LKG copy.
    _write_config(tmp_path, valid=True)
    mod = _load_plugin(monkeypatch, tmp_path)
    ctx = FakeCtx()
    mod.register(ctx)
    assert (tmp_path / "plugin-data" / "ambient_watch" / "config.json.lkg").exists()

    # Corrupt the live config; a fresh load must still guard the channels.
    _write_config(tmp_path, valid=False)
    mod2 = _load_plugin(monkeypatch, tmp_path)
    ctx2 = FakeCtx()
    mod2.register(ctx2)
    assert "pre_gateway_dispatch" in ctx2.hooks
    verdict = ctx2.hooks["pre_gateway_dispatch"](
        event=make_event(text="plain traffic"), gateway=None, session_store=None
    )
    assert verdict == {"action": "skip", "reason": "ambient-watch: recorded"}


def test_no_config_at_all_registers_emergency_suppressor(monkeypatch, tmp_path):
    """No config.json, no LKG — but Hermes config.yaml lists
    free_response_channels. Leaving them undispatched-guarded would make
    the bot answer everything there (review: 'dormant' was fail-OPEN)."""
    (tmp_path / "plugin-data" / "ambient_watch").mkdir(parents=True)
    (tmp_path / "config.yaml").write_text(
        f'slack:\n  free_response_channels: ["{WATCHED}"]\n', encoding="utf-8"
    )
    mod = _load_plugin(monkeypatch, tmp_path)
    ctx = FakeCtx()
    mod.register(ctx)
    assert "pre_gateway_dispatch" in ctx.hooks
    # Un-mentioned traffic in the orphaned channel is suppressed…
    verdict = ctx.hooks["pre_gateway_dispatch"](
        event=make_event(text="plain traffic", channel=WATCHED),
        gateway=None, session_store=None,
    )
    assert verdict == {"action": "skip", "reason": "ambient-watch: emergency suppressor"}
    # …but anything mention-shaped still flows (better to over-answer a
    # mention than to eat one).
    verdict = ctx.hooks["pre_gateway_dispatch"](
        event=make_event(text=f"<@{BOT_ID}> are you there?", channel=WATCHED),
        gateway=None, session_store=None,
    )
    assert verdict is None
