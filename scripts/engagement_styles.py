#!/usr/bin/env python3
"""Shared engagement style definitions for all platforms.

Centralizes style taxonomy, platform-specific guidance, content rules,
and prompt generation so every pipeline (post_reddit, engage_reddit,
run-twitter-cycle, run-linkedin, engage-twitter, engage-linkedin) references
a single source of truth.

Usage:
    from engagement_styles import VALID_STYLES, REPLY_STYLES, get_styles_prompt, get_content_rules, get_anti_patterns

Style universe:
    The hardcoded STYLES dict is the curated, "active" baseline. The model
    may also INVENT new styles inline at decision time by emitting a
    `new_style` block alongside an unknown `engagement_style` in its JSON.
    Those land in scripts/engagement_styles_extra.json with status="candidate"
    and merge back into the live universe via _load_extra_styles(). A nightly
    promoter (scripts/promote_engagement_styles.py) graduates candidates to
    "active" once they prove out. Until then candidates appear in prompts
    but only receive STYLE_FLOOR_PCT weight in the picker, so a single weird
    invention can't dominate.
"""

import json
import os
import random
import sys as _sys_mod
from datetime import datetime, timezone

# ── Style taxonomy ──────────────────────────────────────────────────

STYLES = {
    "critic": {
        "description": "Point out what's missing, flawed, or naive. Reframe the problem.",
        "example": "The part that breaks down is...",
        "best_in": {
            "reddit": ["r/Entrepreneur", "r/smallbusiness", "r/startups"],
            "twitter": ["tech", "startup", "business"],
            "linkedin": ["strategy", "leadership", "operations"],
        },
        "note": "NEVER just nitpick; offer a non-obvious insight.",
    },
    "storyteller": {
        "description": (
            "Narrative-driven comment. Per the GROUNDING RULE, every "
            "storyteller comment picks ONE of two mutually exclusive lanes: "
            "Lane 1 (DISCLOSED STORY) opens with a hedge like "
            "'hypothetically', 'imagine someone running this', 'scenario:', "
            "'say a friend tried' and is then free to invent any specifics; "
            "Lane 2 (NO FABRICATION) stays first-person only when every "
            "specific (numbers, durations, places, course names, brands, "
            "headcount) appears verbatim in the matched project's "
            "content_angle / voice / messaging in config.json, otherwise "
            "drops the specifics or pattern-frames "
            "('the part that breaks down is...', 'the typical failure mode "
            "is...'). Lead with failure or surprise, not success."
        ),
        "example": (
            "LANE 1 (disclosed): 'hypothetically, imagine running this for "
            "a couple of lecture blocks: cheap recorder into whisper into "
            "gpt into anki. raw prompts get you somewhere around a third "
            "usable cards before duplicate distractors take over.' "
            "LANE 2 grounded: 'on a 90-slide deck the rubric scored 81.3 "
            "vs ~68 field average; the cards weren't the bottleneck, the "
            "rubric was.' "
            "LANE 2 pattern-frame: 'the whisper-to-gpt-to-anki setup isn't "
            "where this breaks. card generation is.'"
        ),
        "best_in": {
            "reddit": ["r/startups", "r/Meditation", "r/vipassana"],
            "twitter": ["personal growth", "founder stories"],
            "linkedin": ["career", "leadership", "lessons learned"],
        },
        "note": (
            "NEVER pivot to a product pitch. NEVER mix lanes: presenting an "
            "invented specific as a personal first-hand claim ('i ran this "
            "exact pipeline last semester for two anatomy blocks', 'ran 22 "
            "cameras across three properties for 8 months', 'sat 6 courses "
            "across three centers') without a Lane 1 opener and without "
            "config.json grounding is the exact failure mode the GROUNDING "
            "RULE forbids."
        ),
    },
    "pattern_recognizer": {
        "description": "Name the pattern or phenomenon. Authority through pattern recognition, not credentials.",
        "example": "This is called X / I've seen this play out dozens of times across Y.",
        "best_in": {
            "reddit": ["r/ExperiencedDevs", "r/programming", "r/webdev"],
            "twitter": ["dev", "engineering", "tech trends"],
            "linkedin": ["industry analysis", "tech leadership"],
        },
        "note": "Authority through pattern recognition, not credentials.",
    },
    "curious_probe": {
        "description": "One specific follow-up question about the most interesting detail. Include 'curious because...' context.",
        "example": "curious because we ran into something similar...",
        "best_in": {
            "reddit": ["r/startups", "r/SaaS", "niche subs"],
            "twitter": ["niche topics", "founder discussions"],
            "linkedin": ["thought leadership", "niche B2B"],
        },
        "note": "ONE question only. Never multiple.",
    },
    "contrarian": {
        "description": "Take a clear opposing position backed by experience.",
        "example": "Everyone recommends X. I've done X for Y years and it's wrong.",
        "best_in": {
            "reddit": ["r/Entrepreneur", "r/ExperiencedDevs"],
            "twitter": ["hot takes", "industry debates"],
            "linkedin": ["industry debates", "contrarian leadership"],
        },
        "note": "Must have credible evidence. Empty hot takes get destroyed.",
    },
    "data_point_drop": {
        "description": "Share one specific, believable metric. Let the number do the talking.",
        "example": "$12k in a month (not 'a lot of money')",
        "best_in": {
            "reddit": ["r/Entrepreneur", "r/startups", "r/SaaS"],
            "twitter": ["growth", "revenue", "metrics"],
            "linkedin": ["results", "case studies"],
        },
        "note": "No links. Numbers must be believable, not impressive.",
    },
    "snarky_oneliner": {
        "description": "Short, sharp, emotionally resonant observation (1 sentence max). Validates a shared frustration.",
        "example": "(witty one-liner that nails the shared pain)",
        "best_in": {
            "reddit": ["large subs (500k+ members)"],
            "twitter": ["viral threads", "tech complaints", "industry snark"],
            "linkedin": [],  # never on LinkedIn
        },
        "note": "NEVER in small/serious subs like r/vipassana. NEVER on LinkedIn.",
    },
    # ── Instagram-native caption styles (2026-05-21) ──
    # Distinct from the reply/comment styles above: these describe the
    # structural ARCHETYPE of a long-form IG caption (1400-2150 chars) +
    # the matching 4-5 card overlay. Manually classified from the first
    # 50 posted reels; the defeat-flip arc owns the viral lane (4 of top 5
    # all-time hits, 1.14M peak). Walkin/studyly are product-gated.
    "ig_defeat_flip_arc": {
        "description": (
            "8-beat first-person caption: 'i was [role] for N years. i posted a "
            "confident take. last [time], [agent/junior] did [my job] in [short "
            "time]. i sat at the kitchen counter at midnight with a coffee that "
            "had gone cold. i changed what i sell. the lesson is [skill] was "
            "never the job. [skill] was the typing, typing is free now. stop "
            "[old behavior]. start [new behavior].' Self-deprecating founder "
            "voice; specific numbers (ages, dollar amounts, dates, view counts); "
            "lowercase throughout. Top performer for organic IG posts."
        ),
        "example": (
            "i was 33. nine years writing typescript. fast hands, faster "
            "opinions. last tuesday a 26-year-old shipped my roadmap in 3 days "
            "with claude code. i sat in my kitchen at 1am with a coffee that "
            "had gone cold. ... the lesson is the typing was the job. typing is "
            "free now. stop defending your seat. start running the review."
        ),
        "best_in": {
            "instagram": ["matt_diak", "matthewheartful", "organic AI-lesson reels"],
        },
        "note": (
            "Caption MUST be 1400-2150 chars; overlay is 4 hook-arc cards (2s "
            "each, white bg, black text). Open 'here is a story.'; close with "
            "lesson + 'stop X, start Y' imperative. NO product mention (no "
            "Fazm/Mediar/AppMaker/mk0r/studyly): organic only."
        ),
    },
    "ig_walkin_storefront_playbook": {
        "description": (
            "Door-to-door SMB story for mk0r: 'i was [working-class role]. i "
            "drove past N businesses with no website every day. a friend told "
            "me about mk0r. i walked in / sat at the counter. i opened mk0r.com, "
            "typed one prompt, the site built itself while [owner] watched. "
            "they paid me $[300-500] cash from a [coffee tin / register]. N "
            "months later i've signed [N] of these = $[N]/mo recurring.' Ends "
            "with the mk0r.com footer."
        ),
        "example": (
            "i was 27. shipping clerk at a parts warehouse outside fresno for "
            "4 years, $21 an hour. i drove past 14 auto shops on the way home "
            "every night. ... i opened mk0r.com on his waiting room table and "
            "turned it around. he paid me from the register drawer. ... 23 "
            "auto shops signed since march. 🔗 mk0r.com"
        ),
        "best_in": {
            "instagram": [
                "matt_diak / mk0r product reels",
                "spa", "auto shops", "hotel", "retail", "motel",
            ],
        },
        "note": (
            "Caption is the niche walk-in arc (working-class persona, drive-by "
            "niche, first walk-in success, recurring revenue total). Overlay "
            "is the 5-beat playbook (title card + 4 step cards). MUST include "
            "'mk0r.com' in caption and the mk0r.com footer. project_name='mk0r' "
            "on the row. Fires when TARGET=product AND selected_project=mk0r."
        ),
    },
    "ig_studyly_failing_student_arc": {
        "description": (
            "Failing-student outcome arc for studyly: 'i was [age], [program]. "
            "i [failed/scored low] on [exam]. i [reread/flashcards/highlights] "
            "for weeks. nothing worked. a friend sent me studyly.io. i pasted "
            "my [notes/chapter] in. it quizzed me until i could answer without "
            "looking. i got [higher score].' Closes with rereading-is-theater "
            "lesson + studyly.io footer."
        ),
        "example": (
            "i was 19. premed track, third semester, organic chemistry. i "
            "failed the first orgo exam. 47 out of 100. ... i pasted my notes "
            "into studyly.io at 2am. ... i got 78 on the second exam. ... the "
            "lesson is rereading is recognizing. close the book. let something "
            "ask you. studyly.io"
        ),
        "best_in": {
            "instagram": [
                "matt_diak / matthewheartful studyly product reels",
                "premed", "MCAT", "nursing pharm",
            ],
        },
        "note": (
            "Caption is shorter than mk0r (1400-1900 chars). 'here is a story.' "
            "opener optional. MUST include 'studyly.io' footer. "
            "project_name='studyly'. Lesson is always rereading-is-theater. "
            "Fires when TARGET=product AND selected_project=studyly."
        ),
    },
}

