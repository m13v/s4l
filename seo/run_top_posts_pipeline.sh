#!/bin/bash
#
# Per-project top-post SEO generation pipeline.
#
# Strategy (different from run_top_pages_pipeline.sh):
# Top-PAGES picks one global winner from PostHog destination traffic and
# fans out to every enabled project as adjacent keywords. Top-POSTS picks
# the highest-engagement SOURCE post per project from the social-autoposter
# `posts` table, identifies the underlying news/event the post references,
# and ships:
#   1. A dedicated /t/ page covering the news with the wedge from the post
#   2. A NewsStrip on the project's homepage routing existing organic
#      clickthroughs ("/" pageviews from t.co) to the new /t/ page.
#
# This closes the gap exposed on 2026-05-07: the May 6 Anthropic doubling
# announcement was tweeted 9x with a combined ~70k Twitter views, all
# linking to claude-meter.com/ (homepage). The Twitter A/B gate
# (TWITTER_PAGE_GEN_RATE=0.25 in twitter_gen_links.py) had rolled
# 'plain_url_ab_skip' for the lead tweet, so no /t/ page was generated at
# post-time. Nothing watched whether the post then went viral and warranted
# a retroactive page. This pipeline is that watcher.
#
# Eligibility (per pick_top_posts.py):
#   - posts.project_name = <product>
#   - posts.posted_at >= NOW() - 14 days
#   - posts.views >= 10000
#   - posts.link_source IS NULL or LIKE 'plain_url_%'  (no /t/ page yet)
#   - status NOT in ('deleted', 'removed')
#   - NOT a recommendation post (those re-amplify existing pages)
#   - (product, post_id) NOT in top_post_winners  (one /t/ per viral post)
#
# Per invocation, per project:
#   1. pick_top_posts.py       -> winner post + sibling tweets brief
#   2. claude opus (sonnet for prod) reads the brief AND the full project
#      config, fetches the underlying news source if there is one (via
#      WebFetch on URLs cited in the post), and writes:
#        a. src/app/.../t/<slug>/page.tsx covering the news + wedge
#        b. mounts <NewsStrip> on the homepage pointing at /t/<slug>
#   3. Insert into seo_keywords (source='top_post', status='in_progress')
#   4. generate_page.py is NOT used here — Claude writes the page directly,
#      using the same schema/JSON-LD/components stack used everywhere else
#   5. Verify the page resolves to 200 on the live URL
#   6. Insert into top_post_winners
#
# Pages produced here surface in the dashboard Activity tab as
# 'page_published_top_post'.
#
# Usage:
#   ./seo/run_top_posts_pipeline.sh                 # all enabled projects
#   ./seo/run_top_posts_pipeline.sh claude-meter    # one project
#   ./seo/run_top_posts_pipeline.sh --list-enabled

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
CONFIG="$ROOT_DIR/config.json"
PICK="python3 $SCRIPT_DIR/pick_top_posts.py"
DB="python3 $SCRIPT_DIR/db_helpers.py"

RUN_START=$(date +%s)

source "$SCRIPT_DIR/claude_helpers.sh"

LOG_ROOT="$SCRIPT_DIR/logs"
LOCK_ROOT="$SCRIPT_DIR/.locks/top_posts"
mkdir -p "$LOG_ROOT" "$LOCK_ROOT"

# Load .env for DATABASE_URL etc.
[ -f "$ROOT_DIR/.env" ] && set -a && source "$ROOT_DIR/.env" && set +a

if [ -z "${POSTHOG_PERSONAL_API_KEY:-}" ]; then
    POSTHOG_PERSONAL_API_KEY=$(security find-generic-password -s "PostHog-Personal-API-Key-m13v" -w 2>/dev/null || true)
    export POSTHOG_PERSONAL_API_KEY
fi

_timestamp() { date -u +%Y-%m-%d_%H%M%S; }

# List-enabled preview.
if [ "${1:-}" = "--list-enabled" ]; then
    $PICK --list-enabled
    exit 0
fi

