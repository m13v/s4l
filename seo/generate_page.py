#!/usr/bin/env python3
"""
Unified SEO page generator.

Called by run_serp_pipeline.sh (discovery) and run_gsc_pipeline.sh (proven demand).
Also usable directly for manual/adhoc triggers. Future pipelines just call generate().

Design: no templates. Creative brief prompt + dynamic palette loaded from
`@m13v/seo-components`'s registry.json (in the consumer repo's node_modules).
Claude decides structure, angle, and content. The generator enforces
observability (stream-json tool capture) and verification (commit lands on
origin/main, live URL 200) before marking state done.

Usage:
    python3 generate_page.py --product Fazm --keyword "local ai agent" \\
        --slug local-ai-agent --trigger serp

    from generate_page import generate
    result = generate(product="Fazm", keyword="local ai agent",
                      slug="local-ai-agent", trigger="serp")
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
CONFIG_PATH = ROOT_DIR / "config.json"

# Dev-mode fallback path for the seo-components registry. In production the
# generator reads the registry from the consumer repo's node_modules copy of
# @m13v/seo-components. If that file is missing (e.g. the consumer hasn't
# upgraded yet) we fall back to the local source checkout so local dev keeps
# working. Hard error only if both are missing.
LOCAL_SEO_COMPONENTS_REGISTRY = Path.home() / "seo-components" / "registry.json"

# Load .env so DATABASE_URL is available when we import db_helpers
ENV_PATH = ROOT_DIR / ".env"
if ENV_PATH.exists():
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(ROOT_DIR / "scripts"))
import db_helpers  # noqa: E402
from http_api import api_get, api_patch, api_post  # noqa: E402
from claude_wait import wait_for_claude  # noqa: E402
from verify_facts import (  # noqa: E402
    verify_dead_urls,
    verify_time_sensitive_claims,
    extract_and_verify_factual_claims,
    verify_keyword_directly_answered,
)


CLAUDE_TIMEOUT_SECONDS = 2400  # 40 minutes. Cloud Run staging+prod chains
                               # (mk0r) sit at ~13 min each; Vercel deploys sit
                               # at ~30-90s. 1200s used to clip tag-trigger
                               # products mid-prod-deploy.


# Content-type routing. Each entry owns the route prefix, the candidate file
# paths Claude should write to, and the example directories the generator tells
# Claude to read for component-composition patterns. Adding a new type (e.g.
# "comparison", "integration") is a matter of adding a row here and, if the
# website repo has a shell component for it, teaching the prompt about it.
CONTENT_TYPES = {
    "guide": {
        "route_prefix": "/t/",
        "path_candidates": [
            "src/app/(content)/t/{slug}/page.tsx",
            "src/app/(main)/t/{slug}/page.tsx",
            "src/app/(landing)/t/{slug}/page.tsx",
            "src/app/t/{slug}/page.tsx",
            "app/(content)/t/{slug}/page.tsx",
            "app/(main)/t/{slug}/page.tsx",
            "app/(landing)/t/{slug}/page.tsx",
            "app/t/{slug}/page.tsx",
            "website/src/app/(content)/t/{slug}/page.tsx",
            "website/src/app/(main)/t/{slug}/page.tsx",
            "website/src/app/t/{slug}/page.tsx",
        ],
        "example_dirs": ["src/app/(content)/t/", "app/t/"],
        "description": "a keyword-targeted guide page",
    },
    "alternative": {
        "route_prefix": "/alternative/",
        "path_candidates": [
            "src/app/alternative/{slug}/page.tsx",
            "app/alternative/{slug}/page.tsx",
        ],
        "example_dirs": ["src/app/alternative/", "src/app/t/", "app/alternative/", "app/t/"],
        "description": "an alternative/comparison page against a competitor product",
    },
    "use_case": {
        "route_prefix": "/use-case/",
        "path_candidates": [
            "src/app/use-case/{slug}/page.tsx",
            "app/use-case/{slug}/page.tsx",
        ],
        "example_dirs": ["src/app/use-case/", "src/app/t/", "app/use-case/", "app/t/"],
        "description": "a use-case page describing one specific job the product does",
    },
    "cross_roundup": {
        "route_prefix": "/best/",
        "path_candidates": [
            "src/app/best/{slug}/page.tsx",
            "src/app/(content)/best/{slug}/page.tsx",
            "src/app/(main)/best/{slug}/page.tsx",
            "app/best/{slug}/page.tsx",
            "app/(content)/best/{slug}/page.tsx",
            "app/(main)/best/{slug}/page.tsx",
            "website/src/app/best/{slug}/page.tsx",
        ],
        "example_dirs": ["src/app/best/", "src/app/t/", "app/best/", "app/t/"],
        "description": "a dated best-of listicle for the host's niche, built from real competitor research. The host appears only when it honestly belongs in the top results, and a small sibling appendix is optional (default zero) when sibling projects genuinely fit the audience.",
    },
    "blog_post": {
        "route_prefix": "/blog/",
        # The blog uses a single dynamic route src/app/blog/[slug]/page.tsx
        # that ships every post via generateStaticParams. Per-post content is
        # an HTML string in src/app/blog/_content/<slug>.ts (default export);
        # metadata goes in src/app/blog/_manifest.ts. The model writes the
        # content module; the pipeline then patches the manifest to add the
        # entry. We point path_candidates at the content module so the
        # existence + commit-landed gates fire on the file the model actually
        # wrote.
        "path_candidates": [
            "src/app/blog/_content/{slug}.ts",
            "app/blog/_content/{slug}.ts",
        ],
        "example_dirs": ["src/app/blog/_content/", "app/blog/_content/"],
        "description": "a blog post on a single dynamic route. Content lives in src/app/blog/_content/<slug>.ts as an HTML string default export; metadata is patched into src/app/blog/_manifest.ts by the pipeline after the commit lands.",
    },
}


_ALTERNATIVE_RE = re.compile(
    r"\b(vs|alternative|alternatives|replacement|replace|competitor|competitors)\b",
    re.IGNORECASE,
)


def classify_content_type(keyword: str) -> str:
    """Cheap regex classifier. Defaults to 'guide' (safe fallback, no shell).

    Conservative on purpose: misrouting a keyword to the wrong shell is worse
    than leaving it on the general /t/ guide path. Expand the patterns as we
    build more page-type shells in the website repo.
    """
    kw = (keyword or "").lower().strip()
    if _ALTERNATIVE_RE.search(kw):
        return "alternative"
    return "guide"


def pick_neutral_family(slug: str) -> str:
    """Deterministic per-slug pick from Tailwind's perceptually-similar
    neutral families. Same slug always returns the same family (so re-runs
    and /improve passes do not flip the palette under the model). Different
    slugs on the same site cycle through the four families, breaking the
    visual sameness where every page is text-zinc-* on bg-zinc-*.
    """
    families = ("zinc", "slate", "neutral", "stone")
    if not slug:
        return "zinc"
    h = hashlib.md5(slug.encode("utf-8")).digest()
    return families[h[0] % len(families)]


def detect_consumer_theme(repo_path: str) -> str:
    """Return 'dark' or 'light' based on the consumer repo's root layout."""
    if not repo_path:
        return "light"
    candidates = [
        Path(repo_path) / "src" / "app" / "layout.tsx",
        Path(repo_path) / "app" / "layout.tsx",
        Path(repo_path) / "src" / "app" / "(main)" / "layout.tsx",
    ]
    for p in candidates:
        try:
            content = p.read_text()
        except (FileNotFoundError, OSError):
            continue
        if 'data-theme="dark"' in content or 'className="dark"' in content:
            return "dark"
        return "light"
    return "light"


def layout_candidates_for_check(root: Path) -> list[Path]:
    return [
        root / "src" / "app" / "layout.tsx",
        root / "app" / "layout.tsx",
    ]


def check_consumer_setup(repo_path: str) -> dict:
    """
    Verify the consumer repo has the @seo/components infrastructure installed
    before we try to ship a page into it. A missing piece means generated pages
    render with white-on-dark component backgrounds, unreadable FaqSection
    headers, no sidebar, and no guide chat (see setup-client-website Phase
    2c/2d/4a/4d/4e).

    Returns {"ok": bool, "missing": [reasons]}. If ok=False, generation must
    refuse until the consumer site is onboarded.
    """
    if not repo_path or not os.path.isdir(repo_path):
        return {"ok": False, "missing": ["repo missing on disk"]}

    root = Path(repo_path)
    missing: list[str] = []

    # Phase 2c: cascade layer ordering so library CSS wins over consumer theme
    globals_css_candidates = [
        root / "src" / "app" / "globals.css",
        root / "app" / "globals.css",
    ]
    globals_css = next((p for p in globals_css_candidates if p.exists()), None)
    if not globals_css:
        missing.append("globals.css not found (Phase 2c)")
    else:
        css_text = globals_css.read_text(errors="ignore")
        if "@layer seo-components" not in css_text:
            missing.append(
                f"{globals_css.relative_to(root)} missing "
                "'@layer seo-components, theme, base, components, utilities' (Phase 2c)"
            )
        has_source_pragma = (
            "@source" in css_text and "seo-components" in css_text
        )
        if not has_source_pragma:
            missing.append(
                f"{globals_css.relative_to(root)} missing "
                "'@source \"../../node_modules/@seo/components/src\"' pragma (Phase 2c). "
                "Without it Tailwind v4 never scans the library and utilities "
                "used inside @seo/components render unstyled."
            )
        # SeoComponentsStyles is the legacy second-stylesheet pattern. It injects
        # duplicates of .hidden / .xl:flex inside @layer seo-components, which
        # loses to the consumer's @layer utilities and forces GuideChatPanel /
        # SitemapSidebar to display:none forever. The @source pragma above
        # already makes this component unnecessary, so flag any lingering usage.
        for lay_p in layout_candidates_for_check(root):
            if lay_p.exists() and "SeoComponentsStyles" in lay_p.read_text(
                errors="ignore"
            ):
                missing.append(
                    f"{lay_p.relative_to(root)} still renders <SeoComponentsStyles /> "
                    "(Phase 2d). Remove it — it collides with @layer utilities "
                    "and forces GuideChatPanel/SitemapSidebar to display:none."
                )
                break

    # Phase 4a: withSeoContent wrapper so /api/guide-chat can read MDX at runtime
    next_cfg_candidates = [
        root / "next.config.ts",
        root / "next.config.mjs",
        root / "next.config.js",
    ]
    next_cfg = next((p for p in next_cfg_candidates if p.exists()), None)
    if not next_cfg:
        missing.append("next.config.* not found (Phase 4a)")
    else:
        cfg_text = next_cfg.read_text(errors="ignore")
        if "withSeoContent" not in cfg_text:
            missing.append(
                f"{next_cfg.name} missing withSeoContent wrapper (Phase 4a)"
            )

    # Phase 4d/4e: sidebar + guide chat + api route mounted in layout
    layout_candidates = layout_candidates_for_check(root)
    layout = next((p for p in layout_candidates if p.exists()), None)
    if not layout:
        missing.append("layout.tsx not found (Phase 2d)")
    else:
        lay_text = layout.read_text(errors="ignore")
        if "HeadingAnchors" not in lay_text:
            missing.append(
                f"{layout.relative_to(root)} missing HeadingAnchors import (Phase 2d)"
            )
        if "SiteSidebar" not in lay_text and "SitemapSidebar" not in lay_text:
            missing.append(
                f"{layout.relative_to(root)} missing SiteSidebar mount (Phase 4d)"
            )
        if "GuideChat" not in lay_text and "GuideChatPanel" not in lay_text:
            missing.append(
                f"{layout.relative_to(root)} missing GuideChat mount (Phase 4e)"
            )

    api_route_candidates = [
        root / "src" / "app" / "api" / "guide-chat" / "route.ts",
        root / "app" / "api" / "guide-chat" / "route.ts",
        root / "src" / "app" / "api" / "guide-chat" / "route.tsx",
    ]
    api_route = next((p for p in api_route_candidates if p.exists()), None)
    if not api_route:
        missing.append("src/app/api/guide-chat/route.ts not found (Phase 4e)")

    # Phase 4a/4e: contentDir in next.config.* (withSeoContent wrapper) and
    # api/guide-chat/route.ts (createGuideChatHandler) must match. If they drift
    # (e.g. route group added to one but not the other), the chat claims "no
    # guides" at runtime and the page-gen pipeline writes MDX to a dir the
    # runtime never scans.
    content_dir_re = re.compile(r'contentDir\s*:\s*"([^"]+)"')
    cfg_dir = None
    route_dir = None
    if next_cfg and next_cfg.exists():
        m = content_dir_re.search(next_cfg.read_text(errors="ignore"))
        if m:
            cfg_dir = m.group(1)
    if api_route and api_route.exists():
        m = content_dir_re.search(api_route.read_text(errors="ignore"))
        if m:
            route_dir = m.group(1)
    if cfg_dir and route_dir and cfg_dir != route_dir:
        missing.append(
            f"contentDir mismatch (Phase 4a/4e): "
            f"{next_cfg.name} has {cfg_dir!r} but "
            f"{api_route.relative_to(root)} has {route_dir!r}. "
            "Both must point to the same directory for withSeoContent's "
            "build-time manifest to match the runtime guide-chat handler."
        )

    return {"ok": len(missing) == 0, "missing": missing}


def load_product_config(product: str) -> dict:
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    lower = product.lower()
    for p in cfg.get("projects", []):
        if p["name"].lower() == lower:
            return p
    raise SystemExit(f"Product '{product}' not found in config.json")


def resolve_seo_author(product_cfg: dict) -> dict:
    """Author/byline that every generated SEO page MUST use.

    Resolution order: project's `seo_author` -> defaults.seo_author from
    config.json -> hardcoded fallback. Never invent or copy from a seed page.
    """
    project_author = product_cfg.get("seo_author")
    if isinstance(project_author, dict) and project_author.get("name"):
        return project_author
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        default_author = cfg.get("defaults", {}).get("seo_author")
        if isinstance(default_author, dict) and default_author.get("name"):
            return default_author
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        pass
    return {
        "name": "Matthew Diakonov",
        "role": "Written with AI",
        "url": "https://m13v.com",
    }


def resolve_source_paths(product_cfg: dict) -> list[dict]:
    """Return list of {path, description} with paths expanded and existence-checked."""
    sources = product_cfg.get("landing_pages", {}).get("product_source", [])
    out = []
    for s in sources:
        raw = s.get("path", "")
        path = os.path.expanduser(raw)
        out.append({
            "path": path,
            "description": s.get("description", "").strip(),
            "exists": os.path.isdir(path),
        })
    return out


def build_cross_roundup_block(host_cfg: dict) -> str:
    """Return a prompt block for the cross_roundup content type.

    The page is a real best-of listicle in the host's niche, built from
    competitor research the model performs at run time. It is NOT a
    portfolio promo and the host is NOT auto-ranked #1 on its own site.
    Sibling projects from config.json are passed in as an OPTIONAL pool
    for a small appendix; the default behavior is to include zero of them
    when no sibling honestly fits the host audience.
    """
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    host_slug = (host_cfg.get("name") or "").lower()
    siblings = []
    for p in cfg.get("projects", []):
        if (p.get("name") or "").lower() == host_slug:
            continue
        if not p.get("website"):
            continue
        sr = p.get("seo_roundup") or {}
        category = sr.get("category") or ""
        siblings.append({
            "name": p["name"],
            "slug": (p["name"] or "").lower(),
            "website": p.get("website", ""),
            "category": category,
            "tagline": p.get("tagline") or p.get("description") or "",
            "get_started_link": p.get("get_started_link") or p.get("website", ""),
            "booking_link": p.get("booking_link") or "",
        })
    host_sr = host_cfg.get("seo_roundup") or {}
    host_category = host_sr.get("category") or ""
    siblings_json = json.dumps(siblings, indent=2, default=str)
    host_name = host_cfg.get("name") or ""
    return (
        "=== CROSS-ROUNDUP INPUT ===\n"
        f"Host product: {host_name} ({host_cfg.get('website','')})\n"
        f"Host niche (use this in the H1 and <title>): {host_category}\n"
        "Today's date is the date the page is generated. Use it in the H1 as:\n"
        '  "Best {host_niche} for {Month} {Day}, {Year}"\n'
        "Slug must include YYYY-MM-DD and lives under /best/<slug>.\n\n"
        "## What this page is\n\n"
        f"A first-party best-of for the host's actual niche ({host_category}),\n"
        "published on the host's own domain. Readers searching this query\n"
        "expect to see the real top players in this category with criteria\n"
        "they can verify. They also expect, on a first-party site, to see\n"
        "the host's own product featured. Lead with the host, then cover\n"
        "the rest of the category honestly. Be transparent about the\n"
        "first-party framing in the intro; do not pretend to be a neutral\n"
        "third-party publication.\n\n"
        "## Step A, do this BEFORE drafting the page\n\n"
        "1. Run WebSearch for the host niche query and at least one variant.\n"
        "   Examples: 'best <host niche>', 'top <host niche> services',\n"
        "   '<host niche> reviews', '<host niche> vs alternatives'. Read the\n"
        "   top 5 to 10 results that rank today.\n"
        "2. Build a candidate list of 5 to 8 REAL competitors in this niche.\n"
        "   These are the businesses, services, or products a buyer for the\n"
        "   host would actually compare against. For each one, capture:\n"
        "     name, homepage URL, what they do, who they serve,\n"
        "     pricing or scope signal if it is public,\n"
        "     and at least one verifiable fact from their site or coverage\n"
        "     (year founded, team size, named clients, public case study,\n"
        "     pricing tier, scope of deliverables, etc.).\n"
        "   If you cannot find a verifiable fact for a candidate, drop it\n"
        "   from the list. Do not invent or paraphrase plausible-sounding\n"
        "   facts.\n"
        "3. Pick a clear, defensible ranking criterion and state it in the\n"
        "   intro (e.g. scope of work done for the buyer, price tier,\n"
        "   client profile fit, time commitment from the buyer). Rank the\n"
        "   competitors against that single criterion using their actual\n"
        "   public information, not vibes.\n\n"
        "## Host inclusion rules\n\n"
        f"- {host_name} is featured first by default. This is the host's\n"
        "  own domain; readers expect the publisher's product up top, the\n"
        "  same way every first-party 'best of' page on the web works.\n"
        f"  Open the ranked list with {host_name} as entry #1.\n"
        f"- The {host_name} entry must be honest, not hype. Give a real\n"
        "  description of what it does, who it serves best, the criterion\n"
        "  it leads on, and at least one verifiable fact (year founded,\n"
        "  scope, public clients, pricing tier, capability). One short\n"
        "  honest line acknowledging tradeoffs (who it is NOT for) is\n"
        "  encouraged; it builds trust and helps the page rank.\n"
        "- Competitors follow ranked by the stated criterion using their\n"
        "  actual public information. If a competitor genuinely beats the\n"
        "  host on a specific buyer profile or sub-criterion, say so\n"
        "  plainly inside that competitor's entry. Honest tradeoffs make\n"
        "  the page more useful and rank better long-term than pure\n"
        "  promo.\n"
        "- Do not invent superlatives for the host (no 'the #1 X', 'the\n"
        "  best X in the world'). The host leads the list because it is\n"
        "  the publisher; let the reader judge from concrete facts.\n\n"
        "## Optional sibling appendix (escape hatch)\n\n"
        "Below is the list of OTHER projects we run. They are NOT in this\n"
        "niche. You MAY add a small appendix at the bottom of the page\n"
        "(label it clearly, e.g. 'Tools we use alongside this category')\n"
        "with 0 to 3 sibling picks, ONLY when:\n"
        "  - The sibling genuinely fits the host audience's daily workflow.\n"
        "    'A buyer of this might also like SaaS' is not enough.\n"
        "  - You can write a tight, honest one-paragraph reason for the\n"
        "    pick from the host audience's point of view.\n"
        "  - Including the appendix does not undermine the page's\n"
        "    credibility as a real best-of for the niche.\n\n"
        "DEFAULT BEHAVIOR: include ZERO siblings. A page with no appendix\n"
        "is the correct outcome whenever no sibling truly fits. Do NOT pad\n"
        "to make the page longer or to add cross-promo. Do NOT include a\n"
        "sibling solely to fill the appendix slot.\n\n"
        "Sibling projects (JSON, OPTIONAL pool, default zero picks):\n\n"
        f"{siblings_json}\n\n"
        "## Mandatory rules for this content type\n\n"
        "  1. H1 MUST be exactly: 'Best <host niche> for <Month Day, Year>'.\n"
        "     The <title> meta should mirror the H1. Use today's real date.\n"
        "  2. The ranked list is REAL competitors found in Step A. Each\n"
        "     entry must include the competitor's real name, a working\n"
        "     homepage link, a 2-4 sentence neutral description, and at\n"
        "     least one verifiable fact you found in research.\n"
        "  3. Aim for 5 to 8 ranked entries. Do not pad to a round number\n"
        "     with weak picks. Fewer strong entries beats more filler.\n"
        "  4. External competitor links use a plain anchor with\n"
        "     target=\"_blank\" rel=\"noopener noreferrer nofollow\". Do NOT\n"
        "     wrap competitor links in trackCrossProductClick. They are\n"
        "     not our products.\n"
        "  5. IF (and only if) you include the optional sibling appendix,\n"
        "     each sibling entry's CTA MUST call trackCrossProductClick on\n"
        "     click. Import from @seo/components:\n"
        "       import { trackCrossProductClick } from \"@seo/components\";\n"
        "     Call with:\n"
        "       trackCrossProductClick({\n"
        f"         site: \"{host_slug}\",\n"
        "         targetProduct: <sibling slug>,\n"
        "         destination: <sibling get_started_link>,\n"
        "         text: <visible button text>,\n"
        "         component: \"CrossRoundupEntry\",\n"
        "         section: <e.g. \"appendix-1\">,\n"
        "       });\n"
        "     The <a href> on the button is the sibling's get_started_link.\n"
        "     Use target=\"_blank\" rel=\"noopener noreferrer\".\n"
        "  6. The HOST product's CTA (if used) stays as the normal primary\n"
        "     CTA: GetStartedCTA, trackGetStartedClick, or BookCallCTA. Do\n"
        "     NOT fire cross_product_click for the host's own button.\n"
        "  7. Visual layout: numbered rank cards, GlowCard, or BentoGrid\n"
        "     for ranked entries. The optional sibling appendix should\n"
        "     look visually distinct (smaller, lower on the page, separate\n"
        "     header) so readers do not confuse it with the ranked list.\n"
        "  8. Intro paragraph must state the date plainly AND the ranking\n"
        "     criterion. Readers should see the date match for 'best X\n"
        "     April 2026' and immediately understand how the list is sorted.\n"
        "  9. No fabricated stats. Every number in the ranked list must\n"
        "     trace to a source you opened during Step A. If you cannot\n"
        "     verify a number, leave it out.\n"
        "=== END CROSS-ROUNDUP INPUT ===\n"
    )


