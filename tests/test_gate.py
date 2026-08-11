"""gate.py tests — the gate is now the WHOLE job (--no-agent).

Contract encoded here (see gate.py's docstring for the source references):

- Nothing to report            -> last line is {"wakeAgent": false} and the
                                  scheduler discards the output entirely.
- Something an operator needs  -> audit lines, and the last line is NOT the
                                  wake JSON, or the whole delivery is
                                  suppressed.
- Shadow mode judges and PAYS but never posts; live mode posts into the
  exact thread_ts through aw_post.
- Any internal error -> {"wakeAgent": false} + a breadcrumb (fail closed,
  and fail-closed must not mean fail-silent).
- The audit lines are EXCERPT-FREE: cron persists this stdout under
  ~/.hermes/cron/output/, which the L3 jail does not cover.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from conftest import WATCHED, FakeJudge, FakeTransport, make_event

from aw_recorder import decide
from gate import ERROR_LOG_NAME, WAKE_FALSE, install_gate, run_gate

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "ambient-watch"

T0 = 1754900000.0


def _seed_question(cfg, store, ts=T0, text="who owns the migration runbook?"):
    decide(make_event(text=text, ts=f"{ts:.6f}"), cfg, store)


def _last(out):
    return [ln for ln in out.splitlines() if ln.strip()][-1]


def _is_silent(out):
    return _last(out) == WAKE_FALSE


def test_no_candidates_is_a_silent_tick(cfg, store, fake_judge):
    out = run_gate(cfg, store, now=T0, judge_fn=fake_judge)
    assert _is_silent(out)
    assert fake_judge.calls == [], "an empty sweep must not spend a token"


def test_shadow_mode_reports_a_would_post_and_never_posts(cfg, store, fake_judge):
    _seed_question(cfg, store)
    transport = FakeTransport()
    out = run_gate(cfg, store, now=T0 + 46 * 60, judge_fn=fake_judge, transport=transport)

    assert not _is_silent(out), "a would-post digest must be delivered"
    assert f"WOULD HAVE POSTED to {WATCHED}/{T0:.6f}" in out
    assert transport.calls == [], "shadow mode must never touch Slack"
    assert store.has_intervention(WATCHED, f"{T0:.6f}") is False
    assert store.is_shadow_seen(WATCHED, f"{T0:.6f}") is True


def test_live_mode_posts_into_the_exact_thread(live_cfg, fake_judge):
    from aw_store import AmbientStore

    store = AmbientStore(live_cfg.data_dir / "ambient.db")
    try:
        _seed_question(live_cfg, store)
        transport = FakeTransport()
        out = run_gate(
            live_cfg, store, now=T0 + 46 * 60, judge_fn=fake_judge, transport=transport
        )
        assert len(transport.calls) == 1
        assert transport.calls[0]["thread_ts"] == f"{T0:.6f}"
        assert transport.calls[0]["text"] == fake_judge.nudge
        assert f"POSTED to {WATCHED}/{T0:.6f}" in out
        assert store.has_intervention(WATCHED, f"{T0:.6f}") is True
    finally:
        store.close()


def test_judge_saying_no_is_a_silent_tick(cfg, store):
    """The judge is the primary filter now: "no" costs nothing downstream."""
    _seed_question(cfg, store)
    judge = FakeJudge(should_post=False)
    transport = FakeTransport()
    out = run_gate(cfg, store, now=T0 + 46 * 60, judge_fn=judge, transport=transport)
    assert _is_silent(out), out
    assert transport.calls == []
    assert store.judgment(WATCHED, f"{T0:.6f}")["verdict"] == "skip"


def test_low_confidence_is_withheld(cfg, store):
    _seed_question(cfg, store)
    judge = FakeJudge(confidence=0.4)
    out = run_gate(cfg, store, now=T0 + 46 * 60, judge_fn=judge, transport=FakeTransport())
    assert _is_silent(out), out
    assert "withheld" in out


def test_judge_failure_never_posts_and_leaves_a_breadcrumb(live_cfg):
    from aw_store import AmbientStore

    store = AmbientStore(live_cfg.data_dir / "ambient.db")
    try:
        _seed_question(live_cfg, store)
        judge = FakeJudge(error="Timeout: judge took too long")
        transport = FakeTransport()
        out = run_gate(
            live_cfg, store, now=T0 + 46 * 60, judge_fn=judge, transport=transport
        )
        assert transport.calls == [], "a failed judgment must never produce a post"
        log = live_cfg.data_dir / ERROR_LOG_NAME
        assert log.exists(), out
        assert "judge unavailable" in log.read_text(encoding="utf-8")
    finally:
        store.close()


def test_a_raising_judge_fails_closed(cfg, store):
    """aw_judge.judge never raises, but the gate must survive one that does."""
    _seed_question(cfg, store)
    judge = FakeJudge(raise_exc=RuntimeError("provider exploded"))
    transport = FakeTransport()
    out = run_gate(cfg, store, now=T0 + 46 * 60, judge_fn=judge, transport=transport)
    assert _is_silent(out), out
    assert transport.calls == []
    body = (cfg.data_dir / ERROR_LOG_NAME).read_text(encoding="utf-8")
    assert "provider exploded" in body


def test_kill_switch_short_circuits_before_any_spend(cfg, store, fake_judge):
    _seed_question(cfg, store)
    store.set_kill_switch(True)
    out = run_gate(cfg, store, now=T0 + 46 * 60, judge_fn=fake_judge)
    assert _is_silent(out)
    assert fake_judge.calls == [], "the kill switch must have no LLM in its path"


def test_internal_error_fails_closed(cfg, store, monkeypatch, fake_judge):
    import aw_detectors

    monkeypatch.setattr(
        aw_detectors, "find_candidates",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("detector exploded")),
    )
    out = run_gate(cfg, store, now=T0, judge_fn=fake_judge)
    assert _is_silent(out)


def test_internal_error_leaves_a_breadcrumb(cfg, store, monkeypatch, fake_judge):
    """A silent run's stdout is discarded by the scheduler, so the fail-closed
    path must also write gate_errors.log — otherwise a permanently dead gate
    is undetectable and ambient mode just goes quiet forever."""
    import aw_detectors

    monkeypatch.setattr(
        aw_detectors, "find_candidates",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("detector exploded")),
    )
    log = cfg.data_dir / ERROR_LOG_NAME
    assert not log.exists()

    run_gate(cfg, store, now=T0, judge_fn=fake_judge)

    assert log.exists(), "fail-closed run left no breadcrumb"
    body = log.read_text(encoding="utf-8")
    assert "RuntimeError" in body and "detector exploded" in body, body


def test_breadcrumb_never_breaks_the_gate(cfg, store, monkeypatch, fake_judge):
    """Breadcrumbs are best-effort: an unwritable log must not change the
    verdict or raise out of run_gate."""
    import aw_detectors
    import gate

    monkeypatch.setattr(
        aw_detectors, "find_candidates",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(
        gate.Path, "mkdir",
        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only fs")),
    )
    assert _is_silent(run_gate(cfg, store, now=T0, judge_fn=fake_judge))


def test_broken_shim_gates_off_and_leaves_a_breadcrumb(tmp_path):
    """Execute the generated shim for real with its plugin dir removed: the
    outer guard must print the false gate, exit 0, AND record why."""
    shim = install_gate(hermes_home=tmp_path)
    broken = shim.with_name("ambient_watch_gate_broken.py")
    broken.write_text(
        shim.read_text(encoding="utf-8").replace(
            repr(str(PLUGIN_DIR)), repr(str(tmp_path / "does" / "not" / "exist"))
        ),
        encoding="utf-8",
    )

    env = dict(os.environ, HERMES_HOME=str(tmp_path))
    proc = subprocess.run(
        [sys.executable, str(broken)],
        capture_output=True, text=True, env=env, timeout=60,
    )

    # rc != 0 would make the scheduler deliver a "script failed" alert.
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.splitlines()[-1]) == {"wakeAgent": False}

    log = tmp_path / "plugin-data" / "ambient_watch" / ERROR_LOG_NAME
    assert log.exists(), f"shim left no breadcrumb; stderr={proc.stderr!r}"
    assert "ModuleNotFoundError" in log.read_text(encoding="utf-8")
    assert "Traceback" in proc.stderr


def test_install_gate_writes_shim_into_scripts_dir(tmp_path):
    """Cron path-jails scripts to HERMES_HOME/scripts (scheduler.py:2392)."""
    shim = install_gate(hermes_home=tmp_path)
    assert shim == tmp_path / "scripts" / "ambient_watch_gate.py"
    content = shim.read_text(encoding="utf-8")
    assert "sys.path.insert" in content
    assert '{"wakeAgent": false}' in content  # fail-closed on any shim error
    # Idempotent re-install
    assert install_gate(hermes_home=tmp_path) == shim
