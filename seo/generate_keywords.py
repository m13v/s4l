#!/usr/bin/env python3
"""
Generate keyword candidates for any product using DataForSEO API.

Usage:
    python3 generate_keywords.py <product_name>
    python3 generate_keywords.py assrt
    python3 generate_keywords.py fazm

Uses two DataForSEO endpoints:
  1. keyword_suggestions: expand seed keywords from config.json topics
  2. keywords_for_site: steal competitor keywords

Filters by volume >= 20 and competition LOW/MEDIUM.
Merges into seo/state/<product>/underserved_keywords.json without
overwriting scored/done entries.
"""

import json
import os
import re
import sys
import urllib.request
import base64
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from project_topics import topics_for_project  # noqa: E402
from http_api import api_get, api_post  # noqa: E402


def slugify(keyword: str) -> str:
    """Normalize a keyword into a URL-safe slug.

    Lowercases, strips apostrophes entirely (so "claude's" -> "claudes"),
    replaces any run of non-alphanumerics with a single dash, collapses
    duplicate dashes, and trims dashes from the ends.
    """
    s = (keyword or "").lower().strip()
    s = s.replace("'", "").replace("\u2019", "")  # ASCII + curly apostrophe
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s

SCRIPT_DIR = Path(__file__).parent
ROOT_DIR = SCRIPT_DIR.parent
CONFIG_PATH = ROOT_DIR / "config.json"
ENV_PATH = ROOT_DIR / ".env"

MIN_VOLUME = 20
MAX_COMPETITION = {"LOW", "MEDIUM", None, "N/A"}


def project_min_volume(project):
    """Per-project override for MIN_VOLUME. Niche B2B services rarely clear the
    default 20/mo floor; lower it via `min_keyword_volume` in config.json."""
    try:
        return int(project.get("min_keyword_volume") or MIN_VOLUME)
    except (TypeError, ValueError):
        return MIN_VOLUME

# Keywords containing these terms are likely irrelevant (e.g. theater playwrights)
NOISE_PATTERNS = {
    # Theater playwrights and theater terms
    "oscar wilde", "sam shepard", "arthur miller", "wallace shawn",
    "mamet", "tennessee williams", "eugene o'neill", "eugene o neill",
    "beckett", "chekhov", "ibsen", "moliere", "shakespeare", "broadway",
    "theater", "theatre", "dramatis", "stage play", "noel coward",
    "maxwell anderson", "august wilson", "bernard shaw", "shaw playwright",
    "neil simon", "christopher marlowe", "marlowe playwright",
    "simon playwright", "define playwright", "playwright bar",
    "playwright nyc", "playwright definition", "famous playwright",
    "playwright meaning", "what is a playwright", "playwright synonym",
    "playwright vs screenwriter", "playwright anton", "playwright irish",
    "playwright english", "playwright american", "playwright british",
    "playwright french", "playwright russian", "playwright german",
    "playwright greek", "lorraine hansberry", "edward albee",
    "tom stoppard", "harold pinter", "david mamet", "tony kushner",
    "caryl churchill", "suzan-lori parks", "lynn nottage",
    "playwright simon", "playwright marlowe", "playwright coward",
    "playwright anderson", "playwright wilson", "playwright shaw",
    "playwright neil", "playwright christopher", "playwright eugene",
    "testing: ai",  # malformed keyword
}


def load_env():
    """Load .env file into os.environ."""
    if ENV_PATH.exists():
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ.setdefault(key.strip(), val.strip())


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def find_project(config, name):
    name_lower = name.lower()
    for p in config.get("projects", []):
        if p["name"].lower() == name_lower:
            return p
    return None


def dataforseo_request(endpoint, payload):
    """Make a DataForSEO API request."""
    login = os.environ.get("DATAFORSEO_LOGIN", "")
    password = os.environ.get("DATAFORSEO_PASSWORD", "")
    if not login or not password:
        print("ERROR: DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD must be set in .env")
        sys.exit(1)

    url = f"https://api.dataforseo.com/v3/{endpoint}"
    auth = base64.b64encode(f"{login}:{password}".encode()).decode()
    data = json.dumps(payload).encode()

    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Basic {auth}")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            if result.get("status_code") != 20000:
                print(f"  API error: {result.get('status_message')}")
                return None
            cost = result.get("cost", 0)
            if cost:
                print(f"  API cost: ${cost:.4f}")
            return result
    except Exception as e:
        print(f"  API request failed: {e}")
        return None


def fetch_keyword_suggestions(seed_keyword, limit=50, min_volume=MIN_VOLUME):
    """Get keyword suggestions from DataForSEO Labs."""
    result = dataforseo_request(
        "dataforseo_labs/google/keyword_suggestions/live",
        [{"keyword": seed_keyword, "location_code": 2840, "language_code": "en",
          "limit": limit, "include_seed_keyword": True}]
    )
    if not result:
        return []

    items = result["tasks"][0]["result"][0].get("items") or []
    keywords = []
    for item in items:
        ki = item.get("keyword_info", {})
        vol = ki.get("search_volume", 0) or 0
        comp = ki.get("competition_level")
        if vol >= min_volume and comp in MAX_COMPETITION:
            keywords.append({
                "keyword": item["keyword"],
                "volume": vol,
                "competition": comp or "N/A",
                "source": f"suggestion:{seed_keyword}",
            })
    return keywords


