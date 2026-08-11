"""Configuration for the ambient-watch plugin.

Runtime config lives at ``$HERMES_HOME/plugin-data/ambient_watch/config.json``
(JSON, not YAML, so the plugin has zero dependencies beyond stdlib).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


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

    # Detector thresholds
    unanswered_after_minutes: int = 45
    stalled_after_minutes: int = 240

    # Caps / noise controls
    caps_per_thread: int = 1
    caps_per_channel_per_day: int = 3
    caps_global_per_day: int = 8
    candidates_per_run: int = 3
    cooldown_minutes: int = 120
    self_quiet_after_ignored: int = 4

    # Quiet hours (local wall clock in quiet_tz; window may wrap midnight)
    quiet_start: str = "20:00"
    quiet_end: str = "09:00"
    quiet_tz: str = "UTC"

    retention_days: int = 14


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
    for key in (
        "unanswered_after_minutes", "stalled_after_minutes", "caps_per_thread",
        "caps_per_channel_per_day", "caps_global_per_day", "candidates_per_run",
        "cooldown_minutes", "self_quiet_after_ignored", "quiet_start",
        "quiet_end", "quiet_tz", "retention_days",
    ):
        if key in raw:
            setattr(cfg, key, raw[key])
    return cfg
