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


def _fetch_style_stats(platform):
    """Query the autoposter API for avg_upvotes per engagement_style on this platform.

    Returns a dict: {style_name: {"n": int, "avg_up": float}}.
    Returns {} on any error (API unreachable, missing env, cold start).

    Routes through the social-autoposter-website API (no direct Neon access)
    so VMs / sandboxes without a DATABASE_URL still get live weights.
    """
    try:
        import os
        import sys as _sys
        _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from http_api import api_get
        resp = api_get("/api/v1/engagement-styles/style-stats", {"platform": platform})
        data = (resp or {}).get("data") or {}
        stats = data.get("stats") or {}
        return {
            name: {"n": int(v.get("n", 0)), "avg_up": float(v.get("avg_up", 0.0))}
            for name, v in stats.items()
            if isinstance(v, dict)
        }
    except Exception:
        return {}


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

def _last_picks(platform, limit=10):
    """Return the last `limit` engagement_style picks on `platform`, newest first.

    Used by the prompt to show recent pick history so the LLM can cool off a
    style that's been over-used. Returns [] on any error.

    Routes through the social-autoposter-website API (no direct Neon access)
    so VMs / sandboxes without a DATABASE_URL still get the recent-pick list.
    """
    try:
        import os
        import sys as _sys
        _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from http_api import api_get
        resp = api_get(
            "/api/v1/engagement-styles/last-picks",
            {"platform": platform, "limit": int(limit)},
        )
        data = (resp or {}).get("data") or {}
        picks = data.get("picks") or []
        return [str(p) for p in picks if p]
    except Exception:
        return []


