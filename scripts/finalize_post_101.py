#!/usr/bin/env /opt/homebrew/bin/python3.11
"""Idempotent finalizer for post-101 (organic TLH lesson-68, matthewheartful).
Self-guarding: verifies the rendered mp4 + caption, then upserts the media_posts row.
Refuses to insert if any artifact check fails. Writes a verification report to
/tmp/final_status.txt.
"""
import os, re, json, subprocess, sys

REPO = os.path.expanduser("~/social-autoposter")
REMO = os.path.join(REPO, "mixer/remotion")
OUT_MP4 = os.path.join(REMO, "out/post-101.mp4")
CAP = os.path.join(REMO, "out/post-101.caption.txt")
AUDIO = os.path.join(REPO, "mixer/audio/track-001_DWBsamriWgC.m4a")
UNPROVEN = os.path.join(REPO, "mixer/unproven new content/EFD123D5-2640-4DED-888E-947926EFA1D0.MP4")
FF = "/opt/homebrew/Cellar/ffmpeg/8.1.1/bin"
NODE = os.path.expanduser("~/.nvm/versions/node/v23.10.0/bin")
ENVPATH = f"{NODE}:{FF}:" + os.environ.get("PATH", "")

report = []
def log(m):
    report.append(str(m)); print(m, flush=True)

def fail(code):
    with open("/tmp/final_status.txt","w") as f:
        f.write("\n".join(report) + f"\nEXIT={code}\n")
    sys.exit(code)

def probe(path):
    """return (width,height,duration) or (None,None,None)"""
    try:
        w = subprocess.check_output([f"{FF}/ffprobe","-v","error","-select_streams","v:0",
            "-show_entries","stream=width,height","-of","csv=p=0:s=x",path], text=True).strip()
        d = subprocess.check_output([f"{FF}/ffprobe","-v","error","-show_entries",
            "format=duration","-of","csv=p=0",path], text=True).strip()
        wh = w.split("x")
        return int(wh[0]), int(wh[1]), float(d)
    except Exception as e:
        return None, None, None

def has_audio(path):
    try:
        s = subprocess.check_output([f"{FF}/ffprobe","-v","error","-select_streams","a:0",
            "-show_entries","stream=codec_type","-of","csv=p=0",path], text=True).strip()
        return s == "audio"
    except Exception:
        return False

# --- 1. Ensure render exists & is correct; re-render if needed ---
w,h,d = probe(OUT_MP4)
need = (w,h) != (1080,1920) or not has_audio(OUT_MP4) or d is None
if need:
    log(f"post-101.mp4 missing/invalid (w={w} h={h} d={d}); rendering...")
    env = dict(os.environ); env["PATH"] = ENVPATH
    tmp = "/tmp/TLH-lesson-68.mp4"
    r = subprocess.run(["npx","remotion","render","src/index.ts","TLH-lesson-68",tmp,"--concurrency=2"],
                       cwd=REMO, env=env, capture_output=True, text=True)
    log("render rc=%d tail=%s" % (r.returncode, (r.stdout+r.stderr).strip().splitlines()[-3:] if (r.stdout+r.stderr).strip() else ""))
    if r.returncode != 0 or not os.path.exists(tmp):
        log("RENDER FAILED"); fail(2)
    dub = subprocess.run([f"{FF}/ffmpeg","-y","-loglevel","error","-i",tmp,"-i",AUDIO,
        "-map","0:v","-map","1:a","-c:v","copy","-c:a","aac","-t","8","-movflags","+faststart",OUT_MP4],
        capture_output=True, text=True)
    log("dub rc=%d %s" % (dub.returncode, dub.stderr.strip()))
    try: os.remove(tmp)
    except OSError: pass
    w,h,d = probe(OUT_MP4)

log(f"FINAL mp4: w={w} h={h} dur={d} audio={has_audio(OUT_MP4)} size={os.path.getsize(OUT_MP4) if os.path.exists(OUT_MP4) else 'NONE'}")

# --- 2. Verify caption ---
with open(CAP, "rb") as f:
    cap_bytes = f.read()
caption_text = cap_bytes.decode("utf-8")
cap_chars = len(cap_bytes)  # wc -c == bytes
log(f"caption bytes={cap_chars} (limit 2150)")

