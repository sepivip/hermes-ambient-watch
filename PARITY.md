# Parity audit vs Claude Tag

Measured against Anthropic's own spec, not against our design doc:
- https://claude.com/docs/claude-tag/concepts/how-it-works
- https://claude.com/docs/claude-tag/users/when-claude-responds
- https://claude.com/docs/claude-tag/users/proactivity

Verified 2026-08-11 against hermes-agent v0.20.0 source and the live install; arrival-time
judging re-verified 2026-08-12; context fidelity (P1) added and scope-audited 2026-08-12.
**Verdict: the session/threading model matches, and the trigger now matches too. The
behaviour still does not — we nudge, Claude Tag answers, and closing *that* gap is
deliberately blocked on human consent (gap 2 below).**

## Matches

| Claude Tag mechanic | Ours | Evidence |
|---|---|---|
| "Typing `@Claude` … is what starts a working session" | mention → `RECORD_PASS` → Hermes session | live T3 |
| "Once a session is active in a thread, it belongs to everyone there" | `thread_sessions_per_user` defaults **False** → threads shared, `[sender name]` prefixes, "Multi-user thread" prompt note | `gateway/session.py:1119-1123` |
| "Two threads in the same channel are two separate sessions" | `thread_id` participates in the session key | `build_session_key` |
| "The thread is durable; the sandbox is not" | Hermes sessions persist in sqlite; no sandbox to lose | `sessions` table |
| "A routine runs the same loop on a schedule" | `--no-agent` cron sweep runs the same detect→judge→post path in one process | `gate.py` |
| Replies "to channel messages it judges warrant a reply" as they arrive | debounced arrival-time judging on `pre_gateway_dispatch`, same prefilter, same judge, same send path | `aw_arrival.py` (dark by default) |
| Agent identity — acts under its own account, not the asker | posts as the bot user | live |
| DM → your own connectors, attributed to you | DMs are passed straight through to Hermes | `aw_recorder.decide` returns `PASS` for `chat_type=="dm"` |
| Result lands in the asking thread | `aw_post` posts with `thread_ts`, `reply_broadcast=False` | live T7 |
| A session "reads its own thread and the channel's history" | `aw_context` — `[THIS THREAD]` + `[CHANNEL]` + `[RECENT CHANNEL ACTIVITY]`, one 4400-char ceiling | `aw_context.py` (dark by default) |
| Other bots' replies filtered out of the window | filtered in `SlackReader._rows`, replaced by `(N bot message(s) omitted)` | `tests/test_context.py` |
| Spend limits, decline-not-truncate, 75/95 alerts | `aw_budget` | 7 tests |
| Self-quiet after N ignored | `channel_self_quieted` | tests |
| In-thread quieting (`!mute`) | `hermes ambient mute` | live T6 |

## Gaps, in priority order

### CLOSED 2026-08-11 — model judgment

*"It answers; we nudge"* is closed. `aw_judge` replaced the `?` regex with one bounded
auxiliary-LLM call per sweep. First real verdict, on "Anyone can tell me the population
of Georgia?":

    post  conf=0.96
    why:   Clarification is needed before answering the population question accurately
    nudge: Do you mean the country of Georgia or the U.S. state?

It spotted the ambiguity rather than answering wrongly — no regex or template reaches
that. Cost $0.0045. Fail-closed with **no fallback wording**: any exception, timeout,
non-JSON reply, schema violation or unsafe text yields silence, never a canned line.
Cooldowns and per-day caps are deleted, as Claude Tag has neither; the spend limit and
the `last_activity_seen` re-judge watermark replace them.

### CLOSED 2026-08-12 (code) — gap 1, trigger latency

**Was:** *"Latency: we wait, Claude Tag doesn't."* Claude Tag replies *"to channel
messages it judges warrant a reply"* as they arrive; our judge only ever saw a thread
that had already been quiet for `min_age_minutes`. The judgment was equivalent; the
trigger was not.

**Now:** `aw_arrival.py` judges on message arrival, debounced, driven by the existing
`pre_gateway_dispatch` hook. Latency goes from *up to `min_age_minutes` + up to one
sweep interval* to `arrival_debounce_seconds` (90s) + up to `arrival_pump_interval_seconds`
(5s) — roughly **45–60 minutes down to ~1.5 minutes**.

