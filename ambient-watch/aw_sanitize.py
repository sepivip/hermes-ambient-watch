"""L1 of the containment design: neutralize untrusted channel text.

TWO DIRECTIONS, TWO PROFILES. Since the judge landed, untrusted text moves
both ways and each direction needs its own sanitizer:

* INBOUND — ``build_judge_view`` builds what the judge model reads. It is
  richer than the export profile (12 messages x 400 chars, pseudonymous
  authors, relative timestamps) because it is never persisted by Hermes:
  it lives in this process's memory and in one HTTPS request body. The
  export profile's tight caps existed because every downstream copy was
  permanent; that rationale does not apply here. Instruction-shaped text is
  still WITHHELD — the judge's output is text we post into a public
  channel, so an injected judge is the worst case in the whole system.
* OUTBOUND — ``sanitize_nudge`` gates what the judge wrote before it is
  posted. The nudge is model-authored but attacker-INFLUENCED; without this
  the judge is a laundering path (hostile text in, relayed text out, posted
  by us). It fails closed: anything suspicious returns None, which the gate
  turns into silence rather than a post.
* ``build_excerpt`` remains the compact export profile, stored in the
  judgments ledger so an operator reviewing a shadow soak in a terminal can
  see the thread next to the nudge it would have produced.

Every excerpt the sweep sees is VERBATIM text an attacker chose. Any
consumer of it may be a full-toolset agent session — the live 2026-08-11
incident proved that, and Hermes' own promptware defence does not cover
the routes it arrived by (``_UNTRUSTED_TOOL_NAMES`` in
agent/tool_dispatch_helpers.py:584 lists ``web_extract``/``web_search``
plus the ``browser_``/``mcp_`` prefixes — not ``read_file``, not
``session_search``). So the plugin must do it itself, at the point of
creation, because that is the only place that also protects the copies
Hermes persists and FTS-indexes forever in its shared session store.

Two guarantees, in order of strength:

1. STRUCTURAL — the payload cannot forge its own container. Every
   character that could close our delimiter, terminate cron's ```
   script-output fence (cron/scheduler.py:2641), open a JSON/markdown
   scaffold, or spoof a bracketed protocol marker such as ``[SILENT]`` is
   removed before the delimiters are attached. There is no escaping
   convention to get wrong: the characters simply cannot occur.
2. HEURISTIC — instruction-shaped text is withheld rather than forwarded.
   Detection runs on the raw text AND on the cleaned text, so smuggling a
   pattern past it with stripped characters or zero-width joiners
   (``ign​ore previous instructions``) trips it instead of evading
   it. A redacted candidate still nudges; it just carries no quote.

Layer 2 is bypassable in principle, which is why the durable-file and
tool-jail layers exist. Layer 1 is what keeps a bypass from mattering to
anything except the one sweep that reads it.
"""

from __future__ import annotations

import re

# Angle brackets are removed from the payload, so these tags are
# unforgeable from a channel — the delimiter is a real boundary, not a
# convention the text is trusted to respect.
DELIM_OPEN = "<untrusted-slack-text>"
DELIM_CLOSE = "</untrusted-slack-text>"

# Brackets are removed from the payload too, so these markers cannot be
# spoofed either (nor can cron's own ``[SILENT]`` control token).
REDACTED = "[redacted: instruction-shaped channel text withheld by ambient-watch]"
EMPTY = "[no quotable text]"

MAX_MESSAGES = 4
MAX_MESSAGE_CHARS = 120
MAX_EXCERPT_CHARS = 480

# Inbound (judge) profile. Bigger because nothing persists it: the view is a
# local variable and a request body, never a Hermes message row.
JUDGE_MAX_MESSAGES = 12
JUDGE_MAX_MESSAGE_CHARS = 400
JUDGE_MAX_VIEW_CHARS = 2400

# Outbound (nudge) profile — one short sentence, posted publicly.
NUDGE_MAX_CHARS = 200

# Structure-forging characters. `<>` = our delimiters and Slack's link /
# mention syntax; backtick = cron's script-output fence and shell command
# substitution; `{}[]` = JSON, markdown links, and bracketed protocol
# markers; `\$` = shell/PowerShell expansion; `*_~^` = markdown emphasis
# used to fake headings and emphasis in the prompt.
_FORGERY_CHARS = "<>`{}[]\\$*_~^"

