"""Integration test: the REAL Hermes cron script runner drives our gate shim.

Unlike the rest of tests/ (which use fakes), this file imports the actual
``cron.scheduler`` from the installed hermes-agent and exercises:

* ``_run_job_script``   (cron/scheduler.py:2352) — the script-path jail to
  ``HERMES_HOME/scripts/`` plus subprocess execution / exit-code handling.
* ``_parse_wake_gate``  (cron/scheduler.py:2574) — the last-stdout-line JSON
  gate whose ``{"wakeAgent": false}`` short-circuits the agent run at the
  call site (cron/scheduler.py:3454).

Contract being proven end to end:

1. ``install_gate()`` puts the shim exactly where the jail will accept it.
2. The real runner does NOT block it, and it exits 0 (rc != 0 makes the
   scheduler wake the agent with a "Script Error" prompt every tick).
3. No candidates  -> gate says wakeAgent=false -> agent skipped.
4. A candidate    -> gate says wakeAgent=true  -> agent woken, and
   candidates.json lands under the (temp) HERMES_HOME.
5. Internal failure (unreadable / corrupt / missing config) STILL exits 0
   with ``{"wakeAgent": false}`` — fail closed, never burn a session.

SAFETY: HERMES_HOME is redirected to a fresh temp dir *before* the scheduler
is imported, and an assertion refuses to run if it ever points at the real
Hermes home. Nothing here touches the user's live install.

Run it with the hermes venv interpreter (which has the hermes deps but no
pytest), either way::

    <hermes>/venv/Scripts/python.exe tests/test_real_cron_gate.py
    pytest tests/test_real_cron_gate.py      # skips if hermes isn't importable
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / "ambient-watch"

# Where the real hermes-agent package lives (overridable for other machines).
HERMES_AGENT_DIR = Path(
    os.environ.get("HERMES_AGENT_DIR")
    or (Path(os.environ.get("LOCALAPPDATA", Path.home())) / "hermes" / "hermes-agent")
)

# --- temp HERMES_HOME, installed BEFORE importing the scheduler -------------
_REAL_HOME = (Path(os.environ.get("LOCALAPPDATA", Path.home())) / "hermes").resolve()
HOME = Path(tempfile.mkdtemp(prefix="aw_realcron_home_")).resolve()
assert _REAL_HOME not in HOME.parents and HOME != _REAL_HOME, (
    f"refusing to run: temp HERMES_HOME {HOME} is inside the real hermes home"
)
os.environ["HERMES_HOME"] = str(HOME)

for _p in (str(PLUGIN_DIR), str(HERMES_AGENT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

SCHEDULER_IMPORT_ERROR = None
try:  # the real thing — no fakes below this line
    import cron.scheduler as scheduler  # type: ignore
except Exception as exc:  # pragma: no cover - environment without hermes deps
    scheduler = None
    SCHEDULER_IMPORT_ERROR = exc

import gate as aw_gate  # noqa: E402  (plugin under test)
from aw_store import AmbientStore  # noqa: E402

CHANNEL = "C0WATCHED1"
SHIM_NAME = "ambient_watch_gate.py"
DATA_DIR = HOME / "plugin-data" / "ambient_watch"
CONFIG_PATH = DATA_DIR / "config.json"
DB_PATH = DATA_DIR / "ambient.db"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _require_scheduler():
    if scheduler is None:  # pragma: no cover
        raise RuntimeError(
            f"real cron.scheduler not importable from {HERMES_AGENT_DIR}: "
            f"{SCHEDULER_IMPORT_ERROR!r}"
        )


def _write_config(**overrides):
    """Write a config the detectors can act on. Quiet window disabled."""
    raw = {
        "bot_user_id": "U0BOTID99",
        "channels": [CHANNEL],
        "mode": "shadow",
        "ops_channel": "C0AMBOPS11",
        "data_dir": str(DATA_DIR),
        "unanswered_after_minutes": 45,
        "stalled_after_minutes": 240,
        # start == end -> _in_quiet_hours() is always False, so the gate's
        # verdict depends only on the ledger, not on when CI happens to run.
        "quiet_start": "00:00",
        "quiet_end": "00:00",
        "quiet_tz": "UTC",
    }
    raw.update(overrides)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(raw), encoding="utf-8")
    return raw


def _reset_state():
    """Fresh config + empty ledger, and no stale candidates.json."""
    for p in (DB_PATH, DATA_DIR / "candidates.json", DATA_DIR / "gate_errors.log"):
        for suffix in ("", "-wal", "-shm"):
            f = Path(str(p) + suffix)
            if f.exists():
                f.unlink()
    _write_config()
    AmbientStore(DB_PATH).close()  # create the schema


def _seed_candidate():
    """One unanswered question, old enough to trip unanswered_after_minutes."""
    store = AmbientStore(DB_PATH)
    try:
        ts = f"{time.time() - 3600:.6f}"
        store.record_message(
            channel=CHANNEL, ts=ts, thread_ts=None, author="U0HUMAN001",
            is_bot=0, is_mention=0,
            text="who owns the deploy key? blocked until someone answers",
        )
    finally:
        store.close()


def _run(script_path):
    """Drive the REAL scheduler entrypoint."""
    _require_scheduler()
    return scheduler._run_job_script(script_path)


def _last_line(out: str) -> str:
    lines = [ln for ln in out.splitlines() if ln.strip()]
    return lines[-1].strip() if lines else ""


# --------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------
def test_install_gate_lands_in_the_jailed_scripts_dir():
    shim = aw_gate.install_gate(hermes_home=HOME)
    assert shim == HOME / "scripts" / SHIM_NAME, shim
    assert shim.is_file()
    body = shim.read_text(encoding="utf-8")
    assert str(PLUGIN_DIR) in body.replace("\\\\", "\\"), body
    # idempotent
    assert aw_gate.install_gate(hermes_home=HOME) == shim

    # The shim must live where the REAL jail resolves scripts to.
    _require_scheduler()
    assert scheduler._get_hermes_home().resolve() == HOME
    assert (scheduler._get_hermes_home() / "scripts").resolve() == shim.parent.resolve()


def test_path_jail_accepts_the_shim_by_relative_and_absolute_path():
    shim = aw_gate.install_gate(hermes_home=HOME)
    _reset_state()

    ok_rel, out_rel = _run(SHIM_NAME)
    assert ok_rel is True, out_rel
    assert "Blocked:" not in out_rel and "Script not found" not in out_rel, out_rel

    ok_abs, out_abs = _run(str(shim))
    assert ok_abs is True, out_abs
    assert "Blocked:" not in out_abs, out_abs

    # Control: the jail is genuinely enforced, so the pass above is not vacuous.
    outsider = HOME / "outside_gate.py"
    outsider.write_text("print('{\"wakeAgent\": true}')\n", encoding="utf-8")
    ok_out, out_out = _run(str(outsider))
    assert ok_out is False
    assert "Blocked:" in out_out, out_out


def test_no_candidates_gates_the_agent_off():
    aw_gate.install_gate(hermes_home=HOME)
    _reset_state()

    ok, out = _run(SHIM_NAME)
    assert ok is True, out                      # exit code 0
    assert json.loads(_last_line(out)) == {"wakeAgent": False}, out
    assert scheduler._parse_wake_gate(out) is False, out
    assert "no candidates" in out, out


def test_candidate_present_wakes_the_agent():
    aw_gate.install_gate(hermes_home=HOME)
    _reset_state()
    _seed_candidate()

    ok, out = _run(SHIM_NAME)
    assert ok is True, out
    assert json.loads(_last_line(out)) == {"wakeAgent": True}, out
    assert scheduler._parse_wake_gate(out) is True, out

    # The child resolved the TEMP home, not the real one.
    payload = json.loads((DATA_DIR / "candidates.json").read_text(encoding="utf-8"))
    assert payload["candidates"], payload
    cand = payload["candidates"][0]
    assert cand["channel"] == CHANNEL
    assert cand["kind"] == "unanswered_question"
    assert cand["untrusted"] is True
    assert cand["target"].startswith(f"slack:{CHANNEL}:")


def test_corrupt_config_still_exits_zero_and_gates_off():
    """A gate crash must never wake the agent: rc 0 + wakeAgent=false."""
    aw_gate.install_gate(hermes_home=HOME)
    _reset_state()
    CONFIG_PATH.write_text("{ this is not json", encoding="utf-8")

    ok, out = _run(SHIM_NAME)
    assert ok is True, out          # rc != 0 -> "Script Error" prompt -> agent wakes
    assert scheduler._parse_wake_gate(out) is False, out
    assert json.loads(_last_line(out)) == {"wakeAgent": False}, out


def test_unreadable_config_dir_still_exits_zero_and_gates_off():
    """Config path unreadable (points at a directory) — same fail-closed rule."""
    aw_gate.install_gate(hermes_home=HOME)
    _reset_state()
    CONFIG_PATH.unlink()
    CONFIG_PATH.mkdir()  # opening a directory as a file raises OSError
    try:
        ok, out = _run(SHIM_NAME)
        assert ok is True, out
        assert scheduler._parse_wake_gate(out) is False, out
        assert json.loads(_last_line(out)) == {"wakeAgent": False}, out
    finally:
        CONFIG_PATH.rmdir()


def test_missing_config_still_exits_zero_and_gates_off():
    aw_gate.install_gate(hermes_home=HOME)
    _reset_state()
    CONFIG_PATH.unlink()

    ok, out = _run(SHIM_NAME)
    assert ok is True, out
    assert scheduler._parse_wake_gate(out) is False, out
    assert json.loads(_last_line(out)) == {"wakeAgent": False}, out


def test_shim_outer_guard_survives_a_missing_plugin_dir():
    """If the plugin dir moves, ``from gate import main`` raises inside the
    shim — its own except-clause must still print the false gate and exit 0."""
    shim = aw_gate.install_gate(hermes_home=HOME)
    body = shim.read_text(encoding="utf-8")
    broken = shim.with_name("ambient_watch_gate_broken.py")
    broken.write_text(
        body.replace(repr(str(PLUGIN_DIR)), repr(str(HOME / "nope" / "gone"))),
        encoding="utf-8",
    )
    try:
        ok, out = _run(broken.name)
        assert ok is True, out
        assert scheduler._parse_wake_gate(out) is False, out
        assert json.loads(_last_line(out)) == {"wakeAgent": False}, out
    finally:
        broken.unlink()


def test_a_dead_gate_leaves_a_breadcrumb_through_the_real_runner():
    """Fail-closed must not mean fail-silent.

    When the gate returns wakeAgent=false the scheduler discards its stdout
    entirely (SILENT_MARKER, cron/scheduler.py:3281-3292), so a gate that is
    broken forever would be invisible — ambient mode would just stop with no
    signal anywhere. The breadcrumb log is the only durable evidence, and it
    must land under the *child's* HERMES_HOME (proving the scrubbed
    subprocess env resolved the temp home, not the real one).
    """
    aw_gate.install_gate(hermes_home=HOME)
    _reset_state()
    log = DATA_DIR / aw_gate.ERROR_LOG_NAME
    assert not log.exists()
    CONFIG_PATH.write_text("{ this is not json", encoding="utf-8")

    ok, out = _run(SHIM_NAME)

    # Contract unchanged: exit 0, gate false, JSON last.
    assert ok is True, out
    assert scheduler._parse_wake_gate(out) is False, out
    assert json.loads(_last_line(out)) == {"wakeAgent": False}, out

    # ...and now the failure is diagnosable.
    assert log.exists(), f"no breadcrumb under {DATA_DIR}; stdout={out!r}"
    body = log.read_text(encoding="utf-8")
    assert "JSONDecodeError" in body or "json" in body.lower(), body

    _reset_state()


def test_cron_creation_gate_accepts_the_bare_shim_filename():
    """The job must be created as script="ambient_watch_gate.py".

    ``tools/cronjob_tools._validate_cron_script_path`` (line 517) rejects
    absolute/``~`` paths at the API boundary, so a job pointed at the shim's
    full path can never be created even though the runner's jail would have
    accepted it.
    """
    try:
        from tools.cronjob_tools import _validate_cron_script_path as validate
    except Exception:  # pragma: no cover - hermes tools not importable
        return

    aw_gate.install_gate(hermes_home=HOME)
    assert validate(SHIM_NAME) is None
    assert validate("../evil.py") is not None
    assert validate(str(HOME / "scripts" / SHIM_NAME)) is not None


def test_kill_switch_gates_off_through_the_real_runner():
    aw_gate.install_gate(hermes_home=HOME)
    _reset_state()
    _seed_candidate()
    store = AmbientStore(DB_PATH)
    try:
        store.set_kill_switch(True)
    finally:
        store.close()

    ok, out = _run(SHIM_NAME)
    assert ok is True, out
    assert scheduler._parse_wake_gate(out) is False, out
    assert "kill switch" in out, out


# --------------------------------------------------------------------------
# pytest hook: skip cleanly when hermes-agent isn't importable
# --------------------------------------------------------------------------
try:  # pragma: no cover - only used under pytest
    import pytest

    pytestmark = pytest.mark.skipif(
        scheduler is None,
        reason=f"real hermes cron.scheduler not importable: {SCHEDULER_IMPORT_ERROR!r}",
    )
except ImportError:  # running under the hermes venv, which has no pytest
    pytest = None  # type: ignore


def _main() -> int:
    _require_scheduler()
    print(f"HERMES_HOME = {HOME}")
    print(f"scheduler   = {scheduler.__file__}")
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = []
    for fn in tests:
        try:
            fn()
        except Exception as exc:
            failures.append((fn.__name__, exc))
            print(f"FAIL {fn.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS {fn.__name__}")
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_main())
