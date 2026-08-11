#!/usr/bin/env python3
"""One-command Slack wiring for ambient-watch.

Reads tokens from this repo's .env, resolves the bot user ID and the
watched/ops channel IDs from Slack, writes the plugin config, and patches
the Hermes config.yaml pairing (plugins.enabled + slack.free_response_channels)
that ambient mode depends on.

Usage:
    python setup_slack.py --watch "#dev" --ops "#ambient-ops" [--allow-all] [--dry-run]

Safety:
- config.yaml is backed up to config.yaml.bak-<epoch> before any edit.
- The channel list written to free_response_channels is ALWAYS exactly the
  watched channels in config.json — the fail-closed pairing invariant.
- Refuses to run if the bot is not a member of both channels.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent


def hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "hermes"
    return Path.home() / ".hermes"


def read_env(path: Path) -> dict:
    out = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def slack(method: str, token: str, **params):
    url = f"https://slack.com/api/{method}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


REQUIRED_SCOPES = {
    "app_mentions:read", "channels:history", "channels:read", "chat:write",
    "groups:history", "groups:read", "reactions:read", "users:read",
}


def check_scopes(token: str) -> tuple[set, set]:
    req = urllib.request.Request(
        "https://slack.com/api/auth.test",
        headers={"Authorization": f"Bearer {token}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        granted = {
            s.strip() for s in (resp.headers.get("x-oauth-scopes") or "").split(",") if s.strip()
        }
    return granted, REQUIRED_SCOPES - granted


def resolve_channels(token: str, wanted: list[str]) -> dict:
    """Map '#name' or raw ID to {name: (id, is_member)}."""
    found, cursor = {}, None
    while True:
        page = slack(
            "conversations.list", token,
            types="public_channel,private_channel", limit=200,
            **({"cursor": cursor} if cursor else {}),
        )
        if not page.get("ok"):
            raise SystemExit(f"conversations.list failed: {page.get('error')}")
        for c in page.get("channels", []):
            found[c["name"]] = (c["id"], c.get("is_member", False))
            found[c["id"]] = (c["id"], c.get("is_member", False))
        cursor = (page.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    out = {}
    for w in wanted:
        key = w.lstrip("#")
        if key not in found:
            raise SystemExit(
                f"channel {w!r} not found. Available: "
                + ", ".join(sorted(k for k in found if not k.startswith("C")))
            )
        out[w] = found[key]
    return out


def patch_config_yaml(path: Path, watched: list[str], dry: bool) -> str:
    """Set plugins.enabled + slack.free_response_channels to exactly `watched`."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines()
    chan_list = "[" + ", ".join(f'"{c}"' for c in watched) + "]"

    def upsert_block(lines, top_key, child_key, child_value):
        out, i, in_block, done = [], 0, False, False
        while i < len(lines):
            line = lines[i]
            if line.strip().startswith(f"{top_key}:") and not line.startswith(" "):
                in_block = True
                out.append(line)
                i += 1
                child_written = False
                while i < len(lines) and (lines[i].startswith(" ") or not lines[i].strip()):
                    if lines[i].strip().startswith(f"{child_key}:"):
                        out.append(f"  {child_key}: {child_value}")
                        child_written = True
                    else:
                        out.append(lines[i])
                    i += 1
                if not child_written:
                    out.append(f"  {child_key}: {child_value}")
                done = True
                continue
            out.append(line)
            i += 1
        if not done:
            out += ["", f"{top_key}:", f"  {child_key}: {child_value}"]
        return out

    lines = upsert_block(lines, "slack", "free_response_channels", chan_list)
    lines = upsert_block(lines, "plugins", "enabled", '["ambient-watch"]')
    new = "\n".join(lines).rstrip() + "\n"

    if dry:
        return new
    if path.exists():
        backup = path.with_suffix(f".yaml.bak-{int(time.time())}")
        backup.write_bytes(path.read_bytes())
        print(f"  backed up config.yaml -> {backup.name}")
    path.write_text(new, encoding="utf-8")
    return new


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", required=True, help="channel to watch, e.g. '#dev'")
    ap.add_argument("--ops", required=True, help="ops channel for shadow digests")
    ap.add_argument("--allow-all", action="store_true",
                    help="set SLACK_ALLOW_ALL_USERS=true so ambient sees teammates' messages")
    ap.add_argument("--mode", default="shadow", choices=["shadow", "live"])
    ap.add_argument("--tz", default="Asia/Tbilisi")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    env = read_env(REPO / ".env")
    token = env.get("SLACK_BOT_TOKEN")
    if not token:
        raise SystemExit("SLACK_BOT_TOKEN missing from .env")

    print("== scopes ==")
    granted, missing = check_scopes(token)
    print(f"  granted: {', '.join(sorted(granted)) or '(none)'}")
    if missing:
        raise SystemExit(
            "  MISSING: " + ", ".join(sorted(missing))
            + "\n  Fix: re-apply the manifest at %LOCALAPPDATA%\\hermes\\slack-manifest.json"
            "\n  then Reinstall to Workspace and update SLACK_BOT_TOKEN in .env."
        )

    auth = slack("auth.test", token)
    if not auth.get("ok"):
        raise SystemExit(f"auth.test failed: {auth.get('error')}")
    bot_id = auth["user_id"]
    print(f"== workspace ==\n  team={auth['team']} bot=@{auth['user']} id={bot_id}")

    print("== channels ==")
    resolved = resolve_channels(token, [args.watch, args.ops])
    for name, (cid, member) in resolved.items():
        print(f"  {name} -> {cid} bot_is_member={member}")
    not_member = [n for n, (_, m) in resolved.items() if not m]
    if not_member:
        raise SystemExit(
            "  Bot is not in: " + ", ".join(not_member)
            + f"\n  Run '/invite @{auth['user']}' in each, then re-run."
        )

    watch_id = resolved[args.watch][0]
    ops_id = resolved[args.ops][0]

    cfg = {
        "bot_user_id": bot_id,
        "channels": [watch_id],
        "mode": args.mode,
        "ops_channel": ops_id,
        "quiet_tz": args.tz,
    }
    data_dir = hermes_home() / "plugin-data" / "ambient_watch"
    cfg_path = data_dir / "config.json"
    print(f"== plugin config ==\n  {cfg_path}")
    print("  " + json.dumps(cfg))
    if not args.dry_run:
        data_dir.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    print("== hermes config.yaml pairing ==")
    yaml_path = hermes_home() / "config.yaml"
    patch_config_yaml(yaml_path, [watch_id], args.dry_run)
    print(f"  slack.free_response_channels = [{watch_id}]")
    print("  plugins.enabled = [ambient-watch]")

    if args.allow_all and not args.dry_run:
        env_path = hermes_home() / ".env"
        text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
        if "SLACK_ALLOW_ALL_USERS" not in text:
            env_path.write_text(
                text.rstrip() + "\nSLACK_ALLOW_ALL_USERS=true\n", encoding="utf-8"
            )
            print("  SLACK_ALLOW_ALL_USERS=true appended to hermes .env")

    print("\nDone." if not args.dry_run else "\nDry run — nothing written.")
    print("Next: start the gateway, then create the sweep cron job (see README step 4).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
