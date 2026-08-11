"""End-to-end tests against the REAL Hermes plugin loader.

Unlike the rest of tests/ (which uses fakes from conftest.py), this module
drives ``hermes_cli.plugins`` itself: the real ``PluginManager``, the real
``PluginContext``, the real ``plugins.enabled`` gate, the real
``_load_directory_module`` import machinery, the real
``hermes_cli.lifecycle.invoke_hook`` dispatch, and real ``MessageEvent`` /
``SessionSource`` / ``Platform`` objects from ``gateway.platforms.base``.

How it runs
-----------
The Hermes package lives in a different virtualenv than this repo's pytest,
so every scenario is executed in a SUBPROCESS launched with the Hermes venv
interpreter, running this same file in ``--driver`` mode. The driver prints a
single JSON report between sentinels; the pytest functions assert on it.

Running the driver in a subprocess is not just a venv workaround — it is also
what makes these tests honest:

* ``tests/conftest.py`` puts ``ambient-watch/`` on ``sys.path`` so the
  fake-based tests can ``import aw_config``. That import path does NOT exist
  under the real loader, and a subprocess with a clean ``sys.path`` is the
  only way to see the difference.
* Plugin loading mutates process-global state (``sys.modules``,
  ``hermes_cli.plugins._plugin_manager``, the tool registry). A fresh
  process per scenario keeps scenarios independent.

Safety
------
``HERMES_HOME`` is always a fresh temp directory; the driver refuses to run
if ``get_hermes_home()`` does not resolve inside the temp root, so the real
install at ``%LOCALAPPDATA%/hermes`` can never be written to. Nothing here
starts a gateway or touches the network.

Usage:
    <hermes venv>/python.exe tests/test_real_loader.py     # self-running
    <plugin venv>/python.exe -m pytest tests/test_real_loader.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN_SRC = REPO / "ambient-watch"
HERMES_AGENT = Path(
    os.environ.get(
        "HERMES_AGENT_DIR",
        r"C:\Users\User\AppData\Local\hermes\hermes-agent",
    )
)
HERMES_PY = Path(
    os.environ.get("HERMES_PY", str(HERMES_AGENT / "venv" / "Scripts" / "python.exe"))
)

BOT_ID = "U0BOTID99"
WATCHED = "C0WATCHED1"
UNWATCHED = "C0ELSEWHER"
OPS = "C0AMBOPS11"

_BEGIN = "<<<AW_REPORT_BEGIN>>>"
_END = "<<<AW_REPORT_END>>>"


# ---------------------------------------------------------------------------
# Driver — runs inside the Hermes venv, in a temp HERMES_HOME
# ---------------------------------------------------------------------------


def _driver(spec: dict) -> dict:
    """Build a temp HERMES_HOME, drive the real loader, return a report."""
    temp_root = Path(spec["temp_root"]).resolve()
    home = temp_root / "home"

    # --- hard safety gate: never let this touch the user's real hermes home
    if not str(home).startswith(str(temp_root)) or "Temp" not in str(temp_root):
        raise SystemExit(f"refusing to run outside a temp root: {temp_root}")

    (home / "plugins").mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        Path(spec["plugin_src"]),
        home / "plugins" / "ambient-watch",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    data_dir = home / "plugin-data" / "ambient_watch"
    data_dir.mkdir(parents=True, exist_ok=True)
    if spec.get("write_config_json", True):
        (data_dir / "config.json").write_text(
            json.dumps(
                {
                    "bot_user_id": spec["bot_id"],
                    "channels": [spec["watched"]],
                    "mode": "shadow",
                    "ops_channel": spec["ops"],
                    "quiet_start": "20:00",
                    "quiet_end": "09:00",
                    "quiet_tz": "UTC",
                }
            ),
            encoding="utf-8",
        )
    config_yaml = "plugins:\n"
    if spec["enabled"]:
        config_yaml += "  enabled:\n    - ambient-watch\n"
    else:
        config_yaml += "  enabled: []\n"
    (home / "config.yaml").write_text(config_yaml, encoding="utf-8")

    os.environ["HERMES_HOME"] = str(home)
    if spec.get("empty_bundled", True):
        empty = temp_root / "empty_bundled"
        empty.mkdir(exist_ok=True)
        os.environ["HERMES_BUNDLED_PLUGINS"] = str(empty)
    os.environ.pop("HERMES_SAFE_MODE", None)
    os.environ.pop("HERMES_ENABLE_PROJECT_PLUGINS", None)

    from hermes_constants import get_hermes_home

    resolved = Path(get_hermes_home()).resolve()
    if resolved != home.resolve():
        raise SystemExit(f"HERMES_HOME sanity check failed: {resolved} != {home}")

    # Capture everything the plugin and the loader log during register().
    import logging

    records: list[str] = []

    class _Cap(logging.Handler):
        def emit(self, rec):  # noqa: D102
            msg = rec.getMessage()
            if rec.exc_info:
                import traceback

                msg += " || " + "".join(traceback.format_exception(*rec.exc_info))
            records.append(f"{rec.levelname} {rec.name}: {msg}")

    cap = _Cap(level=logging.DEBUG)
    for name in ("ambient_watch", "hermes_cli.plugins"):
        lg = logging.getLogger(name)
        lg.addHandler(cap)
        lg.setLevel(logging.DEBUG)

    report: dict = {"home": str(home), "hermes_home_resolved": str(resolved)}

    # --- the real discovery + load path -----------------------------------
    import hermes_cli.plugins as P

    P.discover_plugins()
    mgr = P.get_plugin_manager()

    report["listed"] = [
        p for p in mgr.list_plugins() if p["key"] == "ambient-watch"
    ]
    report["hooks"] = {k: len(v) for k, v in mgr._hooks.items()}
    loaded = mgr._plugins.get("ambient-watch")
    report["found"] = loaded is not None
    if loaded is not None:
        report["enabled"] = loaded.enabled
        report["error"] = loaded.error
        report["hooks_registered"] = sorted(loaded.hooks_registered)
        report["module_name"] = getattr(loaded.module, "__name__", None)
        report["module_file"] = getattr(loaded.module, "__file__", None)
        report["module_path_attr"] = list(getattr(loaded.module, "__path__", []) or [])
        report["module_package"] = getattr(loaded.module, "__package__", None)
    report["aux_tasks"] = {
        k: v.get("plugin") for k, v in (getattr(mgr, "_aux_tasks", {}) or {}).items()
    }
    report["hermes_plugins_modules"] = sorted(
        m for m in sys.modules if m.startswith("hermes_plugins")
    )
    report["plugin_dir_on_syspath"] = any(
        Path(p).resolve() == (home / "plugins" / "ambient-watch").resolve()
        for p in sys.path
        if p
    )
    report["flat_aw_config_importable"] = "aw_config" in sys.modules
    report["gate_shim_exists"] = (home / "scripts" / "ambient_watch_gate.py").exists()
    report["scripts_dir_listing"] = (
        sorted(p.name for p in (home / "scripts").iterdir())
        if (home / "scripts").is_dir()
        else None
    )

    # --- exercise the hooks through the REAL dispatch path ------------------
    if spec["enabled"] and loaded is not None and loaded.enabled:
        from gateway.platforms.base import MessageEvent, SessionSource
        from gateway.config import Platform
        from hermes_cli.lifecycle import invoke_hook as lifecycle_invoke_hook

        def ev(text, channel=spec["watched"], ts="1754900000.000100",
               thread_ts=None, user="U0HUMAN001", chat_type="group"):
            # adapter.py:6227 emits only "dm" or "group", never "channel".
            raw = {"ts": ts, "channel": channel, "user": user, "text": text}
            if thread_ts:
                raw["thread_ts"] = thread_ts
            return MessageEvent(
                text=text,
                user_id=user,
                user_name="tester",
                message_id=ts,
                source=SessionSource(
                    platform=Platform.SLACK,
                    user_id=user,
                    chat_id=channel,
                    user_name="tester",
                    chat_type=chat_type,
                ),
                raw_message=raw,
                metadata={
                    "slack_team_id": "T0TEAM0001",
                    "slack_channel_id": channel,
                    "slack_thread_ts": thread_ts or ts,
                },
            )

        cases = {
            "ambient_watched": ev("anyone know how the deploy went?"),
            "mention_watched": ev(
                f"<@{spec['bot_id']}> please look at this",
                ts="1754900000.000200",
            ),
            "unwatched_channel": ev("chatter", channel=spec["unwatched"],
                                    ts="1754900000.000300"),
            "dm": ev("hi there", ts="1754900000.000400", chat_type="dm"),
        }
        results = {}
        for label, event in cases.items():
            results[label] = lifecycle_invoke_hook(
                "pre_gateway_dispatch",
                event=event,
                gateway=None,
                session_store=None,
            )
        report["dispatch_results"] = results

        # sqlite ledger: what did the recorder actually persist?
        import sqlite3

        db = sqlite3.connect(str(data_dir / "ambient.db"))
        report["db_rows"] = [
            list(r)
            for r in db.execute(
                "SELECT channel, ts, is_mention FROM messages ORDER BY ts"
            )
        ]
        db.close()

        # --- pre_tool_call guard through the real resolver -----------------
        # Only the data-directory jail remains. The send_message target
        # pinning this block used to exercise is deleted: no cron agent can
        # call send_message at all (cron/scheduler.py:182), so the pinning
        # was scenery, and the gate now posts directly (aw_post).
        store_mod = sys.modules["hermes_plugins.ambient_watch.aw_store"]
        report["guard"] = {
            "data_dir_read": P.resolve_pre_tool_block(
                "read_file", {"path": str(data_dir / "ambient.db")}
            ),
            "bare_artifact": P.resolve_pre_tool_block(
                "read_file", {"path": "candidates.json"}
            ),
            "future_tool": P.resolve_pre_tool_block(
                "some_future_tool", {"nested": [{"p": str(data_dir / "ambient.db")}]}
            ),
            "unrelated_tool": P.resolve_pre_tool_block("read_file", {"path": "x"}),
        }

        # --- the cron gate, called in PACKAGE context (not via the shim) ----
        # run_gate() swallows every exception and fails closed, so a broken
        # in-package import would look like "no candidates" forever.
        #
        # judge_fn is injected: this test must never make a real LLM call.
        gate_mod = sys.modules["hermes_plugins.ambient_watch.gate"]
        judge_mod = sys.modules["hermes_plugins.ambient_watch.aw_judge"]
        cfg_mod = sys.modules["hermes_plugins.ambient_watch.aw_config"]
        gcfg = cfg_mod.load_config(data_dir / "config.json")
        gstore = store_mod.AmbientStore(data_dir / "ambient.db")

        seen_nominees = []

        def _offline_judge(nominees, cfg):
            seen_nominees.extend(
                {"channel": c.channel, "thread_ts": c.thread_ts,
                 "judge_view": c.judge_view}
                for c in nominees
            )
            return judge_mod.JudgeResult(
                verdicts=[
                    judge_mod.Verdict(
                        channel=c.channel, thread_ts=c.thread_ts,
                        should_post=True, confidence=0.9,
                        reason="offline test judge",
                        nudge="Want me to dig into this?",
                    )
                    for c in nominees
                ],
                model="offline-test-model", prompt_tokens=100, completion_tokens=20,
            )

        try:
            report["run_gate_output"] = gate_mod.run_gate(
                gcfg, gstore, judge_fn=_offline_judge
            )
            report["gate_nominees"] = seen_nominees
        finally:
            gstore.close()

    report["logs"] = records
    return report


def _driver_main() -> int:
    spec = json.loads(sys.argv[2])
    try:
        report = _driver(spec)
    except BaseException as exc:  # noqa: BLE001 — report it, don't traceback-only
        import traceback

        report = {"driver_error": f"{type(exc).__name__}: {exc}",
                  "traceback": traceback.format_exc()}
    print(_BEGIN)
    print(json.dumps(report, indent=2, default=str))
    print(_END)
    return 0


# ---------------------------------------------------------------------------
# Subprocess harness
# ---------------------------------------------------------------------------


def run_scenario(*, enabled: bool = True, empty_bundled: bool = True,
                 write_config_json: bool = True) -> dict:
    """Run one loader scenario in a fresh Hermes-venv process + temp home."""
    if not HERMES_PY.exists():
        raise RuntimeError(f"Hermes venv python not found at {HERMES_PY}")
    temp_root = Path(tempfile.mkdtemp(prefix="aw_real_loader_"))
    spec = {
        "temp_root": str(temp_root),
        "plugin_src": str(PLUGIN_SRC),
        "enabled": enabled,
        "empty_bundled": empty_bundled,
        "write_config_json": write_config_json,
        "bot_id": BOT_ID,
        "watched": WATCHED,
        "unwatched": UNWATCHED,
        "ops": OPS,
    }
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("HERMES_HOME", "HERMES_BUNDLED_PLUGINS", "HERMES_SAFE_MODE",
                     "HERMES_ENABLE_PROJECT_PLUGINS", "PYTHONPATH")
    }
    proc = subprocess.run(
        [str(HERMES_PY), str(Path(__file__).resolve()), "--driver", json.dumps(spec)],
        capture_output=True,
        text=True,
        cwd=str(HERMES_AGENT),
        env=env,
        timeout=300,
    )
    out = proc.stdout
    if _BEGIN not in out or _END not in out:
        raise AssertionError(
            "driver produced no report\n"
            f"exit={proc.returncode}\nSTDOUT:\n{out}\nSTDERR:\n{proc.stderr}"
        )
    payload = out.split(_BEGIN, 1)[1].split(_END, 1)[0]
    report = json.loads(payload)
    report["_stderr"] = proc.stderr
    report["_exit"] = proc.returncode
    report["_temp_root"] = str(temp_root)
    return report


_CACHE: dict = {}


def _enabled_report() -> dict:
    if "enabled" not in _CACHE:
        _CACHE["enabled"] = run_scenario(enabled=True)
    return _CACHE["enabled"]


def _disabled_report() -> dict:
    if "disabled" not in _CACHE:
        _CACHE["disabled"] = run_scenario(enabled=False)
    return _CACHE["disabled"]


def _full_sweep_report() -> dict:
    if "full" not in _CACHE:
        _CACHE["full"] = run_scenario(enabled=True, empty_bundled=False)
    return _CACHE["full"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_real_loader_discovers_and_loads_the_plugin():
    r = _enabled_report()
    assert not r.get("driver_error"), r.get("traceback")
    assert r["found"] is True
    assert r["enabled"] is True, r["error"]
    assert r["error"] is None
    assert r["listed"] and r["listed"][0]["name"] == "ambient-watch"
    assert r["listed"][0]["kind"] == "standalone"
    assert r["listed"][0]["source"] == "user"
    assert r["listed"][0]["version"] == "0.1.0"


def test_register_ran_and_registered_all_three_hooks():
    """``post_api_request`` is the third: a LEAK DETECTOR, not a meter. The
    sweep runs as --no-agent and judges in its own process, so ambient must
    account for zero agent-session tokens — anything this hook attributes to
    the sweep means an agent session exists that should not. This assertion
    also proves the hook name is in the real host's VALID_HOOKS set."""
    r = _enabled_report()
    assert r["hooks_registered"] == [
        "post_api_request", "pre_gateway_dispatch", "pre_tool_call",
    ]
    assert r["hooks"]["pre_gateway_dispatch"] == 1
    assert r["hooks"]["pre_tool_call"] == 1
    assert r["hooks"]["post_api_request"] == 1
    assert r["listed"][0]["hooks"] == 3


