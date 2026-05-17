#!/usr/bin/env python3
"""Audience-page routing helper.

Each project in config.json can declare `landing_pages.audience_pages`, a list
of curated deep landing pages (not auto-generated SEO /t/<slug> pages — those
live in a separate rail). Each entry looks like:

  {
    "angle": "founder-ghostwriting",
    "url": "https://s4l.ai/ghostwriting",
    "match_keywords": ["ghostwriter", "tweet ghostwriter", ...],
    "when": "human-readable trigger description for LLMs / docs"
  }

This module is the single source of truth for:

  - loading audience_pages for a project
  - matching a candidate's nominated topic/keyword to an audience-page angle
  - mapping a URL back to its angle (for post-hoc tagging)
  - formatting an audience_pages block for injection into post-draft prompts
    (used once post_reddit.py and post_github.py are unlocked to consume it)

Used by twitter_gen_links.py to short-circuit the A/B page-gen lane when a
curated audience page exists for the candidate's topic.
"""

from __future__ import annotations

import json
import os
import re
from typing import Optional
from urllib.parse import urlsplit

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(REPO_DIR, "config.json")


def _load_config() -> dict:
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def _find_project(cfg: dict, name: str) -> Optional[dict]:
    if not name:
        return None
    name_lc = name.lower()
    for p in cfg.get("projects", []):
        if (p.get("name") or "").lower() == name_lc:
            return p
    return None


def load_audience_pages(project_name: str) -> list[dict]:
    """Return the audience_pages list for a project, or [] if none configured.

    Each entry is the raw dict from config.json. Caller does not mutate.
    """
    try:
        cfg = _load_config()
    except Exception:
        return []
    proj = _find_project(cfg, project_name)
    if not proj:
        return []
    lp = (proj.get("landing_pages") or {})
    pages = lp.get("audience_pages") or []
    out = []
    for entry in pages:
        if not isinstance(entry, dict):
            continue
        if not entry.get("url") or not entry.get("angle"):
            continue
        out.append(entry)
    return out


def _normalize(s: str) -> str:
    """Lowercase + collapse whitespace + strip punctuation for substring match."""
    if not s:
        return ""
    s = s.lower()
    s = re.sub(r"[^a-z0-9\s]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def match_by_keyword(
    project_name: str,
    *,
    keyword: Optional[str] = None,
    topic: Optional[str] = None,
    reply_text: Optional[str] = None,
    thread_title: Optional[str] = None,
) -> Optional[dict]:
    """Pick the best-matching audience page for a candidate.

    Match strategy: for each audience-page entry, check whether ANY of its
    `match_keywords` (case-insensitive, normalized substring) appears in
    ANY of the provided signals (keyword, topic, reply_text, thread_title).

    Returns the matched entry dict (with `angle`, `url`, ...) or None.

    First-match-wins ordered by audience_pages list order (so config.json
    list ordering acts as priority). This is intentional: the most specific
    angle should sit first.
    """
    pages = load_audience_pages(project_name)
    if not pages:
        return None

    haystacks: list[str] = []
    for v in (keyword, topic, reply_text, thread_title):
        n = _normalize(v or "")
        if n:
            haystacks.append(n)
    if not haystacks:
        return None

    for entry in pages:
        kws = entry.get("match_keywords") or []
        for kw in kws:
            kw_norm = _normalize(kw)
            if not kw_norm:
                continue
            for hay in haystacks:
                if kw_norm in hay:
                    return entry
    return None


def classify_url_as_audience_page(url: str, project_name: str) -> Optional[str]:
    """Map a URL back to an audience-page angle, or None if not a known page.

    Used for post-hoc tagging in `posts.link_source` when a URL was baked
    into a draft directly (without going through resolve_link()). Match is
    exact-URL OR same-host + same-path (ignoring query/fragment).
    """
    if not url or not project_name:
        return None
    pages = load_audience_pages(project_name)
    if not pages:
        return None
    try:
        target = urlsplit(url.strip())
    except Exception:
        return None
    target_host = (target.netloc or "").lower().lstrip("www.")
    target_path = (target.path or "/").rstrip("/") or "/"

    for entry in pages:
        try:
            ep = urlsplit(entry["url"])
        except Exception:
            continue
        ep_host = (ep.netloc or "").lower().lstrip("www.")
        ep_path = (ep.path or "/").rstrip("/") or "/"
        if ep_host == target_host and ep_path == target_path:
            return entry.get("angle")
    return None


def prompt_block(project_name: str) -> str:
    """Render an audience_pages block for injection into a post-draft LLM prompt.

    Returns "" if the project has no audience_pages. Otherwise returns a short
    markdown-friendly block the LLM can use to pick the right deep URL.
    """
    pages = load_audience_pages(project_name)
    if not pages:
        return ""
    lines = [
        "Curated audience landing pages for this project. Pick the BEST match",
        "for the thread topic and bake the chosen URL into the reply text;",
        "if none obviously match, link to the project homepage as usual.",
        "",
    ]
    for entry in pages:
        lines.append(f"- angle: {entry['angle']}")
        lines.append(f"  url: {entry['url']}")
        when = entry.get("when") or ""
        if when:
            lines.append(f"  when_to_use: {when}")
        kws = entry.get("match_keywords") or []
        if kws:
            lines.append(f"  keyword_signals: {', '.join(kws[:12])}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# CLI for ops / testing
# ---------------------------------------------------------------------------

def _cli() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Audience-page lookup helper")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List audience pages for a project")
    p_list.add_argument("--project", required=True)

    p_match = sub.add_parser("match", help="Match a keyword/topic to an audience page")
    p_match.add_argument("--project", required=True)
    p_match.add_argument("--keyword", default=None)
    p_match.add_argument("--topic", default=None)
    p_match.add_argument("--reply", default=None)
    p_match.add_argument("--title", default=None)

    p_classify = sub.add_parser("classify", help="Classify a URL as an audience page")
    p_classify.add_argument("--project", required=True)
    p_classify.add_argument("--url", required=True)

    p_prompt = sub.add_parser("prompt", help="Render the prompt block for a project")
    p_prompt.add_argument("--project", required=True)

    args = ap.parse_args()
    if args.cmd == "list":
        print(json.dumps(load_audience_pages(args.project), indent=2))
        return 0
    if args.cmd == "match":
        out = match_by_keyword(
            args.project,
            keyword=args.keyword,
            topic=args.topic,
            reply_text=args.reply,
            thread_title=args.title,
        )
        print(json.dumps(out, indent=2) if out else "null")
        return 0 if out else 1
    if args.cmd == "classify":
        angle = classify_url_as_audience_page(args.url, args.project)
        print(angle or "")
        return 0 if angle else 1
    if args.cmd == "prompt":
        print(prompt_block(args.project))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
