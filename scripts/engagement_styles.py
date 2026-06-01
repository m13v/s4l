#!/usr/bin/env python3
"""Shared engagement style definitions for all platforms.

Centralizes style taxonomy, platform-specific guidance, content rules,
and prompt generation so every pipeline (post_reddit, engage_reddit,
run-twitter-cycle, run-linkedin, engage-twitter, engage-linkedin) references
a single source of truth.

Usage:
    from engagement_styles import VALID_STYLES, REPLY_STYLES, get_styles_prompt, get_content_rules, get_anti_patterns

Style universe (post 2026-05-22 cleanup, second pass):
    The hardcoded STYLES dict is the curated baseline kept in-process so
    the picker still works on a cold-start machine with no DB access. The
    live "universe" is the union of STYLES + every row in the Postgres
    table `engagement_styles_registry` (read via the s4l.ai API).

    The registry table now carries THREE flavors discriminated by a `kind`
    column:
      - 'seed'           : curated, ships with the repo
      - 'model_invented' : created by register_style() when the orchestrator
                           proposes a new style inline via `new_style` JSON
      - 'human_derived'  : created once a day per platform by
                           scripts/generate_daily_human_style.py, distilled
                           from the top human replies in thread_top_replies

    The picker bypasses score-based selection with HUMAN_DERIVED_RATE
    probability per platform and asks the registry route for the latest
    active human-derived row on that platform.

    All reads/writes go through the s4l.ai /api/v1/engagement-styles/registry
    route. We never touch the DB directly from this module.
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
        "target_chars": 80,
    },
    "storyteller": {
        "description": (
            "Narrative-driven comment. Per the GROUNDING RULE, every "
            "storyteller comment picks ONE of two mutually exclusive lanes: "
            "Lane 1 (DISCLOSED STORY) opens with a hedge like "
            "'hypothetically', 'imagine someone running this', 'scenario:', "
            "'say a friend tried' and is then free to invent any specifics; "
            "Lane 2 (NO FABRICATION) keeps the narrative plain-voiced but "
            "every specific (numbers, durations, places, course names, "
            "brands, headcount) must appear verbatim in the matched "
            "project's content_angle / voice / messaging in config.json, "
            "otherwise drop the specifics or pattern-frame "
            "('the part that breaks down is...', 'the typical failure mode "
            "is...'). Lead with failure or surprise, not success. Whose "
            "voice tells the story (maker vs outside observer) is set by "
            "the VOICE RELATIONSHIP rule, not by this style."
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
            "invented specific as a lived fact ('ran this exact pipeline "
            "last semester for two anatomy blocks', 'ran 22 cameras across "
            "three properties for 8 months', 'sat 6 courses across three "
            "centers') without a Lane 1 opener and without config.json "
            "grounding is the exact failure mode the GROUNDING RULE forbids."
        ),
        "target_chars": 180,
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
        "target_chars": 80,
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
        "target_chars": 80,
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
        "target_chars": 80,
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
        "target_chars": 60,
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
        "target_chars": 45,
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
        "target_chars": 1800,
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
        "target_chars": 1800,
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
        "target_chars": 1650,
    },
}

# Valid tone styles. Same set for posting and replying: tone is a separate
# dimension from project-recommendation intent, which is now tracked on its
# own boolean column (posts.is_recommendation / replies.is_recommendation).
# REPLY_STYLES is kept as an alias for backwards compatibility with callers
# that historically treated it as a superset.
VALID_STYLES = set(STYLES.keys())
REPLY_STYLES = VALID_STYLES

# ── Registry-backed style universe (DB, not JSON) ──────────────────
#
# Cleanup 2026-05-22: every model-invented style lands in the Postgres
# table `engagement_styles_registry` via POST
# /api/v1/engagement-styles/registry. The legacy file-based sidecar
# (scripts/engagement_styles_extra.json) and the two-tier
# candidate→active promoter are GONE. Every install sees every other
# install's registered styles, and a new invention is live for the next
# picker tick on every install (no JSON file to ship).
#
# DB registry row shape (engagement_styles_registry):
#   {
#     "name": str (PK), "description": str, "example": str, "note": str,
#     "best_in": dict,                          # {platform: hint|bool|[..]}
#     "status": "active" | "retired",           # 'active' on every new row
#     "why_existing_didnt_fit": str | None,
#     "first_post_url": str | None,
#     "first_post_id": int | None,
#     "first_post_platform": str | None,
#     "invented_by_model": str | None,
#     "invented_at": ISO-8601 UTC,
#     "promoted_at": ISO-8601 UTC,              # set = invented_at on new rows
#     "created_at" / "updated_at": ISO-8601 UTC,
#   }
#
# The seeds in STYLES{} are kept in-process as a cold-start fallback so
# the picker works on a machine with no DB access; they're also seeded
# into the table via scripts/migrate_engagement_styles_to_db.py.

_REQUIRED_NEW_STYLE_FIELDS = ("description", "example", "why_existing_didnt_fit")

# In-process cache for registry reads. ~5 min keeps the picker from
# hammering the API on every pick (a Twitter cycle picks ~20 times per
# 15-minute window) while still surfacing a newly-invented style from
# another install within one window.
_REGISTRY_CACHE = {"ts": 0.0, "rows": None}
_REGISTRY_CACHE_TTL_SEC = 300


def _normalize_entry(entry, default_status="active", default_kind="seed"):
    """Ensure a STYLES-style dict has the fields callers expect."""
    out = dict(entry) if isinstance(entry, dict) else {}
    out.setdefault("status", default_status)
    out.setdefault("description", "")
    out.setdefault("example", "")
    out.setdefault("note", "")
    out.setdefault("best_in", {})
    # Authoritative per-style target comment length. Falls back to the
    # short-biased default for legacy rows / cold-start entries that predate
    # the target_chars column.
    try:
        out["target_chars"] = int(out.get("target_chars") or DEFAULT_TARGET_CHARS)
    except (TypeError, ValueError):
        out["target_chars"] = DEFAULT_TARGET_CHARS
    # kind discriminates origin: 'seed' (hardcoded/top-performer), 'model_invented'
    # (Claude proposed during a posting run), 'human_derived' (synthesized from
    # the daily human-reply digest). Surfaced in the dashboard as a bracket
    # next to the style name.
    out.setdefault("kind", default_kind)
    return out


def _fetch_registry_styles(force_refresh=False):
    """Pull every active row in engagement_styles_registry via the API.

    Returns {name: {description, example, note, best_in, status, ...}}.
    Cached for _REGISTRY_CACHE_TTL_SEC; pass force_refresh=True to bust.

    Best-effort: returns {} on any error (API unreachable, missing env,
    cold start) so callers can fall back to the in-process STYLES dict.
    """
    import time as _time
    now = _time.time()
    if (
        not force_refresh
        and _REGISTRY_CACHE["rows"] is not None
        and (now - _REGISTRY_CACHE["ts"]) < _REGISTRY_CACHE_TTL_SEC
    ):
        return _REGISTRY_CACHE["rows"]

    try:
        import sys as _sys
        _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from http_api import api_get
        resp = api_get("/api/v1/engagement-styles/registry", {"status": "active"})
        data = (resp or {}).get("data") or {}
        rows = data.get("styles") or []
    except Exception:
        # Don't poison the cache with an empty on transient failure: if we
        # had data before, keep serving it.
        if _REGISTRY_CACHE["rows"] is not None:
            return _REGISTRY_CACHE["rows"]
        return {}

    out = {}
    for r in rows:
        name = r.get("name")
        if not name:
            continue
        best_in = r.get("best_in") or {}
        if isinstance(best_in, str):
            try:
                best_in = json.loads(best_in)
            except Exception:
                best_in = {}
        # Default kind for legacy rows pre-consolidation is 'seed'; the
        # 2026-05-22 migration backfilled invented_by_model<>null rows to
        # 'model_invented' and the human_derived migration inserted those
        # explicitly. Trust the column.
        out[name] = {
            "description": r.get("description") or "",
            "example": r.get("example") or "",
            "note": r.get("note") or "",
            "best_in": best_in,
            "status": r.get("status") or "active",
            "kind": r.get("kind") or "seed",
            "invented_by_model": r.get("invented_by_model"),
            "invented_at": r.get("invented_at"),
            "promoted_at": r.get("promoted_at"),
            "first_post_url": r.get("first_post_url"),
            "first_post_platform": r.get("first_post_platform"),
            "why_existing_didnt_fit": r.get("why_existing_didnt_fit") or "",
            "target_chars": r.get("target_chars") or DEFAULT_TARGET_CHARS,
        }
    _REGISTRY_CACHE["rows"] = out
    _REGISTRY_CACHE["ts"] = now
    return out


def get_all_styles():
    """Merged universe: hardcoded STYLES + registry rows + human-derived rows.

    Reads pull from the live Postgres registry (cached briefly), so a
    style invented by any install is visible to every other install on
    the next picker tick. STYLES{} is the cold-start fallback when the
    API is unreachable.

    Merge order (later wins on duplicate name):
        1. Hardcoded STYLES (cold-start floor)
        2. engagement_styles_registry rows (the live source of truth,
           includes kind in {'seed','model_invented','human_derived'})
        3. Same registry filtered to kind='human_derived' (only for names
           not already in 1/2; pure defense-in-depth — under normal
           operation step 2 already returned every row, so step 3 is a
           no-op. Kept for the case where _fetch_registry_styles failed
           but _load_human_derived_styles succeeded.)

    Caller MUST treat the returned dict as read-only.
    """
    merged = {
        name: _normalize_entry(meta, "active", "seed")
        for name, meta in STYLES.items()
    }
    for name, meta in _fetch_registry_styles().items():
        if not isinstance(meta, dict):
            continue
        # Trust the kind column the registry already set; fall back to 'seed'.
        merged[name] = _normalize_entry(meta, "active", meta.get("kind") or "seed")
    for name, meta in _load_human_derived_styles().items():
        if name in merged:
            # Don't clobber a curated/registry entry if the synthesizer
            # happens to pick a colliding snake_case name.
            continue
        merged[name] = _normalize_entry(meta, "active", "human_derived")
    return merged


def _load_human_derived_styles():
    """Map of {name: {description, example, note, best_in}} for every
    active human_derived row in engagement_styles_registry.

    Reads via the /api/v1/engagement-styles/registry route filtered by
    kind=human_derived. Best-effort; returns {} on any failure so callers
    don't have to wrap in try/except. Defense-in-depth alongside
    _fetch_registry_styles(): if the synthesizer ever names a row the same
    as an existing seed, get_all_styles() will already have the seed and
    skip the human_derived entry.
    """
    try:
        import sys as _sys
        _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from http_api import api_get
        resp = api_get(
            "/api/v1/engagement-styles/registry",
            {"status": "active", "kind": "human_derived"},
        )
        data = (resp or {}).get("data") or {}
        rows = data.get("styles") or []
    except Exception:
        return {}
    out = {}
    for r in rows:
        name = r.get("name")
        if not name:
            continue
        best_in = r.get("best_in") or {}
        if isinstance(best_in, str):
            try:
                best_in = json.loads(best_in)
            except Exception:
                best_in = {}
        out[name] = {
            "description": r.get("description") or "",
            "example": r.get("example") or "",
            "note": r.get("note") or "",
            "best_in": best_in,
            "target_chars": r.get("target_chars") or DEFAULT_TARGET_CHARS,
        }
    return out


def register_style(name, meta, source_post=None):
    """Register a model-invented style into engagement_styles_registry.

    Called when an orchestrator parses a decision JSON whose
    engagement_style is not in get_all_styles() and whose `new_style`
    block is well-formed.

    POSTs to /api/v1/engagement-styles/registry; the server upserts the
    row (ON CONFLICT DO NOTHING on name). Concurrency is handled at the
    Postgres layer (PK uniqueness), so we don't need a file lock anymore.

    Args:
        name: the style name the model picked.
        meta: dict with at least description/example/why_existing_didnt_fit
              (and optionally note). Anything else is preserved verbatim.
        source_post: optional dict {platform, post_url, post_id, model}
              describing the post that birthed this style. Recorded only
              the FIRST time a name is registered (server-side ON CONFLICT
              keeps the original values).

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

    # Cheap local short-circuit: if our cached registry already has this
    # name, skip the network call and return existing immediately. The
    # cache is shared across calls within the same process so this saves
    # one HTTP round-trip per duplicate invention attempt.
    cached = _fetch_registry_styles()
    if name in cached:
        return "existing", cached[name]

    src = source_post or {}
    # Coerce the model-declared target length. The invent prompt requires a
    # target_chars in the new_style block, but we default gracefully rather
    # than reject an otherwise-valid invention just because the length is
    # missing or garbage. Clamp to a sane 20..2200 band.
    try:
        _tc = int(meta.get("target_chars"))
        target_chars = max(20, min(2200, _tc))
    except (TypeError, ValueError):
        target_chars = DEFAULT_TARGET_CHARS
    # 2026-05-25: explicitly stamp kind='model_invented' on the payload.
    # The server route (social-autoposter-website/src/app/api/v1/engagement-styles/
    # registry/route.ts:220-234) defaults kind to 'seed' when invented_by_model
    # is empty, and to 'model_invented' otherwise. Most callers above don't
    # populate source_post["model"] (it's optional), so the server fallback
    # silently buries every invention under kind='seed' — making the
    # model_invented bucket forever empty. Sending kind explicitly bypasses
    # the heuristic entirely; register_style() is ONLY called from the
    # invent path (see validate_or_register at line ~542), so the label is
    # always correct here.
    payload = {
        "name": name,
        "kind": "model_invented",
        "description": meta["description"].strip(),
        "example": meta["example"].strip(),
        "note": (meta.get("note") or "").strip(),
        "why_existing_didnt_fit": meta["why_existing_didnt_fit"].strip(),
        "first_post_url": src.get("post_url"),
        "first_post_id": src.get("post_id"),
        "first_post_platform": src.get("platform"),
        "invented_by_model": src.get("model"),
        "best_in": {},
        "target_chars": target_chars,
    }
    try:
        import sys as _sys
        _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from http_api import api_post
        resp = api_post("/api/v1/engagement-styles/registry", payload)
    except SystemExit as e:
        return "rejected", {"error": f"registry POST failed: {e}"}
    except Exception as e:
        return "rejected", {"error": f"registry POST raised: {e}"}

    data = (resp or {}).get("data") or {}
    style_row = data.get("style") or {}
    created = bool(data.get("created"))

    # Bust the local cache so the very next get_all_styles() includes this
    # row (otherwise the picker's coerce-or-validate pass would still
    # reject it for the next ~5 minutes).
    _REGISTRY_CACHE["ts"] = 0.0
    _REGISTRY_CACHE["rows"] = None

    entry = _normalize_entry(
        {
            "description": style_row.get("description") or payload["description"],
            "example": style_row.get("example") or payload["example"],
            "note": style_row.get("note") or payload["note"],
            "best_in": style_row.get("best_in") or {},
            "status": style_row.get("status") or "active",
            "target_chars": style_row.get("target_chars") or payload["target_chars"],
        },
        "active",
    )
    return ("new" if created else "existing"), entry


