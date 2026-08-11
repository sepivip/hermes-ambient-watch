"""Where the ARRIVAL trigger meets the controls and the sweep it shares a ledger with.

`tests/test_arrival.py` proves the new trigger works. This file proves it did not
quietly take anything away from the old one:

* the in-channel mute control (`hermes ambient mute` / `ambient mute`) is an
  operator instruction, and the arrival path inherits it from the SHARED ladder
  rather than re-checking it — so a regression there would silently apply the
  control to the sweep only, on the trigger that now fires far more often;
* the two triggers run in two processes against ONE ledger, so the question
  "does a thread get judged twice / nudged twice" has to be asserted, not
  reasoned about. `has_intervention` covers the post; the `needs_judgment`
  watermark is the only thing covering the SPEND;
* the feature ships dark, and the acceptance test for the dark deploy is "the
  log says nothing changed" — which a clamp warning about a default would break.
"""

import asyncio
import json

import pytest
from conftest import BOT_ID, WATCHED, FakeJudge, FakeTransport, make_event

from aw_arrival import ArrivalRuntime
from aw_config import AmbientConfig, load_config
from aw_recorder import decide
from aw_store import AmbientStore

T0 = 1754900000.0
ROOT = f"{T0:.6f}"


class Clock:
    def __init__(self, t=1000.0):
        self.t = float(t)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += float(seconds)
        return self.t


class AsyncJudge:
    """FakeJudge behind an ``async def`` — the arrival path awaits its judge."""

    MODEL = FakeJudge.MODEL

    def __init__(self, **kwargs):
        self.inner = FakeJudge(**kwargs)

    @property
    def calls(self):
        return self.inner.calls

    async def __call__(self, nominees, cfg):
        return self.inner(nominees, cfg)


@pytest.fixture
def live_arrival(tmp_path):
    cfg = AmbientConfig(
        bot_user_id=BOT_ID,
        channels={WATCHED},
        mode="live",
        ops_channel="C0AMBOPS11",
        data_dir=tmp_path / "live",
        min_age_minutes=45,
        quiet_start="20:00",
        quiet_end="09:00",
        quiet_tz="Asia/Tbilisi",
        daily_usd_global=1.00,
        daily_usd_per_channel=0.50,
        monthly_usd_global=20.00,
        prices={FakeJudge.MODEL: (5.0, 15.0)},
    )
    cfg.arrival_enabled = True
    cfg.arrival_debounce_seconds = 90
    cfg.arrival_max_wait_seconds = 300
    cfg.arrival_judgments_per_channel_hour = 4
    cfg.arrival_judgments_global_hour = 12
    cfg.arrival_burst = 2
    cfg.arrival_max_pending = 200
    cfg.arrival_pump_interval_seconds = 5
    return cfg


def _runtime(cfg, judge, transport, clock, wall):
    store = AmbientStore(cfg.data_dir / "ambient.db")
    runtime = ArrivalRuntime(
        cfg, store, judge_fn=judge, transport=transport,
        clock=clock, wall_clock=lambda: wall,
    )
    return runtime, store


def _seed_thread(cfg, store, text="who owns the deploy runbook?", ts=T0):
    decide(make_event(text=text, ts=f"{ts:.6f}"), cfg, store)


# ================================ the mute control, on the arrival trigger too


def test_a_thread_muted_from_slack_is_never_judged_on_arrival(live_arrival):
    judge, transport, clock = AsyncJudge(), FakeTransport(), Clock()
    runtime, store = _runtime(live_arrival, judge, transport, clock, T0 + 400)
    try:
        _seed_thread(live_arrival, store)
        decide(make_event(text="ambient mute", ts=f"{T0 + 1:.6f}", thread_ts=ROOT),
               live_arrival, store)
        assert store.is_muted(WATCHED, ROOT) is True

        runtime.note(make_event(text="still nobody?", ts=f"{T0 + 2:.6f}",
                                thread_ts=ROOT))
        clock.advance(200)
        asyncio.run(runtime.drain())
        assert judge.calls == [], "paid to judge a thread an operator muted"
        assert transport.calls == []

        # NOT VACUOUS: unmute and the very same thread is judged and posted.
        decide(make_event(text="ambient unmute", ts=f"{T0 + 3:.6f}", thread_ts=ROOT),
               live_arrival, store)
        runtime.note(make_event(text="anyone?", ts=f"{T0 + 4:.6f}", thread_ts=ROOT))
        clock.advance(200)
        asyncio.run(runtime.drain())
        assert len(judge.calls) == 1
        assert len(transport.calls) == 1
    finally:
        store.close()


