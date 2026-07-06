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
#   4. pull from Postgres: local_audio_lru (LRU-ordered local mixer/audio pool),
#      used_angles (14d), used_variant_ids (all-time).
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

# Cycle ID for cross-cycle cost accounting. The single claude -p invocation
# inherits this via env so log_claude_session.py stamps claude_sessions.cycle_id.
BATCH_ID="${BATCH_ID:-igren-$(date +%Y%m%d-%H%M%S)}"
export BATCH_ID
export SA_CYCLE_ID="$BATCH_ID"

LOG_FILE="$LOG_DIR/instagram-render-$(date +%Y-%m-%d_%H%M%S).log"
PICK_FILE="/tmp/ig_render_pick_$(date +%s)_$$.json"

if [ -f "$REPO_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$REPO_DIR/.env"
  set +a
fi

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG_FILE"; }

# Run accounting for dashboard Job History (Render · Instagram). Matches the
# pattern in run-instagram-daily.sh / run-twitter-threads.sh / run-reddit-threads.sh:
# each exit site updates POSTED_CT / SKIPPED_CT / FAILED_CT; the EXIT trap
# always emits one log_run.py line so the run shows up under render_instagram.
# "posted" here means "rendered a fresh draft"; "skipped" means buffer was
# healthy (>=3 drafts) or the lock was already held; "failed" is any real
# error path.
RUN_START_EPOCH=$(date +%s)
POSTED_CT=0
SKIPPED_CT=0
FAILED_CT=0

cleanup() {
  local rc=$?
  rm -f "$PICK_FILE" "${UNPROVEN_JSON_FILE:-}"
  if [ "$POSTED_CT" -eq 0 ] && [ "$SKIPPED_CT" -eq 0 ] && [ "$FAILED_CT" -eq 0 ]; then
    if [ "$rc" -eq 0 ]; then SKIPPED_CT=1; else FAILED_CT=1; fi
  fi
  local elapsed=$(( $(date +%s) - RUN_START_EPOCH ))
  local cost
  cost=$(/usr/bin/python3 "$REPO_DIR/scripts/get_run_cost.py" --since "$RUN_START_EPOCH" --scripts "run-instagram-render" 2>/dev/null || echo "0.0000")
  /usr/bin/python3 "$REPO_DIR/scripts/log_run.py" \
      --script "render_instagram" \
      --posted "$POSTED_CT" --skipped "$SKIPPED_CT" --failed "$FAILED_CT" \
      --cost "$cost" --elapsed "$elapsed" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM HUP

log "=== instagram-render fire: $(date) ==="

# Lock against parallel renders. data.ts edits + npx remotion compositions
# are not safe to interleave.
# shellcheck source=lock.sh
source "$REPO_DIR/skill/lock.sh"
acquire_lock instagram-render 60

# Step 0: pick TARGET account (inverse-recent-share over enabled IG accounts in
# config.json:instagram.accounts[]). Honors FORCE_ACCOUNT env override. The
# chosen account scopes Step 1 queries (type buffer, used angles, used
# variants, audio LRU) so each account has its own rotation.
log "step 0: picking target account"
if [ -n "${FORCE_ACCOUNT:-}" ]; then
  TARGET_ACCOUNT=$("$REPO_DIR/scripts/pick_ig_account.py" --account "$FORCE_ACCOUNT" 2>>"$LOG_FILE")
else
  TARGET_ACCOUNT=$("$REPO_DIR/scripts/pick_ig_account.py" 2>>"$LOG_FILE")
fi
if [ -z "$TARGET_ACCOUNT" ]; then
  log "ERROR: pick_ig_account.py returned empty; no enabled accounts?"
  FAILED_CT=1
  exit 1
fi
export TARGET_ACCOUNT
log "target_account=$TARGET_ACCOUNT"

# Step 1: compute target type + exclusion lists (scoped to TARGET_ACCOUNT)
log "step 1: querying Postgres for target_type, draft_count, exclusions (account=$TARGET_ACCOUNT)"
/opt/homebrew/bin/python3.11 - > "$PICK_FILE" 2>>"$LOG_FILE" <<'PY'
import glob, json, os, random, sys
from datetime import datetime
# HTTP-only (2026-06-01): all media_posts reads route through
# /api/v1/media-posts/picker-context via http_api. No DATABASE_URL, no
# psycopg2, no fallback. The api_get call happens once below (after
# target_account + recent_window_days are known) and every aggregate the
# picker needs is read out of the returned context dict.
sys.path.insert(0, os.path.expanduser('~/social-autoposter/scripts'))
from http_api import api_get

# Inverse-recent-share weighting at TWO levels, mirroring the Twitter pipeline
# (scripts/pick_project.py). Effective weight = config_weight / (1 + posts in
# the last RECENT_WINDOW_DAYS). Over-posting damps without ever exceeding the
# raw config weight; under-posting catches up automatically.
#
# Two env-var overrides bypass the stochastic rolls (one-shot / debugging):
#   FORCE_TYPE=organic|product       skip Level-1 roll
#   FORCE_PROJECT=<name>             skip Level-2 roll (implies product)

cfg = json.load(open(os.path.expanduser('~/social-autoposter/config.json')))
ig_cfg = cfg.get('instagram', {}) or {}
recent_window_days = int(ig_cfg.get('recent_window_days', 7))
post_type_weights_cfg = (
    ig_cfg.get('post_type_weights')
    or ig_cfg.get('post_type_ratio')
    or {'organic': 4, 'product': 1}
)
force_type = os.environ.get('FORCE_TYPE') or ''
force_project = os.environ.get('FORCE_PROJECT') or ''
if force_project and not force_type:
    force_type = 'product'  # FORCE_PROJECT implies product

# Target account: scopes every recency / buffer / exclusion query so each
# account has its own type rotation, variant pool, angle list, audio LRU.
target_account = os.environ.get('TARGET_ACCOUNT', '').strip()
if not target_account:
    raise SystemExit("TARGET_ACCOUNT env var missing (Step 0 should have set it)")

# Per-account overrides: each account in instagram.accounts[] may override the
# global post_type_weights and supply a `tlh` block (source_dir,
# variant_prefix, story_brief, unproven_dir, caption_opener) that scopes the
# TLH render to its own clip pool + voice. Accounts without these fields
# fall back to globals (matt_diak / matthewheartful behavior is unchanged).
account_record = next(
    (a for a in (ig_cfg.get('accounts') or [])
     if a.get('username', '').lower() == target_account.lower()),
    {}
)
if account_record.get('post_type_weights'):
    post_type_weights_cfg = account_record['post_type_weights']
account_tlh_config = account_record.get('tlh') or {}

# Single HTTP read: every media_posts aggregate the picker needs, scoped to
# this account + recency window. Local weighting / glob / JSON shaping below
# is unchanged; only the data source moved from psycopg2 to the API.
_pc = api_get(
    '/api/v1/media-posts/picker-context',
    query={'target_account': target_account, 'window_days': recent_window_days},
)
ctx = (_pc.get('data') or {})

# Last-N posted descriptor (kept for telemetry / log readability).
last10 = list(ctx.get('last10_posted') or [])

# ---- LEVEL 1: organic vs product, inverse-recent-share (or FORCE_TYPE) ----
recent_type_counts = {k: v for k, v in (ctx.get('recent_type_counts') or {}).items() if k}
for t in ('organic', 'product'):
    recent_type_counts.setdefault(t, 0)
type_weights = {
    t: float(post_type_weights_cfg.get(t, 0)) / (1 + recent_type_counts[t])
    for t in ('organic', 'product')
    if float(post_type_weights_cfg.get(t, 0)) > 0
}
if force_type in ('organic', 'product'):
    target = force_type
elif not type_weights:
    target = 'organic'  # defensive default if config is empty
else:
    names = list(type_weights.keys())
    ws = [type_weights[n] for n in names]
    target = random.choices(names, weights=ws, k=1)[0]

# ---- LEVEL 2: which project, inverse-recent-share (product only) ----
# Organic content is intentionally product-free -> project_name=NULL.
selected_project = None
mixer_enabled_projects = []
project_post_counts = {}
project_weights = {}
if target == 'product':
    enabled = [
        p for p in cfg.get('projects', [])
        if isinstance(p.get('mixer'), dict)
        and p['mixer'].get('enabled') is True
        and p.get('weight', 0) > 0
    ]
    mixer_enabled_projects = sorted([p['name'] for p in enabled])
    if not enabled:
        # defensive fallback to mk0r if no project is flagged
        enabled = [{'name': 'mk0r', 'weight': 1}]
        mixer_enabled_projects = ['mk0r']

    recent_proj_counts = dict(ctx.get('recent_product_counts_by_project') or {})
    for p in enabled:
        project_post_counts[p['name']] = recent_proj_counts.get(p['name'], 0)
    project_weights = {
        p['name']: float(p['weight']) / (1 + project_post_counts[p['name']])
        for p in enabled
    }
    if force_project:
        if force_project not in [p['name'] for p in enabled]:
            raise SystemExit(
                f"FORCE_PROJECT={force_project!r} not in mixer.enabled projects: "
                f"{[p['name'] for p in enabled]}"
            )
        selected_project = force_project
    else:
        names = list(project_weights.keys())
        ws = [project_weights[n] for n in names]
        selected_project = random.choices(names, weights=ws, k=1)[0]

# Per-(account, type, project) draft buffer. Each account has its own
# rotation; a build-up on matt_diak should not block a heartfulmatthew render.
# draft_counts comes back grouped by (post_type, project_name); sum the rows
# the same way the two SQL variants did (product = type+project, organic =
# type only, project-agnostic).
_draft_rows = ctx.get('draft_counts') or []
if target == 'product':
    draft_count = sum(
        int(r.get('count') or 0) for r in _draft_rows
        if r.get('post_type') == target and r.get('project_name') == selected_project
    )
else:
    draft_count = sum(
        int(r.get('count') or 0) for r in _draft_rows
        if r.get('post_type') == target
    )

next_num = int(ctx.get('next_post_number') or 1)

# Audio policy: local-only, least-recently-used rotation. The render must
# reuse the existing mixer/audio/ pool and NEVER source fresh audio from the
# network. We list the on-disk tracks and order them by last use in
# media_posts (never-used first, then oldest-used first). audio_source values
# vary in shape (local:/abs/path, local:~/path, ig://reel/<code>), so a track
# counts as "used" by a row if the row's audio_source contains the track's
# basename OR its trailing token (reel code / label after the last '_').
audio_dir = os.path.expanduser('~/social-autoposter/mixer/audio')
local_files = sorted(glob.glob(os.path.join(audio_dir, '*.m4a')))

# audio_usage rows arrive as [audio_source, used_at_iso]; parse the ISO
# timestamp back to a datetime so the LRU comparison + .isoformat() sort below
# work exactly as they did with psycopg2-returned datetimes.
def _parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    except Exception:
        return None
audio_usage = [(r[0] or '', _parse_dt(r[1])) for r in (ctx.get('audio_usage') or [])]


def _audio_token(path):
    stem = os.path.splitext(os.path.basename(path))[0]
    return stem.rsplit('_', 1)[-1] if '_' in stem else stem


audio_lru = []
for f in local_files:
    base = os.path.basename(f)
    tok = _audio_token(f)
    last_used = None
    for src, used_at in audio_usage:
        if used_at is None:
            continue
        if base in src or (tok and tok in src):
            if last_used is None or used_at > last_used:
                last_used = used_at
    audio_lru.append((f, last_used))

# never-used tracks first, then oldest-used first
audio_lru.sort(key=lambda x: (1, x[1].isoformat()) if x[1] else (0, ''))
local_audio_lru = [f for f, _ in audio_lru]

used_angles = sorted({a for a in (ctx.get('used_theme_angles_14d') or []) if a})

used_variants = sorted({v for v in (ctx.get('used_variant_ids') or []) if v is not None})

print(json.dumps({
    'target_account': target_account,
    'target_type': target,
    'last10_posted': last10,
    'recent_window_days': recent_window_days,
    'recent_type_counts': recent_type_counts,
    'type_weights_effective': type_weights,
    'post_type_weights_config': post_type_weights_cfg,
    'draft_count_target': draft_count,
    'next_post_number': next_num,
    'local_audio_lru': local_audio_lru,
    'used_theme_angles_14d': used_angles,
    'used_variant_ids': used_variants,
    # Level-2 product routing (NULL when target=='organic').
    'selected_project': selected_project,
    'mixer_enabled_projects': mixer_enabled_projects,
    'recent_product_posts_by_project': project_post_counts,
    'project_weights_effective': project_weights,
    # Per-account TLH overrides (organic format). Empty dict {} means
    # this account uses SKILL.md defaults (Matt's '5. time lapse hooks/'
    # source + AI-defeat caption arc). When non-empty, Claude MUST use
    # these overrides for source_dir, variant_prefix, and story_brief.
    'account_tlh_config': account_tlh_config,
}, indent=2))
PY

if [ ! -s "$PICK_FILE" ]; then
  log "ERROR: pick query produced no output"
  FAILED_CT=1
  exit 1
fi

TARGET=$(/opt/homebrew/bin/python3.11 -c "import json; print(json.load(open('$PICK_FILE'))['target_type'])")
DRAFT_COUNT=$(/opt/homebrew/bin/python3.11 -c "import json; print(json.load(open('$PICK_FILE'))['draft_count_target'])")
NEXT_NUM=$(/opt/homebrew/bin/python3.11 -c "import json; print(json.load(open('$PICK_FILE'))['next_post_number'])")
SELECTED_PROJECT=$(/opt/homebrew/bin/python3.11 -c "import json; print(json.load(open('$PICK_FILE')).get('selected_project') or '')")
NNN=$(printf "%03d" "$NEXT_NUM")

log "target=$TARGET selected_project=${SELECTED_PROJECT:-<null/organic>} draft_count_target=$DRAFT_COUNT next_post_number=$NEXT_NUM"

# Step 1.6: engagement style assignment. Mirrors the twitter cycle pattern:
# - Organic runs: roll via picker (95% top performer / 5% invent).
# - Product runs: deterministic mapping from selected_project (the picker
#   never sees walkin/studyly because they're in PLATFORM_POLICY.instagram.never).
# The assignment is injected into the Claude prompt envelope below and Claude
# stamps metadata.engagement_style on the media_posts row. sync_ig_to_posts.py
# mirrors that field to posts.engagement_style for the dashboard A/B.
source "$REPO_DIR/skill/styles.sh"
STYLE_ASSIGN_FILE=$(mktemp -t s4l_ig_style_XXXXXX.json)
if [ "$TARGET" = "organic" ]; then
  s4l_pick_style instagram posting "$STYLE_ASSIGN_FILE" >/dev/null 2>>"$LOG_FILE" || true
  PICKED_STYLE=$(/opt/homebrew/bin/python3.11 -c "import json; print(json.load(open('$STYLE_ASSIGN_FILE')).get('style') or '')")
  PICK_MODE=$(/opt/homebrew/bin/python3.11 -c "import json; print(json.load(open('$STYLE_ASSIGN_FILE')).get('mode',''))")
else
  case "$SELECTED_PROJECT" in
    mk0r)    PICKED_STYLE="ig_walkin_storefront_playbook" ;;
    studyly) PICKED_STYLE="ig_studyly_failing_student_arc" ;;
    *)       PICKED_STYLE="" ;;
  esac
  PICK_MODE="use"
  /opt/homebrew/bin/python3.11 - "$STYLE_ASSIGN_FILE" "$PICKED_STYLE" "$PICK_MODE" <<'PY'