Honest scoping of that claim, because "CLOSED" is doing a lot of work in a table:

- **The trigger is closed; the code ships DARK.** `arrival_enabled` defaults `false`, so
  the deployed behaviour is still sweep-only until an operator flips it and restarts the
  gateway. Closed in this repo ≠ live on that machine.
- **Not measured against a soak.** 90s is a judgment call, not evidence (see *Deliberate
  divergences* below). The soak's job is to tell us whether humans routinely answer at
  2–4 minutes, in which case the floor is spending our one post to lose a race.
- **The sweep is NOT retired, and that is not legacy.** `stalled_thread` — "this decision
  has been sitting for 45 minutes" — is unreachable by construction from an arrival
  trigger. The sweep also recovers threads whose in-memory debounce state a gateway
  restart lost, judges everything said during quiet hours, and is the only reporting
  surface for arrival activity and budget alerts (the gateway has no `--deliver` and must
  not open a second outbound Slack path). PARITY already maps the sweep onto Claude Tag's
  *routine*, so keeping it is parity, not debt.
- **The rate is now chosen by whoever posts.** The 15-minute cadence was an implicit rate
  limit (≤96 judge calls/day, whatever anyone posted). Per-channel and global token
  buckets are the *replacement* for it, not an optimisation — see the README's residual
  risks.

### P0 — still not the same

1. ~~**Latency**~~ — see above. Trigger closed in code, dark by default, sweep retained.
2. **Capability breadth — OPEN, AND DELIBERATELY NOT SHIPPED.** A Claude Tag session
   *"reads documents, runs code, builds charts, and opens pull requests"* in a sandbox.
   Our ambient path is still one bounded, tool-less LLM call that emits one ≤200-character
   line of text — it cannot act, on either trigger.

   Arrival-time judging does **not** narrow this gap by a single tool, on purpose. The
   obvious way to narrow it — handing a judged thread to a full-toolset Hermes agent
   session ("escalation") — is **not implemented, not scaffolded, and not prepared for**:
   no config key, no hook, no dead code path, no test. A prior attempt was stopped by a
   safety review, and the reasoning still holds: autonomously routing
   attacker-controllable channel text into a session holding `terminal`, `execute_code`
   and `browser_*` is a **self-triggering code-execution surface**, and with arrival-time
   judging the trigger is now chosen by whoever posts rather than by our own cadence —
   which makes it *worse*, not better, than it would have been under the sweep. It stays
   closed pending **explicit, informed human consent to that specific design**, which
   nobody has given. Two further facts for whoever picks this up:
   `ctx.inject_message` **does not work in the gateway** at all
   (`hermes_cli/plugins.py:524-547` returns `False` when `_cli_ref is None`, which only
   the interactive CLI sets), and Hermes' **mention** path already provides the full
   toolset with a human explicitly asking for it — which is the consented version of this
   capability and is arguably more capable than Claude Tag's sandbox.
3. **Per-channel "Respond automatically" toggle**, changeable from the channel itself.
   We have a global `channels` allowlist, a global `arrival_enabled`, plus
   `hermes ambient mute`. If the per-channel toggle lands, `arrival_enabled` should
   **merge** into it rather than compose with it — two global booleans layered on an
   allowlist is one too many.

### P1 — context fidelity

