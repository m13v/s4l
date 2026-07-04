#!/usr/bin/env python3.11
"""Insert the media_posts row for post-230 (organic TLH, lesson-190).

DATABASE_URL was removed from ~/social-autoposter/.env; the canonical value is
preserved in the macOS keychain under service 's4l-database-url'. There is no
HTTP create endpoint for media_posts (base POST 404, by-number POST 405, and
by-number PATCH exposes only update actions: mark_posted / sync_caption /
caption_too_long), so the create still goes direct via psycopg2, same as the
prior insert_post_NNN.py scripts.

NOTE (post-230 fire): the render machine was on a mobile egress IP
(172.56.213.54) not on the Cloud SQL authorized-networks allowlist, so the
direct connect to 34.162.152.77:5432 timed out at render time. The mp4,
caption, and data.ts lesson-190 entry were all produced; only this row could
not be written. Re-run this script from an allowlisted network to land it.
"""
import os, json, subprocess, psycopg2

ROOT = os.path.expanduser("~/social-autoposter")

DATABASE_URL = subprocess.run(
    ["security", "find-generic-password", "-s", "s4l-database-url", "-w"],
    capture_output=True, text=True,
).stdout.strip()
if not DATABASE_URL:
    raise SystemExit("could not read s4l-database-url from keychain")

POST_NUMBER = 230
VARIANT_ID = "lesson-190"
VIDEO_PATH = os.path.join(ROOT, "mixer/remotion/out/post-230.mp4")
CAPTION_PATH = os.path.join(ROOT, "mixer/remotion/out/post-230.caption.txt")
AUDIO = os.path.expanduser(
    "~/social-autoposter/mixer/audio/track-006_iphone-6FEC28CD.m4a")

with open(CAPTION_PATH) as f:
    caption = f.read()

# 4 pre-encoded b-roll slots, played as-is (speedup 1.0), 8.0s comp.
# Fresh recombination: zero prior co-occurrence vs all 188 prior lesson-* sets.
# tlh-5-1 encoded 2.667s holds to 2.0s; the other three encoded 2.0s.
clip_srcs = ["mixer/tlh-5-1.mp4", "mixer/tlh-76-1.mp4",
             "mixer/tlh-78-1.mp4", "mixer/tlh-79-1.mp4"]
clip_src_dur = [2.667, 2.0, 2.0, 2.0]
CLIP = 2.0
source_clips = []
for i, src in enumerate(clip_srcs):
    source_clips.append({
        "order": i, "src": src,
        "src_dur_sec": clip_src_dur[i], "target_dur_sec": CLIP,
        "speedup": round(clip_src_dur[i] / CLIP, 4),
        "start_sec": round(i * CLIP, 3), "end_sec": round((i + 1) * CLIP, 3),
    })

overlay_texts = [
    "the agent shipped the feature by noon.",
    "it wrote the migration. it cleared support.",
    "it did everything except decide.",
    "speed is free. taste isnt.",
]
OV = 2.0
overlays = []
for i, t in enumerate(overlay_texts):
    overlays.append({"order": i, "text": t, "start_sec": round(i * OV, 3),
                     "end_sec": round((i + 1) * OV, 3), "dur_sec": OV})

# mode=invent: NEW ig_-prefixed archetype. Structural archetype = an operator's
# weekly LEDGER of what the AI agent did (itemized parallel "it did X" beats),
# pivoting to the one thing it cannot do (taste / the last decision), then a
# one-line lesson + closing instruction. NOT the ig_defeat_flip_arc.
new_style = {
    "name": "ig_agent_ledger_last_mile",
    "description": (
        "confident operator's itemized ledger of what an AI agent now does for "
        "them (day-by-day parallel 'it did X' beats), pivoting to the single "
        "irreducible thing it cannot do (taste / deciding what the work is for), "
        "then a sharp one-line lesson and a closing instruction. an inventory, "
        "not a public-wrongness defeat arc."),
    "example": (
        "monday it shipped the feature. tuesday it cleared support. friday i "
        "realized it had done every line on my job. the one thing it could not "
        "do was decide the feature was wrong."),
    "note": (
        "use when the narrator has already ADOPTED agents and is calm/authoritative "
        "rather than defeated; the tension is speed-vs-judgment, not "
        "denial-then-reckoning. do not use when the story needs a 'i was wrong in "
        "public' humiliation beat (that is ig_defeat_flip_arc)."),
    "why_existing_didnt_fit": (
        "ig_defeat_flip_arc is a linear single-humiliation arc (posted a confident "
        "take, a junior beat me, cold coffee, i changed what i sell). this post is "
        "an inventory of wins that lands on a boundary, with no public-wrongness "
        "beat and no defeat; the confident-ledger structure is a genuinely "
        "different archetype, not a rename."),
    "target_chars": 1900,
}

metadata = {
    "composition_id": "TLH-lesson-190",
    "format": "TLH",
    "theme": "ai",
    "theme_angle": "ai-agent-speed-vs-taste",
    "theme_label": "ai agents give you speed, you still own taste",
    "clip_count": 4,
    "overlay_count": 4,
    "caption_style": "ig_agent_ledger_last_mile",
    "description_style": "ig_agent_ledger_last_mile",
    "source_repo": "social-autoposter/mixer",
    "engagement_style": "ig_agent_ledger_last_mile",
    "new_style": new_style,
}

cols = dict(
    post_number=POST_NUMBER,
    project_name=None,               # organic: intentionally product-free
    variant_id=VARIANT_ID,
    video_path=VIDEO_PATH,
    audio_source="local:" + AUDIO,
    caption_text=caption,
    caption_version="v1",
    duration_sec=8,
    width=1080,
    height=1920,
    status="draft",
    post_type="organic",
    target_account="matt_diak",
    source_clips=json.dumps(source_clips),
    overlays=json.dumps(overlays),
    metadata=json.dumps(metadata),
)

conn = psycopg2.connect(DATABASE_URL, connect_timeout=15)
conn.autocommit = True
cur = conn.cursor()

cur.execute("SELECT post_number FROM media_posts WHERE variant_id=%s", (VARIANT_ID,))
existing = cur.fetchone()
if existing:
    raise SystemExit(
        f"variant {VARIANT_ID} already exists at post_number={existing[0]}; aborting")
cur.execute("SELECT 1 FROM media_posts WHERE post_number=%s", (POST_NUMBER,))
if cur.fetchone():
    raise SystemExit(f"post_number {POST_NUMBER} already exists; aborting")

keys = list(cols.keys())
placeholders = ", ".join(["%s"] * len(keys))
collist = ", ".join(keys)
cur.execute(
    f"INSERT INTO media_posts ({collist}) VALUES ({placeholders}) RETURNING id, post_number",
    [cols[k] for k in keys],
)
row = cur.fetchone()
print(f"INSERTED media_posts id={row[0]} post_number={row[1]} variant={VARIANT_ID} "
      f"status=draft post_type=organic target_account=matt_diak project_name=NULL")
cur.close()
conn.close()