import json, sys
out, style, mode = sys.argv[1], sys.argv[2], sys.argv[3]
with open(out, 'w') as f:
    json.dump({
        "mode": mode, "style": style or None,
        "description": None, "example": None, "note": None,
        "reference_styles": [], "distribution_snapshot": [],
        "source": "project_gated",
    }, f)
PY
fi
STYLES_BLOCK=$(s4l_render_style_block "$STYLE_ASSIGN_FILE" instagram posting)
log "engagement_style: picked='${PICKED_STYLE}' mode='${PICK_MODE}' target=${TARGET} project=${SELECTED_PROJECT:-organic}"

# Buffer guard: if 3+ drafts of target type already exist, skip.
# Override with FORCE_RENDER=1 for manual / first-fire runs.
if [ "${FORCE_RENDER:-0}" != "1" ] && [ "$DRAFT_COUNT" -ge 3 ]; then
  log "skipped: $DRAFT_COUNT drafts of $TARGET already in queue (>= 3 buffer); no render needed"
  SKIPPED_CT=1
  exit 0
fi

# Step 1.5: organic only — 50% rotation injects one untried clip from
# 'mixer/unproven new content/' as one of the TLH slots. "Tried once" is
# tracked by media_posts.metadata->>'unproven_clip_basename'; once that
# basename appears on any row, the clip is retired from the rotation pool.
# Override with FORCE_UNPROVEN=1 (always pick if any untried) or
# FORCE_UNPROVEN=0 (never pick). Default: probabilistic 50%.
UNPROVEN_JSON_FILE="/tmp/ig_unproven_pick_$(date +%s)_$$.json"
echo '{"use": false, "reason": "default"}' > "$UNPROVEN_JSON_FILE"
if [ "$TARGET" = "organic" ]; then
  /opt/homebrew/bin/python3.11 - "$UNPROVEN_JSON_FILE" > /dev/null 2>>"$LOG_FILE" <<'PY'
