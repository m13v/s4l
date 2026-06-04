#!/usr/bin/env python3
"""Single source of truth for "is this LinkedIn author excluded?".

Every LinkedIn rail can call this instead of re-implementing its own matcher:
  - POST/comment rail   (discover_linkedin_candidates.py -> drop before the picker)
  - scoring             (score_linkedin_candidates.py     -> drop before upsert)
  - engage / mentions   (engage-linkedin.sh prompt        -> inject `slugs`)
  - DM candidate scan    (scan_dm_candidates.py)

WHY SLUG, NOT NAME (learned 2026-06-03 in the harness):
  A LinkedIn vanity slug (the `/in/<slug>/` segment, e.g. `louis030195`) is a
  unique, stable key. A display name is NOT: a people-search for "Louis
  Beaumont" returns a dozen unrelated real people. So:
    * slug match  -> HARD  (drop deterministically; this is the reliable path)
    * name match  -> SOFT  (flag for review only; never an automatic drop,
                            because it would hit innocent namesakes)
  In practice discover always extracts author_profile_url, so the slug path
  covers the normal case; the name path is a backstop for reshares/quotes that
  somehow carry only a name.

SOURCES (unioned, both optional, fail-open):
  1. config.json `exclusions.linkedin_profiles` + `exclusions.authors`
        - entries WITHOUT a space  -> hard slug
        - entries WITH a space     -> soft name
  2. author_blocklist via GET /api/v1/blocklist?platform=linkedin
        - severity=hard handle -> hard slug
        - severity=soft handle -> soft slug

This module does NO direct SQL: the blocklist is read over the website HTTP API
(per the project DB-access rule), and the read fails open (ok_on_404) so a
website hiccup can never wedge a posting cycle.
"""
from __future__ import annotations

import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

REPO = os.path.dirname(_HERE)
CONFIG_PATH = os.path.join(REPO, "config.json")


# ---------------------------------------------------------------- normalizers
def slug_from_url(url):
    """Extract the lowercased /in/<slug> segment from a LinkedIn profile URL."""
    m = re.search(r"/in/([^/?#]+)", url or "")
    return m.group(1).lower() if m else None


def norm_name(name):
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


# ------------------------------------------------------------------- sources
def _config_terms():
    """(hard_slugs, soft_names) from config.json exclusions."""
    hard_slugs, soft_names = set(), set()
    try:
        with open(CONFIG_PATH) as f:
            c = json.load(f)
    except Exception:
        return hard_slugs, soft_names
    ex = c.get("exclusions", {}) or {}
    for term in (ex.get("linkedin_profiles") or []) + (ex.get("authors") or []):
        t = (term or "").strip().lower()
        if not t:
            continue
        if " " in t:
            soft_names.add(re.sub(r"\s+", " ", t))
        else:
            hard_slugs.add(t.lstrip("@"))
    return hard_slugs, soft_names


def _blocklist_terms(platform="linkedin"):
    """(hard_handles, soft_handles) from the author_blocklist HTTP API.

    Fails open: any error -> empty sets, never raises into a posting cycle.
    """
    hard, soft = set(), set()
    try:
        from http_api import api_get
        resp = api_get("/api/v1/blocklist", query={"platform": platform},
                       ok_on_404=True)
        rows = ((resp or {}).get("data") or {}).get("rows") or []
        for r in rows:
            h = (r.get("handle") or "").strip().lstrip("@").lower()
            if not h:
                continue
            if r.get("severity") == "hard":
                hard.add(h)
            elif r.get("severity") == "soft":
                soft.add(h)
    except Exception:
        pass
    return hard, soft


def load_exclusions(platform="linkedin"):
    """Build the unioned exclusion sets once; reuse across many candidates."""
    cfg_hard, cfg_soft_names = _config_terms()
    bl_hard, bl_soft = _blocklist_terms(platform)
    return {
        "hard_slugs": cfg_hard | bl_hard,   # slug match -> drop
        "soft_slugs": bl_soft,              # slug match -> flag
        "soft_names": cfg_soft_names,       # name match -> flag
    }


