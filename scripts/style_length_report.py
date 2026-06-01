#!/usr/bin/env python3
"""Target-vs-realized comment-length report, grouped by engagement_style.

The "fact" half of the target_chars system. Each engagement style now carries
a `target_chars` (the length THIS style is supposed to win at, biased toward
the top-human-reply median). This script answers: for the comments we actually
posted, how long did they come out, and how far is that from the target?

It joins two things per style:
  1. target_chars  — the authoritative target from the live registry
                     (engagement_styles.get_all_styles(); falls back to the
                     in-process STYLES dict / DEFAULT_TARGET_CHARS if the API
                     is unreachable).
  2. realized length — LENGTH() of the comment text we posted, pulled from BOTH
                     Twitter rails: the post rail (`posts`, platform='twitter')
                     and the engage rail (`replies`, platform='x'). Reddit /
                     LinkedIn / GitHub / Moltbook are selectable via --platform.

For each style it reports n, the target, realized p25/p50/p75/avg, the delta
(median realized minus target; positive = we ran long), and the engagement
proxy (avg views, avg likes) so you can A/B whether landing near the target
actually helps. Sorted by n desc.

Usage
-----
  python3 scripts/style_length_report.py                 # twitter, last 30d
  python3 scripts/style_length_report.py --days 14
  python3 scripts/style_length_report.py --platform reddit
  python3 scripts/style_length_report.py --json          # machine-readable
  python3 scripts/style_length_report.py --min-n 10      # hide thin styles

This is read-only. No writes, no locks.
"""
import argparse
import json
import os
import sys

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_DIR, "scripts"))

from db import get_conn  # noqa: E402

# Per-platform mapping of where live comment text lives. Twitter is the only
# platform that splits across two rails (post + engage); the rest live in one
# table. Each spec: (table, text_col, time_col, platform_values, live_statuses,
# views_col, likes_col).
PLATFORM_RAILS = {
    "twitter": [
        ("posts", "our_content", "posted_at", ("twitter",),
         ("active", "posted"), "views", "upvotes"),
        ("replies", "our_reply_content", "replied_at", ("x",),
         ("replied",), "views", "upvotes"),
    ],
    "reddit": [
        ("posts", "our_content", "posted_at", ("reddit",),
         ("active", "posted"), "views", "upvotes"),
        ("replies", "our_reply_content", "replied_at", ("reddit",),
         ("replied",), "views", "upvotes"),
    ],
    "linkedin": [
        ("posts", "our_content", "posted_at", ("linkedin",),
         ("active", "posted"), "views", "upvotes"),
    ],
    "github": [
        ("posts", "our_content", "posted_at", ("github",),
         ("active", "posted"), "views", "upvotes"),
    ],
    "moltbook": [
        ("replies", "our_reply_content", "replied_at", ("moltbook",),
         ("replied",), "views", "upvotes"),
    ],
}


def fetch_rail_rows(conn, rail, days):
    table, text_col, time_col, plats, statuses, views_col, likes_col = rail
    plat_ph = ",".join(["%s"] * len(plats))
    stat_ph = ",".join(["%s"] * len(statuses))
    # target_chars is the per-post SNAPSHOT (frozen at post time). NULL on rows
    # predating the snapshot wiring; summarize() falls back to the live registry
    # target for those so coverage degrades gracefully.
    sql = f"""
        SELECT
            COALESCE(engagement_style, '(none)')          AS style,
            LENGTH(TRIM({text_col}))                       AS clen,
            COALESCE({views_col}, 0)                       AS views,
            COALESCE({likes_col}, 0)                       AS likes,
            target_chars                                   AS snap_target
        FROM {table}
        WHERE platform IN ({plat_ph})
          AND status   IN ({stat_ph})
          AND {time_col} >= NOW() - (%s || ' days')::INTERVAL
          AND {text_col} IS NOT NULL
          AND LENGTH(TRIM({text_col})) > 0
    """
    params = list(plats) + list(statuses) + [str(days)]
    cur = conn.execute(sql, params)
    rows = cur.fetchall()
    cur.close()
    return [
        {"style": r[0], "clen": int(r[1]), "views": int(r[2]),
         "likes": int(r[3]),
         "snap_target": int(r[4]) if r[4] is not None else None}
        for r in rows
    ]


def load_targets():
    """name -> target_chars from the live registry (with cold-start fallback)."""
    targets = {}
    try:
        from engagement_styles import get_all_styles, DEFAULT_TARGET_CHARS
        for name, meta in get_all_styles().items():
            tc = (meta or {}).get("target_chars")
            try:
                targets[name] = int(tc) if tc else DEFAULT_TARGET_CHARS
            except (TypeError, ValueError):
                targets[name] = DEFAULT_TARGET_CHARS
    except Exception as e:
        sys.stderr.write(
            f"[style_length_report] could not load registry targets ({e}); "
            "report will show target=? for all styles\n"
        )
    return targets


