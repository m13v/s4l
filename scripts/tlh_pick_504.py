import os, re, glob, json, subprocess, hashlib
import psycopg
from collections import defaultdict

FFPROBE="/opt/homebrew/Cellar/ffmpeg/8.1.1/bin/ffprobe"
FFMPEG="/opt/homebrew/Cellar/ffmpeg/8.1.1/bin/ffmpeg"
PUB=os.path.expanduser("~/social-autoposter/mixer/remotion/public/mixer")

def dburl():
    for line in open(os.path.expanduser("~/social-autoposter/.env")):
        if line.startswith("DATABASE_URL="):
            return line.split("=",1)[1].strip()
url=dburl()

with psycopg.connect(url) as conn, conn.cursor() as cur:
    cur.execute("SELECT MAX(post_number) FROM media_posts")
    print("MAX_post_number:", cur.fetchone()[0])
    cur.execute("SELECT COUNT(*) FROM media_posts WHERE post_number=504")
    print("post_504_rows:", cur.fetchone()[0])
    cur.execute("SELECT COUNT(*) FROM media_posts WHERE variant_id='lesson-506'")
    print("lesson506_rows:", cur.fetchone()[0])
    # used theme angles ever
    cur.execute("SELECT DISTINCT metadata->>'theme_angle' FROM media_posts WHERE metadata->>'theme_angle' IS NOT NULL")
    angles=sorted(r[0] for r in cur.fetchall())
    print("USED_ANGLES_COUNT:", len(angles))
    # recent posts source_clips + variant + post_number
    cur.execute("""SELECT post_number, variant_id, source_clips FROM media_posts
                   WHERE source_clips IS NOT NULL ORDER BY post_number DESC LIMIT 12""")
    recent=cur.fetchall()
    # per-clip-src -> last post_number across ALL rows
    cur.execute("SELECT post_number, source_clips FROM media_posts WHERE source_clips IS NOT NULL")
    allrows=cur.fetchall()

print("\n=== RECENT 12 (post_number, variant, srcs) ===")
recent_srcs_by_post={}
for pn,vid,sc in recent:
    srcs=[os.path.basename(c.get('src','')) for c in sc] if sc else []
    recent_srcs_by_post[pn]=srcs
    print(pn, vid, srcs)

# save angles
json.dump(angles, open("/tmp/tlh504_angles.json","w"))

# Build clip -> last post_number map, keyed by basename
lastpost_by_base=defaultdict(int)
for pn,sc in allrows:
    for c in (sc or []):
        b=os.path.basename(c.get('src',''))
        if b: lastpost_by_base[b]=max(lastpost_by_base[b], pn)

# Now probe every tlh-*.mp4: video md5 class, dims, duration
def vmd5(path):
    out=subprocess.run([FFMPEG,"-i",path,"-map","0:v","-an","-f","md5","-"],
                       capture_output=True,text=True)
    m=re.search(r'MD5=([0-9a-f]+)', out.stdout+out.stderr)
    return m.group(1)[:10] if m else None
def dims_dur(path):
    out=subprocess.run([FFPROBE,"-v","error","-select_streams","v:0",
        "-show_entries","stream=width,height","-show_entries","format=duration",
        "-of","json",path],capture_output=True,text=True)
    j=json.loads(out.stdout)
    w=j["streams"][0]["width"]; h=j["streams"][0]["height"]
    d=float(j["format"]["duration"])
    return w,h,d

clips=sorted(glob.glob(os.path.join(PUB,"tlh-*.mp4")))
info={}  # base -> dict
cls_members=defaultdict(list)
for p in clips:
    b=os.path.basename(p)
    w,h,d=dims_dur(p)
    cl=vmd5(p)
    info[b]={"w":w,"h":h,"dur":round(d,3),"cls":cl}
    cls_members[cl].append(b)

# class last-used = max lastpost among its member basenames
cls_last={}
for cl,members in cls_members.items():
    cls_last[cl]=max((lastpost_by_base[m] for m in members), default=0)

# candidates: 1080x1920 classes with on-disk dur >= 1.5, sorted by staleness (class_last asc)
cand=[]
for cl,members in cls_members.items():
    reps=[m for m in members if info[m]["w"]==1080 and info[m]["h"]==1920 and info[m]["dur"]>=1.5]
    if not reps: continue
    # pick representative basename (shortest dur>=1.5 tiebreak by name)
    rep=sorted(reps, key=lambda m:(info[m]["dur"], m))[0]
    cand.append((cls_last[cl], cl, rep, info[rep]["dur"], members))
cand.sort(key=lambda x:(x[0], x[2]))

recent5=set()
for pn in sorted(recent_srcs_by_post, reverse=True)[:3]:
    recent5.update(recent_srcs_by_post[pn])
    # also add classes of those srcs
print("\n=== recent 3 posts clip basenames (avoid classes) ===")
print(sorted(recent5))
recent_classes=set()
for b in recent5:
    if b in info: recent_classes.add(info[b]["cls"])
print("recent_classes:", recent_classes)

print("\n=== STALEST 1080x1920 classes (dur>=1.5), class_last asc ===")
picked=[]
for class_last, cl, rep, dur, members in cand:
    if cl in recent_classes: 
        tag="(SKIP recent)"
    else:
        tag=""
        if len(picked)<8:
            picked.append((rep,dur,cl,class_last))
    print(f"class_last={class_last} cls={cl} rep={rep} dur={dur} members={members} {tag}")

print("\n=== PICK (first 5 non-recent stalest) ===")
for rep,dur,cl,cl_last in picked[:5]:
    print(rep, "dur=",dur, "cls=",cl, "class_last=",cl_last)
