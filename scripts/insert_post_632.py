#!/opt/homebrew/bin/python3.11
"""Insert media_posts row for post-632 (TLH-lesson-635, matthewheartful organic). Idempotent: skips if post_number or variant_id already exists."""
import json, os, pathlib, re, sys
import psycopg2

ROOT = pathlib.Path.home() / "social-autoposter"
env = (ROOT / ".env").read_text()
m = re.search(r"^DATABASE_URL=(.+)$", env, re.M)
URL = m.group(1).strip() if m else os.popen("security find-generic-password -s s4l-database-url -w").read().strip()

POST = 632
VARIANT = "lesson-635"
OUT = ROOT / "mixer/remotion/out"
caption = (OUT / f"post-{POST}.caption.txt").read_text()
assert len(caption) <= 2150, len(caption)
video_path = str(OUT / f"post-{POST}.mp4")
assert os.path.exists(video_path)
audio = str(ROOT / "mixer/audio/track-001_DWBsamriWgC.m4a")

clips = [("mixer/tlh-6-3.mp4", 2.0, 1.6), ("mixer/tlh-3-3.mp4", 1.6, 1.5), ("mixer/tlh-2-1.mp4", 1.333, 1.3),
         ("mixer/tlh-5-2.mp4", 2.667, 1.6), ("mixer/tlh-4-4.mp4", 2.0, 1.7)]
source_clips, t = [], 0.0
for i, (src, sd, td) in enumerate(clips, 1):
    source_clips.append({"order": i, "src": src, "src_dur_sec": sd, "target_dur_sec": td,
                         "speedup": round(sd / td, 3), "start_sec": round(t, 3), "end_sec": round(t + td, 3)})
    t = round(t + td, 3)
assert t == 7.7, t

ov_texts = ["26 years tapping steel hulls. i said a robot can't hear the plate.",
            "i rang the forward tank clean and signed the survey. the crawler map said red.",
            "my hammer hit good steel eight times. my ears filled in the gaps.",
            "the machine never heard a thing. it just never skipped an inch."]
ov_durs = [2.0, 2.0, 2.0, 1.7]
overlays, t = [], 0.0
for i, (txt, d) in enumerate(zip(ov_texts, ov_durs), 1):
    overlays.append({"order": i, "text": txt, "start_sec": round(t, 3), "end_sec": round(t + d, 3), "dur_sec": d})
    t = round(t + d, 3)

metadata = {
    "composition_id": f"TLH-{VARIANT}", "format": "tlh", "theme": "ai",
    "theme_angle": "ai-killed-the-hull-surveyor",
    "theme_label": "i told a quay full of people a robot can't hear steel, then rang a forward ballast tank clean with my hammer and signed the condition survey while the crawler map showed forty percent wastage two frames aft, gauged the spot myself and found my hammer had hit good steel eight times and my ears had filled in the gaps between the taps",
    "clip_count": len(source_clips), "overlay_count": len(overlays),
    "caption_style": "here-is-a-story-8beat", "description_style": "first_person_confession",
    "source_repo": "social-autoposter/mixer",
    "engagement_style": "ig_sampled_as_census_arc",
    "new_style": {
        "description": "the narrator's expert test is a sparse sample (taps, spot checks, a handful of readings) and the trained sense interpolates the gaps with confidence; the machine wins by exhaustive coverage, not by sharper judgment at any single point.",
        "example": "my hammer hit good steel eight times. my ears filled in the rest.",
        "note": "use when the craft is physically a sampling act (tapping, probing, spot-checking, random pulls) and the failure can hide between samples; skip when the expert already inspects the whole object or when the error is in interpretation rather than coverage.",
        "why_existing_didnt_fit": "no reference styles were supplied; recent house arcs beat the narrator with blind panels, hidden repeats, answer keys, cancelling comparisons and deferred harvests. none locate the loss in coverage, where every individual judgment the narrator made was correct and the defeat came from the inches never examined.",
        "target_chars": 70,
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
values (%s, NULL, %s, %s, %s, %s, 'v1', %s, 1080, 1920, 'draft', %s, '{}'::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, '{}'::jsonb, 'organic', 'matthewheartful')
returning id, post_number, variant_id, target_account, status
""", (POST, VARIANT, video_path, f"local:{audio}", caption, 7.7, ["instagram"],
      json.dumps(metadata), json.dumps(overlays), json.dumps(source_clips)))
print("INSERTED", cur.fetchone())
conn.commit()
cur.execute("select length(caption_text), caption_text = %s from media_posts where post_number=%s", (caption, POST))
print("caption check", cur.fetchone())