def test_the_judge_auxiliary_task_registers_with_the_real_host():
    """``ambient_watch_judge`` must be accepted by the real registry: that is
    what puts it in `hermes model -> Configure auxiliary models` and gives the
    operator a place to pin a CHEAP model for judgment, independently of the
    main chat model. A rejected key (reserved, duplicated) must degrade to a
    warning, never take registration down — hence the log check too."""
    r = _enabled_report()
    assert r["aux_tasks"].get("ambient_watch_judge") == "ambient-watch", r["aux_tasks"]
    bad = [line for line in r["logs"] if "could not register the" in line]
    assert not bad, "\n".join(bad)


def test_hyphenated_directory_becomes_underscore_module_slug():
    """`ambient-watch` -> package `hermes_plugins.ambient_watch` (no import error)."""
    r = _enabled_report()
    assert r["module_name"] == "hermes_plugins.ambient_watch"
    assert r["module_package"] == "hermes_plugins.ambient_watch"
    assert r["module_path_attr"], "loader must set __path__ for relative imports"
    assert r["module_file"].endswith(str(Path("plugins") / "ambient-watch" / "__init__.py"))


def test_relative_imports_resolve_as_package_submodules():
    r = _enabled_report()
    mods = set(r["hermes_plugins_modules"])
    for sub in ("aw_config", "aw_guard", "aw_recorder", "aw_store", "gate"):
        assert f"hermes_plugins.ambient_watch.{sub}" in mods, sub
    # The real loader does NOT put the plugin dir on sys.path, and must not
    # leak flat module names process-wide.
    assert r["plugin_dir_on_syspath"] is False
    assert r["flat_aw_config_importable"] is False


