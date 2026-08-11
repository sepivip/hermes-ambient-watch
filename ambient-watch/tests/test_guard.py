"""Tool guard: pre_tool_call target pinning for ambient compose sessions.

Contract (verified v0.20.0): pre_tool_call receives tool_name + args and
a returned {"action": "block", "message": "..."} blocks the call, with
the message becoming the tool result.

Guard policy (design doc Part 4/7): while armed intents exist, any
send_message aimed INTO a watched channel must exactly match an armed
intent target. Traffic to unwatched channels is not ambient's business.
Ops-channel delivery (shadow digests) is always permitted.
"""

from conftest import WATCHED, make_event

from aw_guard import check_tool_call
from aw_recorder import decide
from gate import run_gate

T0 = 1754900000.0


def _arm(cfg, store):
    ev = make_event(text="who owns the migration runbook?", ts=f"{T0:.6f}")
    decide(ev, cfg, store)
    run_gate(cfg, store, now=T0 + 46 * 60)
    return f"{WATCHED}:{T0:.6f}"


def test_matching_intent_target_is_allowed(cfg, store):
    target = _arm(cfg, store)
    assert check_tool_call("send_message", {"target": target}, cfg, store) is None


def test_wrong_thread_in_watched_channel_is_blocked(cfg, store):
    _arm(cfg, store)
    verdict = check_tool_call(
        "send_message", {"target": f"{WATCHED}:1754999999.000001"}, cfg, store
    )
    assert verdict["action"] == "block"
    assert "not an armed ambient intent" in verdict["message"]


def test_toplevel_post_to_watched_channel_is_blocked_when_armed(cfg, store):
    _arm(cfg, store)
    verdict = check_tool_call("send_message", {"target": WATCHED}, cfg, store)
    assert verdict["action"] == "block"


def test_unwatched_channel_is_not_our_business(cfg, store):
    _arm(cfg, store)
    assert check_tool_call("send_message", {"target": "C0ELSEWHER"}, cfg, store) is None


def test_ops_channel_delivery_always_allowed(cfg, store):
    _arm(cfg, store)
    assert (
        check_tool_call("send_message", {"target": cfg.ops_channel}, cfg, store) is None
    )


def test_other_tools_are_ignored_by_guard(cfg, store):
    _arm(cfg, store)
    assert check_tool_call("web_search", {"query": "x"}, cfg, store) is None


def test_no_armed_intents_means_guard_is_dormant(cfg, store):
    assert (
        check_tool_call("send_message", {"target": f"{WATCHED}:{T0:.6f}"}, cfg, store)
        is None
    )


def test_fulfilled_intent_disarms_target(cfg, store):
    target = _arm(cfg, store)
    store.mark_intent_done(target)
    verdict = check_tool_call("send_message", {"target": target}, cfg, store)
    assert verdict["action"] == "block"
