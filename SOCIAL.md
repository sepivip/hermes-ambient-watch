# Social post

One paragraph. Pick whichever fits the platform.

## LinkedIn (long form)

**I built an open-source Claude Tag for Hermes Agent. The hard part wasn't answering questions — it was teaching it when to stay quiet.**

Anthropic recently shipped Claude Tag: an AI teammate that lives in your Slack channels, reads threads, and helps without being asked. It's very good, and it's hosted by them, on their models, for Enterprise and Team plans.

I wanted that on my own infrastructure, on my own model. So I built it as a plugin for Nous Research's Hermes Agent, over a couple of days. It's live, it's open source, and it works.

**What it does**

It sits in a Slack channel and says nothing. When someone asks something, it decides for itself whether it can actually help. If it can, it answers in the thread. Nobody tags it.

A real exchange from testing:

> **Me:** Can anyone tell me what the biggest state in the US is?
> **Bot:** Alaska is the largest U.S. state by area; California is largest by population.

Two minutes, unprompted, and it disambiguated area from population without being asked.

**Why this matters for teams**

Every team channel has the same quiet failure: someone asks a real question, nobody who knows is online, and the question scrolls away. Nobody decides to ignore it — it just dies. Multiply that across a year and it's a meaningful amount of blocked work that never shows up in any metric, because the cost is invisible by construction.

The reason nobody has fixed it with a bot is that the naive version is intolerable. A bot that replies to everything containing a question mark is spam, and a team will mute it inside a day. The whole problem is restraint.

**How it works**

Four stages, and the interesting one is second:

1. **Observe.** It records messages in allowlisted channels and answers none of them. Zero cost, no model involved.
2. **Judge.** A model — not a regex — decides whether an uninvited reply would genuinely help, scores its own confidence, and defaults to silence. This is the entire product.
3. **Act.** Only a confident yes posts, once per thread, ever.
4. **Govern.** Hard spend caps that decline work rather than truncate it, quiet hours, self-quieting in channels that ignore it, and a kill switch with no model in the path.

My first version used a regex: does the message contain "?". It would have nagged people about threads they'd already resolved. Replacing it with a judgment call is what made the difference, and the improvement is measurable rather than vibes:

> **Thread:** "who owns the deploy runbook?" → "I do"
> **Verdict:** stay quiet, confidence 0.99 — *"Question was answered by the same participant"*

> **Thread:** "why was it late?"
> **Verdict:** stay quiet, confidence 0.99 — *"No context to determine what was late or why"*

It was more certain about staying quiet than it usually is about speaking. That's the behaviour you want in something that lives in your team's channel uninvited.

**The decision I'm most confident about**

It can hand a thread to a full-power agent — one that reads code, runs commands, opens pull requests. That turns "writes a helpful sentence" into "does the work."

It only does that when a human clicks a 🔎 reaction.

I originally planned to let it decide for itself. Then I did the arithmetic: one such session can run up to 500 model iterations, my spend caps structurally cannot see it because it runs in a separate process, and there's no sandbox — it executes on my own machine. Any message in that channel could have started an unmetered process with shell access.

Anthropic can afford autonomy there because they built four things I haven't: a disposable cloud sandbox per thread, spend limits that refuse work, a proxy that default-denies outbound traffic and never hands the model a real credential, and per-channel permission scoping. They built the box first.

One emoji click gives me the same capability and removes the entire risk. If you're deploying agents inside a company, that trade — a second of human friction for a category of risk removed — is usually available and usually worth taking.

**How do you test whether an AI has good judgment?**

You can unit-test whether code posts to the right Slack thread. You cannot unit-test taste, and taste is the product.

I learned this the expensive way. I switched to a cheaper model to save money, confirmed it returned valid output, and shipped it. It quietly stopped answering anything useful for an hour — it had started reading polite phrasing like "can anyone tell me…" as an instruction aimed at itself. I'd verified the format and called it a quality check.

So I built an eval: sixteen labelled scenarios, most of them real verdicts from production, run against the live model, scored on the two failure modes separately — because they aren't symmetric. Answering when unwanted spends the one post that thread will ever get. Staying silent when you could help is invisible and merely useless.

That comparison now takes 39 seconds and fails loudly. It would have caught the regression before it shipped.

**Where it honestly stands**

394 tests, running live, open source. The README is blunt about what Claude Tag still does better: it runs each thread in a disposable sandbox, remembers per channel across sessions, and searches the whole workspace. Ours does none of that.

Claude Tag is the better product. This one runs on your hardware, with your model, for roughly half a cent of modelled usage per decision.

👉 github.com/sepivip/hermes-ambient-watch

---

## Main

Anthropic shipped Claude Tag — an AI teammate that lives in your Slack — so I built an open-source one for Nous Research's Hermes Agent: self-hosted, your model, your machine. It sits in a channel saying nothing, decides for itself whether it can help, and answers in-thread without being tagged. The hard part wasn't answering, it was knowing when to shut up: a regex on "?" is a spam machine, so the call is a model call that scores its own confidence and defaults to silence — yesterday it declined to answer an already-resolved thread with 0.99 confidence, which is really the whole product. Roughly half a cent of modelled usage per decision, hard spend caps, once per thread, and it can hand a thread to a full-power agent with code and file access, but only when a human clicks a 🔎. 310 tests, and the README is blunt about what Claude Tag still does better.

👉 github.com/sepivip/hermes-ambient-watch

## Shorter (X, if the above runs long)

Anthropic shipped Claude Tag, so I built an open-source one for Hermes Agent — self-hosted, your model, your machine. It watches a Slack channel, decides on its own whether it can help, and answers in-thread untagged. The hard part wasn't answering, it was knowing when to shut up: yesterday it declined an already-answered thread with 0.99 confidence, which is really the whole product. About half a cent of modelled usage per decision, hard spend caps, and it only hands work to a full-power agent when a human clicks 🔎.

👉 github.com/sepivip/hermes-ambient-watch

## Alternative angle (leads with the security lesson)

I spent a day building an open-source Claude Tag for Hermes Agent — an AI teammate that watches a Slack channel and answers unprompted when it judges it can help. It works, for roughly half a cent of modelled usage per decision. But the memorable part was being confidently wrong three times about safety controls that didn't exist: a guard I'd written and cited could never run, a "locked-down" session actually had full shell access, and deleting a sensitive file didn't remove the data because it was already in a permanent search index. None of that came from reading my own code — it came from reading the platform's source and its live database. Writing a mitigation and verifying one are different jobs.

👉 github.com/sepivip/hermes-ambient-watch
