# ambient-watch — Claude-Tag-style ambient mode for Hermes Agent

Passively watches allowlisted Slack channels, asks a **model** which threads
would actually welcome a reply, and posts one nudge into the exact thread —
under a **USD spend limit** that declines work rather than truncating it.
Shadow mode first, fail-closed by design.

Two triggers: a `--no-agent` cron **sweep** (live) and debounced **arrival-time
judging** in the gateway process (`arrival_enabled`, ships **false**). Both run
the same prefilter, the same judge and the same send path — deliberately, since
a second eligibility ladder would be the one that drifts, spends money and
posts.

Built and tested against **hermes-agent v0.20.0** (commit `c0106e5`).
Design doc: see the "Ambient Mode for Hermes Agent" artifact (Parts 4–7).
Parity audit against Anthropic's own Claude Tag spec: [PARITY.md](PARITY.md).

## How it works

Two triggers, **one** pipeline. The sweep is a **`--no-agent` cron job**:
`gate.py` *is* the job, and it does all three stages itself in one process.
Arrival-time judging (`aw_arrival.py`, **off by default**) runs the *same three
stages* inside the gateway process when a message arrives.

| Stage | Component | Cost | Agent session? |
|-------|-----------|------|----------------|
| 0 Observe | `pre_gateway_dispatch` recorder → SQLite ledger | zero | no |
| 1 Prefilter | `aw_detectors.find_candidates` — SQL + wall clock only, never "is this useful?" | zero | no |
| 2 Judge | `aw_judge` — ONE bounded LLM call via `auxiliary.ambient_watch_judge`, spend-gated by `aw_budget` | metered in USD, declined over cap | no |
| 3 Deliver | `aw_post.post_nudge` → one Slack thread | zero | no |

Its stdout is either `{"wakeAgent": false}` (silent tick, cron discards it) or
an **excerpt-free audit line** that cron's `--deliver slack:<ops>` carries to
the ops channel.

### Arrival-time judging (the trigger, `arrival_enabled`, default **false**)

Claude Tag replies to messages *as they arrive*; the sweep can only ever see a
thread that already went quiet. `aw_arrival.py` closes that, without a second
copy of anything:

```
Slack event
   │  (gateway loop thread, SYNCHRONOUS, microseconds, pure memory)
pre_gateway_dispatch → aw_recorder.decide()                    [unchanged]
   │  RECORD_SKIP → {"action":"skip"}   (exactly as today)
   └─ arrival_key(event, cfg) → (channel, root, ts) | None
      Debouncer.upsert(...)      ← coalescing map, in memory
      ensure_pump(loop)          ← ONE lazily-created task
                                     │
   (same loop, one task, serial)     ▼
   pump every arrival_pump_interval_seconds:  due = Debouncer.due(now)
      for each due thread, awaited one at a time:
        to_thread find_candidates(only=(channel,root), min_age_seconds=debounce)
        to_thread Budget.decision(channel)          → "exceeded" ⇒ DECLINE
        take one token from the channel + global bucket   (never refunded)
        await aw_judge.ajudge([cand])   → async_call_llm(task=ambient_watch_judge)
        to_thread record_usage / record_judgment
        to_thread aw_post.post_nudge(..., SlackTransport())
                                     │
   ambient.db ── read by ──▶ the cron sweep: stalled_thread trigger,
                             ops reporting for BOTH triggers, budget alerts
```

Five things about that are load-bearing:

- **The hook is synchronous.** Hermes invokes `pre_gateway_dispatch` with a
  bare `ret = cb(**kwargs)` from inside `async def _handle_message`
  (`gateway/run.py:14902-14923` → `hermes_cli/plugins.py:2126`). There is no
  await, no timeout, no thread offload, and only `dict` returns are inspected —
  so a returned coroutine is silently dropped unawaited, and *any* blocking
  work in the hook delays every inbound message on every platform. Tier A is
  therefore pure-memory (there is a test that counts SQL statements and
  requires **zero** added queries), and everything that blocks goes through
  `asyncio.to_thread` in the pump. That is not tidiness: the ledger's
  `busy_timeout=5000` means one write contended with the sweep process would
  otherwise stall the **gateway event loop** for five seconds.

  The subtle version of the same hazard is worth naming, because it is the one
  that looks too cheap to matter: Tier A still has to honour the kill switch,
  and `AmbientStore` serialises one connection behind an RLock — so even a
  *single* `SELECT` there can block for the full `busy_timeout` while a worker
  thread holds the lock. Tier A therefore reads it through
  `store.kill_switch_nowait()`, which **never waits**: on contention it keeps
  the previous cached answer (and does not extend the cache expiry, so the next
  message retries). That staleness is safe in only one direction, and it is the
  direction it can go — the pump re-reads the switch **fresh**, off-thread,
  immediately before anything can be spent, so a 3am `--kill on` still stops a
  judgment already sitting in the queue.
- **`ctx.llm.acomplete_structured` is the wrong transport.** `PluginLlm._invoke_async`
  hardcodes `async_call_llm(task=None, ...)` (`agent/plugin_llm.py:989-1002`), so
  `auxiliary.ambient_watch_judge` would never be consulted and judgment would
  silently move onto your **main chat model**. We call
  `async_call_llm(task="ambient_watch_judge", ...)` directly — same client, one
  layer down, task resolution and the per-task async semaphore intact.
- **ONE eligibility ladder.** The arrival path reaches the model only through
  `aw_detectors.find_candidates` (with `only=` and `min_age_seconds=`) and
  posts only through `aw_post.post_nudge`. Two ladders drift, and the one that
  drifts is the one that spends money and posts. The mutation test for this is
  in the *Development* section: neuter one control and **both** an arrival test
  and a sweep test fail.
- **One pump, not a task per message.** Bounded task count under a raid, at
  most one judgment in flight process-wide (so the spend rate is a property of
  the code rather than an argument about it), and no per-message task churn.
  `create_task` keeps only a weak reference (`gateway/run.py:10494`), so the
  runtime holds a strong one, and it re-creates the task if the loop identity
  changed (multiplex, in-process restart).
- **`answered-since-detection` got much more important.** On a 15-minute tick
  it was a nicety; at arrival time it is the thing that turns "a human replied
  during the 90-second debounce, or during the in-flight call" into silence
  rather than a stale nudge.

**The debounce is three mechanisms doing three different jobs**, and all three
are needed:

| Mechanism | Job it does | What it cannot do |
|---|---|---|
| coalescing map, keyed on the thread | a 20-message burst is **one** judgment | bound the rate across threads |
| per-thread quiet timer (`arrival_debounce_seconds`) | decide *when* one thread is ready; doubles as the politeness floor | bound anything |
| per-channel + global token buckets | bound the judgment **rate**: 200 threads with one message each are 200 legitimate map entries | stop a burst |

`due(now)` is a **pure** function over the pending map plus an injected clock,
which is why burst / floor / max-wait / eviction are all unit-tested with no
event loop at all (there is no `pytest-asyncio` here and none is being added).

