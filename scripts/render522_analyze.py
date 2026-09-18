#!/usr/bin/env python3.11
import os, re, json, subprocess, hashlib, glob
import psycopg2
from collections import defaultdict

FF = "/opt/homebrew/Cellar/ffmpeg/8.1.1/bin"
os.environ["PATH"] = FF + ":" + os.environ["PATH"]

# --- DATABASE_URL from .env ---
env = open(os.path.expanduser("~/social-autoposter/.env")).read()
DB = re.search(r'DATABASE_URL=(\S+)', env).group(1)
conn = psycopg2.connect(DB)
cur = conn.cursor()

# 1. DB state
cur.execute("SELECT MAX(post_number) FROM media_posts;")
print("MAX(post_number) =", cur.fetchone()[0])

cur.execute("SELECT post_number, variant_id, status, target_account FROM media_posts WHERE post_number IN (520,521,522) ORDER BY post_number;")
print("rows 520-522:", cur.fetchall())

cur.execute("SELECT variant_id, post_number, status FROM media_posts WHERE variant_id IN ('lesson-522','lesson-523','lesson-524') ORDER BY variant_id;")
print("lesson-522/523/524 rows:", cur.fetchall())

# theme angle uniqueness check for candidate angles
for ang in ['ai-killed-the-travel-agent','ai-killed-the-mortgage-underwriter','ai-killed-the-claims-adjuster','ai-killed-the-title-examiner']:
    cur.execute("SELECT count(*) FROM media_posts WHERE metadata->>'theme_angle'=%s;", (ang,))
    print(f"angle {ang}: DB count={cur.fetchone()[0]}")

# 2. Build basename -> most-recent post_number map from source_clips
cur.execute("SELECT post_number, source_clips FROM media_posts WHERE source_clips IS NOT NULL;")
basename_last = defaultdict(int)
render_sets = {}  # post_number -> set(basenames)
for pn, sc in cur.fetchall():
    if isinstance(sc, str):
        try: sc = json.loads(sc)
        except: continue
    if not sc: continue
    bset = set()
    for c in sc:
        src = c.get('src','')
        m = re.search(r'(tlh-[\w-]+?)\.mp4', src)
        if m:
            b = m.group(1)
            basename_last[b] = max(basename_last[b], pn or 0)
            bset.add(b)
    if bset:
        render_sets[pn] = bset

# 3. Probe all pre-encoded tlh clips
clips = sorted(glob.glob(os.path.expanduser("~/social-autoposter/mixer/remotion/public/mixer/tlh-*.mp4")))
info = {}
for path in clips:
    b = re.search(r'(tlh-[\w-]+?)\.mp4', path).group(1)
    dur = subprocess.check_output(["ffprobe","-v","error","-show_entries","format=duration","-of","csv=p=0",path]).decode().strip()
    wh = subprocess.check_output(["ffprobe","-v","error","-select_streams","v:0","-show_entries","stream=width,height","-of","csv=p=0:s=x",path]).decode().strip()
    md5 = subprocess.check_output(["ffmpeg","-v","error","-i",path,"-map","0:v","-an","-f","md5","-"]).decode().strip().replace("MD5=","")
    info[b] = {"dur":float(dur),"wh":wh,"md5":md5,"path":path}

# 4. Group by md5 class -> class_last
class_members = defaultdict(list)
for b,d in info.items():
    class_members[d["md5"]].append(b)
class_last = {}
for md5,members in class_members.items():
    class_last[md5] = max(basename_last.get(m,0) for m in members)

# 5. Candidates: 1080x1920, dur > 1.5
recent_pns = sorted(render_sets.keys())[-8:]
recent_basenames = set()
for pn in recent_pns:
    recent_basenames |= render_sets[pn]

cands = []
for md5,members in class_members.items():
    # pick staler on-disk copy (basename with smallest basename_last) as representative
    elig = [m for m in members if info[m]["dur"] > 1.5 and info[m]["wh"]=="1080x1920"]
    if not elig: continue
    rep = min(elig, key=lambda m: basename_last.get(m,0))
    cands.append({"md5":md5[:12],"rep":rep,"dur":info[rep]["dur"],"class_last":class_last[md5],"members":members})

cands.sort(key=lambda c: c["class_last"])
print("\n=== recent 8 render post_numbers:", recent_pns)
print("=== recent basenames:", sorted(recent_basenames))
print("\n=== stalest distinct classes (dur>1.5, 1080x1920), top 12 ===")
for c in cands[:12]:
    print(f"  class {c['md5']} rep={c['rep']:10s} dur={c['dur']:.3f} class_last={c['class_last']:4d} members={c['members']}")

conn.close()
