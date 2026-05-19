#!/opt/homebrew/bin/python3.11
"""
Pick the next IG post type (organic vs product) and the next pending video of
that type. Writes one JSON line to stdout for the shell harness to read.

Algorithm: inverse-recent-share weighting, identical to the Twitter pipeline's
scripts/pick_project.py. effective_weight = config_weight / (1 + posts in the
last RECENT_WINDOW_DAYS). Configured via the `instagram` block in config.json:
  post_type_weights: { organic: N, product: M }   # relative target shares
  recent_window_days: 7                            # rolling window
A type that has been posting heavily is dampened toward under-posted ones, but
never selected above its raw config weight. Settles toward the target ratio
over time.

Usage:
  ig_post_type_picker.py                  # pick across all accounts (legacy)
  ig_post_type_picker.py --account NAME   # scope to one target_account

Output:
  {"post_type": "organic", "video_path": "...", "post_number": 4,
   "target_account": "matt_diak", "reason": "...", "fallback": false}

Exit codes:
  0  — picked successfully
  2  — no draft videos of either type (queue exhausted for the scoped account)
  3  — config error / DB error
"""

import argparse
import json
import os
import random
import sys
from pathlib import Path

ENV_FILE = Path.home() / "social-autoposter" / ".env"
CONFIG_FILE = Path.home() / "social-autoposter" / "config.json"


def load_env():
    env = {}
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def load_ig_config():
    """Return (post_type_weights dict, recent_window_days int, product_cooldown_posts int) from config.json."""
    cfg = json.loads(CONFIG_FILE.read_text())
    ig = cfg.get("instagram") or {}
    weights = ig.get("post_type_weights") or ig.get("post_type_ratio") or {
        "organic": 4,
        "product": 1,
    }
    days = int(ig.get("recent_window_days", 7))
    # Project diversity cooldown: how many of the most recent posts on this
    # account to look at when deciding which projects are "recently posted"
    # and therefore ineligible for the next product draft. Default 6 means
    # the same project cannot appear twice within any 6-post sliding window
    # per account. Cascading fallback (6 -> 3 -> 1 -> 0) kicks in if the
    # cooldown filters out every product draft, so cadence is preserved.
    cooldown = int(ig.get("product_cooldown_posts", 6))
    return weights, days, cooldown


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", help="scope all queries to target_account (default: pick across all)")
    args = ap.parse_args()

    try:
        import psycopg2
    except ImportError:
        sys.stderr.write("psycopg2 missing\n")
        sys.exit(3)

    env = load_env()
    db_url = env.get("DATABASE_URL")
    if not db_url:
        sys.stderr.write("DATABASE_URL missing in .env\n")
        sys.exit(3)

    type_weights_cfg, window_days = load_ig_config()
    account = args.account

    conn = psycopg2.connect(db_url)
    cur = conn.cursor()

    # Recent posted counts per type for inverse-share weighting.
    # Scoped to target_account when one is requested; otherwise global.
    if account:
        cur.execute(
            "SELECT post_type, COUNT(*) FROM media_posts "
            "WHERE status='posted' AND posted_urls ? 'instagram' "
            "  AND target_account=%s "
            "  AND posted_at > NOW() - INTERVAL %s "
            "GROUP BY post_type",
            (account, f"{window_days} days"),
        )
    else:
        cur.execute(
            "SELECT post_type, COUNT(*) FROM media_posts "
            "WHERE status='posted' AND posted_urls ? 'instagram' "
            "  AND posted_at > NOW() - INTERVAL %s "
            "GROUP BY post_type",
            (f"{window_days} days",),
        )
    recent_counts = {r[0]: r[1] for r in cur.fetchall() if r[0]}
    for t in ("organic", "product"):
        recent_counts.setdefault(t, 0)

    eligible = {
        t: float(type_weights_cfg.get(t, 0))
        for t in ("organic", "product")
        if float(type_weights_cfg.get(t, 0)) > 0
    }
    if not eligible:
        sys.stderr.write("instagram.post_type_weights is empty in config.json\n")
        sys.exit(3)

    effective = {t: w / (1 + recent_counts[t]) for t, w in eligible.items()}
    names = list(effective.keys())
    ws = [effective[n] for n in names]
    target = random.choices(names, weights=ws, k=1)[0]

    def _draft_query(type_):
        # Drafts for this type, optionally scoped to target_account. We allow
        # legacy NULL target_account rows to match when no --account is passed
        # so existing behavior is preserved; when --account is set, rows must
        # match exactly (account-scoped buffer).
        if account:
            cur.execute(
                "SELECT post_number, video_path FROM media_posts "
                "WHERE status='draft' AND post_type=%s "
                "  AND target_account=%s "
                "ORDER BY post_number ASC LIMIT 1",
                (type_, account),
            )
        else:
            cur.execute(
                "SELECT post_number, video_path FROM media_posts "
                "WHERE status='draft' AND post_type=%s "
                "ORDER BY post_number ASC LIMIT 1",
                (type_,),
            )
        return cur.fetchone()

    row = _draft_query(target)
    fallback = False
    fallback_from = None

    if row is None:
        # Fall back to the other type if this one has no drafts. Without the
        # fallback the post-cycle would idle even when usable drafts exist.
        other = "product" if target == "organic" else "organic"
        row = _draft_query(other)
        if row is None:
            sys.stderr.write(
                "queue empty: no draft rows for either organic or product"
                + (f" (target_account={account})" if account else "")
                + "\n"
            )
            sys.exit(2)
        sys.stderr.write(
            f"queue imbalance: target={target} has 0 drafts, falling back to {other}"
            + (f" (target_account={account})" if account else "")
            + "\n"
        )
        fallback_from = target
        target = other
        fallback = True

    post_number, video_path = row

    out = {
        "post_type": target,
        "video_path": video_path,
        "post_number": post_number,
        "target_account": account,
        "reason": (
            f"window={window_days}d account={account or '<global>'} "
            f"recent={recent_counts} config_weights={type_weights_cfg} "
            f"effective={effective} chose={target}"
            + (f" (fallback_from={fallback_from})" if fallback else "")
        ),
        "fallback": fallback,
    }
    print(json.dumps(out))
    conn.close()


if __name__ == "__main__":
    main()
