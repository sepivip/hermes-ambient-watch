# Parity audit vs Claude Tag

Measured against Anthropic's own spec, not against our design doc:
- https://claude.com/docs/claude-tag/concepts/how-it-works
- https://claude.com/docs/claude-tag/users/when-claude-responds
- https://claude.com/docs/claude-tag/users/proactivity

Verified 2026-08-11 against hermes-agent v0.20.0 source and the live install.
**Verdict: the session/threading model matches. The behaviour does not yet —
we nudge, Claude Tag answers.**

## Matches

| Claude Tag mechanic | Ours | Evidence |
|---|---|---|
| "Typing `@Claude` … is what starts a working session" | mention → `RECORD_PASS` → Hermes session | live T3 |
| "Once a session is active in a thread, it belongs to everyone there" | `thread_sessions_per_user` defaults **False** → threads shared, `[sender name]` prefixes, "Multi-user thread" prompt note | `gateway/session.py:1119-1123` |
| "Two threads in the same channel are two separate sessions" | `thread_id` participates in the session key | `build_session_key` |
| "The thread is durable; the sandbox is not" | Hermes sessions persist in sqlite; no sandbox to lose | `sessions` table |
| "A routine runs the same loop on a schedule" | `--no-agent` cron sweep runs the same detect→judge→post path in one process | `gate.py` |
| Agent identity — acts under its own account, not the asker | posts as the bot user | live |
| DM → your own connectors, attributed to you | DMs are passed straight through to Hermes | `aw_recorder.decide` returns `PASS` for `chat_type=="dm"` |
| Result lands in the asking thread | `aw_post` posts with `thread_ts`, `reply_broadcast=False` | live T7 |
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

### P0 — still not the same

1. **Latency: we wait, Claude Tag doesn't.** Claude Tag replies *"to channel messages it
   judges warrant a reply"* as they arrive. Our judge only ever sees a thread that has
   already gone quiet for `min_age_minutes`. The judgment is now equivalent; the *trigger*
   is not. Closing this means judging on message arrival (debounced, budget-gated) rather
   than on a 15-minute sweep.
2. **Capability breadth.** A Claude Tag session *"reads documents, runs code, builds
   charts, and opens pull requests"* in a sandbox. Our ambient path is one LLM call that
   emits one line of text — it cannot act. (Hermes' **mention** path has the full toolset
   and is arguably more capable, but that is Hermes, not our ambient loop.)
3. **Per-channel "Respond automatically" toggle**, changeable from the channel itself.
   We have a global `channels` allowlist plus `hermes ambient mute`.

### P1 — context fidelity

4. **Baseline context.** Spec: every session "reads its own thread and the channel's
   history, **including pinned items**" and "searches the workspace's content". We hand
   Hermes the single message; it does not read channel history or pinned items.
5. **Workspace search.** Claude Tag can find messages by keyword in public channels it
   is *not* a member of. We cannot see outside watched channels at all.
6. **50-message thread window.** Spec: a mid-thread mention gets up to 50 messages from
   the thread root, oldest-first, other bots filtered. Unimplemented and unverified.

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
  ignoring bot messages; for us that is a deliberate loop-safety rule.
