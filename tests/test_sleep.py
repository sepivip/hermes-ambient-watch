"""Channel sleep — stop paying to judge a channel that never wants us.

Copied from Claude Tag's 2026-08-13 update: "In a channel where, message after
message, Claude keeps concluding it has nothing to add, it goes to sleep. A
@-mention wakes it instantly."

The distinction from self-quiet, which already exists: self-quiet counts NUDGES
nobody engaged with — it needs us to have POSTED. Sleep counts consecutive SKIP
verdicts — futile *judgments*, before any post. In a channel where the judge
always says no, self-quiet never triggers (we never post) and we keep paying for
a judge call on every message forever. Sleep is the money fix: after N
consecutive skips the channel is asleep and the prefilter drops its nominees
BEFORE the budget/judge stage, so a dead channel costs nothing.

Waking is the paired half and it is also a self-quiet bug we already had: a
mention must reset both counters, or a channel that went quiet stays dead even
after someone explicitly asks for the bot.
"""

from conftest import BOT_ID, WATCHED, make_event

from aw_detectors import find_candidates
from aw_recorder import decide

T0 = 1754900000.0


def _q(store, cfg, text="does anyone know the timeout?", ts=T0):
    decide(make_event(text=text, ts=f"{ts:.6f}"), cfg, store)


def test_a_channel_sleeps_after_n_consecutive_skips(cfg, store):
    cfg.sleep_after_skips = 3
    for i in range(3):
        store.record_judgment(WATCHED, f"{T0 + i:.6f}", "skip", now=T0 + i)
    assert store.channel_asleep(WATCHED, cfg.sleep_after_skips) is True


def test_below_the_threshold_the_channel_is_awake(cfg, store):
    cfg.sleep_after_skips = 3
    for i in range(2):
        store.record_judgment(WATCHED, f"{T0 + i:.6f}", "skip", now=T0 + i)
    assert store.channel_asleep(WATCHED, cfg.sleep_after_skips) is False


def test_a_post_verdict_breaks_the_skip_streak(cfg, store):
    cfg.sleep_after_skips = 3
    store.record_judgment(WATCHED, "1.0", "skip", now=T0)
    store.record_judgment(WATCHED, "2.0", "skip", now=T0 + 1)
    store.record_judgment(WATCHED, "3.0", "post", now=T0 + 2)  # engaged — streak resets
    store.record_judgment(WATCHED, "4.0", "skip", now=T0 + 3)
    assert store.channel_asleep(WATCHED, cfg.sleep_after_skips) is False


def test_a_sleeping_channel_yields_no_candidates(cfg, store):
    cfg.sleep_after_skips = 3
    for i in range(3):
        store.record_judgment(WATCHED, f"{T0 + i:.6f}", "skip", now=T0 + i)
    _q(store, cfg, ts=T0 + 100)
    assert find_candidates(store, cfg, now=T0 + 100 + 46 * 60) == []


def test_a_mention_wakes_a_sleeping_channel_instantly(cfg, store):
    """'A @-mention wakes it instantly.' The mention itself is not judged
    (it passes through to a normal session), but it must clear the sleep so the
    NEXT ordinary message is considered again."""
    cfg.sleep_after_skips = 3
    for i in range(3):
        store.record_judgment(WATCHED, f"{T0 + i:.6f}", "skip", now=T0 + i)
    assert store.channel_asleep(WATCHED, cfg.sleep_after_skips) is True

    decide(make_event(text=f"<@{BOT_ID}> you there?", ts=f"{T0 + 200:.6f}"), cfg, store)
    assert store.channel_asleep(WATCHED, cfg.sleep_after_skips) is False

    _q(store, cfg, ts=T0 + 300)
    assert len(find_candidates(store, cfg, now=T0 + 300 + 46 * 60)) == 1


def test_a_mention_also_re_arms_self_quiet(cfg, store):
    """The pre-existing gap this fixes: self-quiet counted ignored nudges and
    nothing reset it, so a self-quieted channel stayed dead through a mention.
    Claude Tag wakes on mention; so must we, for BOTH counters."""
    cfg.self_quiet_after_ignored = 3
    for i in range(3):
        store.record_intervention(WATCHED, f"{T0 + i:.6f}", kind="x", now=T0 + i)
    assert store.channel_self_quieted(WATCHED, cfg.self_quiet_after_ignored) is True

    decide(make_event(text=f"<@{BOT_ID}> hello", ts=f"{T0 + 500:.6f}"), cfg, store)
    assert store.channel_self_quieted(WATCHED, cfg.self_quiet_after_ignored) is False


def test_sleep_disabled_when_threshold_is_zero(cfg, store):
    cfg.sleep_after_skips = 0
    for i in range(20):
        store.record_judgment(WATCHED, f"{T0 + i:.6f}", "skip", now=T0 + i)
    assert store.channel_asleep(WATCHED, 0) is False
