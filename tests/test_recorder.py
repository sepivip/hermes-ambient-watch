"""Recorder decision logic — updated per adversarial review.

New contracts encoded:
- Slash-command events (COMMAND type / ts-less slash payload) always PASS.
- Replies in threads we nudged (intervention rows) PASS + count as
  engagement + retire the intent (closes the dead-code feedback loop).
- Block Kit-only mentions (no <@id> in flat text) are mentions (#52387).
"""

from conftest import BOT_ID, UNWATCHED, WATCHED, make_event, make_slash_event

from aw_recorder import Decision, decide


def test_non_slack_platform_passes_through(cfg, store):
    assert decide(make_event(platform="telegram"), cfg, store) is Decision.PASS


def test_unwatched_channel_passes_through(cfg, store):
    assert decide(make_event(channel=UNWATCHED), cfg, store) is Decision.PASS


def test_dm_passes_through(cfg, store):
    assert (
        decide(make_event(chat_type="dm", channel="D0DMCHAN01"), cfg, store)
        is Decision.PASS
    )


def test_slash_command_in_watched_channel_passes_through(cfg, store):
    """adapter.py:7753 slash shape: chat_type='group', no metadata, no ts."""
    assert decide(make_slash_event(channel=WATCHED), cfg, store) is Decision.PASS


def test_plain_message_in_watched_channel_is_recorded_and_skipped(cfg, store):
    ev = make_event(text="anyone know why staging is down?")
    assert decide(ev, cfg, store) is Decision.RECORD_SKIP
    rows = store.messages_in_channel(WATCHED)
    assert len(rows) == 1
    assert rows[0]["ts"] == ev.message_id
    assert rows[0]["is_bot"] == 0


def test_mention_in_watched_channel_is_recorded_and_passed(cfg, store):
    ev = make_event(text=f"<@{BOT_ID}> can you summarize this thread?")
    assert decide(ev, cfg, store) is Decision.RECORD_PASS


def test_blockkit_only_mention_is_a_mention(cfg, store):
    """Mentions can exist only in Block Kit blocks, not flat text (#52387)."""
    blocks = [
        {
            "type": "rich_text",
            "elements": [
                {
                    "type": "rich_text_section",
                    "elements": [
                        {"type": "user", "user_id": BOT_ID},
                        {"type": "text", "text": " thoughts?"},
                    ],
                }
            ],
        }
    ]
    ev = make_event(text="thoughts?", blocks=blocks)
    assert decide(ev, cfg, store) is Decision.RECORD_PASS


def test_quoted_mention_in_blockkit_is_not_a_mention(cfg, store):
    """rich_text_quote subtrees are quoted/forwarded content — ignored."""
    blocks = [
        {
            "type": "rich_text",
            "elements": [
                {
                    "type": "rich_text_quote",
                    "elements": [{"type": "user", "user_id": BOT_ID}],
                }
            ],
        }
    ]
    ev = make_event(text="look at this old message", blocks=blocks)
    assert decide(ev, cfg, store) is Decision.RECORD_SKIP


def test_mention_marks_thread_engaged_so_followups_pass(cfg, store):
    root = "1754900000.000100"
    assert (
        decide(make_event(text=f"<@{BOT_ID}> help here", ts=root), cfg, store)
        is Decision.RECORD_PASS
    )
    m2 = make_event(text="also it affects prod", ts="1754900060.000200", thread_ts=root)
    assert decide(m2, cfg, store) is Decision.RECORD_PASS


def test_unmentioned_new_thread_is_not_engaged(cfg, store):
    assert (
        decide(make_event(text="deploy is stuck", ts="1754900000.000100"), cfg, store)
        is Decision.RECORD_SKIP
    )
    m2 = make_event(
        text="yeah seeing it too", ts="1754900060.000200", thread_ts="1754900000.000100"
    )
    assert decide(m2, cfg, store) is Decision.RECORD_SKIP


def test_reply_to_nudged_thread_passes_and_records_engagement(cfg, store):
    """After we nudge a thread, a human reply must reach the agent and mark
    the intervention engaged — that feedback is what resets self-quiet, which
    is now one of only three noise controls left."""
    root = "1754900000.000100"
    decide(make_event(text="who owns the runbook?", ts=root), cfg, store)
    store.record_intervention(WATCHED, root, kind="unanswered_question", now=1754903000.0)

    reply = make_event(text="oh good point, it's mine", ts="1754903100.000200", thread_ts=root)
    assert decide(reply, cfg, store) is Decision.RECORD_PASS
    assert store.channel_self_quieted(WATCHED, threshold=1) is False  # engaged
    assert store.is_engaged(WATCHED, root) is True


def test_bot_authored_message_is_recorded_as_bot_and_skipped(cfg, store):
    ev = make_event(text="build 4123 failed", bot_id="B0CIBOT001", user=None)
    assert decide(ev, cfg, store) is Decision.RECORD_SKIP
    assert store.messages_in_channel(WATCHED)[0]["is_bot"] == 1


def test_own_bot_user_message_never_marks_engagement(cfg, store):
    assert decide(make_event(text="nudge text", user=BOT_ID), cfg, store) is Decision.RECORD_SKIP
    m2 = make_event(
        text="reply to bot", ts="1754900060.000200", thread_ts="1754900000.000100"
    )
    assert decide(m2, cfg, store) is Decision.RECORD_SKIP


def test_store_failure_fails_safe_not_open(cfg, store, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(store, "record_message", boom)
    plain = make_event(text="no mention here")
    mention = make_event(text=f"hey <@{BOT_ID}> status?", ts="1754900099.000300")
    assert decide(plain, cfg, store) is Decision.RECORD_SKIP
    assert decide(mention, cfg, store) is Decision.RECORD_PASS
