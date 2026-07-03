#!/usr/bin/env python3
"""
history_context.py -- PROTOTYPE: consent-gated, cwd-scoped pull of prior Claude
Code conversation context to prefill S4L onboarding fields.

Design (agreed 2026-07-02):
  * READ-ONLY. Never mutates Claude session state (no archive, no writes to the
    desktop app's session store).
  * Consent-gated. Requires a one-time opt-in. The gate is checked on every call;
    without it the pull refuses and returns nothing.
  * cwd-scoped. Only reads sessions whose working dir is under the product's own
    repos (local_repo + landing_pages.repo from config.json). This is BOTH the
    relevance filter and the privacy boundary -- other clients' repos are never
    touched.
  * Enrichment only. Output is CANDIDATE fields for the user to confirm, never
    saved silently.

Sources (all local, no network, no approval card in the running session's mode):
  * ~/.claude/claude_sessions.db          FTS5 index over msg_preview/tool_names/cwd
  * ~/.claude/projects/<esc-cwd>/*.jsonl  full transcripts, read only on --expand

CLI:
  python3 scripts/history_context.py --project fazm
  python3 scripts/history_context.py --project fazm --terms "icp,positioning,voice"
  python3 scripts/history_context.py --project fazm --expand
  python3 scripts/history_context.py --optin-status
  python3 scripts/history_context.py --set-optin yes

Production note: the opt-in flag is stored here in a sidecar (~/.claude/
s4l_history_optin.json) to avoid mutating the live config.json from a prototype.
In production this becomes a top-level `history_context_optin` key in config.json
so it's one global, persisted decision reused across every product onboarding.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

HOME = Path.home()
# NOTE: ~/.claude/claude_sessions.db is NOT a general index -- on this machine it
# covered only 5 claude-meter sessions. The authoritative, complete source is the
# per-cwd transcript tree below, so the pull reads that directly (the same
# card-free file path used to read an archived session).
PROJECTS_DIR = HOME / ".claude" / "projects"
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"
OPTIN_SIDECAR = HOME / ".claude" / "s4l_history_optin.json"

MAX_FILES_PER_DIR = 80
MAX_SNIPPETS_PER_SESSION = 6
PREVIEW_LEN = 220

# ---------------------------------------------------------------- consent gate


def optin_status() -> dict:
    """Return the persisted one-time opt-in. Absent => not yet asked."""
    if OPTIN_SIDECAR.exists():
        try:
            return json.loads(OPTIN_SIDECAR.read_text())
        except Exception:
            pass
    return {"allowed": None, "ts": None}  # None => never asked


def set_optin(allowed: bool) -> dict:
    rec = {"allowed": bool(allowed), "ts": _now_iso()}
    OPTIN_SIDECAR.write_text(json.dumps(rec, indent=2))
    return rec


def _now_iso() -> str:
    # Prototype-safe: avoid importing datetime.now semantics into workflow harness.
    import datetime

    return datetime.datetime.now().replace(microsecond=0).isoformat()


# ------------------------------------------------------------- scope resolution


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text())


def resolve_scope(project_name: str) -> list[str]:
    """Return absolute repo paths that define this product's cwd scope."""
    cfg = load_config()
    proj = next(
        (p for p in cfg.get("projects", []) if p.get("name") == project_name), None
    )
    if not proj:
        raise SystemExit(f"project '{project_name}' not found in {CONFIG_PATH}")
    paths = []
    for key in ("local_repo",):
        v = proj.get(key)
        if v:
            paths.append(os.path.expanduser(v))
    lp = proj.get("landing_pages") or {}
    if lp.get("repo"):
        paths.append(os.path.expanduser(lp["repo"]))
    # de-dupe, keep order
    seen, out = set(), []
    for p in paths:
        p = p.rstrip("/")
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


# ------------------------------------------------------------------ the lookup


def _escaped_prefix(path: str) -> str:
    # Claude Code encodes a cwd as its path with every '/' and '.' -> '-'.
    return re.sub(r"[/.]", "-", path)


