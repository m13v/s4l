import json, re

with open("/tmp/li_actionable.json") as f:
    items = json.load(f)

dedup_ids = set(l.strip() for l in open("scripts/li_dedup_comment_ids.txt") if l.strip())
author_post_pairs = set(l.strip() for l in open("scripts/li_author_post_pairs.txt") if l.strip())

# active posts: id|our_url ; build map activity_id -> (post_id, our_url)
active = []
for l in open("scripts/li_active_posts.txt"):
    l=l.strip()
    if not l: continue
    pid, url = l.split("|",1)
    active.append((pid.strip(), url.strip()))

EXCLUDED = {"louis030195","louis3195"}
OWN = {"matthew diakonov","m13v"}

def activity_from_urn(urn):
    m = re.search(r'activity:(\d+)', urn or "")
    return m.group(1) if m else None

def find_post(activity_id):
    if not activity_id: return None
    for pid, url in active:
        if activity_id in url:
            return (pid, url)
    return None

results = {"new":[], "already":[], "engaged":[], "excluded":[], "own":[], "no_urn":[]}
print("=== %d actionable items ===" % len(items))
for it in items:
    author = it["author"]
    al = author.lower()
    curn = it.get("comment_urn")
    aid = it.get("activity_id") or activity_from_urn(curn)
    snippet = it["snippet"][:500]
    print("\n--- %s | %s | comment_urn=%s | activity=%s" % (it["type"], author, curn, aid))
    if not curn or not aid:
        print("  SKIP no_comment_urn")
        results["no_urn"].append(it); continue
    if al in EXCLUDED or any(e in al for e in EXCLUDED):
        print("  SKIP excluded_author")
        results["excluded"].append(it); continue
    if al in OWN:
        print("  SKIP own_account")
        results["own"].append(it); continue
    if curn in dedup_ids:
        print("  SKIP already_tracked")
        results["already"].append(it); continue
    post = find_post(aid)
    our_url = post[1] if post else ("https://www.linkedin.com/feed/update/urn:li:activity:%s/" % aid)
    key = author + "|||" + our_url
    if key in author_post_pairs:
        print("  SKIP author_already_engaged (%s)" % key)
        results["engaged"].append(it); continue
    it["_post_id"] = post[0] if post else None
    it["_our_url"] = our_url
    it["_activity_id"] = aid
    it["_matched_post"] = bool(post)
    print("  NEW -> post_id=%s our_url=%s matched_post=%s" % (it["_post_id"], our_url, bool(post)))
    results["new"].append(it)

with open("/tmp/li_results.json","w") as f:
    json.dump(results, f)
print("\n=== TALLY ===")
for k,v in results.items():
    print("%s: %d" % (k, len(v)))