def validate_or_register(decision, source_post=None, context="posting",
                         assigned_style=None, assigned_mode=None):
    """One-shot helper for orchestrators that parse a decision JSON.

    Reads decision["engagement_style"] (and optional decision["new_style"]).
    Returns (style_or_None, action) where action is one of:
        "valid"      → style is in the universe, accept it
        "coerced"    → USE-mode picker assigned a specific style and the
                       model drifted to something else; we silently coerce
                       back to the assigned style (drift-protection)
        "registered" → INVENT-mode + well-formed new_style → registered
                       in the DB registry, accept it
        "rejected"   → unknown style and no usable new_style, OR drift in
                       a context where the assigned style is known but the
                       model neither used it nor shipped a valid new_style
        "passthrough"→ no assignment context (legacy caller); same as
                       valid/registered/rejected branches but logged
                       distinctly

    `assigned_style` / `assigned_mode` (added 2026-05-22): when the caller
    used pick_style_for_post() it now passes the assignment back in. We
    use it to (a) coerce drift in USE mode back to the assigned name
    (eliminating the "model picks pattern_recognizer because it's
    generic" bias), and (b) only allow invention when the picker
    actually asked for it (INVENT mode). This closes the enforcement gap
    where any rail could silently invent a style outside the assigned
    path.

    Logs the action to stdout for the orchestrator's run log.
    """
    style = decision.get("engagement_style") if isinstance(decision, dict) else None
    new_style = decision.get("new_style") if isinstance(decision, dict) else None

    # USE-mode drift protection: picker assigned a style, model picked
    # something else. Don't trust the model's "improvement" — coerce
    # back. Inventions in USE mode are not allowed; the assigned style
    # already exists, so any new_style block here is the model
    # over-reaching.
    if assigned_mode == "use" and assigned_style:
        if style == assigned_style:
            return style, "valid"
        universe = get_all_styles()
        if style and style in universe and style != assigned_style:
            print(
                f"[engagement_styles] DRIFT in USE mode: model returned "
                f"{style!r} but picker assigned {assigned_style!r}; "
                f"coercing back."
            )
            return assigned_style, "coerced"
        # Unknown style or no style — also coerce back to assigned. The
        # assigned style is guaranteed to be in the universe (picker
        # built it from get_all_styles()).
        print(
            f"[engagement_styles] DRIFT in USE mode: model returned "
            f"{style!r} (not in universe); coercing to assigned "
            f"{assigned_style!r}."
        )
        return assigned_style, "coerced"

    # INVENT-mode: picker explicitly asked for a new style. Require a
    # well-formed new_style block; if model returned an existing style
    # name, accept it as if INVENT had landed on something already in
    # the registry (rare but harmless).
    if assigned_mode == "invent":
        if style and style in get_all_styles() and not isinstance(new_style, dict):
            return style, "valid"
        if not style or not isinstance(new_style, dict):
            print(
                f"[engagement_styles] INVENT mode but model returned "
                f"style={style!r} new_style_block={bool(new_style)}; "
                f"rejecting."
            )
            return None, "rejected"
        status, entry = register_style(style, new_style, source_post)
        if status == "rejected":
            print(
                f"[engagement_styles] new_style for {style!r} rejected: "
                f"{entry.get('error')}"
            )
            return None, "rejected"
        if status == "new":
            src_url = (source_post or {}).get("post_url", "?")
            print(
                f"[engagement_styles] REGISTERED new style {style!r} "
                f"from {src_url}"
            )
        return style, "registered"

    # Legacy callers (no assignment context): behave as before, with the
    # caveat that any silent invention will create an `active` registry
    # row instead of a `candidate` sidecar entry.
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
        print(f"[engagement_styles] REGISTERED new style {style!r} from {src_url}")
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

