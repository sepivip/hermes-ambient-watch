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

from conftest import WATCHED, make_event

from aw_recorder import decide
from gate import INTENT_TTL_SECONDS, install_gate, run_gate

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
