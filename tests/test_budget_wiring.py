"""Budget wiring — the spend limit is the limiter now, so it must be exact.

``test_budget.py`` covers the ledger arithmetic. This file covers the wiring:
does the gate actually meter what the judge spent, does the cap gate the call
before it happens, and does an alert reach the ops channel exactly once per
threshold per period?

Why metering happens here and not through Hermes' ``post_api_request`` hook:
``agent.auxiliary_client`` fires no plugin hooks, and even if it did the hook
runs in the gateway process while the gate is a subprocess. The gate holds
the usage object in hand, which is strictly better — it can check the cap
BEFORE spending and record EXACTLY what it spent.
"""

from conftest import WATCHED, FakeJudge, FakeTransport, make_event

from aw_budget import Budget
from aw_recorder import decide
from gate import run_gate

T0 = 1754900000.0
NOW = T0 + 46 * 60
DAY = 86400


def _seed(cfg, store, text="who owns the runbook?", ts=T0):
    decide(make_event(text=text, ts=f"{ts:.6f}"), cfg, store)


def test_usage_is_recorded_from_the_judges_own_reported_tokens(cfg, store):
    _seed(cfg, store)
    judge = FakeJudge(prompt_tokens=100_000, completion_tokens=20_000)
    run_gate(cfg, store, now=NOW, judge_fn=judge, transport=FakeTransport())

    budget = Budget(store, cfg.budget_cfg())
    # 100k in @ $5/M + 20k out @ $15/M = $0.50 + $0.30
    assert round(budget.spent_usd_channel(WATCHED, since=T0), 4) == 0.80
    assert round(budget.spent_usd_global(since=T0), 4) == 0.80


def test_a_zero_token_response_records_nothing(cfg, store):
    """No phantom rows: a provider that reports no usage must not invent cost,
    but it must not silently zero a REAL cost either — the fallback price in
    aw_budget is what protects that side (test_budget.py)."""
    _seed(cfg, store)
    run_gate(cfg, store, now=NOW,
             judge_fn=FakeJudge(prompt_tokens=0, completion_tokens=0),
             transport=FakeTransport())
    assert Budget(store, cfg.budget_cfg()).spent_usd_global(since=0) == 0


def test_spend_is_attributed_to_the_channel_that_caused_it(cfg, store):
    """One batched call, several channels: the per-channel cap is only
    meaningful if the tokens are split across the channels judged."""
    other = "C0SECOND01"
    cfg.channels = {WATCHED, other}
    _seed(cfg, store)
    decide(make_event(text="and here?", ts=f"{T0 + 5:.6f}", channel=other), cfg, store)

    judge = FakeJudge(prompt_tokens=100_000, completion_tokens=0)
    run_gate(cfg, store, now=NOW, judge_fn=judge, transport=FakeTransport())
    assert len(judge.calls[0]) == 2, "both channels should have been judged"

    budget = Budget(store, cfg.budget_cfg())
    assert round(budget.spent_usd_channel(WATCHED, since=T0), 4) == 0.25
    assert round(budget.spent_usd_channel(other, since=T0), 4) == 0.25


def test_an_alert_is_emitted_once_per_threshold_per_period(cfg, store):
    """alert75/alert95 proceed — Claude Tag warns and keeps working; only
    'exceeded' declines. But the ops channel must not be told twice."""
    cfg.daily_usd_per_channel = 1.00
    _seed(cfg, store)
    # $0.80 -> 80% of the channel cap, so the 75% threshold fires.
    judge = FakeJudge(prompt_tokens=100_000, completion_tokens=20_000)
    first = run_gate(cfg, store, now=NOW, judge_fn=judge, transport=FakeTransport())
    assert "BUDGET ALERT" in first and "75%" in first

    # A second sweep on a new thread crosses nothing new -> no repeat alert.
    _seed(cfg, store, text="another one?", ts=T0 + 600)
    second = run_gate(cfg, store, now=NOW + 600,
                      judge_fn=FakeJudge(prompt_tokens=1, completion_tokens=1),
                      transport=FakeTransport())
    assert "BUDGET ALERT" not in second, second