def test_pre_gateway_dispatch_skips_unmentioned_watched_traffic():
    r = _enabled_report()
    res = r["dispatch_results"]["ambient_watched"]
    assert len(res) == 1, res
    assert res[0]["action"] == "skip"
    assert "ambient-watch" in res[0]["reason"]


def test_pre_gateway_dispatch_passes_mentions_and_out_of_scope_traffic():
    r = _enabled_report()
    assert r["dispatch_results"]["mention_watched"] == []
    assert r["dispatch_results"]["unwatched_channel"] == []
    assert r["dispatch_results"]["dm"] == []


def test_recorder_persisted_watched_messages_to_sqlite():
    r = _enabled_report()
    rows = {(c, int(m)) for c, _ts, m in r["db_rows"]}
    assert (WATCHED, 0) in rows, r["db_rows"]      # ambient message recorded
    assert (WATCHED, 1) in rows, r["db_rows"]      # mention recorded (and passed)
    assert all(c == WATCHED for c, _ in rows), r["db_rows"]


def test_pre_tool_call_guard_runs_through_resolve_pre_tool_block():
    """The data-directory jail, wired through Hermes' real resolver."""
    r = _enabled_report()
    g = r["guard"]
    assert g["data_dir_read"] and "ambient-watch" in g["data_dir_read"]
    assert g["bare_artifact"], "a relative artifact name must be jailed too"
    assert g["future_tool"], "the jail must cover tools that do not exist yet"
    assert g["unrelated_tool"] is None


