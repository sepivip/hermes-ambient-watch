"""Tool guard tests against the REAL send_message target grammar.

Adversarial review finding (critical): real deliverable targets are
"slack:C…:<thread_ts>" — platform prefix FIRST. Bare "C…:ts" is not a
valid tool target at all (Platform lookup rejects it). The guard must
strip the platform segment before comparing, and armed intents are
stored as bare refs "C…:<thread_ts>".
"""

from conftest import WATCHED, make_event

from aw_guard import check_tool_call
from aw_recorder import decide
from gate import run_gate

T0 = 1754900000.0


def _arm(cfg, store):
    cfg.mode = "live"  # shadow mode arms nothing
    ev = make_event(text="who owns the migration runbook?", ts=f"{T0:.6f}")
    decide(ev, cfg, store)
    run_gate(cfg, store, now=T0 + 46 * 60)
    return f"{WATCHED}:{T0:.6f}"  # bare ref as stored in intents


def test_matching_intent_with_platform_prefix_is_allowed(cfg, store):
    ref = _arm(cfg, store)
    assert check_tool_call("send_message", {"target": f"slack:{ref}"}, cfg, store) is None


def test_wrong_thread_in_watched_channel_is_blocked(cfg, store):
    _arm(cfg, store)
    verdict = check_tool_call(
        "send_message", {"target": f"slack:{WATCHED}:1754999999.000001"}, cfg, store
    )
    assert verdict["action"] == "block"
    assert "not an armed ambient intent" in verdict["message"]


def test_toplevel_post_to_watched_channel_is_blocked_when_armed(cfg, store):
    _arm(cfg, store)
    verdict = check_tool_call("send_message", {"target": f"slack:{WATCHED}"}, cfg, store)
    assert verdict["action"] == "block"


def test_name_form_slack_target_is_blocked_when_armed(cfg, store):
    """'slack:#channel-name' bypasses ID comparison after directory
    resolution — fail closed while armed."""
    _arm(cfg, store)
    verdict = check_tool_call(
        "send_message", {"target": "slack:#watched-channel"}, cfg, store
    )
    assert verdict["action"] == "block"


def test_unwatched_channel_is_not_our_business(cfg, store):
    _arm(cfg, store)
    assert (
        check_tool_call("send_message", {"target": "slack:C0ELSEWHER"}, cfg, store)
        is None
    )


def test_other_platforms_are_not_our_business(cfg, store):
    _arm(cfg, store)
    assert (
        check_tool_call("send_message", {"target": "telegram:12345"}, cfg, store) is None
    )


def test_ops_channel_delivery_always_allowed(cfg, store):
    _arm(cfg, store)
    assert (
        check_tool_call(
            "send_message", {"target": f"slack:{cfg.ops_channel}"}, cfg, store
        )
        is None
    )


def test_other_tools_are_ignored_by_guard(cfg, store):
    _arm(cfg, store)
    assert check_tool_call("web_search", {"query": "x"}, cfg, store) is None


def test_no_intents_ever_means_guard_is_dormant(cfg, store):
    assert (
        check_tool_call(
            "send_message", {"target": f"slack:{WATCHED}:{T0:.6f}"}, cfg, store
        )
        is None
    )


def test_fulfilled_intent_disarms_target(cfg, store):
    ref = _arm(cfg, store)
    store.mark_intent_done(ref)
    verdict = check_tool_call("send_message", {"target": f"slack:{ref}"}, cfg, store)
    assert verdict["action"] == "block"
