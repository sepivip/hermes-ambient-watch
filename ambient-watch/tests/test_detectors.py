"""Detectors: which threads become nudge candidates, and which never do.

Times are Slack ts strings (epoch seconds). Detector entry point:
    find_candidates(store, cfg, now_epoch) -> list[Candidate]
Candidate carries channel, thread_ts, kind, target ("C...:<thread_ts>").
"""

from conftest import WATCHED, make_event

from aw_detectors import find_candidates
from aw_recorder import decide

T0 = 1754900000.0


def _seed(store, cfg, text, ts, thread_ts=None, bot_id=None, user="U0HUMAN001"):
    ev = make_event(
        text=text, ts=f"{ts:.6f}", thread_ts=(f"{thread_ts:.6f}" if thread_ts else None),
        bot_id=bot_id, user=user,
    )
    decide(ev, cfg, store)


def test_old_unanswered_question_is_candidate(cfg, store):
    _seed(store, cfg, "does anyone know why the deploy failed?", T0)
    cands = find_candidates(store, cfg, now=T0 + 46 * 60)
    assert len(cands) == 1
    c = cands[0]
    assert c.kind == "unanswered_question"
    assert c.channel == WATCHED
    assert c.target == f"{WATCHED}:{T0:.6f}"


def test_fresh_question_is_not_yet_candidate(cfg, store):
    _seed(store, cfg, "why is CI red?", T0)
    assert find_candidates(store, cfg, now=T0 + 10 * 60) == []


def test_answered_question_is_not_candidate(cfg, store):
    _seed(store, cfg, "why is CI red?", T0)
    _seed(store, cfg, "flaky runner, rerunning now", T0 + 300, thread_ts=T0, user="U0HUMAN002")
    assert find_candidates(store, cfg, now=T0 + 46 * 60) == []


def test_non_question_becomes_stalled_candidate_only_after_stall_window(cfg, store):
    _seed(store, cfg, "we should decide on the retry policy", T0)
    assert find_candidates(store, cfg, now=T0 + 60 * 60) == []
    cands = find_candidates(store, cfg, now=T0 + 241 * 60)
    assert [c.kind for c in cands] == ["stalled_thread"]


def test_bot_authored_root_is_never_candidate(cfg, store):
    _seed(store, cfg, "Build #4123 failed?", T0, bot_id="B0CIBOT001", user=None)
    assert find_candidates(store, cfg, now=T0 + 300 * 60) == []


def test_muted_thread_is_not_candidate(cfg, store):
    _seed(store, cfg, "why is prod slow?", T0)
    store.mute_thread(WATCHED, f"{T0:.6f}")
    assert find_candidates(store, cfg, now=T0 + 46 * 60) == []


def test_thread_with_prior_intervention_is_not_candidate(cfg, store):
    _seed(store, cfg, "why is prod slow?", T0)
    store.record_intervention(WATCHED, f"{T0:.6f}", kind="unanswered_question", now=T0 + 100)
    assert find_candidates(store, cfg, now=T0 + 46 * 60) == []


def test_per_channel_daily_cap_suppresses_candidates(cfg, store):
    for i in range(4):
        _seed(store, cfg, f"question number {i}?", T0 + i)
    for i in range(3):
        store.record_intervention(WATCHED, f"{T0 + 100 + i:.6f}", kind="x", now=T0 + 200 + i)
    assert find_candidates(store, cfg, now=T0 + 46 * 60) == []


def test_channel_cooldown_suppresses_candidates(cfg, store):
    _seed(store, cfg, "first question?", T0)
    store.record_intervention(WATCHED, "1754800000.000000", kind="x", now=T0 + 45 * 60)
    # 45 min after an intervention, cooldown (120 min) still active.
    assert find_candidates(store, cfg, now=T0 + 46 * 60) == []
    # After cooldown expires the candidate surfaces.
    assert len(find_candidates(store, cfg, now=T0 + (46 + 120) * 60)) == 1


def test_quiet_hours_suppress_candidates(cfg, store):
    _seed(store, cfg, "late night question?", T0)
    # 23:00 Tbilisi (UTC+4) == 19:00 UTC. T0 anchor: pick an epoch known to be
    # 23:00 in Asia/Tbilisi: 2026-08-11 19:00:00 UTC = 1786561200.
    night = 1786561200.0
    _seed(store, cfg, "another q?", night - 60 * 60)
    assert find_candidates(store, cfg, now=night) == []


def test_self_quiet_after_consecutive_ignored(cfg, store):
    """After N interventions with no human engagement, channel goes quiet."""
    for i in range(4):
        ts = T0 + i * 10
        store.record_intervention(WATCHED, f"{ts:.6f}", kind="x", now=ts)
    assert store.channel_self_quieted(WATCHED, threshold=4) is True
    _seed(store, cfg, "new question?", T0 + 1000)
    assert find_candidates(store, cfg, now=T0 + 1000 + 46 * 60 + 120 * 60) == []


def test_human_engagement_resets_self_quiet(cfg, store):
    for i in range(3):
        ts = T0 + i * 10
        store.record_intervention(WATCHED, f"{ts:.6f}", kind="x", now=ts)
    store.record_engagement(WATCHED, f"{T0:.6f}")
    assert store.channel_self_quieted(WATCHED, threshold=4) is False
