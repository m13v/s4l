#!/usr/bin/env python3
"""Prototype + unit test for a LinkedIn author-exclusion matcher.

Demonstrates the proposed approach against the REAL exclusion sources
(config.json exclusions + author_blocklist via reply_db) and synthetic
candidates shaped exactly like what discover_linkedin_candidates.py emits
(author_name + author_profile_url). No browser, no side effects.
"""
import json
import os
import re
import subprocess
import sys

REPO = os.path.expanduser("~/social-autoposter")


def load_config_exclusions():
    with open(os.path.join(REPO, "config.json")) as f:
        c = json.load(f)
    ex = c.get("exclusions", {})
    # slugs/handles (lowercased) and any name-style entries (contain a space)
    raw = [s.lower().strip() for s in (ex.get("authors", []) + ex.get("linkedin_profiles", [])) if s]
    slugs = {s for s in raw if " " not in s}
    names = {s for s in raw if " " in s}
    return slugs, names


def load_blocklist_hard(platform="linkedin"):
    """Real author_blocklist hard handles via reply_db CLI (read-only)."""
    try:
        out = subprocess.run(
            ["python3", os.path.join(REPO, "scripts", "reply_db.py"),
             "blocklist", "list", platform],
            capture_output=True, text=True, timeout=30,
        )
        handles = set()
        for line in (out.stdout or "").splitlines():
            m = re.search(r"\b([a-z0-9][a-z0-9\-_%]{2,})\b", line.lower())
            # best-effort; the CLI's exact format varies, so we also try JSON
            if line.strip().startswith("{") or line.strip().startswith("["):
                try:
                    data = json.loads(line)
                    rows = data if isinstance(data, list) else data.get("data", [])
                    for r in rows:
                        if (r.get("severity") == "hard") and r.get("handle"):
                            handles.add(r["handle"].lower())
                except Exception:
                    pass
        return handles
    except Exception:
        return set()


def slug_of(url):
    m = re.search(r"/in/([^/?#]+)", url or "")
    return m.group(1).lower() if m else None


def norm_name(n):
    return re.sub(r"\s+", " ", (n or "").strip().lower())


def is_excluded(candidate, slugs, names, block_handles):
    """Return (excluded: bool, reason: str)."""
    slug = slug_of(candidate.get("author_profile_url"))
    name = norm_name(candidate.get("author_name"))
    if slug and (slug in slugs or slug in block_handles):
        return True, f"slug_match:{slug}"
    if name and (name in names or name in block_handles):
        return True, f"name_match:{name}"
    return False, "not_excluded"


def main():
    slugs, names = load_config_exclusions()
    block_handles = load_blocklist_hard("linkedin")
    print("=== REAL exclusion sources ===")
    print("config slugs/handles:", sorted(slugs))
    print("config name-style    :", sorted(names))
    print("author_blocklist hard:", sorted(block_handles))
    print()

    cases = [
        ("Louis post, normal scrape", {"author_name": "Louis Beaumont",
            "author_profile_url": "https://www.linkedin.com/in/louis030195/"}),
        ("Louis reshare, NAME only (no profile url)", {"author_name": "Louis Beaumont",
            "author_profile_url": None}),
        ("Louis comment-urn style url", {"author_name": "Louis Beaumont",
            "author_profile_url": "https://www.linkedin.com/in/louis030195/?miniProfileUrn=x"}),
        ("Decoy: Louis Jordan (ElevenLabs)", {"author_name": "Louis Jordan",
            "author_profile_url": "https://www.linkedin.com/in/louisjor/"}),
        ("Decoy: random namesake", {"author_name": "Louis Beaumont",
            "author_profile_url": "https://www.linkedin.com/in/louis-beaumont/"}),
    ]
    print("=== matcher results ===")
    for label, cand in cases:
        excl, reason = is_excluded(cand, slugs, names, block_handles)
        flag = "EXCLUDED " if excl else "allowed  "
        print(f"[{flag}] {label:<42} -> {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
