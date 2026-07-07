#!/usr/bin/env python3
"""
twitter_gen_links.py — Phase 2b-gen helper for run-twitter-cycle.sh.

Reads a candidate plan JSON file produced by Phase 2b-prep, generates the
matching landing-page (or falls back to the plain project URL) for each
candidate, and writes the file back with a `link_url` field per candidate.

The browser lock is NOT held while this runs. generate_page.py is pure HTTP +
git + Cloud-Run-deploy work, no twitter-harness browser use, so other twitter
pipelines can use the browser during the 10-40 minute landing-page build.

Plan file shape (in/out):
{
  "candidates": [
    {
      "candidate_id": int,
      "candidate_url": str,
      "thread_author": str,
      "thread_text": str,
      "matched_project": str,
      "reply_text": str,
      "engagement_style": str,
      "language": str,
      "has_landing_pages": bool,
      "link_keyword": str,   # only when has_landing_pages=true
      "link_slug": str,      # only when has_landing_pages=true
      ...
      # Written by THIS script:
      "link_url": str,       # final URL to embed in the reply (may be "")
      "link_source": str,    # seo_page | plain_url_fallback | plain_url_no_lp |
                             # plain_url_timeout_fallback | empty
    },
    ...
  ]
}

Usage:
    python3 twitter_gen_links.py --plan /tmp/twitter_cycle_plan_<batch>.json

Exits 0 on best-effort completion (each candidate gets a link_url, even if
generation failed; the fallback chain protects the cycle from blocking on
SEO infra issues). Exits non-zero only when the plan file itself is unreadable
or empty.
"""

import argparse
import json
import os
import random
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audience_pages as audience_pages_mod  # noqa: E402

REPO_DIR = os.path.expanduser("~/social-autoposter")
GENERATE_PAGE = os.path.join(REPO_DIR, "seo", "generate_page.py")
LINK_TAIL = os.path.join(REPO_DIR, "scripts", "link_tail.py")
CONFIG_PATH = os.path.join(REPO_DIR, "config.json")
GEN_TIMEOUT_SEC = 3600  # 60 min per page; observed legit runs take 45-50 min
                        # (pre-Claude inventory + decision + improve/new pipeline +
                        # deploy verify). Don't lower without re-measuring.
MAX_AB_HITS_PER_CYCLE = 2  # cap cumulative gen budget at ~2 x 60 min worst case
                           # so cycle has room under the 180-min watchdog cap.

# Per-candidate tail-link bridge call budget (see apply_tail_link below). This
# stage already has a 60-min phase budget (SEO page-gen alone can take 10-40
# min), so a few minutes of queue latency per candidate fits comfortably even
# in the degenerate case where every call times out waiting for the s4l-worker
# scheduled task (~N x this value, bounded and still well under 60 min for a
# normal-sized batch).
LINK_TAIL_TIMEOUT_SEC = 180

# A/B gate: per-candidate coin flip for the page-gen lane. 0.25 means 25% of
# eligible candidates (project has landing_pages config + LLM provided
# keyword/slug) actually trigger generate_page.py; the rest fall through to
# the plain project URL with link_source='plain_url_ab_skip'. Tunable via
# env var so cadence can be swept without a code change. 0.0 disables
# page-gen entirely; 1.0 restores the pre-A/B behaviour.
def _page_gen_rate() -> float:
    # Bumped from 0.25 -> 0.30 on 2026-05-08 after CTA pipeline review:
    # /t/ pages convert better than /r/ short-link-only fallbacks (Reddit data
    # showed 17-71% click->signup vs 0% on plain_url_ab_skip). Bumping the
    # default rate gives Twitter a higher share of full landing pages while
    # still leaving 70% on the cheap path for budget reasons. See chat note
    # 2026-05-07 "link suffix pipeline rewrite".
    raw = os.environ.get("TWITTER_PAGE_GEN_RATE", "0.0")
    try:
        v = float(raw)
    except ValueError:
        return 0.30
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return v


def load_projects() -> dict:
    """Map name -> project dict."""
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    return {p["name"]: p for p in cfg.get("projects", [])}


