#!/usr/bin/env python3
"""One-shot fix (2026-06-01):
1. Human-derived target_chars was wrongly set to char_length(example), but
   human-derived examples are stored as 'OP: ... / Reply: ...' PAIRS, so the
   target was inflated by the OP framing. Re-set target_chars to the
   reply-only length of the example (the actual comment we'd write).
2. Replace 3 unfit placeholder examples (critic, curious_probe,
   snarky_oneliner) with real finished comments + matching target_chars.
3. Archive 3 dead sentinel "styles" that leaked into the registry
   (auto_discovery, unknown, discovered_via_notification): empty examples,
   low quantity, stale. 'reused' is KEPT (high volume, still in use).
"""
import re
import sys

sys.path.insert(0, "scripts")
from db import get_conn


def reply_only_len(example: str) -> int:
    m = re.search(r"[Rr]eply:\s*['\"]?(.+)", example, re.S)
    reply = (m.group(1) if m else example).strip().strip("'\"")
    reply = re.split(r"\n\(", reply)[0].strip().strip("'\"")
    return len(reply)


NEW_EXAMPLES = {
    "critic": "the missing piece is eval. without a way to catch regressions, every 'improvement' is just vibes",
    "curious_probe": "how are you handling two agents writing at once? curious because we hit silent overwrites and only a lock fixed it",
    "snarky_oneliner": "the demo always works. that's the whole problem.",
}

ARCHIVE = ["auto_discovery", "unknown", "discovered_via_notification"]


def main():
    c = get_conn()
    apply = "--apply" in sys.argv

    # 1. Human-derived reply-only targets
    rows = c.execute(
        "SELECT name, example, target_chars FROM engagement_styles_registry "
        "WHERE kind='human_derived' AND status='active' "
        "AND example IS NOT NULL AND example<>''"
    ).fetchall()
    print("== human-derived target fix (full-example -> reply-only) ==")
    hd_updates = []
    for name, example, tgt in rows:
        new_t = reply_only_len(example)
        if new_t and new_t != tgt:
            hd_updates.append((name, tgt, new_t))
            print(f"  {name:28} {tgt} -> {new_t}")
            if apply:
                c.execute(
                    "UPDATE engagement_styles_registry SET target_chars=%s, "
                    "updated_at=NOW() WHERE name=%s",
                    (new_t, name),
                )
    if not hd_updates:
        print("  (none)")

    # 2. Unfit example rewrites
    print("\n== unfit example rewrites ==")
    for name, ex in NEW_EXAMPLES.items():
        new_t = len(ex)
        print(f"  {name:18} -> target {new_t}  \"{ex[:60]}...\"")
        if apply:
            c.execute(
                "UPDATE engagement_styles_registry SET example=%s, "
                "target_chars=%s, updated_at=NOW() WHERE name=%s",
                (ex, new_t, name),
            )

    # 3. Archive dead sentinels
    print("\n== archive dead sentinels ==")
    for name in ARCHIVE:
        print(f"  {name} -> status=archived")
        if apply:
            c.execute(
                "UPDATE engagement_styles_registry SET status='archived', "
                "updated_at=NOW() WHERE name=%s",
                (name,),
            )

    if apply:
        c.commit()
        print("\nAPPLIED + committed.")
    else:
        print("\nDRY-RUN (pass --apply to write).")


if __name__ == "__main__":
    main()
