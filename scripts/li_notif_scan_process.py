#!/usr/bin/env python3
"""One-shot processor for the LinkedIn notifications discovery scan.

Reads the saved bh_run extraction output (ACCUMULATED_ACTIONABLE JSON line),
derives parent activity/ugcPost id from each comment_urn, dedups against the
replies/posts tables, and inserts new pending replies. Prints the required
LINKEDIN_SCAN_SUMMARY marker line.
"""
import json
import os
import re
import sys

import psycopg2

EXTRACT_FILE = "/Users/matthewdi/.claude/projects/-/0152159f-c679-488f-954a-8312a9a58060/tool-results/toolu_01FaaCg5i5D5xp5ELcp1TiVS.txt"

EXCLUDED_AUTHORS = {"louis030195", "louis3195"}
OWN_NAMES = {"matthew diakonov", "m13v"}

DRY_RUN = "--apply" not in sys.argv


def load_env():
    env_path = os.path.expanduser("~/social-autoposter/.env")
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            v = v.strip().strip('"').strip("'")
            os.environ.setdefault(k.strip(), v)


def load_actionable():
    raw = open(EXTRACT_FILE).read()
    obj = json.loads(raw)
    inner = json.loads(obj["result"])
    stdout = inner["stdout"]
    for line in stdout.splitlines():
        if line.startswith("ACCUMULATED_ACTIONABLE:"):
            return json.loads(line[len("ACCUMULATED_ACTIONABLE:"):].strip())
    raise SystemExit("ACCUMULATED_ACTIONABLE not found in extract file")


def parse_parent(comment_urn):
    """urn:li:comment:(activity:PARENT,COMMENT) or (ugcPost:PARENT,COMMENT)."""
    if not comment_urn:
        return None, None
    m = re.match(r"urn:li:comment:\((activity|ugcPost):(\d+),(\d+)\)", comment_urn)
    if not m:
        return None, None
    ns, parent_id, _comment_id = m.group(1), m.group(2), m.group(3)
    return ns, parent_id


def main():
    load_env()
    items = load_actionable()

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()

    cur.execute("SELECT their_comment_id FROM replies WHERE platform='linkedin';")
    existing_comment_ids = {r[0] for r in cur.fetchall() if r[0]}

    cur.execute(
        "SELECT DISTINCT r.their_author || '|||' || p.our_url "
        "FROM replies r JOIN posts p ON r.post_id = p.id "
        "WHERE r.platform='linkedin' AND r.status IN ('replied','pending','processing');"
    )
    engaged_pairs = {r[0] for r in cur.fetchall() if r[0]}

    cur.execute(
        "SELECT id, our_url, thread_url FROM posts "
        "WHERE platform='linkedin' AND status='active';"
    )
    posts = cur.fetchall()  # (id, our_url, thread_url)

    counts = dict(scanned=len(items), new=0, already=0, author_engaged=0,
                  excluded=0, own=0, no_urn=0, within_batch_dup=0)
    inserted_keys = set()  # (author_lower, parent_id) chosen this run
    actions = []

    for it in items:
        author = (it.get("author") or "").strip()
        author_l = author.lower()
        comment_urn = it.get("comment_urn")
        snippet = it.get("snippet") or ""
        href = it.get("href") or ""
        ns, parent_id = parse_parent(comment_urn)

        if not comment_urn or not parent_id:
            counts["no_urn"] += 1
            actions.append(("no_comment_urn", author, comment_urn))
            continue
        if comment_urn in existing_comment_ids:
            counts["already"] += 1
            actions.append(("already_tracked", author, comment_urn))
            continue
        if author_l in EXCLUDED_AUTHORS or any(e in author_l for e in EXCLUDED_AUTHORS):
            counts["excluded"] += 1
            actions.append(("excluded_author", author, comment_urn))
            continue
        if author_l in OWN_NAMES:
            counts["own"] += 1
            actions.append(("own_account", author, comment_urn))
            continue

        # match a post by parent_id appearing in our_url or thread_url
        post_id = None
        our_url = None
        for pid, pu, tu in posts:
            if (pu and parent_id in pu) or (tu and parent_id in tu):
                post_id = pid
                our_url = pu
                break

        # author+post dedup against existing engaged pairs
        if our_url:
            apk = author + "|||" + our_url
            if apk in engaged_pairs:
                counts["author_engaged"] += 1
                actions.append(("author_already_engaged", author, comment_urn))
                continue

        # within-batch dedup: one reply per author per thread
        batch_key = (author_l, parent_id)
        if batch_key in inserted_keys:
            counts["within_batch_dup"] += 1
            actions.append(("within_batch_dup", author, comment_urn))
            continue

        # need a post_id; create one if no match
        if post_id is None:
            thread_url = ("https://www.linkedin.com/feed/update/"
                          "urn:li:%s:%s/" % (ns, parent_id))
            if DRY_RUN:
                actions.append(("WOULD_CREATE_POST+INSERT", author, comment_urn))
                post_id = -1  # placeholder for dry run
            else:
                cur.execute(
                    "INSERT INTO posts (platform, thread_url, our_url, "
                    "our_content, our_account, project_name, engagement_style, "
                    "status, posted_at) "
                    "VALUES ('linkedin', %s, %s, %s, %s, 'general', "
                    "'curious_probe', 'active', NOW()) RETURNING id;",
                    (thread_url, thread_url,
                     "[discovered thread, engagement reply pending]",
                     "Matthew Diakonov"),
                )
                post_id = cur.fetchone()[0]
                our_url = thread_url

        if DRY_RUN:
            actions.append(("WOULD_INSERT", author, comment_urn, "post_id=%s" % post_id))
        else:
            cur.execute(
                "INSERT INTO replies (post_id, platform, their_comment_id, "
                "their_author, their_content, their_comment_url, depth, status) "
                "VALUES (%s, 'linkedin', %s, %s, %s, %s, 1, 'pending');",
                (post_id, comment_urn, author, snippet, href),
            )

        inserted_keys.add(batch_key)
        counts["new"] += 1

    if not DRY_RUN:
        conn.commit()
        # atomic post-commit verification in the same process
        vcur = conn.cursor()
        vcur.execute(
            "SELECT id, post_id, their_author, status FROM replies "
            "WHERE platform='linkedin' AND their_comment_id = ANY(%s) "
            "ORDER BY id;",
            ([k for k in []] or [it.get("comment_urn") for it in items],),
        )
        verify_rows = vcur.fetchall()
        vcur.close()
        print("=== POST-COMMIT VERIFY: %d reply rows for scanned URNs ===" % len(verify_rows))
        for vr in verify_rows:
            print("  reply id=%s post_id=%s author=%s status=%s" % vr)
    cur.close()
    conn.close()

    print("=== ACTION LOG (%s) ===" % ("DRY RUN" if DRY_RUN else "APPLIED"))
    for a in actions:
        print("  " + " | ".join(str(x) for x in a))
    print()
    print("%d new replies discovered" % counts["new"])
    print("%d already tracked" % counts["already"])
    print("%d author already engaged on thread" % counts["author_engaged"])
    print("%d within-batch duplicates (one reply per author per thread)" % counts["within_batch_dup"])
    print("%d excluded" % counts["excluded"])
    print("%d own account" % counts["own"])
    print("%d no comment URN" % counts["no_urn"])
    print()
    excluded_total = counts["excluded"] + counts["own"]
    print("LINKEDIN_SCAN_SUMMARY: scanned=%d new=%d already=%d excluded=%d unmatched=%d" % (
        counts["scanned"], counts["new"], counts["already"], excluded_total, counts["no_urn"]))


if __name__ == "__main__":
    main()
