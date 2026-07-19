#!/usr/bin/env python3
"""Repo hygiene scanner: block PII, secrets, images, and absolute home paths.

This is a PUBLIC repo. The scanner is the single source of truth reused by:
  - the shared pre-commit hook (scripts/git-hooks/pre-commit), mode --staged
  - the gitleaks-adjacent CI job (.github/workflows/secret-scan.yml), mode --all
  - on-demand audits: `python3 scripts/scan_pii.py --all`

What it flags in staged/tracked content:
  1. Secret-shaped literals (tokens, private keys, db URLs with passwords).
  2. Real client/operator PII from a gitignored denylist (pii_denylist.local.txt):
     one term per line (email, name, handle, phone). Never commit that file.
  3. Tracked images / media (this repo stores none; see .gitignore).
  4. Absolute /Users/<name>/ home paths (leaks layout, breaks other operators).

Exit code 0 = clean, 1 = violations found (prints them). Override a specific
commit with `git commit --no-verify`, but fix the finding instead when you can.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DENYLIST_FILE = REPO / "pii_denylist.local.txt"

# Files/paths the scanner must not scan (it would flag its own rules/examples).
ALLOWLIST_SUFFIXES = (
    "scripts/scan_pii.py",
    "scripts/git-hooks/pre-commit",
    ".github/workflows/secret-scan.yml",
    ".gitignore",
    ".env.example",
    "config.example.json",
    "pii_denylist.local.txt",
)

SECRET_PATTERNS = [
    (r"ghp_[A-Za-z0-9]{36}", "GitHub personal access token"),
    (r"gh[opsu]_[A-Za-z0-9]{36}", "GitHub token"),
    (r"github_pat_[A-Za-z0-9_]{50,}", "GitHub fine-grained PAT"),
    (r"npm_[A-Za-z0-9]{36}", "npm token"),
    (r"sk-(?:proj-)?[A-Za-z0-9]{20,}", "OpenAI-style secret key"),
    (r"xox[baprs]-[A-Za-z0-9-]{10,}", "Slack token"),
    (r"AKIA[0-9A-Z]{16}", "AWS access key id"),
    (r"AIza[0-9A-Za-z_\-]{35}", "Google API key"),
    (r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY", "Private key block"),
    (r"(?:postgres(?:ql)?|mysql|mongodb)://[^\s:/'\"]+:[^\s@'\"]+@", "DB URL with inline password"),
    (r"(?i)(?:api[_-]?key|secret|passwd|password|auth[_-]?token)\s*[=:]\s*[\"'][^\"'\s]{16,}[\"']", "Hardcoded credential"),
]

# Absolute home path leak. Placeholder forms (/Users/<you>, /Users/USERNAME) pass.
HOME_PATH_RE = re.compile(r"/Users/(?!<|USER|USERNAME|you\b|me\b|name\b)[a-z0-9._-]{2,}", re.I)


def _home_path_exempt(path: str) -> bool:
    """launchd .plist files legitimately require absolute paths — launchd does not
    expand ~ or $HOME in ProgramArguments/StandardOutPath/etc. The repo already
    tracks 70+ of them, so exempt this file class from the absolute-home-path rule.
    Secrets, PII, and image checks still apply."""
    return path.startswith("launchd/") and path.endswith(".plist")

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".bmp", ".tiff"}


def _run(cmd: list[str]) -> str:
    return subprocess.run(cmd, cwd=REPO, capture_output=True, text=True).stdout


def _staged_files() -> list[str]:
    out = _run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"])
    return [f for f in out.splitlines() if f.strip()]


def _tracked_files() -> list[str]:
    return [f for f in _run(["git", "ls-files"]).splitlines() if f.strip()]


def _added_lines(path: str) -> list[tuple[int, str]]:
    """Return (lineno, text) for + lines in the staged diff of one file."""
    diff = _run(["git", "diff", "--cached", "--unified=0", "--", path])
    lines: list[tuple[int, str]] = []
    new_ln = 0
    for ln in diff.splitlines():
        if ln.startswith("@@"):
            m = re.search(r"\+(\d+)", ln)
            new_ln = int(m.group(1)) if m else new_ln
            continue
        if ln.startswith("+") and not ln.startswith("+++"):
            lines.append((new_ln, ln[1:]))
            new_ln += 1
        elif not ln.startswith("-"):
            new_ln += 1
    return lines


def _full_lines(path: str) -> list[tuple[int, str]]:
    p = REPO / path
    try:
        text = p.read_text(errors="replace")
    except (OSError, UnicodeDecodeError):
        return []
    return list(enumerate(text.splitlines(), start=1))


def _load_denylist() -> list[str]:
    if not DENYLIST_FILE.exists():
        return []
    terms = []
    for raw in DENYLIST_FILE.read_text().splitlines():
        t = raw.strip()
        if t and not t.startswith("#"):
            terms.append(t)
    return terms


def _is_allowlisted(path: str) -> bool:
    return any(path == s or path.endswith("/" + s) for s in ALLOWLIST_SUFFIXES)


def scan(paths: list[str], staged: bool) -> tuple[list[str], list[str]]:
    """Return (hard, soft) findings.

    hard  = secrets, denylist PII, images. Fail in every mode.
    soft  = absolute home paths. Hard-block NEW ones (--staged) but only warn on
            the existing tree (--all), since there is pre-existing debt to burn
            down gradually rather than block CI on day one.
    """
    denylist = _load_denylist()
    deny_re = None
    if denylist:
        deny_re = re.compile("|".join(re.escape(t) for t in denylist), re.I)
    secret_res = [(re.compile(p), label) for p, label in SECRET_PATTERNS]
    hard: list[str] = []
    soft: list[str] = []

    for path in paths:
        # Image / media files (path-based, no content read).
        if Path(path).suffix.lower() in IMAGE_EXT:
            hard.append(f"{path}: image/media file (this repo tracks no images)")
            continue
        if _is_allowlisted(path):
            continue

        lines = _added_lines(path) if staged else _full_lines(path)
        for lineno, text in lines:
            for rx, label in secret_res:
                if rx.search(text):
                    hard.append(f"{path}:{lineno}: possible {label}")
            if deny_re and deny_re.search(text):
                m = deny_re.search(text)
                hard.append(f"{path}:{lineno}: PII denylist match ('{m.group(0)}')")
            if HOME_PATH_RE.search(text) and not _home_path_exempt(path):
                m = HOME_PATH_RE.search(text)
                (hard if staged else soft).append(
                    f"{path}:{lineno}: absolute home path ('{m.group(0)}...')"
                )
    return hard, soft


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--staged", action="store_true", help="scan staged diff (pre-commit)")
    g.add_argument("--all", action="store_true", help="scan whole tracked tree (CI/audit)")
    args = ap.parse_args()

    staged = args.staged or not args.all
    paths = _staged_files() if staged else _tracked_files()
    hard, soft = scan(paths, staged=staged)

    if soft:
        print("Repo hygiene warnings (not blocking):\n", file=sys.stderr)
        for f in soft:
            print("  warn: " + f, file=sys.stderr)
        print("", file=sys.stderr)

    if not hard:
        return 0
    print("Repo hygiene scan found blocking issues:\n", file=sys.stderr)
    for f in hard:
        print("  " + f, file=sys.stderr)
    print(
        "\nFix the finding (move PII to a *.local.* gitignored file, drop the image, "
        "use an env/config lookup instead of an absolute path).\n"
        "If this is a genuine false positive, override this ONE commit with "
        "`git commit --no-verify`.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