def test_a_channel_muted_from_slack_is_never_judged_on_arrival(live_arrival):
    judge, transport, clock = AsyncJudge(), FakeTransport(), Clock()
    runtime, store = _runtime(live_arrival, judge, transport, clock, T0 + 400)
    try:
        _seed_thread(live_arrival, store)
        decide(make_event(text="ambient mute", ts=f"{T0 + 1:.6f}"),
               live_arrival, store)  # no thread_ts -> the whole channel
        assert store.is_channel_muted(WATCHED) is True

        runtime.note(make_event(text="still nobody?", ts=f"{T0 + 2:.6f}",
                                thread_ts=ROOT))
        clock.advance(200)
        asyncio.run(runtime.drain())
        assert judge.calls == [] and transport.calls == []

        decide(make_event(text="ambient unmute", ts=f"{T0 + 3:.6f}"),
               live_arrival, store)
        runtime.note(make_event(text="anyone?", ts=f"{T0 + 4:.6f}", thread_ts=ROOT))
        clock.advance(200)
        asyncio.run(runtime.drain())
        assert len(judge.calls) == 1, "the unmute did not restore the trigger"
    finally:
        store.close()


def test_a_self_quieted_channel_is_never_judged_on_arrival(live_arrival):
    """``self_quiet_after_ignored`` is the "they keep ignoring us, stop" control.
    It is a channel-scoped rung of the shared ladder and the arrival trigger is
    the one that would burn through it fastest."""
    live_arrival.self_quiet_after_ignored = 1
    judge, transport, clock = AsyncJudge(), FakeTransport(), Clock()
    runtime, store = _runtime(live_arrival, judge, transport, clock, T0 + 400)
    try:
        _seed_thread(live_arrival, store)
        # An earlier nudge in this channel that nobody ever engaged with.
        store.record_intervention(WATCHED, f"{T0 - 5000:.6f}", kind="stalled_thread",
                                  now=T0 - 5000)
        assert store.channel_self_quieted(WATCHED, 1) is True

        runtime.note(make_event(text="still nobody?", ts=f"{T0 + 2:.6f}",
                                thread_ts=ROOT))
        clock.advance(200)
        asyncio.run(runtime.drain())
        assert judge.calls == [], "spent money in a channel that self-quieted"
        assert transport.calls == []
    finally:
        store.close()


# ================================= the two triggers must not both spend or post


def test_the_sweep_does_not_re_judge_or_re_post_what_arrival_already_posted(
    live_arrival,
):
    """After an arrival post the sweep must find nothing at all:
    ``has_intervention`` retires the thread and the re-judge watermark has been
    consumed, so one thread cannot be paid for twice or nudged twice from two
    processes."""
    from gate import run_gate

    judge, transport, clock = AsyncJudge(), FakeTransport(), Clock()
    runtime, store = _runtime(live_arrival, judge, transport, clock, T0 + 200)
    try:
        _seed_thread(live_arrival, store)
        runtime.note(make_event(text="who owns this?", ts=ROOT))
        clock.advance(100)
        asyncio.run(runtime.drain())
        assert len(transport.calls) == 1, "not vacuous: arrival really posted"

        sweep_judge, sweep_transport = FakeJudge(), FakeTransport()
        out = run_gate(live_arrival, store, now=T0 + 46 * 60,
                       judge_fn=sweep_judge, transport=sweep_transport)
        assert sweep_judge.calls == [], "the sweep paid to re-judge an arrival post"
        assert sweep_transport.calls == [], "the sweep double-posted the thread"
        assert "no candidates" in out
    finally:
        store.close()