def parse_last_json_object(text):
    """Extract the last balanced top-level JSON object from a string.

    Mirrors twitter_post_plan.py's helper of the same name (link_tail.py's
    only contract is "print one JSON object to stdout"; this defends against
    stray log lines sharing stdout).
    """
    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("{") and text.endswith("}"):
        try:
            return json.loads(text)
        except Exception:
            pass
    matches = []
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    matches.append(text[start:i + 1])
                    start = None
    for cand in reversed(matches):
        try:
            return json.loads(cand)
        except Exception:
            continue
    return None


def _tail_link_rate() -> float:
    # DRAFT_ONLY=1 candidates always go through a human-approval review card
    # (run-draft-and-publish.sh exports DRAFT_ONLY before invoking the cycle
    # that calls this script). Force rate=1.0 there so a hand-approved draft
    # never drops the link the user already saw baked into the card text —
    # ported from the old post-time override in mcp/src/index.ts
    # (`TWITTER_TAIL_LINK_RATE: "1.0"` on the post_drafts path), which no
    # longer fires now that the decision happens here, before the card is
    # ever shown. The autonomous DRAFT_ONLY=0 lane keeps running the real A/B
    # experiment at the configured rate.
    if os.environ.get("DRAFT_ONLY") == "1":
        return 1.0
    try:
        return float(os.environ.get("TWITTER_TAIL_LINK_RATE", "0.5"))
    except ValueError:
        return 0.5


def apply_tail_link(candidate: dict, link_url: str) -> None:
    """Fold `link_url` into candidate['reply_text'] via the Claude bridge
    (scripts/link_tail.py), moved here from twitter_post_plan.py's post-time
    path on 2026-07-06.

    Why here: this is Phase 2b-gen, which already runs BEFORE the DRAFT_ONLY
    gate (both the review-card path and the autonomous-post path pass through
    twitter_gen_links.py first) and already budgets long waits (SEO page-gen
    alone can take 10-40 min). twitter_post_plan.py's post-time call was a bad
    fit for the queue-backed pipeline: post_drafts is a synchronous MCP call
    the user is actively waiting on after clicking Approve, while the
    s4l-worker scheduled task claims one job per minute and doesn't overlap a
    multi-minute drafting turn — so a queue-routed call there could stall an
    approval for minutes. Doing it here means the review card already shows
    the finalized text (bridge + link, or intentionally no link on the
    no_link A/B arm) and post time just ships it verbatim.

    Stamps tail_link_variant + link_tail_outcome on `candidate` so
    twitter_post_plan.py can detect this candidate is already finalized and
    skip its own (now fallback-only) tail-link block.
    """
    if not link_url:
        return
    rate = _tail_link_rate()
    add_link = random.random() < rate
    candidate["tail_link_variant"] = "link" if add_link else "no_link"
    if not add_link:
        print(f"[gen] candidate_id={candidate.get('candidate_id')} "
              f"tail_link: ab_no_link (rate={rate})", flush=True)
        return
    reply_text = (candidate.get("reply_text") or "").strip()
    cmd = [
        "python3", LINK_TAIL,
        "--reply-text", reply_text,
        "--link-url", link_url,
        "--thread-text", candidate.get("thread_text") or "",
        "--project", candidate.get("matched_project") or "",
        "--platform", "twitter",
        "--timeout", str(LINK_TAIL_TIMEOUT_SEC),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=LINK_TAIL_TIMEOUT_SEC + 30)
        rc, out = r.returncode, r.stdout
    except subprocess.TimeoutExpired:
        candidate["reply_text"] = f"{reply_text} {link_url}".strip()
        candidate["link_tail_outcome"] = "hard_timeout"
        print(f"[gen] candidate_id={candidate.get('candidate_id')} "
              f"link_tail: hard_timeout; keeping mechanical concat", flush=True)
        return
    obj = parse_last_json_object(out) or {}
    if obj.get("text"):
        candidate["reply_text"] = obj["text"]
        if obj.get("model_call_ok") and not obj.get("fallback_used"):
            outcome = "bridge_generated"
        else:
            outcome = f"fallback:{(obj.get('error') or 'unknown')[:60]}"
    else:
        # link_tail.py is supposed to ALWAYS return JSON; if we got nothing,
        # hard-fall-back to the mechanical concat so the card still shows a
        # complete draft (link still present) instead of a bare reply.
        candidate["reply_text"] = f"{reply_text} {link_url}".strip()
        outcome = f"hard_fallback_no_json:rc={rc}"
    candidate["link_tail_outcome"] = outcome
    print(f"[gen] candidate_id={candidate.get('candidate_id')} "
          f"link_tail: {outcome} (elapsed={obj.get('elapsed_sec')}s)", flush=True)


