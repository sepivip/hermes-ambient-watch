# Social post

One paragraph. Pick whichever fits the platform.

## Main

Anthropic shipped Claude Tag — an AI teammate that lives in your Slack — so I built an open-source one for Nous Research's Hermes Agent: self-hosted, your model, your machine. It sits in a channel saying nothing, decides for itself whether it can help, and answers in-thread without being tagged. The hard part wasn't answering, it was knowing when to shut up: a regex on "?" is a spam machine, so the call is a model call that scores its own confidence and defaults to silence — yesterday it declined to answer an already-resolved thread with 0.99 confidence, which is really the whole product. Roughly half a cent of modelled usage per decision, hard spend caps, once per thread, and it can hand a thread to a full-power agent with code and file access, but only when a human clicks a 🔎. 310 tests, and the README is blunt about what Claude Tag still does better.

👉 github.com/sepivip/hermes-ambient-watch

## Shorter (X, if the above runs long)

Anthropic shipped Claude Tag, so I built an open-source one for Hermes Agent — self-hosted, your model, your machine. It watches a Slack channel, decides on its own whether it can help, and answers in-thread untagged. The hard part wasn't answering, it was knowing when to shut up: yesterday it declined an already-answered thread with 0.99 confidence, which is really the whole product. About half a cent of modelled usage per decision, hard spend caps, and it only hands work to a full-power agent when a human clicks 🔎.

👉 github.com/sepivip/hermes-ambient-watch

## Alternative angle (leads with the security lesson)

I spent a day building an open-source Claude Tag for Hermes Agent — an AI teammate that watches a Slack channel and answers unprompted when it judges it can help. It works, for roughly half a cent of modelled usage per decision. But the memorable part was being confidently wrong three times about safety controls that didn't exist: a guard I'd written and cited could never run, a "locked-down" session actually had full shell access, and deleting a sensitive file didn't remove the data because it was already in a permanent search index. None of that came from reading my own code — it came from reading the platform's source and its live database. Writing a mitigation and verifying one are different jobs.

👉 github.com/sepivip/hermes-ambient-watch
