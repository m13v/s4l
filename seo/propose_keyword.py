#!/usr/bin/env python3
"""
seo/propose_keyword.py

Structured-output replacement for the inline `claude -p` proposal call in
run_top_pages_pipeline.sh.

The old call piped a free-form prompt to `claude -p --output-format json`
and depended on the model emitting valid `{"keyword","slug","concept"}`
JSON. Free-text completion does NOT enforce schema, so the model would
occasionally drop the trailing `}` (or wrap the blob in prose) and the
shell-side parser had a 55-line regex fallback to salvage broken output.
On 2026-05-07 the Terminator target tripped a case the fallback could not
recover from, halting that target for the run.

This script calls the Anthropic SDK with two tools:

  * web_search   : Anthropic-hosted server tool, runs grounding searches
                   inline; results are fed back to the model transparently.
                   Replaces the WebSearch capability that the CLI flag
                   `--allowed-tools "WebSearch,WebFetch"` previously enabled.

  * propose_keyword : a custom tool whose `input_schema` enforces the
                   exact `{keyword, slug, concept}` shape. Tool-use blocks
                   are guaranteed schema-valid by the API. The model
                   physically cannot emit truncated JSON the way it did
                   in free-text mode, because the API rejects it before
                   it leaves the server.

Forced tool_choice on `propose_keyword` makes the model finalize via the
custom tool. Web searches still run as a server tool (server tools are
allowed alongside a forced custom tool).

Reads:
  argv[1] : path to brief JSON (winner + targets[] + ranking[])
  argv[2] : target product name (must match an entry in brief['targets'])

Writes to stdout (a single line):
  {"keyword": "...", "slug": "...", "concept": "..."}

Exit codes:
  0  ok
  1  bad args / target not found in brief
  2  no Anthropic credentials
  3  API error after retries
  4  model never emitted propose_keyword tool_use after retries
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from typing import Any

import anthropic

DEFAULT_MODEL = "claude-sonnet-4-5-20250929"
MAX_TOKENS = 4096
MAX_RETRIES = 3
WEB_SEARCH_MAX_USES = 6


# Keychain services to try, in order, when ANTHROPIC_API_KEY is not in env.
# Matches the auth-skill naming convention. Falls back to env-only if none
# match (e.g. CI environments).
KEYCHAIN_SERVICES = (
    "Anthropic API Key Social-Autoposter",  # preferred, project-specific
    "Anthropic API Key Fazm",
    "Claude API",
    "Anthropic API Key Hindsight",
)


def get_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key.strip()
    for svc in KEYCHAIN_SERVICES:
        try:
            out = subprocess.run(
                ["security", "find-generic-password", "-s", svc, "-w"],
                capture_output=True, text=True, timeout=5,
            )
            if out.returncode == 0 and out.stdout.strip().startswith("sk-"):
                return out.stdout.strip()
        except Exception:
            pass
    print(
        "ERROR: ANTHROPIC_API_KEY not set and no keychain match for: "
        + ", ".join(KEYCHAIN_SERVICES),
        file=sys.stderr,
    )
    sys.exit(2)


def build_prompt(brief: dict, target_product: str) -> str:
    target = next(
        (t for t in brief.get("targets", []) if t["product"] == target_product),
        None,
    )
    if not target:
        print(f"ERROR: target {target_product!r} not in brief", file=sys.stderr)
        sys.exit(1)

    proj = target["project_config"]
    winner = brief["winner"]

    now = datetime.now()
    current_month = now.strftime("%B")
    current_month_lower = current_month.lower()
    current_year = now.strftime("%Y")
    current_date_human = now.strftime("%B %Y")

    lines = [
        "You are a senior SEO strategist. A global ranking across multiple",
        "sibling products identified ONE top-performing page in the last 24h, scored",
        "by a weighted composite of pageviews, email_signups, schedule_clicks,",
        "get_started_clicks, and bookings.",
        "",
        f"TODAY IS {current_date_human}.",
        "",
        "GLOBAL WINNER (source of topical momentum):",
        f"  product: {winner['product']}",
        f"  page:    {winner['page_url']}",
        f"  score:   {winner['score']}",
        f"  metrics: {json.dumps(winner['metrics'])}",
        "",
        "Your job: propose ONE NEW adjacent landing page for the TARGET PROJECT below",
        "that rides the same topical wave, adapted for that project's audience and",
        "positioning. Do NOT copy the winning slug verbatim; propose a slug and",
        "keyword that fits the target's voice and ICP.",
        "",
        "TARGET PROJECT:",
        f"  name:        {target['product']}",
        f"  domain:      {target['domain']}",
        f"  website:     {target['website']}",
        f"  positioning: {json.dumps(proj.get('qualification', {}), ensure_ascii=False)}",
        f"  description: {proj.get('description', '')}",
        "",
        "TOP 10 RANKING (for context across all projects):",
    ]
    for r in brief.get("ranking", [])[:10]:
        lines.append(f"  {r['score']:>6} {r['product']:20} {r['page_url']}")

    lines += [
        "",
        "RESEARCH FIRST (HARD REQUIREMENT, do not skip):",
        f"Your training cutoff predates {current_date_human}. You do NOT know what",
        "shipped recently. You MUST call the web_search tool before proposing",
        "anything. A proposal without web-grounded evidence is invalid output.",
        "",
        "Run AT LEAST 3 web_search queries (more is fine). Suggested queries:",
        "",
        "  1. Topical area from the global winner, filtered to recent news:",
        f'     - "<topic from winner> {current_month} {current_year}"',
        f'     - "<topic> news {current_year}" / "<topic> latest release"',
        f'     - "<vendor or category> announcement {current_month} {current_year}"',
        "  2. TARGET project's audience-specific news:",
        f'     - "<target ICP / use case> new tool {current_year}"',
        f'     - "<target category> launch {current_month} {current_year}"',
        "  3. If a specific model / product / release surfaces (e.g. a new LLM,",
        "     a new framework, a new API, a vendor announcement), search that",
        "     thing by name to confirm it shipped in the last ~30 days and pull",
        "     1-2 concrete details (version number, release date, capability claim).",
        "",
        "Use what you find. The proposed page must ride the global winner's",
        "topical momentum AND be grounded in something that demonstrably happened",
        "recently (cite the source URL in your concept field).",
        "",
        "PROPOSAL SHAPES (pick whichever best fits what your research surfaced):",
        "",
        "A. SINGLE-BLOCKBUSTER (preferred when one notable release dominates).",
        "   One post about ONE specific recent thing: a new model, product,",
        "   vendor launch, or feature drop. The keyword can be the product/model",
        '   name itself ("claude opus 4.7 deep dive") or a how-to/explainer about',
        '   it ("how to use <new thing> for <use case>"). Date does NOT need to',
        "   appear in the slug or keyword; freshness comes from the post being",
        f"   about a real {current_date_human} event.",
        "",
        "B. ROUNDUP/DIGEST (preferred when multiple notable releases happened).",
        "   Covers several recent releases in the topical area. SHOULD include",
        f'   "{current_month_lower} {current_year}" or "{current_year}" in the',
        f'   keyword/slug ("ai model releases {current_month_lower} {current_year}").',
        "",
        "C. COMPARISON / HOW-TO with a fresh hook (acceptable when one specific",
        "   recent change makes an old comparison newly relevant). Example:",
        '   "X vs Y after the new Z release". Dated phrasing optional.',
        "",
        "D. EVERGREEN comparison/how-to. Use ONLY if web_search returned no",
        "   relevant recent news. Default to A or B whenever your research",
        "   surfaced something concrete.",
        "",
        "Rules for the final proposal:",
        "- keyword must be a 3-8 word search phrase a human would actually type",
        "  for the TARGET project's audience.",
        "- slug must be kebab-case, ASCII, <= 64 chars, unique on the target site.",
        "- concept must be 1-2 sentences explaining the angle, citing the specific",
        "  news/release you found via web_search (vendor name, version, or event)",
        "  so the downstream generator can verify and write a grounded page.",
        f'- Never echo a stale month/year (any month != "{current_month_lower}"',
        f'  or year != "{current_year}") into a dated slug/keyword.',
        "",
        "WORKFLOW:",
        "  1. Run >=3 web_search queries to ground yourself in current news.",
        "  2. Once grounded, call the propose_keyword tool with your final",
        "     {keyword, slug, concept}. propose_keyword is your final action;",
        "     do not emit prose afterwards.",
    ]

    return "\n".join(lines)


PROPOSE_KEYWORD_TOOL: dict[str, Any] = {
    "name": "propose_keyword",
    "description": (
        "Submit the final keyword, slug, and concept for the new landing page. "
        "Call this AFTER running web_search at least three times. Calling this "
        "tool is your final action; do not emit prose before or after."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "keyword": {
                "type": "string",
                "description": (
                    "3-8 word search phrase a human would type, tailored to "
                    "the target project's audience."
                ),
                "minLength": 3,
            },
            "slug": {
                "type": "string",
                "description": (
                    "URL slug. kebab-case, lowercase ASCII letters/digits, "
                    "single dash separators, <= 64 chars."
                ),
                "pattern": "^[a-z0-9]+(?:-[a-z0-9]+)*$",
                "maxLength": 64,
            },
            "concept": {
                "type": "string",
                "description": (
                    "1-2 sentence angle citing the specific news/release "
                    "(vendor name, version, or event) found via web_search, "
                    "with a source URL so the downstream generator can verify."
                ),
                "minLength": 20,
            },
        },
        "required": ["keyword", "slug", "concept"],
        "additionalProperties": False,
    },
}

WEB_SEARCH_TOOL: dict[str, Any] = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": WEB_SEARCH_MAX_USES,
}


def extract_proposal(content_blocks: list[Any]) -> dict | None:
    for block in content_blocks:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", "") == "propose_keyword":
            return dict(block.input)
    return None


def propose(brief_path: str, target_product: str) -> dict:
    api_key = get_api_key()
    model = os.environ.get("CLAUDE_MODEL", "").strip() or DEFAULT_MODEL
    client = anthropic.Anthropic(api_key=api_key)

    with open(brief_path) as f:
        brief = json.load(f)
    prompt = build_prompt(brief, target_product)

    last_err: str | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        # On the final attempt, force tool_choice to propose_keyword. Earlier
        # attempts use auto so the model can run web_search freely; if it
        # finishes without calling propose_keyword, we tighten the screws.
        force_tool = attempt == MAX_RETRIES
        tool_choice: dict[str, Any] = (
            {"type": "tool", "name": "propose_keyword"} if force_tool else {"type": "auto"}
        )
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=MAX_TOKENS,
                tools=[WEB_SEARCH_TOOL, PROPOSE_KEYWORD_TOOL],
                tool_choice=tool_choice,
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.APIError as e:
            last_err = f"APIError: {type(e).__name__}: {e}"
            wait = 5 * attempt
            print(f"  attempt {attempt}/{MAX_RETRIES} {last_err}; sleeping {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue

        proposal = extract_proposal(resp.content)
        if proposal:
            usage = getattr(resp, "usage", None)
            if usage is not None:
                print(
                    f"  attempt {attempt} ok: model={model} "
                    f"input={usage.input_tokens} output={usage.output_tokens} "
                    f"web_searches={getattr(usage, 'server_tool_use', None)}",
                    file=sys.stderr,
                )
            return proposal

        last_err = (
            f"no propose_keyword tool_use; stop_reason={resp.stop_reason} "
            f"content_types={[getattr(b, 'type', '?') for b in resp.content]}"
        )
        print(f"  attempt {attempt}/{MAX_RETRIES} {last_err}", file=sys.stderr)

    print(f"ERROR: failed after {MAX_RETRIES} attempts: {last_err}", file=sys.stderr)
    sys.exit(4 if (last_err and last_err.startswith("no propose_keyword")) else 3)


def main() -> None:
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} <brief.json> <target_product>", file=sys.stderr)
        sys.exit(1)
    result = propose(sys.argv[1], sys.argv[2])
    json.dump(result, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