import json, os, random, sys, glob
# HTTP-only (2026-06-01): used-clip basenames come from
# /api/v1/media-posts/unproven-clips. No DATABASE_URL, no psycopg2.
sys.path.insert(0, os.path.expanduser('~/social-autoposter/scripts'))
from http_api import api_get
out_path = sys.argv[1]
# Resolve per-account unproven_dir override from config.json. If the
# account opts out (tlh.unproven_dir == null) the step short-circuits.
cfg = json.load(open(os.path.expanduser('~/social-autoposter/config.json')))
target_account_init = os.environ.get('TARGET_ACCOUNT', '').strip()
account_record_init = next(
    (a for a in ((cfg.get('instagram') or {}).get('accounts') or [])
     if a.get('username', '').lower() == target_account_init.lower()),
    {}
)
tlh_cfg_init = account_record_init.get('tlh') or {}
if 'unproven_dir' in tlh_cfg_init:
    unproven_dir_raw = tlh_cfg_init.get('unproven_dir')
    unproven_dir = os.path.expanduser(unproven_dir_raw) if unproven_dir_raw else None
else:
    # Default: matt_diak / matthewheartful unchanged.
    unproven_dir = os.path.expanduser('~/social-autoposter/mixer/unproven new content')
result = {"use": False}
if unproven_dir is None:
    result = {"use": False, "reason": "account opted out of unproven rotation (tlh.unproven_dir=null)"}
    with open(out_path, 'w') as f:
        json.dump(result, f, indent=2)
    raise SystemExit(0)