# --- 3. Hard gates before any DB write ---
gate_ok = (w,h)==(1080,1920) and has_audio(OUT_MP4) and 7.5 <= (d or 0) <= 8.6 \
          and cap_chars <= 2150 and caption_text.startswith("here is a story.") \
          and "—" not in caption_text and "–" not in caption_text
log(f"GATES_OK={gate_ok}")
if not gate_ok:
    log("GATE FAILED - refusing DB insert"); fail(3)

# --- 4. Build jsonb payloads ---
def pdur(fn):
    _,_,dd = probe(os.path.join(REMO,"public",fn))
    return round(dd,6) if dd else None

slots = [
    ("mixer/tlh-68-1.mp4", 2.0, UNPROVEN),
    ("mixer/tlh-7-2.mp4",  2.0, None),
    ("mixer/tlh-3-3.mp4",  2.0, None),
    ("mixer/tlh-5-1.mp4",  2.0, None),
]
source_clips = []
for i,(src,tgt,raw) in enumerate(slots):
    sd = pdur(src)
    entry = {"order": i+1, "src": src, "src_dur_sec": sd, "target_dur_sec": tgt,
             "speedup": round((sd/tgt),4) if sd else None,
             "start_sec": round(i*2.0,3), "end_sec": round((i+1)*2.0,3)}
    if raw: entry["raw_src"] = raw
    source_clips.append(entry)

overlay_texts = [
    "i coded medical charts for 16 years.",
    "ai coded a day of charts in minutes.",
    "i lost three clinics that quarter.",
    "i sell the audit now, not the codes.",
]
overlays = [{"order": i+1, "text": t, "start_sec": round(i*2.0,3),
             "end_sec": round((i+1)*2.0,3), "dur_sec": 2.0}
            for i,t in enumerate(overlay_texts)]

metadata = {
    "composition_id": "TLH-lesson-68",
    "format": "TLH",
    "theme": "ai",
    "theme_angle": "ai-killed-the-medical-coder",
    "theme_label": "ai killed the medical coder",
    "clip_count": 4,
    "overlay_count": 4,
    "caption_style": "defeat_flip_arc",
    "description_style": "first_person_founder_confession",
    "source_repo": "social-autoposter",
    "engagement_style": "ig_defeat_flip_arc",
    "unproven_clip_basename": "EFD123D5-2640-4DED-888E-947926EFA1D0.MP4",
}

# --- 5. DB upsert (keyed on variant_id; new -> insert) ---
env = open(os.path.join(REPO, ".env")).read()
url = re.search(r'DATABASE_URL=["\']?([^"\'\n]+)', env).group(1)
import psycopg2
from psycopg2.extras import Json
conn = psycopg2.connect(url); conn.autocommit = False
cur = conn.cursor()
# clean any partial prior attempt for this variant / post_number
cur.execute("DELETE FROM media_posts WHERE variant_id=%s OR post_number=%s", ("lesson-68", 101))
deleted = cur.rowcount
cur.execute("""
INSERT INTO media_posts
 (post_number, project_name, variant_id, video_path, audio_source, caption_text,
  caption_version, duration_sec, width, height, status, platforms, post_type,
  target_account, overlays, source_clips, metadata, created_at, updated_at)
VALUES
 (%s, NULL, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now())
RETURNING id, post_number, variant_id, post_type, target_account, project_name, status,
          jsonb_array_length(overlays), jsonb_array_length(source_clips),
          metadata->>'theme_angle', metadata->>'engagement_style', char_length(caption_text)
""", (
    101, "lesson-68", OUT_MP4, f"local:{AUDIO}", caption_text, "v1",
    8, 1080, 1920, "draft", ["instagram"], "organic", "matthewheartful",
    Json(overlays), Json(source_clips), Json(metadata),
))
row = cur.fetchone()
conn.commit()
log(f"DB deleted_prior={deleted}")
log("DB_ROW: " + json.dumps(row, default=str))

# verify caption_text byte-identical to file
cur.execute("SELECT caption_text FROM media_posts WHERE post_number=101")
db_cap = cur.fetchone()[0]
log("CAPTION_MATCHES_FILE=" + str(db_cap == caption_text))
cur.close(); conn.close()

log("ALL_DONE")

# write report
with open("/tmp/final_status.txt","w") as f:
    f.write("\n".join(report) + "\n")