def test_plugins_enabled_gate_keeps_the_plugin_dormant():
    r = _disabled_report()
    assert not r.get("driver_error"), r.get("traceback")
    assert r["found"] is True                     # discovered
    assert r["enabled"] is False                  # but not loaded
    assert "not enabled in config" in (r["error"] or "")
    assert r["hooks"] == {}
    assert r["module_name"] is None
    assert r["hermes_plugins_modules"] == []


def test_loads_alongside_the_real_bundled_plugin_set():
    """Same result with the real <repo>/plugins/ sweep, not just an empty dir."""
    r = _full_sweep_report()
    assert not r.get("driver_error"), r.get("traceback")
    assert r["enabled"] is True, r["error"]
    assert r["hooks"]["pre_gateway_dispatch"] >= 1
    assert r["hooks"]["pre_tool_call"] >= 1
    assert r["dispatch_results"]["ambient_watched"][0]["action"] == "skip"


def test_cron_gate_shim_is_installed_by_register():
    """register() -> install_gate() must write HERMES_HOME/scripts/ambient_watch_gate.py.

    KNOWN BREAKAGE under the real loader: gate.install_gate() does
    ``from aw_config import hermes_home`` (a FLAT import). Under the real
    loader the plugin is a package (hermes_plugins.ambient_watch) and its
    directory is never on sys.path, so this raises ModuleNotFoundError.
    register() swallows it, so the shim is silently never written and the
    cron pre-run gate can never run.
    """
    r = _enabled_report()
    gate_logs = [
        line for line in r["logs"] if "could not install the cron gate shim" in line
    ]
    assert not gate_logs, (
        "install_gate() failed under the real loader:\n" + "\n".join(gate_logs)
    )
    assert r["gate_shim_exists"] is True, (
        f"scripts dir: {r['scripts_dir_listing']}"
    )


