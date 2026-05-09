#!/bin/bash
# run-instagram-render.sh — Spawn `claude -p` to render ONE fresh IG reel
# end-to-end per mixer/SKILL.md.
#
# Cadence (com.m13v.social-instagram-render.plist):
#   5 fires/day at 08:30, 11:30, 14:30, 17:30, 20:30 local time, 30 min
#   before each post-cycle slot.
#
# Per-fire logic:
#   1. acquire_lock instagram-render (data.ts edits race otherwise)
#   2. compute target post_type via 4:1 of last 5 posted IG rows
#   3. count existing drafts of that type. If >=3, SKIP (buffer healthy).
#   4. pull exclusion lists from Neon: used_audio (30d), used_angles (14d),
#      used_variant_ids (all-time).
#   5. spawn run_claude.sh with mixer/SKILL.md as the procedure, plus a
#      compact request envelope (type, post_number, exclusions).
#   6. on exit, verify post-NNN.mp4, post-NNN.caption.txt, media_posts row
#      with status='draft' and post_type matching target.
#
# Exit codes:
#   0 - rendered, OR buffer healthy and skipped, OR another run holds the lock
#   1 - real failure (claude error, ffmpeg fail, missing deliverables)

set -uo pipefail

REPO_DIR="$HOME/social-autoposter"
LOG_DIR="$REPO_DIR/skill/logs"
mkdir -p "$LOG_DIR"

LOG_FILE="$LOG_DIR/instagram-render-$(date +%Y-%m-%d_%H%M%S).log"
PICK_FILE="/tmp/ig_render_pick_$(date +%s)_$$.json"

