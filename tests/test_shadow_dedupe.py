"""Shadow mode must digest each thread once, without burning nudge budget.

The adversarial review correctly made shadow mode record NO interventions,
so a digest never consumes a thread's once-per-thread budget or the
per-channel daily cap. The unintended consequence: nothing marks the
thread as already-reported, so every sweep re-emits the identical
candidate. At a 15-minute cadence that is ~96 duplicate digests per day
per thread — the ops channel becomes unreadable and the soak's precision
measurement is meaningless.

Fix: a separate shadow_seen ledger. Shadow sweeps mark threads seen and
skip them next time; live mode is unaffected (it uses interventions).
"""

from conftest import WATCHED, make_event

from aw_detectors import find_candidates
from aw_recorder import decide
from gate import run_gate

T0 = 1754900000.0


def _seed(store, cfg, text, ts):
    decide(make_event(text=text, ts=f"{ts:.6f}"), cfg, store)


def test_shadow_sweep_marks_thread_seen(cfg, store):
    _seed(store, cfg, "who owns the runbook?", T0)
    run_gate(cfg, store, now=T0 + 46 * 60)
    assert store.is_shadow_seen(WATCHED, f"{T0:.6f}") is True
    # and it did NOT burn the real nudge budget
    assert store.has_intervention(WATCHED, f"{T0:.6f}") is False


def test_second_shadow_sweep_finds_nothing(cfg, store):
    _seed(store, cfg, "who owns the runbook?", T0)
    first = run_gate(cfg, store, now=T0 + 46 * 60)
    assert '"wakeAgent": true' in first
    second = run_gate(cfg, store, now=T0 + 48 * 60)
    assert '"wakeAgent": false' in second, "duplicate digest for the same thread"


def test_detectors_exclude_shadow_seen_threads(cfg, store):
    _seed(store, cfg, "who owns the runbook?", T0)
    assert len(find_candidates(store, cfg, now=T0 + 46 * 60)) == 1
    store.mark_shadow_seen(WATCHED, f"{T0:.6f}", now=T0 + 46 * 60)
    assert find_candidates(store, cfg, now=T0 + 48 * 60) == []


def test_live_mode_does_not_use_shadow_seen(live_cfg):
    """Live mode gates on interventions, not shadow_seen, so a thread
    shadow-seen during the soak is still eligible for a real nudge."""
    from aw_store import AmbientStore

    store = AmbientStore(live_cfg.data_dir / "ambient.db")
    _seed(store, live_cfg, "who owns the runbook?", T0)
    store.mark_shadow_seen(WATCHED, f"{T0:.6f}", now=T0 + 10)
    cands = find_candidates(store, live_cfg, now=T0 + 46 * 60)
    assert len(cands) == 1, "flipping to live must not be blocked by shadow history"
    store.close()


def test_new_thread_still_digested_after_a_seen_one(cfg, store):
    cfg.cooldown_minutes = 0  # isolating dedupe from the cooldown gate
    _seed(store, cfg, "first question?", T0)
    run_gate(cfg, store, now=T0 + 46 * 60)
    _seed(store, cfg, "second unrelated question?", T0 + 60 * 60)
    out = run_gate(cfg, store, now=T0 + 2 * 60 * 60)
    assert '"wakeAgent": true' in out