def format_source_block(sources: list[dict]) -> str:
    if not sources:
        return ("(no external product source configured for this product)\n"
                "Treat the website repo as the product surface. Read widely in it "
                "for landing copy, component implementations, fixtures, and data.")
    parts = []
    for s in sources:
        missing = "" if s["exists"] else " [MISSING ON DISK — do not try to read]"
        parts.append(f"- {s['path']}{missing}\n  {s['description']}")
    return "\n\n".join(parts)


def load_component_registry(repo_path: str) -> dict:
    """Load registry.json that describes the @m13v/seo-components palette.

    The registry is the single source of truth for which components exist and
    how they are described to Claude. It ships inside the npm package at
    `node_modules/@m13v/seo-components/registry.json`. Fall back to the local
    seo-components checkout during development.
    """
    candidates = [
        Path(repo_path) / "node_modules" / "@m13v" / "seo-components" / "registry.json",
        LOCAL_SEO_COMPONENTS_REGISTRY,
    ]
    for p in candidates:
        if p.is_file():
            with open(p) as f:
                return json.load(f)
    tried = "\n  ".join(str(p) for p in candidates)
    raise SystemExit(
        "registry.json not found for @m13v/seo-components. Tried:\n"
        f"  {tried}\n"
        "Upgrade @m13v/seo-components to >= 0.10.0 in the consumer repo, or "
        "ensure the local source checkout has a registry.json."
    )


def _components_in_group(registry: dict, group_key: str) -> list[dict]:
    return [c for c in registry.get("components", []) if c.get("group") == group_key]


def _components_with_quota_tag(registry: dict, tag: str) -> list[dict]:
    out = []
    for c in registry.get("components", []):
        if tag in (c.get("quotaTags") or []):
            out.append(c)
    return out


def _quota_names(registry: dict, tag: str) -> list[str]:
    """Expand quota-tagged components into the specific React component names.

    An entry like 'AnimatedMetric / MetricsRow' ships a `componentNames` list
    so each underlying component gets enumerated in the quota line.
    """
    names: list[str] = []
    for c in _components_with_quota_tag(registry, tag):
        for n in c.get("componentNames") or [c["name"]]:
            if n not in names:
                names.append(n)
    return names


def render_palette(registry: dict) -> str:
    """Render the '### Available components' block from the registry."""
    lines: list[str] = []
    header = registry.get("header", "### Available components")
    lines.append(header)
    lines.append("")

    for group in registry.get("groups", []):
        items = _components_in_group(registry, group["key"])
        mode = group.get("renderMode", "list")
        heading = group["heading"]

        if mode == "inline":
            names = ", ".join(c["name"] for c in items)
            trailing = group.get("trailingNote", "")
            trailing_part = f", {trailing}" if trailing else ""
            lines.append(f"{heading}: {names}{trailing_part}")
            lines.append("")
        else:
            lines.append(heading)
            for c in items:
                sig = c.get("signature", "").strip()
                desc = c.get("description", "").strip()
                sig_part = f" {sig}" if sig else ""
                sep = " — " if desc else ""
                lines.append(f"- `{c['name']}`{sig_part}{sep}{desc}")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_quotas(registry: dict) -> str:
    """Render the '### Mandatory component diversity' block with dynamic names.

    Rule text is policy and stays in this generator. Only the component name
    lists are pulled from the registry, so adding a new magic-ui component with
    `quotaTags: ['magic-mandatory']` will extend the quota automatically.
    """
    video_names = ", ".join(f"`{n}`" for n in _quota_names(registry, "video-mandatory"))
    magic_names = ", ".join(f"`{n}`" for n in _quota_names(registry, "magic-mandatory"))
    visual_rich_names = ", ".join(_quota_names(registry, "visual-rich-mandatory"))

    return f"""### Component diversity (soft floor, taste over quotas)

A page that is wall-to-wall prose is thin. A page that uses every component in the library is generic. Aim for a small palette chosen for the angle.

1. **Video-style components are optional**, not required. The available ones are: {video_names}. Use `RemotionClip` ONLY when the angle genuinely benefits from a 4-6 caption motion intro (a behavior unfolding, a sequence the reader cannot picture from words alone). `MotionSequence` is for longer visual narratives. If neither fits the angle, do not use one. Do not insert a video component just because the page would otherwise feel too plain.

2. **Magic UI components are optional**, not required. The available ones are: {magic_names}. Use 0 to 3, and only when each one earns its place against the angle:
   - `ShimmerButton` only as a primary CTA, never as decoration.
   - `GradientText` only on a meaningful word or phrase in a heading.
   - `Marquee` only for genuine "works with" or "trusted by" strips with real items.
   - `AnimatedBeam` only for a real "inputs, system, outputs" story.
   - `OrbitingCircles` only for ecosystem or integrations stories.
   - `NumberTicker` only inside a metric section with concrete numbers.
   - `BackgroundGrid` only when the hero or a callout actually needs a textured surface.
   Never reach for a Magic UI component to satisfy a quota; if none of the above match the angle, use zero.

3. **At least TWO components total** from the "Visual content" and "Rich layout and animation" groups ({visual_rich_names}). Two is the floor, not the target. Pick components that match the angle's natural shape (a tutorial wants step components, an argument wants comparison or before/after, a reference wants tables or diagrams). Do not stack five visual components when two carry the page.

The cheapest way to satisfy any of these rules is to drop in BackgroundGrid + GradientText + RemotionClip + a generic Bento. Do not do that. The page should look unlike the other pages on the same site. Distinctiveness beats coverage.
"""


_SEO_IMPORT_RE = re.compile(
    r"import\s*\{([^}]+)\}\s*from\s*['\"]@seo/components['\"]",
    re.MULTILINE | re.DOTALL,
)
# Each generated SEO page declares `const PUBLISHED = "YYYY-MM-DD"` near the
# top. That string is set at generation time and survives /improve passes,
# so it is a far more reliable "when was this page born" signal than the
# file mtime (which gets clobbered by git pulls, bulk component bumps, and
# improve_page.py rewrites).
_PUBLISHED_RE = re.compile(
    r"""const\s+PUBLISHED\s*=\s*['"](\d{4}-\d{2}-\d{2})['"]""",
)
_SCHEMA_NAMES = {
    "articleSchema",
    "breadcrumbListSchema",
    "faqPageSchema",
    "videoObjectSchema",
    "howToSchema",
    "withBookingAttribution",
}
# Trust signals are required (or recommended) so do not penalize them in the
# overuse anti-quota; otherwise every page would trip the warning on Breadcrumbs.
_ALWAYS_ALLOWED = {
    "Breadcrumbs",
    "ArticleMeta",
    "FaqSection",
    "ProofBand",
    "ProofBanner",
    "BookCallCTA",
    "InlineCta",
    "InlineCTA",
    "StickyBottomCta",
    "StickyBottomCTA",
    "GetStartedCTA",
    "RelatedPostsGrid",
    "AnimatedSection",
}


def _all_seo_content_dirs() -> list[str]:
    """Union of every example_dir across all CONTENT_TYPES, deduped while
    preserving order. The anti-quota scans the entire SEO surface of the
    consumer site so a `guide` page sees the components used by the
    `alternative` and `use_case` pages too (those are written by the same
    pipeline and the visual sameness is shared across content types).
    """
    seen: list[str] = []
    for ct in CONTENT_TYPES.values():
        for d in ct.get("example_dirs", []):
            if d not in seen:
                seen.append(d)
    return seen


def _scan_recent_components(repo: str, example_dirs: list[str] | None = None,
                            limit: int = 25) -> tuple[list[tuple[str, int]], int]:
    """Scan the consumer site's SEO pages and return (component_counts, n_scanned).

    Selection rule: walk all SEO content dirs (across all content types,
    not just the current one), keep only pages that actually import from
    `@seo/components` (so non-SEO routes that happen to live under the same
    tree do not contaminate the sample), and rank by recency. Recency is
    the `const PUBLISHED = "YYYY-MM-DD"` constant the generator stamps
    into every page; mtime is used only as a fallback when PUBLISHED is
    missing. mtime alone is unreliable because git pulls and bulk
    `@m13v/seo-components` bumps reset every page's mtime in unison.

    Counts each component once per page (presence, not raw mention count)
    so a page that uses NumberTicker eight times still contributes 1 to
    the NumberTicker score.
    """
    if not repo:
        return [], 0
    repo_root = Path(repo).expanduser()
    dirs = list(example_dirs) if example_dirs else _all_seo_content_dirs()
    seen_paths: set[Path] = set()
    candidates: list[Path] = []
    for d in dirs:
        base = repo_root / d
        if not base.exists():
            continue
        try:
            for p in base.rglob("page.tsx"):
                if p not in seen_paths:
                    seen_paths.add(p)
                    candidates.append(p)
        except (OSError, PermissionError):
            continue
    if not candidates:
        return [], 0

    # Pull the PUBLISHED date and the @seo/components import block in a
    # single read pass per file. Drop pages with no SEO import block at
    # all (they are not pages this anti-quota cares about).
    enriched: list[tuple[Path, str | None, float, set[str]]] = []
    for p in candidates:
        try:
            text = p.read_text(errors="ignore")
        except (OSError, PermissionError):
            continue
        names: set[str] = set()
        for m in _SEO_IMPORT_RE.finditer(text):
            for raw in m.group(1).split(","):
                name = raw.strip().split(" as ")[0].strip()
                if not name or name in _SCHEMA_NAMES or name in _ALWAYS_ALLOWED:
                    continue
                if not name[:1].isupper():
                    continue
                names.add(name)
        if not names and "@seo/components" not in text:
            # Not an SEO page at all (e.g. a layout, a non-SEO route under
            # the same tree). Skip it so it does not waste a slot.
            continue
        pub_match = _PUBLISHED_RE.search(text)
        published = pub_match.group(1) if pub_match else None
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0.0
        enriched.append((p, published, mtime, names))

    if not enriched:
        return [], 0

    # Primary sort: PUBLISHED date desc (pages without one rank below
    # pages with one). Secondary: mtime desc as a tiebreaker / fallback.
    enriched.sort(
        key=lambda e: (e[1] or "0000-00-00", e[2]),
        reverse=True,
    )
    enriched = enriched[:limit]

    counts: dict[str, int] = {}
    for _, _, _, names in enriched:
        for name in names:
            counts[name] = counts.get(name, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return ranked, len(enriched)


def render_anti_quota(repo: str, example_dirs: list[str] | None = None) -> str:
    """Render the 'forbidden / discouraged components on this site' block.

    Scans the consumer site's recent generated SEO pages across ALL content
    types (guide, alternative, use_case, cross_roundup), ranks
    visual/Magic UI components by frequency of appearance, and tells the
    model not to reuse the most over-used ones. This is the main lever
    against pages on the same site all looking alike: even if every other
    rule is satisfied, the top five components from recent history cannot
    show up on the next page.

    `example_dirs` is accepted for back-compat but ignored by default; the
    scan now unions every CONTENT_TYPES example_dir so the anti-quota
    reflects the full SEO surface of the site, not just the current
    content type's lane.
    """
    counts, pages_scanned = _scan_recent_components(repo)
    if not counts:
        return ""
    forbidden = [name for name, _ in counts[:5]]
    discouraged = [name for name, _ in counts[5:10]]
    forbidden_str = ", ".join(f"`{n}`" for n in forbidden) if forbidden else "(none)"
    discouraged_str = ", ".join(f"`{n}`" for n in discouraged) if discouraged else "(none)"

    return f"""### Anti-quota: do NOT reuse the most over-used components on this site

Recent pages on this consumer site lean heavily on the same components, which is why every page looks like every other page. The last {pages_scanned} generated SEO pages on this site (ranked by `const PUBLISHED` date, falling back to file mtime when PUBLISHED is missing) show the following imbalance:

- **DO NOT use** any of: {forbidden_str}. These are the five components this site has leaned on most. Pick a different way to express the same idea (a different component from the palette, a prose section, a code block, a custom local component, or no visual at all).
- **Use sparingly** (at most one of these, and only if it is the obviously right shape for your angle): {discouraged_str}.

Trust signals (Breadcrumbs, ArticleMeta, FaqSection, ProofBand, BookCallCTA, related-posts grid, inline CTAs) are exempt from this rule; use them as the trust-signal section directs.

If your angle genuinely cannot be expressed without a forbidden component, prefer to (a) use a local component already in `src/components/` of this repo, or (b) write a custom one-off component for this page rather than reaching for the shared library default. Diversity at the visual layer is the goal.
"""


def render_content_guardrails(product_cfg: dict) -> str:
    """Emit hard content rules for products that define content_guardrails.

    This is the only place where per-product content policy reaches the LLM.
    The voice block is marketing guidance; this block is a set of do-not-cross
    lines for traditions or domains that require it (e.g. the Goenka Vipassana
    tradition reserves technique transmission for authorized teachers at
    10-day courses, so the site must not teach the technique).
    """
    cg = product_cfg.get("content_guardrails") or {}
    if not cg:
        return ""
    lines: list[str] = ["## Content guardrails (hard rules, non-negotiable)", ""]
    summary = (cg.get("summary") or "").strip()
    if summary:
        lines.append(summary)
        lines.append("")
    do_not = cg.get("do_not") or []
    if do_not:
        lines.append("### Do NOT")
        for item in do_not:
            lines.append(f"- {item}")
        lines.append("")
    do_instead = cg.get("do_instead") or []
    if do_instead:
        lines.append("### Do instead")
        for item in do_instead:
            lines.append(f"- {item}")
        lines.append("")
    forbidden = cg.get("forbidden_phrases") or []
    if forbidden:
        lines.append("### Forbidden phrases (must not appear in the page)")
        for item in forbidden:
            lines.append(f"- {item!r}")
        lines.append("")
    redirect = (cg.get("redirect_language") or "").strip()
    if redirect:
        lines.append("### Redirect policy")
        lines.append(redirect)
        lines.append("")
    lines.append(
        "A page that violates any rule in this block must not be written. "
        "If the keyword itself forces a violation to answer it, stop and report "
        "`{\"success\": false, \"error\": \"content_guardrail_violation: <reason>\"}` "
        "as the final JSON instead of shipping a page."
    )
    return "\n".join(lines).rstrip() + "\n"


_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "in", "on", "for", "to",
    "with", "by", "as", "is", "are", "was", "were", "be", "been", "being",
    "do", "does", "did", "have", "has", "had", "this", "that", "these",
    "those", "it", "its", "you", "your", "i", "we", "our", "us", "they",
    "them", "their", "from", "at", "into", "than", "then", "so", "if",
    "what", "how", "why", "when", "where", "who", "vs", "vs.", "best",
}


def _keyword_tokens(text: str) -> set[str]:
    """Return the lowercased non-stopword tokens of a keyword/title."""
    if not text:
        return set()
    raw = re.split(r"[^a-z0-9]+", text.lower())
    return {t for t in raw if t and len(t) > 2 and t not in _STOPWORDS}