### Why the sweep stays

Not legacy — it does five jobs the arrival trigger cannot:

1. **`stalled_thread` is unreachable by construction** from an arrival trigger.
   Arrival fires ~90s after the last word; "this decision has been sitting for
   45 minutes" is a different judgment.
2. **The pending map is in memory.** A gateway restart or crash loses in-flight
   debounce state; the sweep is the only thing that recovers those threads.
3. **Quiet hours.** Everything said overnight is recorded and dropped by the
   arrival path. Without the sweep it would never be judged at all.
4. **It is the ops reporting surface for both triggers.** The gateway has no
   `--deliver`, and opening a second gateway→Slack path would be a new place to
   leak — so the sweep reports arrival activity from durable counters and
   carries the budget alerts. *The arrival path never posts anywhere except the
   nominated thread.*
5. **The upstream `message:observed` tap** (issue #80338, F-G02/F-G03) may move
   ingestion; the sweep needs no gateway at all.

The two triggers **partition by age** — arrival owns
`[arrival_debounce_seconds, min_age_minutes)`, the sweep owns
`[min_age_minutes, ∞)` — enforced at config load by clamping
`arrival_max_wait_seconds` strictly below `min_age_minutes × 60`. Even if they
did collide, `needs_judgment` + `has_intervention` + `shadow_seen` bound the
total, so the worst case is one extra judgment, not a loop.

### What arrival mode deliberately does NOT add

No escalation to a tool-bearing agent session. No `ctx.inject_message`. No
toolset widened anywhere. The capability envelope of arrival mode is *exactly*
the sweep's: one bounded, tool-less, structured LLM call whose only effect is a
≤200-character plain-text post into one Slack thread. See
[PARITY.md](PARITY.md) gap 2 for why that gap is left open on purpose and what
consent would have to exist before it is closed.

**Why no agent anywhere.** Two facts from the real source killed the original
"a cron agent calls `send_message` into the thread" design:
`cron/scheduler.py:182` hardcodes `messaging` into every cron session's
disabled toolsets (user config layers on top, so per-job `enabled_toolsets`
cannot re-widen it), and `--deliver` is one static target per job so it cannot
address a per-candidate `thread_ts`. Running as `--no-agent`
(`cron/scheduler.py:3213` short-circuits before `run_agent` is even imported)
makes both constraints irrelevant — and wins the containment argument
outright, because no tool-bearing session ever sees the untrusted text.

### Controls: what Claude Tag actually has

Cooldowns and per-day nudge caps are **gone**. They were crutches for two
missing things — a spend limit and real judgment — and both now exist. Claude
Tag has neither crutch; what it has, and what we now have:

| Control | Where |
|---|---|
| **Spend limit**, per-channel/day + global/day + global/month, alerts at 75/95 %, over cap ⇒ **declined, never truncated** | `aw_budget` + the gate's pre-flight check |
| **Model judgment** as the primary filter (confidence ≥ 0.7 to post) | `aw_judge` |
| **Once per thread, forever** | `interventions` ledger, checked in the detector *and* in the send path |
| **Self-quiet** after 4 ignored nudges in a channel | `channel_self_quieted` |
| Throughput: at most one nominee per channel per sweep | `aw_detectors` |
| Re-judge watermark: a declined thread is not judged again until a human says something new (`judge_max_rejudge` bounds even that) | `judgments.last_activity_seen` |
| Quiet hours, channel/thread mute from Slack, kill switch with no LLM in its path, DM/bot/engaged-thread exclusions | `aw_detectors` / `aw_recorder` |
| **Judgment rate** (arrival trigger only): per-channel + global token buckets, metering **attempts**, never refunded | `aw_arrival.TokenBuckets` |
| **Burst coalescing** (arrival trigger only): N messages in one thread ⇒ one judgment | `aw_arrival.Debouncer` |

The watermark is the honest replacement for the cooldown: what the cooldown
really prevented was a thread costing us repeatedly just by continuing to
exist. Money, not a nudge count, is now the limiter.

### What the ceiling actually is, in dollars

Money is the limiter, so the limit has to be arithmetic rather than a claim.
One sweep = **exactly one** LLM call, whose prompt is hard-capped by the
sanitizer's judge profile (`candidates_per_run` 3 × `JUDGE_MAX_VIEW_CHARS`
2400 + ~2 kB of rules) and whose reply is capped by `judge_max_tokens`:

| | tokens | cost at the unpriced fallback ($5/$15 per 1M) |
|---|---|---|
| worst-case prompt, 3 nominees | ~2 340 | $0.012 |
| worst-case completion | 600 | $0.009 |
| **worst case per sweep** | | **~$0.021** |

At the 2-minute testing cadence with the default quiet hours (11 active
hours ⇒ ~330 sweeps/day) the uncapped worst case is ~**$6.80/day**, so the
default `daily_usd_global` of $1.00 binds after ~48 sweeps and the shipped
caps are the real ceiling — not the sweep cadence, and not a nudge count.
Nudges inherit that ceiling: at most one post per channel per judged sweep,
at most one per thread ever, and a channel stops itself after
`self_quiet_after_ignored` unanswered nudges. **Channel traffic does not
appear in any of these bounds**, which is the property the deleted daily caps
were faking.

#### …and what the ceiling is once the trigger is a message, not a tick

This is the arithmetic that changed, and it is the reason the token buckets
exist. One **arrival** judgment is one nominee: ~2 kB of rules + a 2 400-char
judge view + ~100-char header ≈ **1 150 prompt tokens**, ≤600 completion. At the
deliberately pessimistic unpriced fallback ($5/$15 per 1M):

| | tokens | cost |
|---|---|---|
| prompt, 1 nominee | ~1 150 | $0.0058 |
| completion | 600 | $0.0090 |
| **per arrival judgment** | | **~$0.015** |

Per channel, `arrival_burst` 2 + 4/hour over an 11-hour active day = ≤46
judgments = $0.68 — so the shipped `daily_usd_per_channel` of $0.25 binds
first, after ~17 judgments, exactly as it does for the sweep. The global bucket
(12/hour) caps the process at ≤132 calls/day, the same order as the sweep's 96.
Per thread the watermark caps lifetime cost at
`(1 + judge_max_rejudge) × $0.015 ≈ $0.03`, **whichever trigger spends it**.

**Channel traffic still appears in none of those bounds.** That is the property
that had to survive moving the trigger, and the buckets are what make it
survive: without them the USD caps would still bound the *bill* but not the
*exhaustion* — one hostile actor burns `daily_usd_global` in ninety seconds and
ambient goes silent for the rest of the day, at full price.

**A call we cannot measure still costs money.** A provider that times out,
resets, or 429s *after* reading the prompt has already billed for it and
`call_llm` returns nothing at all — and a failed sweep writes no watermark, so
the same nominees are re-sent on the next tick. Metering zero there was the one
genuinely unbounded hole in the design (720 billed calls/day against a $0.00
ledger, every cap untripped). So when a call yields no usable verdict *and* no
usage figure, `aw_judge.estimate_prompt_tokens` charges the prompt we actually
sent (~4 chars/token) and the breadcrumb says `charged ~N ESTIMATED prompt
tokens`. A provider outage is now a *bounded* outage: the cap trips, the gate
declines, and the operator hears about it.

