"""Tool guard: the data-directory jail (L3) plus send_message target pinning.

L3 — DATA-DIRECTORY JAIL
------------------------
No agent session may reference the ambient data directory from any tool
call. There is deliberately **no principal exemption**, not even for the
cron sweep: once the gate hands its payload over on stdout (see gate.py),
the sweep has no reason to touch the directory either, so the rule can be
absolute. That is what makes it structural — the alternative, a
``session_id.startswith("cron_")`` carve-out, would admit every cron job on
the machine and would still be a string comparison an attacker can aim at.

The jail matters because the directory holds two stores of verbatim,
attacker-controllable Slack text: the recorder's ``ambient.db`` ledger
(kept raw on purpose — the detectors run SQL over it) and, historically,
``candidates.json``. On 2026-08-11 a normal Slack gateway session with the
full core toolset read the latter on its own initiative and quoted it to a
human.

Matching is by marker substring on a normalized string, over every string
in the argument tree, for EVERY tool name. An allowlist of tool names is
precisely the mistake ``_UNTRUSTED_TOOL_NAMES`` makes upstream
(agent/tool_dispatch_helpers.py:584) — it silently omits whatever is added
next. Markers include the distinctive directory segments, the artifact
basenames (the cron job's ``--workdir`` IS the data dir, so a relative
``candidates.json`` needs no path at all), and 8.3 short-name forms.

L2 — SEND_MESSAGE TARGET PINNING
--------------------------------
Real send_message target grammar (tools/send_message_tool.py:221, :365):
``platform``, ``platform:chat_id``, or ``platform:chat_id:thread_id`` —
platform prefix FIRST. A Slack thread post is ``slack:C…:<thread_ts>``.
Armed intents are stored as bare refs ``C…:<thread_ts>``.

While intents are (or ever were) armed, sends INTO a watched channel must
match a pending intent; ambiguous name-form slack targets are blocked
outright (fail closed — a name can resolve into a watched channel after
directory lookup, evading an ID comparison).

NOTE: ``send_message`` is not in Hermes' core agent toolset on this build,
so the pinning half is dormant in practice and must not be mistaken for a
containment control. The jail above is the one that fires.
"""

from __future__ import annotations

_ID_PREFIXES = ("C", "G")

# Distinctive path fragments of the ambient data directory. `plugin-data`
# is included on purpose: nothing an agent legitimately does needs to reach
# into any plugin's private state through a tool.
_DIR_MARKERS = (
    "plugin-data/ambient_watch",
    "ambient_watch",
    "plugin-data",
    # Windows 8.3 short-name forms of the two segments above.
    "ambien~1",
    "plugin~1",
    "plugin~2",
)

# Bare artifact names — the sweep's workdir is the data dir, so relative
# references never contain a directory segment.
_ARTIFACT_MARKERS = (
    "ambient.db",
    "candidates.json",
    "gate_errors.log",
    "config.json.lkg",
)

JAIL_MESSAGE = (
    "ambient-watch: blocked {tool} — the ambient-watch data directory is a "
    "containment boundary and is not reachable from any tool call, in any "
    "session. It stores VERBATIM untrusted Slack text from watched channels; "
    "a full-toolset session reading it is the exact incident this jail "
    "exists to prevent. The cron sweep receives its candidates on the gate's "
    "stdout and needs no file access. Operators: run `python aw_status.py` "
    "in a terminal instead."
)

# Keep the scan cheap — it runs on every tool call in every session.
_MAX_SCAN_CHARS = 20_000
_MAX_DEPTH = 6


def _norm(value: str) -> str:
    return (
        value.replace("\\", "/")
        .replace("%5c", "/")
        .replace("%5C", "/")
        .replace("%2f", "/")
        .replace("%2F", "/")
        .casefold()
    )


def _iter_strings(node, depth=0, budget=None):
    """Yield every string in an argument tree, depth- and size-bounded."""
    if budget is None:
        budget = [_MAX_SCAN_CHARS]
    if depth > _MAX_DEPTH or budget[0] <= 0:
        return
    if isinstance(node, str):
        budget[0] -= len(node)
        yield node
        return
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str):
                budget[0] -= len(key)
                yield key
            yield from _iter_strings(value, depth + 1, budget)
        return
    if isinstance(node, (list, tuple, set, frozenset)):
        for value in node:
            yield from _iter_strings(value, depth + 1, budget)
        return
    if node is not None and not isinstance(node, (bool, int, float)):
        text = str(node)
        budget[0] -= len(text)
        yield text


def _markers(cfg):
    out = list(_DIR_MARKERS) + list(_ARTIFACT_MARKERS)
    data_dir = getattr(cfg, "data_dir", None)
    if data_dir:
        out.append(_norm(str(data_dir)))
    return tuple(out)


def references_data_dir(args, cfg) -> bool:
    """True when any string in ``args`` points into the ambient data dir."""
    markers = _markers(cfg)
    for value in _iter_strings(args):
        hay = _norm(value)
        for marker in markers:
            if marker in hay:
                return True
    return False


def data_dir_jail(tool_name: str, args: dict, cfg):
    """Block ANY tool call that references the ambient data directory."""
    if not references_data_dir(args, cfg):
        return None
    return {
        "action": "block",
        "message": JAIL_MESSAGE.format(tool=tool_name or "tool"),
    }


def looks_sensitive(args) -> bool:
    """Cheap, cfg-free fallback used by the plugin's fail-closed handler."""
    try:
        blob = _norm(repr(args))
    except Exception:  # noqa: BLE001
        return True  # cannot tell -> fail closed
    return any(m in blob for m in _DIR_MARKERS + _ARTIFACT_MARKERS)


def check_tool_call(tool_name: str, args: dict, cfg, store, session_id: str = ""):
    args = args or {}

    # L3 first, and for every tool: the jail has no exemptions, so nothing
    # below it can widen the boundary.
    verdict = data_dir_jail(tool_name, args, cfg)
    if verdict is not None:
        return verdict

    if tool_name != "send_message":
        return None
    if not store.any_intents():
        return None  # never armed -> dormant

    target = str(args.get("target") or args.get("to") or "")
    platform, _, ref = target.partition(":")
    if platform.strip().lower() != "slack":
        return None  # other platforms are not ambient's business
    ref = ref.strip()
    channel_part = ref.split(":", 1)[0].strip()

    if channel_part == cfg.ops_channel:
        return None  # shadow digests / ops alerts always allowed

    id_form = (
        len(channel_part) >= 9
        and channel_part[:1] in _ID_PREFIXES
        and channel_part.isalnum()
    )
    if not id_form:
        # "slack:#name" / "slack:@handle" / bare "slack" while armed:
        # cannot be compared against the channel allowlist -> fail closed.
        return {
            "action": "block",
            "message": (
                f"ambient-watch: ambiguous slack target {target!r} while ambient "
                "intents are armed. Use an explicit channel-ID target."
            ),
        }
    if channel_part not in cfg.channels:
        return None  # not a watched channel

    pending = set(store.pending_intents())
    if ref in pending:
        return None
    return {
        "action": "block",
        "message": (
            f"ambient-watch: send_message target {target!r} is not an armed "
            "ambient intent for this watched channel. Allowed targets: "
            + (", ".join(f"slack:{p}" for p in sorted(pending)) if pending else "(none)")
        ),
    }