# Valid tone styles. Same set for posting and replying: tone is a separate
# dimension from project-recommendation intent, which is now tracked on its
# own boolean column (posts.is_recommendation / replies.is_recommendation).
# REPLY_STYLES is kept as an alias for backwards compatibility with callers
# that historically treated it as a superset.
VALID_STYLES = set(STYLES.keys())
REPLY_STYLES = VALID_STYLES

# ── Sidecar: model-invented candidate styles ────────────────────────
#
# scripts/engagement_styles_extra.json is the registry of styles the model
# invented at decision time. It is read fresh on every get_all_styles()
# call so a new candidate registered by another agent shows up without a
# process restart. Writes are atomic (temp + rename) and serialized via
# fcntl.flock so concurrent agents inventing the same name don't lose
# each other's metadata.
#
# Each entry shape:
#   {
#     "status": "candidate" | "active" | "retired",
#     "description": str,
#     "example": str,
#     "note": str,
#     "why_existing_didnt_fit": str,           # rationale at invention
#     "first_post_url": str | None,
#     "first_post_id": int | None,
#     "first_post_platform": str | None,
#     "invented_by_model": str | None,
#     "invented_at": ISO-8601 UTC,
#     "promoted_at": ISO-8601 UTC | None,
#     "best_in": {platform: [hint,...]},       # filled in by promoter
#   }
#
# Hardcoded STYLES are treated as status="active" implicitly.

SIDECAR_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "engagement_styles_extra.json")

_REQUIRED_NEW_STYLE_FIELDS = ("description", "example", "why_existing_didnt_fit")


