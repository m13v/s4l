#!/usr/bin/env python3.11
"""Insert the media_posts row for post-228 (organic TLH, lesson-188).

DATABASE_URL was removed from ~/social-autoposter/.env; the canonical value is
preserved in the macOS keychain under service 's4l-database-url'. There is no
HTTP create endpoint for media_posts (base POST 404, by-number POST 405, and
by-number PATCH exposes only update actions: mark_posted / sync_caption /
caption_too_long), so the create still goes direct via psycopg2, same as the
prior insert_post_NNN.py scripts.
"""
import os, json, subprocess, psycopg2

ROOT = os.path.expanduser("~/social-autoposter")

DATABASE_URL = subprocess.run(
    ["security", "find-generic-password", "-s", "s4l-database-url", "-w"],
    capture_output=True, text=True,
).stdout.strip()
if not DATABASE_URL:
    raise SystemExit("could not read s4l-database-url from keychain")

POST_NUMBER = 228
VARIANT_ID = "lesson-188"
VIDEO_PATH = os.path.join(ROOT, "mixer/remotion/out/post-228.mp4")
CAPTION_PATH = os.path.join(ROOT, "mixer/remotion/out/post-228.caption.txt")
AUDIO = os.path.expanduser(
    "~/social-autoposter/mixer/audio/track-021_Nhat-give-me-the-font.m4a")

with open(CAPTION_PATH) as f:
    caption = f.read()

# 4 pre-encoded 2.0s b-roll slots, played as-is (speedup 1.0), 8.0s comp.
clip_srcs = ["mixer/tlh-13-1.mp4", "mixer/tlh-14-1.mp4",
             "mixer/tlh-5-1.mp4", "mixer/tlh-85-1.mp4"]
CLIP = 2.0
source_clips = []
for i, src in enumerate(clip_srcs):
    source_clips.append({
        "order": i, "src": src,
        "src_dur_sec": CLIP, "target_dur_sec": CLIP, "speedup": 1.0,
        "start_sec": round(i * CLIP, 3), "end_sec": round((i + 1) * CLIP, 3),
    })

overlay_texts = [
    "i underwrote risk for 15 years.",
    "an agent priced the book by lunch.",
    "cold coffee. kitchen. 1am.",
    "the pricing was never the job.",
]
OV = 2.0
overlays = []
for i, t in enumerate(overlay_texts):
    overlays.append({"order": i, "text": t, "start_sec": round(i * OV, 3),
                     "end_sec": round((i + 1) * OV, 3), "dur_sec": OV})

metadata = {
    "composition_id": "TLH-lesson-188",
    "format": "TLH",
    "theme": "ai",
    "theme_angle": "ai-killed-the-insurance-underwriter",
    "theme_label": "ai killed the insurance underwriter",
    "clip_count": 4,
    "overlay_count": 4,
    "caption_style": "ig_defeat_flip_arc",
    "description_style": "ig_defeat_flip_arc",
    "source_repo": "social-autoposter/mixer",
    "engagement_style": "ig_defeat_flip_arc",
}

cols = dict(
    post_number=POST_NUMBER,
    project_name=None,               # organic: intentionally product-free
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
    target_account="matt_diak",
    source_clips=json.dumps(source_clips),
    overlays=json.dumps(overlays),
    metadata=json.dumps(metadata),
)

conn = psycopg2.connect(DATABASE_URL)
conn.autocommit = True
cur = conn.cursor()

cur.execute("SELECT post_number FROM media_posts WHERE variant_id=%s", (VARIANT_ID,))
existing = cur.fetchone()
if existing:
    raise SystemExit(
        f"variant {VARIANT_ID} already exists at post_number={existing[0]}; aborting")
cur.execute("SELECT 1 FROM media_posts WHERE post_number=%s", (POST_NUMBER,))
if cur.fetchone():
    raise SystemExit(f"post_number {POST_NUMBER} already exists; aborting")

keys = list(cols.keys())
placeholders = ", ".join(["%s"] * len(keys))
collist = ", ".join(keys)
cur.execute(
    f"INSERT INTO media_posts ({collist}) VALUES ({placeholders}) RETURNING id, post_number",
    [cols[k] for k in keys],
)
row = cur.fetchone()
print(f"INSERTED media_posts id={row[0]} post_number={row[1]} variant={VARIANT_ID} "
      f"status=draft post_type=organic target_account=matt_diak project_name=NULL")
cur.close()
conn.close()
