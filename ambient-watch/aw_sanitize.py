"""L1 of the containment design: neutralize untrusted channel text.

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


def neutralize(text: str) -> str:
    """One channel message -> inert, capped, single-line text (or REDACTED)."""
    cleaned = _clean(text or "")
    if not cleaned:
        return ""
    if is_instruction_shaped(text or "") or is_instruction_shaped(cleaned):
        return REDACTED
    if len(cleaned) > MAX_MESSAGE_CHARS:
        cleaned = cleaned[:MAX_MESSAGE_CHARS].rstrip() + "..."
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
