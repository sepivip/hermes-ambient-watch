"""gate.py: the cron pre-run script. Zero-token gate for the sweep.

Contract (verified against cron/scheduler.py v0.20.0): if the script's
stdout ends with JSON containing {"wakeAgent": false}, the agent session
is skipped entirely.
"""

import json

from conftest import WATCHED, make_event

from aw_recorder import decide
from gate import run_gate

T0 = 1754900000.0


def test_no_candidates_emits_wake_false(cfg, store):
    out = run_gate(cfg, store, now=T0)
    assert json.loads(out.splitlines()[-1]) == {"wakeAgent": False}


def test_candidates_write_file_and_wake_agent(cfg, store):
    ev = make_event(text="who owns the migration runbook?", ts=f"{T0:.6f}")
    decide(ev, cfg, store)
    out = run_gate(cfg, store, now=T0 + 46 * 60)
    assert json.loads(out.splitlines()[-1]) == {"wakeAgent": True}
    payload = json.loads((cfg.data_dir / "candidates.json").read_text(encoding="utf-8"))
    assert payload["mode"] == "shadow"
    assert payload["ops_channel"] == cfg.ops_channel
    assert len(payload["candidates"]) == 1
    cand = payload["candidates"][0]
    assert cand["target"] == f"{WATCHED}:{T0:.6f}"
    assert cand["kind"] == "unanswered_question"
    assert "who owns the migration runbook?" in cand["excerpt"]
    # Intent rows are armed for the tool guard before any session starts.
    assert store.pending_intents() == [cand["target"]]


def test_kill_switch_forces_wake_false(cfg, store):
    ev = make_event(text="who owns the migration runbook?", ts=f"{T0:.6f}")
    decide(ev, cfg, store)
    store.set_kill_switch(True)
    out = run_gate(cfg, store, now=T0 + 46 * 60)
    assert json.loads(out.splitlines()[-1]) == {"wakeAgent": False}


def test_candidates_capped_per_run(cfg, store):
    for i in range(6):
        ev = make_event(text=f"unanswered thing {i}?", ts=f"{T0 + i:.6f}")
        decide(ev, cfg, store)
    run_gate(cfg, store, now=T0 + 46 * 60)
    payload = json.loads((cfg.data_dir / "candidates.json").read_text(encoding="utf-8"))
    assert len(payload["candidates"]) <= 3