def _load_extra_styles():
    """Read and parse the sidecar JSON. Returns {} on any error or missing file."""
    try:
        with open(SIDECAR_PATH, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _normalize_entry(entry, default_status="active"):
    """Ensure a STYLES-style dict has the fields callers expect."""
    out = dict(entry) if isinstance(entry, dict) else {}
    out.setdefault("status", default_status)
    out.setdefault("description", "")
    out.setdefault("example", "")
    out.setdefault("note", "")
    out.setdefault("best_in", {})
    return out


def get_all_styles():
    """Merged universe: hardcoded STYLES (active) + sidecar candidates/actives.

    Sidecar entries override hardcoded ones if they share a name (so the
    promoter or a manual edit can adjust description/best_in without
    modifying the locked module). Caller MUST treat the returned dict as
    read-only.
    """
    merged = {name: _normalize_entry(meta, "active") for name, meta in STYLES.items()}
    for name, meta in _load_extra_styles().items():
        if not isinstance(meta, dict):
            continue
        merged[name] = _normalize_entry(meta, "candidate")
    return merged


def _atomic_write_sidecar(data):
    """Write the sidecar JSON atomically (temp + rename) and fsync."""
    tmp = SIDECAR_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    os.replace(tmp, SIDECAR_PATH)


def register_style(name, meta, source_post=None):
    """Register a model-invented style into the sidecar.

    Called when an orchestrator parses a decision JSON whose
    engagement_style is not in get_all_styles() and whose `new_style`
    block is well-formed.

    Args:
        name: the style name the model picked.
        meta: dict with at least description/example/why_existing_didnt_fit
              (and optionally note). Anything else is preserved verbatim.
        source_post: optional dict {platform, post_url, post_id, model}
              describing the post that birthed this style. Recorded only
              the FIRST time a name is registered.

    Returns:
        (status_str, entry_dict): status in {"new", "existing", "rejected"}.
        On "rejected", entry_dict carries an "error" key describing why.
    """
    if not name or not isinstance(name, str):
        return "rejected", {"error": "name must be a non-empty string"}
    if not isinstance(meta, dict):
        return "rejected", {"error": "new_style block must be an object"}
    missing = [f for f in _REQUIRED_NEW_STYLE_FIELDS
               if not (isinstance(meta.get(f), str) and meta[f].strip())]
    if missing:
        return "rejected", {"error": f"new_style missing fields: {missing}"}
    if name in STYLES:
        # The model picked a hardcoded name and *also* shipped a new_style
        # block. Treat as "existing"; never overwrite the curated entry.
        return "existing", _normalize_entry(STYLES[name], "active")

    src = source_post or {}
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    try:
        import fcntl  # POSIX-only; this whole repo is macOS/Linux
    except ImportError:
        fcntl = None

    # Open-or-create a lock file alongside the sidecar so flock has a stable inode.
    lock_path = SIDECAR_PATH + ".lock"
    lock_fd = None
    try:
        lock_fd = open(lock_path, "a+")
        if fcntl is not None:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)

        existing = _load_extra_styles()
        if name in existing:
            return "existing", existing[name]

        entry = {
            "status": "candidate",
            "description": meta["description"].strip(),
            "example": meta["example"].strip(),
            "note": (meta.get("note") or "").strip(),
            "why_existing_didnt_fit": meta["why_existing_didnt_fit"].strip(),
            "first_post_url": src.get("post_url"),
            "first_post_id": src.get("post_id"),
            "first_post_platform": src.get("platform"),
            "invented_by_model": src.get("model"),
            "invented_at": now_iso,
            "promoted_at": None,
            "best_in": {},
        }
        existing[name] = entry
        _atomic_write_sidecar(existing)
        return "new", entry
    finally:
        if lock_fd is not None:
            if fcntl is not None:
                try:
                    fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            lock_fd.close()


def validate_or_register(decision, source_post=None, context="posting"):
    """One-shot helper for orchestrators that parse a decision JSON.

    Reads decision["engagement_style"] (and optional decision["new_style"]).
    Returns (style_or_None, action) where action is one of:
        "valid"      → style was already in the universe, accept it
        "registered" → unknown style + well-formed new_style → registered
                       as candidate, accept it
        "rejected"   → unknown style and no usable new_style → caller
                       should drop the post or null the style column
    Logs the action to stdout for the orchestrator's run log.
    """
    style = decision.get("engagement_style") if isinstance(decision, dict) else None
    new_style = decision.get("new_style") if isinstance(decision, dict) else None

    if style and style in get_all_styles():
        return style, "valid"

    if not style:
        return None, "rejected"

    if not isinstance(new_style, dict):
        print(f"[engagement_styles] unknown style {style!r} and no new_style block; rejecting")
        return None, "rejected"

    status, entry = register_style(style, new_style, source_post)
    if status == "rejected":
        print(f"[engagement_styles] new_style for {style!r} rejected: {entry.get('error')}")
        return None, "rejected"
    if status == "new":
        src_url = (source_post or {}).get("post_url", "?")
        print(f"[engagement_styles] REGISTERED candidate style {style!r} from {src_url}")
    return style, "registered"


# ── Platform-specific policy overlay ────────────────────────────────
#
# Tier assignment (dominant / secondary / rare) is DB-driven — see
# get_dynamic_tiers() below. This dict only stores static policy that
# is not a performance judgment:
#   - `never`: tone/brand constraints (e.g. no snark on LinkedIn). Even
#     if the data showed high upvotes, we still do not want this style.
#   - `note`: per-platform tone/length hint shown at the top of the
#     styles prompt.

PLATFORM_POLICY = {
    "reddit": {
        "never": ["curious_probe"],
        "note": "Short wins. 1 punchy sentence or 4-5 of real substance. Start with 'I' or 'my'. Match style to subreddit culture.",
    },
    "twitter": {
        "never": [],
        "note": "Brevity wins. Direct product mentions OK (unlike Reddit). 1-2 sentences max.",
    },
    "linkedin": {
        "never": ["snarky_oneliner"],
        "note": "Professional but human. Softer critic framing. No snark. 2-4 sentences.",
    },
    "github": {
        "never": ["snarky_oneliner"],
        "note": "Technical and specific. Lead with the pain, then the fix. 400-600 chars.",
    },
    "moltbook": {
        "never": [],
        "note": "Agent voice ('my human'). Conversational but substantive. 2-4 sentences.",
    },
    "instagram": {
        # Reply/comment styles don't apply to long-form IG captions.
        # Product styles are project-gated and assigned by the render
        # script directly (see skill/run-instagram-render.sh) so the
        # picker can't accidentally roll a "walkin" style for an organic
        # matt_diak post.
        "never": [
            "critic", "storyteller", "pattern_recognizer", "curious_probe",
            "contrarian", "data_point_drop", "snarky_oneliner",
            "ig_walkin_storefront_playbook",
            "ig_studyly_failing_student_arc",
        ],
        "note": (
            "IG captions are long-form ORIGINAL posts (1400-2150 chars), "
            "lowercase, 8-beat story arc. Overlay is 4-5 short cards "
            "(2s each, white bg, black text). Voice is self-deprecating "
            "founder confession. NO em/en dashes. The picker only fires "
            "on TARGET=organic; product posts assign style directly from "
            "selected_project."
        ),
    },
}

# Minimum sample size before we trust a style's avg_upvotes.
# Below this, the style is "explore" and gets the STYLE_FLOOR_PCT only.
MIN_SAMPLE_SIZE = 5

# Target-distribution tuning knobs (used by compute_target_distribution).
# WEIGHT_EXPONENT > 1 sharpens the distribution toward the winner.
# STYLE_FLOOR_PCT guarantees every non-never style still gets tested.
# STYLE_CAP_PCT prevents a runaway winner from starving the rest.
WEIGHT_EXPONENT = 2.0
STYLE_FLOOR_PCT = 5.0
STYLE_CAP_PCT = 50.0


