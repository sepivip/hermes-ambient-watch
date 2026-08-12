# Decisions

Binding choices, with the reasoning, so they are not silently re-litigated by a
later change. Newest first.

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