def run_generate(product: str, keyword: str, slug: str) -> tuple[str, str]:
    """Run generate_page.py for a single candidate.

    Returns (page_url, source_tag). On success: (real_url, "seo_page"). On
    any failure: ("", "<reason>") so the caller can fall back to the plain URL.
    """
    cmd = [
        "python3",
        GENERATE_PAGE,
        "--product", product,
        "--keyword", keyword,
        "--slug", slug,
        "--trigger", "twitter",
    ]
    print(f"[gen] product={product} keyword={keyword!r} slug={slug!r}", flush=True)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=GEN_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        print(f"[gen] TIMEOUT after {GEN_TIMEOUT_SEC}s", flush=True)
        return ("", "timeout")
    print(f"[gen] exit={r.returncode}", flush=True)
    if r.stderr:
        # Trail-truncate so we don't blow out the cycle log on a verbose failure.
        print("[gen][stderr-tail]", r.stderr[-2000:], flush=True)
    # generate_page.py prints its final result via json.dumps(result, indent=2),
    # so the success object is a pretty-printed multi-line block. Scan stdout
    # for every top-level JSON object via JSONDecoder.raw_decode and keep the
    # last dict we can parse: that's the final result line regardless of
    # whether it was emitted as one line or many.
    page_url = ""
    last_obj = None
    decoder = json.JSONDecoder()
    text = r.stdout
    i = text.find("{")
    while i != -1:
        try:
            obj, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            i = text.find("{", i + 1)
            continue
        if isinstance(obj, dict):
            last_obj = obj
        i = text.find("{", end)
    if last_obj and last_obj.get("success") and last_obj.get("page_url"):
        page_url = last_obj["page_url"]
    if not page_url:
        print("[gen] no page_url in stdout; tail=", flush=True)
        print(r.stdout[-2000:], flush=True)
        return ("", "no_page_url")
    return (page_url, "seo_page")