def render_existing_pages_inventory(product: str, keyword: str,
                                    new_slug: str,
                                    pages: list[dict] | None = None,
                                    max_show: int = 60) -> str:
    """Render the inventory of already-built pages for the model to choose from.

    Output is a fenced-code-block-shaped table; the model uses it inside
    "Step 0 — Reuse, redirect, or build new" to decide whether to write a
    new page or consolidate into an existing one.

    Inventory is filtered by token overlap with the incoming keyword/slug so
    a 400-page site does not flood the prompt. We always include the most
    recent ~10 done pages too, so the model sees the current cadence of work.

    Returns the empty string if there is nothing to show (a brand-new
    product), in which case Step 0 simply tells the model "no inventory yet,
    build new" and proceeds.
    """
    if pages is None:
        try:
            pages = db_helpers.list_done_pages(product, limit=400)
        except Exception as e:
            # Don't block page generation on a DB read; fall through to "no
            # inventory" so the model still gets Step 0 instructions.
            print(f"  inventory lookup failed (continuing without): {e}",
                  file=sys.stderr)
            pages = []

    if not pages:
        return ""

    new_tokens = _keyword_tokens(keyword) | _keyword_tokens(new_slug)

    scored: list[tuple[int, dict]] = []
    for p in pages:
        if not p.get("slug") or p["slug"] == new_slug:
            continue
        ptokens = _keyword_tokens(p.get("keyword") or "") | _keyword_tokens(p["slug"])
        overlap = len(new_tokens & ptokens)
        scored.append((overlap, p))

    # Sort by overlap desc, then by completed_at desc (already pre-sorted).
    scored.sort(key=lambda r: r[0], reverse=True)

    selected: list[dict] = []
    seen_slugs: set[str] = set()

    # First: relevant siblings (overlap >= 1).
    for overlap, p in scored:
        if overlap < 1:
            break
        if p["slug"] in seen_slugs:
            continue
        selected.append({**p, "_overlap": overlap})
        seen_slugs.add(p["slug"])
        if len(selected) >= max_show:
            break

    # Then: pad with most-recent pages (regardless of overlap) so the model
    # sees the current shape/cadence even when the new keyword is genuinely
    # off-axis from anything we've shipped.
    if len(selected) < max_show:
        for p in pages[:20]:
            if p["slug"] in seen_slugs:
                continue
            selected.append({**p, "_overlap": 0})
            seen_slugs.add(p["slug"])
            if len(selected) >= max_show:
                break

    if not selected:
        return ""

    lines = [
        "## EXISTING PAGES INVENTORY",
        "",
        f"There are {len(pages)} pages already shipped on this site. The "
        f"{len(selected)} most relevant to the new keyword `{keyword}` are "
        "below (top of the list = highest token overlap with this keyword; "
        "the bottom rows are the most-recent pages so you also see the "
        "cadence of recent work). `overlap` is the count of shared "
        "non-stopword tokens.",
        "",
        "```",
        f"{'overlap':<8} {'slug':<60} keyword",
    ]
    for p in selected:
        slug = (p["slug"] or "")[:60]
        kw = (p.get("keyword") or "")[:80]
        lines.append(f"{p['_overlap']:<8} {slug:<60} {kw}")
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def render_reuse_or_new_decision(product: str, keyword: str, slug: str,
                                 page_url: str,
                                 has_inventory: bool) -> str:
    """Step 0 block: tell the model to choose new vs consolidate vs improve.

    This is the load-bearing block. It defines the three exit shapes and
    walks through bad/good examples so the model has concrete patterns to
    match on. Inserted before Step 1 so the decision happens before any
    research budget is spent.
    """
    if not has_inventory:
        # Brand-new product or empty DB: skip the decision and just tell the
        # model to proceed; Step 1 will handle the rest.
        return (
            "## Step 0 — New page or update existing?\n\n"
            "There is no inventory of existing pages on this site yet. "
            "Proceed to Step 1 and build the page.\n\n"
        )

    return (
        "## Step 0 — Reuse, redirect, or build new (decide BEFORE researching)\n"
        "\n"
        "Look at the EXISTING PAGES INVENTORY above. The site already has a "
        "library of pages. Your job is NOT to reflexively add another file. "
        "It is to decide which of three actions actually serves the reader "
        "for this keyword.\n"
        "\n"
        "### The three options\n"
        "\n"
        "**A. Build a new page.** Pick this when no existing page meaningfully "
        "covers the user's literal question, OR when an existing sibling page "
        "is on a related but genuinely distinct angle that would be diluted "
        "by being merged.\n"
        "\n"
        "**B. Consolidate (redirect to an existing page).** Pick this when "
        "an existing page already answers (or is the natural home for) the "
        "literal question behind this keyword. Don't ship a near-duplicate. "
        "Instead, redirect this slug to the existing one, and if the existing "
        "page is missing the specific facet this keyword implies, add a "
        "small section to it. End-state: ONE page on the topic, two slugs "
        "pointing to it.\n"
        "\n"
        "**C. Escalate (skip).** Pick this when the keyword is a content-"
        f"guardrail violation, a semantic mismatch with {product}, or "
        "demands data we cannot honestly produce. Use the ESCALATION RUBRIC "
        "above.\n"
        "\n"
        "### How to choose\n"
        "\n"
        "1. From the inventory, identify the top 1-3 candidate slugs whose "
        f"keyword shares meaningful intent with `{keyword}`. High `overlap` "
        "is a hint, not a verdict; low overlap can still be a duplicate "
        "if the topics are synonyms (e.g. 'changelog' / 'release notes' / "
        "'app updates' / 'what's new').\n"
        "2. Open the top 1-3 candidate pages with `Read` (path under "
        "`src/app/(content)/t/<slug>/page.tsx` or `src/app/t/<slug>/page.tsx`). "
        "Skim the H1, lede, and section headers. Ignore the styling.\n"
        "3. Ask: *if a user typed this exact keyword into Google and landed "
        "on the existing page, would they find their literal answer in the "
        "first 30% of the page?* If YES, you are duplicating. Choose B. If "
        "NO and no existing page is the natural home, choose A.\n"
        "\n"
        "### Bad examples (real, do not repeat)\n"
        "\n"
        "- We shipped 15+ separate pages on this site for variants of the "
        "same Fazm-changelog intent (`fazm-changelog`, `fazm-release-notes`, "
        "`fazm-app-updates`, `fazm-new-features`, `fazm-version-history`, "
        "`open-source-ai-released-april-2026`, etc.). Each ranked for nothing, "
        "fragmented internal links, and produced a maintenance tax. Correct "
        "choice would have been: ONE canonical changelog page, every other "
        "variant 308-redirected to it.\n"
        "- We shipped both `/t/anthropic-vat-number` and "
        "`/t/anthropic-pbc-vat-number` with substantially the same content. "
        "Two pages, same answer. Correct choice: keep the one with the "
        "stronger title (`anthropic-pbc-vat-number`), redirect the other to "
        "it, and merge any unique facts before deleting.\n"
        "\n"
        "### Good examples\n"
        "\n"
        "- Keyword `fazm pricing 2026` arrives, inventory already has "
        "`fazm-pricing`. Open `fazm-pricing/page.tsx`. It already says "
        "'$20/mo, 7-day trial, updated 2026-04-15'. Choose B: redirect "
        "`/t/fazm-pricing-2026` -> `/t/fazm-pricing` (308). If the existing "
        "page does not show the 2026 update, add it as a 'Pricing as of "
        "2026' section in the same edit.\n"
        "- Keyword `fazm vs cluely` arrives, inventory has `fazm-vs-rewind` "
        "and `fazm-vs-granola` but no Cluely page. Different competitor, "
        "different angle, different proof points. Choose A: build the new "
        f"page at `/t/{slug}`.\n"
        "- Keyword `how does fazm capture screen on macos` arrives, inventory "
        "has `fazm-screen-recording-permission` (a permissions guide). The "
        "permissions guide answers a different literal question (how to grant "
        "access). Choose A: build the new page about the capture pipeline. "
        "Add an internal link from each page to the other.\n"
        "\n"
        "### If you choose B (consolidate)\n"
        "\n"
        f"1. Pick the target slug from the inventory. Call it `<TARGET_SLUG>`.\n"
        "2. Edit the consumer repo's `next.config.ts` (or `.mjs`/`.js`). "
        "Find the `async redirects()` block and append a permanent redirect:\n"
        "   ```ts\n"
        f"   {{ source: \"/t/{slug}\", destination: \"/t/<TARGET_SLUG>\", permanent: true }},\n"
        "   ```\n"
        "   `permanent: true` emits a 308 so search engines and existing "
        "links transfer authority to the canonical page.\n"
        "3. Read the existing target page. If your keyword implies a fact "
        "or sub-topic the target page does not currently surface, add a "
        "small section (1-3 paragraphs, or a row in an existing comparison "
        "table) so the redirect is honest, not a bait-and-switch. Update "
        "`dateModified` on the target page if you changed it.\n"
        "4. Do NOT create a `page.tsx` for the new slug. The redirect handles "
        "the URL. The build will fail on a 'page.tsx exists AND a redirect "
        "rule for the same path exists' conflict; only one of the two should "
        "exist.\n"
        "5. Commit both files (`next.config.ts`, optionally the target page) "
        "in a single commit with message "
        f"`fix(seo): consolidate {slug} -> <TARGET_SLUG>`. The auto-commit "
        "agent will push within ~60s, but you should commit yourself to keep "
        "the diff coherent.\n"
        "6. Report this final JSON shape (single line, nothing after it):\n"
        "   ```\n"
        "   {\"success\": true, \"action\": \"consolidate\", "
        f"\"slug\": \"{slug}\", "
        "\"target_slug\": \"<TARGET_SLUG>\", "
        f"\"target_url\": \"<absolute URL of /t/<TARGET_SLUG> on "
        f"{page_url.rsplit('/t/', 1)[0] if '/t/' in page_url else page_url}>\", "
        "\"rationale\": \"<one sentence: why these are the same intent>\", "
        "\"target_page_edited\": true|false}\n"
        "   ```\n"
        "   When you emit this shape, the post-generation gates that look "
        "for a new page file are skipped. The pipeline marks the new "
        "keyword done with notes pointing at the canonical slug.\n"
        "\n"
        "### If you choose A (build new)\n"
        "\n"
        "Proceed to Step 1. The rest of this prompt assumes A.\n"
        "\n"
        "### If you choose C (escalate)\n"
        "\n"
        "Use `seo/escalate.py open` per the ESCALATION RUBRIC above and "
        "exit. Do not ship a page.\n"
        "\n"
        "Make the choice now (in your own working memory; you do not have "
        "to print it). If A or C, continue. If B, do the consolidation "
        "steps and stop.\n\n"
    )


def render_book_call_block(product_cfg: dict, registry: dict) -> str:
    """Emit the BookCallCTA requirement block if the product has a Cal.com link.

    Reads `booking_link` from config.json. If unset, returns an empty string
    (the page will still render; no booking CTA will be injected). If present,
    we require two BookCallCTA instances: one `footer` near the end of the
    article body and one `sticky` that follows the reader on scroll.
    """
    booking = (product_cfg.get("booking_link") or "").strip()
    if not booking:
        return ""
    has_book_cta = bool(_components_with_quota_tag(registry, "book-call-mandatory"))
    if not has_book_cta:
        return ""
    site_slug = product_cfg.get("name") or product_cfg.get("slug") or ""
    return f"""### Book-a-call CTA (mandatory)

This product has a booking link: `{booking}`.

Every page MUST render exactly TWO `BookCallCTA` instances from `@seo/components`, both pointing at that booking link. They are tracked by the canonical `schedule_click` PostHog event, which feeds the Project Funnel Stats dashboard.

1. **Footer variant** near the end of the article body (after the last prose section, before the FAQ):
   ```tsx
   <BookCallCTA
     appearance="footer"
     destination="{booking}"
     site="{site_slug}"
     heading="<one-line hook tailored to the angle>"
     description="<one sentence explaining what the call unlocks>"
   />
   ```

2. **Sticky variant** so the CTA follows the reader on long pages:
   ```tsx
   <BookCallCTA
     appearance="sticky"
     destination="{booking}"
     site="{site_slug}"
     description="<short benefit-oriented line>"
   />
   ```

Do NOT hard-code `href="https://cal.com/..."` in a raw `<a>` tag or copy the booking URL into a custom button; always use `BookCallCTA` so the PostHog event fires with the canonical shape. Do NOT render more than two BookCallCTA instances per page.

**Booking attribution is automatic.** `BookCallCTA` (and `InlineCta` / `StickyBottomCta` with `trackAs="schedule"`) rewrite the Cal.com URL at click time, appending `metadata[utm_source]=<hostname>`, `metadata[utm_medium]=schedule_click`, and `metadata[utm_campaign]=<pathname>`. Cal.com mirrors those into the booking payload, our webhook writes them to `cal_bookings.utm_source / utm_medium / utm_campaign`, and the top-pages pipeline uses `utm_campaign` to score bookings per landing page. If you build a custom Book-a-Call CTA for any reason, you MUST route its href through `withBookingAttribution` from `@seo/components` or page-level booking attribution breaks.
"""


# Deploy verification blocks — selected per-project from
# `landing_pages.deploy.target` and `production_trigger` in config.json.
# Use `__PAGE_URL__`, `__STAGING_URL__`, `__STAGING_PAGE_URL__`, and
# `__TAG_PATTERN__` placeholders so we can splice them in with `str.replace()`
# without fighting f-string brace escaping.

VERCEL_DEPLOY_BLOCK = """## Step 5 — Typecheck, commit, deploy, and verify the Vercel build

This step is a strict gate. You may not report success until a Vercel production deploy for your commit reaches state `READY`. Local typecheck passing is not enough. A 200 on the page is not enough. You must confirm the deploy state.

**5a. Typecheck (mandatory, pre-commit).**

- Run `npx tsc --noEmit` in the repo. If it reports ANY error, fix the code you introduced and re-run until clean. Never commit on a failing typecheck.
- Common trap: `@seo/components` props are strictly typed. Pass primitives where primitives are expected (e.g., `SequenceDiagram.actors: string[]`, `SequenceDiagram.messages[].from: number`, `OrbitingCircles.items: ReactNode[]`). Confirm by reading the component source under `node_modules/@m13v/seo-components/src/components/` or `~/seo-components/src/components/` if the props shape is non-obvious.

**5b. Commit and push.**

- Stage the new page file (and any new shared components you added).
- Commit on the current branch with a clear message naming the keyword.
- Push to origin main (or whatever the repo's main branch is).
- Confirm the commit is on origin. Record the 7-char SHA.

**5c. Poll Vercel for deploy status (mandatory).**

If the repo has `.vercel/project.json`, run this polling loop from bash. Get the token once:

```
VERCEL_TOKEN=$(python3 -c "import json; print(json.load(open('/Users/matthewdi/Library/Application Support/com.vercel.cli/auth.json'))['token'])")
PROJECT_ID=$(python3 -c "import json; print(json.load(open('.vercel/project.json'))['projectId'])")
TEAM_ID=$(python3 -c "import json; print(json.load(open('.vercel/project.json'))['orgId'])")
SHA=<your 40-char commit SHA from git rev-parse HEAD>
```

Then poll (up to 30 attempts, 10s apart = ~5 min budget):

```
curl -s -H "Authorization: Bearer $VERCEL_TOKEN" \\
  "https://api.vercel.com/v6/deployments?teamId=$TEAM_ID&projectId=$PROJECT_ID&target=production&limit=10" \\
  | python3 -c "import sys,json; d=json.load(sys.stdin); [print(x['state'], x.get('meta',{}).get('githubCommitSha',''), x['uid'], x.get('inspectorUrl','')) for x in d['deployments']]"
```

Find the row matching your $SHA. Terminal states are `READY` (good), `ERROR` (failed), `CANCELED` (retry may be needed). `BUILDING`, `QUEUED`, `INITIALIZING` — keep polling.

**5d. If deploy fails, fix and retry (up to 2 times).**

If the state is `ERROR`, fetch the build log and fix the problem:

```
curl -s -H "Authorization: Bearer $VERCEL_TOKEN" \\
  "https://api.vercel.com/v3/deployments/<deploy_uid>/events?teamId=$TEAM_ID&builds=1&direction=backward&limit=100" \\
  | python3 -c "import sys,json; [print(e.get('payload',{}).get('text','')) for e in json.load(sys.stdin) if e.get('type') in ('stdout','stderr','command','build-error')]"
```

Read the log. The last few error lines usually show the file + line that broke the prerender. Do NOT guess: locate the exact file the build complains about, read it, and fix only what's broken (typically your new page, but occasionally a shared component you added).

After fixing, re-run `npx tsc --noEmit`, commit, push, then poll again with the new SHA. Budget: at most 2 self-heal iterations. If you cannot get a READY state in 2 tries, STOP and report `success: false` with the deploy error message as the reason. Do not paper over the failure with `success: true`; a false success here corrupts the DB and blocks future generations.

**5e. Final live-URL sanity check.**

Only after the Vercel deploy is `READY`, run `curl -sI -o /dev/null -w "%{http_code}\\n" __PAGE_URL__` — you should see 200. If it's still 404 after 30s (Vercel alias propagation), wait 30s more and retry once. If still 404, report the problem instead of silently succeeding.
"""


CLOUDRUN_PUSH_DEPLOY_BLOCK = """## Step 5 — Typecheck, commit, deploy, and verify the Cloud Run build

This product deploys to Cloud Run via GitHub Actions on push to `main`. This step is a strict gate. You may not report success until the Cloud Run deploy run reaches `completed: success`. Local typecheck passing is not enough. A 200 on the page is not enough.

**5a. Typecheck (mandatory, pre-commit).**

- Run `npx tsc --noEmit` in the repo. If it reports ANY error, fix the code you introduced and re-run until clean. Never commit on a failing typecheck.
- Common trap: `@seo/components` props are strictly typed. Pass primitives where primitives are expected. Confirm by reading the component source under `node_modules/@m13v/seo-components/src/components/` or `~/seo-components/src/components/` if the props shape is non-obvious.

**5b. Commit and push.**

- Stage the new page file (and any new shared components you added).
- Commit with a clear message naming the keyword.
- `git push origin main`. Confirm the commit is on origin. Record the 7-char SHA.

**5c. Watch the Cloud Run deploy via GitHub Actions (mandatory).**

The repo has `.github/workflows/deploy-cloudrun.yml` triggered on push to main. Find the run for your commit:

```
sleep 5  # let GitHub register the push
gh run list --branch main --limit 5
```

Match the row whose commit message matches yours (or `git log -1 --format=%s`). Then watch it:

```
gh run watch <run-id> --exit-status
```

This blocks until the run completes (~5-7 min typical). Exit 0 means success, non-zero means failure.

**5d. If deploy fails, fix and retry (up to 2 times).**

If `gh run watch` exits non-zero, view the failing log:

```
gh run view <run-id> --log-failed | tail -120
```

The last error chunk usually shows the file + line that broke the build (most often your new page). Fix only what's broken. After fixing, re-run `npx tsc --noEmit`, commit, push, watch the new run.

Budget: at most 2 self-heal iterations. If you cannot get a successful run in 2 tries, STOP and report `success: false` with the deploy error message. Do not paper over the failure with `success: true`.

**5e. Final live-URL sanity check.**

Only after the Cloud Run deploy run completes successful, run:

```
curl -sI -o /dev/null -w "%{http_code}\\n" __PAGE_URL__
```

You should see 200. If still 404 after 30s, wait 30s and retry once. If still 404, report the problem instead of silently succeeding.
"""


