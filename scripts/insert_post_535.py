import os, json, psycopg2
from pathlib import Path

env = {}
for line in Path.home().joinpath("social-autoposter/.env").read_text().splitlines():
    if line.startswith("DATABASE_URL="):
        env["DATABASE_URL"] = line.split("=",1)[1].strip().strip('"').strip("'")
DB = env["DATABASE_URL"]

cap = Path.home().joinpath("social-autoposter/mixer/remotion/out/post-535.caption.txt").read_text()
video_path = str(Path.home().joinpath("social-autoposter/mixer/remotion/out/post-535.mp4"))
audio = "local:" + str(Path.home().joinpath("social-autoposter/mixer/audio/track-020_demo-bgm.m4a"))

source_clips = [
    {"src":"mixer/tlh-21-3.mp4","order":0,"start_sec":0.0,"end_sec":1.6,"src_dur_sec":2.0,   "target_dur_sec":1.6,"speedup":1.25},
    {"src":"mixer/tlh-3-4.mp4", "order":1,"start_sec":1.6,"end_sec":3.2,"src_dur_sec":1.6,   "target_dur_sec":1.6,"speedup":1.0},
    {"src":"mixer/tlh-6-3.mp4", "order":2,"start_sec":3.2,"end_sec":4.8,"src_dur_sec":2.0,   "target_dur_sec":1.6,"speedup":1.25},
    {"src":"mixer/tlh-3-1.mp4", "order":3,"start_sec":4.8,"end_sec":6.4,"src_dur_sec":1.6,   "target_dur_sec":1.6,"speedup":1.0},
    {"src":"mixer/tlh-5-2.mp4", "order":4,"start_sec":6.4,"end_sec":8.0,"src_dur_sec":2.6667,"target_dur_sec":1.6,"speedup":1.6667},
]
overlays = [
    {"text":"i drew faces for police for 22 years.","order":0,"start_sec":0.0,"end_sec":2.0,"dur_sec":2.0},
    {"text":"a model drew the face in seconds.",     "order":1,"start_sec":2.0,"end_sec":4.0,"dur_sec":2.0},
    {"text":"cold coffee. kitchen. midnight.",       "order":2,"start_sec":4.0,"end_sec":6.0,"dur_sec":2.0},
    {"text":"the drawing was never the job.",        "order":3,"start_sec":6.0,"end_sec":8.0,"dur_sec":2.0},
]
metadata = {
    "theme":"ai","format":"tlh","clip_count":5,"source_repo":"social-autoposter",
    "theme_angle":"ai-killed-the-forensic-sketch-artist",
    "theme_label":"AI killed the forensic sketch artist",
    "caption_style":"ig_defeat_flip_arc","overlay_count":4,
    "composition_id":"TLH-lesson-537","engagement_style":"ig_defeat_flip_arc",
    "description_style":"ig_defeat_flip_arc","applied_campaign_ids":[],
}

conn = psycopg2.connect(DB); conn.autocommit = True
cur = conn.cursor()
# UPSERT keyed on variant_id per SKILL Section 5
cur.execute("SELECT post_number FROM media_posts WHERE variant_id=%s", ("lesson-537",))
row = cur.fetchone()
if row:
    print("variant lesson-537 already exists at post", row[0], "-> aborting to avoid clobber")
    raise SystemExit(2)
cur.execute("SELECT count(*) FROM media_posts WHERE post_number=535")
if cur.fetchone()[0]:
    print("post_number 535 already exists -> aborting"); raise SystemExit(2)

cur.execute("""
INSERT INTO media_posts
 (post_number, project_name, variant_id, video_path, audio_source, caption_text, caption_version,
  duration_sec, width, height, status, post_type, target_account, source_clips, overlays, metadata)
VALUES
 (%s, NULL, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb)
RETURNING id, post_number, variant_id, status, post_type, target_account, project_name
""", (
 535, "lesson-537", video_path, audio, cap, "v1",
 8.0, 1080, 1920, "draft", "organic", "matthewheartful",
 json.dumps(source_clips), json.dumps(overlays), json.dumps(metadata),
))
print("INSERTED:", cur.fetchone())
# verify caption matches file exactly
cur.execute("SELECT caption_text FROM media_posts WHERE post_number=535")
db_cap = cur.fetchone()[0]
print("caption matches file:", db_cap == cap, "| db_len:", len(db_cap))
cur.close(); conn.close()