# Minimum sample size to count a style as trusted. n=1 means "at least one
# real post in the 30-day window". 2026-05-25: lowered from 5 → 1 so an
# invented style that produced even a single post (small batch, partial-batch
# failure, etc.) competes on per-post score from day one. The 30-day recency
# window is the only freshness gate; ghost styles (n=0 registry rows) stay
# excluded from the use_pool by this floor.
MIN_SAMPLE_SIZE = 1

# Legacy target-distribution knobs. UNUSED since 2026-05-29: the picker
# (pick_style_for_post) samples on raw _picker_score with no exponent / floor /
# cap, and compute_target_distribution now reports that same true pick
# probability instead of a sharpened-floored-capped target. These only ever
# shaped the DISPLAY, which diverged hard from what actually got picked. Kept
# defined (not deleted) so any external import doesn't break.
WEIGHT_EXPONENT = 2.0   # (legacy, unused)
STYLE_FLOOR_PCT = 5.0   # (legacy, unused)
STYLE_CAP_PCT = 50.0    # (legacy, unused)

# Picker weight floor: mirrors max(_picker_score(r), 0.01) in
# pick_style_for_post so a score-0 trusted style still draws a tiny nonzero
# pick chance instead of being frozen out entirely.
PICK_FLOOR = 0.01


