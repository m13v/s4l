#!/usr/bin/env python3
"""Who is actually using social-autoposter right now.

Reads the `installations` heartbeat table (the only live per-install signal) and
answers "how many real, external people are active" without the inflation that a
raw install count carries:

  * install_id is per identity.json, NOT per machine. A reinstall / reset / each
    ephemeral mk0r E2B sandbox mints a fresh id. So we dedupe by `hardware_uuid`
    (the stable per-machine key) and report MACHINES, not install rows.
  * Our own infra (i@m13v.com operator Mac, the agent@mk0r.com / e2b.local VM
    fleet) is filtered out by default so the roster is real customers. Pass
    --all to include it.
  * Cross-references the `posts` table so you can see the alive-but-not-posting
    gap (the blind spot the Cloud Logging stream exists to explain).

Usage:
  python3 scripts/active_users.py                 # external machines, last 7d
  python3 scripts/active_users.py --days 30        # different window
  python3 scripts/active_users.py --all            # include our own infra
  python3 scripts/active_users.py --json           # machine-readable

Operator-local only: uses the direct-Postgres lane via scripts/db.py (absent in
the shipped npm package), reading DATABASE_URL from ~/social-autoposter/.env.
"""

import argparse
import json
import os
import sys
from urllib.parse import unquote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import load_env, get_conn  # noqa: E402
# Our own installs, hidden by default so the roster is real external users.
# Shared with autopilot_stall_watch.py via identity.py so the two lists can't
# drift apart (see identity.is_internal_install).
from identity import INTERNAL_EMAILS, INTERNAL_HOSTNAME_SUBSTR, INTERNAL_HARDWARE_UUIDS  # noqa: E402

# Connected X handle resolves only from posts.our_account; drop scaffolding values.
PLACEHOLDER_HANDLES = {"your-twitter-handle", "your_handle", "your-handle", "none", "null", ""}


def parse_handles(raw):
    out = []
    for h in (raw or "").split(","):
        h = h.strip().lstrip("@")
        if h and h.lower() not in PLACEHOLDER_HANDLES and h not in out:
            out.append(h)
    return out


def is_internal(emails, hostnames, hardware_uuids):
    if any((e or "").lower() in INTERNAL_EMAILS for e in emails):
        return True
    if any(sub in (h or "") for h in hostnames for sub in INTERNAL_HOSTNAME_SUBSTR):
        return True
    if any((u or "") in INTERNAL_HARDWARE_UUIDS for u in hardware_uuids):
        return True
    return False


def fetch(days):
    # One row per MACHINE (hardware_uuid; fall back to a per-install key when the
    # client never reported a hardware_uuid so those installs aren't all merged).
    # `days` is an argparse int (injection-safe), inlined because the wrapper's
    # SQL translation mangles %s placeholders.
    days = int(days)
    q = f"""
    WITH win AS (
      SELECT *,
             COALESCE(NULLIF(git_email, ''), NULLIF(hardware_uuid, ''),
                      'anon:' || install_id::text) AS entity_key
      FROM installations
      WHERE last_seen_at > now() - interval '{days} days'
    ),
    posted AS (
      SELECT install_id, count(*) AS n
      FROM posts
      WHERE posted_at > now() - interval '{days} days' AND install_id IS NOT NULL
      GROUP BY install_id
    ),
    handles AS (
      -- The connected X handle is NOT in the heartbeat; it only reaches the
      -- central DB via posts.our_account, so it exists ONLY for installs that
      -- ever posted (all-time, not windowed: a handle is identity, not activity).
      SELECT install_id, string_agg(DISTINCT our_account, ',') AS hs
      FROM posts
      WHERE our_account IS NOT NULL AND length(trim(our_account)) > 0
      GROUP BY install_id
    )
    SELECT
      w.entity_key,
      count(DISTINCT w.install_id)                                            AS installs,
      count(DISTINCT w.hardware_uuid)                                         AS machines,
      array_remove(array_agg(DISTINCT w.hardware_uuid), NULL)                 AS hardware_uuids,
      array_remove(array_agg(DISTINCT NULLIF(w.git_email, '')), NULL)         AS emails,
      array_remove(array_agg(DISTINCT w.hostname), NULL)                      AS hostnames,
      string_agg(DISTINCT h.hs, ',')                                          AS handles_raw,
      max(w.os_version)                                                       AS os,
      array_remove(array_agg(DISTINCT
        w.last_country || '/' || COALESCE(w.last_city, '-')), NULL)           AS locations,
      max(w.last_seen_at)                                                     AS last_seen,
      COALESCE(sum(p.n), 0)                                                   AS posts
    FROM win w
    LEFT JOIN posted p  ON p.install_id = w.install_id
    LEFT JOIN handles h ON h.install_id = w.install_id
    GROUP BY w.entity_key
    ORDER BY last_seen DESC;
    """
    conn = get_conn()
    try:
        cur = conn.execute(q)
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def person(row):
    if row["emails"]:
        return row["emails"][0]
    if row["hostnames"]:
        return row["hostnames"][0]
    return row["entity_key"][:12]


