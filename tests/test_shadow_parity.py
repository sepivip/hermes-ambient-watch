"""Shadow mode must be a faithful dry run of live mode.

REPLACES ``test_shadow_simulates_caps.py``. That file existed because shadow
mode records no interventions, which silently made live's cooldown and daily
caps inert during a soak — so the soak over-reported and precision tuning
happened against a volume live could never produce. Those caps are gone (the
spend limit replaced them), so simulating them is meaningless.

The parity that matters now is different, and stricter, because shadow mode
costs real money: it runs the SAME prefilter and the SAME judge as live, and
must differ in exactly one respect — it does not post. So a soak measures the
real precision of the real judge at the real price.
"""

from conftest import WATCHED, FakeJudge, FakeTransport, make_event

from aw_budget import Budget
from aw_detectors import find_candidates
from aw_recorder import decide
from gate import run_gate

T0 = 1754900000.0
DAY = 86400


def _ask(store, cfg, text, ts):
    decide(make_event(text=text, ts=f"{ts:.6f}"), cfg, store)


def test_shadow_spends_real_money_and_meters_it(cfg, store):
    """A soak that judged for free would be measuring a different product."""
    _ask(store, cfg, "who owns the runbook?", T0)
    judge = FakeJudge(prompt_tokens=100_000, completion_tokens=20_000)
    run_gate(cfg, store, now=T0 + 46 * 60, judge_fn=judge, transport=FakeTransport())

    budget = Budget(store, cfg.budget_cfg())
    # 100k in @ $5/M + 20k out @ $15/M = $0.80
    assert round(budget.spent_usd_channel(WATCHED, since=T0), 4) == 0.80


def test_shadow_uses_the_same_prefilter_and_judge_as_live(cfg, live_cfg, store):
    from aw_store import AmbientStore

    _ask(store, cfg, "who owns the runbook?", T0)
    shadow_judge = FakeJudge()
    run_gate(cfg, store, now=T0 + 46 * 60, judge_fn=shadow_judge,
             transport=FakeTransport())

    live_store = AmbientStore(live_cfg.data_dir / "ambient.db")
    try:
        _ask(live_store, live_cfg, "who owns the runbook?", T0)
        live_judge = FakeJudge()
        run_gate(live_cfg, live_store, now=T0 + 46 * 60, judge_fn=live_judge,
                 transport=FakeTransport())
        assert [c.thread_ts for c in shadow_judge.calls[0]] == [
            c.thread_ts for c in live_judge.calls[0]
        ]
    finally:
        live_store.close()


def test_the_only_difference_is_the_post(cfg, store):
    _ask(store, cfg, "who owns the runbook?", T0)
    transport = FakeTransport()
    out = run_gate(cfg, store, now=T0 + 46 * 60, judge_fn=FakeJudge(),
                   transport=transport)
    assert "WOULD HAVE POSTED" in out
    assert transport.calls == []
    assert store.has_intervention(WATCHED, f"{T0:.6f}") is False


def test_shadow_history_never_blocks_a_real_nudge_after_the_flip(live_cfg):
    """Live gates on interventions, not shadow_seen, so flipping to live is
    not throttled by weeks of soak history."""
    from aw_store import AmbientStore

    store = AmbientStore(live_cfg.data_dir / "ambient.db")
    try:
        _ask(store, live_cfg, "a question?", T0)
        store.mark_shadow_seen(WATCHED, f"{T0:.6f}", now=T0 + 10)
        assert len(find_candidates(store, live_cfg, now=T0 + 46 * 60)) == 1
    finally:
        store.close()


def test_a_shadow_soak_does_not_re_judge_the_same_thread_forever(cfg, store):
    """The soak's cost must not scale with the sweep cadence: the shadow_seen
    ledger and the re-judge watermark both stop a second look."""
    _ask(store, cfg, "who owns the runbook?", T0)
    judge = FakeJudge()
    run_gate(cfg, store, now=T0 + 46 * 60, judge_fn=judge, transport=FakeTransport())
    run_gate(cfg, store, now=T0 + 60 * 60, judge_fn=judge, transport=FakeTransport())
    run_gate(cfg, store, now=T0 + 5 * DAY, judge_fn=judge, transport=FakeTransport())
    assert len(judge.calls) == 1, "the same thread was judged more than once"
