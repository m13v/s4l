#!/opt/homebrew/bin/python3.11
"""Regenerate the IG render + post launchd plists so total slot count =
posts_per_account_per_day * count(enabled accounts).

Reads config.json `instagram.accounts` (filtered to `enabled: true`) plus
`posts_per_account_per_day`, `schedule_start_hour`, `schedule_end_hour`.

- Post slots are spread evenly between [schedule_start_hour, schedule_end_hour]
  (post times rounded to nearest 15 minutes to avoid weird launchd entries).
- Render slots are post slots minus 30 minutes (rolling under start_hour if
  needed; clamped at start_hour-1 with minute=30 if the first post is at start_hour).
- Writes plists to ~/Library/LaunchAgents and reloads via `launchctl bootout`
  then `bootstrap`.

Default mode is `--dry-run` so a misconfigured config never silently rewrites
the live schedule. Pass `--apply` to actually write + reload.

Usage:
    regenerate_ig_plists.py                 # dry-run, print proposed plists
    regenerate_ig_plists.py --apply         # write + reload launchd
    regenerate_ig_plists.py --diff          # show diff vs currently-installed plists
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

CONFIG_PATH = Path.home() / "social-autoposter" / "config.json"
REPO_DIR = Path.home() / "social-autoposter"
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"

POST_LABEL = "com.m13v.social-instagram-daily"
RENDER_LABEL = "com.m13v.social-instagram-render"
POST_SHELL = REPO_DIR / "skill" / "run-instagram-daily.sh"
RENDER_SHELL = REPO_DIR / "skill" / "run-instagram-render.sh"
LOG_DIR = REPO_DIR / "skill" / "logs"


def load_cfg():
    cfg = json.loads(CONFIG_PATH.read_text())
    ig = cfg.get("instagram") or {}
    accounts = [a for a in (ig.get("accounts") or []) if a.get("enabled")]
    # Per-account posts_per_day overrides the global default. Accounts without
    # the field fall back to instagram.posts_per_account_per_day.
    global_default = int(ig.get("posts_per_account_per_day", 5))
    per_account = {
        a["username"]: int(a.get("posts_per_day", global_default))
        for a in accounts
    }
    total_slots = sum(per_account.values())
    return {
        "enabled_account_count": len(accounts),
        "posts_per_account_per_day": global_default,
        "per_account_posts_per_day": per_account,
        "total_slots": total_slots,
        "start_hour": int(ig.get("schedule_start_hour", 9)),
        "end_hour": int(ig.get("schedule_end_hour", 22)),
    }


def round_to_15(minutes_float):
    """Round to nearest 15-minute boundary, clamped 0..59."""
    return max(0, min(59, int(round(minutes_float / 15.0)) * 15)) % 60


def compute_post_slots(start_hour, end_hour, n_slots):
    """Return list of (hour, minute) post times spread evenly across the
    window. n_slots in [start_hour*60, end_hour*60] using linspace; first
    slot is at start_hour:00, last at end_hour:00. Single slot lands at the
    midpoint."""
    if n_slots <= 0:
        return []
    if n_slots == 1:
        mid = (start_hour + end_hour) // 2
        return [(mid, 0)]
    start_min = start_hour * 60
    end_min = end_hour * 60
    step = (end_min - start_min) / (n_slots - 1)
    slots = []
    for i in range(n_slots):
        total = start_min + i * step
        h = int(total // 60)
        m = round_to_15(total - h * 60)
        if m == 60:  # round_to_15 returns 0..59, defensive
            h += 1
            m = 0
        slots.append((h, m))
    # dedup defensively (two adjacent rounded slots could collide at high N)
    seen = set()
    deduped = []
    for h, m in slots:
        key = (h, m)
        while key in seen:
            # bump by 15 if collision; never crosses end_hour by construction
            m = (m + 15) % 60
            if m == 0:
                h += 1
            key = (h, m)
        seen.add(key)
        deduped.append(key)
    return deduped


def render_slot_for(post_h, post_m):
    """Render fires 30 minutes before the post."""
    total = post_h * 60 + post_m - 30
    if total < 0:
        total = 0
    return (total // 60, total % 60)


def plist_xml(label, shell_path, slots, extra_env=None):
    cal_entries = "\n".join(
        f"\t\t<dict>\n\t\t\t<key>Hour</key>\n\t\t\t<integer>{h}</integer>\n"
        f"\t\t\t<key>Minute</key>\n\t\t\t<integer>{m}</integer>\n\t\t</dict>"
        for h, m in slots
    )
    env_block = (
        "\t\t<key>HOME</key>\n"
        f"\t\t<string>{Path.home()}</string>\n"
        "\t\t<key>PATH</key>\n"
        "\t\t<string>"
        + (extra_env.get("PATH") if extra_env and "PATH" in extra_env else "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
        + "</string>"
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
\t<key>Label</key>
\t<string>{label}</string>
\t<key>ProgramArguments</key>
\t<array>
\t\t<string>/bin/bash</string>
\t\t<string>{shell_path}</string>
\t</array>
\t<key>StartCalendarInterval</key>
\t<array>
{cal_entries}
\t</array>
\t<key>StandardOutPath</key>
\t<string>{LOG_DIR}/launchd-{label.replace('com.m13v.social-', '')}-stdout.log</string>
\t<key>StandardErrorPath</key>
\t<string>{LOG_DIR}/launchd-{label.replace('com.m13v.social-', '')}-stderr.log</string>
\t<key>EnvironmentVariables</key>
\t<dict>
{env_block}
\t</dict>
\t<key>RunAtLoad</key>
\t<false/>
</dict>
</plist>
"""