if [ -f "$REPO_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$REPO_DIR/.env"
  set +a
fi

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG_FILE"; }

cleanup() { rm -f "$PICK_FILE"; }
trap cleanup EXIT INT TERM HUP

log "=== instagram-render fire: $(date) ==="

# Lock against parallel renders. data.ts edits + npx remotion compositions
# are not safe to interleave.
# shellcheck source=lock.sh
source "$REPO_DIR/skill/lock.sh"
acquire_lock instagram-render 60

# Step 1: compute target type + exclusion lists
log "step 1: querying Neon for target_type, draft_count, exclusions"
/opt/homebrew/bin/python3.11 - > "$PICK_FILE" 2>>"$LOG_FILE" <<'PY'
import json, os, psycopg2
env = {}
for ln in open(os.path.expanduser('~/social-autoposter/.env')).read().splitlines():
    if '=' in ln and not ln.strip().startswith('#'):
        k, v = ln.split('=', 1)
        env[k.strip()] = v.strip()
conn = psycopg2.connect(env['DATABASE_URL'])
c = conn.cursor()

c.execute(
    "SELECT post_type FROM media_posts "
    "WHERE status='posted' AND posted_urls ? 'instagram' "
    "ORDER BY posted_at DESC LIMIT 5"
)
last5 = [r[0] for r in c.fetchall()]
organic_count = sum(1 for t in last5 if t == 'organic')
target = 'organic' if organic_count < 4 else 'product'

c.execute(
    "SELECT COUNT(*) FROM media_posts WHERE status='draft' AND post_type=%s",
    (target,),
)
draft_count = c.fetchone()[0]

c.execute("SELECT COALESCE(MAX(post_number), 0) + 1 FROM media_posts")
next_num = c.fetchone()[0]

c.execute(
    "SELECT DISTINCT audio_source FROM media_posts "
    "WHERE created_at > NOW() - INTERVAL '30 days' AND audio_source IS NOT NULL"
)
used_audio = sorted({r[0] for r in c.fetchall()})

c.execute(
    "SELECT DISTINCT metadata->>'theme_angle' FROM media_posts "
    "WHERE created_at > NOW() - INTERVAL '14 days' "
    "AND metadata->>'theme_angle' IS NOT NULL"
)
used_angles = sorted({r[0] for r in c.fetchall() if r[0]})

c.execute(
    "SELECT DISTINCT variant_id FROM media_posts WHERE variant_id IS NOT NULL"
)
used_variants = sorted({r[0] for r in c.fetchall()})

print(json.dumps({
    'target_type': target,
    'last5_posted': last5,
    'organic_count_last5': organic_count,
    'draft_count_target': draft_count,
    'next_post_number': next_num,
    'used_audio_30d': used_audio,
    'used_theme_angles_14d': used_angles,
    'used_variant_ids': used_variants,
}, indent=2))
conn.close()
PY

if [ ! -s "$PICK_FILE" ]; then
  log "ERROR: pick query produced no output"
  exit 1
fi

TARGET=$(/opt/homebrew/bin/python3.11 -c "import json; print(json.load(open('$PICK_FILE'))['target_type'])")
DRAFT_COUNT=$(/opt/homebrew/bin/python3.11 -c "import json; print(json.load(open('$PICK_FILE'))['draft_count_target'])")
NEXT_NUM=$(/opt/homebrew/bin/python3.11 -c "import json; print(json.load(open('$PICK_FILE'))['next_post_number'])")
NNN=$(printf "%03d" "$NEXT_NUM")

log "target=$TARGET draft_count_target=$DRAFT_COUNT next_post_number=$NEXT_NUM"

# Buffer guard: if 3+ drafts of target type already exist, skip.
# Override with FORCE_RENDER=1 for manual / first-fire runs.
if [ "${FORCE_RENDER:-0}" != "1" ] && [ "$DRAFT_COUNT" -ge 3 ]; then
  log "skipped: $DRAFT_COUNT drafts of $TARGET already in queue (>= 3 buffer); no render needed"
  exit 0
fi

# Step 2: build prompt and spawn claude
PROMPT_FILE="/tmp/ig_render_prompt_$(date +%s)_$$.txt"

cat > "$PROMPT_FILE" <<PROMPT_EOF
You are the Instagram render-cycle agent for the social-autoposter project.
Your single job for this fire: render ONE fresh Instagram reel for posting,
following ~/social-autoposter/mixer/SKILL.md to the letter.

READ THE SKILL FIRST: ~/social-autoposter/mixer/SKILL.md is your complete
creative + technical procedure. It explains the two formats (Mixer for niche
product reels, TLH for AI-lesson hook reels), the caption arc, the ffmpeg
encode + dub commands, the data.ts variant schema, and the media_posts row
shape. Follow it exactly. When SKILL and this prompt disagree, SKILL wins.

REQUEST ENVELOPE FOR THIS RUN:
$(cat "$PICK_FILE")

TYPE MAPPING (do NOT swap these):
- post_type='organic' -> TLH format. AI-themed lesson, NO product mention by
  name (no Fazm, no Mediar, no AppMaker, no mk0r). 7-8s total.
- post_type='product' -> Mixer format. niche template (spa/autoshop/hotel/
  mk0r-retail) with mk0r baked into intro + finale. 26-28s total.

DELIVERABLES (must all exist when you exit):
1. ~/social-autoposter/mixer/remotion/out/post-${NNN}.mp4
   1080x1920, audio dubbed, ready to post. Filename uses post_number=${NEXT_NUM}.
2. ~/social-autoposter/mixer/remotion/out/post-${NNN}.caption.txt
   The Instagram caption story (the "here is a story." body for TLH, the
   niche-specific story for Mixer). UTF-8, plain text, no markdown.
3. media_posts row with post_number=${NEXT_NUM}, status='draft',
   post_type='${TARGET}', and all SKILL Section 5 columns populated
   (variant_id, video_path, caption_text, source_clips, overlays, audio_source,
   metadata.theme_angle, etc.). project_name='fazm' for organic, 'mk0r' for product.

VISUAL STYLE (current as of May 7 2026, do NOT regress):
The Overlays.tsx and TimeLapseHookComposition.tsx components were rewritten
on May 7 to:
  - white background, black text on title cards and overlays
  - instant on: NO spring pop-in, NO fade-up, NO scale-in, NO fade-out
  - elements stay solid for the full overlay duration
DO NOT modify Overlays.tsx, TimeLapseHookComposition.tsx, or MixerComposition.tsx.
If the existing components don't render the way you want for this variant, FAIL
the run and ask the user, do not "fix" the components. The components are the
deliverable contract for ALL future renders.

EXCLUSIONS (read from request envelope above; honor strictly):
- used_audio_30d: do NOT reuse any of these audio file paths.
- used_theme_angles_14d: pick a different angle. SKILL Section 3 lists 5
  acceptable AI angles; pick one not in the exclusion list.
- used_variant_ids: if creating a NEW TLH variant, pick a variant_id not in
  this list. For Mixer, you re-render an existing niche; that's allowed.

PRODUCT-PATH SHORTCUT (post_type='product'):
The 4 existing Mixer variants (spa, autoshop, hotel, mk0r-retail) already
have all clips encoded. For product runs you should re-render one of them
with current Overlays.tsx and write a fresh niche-targeted caption. Pick the
niche least-recently-rendered (check media_posts created_at by variant_id).
A NEW niche requires raw clips that may not exist; only attempt that if
SKILL Section 2 prerequisites are met.

ORGANIC-PATH (post_type='organic'):
Compose a new TLH variant. You may remix existing pre-encoded
remotion/public/mixer/tlh-*.mp4 slots (cheaper, faster) OR encode fresh raw
clips from ~/social-autoposter/mixer/'5. time lapse hooks/' if available
(only if remixing produces a stale recombination). The audio_source MUST be
fresh (not in used_audio_30d). The caption MUST follow SKILL Section 3
caption arc (8 beats: opener, age+setup, wrong-about-AI moment, breaking
event, felt-sense, workflow change, contrarian one-liner, closing
instruction). Theme angle must be in SKILL Section 3 list and NOT in
used_theme_angles_14d.

DO NOT post to Instagram. The post-cycle (skill/run-instagram-daily.sh)
posts separately on its own schedule. Your job ends at status='draft'.

Your final stdout line MUST be exactly one summary line in this format:
  RENDERED post-${NNN} type=${TARGET} variant=<variant_id> angle="<theme_angle>"

If you fail or skip, your final stdout line MUST be:
  FAILED post-${NNN} reason=<short reason>

Begin. Read the SKILL, then execute.
PROMPT_EOF

log "step 2: spawning claude -p (will run for several minutes)"

# CLAUDE_MODEL is honored if exported (override of global default in
# ~/.claude/settings.json); otherwise CLI uses settings.json default.
if ! "$REPO_DIR/scripts/run_claude.sh" "run-instagram-render" \
        ${CLAUDE_MODEL:+--model "$CLAUDE_MODEL"} \
        --permission-mode bypassPermissions \
        -p "$(cat "$PROMPT_FILE")" >>"$LOG_FILE" 2>&1; then
  rc=$?
  log "claude exited rc=$rc"
  rm -f "$PROMPT_FILE"
  if [ "$rc" -eq 79 ]; then
    log "claude blocked by quota stamp; will retry next cycle"
    exit 0
  fi
  log "render failed"
  exit 1
fi
rm -f "$PROMPT_FILE"

# Step 3: verify deliverables
OUT_MP4="$REPO_DIR/mixer/remotion/out/post-${NNN}.mp4"
OUT_CAP="$REPO_DIR/mixer/remotion/out/post-${NNN}.caption.txt"

log "step 3: verifying deliverables"
log "  expected: $OUT_MP4"
log "  expected: $OUT_CAP"

if [ ! -f "$OUT_MP4" ]; then
  log "ERROR: $OUT_MP4 missing"
  exit 1
fi
if [ ! -f "$OUT_CAP" ]; then
  log "ERROR: $OUT_CAP missing"
  exit 1
fi

ROW_OK=$(/opt/homebrew/bin/python3.11 - "$NEXT_NUM" "$TARGET" 2>>"$LOG_FILE" <<'PY'
import json, os, psycopg2, sys
env = {}
for ln in open(os.path.expanduser('~/social-autoposter/.env')).read().splitlines():
    if '=' in ln and not ln.strip().startswith('#'):
        k, v = ln.split('=', 1)
        env[k.strip()] = v.strip()
c = psycopg2.connect(env['DATABASE_URL']).cursor()
c.execute(
    "SELECT post_number, post_type, variant_id, status FROM media_posts WHERE post_number=%s",
    (int(sys.argv[1]),),
)
r = c.fetchone()
if not r:
    print("MISSING")
elif r[1] != sys.argv[2]:
    print(f"BAD_TYPE got={r[1]} want={sys.argv[2]}")
elif r[3] != 'draft':
    print(f"BAD_STATUS got={r[3]} want=draft")
else:
    print(f"OK variant={r[2]}")
PY
)

case "$ROW_OK" in
  OK*) log "DB row OK: $ROW_OK" ;;
  *)   log "ERROR: DB row check failed: $ROW_OK"; exit 1 ;;
esac

VARIANT=$(echo "$ROW_OK" | sed 's/^OK variant=//')
log "=== rendered post-${NNN} (${TARGET}, variant=${VARIANT}) successfully ==="
exit 0
