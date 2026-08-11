"""Live delivery: post a nudge into the originating Slack thread.

Why this exists as its own module rather than the compose agent calling
send_message: cron/scheduler.py:182 hardcodes "messaging" into every cron
session's disabled toolsets, and user config.yaml disabled_toolsets layers
ON TOP (per-job enabled_toolsets cannot re-widen it). So no cron agent can
ever send a Slack message. Delivery has to happen outside the agent.

That constraint is a gift: if the gate posts directly, no tool-bearing
agent session ever needs the untrusted excerpt at all.

Contract:
    post_nudge(cfg, store, candidate, text, transport) -> Result
- posts into candidate.thread_ts, never top-level, never a DM
- refuses any channel not in cfg.channels (fail closed)
- records the intervention + arms/retires the intent exactly once
- idempotent: a second call for the same thread is a no-op
"""

import pytest
from conftest import WATCHED, make_event

from aw_post import PostResult, post_nudge
from aw_recorder import decide

T0 = 1754900000.0


class FakeTransport:
    """Stands in for Slack chat.postMessage."""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def post(self, channel, thread_ts, text):
        self.calls.append({"channel": channel, "thread_ts": thread_ts, "text": text})
        if self.fail:
            return {"ok": False, "error": "channel_not_found"}
        return {"ok": True, "ts": "1754999999.000001"}


class Cand:
    def __init__(self, channel=WATCHED, thread_ts=f"{T0:.6f}", kind="unanswered_question",
                 last_activity=0.0):
        self.channel = channel
        self.thread_ts = thread_ts
        self.kind = kind
        self.target = f"{channel}:{thread_ts}"
        self.excerpt = "who owns the deploy runbook?"
        # The watermark the detector saw. 0.0 (the default here) is the
        # strictest reading: any human reply at all aborts the post.
        self.last_activity = last_activity


@pytest.fixture
def seeded(cfg, store):
    cfg.mode = "live"
    decide(make_event(text="who owns the deploy runbook?", ts=f"{T0:.6f}"), cfg, store)
    return cfg, store


def test_posts_into_the_exact_thread(seeded):
    cfg, store = seeded
    t = FakeTransport()
    res = post_nudge(cfg, store, Cand(), "Anyone able to answer this?", t)
    assert res.posted is True
    assert len(t.calls) == 1
    assert t.calls[0]["channel"] == WATCHED
    assert t.calls[0]["thread_ts"] == f"{T0:.6f}"          # threaded, never top-level
    assert t.calls[0]["text"] == "Anyone able to answer this?"


def test_records_intervention_so_the_thread_is_never_nudged_twice(seeded):
    cfg, store = seeded
    post_nudge(cfg, store, Cand(), "nudge", FakeTransport())
    assert store.has_intervention(WATCHED, f"{T0:.6f}") is True


def test_second_call_for_same_thread_is_a_noop(seeded):
    cfg, store = seeded
    t = FakeTransport()
    assert post_nudge(cfg, store, Cand(), "first", t).posted is True
    second = post_nudge(cfg, store, Cand(), "second", t)
    assert second.posted is False
    assert second.reason == "already-nudged"
    assert len(t.calls) == 1, "must not post twice into one thread"


def test_refuses_a_channel_outside_the_allowlist(seeded):
    cfg, store = seeded
    t = FakeTransport()
    res = post_nudge(cfg, store, Cand(channel="C0ELSEWHER"), "nudge", t)
    assert res.posted is False
    assert res.reason == "channel-not-watched"
    assert t.calls == []


def test_refuses_shadow_mode(cfg, store):
    """Shadow must be structurally incapable of posting to a watched channel."""
    cfg.mode = "shadow"
    t = FakeTransport()
    res = post_nudge(cfg, store, Cand(), "nudge", t)
    assert res.posted is False
    assert res.reason == "shadow-mode"
    assert t.calls == []


def test_refuses_empty_text(seeded):
    cfg, store = seeded
    t = FakeTransport()
    assert post_nudge(cfg, store, Cand(), "   ", t).reason == "empty-text"
    assert t.calls == []


def test_refuses_a_muted_thread(seeded):
    cfg, store = seeded
    store.mute_thread(WATCHED, f"{T0:.6f}")
    t = FakeTransport()
    assert post_nudge(cfg, store, Cand(), "nudge", t).reason == "muted"
    assert t.calls == []


def test_transport_failure_does_not_burn_the_thread(seeded):
    """A failed post must leave the thread eligible for a later retry."""
    cfg, store = seeded
    res = post_nudge(cfg, store, Cand(), "nudge", FakeTransport(fail=True))
    assert res.posted is False
    assert "channel_not_found" in res.reason
    assert store.has_intervention(WATCHED, f"{T0:.6f}") is False


def test_human_reply_after_detection_aborts_the_post(seeded):
    """Freshness re-check: never nudge a thread a human just answered."""
    cfg, store = seeded
    decide(
        make_event(text="I do", ts=f"{T0 + 300:.6f}", thread_ts=f"{T0:.6f}"), cfg, store
    )
    t = FakeTransport()
    res = post_nudge(cfg, store, Cand(), "nudge", t)
    assert res.posted is False
    assert res.reason == "answered-since-detection"
    assert t.calls == []


def test_a_thread_that_already_had_replies_can_still_be_posted_to(seeded):
    """The freshness re-check is measured against the activity the DETECTOR
    saw, not against "any reply at all". Against zero it refused every
    multi-message thread — i.e. every ``stalled_thread`` candidate, the kind
    the detector deliberately ranks HIGHEST — after the judge had already been
    paid for, so live mode could only ever post to single-message threads."""
    cfg, store = seeded
    decide(
        make_event(text="not me, asking around", ts=f"{T0 + 60:.6f}",
                   thread_ts=f"{T0:.6f}", user="U0HUMAN002"),
        cfg, store,
    )
    t = FakeTransport()
    cand = Cand(kind="stalled_thread", last_activity=T0 + 60)
    res = post_nudge(cfg, store, cand, "Want me to chase down the owner?", t)
    assert res.posted is True, res.reason
    assert len(t.calls) == 1
    assert t.calls[0]["thread_ts"] == f"{T0:.6f}"


def test_a_reply_newer_than_the_candidate_still_aborts_the_post(seeded):
    """…and the check it replaced must still fire for genuinely new replies."""
    cfg, store = seeded
    decide(
        make_event(text="not me, asking around", ts=f"{T0 + 60:.6f}",
                   thread_ts=f"{T0:.6f}", user="U0HUMAN002"),
        cfg, store,
    )
    decide(  # arrives AFTER detection
        make_event(text="mine, I'll take it", ts=f"{T0 + 900:.6f}",
                   thread_ts=f"{T0:.6f}", user="U0HUMAN003"),
        cfg, store,
    )
    t = FakeTransport()
    cand = Cand(kind="stalled_thread", last_activity=T0 + 60)
    assert post_nudge(cfg, store, cand, "nudge", t).reason == "answered-since-detection"
    assert t.calls == []


def test_result_is_reported_for_the_audit_trail(seeded):
    cfg, store = seeded
    res = post_nudge(cfg, store, Cand(), "nudge", FakeTransport())
    assert isinstance(res, PostResult)
    assert res.channel == WATCHED and res.thread_ts == f"{T0:.6f}"