try:
    target_account = os.environ.get('TARGET_ACCOUNT', '').strip()
    if not target_account:
        raise SystemExit("TARGET_ACCOUNT env var missing in unproven-clip step")
    _uc = api_get(
        '/api/v1/media-posts/unproven-clips',
        query={'target_account': target_account},
    )
    used = {b for b in ((_uc.get('data') or {}).get('basenames') or []) if b}
    if not os.path.isdir(unproven_dir):
        result = {"use": False, "reason": "unproven dir missing", "dir": unproven_dir}
    else:
        candidates = []
        for ext in ('*.MP4', '*.mp4', '*.MOV', '*.mov', '*.m4v', '*.M4V'):
            candidates.extend(glob.glob(os.path.join(unproven_dir, ext)))
        untried = [p for p in candidates if os.path.basename(p) not in used]
        force = os.environ.get('FORCE_UNPROVEN')
        if force == '0':
            roll_use = False
        elif force == '1':
            roll_use = True
        else:
            roll_use = (random.random() < 0.5)
        if roll_use and untried:
            pick = random.choice(untried)
            result = {
                "use": True,
                "basename": os.path.basename(pick),
                "path": pick,
                "untried_count": len(untried),
                "total_count": len(candidates),
                "used_count": len(used),
                "force": force,
            }
        else:
            result = {
                "use": False,
                "reason": (
                    "no untried clips" if not untried
                    else f"coin flip skipped (force={force})"
                ),
                "untried_count": len(untried),
                "total_count": len(candidates),
                "used_count": len(used),
                "force": force,
            }
except Exception as e:
    result = {"use": False, "reason": f"error: {e}"}
