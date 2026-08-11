"""Recorder decision logic: what pre_gateway_dispatch should do per message.

The recorder's contract (design doc Part 4, Tier 0):
- PASS (return None -> normal dispatch) for anything that isn't ours:
  non-Slack platforms, unwatched channels, DMs.
- RECORD + SKIP for unmentioned traffic in watched channels: written to
  the ledger, {"action": "skip"} so the agent never answers and no
  tokens are spent.
- RECORD + PASS for genuine @mentions and for engaged-thread follow-ups,
  preserving stock mention behavior exactly.
- Fail-safe: an exploding store must never fail open (answer everything)
  nor eat real mentions.
"""

from conftest import BOT_ID, UNWATCHED, WATCHED, make_event

from aw_recorder import Decision, decide


def test_non_slack_platform_passes_through(cfg, store):
    ev = make_event(platform="telegram")
    assert decide(ev, cfg, store) is Decision.PASS


def test_unwatched_channel_passes_through(cfg, store):
    ev = make_event(channel=UNWATCHED)
    assert decide(ev, cfg, store) is Decision.PASS


def test_dm_passes_through(cfg, store):
    ev = make_event(chat_type="dm", channel="D0DMCHAN01")
    assert decide(ev, cfg, store) is Decision.PASS


def test_plain_message_in_watched_channel_is_recorded_and_skipped(cfg, store):
    ev = make_event(text="anyone know why staging is down?")
    assert decide(ev, cfg, store) is Decision.RECORD_SKIP
    rows = store.messages_in_channel(WATCHED)
    assert len(rows) == 1
    assert rows[0]["ts"] == ev.message_id
    assert rows[0]["author"] == "U0HUMAN001"
    assert rows[0]["is_bot"] == 0


def test_mention_in_watched_channel_is_recorded_and_passed(cfg, store):
    ev = make_event(text=f"<@{BOT_ID}> can you summarize this thread?")
    assert decide(ev, cfg, store) is Decision.RECORD_PASS
    assert len(store.messages_in_channel(WATCHED)) == 1


def test_mention_marks_thread_engaged_so_followups_pass(cfg, store):
    root = "1754900000.000100"
    m1 = make_event(text=f"<@{BOT_ID}> help here", ts=root)
    assert decide(m1, cfg, store) is Decision.RECORD_PASS
    # Later un-mentioned reply in the same thread must still reach the agent
    # (stock thread-follow behavior must not break).
    m2 = make_event(text="also it affects prod", ts="1754900060.000200", thread_ts=root)
    assert decide(m2, cfg, store) is Decision.RECORD_PASS


def test_unmentioned_new_thread_is_not_engaged(cfg, store):
    m1 = make_event(text="deploy is stuck", ts="1754900000.000100")
    assert decide(m1, cfg, store) is Decision.RECORD_SKIP
    m2 = make_event(
        text="yeah seeing it too", ts="1754900060.000200", thread_ts="1754900000.000100"
    )
    assert decide(m2, cfg, store) is Decision.RECORD_SKIP


def test_bot_authored_message_is_recorded_as_bot_and_skipped(cfg, store):
    ev = make_event(text="build 4123 failed", bot_id="B0CIBOT001", user=None)
    assert decide(ev, cfg, store) is Decision.RECORD_SKIP
    rows = store.messages_in_channel(WATCHED)
    assert rows[0]["is_bot"] == 1


def test_own_bot_user_message_never_marks_engagement(cfg, store):
    """Our own posts (user == bot_user_id) must not self-engage threads."""
    ev = make_event(text="nudge text", user=BOT_ID)
    assert decide(ev, cfg, store) is Decision.RECORD_SKIP
    m2 = make_event(
        text="reply to bot", ts="1754900060.000200", thread_ts="1754900000.000100"
    )
    assert decide(m2, cfg, store) is Decision.RECORD_SKIP


def test_store_failure_fails_safe_not_open(cfg, store, monkeypatch):
    """If the ledger explodes: mentions still pass, plain traffic still skips.

    Failing open (returning PASS for everything) would make Hermes answer
    every message in a free_response channel with its full toolset.
    """

    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(store, "record_message", boom)
    plain = make_event(text="no mention here")
    mention = make_event(text=f"hey <@{BOT_ID}> status?", ts="1754900099.000300")
    assert decide(plain, cfg, store) is Decision.RECORD_SKIP
    assert decide(mention, cfg, store) is Decision.RECORD_PASS
