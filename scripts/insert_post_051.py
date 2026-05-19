#!/usr/bin/env python3.11
"""Insert media_posts row for post-051 (TLH lesson-32, organic, matthewheartful).

Unproven clip 05704E4C-0777-46DB-B4B8-84BF1E24FA70.MP4 (7.9s raw) encoded into
slot tlh-32-1.mp4 at 2.0s via pure speedup (ratio 0.2532). Slots 2-4 are
fresh pre-encoded pieces never used together as a 3-set in lessons 23-31.
"""
import json
import psycopg2

REPO = "/Users/matthewdi/social-autoposter"
url = [l.strip().split("=", 1)[1] for l in open(f"{REPO}/.env") if l.startswith("DATABASE_URL=")][0]

caption = open(f"{REPO}/mixer/remotion/out/post-051.caption.txt", encoding="utf-8").read()

source_clips = [
    {"order": 0, "src": "mixer/tlh-32-1.mp4",
     "raw_src": f"{REPO}/mixer/unproven new content/05704E4C-0777-46DB-B4B8-84BF1E24FA70.MP4",
     "src_dur_sec": 7.9, "target_dur_sec": 2.0, "speedup": 3.95,
     "start_sec": 0.0, "end_sec": 2.0},
    {"order": 1, "src": "mixer/tlh-4-4.mp4", "src_dur_sec": 2.0, "target_dur_sec": 2.0,
     "speedup": 1.0, "start_sec": 2.0, "end_sec": 4.0},
    {"order": 2, "src": "mixer/tlh-5-2.mp4", "src_dur_sec": 2.667, "target_dur_sec": 2.0,
     "speedup": 1.333, "start_sec": 4.0, "end_sec": 6.0},
    {"order": 3, "src": "mixer/tlh-7-1.mp4", "src_dur_sec": 2.0, "target_dur_sec": 2.0,
     "speedup": 1.0, "start_sec": 6.0, "end_sec": 8.0},
]

overlays = [
    {"order": 0, "text": "i was senior staff. 18 years in.", "dur_sec": 2.0,
     "start_sec": 0.0, "end_sec": 2.0},
    {"order": 1, "text": "i said on stage AI kills juniors.", "dur_sec": 2.0,
     "start_sec": 2.0, "end_sec": 4.0},
    {"order": 2, "text": "a 26yo shipped my roadmap in 3 days.", "dur_sec": 2.0,
     "start_sec": 4.0, "end_sec": 6.0},
    {"order": 3, "text": "AI kills the senior, not the junior.", "dur_sec": 2.0,
     "start_sec": 6.0, "end_sec": 8.0},
]

metadata = {
    "theme": "ai",
    "format": "tlh",
    "clip_count": 4,
    "source_repo": "social-autoposter",
    "theme_angle": "ai-killed-the-senior-engineer",
    "theme_label": "senior staff engineer, 26yo contractor shipped the roadmap in a weekend with claude code, the senior was the bottleneck",
    "caption_style": "8-beat-story",
    "overlay_count": 4,
    "composition_id": "TLH-lesson-32",
    "description_style": "long-form",
    "unproven_clip_basename": "05704E4C-0777-46DB-B4B8-84BF1E24FA70.MP4",
}

conn = psycopg2.connect(url)
cur = conn.cursor()
cur.execute("SELECT id FROM media_posts WHERE variant_id=%s", ("lesson-32",))
existing = cur.fetchone()
if existing:
    raise SystemExit(f"row for lesson-32 already exists (id={existing[0]}); aborting")

cur.execute(
    """
    INSERT INTO media_posts
      (post_number, project_name, variant_id, video_path, audio_source,
       caption_text, caption_version, duration_sec, width, height, status,
       platforms, post_type, target_account, source_clips, overlays, metadata,
       created_at, updated_at)
    VALUES
      (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
    RETURNING id, post_number
    """,
    (
        51, None, "lesson-32",
        f"{REPO}/mixer/remotion/out/post-051.mp4",
        "local:/Users/matthewdi/social-autoposter/mixer/audio/track-012_iphone-49B43323.m4a",
        caption, "v1", 8, 1080, 1920, "draft",
        ["instagram"], "organic", "matthewheartful",
        json.dumps(source_clips), json.dumps(overlays), json.dumps(metadata),
    ),
)
row = cur.fetchone()
conn.commit()
print(f"inserted media_posts id={row[0]} post_number={row[1]}")
cur.close()
conn.close()
