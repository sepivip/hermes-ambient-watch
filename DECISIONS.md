# Decisions

Binding choices, with the reasoning, so they are not silently re-litigated by a
later change. Newest first.

---

## 2026-08-12 — The USD figures are MODELLED, not billed. Say so.

`auth.json` shows the only configured provider is `openai-codex` with
`auth_mode: chatgpt` — a ChatGPT **subscription** OAuth, not a metered API key.
There is no second provider with credentials.

So `aw_budget`'s dollars are computed from our own price table
(`aw_budget._DEFAULT_PRICE`, `cfg.prices`) applied to reported token counts.
Under subscription auth **no per-token charge occurs**; the real constraint is
plan quota and provider rate limits. "$0.0045 per decision" is a *modelled cost*,
not money observed leaving an account.

**This does not make the budget useless** — it is still the throughput governor
that decides when to decline, and it is the only limiter that exists. It just
means the unit is notional here, and it would become literal the moment an API
key is configured for a metered provider.

**Consequence for public claims:** anything we publish must say "roughly half a
cent of modelled usage" or similar, never "costs half a cent". I had written the
stronger claim in `SOCIAL.md` before checking `auth_mode`; corrected. This is the
fourth time in this project that a confident claim about the environment turned
out to need verifying against the environment itself.

---

## 2026-08-12 — Judge pinned to the smallest available model

`auxiliary.ambient_watch_judge` → `openai-codex / gpt-5.4-mini` (the smallest of
the nine models this provider exposes). Judgment now runs on every eligible
message rather than 96 times a day, and channel context will grow the prompt, so
it should not sit on the flagship.

Safe to try because the judge is fail-closed: a model that cannot hold the JSON
schema yields silence, not nonsense — a bad pin shows up as "ambient went quiet",
never as a bad post. Verified live: `gpt-5.4-mini` returned a well-formed verdict
at `conf=0.98`. Revert to `gpt-5.6-sol` if verdict quality drops.

---

## 2026-08-12 — Mirror Claude Tag's thread window exactly

**Decision (operator).** The judge's thread context follows Anthropic's spec
verbatim rather than diverging:

> "Mentioning `@Claude` partway into an existing thread gives it up to **50
> messages from the start of the thread** (the root plus the oldest replies,
> with other bots' replies filtered out)."
> — https://claude.com/docs/claude-tag/concepts/how-it-works

So: **root + oldest replies, cap 50, other bots' replies filtered out.** Not
newest-first.

**Why this is right, including the part I initially argued against.** I had
framed oldest-first as the odd choice — for judging "does this thread need
help?" the *newest* messages seem like what matters. Two things outweigh that:

1. **Prompt-cache stability.** An oldest-first window is a *stable prefix*: it
   does not change as the thread grows, so the cached prompt prefix survives
   across judgments of the same thread. Newest-first invalidates the cache on
   every new message. Given that spend is our only real limiter, and that we
   re-judge a thread when new human activity arrives, this is a direct cost
   argument and probably the reason Anthropic chose it.
2. **The root establishes what the thread is about.** A mid-thread window
   without the root reads as fragments. The ask usually lives in the root; the
   newest message is often "any update?", which is only meaningful with the
   setup attached.

**Consequence to watch.** In a long thread the most recent messages can fall
outside the window — Anthropic documents exactly this ("In long threads, the
most recent messages before your mention can fall outside that window, so
restate anything critical"). Our threads are short today because a thread only
becomes a candidate with few messages, so the truncation is mostly theoretical.
If that changes, the fix is to keep root + oldest **and** append the last N,
not to flip the ordering.

**Also mirrored:** other bots' replies are filtered out of the window. We
already never treat bot messages as triggers; this extends the same rule to
context.

---

## 2026-08-12 — Escalation requires a human reaction, always

Handing a thread to a full-toolset Hermes session is gated on a human adding a
configured reaction to one of **our own** nudges. Autonomous escalation is not
built and will not be added without a separate, explicit, informed decision.

Reasoning: one escalated session runs up to `HERMES_MAX_ITERATIONS=500` model
iterations (order $50–150 against $0.0045 for a judge call), `aw_budget`
structurally cannot see that spend because the session runs in another process,
Hermes has no per-session dollar cap, and there is no sandbox — the blast radius
is the operator's own machine. Claude Tag can afford autonomy because it has
four layers we do not: an ephemeral hosted sandbox per thread, spend limits that
decline work, Agent Proxy default-deny egress with credentials injected at the
proxy, and per-channel access bundles. Until an OS-level boundary exists here
(Hermes' own SECURITY.md §2.2: "the only security boundary against an
adversarial LLM is the operating system"), the human click is what stands in for
those layers.

The path to earning autonomy later is to build the box first: run escalated
sessions on a non-default Docker terminal backend, plus a hard iteration cap.

---

## 2026-08-12 — `reaction_triggers: true`, never a list

Hermes' `slack.reaction_triggers` bool form limits reaction routing to messages
**the bot itself posted**; the list form enables it on **any** message in any
channel the bot can see. Our escalation anchor already requires the reaction to
land on our own nudge, so the bool form is exactly the needed scope and the list
form would only widen the surface for no gain.

---

## 2026-08-11 — No cooldowns, no per-day nudge caps

Deleted once the spend limit and the model judge existed, because Claude Tag has
neither. They were proxies for the two things we were missing. What remains are
the controls Claude Tag itself has: once per thread, self-quiet after N ignored
nudges, quiet hours, a kill switch, and the spend limit — plus a staleness
ceiling, which Claude Tag does not need because its routines only fire on
change.