def pct(sorted_vals, p):
    if not sorted_vals:
        return 0
    k = (len(sorted_vals) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    if lo == hi:
        return sorted_vals[lo]
    return round(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo))


def summarize(rows, targets):
    by_style = {}
    for r in rows:
        by_style.setdefault(r["style"], []).append(r)
    out = []
    for style, items in by_style.items():
        lens = sorted(x["clen"] for x in items)
        n = len(items)
        med = pct(lens, 50)
        # Headline target: the per-post snapshot median when any row carries one
        # (frozen, drift-proof, the true "what we told it to aim for"), else the
        # live registry target as fallback. snap_n shows how many rows are on
        # the snapshot path yet.
        snaps = sorted(x["snap_target"] for x in items if x["snap_target"])
        if snaps:
            target = pct(snaps, 50)
        else:
            target = targets.get(style)
        out.append({
            "style": style,
            "n": n,
            "snap_n": len(snaps),
            "target_chars": target,
            "p25": pct(lens, 25),
            "p50": med,
            "p75": pct(lens, 75),
            "avg": round(sum(lens) / n),
            "delta": (med - target) if target is not None else None,
            "avg_views": round(sum(x["views"] for x in items) / n, 1),
            "avg_likes": round(sum(x["likes"] for x in items) / n, 2),
        })
    out.sort(key=lambda d: d["n"], reverse=True)
    return out


def overall(rows, targets):
    if not rows:
        return {}
    lens = sorted(r["clen"] for r in rows)
    # target per row (so the weighted target reflects the style mix we posted)
    tlist = [targets.get(r["style"]) for r in rows]
    tlist = [t for t in tlist if t is not None]
    return {
        "n": len(rows),
        "realized_p50": pct(lens, 50),
        "realized_avg": round(sum(lens) / len(lens)),
        "target_p50_weighted": pct(sorted(tlist), 50) if tlist else None,
        "target_avg_weighted": round(sum(tlist) / len(tlist)) if tlist else None,
    }


def render_table(report, ov, platform, days):
    lines = []
    lines.append(
        f"Style length report  platform={platform}  window={days}d  "
        f"comments={ov.get('n', 0)}"
    )
    if ov:
        lines.append(
            f"  OVERALL realized median={ov['realized_p50']}  "
            f"avg={ov['realized_avg']}   "
            f"target(weighted) median={ov['target_p50_weighted']}  "
            f"avg={ov['target_avg_weighted']}"
        )
        if ov.get("target_avg_weighted"):
            over = ov["realized_avg"] - ov["target_avg_weighted"]
            ratio = ov["realized_avg"] / ov["target_avg_weighted"]
            lines.append(
                f"  => running {over:+d} chars vs target on average "
                f"({ratio:.1f}x)"
            )
    lines.append("")
    hdr = (f"{'style':28} {'n':>5} {'tgt':>5} {'p25':>5} {'p50':>5} "
           f"{'p75':>5} {'avg':>5} {'delta':>6} {'views':>7} {'likes':>6}")
    lines.append(hdr)
    lines.append("-" * len(hdr))
    for r in report:
        tgt = "?" if r["target_chars"] is None else str(r["target_chars"])
        delta = "" if r["delta"] is None else f"{r['delta']:+d}"
        lines.append(
            f"{r['style'][:28]:28} {r['n']:>5} {tgt:>5} {r['p25']:>5} "
            f"{r['p50']:>5} {r['p75']:>5} {r['avg']:>5} {delta:>6} "
            f"{r['avg_views']:>7} {r['avg_likes']:>6}"
        )
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--platform", default="twitter",
                    choices=sorted(PLATFORM_RAILS.keys()))
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--min-n", type=int, default=1,
                    help="Hide styles with fewer than N comments.")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    targets = load_targets()
    conn = get_conn()
    try:
        rows = []
        for rail in PLATFORM_RAILS[args.platform]:
            rows.extend(fetch_rail_rows(conn, rail, args.days))
    finally:
        conn.close()

    report = [r for r in summarize(rows, targets) if r["n"] >= args.min_n]
    ov = overall(rows, targets)

    if args.json:
        print(json.dumps(
            {"platform": args.platform, "days": args.days,
             "overall": ov, "styles": report},
            indent=2, default=str,
        ))
    else:
        print(render_table(report, ov, args.platform, args.days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
