"""A thread can be too old to answer.

FOUND LIVE, 2026-08-12. "Whats Tbilisi population?" was asked at 18:15 on the
11th, judged in shadow mode (so never posted), and then answered at 10:27 the
next morning — 972 minutes later — because flipping mode to live made every
previously shadow-only thread eligible again. `shadow_seen` deliberately does
not gate live mode, and nothing else capped age, so the flip flushed a
backlog into the channel.

Two things are wrong with that, and neither is latency:
  · answering a 16-hour-old question unprompted is noise; the conversation has
    moved on, and Claude Tag's routines "post only when something changed"
  · it arrives as a BURST on every mode flip or marker clear, which is exactly
    when an operator is least expecting output

So there is a ceiling: past `max_age_minutes` a thread is retired, not
answered. It is checked in the shared eligibility ladder, so it applies to the
arrival path and the sweep identically — and it must not fire for the ordinary
case, or ambient stops working altogether.
"""

from conftest import WATCHED, make_event

from aw_detectors import find_candidates
from aw_recorder import decide

T0 = 1754900000.0
HOUR = 3600


def _ask(store, cfg, text="who owns the deploy runbook?", ts=T0):
    # Quiet hours would otherwise decide these tests instead of the ceiling:
    # T0 + 16h lands inside the default 20:00-09:00 window, so an early draft
    # of this file "passed" while measuring nothing. Disable it here and let
    # test_detectors own quiet-hour behaviour.
    cfg.quiet_start = cfg.quiet_end = "00:00"
    decide(make_event(text=text, ts=f"{ts:.6f}"), cfg, store)


def test_a_fresh_thread_is_still_a_candidate(cfg, store):
    """The ceiling must not break the ordinary case."""
    cfg.max_age_minutes = 12 * 60
    _ask(store, cfg)
    assert len(find_candidates(store, cfg, now=T0 + 46 * 60)) == 1


def test_a_thread_older_than_the_ceiling_is_retired(cfg, store):
    cfg.max_age_minutes = 12 * 60
    _ask(store, cfg)
    assert find_candidates(store, cfg, now=T0 + 16 * HOUR) == []


def test_the_ceiling_is_measured_from_the_last_activity_not_the_root(cfg, store):
    """A long-running thread that people are still talking in is not stale;
    only silence makes it stale."""
    cfg.max_age_minutes = 12 * 60
    _ask(store, cfg)
    # someone spoke 15 hours after the root, and it is asked-shaped
    decide(
        make_event(text="any update on this?", ts=f"{T0 + 15 * HOUR:.6f}",
                   thread_ts=f"{T0:.6f}", user="U0HUMAN002"),
        cfg, store,
    )
    assert len(find_candidates(store, cfg, now=T0 + 16 * HOUR)) == 1


def test_the_ceiling_applies_to_the_arrival_path_too(cfg, store):
    """One ladder, both triggers — the arrival path calls the same function."""
    cfg.max_age_minutes = 12 * 60
    _ask(store, cfg)
    assert find_candidates(
        store, cfg, now=T0 + 16 * HOUR, only=(WATCHED, f"{T0:.6f}")
    ) == []


def test_zero_or_missing_ceiling_means_no_ceiling(cfg, store):
    """Backwards compatible: a deployed config without the key behaves as
    before rather than silently going quiet."""
    cfg.max_age_minutes = 0
    _ask(store, cfg)
    assert len(find_candidates(store, cfg, now=T0 + 30 * 24 * HOUR)) == 1


def test_a_mode_flip_does_not_flush_a_stale_backlog(cfg, store):
    """The exact live incident, as a regression test: three threads judged in
    shadow yesterday must not all be answered when live mode arrives."""
    cfg.max_age_minutes = 12 * 60
    for i in range(3):
        _ask(store, cfg, text=f"stale question {i}?", ts=T0 + i * 60)
        store.mark_shadow_seen(WATCHED, f"{T0 + i * 60:.6f}", now=T0 + 46 * 60)
    cfg.mode = "live"          # the flip
    assert find_candidates(store, cfg, now=T0 + 16 * HOUR) == []
