#!/usr/bin/env python3.11
import os, json, psycopg2

ROOT = os.path.expanduser("~/social-autoposter")
with open(os.path.join(ROOT, ".env")) as f:
    for line in f:
        if line.startswith("DATABASE_URL="):
            os.environ["DATABASE_URL"] = line.split("=", 1)[1].strip()
            break

POST_NUMBER = 222
VARIANT_ID = "lesson-182"
VIDEO_PATH = os.path.join(ROOT, "mixer/remotion/out/post-222.mp4")
CAPTION_PATH = os.path.join(ROOT, "mixer/remotion/out/post-222.caption.txt")
AUDIO = os.path.expanduser("~/social-autoposter/mixer/audio/track-002_DX15AC9pRLU.m4a")

with open(CAPTION_PATH) as f:
    caption = f.read()

clip_srcs = ["mixer/tlh-16-1.mp4", "mixer/tlh-45-1.mp4", "mixer/tlh-59-1.mp4",
             "mixer/tlh-64-1.mp4", "mixer/tlh-78-1.mp4"]
CLIP = 1.4
source_clips = []
for i, src in enumerate(clip_srcs):
    source_clips.append({
        "order": i, "src": src,
        "src_dur_sec": CLIP, "target_dur_sec": CLIP, "speedup": 1.0,
        "start_sec": round(i * CLIP, 3), "end_sec": round((i + 1) * CLIP, 3),
    })

overlay_texts = [
    "i said agents were a toy.",
    "one 5x'd me before my coffee brewed.",
    "then it dropped the guardrail no test saw.",
    "own the line it must never cross.",
]
OV = 1.75
overlays = []
for i, t in enumerate(overlay_texts):
    overlays.append({"order": i, "text": t, "start_sec": round(i * OV, 3),
                     "end_sec": round((i + 1) * OV, 3), "dur_sec": OV})

metadata = {
    "composition_id": "TLH-lesson-182",
    "format": "TLH",
    "theme": "ai",
    "theme_angle": "ai-agents-5x-then-break",
    "theme_label": "AI agents: where they 5x you and where they break",
    "clip_count": 5,
    "overlay_count": 4,
    "caption_style": "ig_it_worked_until_it_didnt_arc",
    "description_style": "ig_it_worked_until_it_didnt_arc",
    "source_repo": "social-autoposter/mixer",
    "engagement_style": "ig_it_worked_until_it_didnt_arc",
    "new_style": {
        "description": "A double-turn agent story: delegate a real task, watch the agent over-deliver (the 5x), then watch it break on the one thing that actually mattered, and let that break define what the human is now for.",
        "example": "the agent shipped the whole migration by morning. then it quietly dropped the one guardrail no test covered. that gap is the job now.",
        "note": "use for the AI-agents angle where the honest take is 'both things are true' (it is stunning AND it breaks). do NOT use for a clean denial-then-humbling (that is ig_defeat_flip_arc) or a pure hype/money claim.",
        "why_existing_didnt_fit": "the defeat_flip arc is a single reversal built on being publicly wrong then humbled; here the narrator was never wrong, they trusted the agent and got a sharper, less flattering lesson from watching exactly where it broke.",
        "target_chars": 1800,
    },
}

conn = psycopg2.connect(os.environ["DATABASE_URL"])
conn.autocommit = True
cur = conn.cursor()

cur.execute("SELECT post_number, caption_version FROM media_posts WHERE variant_id=%s", (VARIANT_ID,))
existing = cur.fetchone()

cols = dict(
    post_number=POST_NUMBER,
    project_name=None,
    variant_id=VARIANT_ID,
    video_path=VIDEO_PATH,
    audio_source="local:" + AUDIO,
    caption_text=caption,
    caption_version="v1",
    duration_sec=7,
    width=1080,
    height=1920,
    status="draft",
    post_type="organic",
    target_account="matthewheartful",
    source_clips=json.dumps(source_clips),
    overlays=json.dumps(overlays),
    metadata=json.dumps(metadata),
)

if existing:
    raise SystemExit(f"variant {VARIANT_ID} already exists at post_number={existing[0]}; aborting (unexpected for a fresh variant)")

keys = list(cols.keys())
placeholders = ", ".join(["%s"] * len(keys))
collist = ", ".join(keys)
cur.execute(
    f"INSERT INTO media_posts ({collist}) VALUES ({placeholders}) RETURNING id, post_number",
    [cols[k] for k in keys],
)
row = cur.fetchone()
print(f"INSERTED media_posts id={row[0]} post_number={row[1]} variant={VARIANT_ID} status=draft target_account=matthewheartful project_name=NULL")
cur.close()
conn.close()
