#!/usr/bin/env python3
"""Daily synthesizer: per platform, distill ONE engagement style from the
top human replies captured in the last 24h on that platform.

Pipeline (per platform)
-----------------------
1. Pull every `thread_top_replies` row from the last 24h on the platform
   with `has_link = false` (human replies, not link-tail spam).
2. Score by likes (the only reliable proxy in the window, since `views` is
   missing for most rows). Take the top REPLY_POOL_SIZE.
3. Build a Claude prompt that lists each reply with its like-count + thread
   URL and asks the model to synthesize ONE new engagement style following
   the seed-style schema (name / description / example / best_in / note).
4. Parse the JSON, POST to /api/v1/engagement-styles/registry with
   kind="human_derived" and platform="<platform>" so it lands in the
   single source-of-truth table alongside seeds and model-invented styles.

The picker (scripts/engagement_styles.py) reads the most recent active
row of kind='human_derived' for the calling platform via the same route
with HUMAN_DERIVED_RATE_BY_PLATFORM[platform] probability per call. See
migrations/2026-05-22_consolidate_engagement_styles_human_derived.sql for
the table shape and the table-consolidation rationale.

Run manually: python3 scripts/generate_daily_human_style.py
              python3 scripts/generate_daily_human_style.py --platform twitter
              python3 scripts/generate_daily_human_style.py --dry-run

Cron entry  : skill/run-generate-daily-style.sh (wraps this via run_claude.sh).
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_DIR, "scripts"))

from http_api import api_get, api_post  # noqa: E402

# Platforms we attempt synthesis for. Each platform that has >= MIN_REPLIES
# human replies in the window gets its own row in engagement_styles_registry
# (kind='human_derived'). Platforms with fewer rows are skipped silently —
# next run will try again.
PLATFORMS = ["twitter", "reddit", "github", "moltbook", "linkedin"]

REPLY_POOL_SIZE = 10            # top N human replies fed to Claude
WINDOW_HOURS = 24
MIN_LIKES = 5                   # exclude noise-floor replies
MIN_REPLIES = 3                 # skip platform if <3 qualifying rows
CLAUDE_MODEL_DEFAULT = None     # inherit from settings.json
RUN_CLAUDE_PATH = os.path.join(REPO_DIR, "scripts", "run_claude.sh")
SCRIPT_TAG = "daily-human-style"

# target_chars computed from the live top-human-reply median (the whole point
# of the human_derived lane: learn the length that actually wins TODAY, not a
# static seed). Clamped to a sane reply-sized band so an outlier essay-reply or
# a one-word "this." can't drag the target out of range. IG long-form captions
# are NOT synthesized here (this lane is reply/comment-shaped), so the ceiling
# stays tweet-sized.
TARGET_CHARS_FLOOR = 30
TARGET_CHARS_CEIL = 300
DEFAULT_TARGET_CHARS = 80       # used only if median can't be computed


def median_reply_chars(replies):
    """Median char length of the top human replies' content, clamped to the
    reply-sized band. This is the realized length of what actually won the
    thread today, so it becomes the style's target_chars: we aim to land where
    humans land, not where our prompts historically bloated to (~215)."""
    lengths = sorted(
        len((r.get("reply_content") or "").strip())
        for r in replies
        if (r.get("reply_content") or "").strip()
    )
    if not lengths:
        return DEFAULT_TARGET_CHARS
    n = len(lengths)
    mid = n // 2
    med = lengths[mid] if n % 2 else (lengths[mid - 1] + lengths[mid]) // 2
    return max(TARGET_CHARS_FLOOR, min(TARGET_CHARS_CEIL, int(med)))


def fetch_top_human_replies(platform,
                            limit=REPLY_POOL_SIZE, hours=WINDOW_HOURS):
    """Top human replies on `platform` from the last <hours>, ordered by likes.

    `likes` is the only engagement column populated across all platforms
    in thread_top_replies (views/comments/retweets are platform-shaped),
    so we lean on it as the ranking key. has_link=false filters out
    link-tail spam so the synthesizer only learns from organic moves.

    Served via the HTTP API (thread-top-replies?top_human=1) so no DATABASE_URL
    is needed. The route returns the same column shape this used to SELECT.
    """
    resp = api_get(
        "/api/v1/thread-top-replies",
        query={
            "top_human": "1",
            "platform": platform,
            "within_hours": int(hours),
            "min_likes": MIN_LIKES,
            "limit": int(limit),
        },
    )
    return (resp.get("data") or {}).get("replies") or []


def load_existing_style_names():
    """Every active style name already in the registry (any kind), so the
    model doesn't propose a collision. Falls back to the in-process STYLES
    dict if the registry can't be reached.

    Reads via the same route the picker uses, not via direct DB access.
    """
    names = set()
    try:
        from engagement_styles import get_all_styles  # noqa: WPS433
        names.update(get_all_styles().keys())
    except Exception:
        pass
    # No DB fallback for the registry table — get_all_styles() already
    # consults the route, and the in-memory STYLES dict is the cold-start
    # floor it merges in. We don't want to bypass the route for an extra
    # DB read here.
    return names


def already_generated_recently(platform, hours=20):
    """True if a human_derived style for `platform` was already created in the
    last `hours`.

    Idempotency guard. The daily cron fires ONCE at 16:00 PDT, so any second
    human_derived row for the same platform inside ~20h is a duplicate, e.g. a
    launchd catch-up run after the Mac woke from sleep, a manual rerun, or a
    double-fire. Without this guard nothing stopped repeated invocations from
    each minting a fresh style: on 2026-05-28 the synthesizer was invoked 4x
    and inserted 4 twitter styles (peer_imperative, utility_for_reader_link,
    build_in_public_artifact, proof_of_claim_link). The contract is "invent no
    more than we consume" => at most one human_derived style per platform per
    day.

    Reads the newest human_derived row for the platform via the registry route
    (kind=human_derived&platform=X&latest=1) and compares generated_at to the
    window locally, so no DATABASE_URL is needed.

    Fails OPEN (returns False) on any error so a transient blip never silently
    kills the daily run.
    """
    try:
        resp = api_get(
            "/api/v1/engagement-styles/registry",
            query={
                "kind": "human_derived",
                "platform": platform,
                "latest": "1",
                "status": "all",
            },
        )
        styles = (resp.get("data") or {}).get("styles") or []
        if not styles:
            return False
        gen = styles[0].get("generated_at")
        if not gen:
            return False
        s = str(gen)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        return dt >= cutoff
    except Exception as e:
        sys.stderr.write(
            f"[generate_daily_human_style] platform={platform} idempotency "
            f"check failed ({e}); proceeding (fail-open)\n"
        )
        return False


def build_prompt(platform, replies, reserved_names):
    lines = []
    lines.append(
        f"You are analyzing the top-performing human {platform} replies from "
        f"the last {WINDOW_HOURS} hours and distilling ONE new engagement "
        "style we can use for our own replies on that platform."
    )
    lines.append("")
    lines.append("## What you're looking for")
    lines.append("")
    lines.append(
        "These replies all WON the thread (top of the conversation by likes). "
        "Find the shared pattern that makes them work — the rhetorical move, "
        "the structural shape, the relationship to the OP. Most winners "
        "share ONE pattern; that pattern is your new engagement style."
    )
    lines.append("")
    lines.append(
        "Ignore: replies that win because of follower count, fame, or "
        "non-repeatable luck. Focus on the structural move that we (a "
        "small account) could imitate and have a chance of replicating."
    )
    lines.append("")
    lines.append(
        f"## Top {len(replies)} human {platform} replies (by likes)"
    )
    lines.append("")
    for i, r in enumerate(replies, 1):
        lines.append(
            f"### #{i} (likes={r['likes']}, replies={r['replies_count']}, "
            f"rt={r['retweets']})"
        )
        lines.append(f"Thread: {r['thread_url']}")
        handle = r.get("reply_author_handle") or "(unknown)"
        lines.append(f"Reply by @{handle}:")
        lines.append(f"> {r['reply_content']}")
        lines.append("")

    lines.append("## Schema (match exactly)")
    lines.append("")
    lines.append(
        "Return ONE JSON object describing a single new engagement style. "
        "No prose around it, no markdown code fence. The object MUST have "
        "every field below:"
    )
    lines.append("")
    lines.append("```")
    lines.append("{")
    lines.append('  "name": "<snake_case_name>",')
    lines.append('  "description": "<one to three sentences describing the style>",')
    lines.append('  "example": "<one short OP + reply pair demonstrating the style>",')
    lines.append('  "best_in": {')
    lines.append(f'    "{platform}": ["<short context label>", ...],')
    # Encourage the model to fill cross-platform `best_in` opportunistically
    # when the move generalizes; leave as [] if not.
    for other in PLATFORMS:
        if other != platform:
            lines.append(f'    "{other}":  [],')
    lines.append("  },")
    lines.append('  "note": "<one to two sentences: when to use, when not to>"')
    lines.append("}")
    lines.append("```")
    lines.append("")
    lines.append("## Rules")
    lines.append("")
    lines.append(
        "1. The name must be unique. Reserved (do NOT propose these): "
        f"{sorted(reserved_names)}."
    )
    lines.append(
        "2. The name should be 2 to 4 snake_case tokens, descriptive of the "
        "MOVE (e.g. `mirror_and_extend`, `flip_to_alt`, not `good_reply`)."
    )
    lines.append(
        "3. The description should make the style copyable: a future model "
        "reading just that one sentence should know what to write."
    )
    lines.append(
        "4. The example should be a realistic OP + reply pair, not lifted "
        "verbatim from the inputs."
    )
    lines.append(
        f"5. best_in.{platform} is required (at least one context label). "
        "Other platforms can stay empty arrays if the style is "
        f"{platform}-specific."
    )
    lines.append(
        "6. NEVER propose a style about including a product, a URL, or a "
        "mechanism. Our link-tail layer handles that downstream. The style "
        "is about the text BEFORE the link."
    )
    return "\n".join(lines)


def call_claude(prompt):
    cmd = [RUN_CLAUDE_PATH, SCRIPT_TAG, "-p", prompt, "--output-format", "json"]
    if CLAUDE_MODEL_DEFAULT:
        cmd.extend(["--model", CLAUDE_MODEL_DEFAULT])
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        sys.stderr.write(
            f"[generate_daily_human_style] claude rc={result.returncode}\n"
            f"stderr: {result.stderr[:2000]}\n"
        )
        raise RuntimeError(f"claude wrapper failed: rc={result.returncode}")
    envelope = json.loads(result.stdout)
    return envelope.get("result", "") or ""


_JSON_OBJ_RE = re.compile(r"\{[\s\S]*\}")


def extract_json(text):
    """Tolerant of code fences or stray prose around the JSON object."""
    fence = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
    if fence:
        return json.loads(fence.group(1))
    m = _JSON_OBJ_RE.search(text)
    if not m:
        raise ValueError("No JSON object found in model output")
    return json.loads(m.group(0))


def validate_style(style, platform, reserved_names):
    """Sanity-check the model output before we POST."""
    required = {"name", "description", "example", "best_in", "note"}
    missing = required - set(style.keys())
    if missing:
        raise ValueError(f"missing fields: {sorted(missing)}")

    name = style["name"]
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{2,40}", name):
        raise ValueError(f"bad name: {name!r}")
    if name in reserved_names:
        raise ValueError(f"name collision: {name!r}")

    best_in = style["best_in"]
    if not isinstance(best_in, dict):
        raise ValueError("best_in must be object")
    # We require the calling platform key to be a non-empty list. Other
    # platforms may be missing or empty — the route accepts them as long
    # as the JSON shape is sane.
    pf = best_in.get(platform)
    if not isinstance(pf, list) or not pf:
        raise ValueError(
            f"best_in.{platform} must be a non-empty list (source platform)"
        )

    for field in ("description", "example", "note"):
        if not isinstance(style[field], str) or not style[field].strip():
            raise ValueError(f"{field} must be non-empty string")


def post_style(style, platform, replies, prompt_chars):
    """POST the synthesized style to the registry route. The route writes
    to engagement_styles_registry with kind='human_derived' and platform=
    <platform> (the picker filters on those two for the latest row).
    Returns the parsed response (with style + created keys).
    """
    source_post_ids = [r["id"] for r in replies]
    target_chars = median_reply_chars(replies)
    window_end = datetime.now(timezone.utc)
    window_start = window_end - timedelta(hours=WINDOW_HOURS)
    gen_log = (
        f"Synthesized {window_end.isoformat(timespec='seconds')} from "
        f"top {len(replies)} human {platform} replies in last "
        f"{WINDOW_HOURS}h. Prompt size: {prompt_chars} chars. "
        f"Reply id range: {min(source_post_ids)}-{max(source_post_ids)}. "
        f"target_chars={target_chars} (median of source-reply lengths)."
    )

    payload = {
        "name": style["name"],
        "description": style["description"],
        "example": style["example"],
        "note": style["note"],
        "best_in": style["best_in"],
        "target_chars": target_chars,
        "kind": "human_derived",
        "platform": platform,
        "first_post_platform": platform,
        "invented_by_model": "daily-human-style-synthesizer",
        "source_window_start": window_start.isoformat(timespec="seconds"),
        "source_window_end": window_end.isoformat(timespec="seconds"),
        "source_post_ids": source_post_ids,
        "generation_log": gen_log,
        "generated_at": window_end.isoformat(timespec="seconds"),
    }
    return api_post(
        "/api/v1/engagement-styles/registry", payload, ok_on_conflict=True,
    )


def synthesize_for_platform(platform, reserved, dry_run=False):
    """Run the synthesizer for ONE platform. Returns a result dict for
    summary logging; raises only on genuinely fatal errors (e.g. Claude
    wrapper crash). Insufficient-data is a soft skip.
    """
    # Idempotency: at most ONE human_derived style per platform per day. Skip
    # before spending a Claude call if today's style already exists (rerun,
    # launchd catch-up, double-fire). dry_run bypasses so prompts stay
    # inspectable.
    if not dry_run and already_generated_recently(platform):
        sys.stderr.write(
            f"[generate_daily_human_style] platform={platform} already has a "
            f"human_derived style from the last 20h; skipping (idempotent).\n"
        )
        return {"platform": platform, "status": "skipped_already_today"}
    replies = fetch_top_human_replies(platform)
    if len(replies) < MIN_REPLIES:
        sys.stderr.write(
            f"[generate_daily_human_style] platform={platform} only "
            f"{len(replies)} replies in last {WINDOW_HOURS}h (need "
            f">={MIN_REPLIES}). Skipping.\n"
        )
        return {
            "platform": platform,
            "status": "skipped_insufficient_data",
            "source_count": len(replies),
        }
    prompt = build_prompt(platform, replies, reserved)
    sys.stderr.write(
        f"[generate_daily_human_style] platform={platform} prompt "
        f"{len(prompt)} chars, {len(replies)} replies, "
        f"reserved={len(reserved)} names\n"
    )
    if dry_run:
        return {
            "platform": platform,
            "status": "dry_run",
            "source_count": len(replies),
            "prompt_chars": len(prompt),
            "source_likes_top": replies[0]["likes"],
            "source_likes_bottom": replies[-1]["likes"],
            "target_chars": median_reply_chars(replies),
        }

    text = call_claude(prompt)
    if not text.strip():
        sys.stderr.write(
            f"[generate_daily_human_style] platform={platform} empty claude "
            "output\n"
        )
        return {"platform": platform, "status": "empty_claude_output"}
    style = extract_json(text)
    validate_style(style, platform, reserved)
    resp = post_style(style, platform, replies, len(prompt))
    data = (resp or {}).get("data") or {}
    created = bool(data.get("created"))
    inserted = data.get("style") or {}
    # Add the name to the live reserved set so the NEXT platform in the
    # same run can't propose the same name.
    if inserted.get("name"):
        reserved.add(inserted["name"])
    return {
        "platform": platform,
        "status": "ok" if created else "duplicate",
        "name": inserted.get("name") or style["name"],
        "kind": inserted.get("kind", "human_derived"),
        "source_count": len(replies),
        "source_likes_top": replies[0]["likes"],
        "source_likes_bottom": replies[-1]["likes"],
        "target_chars": median_reply_chars(replies),
        "created": created,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--platform",
        action="append",
        choices=PLATFORMS,
        help="Limit to one or more platforms (repeatable). Default: all.",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Build prompts and report counts; don't call Claude or POST.",
    )
    args = ap.parse_args()
    platforms = args.platform or PLATFORMS

    summary = []
    reserved = load_existing_style_names()
    for platform in platforms:
        try:
            result = synthesize_for_platform(
                platform, reserved, dry_run=args.dry_run,
            )
        except Exception as e:
            sys.stderr.write(
                f"[generate_daily_human_style] platform={platform} "
                f"failed: {e}\n"
            )
            result = {
                "platform": platform,
                "status": "error",
                "error": str(e),
            }
        summary.append(result)

    print(json.dumps({"runs": summary}, indent=2, default=str))
    # Exit non-zero if every platform errored — soft skips don't count.
    if summary and all(r.get("status") == "error" for r in summary):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