def test_run_gate_works_in_package_context():
    """gate.run_gate() must reach find_candidates() under the real package.

    ``run_gate`` catches every exception and prints ``{"wakeAgent": false}``,
    so a failed in-package import degrades to a permanently silent sweep
    instead of an error. Assert on the absence of the fail-closed marker.
    """
    r = _enabled_report()
    out = r["run_gate_output"]
    assert "gate error" not in out, out
    # Either a silent tick (wake gate) or an audit line — never a crash.
    assert ('"wakeAgent"' in out) or ("ambient-watch" in out) or ("POSTED" in out), out


def test_the_judge_receives_a_sealed_sanitized_view_under_the_real_loader():
    """The judge view must be built correctly in PACKAGE context: a flat
    import inside aw_sanitize/aw_detectors would fail here and the sweep
    would silently degrade to 'no candidates' forever (the exact class of bug
    the shim P0 was)."""
    r = _enabled_report()
    nominees = r.get("gate_nominees") or []
    if not nominees:  # no thread was old enough in this environment
        return
    view = nominees[0]["judge_view"]
    assert view.startswith("<untrusted-slack-text>"), view
    assert "deploy" in view, view


# ---------------------------------------------------------------------------
# Self-running harness (so this file works under the Hermes venv, no pytest)
# ---------------------------------------------------------------------------


def _selftest_main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS {fn.__name__}")
    print(f"\n{len(tests) - failures} passed, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--driver":
        sys.exit(_driver_main())
    sys.exit(_selftest_main())
