"""Shadow mode must simulate live mode's noise controls.

Live gates on the `interventions` table: per-channel cooldown, per-channel
daily cap, global daily cap, and N-strike self-quiet. Shadow deliberately
records no interventions (so a digest never burns a thread's real nudge
budget), which silently made every one of those controls inert in shadow —
`last_intervention_at` was always None and every count 0.

Consequence: the soak over-reports. You would judge "would this nudge have
been welcome?" against a digest volume live could never produce, and tune
thresholds on the wrong data. Shadow must therefore derive the same limits
from its own `shadow_seen` ledger.
"""

from conftest import WATCHED, make_event

from aw_detectors import find_candidates
from aw_recorder import decide

T0 = 1754900000.0
HOUR = 3600


def _ask(store, cfg, text, ts):
    decide(make_event(text=text, ts=f"{ts:.6f}"), cfg, store)


def test_shadow_respects_channel_cooldown(cfg, store):
    cfg.cooldown_minutes = 120
    _ask(store, cfg, "first question?", T0)
    _ask(store, cfg, "second question?", T0 + 60)
    # First sweep emits one and marks it seen.
    assert len(find_candidates(store, cfg, now=T0 + 46 * 60)) == 1
    store.mark_shadow_seen(WATCHED, f"{T0:.6f}", now=T0 + 46 * 60)
    # 30 min later the second question is old enough, but cooldown blocks it.
    assert find_candidates(store, cfg, now=T0 + 76 * 60) == []
    # After the cooldown expires it surfaces.
    assert len(find_candidates(store, cfg, now=T0 + 46 * 60 + 121 * 60)) == 1


def test_shadow_respects_per_channel_daily_cap(cfg, store):
    cfg.cooldown_minutes = 0
    cfg.caps_per_channel_per_day = 2
    for i in range(4):
        _ask(store, cfg, f"question {i}?", T0 + i)
    now = T0 + 46 * 60
    for i in range(2):
        assert len(find_candidates(store, cfg, now=now)) == 1
        c = find_candidates(store, cfg, now=now)[0]
        store.mark_shadow_seen(c.channel, c.thread_ts, now=now)
        now += 60
    # Cap of 2 reached for the day -> nothing more, even though 2 remain.
    assert find_candidates(store, cfg, now=now) == []


def test_shadow_respects_global_daily_cap(cfg, store):
    cfg.cooldown_minutes = 0
    cfg.caps_global_per_day = 1
    _ask(store, cfg, "first?", T0)
    _ask(store, cfg, "second?", T0 + 60)
    now = T0 + 46 * 60
    assert len(find_candidates(store, cfg, now=now)) == 1
    store.mark_shadow_seen(WATCHED, f"{T0:.6f}", now=now)
    assert find_candidates(store, cfg, now=now + 60) == []


def test_shadow_caps_reset_after_a_day(cfg, store):
    cfg.cooldown_minutes = 0
    cfg.caps_per_channel_per_day = 1
    _ask(store, cfg, "first?", T0)
    _ask(store, cfg, "second?", T0 + 60)
    now = T0 + 46 * 60
    store.mark_shadow_seen(WATCHED, f"{T0:.6f}", now=now)
    assert find_candidates(store, cfg, now=now + 60) == []
    # 25 hours later the daily window has rolled over.
    assert len(find_candidates(store, cfg, now=now + 25 * HOUR)) == 1


def test_live_mode_still_uses_interventions_not_shadow_seen(live_cfg):
    """Live must not be throttled by shadow history from the soak."""
    from aw_store import AmbientStore

    store = AmbientStore(live_cfg.data_dir / "ambient.db")
    live_cfg.cooldown_minutes = 120
    _ask(store, live_cfg, "a question?", T0)
    # A shadow marker from the soak, recent enough to trip a cooldown.
    store.mark_shadow_seen(WATCHED, "1754000000.000000", now=T0 + 46 * 60)
    assert len(find_candidates(store, live_cfg, now=T0 + 46 * 60)) == 1
    store.close()