def test_the_sweep_does_not_re_judge_a_thread_arrival_withheld(live_arrival):
    """The harder half. With no intervention row the ONLY thing standing between
    the two triggers is the ``needs_judgment`` watermark that ``record_judgment``
    wrote at arrival time — so this is the assertion that a withheld arrival
    judgment does not become a second, sweep-priced judgment of the same
    unchanged thread."""
    from gate import run_gate

    judge, clock = AsyncJudge(should_post=False), Clock()
    runtime, store = _runtime(live_arrival, judge, FakeTransport(), clock, T0 + 200)
    try:
        _seed_thread(live_arrival, store)
        runtime.note(make_event(text="who owns this?", ts=ROOT))
        clock.advance(100)
        asyncio.run(runtime.drain())
        assert len(judge.calls) == 1
        assert store.has_intervention(WATCHED, ROOT) is False

        sweep_judge = FakeJudge()
        run_gate(live_arrival, store, now=T0 + 46 * 60,
                 judge_fn=sweep_judge, transport=FakeTransport())
        assert sweep_judge.calls == [], (
            "the sweep re-judged a thread arrival had already judged, so one "
            "unchanged thread costs two calls"
        )
    finally:
        store.close()


def test_arrival_does_not_re_judge_what_the_sweep_already_nudged(live_arrival):
    """The same invariant in the other direction: the sweep posts first, then a
    human keeps talking in that thread. Every one of those messages reaches the
    arrival ladder, and only ``has_intervention`` stops each of them being a
    fresh judgment."""
    from gate import run_gate

    judge, clock = AsyncJudge(), Clock()
    runtime, store = _runtime(live_arrival, judge, FakeTransport(), clock, T0 + 47 * 60)
    try:
        _seed_thread(live_arrival, store)
        sweep_transport = FakeTransport()
        run_gate(live_arrival, store, now=T0 + 46 * 60,
                 judge_fn=FakeJudge(), transport=sweep_transport)
        assert len(sweep_transport.calls) == 1, "not vacuous: the sweep posted"

        for i in range(3):
            runtime.note(make_event(text=f"thanks, on it {i}",
                                    ts=f"{T0 + 46 * 60 + i:.6f}", thread_ts=ROOT))
            clock.advance(200)
            asyncio.run(runtime.drain())
        assert judge.calls == [], "arrival re-judged a thread the sweep had nudged"
    finally:
        store.close()


# ==================================================== the dark deploy is quiet


def test_a_dark_config_clamps_the_defaults_without_warning_about_them(tmp_path, caplog):
    """The DEPLOYED config carries ``min_age_minutes: 5``, which the DEFAULT
    ``arrival_max_wait_seconds`` of 300 collides with exactly — so a config that
    never mentions arrival mode would otherwise print a clamp warning about a
    switched-off feature on every gateway start AND every sweep tick. The
    documented first step of the rollout is "leave it false, restart, and
    confirm from the log that nothing changed", which that line would break.
    The clamp itself still runs, because it has to be right at the instant the
    boolean is flipped."""
    import logging

    def write(**extra):
        path = tmp_path / "config.json"
        path.write_text(
            json.dumps({"bot_user_id": BOT_ID, "channels": [WATCHED],
                        "min_age_minutes": 5, **extra}),
            encoding="utf-8",
        )
        return path

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="ambient_watch"):
        cfg = load_config(write())
    assert cfg.arrival_max_wait_seconds == 299, "the clamp itself must still run"
    assert cfg.min_age_minutes == 5, "the sweep's own knob is untouched"
    assert [r.getMessage() for r in caplog.records] == []

    # …and the same collision IS announced once it can actually matter.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="ambient_watch"):
        cfg = load_config(write(arrival_enabled=True))
    assert cfg.arrival_max_wait_seconds == 299
    assert any("overlaps the sweep" in r.getMessage() for r in caplog.records)