CLOUDRUN_TAG_DEPLOY_BLOCK = """## Step 5 — Typecheck, commit, deploy via tag, and verify the Cloud Run build

This product deploys to Cloud Run with TWO triggers: push to `main` builds STAGING, and pushing a tag matching `__TAG_PATTERN__` builds PRODUCTION. The fast path is to push BOTH refs in a single `git push` so staging and production deploys run in parallel. Doing them sequentially (push, watch, tag, watch) doubles the wall clock and can clip the per-page timeout.

This step is a strict gate. You may not report success until the production tag deploy reaches `completed: success`.

**5a. Typecheck (mandatory, pre-commit).**

- Run `npx tsc --noEmit`. Fix any errors before committing.
- `@seo/components` props are strictly typed; verify the component source if a prop shape is non-obvious.

**5b. Commit, tag, and push BOTH refs in one operation.**

- Stage the new page file. Commit on `main` with a clear message naming the keyword.
- Compute the next tag, matching the existing pattern (`__TAG_PATTERN__`):
  ```
  latest=$(git tag --list 'v*' --sort=-v:refname | head -1)
  echo "latest=$latest"
  # Bump the patch component. e.g. v0.3.42 -> v0.3.43.
  # If no v* tags exist, start at v0.0.1.
  next="<computed>"
  ```
  Create the tag on the new commit:
  ```
  git tag $next
  ```
- Push BOTH refs simultaneously (this is the parallelism trick):
  ```
  git push origin main $next
  ```
- Confirm both refs landed on origin. Record the 7-char commit SHA and the tag name.

**5c. Watch the production deploy via GitHub Actions (mandatory).**

The single push triggers two parallel workflow runs: one for `main` (staging) and one for the tag (production). Find them:

```
sleep 5
gh run list --limit 8
```

Two new runs should be in_progress. Identify them by the commit message and the branch / display name (the production one will reference your new tag). Watch the production run:

```
gh run watch <prod-run-id> --exit-status
```

This blocks ~5-7 min. Exit 0 = success, non-zero = failure. You can ignore the staging run; if it failed but production passed, the tag deploy is still good (production is what ships).

**5d. If the production deploy fails, fix and retry (up to 2 times).**

```
gh run view <prod-run-id> --log-failed | tail -120
```

Fix the broken file. Then commit, compute the NEXT tag (do not reuse the failing tag), push both refs again, watch the new production run.

Budget: at most 2 self-heal iterations. If you cannot get a successful production run in 2 tries, STOP and report `success: false`.

**5e. Final live-URL sanity check.**

Optional fast pre-check on staging (does not block):

```
curl -sI -o /dev/null -w "staging:%{http_code}\\n" __STAGING_PAGE_URL__
```

Mandatory production check after `gh run watch` succeeded:

```
curl -sI -o /dev/null -w "%{http_code}\\n" __PAGE_URL__
```

Should be 200. If still 404 after 30s, wait 30s and retry once. If still 404, report the problem.
"""


MANUAL_DEPLOY_BLOCK = """## Step 5 — Typecheck, commit, push, and stop

This product has manual deploy: there is no automated production deploy on push. Commit and push the page; deploy is out-of-band.

**5a. Typecheck (mandatory, pre-commit).**

- Run `npx tsc --noEmit`. Fix any errors before committing.

**5b. Commit and push.**

- Stage the new page file. Commit with a clear message naming the keyword.
- `git push origin main`. Record the 7-char SHA.

**5c. Live URL probe (best-effort).**

Run:

```
curl -sI -o /dev/null -w "%{http_code}\\n" __PAGE_URL__
```

If it already returns 200, great. If not, do not block here — the upstream caller has its own probe budget and will retry. There is no `gh run watch` to perform; skip 5d/5e.

Report `success: true` after the push lands and you have a SHA, even if the URL is not yet 200.
"""