4. **Baseline context — CLOSED 2026-08-12 (code), SHIPS DARK.** Spec: every session
   "reads its own thread and the channel's history, **including pinned items**".
   `aw_context.py` adds four labelled sections in a fixed priority order under ONE
   character ceiling: `[THIS THREAD]` (ledger, backfilled from `conversations.replies`),
   `[CHANNEL]` (`conversations.info` — name/topic/purpose), `[RECENT CHANNEL ACTIVITY]`
   (ledger first; `conversations.history` only when the ledger is thin), `[PINNED]`
   (`pins.list`).

   Honest scoping, because "CLOSED" is doing a lot of work in a heading:

   - **`context_enabled` defaults `false`.** Closed in this repo ≠ live on that machine.
     A deployed `config.json` without the `context_*` keys produces a **byte-identical**
     judge prompt, pinned by a golden-hash test.
   - **Pinned items are PARTIAL and blocked on a human.** `pins:read` is not in the
     granted bot token — verified: `slack-manifest.json`'s `oauth_config.scopes.bot` has
     17 entries and pins is not one — so they need an app-manifest change **plus a
     Reinstall to Workspace**. `context_pins` therefore defaults false, a `missing_scope`
     reply is cached for the process so it is never retried, the judge is told
     `context: pinned items unavailable`, and `aw_status.py` prints the remediation.
     Until an operator does that, this half of the spec line is **not met** — and it
     says so rather than failing quietly.
   - **We diverge on the window itself; see gap 6.**
   - **A live bug fell out of it.** A thread whose root row is absent — root predates the
     plugin, or `prune()` deleted it while replies continued — was structurally invisible
     to **both** triggers, forever, because `thread_roots()` selects `WHERE ts=thread_root`.
     Not "judged with poor context": never nominated. `prune()` no longer deletes a root
     under an active thread, and such threads are now nominated *provided* the root can be
     fetched and the root-is-bot loop-safety rung re-established from Slack itself. If it
     cannot be, the nominee is **dropped, not judged** (`declined-root-unknown`, which
     consumes no watermark, so the sweep retries).
   - **Not included, deliberately:** files/attachments (unbounded bytes, classic injection
     vector, `files:read` *is* granted), real names or Slack user ids (we pseudonymise to
     `A1`/`A2`; an id in a prompt invites an @-mention, and `sanitize_nudge` refuses every
     `@`), and custom emoji names outside our own fixed `ACK_REACTIONS` allowlist.
5. **Workspace search — STILL OPEN, reason upgraded from "we cannot" to "a bot token
   structurally cannot".** `search.messages` requires `search:read`, which is a **user**
   scope and is not grantable to a bot token — so this is not closeable with this app at
   all, no matter what we build. It is also the one addition that would breach the
   containment perimeter: it ingests text from channels nobody opted into, defeating
   "watched channels only". Not planned.
6. **50-message thread window — MIRRORED on two axes, DIVERGED on two.** Mirrored: other
   bots' replies are **filtered** (replaced by `(N bot message(s) omitted)`), and the
   **root is included** — that is what the backfill is for. Diverged, deliberately:

   - **Oldest-first → root + oldest + newest.** Their question is "a human just
     @mentioned me mid-thread, what is this about?", so the origin is the right answer and
     the session can read more later. Ours is "would an *uninvited* line help, or has this
     already resolved?" — and that answer lives in the last few messages ("never mind,
     found it", "thanks Bob"), which oldest-first systematically hides. We also get
     exactly one shot with no follow-up read, so we cannot spend the window on prologue.
   - **16 messages, not 50 — a money decision, not a fidelity one.** At a fixed 3000-char
     thread budget, count and per-message fidelity trade off directly: 50 messages is 60
     chars each (a fragment), 16 is ~190 (about two sentences, the minimum at which a
     Slack message still means something). Anthropic can afford 50 because their prompt is
     cached across a multi-turn session; ours is a one-shot call priced per judgment.

   Worth noting for the table: the spec's "50 messages" sentence is about *a mid-thread
   mention*, and our mention path is Hermes' own full-toolset session rather than the
   judge — so that line arguably belongs to a different lane of ours entirely.

### P2 — lifecycle semantics

7. **Edits — MATCH, verify on upgrade.** Hermes' adapter handles `message_changed` by
   replacing the event with `dict(updated_message)`, which carries the **original `ts`**
   (`slack/adapter.py:5275-5306`). Our `record_message` is `INSERT OR REPLACE` on
   `PRIMARY KEY (channel, ts)`, so an edit *updates* the existing row instead of adding a
   phantom reply. This is load-bearing: an autoincrement key would make editing your own
   question look like someone answered it, silently suppressing a legitimate nudge.
   There is a regression test to add here.
8. **Deletions — MATCH.** `subtype == "message_deleted"` returns immediately
   (`adapter.py:5369`), so no event reaches us. Spec: *"Deleting a reply: Claude gets no
   notification and keeps the version it already read."* Same behaviour, by inheritance.
   Not matched: *"deleting the thread's first message closes the session if there are no
   replies"* — we keep the ledger row.
