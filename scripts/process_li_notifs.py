#!/usr/bin/env python3
import json, os, re, sys

BASE = os.path.dirname(__file__)
ROOT = os.path.dirname(BASE)
APPLY = "--apply" in sys.argv

EXCLUDED_SLUGS = {"louis030195", "louis3195"}
EXCLUDED_AUTHORS = {"louis030195", "louis3195"}
OWN_NAMES = {"matthew diakonov", "m13v"}

def load_lines(p):
    if not os.path.exists(p):
        return set()
    with open(p) as f:
        return set(l.strip() for l in f if l.strip())

notifs = json.load(open(os.path.join(BASE, "li_notifs.json")))
dedup_comments = load_lines(os.path.join(BASE, "li_dedup_comments.txt"))
engaged_pairs = load_lines(os.path.join(BASE, "li_engaged_pairs.txt"))

# posts: "id|our_url"
posts = []
with open(os.path.join(BASE, "li_posts.txt")) as f:
    for line in f:
        line = line.rstrip("\n")
        if "|" not in line:
            continue
        pid, url = line.split("|", 1)
        posts.append((pid.strip(), url.strip()))

cfg = json.load(open(os.path.join(ROOT, "config.json")))
projects = []
for p in cfg.get("projects", []):
    name = p.get("name")
    terms = []
    for t in (p.get("search_topics") or []):
        if isinstance(t, str):
            terms.append(t.lower())
    # also use a couple words from features/differentiator
    projects.append((name, terms))

def match_project(text):
    t = (text or "").lower()
    best = None
    for name, terms in projects:
        for term in terms:
            term = term.strip()
            if len(term) < 4:
                continue
            if term in t:
                return name
    return "general"

def find_existing_post(activity_id, parent_id):
    for pid, url in posts:
        if activity_id and activity_id in url:
            return pid, url
    for pid, url in posts:
        if parent_id and parent_id in url:
            return pid, url
    return None, None

summary = {"scanned": len(notifs), "new": 0, "already": 0, "engaged": 0,
           "excluded": 0, "own": 0, "no_urn": 0}
plan = []
batch_keys = set()        # author_post_key seen this batch
batch_comment_ids = set() # their_comment_id seen this batch

for r in notifs:
    comment_urn = r.get("comment_urn")
    reply_urn = r.get("reply_urn")
    activity_id = r.get("activity_id")
    author = (r.get("author") or "").strip()
    slug = (r.get("author_slug") or "").lower()

    # their_comment_id: prefer reply_urn (the other person's actual comment), unique-safe
    their_comment_id = reply_urn or comment_urn

    if not comment_urn or not activity_id or not their_comment_id:
        summary["no_urn"] += 1
        plan.append(("no_comment_urn", author, None, their_comment_id))
        continue

    # dedup vs DB (check both reply_urn and comment_urn)
    if their_comment_id in dedup_comments or (comment_urn in dedup_comments) or (reply_urn and reply_urn in dedup_comments):
        summary["already"] += 1
        plan.append(("already_tracked", author, None, their_comment_id))
        continue
    if their_comment_id in batch_comment_ids:
        summary["already"] += 1
        plan.append(("already_tracked_batch", author, None, their_comment_id))
        continue

    # exclusions
    if slug in EXCLUDED_SLUGS or author.lower() in EXCLUDED_AUTHORS:
        summary["excluded"] += 1
        plan.append(("excluded_author", author, None, their_comment_id))
        continue
    if author.lower() in OWN_NAMES:
        summary["own"] += 1
        plan.append(("own_account", author, None, their_comment_id))
        continue

    # resolve our_url / post
    pid, our_url = find_existing_post(activity_id, r.get("parent_id"))
    if not our_url:
        our_url = f"https://www.linkedin.com/feed/update/urn:li:activity:{activity_id}/"

    author_post_key = author + "|||" + our_url
    if author_post_key in engaged_pairs or author_post_key in batch_keys:
        summary["engaged"] += 1
        plan.append(("author_already_engaged", author, our_url, their_comment_id))
        continue

    project = match_project(r.get("snippet"))
    plan.append(("NEW", author, our_url, their_comment_id))
    batch_keys.add(author_post_key)
    batch_comment_ids.add(their_comment_id)
    summary["new"] += 1
    r["_resolved"] = {
        "post_id": pid, "our_url": our_url, "their_comment_id": their_comment_id,
        "project": project, "existing_post": bool(pid),
    }

# write actionable NEW list for the insert stage
new_items = [r for r in notifs if r.get("_resolved")]
json.dump(new_items, open(os.path.join(BASE, "li_notifs_new.json"), "w"), indent=2)

print("=== PLAN ===")
for decision, author, url, cid in plan:
    print(f"  {decision:24s} {author[:30]:30s} {cid}")
print()
print("=== NEW (to insert) ===")
for r in new_items:
    res = r["_resolved"]
    print(f"  {r['author'][:28]:28s} proj={res['project']:12s} existing_post={res['existing_post']} post_id={res['post_id']}")
    print(f"      our_url={res['our_url']}")
print()
print("SUMMARY:", summary)
