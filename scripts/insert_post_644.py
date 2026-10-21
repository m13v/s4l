#!/opt/homebrew/bin/python3.11
"""Insert media_posts row for post-644 (TLH-lesson-647, matthewheartful organic). Idempotent: skips if post_number or variant_id already exists."""
import json, os, pathlib, re, sys
import psycopg2

ROOT = pathlib.Path.home() / "social-autoposter"
env = (ROOT / ".env").read_text()
m = re.search(r"^DATABASE_URL=(.+)$", env, re.M)
URL = m.group(1).strip().strip('"') if m else os.popen("security find-generic-password -s s4l-database-url -w").read().strip()

POST = 644
VARIANT = "lesson-647"
OUT = ROOT / "mixer/remotion/out"
caption = (OUT / f"post-{POST}.caption.txt").read_text()
assert len(caption) <= 2150, len(caption)
video_path = str(OUT / f"post-{POST}.mp4")
assert os.path.exists(video_path)
audio = str(ROOT / "mixer/audio/track-007_iphone-2EAC148F.m4a")

clips = [("mixer/tlh-505-6.mp4", 1.333, 1.3), ("mixer/tlh-5-1.mp4", 2.667, 1.8), ("mixer/tlh-2-4.mp4", 1.333, 1.3),
         ("mixer/tlh-3-5.mp4", 1.6, 1.6), ("mixer/tlh-48-1.mp4", 1.767, 1.7)]
source_clips, t = [], 0.0
for i, (src, sd, td) in enumerate(clips, 1):
    source_clips.append({"order": i, "src": src, "src_dur_sec": sd, "target_dur_sec": td,
                         "speedup": round(sd / td, 3), "start_sec": round(t, 3), "end_sec": round(t + td, 3)})
    t = round(t + td, 3)
assert t == 7.7, t

ov_texts = ["14 years on a lifeguard stand. i said a camera can't tell a drowning from a kid playing.",
            "it couldn't. 31 false alarms the first week. i logged every one. i was right.",
            "then it buzzed for a lane i'd scanned 4 seconds earlier. a kid, vertical, two feet under, silent.",
            "i was right about its mistakes. i priced them wrong. stop counting its errors. weigh them."]
ov_durs = [2.0, 2.0, 2.0, 1.7]
overlays, t = [], 0.0
for i, (txt, d) in enumerate(zip(ov_texts, ov_durs), 1):
    overlays.append({"order": i, "text": txt, "start_sec": round(t, 3), "end_sec": round(t + d, 3), "dur_sec": d})
    t = round(t + d, 3)

metadata = {
    "composition_id": f"TLH-{VARIANT}", "format": "tlh", "theme": "ai",
    "theme_angle": "ai-killed-the-lifeguard",
    "theme_label": "AI killed the lifeguard",
    "clip_count": len(source_clips), "overlay_count": len(overlays),
    "caption_style": "here-is-a-story-8beat", "description_style": "first_person_confession",
    "source_repo": "social-autoposter/mixer",
    "audio_note": "lru[0] track-007_iphone-2EAC148F.m4a is 55.2s; comp 7.7s (231 frames), dub -t 8",
    "engagement_style": "ig_counted_errors_never_weighed_arc",
    "new_style": {
        "description": "the expert correctly predicts the machine's frequent, visible error, logs it and is vindicated on the count, then loses because the error that matters is the rare one with the asymmetric cost; the loss is pricing errors, not perceiving them.",
        "example": "i was right about its mistakes. i priced them wrong.",
        "note": "use for monitoring and safety jobs where false alarms are cheap and misses are catastrophic (lifeguarding, screening, alarms, triage); skip when both error types cost about the same or when the machine's errors are rare.",
        "why_existing_didnt_fit": "no reference styles were supplied; recent invented arcs locate the loss in an anchor, a blind count, difficulty triage, a sampled census or an outgrown catalog. none make the move that the expert's prediction about the machine was true and documented, and the vindication itself was the trap because the error being counted was the cheap one.",
        "target_chars": 55,
    },
}

conn = psycopg2.connect(URL, connect_timeout=20)
conn.autocommit = False
cur = conn.cursor()
cur.execute("select post_number, variant_id from media_posts where post_number=%s or variant_id=%s", (POST, VARIANT))
rows = cur.fetchall()
if rows:
    print("EXISTS", rows); sys.exit(0)
cur.execute("""
insert into media_posts (post_number, project_name, variant_id, video_path, audio_source, caption_text, caption_version,
  duration_sec, width, height, status, platforms, posted_urls, metadata, overlays, source_clips, engagement, post_type, target_account)
values (%s, NULL, %s, %s, %s, %s, 'v1', %s, 1080, 1920, 'draft', %s, '{}'::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, '{}'::jsonb, 'organic', 'matthewheartful')
returning id, post_number, variant_id, target_account, status
""", (POST, VARIANT, video_path, f"local:{audio}", caption, 7.7, ["instagram"],
      json.dumps(metadata), json.dumps(overlays), json.dumps(source_clips)))
print("INSERTED", cur.fetchone())
conn.commit()
cur.execute("select length(caption_text), caption_text = %s, post_type, target_account, project_name, metadata->>'engagement_style', metadata->>'theme_angle', status, jsonb_array_length(source_clips), jsonb_array_length(overlays) from media_posts where post_number=%s", (caption, POST))
print("row check", cur.fetchone())
