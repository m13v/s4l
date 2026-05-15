#!/usr/bin/env python3
"""Append a point-in-time snapshot of the style-pick target distribution.

The picker (engagement_styles.compute_target_distribution) weights styles by
a click-blended score. Clicks accrue retroactively — a click logged today on
a 30-day-old post changes that style's historical average — so the target %
the model saw on any past day CANNOT be cleanly reconstructed from the posts
table after the fact. This script freezes the live numbers daily.

Output: log/style_target_history.jsonl (gitignored), one JSON line per
(date, platform, context) run:

    {"date": "2026-05-15", "platform": "twitter", "context": "replying",
     "distribution": [{"style": "...", "pct": ..., "score": ..., ...}, ...]}

Append-only by design (mirrors the repo's no-retention-pruning rule). A
reader wanting "the target on day D" takes the last line matching that
(date, platform, context). Schedule daily via launchd; it is cheap and
idempotent enough to run more often.

Usage:
    python3 scripts/snapshot_style_targets.py
    python3 scripts/snapshot_style_targets.py --platform twitter
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engagement_styles import PLATFORM_POLICY, target_distribution_snapshot

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HISTORY_PATH = os.path.join(REPO_DIR, "log", "style_target_history.jsonl")

# Both contexts are snapshotted: posting and replying can diverge because
# compute_target_distribution takes a context arg (replying adds the
# project-recommendation note but the same weight math).
CONTEXTS = ("posting", "replying")


def main():
    parser = argparse.ArgumentParser(description="Snapshot style-pick target distributions")
    parser.add_argument(
        "--platform", default=None,
        help="Limit to one platform (default: all in PLATFORM_POLICY)",
    )
    args = parser.parse_args()

    platforms = [args.platform] if args.platform else list(PLATFORM_POLICY.keys())
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    written = 0
    with open(HISTORY_PATH, "a", encoding="utf-8") as fh:
        for platform in platforms:
            for context in CONTEXTS:
                try:
                    dist = target_distribution_snapshot(platform, context=context)
                except Exception as e:  # noqa: BLE001 - never block on one platform
                    print(f"WARN: {platform}/{context} snapshot failed: {e}", file=sys.stderr)
                    continue
                if not dist:
                    print(f"WARN: {platform}/{context} returned empty distribution", file=sys.stderr)
                    continue
                fh.write(json.dumps({
                    "date": today,
                    "snapshot_at": ts,
                    "platform": platform,
                    "context": context,
                    "distribution": dist,
                }, ensure_ascii=False) + "\n")
                written += 1
                top = dist[0]
                print(f"{platform}/{context}: {len(dist)} styles, "
                      f"top={top['style']} {top['pct']:.0f}% (score {top['score']})")

    print(f"Wrote {written} snapshot line(s) to {HISTORY_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
