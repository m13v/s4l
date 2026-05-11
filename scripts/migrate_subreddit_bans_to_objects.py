#!/usr/bin/env python3
"""Convert subreddit_bans.{comment_blocked,thread_blocked} from list-of-strings
to list-of-objects with audit metadata.

Old shape:
  "subreddit_bans": {
    "comment_blocked": ["powerbi", "startup", ...],
    "thread_blocked":  ["someplace", ...]
  }

New shape:
  "subreddit_bans": {
    "comment_blocked": [
      {"sub": "powerbi", "added_at": null, "reason": null, "project": null},
      ...
    ],
    "thread_blocked": [...]
  }

Existing entries get nulls for unknown fields (we never recorded them).
Idempotent: re-running on the new shape is a no-op.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


def upgrade_list(entries: list) -> tuple[list, int, int]:
    """Return (new_list, upgraded_count, already_object_count).

    Strings become {"sub": s, "added_at": null, "reason": null, "project": null}.
    Dicts pass through (idempotent).
    Anything else is dropped with a warning.
    """
    out: list[dict] = []
    upgraded = 0
    already = 0
    for entry in entries:
        if isinstance(entry, str):
            sub = entry.strip().lower()
            if not sub:
                continue
            out.append({
                "sub": sub,
                "added_at": None,
                "reason": None,
                "project": None,
            })
            upgraded += 1
        elif isinstance(entry, dict):
            # Already migrated. Normalize the sub field.
            sub = (entry.get("sub") or "").strip().lower()
            if not sub:
                print(f"  WARN: dict entry missing sub: {entry!r}, skipping", file=sys.stderr)
                continue
            out.append({
                "sub": sub,
                "added_at": entry.get("added_at"),
                "reason": entry.get("reason"),
                "project": entry.get("project"),
            })
            already += 1
        else:
            print(f"  WARN: unknown entry type {type(entry).__name__}: {entry!r}, skipping",
                  file=sys.stderr)
    return out, upgraded, already


def main() -> int:
    with CONFIG_PATH.open() as f:
        config = json.load(f)

    bans = config.get("subreddit_bans") or {}
    if not isinstance(bans, dict):
        print(f"subreddit_bans is not a dict (got {type(bans).__name__}); aborting",
              file=sys.stderr)
        return 1

    changed = False
    for key in ("comment_blocked", "thread_blocked"):
        existing = bans.get(key) or []
        if not isinstance(existing, list):
            print(f"  WARN: bans.{key} is not a list (got {type(existing).__name__}); replacing with []",
                  file=sys.stderr)
            existing = []
        new_list, upgraded, already = upgrade_list(existing)
        # Sort by sub for stable diffs.
        new_list.sort(key=lambda e: e["sub"])
        bans[key] = new_list
        print(f"  {key}: {len(new_list)} total (upgraded {upgraded} strings, "
              f"{already} already objects)")
        if upgraded > 0:
            changed = True

    config["subreddit_bans"] = bans

    if not changed:
        print("Nothing to migrate (all entries already in new shape).")
        return 0

    with CONFIG_PATH.open("w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")
    print(f"Wrote {CONFIG_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