def reload_plist(plist_path, label):
    uid = os.getuid()
    domain = f"gui/{uid}"
    # bootout is allowed to fail (not loaded yet); bootstrap must succeed.
    subprocess.run(
        ["launchctl", "bootout", f"{domain}/{label}"],
        capture_output=True, check=False, text=True,
    )
    r = subprocess.run(
        ["launchctl", "bootstrap", domain, str(plist_path)],
        capture_output=True, check=False, text=True,
    )
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write plists + reload launchd")
    ap.add_argument("--diff", action="store_true", help="diff proposed vs installed")
    args = ap.parse_args()

    cfg = load_cfg()
    n_accounts = cfg["enabled_account_count"]
    if n_accounts == 0:
        sys.stderr.write("no enabled accounts in config.json:instagram.accounts; refusing to rewrite plists\n")
        sys.exit(2)
    # Total slots = sum of per-account posts_per_day (each account opts in to
    # its own daily cadence; defaults to global posts_per_account_per_day).
    n_slots = cfg["total_slots"]
    post_slots = compute_post_slots(cfg["start_hour"], cfg["end_hour"], n_slots)
    render_slots = [render_slot_for(h, m) for h, m in post_slots]

    per_acct = ", ".join(
        f"{u}={c}" for u, c in sorted(cfg["per_account_posts_per_day"].items())
    )
    print(
        f"plan: {n_accounts} enabled account(s) [{per_acct}] "
        f"= {n_slots} slots/day; window {cfg['start_hour']}:00-{cfg['end_hour']}:00"
    )
    print("post slots:")
    for h, m in post_slots:
        print(f"  {h:02d}:{m:02d}")
    print("render slots (post - 30min):")
    for h, m in render_slots:
        print(f"  {h:02d}:{m:02d}")

    # Render plist must carry the rich PATH the existing one has (ffmpeg + nvm)
    render_path = (
        "/Users/matthewdi/.nvm/versions/node/v20.19.4/bin:"
        "/Users/matthewdi/.nvm/versions/node/v23.10.0/bin:"
        "/opt/homebrew/Cellar/ffmpeg/8.1.1/bin:"
        "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    )

    post_xml = plist_xml(POST_LABEL, str(POST_SHELL), post_slots)
    render_xml = plist_xml(RENDER_LABEL, str(RENDER_SHELL), render_slots, extra_env={"PATH": render_path})

    post_target = LAUNCH_AGENTS / f"{POST_LABEL}.plist"
    render_target = LAUNCH_AGENTS / f"{RENDER_LABEL}.plist"

    if args.diff:
        for label, target, xml in (
            (POST_LABEL, post_target, post_xml),
            (RENDER_LABEL, render_target, render_xml),
        ):
            print(f"\n=== diff {label} ===")
            if not target.exists():
                print(f"  (no existing file at {target})")
                continue
            current = target.read_text()
            if current == xml:
                print("  (no changes)")
            else:
                import difflib
                for line in difflib.unified_diff(
                    current.splitlines(), xml.splitlines(),
                    fromfile=f"installed:{target.name}",
                    tofile=f"proposed:{target.name}",
                    lineterm="",
                ):
                    print(line)
        return

    if not args.apply:
        print("\n(dry-run; pass --apply to write + reload)")
        return

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)

    for label, target, xml in (
        (POST_LABEL, post_target, post_xml),
        (RENDER_LABEL, render_target, render_xml),
    ):
        target.write_text(xml)
        rc, out = reload_plist(target, label)
        if rc != 0:
            print(f"WARN: bootstrap {label} returned rc={rc}: {out.strip()}")
        else:
            print(f"reloaded {label} -> {target}")


if __name__ == "__main__":
    main()
