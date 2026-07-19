import os, re, subprocess, hashlib, itertools, json
import psycopg2
from collections import defaultdict

REM = os.path.expanduser("~/social-autoposter/mixer/remotion")
PUB = os.path.join(REM, "public", "mixer")
FFPROBE = "ffprobe"
os.environ["PATH"] = "/opt/homebrew/Cellar/ffmpeg/8.1.1/bin:" + os.environ.get("PATH","")

def dburl():
    with open(os.path.expanduser("~/social-autoposter/.env")) as f:
        for line in f:
            if line.startswith("DATABASE_URL="):
                return line.split("=",1)[1].strip()
    raise SystemExit("no DATABASE_URL")

def probe_dur(p):
    try:
        out = subprocess.check_output([FFPROBE,"-v","error","-show_entries","format=duration","-of","csv=p=0",p]).decode().strip()
        return float(out)
    except Exception:
        return None

def md5(p):
    h=hashlib.md5()
    with open(p,"rb") as f:
        for chunk in iter(lambda:f.read(1<<20), b""):
            h.update(chunk)
    return h.hexdigest()

# 1. all tlh-*.mp4 slot files, their duration + content md5
slots={}
for fn in os.listdir(PUB):
    if fn.startswith("tlh-") and fn.endswith(".mp4"):
        p=os.path.join(PUB,fn)
        d=probe_dur(p)
        slots[fn]={"dur":d,"md5":md5(p)}

# content class = md5. Map filename -> class
fn2class={fn:info["md5"] for fn,info in slots.items()}
# which classes are ~2.0s (target 2.0 slots). Consider a file 2.0s if 1.95<=dur<=2.05
two_sec_classes=set()
class_dur=defaultdict(list)
for fn,info in slots.items():
    if info["dur"] is not None:
        class_dur[info["md5"]].append(info["dur"])
for cls,durs in class_dur.items():
    avg=sum(durs)/len(durs)
    if 1.95<=avg<=2.05:
        two_sec_classes.add(cls)

# 2. DB: all lesson-% rows, source_clips + post_number
conn=psycopg2.connect(dburl())
cur=conn.cursor()
cur.execute("""SELECT post_number, variant_id, source_clips, metadata->>'theme_angle'
               FROM media_posts WHERE variant_id LIKE 'lesson-%' AND source_clips IS NOT NULL
               ORDER BY post_number""")
rows=cur.fetchall()

# map each row -> set of content classes (from src basenames), record post ordering
variant_classes=[]  # (post_number, variant_id, frozenset(classes))
class_last_post=defaultdict(lambda:-1)
for pn,vid,sc,angle in rows:
    if isinstance(sc,str):
        sc=json.loads(sc)
    classes=set()
    for c in sc:
        src=c.get("src","")
        base=os.path.basename(src)
        cls=fn2class.get(base)
        if cls:
            classes.add(cls)
    variant_classes.append((pn,vid,frozenset(classes)))
    for cls in classes:
        if pn>class_last_post[cls]:
            class_last_post[cls]=pn

# recent organic renders = last 5 lesson rows by post_number
recent5=set()
for pn,vid,cls in sorted(variant_classes,key=lambda x:-x[0])[:5]:
    recent5|=set(cls)

# candidate 2.0s classes, ranked by staleness (lowest last_post first)
cands=[(class_last_post[c],c) for c in two_sec_classes]
cands.sort()  # stalest first
print("== 2.0s content classes, stalest first (last_post, class, example_files) ==")
class_files=defaultdict(list)
for fn,cls in fn2class.items():
    class_files[cls].append(fn)
for lp,c in cands[:20]:
    print(f"  last_post={lp:>4}  {c[:12]}  files={sorted(class_files[c])[:4]}")

# no-3+-co-occurrence: chosen 4-set shares <=2 classes with ANY prior variant
def ok_set(fourset):
    fs=set(fourset)
    for pn,vid,cls in variant_classes:
        if len(fs & set(cls))>=3:
            return False
    return True

# not in recent5
pool=[c for lp,c in cands if c not in recent5]
print(f"\n== pool (stale, not in last-5) size={len(pool)} ==")

# greedily search: take combinations of the stalest, prefer overall stalest sum
chosen=None
# limit search to stalest ~14 for tractability
search=pool[:14]
best=None
for combo in itertools.combinations(search,4):
    if ok_set(combo):
        score=sum(class_last_post[c] for c in combo)
        if best is None or score<best[0]:
            best=(score,combo)
if best:
    chosen=best[1]
    print("\n== CHOSEN 4-set (min staleness score, no 3+ co-occurrence) ==")
    for c in chosen:
        print(f"  last_post={class_last_post[c]:>4}  {c[:12]}  pick_file={sorted(class_files[c])[0]}")
    # verify max pairwise co-occurrence
    maxco=0
    for pn,vid,cls in variant_classes:
        ov=len(set(chosen)&set(cls))
        maxco=max(maxco,ov)
    print(f"  max overlap with any prior variant = {maxco}")
    print("  RESULT_FILES="+",".join(sorted(class_files[c])[0] for c in chosen))
else:
    print("NO VALID SET FOUND")

# also report existing max lesson number
cur.execute("SELECT variant_id FROM media_posts WHERE variant_id ~ '^lesson-[0-9]+$'")
nums=[int(r[0].split('-')[1]) for r in cur.fetchall()]
print("\nDB max lesson num =", max(nums) if nums else None)
cur.close(); conn.close()
