#!/usr/bin/env python3.11
"""Insert the media_posts row for post-230 (organic TLH, lesson-190).

DATABASE_URL was removed from ~/social-autoposter/.env; the canonical value is
preserved in the macOS keychain under service 's4l-database-url'. There is no
HTTP create endpoint for media_posts, so the create goes direct via psycopg2,
same as the prior insert_post_NNN.py scripts.

ROOT CAUSE of the 2026-07-03/07-04 stalled fires: the Cloud SQL instance
`autoposter-pg` (project social-autoposter-prod) was STOPPED, not IP-blocked
(authorizedNetworks is 0.0.0.0/0). This fire started it back to RUNNABLE via
`gcloud sql instances patch autoposter-pg --activation-policy=ALWAYS` (account
i@m13v.com), which unblocked the entire autoposter DB, then inserted this row.

This fire re-rendered post-230 as lesson-190 (angle
ai-killed-the-executive-assistant), overwriting out/post-230.mp4 +
out/post-230.caption.txt and superseding the prior fire's orphaned
lesson-190/191/192 data.ts entries (none of which have DB rows). Engagement
style is ig_defeat_flip_arc (mode=use), so new_style is null. Target account is
matthewheartful per this fire's request envelope.
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
VARIANT_ID = "lesson-190"
VIDEO_PATH = os.path.join(ROOT, "mixer/remotion/out/post-230.mp4")
CAPTION_PATH = os.path.join(ROOT, "mixer/remotion/out/post-230.caption.txt")
AUDIO = os.path.expanduser(
    "~/social-autoposter/mixer/audio/track-010_iphone-0973CE8D.m4a")

with open(CAPTION_PATH) as f:
    caption = f.read()

# 4 pre-encoded 2.0s b-roll slots, played as-is (speedup 1.0), 8.0s comp.
# Fresh recombination (62/76/79/82); no exact-set clash vs prior lesson clip-sets.
clip_srcs = ["mixer/tlh-62-1.mp4", "mixer/tlh-76-1.mp4",
             "mixer/tlh-79-1.mp4", "mixer/tlh-82-1.mp4"]
CLIP = 2.0
source_clips = []
for i, src in enumerate(clip_srcs):
    source_clips.append({
        "order": i, "src": src,
        "src_dur_sec": CLIP, "target_dur_sec": CLIP, "speedup": 1.0,
        "start_sec": round(i * CLIP, 3), "end_sec": round((i + 1) * CLIP, 3),
    })

overlay_texts = [
    "i was an assistant for 15 years.",
    "an agent cleared the inbox by 9.",
    "cold coffee. kitchen. midnight.",
    "scheduling was never the job.",
]
OV = 2.0
overlays = []
for i, t in enumerate(overlay_texts):
    overlays.append({"order": i, "text": t, "start_sec": round(i * OV, 3),
                     "end_sec": round((i + 1) * OV, 3), "dur_sec": OV})

metadata = {
    "composition_id": "TLH-lesson-190",
    "format": "TLH",
    "theme": "ai",
    "theme_angle": "ai-killed-the-executive-assistant",
    "theme_label": "ai killed the executive assistant",
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
    target_account="matthewheartful",
    source_clips=json.dumps(source_clips),
    overlays=json.dumps(overlays),
    metadata=json.dumps(metadata),
)

conn = psycopg2.connect(DATABASE_URL, connect_timeout=20)
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
      f"status=draft post_type=organic target_account=matthewheartful project_name=NULL")
cur.close()
conn.close()
