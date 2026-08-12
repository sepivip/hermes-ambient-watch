"""The judge's thread window: root + oldest few + newest many.

WHY NOT "root + newest", which is what shipped first. That was justified with a
claim that turned out to be FALSE: that Claude Tag is always tagged and so
answers "what is this thread about", while we are never tagged and answer "has
this already resolved". Anthropic's own docs contradict it —

    "Claude replies without an @-mention in DMs, in any thread it's already
    part of, and to channel messages it judges warrant a reply. It's an ambient
    presence in the channel, and the @-mention is how you guarantee a response,
    not a requirement for one."

Claude Tag answers unprompted too. It faces our question, and it still reads
oldest-first. So there is no "different question" to justify dropping the start
of a thread.

WHY NOT "root + oldest 50" verbatim either. The 50-message oldest-first window
is documented for the *mid-thread mention* case specifically; the docs do not
state what the unprompted path reads, and Claude Tag's ambient path also reads
channel history and searches the workspace, so its total context is broader than
any thread window. There is no documented ambient rule to copy.

WHAT DOES justify keeping the tail is a property of OUR design, not of a
different question: we get exactly ONE post per thread, ever. A late "never
mind, found it" is the single most expensive thing to miss, because missing it
spends the only post we will ever have on a thread that is already done.

So: the root (what this is about) + the oldest couple (how it developed) + as
much of the tail as the budget allows (whether it is still open). Both ends,
because both carry signal.
"""

from aw_sanitize import CTX_THREAD_MESSAGES, build_judge_view


def _rows(n, text=lambda i: f"message number {i:03d}"):
    return [
        {"ts": f"{1754900000 + i:.6f}", "author": f"U{i%3}", "is_bot": 0, "text": text(i)}
        for i in range(n)
    ]


def _visible(view, n):
    """Which of the n messages survived into the view.

    Zero-padded, for two reasons found the hard way. Unpadded, "message number
    4" is a substring of "message number 45", so the helper reported phantom
    survivors and failed a correct implementation. And a bracketed marker like
    "message[4]" does not survive at all — ``_clean`` strips ``[]`` as
    container-forging characters, which is L1 working exactly as intended.
    """
    return [i for i in range(n) if f"message number {i:03d}" in view]


def test_a_short_thread_is_shown_whole():
    n = CTX_THREAD_MESSAGES - 2
    assert _visible(build_judge_view(_rows(n), CTX_THREAD_MESSAGES, 100_000), n) == list(range(n))


def test_the_root_always_survives_truncation():
    n = 60
    assert 0 in _visible(build_judge_view(_rows(n), CTX_THREAD_MESSAGES, 100_000), n)


def test_the_newest_message_always_survives_truncation():
    """The resolution signal ('never mind, solved it') is the last message."""
    n = 60
    assert n - 1 in _visible(build_judge_view(_rows(n), CTX_THREAD_MESSAGES, 100_000), n)


def test_the_oldest_replies_survive_too_not_just_the_root():
    """This is the part 'root + newest' dropped: how the thread developed."""
    n = 60
    seen = _visible(build_judge_view(_rows(n), CTX_THREAD_MESSAGES, 100_000), n)
    assert 1 in seen and 2 in seen, f"expected the oldest replies, got {seen[:6]}"


def test_the_window_takes_both_ends_and_nothing_from_the_middle():
    n = 60
    seen = _visible(build_judge_view(_rows(n), CTX_THREAD_MESSAGES, 100_000), n)
    assert len(seen) <= CTX_THREAD_MESSAGES
    head, tail = [i for i in seen if i <= 2], [i for i in seen if i > 2]
    assert head == [0, 1, 2], f"head should be root + oldest 2, got {head}"
    assert tail == list(range(n - len(tail), n)), f"tail should be contiguous newest, got {tail}"


def test_the_tail_is_larger_than_the_head():
    """Recency carries more of the decision, so it gets more of the budget."""
    n = 60
    seen = _visible(build_judge_view(_rows(n), CTX_THREAD_MESSAGES, 100_000), n)
    assert len([i for i in seen if i > 2]) > 3


def test_truncation_is_marked_so_the_judge_knows_it_is_partial():
    """Otherwise a gap reads as 'nobody said anything', which is a different
    thing from 'we did not show you what they said'."""
    view = build_judge_view(_rows(60), CTX_THREAD_MESSAGES, 100_000)
    assert "omitted" in view.lower() or "…" in view or "..." in view