# Global lock.
GLOBAL_LOCK="$LOCK_ROOT/_global.lock"
if [ -f "$GLOBAL_LOCK" ]; then
    AGE=$(( $(date +%s) - $(stat -f %m "$GLOBAL_LOCK" 2>/dev/null || stat -c %Y "$GLOBAL_LOCK" 2>/dev/null) ))
    if [ "$AGE" -lt 3600 ]; then
        echo "=== top-posts pipeline: global lock held (age ${AGE}s), skip"
        exit 0
    fi
    rm -f "$GLOBAL_LOCK"
fi
echo "$$" > "$GLOBAL_LOCK"
# SKIP_TARGETS / OK_TARGETS / FAIL_TARGETS get populated as the loop runs.
# Initialize them here so the EXIT trap can reference them safely even if
# we exit before the loop (e.g. an early `set -u` violation).
OK_TARGETS=()
SKIP_TARGETS=()
FAIL_TARGETS=()
trap '__e=$?; rm -f "$GLOBAL_LOCK"; python3 "$SCRIPT_DIR/log_seo_run.py" --script "seo_top_posts" --since "$RUN_START" --failed "$__e" --elapsed "$(( $(date +%s) - RUN_START ))" --skipped-override "${#SKIP_TARGETS[@]}" --posted-override "${#OK_TARGETS[@]}" >/dev/null 2>&1 || true' EXIT

TS=$(_timestamp)
LOG_DIR="$LOG_ROOT/_global/top_posts"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/${TS}.log"

echo "=== Top-Posts pipeline (per-project): $TS ===" | tee -a "$LOG_FILE"

# Enumerate target products.
if [ -n "${1:-}" ]; then
    PRODUCTS="$1"
else
    PRODUCTS=$($PICK --list-enabled)
fi

# Quota markers (same set as run_top_pages_pipeline.sh).
QUOTA_MARKERS='monthly usage limit|429 Too Many|insufficient_quota|"api_error_status":429|hit your limit|"status":"rejected"'
QUOTA_HIT=0

# OK_TARGETS / SKIP_TARGETS / FAIL_TARGETS are initialized earlier (right
# after the global lock) so the EXIT trap can read their counts safely even
# on an early-exit path. Don't re-declare here.

