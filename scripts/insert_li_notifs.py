#!/usr/bin/env python3
import json, os, sys
import psycopg2

BASE = os.path.dirname(__file__)
APPLY = "--apply" in sys.argv
DB = os.environ["DATABASE_URL"]
OUR_ACCOUNT = "Matthew Diakonov"

new_items = json.load(open(os.path.join(BASE, "li_notifs_new.json")))

conn = psycopg2.connect(DB)
conn.autocommit = False
cur = conn.cursor()

inserted_posts = 0
inserted_replies = 0
skipped = 0

for r in new_items:
    res = r["_resolved"]
    post_id = res["post_id"]
    our_url = res["our_url"]
    project = res["project"]
    their_comment_id = res["their_comment_id"]
    author = r["author"]
    snippet = r["snippet"]
    href = r["href"]

    if not post_id:
        # create a stub post row for this thread
        thread_url = our_url
        cur.execute(
            """INSERT INTO posts (platform, thread_url, our_url, our_content, our_account,
                                   project_name, status, is_recommendation)
               VALUES (%s,%s,%s,%s,%s,%s,'active', false)
               RETURNING id""",
            ("linkedin", thread_url, our_url, "[engagement-comment discovered via notifications]",
             OUR_ACCOUNT, project),
        )
        post_id = cur.fetchone()[0]
        inserted_posts += 1

    # insert the reply (guard against unique-constraint race)
    cur.execute(
        """INSERT INTO replies (post_id, platform, their_comment_id, their_author,
                                 their_content, their_comment_url, depth, status, project_name)
           VALUES (%s,'linkedin',%s,%s,%s,%s,1,'pending',%s)
           ON CONFLICT (platform, their_comment_id) DO NOTHING
           RETURNING id""",
        (post_id, their_comment_id, author, snippet, href, project),
    )
    row = cur.fetchone()
    if row:
        inserted_replies += 1
        print(f"  + reply id={row[0]} post_id={post_id} {author[:28]} ({their_comment_id})")
    else:
        skipped += 1
        print(f"  = skipped (conflict) {author[:28]} ({their_comment_id})")

if APPLY:
    conn.commit()
    print(f"\nCOMMITTED. posts_created={inserted_posts} replies_inserted={inserted_replies} skipped={skipped}")
else:
    conn.rollback()
    print(f"\nDRY-RUN (rolled back). would_create_posts={inserted_posts} would_insert_replies={inserted_replies} skipped={skipped}")

cur.close()
conn.close()
