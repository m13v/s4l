#!/opt/homebrew/bin/python3.11
"""Insert media_posts row for post-634 (TLH-lesson-637, matt_diak organic). Idempotent: skips if post_number or variant_id already exists."""
import json, os, pathlib, re, sys
import psycopg2

ROOT = pathlib.Path.home() / "social-autoposter"
env = (ROOT / ".env").read_text()
m = re.search(r"^DATABASE_URL=(.+)$", env, re.M)
URL = m.group(1).strip() if m else os.popen("security find-generic-password -s s4l-database-url -w").read().strip()

POST = 634
VARIANT = "lesson-637"
OUT = ROOT / "mixer/remotion/out"
caption = (OUT / f"post-{POST}.caption.txt").read_text()
assert len(caption) <= 2150, len(caption)
video_path = str(OUT / f"post-{POST}.mp4")
assert os.path.exists(video_path)
audio = str(ROOT / "mixer/audio/track-001_DWBsamriWgC.m4a")

clips = [("mixer/tlh-22-1.mp4", 2.0, 1.6), ("mixer/tlh-52-1.mp4", 1.6, 1.5), ("mixer/tlh-505-6.mp4", 1.333, 1.3),
         ("mixer/tlh-66-1.mp4", 2.0, 1.6), ("mixer/tlh-69-1.mp4", 2.0, 1.7)]
source_clips, t = [], 0.0
for i, (src, sd, td) in enumerate(clips, 1):
    source_clips.append({"order": i, "src": src, "src_dur_sec": sd, "target_dur_sec": td,
                         "speedup": round(sd / td, 3), "start_sec": round(t, 3), "end_sec": round(t + td, 3)})
    t = round(t + td, 3)
assert t == 7.7, t

ov_texts = ["19 years inspecting chickens on the line. i said a camera can't see a sick bird.",
            "they ran it behind me for six weeks and printed my day by the hour.",
            "7am: we agreed on everything. after 2:30 i passed birds it flagged at 3x.",
            "i wasn't a bad inspector. i was a bad inspector after 2:30."]
ov_durs = [2.0, 2.0, 2.0, 1.7]
overlays, t = [], 0.0
for i, (txt, d) in enumerate(zip(ov_texts, ov_durs), 1):
    overlays.append({"order": i, "text": txt, "start_sec": round(t, 3), "end_sec": round(t + d, 3), "dur_sec": d})
    t = round(t + d, 3)

metadata = {
    "composition_id": f"TLH-{VARIANT}", "format": "tlh", "theme": "ai",
    "theme_angle": "ai-killed-the-poultry-inspector",
    "theme_label": "i told a break room a camera can't see a sick bird, then the plant ran the vision system behind me for six weeks and printed my day by the hour: mornings we agreed on nearly everything, after two thirty i passed birds it flagged at three times my morning rate. nineteen years of being one number on a yearly review, and the machine turned me into eight numbers a day",
    "clip_count": len(source_clips), "overlay_count": len(overlays),
    "caption_style": "here-is-a-story-8beat", "description_style": "first_person_confession",
    "source_repo": "social-autoposter/mixer",
    "engagement_style": "ig_time_sliced_accuracy_arc",
    "new_style": {
        "description": "the narrator's whole career is judged as one lifetime number; the machine slices the same work by hour and finds the error lives in a time window, not in the skill, so the narrator was genuinely right in the morning and wrong after lunch.",
        "example": "i wasn't a bad inspector. i was a bad inspector after 2:30.",
        "note": "use when the craft is repetitive shift work where vigilance decays and nobody timestamps judgment; skip when the error is a stable blind spot that would show up at any hour.",
        "why_existing_didnt_fit": "no reference styles were supplied; recent house arcs beat the narrator with blind panels, hidden repeats, cancelling comparisons, sparse samples and silent full-audit tallies. all of those treat the narrator's error as constant. none put the defeat on the clock, where the same eyes pass in the morning and fail in the afternoon.",
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
""", (POST, VARIANT, video_path, f"local:{audio}", caption, 7.7, ["instagram"],
      json.dumps(metadata), json.dumps(overlays), json.dumps(source_clips)))
print("INSERTED", cur.fetchone())
conn.commit()
cur.execute("select length(caption_text), caption_text = %s, post_type, target_account, project_name, metadata->>'engagement_style', metadata->>'theme_angle' from media_posts where post_number=%s", (caption, POST))
print("row check", cur.fetchone())
