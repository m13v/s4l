#!/opt/homebrew/bin/python3.11
"""Insert media_posts row for post-638 (TLH-lesson-641, matt_diak organic). Idempotent: skips if post_number or variant_id already exists."""
import json, os, pathlib, re, sys
import psycopg2

ROOT = pathlib.Path.home() / "social-autoposter"
env = (ROOT / ".env").read_text()
m = re.search(r"^DATABASE_URL=(.+)$", env, re.M)
URL = m.group(1).strip().strip('"') if m else os.popen("security find-generic-password -s s4l-database-url -w").read().strip()

POST = 638
VARIANT = "lesson-641"
OUT = ROOT / "mixer/remotion/out"
caption = (OUT / f"post-{POST}.caption.txt").read_text()
assert len(caption) <= 2150, len(caption)
video_path = str(OUT / f"post-{POST}.mp4")
assert os.path.exists(video_path)
audio = str(ROOT / "mixer/audio/track-019_iphone-3520C8EA.m4a")

clips = [("mixer/tlh-4-3.mp4", 2.0, 1.6), ("mixer/tlh-1.mp4", 1.5, 1.5), ("mixer/tlh-2-2.mp4", 1.333, 1.3),
         ("mixer/tlh-25-1.mp4", 1.933, 1.6), ("mixer/tlh-6-4.mp4", 2.0, 1.7)]
source_clips, t = [], 0.0
for i, (src, sd, td) in enumerate(clips, 1):
    source_clips.append({"order": i, "src": src, "src_dur_sec": sd, "target_dur_sec": td,
                         "speedup": round(sd / td, 3), "start_sec": round(t, 3), "end_sec": round(t + td, 3)})
    t = round(t + td, 3)
assert t == 7.7, t

ov_texts = ["22 years counting salmon through a window. i said a camera can't read a fish that turns.",
            "we ran both all season. the three weeks i was high, the run came in under forecast.",
            "i'd never missed the forecast by more than 5%. in 22 years. the river had.",
            "the machine won because it had no idea what the number was supposed to be."]
ov_durs = [2.0, 2.0, 2.0, 1.7]
overlays, t = [], 0.0
for i, (txt, d) in enumerate(zip(ov_texts, ov_durs), 1):
    overlays.append({"order": i, "text": txt, "start_sec": round(t, 3), "end_sec": round(t + d, 3), "dur_sec": d})
    t = round(t + d, 3)

metadata = {
    "composition_id": f"TLH-{VARIANT}", "format": "tlh", "theme": "ai",
    "theme_angle": "ai-killed-the-fish-counter",
    "theme_label": "AI killed the fish counter",
    "clip_count": len(source_clips), "overlay_count": len(overlays),
    "caption_style": "here-is-a-story-8beat", "description_style": "first_person_confession",
    "source_repo": "social-autoposter/mixer",
    "audio_note": "lru[0] track-019_iphone-3520C8EA.m4a is 7.707s; comp sized to 7.7s (231 frames) to honor first-LRU, dub -t 8",
    "engagement_style": "ig_anchored_to_the_forecast_arc",
    "new_style": {
        "description": "the narrator's expert output turns out to have been silently anchored to a prior expectation (a forecast, last year's number, what the room expected); the machine, having no expectation, diverges exactly in the stretches where reality diverged from the forecast, and that divergence is how the anchor is found. the loss is independence, not skill.",
        "example": "i'd never missed the forecast by more than 5%. in 22 years. the river had.",
        "note": "use when the craft produces a number or verdict that gets compared against a published expectation (forecasts, quotas, prior-year baselines, consensus estimates) and the work can't be replayed; skip when there is no prior expectation to anchor to or when the output is routinely re-checked.",
        "why_existing_didnt_fit": "no reference styles were supplied; recent invented arcs locate the loss in an echo backstop, upstream blame, expired rules, time-of-day fatigue, missing auditors or rigged tests. none make the structural move that the expert was accurate AND biased at once, with the bias only visible as a correlation between the expert's error and a known prior expectation.",
        "target_chars": 70,
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
values (%s, NULL, %s, %s, %s, %s, 'v1', %s, 1080, 1920, 'draft', %s, '{}'::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, '{}'::jsonb, 'organic', 'matt_diak')
returning id, post_number, variant_id, target_account, status
""", (POST, VARIANT, video_path, f"local:{audio}", caption, 7.7, ["instagram"],
      json.dumps(metadata), json.dumps(overlays), json.dumps(source_clips)))
print("INSERTED", cur.fetchone())
conn.commit()
cur.execute("select length(caption_text), caption_text = %s, post_type, target_account, project_name, metadata->>'engagement_style', metadata->>'theme_angle', status from media_posts where post_number=%s", (caption, POST))
print("row check", cur.fetchone())