while read -r TARGET_PRODUCT; do
    [ -z "$TARGET_PRODUCT" ] && continue

    if [ "$QUOTA_HIT" = "1" ]; then
        echo "=== $TARGET_PRODUCT: skip — quota hit earlier this tick ===" | tee -a "$LOG_FILE"
        continue
    fi

    LOWER=$(echo "$TARGET_PRODUCT" | tr '[:upper:]' '[:lower:]')
    PER_LOCK="$LOCK_ROOT/${LOWER}.lock"
    PER_LOG_DIR="$LOG_ROOT/${LOWER}/top_posts"
    mkdir -p "$PER_LOG_DIR"
    PER_LOG="$PER_LOG_DIR/${TS}.log"

    if [ -f "$PER_LOCK" ]; then
        AGE=$(( $(date +%s) - $(stat -f %m "$PER_LOCK" 2>/dev/null || stat -c %Y "$PER_LOCK" 2>/dev/null) ))
        if [ "$AGE" -lt 1800 ]; then
            echo "=== $TARGET_PRODUCT: per-project lock held (${AGE}s), skip" | tee -a "$LOG_FILE" "$PER_LOG"
            continue
        fi
        rm -f "$PER_LOCK"
    fi
    echo "$$" > "$PER_LOCK"

    {
        echo "=== Top-Posts target: $TARGET_PRODUCT (ts=$TS) ==="

        BRIEF_FILE="$PER_LOG_DIR/${TS}.brief.json"
        # NOTE: do NOT use `if ! cmd; then RC=$?` here — bash's `!` resets $?
        # to 0 in the then-branch, hiding the picker's real exit code (2 for
        # "skip"). That bug caused every dry-night fire to report 15 fake
        # failures (and a launchd exit 1) on 2026-05-08 11:41 PT. Capture
        # $? on the line immediately after the call instead.
        $PICK --product "$TARGET_PRODUCT" --days 14 --min-views 10000 --out "$BRIEF_FILE" 2>>"$PER_LOG"
        RC=$?
        if [ "$RC" -ne 0 ]; then
            if [ "$RC" -eq 2 ]; then
                echo "  no eligible viral post in last 14d for $TARGET_PRODUCT, skip"
                exit 13   # treated as "skip ok" by case below
            fi
            echo "  picker failed (rc=$RC); see $PER_LOG"
            exit 11
        fi

        # Echo the picked winner.
        python3 - "$BRIEF_FILE" <<'PY'
import json, sys
b = json.load(open(sys.argv[1]))
w = b["winner"]
print(f"  WINNER: post_id={w['id']} platform={w['platform']} views={w['views']} score={w['score']}")
print(f"    url:    {w['our_url']}")
print(f"    posted: {w['posted_at']}")
print(f"    source: {w.get('link_source')}")
print(f"    siblings: {len(b['siblings'])}")
PY

        # Build the Claude agent prompt. We embed the user's *intent verbatim*
        # (the prompts that originally produced the working pattern on
        # claude-meter.com on 2026-05-07) so the agent ships a consistent
        # quality of page and homepage placement across every product.
        PROMPT_FILE="$PER_LOG_DIR/${TS}.prompt.md"
        python3 - "$BRIEF_FILE" > "$PROMPT_FILE" <<'PY'
import json, sys
from datetime import datetime

brief = json.load(open(sys.argv[1]))
proj = brief["project_config"]
w = brief["winner"]
sibs = brief["siblings"]
now_human = datetime.utcnow().strftime("%B %d, %Y")
now_year = datetime.utcnow().strftime("%Y")
now_month = datetime.utcnow().strftime("%B").lower()

# The prompts the user actually used to produce the working
# claude-meter.com /t/claude-rate-limits-doubled-weekly-cap-unchanged
# page + homepage NewsStrip on 2026-05-07. Verbatim where possible,
# lightly normalized so the model treats them as task instructions.
USER_INTENT = '''The original ask, in the user's own words, that this pipeline reproduces:

> For the tweet that got a lot of [traction], did we create a custom page
> for it? If not, should we? What is the underlying news / Anthropic post
> about? What is the news?

> We need to create the page first. Reflect the wedge that we got. Since
> the existing links already point to the homepage, we need to have a
> section on the homepage, somewhere towards the top, that prompts the
> users to visit this dedicated page. Make sure that we have comprehensive
> coverage of the story with the wedge, and that will bring further
> attention. Kindly expand on the Twitter story.

Translate that intent into a working PR for the TARGET PROJECT below.'''

# Sibling thread, formatted for the model.
sib_block = ""
for s in sibs:
    sib_block += (
        f"  - [{s['platform']}] views={s['views']} up={s['upvotes']} cm={s['comments']}\n"
        f"    url: {s['our_url']}\n"
        f"    text: {(s.get('our_content') or '')[:280]}\n\n"
    )

prompt = f"""You are running an ENGINEERING TASK inside the {brief['product']} website repo at:
  {brief['repo_path']}

TODAY IS {now_human}.

{USER_INTENT}

============================================================================
WINNER POST (the viral source, currently links to homepage with no /t/ page)
============================================================================
  product:     {brief['product']}
  domain:      {brief['domain']}
  platform:    {w['platform']}
  url:         {w['our_url']}
  views:       {w['views']}
  upvotes:     {w['upvotes']}
  comments:    {w['comments']}
  posted_at:   {w['posted_at']}
  link_source: {w.get('link_source')}
  content:
    {(w.get('our_content') or '')[:1200]}

============================================================================
SIBLING POSTS — same project, same 48h window, used to enrich the brief
(these are the follow-up tweets/posts that piled on the same news cycle)
============================================================================
{sib_block}
============================================================================
TARGET PROJECT CONFIG (from config.json projects[])
============================================================================
{json.dumps({k: v for k, v in proj.items() if k not in ('voice','content_guardrails')}, indent=2, ensure_ascii=False)}

VOICE GUIDELINES:
{json.dumps(proj.get('voice', {}), indent=2, ensure_ascii=False)}

CONTENT GUARDRAILS:
{json.dumps(proj.get('content_guardrails', {}), indent=2, ensure_ascii=False)}

============================================================================
YOUR JOB (3 deliverables, in this order)
============================================================================

(1) IDENTIFY THE UNDERLYING NEWS.
    The viral post is reacting to something. It might be an Anthropic
    blog post, an OpenAI release, a competitor pricing change, a HN
    thread, a Twitter announcement from a CEO. Use WebFetch on the URLs
    cited in the winner post and any URLs in the sibling thread to find
    the source-of-truth announcement. If the post reacts to a *behavior
    change* with no formal post (e.g. "rate limits silently tightened"),
    treat the cluster of sibling posts as the primary source and call
    that out explicitly in the article.

    Quote exact numbers from the source. No invented benchmarks.

(2) WRITE THE DEDICATED /t/ PAGE.
    Path: src/app/(main)/t/<slug>/page.tsx (or whatever the product's
    /t route convention is — check existing pages first with `ls`).

    Slug rules:
      - kebab-case, ASCII, <= 64 chars
      - if the news has a date hook, include the month+year only when
        it's the angle ("...-may-2026")
      - target the user search for the news, not the brand

    Required structure (model on
    src/app/(main)/t/claude-rate-limits-doubled-weekly-cap-unchanged/page.tsx
    on the claude-meter site as reference — read it first if you need
    a working example):

      - "BREAKING" pill at the top of the article header
      - <GradientText> H1 that names the news AND the wedge
      - 2-3 sentence dek that maps the news to the project's wedge
      - <ArticleMeta>, <ProofBand> with sources
      - <AnimatedChecklist title="What changed, what stayed the same">
      - "The wedge in one sentence" section, then expanded
      - <BeforeAfter> showing the narrative shift
      - <AnimatedCodeBlock> (if there is a JSON / code / config angle)
      - <SequenceDiagram> or <AnimatedBeam> for the mechanism
      - <TerminalOutput> or repro instructions if applicable
      - SECTION QUOTING THE TWITTER THREAD (this is critical — you have
        the winner + sibling URLs; render them as a vertical stack of
        cards with the verbatim text + view counts. The user explicitly
        asked us to "expand on the Twitter story". This section is what
        they meant.)
      - <StepTimeline> for "what to do this week"
      - <ComparisonTable> for "before-news vs after-news"
      - <BackgroundGrid><MetricsRow> with EXACT numbers from the
        announcement (no invented metrics)
      - <Marquee> with myth pills the article corrects
      - <GlowCard> with a synthesized takeaway
      - "Plan-by-plan / impact" section (Pro / Max / API / etc.)
      - "Honest caveats" section
      - <ShimmerButton> CTA to /install (or {proj.get('get_started_link', '/install')})
      - <FaqSection> with at least 8 Q&A pairs
      - <RelatedPostsGrid> with 3 related /t/ pages already on the site
      - <BookCallCTA> footer + sticky pointing at {proj.get('booking_link', '')}
      - JSON-LD: articleSchema, breadcrumbListSchema, faqPageSchema in a
        single <script type="application/ld+json"> tag

    Voice: stay in the project's voice (see VOICE block above). Use the
    "examples" in the voice config as tonal reference. Avoid every word
    in the "never" list. Length: 600-1000 lines of TSX, prose-heavy.

    Match the import surface the existing /t/ pages use:
        import {{
          Breadcrumbs, ArticleMeta, ProofBand, FaqSection,
          AnimatedCodeBlock, TerminalOutput, SequenceDiagram,
          ComparisonTable, BeforeAfter, AnimatedChecklist, MetricsRow,
          GlowCard, StepTimeline, AnimatedBeam, NumberTicker,
          ShimmerButton, GradientText, BackgroundGrid, Marquee,
          RelatedPostsGrid, BookCallCTA,
          articleSchema, breadcrumbListSchema, faqPageSchema,
        }} from "@m13v/seo-components";

(3) MOUNT <NewsStrip> ON THE HOMEPAGE.
    The viral post links to "/" — every existing tweet retweet keeps
    delivering visitors to the bare homepage. Add or update a <NewsStrip>
    near the very top of the homepage that prompts users to visit the
    new /t/ page. Component is in @m13v/seo-components v0.35.0+.

    Find HomeClient.tsx (usually src/app/(main)/HomeClient.tsx or
    src/app/(main)/page.tsx). Insert <NewsStrip> as the FIRST child of
    the home root container, BEFORE the hero header:

        <NewsStrip
          href="/t/<your-slug>"
          pillText="<short date or BREAKING>"
          lead="<the news in one short sentence>"
          wedge="<your wedge, why the user should click>"
          ctaLabel="Read the breakdown"
          tone="amber"          // amber for breaking news, teal for softer launches
          site="{brief['product']}"
          section="homepage-news-strip"
          datePublished="<ISO date of the news>"
        />

    If a <NewsStrip> already exists for an older story, REPLACE it with
    the new one — only one strip on the homepage at a time.

    Add the import: import {{ NewsStrip }} from "@seo/components"; (or
    whatever alias the consumer site uses for @m13v/seo-components).

============================================================================
FACTUAL ANCHORS (these MUST appear in the article verbatim)
============================================================================
- The exact URL of the source announcement (after WebFetch identifies it)
- The exact numeric values from the announcement (rate limits, percentages,
  dollars, dates)
- The winner post URL ({w['our_url']})
- At least 3 sibling post URLs as inline citations

============================================================================
SHIPPING
============================================================================
- Verify the new page builds locally (`npm run build` in the repo, or
  next typecheck).
- Stage and commit both changes (the /t/ page and the HomeClient diff)
  in the same commit. Commit message format:
    feat(seo): top-post page <slug> for viral <platform> post (<views> views)
- Push. The auto-commit + auto-deploy agent on this machine will catch
  it within a minute even if you forget; explicit push is faster.
- Print the new /t/ URL on stdout when done so the orchestrator can
  verify it returns 200.

============================================================================
COMMIT BUDGET
============================================================================
- One /t/ page file (~600-1000 lines TSX)
- One HomeClient diff (~10 lines)
- One commit. Don't write supplementary README/markdown files.

WHEN DONE: print exactly one line to stdout:
TOP_POST_DONE slug=<slug> url=<full https URL of the new page>

If you get blocked (build fail, missing component, content guardrail
collision), print:
TOP_POST_BLOCKED reason=<short>
"""
sys.stdout.write(prompt)
PY

        echo "  prompt: $PROMPT_FILE ($(wc -l < "$PROMPT_FILE") lines)"

        # Insert pending row in seo_keywords so the dashboard surfaces
        # in-progress work. We don't know the slug yet — we'll patch it
        # post-Claude. Use the post URL as the unique key so the same
        # viral post can't double-insert.
        WINNER_POST_ID=$(python3 -c "import json; b=json.load(open('$BRIEF_FILE')); print(b['winner']['id'])")
        WINNER_POST_URL=$(python3 -c "import json; b=json.load(open('$BRIEF_FILE')); print(b['winner']['our_url'])")
        WINNER_VIEWS=$(python3 -c "import json; b=json.load(open('$BRIEF_FILE')); print(b['winner']['views'])")
        PROVISIONAL_KEYWORD="top_post:$WINNER_POST_ID"
        PROVISIONAL_SLUG="pending-$WINNER_POST_ID"
        SEO_SCRIPT_DIR="$SCRIPT_DIR" python3 - <<PY
import os, sys, psycopg2
sys.path.insert(0, os.environ['SEO_SCRIPT_DIR'])
import db_helpers
conn = db_helpers.get_conn()
cur = conn.cursor()
cur.execute(
    """
    INSERT INTO seo_keywords (product, keyword, slug, source, status, score)
    VALUES (%s, %s, %s, 'top_post', 'in_progress', 2.0)
    ON CONFLICT (product, keyword) DO UPDATE SET
      slug = EXCLUDED.slug,
      source = 'top_post',
      status = 'in_progress',
      score = GREATEST(seo_keywords.score, 2.0),
      updated_at = NOW()
    """,
    ("$TARGET_PRODUCT", "$PROVISIONAL_KEYWORD", "$PROVISIONAL_SLUG"),
)
conn.commit()
cur.close(); conn.close()
print(f"  seo_keywords row staked (provisional slug=$PROVISIONAL_SLUG)")
PY

        # Run Claude as agent (NOT --print). Inherit the global default model
        # from ~/.claude/settings.json (currently "claude-opus-4-7") so the
        # whole social-autoposter stack stays on the same opus build without
        # each pipeline pinning its own version string. CLAUDE_MODEL env wins
        # for one-off overrides. The agent reads the repo, WebFetches sources,
        # writes files, runs npm/typecheck.
        #
        # The conditional `${CLAUDE_MODEL:+--model "$CLAUDE_MODEL"}` expands
        # to nothing when unset (CLI uses settings.json) and to `--model X`
        # when set. Sidesteps the bash 3.2 set -u empty-array bug.
        STREAM_FILE="$PER_LOG_DIR/${TS}_stream.jsonl"
        OUTPUT_FILE="$PER_LOG_DIR/${TS}.output.txt"

        REPO_PATH=$(python3 -c "import json; b=json.load(open('$BRIEF_FILE')); print(b['repo_path'])")
        echo "  invoking Claude (model=${CLAUDE_MODEL:-settings.json default}, agent mode, in $REPO_PATH)..."

        cd "$REPO_PATH" || { echo "  cd failed: $REPO_PATH"; exit 14; }

        # Stream-json so we can grep for quota markers afterward.
        if ! claude_with_retry ${CLAUDE_MODEL:+--model "$CLAUDE_MODEL"} \
                --output-format stream-json \
                --verbose \
                --permission-mode acceptEdits \
                --print \
                < "$PROMPT_FILE" \
                > "$STREAM_FILE" 2>>"$PER_LOG"; then
            echo "  claude exited non-zero (after retries)"
            if grep -qiE "$QUOTA_MARKERS" "$STREAM_FILE" "$PER_LOG" 2>/dev/null; then
                echo "  !! quota hit — halting tick"
                QUOTA_HIT=1
            fi
            exit 15
        fi

        # Extract the final TOP_POST_DONE / TOP_POST_BLOCKED line.
        python3 - "$STREAM_FILE" "$OUTPUT_FILE" <<'PY'
import json, sys
out = []
for line in open(sys.argv[1]):
    line = line.strip()
    if not line:
        continue
    try:
        ev = json.loads(line)
    except Exception:
        continue
    if ev.get("type") == "result":
        out.append(ev.get("result", "") or "")
    elif ev.get("type") == "assistant":
        msg = ev.get("message") or {}
        for block in (msg.get("content") or []):
            if block.get("type") == "text":
                out.append(block.get("text", "") or "")
sys.stdout = open(sys.argv[2], "w")
sys.stdout.write("\n".join(out))
PY

        STATUS_LINE=$(grep -E '^(TOP_POST_DONE|TOP_POST_BLOCKED)' "$OUTPUT_FILE" | tail -1 || true)
        if [[ "$STATUS_LINE" == TOP_POST_BLOCKED* ]]; then
            echo "  blocked: $STATUS_LINE"
            exit 16
        fi
        if [[ -z "$STATUS_LINE" ]]; then
            echo "  no TOP_POST_DONE line emitted; treating as failure"
            exit 17
        fi

        SLUG=$(echo "$STATUS_LINE" | sed -E 's/.*slug=([^ ]+).*/\1/')
        PAGE_URL=$(echo "$STATUS_LINE" | sed -E 's/.*url=([^ ]+).*/\1/')
        echo "  shipped: slug=$SLUG url=$PAGE_URL"

        # Update seo_keywords row with the real slug + completed_at.
        SEO_SCRIPT_DIR="$SCRIPT_DIR" python3 - <<PY
import os, sys, psycopg2
sys.path.insert(0, os.environ['SEO_SCRIPT_DIR'])
import db_helpers
conn = db_helpers.get_conn()
cur = conn.cursor()
cur.execute(
    """
    UPDATE seo_keywords
       SET slug = %s,
           page_url = %s,
           status = 'done',
           completed_at = NOW(),
           updated_at = NOW()
     WHERE product = %s AND keyword = %s
    """,
    ("$SLUG", "$PAGE_URL", "$TARGET_PRODUCT", "$PROVISIONAL_KEYWORD"),
)
conn.commit()
cur.close(); conn.close()
print(f"  seo_keywords row marked done")
PY

        # Verify the page is live (Cloud Run / Vercel deploy should have
        # picked up the auto-commit by the time we check).
        sleep 5
        HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$PAGE_URL" || echo "000")
        echo "  live URL probe: $PAGE_URL -> HTTP $HTTP_CODE"

        # Insert into top_post_winners for cooldown.
        SEO_SCRIPT_DIR="$SCRIPT_DIR" python3 - <<PY
import os, sys, json, psycopg2
sys.path.insert(0, os.environ['SEO_SCRIPT_DIR'])
import db_helpers
brief = json.load(open("$BRIEF_FILE"))
w = brief["winner"]
conn = db_helpers.get_conn()
cur = conn.cursor()
cur.execute(
    """
    INSERT INTO top_post_winners
      (product, post_id, platform, post_url, views, upvotes, comments,
       score, target_slug, target_url, keyword, metrics)
    VALUES
      (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
    ON CONFLICT (product, post_id) DO UPDATE SET
      target_slug = EXCLUDED.target_slug,
      target_url  = EXCLUDED.target_url,
      keyword     = EXCLUDED.keyword,
      score       = EXCLUDED.score,
      won_at      = NOW()
    """,
    (
        "$TARGET_PRODUCT",
        w["id"],
        w["platform"],
        w["our_url"],
        w["views"],
        w["upvotes"],
        w["comments"],
        w["score"],
        "$SLUG",
        "$PAGE_URL",
        "$PROVISIONAL_KEYWORD",
        json.dumps({"siblings": [{"id": s["id"], "url": s["our_url"], "views": s["views"]} for s in brief["siblings"]]}),
    ),
)
conn.commit()
cur.close(); conn.close()
print(f"  top_post_winners row recorded")
PY

        exit 0
    } 2>&1 | tee -a "$PER_LOG" "$LOG_FILE"

    TARGET_RC=${PIPESTATUS[0]}
    rm -f "$PER_LOCK"

    case "$TARGET_RC" in
        0)
            echo "=== $TARGET_PRODUCT ok ===" | tee -a "$LOG_FILE"
            OK_TARGETS+=("$TARGET_PRODUCT")
            ;;
        13)
            echo "=== $TARGET_PRODUCT skipped — no eligible viral post ===" | tee -a "$LOG_FILE"
            SKIP_TARGETS+=("$TARGET_PRODUCT")
            ;;
        *)
            echo "=== $TARGET_PRODUCT failed (rc=$TARGET_RC) ===" | tee -a "$LOG_FILE"
            FAIL_TARGETS+=("$TARGET_PRODUCT")
            if grep -qiE "$QUOTA_MARKERS" "$PER_LOG" 2>/dev/null; then
                echo "  !! quota detected — halting subsequent targets" | tee -a "$LOG_FILE"
                QUOTA_HIT=1
            fi
            ;;
    esac
done <<< "$PRODUCTS"

{
    echo
    echo "=== Top-Posts pipeline summary ==="
    echo "  ok:      ${#OK_TARGETS[@]} ${OK_TARGETS[*]:-}"
    echo "  skip:    ${#SKIP_TARGETS[@]} ${SKIP_TARGETS[*]:-}"
    echo "  failed:  ${#FAIL_TARGETS[@]} ${FAIL_TARGETS[*]:-}"
    echo "  elapsed: $(( $(date +%s) - RUN_START ))s"
} | tee -a "$LOG_FILE"

if [ "${#FAIL_TARGETS[@]}" -gt 0 ]; then
    exit 1
fi
exit 0
