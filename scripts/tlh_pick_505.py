import os, glob, subprocess, json, hashlib, psycopg2
os.chdir(os.path.expanduser("~/social-autoposter/mixer/remotion"))
os.environ["PATH"]="/opt/homebrew/Cellar/ffmpeg/8.1.1/bin:"+os.environ["PATH"]
FF="/opt/homebrew/Cellar/ffmpeg/8.1.1/bin/ffmpeg"; FP="/opt/homebrew/Cellar/ffmpeg/8.1.1/bin/ffprobe"
clips=sorted(glob.glob("public/mixer/tlh-*.mp4"))
info={}
for c in clips:
    base=os.path.basename(c)
    # skip the post-505 leftover slots (belong to posted lesson-505)
    dur=subprocess.run([FP,"-v","error","-show_entries","format=duration","-of","csv=p=0",c],capture_output=True,text=True).stdout.strip()
    dims=subprocess.run([FP,"-v","error","-select_streams","v:0","-show_entries","stream=width,height","-of","csv=p=0:s=x",c],capture_output=True,text=True).stdout.strip()
    md5=subprocess.run([FF,"-i",c,"-map","0:v","-an","-f","md5","-"],capture_output=True,text=True).stdout.strip().split("=")[-1]
    info[base]={"dur":float(dur) if dur else 0,"dims":dims,"md5":md5[:10]}
# DB: most recent post_number per basename
import os as _o
from urllib.parse import urlparse
# load .env DATABASE_URL
env={}
for line in open(os.path.expanduser("~/social-autoposter/.env")):
    if line.strip() and not line.startswith("#") and "=" in line:
        k,v=line.split("=",1); env[k.strip()]=v.strip().strip('"').strip("'")
conn=psycopg2.connect(env["DATABASE_URL"]); cur=conn.cursor()
cur.execute("select post_number, source_clips from media_posts where source_clips is not null")
last_by_base={}
for pn,sc in cur.fetchall():
    if not sc: continue
    for e in sc:
        s=(e.get("src") or "")
        b=os.path.basename(s)
        if b:
            last_by_base[b]=max(last_by_base.get(b,0),pn)
# class_last = max post over all basenames sharing md5 class
class_last={}
by_class={}
for base,d in info.items():
    by_class.setdefault(d["md5"],[]).append(base)
for md5,bases in by_class.items():
    cl=max((last_by_base.get(b,0) for b in bases), default=0)
    class_last[md5]=cl
# recent renders to avoid (post 500..504)
cur.execute("select post_number, source_clips from media_posts where post_number>=500 order by post_number desc")
recent={}
for pn,sc in cur.fetchall():
    recent[pn]=[os.path.basename(e.get("src","")) for e in (sc or [])]
print("RECENT RENDERS 500..504:")
for pn in sorted(recent,reverse=True): print(" ",pn,recent[pn])
# candidate classes: 1080x1920, on-disk dur>=1.5, exclude tlh-505-* leftovers
rows=[]
seen_class=set()
for md5,bases in by_class.items():
    bs=[b for b in bases if not b.startswith("tlh-505-")]
    if not bs: continue
    # representative: pick one 1080x1920 with dur>=1.5
    reps=[b for b in bs if info[b]["dims"]=="1080x1920" and info[b]["dur"]>=1.5]
    if not reps: continue
    rep=sorted(reps)[0]
    rows.append((class_last[md5], md5, rep, info[rep]["dur"], [b for b in bs]))
rows.sort(key=lambda r:(r[0], r[2]))
print("\nSTALEST 1080x1920 classes (dur>=1.5), by class_last asc:")
for r in rows[:14]:
    print(f"  class_last=post-{r[0]:>3}  md5={r[1]}  rep={r[2]:<14} dur={r[3]:.3f}  bases={r[4]}")
