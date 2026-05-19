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
    # Project diversity cooldown: how many of the most recent posted rows on
    # this account to look at when deciding which project_names are "recently
    # posted" and therefore ineligible for the next product draft. Default 6
    # means the same project cannot appear twice within any 6-post sliding
    # window per account. Hard rule (no cascade relaxation): if every product
    # draft is blocked, the picker falls back to organic (which has NULL
    # project_name and is never on cooldown).
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

    type_weights_cfg, window_days, cooldown_posts = load_ig_config()
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

    def _recent_project_names(window):
        # Project names appearing in the last `window` posted IG rows on this
        # account. Used to exclude product drafts whose project recently posted.
        # Organic rows have project_name IS NULL and are skipped via WHERE.
        # Returns a set; empty when window<=0 or no account scoping.
        if window <= 0 or not account:
            return set()
        cur.execute(
            "SELECT project_name FROM ("
            "  SELECT project_name FROM media_posts "
            "   WHERE status='posted' AND posted_urls ? 'instagram' "
            "     AND target_account=%s "
            "   ORDER BY posted_at DESC LIMIT %s"
            ") t WHERE project_name IS NOT NULL",
            (account, window),
        )
        return {r[0] for r in cur.fetchall()}

    def _draft_query(type_, blocked_projects=None):
        # Drafts for this type, optionally scoped to target_account. We allow
        # legacy NULL target_account rows to match when no --account is passed
        # so existing behavior is preserved; when --account is set, rows must
        # match exactly (account-scoped buffer).
        # For product drafts, optionally exclude rows whose project_name is in
        # blocked_projects (project diversity cooldown). NULL project_name rows
        # always pass (they're organic-shaped and not subject to the cooldown).
        params = [type_]
        sql = (
            "SELECT post_number, video_path, project_name FROM media_posts "
            "WHERE status='draft' AND post_type=%s "
        )
        if account:
            sql += "  AND target_account=%s "
            params.append(account)
        if blocked_projects:
            placeholders = ",".join(["%s"] * len(blocked_projects))
            sql += f"  AND (project_name IS NULL OR project_name NOT IN ({placeholders})) "
            params.extend(sorted(blocked_projects))
        sql += "ORDER BY post_number ASC LIMIT 1"
        cur.execute(sql, params)
        return cur.fetchone()

    # Project diversity cooldown applies to product drafts only. Hard rule:
    # same project_name cannot appear within the last N posted rows on this
    # account. If no product draft survives the filter, fall back to organic
    # (which is not subject to the cooldown since its project_name is NULL).
    # We do NOT relax the cooldown window, because the whole point is to
    # prevent the exact same product from posting twice in a short stretch.
    row = None
    fallback = False
    fallback_from = None
    cooldown_blocked = set()
    cooldown_window_used = 0
    cooldown_skipped_drafts = []

    def _pick_product_with_cooldown():
        nonlocal cooldown_blocked, cooldown_window_used, cooldown_skipped_drafts
        cooldown_blocked = _recent_project_names(cooldown_posts) if cooldown_posts > 0 else set()
        cooldown_window_used = cooldown_posts
        if cooldown_blocked:
            # Diagnostic: which drafts got filtered out by the cooldown
            if account:
                cur.execute(
                    "SELECT post_number, project_name FROM media_posts "
                    "WHERE status='draft' AND post_type='product' "
                    "  AND target_account=%s AND project_name = ANY(%s) "
                    "ORDER BY post_number ASC",
                    (account, list(cooldown_blocked)),
                )
            else:
                cur.execute(
                    "SELECT post_number, project_name FROM media_posts "
                    "WHERE status='draft' AND post_type='product' "
                    "  AND project_name = ANY(%s) "
                    "ORDER BY post_number ASC",
                    (list(cooldown_blocked),),
                )
            cooldown_skipped_drafts = [(r[0], r[1]) for r in cur.fetchall()]
        return _draft_query("product", blocked_projects=cooldown_blocked)

    if target == "product":
        row = _pick_product_with_cooldown()
    else:
        row = _draft_query(target)

    if row is None:
        # Fall back to the other type if this one has no drafts (either truly
        # empty for organic, or all-cooldown-blocked for product). For
        # product->organic fallback this preserves cadence without weakening
        # the cooldown.
        other = "product" if target == "organic" else "organic"
        if other == "product":
            row = _pick_product_with_cooldown()
        else:
            row = _draft_query(other)
        if row is None:
            sys.stderr.write(
                "queue empty: no draft rows for either organic or product"
                + (f" (target_account={account})" if account else "")
                + (
                    f" (cooldown blocked product drafts: {cooldown_skipped_drafts})"
                    if cooldown_skipped_drafts else ""
                )
                + "\n"
            )
            sys.exit(2)
        sys.stderr.write(
            f"queue imbalance: target={target} has 0 drafts"
            + (
                f" (cooldown blocked product drafts: {cooldown_skipped_drafts})"
                if target == "product" and cooldown_skipped_drafts else ""
            )
            + f", falling back to {other}"
            + (f" (target_account={account})" if account else "")
            + "\n"
        )
        fallback_from = target
        target = other
        fallback = True

    post_number, video_path, project_name = row

    if target == "product":
        sys.stderr.write(
            f"[ig_picker] cooldown account={account or '<global>'} "
            f"window={cooldown_window_used} "
            f"blocked={sorted(cooldown_blocked) or '[]'} "
            f"chose=project_name={project_name} post_number={post_number}\n"
        )

    out = {
        "post_type": target,
        "video_path": video_path,
        "post_number": post_number,
        "target_account": account,
        "project_name": project_name,
        "reason": (
            f"window={window_days}d account={account or '<global>'} "
            f"recent={recent_counts} config_weights={type_weights_cfg} "
            f"effective={effective} chose={target}"
            + (f" (fallback_from={fallback_from})" if fallback else "")
            + (
                f" cooldown_window={cooldown_window_used}"
                f" cooldown_blocked={sorted(cooldown_blocked)}"
                + (f" cooldown_skipped_drafts={cooldown_skipped_drafts}" if cooldown_skipped_drafts else "")
                if target == "product" else ""
            )
        ),
        "fallback": fallback,
        "cooldown_window": cooldown_window_used if target == "product" else None,
        "cooldown_blocked_projects": sorted(cooldown_blocked) if target == "product" else [],
        "cooldown_skipped_drafts": cooldown_skipped_drafts if target == "product" else [],
    }
    print(json.dumps(out))
    conn.close()


if __name__ == "__main__":
    main()
