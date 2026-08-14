"""Standing instructions — a per-channel steer for the judge.

Copied from Claude Tag's 2026-08-13 update: the judge uses "the standing
instructions you have given it" to decide when to contribute. e.g. "only weigh
in on deploy and infra questions here" or "never answer HR questions".

THE ONE DIVERGENCE THAT MATTERS. Claude Tag lets any channel member set these
conversationally. We cannot: with SLACK_ALLOW_ALL_USERS=true, "any member" means
anyone can rewrite the judge's guidance — attacker-writable text steering the
decision, the exact category the whole sanitizer keeps OUT of the trusted prompt.
So v1 is OPERATOR-SET IN CONFIG ONLY, and the instruction is TRUSTED prompt text
(the operator wrote it), placed OUTSIDE the untrusted-slack-text delimiters,
never confusable with channel content.

It must also preserve the dark-deploy guarantee: a config with no instructions
produces a byte-identical prompt, exactly like context_block does.
"""

import hashlib

from conftest import WATCHED, make_event

from aw_detectors import Candidate
from aw_recorder import decide
import aw_judge
import aw_sanitize

T0 = 1754900000.0


def _cand(cfg, store, text="can someone approve my PR?"):
    decide(make_event(text=text, ts=f"{T0:.6f}"), cfg, store)
    msgs = store.thread_messages(WATCHED, f"{T0:.6f}")
    return Candidate(
        channel=WATCHED, thread_ts=f"{T0:.6f}", kind="unanswered_question",
        target="x", excerpt=aw_sanitize.build_excerpt(m["text"] for m in msgs),
        judge_view=aw_sanitize.build_judge_view(msgs),
        human_participants=1, idle_minutes=50, last_activity=T0, messages=msgs,
    )


def _prompt(cfg, cand):
    return "\n".join(m["content"] for m in aw_judge.build_messages([cand], cfg))


def test_no_instructions_is_byte_identical_to_no_cfg(cfg, store):
    """Dark-deploy guarantee: a config without standing instructions must not
    change the prompt by one byte."""
    cand = _cand(cfg, store)
    with_cfg = _prompt(cfg, cand)
    without_cfg = "\n".join(m["content"] for m in aw_judge.build_messages([cand]))
    assert with_cfg == without_cfg


def test_a_channel_instruction_appears_in_the_prompt(cfg, store):
    cfg.standing_instructions = {WATCHED: "Only weigh in on deploy and infra questions."}
    cand = _cand(cfg, store)
    p = _prompt(cfg, cand)
    assert "Only weigh in on deploy and infra questions." in p


def test_the_instruction_is_trusted_and_sits_outside_the_untrusted_delimiters(cfg, store):
    """The operator wrote it, so it is guidance, not channel data. It must NOT
    be wrapped in <untrusted-slack-text>, or the judge would be told to ignore
    the very steer it is meant to follow."""
    cfg.standing_instructions = {WATCHED: "Never answer HR or salary questions."}
    cand = _cand(cfg, store)
    p = _prompt(cfg, cand)
    instr_at = p.index("Never answer HR or salary questions.")
    # No untrusted delimiter opens between the labelled instruction and the text.
    window = p[max(0, instr_at - 200):instr_at]
    assert aw_sanitize.DELIM_OPEN not in window.split("STANDING INSTRUCTIONS")[-1]


def test_an_instruction_for_a_different_channel_is_not_applied(cfg, store):
    cfg.standing_instructions = {"C0OTHER": "only respond to billing questions"}
    cand = _cand(cfg, store)
    assert "billing" not in _prompt(cfg, cand)


def test_the_instruction_is_length_capped(cfg, store):
    cap = aw_sanitize.STANDING_INSTRUCTION_CHARS
    cfg.standing_instructions = {WATCHED: "x" * 5000}
    cand = _cand(cfg, store)
    p = _prompt(cfg, cand)
    # The RUN length is the contract. Counting every "x" in the prompt would
    # also count the ones in JUDGE_RULES ("expensive", "text") — an earlier
    # version of this test did, and failed at 413 against a correct 400 cap.
    assert "x" * cap in p
    assert "x" * (cap + 1) not in p


def test_a_non_string_instruction_is_ignored_not_crashed(cfg, store):
    cfg.standing_instructions = {WATCHED: ["not", "a", "string"]}
    cand = _cand(cfg, store)
    # must not raise; behaves as if unset
    _prompt(cfg, cand)


def test_changing_the_instruction_changes_the_prompt_hash(cfg, store):
    cand = _cand(cfg, store)
    cfg.standing_instructions = {WATCHED: "A"}
    a = hashlib.sha256(_prompt(cfg, cand).encode()).hexdigest()
    cfg.standing_instructions = {WATCHED: "B"}
    b = hashlib.sha256(_prompt(cfg, cand).encode()).hexdigest()
    assert a != b