# ------------------------------------------------------------------- matcher
def classify_author(author_name, author_profile_url, excl=None):
    """Return (severity, reason).

    severity: "hard" -> caller should DROP the candidate
              "soft" -> caller should KEEP but flag for review
              None   -> not excluded
    """
    if excl is None:
        excl = load_exclusions()
    slug = slug_from_url(author_profile_url)
    if slug:
        if slug in excl["hard_slugs"]:
            return "hard", f"slug:{slug}"
        if slug in excl["soft_slugs"]:
            return "soft", f"blocklist_soft_slug:{slug}"
    name = norm_name(author_name)
    if name and name in excl["soft_names"]:
        return "soft", f"name:{name}"
    return None, ""


def filter_candidates(candidates, excl=None):
    """Split a discover candidate list into (kept, dropped).

    `kept` keeps soft matches but tags them with `_exclusion_flag`. `dropped`
    are the hard matches. Each item is whatever shape discover emitted; we only
    read author_name + author_profile_url.
    """
    if excl is None:
        excl = load_exclusions()
    kept, dropped = [], []
    for cand in candidates or []:
        sev, reason = classify_author(
            cand.get("author_name"), cand.get("author_profile_url"), excl)
        if sev == "hard":
            cand = dict(cand)
            cand["_exclusion_reason"] = reason
            dropped.append(cand)
        else:
            if sev == "soft":
                cand = dict(cand)
                cand["_exclusion_flag"] = reason
            kept.append(cand)
    return kept, dropped


# ----------------------------------------------------------------------- CLI
def _extract_list(blob):
    """Find the candidate list inside a discover JSON payload."""
    if isinstance(blob, list):
        return blob, None
    for key in ("candidates", "results", "items"):
        if isinstance(blob.get(key), list):
            return blob[key], key
    return [], None


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    excl = load_exclusions()

    if cmd == "slugs":
        # Hard slugs only, comma-separated. For injecting into engage prompts.
        print(", ".join(sorted(excl["hard_slugs"])))
        return 0

    if cmd == "show":
        print(json.dumps({k: sorted(v) for k, v in excl.items()}, indent=2))
        return 0

    if cmd == "classify":
        name = argv[2] if len(argv) > 2 else ""
        url = argv[3] if len(argv) > 3 else ""
        sev, reason = classify_author(name, url, excl)
        print(json.dumps({"severity": sev, "reason": reason,
                          "excluded": sev == "hard"}))
        return 0 if sev == "hard" else 1

    if cmd == "filter":
        # Read discover JSON on stdin, drop hard matches, flag soft, re-emit.
        raw = sys.stdin.read()
        try:
            blob = json.loads(raw)
        except Exception:
            sys.stdout.write(raw)  # pass through unparseable input untouched
            return 0
        items, key = _extract_list(blob)
        kept, dropped = filter_candidates(items, excl)
        if key:
            blob[key] = kept
            out = blob
        else:
            out = kept
        sys.stderr.write(
            f"[li_exclusions] dropped_hard={len(dropped)} "
            f"flagged_soft={sum(1 for k in kept if k.get('_exclusion_flag'))} "
            f"kept={len(kept)} "
            f"slugs={sorted(excl['hard_slugs'])}\n"
        )
        for d in dropped:
            sys.stderr.write(
                f"[li_exclusions]   DROP {d.get('author_name')!r} "
                f"({d.get('author_profile_url')}) -> {d.get('_exclusion_reason')}\n"
            )
        print(json.dumps(out))
        return 0

    sys.stderr.write(
        "usage: linkedin_exclusions.py {slugs|show|classify <name> <url>|filter}\n"
        "  slugs    - comma-separated hard slug list (for prompt injection)\n"
        "  show     - dump the unioned exclusion sets as JSON\n"
        "  classify - exit 0 (+json) if <name>/<url> is a HARD exclusion\n"
        "  filter   - stdin discover JSON -> stdout with hard matches dropped\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
