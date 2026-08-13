#!/usr/bin/env python3
"""Judgment-quality eval. The thing 394 unit tests do not measure.

WHY THIS EXISTS. The unit suite tests MECHANICS — does the gate fail closed,
does the ladder gate, does the window truncate. It cannot tell you whether the
judge has good taste, and taste IS the product: a bot that answers resolved
threads is worse than no bot.

Every quality bug in this project so far was found by a human reading one
`reason` string in production. `gpt-5.4-mini` shipped, answered nothing useful
for an hour, and was caught by luck. This file is how that stops being the
detector: one command, real model, labelled cases, a score, and a diff between
two models.

WHAT IT IS NOT. Not a pass/fail gate on absolute score — judgment is not
deterministic and a single run is noisy. It is a REGRESSION detector. The
signal is the delta against a saved baseline, and the critical cases, which
must not regress at all.

Usage
-----
    python aw_eval.py                        # run against the configured judge
    python aw_eval.py --model gpt-5.4-mini   # try a model before pinning it
    python aw_eval.py --compare gpt-5.6-sol gpt-5.4-mini
    python aw_eval.py --save baseline.json   # record a baseline
    python aw_eval.py --against baseline.json  # diff against one
    python aw_eval.py --no-context           # measure what context is worth
    python aw_eval.py --dry-run              # print prompts, spend nothing

Cost: one batched call per case (~$0.004 modelled), so a full 15-case run is
about six cents of modelled usage. Cheap enough to run before every model change
and far cheaper than finding out in a live channel.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent
PLUGIN = REPO / "ambient-watch"
if str(PLUGIN) not in sys.path:
    sys.path.insert(0, str(PLUGIN))

CASES = REPO / "evals" / "cases.json"


@dataclass
class Outcome:
    case_id: str
    expect: str
    got: str
    confidence: float
    reason: str
    nudge: str
    weight: str = "normal"
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.got == self.expect

    @property
    def critical(self) -> bool:
        return self.weight == "critical"


@dataclass
class Report:
    model: str = ""
    outcomes: list = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0

    @property
    def passed(self):
        return [o for o in self.outcomes if o.ok]

    @property
    def failed(self):
        return [o for o in self.outcomes if not o.ok]

    @property
    def critical_failures(self):
        return [o for o in self.failed if o.critical]

    @property
    def score(self) -> float:
        return len(self.passed) / len(self.outcomes) if self.outcomes else 0.0

    def confusion(self):
        """(false_silence, false_speech) — the two failures, named by their cost.

        Separated because they are NOT symmetric. False speech posts into a
        thread that did not want us and spends the only post that thread will
        ever get. False silence is invisible and merely unhelpful. Anthropic
        report false negatives as Claude Tag's own dominant failure mode, so a
        run can look fine on score while drifting toward useless.
        """
        false_silence = len([o for o in self.failed if o.expect == "post"])
        false_speech = len([o for o in self.failed if o.expect == "skip"])
        return false_silence, false_speech


def _candidate(case, cfg):
    """A real Candidate, built through the real sanitizer.

    Deliberately NOT a hand-written prompt: the eval has to exercise the same
    build_judge_view / neutralize path production uses, or it would be measuring
    a prompt nobody ships.
    """
    import aw_sanitize
    from aw_detectors import Candidate

    base = 1754900000.0
    rows = [
        {
            "ts": f"{base + i * 60:.6f}",
            "author": m.get("author", f"U{i}"),
            "is_bot": int(m.get("is_bot", 0)),
            "is_mention": 0,
            "text": m.get("text", ""),
        }
        for i, m in enumerate(case["messages"])
    ]
    root = rows[0]["ts"]
    cand = Candidate(
        channel="C0EVALCHAN",
        thread_ts=root,
        kind="unanswered_question",
        target=f"C0EVALCHAN:{root}",
        # build_excerpt takes TEXTS; build_judge_view takes ROWS. Different
        # shapes on purpose — the excerpt is the compact operator-review record,
        # the view is what the model reads.
        excerpt=aw_sanitize.build_excerpt([r["text"] for r in rows]),
        judge_view=aw_sanitize.build_judge_view(
            rows, aw_sanitize.CTX_THREAD_MESSAGES, aw_sanitize.CTX_THREAD_VIEW_CHARS
        ),
        human_participants=len({r["author"] for r in rows if not r["is_bot"]}),
        idle_minutes=90,
        last_activity=float(rows[-1]["ts"]),
        messages=rows,
    )
    # Channel identity is the highest-value-per-byte context and the only part
    # some cases turn on (see channel-topic-should-suppress), so build it in the
    # same labelled, sealed shape aw_context produces.
    if getattr(cfg, "context_enabled", False):
        name = case.get("channel_name", "")
        topic = case.get("channel_topic", "")
        if name or topic:
            body = aw_sanitize.neutralize(f"#{name} — {topic}", 200)
            cand.context_block = (
                f"{aw_sanitize.DELIM_OPEN}\n[CHANNEL] {body}\n{aw_sanitize.DELIM_CLOSE}"
            )
    return cand


def _cfg(model=None, provider=None, context=True):
    from aw_config import AmbientConfig

    cfg = AmbientConfig(bot_user_id="U0EVALBOT", channels={"C0EVALCHAN"}, mode="shadow")
    cfg.context_enabled = context
    if model:
        cfg.judge_model = model
    if provider:
        cfg.judge_provider = provider
    return cfg


def run(cases, model=None, provider=None, context=True, dry_run=False) -> Report:
    import aw_judge

    cfg = _cfg(model, provider, context)
    rep = Report(model=model or "(configured)")
    started = time.time()

    for case in cases:
        cand = _candidate(case, cfg)
        if dry_run:
            print(f"\n===== {case['id']} (expect {case['expect']}) =====")
            for m in aw_judge.build_messages([cand]):
                print(f"--- {m.get('role')} ---\n{m.get('content')}")
            continue
        # One case per call: batching would let one thread's text influence
        # another's verdict, which production never does (at most one nominee
        # per channel per sweep) and which would make a failure unattributable.
        result = aw_judge.judge([cand], cfg)
        rep.prompt_tokens += result.prompt_tokens
        rep.completion_tokens += result.completion_tokens
        if result.model:
            rep.model = result.model
        v = result.verdicts[0] if result.verdicts else None
        rep.outcomes.append(
            Outcome(
                case_id=case["id"],
                expect=case["expect"],
                got=("post" if (v and v.should_post) else "skip"),
                confidence=(v.confidence if v else 0.0),
                reason=(v.reason if v else result.error or "no verdict"),
                nudge=(v.nudge if v else ""),
                weight=case.get("weight", "normal"),
                error=result.error,
            )
        )
        print(
            f"  {'PASS' if rep.outcomes[-1].ok else 'FAIL'} "
            f"{case['id']:<38} expect={case['expect']:<4} got={rep.outcomes[-1].got:<4} "
            f"conf={rep.outcomes[-1].confidence:.2f}"
        )
    rep.seconds = time.time() - started
    return rep


def show(rep: Report, usd_per_mtok=(5.0, 15.0)):
    fs, fsp = rep.confusion()
    cost = (rep.prompt_tokens / 1e6) * usd_per_mtok[0] + (
        rep.completion_tokens / 1e6
    ) * usd_per_mtok[1]
    print("\n" + "=" * 72)
    print(f"MODEL {rep.model}    score {len(rep.passed)}/{len(rep.outcomes)} "
          f"({rep.score:.0%})    {rep.seconds:.0f}s    ~${cost:.4f} modelled")
    print(f"  false silence (expected post, stayed quiet) : {fs}   <- unhelpful")
    print(f"  false speech  (expected skip, posted)       : {fsp}   <- annoying, spends the one post")
    if rep.critical_failures:
        print(f"  !! CRITICAL failures: {[o.case_id for o in rep.critical_failures]}")
    for o in rep.failed:
        print(f"\n  FAIL {o.case_id}  (expected {o.expect}, got {o.got} @ {o.confidence:.2f})")
        print(f"       reason: {o.reason}")
        if o.nudge:
            print(f"       nudge : {o.nudge}")
    print("=" * 72)


def compare(a: Report, b: Report):
    print("\n" + "=" * 72)
    print(f"COMPARE   A={a.model}   B={b.model}")
    print(f"  score   A {a.score:.0%}   B {b.score:.0%}")
    by_id = {o.case_id: o for o in b.outcomes}
    moved = False
    for oa in a.outcomes:
        ob = by_id.get(oa.case_id)
        if ob and oa.got != ob.got:
            moved = True
            worse = ob.expect != ob.got
            print(f"  {'WORSE' if worse else 'BETTER'} {oa.case_id}: "
                  f"{oa.got} -> {ob.got}  (expected {oa.expect})")
            print(f"        A: {oa.reason}")
            print(f"        B: {ob.reason}")
    if not moved:
        print("  no verdict changed — the models agree on every case")
    print("=" * 72)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--model")
    ap.add_argument("--provider")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"))
    ap.add_argument("--no-context", action="store_true",
                    help="run without channel identity, to measure what context is worth")
    ap.add_argument("--dry-run", action="store_true", help="print prompts, spend nothing")
    ap.add_argument("--only", help="substring filter on case id")
    ap.add_argument("--save", metavar="PATH")
    ap.add_argument("--against", metavar="PATH")
    a = ap.parse_args(argv)

    data = json.loads(CASES.read_text(encoding="utf-8"))
    cases = data["cases"]
    if a.only:
        cases = [c for c in cases if a.only in c["id"]]
    if not cases:
        print("no cases matched")
        return 2
    context = not a.no_context

    if a.compare:
        ra = run(cases, model=a.compare[0], provider=a.provider, context=context)
        show(ra)
        rb = run(cases, model=a.compare[1], provider=a.provider, context=context)
        show(rb)
        compare(ra, rb)
        return 1 if rb.critical_failures else 0

    rep = run(cases, model=a.model, provider=a.provider, context=context,
              dry_run=a.dry_run)
    if a.dry_run:
        return 0
    show(rep)

    if a.save:
        Path(a.save).write_text(json.dumps({
            "model": rep.model, "score": rep.score,
            "outcomes": {o.case_id: {"got": o.got, "confidence": o.confidence,
                                     "reason": o.reason} for o in rep.outcomes},
        }, indent=2), encoding="utf-8")
        print(f"baseline saved -> {a.save}")

    if a.against:
        old = json.loads(Path(a.against).read_text(encoding="utf-8"))
        print(f"\nAGAINST {a.against} (model {old.get('model')}, score {old.get('score', 0):.0%})")
        regressed = []
        for o in rep.outcomes:
            prev = (old.get("outcomes") or {}).get(o.case_id)
            if prev and prev["got"] != o.got:
                tag = "REGRESSED" if not o.ok else "improved"
                if not o.ok:
                    regressed.append(o.case_id)
                print(f"  {tag} {o.case_id}: {prev['got']} -> {o.got}")
        if not regressed:
            print("  no regressions")
        else:
            print(f"  !! {len(regressed)} regression(s): {regressed}")
            return 1

    # Exit non-zero only on a critical failure. A soft score dip is noise; a
    # critical case flipping is a product change and should fail a pipeline.
    return 1 if rep.critical_failures else 0


if __name__ == "__main__":
    sys.exit(main())
