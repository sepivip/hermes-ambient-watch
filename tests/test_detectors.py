"""Prefilter tests — what the deterministic stage may and may not decide.

The big change: the prefilter no longer judges. It used to nominate a thread
because the text contained ``?`` or matched an ask-language regex, which was
the whole of the plugin's "judgment". Now it nominates anything quiet enough
that no hard rule excludes, and the model decides (test_judge.py,
test_live_delivery.py).

RETIRED HERE, DELIBERATELY:
- ``test_per_channel_daily_cap_suppresses_candidates``
- ``test_channel_cooldown_suppresses_candidates``
- ``test_non_question_becomes_stalled_candidate_only_after_stall_window``
The first two asserted crutches that are gone (the spend limit is the real
limiter — test_budget_wiring.py). The third asserted the regex heuristic.
Their replacements are ``test_the_regex_heuristic_no_longer_gates_anything``
and the re-judge watermark tests below, which are what actually bounds cost
now.
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


def test_old_quiet_thread_is_a_nominee(cfg, store):
    _seed(store, cfg, "does anyone know why the deploy failed?", T0)
    cands = find_candidates(store, cfg, now=T0 + 46 * 60)
    assert len(cands) == 1
    c = cands[0]
    assert c.kind == "unanswered_question"
    assert c.channel == WATCHED
    assert c.thread_ts == f"{T0:.6f}"
    assert c.judge_view, "a nominee must carry the view the judge reads"


def test_fresh_thread_is_not_yet_a_nominee(cfg, store):
    _seed(store, cfg, "why is CI red?", T0)
    assert find_candidates(store, cfg, now=T0 + 10 * 60) == []


def test_a_still_moving_thread_is_left_alone(cfg, store):
    """min_age_minutes is measured from the LAST activity, not the root: a
    thread someone is still typing in does not need us."""
    _seed(store, cfg, "why is CI red?", T0)
    _seed(store, cfg, "looking now", T0 + 45 * 60, thread_ts=T0, user="U0HUMAN002")
    assert find_candidates(store, cfg, now=T0 + 46 * 60) == []
    # …and once it goes quiet it becomes a nominee, labelled as stalled.
    cands = find_candidates(store, cfg, now=T0 + 45 * 60 + 46 * 60)
    assert [c.kind for c in cands] == ["stalled_thread"]


def test_the_regex_heuristic_no_longer_gates_anything(cfg, store):
    """A statement with no '?' and no ask-language used to be invisible for
    four hours and then only via the stalled path. It is now a nominee like
    any other quiet thread — whether it deserves a nudge is the judge's call,
    which is the entire point of the parity work."""
    _seed(store, cfg, "the retry policy is still undecided", T0)
    cands = find_candidates(store, cfg, now=T0 + 46 * 60)
    assert len(cands) == 1, "the prefilter must not second-guess usefulness"


def test_engaged_thread_is_never_a_nominee(cfg, store):
    """The bot is already conversing there (mention) — nudging it would be
    absurd, and it is also the anti-feedback-loop rule."""
    _seed(store, cfg, f"<@{BOT_ID}> why is CI red?", T0)
    assert find_candidates(store, cfg, now=T0 + 46 * 60) == []


def test_bot_authored_root_is_never_a_nominee(cfg, store):
    """Our own nudge is a channel message the recorder sees. With cooldowns
    gone, this rule plus once-per-thread is the whole defence against a
    self-reinforcing loop, so it gets its own test."""
    _seed(store, cfg, "Build #4123 failed?", T0, bot_id="B0CIBOT001", user=None)
    assert find_candidates(store, cfg, now=T0 + 300 * 60) == []


def test_muted_thread_is_not_a_nominee(cfg, store):
    _seed(store, cfg, "why is prod slow?", T0)
    store.mute_thread(WATCHED, f"{T0:.6f}")
    assert find_candidates(store, cfg, now=T0 + 46 * 60) == []


def test_muted_channel_is_not_a_nominee(cfg, store):
    _seed(store, cfg, "why is prod slow?", T0)
    store.mute_channel(WATCHED)
    assert find_candidates(store, cfg, now=T0 + 46 * 60) == []


def test_thread_with_prior_intervention_is_not_a_nominee(cfg, store):
    """Once per thread, forever — the one hard nudge cap Claude Tag has too."""
    _seed(store, cfg, "why is prod slow?", T0)
    store.record_intervention(WATCHED, f"{T0:.6f}", kind="unanswered_question", now=T0 + 100)
    assert find_candidates(store, cfg, now=T0 + 46 * 60) == []


def test_at_most_one_nominee_per_channel_per_sweep(cfg, store):
    """Throughput limit (Claude Tag has rate limits, not cooldowns). The most
    engaged, most recently active thread wins — a three-day-dead thread should
    not outrank one that stalled an hour ago."""
    for i in range(4):
        _seed(store, cfg, f"question number {i}?", T0 + i * 600)
    cands = find_candidates(store, cfg, now=T0 + 4 * 600 + 46 * 60)
    assert len(cands) == 1
    assert cands[0].thread_ts == f"{T0 + 3 * 600:.6f}", "most recent should win"


def test_a_busier_thread_outranks_a_quieter_one(cfg, store):
    _seed(store, cfg, "lonely question?", T0 + 600)
    _seed(store, cfg, "busy question?", T0)
    _seed(store, cfg, "same here", T0 + 60, thread_ts=T0, user="U0HUMAN002")
    _seed(store, cfg, "and me", T0 + 120, thread_ts=T0, user="U0HUMAN003")
    cands = find_candidates(store, cfg, now=T0 + 600 + 46 * 60)
    assert len(cands) == 1
    assert cands[0].thread_ts == f"{T0:.6f}"
    assert cands[0].human_participants == 3


# ------------------------------------------------- the re-judge watermark


def test_a_judged_thread_is_not_re_judged_without_new_activity(cfg, store):
    """THIS IS WHAT REPLACED THE COOLDOWN. A declined thread must not cost a
    judge call on every sweep merely by continuing to exist — that, and not
    "one nudge per two hours", was all the cooldown ever really bought."""
    _seed(store, cfg, "who owns this?", T0)
    cands = find_candidates(store, cfg, now=T0 + 46 * 60)
    assert len(cands) == 1

    store.record_judgment(
        WATCHED, f"{T0:.6f}", "skip", confidence=0.2,
        last_activity_seen=cands[0].last_activity, now=T0 + 46 * 60,
    )
    assert find_candidates(store, cfg, now=T0 + 90 * 60) == []
    assert find_candidates(store, cfg, now=T0 + 10 * 86400) == []


def test_new_human_activity_re_opens_a_judged_thread(cfg, store):
    _seed(store, cfg, "who owns this?", T0)
    store.record_judgment(
        WATCHED, f"{T0:.6f}", "skip", last_activity_seen=T0, now=T0 + 46 * 60,
    )
    assert find_candidates(store, cfg, now=T0 + 90 * 60) == []

    _seed(store, cfg, "still nobody?", T0 + 100 * 60, thread_ts=T0, user="U0HUMAN002")
    assert len(find_candidates(store, cfg, now=T0 + 150 * 60)) == 1


def test_re_judging_is_bounded(cfg, store):
    """judge_max_rejudge caps even the new-activity path, so a chatty thread
    cannot be judged indefinitely."""
    cfg.judge_max_rejudge = 1
    _seed(store, cfg, "who owns this?", T0)
    for i in range(3):
        ts = T0 + (i + 1) * 100 * 60
        store.record_judgment(
            WATCHED, f"{T0:.6f}", "skip", last_activity_seen=ts - 60, now=ts,
        )
        _seed(store, cfg, f"ping {i}", ts, thread_ts=T0, user="U0HUMAN002")
    assert find_candidates(store, cfg, now=T0 + 500 * 60) == []


def test_quiet_hours_suppress_nominees(cfg, store):
    night = 1786561200.0  # 2026-08-11 19:00 UTC == 23:00 Asia/Tbilisi
    _seed(store, cfg, "another q?", night - 60 * 60)
    assert find_candidates(store, cfg, now=night) == []


def test_self_quiet_after_consecutive_ignored(cfg, store):
    for i in range(4):
        ts = T0 + i * 10
        store.record_intervention(WATCHED, f"{ts:.6f}", kind="x", now=ts)
    assert store.channel_self_quieted(WATCHED, threshold=4) is True
    _seed(store, cfg, "new question?", T0 + 1000)
    assert find_candidates(store, cfg, now=T0 + 1000 + 46 * 60) == []


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
