"""Detector tests — updated per adversarial review.

New contracts encoded:
- Engaged threads (bot is conversing there) are never candidates.
- At most ONE candidate per channel per sweep (respects the cooldown's
  intent; review showed 3-at-once bypassed it).
- Retention pruning exists and is exercised by the sweep.
"""

from conftest import BOT_ID, WATCHED, make_event

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
    assert c.thread_ts == f"{T0:.6f}"


def test_fresh_question_is_not_yet_candidate(cfg, store):
    _seed(store, cfg, "why is CI red?", T0)
    assert find_candidates(store, cfg, now=T0 + 10 * 60) == []


def test_answered_question_is_not_candidate(cfg, store):
    _seed(store, cfg, "why is CI red?", T0)
    _seed(store, cfg, "flaky runner, rerunning now", T0 + 300, thread_ts=T0, user="U0HUMAN002")
    assert find_candidates(store, cfg, now=T0 + 46 * 60) == []


def test_engaged_thread_is_never_candidate(cfg, store):
    """The bot is already conversing there (mention) — nudging it would be
    absurd. Review finding: detectors ignored engagement entirely."""
    _seed(store, cfg, f"<@{BOT_ID}> why is CI red?", T0)
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


def test_at_most_one_candidate_per_channel_per_sweep(cfg, store):
    """Review finding: 3 same-channel candidates in one sweep bypassed the
    120-min cooldown. One per channel per sweep restores its intent."""
    for i in range(4):
        _seed(store, cfg, f"question number {i}?", T0 + i)
    cands = find_candidates(store, cfg, now=T0 + 46 * 60)
    assert len(cands) == 1
    assert cands[0].thread_ts == f"{T0:.6f}"  # oldest first


def test_per_channel_daily_cap_suppresses_candidates(cfg, store):
    _seed(store, cfg, "unanswered?", T0)
    for i in range(3):
        store.record_intervention(WATCHED, f"{T0 + 100 + i:.6f}", kind="x", now=T0 + 200 + i)
    assert find_candidates(store, cfg, now=T0 + 46 * 60 + 120 * 60) == []


def test_channel_cooldown_suppresses_candidates(cfg, store):
    _seed(store, cfg, "first question?", T0)
    store.record_intervention(WATCHED, "1754800000.000000", kind="x", now=T0 + 45 * 60)
    assert find_candidates(store, cfg, now=T0 + 46 * 60) == []
    assert len(find_candidates(store, cfg, now=T0 + (46 + 120) * 60)) == 1


def test_quiet_hours_suppress_candidates(cfg, store):
    night = 1786561200.0  # 2026-08-11 19:00 UTC == 23:00 Asia/Tbilisi
    _seed(store, cfg, "another q?", night - 60 * 60)
    assert find_candidates(store, cfg, now=night) == []


def test_self_quiet_after_consecutive_ignored(cfg, store):
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


def test_prune_removes_expired_messages_but_keeps_recent(cfg, store):
    _seed(store, cfg, "ancient question?", T0)
    _seed(store, cfg, "recent question?", T0 + 15 * 86400)
    removed = store.prune(now=T0 + 15 * 86400 + 60, retention_days=cfg.retention_days)
    assert removed >= 1
    remaining = [m["text"] for m in store.messages_in_channel(WATCHED)]
    assert remaining == ["recent question?"]
