#!/usr/bin/env python3.11
"""Insert media_posts row for post-059 (TLH lesson-38, organic, matt_diak).

ai-killed-the-paralegal. Cat 2 (AI replacing jobs / skills). 4 clips x 2.0s +
4 overlays x 2.0s = 8.0s comp. Audio: track-023 (LRU staleest local track,
71.3s). Clips remixed fresh: {tlh-32-1, tlh-4-1, tlh-6-2, tlh-7-3} are each
2.0s pre-encoded and never appear together as a 4-set in any prior lesson.
"""
import json
import psycopg2

REPO = "/Users/matthewdi/social-autoposter"
url = [l.strip().split("=", 1)[1] for l in open(f"{REPO}/.env") if l.startswith("DATABASE_URL=")][0]

caption = open(f"{REPO}/mixer/remotion/out/post-059.caption.txt", encoding="utf-8").read()

source_clips = [
    {"order": 0, "src": "mixer/tlh-32-1.mp4", "src_dur_sec": 2.0, "target_dur_sec": 2.0,
     "speedup": 1.0, "start_sec": 0.0, "end_sec": 2.0},
    {"order": 1, "src": "mixer/tlh-4-1.mp4", "src_dur_sec": 2.0, "target_dur_sec": 2.0,
     "speedup": 1.0, "start_sec": 2.0, "end_sec": 4.0},
    {"order": 2, "src": "mixer/tlh-6-2.mp4", "src_dur_sec": 2.0, "target_dur_sec": 2.0,
     "speedup": 1.0, "start_sec": 4.0, "end_sec": 6.0},
    {"order": 3, "src": "mixer/tlh-7-3.mp4", "src_dur_sec": 2.0, "target_dur_sec": 2.0,
     "speedup": 1.0, "start_sec": 6.0, "end_sec": 8.0},
]

overlays = [
    {"order": 0, "text": "i billed $190/hr finding exhibits.",  "dur_sec": 2.0,
     "start_sec": 0.0, "end_sec": 2.0},
    {"order": 1, "text": "a model did it in 40 minutes.",       "dur_sec": 2.0,
     "start_sec": 2.0, "end_sec": 4.0},
    {"order": 2, "text": "the firm paid 14 dollars in tokens.", "dur_sec": 2.0,
     "start_sec": 4.0, "end_sec": 6.0},
    {"order": 3, "text": "the search was the typing.",          "dur_sec": 2.0,
     "start_sec": 6.0, "end_sec": 8.0},
]

metadata = {
    "theme": "ai",
    "format": "tlh",
    "clip_count": 4,
    "source_repo": "social-autoposter",
    "theme_angle": "ai-killed-the-paralegal",
    "theme_label": "paralegal at chicago commercial litigation firm, model trained on case archive pulled exhibit and flagged depo contradictions in 40 minutes for fourteen dollars, the search was the typing",
    "caption_style": "8-beat-story",
    "overlay_count": 4,
    "composition_id": "TLH-lesson-38",
    "description_style": "long-form",
    "engagement_style": "ig_defeat_flip_arc",
}

conn = psycopg2.connect(url)
cur = conn.cursor()
cur.execute("SELECT id FROM media_posts WHERE variant_id=%s", ("lesson-38",))
existing = cur.fetchone()
if existing:
    raise SystemExit(f"row for lesson-38 already exists (id={existing[0]}); aborting")
cur.execute("SELECT id FROM media_posts WHERE post_number=%s", (59,))
existing = cur.fetchone()
if existing:
    raise SystemExit(f"row for post_number=59 already exists (id={existing[0]}); aborting")

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
        59, None, "lesson-38",
        f"{REPO}/mixer/remotion/out/post-059.mp4",
        "local:/Users/matthewdi/social-autoposter/mixer/audio/track-023_DYUenfSRQe7.m4a",
        caption, "v1", 8, 1080, 1920, "draft",
        ["instagram"], "organic", "matt_diak",
        json.dumps(source_clips), json.dumps(overlays), json.dumps(metadata),
    ),
)
row = cur.fetchone()
conn.commit()
print(f"inserted media_posts id={row[0]} post_number={row[1]}")
cur.close()
conn.close()