# Invisible smuggling: zero-width joiners, bidi overrides, BOM, soft hyphen.
_INVISIBLE = "".join(
    chr(c)
    for c in (
        0x00AD, 0x200B, 0x200C, 0x200D, 0x200E, 0x200F,
        0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
        0x2060, 0x2066, 0x2067, 0x2068, 0x2069, 0xFEFF,
    )
)

_REMOVE = {ord(c): None for c in _FORGERY_CHARS + _INVISIBLE}
_TO_SPACE = {c: " " for c in range(0x20)}
_TO_SPACE[0x7F] = " "

_URL = re.compile(r"\b(?:[a-z][a-z0-9+.\-]{1,15}://|www\.)\S*", re.IGNORECASE)
_WS = re.compile(r"\s+")

# Instruction-shaped text. Deliberately trigger-happy: a false positive
# costs one quote in one nudge, a false negative hands an imperative to a
# session holding terminal/write_file/cronjob.
_INJECTION = re.compile(
    "|".join(
        (
            r"\b(?:ignore|disregard|forget|override)\b[^.]{0,30}\b"
            r"(?:instruction|prompt|rule|direction|context|above|previous|prior|earlier)",
            r"\bsystem\s*(?:prompt|message|instruction)",
            r"\bnew\s+(?:instruction|rule|task|persona)",
            r"\byou\s+are\s+now\b|\bact\s+as\b|\bpretend\s+to\s+be\b|\bas\s+an?\s+ai\b",
            r"\b(?:when|before|after|while)\s+(?:you\s+)?"
            r"(?:summariz|report|respond|repl|answer|process|read|post)\w*\b[^.]{0,40}"
            r"\b(?:run|execute|call|invoke|use|send|post|write|fetch|delete|upload)\b",
            r"\b(?:send_message|read_file|write_file|patch|search_files|execute_code|"
            r"terminal|process|cronjob|delegate_task|session_search|memory|"
            r"browser_\w+|web_extract|web_search|vision_analyze)\b",
            r"\brm\s+-rf\b|\bcurl\b|\bwget\b|\bpowershell\b|\bcmd\s*/c\b|"
            r"\binvoke-webrequest\b|\bbase64\s+-{1,2}d\b|\bnc\s+-e\b|\bchmod\s+\+x\b",
            r"\.env\b|\bauth\.json\b|\bstate\.db\b|\bid_rsa\b|\bcredentials?\b|"
            r"\bplugin-data\b|\bambient_watch\b|\bAGENTS\.md\b|\bCLAUDE\.md\b",
            r"\bapi[_\- ]?key\b|\bsecret\s+key\b|\baccess\s+token\b|"
            r"\bbearer\s+[A-Za-z0-9._\-]{8,}|\bsk-[A-Za-z0-9]{12,}|"
            r"\bxox[baprs]-[A-Za-z0-9\-]{8,}",
            r"\bprompt\s+injection\b|\bexfiltrat|\bjailbreak\b",
        )
    ),
    re.IGNORECASE,
)


def _clean(raw: str) -> str:
    """Reduce text to one inert line that cannot forge structure."""
    s = raw.translate(_TO_SPACE).translate(_REMOVE)
    s = _URL.sub(" (link removed) ", s)
    s = s.replace("|", "/")  # our own field separator
    return _WS.sub(" ", s).strip()


def is_instruction_shaped(text: str) -> bool:
    """True when ``text`` reads as an imperative aimed at an agent.

    Checked on both the raw and the de-obfuscated form so that inserting
    stripped characters mid-pattern trips the detector instead of evading
    it.
    """
    raw = text or ""
    return bool(_INJECTION.search(raw) or _INJECTION.search(_clean(raw)))


def neutralize(text: str, max_chars: int = MAX_MESSAGE_CHARS) -> str:
    """One channel message -> inert, capped, single-line text (or REDACTED)."""
    cleaned = _clean(text or "")
    if not cleaned:
        return ""
    if is_instruction_shaped(text or "") or is_instruction_shaped(cleaned):
        return REDACTED
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].rstrip() + "..."
    return cleaned


