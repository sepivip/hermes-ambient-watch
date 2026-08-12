# Social posts

## X (short)

Anthropic shipped Claude Tag — an AI teammate that lives in your Slack.

So I built an open-source one for @NousResearch's Hermes Agent. Self-hosted, your model, your machine.

It watches a channel, decides on its own whether it can help, and answers in-thread.

Cost per decision: $0.0045.

🧵

---

## X (thread continuation, optional)

2/ The hard part isn't answering. It's knowing when to shut up.

A regex that fires on "?" is a spam machine. So the decision is a model call: it scores confidence, explains itself, and stays silent by default.

Yesterday it declined to answer a resolved thread — with 0.99 confidence. That's the whole product.

3/ Guardrails that actually exist:
· USD spend caps — over budget = declined, not truncated
· once per thread, forever
· self-quiets in channels that ignore it
· quiet hours
· kill switch with no model in the path
· 12h staleness ceiling

4/ Security was the real work.

A full-toolset session read my plugin's internal state and quoted a channel excerpt back to a human. Nothing was exploited — but the path existed.

Fix: sanitize at the source, keep nothing untrusted at rest, and jail the data dir from every tool in every session.

5/ It can hand a thread to a full-power agent — code, files, browser, PRs.

But only when a human clicks a 🔎 reaction.

Autonomous handoff would let any Slack message start an unmetered shell session on my laptop. One click removes that entirely.

6/ Open source, MIT-ish, 310 tests.

Not at parity with Claude Tag and the README says exactly where: no sandbox, no channel memory, no workspace search.

Claude Tag is a better product. This one runs on your hardware.

github.com/sepivip/hermes-ambient-watch

---

## Facebook / LinkedIn (longer)

**I built an open-source Claude Tag for Hermes Agent.**

Anthropic recently shipped Claude Tag: tag @Claude in a Slack channel and it works alongside your team — reads the thread, uses your tools, replies in place. It's excellent, and it's Enterprise/Team only, hosted by them.

I wanted that on my own hardware, on my own model. So I built it as a plugin for Nous Research's Hermes Agent.

**What it does**

It sits in a Slack channel and says nothing. When someone asks something, it decides for itself whether it can help — and if it can, it answers in the thread. Nobody has to tag it.

A real one from this morning:

> **me:** Can anyone tell me what the biggest state in the US is?
> **bot:** Alaska is the largest U.S. state by area; California is largest by population.

Two minutes from question to answer, unprompted. It cost less than half a cent.

**The interesting problem wasn't answering**

It was knowing when to stay quiet. My first version decided using a regex — does the message contain a question mark? That's a spam machine. It would nag people about threads they'd already resolved.

Now a model makes the call: it scores its own confidence, writes down why, and defaults to silence. Yesterday it looked at a thread someone had already answered and declined to post — with 0.99 confidence. It was more certain about shutting up than it usually is about speaking. That's the feature.

**What went wrong along the way**

Three times I was confident about a safety control that turned out not to exist:

- a guard I'd written, tested and cited couldn't ever run — the tool it protected isn't available in that context
- a "locked-down" session I'd described actually had full shell access
- deleting a file didn't remove the sensitive data, because it had already been copied into a permanent search index

None of them were caught by reading my own code. They were caught by reading the *platform's* source and its live database. Writing a mitigation and verifying one are different jobs.

The real incident: a normal agent session, with full tools, went looking through past sessions on its own, found my plugin's internal file, read it, and quoted a channel message back to a human. Nothing was exploited — every message involved was my own test text. But the whole path existed. The fix has three layers, and the load-bearing one is neutralizing untrusted text at the moment it's created, because by the time it's on disk it's already been indexed forever.

**The line I didn't cross**

It can hand a thread to a full-power agent session — real tools, code, files, browser, pull requests. That's what turns "writes a sentence" into "does the work."

But only when a human adds a 🔎 reaction.

I'd originally planned for it to decide that itself. Then the numbers landed: one such session can run 500 model iterations — $50–150 — my spend caps can't see any of it, and there's no sandbox, so the blast radius is my laptop. Any message in that channel could have started an unmetered shell session on my machine.

Claude Tag can do it autonomously because it has four things I don't: a disposable cloud sandbox per thread, spend limits that actually refuse work, a proxy that default-denies outbound traffic and never hands the model a real credential, and per-channel permission scoping. They built the box first.

One emoji click gives me the same capability and removes the entire risk. That felt like the honest trade.

**Where it stands**

310 tests. Running live. The repo includes an audit measuring it against Anthropic's own documentation, and it's blunt about the gaps: no sandbox, no channel-scoped memory, no workspace search, no code execution in the ambient path.

Claude Tag is the better product. This one runs on your hardware, with your model, for half a cent a decision.

github.com/sepivip/hermes-ambient-watch
