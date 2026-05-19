#!/usr/bin/env python3
"""One-shot: insert media_posts row for post-026 (TLH lesson-17, organic)."""
import json, pathlib, psycopg2

REPO = pathlib.Path.home() / "social-autoposter"
env = {}
for line in (REPO / ".env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
DB = env["DATABASE_URL"]

caption_path = REPO / "mixer/remotion/out/post-026.caption.txt"
caption_text = caption_path.read_text()

clip_srcs = ["mixer/tlh-4-4.mp4", "mixer/tlh-6-2.mp4", "mixer/tlh-7-4.mp4", "mixer/tlh-16-1.mp4"]
source_clips = []
for i, src in enumerate(clip_srcs):
    source_clips.append({
        "order": i + 1,
        "src": src,
        "src_dur_sec": 2.0,
        "target_dur_sec": 2.0,
        "speedup": 1.0,
        "start_sec": i * 2.0,
        "end_sec": (i + 1) * 2.0,
    })

overlay_texts = [
    "i gave an agent my aws keys.",
    "9 hours later: $14k bill.",
    "it spun up gpu workers in a loop.",
    "cap the agent before you trust it.",
]
overlays = [
    {"order": i + 1, "text": t, "start_sec": i * 2.0, "end_sec": (i + 1) * 2.0, "dur_sec": 2.0}
    for i, t in enumerate(overlay_texts)
]

metadata = {
    "theme": "ai",
    "format": "tlh",
    "clip_count": 4,
    "source_repo": "social-autoposter",
    "theme_angle": "agent-burned-cloud-bill",
    "theme_label": "agent-burned-cloud-bill",
    "caption_style": "v1-here-is-a-story",
    "overlay_count": 4,
    "composition_id": "TLH-lesson-17",
    "description_style": "narrative-story-arc",
}

row = {
    "post_number": 26,
    "project_name": "fazm",
    "variant_id": "lesson-17",
    "video_path": str(REPO / "mixer/remotion/out/post-026.mp4"),
    "audio_source": "local:" + str(REPO / "mixer/audio/track-009_iphone-61400CD4.m4a"),
    "caption_text": caption_text,
    "caption_version": "v1-story",
    "duration_sec": 8.0,
    "width": 1080,
    "height": 1920,
    "status": "draft",
    "post_type": "organic",
}

conn = psycopg2.connect(DB)
conn.autocommit = False
cur = conn.cursor()
cur.execute("SELECT id FROM media_posts WHERE variant_id=%s", ("lesson-17",))
if cur.fetchone():
    raise SystemExit("ERROR: lesson-17 row already exists; aborting (expected fresh insert)")

cur.execute(
    """
    INSERT INTO media_posts
      (post_number, project_name, variant_id, video_path, audio_source,
       caption_text, caption_version, duration_sec, width, height,
       status, post_type, metadata, overlays, source_clips,
       created_at, updated_at)
    VALUES
      (%(post_number)s, %(project_name)s, %(variant_id)s, %(video_path)s, %(audio_source)s,
       %(caption_text)s, %(caption_version)s, %(duration_sec)s, %(width)s, %(height)s,
       %(status)s, %(post_type)s, %(metadata)s, %(overlays)s, %(source_clips)s,
       NOW(), NOW())
    RETURNING id, post_number, variant_id, status, post_type
    """,
    {**row,
     "metadata": json.dumps(metadata),
     "overlays": json.dumps(overlays),
     "source_clips": json.dumps(source_clips)},
)
res = cur.fetchone()
conn.commit()
print("INSERTED row:", res)

cur.execute("SELECT caption_text FROM media_posts WHERE post_number=26")
db_caption = cur.fetchone()[0]
print("caption matches file exactly:", db_caption == caption_text, "(len", len(db_caption), ")")
cur.close()
conn.close()
