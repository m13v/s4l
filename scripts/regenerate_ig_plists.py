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

POST_LABEL_PREFIX = "com.m13v.social-instagram-daily"
RENDER_LABEL_PREFIX = "com.m13v.social-instagram-render"
# Legacy unified plists (one shared label across all accounts). Booted out
# during --apply if present; replaced by per-account plists labelled
# `<prefix>-<username>`.
LEGACY_POST_LABEL = POST_LABEL_PREFIX
LEGACY_RENDER_LABEL = RENDER_LABEL_PREFIX
POST_SHELL = REPO_DIR / "skill" / "run-instagram-daily.sh"
RENDER_SHELL = REPO_DIR / "skill" / "run-instagram-render.sh"
LOG_DIR = REPO_DIR / "skill" / "logs"


def plist_label_for(prefix, username):
    """Per-account launchd label, e.g. com.m13v.social-instagram-daily-matt_diak."""
    safe = username.replace(".", "_")  # launchd labels can't contain dots beyond the reverse-DNS prefix
    return f"{prefix}-{safe}"


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
    env_keys = {
        "HOME": str(Path.home()),
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
    }
    if extra_env:
        env_keys.update(extra_env)
    env_block = "\n".join(
        f"\t\t<key>{k}</key>\n\t\t<string>{v}</string>"
        for k, v in env_keys.items()
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

    per_account = cfg["per_account_posts_per_day"]
    start_h, end_h = cfg["start_hour"], cfg["end_hour"]

    # Plan: one render + one post plist PER enabled account. Slots within each
    # plist are spread evenly over [start_hour, end_hour] sized by that
    # account's posts_per_day. Per-account plists set FORCE_ACCOUNT in env so
    # the shells hard-pin the slot to the right account without ever calling
    # pick_ig_account.py.
    print(
        f"plan: {n_accounts} enabled account(s); per-account plists, "
        f"window {start_h}:00-{end_h}:00"
    )
    plans = []  # list of (username, post_slots, render_slots)
    for username, ppd in sorted(per_account.items()):
        post_slots = compute_post_slots(start_h, end_h, ppd)
        render_slots = [render_slot_for(h, m) for h, m in post_slots]
        plans.append((username, post_slots, render_slots))
        print(f"\n  {username}: posts_per_day={ppd}")
        print(f"    post slots:   " + ", ".join(f"{h:02d}:{m:02d}" for h, m in post_slots))
        print(f"    render slots: " + ", ".join(f"{h:02d}:{m:02d}" for h, m in render_slots))

    # Render plist needs the rich PATH (ffmpeg + nvm).
    render_path = (
        "/Users/matthewdi/.nvm/versions/node/v20.19.4/bin:"
        "/Users/matthewdi/.nvm/versions/node/v23.10.0/bin:"
        "/opt/homebrew/Cellar/ffmpeg/8.1.1/bin:"
        "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    )

    # Build (label, path, xml) tuples for everything we want installed.
    desired = []
    for username, post_slots, render_slots in plans:
        post_label = plist_label_for(POST_LABEL_PREFIX, username)
        render_label = plist_label_for(RENDER_LABEL_PREFIX, username)
        post_xml = plist_xml(
            post_label, str(POST_SHELL), post_slots,
            extra_env={"FORCE_ACCOUNT": username},
        )
        render_xml = plist_xml(
            render_label, str(RENDER_SHELL), render_slots,
            extra_env={"FORCE_ACCOUNT": username, "PATH": render_path},
        )
        desired.append((post_label, LAUNCH_AGENTS / f"{post_label}.plist", post_xml))
        desired.append((render_label, LAUNCH_AGENTS / f"{render_label}.plist", render_xml))

    desired_labels = {d[0] for d in desired}

    # Stale plists to bootout + remove: legacy unified labels, plus per-account
    # plists for accounts that are no longer enabled.
    stale = []
    if (LAUNCH_AGENTS / f"{LEGACY_POST_LABEL}.plist").exists():
        stale.append((LEGACY_POST_LABEL, LAUNCH_AGENTS / f"{LEGACY_POST_LABEL}.plist"))
    if (LAUNCH_AGENTS / f"{LEGACY_RENDER_LABEL}.plist").exists():
        stale.append((LEGACY_RENDER_LABEL, LAUNCH_AGENTS / f"{LEGACY_RENDER_LABEL}.plist"))
    for p in sorted(LAUNCH_AGENTS.glob(f"{POST_LABEL_PREFIX}-*.plist")) + \
             sorted(LAUNCH_AGENTS.glob(f"{RENDER_LABEL_PREFIX}-*.plist")):
        label = p.stem
        if label not in desired_labels:
            stale.append((label, p))

    if args.diff:
        for label, target, xml in desired:
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
        if stale:
            print("\n=== stale (will be booted out + removed) ===")
            for label, p in stale:
                print(f"  - {label} ({p.name})")
        return

    if not args.apply:
        if stale:
            print("\nstale plists (will be booted out + removed on --apply):")
            for label, p in stale:
                print(f"  - {label} ({p.name})")
        print("\n(dry-run; pass --apply to write + reload)")
        return

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)

    # 1. Bootout + remove stale plists (legacy unified + disabled per-account)
    uid = os.getuid()
    domain = f"gui/{uid}"
    for label, p in stale:
        subprocess.run(
            ["launchctl", "bootout", f"{domain}/{label}"],
            capture_output=True, check=False, text=True,
        )
        try:
            p.unlink()
            print(f"removed stale plist: {label} ({p})")
        except FileNotFoundError:
            pass

    # 2. Write + reload desired plists
    for label, target, xml in desired:
        target.write_text(xml)
        rc, out = reload_plist(target, label)
        if rc != 0:
            print(f"WARN: bootstrap {label} returned rc={rc}: {out.strip()}")
        else:
            print(f"reloaded {label} -> {target}")


if __name__ == "__main__":
    main()
