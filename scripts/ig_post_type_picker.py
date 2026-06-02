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

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
from http_api import api_get

CONFIG_FILE = Path.home() / "social-autoposter" / "config.json"


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

    type_weights_cfg, window_days, cooldown_posts = load_ig_config()
    account = args.account

    # Single round trip: raw rows for weighting + cooldown + draft selection.
    # All weighting / cooldown / fallback logic stays local (HTTP-only).
    _ctx = api_get(
        "/api/v1/media-posts/ig-picker-context",
        query={
            "target_account": account,
            "window_days": window_days,
            "cooldown_posts": cooldown_posts,
        },
    )
    ctx = (_ctx.get("data") or {})
    recent_counts = dict(ctx.get("recent_type_counts") or {})
    recent_posted_projects = list(ctx.get("recent_posted_projects") or [])
    all_drafts = list(ctx.get("drafts") or [])
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
        # account (already account-scoped + NULL-excluded by the endpoint).
        # Returns a set; empty when window<=0 or no account scoping.
        if window <= 0 or not account:
            return set()
        return set(recent_posted_projects)

    def _draft_query(type_, blocked_projects=None):
        # First draft of this type from the endpoint's account-scoped list
        # (already ordered by post_number ASC). For product drafts, exclude
        # rows whose project_name is in blocked_projects (project diversity
        # cooldown). NULL project_name rows always pass (organic-shaped, never
        # on cooldown). Returns (post_number, video_path, project_name) or None.
        for d in all_drafts:
            if d.get("post_type") != type_:
                continue
            pn = d.get("project_name")
            if blocked_projects and pn is not None and pn in blocked_projects:
                continue
            return (d.get("post_number"), d.get("video_path"), pn)
        return None

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
            # Diagnostic: which product drafts got filtered out by the cooldown
            # (computed locally from the endpoint's draft list).
            cooldown_skipped_drafts = [
                (d.get("post_number"), d.get("project_name"))
                for d in all_drafts
                if d.get("post_type") == "product" and d.get("project_name") in cooldown_blocked
            ]
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


if __name__ == "__main__":
    main()
