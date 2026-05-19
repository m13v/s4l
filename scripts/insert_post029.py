#!/usr/bin/env python3.11
"""Insert media_posts row for post-029, lesson-19 (organic TLH)."""
import os
import json
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

load_dotenv(Path.home() / "social-autoposter" / ".env")
DB = os.environ["DATABASE_URL"]

VIDEO_PATH = str(Path.home() / "social-autoposter/mixer/remotion/out/post-029.mp4")
CAPTION_PATH = Path.home() / "social-autoposter/mixer/remotion/out/post-029.caption.txt"
AUDIO_PATH = str(Path.home() / "social-autoposter/mixer/audio/track-004_reel-A.m4a")
CAPTION = CAPTION_PATH.read_text(encoding="utf-8")

VARIANT_ID = "lesson-19"
POST_NUMBER = 29
THEME_ANGLE = "ai-killed-the-translator"
THEME_LABEL = "AI replacing jobs / skills"

source_clips = [
    {"order": 1, "src": "mixer/tlh-14-1.mp4", "src_dur_sec": 2.0, "target_dur_sec": 2.0, "speedup": 1.0, "start_sec": 0.0, "end_sec": 2.0},
    {"order": 2, "src": "mixer/tlh-4-1.mp4",  "src_dur_sec": 2.0, "target_dur_sec": 2.0, "speedup": 1.0, "start_sec": 2.0, "end_sec": 4.0},
    {"order": 3, "src": "mixer/tlh-6-2.mp4",  "src_dur_sec": 2.0, "target_dur_sec": 2.0, "speedup": 1.0, "start_sec": 4.0, "end_sec": 6.0},
    {"order": 4, "src": "mixer/tlh-7-3.mp4",  "src_dur_sec": 2.0, "target_dur_sec": 2.0, "speedup": 1.0, "start_sec": 6.0, "end_sec": 8.0},
]

overlays = [
    {"order": 1, "text": "i was a legal translator 11 years.", "start_sec": 0.0, "end_sec": 2.0, "dur_sec": 2.0},
    {"order": 2, "text": "claude did 240 pages in 4 hours.",   "start_sec": 2.0, "end_sec": 4.0, "dur_sec": 2.0},
    {"order": 3, "text": "i lost 4 of 5 clients in a month.",  "start_sec": 4.0, "end_sec": 6.0, "dur_sec": 2.0},
    {"order": 4, "text": "fluency is free. judgment isn't.",   "start_sec": 6.0, "end_sec": 8.0, "dur_sec": 2.0},
]

metadata = {
    "composition_id": "TLH-lesson-19",
    "format": "TLH",
    "theme": "AI",
    "theme_angle": THEME_ANGLE,
    "theme_label": THEME_LABEL,
    "clip_count": 4,
    "overlay_count": 4,
    "caption_style": "8-beat-arc-v1",
    "description_style": "here-is-a-story",
    "source_repo": "social-autoposter/mixer",
}

with psycopg.connect(DB, autocommit=False) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT id, post_number, status FROM media_posts WHERE variant_id = %s", (VARIANT_ID,))
        existing = cur.fetchone()
        if existing:
            print(f"variant_id={VARIANT_ID} already exists: id={existing[0]} post_number={existing[1]} status={existing[2]}", file=sys.stderr)
            sys.exit(1)

        cur.execute("SELECT COALESCE(MAX(post_number), 0) FROM media_posts")
        max_post = cur.fetchone()[0]
        if POST_NUMBER <= max_post:
            print(f"WARN: POST_NUMBER={POST_NUMBER} <= MAX(post_number)={max_post}", file=sys.stderr)

        cur.execute("""
            INSERT INTO media_posts (
                post_number, project_name, variant_id, video_path, audio_source,
                caption_text, caption_version, duration_sec, width, height, status,
                post_type, target_account,
                posted_urls, source_clips, overlays, engagement, metadata
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s,
                %s, %s,
                %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb
            )
            RETURNING id, post_number
        """, (
            POST_NUMBER, None, VARIANT_ID, VIDEO_PATH, f"local:{AUDIO_PATH}",
            CAPTION, "v1", 8.0, 1080, 1920, "draft",
            "organic", "matt_diak",
            json.dumps({}),
            json.dumps(source_clips),
            json.dumps(overlays),
            json.dumps({}),
            json.dumps(metadata),
        ))
        row = cur.fetchone()
        conn.commit()
        print(f"INSERTED media_posts id={row[0]} post_number={row[1]} variant_id={VARIANT_ID} status=draft")
