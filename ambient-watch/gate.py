"""Cron pre-run gate for the ambient sweep.

Printed stdout's last line is JSON honored by the Hermes cron scheduler
(verified v0.20.0, cron/scheduler.py): {"wakeAgent": false} skips the
agent session entirely, so idle ticks cost zero tokens.

When candidates exist, writes candidates.json into the plugin data dir,
arms the tool-guard intents, and wakes the agent.

CLI:
    python gate.py            # normal gate run (for cron pre_run)
    python gate.py --kill on  # flip kill switch (also: off)
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict


def run_gate(cfg, store, now: float | None = None) -> str:
    from aw_detectors import find_candidates

    now = time.time() if now is None else now
    lines = []

    if store.kill_switch():
        lines.append("ambient-watch: kill switch is ON")
        lines.append(json.dumps({"wakeAgent": False}))
        return "\n".join(lines)

    cands = find_candidates(store, cfg, now)
    if not cands:
        lines.append("ambient-watch: no candidates")
        lines.append(json.dumps({"wakeAgent": False}))
        return "\n".join(lines)

    payload = {
        "mode": cfg.mode,
        "ops_channel": cfg.ops_channel,
        "generated_at": now,
        "candidates": [asdict(c) for c in cands],
    }
    out_path = cfg.data_dir / "candidates.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    for c in cands:
        store.arm_intent(c.target, c.channel, c.thread_ts, now=now)
        store.record_intervention(c.channel, c.thread_ts, kind=c.kind, now=now)

    lines.append(f"ambient-watch: {len(cands)} candidate(s) -> {out_path}")
    lines.append(json.dumps({"wakeAgent": True}))
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    from aw_config import load_config
    from aw_store import AmbientStore

    cfg = load_config()
    store = AmbientStore(cfg.data_dir / "ambient.db")
    try:
        if len(argv) >= 2 and argv[0] == "--kill":
            store.set_kill_switch(argv[1].lower() in ("on", "1", "true"))
            print(f"ambient-watch kill switch: {argv[1]}")
            return 0
        print(run_gate(cfg, store))
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