def build_prompt(product: str, keyword: str, slug: str, trigger: str,
                 product_cfg: dict, source_block: str,
                 content_type: str = "guide",
                 human_guidance: str | None = None,
                 setup_missing: list[str] | None = None) -> str:
    repo = os.path.expanduser(product_cfg.get("landing_pages", {}).get("repo", ""))
    website = (product_cfg.get("landing_pages", {}).get("base_url")
               or product_cfg.get("website", ""))
    differentiator = product_cfg.get("differentiator", "")
    accent_cfg = product_cfg.get("landing_pages", {}).get("accent", {})
    accent_hex = accent_cfg.get("hex", "")
    accent_hex_dark = accent_cfg.get("hex_dark", "")
    accent_name = accent_cfg.get("name", "teal")
    accent_gradient_from = accent_cfg.get(
        "gradient_from",
        "cyan" if accent_name == "teal" else accent_name,
    )

    seo_author = resolve_seo_author(product_cfg)
    author_name = seo_author.get("name", "Matthew Diakonov")
    author_role = seo_author.get("role", "Written with AI")
    author_url = seo_author.get("url", "https://m13v.com")

    trigger_context = {
        "serp": "This topic came from discovery. Top-ranking pages leave a real gap, and the product fits the commercial intent behind this search.",
        "gsc": "This query is already driving impressions to the site in Google Search Console. Real users are searching for this. Capture the demand.",
        "manual": "This is an adhoc trigger. Treat the keyword as worth building.",
        "reddit": "This page is being created to drop into a high-performing Reddit thread. Match the thread audience's vocabulary and pain points; the page should genuinely help someone who landed on it from a Reddit comment.",
        "twitter": "This page is being created to drop into the primary reply on a high-momentum X (Twitter) thread. Match the thread audience's vocabulary and pain points; the page should genuinely help someone who landed on it from an X reply. Keep it concise and skimmable, and assume the reader arrived via mobile.",
        "roundup": "This is a weekly best-of listicle for the host's niche. The SEO target is the freshness query 'best <host niche> <Month Year>'. Build it from real competitor research, not from the portfolio of products we run. Readers landing here expect a balanced, objective view of the actual category. The host product is included only when an honest reading of the chosen ranking criterion places it in the top results, and is never ranked #1 by default.",
    }.get(trigger, "")

    ct = CONTENT_TYPES.get(content_type, CONTENT_TYPES["guide"])
    route_prefix = ct["route_prefix"]
    # Prefer a candidate whose anchored app-router root (src/app or app) exists
    # in this repo, so consumer sites with a bare app/ router (no src/) get a
    # primary_path that lands inside their actual app, instead of being told
    # to write into a non-existent src/app tree (which Next.js would silently
    # ignore in favor of the bare app/ router).
    has_src_app = os.path.isdir(os.path.join(repo, "src", "app"))
    has_bare_app = os.path.isdir(os.path.join(repo, "app"))
    primary_path = ct["path_candidates"][0].format(slug=slug)
    for tmpl in ct["path_candidates"]:
        if tmpl.startswith("src/app/") and has_src_app:
            primary_path = tmpl.format(slug=slug)
            break
        if tmpl.startswith("app/") and has_bare_app and not has_src_app:
            primary_path = tmpl.format(slug=slug)
            break
    example_dirs_str = ", ".join(f"`{repo}/{d}`" for d in ct["example_dirs"])
    page_url = f"{website.rstrip('/')}{route_prefix}{slug}"

    registry = load_component_registry(repo)
    palette_block = render_palette(registry)
    quotas_block = render_quotas(registry)
    anti_quota_block = render_anti_quota(repo)
    book_call_block = render_book_call_block(product_cfg, registry)
    guardrails_block = render_content_guardrails(product_cfg)
    consumer_theme = detect_consumer_theme(repo)
    neutral = pick_neutral_family(slug)

    # Inventory of already-shipped pages on this site, plus the Step 0
    # decision block that tells the model to pick consolidate-vs-build-new
    # BEFORE spending research budget. Together these prevent the
    # multi-slug-same-topic clusters we shipped on Fazm (changelog, VAT,
    # etc.) where N keywords with non-overlapping slugs all became
    # near-duplicate pages.
    inventory_block = render_existing_pages_inventory(product, keyword, slug)
    reuse_block = render_reuse_or_new_decision(
        product, keyword, slug,
        page_url=f"{website.rstrip('/')}{ct['route_prefix']}{slug}",
        has_inventory=bool(inventory_block),
    )

    type_context = {
        "guide": "This is a general guide/explainer page. You have the most creative freedom here — the angle, section shape, and length are all yours.",
        "alternative": f"This is an alternative/comparison page. Readers arrived by searching for a competitor product. Your job is to show them {product} is the better pick for the use case their keyword implies. Read an existing alternative page in `{repo}/src/app/alternative/` to see if a shell component exists (e.g. AlternativePageShell) — if it does, use it and emit only a typed data object. If no shell exists in this repo, compose raw sections using the trust-signal components below.",
        "use_case": f"This is a use-case page describing one concrete job {product} does. Readers want to know whether {product} can handle their specific workflow. Show them, with at least one anchor_fact drawn from real product source. If a UseCasePageShell exists in `{repo}/src/components/seo/`, prefer it; otherwise compose raw sections.",
        "cross_roundup": f"This is a dated best-of listicle hosted on {product}'s domain, covering {product}'s actual niche. Target query: \"best <host niche> <Month Year>\". See the CROSS-ROUNDUP INPUT block below for the niche, mandatory H1/title format, host inclusion rules, the optional sibling appendix, and the trackCrossProductClick wiring (only used if the appendix is included). DO Step 1 research as written below; the spine of this page is real competitor research from the live SERP, plus the additional Step A in the CROSS-ROUNDUP INPUT block. The page must read as a balanced, objective best-of for the niche, not as a portfolio promo.",
        "blog_post": (
            f"This is a blog post on {product}'s blog at /blog/<slug>. The blog uses ONE dynamic "
            f"route, `{repo}/src/app/blog/[slug]/page.tsx`, that pre-renders every post via "
            f"`generateStaticParams`. You do NOT touch the route file. What you write is the "
            f"per-post content module:\\n\\n"
            f"  Path: `{repo}/src/app/blog/_content/{slug}.ts`\\n"
            f"  Shape: a single TypeScript file that ends with\\n"
            f"    ```\\n"
            f"    const HTML_CONTENT = `...your html body...`;\\n"
            f"    export default HTML_CONTENT;\\n"
            f"    ```\\n\\n"
            f"Read any existing module under `{repo}/src/app/blog/_content/` to see the exact "
            f"shape and the way backticks/${{}} are escaped. CRITICAL RULES for the body: it is "
            f"HTML (not markdown) because `BlogPostLayout` renders the string via "
            f"dangerouslySetInnerHTML. Use real HTML tags (`<h1>`, `<h2>`, `<h3>`, `<p>`, `<ul>`, "
            f"`<ol>`, `<li>`, `<a href=\"...\">`, `<strong>`, `<em>`, `<blockquote>`, `<code>`, "
            f"`<pre>`). Tables MUST use real `<table><thead><tr><th>...</th></tr></thead>"
            f"<tbody><tr><td>...</td></tr></tbody></table>` markup — pipe-style markdown tables "
            f"will NOT render. Inside the template literal, escape backticks as `\\\\`` and "
            f"escape `${{` interpolation as `\\\\${{`.\\n\\n"
            f"After writing `{slug}.ts`, ALSO append a metadata entry to "
            f"`{repo}/src/app/blog/_manifest.ts`. Read the file, find the `BLOG_MANIFEST` array, "
            f"and insert your new object so the array stays sorted by `date` descending. The "
            f"object must look exactly like the existing entries (single line, double-quoted "
            f"strings): "
            f"`{{ slug: \"<slug>\", title: \"...\", description: \"...\", date: \"YYYY-MM-DD\", "
            f"lastModified: \"YYYY-MM-DD\" or undefined, author: \"...\", tags: [\"a\", \"b\"], "
            f"image: \"...\" or undefined, readingTime: \"N min read\" }}`. Without the manifest "
            f"entry the post will not appear in /blog, RSS, or tag pages.\\n\\n"
            f"Commit both files in the same commit. Skip the palette/quotas/anti-quota guidance "
            f"below; those are for `/t/` guide pages and don't apply to the blog."
        ),
    }.get(content_type, "")

    cross_roundup_block = ""
    if content_type == "cross_roundup":
        cross_roundup_block = build_cross_roundup_block(product_cfg)

    guidance_block = ""
    if human_guidance:
        # Prepended at the very top so the model cannot miss it. The reply
        # came from a human after a previous escalation; treat it as binding
        # context, not a suggestion. The text is verbatim; do not summarize
        # or interpret -- just follow the instructions.
        guidance_block = (
            "=== HUMAN GUIDANCE (escalation reply) ===\n"
            "A previous attempt to build this page was blocked and escalated\n"
            "to a human. The human's reply is below. Treat it as a binding\n"
            "instruction. If it tells you to apply a specific phase of\n"
            "setup-client-website, do that first. If it says 'skip', mark the\n"
            "row done with notes='skipped per human guidance' and exit\n"
            "without writing any page.\n\n"
            f"{human_guidance}\n"
            "=== END HUMAN GUIDANCE ===\n\n"
        )

    setup_self_heal_block = ""
    if setup_missing:
        # Injected when the consumer-site setup gate found a small number of
        # missing pieces (below the escalation threshold). The model is
        # expected to apply the relevant phases of the setup-client-website
        # skill before doing anything else, then continue with the page
        # build. Hard-cap the tool budget so a broken setup can't burn the
        # whole page-gen budget on yak-shaving.
        bullet_lines = "\n".join(f"  - {item}" for item in setup_missing)
        setup_self_heal_block = (
            "=== SETUP SELF-HEAL (do this BEFORE Step 1) ===\n"
            f"This consumer site is missing {len(setup_missing)} piece(s) of\n"
            "@seo/components / analytics setup. Fix them before writing the\n"
            "page. Budget: <=30 tool calls for setup work. If you can't fix\n"
            "it within that budget, escalate (see ESCALATION RUBRIC below)\n"
            "instead of half-fixing it.\n\n"
            "Missing:\n"
            f"{bullet_lines}\n\n"
            "How: invoke the `setup-client-website` skill and apply only\n"
            "the phases that match the missing items above (typically\n"
            "Phase 2c/2d for mounts and Phase 4a/4d/4e for analytics).\n"
            "Verify with `python3 ~/social-autoposter/scripts/check_analytics_wiring.py`\n"
            "(must show this product as OK) before continuing.\n"
            "=== END SETUP SELF-HEAL ===\n\n"
        )

    escalation_rubric_block = (
        "=== ESCALATION RUBRIC ===\n"
        "If you hit a wall this prompt cannot solve, escalate to a human.\n"
        "DO NOT silently give up, write a stub, or invent facts.\n\n"
        "How to escalate (single shell command, then exit cleanly):\n"
        "  python3 ~/social-autoposter/seo/escalate.py open \\\n"
        f"    --product \"{product}\" \\\n"
        f"    --keyword \"{keyword}\" \\\n"
        f"    --slug \"{slug}\" \\\n"
        "    --trigger model_initiated \\\n"
        "    --reason \"<one to three sentences: what you tried, what's blocking>\"\n\n"
        "After escalating, exit. The pipeline will pause this row and resume\n"
        "it once a human replies (their reply lands as HUMAN GUIDANCE on the\n"
        "next attempt).\n\n"
        "VALID reasons to escalate:\n"
        "  - The keyword and product are semantically incompatible and no\n"
        "    honest angle exists (e.g. keyword is about a topic the product\n"
        "    genuinely does not address).\n"
        "  - Required source files / scripts referenced in this prompt do\n"
        "    not exist on disk and you cannot find equivalents.\n"
        "  - The repo is broken in a way the setup-client-website skill\n"
        "    cannot repair (e.g. unknown framework, corrupted package.json,\n"
        "    custom build system you don't understand).\n"
        "  - You hit a real auth/API wall (missing credential, revoked token,\n"
        "    403 from an external service that requires human intervention).\n"
        "  - Setup self-heal is required and you cannot complete it within\n"
        "    the 30 tool-call budget.\n\n"
        "INVALID reasons (push through instead):\n"
        "  - 'I'm not sure which file to read' -> read more files.\n"
        "  - 'The first WebSearch returned weak results' -> try other queries.\n"
        "  - 'A tool returned an error once' -> retry, read the error, adapt.\n"
        "  - 'This is taking many tool calls' -> volume alone is not a block.\n"
        "  - 'I want to confirm the angle is good' -> commit and ship.\n\n"
        "Threshold: only escalate after you have made at least 10 substantive\n"
        "tool calls trying to make forward progress on the actual blocker.\n"
        "Process noise (linting, log reads, unrelated failures) does not count.\n"
        "=== END ESCALATION RUBRIC ===\n\n"
    )

    cfg_json = json.dumps(product_cfg, indent=2, ensure_ascii=False)

    # Per-project deploy verification block. Selected from
    # `landing_pages.deploy.target` and `production_trigger`. The result is
    # a plain string spliced into the outer f-string as `{deploy_block}`.
    deploy_cfg = (product_cfg.get("landing_pages") or {}).get("deploy") or {}
    deploy_target = (deploy_cfg.get("target") or "vercel").lower()
    production_trigger = (deploy_cfg.get("production_trigger") or "push:main").lower()
    staging_url = (deploy_cfg.get("staging_url") or "").rstrip("/")
    website_no_slash = website.rstrip("/")
    staging_page_url = (
        page_url.replace(website_no_slash, staging_url, 1)
        if staging_url and website_no_slash and page_url.startswith(website_no_slash)
        else ""
    )

    if deploy_target == "vercel":
        deploy_block = VERCEL_DEPLOY_BLOCK
    elif deploy_target == "cloudrun" and production_trigger.startswith("tag:"):
        deploy_block = CLOUDRUN_TAG_DEPLOY_BLOCK
    elif deploy_target == "cloudrun":
        deploy_block = CLOUDRUN_PUSH_DEPLOY_BLOCK
    elif deploy_target == "manual":
        deploy_block = MANUAL_DEPLOY_BLOCK
    else:
        # Unknown target -> safest is the strict Vercel lane; the prompt's
        # 5c/5d will simply find no .vercel/project.json and the agent will
        # fall back to the live URL probe.
        deploy_block = VERCEL_DEPLOY_BLOCK

    deploy_block = (
        deploy_block.replace("__PAGE_URL__", page_url)
        .replace("__STAGING_PAGE_URL__", staging_page_url or page_url)
        .replace("__STAGING_URL__", staging_url)
        .replace("__TAG_PATTERN__", deploy_cfg.get("production_trigger", "tag:v*"))
    )

    return f"""{guidance_block}{setup_self_heal_block}{escalation_rubric_block}You are building one SEO page for {product}. You decide the angle and the content. Your one job is to find something real about the product that no competitor page mentions, and build a page around that.

CONTENT TYPE: {content_type} ({ct['description']})
{type_context}

KEYWORD: "{keyword}"
SLUG: "{slug}"
PRODUCT: {product}
WEBSITE: {website}
REPO (your current working directory): {repo}
DIFFERENTIATOR: {differentiator}

TRIGGER: {trigger}
{trigger_context}

Full product config (authoritative source for description, voice, ICP, personas, features, messaging, founders, proof points, qualification, pricing). Treat fields here as the canonical positioning. Do not invent claims that contradict this block; do quote verbatim from `voice.examples`, `messaging.lead_with_pain / solution / proof`, and `proof_points` when they fit the page's angle.

```json
{cfg_json}
```

{guardrails_block}
{cross_roundup_block}
{inventory_block}
{reuse_block}
## Step 1 — Find an angle no competitor has

Before you write anything, do research. Budget ~15 minutes for this step. It matters more than the writing.

1a. Read the product source.

{source_block}

These are not prompts to extract facts from. They are where the real implementation, real behavior, and real constraints live. Open files. Trace what happens when the product actually does the thing the keyword describes. You are looking for something specific a reader would not find anywhere else.

1b. Run scripts for real data.

If the product has a `scripts/` folder in any of the paths above, look there. That is where database queries, analytics pulls, and data exports live. Run what is available instead of trying to connect to databases directly. Real numbers from the product beat invented benchmarks.

1c. Check what currently ranks.

Use WebSearch for "{keyword}" and read the top 5 results that rank today. Note what they all cover. Note what they all miss. Your angle should be in the gap.

1d. Commit to an angle.

Pick ONE specific thing the product does that is not covered by the top-ranking pages. That thing is the spine of your page.

## Step 2 — Write the concept

Before writing any code, output this block (prose, not JSON, not a code fence):

CONCEPT
  angle: <one sentence describing the specific product behavior your page is built around>
  source: <the exact file path or script command you verified this from>
  anchor_fact: <one concrete, checkable thing — a file name, a number, a specific behavior — that makes the page uncopyable>
  competitor_gap: <what the top-ranking pages miss that your angle fills>
  direct_answer_shape: lookup | explanatory
  literal_question: <the one specific question a user types into Google when they search this keyword, in 8 words or fewer>
  literal_answer: <the actual datum, OR "no public answer; verified via <source> on <YYYY-MM-DD>"; this MUST appear prominently on the page>
  authoritative_source: <URL of the service you checked the literal_answer against>

If you cannot fill in all eight lines with specific non-generic answers, stop and do more research. Do not proceed to Step 3 with a generic concept. The Direct-answer block (see below) is mandatory — pages that punt on the literal question fail the post-generation gate.

### SEO jargon must not appear in the rendered page

The words "SERP", "keyword", "search intent", "search results", "top 10", "top 5", "ranking", "rank for", and "SEO" are INTERNAL vocabulary for this prompt. Never write them into the page body, section headers, FAQ answers, hero captions, schema descriptions, or meta descriptions. They break authenticity: a reader who arrived via Google should not be reminded that you wrote this for Google.

When you need to reference competing content, write it the way a subject-matter expert would: "every other guide on this", "most articles about X", "the pages that currently rank", "the existing playbooks", "common advice online", or paraphrase entirely. When you need to reference the query itself, write "this topic", "this question", or the actual subject. If a sentence cannot be rewritten without using one of the forbidden words, rewrite the sentence.

## Step 3 — Pick your component palette

You are working in an existing website repo with a shared SEO component library (`@seo/components`). Import everything from `@seo/components`. If the repo also has local components (e.g. in `@/components/`), you may use those too.

{palette_block}
### Differentiation rule

**Do NOT clone the structure of existing pages.** Read one existing page in {example_dirs_str} ONLY to understand the import syntax and color conventions. Do NOT copy its section ordering, component selection, or layout pattern.

Each page must feel editorially distinct. Think about the SHAPE the angle wants, not which named components to drop in. The shape is what the reader's eye traces down the page.

- A "how it works" angle wants the reader to walk through one execution: think a sequence the eye follows top to bottom, with the moments where state changes called out visually. Pick whichever components actually express that walk.
- A "vs. competitors" angle wants two things compared side by side, with one clear differentiator that the reader can verify. Pick whichever components make the comparison legible without flattening it into a generic feature matrix.
- A "deep dive" angle wants to slow the reader down on one specific mechanism. Long-form prose with one or two visuals at the right moment beats a bento grid of shallow points.
- A "getting started" angle wants a sequence of concrete actions with verifiable output after each step. The shape is steps and outputs, not a feature tour.
- A "feature showcase" angle wants the reader to leave knowing what one specific thing this product does that nothing else does. If a bento grid is the answer, fine, but only if every cell carries weight.
- A "reference" or "data" angle wants dense lookup material (tables, lists, code) with minimal narration.
- An "argument" or "essay" angle wants prose. A 2000 word piece with a single supporting diagram can be the right answer.

DO NOT pick named components first and reverse-engineer a page around them. Pick the shape, then pick the components that express the shape. Two pages built from the same shape on the same site should still look different because the angle pulls them in different directions.

{quotas_block}
{anti_quota_block}
### Lottie

If you reference a Lottie animation via `LottiePlayer`, you MUST also create the JSON file at a real path in `public/` (e.g. `public/lottie/<slug>-hero.json`). Do not reference a Lottie path that does not exist. If you cannot produce a real Lottie JSON, skip it.

### Page chrome: do NOT render a navbar or footer

The site's intermediate layout (`src/app/t/layout.tsx` or `src/app/(main)/layout.tsx`) renders the shared `<SiteNavbar>` and `<SiteFooter>` once for every SEO page. Your page.tsx MUST start directly with the article body (`<article>`, breadcrumbs, hero, etc.) and MUST NOT import or render any of: `SiteNavbar`, `SiteFooter`, `GuideNavbar`, `GuideFooter`, `Navbar`, `Header`, `Footer`. No top-level `<nav>`, `<header>`, or `<footer>` elements either. Rendering one yourself causes a double-navbar / double-footer. If you read an existing page for syntax reference and it renders one of these, IGNORE that part and omit it from your output.

### Color palette (mandatory)

CONSUMER THEME DETECTED: {consumer_theme}. The consumer site's root layout uses a {consumer_theme}-mode global, so your page MUST match. Using the wrong theme renders the article body as a contrasting slab between the navbar and footer.

{f'''DARK theme palette (use these exact classes, do NOT use the light palette below). BRAND ACCENT FAMILY: `{accent_name}` (do NOT substitute teal or any other color — this is the consumer's brand color). NEUTRAL FAMILY for THIS page: `{neutral}` (chosen deterministically from the slug so this page does not look identical to the previous five pages on this site; do NOT switch to a different neutral mid-page):

- Article wrapper: `<article className="min-h-screen">` (no explicit bg; let the site-wide dark root show through). Never `bg-white`.
- Headings: `text-{neutral}-100` (NOT text-{neutral}-900). Vary heading weight and size by section: a hero h1 can be `text-5xl font-semibold tracking-tight` or `text-6xl font-bold` or `text-4xl font-medium`; do not default to the same weight for every page.
- Body text: `text-{neutral}-400`. Muted/lede: `text-{neutral}-500`.
- Primary pill: `bg-{accent_name}-900/30 text-{accent_name}-300`. Secondary pill: `bg-{neutral}-800/60 text-{neutral}-300`. Outline pill: `bg-transparent border border-{neutral}-800 text-{neutral}-300`.
- Section bands: `bg-{neutral}-950/40 border-y border-{neutral}-800/60`.
- Tinted boxes: `bg-{accent_name}-500/10 border border-{accent_name}-500/30`.
- Inline code: `bg-{neutral}-900 border border-{neutral}-800 text-{accent_name}-300 font-mono`.
- Dividers/borders: `border-{neutral}-800/60` (NOT border-{neutral}-200).
- Links: `text-{accent_name}-300` (NOT text-{accent_name}-600). Accent gradients for CTAs: `from-{accent_gradient_from}-500 to-{accent_name}-500` still works.
- CTA button: `bg-{accent_name}-500 text-{neutral}-950 hover:bg-{accent_name}-400 font-semibold`.

Stay inside the `{neutral}` family for all neutral classes on this page. Do NOT mix `text-zinc-400` with `border-slate-800` etc; pick one family and use it consistently. The accent classes (above) are NOT neutrals and stay as-is.

NEVER emit on a dark consumer: bg-white (solid), bg-{neutral}-50, bg-{neutral}-100, text-{neutral}-900, text-{neutral}-700, text-{neutral}-600, text-{accent_name}-700, text-{accent_name}-600, border-{neutral}-200, bg-{accent_name}-50. Translucent overlays like `bg-white/5` and `bg-black/30` are fine.

NEVER use violet, indigo, or purple anywhere. NEVER use teal unless the brand accent family IS teal.''' if consumer_theme == 'dark' else f'''LIGHT theme palette. BRAND ACCENT FAMILY: `{accent_name}` (do NOT substitute teal or any other color, this is the consumer's brand color):

- Article wrapper: `<article className="min-h-screen">` (no explicit bg; let the site-wide light root show through). NEVER `bg-white` on the `<article>` wrapper or top-level full-width section wrappers. Consumer body bg may be `#fff`, `#fafafa`, `#FAFAF7`, or any tinted off-white; hardcoding `bg-white` paints over it and produces a visible mismatch. Section bands that intentionally contrast with the body bg are fine (e.g. `bg-zinc-50` or `bg-{accent_name}-50` for an alternating band), and inline `bg-white` on small tiles/cards inside a tinted band is also fine. The outermost wrapper must stay transparent.
- Headings: `text-zinc-900`. Body text: `text-zinc-700` for primary copy, `text-zinc-500` or `text-gray-600` for secondary/lede.
- Accents: `from-{accent_gradient_from}-500 to-{accent_name}-500` gradient for CTAs, `text-{accent_name}-600` for links, `bg-{accent_name}-50 text-{accent_name}-700` for badges/pills, `bg-{accent_name}-50 border-{accent_name}-200` for tinted boxes.
- NEVER use violet, indigo, or purple anywhere. NEVER use teal unless the brand accent family IS teal.

NEVER emit on a light consumer: `bg-white` on the `<article>` wrapper or any top-level full-width section wrapper. Inline `bg-white` on small cards/tiles nested inside a tinted band is acceptable.

Do NOT use Tailwind semantic theme tokens like `text-foreground`, `text-muted`, `bg-card`, `bg-background`, `bg-surface-light`, `border-border`, `border-white/5`. Those tokens are not wired for light pages and will render invisibly. Use explicit classes like `text-zinc-900`, `text-zinc-500`, `bg-zinc-50`, `border-zinc-200`.'''}

{"" if not accent_hex else f'''### Product accent color override

This product's brand accent is `{accent_name}` ({accent_hex}), already baked into the Tailwind classes you emit above. For isolated-canvas components that render outside the Tailwind pipeline, pass the hex explicitly:

- `<RemotionClip accentHex="{accent_hex}" accentHexDark="{accent_hex_dark or accent_hex}" ... />`
- `<AnimatedBeam accentColor="{accent_hex}" ... />`
- `<Particles color="{accent_hex}" ... />`
- `<ShineBorder color="{accent_hex}" ... />`

Library components (GradientText, SitemapSidebar, GlowCard, BackgroundGrid, etc.) read `--seo-accent*` CSS variables, which the consumer's `globals.css` already sets to match `{accent_hex}`. You do not need to override those components' variants or props.
'''}### No decorative icons or emoji

Functional icons are allowed: expand/collapse chevrons, check/x indicators in comparison tables, arrow indicators on CTA buttons, terminal chrome, status badges. These exist to convey meaning the text cannot.

Decorative emoji and decorative icons are NOT allowed. Never pass emoji like 🎯 📐 🚫 🔢 🔗 📋 ✨ 🚀 💡 ⚡ as the `icon` prop of BentoGrid cards, FlowDiagram steps, StepTimeline steps, or anywhere else. Never add decorative emoji to section headers, feature lists, use case lists, how-it-works steps, or bullet points. If a component's props accept an `icon` field, leave it undefined unless the icon is genuinely functional (a check, an x, a chevron, an arrow, a lock, a warning triangle). When in doubt, omit it.

### Creative inspiration

You are not building a cookie-cutter SEO shell. Draw from the best modern product marketing and editorial sites. Specifically study these patterns when picking your layout and motion:

- **Linear** (linear.app) — mouse-tracking radial glow on cards, ultra-clean typography, narrow content column, heavy use of whitespace, subtle gradients in accent borders, micro-interactions on hover.
- **Vercel** (vercel.com) — geometric pattern backgrounds, grid lines, bold hero stats, inline small code chips, sequential reveal on scroll.
- **Stripe** (stripe.com) — animated diagrams that draw themselves on scroll, floating 3D-ish cards with layered shadows, gradient meshes in hero areas, side-by-side code + diagram layouts.
- **Apple product pages** — full-bleed sections with large-format photography (or here, large-format diagrams), sticky heading while content scrolls, dramatic scale shifts.
- **Framer** (framer.com) — physics-based spring animations, staggered reveals, hover-triggered card flips and morphs, bold accent gradients.
- **Remotion** (remotion.dev) — video-style sequenced animations: elements enter on a timeline, not just on scroll. Use the `MotionSequence` component (see below) for this.
- **Magic UI / Aceternity** (magicui.design, ui.aceternity.com) — bento grids with animated content, moving gradients, animated beam connectors between elements, glowing borders.

Motion principles: prefer spring physics (`type: "spring"`, `stiffness: 200-400`, `damping: 20-30`) over linear easing. Stagger reveals by 80-120ms. Use `whileInView` with `viewport={{ once: true, margin: "-40px" }}` for scroll entrances. For hover, use subtle scale (1.02) not aggressive (1.1). For emphasis, animate numbers counting up rather than just fading in.

### Tailwind setup (do this BEFORE writing the page)

Check `src/app/globals.css` for a `@source` line pointing at the `@seo/components` package. If it is missing, add it right after the `@import "tailwindcss"` line:

```
@source "../../node_modules/@seo/components/src";
```

Without this line, Tailwind will not generate utility classes used inside the components and SVGs/icons will render at wrong sizes. This only needs to be done once per repo.

Also check that the repo has `transpilePackages: ["@seo/components"]` in its `next.config.ts` (or `.mjs`/`.js`). If missing, add it. This tells Next.js to compile the raw TypeScript source from the package.

## Step 4 — Build the page

- Location: `{repo}/{primary_path}` (or match the convention you found in Step 3 if the repo uses a different path).
- **Structure is yours to invent.** Let the angle from Step 2 dictate everything: section count, section order, which visual components appear where, how the story unfolds. Do not follow a fixed outline. Do not replicate the skeleton of any existing page.
- Length: however long the angle deserves. Shorter and specific beats longer and generic. Do not pad.
- Style: no em dashes, no en dashes, anywhere. Plain direct prose. First person fine where natural.
- At least one section must surface the anchor_fact from your concept, with enough specificity that a reader could verify it (file name, command, number, behavior description). This is the uncopyable part of the page.
- Do not invent statistics. Do not fabricate quotes. If you use numbers, they come from something you read or ran.

### Direct-answer block (mandatory — do not be a dead end)

Before you start writing the page, take 60 seconds to articulate the user's literal question and the literal answer. Write a `DIRECT_ANSWER` section into your CONCEPT block from Step 2:

```
DIRECT_ANSWER
  shape: lookup | explanatory
  literal_question: <the one specific question this user typed into Google, in 8 words or fewer>
  literal_answer: <the one-sentence answer with the actual datum, OR "no public answer; verified via <authoritative_source> on <date> that no such datum exists">
  authoritative_source: <the URL or service that confirms the answer (e.g. https://www.tax.service.gov.uk/check-vat-number/enter-vat-details, https://www.sec.gov/edgar/searchedgar/companysearch, vendor pricing page, GitHub releases page)>
  verified_on: <YYYY-MM-DD you actually re-verified it>
```

Lookup-shaped keywords (the user wants ONE specific datum: a number, ID, code, exact name, address, price, date, version, count) MUST get the literal datum rendered prominently in the first 30% of the page, in a callout clearly labelled "Direct answer", "Verified <date>", or equivalent.

If the literal datum genuinely does not exist publicly (e.g. Anthropic does not publish a UK VAT number), the page MUST still render the same callout, but with the verified-not-available answer plus a link to the authoritative source you checked. Saying "ask them directly", "may or may not", "varies", "depends" is a dead end and FAILS. A real Fazm visitor commented "gbvat number?" on a page that did exactly this. Don't repeat that.

Explanatory-shaped keywords ("how", "what is", "why", "vs", "best") MUST get a 1-3 sentence direct answer in the first 30% of the page (hero subhead, TLDR block, lede) before any tangential build-up.

Use WebSearch + WebFetch + the relevant authoritative service (HMRC, SEC EDGAR, Companies House, vendor pricing page, GitHub releases, etc.) to actually verify the answer right now. Do not write the literal answer from memory. Re-verify it.

### Claim discipline (post-generation gates will check this)

After you finish, four automated gates run before this page can ship:
1. Every external URL on the page is DNS-resolved and HEAD-fetched. If any URL returns ENOTFOUND or 404, the page is removed and the row goes back to pending. Treat any concrete URL you write as a load-bearing fact, not a placeholder. If you do not have a real URL, write `<placeholder>` or omit the link.

   **NEVER link to a bare `/t` path.** The article-index page (e.g. `https://<site>/t`, `https://<site>/t/`, or any `<Link href="/t">`) does not exist on most consumer sites in this repo, so any link to it 404s and fails the dead-URL gate. If you want to point at "more guides", link to ONE specific `/t/<existing-slug>` page that is verifiably live (check the inventory block above), or skip the link entirely. Same rule applies to `/blog`, `/use-case`, `/best`, `/alternative` index roots: link to a specific child slug or omit. This single mistake killed five pages in the 2026-04-28 top_pages run.
2. Every sentence matching shapes like "<Vendor> shipped X in <Month> <Year>", "<Vendor> raised $X", or "<Person> named CEO of <Vendor>" is re-verified via WebSearch. Wrong dates and amounts fail the page. Use WebSearch yourself before writing any time-stamped vendor event; do not write from recall.
3. A claims-extractor pass reads the rendered page and re-checks every name, date, dollar amount, customer logo, license name, and product feature against a fresh search. Mis-named products ("X with Y AI" when the real name is just "X Y"), invented metric precision ("172 customers, one third Fortune 500"), and wrong open-source licenses all fail.
4. A keyword-answer pass classifies the keyword as lookup or explanatory and verifies the page contains the literal answer (or a verified-not-available callout) in a prominent position. Pages that punt on the user's actual question fail this gate. The remediation is a `Direct answer (verified <date>)` section near the top, naming the authoritative source.

A useful working pattern: as you draft, keep a private list of `(claim, source_url, evidence_quote)` tuples. Every concrete number, date, customer name, integration, and feature you write about a real third-party entity should trace to one of those tuples. If you cannot fill the source_url, replace the assertion with a more general phrasing or drop it.

### Pick a page skeleton (or invent your own)

The default "Hero, ProofBand, six alternating prose-and-visual sections, FAQ at the bottom" template is what every other SEO page on the internet looks like. Do not default to it. Pick one of the skeletons below that matches your angle, OR invent a different shape entirely. Whichever you pick, do not reuse the same skeleton as the most recent few pages on this site.

- **Walkthrough skeleton.** Numbered or named steps as the spine. Each step is a short prose lede plus one piece of evidence (a code block, a terminal output, a small diagram). No giant hero. No bento. The page reads like documentation a peer would write.
- **Q-and-A skeleton.** The whole page is 8 to 12 sub-questions, each answered in 2-4 paragraphs. The FAQ trust signal is structural, not a footer. No bento, no metrics row.
- **Argument skeleton.** Thesis up top, three or four supporting prose sections, one counterargument section, one resolution. Visual breaks are optional and rare. A 1800-2500 word essay shape.
- **Comparison skeleton.** A clear side-by-side. Comparison table or before/after early, then 3-4 prose sections digging into the dimensions where the comparison actually matters, then a recommendation that admits the cases where the other option wins.
- **Reference skeleton.** Dense lookup material. Tables, lists, short definitions. Minimal narration. The reader scans, does not read. Almost no Magic UI, no decorative motion.
- **Story skeleton.** One specific narrative (a debugging session, a customer scenario, a measurement we ran), told in chronological order with concrete details. One supporting visual at most.
- **Invent your own.** If none of the above fit, design a shape and name it in your CONCEPT block. Anything goes as long as it serves the angle.

State the chosen skeleton (or your invented name) explicitly at the top of your CONCEPT block from Step 2. Add a `skeleton:` line.

- **Visual rhythm:** Match the rhythm to the skeleton. Walkthrough, Comparison, and Story skeletons want a visual every 1-2 sections. Argument, Q-and-A, and Reference skeletons can run 4-5 prose sections in a row with no visual at all. Do NOT force a visual in just to break up prose if the skeleton does not want one. Content type for this page is `{content_type}`; let that guide whether visual breaks are doing real work or are filler.

### Trust signals (required core, optional extras)

Required on every page, placement entirely up to you (not locked to top, bottom, or any particular section):

1. **`Breadcrumbs`** somewhere in the page chrome (top, side, or under the hero, your call).
2. **`FaqSection`** somewhere in the page (does not have to be near the bottom; an inline FAQ between body sections is fine, a Q-and-A skeleton can be all FAQ). At least 5 concrete, specific FAQs drawn from your research. Generic FAQs are worse than no FAQs.
3. **JSON-LD structured data** as a `<script type="application/ld+json">` tag. Import `articleSchema`, `breadcrumbListSchema`, and `faqPageSchema` from `@seo/components`.

Required on cross_roundup pages, optional elsewhere:

4. **`ArticleMeta` (REQUIRED on cross_roundup, optional otherwise).** On cross_roundup pages the byline MUST render directly under the H1 as the first element of the article body, before any prose, hero card, or proof band. This is locked: do not move it to the footer, into a sidebar, or below the intro. On guide and use_case pages it is appropriate but placement is your call. Skip it on alternative/comparison pages and on reference-style pages where a byline reads as filler. If you skip it, still emit the author info inside `articleSchema` (the JSON-LD must always carry the byline). The current content type is `{content_type}`; let it inform the choice.
5. **`ProofBand`** is appropriate when the page sits near a buying decision (alternative, use_case) and you have honest social proof or numbers to put there. Skip it on guide/explainer pages where a proof strip would feel grafted on. If you do include it, put it wherever it lands naturally in the flow (top, mid, footer), not in a fixed slot.

Vary placement of optional chrome (Breadcrumbs, ProofBand) across pages on the same site. ArticleMeta on cross_roundup is the one fixed slot: directly under the H1.

### AUTHOR ATTRIBUTION (mandatory, non-negotiable, do not invent)

This page's author is set by config, not by you. Use these exact values verbatim, in BOTH the `<ArticleMeta>` byline and the JSON-LD `articleSchema` block. Do not substitute the brand name, do not invent a persona, do not copy whatever you saw in a seed page.

- author name: `{author_name}`
- author role/subtitle: `{author_role}`
- author url: `{author_url}`

Required wiring:

```tsx
<ArticleMeta
  author="{author_name}"
  authorRole="{author_role}"
  datePublished={{DATE_PUBLISHED}}
  readingTime="..."
/>
```

```ts
articleSchema({{
  ...,
  author: "{author_name}",
  authorUrl: "{author_url}",
  ...
}})
```

If the role line `{author_role}` reads as an AI disclosure (e.g. "Written with AI"), keep it visible: it is the public AI-content disclosure for this page and must render in the byline. Do not move it into a hidden comment, an alt attribute, a tooltip, or a footer note.

{book_call_block}
{deploy_block}

## Step 6 — Report back

Output your CONCEPT block from Step 2 in the conversation so it is captured in the log.

Then, as your FINAL message (nothing after it), output exactly one line of JSON. There are three valid shapes; pick the one that matches what you did.

If you BUILT a new page (Step 0 option A):

{{"success": true, "page_url": "{page_url}", "slug": "{slug}", "commit_sha": "<7-char sha>", "concept_angle": "<one-line angle>"}}

If you CONSOLIDATED into an existing page (Step 0 option B):

{{"success": true, "action": "consolidate", "slug": "{slug}", "target_slug": "<existing-canonical-slug>", "target_url": "<absolute URL of the canonical page>", "rationale": "<one sentence: why these are the same intent>", "target_page_edited": true}}

If anything went wrong:

{{"success": false, "error": "<specific reason>", "slug": "{slug}"}}

Do not output any text after the final JSON line.
"""