def compute_target_distribution(platform, context="posting"):
    """Compute per-style target pick% using sharpened avg_upvotes with floor+cap.

    Returns a list of dicts sorted by target pct DESC:
        [{"style": name, "pct": float, "n": int, "avg_up": float, "trusted": bool}]

    Policy:
      - Styles in PLATFORM_POLICY[platform].never are excluded.
      - Styles with N >= MIN_SAMPLE_SIZE get weight = avg_up ** WEIGHT_EXPONENT.
      - Styles with N < MIN_SAMPLE_SIZE (incl. zero) get STYLE_FLOOR_PCT only
        so noisy small-n styles (e.g. n=1 with avg_up=14) don't dominate.
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
        avg = float(s["avg_up"]) if s else 0.0
        # Sidecar candidates never count as trusted; they get floor weight only
        # until promoted, regardless of their sample count.
        is_candidate_status = universe[style].get("status") == "candidate"
        trusted = (s is not None and n >= MIN_SAMPLE_SIZE
                   and not is_candidate_status)
        weight = (avg ** WEIGHT_EXPONENT) if trusted else 0.0
        if trusted:
            trusted_total += weight
        rows.append({"style": style, "n": n, "avg_up": avg, "trusted": trusted,
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


# ── Prompt generators ───────────────────────────────────────────────

def get_styles_prompt(platform, context="posting"):
    """Generate the engagement styles prompt block for a given platform.

    Shows an explicit per-style target pick% (computed from live avg_upvotes
    via compute_target_distribution) plus the last 10 picks on this platform,
    so the LLM can preferentially pick styles that are under-used vs target.

    Replaces the old PRIMARY/SECONDARY/RARE buckets, which tended to make
    the LLM fixate on one "safe" style regardless of actual performance data.

    Args:
        platform: "reddit", "twitter", "linkedin", "github", "moltbook"
        context: "posting" (new posts) or "replying" (engagement replies)
    """
    policy = PLATFORM_POLICY.get(platform, PLATFORM_POLICY["reddit"])
    never_styles = set(policy.get("never", []))

    targets = compute_target_distribution(platform, context)
    targets = [t for t in targets if t["style"] not in never_styles]
    last_picks = _last_picks(platform, limit=10)

    recent_counts = {t["style"]: 0 for t in targets}
    for p in last_picks:
        if p in recent_counts:
            recent_counts[p] += 1
    pick_n = max(1, len(last_picks))
    under_represented = []
    over_represented = []
    for t in targets:
        recent_pct = (recent_counts[t["style"]] / pick_n) * 100.0
        if recent_pct < t["pct"] - 5.0:
            under_represented.append(t["style"])
        elif recent_pct > t["pct"] + 10.0:
            over_represented.append(t["style"])

    lines = []
    lines.append("## Engagement styles (pick the one that fits — and that we're under-using)")
    lines.append("")
    lines.append(f"Match your style to the conversation. {policy['note']}")
    lines.append("")
    lines.append(
        f"Target pick distribution on {platform} (derived from live avg_upvotes, "
        f"sharpened by exponent {WEIGHT_EXPONENT:g} so the winner gets most traffic; "
        f"{STYLE_FLOOR_PCT:g}% floor per style so every style keeps getting tested):"
    )
    lines.append("")
    for t in targets:
        if t["trusted"]:
            sample = f"avg_up {t['avg_up']:.2f} · n={t['n']}"
        elif t.get("is_candidate"):
            sample = f"n={t['n']} (candidate, model-invented; floor only)"
        else:
            sample = f"n={t['n']} (below trust threshold; floor only)"
        tag = " [NEW]" if t.get("is_candidate") else ""
        lines.append(f"- **{t['style']}**{tag}: {t['pct']:.0f}%  ({sample})")
    lines.append("")
    if last_picks:
        lines.append(f"Your last {len(last_picks)} picks on {platform} (newest first): {', '.join(last_picks)}")
    else:
        lines.append(f"Your last picks on {platform}: (none yet)")
    if under_represented:
        lines.append(f"Under-used vs target right now (lean toward these): {', '.join(under_represented)}")
    if over_represented:
        lines.append(f"Over-used vs target right now (lean away): {', '.join(over_represented)}")
    lines.append("")
    lines.append("Rules:")
    lines.append("- Prefer a style whose recent pick-rate is BELOW its target% unless another style clearly fits the thread better.")
    lines.append("- The top style is the winner to reach for, not the default — pick it when it fits, not by habit.")
    lines.append("- Every style in the list is allowed; there is no hard tier.")
    if never_styles:
        lines.append(f"- Never on {platform}: {', '.join(sorted(never_styles))}.")
    lines.append("")

    full_universe = get_all_styles()
    for t in targets:
        style = full_universe.get(t["style"])
        if not style:
            continue
        best = style.get("best_in", {}).get(platform, [])
        tag = " [NEW, model-invented candidate]" if t.get("is_candidate") else ""
        lines.append(f"**{t['style']}**{tag}: {style['description']}")
        lines.append(f'  "{style["example"]}"')
        if best:
            lines.append(f"  Best in: {', '.join(best)}.")
        if style.get("note"):
            lines.append(f"  {style['note']}")
        if t.get("is_candidate") and style.get("why_existing_didnt_fit"):
            lines.append(f"  Invented because: {style['why_existing_didnt_fit']}")
        lines.append("")

    lines.append(
        "## Inventing a new style"
    )
    lines.append(
        "If none of the styles above genuinely fit the thread, you may invent "
        "a new one. To do so, set `engagement_style` to your new name and ALSO "
        "include a `new_style` block in the same JSON object:"
    )
    lines.append("")
    lines.append('  "new_style": {')
    lines.append('    "description": "<what this style is, in one sentence>",')
    lines.append('    "example": "<a short example utterance>",')
    lines.append('    "note": "<when to use, when not to>",')
    lines.append('    "why_existing_didnt_fit": "<why none of the styles above worked here>"')
    lines.append('  }')
    lines.append("")
    lines.append(
        "Use this sparingly. If an existing style is even 80% right, prefer "
        "it. New styles are accepted as candidates and only graduate to full "
        "weight after they prove out across multiple posts."
    )
    lines.append("")

    if context == "replying":
        lines.append(
            "## Project recommendation (separate dimension from tone)"
        )
        lines.append(
            "Tone and intent are independent. Pick one tone from the list above. "
            "SEPARATELY, up to ~20% of replies should casually mention a project "
            "from config (Tier 2 / Tier 3 of the link strategy). When you do, "
            "pass --is-recommendation to reply_db.py so the DB reflects the intent. "
            "Do NOT pick a tone called 'recommendation' — that field is tone, not intent."
        )
        lines.append("")

    lines.append('AVOID the "pleaser/validator" style ("this is great", "had similar results", "100% agree"). It consistently gets the lowest engagement across all platforms.')
    lines.append("")
    lines.append(get_grounding_rule())
    return "\n".join(lines)


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
