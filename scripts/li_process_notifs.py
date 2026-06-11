import json, re, sys

actionable = json.load(open("/tmp/li_actionable.json"))
comment_ids = set(l.strip() for l in open("/tmp/li_comment_ids.txt") if l.strip())
engaged_pairs = [l.strip() for l in open("/tmp/li_engaged_pairs.txt") if l.strip()]
posts = []  # (id, our_url)
for l in open("/tmp/li_posts.txt"):
    l=l.strip()
    if not l or '|' not in l: continue
    pid, url = l.split('|',1)
    posts.append((pid.strip(), url.strip()))

EXCLUDED = {"louis030195","louis3195"}
OWN = {"matthew diakonov","m13v"}

def parent_id(curn):
    # urn:li:comment:(activity:PARENT,COMMENT) or (ugcPost:PARENT,COMMENT)
    m = re.search(r'\((?:activity|ugcPost|share):(\d+),', curn or '')
    return m.group(1) if m else None

# dedup actionable by comment_urn (keep first occurrence)
seen=set(); items=[]
for a in actionable:
    cu=a.get("comment_urn")
    if cu in seen: continue
    seen.add(cu); items.append(a)

# build engaged author+parentid index for thread-level dedup
engaged_idx=[]  # (author_lower, set_of_ids_in_url)
for pair in engaged_pairs:
    if '|||' not in pair: continue
    author, url = pair.split('|||',1)
    ids = set(re.findall(r'(\d{15,})', url))
    engaged_idx.append((author.strip().lower(), ids))

def find_post(pid):
    for post_id, url in posts:
        if pid and pid in url:
            return post_id, url
    return None, None

stats=dict(new=0, already=0, engaged=0, excluded=0, own=0, nourn=0)
plan=[]
for a in items:
    cu=a.get("comment_urn"); author=a.get("author","unknown"); snip=a.get("snippet","")
    pid=parent_id(cu)
    al=author.strip().lower()
    href=a.get("href","")
    if not cu or not pid:
        stats["nourn"]+=1; plan.append(("SKIP_nourn",author,cu,None)); continue
    if cu in comment_ids:
        stats["already"]+=1; plan.append(("SKIP_already",author,cu,None)); continue
    if al in OWN:
        stats["own"]+=1; plan.append(("SKIP_own",author,cu,None)); continue
    if al in EXCLUDED or any(x in al for x in EXCLUDED):
        stats["excluded"]+=1; plan.append(("SKIP_excluded",author,cu,None)); continue
    # thread-level dedup: same author already engaged on a thread containing this parent id
    engaged_hit = any(al==ea and pid in ids for ea,ids in engaged_idx)
    if engaged_hit:
        stats["engaged"]+=1; plan.append(("SKIP_engaged",author,cu,None)); continue
    post_id, post_url = find_post(pid)
    stats["new"]+=1
    plan.append(("INSERT",author,cu,dict(parent_id=pid,post_id=post_id,post_url=post_url,href=href,snippet=snip[:300])))

print("=== STATS ===")
print(json.dumps(stats,indent=2))
print("\n=== INSERT PLAN ===")
for kind,author,cu,extra in plan:
    if kind=="INSERT":
        e=extra
        print(f"INSERT | {author} | parent={e['parent_id']} | post_id={e['post_id']} | post_url={(e['post_url'] or '')[:70]}")
print("\n=== SKIPS ===")
from collections import Counter
c=Counter(k for k,_,_,_ in plan if k!="INSERT")
print(dict(c))
json.dump(plan, open("/tmp/li_plan.json","w"))
print(f"\nTotal items (deduped): {len(items)}  / raw actionable: {len(actionable)}")
