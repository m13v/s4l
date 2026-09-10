#!/usr/bin/env python3.11
"""One-shot helper for post-493 organic TLH render: DB checks + stalest-clip selection."""
import os, re, json, subprocess, hashlib, glob
import psycopg2

ROOT = os.path.expanduser("~/social-autoposter")
PUBLIC = os.path.join(ROOT, "mixer/remotion/public/mixer")
FFPROBE = "/opt/homebrew/Cellar/ffmpeg/8.1.1/bin/ffprobe"
FFMPEG = "/opt/homebrew/Cellar/ffmpeg/8.1.1/bin/ffmpeg"

def dburl():
    for line in open(os.path.join(ROOT, ".env")):
        if line.startswith("DATABASE_URL"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")

conn = psycopg2.connect(dburl())
cur = conn.cursor()

# 1. Max post_number + confirm 493 free + lesson-495 free
cur.execute("SELECT MAX(post_number) FROM media_posts;")
maxpn = cur.fetchone()[0]
cur.execute("SELECT COUNT(*) FROM media_posts WHERE post_number=493;")
pn493 = cur.fetchone()[0]
cur.execute("SELECT COUNT(*) FROM media_posts WHERE variant_id='lesson-495';")
v495 = cur.fetchone()[0]
print(f"MAX(post_number)={maxpn}  post_number=493 rows={pn493}  variant_id=lesson-495 rows={v495}")

# theme angle collision check
cur.execute("SELECT COUNT(*) FROM media_posts WHERE metadata->>'theme_angle'='ai-killed-the-perfumer';")
print(f"theme_angle ai-killed-the-perfumer rows={cur.fetchone()[0]}")

# 2. video md5 class + on-disk duration for every tlh clip
clips = sorted(glob.glob(os.path.join(PUBLIC, "tlh-*.mp4")))
info = {}
for p in clips:
    b = "mixer/" + os.path.basename(p)
    dur = float(subprocess.check_output([FFPROBE, "-v", "error", "-show_entries",
        "format=duration", "-of", "csv=p=0", p]).strip())
    md5 = subprocess.run([FFMPEG, "-i", p, "-map", "0:v", "-an", "-f", "md5", "-"],
        capture_output=True, text=True).stdout.strip().split("=")[-1][:10]
    info[b] = {"path": p, "dur": round(dur, 3), "md5": md5}

# group by md5 class
classes = {}
for b, d in info.items():
    classes.setdefault(d["md5"], []).append(b)

# 3. most-recent post_number per clip basename across media_posts.source_clips
cur.execute("SELECT post_number, source_clips FROM media_posts WHERE source_clips IS NOT NULL ORDER BY post_number;")
last_used = {}  # basename -> max post_number
recent_sets = {}  # post_number -> set(basenames)
for pn, sc in cur.fetchall():
    if isinstance(sc, str):
        sc = json.loads(sc)
    s = set()
    for item in sc:
        src = item.get("src", "")
        if "tlh-" in src:
            base = "mixer/" + os.path.basename(src)
            s.add(base)
            last_used[base] = max(last_used.get(base, 0), pn)
    if s:
        recent_sets[pn] = s

# class-level last_used = max over its members
class_last = {}
for md5, members in classes.items():
    class_last[md5] = max((last_used.get(m, 0) for m in members), default=0)

# recent render clip sets (last 3)
recent_pns = sorted(recent_sets.keys())[-3:]
recent_union = set()
for pn in recent_pns:
    recent_union |= recent_sets[pn]
print(f"\nLast 3 render post_numbers: {recent_pns}")
for pn in recent_pns:
    print(f"  post-{pn}: {sorted(recent_sets[pn])}")

# 4. eligible classes: on-disk dur>=1.5 for a representative member, not in recent_union
def class_ok(md5):
    for m in classes[md5]:
        if info[m]["dur"] >= 1.5 and m not in recent_union:
            return m
    return None

ranked = []
for md5 in classes:
    rep = class_ok(md5)
    if rep:
        ranked.append((class_last[md5], md5, rep))
ranked.sort(key=lambda x: (x[0], x[1]))  # stalest first

print("\nStalest eligible distinct md5 classes (dur>=1.5, not in last 3 renders):")
for last, md5, rep in ranked[:12]:
    print(f"  class {md5} last_used post-{last:>3}  rep={rep} dur={info[rep]['dur']}")

pick = ranked[:5]
print("\nPICK (5 stalest):")
for last, md5, rep in pick:
    print(f"  {rep}  md5={md5}  dur={info[rep]['dur']}  last_used=post-{last}")

cur.close(); conn.close()