def fetch_keywords_for_site(domain, limit=50, min_volume=MIN_VOLUME):
    """Get keywords a competitor ranks for."""
    result = dataforseo_request(
        "dataforseo_labs/google/keywords_for_site/live",
        [{"target": domain, "location_code": 2840, "language_code": "en",
          "limit": limit}]
    )
    if not result:
        return []

    items = result["tasks"][0]["result"][0].get("items") or []
    keywords = []
    for item in items:
        ki = item.get("keyword_info", {})
        vol = ki.get("search_volume", 0) or 0
        comp = ki.get("competition_level")
        if vol >= min_volume and comp in MAX_COMPETITION:
            keywords.append({
                "keyword": item["keyword"],
                "volume": vol,
                "competition": comp or "N/A",
                "source": f"competitor:{domain}",
            })
    return keywords


def extract_competitor_domains(project):
    """Extract competitor domains from config.

    Two sources, merged and deduped:
      1. Top-level `competitor_domains` (explicit list of domains).
      2. `competitive_positioning` keys mapped via `domain_map`.
    """
    domains = []
    for d in project.get("competitor_domains") or []:
        if d and isinstance(d, str):
            domains.append(d.strip().lower())

    comp = project.get("competitive_positioning", {})
    for key, desc in comp.items():
        name = key.replace("vs_", "").replace("_", " ")
        domain_map = {
            "qa wolf": "qawolf.com",
            "momentic": "momentic.ai",
            "manual playwright": None,
            "verkada rhombus": "verkada.com",
            "hakimo": "hakimo.ai",
            "guard": None,
            "soundhound": "soundhound.com",
            "loman": "loman.ai",
            "conversenow": "conversenow.ai",
            "human staff": None,
            "per minute pricing": None,
        }
        domain = domain_map.get(name)
        if domain:
            domains.append(domain)

    seen = set()
    unique = []
    for d in domains:
        if d not in seen:
            seen.add(d)
            unique.append(d)
    return unique


def generate_keywords_dataforseo(project):
    """Generate keywords using DataForSEO API."""
    all_keywords = []
    min_vol = project_min_volume(project)

    # 1. Keyword suggestions from topics
    # Use more specific seed phrases to avoid ambiguity (e.g. "playwright" the person)
    topics = topics_for_project(project["name"])
    seed_suffixes = ["tool", "framework", "software", "automation"]
    print(f"\n  Fetching suggestions for {len(topics)} topics (min vol >= {min_vol})...")
    for topic in topics:
        # If the topic is a single ambiguous word, make it more specific
        if len(topic.split()) == 1 and topic.lower() in {"playwright", "cypress", "selenium"}:
            seeds = [f"{topic} testing"]
        else:
            seeds = [topic]
        for seed in seeds:
            print(f"    Topic: {seed}")
            kws = fetch_keyword_suggestions(seed, limit=30, min_volume=min_vol)
            print(f"    -> {len(kws)} keywords (vol >= {min_vol})")
            all_keywords.extend(kws)

    # 2. Competitor keyword stealing
    domains = extract_competitor_domains(project)
    if domains:
        print(f"\n  Stealing keywords from {len(domains)} competitors (min vol >= {min_vol})...")
        for domain in domains:
            print(f"    Competitor: {domain}")
            kws = fetch_keywords_for_site(domain, limit=30, min_volume=min_vol)
            print(f"    -> {len(kws)} keywords (vol >= {min_vol})")
            all_keywords.extend(kws)

    # Deduplicate by keyword, keep highest volume, filter noise
    seen = {}
    for kw in all_keywords:
        key = kw["keyword"].lower()
        # Filter out noise (e.g. theater playwrights for "playwright" topic)
        if any(noise in key for noise in NOISE_PATTERNS):
            continue
        # Skip single-word keywords (too broad to rank for)
        if len(key.split()) < 2:
            continue
        # Skip keywords that are just rearrangements of the same 1-2 words
        words = set(key.split())
        if len(words) <= 2 and kw["volume"] > 50000:
            continue
        # Skip overly generic high-volume terms (>20K) that we can't realistically rank for
        if kw["volume"] > 20000:
            continue
        # Relevance filter: keyword must contain at least one relevant word
        # Build relevance word set from topics, features, competitors, and industry terms
        if not hasattr(generate_keywords_dataforseo, '_relevance_words'):
            rw = set()
            for t in topics_for_project(project["name"]):
                rw.update(w.lower() for w in t.split() if len(w) > 3)
            rw.add(project["name"].lower())
            for f in project.get("features", [])[:10]:
                rw.update(w.lower() for w in f.split() if len(w) > 4)
            # Add competitor names
            for ck in project.get("competitive_positioning", {}):
                rw.update(w.lower() for w in ck.replace("vs_", "").split("_") if len(w) > 3)
            # Add ICP terms (icp may be a dict {personas, verticals} or a flat list)
            icp_raw = project.get("icp") or []
            if isinstance(icp_raw, dict):
                icp_raw = [s for v in icp_raw.values() for s in (v if isinstance(v, list) else [v])]
            for icp in icp_raw[:5]:
                rw.update(w.lower() for w in str(icp).split() if len(w) > 4)
            generate_keywords_dataforseo._relevance_words = rw
        if not any(tw in key for tw in generate_keywords_dataforseo._relevance_words):
            continue
        if key not in seen or kw["volume"] > seen[key]["volume"]:
            seen[key] = kw
    unique = sorted(seen.values(), key=lambda x: x["volume"], reverse=True)

    return unique


