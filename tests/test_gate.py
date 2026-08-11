"""gate.py tests — cron gate semantics per adversarial review.

Corrections encoded here:
- Shadow mode arms NO intents and burns NO interventions (threads are
  not consumed by digests that never post to them).
- Live mode arms intents (bare refs) and records interventions at arm
  time; candidates.json targets carry the deliverable "slack:" prefix.
- Any internal error → {"wakeAgent": false} (fail closed, never wake).
- Stale pending intents expire at the next run.
- install_gate() writes a path-jailed shim into HERMES_HOME/scripts.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from conftest import WATCHED, make_event

from aw_recorder import decide
from gate import ERROR_LOG_NAME, INTENT_TTL_SECONDS, install_gate, run_gate

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "ambient-watch"

T0 = 1754900000.0


def _seed_question(cfg, store, ts=T0):
    ev = make_event(text="who owns the migration runbook?", ts=f"{ts:.6f}")
    decide(ev, cfg, store)


def _last_json(out):
    return json.loads(out.splitlines()[-1])


def test_no_candidates_emits_wake_false(cfg, store):
    assert _last_json(run_gate(cfg, store, now=T0)) == {"wakeAgent": False}


def test_shadow_mode_wakes_agent_but_arms_nothing(cfg, store):
    _seed_question(cfg, store)
    out = run_gate(cfg, store, now=T0 + 46 * 60)
    assert _last_json(out) == {"wakeAgent": True}
    payload = json.loads((cfg.data_dir / "candidates.json").read_text(encoding="utf-8"))
    assert payload["mode"] == "shadow"
    assert payload["candidates"][0]["target"] == f"slack:{WATCHED}:{T0:.6f}"
    assert "who owns the migration runbook?" in payload["candidates"][0]["excerpt"]
    # Shadow: no intents armed, no interventions burned.
    assert store.pending_intents() == []
    assert not store.has_intervention(WATCHED, f"{T0:.6f}")


def test_live_mode_arms_intents_and_records_interventions(live_cfg):
    from aw_store import AmbientStore

    store = AmbientStore(live_cfg.data_dir / "ambient.db")
    _seed_question(live_cfg, store)
    out = run_gate(live_cfg, store, now=T0 + 46 * 60)
    assert _last_json(out) == {"wakeAgent": True}
    assert store.pending_intents() == [f"{WATCHED}:{T0:.6f}"]
    assert store.has_intervention(WATCHED, f"{T0:.6f}")
    store.close()


def test_kill_switch_forces_wake_false(cfg, store):
    _seed_question(cfg, store)
    store.set_kill_switch(True)
    assert _last_json(run_gate(cfg, store, now=T0 + 46 * 60)) == {"wakeAgent": False}


def test_internal_error_fails_closed(cfg, store, monkeypatch):
    """A gate crash must never wake the agent (token-burn inversion)."""
    import aw_detectors

    def boom(*a, **k):
        raise RuntimeError("detector exploded")

    monkeypatch.setattr(aw_detectors, "find_candidates", boom)
    assert _last_json(run_gate(cfg, store, now=T0)) == {"wakeAgent": False}


def test_internal_error_leaves_a_breadcrumb(cfg, store, monkeypatch):
    """A silent run's stdout is discarded by the scheduler, so the fail-closed
    path must also write gate_errors.log — otherwise a permanently dead gate
    is undetectable and ambient mode just goes quiet forever."""
    import aw_detectors

    def boom(*a, **k):
        raise RuntimeError("detector exploded")

    monkeypatch.setattr(aw_detectors, "find_candidates", boom)
    log = cfg.data_dir / ERROR_LOG_NAME
    assert not log.exists()

    run_gate(cfg, store, now=T0)

    assert log.exists(), "fail-closed run left no breadcrumb"
    body = log.read_text(encoding="utf-8")
    assert "RuntimeError" in body and "detector exploded" in body, body


def test_breadcrumb_never_breaks_the_gate(cfg, store, monkeypatch):
    """Breadcrumbs are best-effort: an unwritable log must not change the
    verdict or raise out of run_gate."""
    import aw_detectors
    import gate

    monkeypatch.setattr(
        aw_detectors, "find_candidates",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(
        gate.Path, "mkdir",
        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only fs")),
    )
    assert _last_json(run_gate(cfg, store, now=T0)) == {"wakeAgent": False}


def test_broken_shim_gates_off_and_leaves_a_breadcrumb(tmp_path):
    """Execute the generated shim for real with its plugin dir removed: the
    outer guard must print the false gate, exit 0, AND record why."""
    shim = install_gate(hermes_home=tmp_path)
    broken = shim.with_name("ambient_watch_gate_broken.py")
    broken.write_text(
        shim.read_text(encoding="utf-8").replace(
            repr(str(PLUGIN_DIR)), repr(str(tmp_path / "does" / "not" / "exist"))
        ),
        encoding="utf-8",
    )

    env = dict(os.environ, HERMES_HOME=str(tmp_path))
    proc = subprocess.run(
        [sys.executable, str(broken)],
        capture_output=True, text=True, env=env, timeout=60,
    )

    # rc != 0 would make the scheduler wake the agent with a "Script Error".
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.splitlines()[-1]) == {"wakeAgent": False}

    log = tmp_path / "plugin-data" / "ambient_watch" / ERROR_LOG_NAME
    assert log.exists(), f"shim left no breadcrumb; stderr={proc.stderr!r}"
    assert "ModuleNotFoundError" in log.read_text(encoding="utf-8")
    assert "Traceback" in proc.stderr


def test_stale_pending_intents_expire_on_next_run(live_cfg):
    from aw_store import AmbientStore

    store = AmbientStore(live_cfg.data_dir / "ambient.db")
    store.arm_intent("C0OLD:1754000000.000001", "C0OLD", "1754000000.000001", now=T0)
    run_gate(live_cfg, store, now=T0 + INTENT_TTL_SECONDS + 1)
    assert "C0OLD:1754000000.000001" not in store.pending_intents()
    store.close()


def test_install_gate_writes_shim_into_scripts_dir(tmp_path):
    """Cron path-jails scripts to HERMES_HOME/scripts (scheduler.py:2392)."""
    shim = install_gate(hermes_home=tmp_path)
    assert shim == tmp_path / "scripts" / "ambient_watch_gate.py"
    content = shim.read_text(encoding="utf-8")
    assert "sys.path.insert" in content
    assert '{"wakeAgent": false}' in content  # fail-closed on any shim error
    # Idempotent re-install
    assert install_gate(hermes_home=tmp_path) == shim