def _safe_slug_component(slug: str) -> str:
    """Sanitize a slug so it's safe to embed in a filename. Replaces path
    separators and other filesystem-unsafe characters with underscore. Without
    this, slugs like 't/ai-agent-monitor' would create implicit subdirectories
    when used as part of a filename and the open() would crash with
    FileNotFoundError."""
    if not slug:
        return "page"
    return re.sub(r"[^A-Za-z0-9._-]+", "_", slug).strip("_") or "page"


def run_claude_stream(prompt: str, cwd: str, log_dir: Path, slug: str) -> dict:
    """
    Invoke claude -p with stream-json output. Capture every tool call to a jsonl file.
    Returns a dict: {exit_code, final_result_text, tool_summary, stream_log_path}.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    safe_slug = _safe_slug_component(slug)
    stream_log = log_dir / f"{ts}_{safe_slug}_stream.jsonl"

    session_id = str(uuid.uuid4())
    cmd = [
        "claude", "-p", prompt,
        "--session-id", session_id,
        "--output-format", "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
    ]

    tool_calls: list[dict] = []
    final_text = ""
    orch_cost = None
    start = time.time()

    # Bridge the Claude Code auto-update unlink window before spawning.
    if not wait_for_claude():
        return {"exit_code": 127, "final_result_text": "",
                "tool_summary": {}, "stream_log_path": str(stream_log),
                "session_id": session_id,
                "error": "claude CLI not on PATH after wait_for_claude timeout"}

    with open(stream_log, "w") as log_f:
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            return {"exit_code": 127, "final_result_text": "",
                    "tool_summary": {}, "stream_log_path": str(stream_log),
                    "session_id": session_id,
                    "error": "claude CLI not found on PATH"}

        assert proc.stdout is not None
        while True:
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                continue
            log_f.write(line)
            log_f.flush()

            if time.time() - start > CLAUDE_TIMEOUT_SECONDS:
                proc.kill()
                _log_claude_session(session_id, "seo_generate_page", orch_cost)
                return {"exit_code": 124, "final_result_text": final_text,
                        "tool_summary": _summarize_tools(tool_calls),
                        "stream_log_path": str(stream_log),
                        "session_id": session_id,
                        "error": f"timeout after {CLAUDE_TIMEOUT_SECONDS}s"}

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            if event.get("type") == "assistant":
                msg = event.get("message", {})
                for block in msg.get("content", []):
                    if block.get("type") == "tool_use":
                        tool_calls.append({
                            "name": block.get("name"),
                            "input": block.get("input", {}),
                        })
            elif event.get("type") == "result":
                final_text = event.get("result", "") or ""
                c = event.get("total_cost_usd")
                if isinstance(c, (int, float)) and c > 0:
                    orch_cost = float(c)

        proc.wait()

    _log_claude_session(session_id, "seo_generate_page", orch_cost)
    return {
        "exit_code": proc.returncode,
        "final_result_text": final_text,
        "tool_summary": _summarize_tools(tool_calls),
        "stream_log_path": str(stream_log),
        "session_id": session_id,
    }


def run_claude_stream_resume(session_id: str, prompt: str, cwd: str,
                             log_dir: Path, slug: str,
                             timeout: int = 600) -> dict:
    """Resume a prior Claude session with a follow-up prompt and stream its
    output. Used by the typecheck-fix retry path where we want Claude to
    keep full context of the work it just did. Shorter timeout than a fresh
    session (default 10 min) because we expect a targeted patch, not a full
    rewrite. Same return shape as run_claude_stream."""
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    safe_slug = _safe_slug_component(slug)
    stream_log = log_dir / f"{ts}_{safe_slug}_retry_stream.jsonl"

    cmd = [
        "claude", "-p", prompt,
        "--resume", session_id,
        "--output-format", "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
    ]

    tool_calls: list[dict] = []
    final_text = ""
    orch_cost = None
    start = time.time()

    if not wait_for_claude():
        return {"exit_code": 127, "final_result_text": "",
                "tool_summary": {}, "stream_log_path": str(stream_log),
                "session_id": session_id,
                "error": "claude CLI not on PATH after wait_for_claude timeout"}

    with open(stream_log, "w") as log_f:
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            return {"exit_code": 127, "final_result_text": "",
                    "tool_summary": {}, "stream_log_path": str(stream_log),
                    "session_id": session_id,
                    "error": "claude CLI not found on PATH"}

        assert proc.stdout is not None
        while True:
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                continue
            log_f.write(line)
            log_f.flush()

            if time.time() - start > timeout:
                proc.kill()
                _log_claude_session(session_id, "seo_generate_page_retry", orch_cost)
                return {"exit_code": 124, "final_result_text": final_text,
                        "tool_summary": _summarize_tools(tool_calls),
                        "stream_log_path": str(stream_log),
                        "session_id": session_id,
                        "error": f"retry timeout after {timeout}s"}

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            if event.get("type") == "assistant":
                msg = event.get("message", {})
                for block in msg.get("content", []):
                    if block.get("type") == "tool_use":
                        tool_calls.append({
                            "name": block.get("name"),
                            "input": block.get("input", {}),
                        })
            elif event.get("type") == "result":
                final_text = event.get("result", "") or ""
                c = event.get("total_cost_usd")
                if isinstance(c, (int, float)) and c > 0:
                    orch_cost = float(c)

        proc.wait()

    _log_claude_session(session_id, "seo_generate_page_retry", orch_cost)
    return {
        "exit_code": proc.returncode,
        "final_result_text": final_text,
        "tool_summary": _summarize_tools(tool_calls),
        "stream_log_path": str(stream_log),
        "session_id": session_id,
    }


def _log_claude_session(session_id: str, script_tag: str, orch_cost: float | None = None) -> None:
    """Best-effort: invoke log_claude_session.py to record cost into claude_sessions.
    When orch_cost is provided (captured from the stream-json result event's
    total_cost_usd field), it's forwarded as --orchestrator-cost-usd so the
    SDK lane gets populated. Sessions without orch_cost fall back to the
    transcript-walk estimate the logger computes itself."""
    logger = ROOT_DIR / "scripts" / "log_claude_session.py"
    if not logger.exists():
        return
    args = ["python3", str(logger), "--session-id", session_id, "--script", script_tag]
    if orch_cost is not None and orch_cost > 0:
        args.extend(["--orchestrator-cost-usd", str(orch_cost)])
    try:
        subprocess.run(args, capture_output=True, text=True, timeout=30)
    except Exception:
        pass


def _summarize_tools(calls: list[dict]) -> dict:
    """Count tool calls and flag whether product source was touched."""
    summary: dict = {"total": len(calls), "by_name": {}, "reads": [], "bash": []}
    for c in calls:
        name = c.get("name", "unknown")
        summary["by_name"][name] = summary["by_name"].get(name, 0) + 1
        inp = c.get("input", {}) or {}
        if name == "Read":
            summary["reads"].append(inp.get("file_path", ""))
        elif name == "Bash":
            summary["bash"].append(inp.get("command", "")[:200])
    return summary


def count_source_touches(tool_summary: dict, source_paths: list[str]) -> dict:
    """How many Read/Bash calls touched the product source paths."""
    touches = {p: {"reads": 0, "bash": 0} for p in source_paths}
    for read_path in tool_summary.get("reads", []):
        for sp in source_paths:
            if read_path.startswith(sp):
                touches[sp]["reads"] += 1
    for cmd in tool_summary.get("bash", []):
        for sp in source_paths:
            if sp in cmd:
                touches[sp]["bash"] += 1
    return touches


_FINAL_JSON_RE = re.compile(r"\{[^{}]*\"success\"[^{}]*\}")


def parse_final_json(text: str) -> dict | None:
    """Extract the final JSON status line from Claude's result text."""
    if not text:
        return None
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if not line:
            continue
        m = _FINAL_JSON_RE.search(line)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    m = _FINAL_JSON_RE.search(text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


def parse_concept(text: str) -> dict:
    """Extract the CONCEPT block from Claude's output, best-effort."""
    if not text:
        return {}
    out = {}
    lines = text.splitlines()
    in_block = False
    for line in lines:
        if line.strip().startswith("CONCEPT"):
            in_block = True
            continue
        if in_block:
            stripped = line.strip()
            if not stripped:
                if any(out.values()):
                    break
                continue
            m = re.match(r"^([a-z_]+):\s*(.+)$", stripped)
            if m:
                out[m.group(1)] = m.group(2).strip()
            else:
                break
    return out


def verify_commit_landed(repo_path: str, expected_file: str,
                         max_wait: float = 180.0,
                         poll_interval: float = 20.0) -> dict:
    """Check origin/main (or local HEAD if no remote) for the expected file.
    When the repo has an 'origin' remote, polls with git fetch for up to
    max_wait seconds so the background auto-commit agent (which pushes on a
    ~60s cadence) has time to land the commit before we call it a failure.
    Returns {ok, commit_sha, error}."""
    try:
        r = subprocess.run(["git", "remote"], cwd=repo_path,
                           capture_output=True, text=True, check=True, timeout=10)
        remotes = set(r.stdout.split())
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "error": f"git remote failed: {e}"}

    has_origin = "origin" in remotes
    ref = "origin/main" if has_origin else "HEAD"
    deadline = time.time() + max_wait
    last_err = f"no commit on {ref} touching {expected_file}"
    while True:
        if has_origin:
            try:
                subprocess.run(["git", "fetch", "origin"], cwd=repo_path,
                               check=True, capture_output=True, timeout=60)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                last_err = f"git fetch failed: {e}"
        try:
            r = subprocess.run(
                ["git", "log", ref, "-1", "--format=%h", "--", expected_file],
                cwd=repo_path, capture_output=True, text=True, check=True,
            )
            sha = r.stdout.strip()
        except subprocess.CalledProcessError as e:
            last_err = f"git log {ref} failed: {e.stderr}"
            sha = ""

        if sha:
            return {"ok": True, "commit_sha": sha, "ref": ref}
        if not has_origin or time.time() >= deadline:
            return {"ok": False, "error": last_err}
        time.sleep(poll_interval)


RAW_BOOKING_HREF_RE = re.compile(
    # Match `href=` at a word boundary so `bookingHref=` on a BookCallCTA
    # prop doesn't false-positive. Case-insensitive so `Href` on a native
    # attribute still matches. Catches `<a href="cal.com/...">` — that
    # bypasses withBookingAttribution. Skips `<BookCallCTA bookingHref="...">`
    # — that's the sanctioned wrapper which owns attribution internally.
    r"""\bhref\s*=\s*\{?\s*["'`][^"'`]*?(cal\.com|calendly\.com)""",
    re.IGNORECASE,
)


# Tailwind classes that produce light-only surfaces/text. On a dark consumer
# (fazm, mediar, assrt, cyrano), every occurrence of a bare class listed here
# must be paired with a `dark:*` variant on the SAME className attribute so
# the element reads correctly in dark mode. A `bg-white` without `dark:bg-*`
# prints a bright component block on a dark page; `text-gray-900` prints
# dark-on-dark; `text-blue-600` links are illegible; `bg-{accent}-50` pastel
# bands disappear. Fazm guide regression, 2026-04-22.
#
# Each entry maps a bare-class regex to the `dark:` prefix that must appear
# somewhere in the same className to pass. Pattern uses a `(?<![:\w])`
# lookbehind so `dark:bg-white/[0.05]` and `hover:bg-gray-50` don't count as
# bare matches — only truly unprefixed occurrences are flagged.
_THEME_LINT_RULES: list[tuple[str, str]] = [
    (r"(?<![:\w])bg-white\b", "dark:bg-"),
    (r"(?<![:\w])bg-gray-50\b", "dark:bg-"),
    (r"(?<![:\w])bg-gray-100\b", "dark:bg-"),
    (r"(?<![:\w])bg-slate-50\b", "dark:bg-"),
    (r"(?<![:\w])bg-zinc-50\b", "dark:bg-"),
    (r"(?<![:\w])bg-neutral-50\b", "dark:bg-"),
    # pastel accent bands (bg-<accent>-50 reads as a washed-out block on dark)
    (r"(?<![:\w])bg-(?:blue|purple|pink|red|orange|amber|yellow|green|"
     r"emerald|teal|cyan|sky|indigo|violet|fuchsia|rose)-50\b", "dark:bg-"),
    # body/heading text
    (r"(?<![:\w])text-gray-900\b", "dark:text-"),
    (r"(?<![:\w])text-gray-800\b", "dark:text-"),
    (r"(?<![:\w])text-gray-700\b", "dark:text-"),
    (r"(?<![:\w])text-gray-600\b", "dark:text-"),
    (r"(?<![:\w])text-slate-900\b", "dark:text-"),
    (r"(?<![:\w])text-slate-800\b", "dark:text-"),
    (r"(?<![:\w])text-slate-700\b", "dark:text-"),
    (r"(?<![:\w])text-slate-600\b", "dark:text-"),
    (r"(?<![:\w])text-zinc-900\b", "dark:text-"),
    (r"(?<![:\w])text-zinc-800\b", "dark:text-"),
    (r"(?<![:\w])text-zinc-700\b", "dark:text-"),
    (r"(?<![:\w])text-zinc-600\b", "dark:text-"),
    # link color
    (r"(?<![:\w])text-blue-600\b", "dark:text-"),
    # borders
    (r"(?<![:\w])border-gray-200\b", "dark:border-"),
    (r"(?<![:\w])border-gray-300\b", "dark:border-"),
    (r"(?<![:\w])border-slate-200\b", "dark:border-"),
    (r"(?<![:\w])border-zinc-200\b", "dark:border-"),
]

_THEME_LINT_COMPILED: list[tuple[re.Pattern, str]] = [
    (re.compile(pat), dark_prefix) for pat, dark_prefix in _THEME_LINT_RULES
]

# Matches className="...", className={`...`}, and className={"..."}.
# Captures the inner classes string in one of the three alternation groups.
_CLASSNAME_ATTR_RE = re.compile(
    r'className\s*=\s*(?:"([^"]*)"|\{`([^`]*)`\}|\{"([^"]*)"\})'
)


def validate_booking_attribution(repo_path: str,
                                 expected_file_candidates: list[str]) -> dict:
    """Fail the run if any generated page file contains a raw Cal.com or
    Calendly href. Booking CTAs must go through `BookCallCTA`, which rewrites
    the URL at click time via `withBookingAttribution` (utm_* + metadata[utm_*]).
    A raw `<a href="https://cal.com/...">` bypasses that rewrite, so the
    resulting booking lands in `cal_bookings` with empty utm columns and
    never attributes back to its source page (PieLine/Clone/mk0r 84-page
    incident, 2026-04-22).

    On failure, restores tracked files and removes untracked ones so the
    background auto-commit daemon has nothing to push, mirroring
    `typecheck_and_cleanup`'s cleanup path.
    """
    root = Path(repo_path)
    findings: list[str] = []
    for rel in expected_file_candidates:
        abs_path = root / rel
        if not abs_path.exists():
            continue
        try:
            text = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            findings.append(f"{rel}: read failed: {e}")
            continue
        for m in RAW_BOOKING_HREF_RE.finditer(text):
            line_no = text.count("\n", 0, m.start()) + 1
            snippet = text[max(0, m.start() - 10):m.end() + 40].replace("\n", " ")
            findings.append(f"{rel}:L{line_no} {snippet!r}")

    if not findings:
        return {"ok": True}

    cleaned: list[str] = []
    for rel in expected_file_candidates:
        abs_path = root / rel
        if not abs_path.exists():
            continue
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", rel],
            cwd=repo_path, capture_output=True, text=True,
        )
        if tracked.returncode == 0:
            subprocess.run(["git", "restore", "--", rel],
                           cwd=repo_path, capture_output=True, text=True)
            cleaned.append(f"{rel} (restored)")
        else:
            try:
                abs_path.unlink()
                parent = abs_path.parent
                if parent.is_dir() and parent != root and not any(parent.iterdir()):
                    parent.rmdir()
                cleaned.append(f"{rel} (removed)")
            except OSError:
                pass

    return {
        "ok": False,
        "error": "raw booking href (must use BookCallCTA): "
                 + " | ".join(findings[:3]),
        "cleaned": cleaned,
    }


