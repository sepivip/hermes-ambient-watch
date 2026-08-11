"""A call we cannot measure must still cost money.

THE HOLE THIS CLOSES. ``call_llm`` returns nothing at all when the provider
times out, resets the connection, or 429s after reading the prompt — but the
prompt was sent and billed. The gate meters only what the judge reports, and a
failed sweep also writes no re-judge watermark, so the same nominees were
re-sent on the very next tick: at the testing cadence of 2 minutes that is 720
billed calls a day against a ledger that stayed at $0.00, which means every
cap stayed untripped forever. "Fail closed" covered the posting decision and
missed the spending one.

The fix is pessimism: when a call produces no usable answer AND no usage
figure, charge an estimate built from the prompt we actually sent. A provider
outage then trips the spend cap like any other spend, which is the whole point
of having one.
"""

from conftest import WATCHED, make_event

import aw_judge
from aw_budget import Budget
from aw_recorder import decide
from gate import run_gate

T0 = 1754900000.0
NOW = T0 + 46 * 60


class Nominee:
    channel = WATCHED
    thread_ts = f"{T0:.6f}"
    kind = "unanswered_question"
    judge_view = "<untrusted-slack-text>\nA1: who owns the runbook?\n</untrusted-slack-text>"
    human_participants = 1
    idle_minutes = 46
    last_activity = T0
    excerpt = judge_view


def _reply(text, usage=None, model="judge-test-model"):
    body = {"choices": [{"message": {"content": text}}], "model": model}
    if usage is not None:
        body["usage"] = usage
    return body


GOOD_JSON = (
    '{"verdicts": [{"id": "n1", "should_post": true, "confidence": 0.9,'
    ' "reason": "blocked on an owner", "nudge": "I can dig up who owns that."}]}'
)


def test_a_provider_failure_is_charged_an_estimate_not_zero(cfg):
    def boom(messages, _cfg):
        raise TimeoutError("read timed out")

    result = aw_judge.judge([Nominee()], cfg, llm=boom)
    assert result.verdicts == [], "a failure must never produce a post"
    assert result.error.startswith("TimeoutError")
    assert result.estimated is True
    assert result.prompt_tokens > 0, "a billed prompt was metered as free"


def test_the_estimate_tracks_the_prompt_we_actually_sent(cfg):
    one = aw_judge.estimate_prompt_tokens([Nominee()])
    three = aw_judge.estimate_prompt_tokens([Nominee(), Nominee(), Nominee()])
    assert three > one > 0, "more nominees must estimate more tokens"
    # Sanity-bounded by the sanitizer's judge profile, not by channel volume.
    assert three < 4000


def test_a_reply_with_no_usage_and_no_verdicts_is_charged(cfg):
    """A 200 that omits usage and says nothing usable is still a paid call."""
    result = aw_judge.judge(
        [Nominee()], cfg, llm=lambda m, c: _reply("I'm sorry, I can't help.")
    )
    assert result.verdicts == []
    assert result.error
    assert result.estimated is True and result.prompt_tokens > 0


def test_a_usable_reply_is_never_charged_an_estimate(cfg):
    """No phantom cost on the happy path: provider figures win outright."""
    result = aw_judge.judge(
        [Nominee()], cfg,
        llm=lambda m, c: _reply(
            GOOD_JSON, usage={"prompt_tokens": 1234, "completion_tokens": 56}
        ),
    )
    assert [v.should_post for v in result.verdicts] == [True]
    assert (result.prompt_tokens, result.completion_tokens) == (1234, 56)
    assert result.estimated is False


def test_a_usable_reply_without_usage_invents_nothing(cfg):
    """The 'no phantom rows' contract (test_budget_wiring) is preserved: only
    a call that produced NOTHING usable is charged an estimate."""
    result = aw_judge.judge([Nominee()], cfg, llm=lambda m, c: _reply(GOOD_JSON))
    assert [v.should_post for v in result.verdicts] == [True]
    assert result.estimated is False
    assert (result.prompt_tokens, result.completion_tokens) == (0, 0)


def test_a_provider_outage_trips_the_spend_cap_instead_of_looping_forever(cfg, store):
    """THE bound. A dead provider re-nominates the same thread every tick
    (nothing is judged, so no watermark is written and no decline consumes the
    thread). Money is the only thing that can stop it, so money must move."""
    cfg.daily_usd_per_channel = 0.01
    cfg.daily_usd_global = 99.0
    cfg.monthly_usd_global = 99.0
    decide(make_event(text="who owns the runbook?", ts=f"{T0:.6f}"), cfg, store)

    attempts = []

    def boom(messages, _cfg):
        attempts.append(messages)
        raise TimeoutError("read timed out")

    outs = []
    for i in range(12):
        outs.append(
            run_gate(cfg, store, now=NOW + i * 120,
                     judge_fn=lambda n, c: aw_judge.judge(n, c, llm=boom))
        )

    assert len(attempts) < 12, "an outage was retried on every single tick"
    assert Budget(store, cfg.budget_cfg()).spent_usd_global(since=0) > 0
    assert "DECLINED" in outs[-1] and "spend cap reached" in outs[-1]
    log = (cfg.data_dir / "gate_errors.log").read_text(encoding="utf-8")
    assert "ESTIMATED prompt tokens" in log, "the estimate must be auditable"
