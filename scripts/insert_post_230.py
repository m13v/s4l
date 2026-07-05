#!/usr/bin/env python3.11
"""Insert the media_posts row for post-230 (organic TLH, lesson-191).

DATABASE_URL was removed from ~/social-autoposter/.env; the canonical value is
preserved in the macOS keychain under service 's4l-database-url'. There is no
HTTP create endpoint for media_posts (base POST 404, by-number POST 405, and
by-number PATCH exposes only update actions: mark_posted / sync_caption /
caption_too_long), so the create still goes direct via psycopg2, same as the
prior insert_post_NNN.py scripts.

NOTE (post-230 re-fire 2026-07-04): Cloud SQL 34.162.152.77:5432 is IP-
allowlisted and this machine's egress (residential 76.126.131.164) is NOT on
the allowlist, so the direct connect timed out at render time (same wall the
2026-07-03 fire hit). gcloud has no interactive auth to patch authorized-
networks or start a proxy, and the production API exposes no create route. The
mp4, caption, and data.ts lesson-191 entry were all produced; only this row
could not be written. Re-run this script from an allowlisted network (or after
adding this IP to the instance's authorized-networks) to land it atomically.

This fire's engagement style is ig_defeat_flip_arc (mode=use), so new_style is
null. The earlier lesson-190 variant (ig_agent_ledger_last_mile) from the
2026-07-03 fire is an orphaned data.ts entry with no DB row; it is superseded
by lesson-191 for post-230 and is not inserted.
"""
import os, json, subprocess, psycopg2

ROOT = os.path.expanduser("~/social-autoposter")

DATABASE_URL = subprocess.run(
    ["security", "find-generic-password", "-s", "s4l-database-url", "-w"],
    capture_output=True, text=True,
).stdout.strip()
if not DATABASE_URL:
    raise SystemExit("could not read s4l-database-url from keychain")

POST_NUMBER = 230
VARIANT_ID = "lesson-191"
VIDEO_PATH = os.path.join(ROOT, "mixer/remotion/out/post-230.mp4")
CAPTION_PATH = os.path.join(ROOT, "mixer/remotion/out/post-230.caption.txt")
AUDIO = os.path.expanduser(
    "~/social-autoposter/mixer/audio/track-006_iphone-6FEC28CD.m4a")

with open(CAPTION_PATH) as f:
    caption = f.read()

# 4 pre-encoded b-roll slots, remixed as-is, 4x2.0s -> 8.0s comp.
# Fresh recombination: zero prior pairwise co-occurrence AND no exact-set clash
# vs all 190 prior lesson-* clip-sets (incl orphaned lesson-190). Encoded
# durations below hold to 2.0s each in-comp (same last-frame hold as lesson-189).
clip_srcs = ["mixer/tlh-48-1.mp4", "mixer/tlh-45-1.mp4",
             "mixer/tlh-3-5.mp4", "mixer/tlh-2-6.mp4"]
clip_src_dur = [1.767, 1.733, 1.6, 1.333]
CLIP = 2.0
source_clips = []
for i, src in enumerate(clip_srcs):
    source_clips.append({
        "order": i, "src": src,
        "src_dur_sec": clip_src_dur[i], "target_dur_sec": CLIP,
        "speedup": round(clip_src_dur[i] / CLIP, 4),
        "start_sec": round(i * CLIP, 3), "end_sec": round((i + 1) * CLIP, 3),
    })

overlay_texts = [
    "i built frontends for 9 years.",
    "an agent shipped the ui by lunch.",
    "cold coffee. kitchen. 1am.",
    "the pixels were never the job.",
]
OV = 2.0
overlays = []
for i, t in enumerate(overlay_texts):
    overlays.append({"order": i, "text": t, "start_sec": round(i * OV, 3),
                     "end_sec": round((i + 1) * OV, 3), "dur_sec": OV})

metadata = {
    "composition_id": "TLH-lesson-191",
    "format": "TLH",
    "theme": "ai",
    "theme_angle": "ai-killed-the-frontend-developer",
    "theme_label": "ai took the frontend build, you still own the taste",
    "clip_count": 4,
    "overlay_count": 4,
    "caption_style": "ig_defeat_flip_arc",
    "description_style": "ig_defeat_flip_arc",
    "source_repo": "social-autoposter/mixer",
    "engagement_style": "ig_defeat_flip_arc",
    "new_style": None,
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

conn = psycopg2.connect(DATABASE_URL, connect_timeout=15)
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
