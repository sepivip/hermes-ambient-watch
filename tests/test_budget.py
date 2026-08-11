"""Token/$ budget — Claude Tag spend-limit parity.

Claude Tag meters token spend per org and per channel, alerts at 75% and
95% of the period limit, and *declines* work that would exceed the cap
rather than truncating it. Hermes has no such subsystem, but its
post_api_request hook hands every call's token usage to a plugin, so we
build the equivalent in the plugin's own ledger.

Design:
- record_usage(channel, model, prompt, completion) tallies tokens and
  converts to USD via a model price table.
- spent_usd(period) / remaining_usd — per-channel and global rollups over
  a daily and a monthly window.
- decision(channel) -> "ok" | "alert75" | "alert95" | "exceeded"
  drives the gate: "exceeded" means decline (never truncate).
"""

from conftest import WATCHED

from aw_budget import Budget

DAY = 86400
MONTH = 30 * DAY


def _budget(store, **kw):
    cfg = dict(
        daily_usd_global=1.00,
        daily_usd_per_channel=0.50,
        monthly_usd_global=20.00,
        alert_thresholds=(0.75, 0.95),
        prices={"gpt-5.6-sol": (5.0, 15.0)},  # $/1M input, $/1M output
    )
    cfg.update(kw)
    return Budget(store, cfg)


def test_records_usage_and_computes_usd(store):
    b = _budget(store)
    # 100k input @ $5/M + 20k output @ $15/M = 0.50 + 0.30 = $0.80
    b.record_usage(WATCHED, "gpt-5.6-sol", 100_000, 20_000, now=0)
    assert round(b.spent_usd_channel(WATCHED, since=-1), 4) == 0.80


def test_unknown_model_falls_back_to_a_default_price(store):
    b = _budget(store)
    b.record_usage(WATCHED, "some-new-model", 1_000_000, 0, now=0)
    # must not silently cost $0 — an unpriced model is a budgeting hole
    assert b.spent_usd_channel(WATCHED, since=-1) > 0


def test_channel_decision_crosses_alert_and_exceeded_thresholds(store):
    b = _budget(store, daily_usd_per_channel=1.00)
    assert b.decision(WATCHED, now=10) == "ok"
    b.record_usage(WATCHED, "gpt-5.6-sol", 150_000, 0, now=10)   # $0.75 -> 75%
    assert b.decision(WATCHED, now=10) == "alert75"
    b.record_usage(WATCHED, "gpt-5.6-sol", 40_000, 0, now=11)    # $0.95 -> 95%
    assert b.decision(WATCHED, now=11) == "alert95"
    b.record_usage(WATCHED, "gpt-5.6-sol", 20_000, 0, now=12)    # $1.05 -> over
    assert b.decision(WATCHED, now=12) == "exceeded"


def test_global_cap_can_exceed_before_channel_cap(store):
    b = _budget(store, daily_usd_global=0.50, daily_usd_per_channel=99.0)
    b.record_usage("C0OTHER", "gpt-5.6-sol", 120_000, 0, now=5)  # $0.60 global
    assert b.decision(WATCHED, now=5) == "exceeded"  # global cap hit, all channels declined


def test_daily_window_rolls_over(store):
    b = _budget(store, daily_usd_per_channel=0.50)
    b.record_usage(WATCHED, "gpt-5.6-sol", 120_000, 0, now=0)    # $0.60 today
    assert b.decision(WATCHED, now=100) == "exceeded"
    assert b.decision(WATCHED, now=DAY + 100) == "ok"  # next day resets


def test_monthly_cap_independent_of_daily(store):
    b = _budget(store, daily_usd_global=100.0, monthly_usd_global=1.00)
    # Spread small spend across many days: under daily, over monthly.
    for d in range(5):
        b.record_usage(WATCHED, "gpt-5.6-sol", 50_000, 0, now=d * DAY)  # $0.25/day
    assert b.decision(WATCHED, now=5 * DAY) == "exceeded"  # $1.25 > $1.00/mo


def test_alert_fires_once_per_threshold_per_period(store):
    b = _budget(store, daily_usd_per_channel=1.00)
    b.record_usage(WATCHED, "gpt-5.6-sol", 160_000, 0, now=0)  # 80%
    assert b.take_pending_alert(WATCHED, now=0) == 0.75
    assert b.take_pending_alert(WATCHED, now=0) is None  # not re-fired same period