def resolve_link(candidate: dict, projects: dict, page_gen_rate: float) -> tuple[str, str]:
    """Decide the link URL for a single candidate.

    Order of preference:
      1. CURATED AUDIENCE PAGE (landing_pages.audience_pages) — wins outright
         when the candidate's link_keyword / search_topic / reply_text matches
         any entry's match_keywords. Skips the A/B gate entirely; curated pages
         are higher-quality than auto-generated /t/<slug> pages.
      2. SEO page (when has_landing_pages AND dice lands in gen lane)
      3. plain project URL
      4. ""

    The per-candidate dice roll (random.random() < page_gen_rate) only fires
    for projects that actually support landing pages and where the LLM
    supplied a keyword + slug. Eligible-but-lost candidates surface as
    link_source='plain_url_ab_skip' so post-hoc engagement analysis can
    compare the two lanes apples-to-apples.

    Audience-page hits surface as link_source='audience_page:<angle>' so the
    dashboard and stats can break out curated-page traffic separately.
    """
    proj_name = candidate.get("matched_project") or ""
    proj = projects.get(proj_name) or {}
    # Personal-brand (persona) lane is link-free by definition. Self-promotion
    # mode is pure organic engagement: no company, no signup, no profile URL. Any
    # `website`/`url` a persona project happens to carry (some installs got the
    # user's own X profile written there) must NEVER become a tail link. Enforce
    # it here at the single source so no downstream surface (review card, manual
    # post_drafts) has to strip a link that should never have been generated.
    if proj.get("persona"):
        return ("", "persona_no_link")
    plain_url = proj.get("website") or proj.get("url") or ""
    has_lp = bool(candidate.get("has_landing_pages"))
    keyword = (candidate.get("link_keyword") or "").strip()
    slug = (candidate.get("link_slug") or "").strip()

    # (1) Curated audience-page short-circuit. Runs BEFORE the A/B gate so a
    # well-targeted curated page always beats a freshly-spun SEO /t/<slug>.
    # Signals checked: link_keyword (LLM nomination), search_topic (the topic
    # bucket the candidate was discovered under), reply_text (the actual draft),
    # and thread_title (raw thread title from Twitter). First match wins per
    # the audience_pages list order in config.json.
    audience_hit = audience_pages_mod.match_by_keyword(
        proj_name,
        keyword=keyword,
        topic=candidate.get("search_topic"),
        reply_text=candidate.get("reply_text"),
        thread_title=candidate.get("thread_title") or candidate.get("thread_text"),
    )
    if audience_hit:
        angle = audience_hit.get("angle") or "unknown"
        url = audience_hit.get("url") or ""
        if url:
            print(f"[gen] audience_page hit: angle={angle} url={url} "
                  f"(skipping A/B page-gen)", flush=True)
            return (url, f"audience_page:{angle}")

    if proj.get("page_gen_disabled"):
        print(f"[gen] page_gen_disabled=true for {proj_name}; using plain URL", flush=True)
        return (plain_url, "plain_url_no_lp")

    if has_lp and keyword and slug and proj.get("landing_pages"):
        roll = random.random()
        if roll >= page_gen_rate:
            print(f"[gen] AB skip: roll={roll:.3f} >= rate={page_gen_rate:.3f}; "
                  f"using plain URL", flush=True)
            if plain_url:
                return (plain_url, "plain_url_ab_skip")
            return ("", "empty_ab_skip")
        print(f"[gen] AB hit: roll={roll:.3f} < rate={page_gen_rate:.3f}; "
              f"running generate_page.py", flush=True)
        page_url, source = run_generate(proj_name, keyword, slug)
        if page_url:
            return (page_url, "seo_page")
        # Fell through; fall back to plain project URL.
        if plain_url:
            return (plain_url, f"plain_url_fallback:{source}")
        return ("", f"empty:{source}")
    # No landing-pages config or LLM didn't supply keyword/slug.
    if plain_url:
        return (plain_url, "plain_url_no_lp")
    return ("", "empty")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True,
                    help="Path to the plan JSON file (read+rewrite in place)")
    args = ap.parse_args()

    plan_path = Path(args.plan)
    if not plan_path.exists():
        print(f"[gen] plan file not found: {plan_path}", file=sys.stderr)
        return 2
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[gen] plan file unreadable: {e}", file=sys.stderr)
        return 2

    candidates = plan.get("candidates") or []
    if not candidates:
        print("[gen] plan has 0 candidates; nothing to do", flush=True)
        return 0

    projects = load_projects()
    page_gen_rate = _page_gen_rate()
    print(f"[gen] page_gen_rate={page_gen_rate:.3f} "
          f"(env TWITTER_PAGE_GEN_RATE)", flush=True)
    print(f"[gen] max_ab_hits_per_cycle={MAX_AB_HITS_PER_CYCLE} "
          f"timeout_per_call_sec={GEN_TIMEOUT_SEC}", flush=True)

    ab_hits = 0
    for c in candidates:
        cap_reached = ab_hits >= MAX_AB_HITS_PER_CYCLE
        if cap_reached:
            print(f"[gen] AB cap reached ({ab_hits}/"
                  f"{MAX_AB_HITS_PER_CYCLE}); forcing plain URL", flush=True)
        link_url, source = resolve_link(c, projects,
                                        0.0 if cap_reached else page_gen_rate)
        if source == "seo_page":
            ab_hits += 1
        elif cap_reached and source == "plain_url_ab_skip":
            source = "plain_url_ab_cap"
        c["link_url"] = link_url
        c["link_source"] = source
        print(f"[gen] candidate_id={c.get('candidate_id')} "
              f"link_url={link_url!r} source={source}", flush=True)
        apply_tail_link(c, link_url)

    plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    print(f"[gen] plan rewritten with link_url for {len(candidates)} candidates",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
