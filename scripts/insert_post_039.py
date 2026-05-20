#!/usr/bin/env python3.11
"""Insert media_posts row for post-039 (TLH lesson-25, organic, matthewheartful)."""
import json
import psycopg2

REPO = "/Users/matthewdi/social-autoposter"
url = [l.strip().split("=", 1)[1] for l in open(f"{REPO}/.env") if l.startswith("DATABASE_URL=")][0]

caption = open(f"{REPO}/mixer/remotion/out/post-039.caption.txt", encoding="utf-8").read()

source_clips = [
    {"order": 0, "src": "mixer/tlh-25-1.mp4",
     "raw_src": f"{REPO}/mixer/unproven new content/624C69D4-7850-4FD3-BFAA-47C075DB0041.MP4",
     "src_dur_sec": 1.933, "target_dur_sec": 1.95, "speedup": 0.991,
     "start_sec": 0.0, "end_sec": 1.95},
    {"order": 1, "src": "mixer/tlh-7-2.mp4", "src_dur_sec": 2.0, "target_dur_sec": 1.95,
     "speedup": 1.026, "start_sec": 1.95, "end_sec": 3.9},
    {"order": 2, "src": "mixer/tlh-5-1.mp4", "src_dur_sec": 2.667, "target_dur_sec": 1.95,
     "speedup": 1.368, "start_sec": 3.9, "end_sec": 5.85},
    {"order": 3, "src": "mixer/tlh-3-2.mp4", "src_dur_sec": 1.6, "target_dur_sec": 1.95,
     "speedup": 0.821, "start_sec": 5.85, "end_sec": 7.8},
]

overlays = [
    {"order": 0, "text": "i charged 14 cents a word for novels.", "dur_sec": 1.95,
     "start_sec": 0.0, "end_sec": 1.95},
    {"order": 1, "text": "a model did the book in an afternoon.", "dur_sec": 1.95,
     "start_sec": 1.95, "end_sec": 3.9},
    {"order": 2, "text": "the publisher paid it nine dollars.", "dur_sec": 1.95,
     "start_sec": 3.9, "end_sec": 5.85},
    {"order": 3, "text": "translating is typing. meaning isn't.", "dur_sec": 1.95,
     "start_sec": 5.85, "end_sec": 7.8},
]

metadata = {
    "theme": "ai",
    "format": "tlh",
    "clip_count": 4,
    "source_repo": "social-autoposter",
    "theme_angle": "ai-killed-the-translator",
    "theme_label": "literary translator, model translated the novel, meaning vs typing",
    "caption_style": "8-beat-story",
    "overlay_count": 4,
    "composition_id": "TLH-lesson-25",
    "description_style": "long-form",
    "unproven_clip_basename": "624C69D4-7850-4FD3-BFAA-47C075DB0041.MP4",
}

conn = psycopg2.connect(url)
cur = conn.cursor()
cur.execute("SELECT id FROM media_posts WHERE variant_id=%s", ("lesson-25",))
existing = cur.fetchone()
if existing:
    raise SystemExit(f"row for lesson-25 already exists (id={existing[0]}); aborting")

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
        39, None, "lesson-25",
        f"{REPO}/mixer/remotion/out/post-039.mp4",
        "local:/Users/matthewdi/social-autoposter/mixer/audio/track-003_DX7GQruOW36.m4a",
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
