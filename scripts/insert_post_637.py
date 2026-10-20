#!/opt/homebrew/bin/python3.11
"""Insert media_posts row for post-637 (TLH-lesson-640, matt_diak organic). Idempotent: skips if post_number or variant_id already exists."""
import json, os, pathlib, re, sys
import psycopg2

ROOT = pathlib.Path.home() / "social-autoposter"
env = (ROOT / ".env").read_text()
m = re.search(r"^DATABASE_URL=(.+)$", env, re.M)
URL = m.group(1).strip().strip('"') if m else os.popen("security find-generic-password -s s4l-database-url -w").read().strip()

POST = 637
VARIANT = "lesson-640"
OUT = ROOT / "mixer/remotion/out"
caption = (OUT / f"post-{POST}.caption.txt").read_text()
assert len(caption) <= 2150, len(caption)
video_path = str(OUT / f"post-{POST}.mp4")
assert os.path.exists(video_path)
audio = str(ROOT / "mixer/audio/track-014_iphone-DBAA6455.m4a")

clips = [("mixer/tlh-3.mp4", 1.5, 1.5), ("mixer/tlh-2-5.mp4", 1.333, 1.3), ("mixer/tlh-68-1.mp4", 2.0, 1.7),
         ("mixer/tlh-21-3.mp4", 2.0, 1.6), ("mixer/tlh-63-1.mp4", 2.0, 1.7)]
source_clips, t = [], 0.0
for i, (src, sd, td) in enumerate(clips, 1):
    source_clips.append({"order": i, "src": src, "src_dur_sec": sd, "target_dur_sec": td,
                         "speedup": round(sd / td, 3), "start_sec": round(t, 3), "end_sec": round(t + td, 3)})
    t = round(t + td, 3)
assert t == 7.8, t

ov_texts = ["19 years matching fingerprints. every call i made had a second signature.",
            "the lab ran 9 years of our verified matches cold. it excluded two of mine.",
            "my verifier had been handed my circles. my backstop was an echo.",
            "a second opinion that saw your first one is a copy. the machine never read my notes."]
ov_durs = [2.0, 2.0, 2.0, 1.8]
overlays, t = [], 0.0
for i, (txt, d) in enumerate(zip(ov_texts, ov_durs), 1):
    overlays.append({"order": i, "text": txt, "start_sec": round(t, 3), "end_sec": round(t + d, 3), "dur_sec": d})
    t = round(t + d, 3)

metadata = {
    "composition_id": f"TLH-{VARIANT}", "format": "tlh", "theme": "ai",
    "theme_angle": "ai-killed-the-fingerprint-examiner",
    "theme_label": "AI killed the fingerprint examiner",
    "clip_count": len(source_clips), "overlay_count": len(overlays),
    "caption_style": "here-is-a-story-8beat", "description_style": "first_person_confession",
    "source_repo": "social-autoposter/mixer",
    "audio_note": "lru[0] track-014_iphone-DBAA6455.m4a is 55.2s; comp 7.8s, dub -t 8",
    "engagement_style": "ig_backstop_was_an_echo_arc",
    "new_style": {
        "description": "the narrator's safety net (a second examiner's verification on every call) turns out to have been contaminated by the narrator's own marked-up call, so the machine wins not by being smarter but by being the first genuinely blind second opinion; the loss is a backstop that was an echo, not skill.",
        "example": "my verifier had been handed my circles. my backstop was an echo.",
        "note": "use when the craft has a formal peer-verification or sign-off step that everyone treats as independent (forensics, audits, code review, second-reader screening); skip when the narrator works alone with no backstop to expose.",
        "why_existing_didnt_fit": "no reference styles were supplied; recent invented arcs locate the loss in upstream blame, expired expertise, time-of-day vigilance, missing auditors, sparse samples or rigged tests. none make the structural move of revealing that the narrator's existing second check was never independent, so the machine's edge is blindness rather than skill.",
        "target_chars": 60,
    },
}

conn = psycopg2.connect(URL)
conn.autocommit = False
cur = conn.cursor()
cur.execute("select post_number, variant_id from media_posts where post_number=%s or variant_id=%s", (POST, VARIANT))
rows = cur.fetchall()
if rows:
    print("EXISTS", rows); sys.exit(0)
cur.execute("""
insert into media_posts (post_number, project_name, variant_id, video_path, audio_source, caption_text, caption_version,
  duration_sec, width, height, status, platforms, posted_urls, metadata, overlays, source_clips, engagement, post_type, target_account)
values (%s, NULL, %s, %s, %s, %s, 'v1', %s, 1080, 1920, 'draft', %s, '{}'::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, '{}'::jsonb, 'organic', 'matt_diak')
returning id, post_number, variant_id, target_account, status
""", (POST, VARIANT, video_path, f"local:{audio}", caption, 7.8, ["instagram"],
      json.dumps(metadata), json.dumps(overlays), json.dumps(source_clips)))
print("INSERTED", cur.fetchone())
conn.commit()
cur.execute("select length(caption_text), caption_text = %s, post_type, target_account, project_name, metadata->>'engagement_style', metadata->>'theme_angle' from media_posts where post_number=%s", (caption, POST))
print("row check", cur.fetchone())
