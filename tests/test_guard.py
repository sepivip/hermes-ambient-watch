"""Tool guard tests — only the data-directory jail is left.

WHAT THIS FILE USED TO TEST, AND WHY IT IS GONE. Ten tests here exercised
``send_message`` target pinning: while an ambient "intent" was armed, a send
into a watched channel had to match the nominated thread. The control could
never fire — ``send_message`` is absent from Hermes' core agent toolset and
``cron/scheduler.py:182`` hardcodes ``messaging`` into every cron session's
disabled toolsets — so those tests proved a no-op worked. Worse, they made a
dead control look load-bearing in review, which is precisely how the
2026-08-11 containment incident got past thirty adversarial reviewers.

The real outbound invariant moved into ``aw_post.post_nudge`` (the actual
send path) and is tested in ``test_post.py`` and ``test_live_delivery.py``.
What remains here is the jail, which does fire, on every tool call, in every
session. Its exhaustive coverage lives in ``test_containment.py`` (L3); this
file keeps the entry-point contract.
"""

from conftest import WATCHED

from aw_guard import check_tool_call


def test_check_tool_call_is_the_jail(cfg, store):
    verdict = check_tool_call(
        "read_file", {"path": str(cfg.data_dir / "ambient.db")}, cfg, store
    )
    assert verdict is not None and verdict["action"] == "block"
    assert "ambient-watch" in verdict["message"]


def test_unrelated_calls_pass(cfg, store):
    assert check_tool_call("web_search", {"query": "x"}, cfg, store) is None
    assert check_tool_call("terminal", {"command": "git status"}, cfg, store) is None


def test_send_message_is_no_longer_special_cased(cfg, store):
    """A cron agent cannot call it at all, so the guard must not pretend to
    police it — and must not block a legitimate send either."""
    assert check_tool_call(
        "send_message", {"target": f"slack:{WATCHED}:1754900000.000100"}, cfg, store
    ) is None
    assert check_tool_call(
        "send_message", {"target": "slack:#some-channel"}, cfg, store
    ) is None


def test_the_jail_still_covers_send_message_arguments(cfg, store):
    """…but the jail has no tool allowlist, so a data-dir reference smuggled
    through send_message's arguments is still blocked."""
    verdict = check_tool_call(
        "send_message",
        {"target": "slack:C0ELSEWHER", "message": f"look: {cfg.data_dir}/ambient.db"},
        cfg,
        store,
    )
    assert verdict is not None and verdict["action"] == "block"


def test_store_is_optional(cfg):
    """The guard needs no ledger now; nothing may depend on one being passed."""
    assert check_tool_call("read_file", {"path": "README.md"}, cfg) is None
    assert check_tool_call("read_file", {"path": "ambient.db"}, cfg) is not None