def loc(row):
    return ", ".join(unquote(x) for x in (row["locations"] or [])) or "?"


def main():
    ap = argparse.ArgumentParser(
        description="Active social-autoposter users, deduped per person (email, else machine).")
    ap.add_argument("--days", type=int, default=7, help="lookback window in days (default 7)")
    ap.add_argument("--all", action="store_true", help="include our own infra (i@m13v / mk0r)")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args()

    load_env()
    rows = fetch(args.days)
    for r in rows:
        r["internal"] = is_internal(r["emails"], r["hostnames"], r["hardware_uuids"])
        r["handles"] = parse_handles(r.get("handles_raw"))

    external = [r for r in rows if not r["internal"]]
    internal = [r for r in rows if r["internal"]]
    shown = rows if args.all else external

    if args.json:
        out = [{
            "person": person(r), "x_handles": r["handles"], "machines": r["machines"],
            "installs": r["installs"], "hostnames": r["hostnames"], "emails": r["emails"],
            "os": r["os"], "location": loc(r), "posts": int(r["posts"]),
            "last_seen": r["last_seen"].isoformat() if r["last_seen"] else None,
            "internal": r["internal"],
        } for r in shown]
        print(json.dumps({
            "window_days": args.days,
            "external_machines": len(external),
            "external_people": len({e for r in external for e in r["emails"]}),
            "internal_machines_hidden": 0 if args.all else len(internal),
            "rows": out,
        }, indent=2))
        return

    people = len({e for r in external for e in r["emails"]})
    print(f"\nActive in last {args.days}d: {len(external)} external machines "
          f"(~{people} identified people){'' if args.all else f', {len(internal)} internal hidden'}\n")
    hdr = (f"{'PERSON':<30} {'X HANDLE':<16} {'HOST':<22} {'OS':<7} {'LOC':<16} "
           f"{'INST':>4} {'POSTS':>6}  LAST SEEN")
    print(hdr)
    print("-" * len(hdr))
    for r in shown:
        tag = "  [internal]" if r["internal"] else ""
        host = (r["hostnames"][0] if r["hostnames"] else "?")[:22]
        handle = (", ".join(r["handles"]) or "-")[:16]
        print(f"{person(r)[:30]:<30} {handle:<16} {host:<22} {(r['os'] or '?'):<7} "
              f"{loc(r)[:16]:<16} {r['installs']:>4} {int(r['posts']):>6}  "
              f"{r['last_seen']:%Y-%m-%d %H:%M}{tag}")
    posting = sum(1 for r in external if r["posts"] > 0)
    print(f"\n  of {len(external)} external machines, {posting} posted in the window, "
          f"{len(external) - posting} are alive-but-not-posting.\n")


if __name__ == "__main__":
    main()