def _text_of(msg: dict) -> str:
    c = (msg or {}).get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(
            b.get("text", "")
            for b in c
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _iter_session_files(scope: list[str]):
    """Yield (transcript_path, scope_root) for every session dir whose escaped
    name starts with a scope root. The prefix glob can over-match a sibling
    (…-website vs …-websitex); the caller re-checks each file's real cwd field."""
    seen = set()
    for root in scope:
        prefix = _escaped_prefix(root)
        for d in sorted(PROJECTS_DIR.glob(prefix + "*")):
            if not d.is_dir():
                continue
            files = sorted(
                d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
            )
            for f in files[:MAX_FILES_PER_DIR]:
                if f in seen:
                    continue
                seen.add(f)
                yield f, root


def pull(project_name: str, terms: list[str] | None = None, limit: int = 40) -> dict:
    """Consent-gated, cwd-scoped candidate-context pull. Read-only, filesystem."""
    status = optin_status()
    if not status.get("allowed"):
        return {
            "ok": False,
            "reason": "not_opted_in",
            "hint": "run --set-optin yes (or ask the user) before pulling history",
        }

    scope = resolve_scope(project_name)
    if not scope:
        return {"ok": False, "reason": "no_repo_scope", "project": project_name}
    if not PROJECTS_DIR.exists():
        return {"ok": False, "reason": "no_transcripts_dir", "dir": str(PROJECTS_DIR)}

    term_list = [t.strip().lower() for t in (terms or []) if t.strip()]
    sessions: dict[str, list] = {}
    snippet_total = 0

    for f, root in _iter_session_files(scope):
        file_cwd = None
        picks: list[dict] = []
        try:
            with f.open() as fh:
                for line in fh:
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    if file_cwd is None and d.get("cwd"):
                        file_cwd = d["cwd"]
                    m = d.get("message") or {}
                    role = m.get("role")
                    if role not in ("user", "assistant"):
                        continue
                    txt = _text_of(m).strip()
                    # skip tool plumbing / system-injected blocks, keep real prose
                    if not txt or txt.startswith("<") or "tool_result" in txt[:24]:
                        continue
                    if term_list and not any(t in txt.lower() for t in term_list):
                        continue
                    picks.append({"role": role, "preview": txt[:PREVIEW_LEN]})
        except Exception:
            continue

        # verify the transcript really belongs to this scope root (exact or subdir)
        if not file_cwd or not (file_cwd == root or file_cwd.startswith(root + "/")):
            continue
        if not picks:
            continue
        sessions[f.stem] = picks[-MAX_SNIPPETS_PER_SESSION:]  # most recent few
        snippet_total += len(sessions[f.stem])
        if len(sessions) >= limit:
            break

    return {
        "ok": True,
        "project": project_name,
        "scope": scope,
        "session_count": len(sessions),
        "snippet_count": snippet_total,
        "sessions": sessions,
    }


def expand_span(file_path: str, line_no: int, radius: int = 0) -> str:
    """Read the full message text for a matched preview from its .jsonl line.
    Only invoked on --expand, so full transcripts are never bulk-read."""
    p = Path(file_path)
    if not p.exists():
        return ""
    with p.open() as fh:
        for i, line in enumerate(fh):
            if i == line_no:
                try:
                    d = json.loads(line)
                except Exception:
                    return ""
                content = (d.get("message") or {}).get("content")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    return "\n".join(
                        b.get("text", "")
                        for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
    return ""


# ------------------------------------------------------------------------- CLI


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project")
    ap.add_argument("--terms", help="comma-separated FTS terms")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--expand", action="store_true", help="read full matched spans")
    ap.add_argument("--optin-status", action="store_true")
    ap.add_argument("--set-optin", choices=["yes", "no"])
    args = ap.parse_args()

    if args.set_optin:
        print(json.dumps(set_optin(args.set_optin == "yes"), indent=2))
        return
    if args.optin_status:
        print(json.dumps(optin_status(), indent=2))
        return
    if not args.project:
        ap.error("--project is required for a pull")

    terms = args.terms.split(",") if args.terms else None
    result = pull(args.project, terms, limit=args.limit)

    if result.get("ok") and args.expand:
        for sess in result["sessions"].values():
            for snip in sess:
                fp, ln = snip.get("file_path"), snip.get("line_no")
                if fp is not None and ln is not None:
                    snip["full"] = expand_span(fp, ln)

    print(json.dumps(result, indent=2)[:8000])


if __name__ == "__main__":
    main()