# Recency window for the picker target distribution. Lifetime aggregation
# drifted too far from current performance reality (e.g. 2025 wins from
# pattern_recognizer kept it in the pool even after 2026's audience shift).
# 2026-05-29: tightened 30 -> 7 days across all platforms (per user). The 30d
# window lagged badly: pattern_recognizer showed 963 posts / 24% volume at 30d
# but only 6 posts at 7d because the picker had already abandoned it; the 30d
# snapshot kept dragging dead historical volume. 7 days tracks the live picker
# much more tightly. Tradeoff: tail styles drop below the n>=50 density the 30d
# window held, so they fall back on the explore floor sooner, and low-volume /
# scrape-lagged platforms (LinkedIn) may cold-start to an equal split until 7d
# of data accumulates. Set RECENCY_DAYS=0 to fall back to lifetime.
RECENCY_DAYS = 7


def _fetch_style_stats(platform, days=None):
    """Query the autoposter API for per-engagement_style performance.

    Returns a dict:
        {style_name: {"n": int, "avg_up": float, "avg_cm": float,
                      "avg_clicks": float}}
    avg_up is NET (Reddit/Moltbook self-upvote stripped); avg_clicks is the
    bot-filtered click count. The three combine into the same composite the
    top_performers report ranks on. Returns {} on any error (API unreachable,
    missing env, cold start).

    Recency: `days` overrides the module-level RECENCY_DAYS (default 7).
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
# middle. The picker weights styles LINEARLY by this score (no exponent,
# no shrinkage) so the style that actually drives clicks wins proportionally
# more picks, not the one that merely accumulates passive likes.
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
        # Every style with N >= MIN_SAMPLE_SIZE can be trusted. The legacy
        # two-tier candidate→active gate was removed 2026-05-22 and the
        # picker's sample-size shrinkage was removed 2026-05-25: invented
        # styles now compete on raw per-post score from day one, protected
        # only by the MIN_SAMPLE_SIZE=1 existence floor (excludes n=0 ghost
        # styles) and the 30-day recency window.
        if s and s["n"] >= MIN_SAMPLE_SIZE:
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
    """Per-style pick probability, mirroring the live picker exactly.

    Returns a list of dicts sorted by pct DESC:
        [{"style", "pct", "n", "avg_up", "avg_cm", "avg_clicks", "score",
          "trusted", "is_candidate", "weight"}]

    `pct` is the probability that pick_style_for_post() assigns this style to a
    given post, so the UI / snapshot now matches what actually happens:
      - Styles in PLATFORM_POLICY[platform].never are excluded.
      - score = clicks*10 + comments*3 + upvotes_net (the top_performers
        composite). A real human click is the ground-truth conversion signal;
        upvotes are passive vibes.
      - The scored-use path (probability = 1 - INVENT_RATE - human_derived_rate)
        samples across TRUSTED styles weighted LINEARLY by max(score,PICK_FLOOR)
        — the exact weights pick_style_for_post() builds. So a trusted style's
        pct = scored_use_fraction * max(score,PICK_FLOOR) / sum_trusted_weights.
      - Non-trusted styles (n=0 ghost registry rows) are never on the use path,
        so pct = 0. They can still surface via the invent-mode reference list.
      - The leftover INVENT_RATE + human_derived_rate (~10%) is NOT attributed
        to any fixed style (invent mints a new one, human_derived picks the
        latest synthesized row), so trusted pcts sum to scored_use_fraction*100.
      - Cold start (no trusted data): the picker always invents, so we show an
        equal share across the non-never explore universe.

    2026-05-29: replaced the legacy floor/cap/exponent target math
    (WEIGHT_EXPONENT / STYLE_FLOOR_PCT / STYLE_CAP_PCT). That predated the
    2026-05-28 switch to raw linear score sampling in pick_style_for_post and
    diverged hard: with ~56 styles the 5% floor over-subscribed to ~245% and
    zeroed the real winners, so the displayed % bore no relation to picks.
    """
    never = set(PLATFORM_POLICY.get(platform, {}).get("never", []))
    universe = get_all_styles()
    candidates = [s for s in universe.keys() if s not in never]
    stats = _fetch_style_stats(platform)

    rows = []
    trusted_weight_sum = 0.0
    for style in candidates:
        s = stats.get(style)
        n = int(s["n"]) if s else 0
        avg_up = float(s["avg_up"]) if s else 0.0
        avg_cm = float(s.get("avg_cm", 0.0)) if s else 0.0
        avg_clicks = float(s.get("avg_clicks", 0.0)) if s else 0.0
        score = _style_score(s) if s else 0.0
        # Trusted = at least one real post in the recency window
        # (MIN_SAMPLE_SIZE=1 excludes only n=0 ghost registry rows). The
        # picker's use-path weight is max(_picker_score, PICK_FLOOR) with NO
        # exponent and NO sample-size shrinkage, so we mirror it verbatim.
        trusted = (s is not None and n >= MIN_SAMPLE_SIZE)
        weight = max(score, PICK_FLOOR) if trusted else 0.0
        if trusted:
            trusted_weight_sum += weight
        rows.append({"style": style, "n": n, "avg_up": avg_up,
                     "avg_cm": avg_cm, "avg_clicks": avg_clicks,
                     "score": score, "trusted": trusted,
                     "weight": weight, "pct": 0.0,
                     "is_candidate": False})

    if not rows:
        return []

    # Cold start: no trusted data -> the picker always invents. Show an equal
    # share across the explore universe (the invent-mode reference pool).
    if trusted_weight_sum <= 0:
        share = 100.0 / len(rows)
        for r in rows:
            r["pct"] = share
        rows.sort(key=lambda r: r["style"])
        return rows

    # Scored-use fraction: the picker spends INVENT_RATE on invention and
    # _human_derived_rate(platform) on the latest human-derived style BEFORE
    # the scored sample runs, so trusted styles share only the remainder.
    scored_use_fraction = max(
        0.0, 1.0 - INVENT_RATE - _human_derived_rate(platform)
    )
    for r in rows:
        if r["trusted"]:
            r["pct"] = (
                (r["weight"] / trusted_weight_sum)
                * scored_use_fraction * 100.0
            )
        else:
            r["pct"] = 0.0

    rows.sort(key=lambda r: r["pct"], reverse=True)
    return rows


# ── Programmatic picker (2026-05-19) ────────────────────────────────
#
# The old flow ("show the model 9 styles + target % and let it pick") was
# soft: the model anchored on the most-generic-fit style (pattern_recognizer)
# and over-picked it ~30% of posts even when its target % was the 5% floor.
# This picker flips the contract: code picks ONE style by weighted sample
# across all trusted styles (weighted by composite score), the prompt assigns
# that style, and the model only authors the comment. Higher-scoring styles
# win proportionally more picks while the whole pool stays eligible, so styles
# auto-rotate as their click-weighted score shifts. The
# model can still invent: with probability INVENT_RATE the picker returns
# mode="invent" and the prompt hands the model the top N as reference
# material to derive a new style from.

INVENT_RATE = 0.05  # ~1 in 20 posts forces a new-style invention
CURATED_TOP_N = 5   # size of the invent-mode reference list (top 5 by score)

# Fallback target comment length (chars) for any style that lacks an explicit
# target_chars (legacy DB rows, cold-start before the registry is reachable).
# Set just above the top-human-reply median (~74) so the long tail of styles
# defaults SHORT, not to our historical ~215-char bloat. Mirrors the DB column
# default in migrations/2026-05-30-engagement-styles-target-chars.sql.
DEFAULT_TARGET_CHARS = 80

# Additive ~5% branch per platform (2026-05-22, second pass): with this
# probability, the picker bypasses score-based selection and assigns the
# most recently synthesized "human-derived" style on the calling platform.
# Those rows are distilled by scripts/generate_daily_human_style.py from
# the previous 24h of top-performing HUMAN replies in thread_top_replies
# and live in engagement_styles_registry with kind='human_derived' (one
# row per platform per day). Goal: keep our voice continuously calibrated
# to whatever rhetorical move is winning on each platform right now,
# without waiting for the historical scoring window to accumulate enough
# samples to surface it naturally.
#
# Distribution per platform: HUMAN_DERIVED_RATE_BY_PLATFORM[platform] +
# INVENT_RATE + scored-use (defaults: 5% + 5% + 90%).
#
# Rate is a per-platform dict so we can tune individually. A platform
# missing from the dict defaults to HUMAN_DERIVED_RATE_DEFAULT. Set the
# entry to 0 to disable the branch for one platform (e.g. while the
# synthesizer is bootstrapping data for that platform).
HUMAN_DERIVED_RATE_DEFAULT = 0.05
HUMAN_DERIVED_RATE_BY_PLATFORM = {
    "twitter": 0.05,
    "reddit": 0.05,
    "github": 0.05,
    "moltbook": 0.05,
    "linkedin": 0.05,
}


def _human_derived_rate(platform):
    """Per-platform rate; falls back to HUMAN_DERIVED_RATE_DEFAULT."""
    return HUMAN_DERIVED_RATE_BY_PLATFORM.get(platform, HUMAN_DERIVED_RATE_DEFAULT)

# Credibility shrinkage was removed 2026-05-25 (per user instruction): we
# don't favor or penalize styles by post-count, only by per-post performance.
# Existence floor is MIN_SAMPLE_SIZE=1 (defined above): n=0 ghost registry
# rows are excluded, single-post inventions count. RECENCY_DAYS=7 remains
# the only freshness gate.


def _picker_score(row):
    """Picker score = raw composite score (clicks*10 + comments*3 + upvotes).

    No sample-size shrinkage. A style with n=1 averaging 50 clicks/post
    competes head-to-head with a style at n=400 averaging 5 clicks/post,
    and the better per-post style wins the slot. The MIN_SAMPLE_SIZE=1
    floor inside compute_target_distribution only excludes n=0 ghosts."""
    return float(row.get("score", 0.0))


def _fetch_latest_human_derived(platform):
    """Return the most recently synthesized active human-derived style for
    `platform`, or None if the registry has none for that platform / the
    API is unreachable.

    Reads via /api/v1/engagement-styles/registry?kind=human_derived
    &platform=<platform>&latest=1. The route returns 0 or 1 rows ordered
    by generated_at DESC.

    Network failures are swallowed silently and return None so the picker
    falls through to the normal scored path. This branch is best-effort,
    never load-bearing.
    """
    try:
        import sys as _sys
        _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from http_api import api_get
        resp = api_get(
            "/api/v1/engagement-styles/registry",
            {
                "status": "active",
                "kind": "human_derived",
                "platform": platform,
                "latest": "1",
            },
        )
        data = (resp or {}).get("data") or {}
        rows = data.get("styles") or []
    except Exception:
        return None
    if not rows:
        return None
    row = rows[0]
    best_in = row.get("best_in") or {}
    if isinstance(best_in, str):
        try:
            best_in = json.loads(best_in)
        except Exception:
            best_in = {}
    return {
        # Registry rows use `name` as the primary key, so there's no
        # standalone numeric id; expose name as the stable identifier.
        # Callers used to expect `human_derived_id` for log attribution.
        "id": row.get("name"),
        "name": row.get("name"),
        "description": row.get("description") or "",
        "example": row.get("example") or "",
        "best_in": best_in,
        "note": row.get("note") or "",
        "target_chars": row.get("target_chars") or DEFAULT_TARGET_CHARS,
        "generated_at": row.get("generated_at"),
        "platform": row.get("platform"),
    }


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

    The scored-use pick samples across ALL trusted styles, weighted by raw
    composite score (clicks*10 + comments*3 + upvotes_net), so every style
    keeps a performance-proportional chance and none is frozen out. top_n
    bounds only the invent-mode reference list. Styles in
    PLATFORM_POLICY.never are excluded.
    Sidecar candidate styles (status="candidate") are excluded from the
    use path until the promoter graduates them, but stay available as
    invent-mode references once graduated.

    Args:
        platform: "reddit" | "twitter" | "linkedin" | "github" | "moltbook"
        context: "posting" | "replying"
        top_n: size of the invent-mode reference list (top N by score). The
               scored-use pick samples across all trusted styles, not just N.
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
            "target_chars": int | None,           # authoritative length; None on invent
            "reference_styles": [                 # top-N meta (always populated)
                {"style", "description", "example", "note", "target_chars",
                 "score", "pct", "n", "avg_clicks", "avg_cm", "avg_up"},
                ...
            ],
            "distribution_snapshot": list,         # full target distribution at pick time
            "picked_at": ISO-8601 UTC,
        }
    """
    rnd = rng or random

    # Human-derived branch (2026-05-22, second pass): on every platform,
    # with HUMAN_DERIVED_RATE_BY_PLATFORM[platform] probability, bypass the
    # score-based path entirely and assign the most recently synthesized
    # human_derived style for that platform from engagement_styles_registry
    # (read via the s4l.ai /api/v1/engagement-styles/registry route).
    #
    # ADDITIVE to the existing INVENT branch — both rates coexist on a
    # platform-by-platform basis, leaving the remainder for normal
    # scored-use. Fails open: if the route returns no active row for this
    # platform, we fall through to the normal flow as if this branch
    # didn't exist.
    _hd_rate = _human_derived_rate(platform)
    if _hd_rate > 0 and rnd.random() < _hd_rate:
        hd = _fetch_latest_human_derived(platform)
        if hd:
            return {
                "mode": "use",
                "style": hd["name"],
                "description": hd["description"],
                "example": hd["example"],
                "note": hd["note"],
                "target_chars": hd.get("target_chars") or DEFAULT_TARGET_CHARS,
                "source": "human_derived",
                "human_derived_id": hd["id"],
                "reference_styles": [],
                "distribution_snapshot": [],
                "picked_at": datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                ),
            }
        # else fall through to normal scored path.

    never = set(PLATFORM_POLICY.get(platform, {}).get("never", []))
    rows = compute_target_distribution(platform, context=context)
    rows = [r for r in rows if r["style"] not in never]

    # Trust-filter first so n=0 ghost registry rows can't claim a top
    # reference slot. They stay in distribution_snapshot for audit but not
    # in use_pool or reference_pool. (MIN_SAMPLE_SIZE=1 since 2026-05-25;
    # single-post inventions are trusted from day one.)
    #
    # Ranking uses raw `score` (clicks*10 + comments*3 + upvotes) with no
    # sample-size shrinkage — a genuinely better per-post style outranks
    # established ones from its first post.
    trusted_rows = [r for r in rows if r.get("trusted")]
    trusted_sorted = sorted(trusted_rows, key=_picker_score, reverse=True)
    # Use path samples across ALL trusted styles, weighted by raw score
    # (2026-05-28, per user request). The old top_n cutoff froze out every
    # style ranked beyond top_n: it could never earn a post back, so its
    # 30-day sample aged out to n=0 and it dropped from the pool for good.
    # Now every trusted style keeps a performance-proportional chance; the
    # highest scorer still wins the most picks, while score-0 styles draw
    # ~0 via the 0.01 floor below. top_n now bounds ONLY the invent-mode
    # reference list (reference_pool), not the scored-use pool.
    use_pool = trusted_sorted
    reference_pool = trusted_sorted[:top_n]

    universe = get_all_styles()

    def _meta_for(row):
        m = universe.get(row["style"], {})
        return {
            "style": row["style"],
            "description": m.get("description", ""),
            "example": m.get("example", ""),
            "note": m.get("note", ""),
            "target_chars": m.get("target_chars") or DEFAULT_TARGET_CHARS,
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
            "target_chars": None,
            "reference_styles": reference_styles,
            "distribution_snapshot": distribution_snapshot,
            "picked_at": picked_at,
        }

    # Use path: weighted random sample by raw score across ALL trusted styles
    # (filtered by MIN_SAMPLE_SIZE=1 and the 30-day recency window). Linear
    # score weighting, so each style competes on per-post performance: the
    # head wins most picks while the tail keeps a proportional, nonzero chance.
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
        "target_chars": meta.get("target_chars") or DEFAULT_TARGET_CHARS,
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
        _tc = assignment.get("target_chars") or DEFAULT_TARGET_CHARS
        _ceil = int(round(_tc * 1.5))
        lines.append("")
        lines.append(
            f"**HARD LENGTH LIMIT: {_ceil} characters, absolute max. Target ~{_tc}.**\n"
            f"This is non-negotiable. Count the characters in your comment before "
            f"you return it. If it exceeds {_ceil} characters, CUT IT DOWN until "
            f"it fits. The length THIS style wins at is ~{_tc} chars; aim there, "
            f"and coming in UNDER is always better than over.\n"
            f"- The top human replies that actually get engagement are fragments "
            f"and single lines, not tidy two-clause sentences. One sharp sentence "
            f"beats a paragraph every time.\n"
            f"- The example above demonstrates TONE and ANGLE, not length. Do NOT "
            f"match its length; many of our stored examples are too long. Match "
            f"its voice in far fewer words.\n"
            f"- This limit is for the COMMENT TEXT ONLY. Any link/CTA the system "
            f"appends afterward is separate and does not count against your budget, "
            f"so do NOT pad the comment to 'make room' for or to introduce a link. "
            f"Write the comment as if no link will follow."
        )
        lines.append("")
        lines.append(
            'In your output JSON, set "engagement_style" to exactly '
            f'"{assignment["style"]}" and leave "new_style" as null. '
            'Do not substitute a different style. The picker has already '
            'made the choice based on live performance data; your job is '
            'to author a great comment in that style, not to second-guess '
            'the assignment. If you return any other style name, the '
            'orchestrator silently coerces it back to '
            f'"{assignment["style"]}" before logging.'
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
                f"cm {ref['avg_cm']:.2f}, up {ref['avg_up']:.2f}, n={ref['n']}, "
                f"target ~{ref.get('target_chars') or DEFAULT_TARGET_CHARS} chars)"
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
        lines.append(
            f"  - target_chars: integer, the comment length this style wins "
            f"at. Bias SHORT — the top human replies cluster near {DEFAULT_TARGET_CHARS} "
            f"chars and below. Only go high (150+) if the style is genuinely "
            f"narrative; never propose a target just to fill space."
        )

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

    Purely about FABRICATION of specifics, not about whose voice is
    speaking. Voice positioning (whether the narrator is the maker of
    the matched project or an outside observer) is governed separately
    by get_voice_relationship_rule(), which reads the matched project's
    voice_relationship field in config.json.

    Every comment picks ONE of two mutually exclusive lanes. They cannot
    be combined: a comment can disclose a worked example OR keep its
    specifics grounded, never present a fabricated specific as a lived
    fact.

      LANE 1 - DISCLOSED STORY: invent freely, but flag the frame.
        Open with a hedge that signals the story is illustration, not
        testimony ('hypothetically', 'imagine someone running this',
        'say a friend tried', 'as a thought experiment', 'scenario:').
        Once the frame is set, free to use any names, durations, counts,
        places. The reader can tell from the first phrase that this is
        a worked example, not a lived claim.

      LANE 2 - NO FABRICATION: stay plain-voiced, but every specific
        (number, duration, date, place, course/program, headcount,
        named tool, named person) must appear verbatim in the matched
        project's content_angle / voice / messaging in config.json. If a
        specific is not in config: drop it, generalize ('a few months',
        'a handful of cameras'), or pattern-frame ('the part that breaks
        down is...'). Pattern-frame counts as observation, not lived
        testimony, so no disclosure is needed.

    Pick the lane that fits the thread. Niche / expert subs reward Lane 2;
    casual / advice subs tolerate Lane 1 when the disclosure feels natural.
    """
    return """## GROUNDING RULE (highest priority; overrides any other style guidance)

This rule is about FABRICATION of specifics. Whose voice is speaking
(maker vs outside observer) is governed by the VOICE RELATIONSHIP rule,
not here.

Every comment picks ONE lane. The two lanes are mutually exclusive: you
cannot present an invented detail as a lived fact. Choose:

LANE 1 - DISCLOSED STORY (invent freely, but flag it)
  Open with a phrase that signals the story is illustration, not lived
  testimony. Once the frame is set, use whatever names / durations /
  counts / places fit the point.
  Acceptable openers: "hypothetically", "imagine someone running this",
  "say a friend tried", "as a thought experiment", "scenario:",
  "to make this concrete, picture", "made-up example but".
  After the opener, full creative license on the details.

LANE 2 - NO FABRICATION (specifics must be real)
  Stay plain-voiced. Any specific (number, duration, date, place name,
  course/program, headcount, named tool, named person) is allowed ONLY
  if it appears verbatim in the matched project's content_angle, voice
  (tone/examples/examples_good), or messaging (lead_with_pain / solution
  / proof) in config.json. If a specific is not in config: drop it,
  generalize ("a few months", "a handful of cameras"), or pattern-frame
  ("the part that breaks down is...", "the typical failure mode is...").
  Pattern-frame counts as observation, not lived testimony, so no
  disclosure is needed.

NEVER MIX: do not write "ran 22 cameras for 8 months" without either
(a) a Lane 1 opener in front of it, or (b) those numbers being in
config.json. That is the failure mode this rule exists to kill.

Worked examples (drawn from real posts in our DB):

  BAD (fabricated anecdote, no disclosure, no config anchor):
    "ran this exact pipeline last semester for two anatomy blocks,
    cheap recorder into whisper into gpt into anki, raw gpt got
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
    "ran 22 cameras across three properties for about 8 months and
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

  GOOD LANE 2 (config-grounded, specifics are real):
    "on a 90-slide deck the eval scored 81.3 vs the field average
    around 68; the cards weren't the bottleneck, the rubric was."
    (The 81.3 / 68 / 90-slide numbers come from the matched project's
    messaging.proof and voice.examples.)

This rule outranks "specificity is the #1 authenticity signal" wherever
they conflict. Specificity still wins, but only via Lane 1 disclosure
or Lane 2 config grounding."""


def get_voice_relationship_rule():
    """Voice positioning rule, driven by each project's voice_relationship.

    Communicates how the narrator should relate to the matched project's
    product. The relationship is a structural property of the project
    (whether @m13v_ / @matt_diak speaks AS the maker or as an outside
    observer), declared in config.json per project; the prompt does not
    hardcode it.

    This rule is independent of the GROUNDING RULE: grounding governs
    whether SPECIFICS are real, this rule governs whose VOICE is speaking
    when those specifics are stated.
    """
    return """## VOICE RELATIONSHIP (highest priority; overrides any default voice in the styles block)

For EACH candidate, look up the matched project's `voice_relationship`
field in its config.json entry. The field is always one of two values
and dictates whose voice you write in. There is no third option and no
default; every project declares the field explicitly.

- `voice_relationship: "first_party"`
  You speak AS the builder / maker of this product. First-person plural
  about the product ("we ship X", "our Y", "we built Z") is allowed
  when grounded in config.json. The audience expects to hear from the
  maker. Recommendation-style framing is also fine ("a thing we ship
  is...", "this is why we built...").

- `voice_relationship: "third_party"`
  You are an OUTSIDE OBSERVER of this product, NOT its team. Never use
  first-person plural about the product. Always refer to it in
  third-person, by name or by 'their': "Agora ships X", "their EAS
  variant", "the OODAO product", "the Studyly team rolled out Y". The
  narrator is a tech-curious user / builder who has noticed and
  recommends the project, NOT its marketer or staff. Acceptable narrator
  framings: "I tried [project]", "I noticed [project] does X", "[project]
  ships Y". Banned framings: "we ship", "our product", "we built", "I
  built this", "what we offer".

The narrator's own life (the @m13v_ / @matt_diak persona's own voice,
"I", "my") is still available in BOTH modes. The rule only restricts
"we" / "our" / "we built" CLAIMS ABOUT THE PROJECT'S PRODUCT. For
first_party projects those claims are accurate; for third_party
projects they impersonate the client and are banned.

Fabrication of specifics (numbers, dates, places, named tools) is
governed by the GROUNDING RULE above; that rule is orthogonal to this
one. A grounded specific stated in the wrong voice still violates this
rule. An invented specific stated in the right voice still violates the
GROUNDING RULE."""


def get_content_rules(platform):
    """Generate platform-specific content rules.

    Args:
        platform: "reddit", "twitter", or "linkedin"

    Returns:
        Multi-line string of content rules.
    """
    common = [
        "NO em dashes. Use commas, periods, or regular dashes (-).",
        "Never mention any project by name unless recommending. Product voice (claims about the matched project) is governed by the VOICE RELATIONSHIP rule, not here.",
        'Never start with "exactly", "yeah totally", "100%", "that\'s smart".',
        "Specificity is the #1 authenticity signal, but only for specifics you have license to use. See GROUNDING RULE: pick LANE 1 (open with a disclosure phrase like 'hypothetically' or 'scenario:' and then invent freely) OR LANE 2 (no fabricated specifics; every specific must come verbatim from the matched project's config.json). Specifics that pass the GROUNDING RULE still have to be stated in the voice required by the VOICE RELATIONSHIP rule.",
        "Include imperfections: contractions, casual asides, occasional lowercase.",
    ]

    platform_rules = {
        "reddit": [
            "Go BIMODAL: either 1 punchy sentence (<100 chars, highest avg upvotes) or 4-5 sentences of real substance. AVOID the 2-3 sentence dead zone.",
            "Lead with personal experience or observation ('I tried X' / 'I noticed X' / 'the part that broke for me was X') rather than instructional advice ('you should X'). Whether you can speak AS the project's maker is governed by the VOICE RELATIONSHIP rule.",
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
- NEVER claim authorship or operational control of a product whose voice_relationship is "third_party" (see VOICE RELATIONSHIP rule). For first_party projects, prefer recommendation framing over bare "I built it" self-promotion even though the voice is yours to use.
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
