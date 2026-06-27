#!/usr/bin/env python3
"""Build the personal-brand PERSONA from the user's own public footprint (step 2,
2026-06-26). Companion to scripts/saps_mode.py + the menu-bar toggle.

Personal-brand mode (the menu-bar toggle) drafts organic replies for the persona
project (config.json entry with `persona: true`). Those replies are only as good
as how well the persona is GROUNDED in who the user actually is. This script
assembles a grounding CORPUS from the user's own footprint so the persona's
description / content_angle / voice / search_topics reflect the real person, the
same way the original 2026-02 flow grounded every reply in "Matthew's work."

DESIGN (mirrors the onboarding profile_scan philosophy):
  - This script GATHERS a corpus and emits `grounding_instructions`. It does NOT
    synthesize voice/description by itself in the default mode — synthesis stays
    in the conversation (or a deliberate --apply step) so the user reviews and
    confirms before anything is written. Keeping a human in the loop is the whole
    privacy contract here.
  - PUBLICLY PUBLISHABLE ONLY. Every source is something the user has already
    made public (a bio, public posts, a public repo, a public website) OR, for
    the opt-in local/Chrome sources, reduced to non-identifying topical signal
    (interest domains, not PII). Nothing private (cookies, passwords, private
    files, contacts, message bodies) is ever read or emitted.

SOURCES (each best-effort; a failure is recorded and skipped, never fatal):
  x        the connected X account's bio + original posts + replies, via the
           existing read-only scripts/scan_x_profile.py (managed Chrome :9555).
           This is the strongest authentic-voice signal.
  github   a public GitHub profile: bio, top repos, languages, repo blurbs, via
           the public REST API (no auth, public data only).
  website  a personal website URL: visible title/description/headings only.
  provided arbitrary text blobs the caller passes with --source LABEL=<file>.
           Use this to fold in footprint the host agent already gathered with the
           linkedin-agent / reddit-agent (which need auth this script won't touch).
  local    OPT-IN (--include-local): a tiny allowlist of obviously-public files
           (e.g. an about.md you point at with --local-file). Default OFF; prints
           exactly what it would read.
  chrome   OPT-IN (--include-chrome): the SET OF DISTINCT DOMAINS in Chrome
           history as an interest signal — no full URLs, no titles, no
           timestamps, no PII. Default OFF; a loud warning is printed.

Usage:
  # Gather a corpus (public sources) and print it for the agent/user to review:
  python3 scripts/build_persona.py gather --github m13v --website https://m13v.com

  # Fold in agent-gathered LinkedIn/Reddit text:
  python3 scripts/build_persona.py gather --source linkedin=/tmp/li.txt --source reddit=/tmp/rd.txt

  # Opt in to local / Chrome interest signal (prints what it reads first):
  python3 scripts/build_persona.py gather --include-local --local-file ~/about.md --include-chrome

  # Apply a REVIEWED persona (synthesized by the agent/user) to config + DB:
  python3 scripts/build_persona.py apply --from /tmp/persona.json
      where persona.json = {"description": "...", "content_angle": "...",
                            "voice": {...}, "search_topics": ["...", ...]}

Read-only in `gather`. `apply` writes ONLY the persona project's grounding fields
in config.json and seeds its search_topics into the DB (via seed_search_topics).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import saps_mode  # noqa: E402  (config/persona resolution, shared source of truth)

PUBLIC_ONLY_NOTE = (
    "PUBLICLY PUBLISHABLE ONLY: every field below must be safe to post in "
    "public. Use only what the user has already made public (bios, public "
    "posts, public repos, a public site) or non-identifying interest signal. "
    "Never include private data, contact details, or anything the user would "
    "not say to a stranger."
)

GROUNDING_INSTRUCTIONS = (
    "You are grounding the user's PERSONAL-BRAND persona from their own public "
    "footprint. From the corpus, synthesize four fields and CONFIRM with the "
    "user before applying:\n"
    "  description    2-3 sentences: who this person is as a builder/voice.\n"
    "  content_angle  one paragraph of concrete, first-hand experience the "
    "persona can speak from (real projects, real numbers, real pain).\n"
    "  voice          {tone, never[]}: how they actually write (read their own "
    "posts/replies in the x source). Keep the organic rules: first person, "
    "specific, no links, no feature lists, no sales, no em dashes.\n"
    "  search_topics  ~15 topics they have genuine experience with.\n"
    + PUBLIC_ONLY_NOTE
    + "\nThen write the four fields to /tmp/persona.json and run "
    "`build_persona.py apply --from /tmp/persona.json`, or hand them to the "
    "project_config tool for the persona project."
)


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #
def _gather_x(handle: str | None, posts: int, comments: int) -> dict:
    """Reuse the read-only X profile scanner (managed Chrome). Best-effort."""
    script = HERE / "scan_x_profile.py"
    if not script.exists():
        return {"ok": False, "error": "scan_x_profile.py not found"}
    py = os.environ.get("SAPS_PYTHON") or sys.executable or "python3"
    cmd = [py, str(script), "--posts", str(posts), "--comments", str(comments)]
    if handle:
        cmd += ["--handle", handle]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except Exception as e:
        return {"ok": False, "error": f"scan_x_profile failed: {e}"}
    # The scanner prints a JSON object as its last stdout line.
    last = ""
    for line in (res.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            last = line
    if not last:
        return {"ok": False, "error": "scan_x_profile produced no JSON",
                "stderr_tail": (res.stderr or "")[-300:]}
    try:
        obj = json.loads(last)
    except Exception as e:
        return {"ok": False, "error": f"scan_x_profile JSON parse: {e}"}
    return obj


def _http_json(url: str, timeout: int = 20):
    req = urllib.request.Request(
        url, headers={"User-Agent": "s4l-build-persona", "Accept": "application/vnd.github+json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _gather_github(user: str) -> dict:
    """Public GitHub profile + top repos. No auth (public data only)."""
    try:
        prof = _http_json(f"https://api.github.com/users/{user}")
        repos = _http_json(
            f"https://api.github.com/users/{user}/repos?sort=pushed&per_page=20"
        )
    except Exception as e:
        return {"ok": False, "error": f"github fetch failed: {e}"}
    top = []
    for r in sorted(repos, key=lambda x: x.get("stargazers_count", 0), reverse=True)[:12]:
        top.append({
            "name": r.get("name"),
            "description": r.get("description"),
            "language": r.get("language"),
            "stars": r.get("stargazers_count"),
            "topics": r.get("topics") or [],
        })
    return {
        "ok": True,
        "login": prof.get("login"),
        "name": prof.get("name"),
        "bio": prof.get("bio"),
        "blog": prof.get("blog"),
        "followers": prof.get("followers"),
        "public_repos": prof.get("public_repos"),
        "top_repos": top,
    }


def _gather_website(url: str) -> dict:
    """Visible title/description/headings of a public page. Best-effort, no JS."""
    import html
    import re
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "s4l-build-persona"})
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read(400_000).decode("utf-8", "replace")
    except Exception as e:
        return {"ok": False, "error": f"website fetch failed: {e}"}

    def _find(pat):
        m = re.search(pat, raw, re.I | re.S)
        return html.unescape(re.sub(r"<[^>]+>", "", m.group(1)).strip()) if m else None

    title = _find(r"<title[^>]*>(.*?)</title>")
    desc = None
    m = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']', raw, re.I)
    if m:
        desc = html.unescape(m.group(1).strip())
    heads = [
        html.unescape(re.sub(r"<[^>]+>", "", h).strip())
        for h in re.findall(r"<h[12][^>]*>(.*?)</h[12]>", raw, re.I | re.S)
    ]
    heads = [h for h in heads if h][:15]
    return {"ok": True, "url": url, "title": title, "description": desc, "headings": heads}


def _gather_provided(specs: list[str]) -> list[dict]:
    """--source LABEL=path: fold in text the agent already gathered."""
    out = []
    for spec in specs or []:
        if "=" not in spec:
            out.append({"ok": False, "error": f"bad --source {spec!r} (need LABEL=path)"})
            continue
        label, path = spec.split("=", 1)
        try:
            text = Path(path).expanduser().read_text(errors="replace")
            out.append({"ok": True, "label": label, "text": text[:20_000]})
        except Exception as e:
            out.append({"ok": False, "label": label, "error": str(e)})
    return out


def _gather_local(files: list[str]) -> dict:
    """OPT-IN. Read a short allowlist of caller-named, obviously-public files."""
    read = []
    for f in files or []:
        p = Path(f).expanduser()
        print(f"[build_persona] local: reading {p}", file=sys.stderr)
        try:
            read.append({"ok": True, "path": str(p), "text": p.read_text(errors="replace")[:20_000]})
        except Exception as e:
            read.append({"ok": False, "path": str(p), "error": str(e)})
    return {"ok": True, "files": read, "note": "only files you explicitly passed with --local-file"}


def _gather_chrome_domains(limit: int = 80) -> dict:
    """OPT-IN. Distinct domains from Chrome history as an INTEREST signal only.

    No full URLs, no page titles, no timestamps, no PII. Reads a temp COPY of the
    history DB (Chrome locks the live file), counts visits per host, returns the
    top hosts. This is interest-level signal (what topics the user follows), the
    kind of thing already inferable from their public posts.
    """
    print(
        "[build_persona] WARNING: --include-chrome reads your Chrome history to "
        "extract DISTINCT DOMAINS only (no URLs/titles/PII). Ctrl-C now to abort.",
        file=sys.stderr,
    )
    base = Path.home() / "Library/Application Support/Google/Chrome"
    candidates = [base / "Default/History"] + sorted(base.glob("Profile */History"))
    src = next((c for c in candidates if c.exists()), None)
    if not src:
        return {"ok": False, "error": "no Chrome History DB found"}
    from urllib.parse import urlparse
    from collections import Counter
    tmp = Path(tempfile.gettempdir()) / "s4l_chrome_history_copy.sqlite"
    try:
        shutil.copy2(src, tmp)
        con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        rows = con.execute("SELECT url, visit_count FROM urls").fetchall()
        con.close()
    except Exception as e:
        return {"ok": False, "error": f"chrome history read failed: {e}"}
    finally:
        try:
            tmp.unlink()
        except Exception:
            pass
    hosts: "Counter[str]" = Counter()
    for url, vc in rows:
        host = (urlparse(url).hostname or "").lstrip("www.")
        if host and "." in host:
            hosts[host] += int(vc or 1)
    top = [{"domain": h, "weight": w} for h, w in hosts.most_common(limit)]
    return {"ok": True, "top_domains": top,
            "note": "interest signal only: distinct domains, no URLs/titles/PII"}


# --------------------------------------------------------------------------- #
# gather / apply
# --------------------------------------------------------------------------- #
def cmd_gather(args) -> int:
    sources: dict = {}
    sources["x"] = _gather_x(args.handle, args.posts, args.comments)
    if args.github:
        sources["github"] = _gather_github(args.github)
    if args.website:
        sources["website"] = _gather_website(args.website)
    if args.source:
        sources["provided"] = _gather_provided(args.source)
    if args.include_local:
        sources["local"] = _gather_local(args.local_file)
    if args.include_chrome:
        sources["chrome"] = _gather_chrome_domains()

    corpus = {
        "ok": True,
        "persona_project": saps_mode.persona_name() or "(none configured)",
        "public_only": True,
        "sources": sources,
        "grounding_instructions": GROUNDING_INSTRUCTIONS,
    }
    print(json.dumps(corpus, ensure_ascii=False, indent=2))
    return 0


def cmd_apply(args) -> int:
    """Write a REVIEWED persona into config.json + seed its topics into the DB."""
    try:
        data = json.loads(Path(args.from_file).read_text())
    except Exception as e:
        print(f"could not read --from {args.from_file!r}: {e}", file=sys.stderr)
        return 2

    name = saps_mode.persona_name()
    if not name:
        print("no persona project (persona:true) in config.json", file=sys.stderr)
        return 2

    cfg_path = saps_mode.config_path()
    cfg = json.loads(cfg_path.read_text())
    proj = next((p for p in cfg.get("projects", []) if p.get("name") == name), None)
    if proj is None:
        print(f"persona project {name!r} vanished from config", file=sys.stderr)
        return 2

    # Merge ONLY the grounding fields; never touch enabled/persona/weight or any
    # marketing field (the persona must stay link-free and out of the promo pick).
    changed = []
    for field in ("description", "content_angle", "voice"):
        if field in data and data[field]:
            proj[field] = data[field]
            changed.append(field)
    topics = data.get("search_topics")
    if isinstance(topics, list) and topics:
        proj["search_topics"] = [str(t).strip() for t in topics if str(t).strip()]
        changed.append("search_topics")

    if args.dry_run:
        print(json.dumps({"would_update": name, "fields": changed,
                          "search_topics": proj.get("search_topics")}, indent=2))
        return 0

    tmp = cfg_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(cfg_path)
    print(f"[build_persona] updated config.json persona {name!r}: {', '.join(changed)}")

    # Seed the (possibly new) topics into project_search_topics via the canonical
    # path, so pick_search_topic has a live universe for the persona.
    if "search_topics" in changed:
        seed = HERE / "seed_search_topics.py"
        py = os.environ.get("SAPS_PYTHON") or sys.executable or "python3"
        try:
            r = subprocess.run([py, str(seed), "--project", name],
                               capture_output=True, text=True, timeout=120)
            print((r.stdout or r.stderr or "").strip()[-400:])
        except Exception as e:
            print(f"[build_persona] topic seed failed (run manually): {e}", file=sys.stderr)
    return 0


def main(argv) -> int:
    ap = argparse.ArgumentParser(description="Build the personal-brand persona from public footprint")
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("gather", help="gather a grounding corpus (read-only)")
    g.add_argument("--handle", default=None, help="X @handle (default: live logged-in handle)")
    g.add_argument("--posts", type=int, default=20)
    g.add_argument("--comments", type=int, default=50)
    g.add_argument("--github", default=None, help="public GitHub username")
    g.add_argument("--website", default=None, help="personal website URL")
    g.add_argument("--source", action="append", default=[], metavar="LABEL=path",
                   help="fold in agent-gathered text (repeatable)")
    g.add_argument("--include-local", action="store_true", help="opt in to reading --local-file files")
    g.add_argument("--local-file", action="append", default=[], help="a public file to include (repeatable)")
    g.add_argument("--include-chrome", action="store_true", help="opt in to Chrome interest domains")
    g.set_defaults(func=cmd_gather)

    a = sub.add_parser("apply", help="write a reviewed persona to config + DB")
    a.add_argument("--from", dest="from_file", required=True, help="persona JSON file")
    a.add_argument("--dry-run", action="store_true", help="show the change, write nothing")
    a.set_defaults(func=cmd_apply)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
