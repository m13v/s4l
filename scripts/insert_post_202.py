#!/usr/bin/env python3.11
import os, json, re, psycopg2

ROOT = os.path.expanduser("~/social-autoposter")
with open(os.path.join(ROOT, ".env")) as f:
    for line in f:
        if line.startswith("DATABASE_URL="):
            os.environ["DATABASE_URL"] = line.split("=", 1)[1].strip()
            break

POST_NUMBER = 202
VARIANT_ID = "lesson-162"
VIDEO_PATH = os.path.join(ROOT, "mixer/remotion/out/post-202.mp4")
CAPTION_PATH = os.path.join(ROOT, "mixer/remotion/out/post-202.caption.txt")
AUDIO = os.path.expanduser("~/social-autoposter/mixer/audio/track-018_iphone-IMG3634.m4a")

with open(CAPTION_PATH) as f:
    caption = f.read()

clip_srcs = ["mixer/tlh-13-1.mp4", "mixer/tlh-72-1.mp4", "mixer/tlh-76-1.mp4", "mixer/tlh-82-1.mp4"]
source_clips = []
for i, src in enumerate(clip_srcs):
    source_clips.append({
        "order": i, "src": src,
        "src_dur_sec": 2.0, "target_dur_sec": 2.0, "speedup": 1.0,
        "start_sec": i * 2.0, "end_sec": (i + 1) * 2.0,
    })

overlay_texts = [
    "i kept the books for 14 years.",
    "an agent closed the month in an hour.",
    "cold coffee. kitchen. midnight.",
    "the bookkeeping was never the job.",
]
overlays = []
for i, t in enumerate(overlay_texts):
    overlays.append({"order": i, "text": t, "start_sec": i * 2.0,
                     "end_sec": (i + 1) * 2.0, "dur_sec": 2.0})

metadata = {
    "composition_id": "TLH-lesson-162",
    "format": "TLH",
    "theme": "ai",
    "theme_angle": "ai-killed-the-bookkeeper",
    "theme_label": "AI killed the bookkeeper",
    "clip_count": 4,
    "overlay_count": 4,
    "caption_style": "ig_defeat_flip_arc",
    "description_style": "ig_defeat_flip_arc",
    "source_repo": "social-autoposter/mixer",
    "engagement_style": "ig_defeat_flip_arc",
}

conn = psycopg2.connect(os.environ["DATABASE_URL"])
conn.autocommit = True
cur = conn.cursor()

cur.execute("SELECT post_number, caption_version FROM media_posts WHERE variant_id=%s", (VARIANT_ID,))
existing = cur.fetchone()

cols = dict(
    post_number=POST_NUMBER,
    project_name=None,
    variant_id=VARIANT_ID,
    video_path=VIDEO_PATH,
    audio_source="local:" + AUDIO,
    caption_text=caption,
    caption_version="v1",
    duration_sec=8,
    width=1080,
    height=1920,
    status="draft",
    post_type="organic",
    target_account="matthewheartful",
    source_clips=json.dumps(source_clips),
    overlays=json.dumps(overlays),
    metadata=json.dumps(metadata),
)

if existing:
    raise SystemExit(f"variant {VARIANT_ID} already exists at post_number={existing[0]}; aborting (unexpected for a fresh variant)")

keys = list(cols.keys())
placeholders = ", ".join(["%s"] * len(keys))
collist = ", ".join(keys)
cur.execute(
    f"INSERT INTO media_posts ({collist}) VALUES ({placeholders}) RETURNING id, post_number",
    [cols[k] for k in keys],
)
row = cur.fetchone()
print(f"INSERTED media_posts id={row[0]} post_number={row[1]} variant={VARIANT_ID} status=draft target_account=matthewheartful project_name=NULL")
cur.close()
conn.close()