# Recency window for the picker target distribution. Lifetime aggregation
# drifted too far from current performance reality (e.g. 2025 wins from
# pattern_recognizer kept it in the pool even after 2026's audience shift).
# 30 days keeps n>=50 per active style on Reddit/Twitter while letting the
# pool track the live algorithm. Set RECENCY_DAYS=0 to fall back to lifetime.
RECENCY_DAYS = 30


def _fetch_style_stats(platform, days=None):
    """Query the autoposter API for per-engagement_style performance.

    Returns a dict:
        {style_name: {"n": int, "avg_up": float, "avg_cm": float,
                      "avg_clicks": float}}
    avg_up is NET (Reddit/Moltbook self-upvote stripped); avg_clicks is the
    bot-filtered click count. The three combine into the same composite the
    top_performers report ranks on. Returns {} on any error (API unreachable,
    missing env, cold start).

    Recency: `days` overrides the module-level RECENCY_DAYS (default 30).
    Pass days=0 for lifetime aggregation.

    Routes through the social-autoposter-website API (no direct DB access)
    so VMs / sandboxes without a DATABASE_URL still get live weights.
    """
    try:
        import os
        import sys as _sys
        _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from http_api import api_get
        eff_days = RECENCY_DAYS if days is None else int(days)
        resp = api_get(
            "/api/v1/engagement-styles/style-stats",
            {"platform": platform, "days": str(eff_days)},
        )
        data = (resp or {}).get("data") or {}
        stats = data.get("stats") or {}
        return {
            name: {
                "n": int(v.get("n", 0)),
                "avg_up": float(v.get("avg_up", 0.0)),
                "avg_cm": float(v.get("avg_cm", 0.0)),
                "avg_clicks": float(v.get("avg_clicks", 0.0)),
            }
            for name, v in stats.items()
            if isinstance(v, dict)
        }
    except Exception:
        return {}


# Composite style score, mirroring scripts/top_performers.py SCORE_SQL:
# a real human click outweighs 10 upvotes of vibes, comments sit in the
# middle. The picker weights styles by this score (sharpened by
# WEIGHT_EXPONENT) so the style that actually drives clicks wins the
# distribution, not the one that merely accumulates passive likes.
CLICK_WEIGHT = 10.0
COMMENT_WEIGHT = 3.0


def _style_score(stat):
    """Composite per-style score from a _fetch_style_stats() row.

    stat is {"avg_up", "avg_cm", "avg_clicks", ...}. avg_up is already net.
    """
    return (
        float(stat.get("avg_clicks", 0.0)) * CLICK_WEIGHT
        + float(stat.get("avg_cm", 0.0)) * COMMENT_WEIGHT
        + float(stat.get("avg_up", 0.0))
    )


