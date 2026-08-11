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
    # ONE age knob. "Is this thread actually stalled / does it need help?"
    # is a judgment now, not a threshold, so the two old windows
    # (unanswered_after / stalled_after) collapse into "quiet for a while".
    min_age_minutes: int = 45
    # Throughput cap: nominees handed to the judge per sweep (also the
    # Claude-Tag-style rate limit — at most one candidate per channel).
    candidates_per_run: int = 3

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
