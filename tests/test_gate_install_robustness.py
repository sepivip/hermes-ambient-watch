"""install_gate() must work in EVERY import context.

Regression: pre-flight against the real Hermes install hit
    ModuleNotFoundError: No module named 'aw_config'
because install_gate() imported aw_config to find HERMES_HOME. That
import resolves differently depending on how the module was loaded
(real plugin loader = package with parent, cron shim = flat sys.path,
harness = package WITHOUT parent registered). Since the cron shim is the
only thing standing between an idle sweep and a woken agent, this
function must not depend on import context at all.
"""

import importlib.util
import sys
from pathlib import Path

from conftest import PLUGIN_DIR


def _load_gate_without_parent_package(monkeypatch, tmp_path):
    """Load gate.py as 'ghost_pkg.gate' with NO 'ghost_pkg' in sys.modules
    and the plugin dir NOT on sys.path — the harshest context."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.syspath_prepend  # noqa: B018 - explicit: we do NOT prepend
    for mod in ("aw_config", "ghost_pkg", "ghost_pkg.gate"):
        sys.modules.pop(mod, None)
    saved = [p for p in sys.path if Path(p).resolve() == Path(PLUGIN_DIR).resolve()]
    for p in saved:
        sys.path.remove(p)
    try:
        spec = importlib.util.spec_from_file_location(
            "ghost_pkg.gate",
            Path(PLUGIN_DIR) / "gate.py",
            submodule_search_locations=[str(PLUGIN_DIR)],
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for p in saved:
            sys.path.insert(0, p)
        sys.modules.pop("ghost_pkg.gate", None)


def test_install_gate_works_without_parent_package(monkeypatch, tmp_path):
    gate = _load_gate_without_parent_package(monkeypatch, tmp_path)
    shim = gate.install_gate()  # no explicit hermes_home — must self-resolve
    assert shim == tmp_path / "scripts" / "ambient_watch_gate.py"
    assert shim.exists()
    content = shim.read_text(encoding="utf-8")
    assert '{"wakeAgent": false}' in content


def test_install_gate_respects_explicit_home_in_any_context(monkeypatch, tmp_path):
    gate = _load_gate_without_parent_package(monkeypatch, tmp_path)
    other = tmp_path / "elsewhere"
    shim = gate.install_gate(hermes_home=other)
    assert shim == other / "scripts" / "ambient_watch_gate.py"
    assert shim.exists()