def test_alerts_re_arm_in_the_next_period(cfg, store):
    budget = Budget(store, dict(cfg.budget_cfg(), daily_usd_per_channel=1.00))
    budget.record_usage(WATCHED, "judge-test-model", 160_000, 0, now=0)
    assert budget.take_pending_alert(WATCHED, now=0) == 0.75
    # A day later the same ratio is a NEW period, so the operator hears again.
    budget.record_usage(WATCHED, "judge-test-model", 160_000, 0, now=DAY + 10)
    assert budget.take_pending_alert(WATCHED, now=DAY + 10) == 0.75


def test_over_cap_declines_but_never_truncates(cfg, store):
    """Claude Tag's rule, and the reason the crutches could go: work over the
    cap is refused whole, not silently degraded."""
    _seed(cfg, store)
    budget = Budget(store, cfg.budget_cfg())
    budget.record_usage(WATCHED, "judge-test-model", 200_000, 0, now=NOW - 60)
    judge = FakeJudge()
    out = run_gate(cfg, store, now=NOW, judge_fn=judge, transport=FakeTransport())
    assert judge.calls == []
    assert "DECLINED" in out and "spend cap reached" in out
    assert "WOULD HAVE POSTED" not in out


def test_a_declined_candidate_is_still_there_when_the_cap_resets(cfg, store):
    """A decline is not a verdict: no model saw the thread, so it must not
    consume the re-judge watermark. Recorded as a judgment it retired the
    thread permanently — the cap resets tomorrow but the thread was never
    looked at again, which is 'ambient silently does nothing' with no error
    anywhere."""
    _seed(cfg, store)
    budget = Budget(store, cfg.budget_cfg())
    budget.record_usage(WATCHED, "judge-test-model", 200_000, 0, now=NOW - 60)
    judge = FakeJudge()
    out = run_gate(cfg, store, now=NOW, judge_fn=judge, transport=FakeTransport())
    assert "DECLINED" in out and judge.calls == []
    assert store.judgment(WATCHED, f"{T0:.6f}")["verdict"] == "declined-exceeded"

    # A day later the spend window has rolled; the thread is still unanswered.
    later = NOW + DAY + 60
    assert budget.decision(WATCHED, now=later) == "ok"
    out = run_gate(cfg, store, now=later, judge_fn=judge, transport=FakeTransport())
    assert len(judge.calls) == 1, "the declined thread was never judged"
    assert "WOULD HAVE POSTED" in out


def test_configuring_the_budget_un_declines_the_backlog(cfg, store):
    """The shipped config carries no caps, so the first sweeps decline
    everything. When the operator sets the caps, the threads already declined
    must still be reachable."""
    cfg.daily_usd_global = cfg.daily_usd_per_channel = cfg.monthly_usd_global = 0
    _seed(cfg, store)
    judge = FakeJudge()
    assert "MISCONFIGURED" in run_gate(cfg, store, now=NOW, judge_fn=judge,
                                      transport=FakeTransport())
    assert judge.calls == []

    cfg.daily_usd_global, cfg.daily_usd_per_channel = 1.00, 0.50
    cfg.monthly_usd_global = 20.00
    out = run_gate(cfg, store, now=NOW + 600, judge_fn=judge, transport=FakeTransport())
    assert len(judge.calls) == 1, "the backlog stayed declined forever"
    assert "WOULD HAVE POSTED" in out


def test_the_spend_limit_replaces_the_deleted_daily_cap(cfg, store):
    """Behavioural proof that the crutch is not merely absent but replaced:
    thread after thread is judged with no per-day nudge cap in sight, until
    the MONEY runs out."""
    cfg.daily_usd_per_channel = 0.10   # ~2 judge calls at $0.05 each
    cfg.daily_usd_global = 99.0
    cfg.monthly_usd_global = 99.0
    judged = 0
    for i in range(6):
        _seed(cfg, store, text=f"question {i}?", ts=T0 + i * 600)
        judge = FakeJudge(prompt_tokens=10_000, completion_tokens=0)  # $0.05
        run_gate(cfg, store, now=T0 + i * 600 + 46 * 60, judge_fn=judge,
                 transport=FakeTransport())
        judged += len(judge.calls)
    assert judged == 2, f"spend, not a nudge count, must be the limiter (got {judged})"
