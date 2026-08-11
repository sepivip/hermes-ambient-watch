"""Configuration for the ambient-watch plugin.

Runtime config lives at ``$HERMES_HOME/plugin-data/ambient_watch/config.json``
(JSON, not YAML, so the plugin has zero dependencies beyond stdlib).

PARITY NOTE (the crutches are gone). Cooldowns and per-channel/global daily
nudge caps were proxies for two things the plugin did not have: a spend
limit and real judgment. Both now exist — ``aw_budget`` meters every LLM
call in USD and declines over cap, and ``aw_judge`` asks a model instead of
matching ``?`` with a regex — so the proxies are deleted. What remains are
the controls Claude Tag itself has: once per thread, self-quiet after N
ignored nudges, quiet hours, a kill switch, and the spend limit.

Deleted keys are still *accepted* from a deployed config.json (they are
ignored with one warning) so a live machine does not crash on upgrade.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("ambient_watch")

# Keys the decision path no longer reads. Present in configs written before
# the parity work; ignored rather than fatal.
LEGACY_KEYS = (
    "cooldown_minutes",
    "caps_per_channel_per_day",
    "caps_global_per_day",
    "unanswered_after_minutes",
    "stalled_after_minutes",
)

# Config keys copied verbatim onto the dataclass when present.
_PASSTHROUGH_KEYS = (
    "min_age_minutes",
    "arrival_enabled",
    "arrival_debounce_seconds",
    "arrival_max_wait_seconds",
    "arrival_judgments_per_channel_hour",
    "arrival_judgments_global_hour",
    "arrival_burst",
    "arrival_max_pending",
    "arrival_pump_interval_seconds",
    "caps_per_thread",
    "candidates_per_run",
    "self_quiet_after_ignored",
    "quiet_start",
    "quiet_end",
    "quiet_tz",
    "retention_days",
    "judge_confidence_threshold",
    "judge_max_rejudge",
    "judge_max_tokens",
    "judge_timeout_seconds",
    "judge_model",
    "judge_provider",
    "daily_usd_global",
    "daily_usd_per_channel",
    "monthly_usd_global",
    "alert_thresholds",
    "prices",
    "sweep_job_id",
)


def hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "hermes"
    return Path.home() / ".hermes"


@dataclass
class AmbientConfig:
    bot_user_id: str
    channels: set = field(default_factory=set)
    mode: str = "shadow"  # "shadow" | "live"
    ops_channel: str = ""
    data_dir: Path = field(default_factory=lambda: hermes_home() / "plugin-data" / "ambient_watch")

    # -- deterministic prefilter (zero tokens) ----------------------------
    # SWEEP-SCOPED age knob. "Is this thread actually stalled / does it need
    # help?" is a judgment now, not a threshold, so the two old windows
    # (unanswered_after / stalled_after) collapse into "quiet for a while".
    #
    # This is the SWEEP's window only. The arrival path has its own floor
    # (arrival_debounce_seconds) because it is triggered by a message rather
    # than by a tick, and the two triggers partition by age so they cannot
    # race over one thread's re-judge budget: arrival owns
    # [arrival_debounce_seconds, min_age_minutes), the sweep owns
    # [min_age_minutes, inf). load_config() enforces that by clamping
    # arrival_max_wait_seconds strictly below min_age_minutes*60.
    min_age_minutes: int = 45
    # Throughput cap: nominees handed to the judge per sweep (also the
    # Claude-Tag-style rate limit — at most one candidate per channel).
    candidates_per_run: int = 3

    # -- arrival-time judging (ships DARK) --------------------------------
    # Judge a thread when a message arrives, debounced, instead of only on a
    # sweep tick. Everything here is off by one boolean: with
    # arrival_enabled False no pump task is ever created and the plugin's
    # observable behaviour is byte-identical to the sweep-only build.
    arrival_enabled: bool = False
    # Both the coalescing quiet period AND the politeness floor — they are the
    # same requirement. We get exactly one post per thread ever, and a
    # 5-second reply spends that single shot on a thread a colleague was
    # already answering. Clamped to >= 30 at load.
    arrival_debounce_seconds: int = 90
    # A thread that never goes quiet still gets judged once. 0 disables.
    arrival_max_wait_seconds: int = 300
    # THE REPLACEMENT FOR THE CADENCE. The 15-minute sweep was an implicit
    # rate limit no message volume could change; at arrival time whoever posts
    # chooses when we spend, so the rate has to be metered explicitly. The
    # buckets meter ATTEMPTS, not successes, and a token is never refunded.
    arrival_judgments_per_channel_hour: int = 4
    arrival_judgments_global_hour: int = 12   # without it, N channels multiply
    arrival_burst: int = 2                    # bucket capacity, both scopes
    # Cap on the in-memory pending map. Over cap the NEW entry is dropped and
    # counted: under a raid arrival mode degrades to sweep behaviour (latency)
    # rather than evicting a genuinely pending thread.
    arrival_max_pending: int = 200
    # Pump wake interval. Worst added latency is debounce + this.
    arrival_pump_interval_seconds: int = 5

    # -- noise controls that survive (Claude-Tag-native) ------------------
    caps_per_thread: int = 1          # once per thread, forever
    self_quiet_after_ignored: int = 4  # stop nudging a channel that ignores us

    # Quiet hours (local wall clock in quiet_tz; window may wrap midnight)
    quiet_start: str = "20:00"
    quiet_end: str = "09:00"
    quiet_tz: str = "UTC"

    retention_days: int = 14

    # -- judgment ---------------------------------------------------------
    judge_confidence_threshold: float = 0.7
    judge_max_rejudge: int = 1       # re-judges allowed after NEW human activity
    judge_max_tokens: int = 600
    judge_timeout_seconds: int = 30
    judge_model: str = ""            # "" -> auxiliary.ambient_watch_judge config
    judge_provider: str = ""

    # -- spend limit (the real limiter) -----------------------------------
    # Conservative non-zero defaults on purpose: a budget with no caps
    # configured is a spend hole, and Budget.decision() reports
    # "unconfigured" (which the gate treats as a decline) if they are all
    # cleared.
    daily_usd_global: float = 1.00
    daily_usd_per_channel: float = 0.25
    monthly_usd_global: float = 10.00
    alert_thresholds: tuple = (0.75, 0.95)
    # {model: [usd_per_1M_input, usd_per_1M_output]}. Left empty on purpose:
    # an unpriced model falls back to aw_budget._DEFAULT_PRICE, which is
    # deliberately expensive so a missing price never reads as free.
    prices: dict = field(default_factory=dict)

    # Optional: cron job id of the sweep. Set it to arm the post_api_request
    # leak detector (see __init__.py) — ambient must account for ZERO
    # agent-session tokens, so any usage attributed to the sweep is a bug.
    sweep_job_id: str = ""

    def budget_cfg(self) -> dict:
        return {
            "daily_usd_global": self.daily_usd_global,
            "daily_usd_per_channel": self.daily_usd_per_channel,
            "monthly_usd_global": self.monthly_usd_global,
            "alert_thresholds": tuple(self.alert_thresholds or ()),
            "prices": {
                k: tuple(v) for k, v in (self.prices or {}).items()
                if isinstance(v, (list, tuple)) and len(v) == 2
            },
        }


#: The politeness floor cannot be typo'd away. Below this a nudge lands before
#: any human could plausibly have answered, which is how a thread's single post
#: gets spent losing a race we did not know we were in.
MIN_ARRIVAL_DEBOUNCE_SECONDS = 30


def _int_at_least(value, floor: int, default: int, label: str, warn=None) -> int:
    warn = warn or logger.warning
    try:
        out = int(value)
    except (TypeError, ValueError):
        warn(
            "ambient-watch: %s is not a number (%r); using %d", label, value, default
        )
        return default
    if out < floor:
        warn(
            "ambient-watch: %s=%r is below the floor of %d; clamping", label, out, floor
        )
        return floor
    return out


def _clamp_arrival(cfg: AmbientConfig) -> AmbientConfig:
    """Coerce the arrival knobs into a range that cannot misbehave.

    Every clamp logs exactly once, at load. A config typo on this path is not
    a cosmetic problem: it either spends money faster than intended or replies
    before a human could have.

    LOG LEVEL DEPENDS ON arrival_enabled, and that is not cosmetic either.
    While the feature is dark nothing below is read by anything, so a clamp of
    a DEFAULT against an operator's existing sweep window must not shout: the
    live config has ``min_age_minutes: 5``, which the default
    ``arrival_max_wait_seconds`` of 300 collides with exactly, so warning here
    would print a scary line about a disabled feature on every gateway start
    AND every sweep tick — and the acceptance test for the dark deploy is
    "the log says nothing changed". The clamp itself still happens, because it
    must already be right at the moment the boolean is flipped.
    """
    cfg.arrival_enabled = bool(cfg.arrival_enabled)
    warn = logger.warning if cfg.arrival_enabled else logger.debug
    cfg.arrival_debounce_seconds = _int_at_least(
        cfg.arrival_debounce_seconds, MIN_ARRIVAL_DEBOUNCE_SECONDS, 90,
        "arrival_debounce_seconds", warn,
    )
    cfg.arrival_burst = _int_at_least(
        cfg.arrival_burst, 1, 2, "arrival_burst", warn
    )
    cfg.arrival_max_pending = _int_at_least(
        cfg.arrival_max_pending, 1, 200, "arrival_max_pending", warn
    )
    cfg.arrival_pump_interval_seconds = _int_at_least(
        cfg.arrival_pump_interval_seconds, 1, 5,
        "arrival_pump_interval_seconds", warn,
    )
    cfg.arrival_judgments_per_channel_hour = _int_at_least(
        cfg.arrival_judgments_per_channel_hour, 0, 4,
        "arrival_judgments_per_channel_hour", warn,
    )
    cfg.arrival_judgments_global_hour = _int_at_least(
        cfg.arrival_judgments_global_hour, 0, 12,
        "arrival_judgments_global_hour", warn,
    )

    # The two triggers must partition by age, or they race over the same
    # re-judge budget. 0 disables the max-wait backstop entirely.
    max_wait = _int_at_least(cfg.arrival_max_wait_seconds, 0, 300,
                             "arrival_max_wait_seconds", warn)
    # min_age_minutes is the SWEEP's knob: it is live whether or not arrival
    # mode is, so its own clamp always warns.
    sweep_floor = _int_at_least(cfg.min_age_minutes, 1, 45, "min_age_minutes") * 60
    cfg.min_age_minutes = sweep_floor // 60
    if max_wait >= sweep_floor:
        clamped = max(cfg.arrival_debounce_seconds, sweep_floor - 1)
        warn(
            "ambient-watch: arrival_max_wait_seconds=%d overlaps the sweep's "
            "window (min_age_minutes=%d => %ds); clamping to %ds so the two "
            "triggers keep partitioning by age",
            max_wait, cfg.min_age_minutes, sweep_floor, clamped,
        )
        max_wait = clamped
    if 0 < max_wait < cfg.arrival_debounce_seconds:
        warn(
            "ambient-watch: arrival_max_wait_seconds=%d is below "
            "arrival_debounce_seconds=%d, which would fire before the "
            "politeness floor; raising it to the floor",
            max_wait, cfg.arrival_debounce_seconds,
        )
        max_wait = cfg.arrival_debounce_seconds
    cfg.arrival_max_wait_seconds = max_wait
    return cfg


def load_config(path: Path | None = None) -> AmbientConfig:
    """Load config.json from the plugin-data dir. Fail closed on absence."""
    data_dir = hermes_home() / "plugin-data" / "ambient_watch"
    cfg_path = path or (data_dir / "config.json")
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg = AmbientConfig(
        bot_user_id=raw["bot_user_id"],
        channels=set(raw.get("channels", [])),
        mode=raw.get("mode", "shadow"),
        ops_channel=raw.get("ops_channel", ""),
        data_dir=Path(raw.get("data_dir", data_dir)),
    )
    for key in _PASSTHROUGH_KEYS:
        if key in raw:
            setattr(cfg, key, raw[key])

    _clamp_arrival(cfg)

    stale = [k for k in LEGACY_KEYS if k in raw]
    if stale:
        # One line, once per load: cooldowns and daily nudge caps were
        # crutches for the missing spend limit + weak judgment. Both exist
        # now, so the keys do nothing.
        logger.warning(
            "ambient-watch: ignoring retired config key(s) %s -- cooldowns and "
            "per-day nudge caps were replaced by the spend limit "
            "(daily_usd_*/monthly_usd_global) and model judgment. Safe to "
            "delete them from config.json.",
            ", ".join(sorted(stale)),
        )
    return cfg