with open(out_path, 'w') as f:
    json.dump(result, f, indent=2)
PY
  UNPROVEN_USE=$(/opt/homebrew/bin/python3.11 -c "import json; print(json.load(open('$UNPROVEN_JSON_FILE')).get('use', False))")
  if [ "$UNPROVEN_USE" = "True" ]; then
    UNPROVEN_BASENAME=$(/opt/homebrew/bin/python3.11 -c "import json; print(json.load(open('$UNPROVEN_JSON_FILE'))['basename'])")
    log "unproven clip injected: $UNPROVEN_BASENAME"
  else
    UNPROVEN_REASON=$(/opt/homebrew/bin/python3.11 -c "import json; print(json.load(open('$UNPROVEN_JSON_FILE')).get('reason', 'n/a'))")
    log "unproven clip not injected: $UNPROVEN_REASON"
  fi
fi

# Step 2: build prompt and spawn claude
PROMPT_FILE="/tmp/ig_render_prompt_$(date +%s)_$$.txt"

cat > "$PROMPT_FILE" <<PROMPT_EOF
You are the Instagram render-cycle agent for the social-autoposter project.
Your single job for this fire: render ONE fresh Instagram reel for posting,
following ~/social-autoposter/mixer/SKILL.md to the letter.

TARGET ACCOUNT FOR THIS RUN: ${TARGET_ACCOUNT}
This reel will be posted to the @${TARGET_ACCOUNT} Instagram account. Every
recency/exclusion list in the request envelope below is already scoped to
this account, so you can treat them as authoritative. You MUST set
target_account='${TARGET_ACCOUNT}' on the media_posts row you write.

READ THE SKILL FIRST: ~/social-autoposter/mixer/SKILL.md is your complete
creative + technical procedure. It explains the two formats (Mixer for niche
product reels, TLH for AI-lesson hook reels), the caption arc, the ffmpeg
encode + dub commands, the data.ts variant schema, and the media_posts row
shape. Follow it exactly. When SKILL and this prompt disagree, SKILL wins.

REQUEST ENVELOPE FOR THIS RUN:
$(cat "$PICK_FILE")

UNPROVEN CLIP INJECTION (organic runs only):
$(cat "$UNPROVEN_JSON_FILE")
If "use": true above, you MUST include the clip at "path" as ONE of the 3-6
TLH slots in this render, encoded via the same pure-speedup ffmpeg recipe
SKILL Section 3 step 2 uses for any raw clip from '5. time lapse hooks/'.
After insert, set metadata.unproven_clip_basename = "<basename>" on the
media_posts row. The render-cycle uses this to retire the clip from future
rotation. See SKILL Section 3 "Unproven clip injection" for details.
If "use": false, ignore this block; render normally from the existing pool.

TYPE MAPPING (do NOT swap these):
- post_type='organic' -> TLH format. AI-themed lesson, NO product mention by
  name (no Fazm, no Mediar, no AppMaker, no mk0r, no studyly). 7-8s total.
  project_name MUST be NULL in the media_posts row (organic content is
  intentionally product-free; null is the correct attribution).
- post_type='product' -> Mixer format. The picker has already chosen which
  PROJECT this product reel promotes; see selected_project in the request
  envelope above (currently '${SELECTED_PROJECT}'). The variant you render
  MUST belong to that project (variant.project field in data.ts). Pick from
  the project's pool, oldest-rendered first. project_name MUST equal that
  project string in the media_posts row.

DELIVERABLES (must all exist when you exit):
1. ~/social-autoposter/mixer/remotion/out/post-${NNN}.mp4
   1080x1920, audio dubbed, ready to post. Filename uses post_number=${NEXT_NUM}.