def get_dynamic_tiers(platform, context="posting"):
    """Rank styles for `platform` by avg_upvotes from the posts table.

    Returns (dominant, secondary, rare) tuple of style-name lists.

    Policy:
      - Styles in PLATFORM_POLICY[platform].never are excluded entirely.
      - Styles with N < MIN_SAMPLE_SIZE are placed in `secondary` (explore),
        regardless of their noisy avg_up.
      - Styles with N >= MIN_SAMPLE_SIZE are sorted by avg_up DESC and split:
          top third  -> dominant
          middle     -> secondary
          bottom third (or single worst) -> rare
      - Any style with zero samples (never logged yet) is added to
        `secondary` so the LLM still explores it.
      - Cold start (no data at all): every non-never style becomes secondary.
    """
    never = set(PLATFORM_POLICY.get(platform, {}).get("never", []))
    universe = get_all_styles()
    candidate_styles = [s for s in universe.keys() if s not in never]

    stats = _fetch_style_stats(platform)

    trusted = []  # (style, avg_up) with N >= MIN_SAMPLE_SIZE
    explore = []  # styles with N < MIN_SAMPLE_SIZE (incl. zero samples)

    for style in candidate_styles:
        s = stats.get(style)
        # Sidecar `candidate` styles never enter the trusted bucket; they
        # only get floor-weight exploration until the promoter graduates them.
        is_candidate = universe[style].get("status") == "candidate"
        if s and s["n"] >= MIN_SAMPLE_SIZE and not is_candidate:
            trusted.append((style, s["avg_up"]))
        else:
            explore.append(style)

    trusted.sort(key=lambda x: x[1], reverse=True)

    if not trusted:
        # Cold start: no trusted performance data for this platform yet.
        return [], explore, []

    # Split trusted into thirds. Small lists (1-2 items) go entirely to dominant.
    t = len(trusted)
    if t <= 2:
        dominant = [s for s, _ in trusted]
        rare = []
    else:
        third = max(1, t // 3)
        dominant = [s for s, _ in trusted[:third]]
        rare = [s for s, _ in trusted[-third:]]
    secondary = [s for s, _ in trusted if s not in dominant and s not in rare]
    secondary = secondary + explore  # untrusted styles always explore
    return dominant, secondary, rare


# ── Target distribution ─────────────────────────────────────────────
# (_last_picks helper removed 2026-05-19 alongside the legacy
# "show all 9 styles" prompt block it served. The picker doesn't use
# recent-pick history; it samples weighted-random from the top-N each
# turn. The `/api/v1/engagement-styles/last-picks` endpoint is still
# live for the dashboard's audit surface.)


def compute_target_distribution(platform, context="posting"):
    """Compute per-style target pick% using a sharpened click-weighted score.

    Returns a list of dicts sorted by target pct DESC:
        [{"style", "pct", "n", "avg_up", "avg_cm", "avg_clicks", "score",
          "trusted", "is_candidate", "weight"}]

    Policy:
      - Styles in PLATFORM_POLICY[platform].never are excluded.
      - The per-style score is clicks*10 + comments*3 + upvotes_net, the
        same composite scripts/top_performers.py ranks posts on. A real
        human click is the ground-truth conversion signal; upvotes are
        passive vibes. (Pre-2026-05-15 this used avg_upvotes alone, which
        disagreed with every click-aware surface in the repo.)
      - Styles with N >= MIN_SAMPLE_SIZE get weight = score ** WEIGHT_EXPONENT.
      - Styles with N < MIN_SAMPLE_SIZE (incl. zero) get STYLE_FLOOR_PCT only
        so noisy small-n styles (e.g. n=1 with a lucky viral post) don't
        dominate.
      - STYLE_FLOOR_PCT is applied to every remaining style so nothing hits 0%.
      - STYLE_CAP_PCT caps the top style; overflow redistributes pro-rata.
      - Cold start (no trusted data): equal share across non-never styles.
    """
    never = set(PLATFORM_POLICY.get(platform, {}).get("never", []))
    universe = get_all_styles()
    candidates = [s for s in universe.keys() if s not in never]
    stats = _fetch_style_stats(platform)

    rows = []
    trusted_total = 0.0
    for style in candidates:
        s = stats.get(style)
        n = int(s["n"]) if s else 0
        avg_up = float(s["avg_up"]) if s else 0.0
        avg_cm = float(s.get("avg_cm", 0.0)) if s else 0.0
        avg_clicks = float(s.get("avg_clicks", 0.0)) if s else 0.0
        score = _style_score(s) if s else 0.0
        # Sidecar candidates never count as trusted; they get floor weight only
        # until promoted, regardless of their sample count.
        is_candidate_status = universe[style].get("status") == "candidate"
        trusted = (s is not None and n >= MIN_SAMPLE_SIZE
                   and not is_candidate_status)
        weight = (score ** WEIGHT_EXPONENT) if trusted else 0.0
        if trusted:
            trusted_total += weight
        rows.append({"style": style, "n": n, "avg_up": avg_up,
                     "avg_cm": avg_cm, "avg_clicks": avg_clicks,
                     "score": score, "trusted": trusted,
                     "weight": weight, "pct": 0.0,
                     "is_candidate": is_candidate_status})

    if not rows:
        return []

    # Cold start: no trusted data. Equal share across all non-never styles.
    if trusted_total <= 0:
        share = 100.0 / len(rows)
        for r in rows:
            r["pct"] = share
        rows.sort(key=lambda r: r["style"])
        return rows

    # Raw score%: weight / total. Explore styles stay at 0.
    for r in rows:
        r["pct"] = (r["weight"] / trusted_total) * 100.0 if r["trusted"] else 0.0

    # Apply floor: every style gets at least STYLE_FLOOR_PCT.
    # Redistribute remaining mass pro-rata among styles that were already above floor.
    below = [r for r in rows if r["pct"] < STYLE_FLOOR_PCT]
    above = [r for r in rows if r["pct"] >= STYLE_FLOOR_PCT]
    floored_total = STYLE_FLOOR_PCT * len(below)
    remaining = max(0.0, 100.0 - floored_total)
    above_sum = sum(r["pct"] for r in above) or 1.0
    for r in below:
        r["pct"] = STYLE_FLOOR_PCT
    for r in above:
        r["pct"] = (r["pct"] / above_sum) * remaining

    # Apply cap: top style can't exceed STYLE_CAP_PCT. Overflow redistributes
    # pro-rata among others (their current pct as the weight).
    rows.sort(key=lambda r: r["pct"], reverse=True)
    if rows and rows[0]["pct"] > STYLE_CAP_PCT:
        overflow = rows[0]["pct"] - STYLE_CAP_PCT
        rows[0]["pct"] = STYLE_CAP_PCT
        others = rows[1:]
        others_sum = sum(r["pct"] for r in others) or 1.0
        for r in others:
            r["pct"] += overflow * (r["pct"] / others_sum)

    return rows


# ── Programmatic picker (2026-05-19) ────────────────────────────────
#
# The old flow ("show the model 9 styles + target % and let it pick") was
# soft: the model anchored on the most-generic-fit style (pattern_recognizer)
# and over-picked it ~30% of posts even when its target % was the 5% floor.
# This picker flips the contract: code picks ONE style by weighted sample
# from the top N by composite score, the prompt assigns that style, and the
# model only authors the comment. Curation to top N happens dynamically per
# score, so styles auto-rotate as their click-weighted score shifts. The
# model can still invent: with probability INVENT_RATE the picker returns
# mode="invent" and the prompt hands the model the top N as reference
# material to derive a new style from.

INVENT_RATE = 0.05  # ~1 in 20 posts forces a new-style invention
CURATED_TOP_N = 5   # the model only ever sees one of the top 5 by score

# Sample-size credibility floor for the picker. Below this, a style's score
# is linearly shrunk toward 0 so a single viral comment with n=6 can't
# dominate the use_pool. Once n >= MIN_SAMPLE_FULL_WEIGHT the style gets
# its full raw score weight.
#
# Why: LinkedIn 2026-05-19 had data_point_drop n=6 with avg_cm=81 (one
# viral comment) producing score=290 — 10x the next style. The picker was
# returning data_point_drop 67% of the time on a near-non-existent
# evidence base. Shrinkage of `score * min(1.0, n / 20)` brings n=6 →
# credibility 0.30, knocking 290 down to ~87 and letting n=18 styles
# (agree_and_extend, etc.) compete fairly. Existence floor stays at
# MIN_SAMPLE_SIZE=5 so n=2 outliers still get filtered entirely.
MIN_SAMPLE_FULL_WEIGHT = 20


def _credibility_factor(n):
    """Sample-size shrinkage factor in [0, 1] for the picker.

    n >= MIN_SAMPLE_FULL_WEIGHT → 1.0 (full trust)
    n <  MIN_SAMPLE_FULL_WEIGHT → n / MIN_SAMPLE_FULL_WEIGHT (linear ramp)
    n <= 0                       → 0.0
    """
    if not n or n <= 0:
        return 0.0
    if n >= MIN_SAMPLE_FULL_WEIGHT:
        return 1.0
    return float(n) / float(MIN_SAMPLE_FULL_WEIGHT)


def _picker_score(row):
    """Picker-only credibility-adjusted score used to (a) rank into the
    top-N use_pool and (b) weight the random draw inside that pool.

    Keeps `_style_score` pure so top_performers / dashboard / other
    surfaces keep showing the raw composite. Only the picker shrinks."""
    return float(row.get("score", 0.0)) * _credibility_factor(row.get("n", 0))


def pick_style_for_post(platform, context="posting",
                        top_n=CURATED_TOP_N, invent_rate=INVENT_RATE,
                        rng=None):
    """Programmatically pick ONE engagement style for the model to use.

    Replaces the legacy "show all styles, model picks" flow. Returns a
    dict that get_assigned_style_prompt() turns into a compact prompt
    block (one style + description + example + note, or invent + top-N
    reference). The caller also passes the picked style downstream to
    log_post / validate_or_register so we can detect drift between the
    assignment and the final logged style.

    Curation is automatic: top_n styles by composite score (clicks*10 +
    comments*3 + upvotes_net) are eligible. As performance shifts, the
    curated set rotates. Styles in PLATFORM_POLICY.never are excluded.
    Sidecar candidate styles (status="candidate") are excluded from the
    use path until the promoter graduates them, but stay available as
    invent-mode references once graduated.

    Args:
        platform: "reddit" | "twitter" | "linkedin" | "github" | "moltbook"
        context: "posting" | "replying"
        top_n: curation size. Top N styles by score are eligible.
        invent_rate: probability of returning mode="invent" so the model
                     creates a new style from the top N references.
                     Set to 0 to disable invention entirely.
        rng: optional random.Random for deterministic tests.

    Returns:
        {
            "mode": "use" | "invent",
            "style": str | None,                  # the assigned style, None on invent
            "description": str | None,
            "example": str | None,
            "note": str | None,
            "reference_styles": [                 # top-N meta (always populated)
                {"style", "description", "example", "note",
                 "score", "pct", "n", "avg_clicks", "avg_cm", "avg_up"},
                ...
            ],
            "distribution_snapshot": list,         # full target distribution at pick time
            "picked_at": ISO-8601 UTC,
        }
    """
    rnd = rng or random
    never = set(PLATFORM_POLICY.get(platform, {}).get("never", []))
    rows = compute_target_distribution(platform, context=context)
    rows = [r for r in rows if r["style"] not in never]

    # Trust-filter first so n=1 outliers (e.g. snarky_oneliner with a single
    # lucky 12-upvote post) can't claim a top reference slot. They stay in
    # distribution_snapshot for audit but not in use_pool or reference_pool.
    #
    # Ranking uses `_picker_score` (raw score shrunk by sample-size
    # credibility) instead of raw `score`. Without this, a style with n=6 and
    # one viral comment can leapfrog n=400 styles into the use_pool. With
    # shrinkage, n=6 only contributes ~30% of its raw score weight, so it
    # competes fairly with the wider-sample alternatives.
    trusted_rows = [r for r in rows if r.get("trusted")]
    trusted_sorted = sorted(trusted_rows, key=_picker_score, reverse=True)
    use_pool = trusted_sorted[:top_n]
    reference_pool = trusted_sorted[:top_n]

    universe = get_all_styles()

    def _meta_for(row):
        m = universe.get(row["style"], {})
        return {
            "style": row["style"],
            "description": m.get("description", ""),
            "example": m.get("example", ""),
            "note": m.get("note", ""),
            "score": round(row.get("score", 0.0), 3),
            "pct": round(row.get("pct", 0.0), 1),
            "n": row.get("n", 0),
            "avg_clicks": round(row.get("avg_clicks", 0.0), 3),
            "avg_cm": round(row.get("avg_cm", 0.0), 3),
            "avg_up": round(row.get("avg_up", 0.0), 3),
        }

    reference_styles = [_meta_for(r) for r in reference_pool]
    distribution_snapshot = [
        {"style": r["style"], "score": round(r.get("score", 0.0), 3),
         "pct": round(r.get("pct", 0.0), 1), "n": r.get("n", 0),
         "trusted": bool(r.get("trusted"))}
        for r in rows
    ]
    picked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Invent path. Also fires as fallback when no trusted style exists yet
    # (cold start), because we'd rather the model invent something fresh
    # than be assigned a noisy n=1 outlier.
    invent = (not use_pool) or (
        invent_rate > 0 and rnd.random() < invent_rate
    )
    if invent:
        return {
            "mode": "invent",
            "style": None,
            "description": None,
            "example": None,
            "note": None,
            "reference_styles": reference_styles,
            "distribution_snapshot": distribution_snapshot,
            "picked_at": picked_at,
        }

    # Use path: weighted random sample by credibility-shrunk score. Raw score
    # (not score ** WEIGHT_EXPONENT) keeps the distribution smoother across
    # the top — the cap+floor logic that lives in compute_target_distribution
    # is what the picker historically used to give a winner most weight;
    # here, the top N is already a curated head, so raw score is the right
    # knob, with the credibility shrink (`_picker_score`) preventing small-n
    # outliers from dominating the draw.
    weights = [max(_picker_score(r), 0.01) for r in use_pool]
    total = sum(weights) or 1.0
    pick = rnd.uniform(0.0, total)
    cum = 0.0
    chosen_row = use_pool[0]
    for r, w in zip(use_pool, weights):
        cum += w
        if pick <= cum:
            chosen_row = r
            break

    meta = _meta_for(chosen_row)
    return {
        "mode": "use",
        "style": chosen_row["style"],
        "description": meta["description"],
        "example": meta["example"],
        "note": meta["note"],
        "reference_styles": reference_styles,
        "distribution_snapshot": distribution_snapshot,
        "picked_at": picked_at,
    }


def get_assigned_style_prompt(platform, assignment, context="posting"):
    """Compact prompt block built from a pick_style_for_post() assignment.

    Replaces get_styles_prompt() for callers that have flipped to the
    programmatic picker. Two shapes:

    USE mode (the common case):
      One style is assigned. The block shows description / example / note
      plus the platform tone hint and the grounding rule. No list of other
      styles, no target %, no over/under-used hints. Decision was already
      made; the model only needs to know what the assigned style means.

    INVENT mode (~5% of posts):
      No style assigned. The block shows the top N curated styles as
      reference (each with description + example + score) and instructs
      the model to invent a fresh style. Output JSON must set
      engagement_style=<new_snake_case_name> AND include a new_style block;
      validate_or_register handles the rest.
    """
    policy = PLATFORM_POLICY.get(platform, PLATFORM_POLICY["reddit"])
    lines = []

    if assignment["mode"] == "use":
        lines.append(f"## Your assigned engagement style: **{assignment['style']}**")
        lines.append("")
        lines.append(
            f"This style was selected by the picker (weighted by live "
            f"click-driven performance across {platform}). Use it. Do not "
            f"swap it for a different listed style."
        )
        lines.append("")
        lines.append(f"Platform tone: {policy.get('note', '')}")
        lines.append("")
        lines.append(f"**{assignment['style']}**: {assignment.get('description', '')}")
        if assignment.get("example"):
            lines.append(f'  Example: "{assignment["example"]}"')
        if assignment.get("note"):
            lines.append(f"  Note: {assignment['note']}")
        lines.append("")
        lines.append(
            'In your output JSON, set "engagement_style" to '
            f'"{assignment["style"]}" and leave "new_style" as null.'
        )
        lines.append("")
        lines.append(
            "Escape hatch: if and only if this assigned style genuinely "
            "cannot fit the thread (rare), you may instead invent a new "
            "style by setting `engagement_style` to your snake_case name "
            "AND replacing `\"new_style\": null` with a full new_style "
            "block (description / example / note / why_existing_didnt_fit). "
            "Do not silently substitute a different listed style."
        )
    else:
        lines.append("## Invent a new engagement style for this post")
        lines.append("")
        lines.append(
            "The picker is asking you to derive a fresh style for this "
            f"thread (~{int(INVENT_RATE * 100)}% of posts get the invent path). "
            "Look at our top performers below as reference for what already "
            "works on this platform, then pick a fresh angle that none of "
            "them captures. Set `engagement_style` to your snake_case name "
            "AND include a full `new_style` block in the same JSON."
        )
        lines.append("")
        lines.append(f"Platform tone: {policy.get('note', '')}")
        lines.append("")
        lines.append(f"### Top {len(assignment.get('reference_styles', []))} reference styles on {platform}")
        lines.append("")
        for ref in assignment.get("reference_styles", []):
            lines.append(
                f"- **{ref['style']}** "
                f"(score {ref['score']:.2f}, clicks {ref['avg_clicks']:.2f}, "
                f"cm {ref['avg_cm']:.2f}, up {ref['avg_up']:.2f}, n={ref['n']})"
            )
            lines.append(f"  {ref['description']}")
            if ref.get("example"):
                lines.append(f'  Example: "{ref["example"]}"')
        lines.append("")
        lines.append(
            "Your new style should be a real third option — not a rename "
            "of one above. Set the new_style block fields:"
        )
        lines.append("  - description: one sentence")
        lines.append("  - example: short utterance demonstrating the style")
        lines.append("  - note: when to use / when not to")
        lines.append("  - why_existing_didnt_fit: why none of the above worked here")

    lines.append("")
    lines.append(
        'AVOID the "pleaser/validator" style ("this is great", "had similar '
        'results", "100% agree"). Consistently the lowest engagement on every '
        'platform.'
    )
    lines.append("")
    lines.append(get_grounding_rule())
    return "\n".join(lines)


# ── Prompt generators ───────────────────────────────────────────────

def get_styles_prompt(platform, context="posting", assignment=None):
    """Generate the engagement-styles prompt block for a platform.

    Always routes through the picker: one style is assigned (weighted by
    live click-driven performance over the recent window) and the prompt
    embeds that single style's description + example + note + grounding
    rule. The model is told to use it, not to choose from a menu.

    Args:
        platform: "reddit", "twitter", "linkedin", "github", "moltbook"
        context: "posting" (new posts) or "replying" (engagement replies)
        assignment: an optional pre-computed pick_style_for_post() result.
                    Orchestrators that need to know the picked style (to
                    filter top_performers, pin the literal style name into
                    their JSON output example, etc.) pick once, then pass
                    the assignment in here to avoid double-picking. Callers
                    that don't care can omit it and get a fresh pick.
    """
    if assignment is None:
        assignment = pick_style_for_post(platform, context=context)
    return get_assigned_style_prompt(platform, assignment, context=context)


def get_grounding_rule():
    """The two-lane grounding rule, shared across every pipeline.

    Every comment picks ONE of two mutually exclusive lanes. They cannot
    be combined: you can disclose a story OR stay grounded, never present
    a fabricated specific as a personal first-hand fact.

      LANE 1 - DISCLOSED STORY: invent freely, but flag the frame.
        Open the story with a hedge that signals it is illustration,
        not testimony ('hypothetically', 'imagine someone running this',
        'say a friend tried', 'as a thought experiment', 'scenario:').
        Once the frame is set, free to use any names, durations, counts,
        places. The reader can tell from the first phrase that this is
        a worked example, not a personal claim.

      LANE 2 - NO FABRICATION: stay first-person / plain voice, but every
        specific (number, duration, date, place, course/program, headcount,
        named tool, named person) must appear verbatim in the matched
        project's content_angle / voice / messaging in config.json. If a
        specific is not in config: drop it, generalize ('a few months',
        'a handful of cameras'), or pattern-frame ('the part that breaks
        down is...'). Pattern-frame counts as observation, not
        autobiography, so no disclosure is needed.

    Pick the lane that fits the thread. Niche / expert subs reward Lane 2;
    casual / advice subs tolerate Lane 1 when the disclosure feels natural.
    """
    return """## GROUNDING RULE (highest priority; overrides any other style guidance)

Every comment picks ONE lane. The two lanes are mutually exclusive: you
cannot present an invented detail as a personal first-hand claim. Choose:

LANE 1 - DISCLOSED STORY (invent freely, but flag it)
  Open with a phrase that signals the story is illustration, not lived
  testimony. Once the frame is set, use whatever names / durations /
  counts / places fit the point.
  Acceptable openers: "hypothetically", "imagine someone running this",
  "say a friend tried", "as a thought experiment", "scenario:",
  "to make this concrete, picture", "made-up example but".
  After the opener, full creative license on the details.

LANE 2 - NO FABRICATION (first-person OK only if every specific is real)
  Stay plain-voiced. Any specific (number, duration, date, place name,
  course/program, headcount, named tool, named person) is allowed ONLY
  if it appears verbatim in the matched project's content_angle, voice
  (tone/examples/examples_good), or messaging (lead_with_pain / solution
  / proof) in config.json. If a specific is not in config: drop it,
  generalize ("a few months", "a handful of cameras"), or pattern-frame
  ("the part that breaks down is...", "the typical failure mode is...").
  Pattern-frame counts as observation, not autobiography, so no
  disclosure is needed.

NEVER MIX: do not write "i ran 22 cameras for 8 months" without either
(a) a Lane 1 opener in front of it, or (b) those numbers being in
config.json. That is the failure mode this rule exists to kill.

Worked examples (drawn from real posts in our DB):

  BAD (fabricated personal anecdote, no disclosure, no config anchor):
    "i ran this exact pipeline last semester for two anatomy blocks,
    cheap recorder into whisper into gpt into anki, raw gpt got me
    about 35% usable cards..."
  LANE 1 REWRITE (same details, but disclosed):
    "hypothetically, imagine running this for a couple of lecture
    blocks: cheap recorder into whisper into gpt into anki. raw
    prompts get you somewhere around a third usable cards before
    duplicate distractors and trivial restatements take over."
  LANE 2 REWRITE (pattern-frame, no invented specifics):
    "the whisper-to-gpt-to-anki setup isn't where this breaks. card
    generation is. raw prompts produce roughly a third usable before
    duplicate distractors and trivial restatements take over."

  BAD (fabricated rig, no disclosure):
    "i ran 22 cameras across three properties for about 8 months and
    we were getting 400+ 'person detected' pings a night..."
  LANE 1 REWRITE (disclosed scenario):
    "scenario: 20-something cameras across a few properties, motion
    alerts firing 400+ times a night. by week two everyone has
    notifications muted and the system is dead..."
  LANE 2 REWRITE (no invented numbers):
    "the issue with motion alerts isn't reliability, it's signal to
    noise. once cameras start firing on every shadow and leaf blower,
    everyone mutes notifications inside a week. the fix is filtering
    on intent (vehicle at gate after midnight, person near door,
    loiter over a minute), not more cameras."

  GOOD LANE 2 (config-grounded, first-person OK, specifics are real):
    "on a 90-slide deck the eval scored 81.3 vs the field average
    around 68; the cards weren't the bottleneck, the rubric was."
    (The 81.3 / 68 / 90-slide numbers come from the matched project's
    messaging.proof and voice.examples.)

This rule outranks "specificity is the #1 authenticity signal" wherever
they conflict. Specificity still wins, but only via Lane 1 disclosure
or Lane 2 config grounding."""


def get_content_rules(platform):
    """Generate platform-specific content rules.

    Args:
        platform: "reddit", "twitter", or "linkedin"

    Returns:
        Multi-line string of content rules.
    """
    common = [
        "NO em dashes. Use commas, periods, or regular dashes (-).",
        "Never say 'I built' or 'we built'. Never mention any project by name unless recommending.",
        'Never start with "exactly", "yeah totally", "100%", "that\'s smart".',
        "Specificity is the #1 authenticity signal, but only for specifics you have license to use. See GROUNDING RULE: pick LANE 1 (open with a disclosure phrase like 'hypothetically' or 'scenario:' and then invent freely) OR LANE 2 (no fabricated specifics; first-person only when the numbers, durations, dates, places, course/program names, headcount, or named tools come verbatim from the matched project's config.json). Never present a fabricated specific as a personal first-hand claim.",
        "Include imperfections: contractions, casual asides, occasional lowercase.",
    ]

    platform_rules = {
        "reddit": [
            "Go BIMODAL: either 1 punchy sentence (<100 chars, highest avg upvotes) or 4-5 sentences of real substance. AVOID the 2-3 sentence dead zone.",
            "Start with 'I' or 'my' (first-person experience). 'I did X' beats 'you should do X'.",
            "No markdown in Reddit (no ##, **, numbered lists). Casual tone, lowercase OK, fragments OK.",
            "NEVER mention product names (fazm, assrt, pieline, cyrano, terminator, mk0r, s4l). NEVER include URLs or links.",
            "Statements beat questions. Be authoritative, not inquisitive. No 'anyone else experience this?'",
        ],
        "twitter": [
            "Keep it short: 1-2 sentences max. Fragments and lowercase OK.",
            "Direct product mentions OK when relevant (unlike Reddit).",
            "No hashtags. No threads. No 'RT if you agree' bait.",
            "Punch line first, context second.",
        ],
        "linkedin": [
            "Professional but casual tone. 2-4 sentences.",
            "Softer framing for critic style (constructive, not combative).",
            "No snark. No sarcasm. Earnest insights land better here.",
            "Line breaks between thoughts for readability.",
        ],
    }

    rules = platform_rules.get(platform, platform_rules["reddit"]) + common
    return "\n".join(f"- {r}" for r in rules)


def get_anti_patterns():
    """Content anti-patterns shared across all platforms."""
    return """## Anti-patterns
- NEVER start with "exactly", "yeah totally", "100%", "that's smart". Vary first words.
- NEVER say "I built" / "we built" / "I'm working on". Frame products as recommendations, not self-promotion.
- NEVER suggest calls, meetings, demos.
- NEVER promise to share links/files not in config.json.
- NEVER offer to DM. NEVER make time-bound promises.
- Some replies should be 1 sentence. Not everything needs 3-4 sentences."""


def get_valid_styles(context="posting"):
    """Return the set of valid style names.

    Args:
        context: "posting" for new posts, "replying" for engagement replies.
    """
    if context == "replying":
        return REPLY_STYLES
    return VALID_STYLES


def validate_style(style, context="posting"):
    """Check if a style name is valid. Returns the style or None.

    Consults the live universe (hardcoded STYLES + sidecar candidates) so
    a candidate registered in this process or by another agent passes.
    """
    if not style:
        return None
    if style in get_all_styles():
        return style
    # Backwards path: a few callers (like locked octolens scripts) only
    # know the hardcoded set. Keep that path working for them.
    valid = get_valid_styles(context)
    if style in valid:
        return style
    return None


def target_distribution_snapshot(platform, context="posting"):
    """Compact, JSON-serializable snapshot of the current target distribution.

    This is what the picker would tell the model to aim for RIGHT NOW.
    Persisted into generation_trace.extras / the daily snapshot log so the
    "did the clicks-weighted reweight actually shift picks" audit can replay
    point-in-time targets — clicks accrue retroactively, so the live numbers
    cannot be reconstructed cleanly from posts after the fact.
    """
    rows = compute_target_distribution(platform, context=context)
    return [
        {
            "style": r["style"],
            "pct": round(r["pct"], 1),
            "score": round(r.get("score", 0.0), 3),
            "avg_clicks": round(r.get("avg_clicks", 0.0), 3),
            "avg_cm": round(r.get("avg_cm", 0.0), 3),
            "avg_up": round(r.get("avg_up", 0.0), 3),
            "n": r["n"],
            "trusted": bool(r["trusted"]),
        }
        for r in rows
    ]


if __name__ == "__main__":
    import argparse
    import json as _json

    _parser = argparse.ArgumentParser(
        description="Engagement styles CLI (target distribution inspection)"
    )
    _sub = _parser.add_subparsers(dest="cmd")
    _td = _sub.add_parser(
        "target-distribution",
        help="Print the current per-style target pick distribution as JSON",
    )
    _td.add_argument("--platform", required=True)
    _td.add_argument("--context", default="posting", choices=["posting", "replying"])

    _pk = _sub.add_parser(
        "pick",
        help="Run pick_style_for_post() and print the assignment + prompt block",
    )
    _pk.add_argument("--platform", required=True)
    _pk.add_argument("--context", default="posting", choices=["posting", "replying"])
    _pk.add_argument("--top-n", type=int, default=CURATED_TOP_N)
    _pk.add_argument("--invent-rate", type=float, default=INVENT_RATE)
    _pk.add_argument("--seed", type=int, default=None,
                     help="Deterministic seed for the picker RNG")
    _pk.add_argument("--show-prompt", action="store_true",
                     help="Also print the compact prompt block the model would see")

    _args = _parser.parse_args()

    if _args.cmd == "target-distribution":
        print(_json.dumps(
            target_distribution_snapshot(_args.platform, context=_args.context),
            ensure_ascii=False,
        ))
    elif _args.cmd == "pick":
        _rng = random.Random(_args.seed) if _args.seed is not None else random
        _assignment = pick_style_for_post(
            _args.platform, context=_args.context,
            top_n=_args.top_n, invent_rate=_args.invent_rate, rng=_rng,
        )
        print(_json.dumps(_assignment, ensure_ascii=False, indent=2))
        if _args.show_prompt:
            print()
            print("=" * 60)
            print("PROMPT BLOCK")
            print("=" * 60)
            print(get_assigned_style_prompt(
                _args.platform, _assignment, context=_args.context))
    else:
        _parser.print_help()