def generate_keyword_templates(project):
    """Fallback: generate keywords from templates (no API needed)."""
    name = project["name"]
    name_lower = name.lower()
    topics = topics_for_project(name)
    features = project.get("features", [])
    competitive = project.get("competitive_positioning", {})

    keywords = []

    topic_prefixes = [
        "how to {topic}", "{topic} guide", "{topic} best practices",
        "{topic} tutorial", "automated {topic}", "ai {topic}",
        "best {topic} tools", "{topic} framework",
    ]
    for topic in topics:
        for template in topic_prefixes:
            kw = template.format(topic=topic.lower())
            keywords.append({"keyword": kw, "slug": slugify(kw), "source": "topic_template"})

    for comp_key in competitive:
        comp_name = comp_key.replace("vs_", "").replace("_", " ")
        for kw in [f"{comp_name} alternative", f"{comp_name} vs {name_lower}",
                    f"{name_lower} vs {comp_name}", f"best {comp_name} alternative free",
                    f"{comp_name} alternative open source"]:
            keywords.append({"keyword": kw, "slug": slugify(kw), "source": "competitor"})

    seen = set()
    unique = []
    for kw in keywords:
        if kw["keyword"] not in seen:
            seen.add(kw["keyword"])
            unique.append(kw)
    return unique


def merge_keywords_db(product_name, new_candidates):
    """Merge new candidates via the keywords API, which skips existing keywords
    and existing page slugs and bumps volume via GREATEST. Server-side mirror of
    the former direct-Postgres merge."""
    candidates = []
    for candidate in new_candidates:
        key = candidate["keyword"].lower()
        candidates.append({
            "keyword": candidate["keyword"],
            "slug": candidate.get("slug") or slugify(key),
            "source": candidate.get("source", "dataforseo"),
            "volume": candidate.get("volume"),
            "competition": candidate.get("competition"),
        })

    resp = api_post("/api/v1/seo/keywords",
                    {"product": product_name, "candidates": candidates})
    data = resp.get("data") or {}
    return (
        int(data.get("added") or 0),
        int(data.get("updated") or 0),
        int(data.get("total") or 0),
        data.get("statuses") or {},
    )


def main():
    load_env()

    if len(sys.argv) < 2:
        print("Usage: python3 generate_keywords.py <product_name> [--fallback]")
        config = load_config()
        names = [p["name"] for p in config.get("projects", [])]
        print("Products:", ", ".join(names))
        sys.exit(1)

    product_name = sys.argv[1]
    use_fallback = "--fallback" in sys.argv

    config = load_config()
    project = find_project(config, product_name)

    if not project:
        print(f"Error: product '{product_name}' not found in config.json")
        sys.exit(1)

    print(f"Generating keywords for: {project['name']}")
    print(f"  Topics: {len(topics_for_project(project['name']))}")
    print(f"  Competitors: {len(extract_competitor_domains(project))}")
    print(f"  Min volume: {project_min_volume(project)}")

    if use_fallback or not os.environ.get("DATAFORSEO_LOGIN"):
        print("  Mode: template fallback (no DataForSEO API)")
        candidates = generate_keyword_templates(project)
    else:
        print("  Mode: DataForSEO API")
        candidates = generate_keywords_dataforseo(project)

    print(f"\n  Generated: {len(candidates)} candidates")

    added, updated, total, statuses = merge_keywords_db(project["name"], candidates)

    print(f"  New added: {added}")
    print(f"  Volume updated: {updated}")
    print(f"  Total in DB: {total}")
    print(f"  Status breakdown: {json.dumps(statuses)}")

    # Show top keywords by volume from the API
    resp = api_get("/api/v1/seo/keywords",
                   query={"mode": "top_volume", "product": project["name"], "limit": 10})
    rows = (resp.get("data") or {}).get("rows") or []
    if rows:
        print(f"\n  Top keywords by volume:")
        for r in rows:
            print(f"    {int(r.get('volume') or 0):>6} | {str(r.get('keyword') or ''):50s} | {r.get('status')}")


if __name__ == "__main__":
    main()