2. ~/social-autoposter/mixer/remotion/out/post-${NNN}.caption.txt
   The Instagram caption story. UTF-8, plain text, no markdown.
   **CAPTION HARD LIMIT: ≤ 2150 chars total** (Instagram's cap is 2200; we leave
   50 chars of safety buffer for emoji / unicode). If your draft overshoots,
   tighten ruthlessly BEFORE writing the file. Verify with \`wc -c <file>\`.
   The harness enforces this programmatically: if the file is > 2150 on exit, it
   will spawn a focused tighten-only Claude call (up to 3 attempts); if all 3
   fail, the row is flipped to status='caption_too_long' and the render fails.
3. media_posts row with post_number=${NEXT_NUM}, status='draft',
   post_type='${TARGET}', target_account='${TARGET_ACCOUNT}', and all
   SKILL Section 5 columns populated (variant_id, video_path, caption_text,
   source_clips, overlays, audio_source, metadata.theme_angle, etc.).
   project_name='${SELECTED_PROJECT}' for product, NULL for organic.
   target_account is a NOT NULL column — you MUST set it to '${TARGET_ACCOUNT}'
   on this row. The post-cycle uses target_account to load the right token
   and route the reel to the right Instagram account.
   The caption_text column MUST match the caption.txt file exactly.

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
- local_audio_lru: the LOCAL audio pool (mixer/audio/*.m4a), ordered
  least-recently-used first. Pick the FIRST entry, the most stale local
  track. Reusing a track is fine; audio repeating across reels is normal on
  Instagram. NEVER download new audio, NEVER run yt-dlp, NEVER open a browser
  to Instagram for audio. The pool only grows when the user manually drops a
  track in. If the list is empty, FAIL the run; do not source from the network.
- used_theme_angles_14d: pick a different angle. SKILL Section 3 lists 5
  acceptable AI angles; pick one not in the exclusion list.
- used_variant_ids: for product runs, exclude these globally; even within
  the selected project, prefer a variant not in this list. For TLH (organic),
  pick a variant_id not in this list.

ENGAGEMENT STYLE ASSIGNMENT FOR THIS RUN: ${PICKED_STYLE} (mode=${PICK_MODE})

${STYLES_BLOCK}

You MUST stamp metadata.engagement_style='${PICKED_STYLE}' on the media_posts
row you write (in addition to caption_style, theme_angle, theme_label, etc.).
The dashboard A/B-tests on this label and the next picker round re-reads it as
performance signal, so it MUST match the assigned style verbatim. If mode=invent
(no style assigned above), invent a new ig_-prefixed snake_case style name that
describes the structural archetype of your caption (not the topic), and stamp
THAT on metadata.engagement_style. Do NOT use any non-ig_ prefixed style for
Instagram captions.

PRODUCT-PATH (post_type='product', project='${SELECTED_PROJECT}'):
The Mixer registry lives in mixer/remotion/src/mixer/data.ts. Every variant
declares a 'project' field. For this run you MUST pick a variant whose
variant.project === '${SELECTED_PROJECT}'. Variants are pre-registered in
Remotion via Root.tsx; you re-render an existing variant, write a fresh
caption targeted at the selected project, and log the row.

For mk0r: 4 niche variants (spa/autoshop/hotel/mk0r-retail). The reel shows the
mk0r workflow ("find a local business with no website -> go to mk0r.com ->
prompt it -> publish"). Each render MUST generate fresh TITLE-OVERLAY text that
fits the caption: before running npx remotion render, edit
mixer/remotion/src/mixer/data.ts and update MK0R_OVERLAY_TEXT["<picked-variant-id>"]
with three model-generated values:
  - headline: 2-3 short lines, with __ACCENT__ marking the accented line
    (e.g. "build an auto shop\n__ACCENT__\nin minutes")
  - accentText: 1-4 words shown in accent color (e.g. "a real website")
  - tagline: 3-7 word subtitle (e.g. "no agency, no template")
All three MUST describe the CAPABILITY (mk0r builds a real site fast, in one
prompt) and vary from the current defaults and across runs. Only edit the
picked variant's entry; leave the other 3 untouched.

CAPTION RULES (mk0r) -- HARD, do NOT violate:
The caption is a PRODUCT DEMO: a real local business that has no website, mk0r
builds it a real site from one prompt, and what that means for the owner. You
may reference mk0r.com plainly. You MUST NOT write any income/earnings framing:
no "\$X a month", no "they paid me", no "recurring revenue", no "signed N
clients", no "make money / side income / quit your job / flip websites", no
fabricated dollar amounts or client counts. That get-rich-quick framing tripped
a Meta fraud-and-deceptive-practices restriction on 2026-06-02 and is
permanently banned from mk0r captions. Keep it about what mk0r BUILDS, never
about money the viewer earns.

For studyly: 8 generated variants (studyly-i{1,2}-r{1,2,3,4}). Each render
MUST generate fresh overlay text that fits the caption story arc. Before
running npx remotion render, edit mixer/remotion/src/mixer/data.ts and update
STUDYLY_OVERLAY_TEXT["<picked-variant-id>"] with five model-generated values:
  - headline: 2-3 short lines, with __ACCENT__ marking the accented word/phrase
    on its own line (e.g. "stop wasting\n__ACCENT__\nbefore exams")
  - accentText: 1-4 words shown in accent color (e.g. "the night before")
  - tagline: 3-7 word subtitle under the headline (e.g. "the smarter study method")
  - stepOverlay: action text shown during the guide clip, ~40 chars max
    (e.g. "drop your notes into studyly.io")
  - finaleOverlay: payoff text shown during the result clip, ~40 chars max
    (e.g. "and actually remember it this time")
All five values MUST vary from the current defaults and from each other across
runs. Only edit the picked variant's entry; leave the other 7 untouched.
The caption arc is: a real study-method frustration (rereading / flashcards not
sticking, blanking when the wording changes) -> opens studyly.io -> the method
shift (it tests you on your OWN notes, rewording so you cant pattern-match) ->
the lesson. Reference studyly.io as the product. Do NOT fabricate specific
before/after exam scores or "failed -> passed / topped the class" miracle jumps
as a typical result; keep any outcome qualitative and personal (exaggerated-
results claims are a deceptive-practices signal, the same Meta rail that
restricted mk0r 2026-06-02).
Studyly variants are intentionally simpler/shorter (15-25s vs mk0r's 26-28s).

Pick the variant within the selected project that is least-recently-rendered
(check media_posts created_at WHERE project_name=selected_project AND variant_id IS NOT NULL).

ORGANIC-PATH (post_type='organic'):
Compose a new TLH variant. You may remix existing pre-encoded
remotion/public/mixer/tlh-*.mp4 slots (cheaper, faster) OR encode fresh raw
clips from the account's source folder if available (only if remixing
produces a stale recombination). The audio_source MUST be a LOCAL file from
local_audio_lru -- pick the least-recently-used (first) entry. NEVER source
audio from the network. The caption MUST follow SKILL Section 3 caption arc
(8 beats). Theme angle must be in SKILL Section 3 list and NOT in
used_theme_angles_14d.

PER-ACCOUNT TLH CONFIG (account_tlh_config in the envelope above):
- If account_tlh_config is non-empty, it REPLACES the SKILL Section 3 defaults
  for THIS account's organic renders. Specifically:
    * source_dir     -> raw clip folder (use this, NOT '5. time lapse hooks/')
    * variant_prefix -> variant_id prefix (e.g. 'omi-lesson-'); pick the next
                        free integer (omi-lesson-1, omi-lesson-2, ...).
                        Existing variants for this account are in
                        used_variant_ids; the new variant_id MUST start with
                        variant_prefix AND not collide with used_variant_ids.
    * unproven_dir   -> null means this account opts out of the unproven
                        rotation entirely (the harness already short-circuits
                        the injection step; treat 'use':false as authoritative).
    * caption_opener -> override the 'here is a story.' default if set.
    * story_brief    -> REPLACES SKILL Section 3's AI-defeat brief. The
                        8-beat structure still applies but the persona,
                        setup, forgetting moment, etc. come from the brief.
                        Voice + content come from THE BRIEF, not from
                        SKILL examples. SKILL examples are reference for the
                        default (matt_diak / matthewheartful) account.
- If account_tlh_config is empty {}, use SKILL Section 3 defaults as before
  (matt_diak / matthewheartful behavior is unchanged).
- Variant encoding still uses the pure-speedup recipe in SKILL Section 3
  step 2. Variant registration in data.ts is unchanged.

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
        --output-format stream-json --verbose \
        -p "$(cat "$PROMPT_FILE")" >>"$LOG_FILE" 2>&1; then
  rc=$?
  log "claude exited rc=$rc"
  rm -f "$PROMPT_FILE"
  if [ "$rc" -eq 79 ]; then
    log "claude blocked by quota stamp; will retry next cycle"
    SKIPPED_CT=1
    exit 0
  fi
  log "render failed"
  FAILED_CT=1
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
  FAILED_CT=1
  exit 1
fi
if [ ! -f "$OUT_CAP" ]; then
  log "ERROR: $OUT_CAP missing"
  FAILED_CT=1
  exit 1
fi

ROW_OK=$(/opt/homebrew/bin/python3.11 - "$NEXT_NUM" "$TARGET" 2>>"$LOG_FILE" <<'PY'
import os, sys
# HTTP-only (2026-06-01): row check via /api/v1/media-posts/by-number/<n>.
# No DATABASE_URL, no psycopg2. ok_on_404 lets us print MISSING ourselves.
sys.path.insert(0, os.path.expanduser('~/social-autoposter/scripts'))
from http_api import api_get
resp = api_get(f"/api/v1/media-posts/by-number/{int(sys.argv[1])}", ok_on_404=True)
row = None if resp.get('_not_found') else (resp.get('data') or {}).get('media_post')
if not row:
    print("MISSING")
elif row.get('post_type') != sys.argv[2]:
    print(f"BAD_TYPE got={row.get('post_type')} want={sys.argv[2]}")
elif row.get('status') != 'draft':
    print(f"BAD_STATUS got={row.get('status')} want=draft")
else:
    print(f"OK variant={row.get('variant_id')}")
PY
)

case "$ROW_OK" in
  OK*) log "DB row OK: $ROW_OK" ;;
  *)   log "ERROR: DB row check failed: $ROW_OK"; FAILED_CT=1; exit 1 ;;
esac

VARIANT=$(echo "$ROW_OK" | sed 's/^OK variant=//')

# Step 3.5: caption length gate (HARD LIMIT 2150 chars).
# IG's actual limit is 2200; we keep a 50-char safety buffer for emoji/unicode.
# If over: spawn focused tighten-only claude calls (max 3 attempts).
# If still over after 3 attempts: flip row to status='caption_too_long' and
# exit non-zero. The picker filters by status='draft' so the bad row is
# automatically skipped; the next render fire creates a fresh draft.
# Never auto-truncate -- silent author-voice loss is worse than a failed render.
CAP_LIMIT=2150
CAP_LEN=$(wc -c < "$OUT_CAP" | tr -d ' ')
log "step 3.5: caption length check len=${CAP_LEN} limit=${CAP_LIMIT}"

if [ "$CAP_LEN" -gt "$CAP_LIMIT" ]; then
  log "caption over limit (${CAP_LEN} > ${CAP_LIMIT}); spawning tighten loop (max 3 attempts)"
  attempt=0
  while [ "$attempt" -lt 3 ] && [ "$CAP_LEN" -gt "$CAP_LIMIT" ]; do
    attempt=$((attempt + 1))
    log "  tighten attempt ${attempt}/3 (current len=${CAP_LEN})"

    TIGHTEN_PROMPT=$(mktemp /tmp/ig_tighten_prompt_XXXXXX.txt)
    NEW_CAP_OUT=$(mktemp /tmp/ig_tighten_out_XXXXXX.txt)

    cat > "$TIGHTEN_PROMPT" <<TIGHTEN_EOF
You are the caption-tightening agent for an Instagram reel.

Your single job: rewrite the caption below so total length is <= ${CAP_LIMIT}
characters, while preserving the voice and ALL 8 beats of the caption arc
(opener "here is a story.", age+setup, wrong-about-AI moment, breaking event,
felt sense, workflow change, contrarian lesson in one sharp line, closing
instruction). For Mixer/product captions, preserve the product-demo structure
and the plain product reference (mk0r.com / studyly.io) if present. NEVER add
income/earnings framing ("\$X a month", "they paid me", "recurring revenue",
"signed N clients") to an mk0r caption while tightening -- that framing is
banned (Meta fraud restriction 2026-06-02).

RULES (hard):
- Total length MUST be <= ${CAP_LIMIT} chars. Count and verify before responding.
- Keep ALL beats. Do NOT drop a beat to fit.
- Preserve the voice: lowercase, plain, no markdown. Keep existing emoji.
- Cut adjectives, collapse compound sentences, drop redundant examples,
  prefer "i was tired" over "i was tired in a way that didn't show up in a paycheck".
- Output ONLY the rewritten caption. No prose around it. No 'here is the
  rewritten caption' preamble. No backticks. No commentary. Just the caption text.

CURRENT CAPTION (length=${CAP_LEN}, must shrink to <= ${CAP_LIMIT}):
---BEGIN---
$(cat "$OUT_CAP")
---END---

Output the tightened caption now. Just the text body. Nothing else.
TIGHTEN_EOF

    if "$REPO_DIR/scripts/run_claude.sh" "run-instagram-render-tighten" \
            ${CLAUDE_MODEL:+--model "$CLAUDE_MODEL"} \
            --permission-mode bypassPermissions \
            -p "$(cat "$TIGHTEN_PROMPT")" > "$NEW_CAP_OUT" 2>>"$LOG_FILE"; then
      # run_claude.sh prepends a JSON session-log line; strip any leading
      # JSON-looking line before treating the rest as the new caption.
      # The actual caption is everything except trailing JSON metadata.
      /opt/homebrew/bin/python3.11 - "$NEW_CAP_OUT" "$OUT_CAP" <<'PY'
import sys, json, re
raw = open(sys.argv[1]).read()
# claude -p output: caption text, then possibly a trailing JSON line from
# log_claude_session.py. Strip any line that parses as a single JSON object
# containing 'session_id' or 'logged' keys (the session marker).
lines = raw.splitlines(keepends=True)
out = []
for ln in lines:
    stripped = ln.strip()
    if stripped.startswith('{') and stripped.endswith('}'):
        try:
            j = json.loads(stripped)
            if isinstance(j, dict) and ('session_id' in j or 'logged' in j):
                continue  # drop the session marker line
        except Exception:
            pass
    out.append(ln)
text = ''.join(out).strip() + '\n'
open(sys.argv[2], 'w').write(text)
PY
      CAP_LEN=$(wc -c < "$OUT_CAP" | tr -d ' ')
      log "  attempt ${attempt} result: new len=${CAP_LEN}"
    else
      log "  attempt ${attempt} failed: claude exited non-zero"
    fi
    rm -f "$TIGHTEN_PROMPT" "$NEW_CAP_OUT"
  done

  if [ "$CAP_LEN" -gt "$CAP_LIMIT" ]; then
    log "ERROR: caption still over limit after 3 tighten attempts (final len=${CAP_LEN})"
    log "flipping media_posts row to status='caption_too_long' so picker skips it"
    FAILED_CT=1
    /opt/homebrew/bin/python3.11 - "$NEXT_NUM" "$CAP_LEN" 2>>"$LOG_FILE" <<'PY'
import os, sys
# HTTP-only (2026-06-01): status flip via PATCH /api/v1/media-posts/by-number.
sys.path.insert(0, os.path.expanduser('~/social-autoposter/scripts'))
from http_api import api_patch
api_patch(
    f"/api/v1/media-posts/by-number/{int(sys.argv[1])}",
    {"action": "caption_too_long", "caption_len": int(sys.argv[2])},
)
print(f"flipped post-{int(sys.argv[1]):03d} to caption_too_long (len={sys.argv[2]})")
PY
    exit 1
  fi

  # Sync tightened caption into DB row.
  log "tighten loop succeeded; syncing caption_text column"
  /opt/homebrew/bin/python3.11 - "$NEXT_NUM" "$OUT_CAP" 2>>"$LOG_FILE" <<'PY'
import os, sys
# HTTP-only (2026-06-01): caption sync via PATCH /api/v1/media-posts/by-number.
sys.path.insert(0, os.path.expanduser('~/social-autoposter/scripts'))
from http_api import api_patch
cap = open(sys.argv[2]).read()
api_patch(
    f"/api/v1/media-posts/by-number/{int(sys.argv[1])}",
    {"action": "sync_caption", "caption_text": cap},
)
print(f"synced post-{int(sys.argv[1]):03d} caption_text (len={len(cap)})")
PY
  log "caption tightened OK (final len=${CAP_LEN})"
fi

log "=== rendered post-${NNN} (${TARGET}, variant=${VARIANT}) successfully ==="
POSTED_CT=1
exit 0