def build_excerpt(texts) -> str:
    """Join neutralized messages and seal them in unforgeable delimiters.

    The delimiters are attached LAST, after every ``<``/``>`` has been
    removed from the payload — that ordering is the whole guarantee.
    """
    parts = [n for n in (neutralize(t) for t in list(texts)[:MAX_MESSAGES]) if n]
    body = " / ".join(parts) if parts else EMPTY
    if len(body) > MAX_EXCERPT_CHARS:
        body = body[:MAX_EXCERPT_CHARS].rstrip() + "..."
    # Re-check the join: a pattern split across two messages only becomes
    # visible once they are concatenated.
    if _INJECTION.search(body.replace(REDACTED, "")):
        body = REDACTED
    return f"{DELIM_OPEN}{body}{DELIM_CLOSE}"


def _relative(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 3600:
        return f"+{int(seconds // 60)}m"
    if seconds < 86400:
        return f"+{int(seconds // 3600)}h"
    return f"+{int(seconds // 86400)}d"


def build_judge_view(messages) -> str:
    """Build the INBOUND view the judge model reads.

    ``messages`` may be store rows (dicts with ts/author/is_bot/text) or
    bare strings. Authors become stable pseudonyms (``A1``, ``A2``, ``BOT``)
    rather than Slack user ids — an id in the prompt is an invitation for the
    model to @-mention someone, and the nudge must never do that. Times
    become relative offsets, so the view carries no absolute identifiers at
    all.

    The delimiters are attached LAST, after every ``<``/``>`` is gone, so a
    channel cannot forge its own container. Same guarantee as
    ``build_excerpt``; only the caps differ.
    """
    rows = list(messages or [])
    if not rows:
        return f"{DELIM_OPEN}{EMPTY}{DELIM_CLOSE}"

    # Root plus the most recent tail: the two ends of a thread are what a
    # judgment actually needs.
    if len(rows) > JUDGE_MAX_MESSAGES:
        rows = rows[:1] + rows[-(JUDGE_MAX_MESSAGES - 1):]

    base = None
    for row in rows:
        if isinstance(row, dict):
            try:
                base = float(row.get("ts"))
                break
            except (TypeError, ValueError):
                continue

    aliases: dict = {}
    lines = []
    for row in rows:
        if isinstance(row, dict):
            text = row.get("text")
            author = row.get("author")
            if row.get("is_bot"):
                who = "BOT"
            elif author is None:
                who = "A?"
            else:
                who = aliases.setdefault(author, f"A{len(aliases) + 1}")
            when = ""
            try:
                if base is not None:
                    when = " " + _relative(float(row.get("ts")) - base)
            except (TypeError, ValueError):
                when = ""
        else:
            text, who, when = row, "A1", ""
        body = neutralize(text, JUDGE_MAX_MESSAGE_CHARS)
        if body:
            lines.append(f"{who}{when}: {body}")

    view = "\n".join(lines) if lines else EMPTY
    if len(view) > JUDGE_MAX_VIEW_CHARS:
        view = view[:JUDGE_MAX_VIEW_CHARS].rstrip() + "..."
    # A pattern can be split across two messages and only appear once they
    # are concatenated — same join re-check as the export profile.
    if _INJECTION.search(view.replace(REDACTED, "")):
        view = REDACTED
    return f"{DELIM_OPEN}\n{view}\n{DELIM_CLOSE}"


def sanitize_nudge(text: str):
    """Gate the OUTBOUND nudge. Returns clean text, or None to stay silent.

    The judge's wording is model-authored but attacker-influenced, and it is
    about to be posted into a public channel under our name. Every check
    fails closed, because the cost asymmetry is extreme: rejecting a good
    nudge costs one skipped nudge, relaying a bad one is a public post we
    cannot undo.
    """
    raw = text or ""
    cleaned = _clean(raw)
    if not cleaned:
        return None
    if is_instruction_shaped(raw) or is_instruction_shaped(cleaned):
        return None
    # _clean defangs URLs to a marker; its presence means there WAS a link.
    if "(link removed)" in cleaned:
        return None
    # `<@U…>` loses its brackets in _clean but keeps the '@' — so this one
    # check covers mentions, handles and e-mail addresses.
    if "@" in cleaned:
        return None
    if REDACTED in cleaned or EMPTY in cleaned:
        return None
    if len(cleaned) > NUDGE_MAX_CHARS:
        return None  # refuse rather than truncate mid-sentence
    return cleaned