## Containment: untrusted channel text

Every excerpt this plugin produces is **verbatim text an attacker chose**. Any
workspace member can put it in a watched channel, and `--allow-all` (required
for ambient to see channel traffic at all) means there is no sender filter in
front of it. So the design question is not *whether* hostile text enters the
pipeline — it does, by construction — but *what it can reach* once inside.

On 2026-08-11 the answer turned out to be "more than intended". A human posted
an un-mentioned reply in a thread the bot had been @mentioned in. The
engaged-thread rule correctly let it through, and the resulting **normal**
gateway session — full core toolset: `terminal`, `read_file`, `write_file`,
`execute_code`, `browser_*`, `cronjob`, `delegate_task` — then, unprompted:
ran `session_search`, found the *cron sweep's* transcript, learned the absolute
path of `candidates.json` from it, read the file, and quoted a watched-channel
excerpt back to the human.

Nothing was exploited; the excerpts were benign. But the whole chain existed
and the two controls the design leaned on were both absent:

- the live cron job was created with `hermes cron create`, which leaves
  `enabled_toolsets: null` — so the "locked-down compose session restricted to
  `send_message`" **did not exist**; the sweep session had `terminal`;
- `send_message` is not in Hermes' core agent toolset at all, and cron always
  disables the `messaging` toolset (`cron/scheduler.py:182`) — so
  `aw_guard.py`'s target pinning was a **dormant no-op**, not a mitigation.

And Hermes' own promptware defence did not apply either: the
`<untrusted_tool_result>` wrapper plus `scan_for_threats` is keyed to
`web_search` / `web_extract` / `browser_*` / `mcp_*`
(`agent/tool_dispatch_helpers.py:584`). `read_file` and `session_search` are
not on that list, so the excerpt arrived as ordinary trusted tool output.

### The three layers

**L1 — neutralize at the source** (`aw_sanitize.py`). Now **bidirectional**,
because since the judge landed untrusted text moves both ways:

- *inbound* `build_judge_view` — what the model reads: 12 messages × 400
  chars / 2400 total, pseudonymous authors (`A1`, `A2`, `BOT` — never Slack
  user ids, which are an invitation to @-mention someone), relative
  timestamps. Deliberately richer than the export profile, because the view
  is a local variable and one HTTPS body: nothing persists it, so the tight
  caps' rationale does not apply. Instruction-shaped text is still withheld —
  the judge's output is text we post publicly, so an injected judge is the
  worst case in the whole system.
- *outbound* `sanitize_nudge` — what we post: one line, ≤ 200 chars, no URLs,
  no `@`, no code, not instruction-shaped. **Without this the judge is a
  laundering path**: hostile text in, model-relayed text out, posted into
  Slack under our name. It refuses rather than truncates, and a refused nudge
  becomes silence.
- `build_excerpt` remains the compact export profile (4 × 120 / 480), stored
  in the `judgments` ledger so an operator reviewing a soak in a terminal can
  see the thread next to the nudge it produced.

All three share the same machinery, and the two guarantees below are
deliberately unequal:

- *Structural*: every character that could forge the payload's own container is
  removed — `<>` (our `<untrusted-slack-text>` delimiter and Slack's link
  syntax), backticks (cron wraps script stdout in a ``` fence,
  `cron/scheduler.py:2644`), `{}[]` (JSON, markdown, and bracketed protocol
  markers such as cron's own `[SILENT]`), `\$*_~^`. The delimiters are attached
  **after** stripping, so forging them is impossible rather than unlikely.
- *Heuristic*: instruction-shaped text is withheld entirely and replaced with a
  redaction marker, checked against both the raw and the de-obfuscated form so
  that `ign​ore previous instructions` trips the detector instead of evading it.
  A redacted candidate still produces a nudge; it just carries no quote.

L1 stays load-bearing as defence in depth, and `ambient.db` still holds raw
channel text on purpose.

**L2 — nothing untrusted leaves the process** (`gate.py`). This is the layer
the `--no-agent` design *upgraded* rather than merely kept. Previously the
excerpt travelled on stdout into a cron agent prompt, and that prompt became a
`messages` row in the shared `state.db`, FTS5-indexed and retrievable by every
future session on any platform — the exact channel the incident used. **There
is no agent prompt any more**, so the rule is now the strongest available
version: *no channel text appears on the gate's stdout at all*. The audit line
carries channel id, thread ts, kind, verdict, confidence, spend and the
model-authored nudge — nothing verbatim.

That matters beyond Slack: `_run_job_script` pipes stdout through
`redact_sensitive_text` and `save_job_output` then persists the whole document
under `~/.hermes/cron/output/<job_id>/`, which the **L3 jail does not cover**.
Keeping it excerpt-free is what makes that persistence a non-issue instead of
a new leak. `candidates.json` is gone, and `purge_untrusted_artifacts()`
deletes the legacy file on *every* run including silent ones, plus once at
plugin registration, so a payload cannot outlive the sweep that produced it
(the leaked excerpt came from an *earlier* run) and the file already sitting on
a deployed machine removes itself.

The untrusted text now exists in exactly two places: `ambient.db` (jailed) and
the judge's HTTPS request body.

**L3 — unconditional data-directory jail** (`aw_guard.py`, via the existing
`pre_tool_call` hook). No agent session may reference the ambient data directory
from any tool call, matched on every string in the argument tree, for **every**
tool name — including tools that do not exist yet, because an allowlist of tool
names is exactly the mistake `_UNTRUSTED_TOOL_NAMES` makes upstream. There is
**no principal exemption, not even for the sweep**: the sweep needs no file
access, so the rule can be absolute. A
`session_id.startswith("cron_")` carve-out would have admitted every cron job on
the machine and would still have been a string comparison an attacker can aim
at. The jail is what protects `ambient.db`, which holds raw channel text on
purpose (the detectors run SQL over it) and is therefore never sanitized. It is registered on the config-failure path too: "dormant" must not
mean fail-open for containment the way it once did for dispatch.

Operators read the ledger with `python aw_status.py` in a terminal — outside the
agent, where the jail does not apply and a human is doing the reading.

**The jail's blind spot, and why `aw_status.py` closes it.** The jail matches
*names*: a tool call that mentions the data directory, `ambient.db`,
`gate_errors.log` or `candidates.json` is blocked. `terminal: python
aw_status.py` mentions none of those, so an agent session with the terminal
tool can run the reader — and the jail's own block message points at that
command. "In a terminal" is therefore not proof of "by a human". So the reader
is treated as part of containment: every `messages` body it prints goes through
the same L1 export profile as a stored excerpt (`aw_status.safe_text`), and if
`aw_sanitize` cannot be imported it prints `[text withheld: …]` rather than the
raw row. What an agent can obtain by shelling out is exactly what the gate
would have shown it anyway — inert, capped, injections redacted — instead of
the verbatim payload that caused the 2026-08-11 incident.
(Regression: `tests/test_status_output.py`.)

**Deleted: the `send_message` target pinning.** `aw_guard` used to pin
`send_message` targets to an "armed intent" ledger so a compose agent could
only post into the nominated thread. That control could never fire —
`send_message` is not in the core agent toolset and cron always disables
`messaging` — so it was not "dormant", it was scenery, and a review read it as
protection. It and the `intents` table are gone. The invariant it was meant to
express now lives in `aw_post.post_nudge`, in the **actual send path**, where
it can fire: watched channel only, thread must exist in our own ledger, once
per thread, not muted, not already answered by a human, live mode only, and
the text must survive `sanitize_nudge`.

The jail's markers deliberately include the whole `plugin-data` segment, not
just `ambient_watch`, so asking the agent to poke at *any* plugin's private
state gets a block with an explanation. That is a small, intentional loss of
convenience; the block message says what to run instead.

## Install (already done on this machine)

Plugin lives at `%LOCALAPPDATA%\hermes\plugins\ambient-watch` and shows in
`hermes plugins list` as **not enabled** — it stays dormant and fail-closed
until configured.

## Status: verified live

Confirmed working against a real Slack workspace on 2026-08-11:

- a human message in a watched channel is **recorded and suppressed** —
  gateway log shows `pre_gateway_dispatch skip: reason=ambient-watch: recorded`,
  no reply, no agent session, zero tokens
- the sweep detects it, drafts a nudge, and delivers the shadow digest to the
  ops channel
- the gate returns `{"wakeAgent": false}` when idle, so idle sweeps cost nothing

⚠️ **The judge + spend limit + direct delivery are verified by tests, not yet
by a live run.** 210 tests pass under the Hermes venv, including the real `cron.scheduler`
subprocess path, the real plugin loader, and the real auxiliary-task
resolution — but no real LLM call and no real Slack post has been made through
the new pipeline. See [Operator actions required](#operator-actions-required)
for what to run, and expect the first sweep after the flip to be the thing
that finds the operational bug (that is the pattern this project has, three
times running).

## Operator actions required

Run these yourself, **in this order**; nothing in this repo runs them for you.

0. **Install the new code into the live plugin dir.** It was deliberately left
   un-synced, because the currently-installed sweep is an *agent* job on a
   **2-minute** schedule and it runs `gate.py` as its pre-run script: the
   moment the new gate lands there, every tick starts calling the judge — real
   money, on a 2-minute timer, before step 3 has fixed the job. So sync and
   re-create the job in one sitting:
   ```
   robocopy "E:\GIT\ClaudeTag\ambient-watch" "%LOCALAPPDATA%\hermes\plugins\ambient-watch" /MIR /XD __pycache__
   ```
   While you are there, put the schedule back to `every 15m` (it is at `2m`
   for testing, which is 8× the exposure and 8× the ticks that can spend).
1. **Add the spend caps + judge settings** to
   `%LOCALAPPDATA%\hermes\plugin-data\ambient_watch\config.json` (see the
   config table below). The plugin ships conservative defaults ($0.25 per
   channel per day, $1/day, $10/month) and they apply to the config already
   deployed, which sets none of them — so the first sweep after step 0 *will*
   spend up to those limits unless you lower them first. Setting all three to
   zero makes `Budget.decision()` return `unconfigured` and **every candidate
   is declined**: ambient does nothing, and says so once a day in the ops
   channel. Delete the retired keys while you are in there — and note the live
   config has no `min_age_minutes`, so it will use the 45-minute default rather
   than the 3-minute test window `unanswered_after_minutes` used to give it.
2. **Pin a cheap model for judgment** (optional but recommended): after the
   gateway restart, `hermes model` → *Configure auxiliary models* →
   **Ambient judgment**. Nothing else needs the key — the gate calls
   `call_llm(task="ambient_watch_judge")` with no provider argument, so
   `auxiliary.ambient_watch_judge` in `config.yaml` is the whole knob. To give
   the judge its own credential use `auxiliary.ambient_watch_judge.api_key` or
   `key_env` **in config.yaml**, not an `AUXILIARY_*` env var: cron strips
   those from the script's environment (`_is_hermes_internal_secret`).
3. **Re-create the sweep as a `--no-agent` job.** The old job runs an agent
   session that no longer has anything to do, and it would still be billed:
   ```
   hermes cron remove ambient-sweep
   hermes cron create "every 15m" \
     --name ambient-sweep \
     --script ambient_watch_gate.py \
     --no-agent \
     --deliver "slack:C0…ops channel"
   ```
   No prompt argument (it is `nargs="?"` and ignored under `--no-agent`), no
   `--workdir`, no `enabled_toolsets` — there is no agent to scope. Keep the
   schedule at `every 15m`: every tick is one more ingestion of
   attacker-controllable text, and now also one more chance to spend.
4. **Restart the gateway** so `register()` runs the new code (it registers the
   `post_api_request` leak detector and the `ambient_watch_judge` auxiliary
   task, and rewrites the cron shim).
5. Optional: set `"sweep_job_id": "<the id from hermes cron list>"` in
   `config.json` to arm the leak detector. Under this design ambient must
   account for **zero** agent-session tokens, so if that hook ever sees usage
   attributed to the sweep, the job lost its `no_agent` flag — the anomaly
   lands in `gate_errors.log`.
6. Keep `cron: {wrap_response: false}` in `config.yaml` to drop Hermes'
   `Cronjob Response: …` delivery envelope around the audit line.

Then watch `python aw_status.py` — it now shows spend against each cap, which
thresholds have alerted, and every judge verdict with its confidence,
reasoning and the nudge it produced.

### Turning ON arrival-time judging (separate, later, in this order)

Nothing below is run for you, and the order matters: steps 1–2 are the
"prove it changed nothing" step, and skipping them means the first thing you
learn about the new trigger is whatever it does in production.

1. **Sync the plugin dir and restart the gateway BEFORE flipping anything.** The
   pump only exists once `register()` has run the new code, and `arrival_enabled`
   is read at registration:
   ```
   robocopy "E:\GIT\ClaudeTag\ambient-watch" "%LOCALAPPDATA%\hermes\plugins\ambient-watch" /MIR /XD __pycache__
   ```
2. **Leave `arrival_enabled` absent/false for that first restart** and confirm
   from the Hermes log that nothing changed. The new keys are additive: a
   deployed `config.json` without them loads and behaves exactly as before.
3. **Flip it with `mode: shadow` and soak.** Shadow judges and spends but never
   posts, so the soak measures the *real trigger* at the *real price* — which is
   the only way to answer whether 90 seconds is the right floor. Add to
   `config.json`:
   ```json
   { "arrival_enabled": true, "arrival_debounce_seconds": 90 }
   ```
   then restart the gateway again (this is a `register()`-time decision).
4. **Drop the sweep cadence to `every 30m`.** The sweep is now the
   stalled-thread backstop and the reporter, not the primary trigger:
   ```
   hermes cron remove ambient-sweep
   hermes cron create "every 30m" --name ambient-sweep \
     --script ambient_watch_gate.py --no-agent --deliver "slack:C0…ops channel"
   ```
5. **Re-verify the pinned judge model.** `hermes model` → *Configure auxiliary
   models* → **Ambient judgment**. The arrival path resolves the **same**
   `auxiliary.ambient_watch_judge` block as the sweep, so an unpinned model
   silently means main-model prices on a trigger that now fires far more often.
6. **Confirm `plugins.entries.ambient-watch.llm.*` is absent from
   `config.yaml`.** We do not use `ctx.llm` and nothing should suggest we do —
   a stray `allow_model_override` there would be a second, undocumented knob
   for something `config.json` already owns.
7. **Measure the loop cost before trusting it.** `async_call_llm` does
   `_resolve_task_provider_model` (a config file read) and `_get_cached_client`
   before its first await — a few milliseconds on the loop per judgment. If you
   ever see inbound-message latency move, that prelude is the first suspect and
   it belongs in `to_thread` too.
8. **Multiplex profiles are untested here.** `create_task` snapshots the context
   with `copy_context()`, so the pump *should* inherit the profile's
   `set_hermes_home_override` / `set_secret_scope` (verified from
   `gateway/run.py:2006-2039`), but nobody has run ambient mode under
   `multiplex_profiles: true`. Leave the sweep in charge there until someone
   does.

Rollback at any point: set `arrival_enabled: false` and restart the gateway. The
sweep keeps working throughout; nothing in the ledger needs undoing.

## Config surface

| Key | Default | Meaning |
|---|---|---|
| `mode` | `shadow` | `live` posts; `shadow` judges, pays, and only digests |
| `min_age_minutes` | 45 | **SWEEP ONLY** — thread must be quiet this long. Unchanged name, unchanged meaning; it is now documented as sweep-scoped because the arrival path has its own floor |
| `candidates_per_run` | 3 | nominees judged per sweep (throughput cap) |
| `arrival_enabled` | **false** | ships dark. `false` ⇒ no runtime object, no pump task, behaviour byte-identical to sweep-only. **This is the rollback: one boolean.** |
| `arrival_debounce_seconds` | 90 | the coalescing quiet period **and** the politeness floor — one number because they are the same requirement. Clamped to ≥30 at load: a typo must not produce a one-second reply |
| `arrival_max_wait_seconds` | 300 | a never-quiet thread still gets judged once; `0` disables. Clamped strictly below `min_age_minutes × 60` so the two triggers keep partitioning by age |
| `arrival_judgments_per_channel_hour` | 4 | per-channel bucket refill rate |
| `arrival_judgments_global_hour` | 12 | global bucket refill — **without it, N watched channels multiply the spend rate** |
| `arrival_burst` | 2 | bucket capacity, both scopes |
| `arrival_max_pending` | 200 | cap on the in-memory pending map; over cap the **new** entry is dropped and counted (the sweep is the backstop, so the loss is latency) |
| `arrival_pump_interval_seconds` | 5 | pump wake interval; worst added latency is debounce + this |
| `judge_confidence_threshold` | 0.7 | below this the nudge is withheld |
| `judge_max_rejudge` | 1 | extra judgments allowed after new human activity |
| `judge_max_tokens` / `judge_timeout_seconds` | 600 / 30 | hard bounds on the call |
| `judge_model` / `judge_provider` | `""` | override `auxiliary.ambient_watch_judge` |
| `daily_usd_per_channel` | 0.25 | **the limiter.** Over cap ⇒ declined |
| `daily_usd_global` | 1.00 | whichever cap is tightest wins |
| `monthly_usd_global` | 10.00 | |
| `alert_thresholds` | `[0.75, 0.95]` | one ops line per threshold per period |
| `prices` | `{}` | `{model: [usd_per_1M_in, usd_per_1M_out]}`. **Unpriced models fall back to $5/$15 per 1M on purpose** — an unpriced model must never read as free. Set your real prices here |
| `self_quiet_after_ignored` | 4 | ignored nudges before a channel goes quiet |
| `quiet_start` / `quiet_end` / `quiet_tz` | 20:00 / 09:00 / UTC | |
| `retention_days` | 14 | ledger pruning |
| `sweep_job_id` | `""` | arms the `post_api_request` leak detector |

**Retired keys** — `cooldown_minutes`, `caps_per_channel_per_day`,
`caps_global_per_day`, `unanswered_after_minutes`, `stalled_after_minutes`.
A deployed config keeps working: they are ignored with one warning at load,
and `aw_status.py` flags them as safe to delete.

## Go-live runbook

1. **Create the Slack app.** Generate the manifest with
   `hermes slack manifest --agent-view --write`, then at
   [api.slack.com/apps](https://api.slack.com/apps) use **From a manifest**.
   Copy the bot token from **OAuth & Permissions** (not the creation modal) and
   set `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` in `%LOCALAPPDATA%\hermes\.env`.
   Verify the grant actually landed — `auth.test`'s `x-oauth-scopes` header must
   list all 17; a partial install silently breaks channel events.
2. **Run the setup script** — resolves the bot ID and channel IDs, writes the
   plugin config, and patches the `config.yaml` pairing (with a backup):
   ```
   python setup_slack.py --watch "#dev" --ops "#ambient-ops" --allow-all
   ```
   It refuses to run on missing scopes or if the bot is not a member of both
   channels, so it cannot half-configure you. `--allow-all` sets
   `SLACK_ALLOW_ALL_USERS=true`; without it the Slack adapter rejects
   non-allowlisted senders *before* the plugin's hook fires, so ambient would
   only ever see your own messages.
   <details><summary>Or configure by hand</summary>

   `%LOCALAPPDATA%\hermes\plugin-data\ambient_watch\config.json`:
   ```json
   {
     "bot_user_id": "U0…(from auth.test)",
     "channels": ["C0…watched channel"],
     "mode": "shadow",
     "ops_channel": "C0…ops channel",
     "quiet_tz": "Asia/Tbilisi",
     "daily_usd_per_channel": 0.25,
     "daily_usd_global": 1.00,
     "monthly_usd_global": 10.00
   }
   ```
   </details>
3. **Pair the adapter** — in `%LOCALAPPDATA%\hermes\config.yaml`:
   ```yaml
   plugins:
     enabled: [ambient-watch]
   slack:
     free_response_channels: ["C0…watched channel"]   # MUST equal config.json channels
   ```
   ⚠️ The pairing is the one sharp edge: `free_response_channels` without a
   healthy plugin = Hermes answers everything there. The plugin fails closed
   (skips recording-eligible traffic even on internal errors), but keep the
   two lists identical, always.

   ⚠️ Never put a watched channel in `slack.ignored_channels`. The gateway
   drops ignored-channel messages *before* `pre_gateway_dispatch` fires
   (run.py:14873-14884), so the plugin would never see — or record — a
   single message there, and the sweep would find nothing forever.
4. **Create the sweep cron job** — see
   [Operator actions required](#operator-actions-required) for the exact
   command. Three things about it are load-bearing:

   - the script MUST be the **bare filename** `ambient_watch_gate.py`. Cron
     path-jails scripts to `%LOCALAPPDATA%\hermes\scripts\` and
     `cronjob_tools._validate_cron_script_path` rejects absolute/`~` paths at
     job-creation time, so a full path (or a `python <path>` command string) is
     refused. The plugin writes the shim there at every registration, and it
     fails closed — any error prints `{"wakeAgent": false}` and exits 0.
   - `--no-agent` is what makes the whole design work. Without it you get an
     agent session that has nothing to do, cannot post, and is billed anyway.
   - `--deliver "slack:<ops channel>"` carries the audit line. It is subject to
     `wrap_response` and to `redact_sensitive_text`; note that if redaction
     itself fails, cron replaces the ENTIRE output with
     `[REDACTED - redaction failed]`, which is non-empty and not wake-gate
     JSON, so it *will* be delivered as a mystery line. That is cosmetic, but
     do not read it as a gate failure.

   **Live vs shadow** is `mode` in `config.json` — nothing else changes. Shadow
   still runs the judge and still spends money (that is the point: the soak
   measures the real product at the real price); it just never posts.
5. **Shadow soak ≥ 2 weeks** in one high-signal channel → review digests in the
   ops channel and `python aw_status.py` → flip to live with
   `python aw_status.py --mode live` when ~70 % of would-posts look useful.
   Each thread is digested **once** in shadow mode (`shadow_seen` ledger), and
   shadow history never blocks a real nudge after the flip.

## Operating it

```
python aw_status.py                # config, SPEND vs caps, ARRIVAL state,
                                   # judge verdicts, kill switch, ledger,
                                   # engaged threads, interventions, errors
python aw_status.py --mode live    # shadow -> live
python aw_status.py --kill on|off  # halt / re-arm the sweep (no LLM in path)
python aw_status.py --prod         # restore production thresholds
python aw_status.py --reset-ledger # clean slate between tests
```

There is no candidate file to inspect: read the digests in the ops channel or
`%LOCALAPPDATA%\hermes\cron\output\`, and the verdict/spend detail with
`python aw_status.py`. **Do not run the gate shim by hand to "see what it
would do"** — it is a *real* sweep now: it spends money on a judge call, marks
threads `shadow_seen`, and in live mode posts to Slack.

**Two `pause`-shaped controls exist, with different blast radii.** At 3am reach
for `python aw_status.py --kill on`: it needs no Hermes CLI, it is checked
first in the gate, and it short-circuits before any spend or any post.
`hermes cron pause ambient-sweep` also works and is the right choice for a
planned stop, but it needs a healthy CLI and it stops the tick rather than the
behaviour.

With arrival mode on, the kill switch stops **both** triggers, and it stops a
judgment that is already sitting in the debounce queue: the arrival path reads
it from a 5-second TTL cache in the hook (a SQL read per inbound message must
not touch the loop's hot path) but **re-reads it fresh** in the pump,
immediately before spending anything. Note that `hermes cron pause` does *not*
stop arrival judgments — it only pauses the sweep. To stop the arrival trigger
specifically, set `arrival_enabled: false` and restart the gateway; to stop
everything now, use the kill switch.

### When ambient mode goes quiet

The gate fails closed, and the scheduler *discards* the stdout of a
`wakeAgent=false` run — so a permanently broken gate looks exactly like a
quiet week. This got more load-bearing with the judge: a judge timeout, a
Slack `channel_not_found`, a stripped token or an unconfigured budget all end
in silence by design. In order:

1. `%LOCALAPPDATA%\hermes\plugin-data\ambient_watch\gate_errors.log` — every
   fail-closed path (shim import failure, config parse error, detector crash,
   **judge unavailable**, **post failed**, **no spend cap configured**,
   **agent tokens billed to the sweep**) appends a timestamped entry here. An
   empty/absent file means the gate is genuinely finding nothing; entries mean
   it is broken. The log rotates to `.log.1` at 64 KB.
2. `python aw_status.py` — is spend at 100 % of a cap (everything is being
   declined), or is there no cap at all?
3. `%LOCALAPPDATA%\hermes\scripts\ambient_watch_gate.py` — written by
   `register()` at every Hermes start. If it is missing, the plugin failed to
   register (check the Hermes log for `could not install the cron gate shim`)
   and the cron job has nothing to run.

An unconfigured budget is the one failure that is also *audible*: it puts one
`MISCONFIGURED` line into the ops channel per day, because "ambient silently
does nothing" must not be indistinguishable from a quiet week.

**With arrival mode on, ops visibility is split across two processes** — the
judgments happen in the gateway, the reporting happens on the sweep's tick — so
"ambient looks like a quiet week" has one more way to happen: a broken sweep now
hides arrival activity too. The compensations, both of which need a human to
look:

4. `%LOCALAPPDATA%\hermes\plugin-data\ambient_watch\arrival.log` — one
   excerpt-free line per arrival outcome (`POSTED`, `WOULD HAVE POSTED`,
   `withheld`, `DECLINED`, `throttled`, `kill-switch`, `judge unavailable`).
   Rotates to `.log.1` at 64 KB, same as `gate_errors.log`. An empty file with
   `arrival_enabled: true` means nothing has been judged on arrival — check
   whether the pump exists (the Hermes log prints
   `arrival-time judging ENABLED` at registration) and whether the channel is
   inside quiet hours.
5. `python aw_status.py` → the **ARRIVAL-TIME JUDGING** block: the effective
   settings, durable counters (judged / posted / withheld / declined /
   throttled), arrival spend, and what has not yet been reported to the ops
   channel. It states explicitly that the pending map and the rate buckets are
   in-memory in the gateway process and therefore **not visible from the CLI** —
   an operator who reads "pending: 0" into a stalled pump has been misled.

## Development

```
.venv\Scripts\python -m pytest                      # 274 passed, 14 skipped (fakes only)
PYTHONPATH=<hermes-agent> <hermes-venv>/python -m pytest   # 305 passed, incl. real runtime
```

Most tests use fakes of the verified v0.20.0 contracts (`tests/conftest.py`),
so no Hermes install is needed to develop. `tests/test_real_*.py` drive the
actual Hermes loader, `GatewayRunner._handle_message`, and
`cron.scheduler._run_job_script` — they skip without Hermes importable.

**No test ever makes an LLM call or a Slack call.** The judge and the transport
are injected (`run_gate(..., judge_fn=, transport=)`), and every config the
real-runtime tests write sets all three spend caps to zero — which makes
`Budget.decision()` return `unconfigured` and the gate decline before the judge
is reached. That is belt and braces on purpose: a test suite that can spend
money is a test suite nobody runs twice.

Where the interesting assertions live:

- `test_containment.py` — L1 (both directions), L2 (**no channel text on
  stdout at all**), L3 (the jail, no exemptions).
- `test_live_delivery.py` — one post per thread ever; budget-exceeded declines
  *before* the judge is called (asserted by the fake judge never being
  invoked); judge failure ⇒ silence; audit line carries no channel text.
- `test_judge.py` — the model's reply is validated, not trusted: bad JSON,
  unknown nominee ids, junk confidences and unsafe nudges all fail closed.
- `test_budget_wiring.py` — spend metered from the judge's own token counts,
  attributed per channel, alerts once per threshold per period, and a
  behavioural proof that **money, not a nudge count, is the limiter**.
- `test_detectors.py` — the re-judge watermark that replaced the cooldown.
- `test_arrival.py` — the debounce core, tested as **pure functions** with an
  injected clock (a 20-message burst ⇒ one due key; the floor does not fire
  early; `arrival_max_wait_seconds` fires a never-quiet thread once; the pending
  cap drops the *new* entry; the global bucket bounds channels together;
  availability does not consume a token), plus thin `asyncio.run()` wiring tests
  for the parts that need a loop (budget-exceeded ⇒ the fake judge is never
  called; the kill switch flipped mid-debounce; a human reply during the
  in-flight call ⇒ `answered-since-detection`; a failing judge still spends its
  bucket token; shadow mode cannot post; `arrival_enabled: false` creates no
  task at all; the pump is recreated when the loop identity changes).
  **No new test dependency** — `pytest-asyncio` is not installed and is not
  being added.

Mutation-verified (neutered the code, watched the test fail, restored, watched
it pass). Earlier passes: `exceeded` no longer declining ⇒ 2 failures;
`sanitize_nudge` removed from the judge's parse path ⇒ 1; the shadow digest
quoting the excerpt ⇒ 1; the once-per-thread check removed from `post_nudge`
⇒ 1; the re-judge watermark removed ⇒ 2. Arrival pass:

| Neutered | Result |
|---|---|
| the bot rung in `arrival_key` | `test_a_bot_message_is_never_enqueued` fails — i.e. our own nudge would re-trigger judgment on the thread it landed in |
| the bucket token refunded when the judge fails | `test_the_bucket_token_is_consumed_by_a_FAILING_judge` fails — attempts, not successes |
| `has_intervention` removed from the **shared** ladder | **an arrival test AND a sweep test fail** (`test_a_thread_already_nudged_is_not_judged_on_arrival` + `test_thread_with_prior_intervention_is_not_a_nominee`) — this is the one-ladder proof |
| Tier B reading the kill switch from the TTL cache instead of fresh | `test_the_kill_switch_flipped_mid_debounce_stops_a_queued_judgment` fails |
| the budget check no longer declining before the call | 2 failures (`exceeded` and `unconfigured`) |
| Tier A using the **blocking** `kill_switch()` instead of `kill_switch_nowait()` | 2 failures — `test_tier_a_never_blocks_on_the_store_lock` (a real thread holds the store's RLock; Tier A must return in well under a second) and `test_a_lock_busy_read_retries_on_the_next_message` |

Hardening history, in the order the bugs were found:

| Pass | Found |
|------|-------|
| 30-agent adversarial review | 24 confirmed: guard parsed the wrong target segment (a **production no-op**), non-thread-safe store, dead engagement/intent feedback loop, swallowed slash commands, missed Block Kit mentions, fail-open config failure, cooldown bypass, no retention |
| 4-agent real-runtime integration | **P0**: the cron shim was *never installed* — the real loader imports plugins as `hermes_plugins.<slug>` without touching `sys.path`, so `gate.py`'s flat imports always threw, and `register()` swallowed it. Ambient mode looked healthy and never swept once |
| First live Slack run | Shadow mode re-digested the same thread every sweep (~96/day/thread); cron's `--deliver` vs `send_message` preamble conflict sent digests to a local file |
| Second live Slack run | **CRITICAL, containment**: a normal full-toolset gateway session found the sweep's transcript via `session_search`, learned `candidates.json`'s absolute path from it (the gate printed the path; cron embeds stdout into an FTS-indexed message row), read the file, and quoted verbatim watched-channel text to a human. Both controls the design assumed were absent — the sweep job ran with `enabled_toolsets: null` (so the "locked-down compose session" never existed and had `terminal`), and `send_message` is not in the cron toolset at all, making the `pre_tool_call` target pinning a dormant no-op. Fixed by the three containment layers above |
| Parity pass (judge + budget) | The two "noise controls" everyone had been tuning — a 120-minute cooldown and per-day nudge caps — were **crutches for two absent capabilities**: a spend limit and real judgment. A regex matching `?` was the entire judgment stage. Deleting the crutches was only safe *after* building both; doing it in the other order would have removed the only brake on an unmetered, unjudged loop. Also found: `Budget.decision()` returned `ok` when no cap was configured (fail-OPEN), and the judge's own output needed a sanitizer — otherwise it launders hostile channel text into a public post |
| Cost / fail-safety verify pass | The spend limit had a hole exactly where fail-closed thinking had stopped at *posting*: a judge call that **failed** was metered as $0.00, and a failed sweep writes no re-judge watermark either, so a provider outage re-sent the same nominees every tick — 720 billed calls a day against a ledger that never moved and caps that therefore never tripped. Fixed by charging `estimate_prompt_tokens` whenever a call yields neither a verdict nor a usage figure. Related, and only visible in the same lens: `call_llm` owns provider fallback, so one metered call can hide several billed attempts (documented, not fixable from here) |
| Arrival-trigger pass | Three premises in the obvious design were false, and all three would have shipped silently. The third was self-inflicted and only visible on the second read: Tier A's kill-switch check, written to be "one cheap SELECT", would block on `AmbientStore`'s RLock for the full `busy_timeout` whenever a worker thread held it — reintroducing the exact five-second gateway stall that the whole `to_thread` discipline exists to prevent, via the one call that looked too small to audit. Fixed with a non-blocking read that keeps its previous answer on contention. The other two came from the source: `pre_gateway_dispatch` is invoked with a bare `ret = cb(**kwargs)` inside `async def _handle_message`, so a coroutine returned from the hook is **dropped unawaited** — the natural async implementation is a no-op that logs a RuntimeWarning. And `ctx.llm.acomplete_structured` hardcodes `task=None`, so pinning a cheap model in `auxiliary.ambient_watch_judge` would have been silently ignored and every arrival judgment billed at **main-model prices**, on the trigger that fires most often. Also found: `RECORD_SKIP` includes bot messages, so enqueueing on the recorder's verdict alone would let our own nudge re-trigger judgment on the thread it landed in — invisible, because `has_intervention` stops the second *post* but not the second *judgment*. And a tick with no sweep candidates returned before `Budget` was even constructed, so a threshold crossed by an arrival-time call would never have been announced anywhere |
| Containment verify pass | The jail matches *names*, so it never saw the one command that reads the ledger without naming it: `terminal: python aw_status.py`, which the jail's own block message recommends. The reader printed raw `messages` bodies, so an agent that got blocked could shell out and receive the verbatim payload — the 2026-08-11 leak, via a route added by the tool built to make that leak unnecessary. Fixed by putting the reader's output through L1 and rewording the block message. Same pass: the freshness re-check compared against "any human reply at all" instead of the detector's watermark, so every `stalled_thread` candidate was judged (paid for) and then refused as `answered-since-detection` — undeliverable forever in live mode, while shadow mode reported it as a would-post, i.e. the soak overstated the product |

The lesson worth carrying: the review caught *correctness* bugs, the
integration pass caught a *deployment* bug, and only a live run caught the
*operational* ones. Each layer found what the previous could not.

The containment finding sharpens that. Thirty adversarial reviewers and a
real-runtime integration pass both read a design whose security rested on
"only a locked-down cron session ever sees the excerpts", and neither checked
whether that session was actually locked down — it never had been. Reviews
verify the code against the design; only the live system tells you whether the
design's *premises* hold. And the exploit path was not adversarial input at
all: an ordinary agent went looking for context and found it, which is the
failure mode a threat model built around attackers is worst at predicting.

The parity pass adds a different lesson: **a control that cannot be removed
is a symptom.** The cooldown and the daily caps survived a 30-agent review, a
real-runtime pass and two live runs because everyone treated them as safety
features. They were load-bearing only because nothing else was — the moment a
spend limit and a judge existed, they had no job left. Worth asking of any
remaining knob: what would have to be true for this to be deletable?

### Residual risks worth naming

- **THE TRIGGER IS NOW ATTACKER-TIMED** (arrival mode only). The 15-minute
  cadence was an implicit rate limit that no message volume could change; at
  arrival time, whoever posts chooses when we spend. The per-channel and global
  **token buckets are the replacement for that cadence, not an optimisation.**
  If they are ever removed or set high "because the caps will catch it", the
  caps bound the bill but not the exhaustion: one hostile actor burns
  `daily_usd_global` in ninety seconds and ambient is silent for the rest of the
  day at full price — a denial-of-ambient with a real dollar cost. The buckets
  meter **attempts** and never refund, so a provider outage stays a *bounded*
  outage even though the trigger is now driven by channel traffic.
- **In-memory debounce state is lost on gateway restart or crash.** Threads
  pending at that moment are never judged by the arrival path. Mitigated only by
  the sweep — which is one of the five reasons the sweep stays, and why retiring
  it would turn this into silent data loss.
- **The judge's HTTPS body is now composed in the long-lived gateway process**
  rather than a short-lived subprocess. Nothing untrusted is persisted there,
  and the arrival path writes **no** channel text and **no** excerpt to any
  logger; its audit trail is `plugin-data/ambient_watch/arrival.log`, which is
  *inside* the L3 jail's `plugin-data` markers — strictly better than the
  sweep's `cron/output/<job_id>/`, which the jail does not cover.
- **Pending-map eviction is gameable in both directions.** Drop-new lets a flood
  of junk threads starve a legitimate one of its arrival-time judgment;
  drop-oldest would let a flood evict a real pending thread. Drop-new was chosen
  because the sweep is the backstop, so the loss is latency rather than the
  judgment — but there is no policy here that is not gameable, and the honest
  statement is that **under a raid arrival mode degrades to sweep behaviour**.
- **Cross-process re-judge race.** `needs_judgment` read-then-`record_judgment`
  write is not atomic across the gateway and the sweep. The age partition makes
  an overlap require an unlucky re-judge, and the blast radius is one extra
  ~$0.015 call, bounded by the watermark either way. Deliberately not fixed: a
  cross-process lock would put a new failure mode on the event loop. The same
  applies to the arrival counters — the read-modify-write can lose an increment,
  and the cost of that is one ops line off by one. Those counters are also
  accumulated in memory and flushed **once per drain**, off the loop thread, so a
  crash mid-drain costs that drain's counts: the counter file is an ops surface,
  not a ledger, and one durable write per refused thread was itself an
  amplification an attacker could drive (a write per message, contending with the
  recorder for the store's RLock — see `ArrivalRuntime._bump`).
- **Sqlite contention is correct, not fast.** `arrival.log` and the ledger are
  written from the gateway while the sweep process reads and writes the same
  file. WAL plus `busy_timeout` makes that correct; sustained arrival traffic
  *during* a sweep is the one contention scenario this has not been measured
  under.
- **The 90-second politeness floor is a judgment call**, made on the project's
  behalf, and a measurable divergence from Anthropic's spec. If the soak shows
  nudges landing after the thread moved on, it is the first knob to lower — but
  lowering it below ~30s should require evidence, not impatience, because the
  cost of being early is spending the thread's single post.
- **The gate is now a privileged out-of-process actor.** It loads the real
  `.env` (via `load_hermes_dotenv`, the same call the scheduler makes for
  `no_agent` jobs at `cron/scheduler.py:3222`) and posts to Slack with no
  agent, no approval prompt and no `pre_tool_call` guard in front of it. Note
  that the env scrubbing in `build_subprocess_env` is therefore **not** a
  boundary here — a future reader could reasonably mistake it for one. The
  trade is deliberate: a script whose entire logic we wrote is a far smaller
  attack surface than a full-toolset agent session reading attacker-chosen
  text. The price is that every bug in `aw_post.py` is a bug that posts.
- **Judgment spends money on a schedule with no human in the loop.** The
  pre-flight cap check is the only thing between a detector bug and a real
  bill, which is why an unconfigured budget now declines instead of proceeding.
- **The ledger under-counts a flaky provider.** `call_llm` owns retry and
  provider-fallback policy (`agent/auxiliary_client.py:42`, `:265`), so *one*
  judge call can bill several provider requests while the response we meter
  reports only the attempt that finally succeeded. A primary that fails after
  reading the prompt is therefore invisible spend on the success path — the
  estimate only covers a call that failed *entirely*. Pinning
  `judge_provider`/`judge_model` narrows the fallback chain, and the pessimistic
  $5/$15 fallback price absorbs some of it; neither is a guarantee. Compare the
  provider's own bill against `aw_status.py` during the soak.
- **The nudge is attacker-influenced by construction.** `sanitize_nudge` is a
  filter, not a proof: it cannot detect a nudge that faithfully paraphrases
  something an attacker wanted said. Shadow soak, once-per-thread and the
  200-char limit are what bound the damage.
- **The in-process vs standalone Slack send paths may not agree** on where a
  threaded reply lands if `reply_in_thread: false` is ever configured
  (`SlackAdapter._resolve_thread_ts` vs a direct `thread_ts`). Which branch you
  get depends on whether the gateway happens to host the tick.
- **Crash between post and ledger write** would allow a second nudge to the
  same thread on the next tick. Recording first and rolling back would invert
  that into a silent miss; the current ordering is deliberate (a duplicate is
  visible, a miss is not) but it is an ordering *choice*, not an accident.

Upstream path: this plugin's ingestion is isolated behind the recorder so a
future core `message:observed` tap (issue #80338, gaps F-G02/F-G03) replaces
`free_response_channels` with a one-module swap.