9. **Progress surface.** Spec: a live checklist edited in place for longer tasks. We
   have Hermes' reaction ack and streaming, no checklist.

### P3 — surfaces

10. **Result forms** — file/chart attachments, a page kept current, a hosted page.
    Partial: files possible via Hermes, no "kept current" page.
11. **"Open session in Claude" link** — read-only record incl. every tool call. Hermes
    keeps transcripts but exposes no link.
12. **Channel-scoped memory.** Spec: public-channel memory is shared workspace-wide,
    private channels keep their own store, and anyone in the channel can read or correct
    it via `@Claude what do you remember about this channel?`. We have no memory feature;
    Hermes' own memory is not channel-scoped.
13. **Introspection** — `@Claude what can you access from this channel?` /
    `what do you remember about this channel?` / `what triggers do you have set up?`.
14. **Per-channel access scoping.** Spec: skills/plugins/instructions lock at thread
    start; connections are enforced per request. Hermes' toolsets are global.

## Deliberate divergences (not gaps)

- **No sandbox.** Claude Tag runs an ephemeral Anthropic-hosted sandbox per thread.
  Hermes runs on the operator's own machine — that is the entire point of self-hosting.
  Consequence: Claude Tag's isolation story is stronger, and ours has to be earned with
  the L1/L2/L3 containment model instead.
- **No cooldowns / daily nudge caps.** Claude Tag has neither; we deleted ours once the
  spend limit and the judge existed.
- **Bot-authored messages never trigger.** Claude Tag's documented failure mode is
  ignoring bot messages; for us that is a deliberate loop-safety rule. With arrival-time
  judging it is load-bearing rather than merely tidy: `decide()` returns `RECORD_SKIP` for
  bot messages too, so without the explicit bot rung in `arrival_key` our own posted nudge
  would re-trigger judgment on the thread it just landed in — and `has_intervention` would
  stop the second *post*, making the symptom silent wasted spend rather than a visible
  loop.
- **A 90-second politeness floor on the arrival path.** This is a measurable divergence
  from the spec: Claude Tag has no age gate, because it is a first-party product a channel
  opted into. Ours is an unsolicited line from a bot that is otherwise silent, we get
  exactly **one post per thread, ever**, and replying five seconds in spends that single
  shot on a thread a colleague was already typing an answer to. So
  `arrival_debounce_seconds` is deliberately *both* the debounce quiet period and the
  politeness floor — one number, because they are the same requirement — and it is a
  floor, not a wait: the trigger is arrival, not a tick. It is also free, since the
  debounce has to exist anyway to coalesce a burst. **90s is my judgment, not evidence**;
  the shadow soak can answer it empirically by logging how long after each arrival
  judgment a human replied anyway. Lowering it below 30s is refused at config load.
- **Context: root + oldest + newest, 16 messages, no workspace search.** Three named
  divergences from the spec's context model, each argued in gap 4/6 above rather than
  hidden in a constant: we take the two ENDS of a thread instead of its start (our
  question is "has this resolved?", not "what is this about?"), we take 16 messages
  instead of 50 (one-shot uncached prompt priced per judgment, so count trades against
  per-message fidelity), and we do not search the workspace at all (`search:read` is
  user-scope-only, and reading channels nobody opted into would defeat the watched-channel
  perimeter).
- **Pinned items are optional, not baseline.** Claude Tag reads them as a matter of course.
  For us they cost a manifest change plus a human reinstall, they are the stalest content
  in the design, and they are the least likely to change "does this thread need help right
  now" — so they are last in priority, default false, and their absence is reported.
- **Two triggers, partitioned by age.** Arrival owns
  `[arrival_debounce_seconds, min_age_minutes)`; the sweep owns `[min_age_minutes, ∞)`.
  `load_config` clamps `arrival_max_wait_seconds` strictly below `min_age_minutes * 60` to
  keep that true, so the two triggers cannot race over one thread's re-judge budget.
  Claude Tag has one trigger; we have two because our second one does a job (stalled
  threads, quiet-hours catch-up, restart recovery, ops reporting) the first cannot.