def validate_theme_classes(repo_path: str,
                           expected_file_candidates: list[str]) -> dict:
    """Fail the run if a generated page ships light-only Tailwind on a dark
    consumer. Catches the failure mode where the inner Claude session writes
    `bg-gray-50`, `text-gray-900`, `text-blue-600`, or a `bg-{accent}-50`
    band without a paired `dark:*` variant, producing a bright component
    block on fazm/mediar/assrt/cyrano's dark theme (Fazm guide pages
    regression, 2026-04-22).

    Heuristic: for each forbidden bare class found inside a `className=...`
    attribute, require that the matching `dark:` prefix (e.g. `dark:bg-`,
    `dark:text-`, `dark:border-`) appear somewhere in the SAME className
    string. This catches the overwhelmingly common failure mode (the LLM
    wrote zero dark: variants on an element); it intentionally does not
    count every pair individually, because that's enough to retry the
    generation with actionable feedback.

    Skipped on light consumers (bg-white there is correct). Cleanup path
    mirrors validate_booking_attribution / typecheck_and_cleanup: restore
    tracked files, remove untracked ones, so the ~/git-dashboard auto-commit
    cron cannot push the bad page while we retry.
    """
    if detect_consumer_theme(repo_path) != "dark":
        return {"ok": True, "skipped": "consumer theme is light"}

    root = Path(repo_path)
    findings: list[str] = []

    for rel in expected_file_candidates:
        abs_path = root / rel
        if not abs_path.exists():
            continue
        try:
            text = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            findings.append(f"{rel}: read failed: {e}")
            continue

        for m in _CLASSNAME_ATTR_RE.finditer(text):
            classes = m.group(1) or m.group(2) or m.group(3) or ""
            if not classes.strip():
                continue
            for rx, dark_prefix in _THEME_LINT_COMPILED:
                bare = rx.search(classes)
                if not bare:
                    continue
                if dark_prefix in classes:
                    continue
                line_no = text.count("\n", 0, m.start()) + 1
                findings.append(
                    f"{rel}:L{line_no} '{bare.group(0)}' needs paired "
                    f"{dark_prefix}* on dark consumer"
                )

    if not findings:
        return {"ok": True}

    cleaned: list[str] = []
    for rel in expected_file_candidates:
        abs_path = root / rel
        if not abs_path.exists():
            continue
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", rel],
            cwd=repo_path, capture_output=True, text=True,
        )
        if tracked.returncode == 0:
            subprocess.run(["git", "restore", "--", rel],
                           cwd=repo_path, capture_output=True, text=True)
            cleaned.append(f"{rel} (restored)")
        else:
            try:
                abs_path.unlink()
                parent = abs_path.parent
                if parent.is_dir() and parent != root and not any(parent.iterdir()):
                    parent.rmdir()
                cleaned.append(f"{rel} (removed)")
            except OSError:
                pass

    return {
        "ok": False,
        "error": "theme lint (light-only Tailwind on dark consumer; pair "
                 "with dark:* or drop the class): "
                 + " | ".join(findings[:5]),
        "cleaned": cleaned,
    }


def typecheck_and_cleanup(repo_path: str, expected_file_candidates: list[str],
                          restore_on_fail: bool = True) -> dict:
    """Run `npx tsc --noEmit` in the repo to catch type errors the inner Claude
    session may have left behind. If typecheck fails and restore_on_fail is
    True, restore/remove any uncommitted page files under
    expected_file_candidates so the background auto-commit daemon cannot push
    broken work to main.

    restore_on_fail=False is used by the typecheck-retry path in the caller:
    we need Claude to still see the broken file during the resume session so
    it can diff/read/fix it. The caller re-invokes with the default True if
    the retry also fails.

    Returns {ok: bool, error?: str, cleaned?: list[str], skipped?: str}.
    Skipped if the repo is not a TypeScript project.
    """
    root = Path(repo_path)
    if not (root / "package.json").exists() or not (root / "tsconfig.json").exists():
        return {"ok": True, "skipped": "not a TypeScript project"}

    try:
        r = subprocess.run(
            ["npx", "--no-install", "tsc", "--noEmit"],
            cwd=repo_path, capture_output=True, text=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "tsc --noEmit timed out after 300s"}
    except FileNotFoundError:
        return {"ok": True, "skipped": "npx not on PATH"}

    if r.returncode == 0:
        return {"ok": True}

    tsc_output = (r.stdout + r.stderr).strip()
    if not restore_on_fail:
        return {"ok": False, "error": tsc_output}

    cleaned: list[str] = []
    for rel in expected_file_candidates:
        abs_path = root / rel
        if not abs_path.exists():
            continue
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", rel],
            cwd=repo_path, capture_output=True, text=True,
        )
        if tracked.returncode == 0:
            subprocess.run(["git", "restore", "--", rel],
                           cwd=repo_path, capture_output=True, text=True)
            cleaned.append(f"{rel} (restored)")
        else:
            try:
                abs_path.unlink()
                parent = abs_path.parent
                if parent.is_dir() and parent != root and not any(parent.iterdir()):
                    parent.rmdir()
                cleaned.append(f"{rel} (removed)")
            except OSError:
                pass

    return {"ok": False, "error": tsc_output, "cleaned": cleaned}


def probe_url_live(url: str, timeout: int = 15, retries: int = 30,
                   interval: int = 10) -> dict:
    """HEAD-then-GET probe with fixed-interval retries. Returns {ok, status, error}.
    Default budget: 30 retries x 10s = ~5 min, enough for a typical Vercel/Netlify
    deploy to finish after git push. 2xx counts as live.
    """
    import urllib.request
    import urllib.error
    last_err = ""
    for attempt in range(retries):
        for method in ("HEAD", "GET"):
            try:
                req = urllib.request.Request(url, method=method,
                                             headers={"User-Agent": "social-autoposter/seo-verify"})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    status = resp.getcode()
                    if 200 <= status < 300:
                        return {"ok": True, "status": status}
                    last_err = f"{method} {url} -> {status}"
            except urllib.error.HTTPError as e:
                if 200 <= e.code < 300:
                    return {"ok": True, "status": e.code}
                last_err = f"{method} {url} -> {e.code}"
            except Exception as e:
                last_err = f"{method} {url} failed: {e}"
        if attempt < retries - 1:
            time.sleep(interval)
    return {"ok": False, "error": last_err}


def save_concept_file(concepts_dir: Path, slug: str, product: str, keyword: str,
                      concept: dict, final_json: dict | None,
                      tool_summary: dict, touches: dict) -> Path:
    concepts_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = concepts_dir / f"{ts}_{slug}.md"
    body = [
        f"# {keyword}",
        "",
        f"- product: {product}",
        f"- slug: {slug}",
        f"- generated: {ts}Z",
        "",
        "## Concept",
    ]
    if concept:
        for k, v in concept.items():
            body.append(f"- **{k}**: {v}")
    else:
        body.append("(no concept parsed)")
    body += ["", "## Final JSON", "```json",
             json.dumps(final_json or {}, indent=2), "```", "",
             "## Tool summary", "```json",
             json.dumps({"total": tool_summary.get("total", 0),
                         "by_name": tool_summary.get("by_name", {}),
                         "source_touches": touches}, indent=2),
             "```"]
    out.write_text("\n".join(body))
    return out


def update_state(trigger: str, product: str, keyword: str, status: str,
                 page_url: str | None = None, notes: str | None = None,
                 slug: str | None = None,
                 content_type: str | None = None,
                 claude_session_id: str | None = None) -> None:
    """Dispatch state updates to the right table based on trigger."""
    if trigger == "serp":
        kwargs = {}
        if page_url is not None:
            kwargs["page_url"] = page_url
        if notes is not None:
            kwargs["notes"] = notes
        if content_type is not None:
            kwargs["content_type"] = content_type
        if claude_session_id is not None:
            kwargs["claude_session_id"] = claude_session_id
        db_helpers.update_status(product, keyword, status, **kwargs)
    elif trigger == "gsc":
        body = {"product": product, "query": keyword, "status": status}
        if page_url is not None:
            body["page_url"] = page_url
        if slug is not None:
            body["page_slug"] = slug
        if notes is not None:
            body["notes"] = notes
        if content_type is not None:
            body["content_type"] = content_type
        if claude_session_id is not None:
            body["claude_session_id"] = claude_session_id
        api_patch("/api/v1/seo/gsc-queries", body)
    elif trigger in ("reddit", "twitter", "top_page", "roundup"):
        # Reply/topic-driven landing pages. source tags the keyword row by its
        # origin rail; upsert ensures the row exists (ON CONFLICT DO NOTHING)
        # before the status/metadata UPDATE. Mirrors db_helpers.update_status.
        body = {
            "product": product, "keyword": keyword, "status": status,
            "upsert": True, "source": trigger,
        }
        if slug is not None:
            body["slug"] = slug
        if page_url is not None:
            body["page_url"] = page_url
        if notes is not None:
            body["notes"] = notes
        if content_type is not None:
            body["content_type"] = content_type
        if claude_session_id is not None:
            body["claude_session_id"] = claude_session_id
        api_patch("/api/v1/seo/keywords", body)
    elif trigger == "manual":
        pass  # caller manages state


