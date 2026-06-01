#!/usr/bin/env python3
"""Persist prospect profile data to the prospects table.

Subcommands:
  upsert  - Insert or update a prospect row and return the prospect_id.
  get     - Print a prospect row as JSON (for use from shell/Claude prompts).
  link    - Link an existing dms row to a prospect by platform+author.

The scraping itself is driven by Claude via the per-platform MCP browser
agents (reddit-agent, twitter-harness, linkedin-agent). This script only
handles DB persistence: Claude collects the fields and passes them in.

Usage:
  python3 fetch_prospect_profile.py upsert \\
      --platform linkedin --author "Karl Treen" \\
      --profile-url https://linkedin.com/in/karltreen \\
      --headline "CEO at Foo" --bio "..." --company Foo --role CEO

  python3 fetch_prospect_profile.py get --platform linkedin --author "Karl Treen"

  python3 fetch_prospect_profile.py link --dm-id 510
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as dbmod

# Columns on the prospects table that callers may set (besides platform/author).
UPDATABLE_COLS = [
    "profile_url",
    "display_name",
    "headline",
    "bio",
    "follower_count",
    "recent_activity",
    "company",
    "role",
    "notes",
]


def upsert_prospect(conn, platform, author, fields):
    """Insert (platform, author) if missing, then update any provided fields.

    Always stamps profile_fetched_at=NOW() when any field is provided.
    Returns the prospect_id.
    """
    # Ensure row exists.
    conn.execute(
        """
        INSERT INTO prospects (platform, author)
        VALUES (%s, %s)
        ON CONFLICT ON CONSTRAINT prospects_platform_author_unique DO NOTHING
        """,
        (platform, author),
    )

    # Fetch id.
    cur = conn.execute(
        "SELECT id FROM prospects WHERE platform=%s AND author=%s",
        (platform, author),
    )
    row = cur.fetchone()
    if not row:
        conn.commit()
        cur = conn.execute(
            "SELECT id FROM prospects WHERE platform=%s AND author=%s",
            (platform, author),
        )
        row = cur.fetchone()
    prospect_id = row["id"]

    # Apply any non-null, non-empty field updates.
    sets = []
    params = []
    for col in UPDATABLE_COLS:
        val = fields.get(col)
        if val is None:
            continue
        if isinstance(val, str) and val.strip() == "":
            continue
        sets.append(f"{col} = %s")
        params.append(val)

    if sets:
        sets.append("profile_fetched_at = NOW()")
        sql = f"UPDATE prospects SET {', '.join(sets)} WHERE id = %s"
        params.append(prospect_id)
        conn.execute(sql, params)

    conn.commit()
    return prospect_id


def get_prospect(conn, platform, author):
    cur = conn.execute(
        """
        SELECT id, platform, author, profile_url, display_name, headline, bio,
               follower_count, recent_activity, company, role,
               profile_fetched_at, notes, created_at
        FROM prospects WHERE platform=%s AND author=%s
        """,
        (platform, author),
    )
    row = cur.fetchone()
    if not row:
        return None
    d = dict(row)
    for k, v in d.items():
        if hasattr(v, "isoformat"):
            d[k] = v.isoformat()
    return d


def link_dm(conn, dm_id):
    """Link dms.prospect_id to the matching prospect row by (platform, their_author)."""
    cur = conn.execute(
        "SELECT platform, their_author FROM dms WHERE id=%s",
        (dm_id,),
    )
    row = cur.fetchone()
    if not row:
        print(f"ERROR: DM #{dm_id} not found", file=sys.stderr)
        return None
    platform = row["platform"]
    author = row["their_author"]

    cur = conn.execute(
        "SELECT id FROM prospects WHERE platform=%s AND author=%s",
        (platform, author),
    )
    prow = cur.fetchone()
    if not prow:
        print(
            f"ERROR: no prospect row for {platform}:{author}; run `upsert` first",
            file=sys.stderr,
        )
        return None

    prospect_id = prow["id"]
    conn.execute(
        "UPDATE dms SET prospect_id=%s WHERE id=%s", (prospect_id, dm_id)
    )
    conn.commit()
    return prospect_id


def _http_upsert(args):
    """DB-free upsert via POST /api/v1/prospects. Returns prospect_id (int).

    Mirrors upsert_prospect(): we always create-or-update the (platform,author)
    row and stamp profile_fetched_at only when at least one field was supplied.
    """
    from http_api import api_post

    field_map = {
        "profile_url": args.profile_url,
        "display_name": args.display_name,
        "headline": args.headline,
        "bio": args.bio,
        "follower_count": args.follower_count,
        "recent_activity": args.recent_activity,
        "company": args.company,
        "role": args.role,
        "notes": args.notes,
    }
    body = {"platform": args.platform, "author": args.author}
    has_field = False
    for k, v in field_map.items():
        if v is None:
            continue
        if isinstance(v, str) and v.strip() == "":
            continue
        body[k] = v
        has_field = True
    # Match the DB path: profile_fetched_at is stamped only when a field was set.
    if has_field:
        body["profile_fetched_at_now"] = True

    resp = api_post("/api/v1/prospects", body)
    prospect = (resp.get("data") or {}).get("prospect") or {}
    return prospect.get("id")


def _http_get(args):
    """DB-free get via GET /api/v1/prospects?platform&author. Returns dict|None."""
    from http_api import api_get

    resp = api_get(
        "/api/v1/prospects",
        query={"platform": args.platform, "author": args.author},
        ok_on_404=True,
    )
    if not resp.get("ok"):
        return None
    return (resp.get("data") or {}).get("prospect")


def _http_link_dm(dm_id):
    """DB-free link via GET dms -> GET prospect -> PATCH dms.prospect_id.

    Returns prospect_id (int) or None, printing the same stderr the DB path does.
    """
    from http_api import api_get, api_patch

    dm_resp = api_get(f"/api/v1/dms/{dm_id}", ok_on_404=True)
    if not dm_resp.get("ok"):
        print(f"ERROR: DM #{dm_id} not found", file=sys.stderr)
        return None
    dm = (dm_resp.get("data") or {}).get("dm") or {}
    platform = dm.get("platform")
    author = dm.get("their_author")

    p_resp = api_get(
        "/api/v1/prospects",
        query={"platform": platform, "author": author},
        ok_on_404=True,
    )
    prospect = (p_resp.get("data") or {}).get("prospect") if p_resp.get("ok") else None
    if not prospect:
        print(
            f"ERROR: no prospect row for {platform}:{author}; run `upsert` first",
            file=sys.stderr,
        )
        return None

    prospect_id = prospect.get("id")
    api_patch(f"/api/v1/dms/{dm_id}", {"prospect_id": prospect_id})
    return prospect_id


def _http_dispatch(args):
    """Handle a subcommand over HTTP when DATABASE_URL is absent.

    Prints identical stdout to the DB path. Exits the process on the same
    conditions (get-miss -> exit 1; link-failure -> exit 1).
    """
    from http_api import api_patch

    if args.cmd == "upsert":
        pid = _http_upsert(args)
        if args.link_dm is not None:
            api_patch(f"/api/v1/dms/{args.link_dm}", {"prospect_id": pid})
        if args.json:
            out = _http_get(args) or {"id": pid}
            print(json.dumps(out))
        else:
            print(f"prospect_id={pid}")
        return
    if args.cmd == "get":
        row = _http_get(args)
        if row is None:
            print("null")
            sys.exit(1)
        print(json.dumps(row, indent=2))
        return
    if args.cmd == "link":
        pid = _http_link_dm(args.dm_id)
        if pid is None:
            sys.exit(1)
        print(f"prospect_id={pid} linked to DM #{args.dm_id}")
        return


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    up = sub.add_parser("upsert", help="Insert or update a prospect row")
    up.add_argument("--platform", required=True, choices=["reddit", "twitter", "linkedin"])
    up.add_argument("--author", required=True)
    up.add_argument("--profile-url")
    up.add_argument("--display-name")
    up.add_argument("--headline")
    up.add_argument("--bio")
    up.add_argument("--follower-count", type=int)
    up.add_argument("--recent-activity")
    up.add_argument("--company")
    up.add_argument("--role")
    up.add_argument("--notes")
    up.add_argument(
        "--link-dm",
        type=int,
        help="Also set dms.prospect_id on this dm_id after upsert",
    )
    up.add_argument(
        "--json", action="store_true", help="Emit {id,platform,author,...} as JSON"
    )

    gp = sub.add_parser("get", help="Print a prospect row as JSON")
    gp.add_argument("--platform", required=True)
    gp.add_argument("--author", required=True)

    lk = sub.add_parser("link", help="Link a dms row to its prospect by platform+author")
    lk.add_argument("--dm-id", type=int, required=True)

    args = ap.parse_args()

    # HTTP-only lane: every subcommand routes through the s4l.ai HTTP API. The
    # direct-Postgres lane was removed 2026-06-01 — there is NO database-driven
    # path any more, not as primary, not as fallback. DATABASE_URL, if present
    # in the environment, is deliberately ignored; all reads/writes go through
    # _http_dispatch against /api/v1/prospects and /api/v1/dms.
    dbmod.load_env()
    _http_dispatch(args)


if __name__ == "__main__":
    main()
