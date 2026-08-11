"""In-channel mute control — Claude Tag parity for noise suppression.

Claude Tag lets anyone in a channel quiet the bot in-thread (!mute) without
admin access. We only had a CLI kill switch, which means a teammate annoyed
by a nudge cannot stop it without shell access on someone else's machine.

Implementation uses the pre_gateway_dispatch "rewrite" action (verified in
hermes_cli/plugins.py VALID_HOOKS docs): the plugin applies the mute itself
and rewrites the message so the agent confirms it to the human, instead of
swallowing the command silently.
"""

from conftest import BOT_ID, WATCHED, make_event

from aw_recorder import Decision, decide

ROOT = "1754900000.000100"


def _thread_reply(text, ts="1754900500.000200"):
    return make_event(text=text, ts=ts, thread_ts=ROOT)


def test_mute_command_in_thread_mutes_that_thread(cfg, store):
    decide(make_event(text="why is staging down?", ts=ROOT), cfg, store)
    verdict = decide(_thread_reply("hermes ambient mute"), cfg, store)
    assert store.is_muted(WATCHED, ROOT) is True
    assert isinstance(verdict, tuple), "mute must rewrite so the human gets a reply"
    action, payload = verdict
    assert action is Decision.RECORD_REWRITE
    assert "muted" in payload.lower()


def test_bang_prefix_form_also_works(cfg, store):
    decide(make_event(text="why is staging down?", ts=ROOT), cfg, store)
    decide(_thread_reply("!ambient mute"), cfg, store)
    assert store.is_muted(WATCHED, ROOT) is True


def test_mention_prefixed_form_also_works(cfg, store):
    decide(make_event(text="why is staging down?", ts=ROOT), cfg, store)
    decide(_thread_reply(f"<@{BOT_ID}> ambient mute"), cfg, store)
    assert store.is_muted(WATCHED, ROOT) is True


def test_unmute_reverses_it(cfg, store):
    decide(make_event(text="why is staging down?", ts=ROOT), cfg, store)
    decide(_thread_reply("hermes ambient mute"), cfg, store)
    decide(_thread_reply("hermes ambient unmute", ts="1754900600.000300"), cfg, store)
    assert store.is_muted(WATCHED, ROOT) is False


def test_mute_at_top_level_mutes_the_whole_channel(cfg, store):
    verdict = decide(make_event(text="hermes ambient mute", ts=ROOT), cfg, store)
    assert store.is_channel_muted(WATCHED) is True
    action, payload = verdict
    assert action is Decision.RECORD_REWRITE
    assert "channel" in payload.lower()


def test_muted_channel_yields_no_candidates(cfg, store):
    from aw_detectors import find_candidates

    decide(make_event(text="why is staging down?", ts=ROOT), cfg, store)
    decide(make_event(text="hermes ambient mute", ts="1754900900.000900"), cfg, store)
    assert find_candidates(store, cfg, now=1754900000.0 + 46 * 60) == []


def test_ordinary_message_containing_the_word_mute_is_not_a_command(cfg, store):
    """'we should mute the alerts' must not silence anything."""
    decide(make_event(text="why is staging down?", ts=ROOT), cfg, store)
    verdict = decide(_thread_reply("we should mute the noisy alert channel"), cfg, store)
    assert verdict is Decision.RECORD_SKIP
    assert store.is_muted(WATCHED, ROOT) is False


def test_mute_command_in_unwatched_channel_is_ignored(cfg, store):
    ev = make_event(text="hermes ambient mute", channel="C0ELSEWHER")
    assert decide(ev, cfg, store) is Decision.PASS
    assert store.is_channel_muted("C0ELSEWHER") is False