def update_blog_manifest(repo_path: str, content_module_path: str) -> dict:
    """Verify the model added a manifest entry for the new blog post.

    The model writes both `src/app/blog/_content/<slug>.ts` (the content
    module) and an entry in `src/app/blog/_manifest.ts` in the same commit.
    This helper double-checks: it reads the manifest after the commit lands
    and confirms an entry exists for the slug. If not, the row goes back to
    pending so the next cron tick retries.

    Returns {"ok": bool, "error"?: str, "skipped"?: bool, "slug"?: str}.

    Skipped silently on consumers that don't have a manifest yet (i.e. they
    haven't adopted the BlogPostLayout pattern).
    """
    manifest_path = os.path.join(repo_path, "src", "app", "blog", "_manifest.ts")
    if not os.path.exists(manifest_path):
        return {"ok": True, "skipped": True,
                "reason": "no _manifest.ts on this consumer"}

    # Derive slug from the content module path: src/app/blog/_content/<slug>.ts
    rel = content_module_path
    slug_match = re.match(
        r"^(?:src/)?app/blog/_content/([^/]+)\.ts$", rel)
    if not slug_match:
        return {"ok": False,
                "error": f"unexpected content module path: {rel}"}
    slug = slug_match.group(1)

    if not os.path.exists(os.path.join(repo_path, rel)):
        return {"ok": False,
                "error": f"content module missing at {rel}"}

    try:
        manifest_text = Path(manifest_path).read_text(encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": f"read manifest: {e}"}

    # Look for a slug: "<slug>" entry on a single manifest line.
    if not re.search(
        rf'\{{\s*slug:\s*"{re.escape(slug)}"\s*,',
        manifest_text):
        return {"ok": False,
                "error": (f"manifest missing entry for slug={slug}; "
                          "model wrote the content module but skipped "
                          "_manifest.ts. Re-run will retry.")}

    return {"ok": True, "slug": slug}


def find_existing_target_path(repo_path: str, content_type: str,
                              slug: str) -> str | None:
    """Return first existing candidate page path for this slug, or None.

    Protects hand-edited or previously-generated pages from silent overwrite.
    The generator invokes Claude with --dangerously-skip-permissions, so there
    is no interactive guard inside the Claude session; we must gate at the
    Python layer before calling out. Any match across the candidate list means
    'a page already lives here' — abort unless the caller passes force=True.
    """
    ct = CONTENT_TYPES.get(content_type, CONTENT_TYPES["guide"])
    for tmpl in ct["path_candidates"]:
        p = os.path.join(repo_path, tmpl.format(slug=slug))
        if os.path.exists(p):
            return p
    return None


def generate(product: str, keyword: str, slug: str, trigger: str = "manual",
             content_type: str | None = None, force: bool = False,
             escalation_id: int | None = None,
             human_guidance: str | None = None) -> dict:
    """
    Full generation lifecycle. Caller already marked the row in_progress.
    Returns a structured result; also updates state on success/failure.

    content_type: override classifier. If None, classify_content_type() runs.
    force: if True, overwrite an existing target file. Default False — abort
           with overwrite_blocked status so hand-edited pages are never
           clobbered by a cron tick.
    escalation_id / human_guidance: set by --resume-escalation. When
           escalation_id is provided, on success we shell out to
           seo/escalate.py mark-resumed so the escalation row flips
           from 'replied' to 'resumed'. human_guidance is prepended at
           the very top of the prompt as binding context.
    """
    if content_type is None:
        content_type = classify_content_type(keyword)
    if content_type not in CONTENT_TYPES:
        content_type = "guide"

    product_cfg = load_product_config(product)
    # Normalize to canonical name from config so DB writes never diverge by casing
    product = product_cfg["name"]
    repo_path = os.path.expanduser(
        product_cfg.get("landing_pages", {}).get("repo", "")
    )
    if not repo_path or not os.path.isdir(repo_path):
        update_state(trigger, product, keyword, "pending",
                     notes="repo missing on disk", slug=slug,
                     content_type=content_type)
        return {"success": False, "error": f"repo not found: {repo_path}",
                "content_type": content_type}

    existing = find_existing_target_path(repo_path, content_type, slug)
    if existing and not force:
        rel = os.path.relpath(existing, repo_path)
        note = f"overwrite_blocked: target already exists at {rel}"
        _base_url = (product_cfg.get("landing_pages", {}).get("base_url")
                     or product_cfg.get("website", "")).rstrip("/")
        _route_prefix = CONTENT_TYPES.get(content_type, CONTENT_TYPES["guide"])["route_prefix"]
        existing_page_url = f"{_base_url}{_route_prefix}{slug}" if _base_url else None
        update_state(trigger, product, keyword, "done",
                     notes=note[:500], slug=slug,
                     content_type=content_type,
                     page_url=existing_page_url)
        return {"success": False, "error": note,
                "content_type": content_type,
                "existing_path": existing}

    setup = check_consumer_setup(repo_path)
    # The 1-3 missing branch ("soft") will not return early; we inject
    # setup_missing into the prompt and let the model self-heal as part of
    # the normal page-build run. Track that here so build_prompt can grow
    # a SETUP SELF-HEAL block.
    setup_missing_for_prompt: list[str] | None = None
    # On a resume, trust the human guidance: they have presumably told the
    # model which setup-client-website phases to apply. The model will run
    # those before writing the page; we cannot enforce setup pre-flight or
    # we will re-escalate before Claude is even spawned. If the model fails
    # to fix setup, the downstream build/verification will catch it and
    # leave the escalation in 'replied' for the next tick to retry.
    if not setup["ok"] and escalation_id is not None:
        print(f"  [resume #{escalation_id}] setup gate has "
              f"{len(setup['missing'])} missing items; skipping pre-flight "
              f"because human guidance is binding context")
    elif not setup["ok"]:
        # Self-heal is the ONLY default path. Pass the missing pieces to the
        # model via build_prompt; the SETUP SELF-HEAL block tells it to
        # invoke the setup-client-website skill (Phase 2c/2d/4a/4d/4e etc.)
        # before writing the page, with a 30 tool-call budget. If the model
        # decides setup is genuinely unfixable mid-run, it can call
        # escalate.py itself with trigger=model_initiated.
        reason = "; ".join(setup["missing"])[:400]
        setup_missing_for_prompt = setup["missing"]
        print(f"  [setup-self-heal] {len(setup['missing'])} missing piece(s); "
              f"injecting into prompt: {reason}")

    sources = resolve_source_paths(product_cfg)
    source_block = format_source_block(sources)
    prompt = build_prompt(product, keyword, slug, trigger, product_cfg,
                          source_block, content_type=content_type,
                          human_guidance=human_guidance,
                          setup_missing=setup_missing_for_prompt)

    log_dir = SCRIPT_DIR / "logs" / product.lower()
    concepts_dir = SCRIPT_DIR / "concepts" / product.lower()

    stream = run_claude_stream(prompt=prompt, cwd=repo_path,
                               log_dir=log_dir, slug=slug)

    final_json = parse_final_json(stream["final_result_text"])
    concept = parse_concept(stream["final_result_text"])

    source_paths = [s["path"] for s in sources if s["exists"]]
    touches = count_source_touches(stream["tool_summary"], source_paths)

    save_concept_file(concepts_dir, slug, product, keyword,
                      concept, final_json, stream["tool_summary"], touches)

    session_id = stream.get("session_id")

    # Stream-level errors (process crash, timeout) are still hard failures.
    if stream.get("error"):
        update_state(trigger, product, keyword, "pending",
                     notes=stream["error"][:500], slug=slug,
                     content_type=content_type,
                     claude_session_id=session_id)
        return {"success": False, "error": stream["error"],
                "content_type": content_type,
                "stream_log": stream["stream_log_path"],
                "tool_summary": stream["tool_summary"]}

    # Step 0 path: model decided to consolidate this slug into an existing
    # page rather than write a new file. Skip the page-file gates (typecheck,
    # theme, fact-verification) because there is no new page.tsx; the actual
    # write was a redirect rule in next.config.ts plus an optional edit on
    # the canonical page. We trust the auto-commit + Vercel build to catch
    # an invalid redirect rule (next will refuse to start), and downstream
    # GSC/dashboard rollups will see the new keyword as `done` pointing at
    # the canonical URL.
    if final_json and final_json.get("action") == "consolidate":
        target_slug = (final_json.get("target_slug") or "").strip()
        target_url = (final_json.get("target_url") or "").strip()
        rationale = (final_json.get("rationale") or "").strip()
        if not target_slug:
            update_state(trigger, product, keyword, "pending",
                         notes="consolidate_missing_target_slug"[:500],
                         slug=slug, content_type=content_type,
                         claude_session_id=session_id)
            return {"success": False,
                    "error": "consolidate action returned without target_slug",
                    "content_type": content_type,
                    "stream_log": stream["stream_log_path"],
                    "tool_summary": stream["tool_summary"]}
        note = f"consolidated into {target_slug}; {rationale}"[:500]
        update_state(trigger, product, keyword, "done",
                     notes=note, slug=slug,
                     content_type=content_type,
                     page_url=target_url or None,
                     claude_session_id=session_id)
        print(f"  [consolidate] {slug} -> {target_slug} ({rationale})")
        return {"success": True,
                "action": "consolidate",
                "slug": slug,
                "target_slug": target_slug,
                "target_url": target_url,
                "rationale": rationale,
                "content_type": content_type,
                "stream_log": stream["stream_log_path"],
                "tool_summary": stream["tool_summary"]}

    # Whether the inner Claude session reported success itself. If False, we
    # attempt salvage: when the page is committed AND live, treat as success
    # regardless of the missing final marker (e.g. inner session credit-out
    # after the file was already committed/pushed).
    claimed_success = bool(final_json and final_json.get("success"))
    claimed_err = (final_json or {}).get("error", "no final success JSON from claude")

    expected_file_candidates = [
        tmpl.format(slug=slug)
        for tmpl in CONTENT_TYPES[content_type]["path_candidates"]
    ]

    # Pipeline-side typecheck gate. The prompt asks the inner session to run
    # `npx tsc --noEmit`; this enforces it. If the session skipped typecheck
    # and left broken TS in the working tree, the ~/git-dashboard auto-commit
    # cron would otherwise push it to main within ~60s and break the deploy
    # (PieLine aiphoneordering.com incident, 2026-04-21). On failure we
    # restore/remove the candidate page files so auto-commit has nothing to
    # push, then mark the row pending.
    tc = typecheck_and_cleanup(repo_path, expected_file_candidates,
                               restore_on_fail=False)
    if not tc["ok"] and session_id and not tc.get("skipped"):
        # Typecheck-fix retry. Resume the same Claude session with the tsc
        # errors appended, ask for a targeted patch, then re-run typecheck.
        # Salvages lanes where Claude wrote mostly-right TSX with a small
        # type slip (unknown prop, missing local module, wrong generic).
        # Only one retry — if it still fails, fall through to cleanup.
        tsc_err = tc.get("error", "")[-4000:]
        retry_prompt = (
            "Your previous work failed `npx tsc --noEmit`. The errors are "
            "below. Fix them in the files you already wrote. Constraints: "
            "do NOT rename or create new files, do NOT change behavior or "
            "copy, keep edits minimal. After editing, run "
            "`npx tsc --noEmit` yourself to confirm it passes, then "
            "`git add -A && git commit -m \"fix(typecheck): " + slug + "\"`.\n\n"
            "End your final message with exactly one JSON block on its own "
            "line. If fixed successfully: `{\"success\": true}`. "
            "If the errors are structural and you cannot fix in place "
            "without a rewrite: `{\"success\": false, \"error\": "
            "\"cannot fix in place\"}` and stop.\n\n"
            "Typecheck output:\n" + tsc_err
        )
        retry_log_dir = Path(stream["stream_log_path"]).parent
        retry = run_claude_stream_resume(
            session_id, retry_prompt, cwd=repo_path,
            log_dir=retry_log_dir, slug=slug,
        )
        # Re-check. On second failure this also performs the restore/remove
        # so auto-commit can't push a broken page.
        tc = typecheck_and_cleanup(repo_path, expected_file_candidates)
        if tc["ok"]:
            # Second pass passed. Prefer the retry's final result text for
            # the downstream JSON-parse since it's the most recent signal.
            new_final = retry.get("final_result_text") or ""
            if new_final:
                stream = dict(stream)
                stream["final_result_text"] = new_final
                final_json = parse_final_json(new_final)
                claimed_success = bool(final_json and final_json.get("success"))
                claimed_err = (final_json or {}).get("error",
                                                     "no final success JSON from claude")

    if not tc["ok"]:
        # Ensure the cleanup step ran — first pass used restore_on_fail=False
        # so Claude could see the broken file during the retry. If retry was
        # attempted, the second call above already did the cleanup; otherwise
        # run it now so auto-commit can't push broken TS.
        if "cleaned" not in tc:
            tc = typecheck_and_cleanup(repo_path, expected_file_candidates)
        tsc_err = tc.get("error", "")[-800:]
        cleaned = tc.get("cleaned", [])
        note = f"typecheck_failed; cleaned={cleaned}; tsc_tail={tsc_err}"[:500]
        update_state(trigger, product, keyword, "pending",
                     notes=note, slug=slug,
                     content_type=content_type,
                     claude_session_id=session_id)
        return {"success": False,
                "error": f"typecheck failed: {tsc_err}",
                "content_type": content_type,
                "cleaned": cleaned,
                "stream_log": stream["stream_log_path"],
                "tool_summary": stream["tool_summary"]}

    attr = validate_booking_attribution(repo_path, expected_file_candidates)
    if not attr["ok"]:
        attr_err = attr.get("error", "")[:800]
        cleaned = attr.get("cleaned", [])
        note = f"booking_attribution_failed; cleaned={cleaned}; {attr_err}"[:500]
        update_state(trigger, product, keyword, "pending",
                     notes=note, slug=slug,
                     content_type=content_type,
                     claude_session_id=session_id)
        return {"success": False,
                "error": attr_err,
                "content_type": content_type,
                "cleaned": cleaned,
                "stream_log": stream["stream_log_path"],
                "tool_summary": stream["tool_summary"]}

    # Theme lint. Rejects pages that ship light-only Tailwind (bg-white,
    # bg-gray-50, text-gray-900, text-blue-600, bg-{accent}-50, etc.) on a
    # dark consumer without a paired dark:* variant. Skipped on light
    # consumers. Same restore/remove cleanup as the gates above so the
    # auto-commit cron can't push the bad page while we retry.
    theme = validate_theme_classes(repo_path, expected_file_candidates)
    if not theme["ok"]:
        theme_err = theme.get("error", "")[:800]
        cleaned = theme.get("cleaned", [])
        note = f"theme_lint_failed; cleaned={cleaned}; {theme_err}"[:500]
        update_state(trigger, product, keyword, "pending",
                     notes=note, slug=slug,
                     content_type=content_type,
                     claude_session_id=session_id)
        return {"success": False,
                "error": theme_err,
                "content_type": content_type,
                "cleaned": cleaned,
                "stream_log": stream["stream_log_path"],
                "tool_summary": stream["tool_summary"]}

    # Fact-verification gates. Three checks, cheapest first, each follows the
    # same cleanup-and-pending pattern as the gates above. Backs the post-
    # 2026-04-27 tenxats audit (Sean Thompson / mcp.10xats.com / Greenhouse
    # AI Principles date / Ashby fraud-detection date / OpenCATS license /
    # 'Real Talent AI' product name etc).
    #
    # Gate A: dead-URL probe. Pure Python, no LLM. Catches invented endpoints
    #         like https://mcp.10xats.com/v1.
    # Gate B: time-sensitive claim trip-wire. Regex matches '<Vendor> shipped
    #         X in <Month Year>' / '<Vendor> raised $X' / '<Person> named CEO'
    #         and re-verifies each via Claude+WebSearch. Catches wrong dates
    #         and amounts.
    # Gate C: full claims extractor + verifier. Claude+WebSearch reads the
    #         page, extracts every checkable factual claim, and confirms each
    #         against a fresh search. Catches mis-named products, invented
    #         metric precision, and wrong license attribution.
    #
    # Skipped failures (verifier exception, claude unreachable) return ok=True
    # with a 'skipped' field so a transient web outage never blocks a page.
    # Only verdicts the verifier returned with confidence trip the gate.
    # Gates run in cheap-to-expensive order. The keyword_answer gate is a
    # mandatory anti-dead-end check: the page must literally answer the
    # search query (or transparently document why the answer is unavailable
    # with an authoritative verification trail). A real Fazm visitor
    # commented "gbvat number?" on a page that ranked for the keyword but
    # punted on the actual datum. Don't be a dead end.
    keyword_for_gate = (keyword or "").strip()
    for gate_name, gate_fn in (
        # dead_urls: first pass with cleanup=False so we can hand the list
        # back to Claude for a fix-up attempt before reverting.
        ("dead_urls", lambda repo, files: verify_dead_urls(
            repo, files, cleanup=False)),
        ("time_sensitive",
         lambda repo, files: verify_time_sensitive_claims(repo, files)),
        ("factual_claims",
         lambda repo, files: extract_and_verify_factual_claims(repo, files)),
        ("keyword_answer",
         lambda repo, files: verify_keyword_directly_answered(
             repo, files, keyword_for_gate)),
    ):
        gate_result = gate_fn(repo_path, expected_file_candidates)
        if gate_result.get("ok"):
            continue

        # dead_urls fixup: resume the Claude session with the list of dead
        # URLs so it can replace or remove them, then re-run the gate once.
        # Only attempt when we have a session to resume and there is at least
        # one dead URL to report. Any other gate failure falls straight through
        # to the revert-and-pending path below.
        if gate_name == "dead_urls" and session_id:
            dead = gate_result.get("dead_urls", [])
            if dead:
                dead_list = "\n".join(
                    f"  - {d['url']} ({d['error']}) in "
                    f"{', '.join(d.get('files', []))}"
                    for d in dead[:10]
                )
                fixup_prompt = (
                    f"The fact-gate found {len(dead)} dead URL(s) in the page "
                    "you just wrote. These URLs must not be published.\n\n"
                    f"Dead URLs:\n{dead_list}\n\n"
                    "For each dead URL:\n"
                    "1. Placeholder (your-*, example.com, localhost, "
                    "127.0.0.1, etc.) -- remove the link entirely.\n"
                    "2. Real but wrong path -- find the correct URL via "
                    "WebSearch and replace it.\n"
                    "3. No valid replacement exists -- remove the link.\n\n"
                    "After fixing every dead URL, run:\n"
                    "  git add -A && git commit --amend --no-edit && "
                    "git push --force-with-lease\n"
                    "Then verify with `git log -1 --oneline`. "
                    "As your FINAL message output exactly one JSON line:\n"
                    '`{"success": true, "slug": "' + (slug or "") + '", '
                    '"page_url": "..."}` on success, or '
                    '`{"success": false, "error": "..."}` if you cannot fix '
                    "all dead URLs. Budget: <=15 tool calls."
                )
                retry_log_dir = Path(stream["stream_log_path"]).parent
                try:
                    run_claude_stream_resume(
                        session_id, fixup_prompt, cwd=repo_path,
                        log_dir=retry_log_dir, slug=(slug or ""),
                        timeout=600,
                    )
                except Exception:
                    pass  # best-effort; fall through to re-gate
            # Re-run dead_urls gate -- this time with cleanup so any remaining
            # bad files are reverted before we potentially fail out.
            gate_result = verify_dead_urls(
                repo_path, expected_file_candidates, cleanup=True)
            if gate_result.get("ok"):
                continue  # fixup worked; advance to the next gate

        gate_err = gate_result.get("error", "")[:800]
        cleaned = gate_result.get("cleaned", [])
        note = f"{gate_name}_failed; cleaned={cleaned}; {gate_err}"[:500]
        update_state(trigger, product, keyword, "pending",
                     notes=note, slug=slug,
                     content_type=content_type,
                     claude_session_id=session_id)
        return {"success": False,
                "error": gate_err,
                "content_type": content_type,
                "cleaned": cleaned,
                "fact_gate": gate_name,
                "fact_details": {k: v for k, v in gate_result.items()
                                 if k not in ("cleaned", "error", "ok")},
                "stream_log": stream["stream_log_path"],
                "tool_summary": stream["tool_summary"]}

    verify = {"ok": False, "error": f"no candidate matched: {expected_file_candidates}"}
    last_verify_err = ""
    for candidate in expected_file_candidates:
        v = verify_commit_landed(repo_path, candidate)
        if v["ok"]:
            verify = v
            verify["file"] = candidate
            break
        last_verify_err = v.get("error", "")
    if not verify["ok"] and last_verify_err:
        verify["error"] = f"{last_verify_err} (tried {len(expected_file_candidates)} candidates)"

    # Salvage retry: when Claude bailed without emitting the final success JSON
    # AND no commit landed, the session likely ran out of context or got
    # confused mid-task. Resume the session ONE TIME and ask it to finish:
    # write whatever piece is missing, then `git add -A && commit && push`,
    # then emit the success JSON. Cheap because the session already has full
    # context. Killed Clone + tenxats in the 2026-04-28 run.
    if not verify["ok"] and not claimed_success and session_id:
        retry_log_dir = Path(stream["stream_log_path"]).parent
        salvage_prompt = (
            "Your previous session ended without committing a page or emitting "
            "the final success JSON. The pipeline is about to fail this lane. "
            "Finish the work now: if a page file is partially written, complete "
            "it; if nothing was written, write the page at "
            f"`{repo_path}/{expected_file_candidates[0]}` per the original "
            "instructions. Then run "
            "`git add -A && git commit -m \"feat(seo): " + slug + "\" && git push`. "
            "Verify the commit landed with `git log -1 --oneline`. As your FINAL "
            "message, output exactly one JSON line: "
            "`{\"success\": true, \"slug\": \"" + slug + "\", \"page_url\": \"...\"}` "
            "on success, or `{\"success\": false, \"error\": \"...\"}` if you "
            "genuinely cannot complete it. Budget: <=20 tool calls."
        )
        try:
            salvage = run_claude_stream_resume(
                session_id, salvage_prompt, cwd=repo_path,
                log_dir=retry_log_dir, slug=slug,
                timeout=900,
            )
        except Exception as e:
            salvage = {"exit_code": -1, "final_result_text": "",
                       "tool_summary": {}, "error": f"salvage exception: {e}"}
        # Re-check verify on each candidate after the salvage attempt.
        for candidate in expected_file_candidates:
            v = verify_commit_landed(repo_path, candidate)
            if v["ok"]:
                verify = v
                verify["file"] = candidate
                break
        if verify.get("ok"):
            # Adopt the salvage stream's final result for downstream parsing.
            new_final = salvage.get("final_result_text") or ""
            if new_final:
                stream = dict(stream)
                stream["final_result_text"] = new_final
                final_json = parse_final_json(new_final)
                claimed_success = bool(final_json and final_json.get("success"))
                claimed_err = (final_json or {}).get(
                    "error", "no final success JSON from claude")

    if not verify["ok"]:
        # Surface the real gate error (e.g. "no commit on origin/main ...").
        # When Claude also skipped the final success JSON, append that as
        # secondary context so we can tell apart "Claude finished but push
        # races lost" from "Claude never finished at all".
        gate_err = verify.get("error", "") or "commit not on origin/main"
        err = gate_err if claimed_success else f"{gate_err}; claude={claimed_err}"
        update_state(trigger, product, keyword, "pending",
                     notes=f"commit not on origin/main: {err}"[:500],
                     slug=slug, content_type=content_type,
                     claude_session_id=session_id)
        return {"success": False, "error": err,
                "content_type": content_type,
                "stream_log": stream["stream_log_path"],
                "tool_summary": stream["tool_summary"]}

    # Blog-post manifest update. Only fires after the page commit landed; we
    # parse the just-written page.tsx for metadata and append/replace the entry
    # in src/app/blog/_manifest.ts, then commit + push so the index, RSS, and
    # tag pages pick it up. Skipped silently on consumers without a manifest
    # file (they're not on the BlogPostLayout pattern yet). A failure here
    # marks the row pending so the cron retries; the page itself is already
    # live, so this is purely about the listing surfaces catching up.
    if content_type == "blog_post":
        landed_path = verify.get("file") or expected_file_candidates[0]
        manifest_result = update_blog_manifest(repo_path, landed_path)
        if not manifest_result.get("ok"):
            mfst_err = manifest_result.get("error", "")[:400]
            update_state(trigger, product, keyword, "pending",
                         notes=f"manifest_update_failed: {mfst_err}"[:500],
                         slug=slug, content_type=content_type,
                         claude_session_id=session_id)
            return {"success": False,
                    "error": f"manifest update failed: {mfst_err}",
                    "content_type": content_type,
                    "stream_log": stream["stream_log_path"],
                    "tool_summary": stream["tool_summary"]}

    base_url = (product_cfg.get("landing_pages", {}).get("base_url")
                or product_cfg.get("website", "")).rstrip("/")
    has_website = bool(base_url)
    constructed_url = f"{base_url}{CONTENT_TYPES[content_type]['route_prefix']}{slug}" if has_website else ""
    page_url = (final_json or {}).get("page_url") or constructed_url

    # Always probe the live URL before marking done. Vercel/Netlify deploys
    # take minutes after git push, and the inner Claude session may declare
    # success the moment the commit lands. Wait for the page to actually serve.
    # If the product has no configured website (repo-only), trust the commit.
    if has_website and page_url:
        live = probe_url_live(page_url)
        if not live["ok"]:
            # Same pattern as the commit verify gate: surface the real probe
            # error primarily. The DB note already kept both sides; mirror
            # that shape in the return so the lane log isn't misleading.
            live_err = live.get("error", "") or "url not live"
            ret_err = live_err if claimed_success else f"{live_err}; claude={claimed_err}"
            update_state(trigger, product, keyword, "pending",
                         notes=f"url not live: claude={'ok' if claimed_success else claimed_err}; live={live_err}"[:500],
                         slug=slug, content_type=content_type,
                         claude_session_id=session_id)
            return {"success": False,
                    "error": ret_err,
                    "content_type": content_type,
                    "stream_log": stream["stream_log_path"],
                    "tool_summary": stream["tool_summary"]}
    else:
        live = {"ok": True, "status": "no-website-configured"}

    salvage_note = ""
    if not claimed_success:
        salvage_note = f"salvaged after claude={claimed_err}; commit={verify['commit_sha']}; url={live.get('status')}"

    update_state(trigger, product, keyword, "done",
                 page_url=page_url, slug=slug,
                 content_type=content_type,
                 notes=salvage_note or None,
                 claude_session_id=session_id)

    # Close out an escalation if this run was a resume. Best-effort: a
    # mark-resumed failure should not flip the page success to failure.
    if escalation_id is not None:
        try:
            subprocess.run(
                [sys.executable, str(SCRIPT_DIR / "escalate.py"), "mark-resumed",
                 "--id", str(escalation_id),
                 "--log-path", stream["stream_log_path"],
                 "--outcome", "success"],
                check=False, timeout=30,
            )
        except Exception as e:
            print(f"  WARN: mark-resumed failed for #{escalation_id}: {e}",
                  file=sys.stderr)

    return {
        "success": True,
        "page_url": page_url,
        "commit_sha": verify["commit_sha"],
        "content_type": content_type,
        "concept": concept,
        "tool_summary": stream["tool_summary"],
        "source_touches": touches,
        "stream_log": stream["stream_log_path"],
        "salvaged": bool(salvage_note),
        "salvage_note": salvage_note,
        "escalation_id": escalation_id,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    # When --resume-escalation is given, product/keyword/slug are loaded
    # from the seo_escalations row, so they are not required on the CLI.
    ap.add_argument("--product")
    ap.add_argument("--keyword")
    ap.add_argument("--slug")
    ap.add_argument("--trigger", choices=["serp", "gsc", "manual", "reddit", "twitter", "top_page", "roundup"], default="manual")
    ap.add_argument("--content-type", choices=list(CONTENT_TYPES.keys()), default=None,
                    help="Override the regex classifier. Default: auto-classify from keyword.")
    ap.add_argument("--force", action="store_true",
                    help="Overwrite an existing page at the target path. Default: abort if the target file already exists.")
    ap.add_argument("--resume-escalation", type=int, default=None,
                    help="Resume from a replied seo_escalations row. "
                         "Loads product/keyword/slug from the row, prepends "
                         "the human reply into the prompt as binding "
                         "guidance, and on success calls escalate.py "
                         "mark-resumed.")
    args = ap.parse_args()

    escalation_id = args.resume_escalation
    human_guidance = None
    trigger = args.trigger
    product = args.product
    keyword = args.keyword
    slug = args.slug

    if escalation_id is not None:
        resp = api_get("/api/v1/seo/escalations",
                       query={"mode": "show", "id": escalation_id}, ok_on_404=True)
        row = None if resp.get("_not_found") else (resp.get("data") or {}).get("row")
        if not row:
            print(f"ERROR: escalation #{escalation_id} not found", file=sys.stderr)
            return 1
        e_product = row.get("product")
        e_keyword = row.get("keyword")
        e_slug = row.get("slug")
        e_status = row.get("status")
        e_reply = row.get("human_reply")
        e_source_table = row.get("source_table")
        if e_status != "replied":
            print(f"ERROR: escalation #{escalation_id} status={e_status} (expected 'replied')",
                  file=sys.stderr)
            return 1
        if not e_reply or not e_reply.strip():
            print(f"ERROR: escalation #{escalation_id} has empty human_reply",
                  file=sys.stderr)
            return 1
        product = product or e_product
        keyword = keyword or e_keyword
        slug = slug or e_slug
        # Pick trigger from the source table if caller did not specify
        if args.trigger == "manual":
            trigger = "gsc" if e_source_table == "gsc_queries" else "serp"
        human_guidance = e_reply

    if not (product and keyword and slug):
        print("ERROR: --product, --keyword, --slug are required (or use --resume-escalation)",
              file=sys.stderr)
        return 2

    result = generate(product=product, keyword=keyword,
                      slug=slug, trigger=trigger,
                      content_type=args.content_type,
                      force=args.force,
                      escalation_id=escalation_id,
                      human_guidance=human_guidance)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
