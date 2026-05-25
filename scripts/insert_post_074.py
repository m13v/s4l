#!/usr/bin/env python3
"""One-shot: insert media_posts row for post-074 (TLH lesson-50, organic, matthewheartful)."""
import json, pathlib, psycopg2

REPO = pathlib.Path.home() / "social-autoposter"
env = {}
for line in (REPO / ".env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
DB = env["DATABASE_URL"]

caption_path = REPO / "mixer/remotion/out/post-074.caption.txt"
caption_text = caption_path.read_text()

clip_srcs = [
    ("mixer/tlh-22-1.mp4", 2.0),
    ("mixer/tlh-13-1.mp4", 2.0),
    ("mixer/tlh-7-1.mp4",  2.0),
    ("mixer/tlh-4-4.mp4",  2.0),
]
source_clips = []
target = 2.0
for i, (src, src_dur) in enumerate(clip_srcs):
    source_clips.append({
        "order": i + 1,
        "src": src,
        "src_dur_sec": src_dur,
        "target_dur_sec": target,
        "speedup": round(src_dur / target, 4),
        "start_sec": round(i * target, 4),
        "end_sec": round((i + 1) * target, 4),
    })

overlay_texts = [
    "i sourced senior engineers for 14 years.",
    "an agent shipped my pipeline in a weekend.",
    "my best client closed without my call.",
    "sourcing was the typing.",
]
overlays = [
    {
        "order": i + 1,
        "text": t,
        "start_sec": round(i * target, 4),
        "end_sec": round((i + 1) * target, 4),
        "dur_sec": target,
    }
    for i, t in enumerate(overlay_texts)
]

metadata = {
    "theme": "ai",
    "format": "tlh",
    "clip_count": 4,
    "source_repo": "social-autoposter",
    "theme_angle": "ai-killed-the-recruiter",
    "theme_label": "ai-killed-the-recruiter",
    "caption_style": "v1-here-is-a-story",
    "overlay_count": 4,
    "composition_id": "TLH-lesson-50",
    "description_style": "narrative-story-arc",
    "engagement_style": "ig_defeat_flip_arc",
}

row = {
    "post_number": 74,
    "project_name": None,
    "variant_id": "lesson-50",
    "video_path": str(REPO / "mixer/remotion/out/post-074.mp4"),
    "audio_source": "local:" + str(REPO / "mixer/audio/track-005_reel-B.m4a"),
    "caption_text": caption_text,
    "caption_version": "v1-story",
    "duration_sec": 8.0,
    "width": 1080,
    "height": 1920,
    "status": "draft",
    "post_type": "organic",
    "target_account": "matthewheartful",
}

conn = psycopg2.connect(DB)
conn.autocommit = False
cur = conn.cursor()
cur.execute("SELECT id FROM media_posts WHERE variant_id=%s", ("lesson-50",))
existing = cur.fetchone()
if existing:
    raise SystemExit(f"ERROR: lesson-50 row already exists (id={existing[0]}); aborting")
cur.execute("SELECT id FROM media_posts WHERE post_number=%s", (74,))
if cur.fetchone():
    raise SystemExit("ERROR: post_number 74 already exists; aborting")

cur.execute(
    """
    INSERT INTO media_posts
      (post_number, project_name, variant_id, video_path, audio_source,
       caption_text, caption_version, duration_sec, width, height,
       status, post_type, target_account, metadata, overlays, source_clips,
       created_at, updated_at)
    VALUES
      (%(post_number)s, %(project_name)s, %(variant_id)s, %(video_path)s, %(audio_source)s,
       %(caption_text)s, %(caption_version)s, %(duration_sec)s, %(width)s, %(height)s,
       %(status)s, %(post_type)s, %(target_account)s, %(metadata)s, %(overlays)s, %(source_clips)s,
       NOW(), NOW())
    RETURNING id, post_number, variant_id, status, post_type, target_account
    """,
    {**row,
     "metadata": json.dumps(metadata),
     "overlays": json.dumps(overlays),
     "source_clips": json.dumps(source_clips)},
)
res = cur.fetchone()
conn.commit()
print("INSERTED row:", res)

cur.execute("SELECT caption_text FROM media_posts WHERE post_number=74")
db_caption = cur.fetchone()[0]
print("caption matches file exactly:", db_caption == caption_text, "(len", len(db_caption), ")")
cur.close()
conn.close()
