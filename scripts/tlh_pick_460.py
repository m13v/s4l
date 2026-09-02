#!/opt/homebrew/bin/python3.11
import os, subprocess, json, hashlib, glob, sys

FFPROBE="/opt/homebrew/Cellar/ffmpeg/8.1.1/bin/ffprobe"
FFMPEG="/opt/homebrew/Cellar/ffmpeg/8.1.1/bin/ffmpeg"
POOL=os.path.expanduser("~/social-autoposter/mixer/remotion/public/mixer")

def get_db_url():
    # keychain first (media_posts needs it per project memory), fall back to .env
    try:
        u=subprocess.check_output(["security","find-generic-password","-s","s4l-database-url","-w"]).decode().strip()
        if u: return u
    except Exception: pass
    for line in open(os.path.expanduser("~/social-autoposter/.env")):
        if line.startswith("DATABASE_URL="):
            return line.split("=",1)[1].strip().strip('"')
    return None

import psycopg2
url=get_db_url()
conn=psycopg2.connect(url)
cur=conn.cursor()
cur.execute("SELECT MAX(post_number) FROM media_posts")
print("MAX(post_number)=", cur.fetchone()[0])
cur.execute("SELECT COUNT(*) FROM media_posts WHERE post_number=458")
print("post_number=458 rows:", cur.fetchone()[0])
cur.execute("SELECT COUNT(*) FROM media_posts WHERE variant_id='lesson-460'")
print("variant_id lesson-460 rows:", cur.fetchone()[0])

# last 12 lesson-% renders by post_number desc, their source_clips
cur.execute("""
  SELECT post_number, variant_id, source_clips
  FROM media_posts
  WHERE variant_id LIKE 'lesson-%'
  ORDER BY post_number DESC
  LIMIT 12
""")
rows=cur.fetchall()
last10=rows[:10]
print("\n=== last 10 lesson renders ===")
used_basenames=set()
for pn,vid,sc in last10:
    names=[]
    if sc:
        for c in sc:
            src=c.get("src","")
            b=os.path.basename(src)
            names.append(b)
            used_basenames.add(b)
    print(pn, vid, names)

# hash every tlh clip's video stream
def vmd5(path):
    out=subprocess.run([FFMPEG,"-v","error","-i",path,"-map","0:v","-f","md5","-"],
                       capture_output=True,text=True).stdout.strip()
    return out.replace("MD5=","")[:12]

def dur(path):
    return float(subprocess.check_output([FFPROBE,"-v","error","-show_entries","format=duration","-of","csv=p=0",path]).decode().strip())

def blackframes(path):
    r=subprocess.run([FFMPEG,"-v","error","-i",path,"-vf","blackdetect=d=0.05:pic_th=0.98","-an","-f","null","-"],
                     capture_output=True,text=True)
    return r.stderr.count("black_start")

clips={}
for p in sorted(glob.glob(os.path.join(POOL,"tlh-*.mp4"))):
    b=os.path.basename(p)
    clips[b]={"class":vmd5(p),"dur":dur(p)}

used_classes=set()
for b in used_basenames:
    if b in clips:
        used_classes.add(clips[b]["class"])
print("\nused basenames count:", len(used_basenames), "used classes count:", len(used_classes))

# fresh classes: pick one representative filename per fresh class
fresh={}
for b,info in clips.items():
    cl=info["class"]
    if cl in used_classes: continue
    fresh.setdefault(cl,[]).append((b,info["dur"]))
print("\n=== FRESH classes (not in last-10 used set) ===")
for cl,members in sorted(fresh.items()):
    print(cl, members)
print("\nfresh class count:", len(fresh))
conn.close()
