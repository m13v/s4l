#!/opt/homebrew/bin/python3.11
"""
Pick the next IG post type (organic vs product) and the next pending video of
that type. Writes one JSON line to stdout for the shell harness to read.

Algorithm: deterministic 4:1 sliding window. Look at the last 5 posted IG
rows; if fewer than 4 are organic, post organic; otherwise post product. Over
time this locks to exactly 4 organic + 1 product per 5 posts.

Output:
  {"post_type": "organic", "video_path": "...", "post_number": 4,
   "reason": "last5=[product,organic,product] organic=1 target=organic",
   "fallback": false}

Exit codes:
  0  — picked successfully
  2  — no draft videos of either type (queue exhausted)
  3  — config error / DB error
"""

import json
import sys
from pathlib import Path

ENV_FILE = Path.home() / "social-autoposter" / ".env"
WINDOW = 5
ORGANIC_TARGET = 4  # of last WINDOW posts


def load_env():
    env = {}
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def main():
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

    conn = psycopg2.connect(db_url)
    cur = conn.cursor()

    cur.execute(
        "SELECT post_type FROM media_posts "
        "WHERE status='posted' AND posted_urls ? 'instagram' "
        "ORDER BY posted_at DESC NULLS LAST LIMIT %s",
        (WINDOW,),
    )
    last5 = [r[0] for r in cur.fetchall()]
    organic_count = sum(1 for t in last5 if t == "organic")

    target = "organic" if organic_count < ORGANIC_TARGET else "product"

    cur.execute(
        "SELECT post_number, video_path FROM media_posts "
        "WHERE status='draft' AND post_type=%s "
        "ORDER BY post_number ASC LIMIT 1",
        (target,),
    )
    row = cur.fetchone()
    fallback = False

    if row is None:
        other = "product" if target == "organic" else "organic"
        cur.execute(
            "SELECT post_number, video_path FROM media_posts "
            "WHERE status='draft' AND post_type=%s "
            "ORDER BY post_number ASC LIMIT 1",
            (other,),
        )
        row = cur.fetchone()
        if row is None:
            sys.stderr.write(
                f"queue empty: no draft rows for either organic or product\n"
            )
            sys.exit(2)
        sys.stderr.write(
            f"queue imbalance: target={target} has 0 drafts, falling back to {other}\n"
        )
        target = other
        fallback = True

    post_number, video_path = row

    out = {
        "post_type": target,
        "video_path": video_path,
        "post_number": post_number,
        "reason": (
            f"last{WINDOW}={last5} organic_count={organic_count} "
            f"target_organic_per_window={ORGANIC_TARGET} chose={target}"
        ),
        "fallback": fallback,
    }
    print(json.dumps(out))
    conn.close()


if __name__ == "__main__":
    main()
