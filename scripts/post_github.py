#!/usr/bin/env python3
"""GitHub Issues posting orchestrator with momentum-gated candidate selection.

Two-phase design (consolidated 2026-04-24, replacing the short-lived
run_github_cycle.py):

  Phase 1: search project topics across N seeds, snapshot T0 comment + reaction
           counts. The originating seed is stamped on every candidate so the
           feedback loop (top_search_topics.py) gets fed back into the next run.
  Sleep --sleep seconds (default 600).
  Phase 2a: re-poll every candidate, compute delta_score = 3*Δcomments + 2*Δreactions.
  Phase 2b: adaptive cap (CAP_DEFAULT, bumped to CAP_BUMPED when >= HIGH_DELTA_BUMP
            candidates clear DELTA_THRESHOLD), Claude only drafts comments — no
            Bash tools, no in-flight searches, single JSON response. Python posts
            via gh and persists everything (search_topic, language, engagement_style,
            claude_session_id) to the posts table.

Why a single Python orchestrator instead of letting Claude search itself:
the pre-filter cuts Claude's tool budget to zero, the momentum gate suppresses
posts on stale threads, and the seed-per-candidate signal closes the
top_search_topics feedback loop. Claude returns one JSON in one shot.

Usage:
    python3 scripts/post_github.py
    python3 scripts/post_github.py --sleep 60 --dry-run         # quick dev
    python3 scripts/post_github.py --project Fazm               # force project
    python3 scripts/post_github.py --limit 5                    # caps adaptive cap
"""

import argparse
import atexit
import json
import os
import random
import re
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get
import pick_project
from author_history_block import render as _render_author_history
from project_topics import topics_for_project

# ---------------------------------------------------------------------------
# Run-summary safety net (atexit + SIGTERM/SIGHUP handlers).
# ---------------------------------------------------------------------------
# Mirrors the bash-side fix shipped to run-reddit-search.sh / run-twitter-cycle.sh
# / run-linkedin.sh: under SIGTERM the orchestrator can land between a
# successful gh-comment post (`posted += 1`) and the inline log_run.py call
# at the bottom of main(), silently dropping the run from run_monitor.log
# while the `posts` table already shows the comment.
#
# Mechanism:
#   - _RUN_STATE is a module-level dict main() updates as it runs
#     (run_start, cost, posted, skipped, failed).
#   - _emit_run_summary_oneshot() shells out to scripts/log_run.py with
#     whatever state is current. Idempotent via _RUN_STATE['emitted'].
#   - atexit.register catches normal exits + uncaught exceptions.
#   - signal.signal() converts SIGTERM/SIGHUP into a sys.exit(128+signum)
#     call so atexit handlers actually run (Python's default SIGTERM
#     handler is to exit immediately, BYPASSING atexit).
#   - SIGINT / KeyboardInterrupt: Python's default already raises an
#     exception that unwinds through atexit, no extra wiring needed.
#
# Each existing inline log_run.py call (Claude failure path, success path)
# sets _RUN_STATE['emitted'] = True after running so the atexit handler
# becomes a no-op for those branches and we don't double-write.
_RUN_STATE = {
    "emitted": False,
    "run_start": None,
    "posted": 0,
    "skipped": 0,
    "failed": 0,
    "cost": 0.0,
}


def _emit_run_summary_oneshot():
    if _RUN_STATE["emitted"] or _RUN_STATE["run_start"] is None:
        return
    _RUN_STATE["emitted"] = True
    elapsed = int(time.time() - _RUN_STATE["run_start"])
    try:
        subprocess.run(
            [
                PYTHON, os.path.join(os.path.dirname(os.path.abspath(__file__)), "log_run.py"),
                "--script", "post_github",
                "--posted", str(_RUN_STATE["posted"]),
                "--skipped", str(_RUN_STATE["skipped"]),
                "--failed", str(_RUN_STATE["failed"]),
                "--cost", f"{_RUN_STATE['cost']:.4f}",
                "--elapsed", str(elapsed),
            ],
            timeout=15,
            check=False,
        )
    except Exception:
        # Trap context: never raise from the safety net. Better to lose this
        # one summary line than to crash a shutdown sequence that might be
        # holding a browser lock or DB connection that other peers need.
        pass


def _signal_to_exit(signum, _frame):
    # Convert the signal into a normal-looking exit so atexit fires.
    sys.exit(128 + signum)


atexit.register(_emit_run_summary_oneshot)
# Only install handlers when running as the main entry point so importing
# post_github (e.g. for unit tests, or when SCRIPTS adds it to PYTHONPATH)
# doesn't override the parent process's signal handling.
if __name__ == "__main__" or os.environ.get("POST_GITHUB_INSTALL_TRAPS") == "1":
    for _sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(_sig, _signal_to_exit)
        except (ValueError, OSError):
            # Non-main-thread import or unsupported signal: skip silently.
            pass

from engagement_styles import (
    VALID_STYLES, get_styles_prompt, get_content_rules, get_anti_patterns,
    validate_or_register, pick_style_for_post,
)
# Audience-page routing: tells Claude which curated landing pages exist for the
# project so it can bake a deep URL (e.g. https://s4l.ai/ghostwriting) into the
# draft when the issue topic matches. See scripts/audience_pages.py + the
# landing_pages.audience_pages block in config.json.
from audience_pages import (
    prompt_block as _audience_prompt_block,
    classify_url_as_audience_page as _audience_classify_url,
)
# Learned preferences (2026-07-03): human review feedback distilled by
# feedback_digest.py into the project's learned_preferences config block.
# Rendered as an explicit prompt section; empty string when absent.
try:
    from learned_preferences import prompt_block as _learned_prefs_block
except Exception:  # never let a missing module break the poster
    def _learned_prefs_block(_project_cfg):
        return ""

REPO_DIR = os.path.expanduser("~/social-autoposter")
SCRIPTS = os.path.join(REPO_DIR, "scripts")
# THE canonical config loader (scripts/config.py): S4L_CONFIG_PATH / state-dir /
# S4L_REPO_DIR aware, mtime-cached. Replaces this file's hand-rolled loader and
# its hardcoded config path (the S4L-4H dead-path class on customer boxes).
import os as _cfg_os, sys as _cfg_sys
_cfg_sys.path.insert(0, _cfg_os.path.dirname(_cfg_os.path.abspath(__file__)))
from config import config_path as _canonical_config_path, load_config
CONFIG_PATH = _canonical_config_path()
SKILL_FILE = os.path.join(REPO_DIR, "SKILL.md")
GITHUB_TOOLS = os.path.join(SCRIPTS, "github_tools.py")
RUN_CLAUDE = os.path.join(SCRIPTS, "run_claude.sh")

# Repo-state lookup (stars, gone, has_issues) with github_tools' shared
# two-tier cache; feeds the tiny-repo audience floor in phase 1.
from github_tools import _fetch_repo_state as _repo_state

# Interpreter every child subprocess must run under. A bare PYTHON resolved
# to the user's system python, which lacks the pipeline deps that live only in
# the owned uv runtime — the same fresh-box failure class that broke the Twitter
# poster (Karol, 2026-06-22). The GitHub rail posts via the REST API (no browser,
# so no Playwright dep), but its util/DB children still need the owned venv, so
# pin the interpreter here too. Honor S4L_PYTHON (set by the launchd plist),
# else sys.executable; never the literal PYTHON.
PYTHON = os.environ.get("S4L_PYTHON") or sys.executable
os.environ["S4L_PYTHON"] = PYTHON

# Momentum tunables. Edit here, not at call sites.
DELTA_THRESHOLD = 1.0
HIGH_DELTA_BUMP = 3
CAP_DEFAULT = 1
CAP_BUMPED = 3
CLAUDE_CANDIDATE_LIMIT = 8     # show top N to Claude
SEARCH_PER_TOPIC = 5            # gh search --limit per topic
MAX_TOPICS_PER_PROJECT = 6

# Maintainer-just-spoke gate. authorAssociation values that count as "maintainer".
# If the most recent commenter on a candidate issue is one of these, we drop the
# candidate to avoid piling on a maintainer who just set direction (root cause of
# the antiwork/gumroad LOW_QUALITY minimization, posts #21826 + #22200).
MAINTAINER_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}

# Relevance gate. Claude returns relevance:0..3 per draft; we drop everything
# below this floor before posting. 2 = "project's tools/audience could plausibly
# help here." 0/1 = off-domain. Tunable.
MIN_RELEVANCE = 2

# Zero-audience gate (2026-07-17). Root cause of the vestlang #554 spam
# minimization (post #46421): the issue was the repo owner's self-authored work
# ticket with zero other commenters, so our comment's only audience was the
# owner, who hid it as spam within the hour. Require at least one non-bot
# commenter besides the issue author before we engage, and skip tiny-audience
# repos (fewer than STAR_FLOOR stars) unless a real multi-person discussion
# (2+ outside participants) is underway.
MIN_OUTSIDE_PARTICIPANTS = 1
STAR_FLOOR = 5

# Bot detection for `gh issue view` comment authors. GraphQL strips the
# "[bot]" suffix REST shows (github-actions[bot] arrives as plain
# "github-actions"), so we combine a known-login set with suffix heuristics.
# A missed bot only weakens the participant/momentum signal marginally.
KNOWN_BOT_LOGINS = {
    "github-actions", "dependabot", "dependabot-preview", "renovate",
    "renovate-bot", "codecov", "codecov-commenter", "vercel", "netlify",
    "stale", "snyk-bot", "greenkeeper", "coderabbitai", "copilot",
    "sonarcloud", "cypress", "changeset-bot", "allcontributors",
    "google-cla", "cla-assistant", "codesandbox-ci", "circleci",
}


def _is_bot_login(login):
    l = (login or "").lower()
    return bool(l) and (
        l in KNOWN_BOT_LOGINS or l.endswith("[bot]") or l.endswith("-bot")
        or l.endswith("bot]")
    )


def _our_github_handle(_memo={}):
    """Our own posting handle, lowercased ('' if unresolvable). Excluded from
    outside_participants so a thread we already commented in doesn't count us
    as its audience."""
    if "h" not in _memo:
        try:
            from account_resolver import resolve as _resolve
            _memo["h"] = (_resolve("github") or "").lower()
        except Exception:
            _memo["h"] = ""
    return _memo["h"]


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [post_github] {msg}", flush=True)




# ---------- Project picking & context ---------------------------------------

def get_top_performers(project_name, platform="github"):
    try:
        result = subprocess.run(
            [PYTHON, os.path.join(SCRIPTS, "top_performers.py"),
             "--platform", platform, "--project", project_name],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def get_top_search_topics(project_name, platform="github", limit=8, window_days=30):
    """Best-performing search_topic seeds for this project on this platform.
    Empty string if no data yet. Mirrors post_reddit.get_top_search_topics."""
    try:
        result = subprocess.run(
            [PYTHON, os.path.join(SCRIPTS, "top_search_topics.py"),
             "--project", project_name, "--platform", platform,
             "--window-days", str(window_days), "--limit", str(limit)],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def get_recent_comments(limit=5):
    """Last N github comments by id DESC. Tuple form `(id, our_content)` so
    the generation_trace audit row can store the IDs alongside the text (no
    duplication: the text is already in the posts table, the IDs let us
    reverse-link). Backward-compat note: this used to return a plain list
    of strings; callers that consume `recent_comments` for prompt-building
    were updated in the same change."""
    resp = api_get("/api/v1/posts", query={
        "platform": "github",
        "order_by": "id",
        "order_dir": "desc",
        "limit": int(limit),
    })
    rows = ((resp or {}).get("data") or {}).get("posts") or []
    # Return as list of (id, content) tuples. The caller-side conversion
    # to a flat string list for prompt-building is one-line below in main().
    return [(int(r["id"]), r.get("our_content") or "") for r in rows]


# Generation trace plumbing lives in scripts/generation_trace.py so the
# github / reddit / twitter pipelines all write the same shape. See that
# module for the shape contract and migrations/2026-05-12_generation_trace.sql
# for the JSONB column definition.
import generation_trace as _gen_trace


def _angle_str(v):
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, dict):
        return "; ".join(f"{k}: {_angle_str(x)}" for k, x in v.items() if x)
    if isinstance(v, (list, tuple)):
        return ", ".join(_angle_str(x) for x in v if x)
    return str(v) if v else ""


def build_content_angle(project, config):
    """Rich angle: prefer content_angle override, otherwise compose from
    description / differentiator / icp / setup / messaging / voice.

    Always appends the project's audience-pages block (when configured) so the
    draft prompt knows which curated landing pages it should link to for
    topic-matched issues.
    """
    if project.get("content_angle"):
        base = project["content_angle"]
    else:
        parts = []
        for key in ("description", "differentiator", "icp", "setup"):
            s = _angle_str(project.get(key))
            if s:
                parts.append(s)
        messaging = project.get("messaging", {}) or {}
        for key in ("lead_with_pain", "solution", "proof"):
            s = _angle_str(messaging.get(key))
            if s:
                parts.append(s)
        voice = project.get("voice", {}) or {}
        if voice.get("tone"):
            parts.append(f"Voice: {voice['tone']}")
        if voice.get("never"):
            parts.append("Never: " + "; ".join(voice["never"]))
        examples = voice.get("examples") or voice.get("examples_good") or []
        if examples:
            parts.append("Voice examples: " + " | ".join(examples[:3]))
        base = " ".join(parts) if parts else config.get("content_angle", "")

    try:
        ap_block = _audience_prompt_block(project.get("name") or "")
    except Exception:
        ap_block = ""
    if ap_block:
        return (base + "\n\n" + ap_block).strip() if base else ap_block.strip()
    return base


# ---------- Phase 1 / 2 momentum helpers ------------------------------------

def gh_search(query, limit=SEARCH_PER_TOPIC):
    try:
        out = subprocess.check_output(
            [PYTHON, GITHUB_TOOLS, "search", query, "--limit", str(limit)],
            text=True, timeout=45,
        )
        items = json.loads(out)
    except Exception as e:
        log(f"  gh_search failed for '{query}': {e}")
        return []
    return [i for i in items if not i.get("already_posted")]


def gh_view_counts(repo, number):
    """Return dict{comment_count, reaction_count, title, body, author, url,
    maintainer_last_speaker, last_commenter, last_comment_assoc,
    outside_participants, author_is_owner} or None if the issue is no longer
    open / unfetchable.

    `gh issue view --json comments` returns each comment with `authorAssociation`
    (OWNER/MEMBER/COLLABORATOR/CONTRIBUTOR/NONE/...) and `createdAt`. We use the
    most recent comment to detect "maintainer just spoke" so phase 1 can drop
    those candidates without an extra API call.

    Bot comments (github-actions, dependabot, etc.) are excluded from
    comment_count (so CI chatter can't inflate delta_score momentum), from the
    maintainer-last-speaker check (a bot posting after a maintainer shouldn't
    shadow the maintainer's word), and from outside_participants (the count of
    distinct non-bot commenters other than the issue author, which feeds the
    zero-audience gate)."""
    try:
        out = subprocess.check_output(
            ["gh", "issue", "view", str(number), "-R", repo,
             "--json", "title,body,author,url,comments,reactionGroups,state"],
            text=True, timeout=30, stderr=subprocess.STDOUT,
        )
        data = json.loads(out)
    except Exception:
        return None
    if data.get("state") and data["state"].lower() != "open":
        return None
    comments = data.get("comments") or []
    reaction_count = 0
    for g in data.get("reactionGroups") or []:
        reaction_count += int(
            (g.get("users") or {}).get("totalCount", 0) or g.get("totalCount", 0) or 0
        )

    issue_author = ((data.get("author") or {}).get("login", "") or "").lower()
    repo_owner = (repo.split("/", 1)[0] or "").lower()
    human_comments = [
        c for c in comments
        if not _is_bot_login(((c.get("author") or {}).get("login", "")))
    ]
    outside_participants = {
        ((c.get("author") or {}).get("login", "") or "").lower()
        for c in human_comments
    } - {issue_author, _our_github_handle(), ""}

    # Maintainer-just-spoke gate. Sort non-bot comments by createdAt desc, look
    # at the most recent one (regardless of timing). If the issue's last human
    # word came from someone with push access, the thread is being driven and we
    # shouldn't pile on. The OP's authorAssociation is checked separately
    # (issue.author isn't included in `comments`, only in the top-level `author`
    # field).
    maintainer_last_speaker = False
    last_commenter = ""
    last_comment_assoc = ""
    if human_comments:
        try:
            sorted_c = sorted(
                human_comments,
                key=lambda c: c.get("createdAt", "") or "",
                reverse=True,
            )
            last = sorted_c[0]
            last_commenter = (last.get("author") or {}).get("login", "") or ""
            last_comment_assoc = (last.get("authorAssociation") or "").upper()
            if last_comment_assoc in MAINTAINER_ASSOCIATIONS:
                maintainer_last_speaker = True
        except Exception:
            pass

    return {
        "comment_count": len(human_comments),
        "reaction_count": reaction_count,
        "title": data.get("title", ""),
        "body": (data.get("body") or ""),
        "author": (data.get("author") or {}).get("login", ""),
        "url": data.get("url", ""),
        "maintainer_last_speaker": maintainer_last_speaker,
        "last_commenter": last_commenter,
        "last_comment_assoc": last_comment_assoc,
        "outside_participants": len(outside_participants),
        "author_is_owner": bool(issue_author) and issue_author == repo_owner,
    }


def delta_score(c0, r0, c1, r1):
    return 3.0 * max(c1 - c0, 0) + 2.0 * max(r1 - r0, 0)


def parse_repo_number(url):
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/(?:issues|pull)/(\d+)", url or "")
    if not m:
        return None, None
    return f"{m.group(1)}/{m.group(2)}", int(m.group(3))


def parse_issue_url(url):
    if not url:
        return None, None, None
    m = re.search(r"github\.com/([^/]+)/([^/]+)/(?:issues|pull)/(\d+)", url)
    if not m:
        return None, None, None
    return m.group(1), m.group(2), int(m.group(3))


# ---------- Prompt -----------------------------------------------------------

def build_prompt(project, config, candidates, cap, top_report, recent_comments,
                 top_topics_report="", style_assignment=None):
    content_angle = build_content_angle(project, config)
    excluded_repos = config.get("exclusions", {}).get("github_repos", [])
    excluded_authors = config.get("exclusions", {}).get("authors", [])
    # Style enforcement: when style_assignment is provided the JSON example
    # pins the assigned style name into engagement_style so the model cannot
    # silently substitute a different label. INVENT mode (style=None) still
    # leaves engagement_style up to the model but it's expected to fill
    # new_style with the registration block. Without an assignment the
    # legacy menu wording is preserved for backward compatibility.
    _assigned_style_name = (style_assignment or {}).get("style")
    _assigned_mode = (style_assignment or {}).get("mode")
    if _assigned_style_name:
        # USE mode: pin literal name.
        _style_field_example = _assigned_style_name
    elif _assigned_mode == "invent":
        # INVENT mode: the model writes a new snake_case name and fills new_style.
        _style_field_example = "<your invented snake_case name>"
    else:
        # No assignment: legacy menu mode.
        _style_field_example = (
            f"<one of {', '.join(sorted(VALID_STYLES))}, or your invented snake_case name>"
        )

    cand_block = []
    for i, c in enumerate(candidates, 1):
        seed_line = f"seed: {c['search_topic']}\n" if c.get("search_topic") else ""
        last_speaker_line = ""
        if c.get("last_commenter"):
            last_speaker_line = (
                f"last_commenter: {c['last_commenter']} "
                f"({c.get('last_comment_assoc') or 'NONE'})\n"
            )
        audience_line = (
            f"outside_participants: {c.get('outside_participants', '?')}"
        )
        if c.get("author_is_owner"):
            audience_line += " | author_is_owner: true"
        audience_line += "\n"
        history_block = ""
        try:
            _hb = _render_author_history(
                "github", c.get("author") or "", days=30, limit=5
            )
            if _hb:
                history_block = _hb + "\n"
        except Exception:
            pass
        cand_block.append(
            f"--- #{i} {c['repo']}#{c['number']} delta={c['delta_score']:.1f} "
            f"(cm {c['comment_count_t0']}->{c['comment_count_t1']}, "
            f"rx {c['reaction_count_t0']}->{c['reaction_count_t1']}) ---\n"
            f"{seed_line}"
            f"{last_speaker_line}"
            f"{audience_line}"
            f"title: {c['title']}\n"
            f"author: {c['author']}\n"
            f"url: {c['url']}\n"
            f"body: {c['body']}\n"
            f"{history_block}"
        )
    candidates_text = "\n".join(cand_block)

    recent_ctx = ""
    if recent_comments:
        # recent_comments is now a list of (id, content) tuples (2026-05-12
        # change to support generation_trace audit). Accept both shapes
        # here so any caller still passing plain strings keeps working.
        def _extract(item):
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                return item[1]
            return item
        snippets = "\n".join(
            f"  - {_extract(c)}"
            for c in recent_comments
            if _extract(c)
        )
        if snippets:
            recent_ctx = f"""
Your last {len(recent_comments)} GitHub comments (don't repeat talking points):
{snippets}
"""

    top_ctx = ""
    if top_report:
        lines = top_report.split("\n")[:30]
        top_ctx = f"""
## Feedback from past performance:
{chr(10).join(lines)}
"""

    top_topics_ctx = ""
    if top_topics_report:
        top_topics_ctx = f"""
## Past top-performing search topics (sorted by clicks DESC first, then composite-scored: clicks*100 + comments*3 + upvotes)
CLICKS ARE THE PRIORITY SIGNAL. Any topic with `clicks > 0` is GOLD TIER, clicks
are the only metric that proves our reply drove someone to actually visit the
project's link. Comments and upvotes are vanity. If an issue's seed matches a
gold-tier topic, prefer that issue; mimic ITS framing (repo type, language,
issue keyword cluster) FIRST before falling back to other styles. Optimize the
entire pipeline for clicks; everything else is leading indicators.

{top_topics_report}

If none of the top topics match this run's candidates, prefer issues with
strong delta scores. New topics with 0 clicks are fine, we still need to
explore, but a gold-tier topic that fits should beat any unproven topic.
"""

    project_name = project["name"]
    min_relevance = MIN_RELEVANCE
    project_github = (project.get("github") or "").strip()
    github_repo_block = (
        f"\n\n## Our public repo for self-reply links\n{project_github}\n"
        f"When the self-reply policy below applies, the github blob URL MUST live "
        f"under this repo. Pick a real path you have reason to believe exists; if "
        f"you're unsure, default to the repo root or a top-level README rather "
        f"than inventing a deep path."
        if project_github else ""
    )
    return f"""You are the Social Autoposter drafting GitHub issue comments for project {project_name}.

Read {SKILL_FILE} for content rules (no em dashes, anti-AI tells, voice).

## Project context
{content_angle}{github_repo_block}

## Pre-filtered candidates (top {len(candidates)} by recent engagement delta)

Each candidate already cleared exclusion + already-posted filtering. The seed
shown is the search_topic that surfaced the issue, echo it back verbatim in
"search_topic" so we can score which seeds produce engagement.

{candidates_text}
{recent_ctx}{top_ctx}{top_topics_ctx}
{get_styles_prompt("github", context="posting", assignment=style_assignment)}

{_learned_prefs_block(project)}
## Targeting
- Best topics: Agents, Accessibility, Voice/ASR, Tool Use. Prioritize when present.
- Exclusions are already filtered, but for reference:
  - Excluded repos: {', '.join(excluded_repos) if excluded_repos else '(none)'}
  - Excluded authors: {', '.join(excluded_authors) if excluded_authors else '(none)'}

## Comment style (parent comment)
- Lead with the pain you hit, then your fix. "the token overhead is brutal" beats "here is how to optimize".
- Conversational, no markdown headings, no code blocks unless tiny.
- 400-600 chars. Short enough to read, long enough to show concrete observation, not generic advice.
- File names FROM THE MAINTAINER'S ISSUE OR REPO are great evidence you read it. File names from OUR OWN codebase do NOT belong in the parent comment, save them for the self-reply (see below) where they ride a real URL. Bare filenames from our repos with no URL ("server.rs, ChatToolExecutor.swift") are the spam shape that gets us moderated; never do that.
- NO links in the parent comment. The optional self-reply is where one link goes.

## Self-reply policy (optional follow-up with ONE github link)

Each post may carry an OPTIONAL `self_reply_text` that posts as a separate comment a minute or two after the parent. Its job is to point the maintainer at a specific, public file in one of OUR repos that demonstrates a concrete claim the parent comment made.

The self-reply ONLY fires when ALL THREE hold:
  1. Your parent comment makes a specific technical claim ("we ran into X and ended up doing Y") that a single file in our repo would back up.
  2. You can point to a REAL https://github.com/ blob URL with a plausible path. Use the project's public repo (see "Project context" above for the `github` field).
  3. The file is genuinely relevant to the maintainer's question, not a tangentially-related drop.

If ANY of the three is missing, set `self_reply_text: null`. Quiet bundles are healthy. A forced "here is some code" reply with a bare filename or an off-topic file is the exact pattern that gets us moderated; we'd rather skip the link than post a weak one.

Shape when present (100-220 chars, ONE URL, no markdown):
  "our X that handles Y: https://github.com/<our-org>/<our-repo>/blob/main/<path>"
  or just the natural framing + URL. No tagline, no signoff, no project pitch.

## Relevance scoring (REQUIRED, drop anything < {min_relevance})

For every candidate you draft, also score `relevance` 0..3 vs. the project above:
  - 3 = direct fit. The issue's problem is exactly what {project_name} solves.
  - 2 = relevant. The project's tools, audience, or problem-space could plausibly help.
  - 1 = tangential. Same abstractions, different problem (e.g. caching advice on a
        copy-variation issue). Don't post these.
  - 0 = unrelated. Don't post these.

Scoring < {min_relevance} must go to "skipped" with reason "low_relevance".
The pipeline drops these automatically; do not try to bypass.

## Anti-spam guardrails (skip a candidate if ANY apply)

Recent strikes were minimized as LOW_QUALITY because we drafted "expert"
takes that ignored what the maintainer just said. Skip when:
  - `last_commenter` is OWNER/MEMBER/COLLABORATOR (already pre-filtered, but
    re-confirm: if the maintainer's most recent message sets a clear direction,
    don't pile on with a counter-take).
  - The issue is about content/copy/ux/business decisions and you'd have to
    pivot to architecture/perf/caching to have something to say.
  - You'd have to manufacture experience ("I ran this in production at scale...",
    "I've seen this play out dozens of times...") to fill the 400-char budget.
  - Other recent commenters are obviously pitching their own tool. You'll be
    grouped with them by the maintainer.
  - You'd cite a precedent you can't actually link to (Apple ?ppid, Stripe X,
    Shopify Y, etc.). Hand-wavy precedent name-drops read as fake-expert.
  - The point you'd make is already stated in the issue body or an existing
    comment. Re-deriving the author's own caveats in your voice reads as an
    LLM paraphrasing them back at themselves; that exact pattern got our
    vestlang comment hidden as spam within the hour. If you can't add
    something the thread doesn't already contain, skip.
  - `author_is_owner: true` and the issue reads like the owner's internal
    work ticket (committed scope, acceptance criteria, "design decision to
    settle", self-addressed spec). The owner's notification inbox is the
    only audience, and they didn't ask for outside input.
  - `outside_participants` is 1 and your comment doesn't offer that one
    other person something genuinely new. Thin threads reach you already
    filtered (zero-participant threads are dropped upstream), but 1 is
    still thin; treat it as a high bar, not a green light.

## YOUR JOB

Pick UP TO {cap} candidates worth commenting on and draft one comment for each.

ZERO POSTS IS A VALID, FREQUENTLY CORRECT OUTCOME. Returning `"posts": []` and
listing the candidates in `"skipped"` is preferred over forcing a comment on a
mediocre fit. The pipeline runs every cycle; quiet cycles are healthy.

## Content rules
{get_content_rules("github")}

{get_anti_patterns()}

## OUTPUT FORMAT

Return ONLY a single JSON object. No prose, no markdown fencing, no Bash calls:

{{
  "posts": [
    {{
      "repo": "<owner/repo>",
      "number": <issue number>,
      "thread_url": "<issue url>",
      "thread_title": "<issue title>",
      "thread_author": "<issue author>",
      "matched_project": "{project_name}",
      "engagement_style": "{_style_field_example}",
      "new_style": null,
      "search_topic": "<the seed from the candidate block, copied verbatim>",
      "language": "<ISO 639-1 code matching the issue language: en, ja, zh, es, ...>",
      "relevance": <int 0..3, see scoring rules above; must be >= {min_relevance} to post>,
      "relevance_rationale": "<one short sentence: why this score>",
      "comment_text": "<the actual comment to post, 400-600 chars, NO links>",
      "self_reply_text": <null OR a 100-220 char follow-up containing exactly ONE https://github.com/... blob URL into one of OUR public repos. See "Self-reply policy" above. Default to null unless all three conditions hold.>
    }}
  ],
  "skipped": [
    {{ "url": "<issue url>", "reason": "<short reason; use 'low_relevance' when relevance < {min_relevance}>" }}
  ]
}}

If, and ONLY if, none of the listed styles fits, you may invent one. Set
"engagement_style" to your snake_case name AND replace `"new_style": null` with
`{{"description": "...", "example": "...", "note": "...", "why_existing_didnt_fit": "..."}}`.
Inventing should be rare; prefer an existing style if it's even 80% right.

CRITICAL: Do NOT call gh, Bash, or any tool. The orchestrator already searched
and viewed; just return the JSON.
"""


# ---------- Claude one-shot (no tools needed since pre-filter is in Python) -

def run_claude(prompt, timeout=900):
    """One-shot non-streaming Claude via run_claude.sh wrapper. Returns
    (ok, raw_stdout, usage_dict)."""
    usage = {"cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0,
             "cache_read": 0, "cache_create": 0}
    cmd = [RUN_CLAUDE, "post_github",
           "--strict-mcp-config",
           "--mcp-config", os.path.expanduser("~/.claude/browser-agent-configs/no-agents-mcp.json"),
           "-p", "--output-format", "json", prompt]
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    try:
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT", usage
    try:
        outer = json.loads(proc.stdout)
        usage["cost_usd"] = float(outer.get("total_cost_usd", 0.0) or 0.0)
        u = outer.get("usage", {}) or {}
        usage["input_tokens"] = int(u.get("input_tokens", 0) or 0)
        usage["output_tokens"] = int(u.get("output_tokens", 0) or 0)
        usage["cache_read"] = int(u.get("cache_read_input_tokens", 0) or 0)
        usage["cache_create"] = int(u.get("cache_creation_input_tokens", 0) or 0)
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return proc.returncode == 0, proc.stdout, usage


def parse_claude_json(output):
    """Extract the inner JSON object from --output-format json envelope."""
    try:
        outer = json.loads(output)
        result = outer.get("result", "") if isinstance(outer, dict) else str(outer)
    except Exception:
        result = output
    start = result.find("{")
    if start < 0:
        return None
    depth, in_str, esc, end = 0, False, False, -1
    for i in range(start, len(result)):
        ch = result[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end < 0:
        return None
    try:
        return json.loads(result[start:end + 1])
    except Exception:
        return None


# ---------- Posting + logging ------------------------------------------------

def post_comment(owner, repo, number, body):
    # Identity gate (mirrors twitter_browser.reply_to_tweet /
    # reddit_browser.post_comment): the gh CLI would happily post as whatever
    # account it is authed as, but with no resolved accounts.github.username
    # the row's attribution is blank and self-filtering breaks. Refuse loudly.
    from account_resolver import resolve as _resolve_gh
    if not _resolve_gh("github"):
        return False, ("no_account_configured (set accounts.github.username in "
                       "config.json or AUTOPOSTER_GITHUB_USERNAME)")
    try:
        out = subprocess.check_output(
            ["gh", "issue", "comment", str(number), "-R", f"{owner}/{repo}", "--body", body],
            text=True, timeout=60, stderr=subprocess.STDOUT,
        )
        url = None
        for line in out.strip().splitlines():
            if line.startswith("https://github.com"):
                url = line.strip()
                break
        return True, url
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        err = e.output if hasattr(e, "output") and e.output else str(e)
        return False, str(err)[:300]


def log_post(thread_url, our_url, text, project_name, thread_author, thread_title,
             github_username, engagement_style=None, search_topic=None, language=None,
             claude_session_id=None, generation_trace_path=None, link_source=None):
    """Defers to github_tools.py log-post, which handles dedup + INSERT.

    Returns the new posts.id on success, or None on failure / dedup hit.
    Callers who need attribution wiring (e.g. post_links backfill) check
    the return for truthy before calling backfill_post_id.

    generation_trace_path (added 2026-05-12): optional path to a JSON
    file with the few-shot context Claude saw. Passed to github_tools.py
    as --generation-trace and stored in posts.generation_trace JSONB.
    File-based instead of inline-JSON to keep argv short (the report
    text can be several KB) and to avoid shell-escape pain.

    link_source (added 2026-05-17): tags audience-page traffic (e.g.
    'audience_page:founder-ghostwriting') so the dashboard can break out
    curated landing-page hits from generic homepage links.
    """
    try:
        cmd = [PYTHON, GITHUB_TOOLS, "log-post",
               thread_url, our_url or "", text, project_name,
               thread_author or "unknown", thread_title or "",
               "--account", github_username]
        if engagement_style:
            cmd.extend(["--engagement-style", engagement_style])
        if search_topic:
            cmd.extend(["--search-topic", search_topic])
        if language:
            cmd.extend(["--language", language])
        if claude_session_id:
            cmd.extend(["--claude-session-id", claude_session_id])
        if generation_trace_path:
            cmd.extend(["--generation-trace", generation_trace_path])
        if link_source:
            cmd.extend(["--link-source", link_source])
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.stdout.strip():
            try:
                parsed = json.loads(result.stdout.strip())
                if parsed.get("error"):
                    log(f"log-post error: {parsed}")
                    return None
                # Success envelope from github_tools.py log-post should match
                # log_post.py's shape: {"logged": true, "post_id": N, ...}.
                pid = parsed.get("post_id")
                return pid if isinstance(pid, int) else None
            except json.JSONDecodeError:
                pass
    except Exception as e:
        log(f"WARNING: log-post failed: {e}")
    return None


# ---------- Main -------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="GitHub Issues posting orchestrator (momentum-gated)")
    parser.add_argument("--sleep", type=int, default=600,
                        help="Phase 1 -> Phase 2 momentum window in seconds (default 600)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Hard ceiling on posts per run; caps the adaptive cap")
    parser.add_argument("--timeout", type=int, default=900,
                        help="Claude drafting timeout in seconds")
    parser.add_argument("--project", default=None, help="Override project selection")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print prompt + would-post candidates; do not invoke Claude or post")
    args = parser.parse_args()

    run_start = time.time()
    # Arm the atexit/SIGTERM safety net: it skips emit until run_start is
    # set, so any pre-main exit (argparse, etc.) is a no-op.
    _RUN_STATE["run_start"] = run_start
    log(f"=== GitHub run: sleep={args.sleep}s ===")

    config = load_config()
    from account_resolver import resolve as _resolve_account
    github_username = _resolve_account("github") or ""

    # ---- Pick project ------------------------------------------------------
    if args.project:
        project = next(
            (p for p in config.get("projects", [])
             if p.get("name", "").lower() == args.project.lower()),
            None,
        )
        if not project:
            log(f"ERROR: project '{args.project}' not found")
            sys.exit(1)
        project_name = project.get("name")
        log(f"Project (forced): {project_name}")
    else:
        # Shared inverse-recent-share picker (scripts/pick_project.py), the
        # same selection logic twitter and reddit use.
        picks = pick_project.pick_projects(config, platform="github", n=1)
        project = picks[0] if picks else None
        if project is None:
            log("ERROR: no eligible project (none have search_topics)")
            sys.exit(1)
        project_name = project.get("name")
        log(f"Project (inverse-recent-share): {project_name} "
            f"(weight={project.get('weight', 0)})")

    # ---- Phase 1: search topics, T0 snapshot -------------------------------
    topics_pool = list(topics_for_project(project["name"]))
    if not topics_pool:
        log("Project has no topics to search. Exiting.")
        sys.exit(0)
    # Shuffle before slicing so each run samples a different MAX_TOPICS_PER_PROJECT
    # subset. Without this, projects with >6 seeds always query the first 6, which
    # starves diverse coverage and biases top_search_topics scoring (c0nsl run on
    # 2026-04-24 yielded only 2 candidates because its first 6 seeds were narrow).
    random.shuffle(topics_pool)
    topics = topics_pool[:MAX_TOPICS_PER_PROJECT]

    log(f"Phase 1: searching {len(topics)} topic queries...")
    raw = []
    seen_urls = set()
    for topic in topics:
        for item in gh_search(topic):
            url = item.get("url")
            if url and url not in seen_urls:
                seen_urls.add(url)
                # Stamp the originating seed so it survives dedup -> INSERT and
                # feeds top_search_topics scoring on the next run.
                item["search_topic"] = topic
                raw.append(item)
    log(f"Phase 1: {len(raw)} unique issues after dedup + already-posted filter")
    if not raw:
        log("No candidates. Exiting.")
        sys.exit(0)

    candidates = []
    skipped_maintainer = 0
    skipped_no_audience = 0
    skipped_small_repo = 0
    for item in raw[:CLAUDE_CANDIDATE_LIMIT * 3]:
        repo, number = parse_repo_number(item.get("url"))
        if not repo:
            continue
        counts = gh_view_counts(repo, number)
        if not counts:
            continue
        # Maintainer-just-spoke gate. If the most recent comment is from someone
        # with push access (OWNER/MEMBER/COLLABORATOR), they are driving the
        # thread, so piling on reads as noise and risks LOW_QUALITY hide.
        if counts.get("maintainer_last_speaker"):
            skipped_maintainer += 1
            log(
                f"  skip {repo}#{number}: maintainer-just-spoke "
                f"(last={counts.get('last_commenter')}/"
                f"{counts.get('last_comment_assoc')})"
            )
            continue
        # Zero-audience gate. Nobody but the issue author (and bots) has ever
        # touched this thread, so a comment's only reader is the author's
        # notification inbox. Worst case is the author being the repo owner
        # writing their own work ticket (vestlang #554, post #46421): the one
        # person notified is the one person who can hide us as spam.
        if counts.get("outside_participants", 0) < MIN_OUTSIDE_PARTICIPANTS:
            skipped_no_audience += 1
            log(
                f"  skip {repo}#{number}: no outside participants "
                f"(author={counts.get('author')}, "
                f"owner_authored={counts.get('author_is_owner')})"
            )
            continue
        # Tiny-repo audience floor. A repo with almost no stars has no
        # bystander audience reading its issues; require a real multi-person
        # discussion before engaging there. stars=None (fetch failed) passes:
        # fail open, the participant gate above already ran.
        _owner, _name = repo.split("/", 1)
        _stars = _repo_state(_owner, _name).get("stars")
        if (_stars is not None and _stars < STAR_FLOOR
                and counts.get("outside_participants", 0) < 2):
            skipped_small_repo += 1
            log(
                f"  skip {repo}#{number}: {_stars} stars < {STAR_FLOOR} "
                f"and thin discussion "
                f"({counts.get('outside_participants')} outside participants)"
            )
            continue
        candidates.append({
            "repo": repo,
            "number": number,
            "url": counts["url"],
            "title": counts["title"],
            "body": counts["body"],
            "author": counts["author"],
            "comment_count_t0": counts["comment_count"],
            "reaction_count_t0": counts["reaction_count"],
            "outside_participants": counts.get("outside_participants", 0),
            "author_is_owner": bool(counts.get("author_is_owner")),
            "search_topic": item.get("search_topic"),
        })
    log(
        f"Phase 1: {len(candidates)} candidates with T0 snapshot "
        f"(skipped {skipped_maintainer} maintainer-just-spoke, "
        f"{skipped_no_audience} no-outside-participants, "
        f"{skipped_small_repo} tiny-repo)"
    )
    if not candidates:
        log("No live open issues to re-poll. Exiting.")
        sys.exit(0)

    # ---- Sleep -------------------------------------------------------------
    log(f"Sleeping {args.sleep}s before T1...")
    time.sleep(args.sleep)

    # ---- Phase 2a: re-poll T1 ---------------------------------------------
    log("Phase 2a: re-polling T1 counts...")
    survivors = []
    skipped_maintainer_phase2 = 0
    for c in candidates:
        counts = gh_view_counts(c["repo"], c["number"])
        if not counts:
            c["comment_count_t1"] = c["comment_count_t0"]
            c["reaction_count_t1"] = c["reaction_count_t0"]
            c["delta_score"] = 0.0
            survivors.append(c)
            continue
        # Re-check maintainer-just-spoke gate. A maintainer may have arrived
        # during the sleep window. If so, drop to avoid piling on.
        if counts.get("maintainer_last_speaker"):
            skipped_maintainer_phase2 += 1
            log(
                f"  phase2 skip {c['repo']}#{c['number']}: maintainer arrived "
                f"during sleep (last={counts.get('last_commenter')}/"
                f"{counts.get('last_comment_assoc')})"
            )
            continue
        c["comment_count_t1"] = counts["comment_count"]
        c["reaction_count_t1"] = counts["reaction_count"]
        # Participants only grow, so no re-skip needed; refresh so the prompt
        # shows the T1 value.
        c["outside_participants"] = counts.get(
            "outside_participants", c.get("outside_participants", 0))
        c["delta_score"] = delta_score(
            c["comment_count_t0"], c["reaction_count_t0"],
            c["comment_count_t1"], c["reaction_count_t1"],
        )
        survivors.append(c)
    if skipped_maintainer_phase2:
        log(
            f"Phase 2a: dropped {skipped_maintainer_phase2} candidates "
            f"after maintainer comment during sleep"
        )
    candidates = survivors
    if not candidates:
        log("Phase 2a: no candidates left after maintainer recheck. Exiting.")
        sys.exit(0)

    # ---- Phase 2b: adaptive cap -------------------------------------------
    high_delta = [c for c in candidates if c["delta_score"] >= DELTA_THRESHOLD]
    cap = CAP_BUMPED if len(high_delta) >= HIGH_DELTA_BUMP else CAP_DEFAULT
    if args.limit is not None:
        cap = min(cap, max(0, args.limit))
    log(f"Phase 2b: {len(high_delta)} high-momentum candidates -> cap = {cap}")

    candidates.sort(key=lambda c: c["delta_score"], reverse=True)
    top = candidates[:CLAUDE_CANDIDATE_LIMIT]
    log(f"Phase 2b: showing Claude top {len(top)} by delta, cap = {cap}")

    if cap <= 0:
        log("cap=0, nothing to post. Exiting.")
        sys.exit(0)

    top_report = get_top_performers(project_name)
    recent_comments = get_recent_comments()
    top_topics_report = get_top_search_topics(project_name, platform="github")

    # 2026-05-22: pick the engagement style for this draft batch ONCE so
    # validate_or_register can enforce the picker's choice on every post in
    # the batch (USE mode coerces drift back; INVENT mode lets the model
    # register a new style). GitHub batches share one assignment per cycle;
    # cycles run frequently enough that the picker's distribution averages
    # out across batches. Per-candidate assignment would require N picker
    # calls + injected per-candidate blocks; deferred until the data shows
    # it matters.
    style_assignment = pick_style_for_post("github", context="posting")
    log(f"Style assignment for this batch: mode={style_assignment.get('mode')} "
        f"style={style_assignment.get('style') or '(invent)'}")

    prompt = build_prompt(project, config, top, cap, top_report,
                          recent_comments, top_topics_report=top_topics_report,
                          style_assignment=style_assignment)

    # Build the generation_trace audit blob: what Claude is about to see.
    # Captured BEFORE the Claude call so we never end up with a post row
    # missing its trace (e.g. if Claude errors out, we never call
    # log_post and the file is GC'd by the OS). Same trace path is used
    # for every post produced from this Claude invocation, since they
    # all saw the same few-shot context.
    #
    # Why a temp file: argv has a ~256 KB cap on macOS and the top_report
    # alone can run several KB. The path travels through 3 hops
    # (post_github → log_post() → github_tools.py log-post) and stays
    # cheap to pass; the JSON body only deserializes once at the SQL
    # INSERT step. tempfile.NamedTemporaryFile(delete=False) so the file
    # survives the with-block close and the child process can read it.
    generation_trace_path = None
    try:
        trace = _gen_trace.build_trace(
            platform="github",
            project_name=project_name,
            prompt_chars=len(prompt),
            top_performers_text=top_report or "",
            top_search_topics_text=top_topics_report or "",
            # recent_comments here is the list of (id, content) tuples
            # from get_recent_comments(); extract just the IDs.
            recent_comment_ids=[pid for pid, _ in (recent_comments or [])],
            model=None,
            min_score_floor=5,  # PLATFORM_MIN_SCORE['github']
        )
        generation_trace_path = _gen_trace.write_trace_tempfile(
            trace, prefix="github_gen_trace_",
        )
        if generation_trace_path:
            log(f"Generation trace: {generation_trace_path} "
                f"({os.path.getsize(generation_trace_path)} bytes)")
    except Exception as e:
        # Audit row is nice-to-have, never a blocker. Log and continue.
        log(f"WARNING: generation_trace build failed ({e}); proceeding without trace")

    if args.dry_run:
        log("=== DRY RUN ===")
        log(f"Prompt length: {len(prompt)} chars")
        if generation_trace_path:
            log(f"Trace would be saved with each post: {generation_trace_path}")
        for c in top[:cap]:
            log(f"  would consider {c['repo']}#{c['number']} "
                f"delta={c['delta_score']:.1f} title={c['title'][:60]}")
        return

    # ---- Phase 2b: invoke Claude (one-shot, no tools) ----------------------
    claude_session_id = str(uuid.uuid4())
    os.environ["CLAUDE_SESSION_ID"] = claude_session_id
    log("Phase 2b: invoking Claude for drafting...")
    claude_start = time.time()
    ok, output, usage = run_claude(prompt, timeout=args.timeout)
    log(f"Claude finished in {time.time() - claude_start:.0f}s (${usage['cost_usd']:.4f})")
    # Mirror cost into the safety-net state so a SIGTERM after this point
    # records the spend even if we never reach the post loop.
    _RUN_STATE["cost"] = usage["cost_usd"]

    if not ok:
        log(f"Claude FAILED: {output[:300]}")
        _RUN_STATE["failed"] = 1
        subprocess.run([
            PYTHON, os.path.join(SCRIPTS, "log_run.py"),
            "--script", "post_github",
            "--posted", "0", "--skipped", "0", "--failed", "1",
            "--cost", f"{usage['cost_usd']:.4f}",
            "--elapsed", f"{int(time.time() - run_start)}",
        ])
        # Mark emitted so the atexit handler doesn't double-write the
        # tailored Claude-failure summary above.
        _RUN_STATE["emitted"] = True
        sys.exit(1)

    decisions = parse_claude_json(output) or {}
    posts = decisions.get("posts", []) or []
    skipped = decisions.get("skipped", []) or []
    log(f"Claude picked {len(posts)}, skipped {len(skipped)}")

    # Relevance gate. Anything Claude scored below MIN_RELEVANCE goes to the
    # skipped bucket, NOT posted, regardless of how confident the comment_text
    # reads. This is the programmatic backstop for the prompt rule.
    relevance_dropped = []
    kept_posts = []
    for d in posts:
        try:
            rel = int(d.get("relevance", 0))
        except (TypeError, ValueError):
            rel = 0
        if rel < MIN_RELEVANCE:
            relevance_dropped.append({
                "url": d.get("thread_url", ""),
                "reason": (
                    f"low_relevance (relevance={rel}, "
                    f"rationale={(d.get('relevance_rationale') or '').strip()[:120]})"
                ),
            })
        else:
            kept_posts.append(d)
    if relevance_dropped:
        log(
            f"Relevance gate dropped {len(relevance_dropped)}/{len(posts)} "
            f"draft(s) below MIN_RELEVANCE={MIN_RELEVANCE}"
        )
        for r in relevance_dropped:
            log(f"  drop {r['url']}: {r['reason']}")
    skipped.extend(relevance_dropped)
    posts = kept_posts

    if not posts:
        log("No valid post decisions. Last 500 chars of output:")
        log(output.strip()[-500:])

    posted = 0
    failed = 0
    for i, decision in enumerate(posts):
        thread_url = decision.get("thread_url", "")
        text = (decision.get("comment_text") or "").strip()
        thread_author = decision.get("thread_author", "unknown")
        thread_title = decision.get("thread_title", "")
        # validate_or_register enforces the picker's batch-level assignment:
        # in USE mode any drifted engagement_style label is silently coerced
        # back to the assigned name; in INVENT mode the new_style block is
        # registered into engagement_styles_registry via the s4l API. All
        # posts in this batch share style_assignment by design (see picker
        # call above).
        engagement_style, _style_action = validate_or_register(
            decision,
            source_post={
                "platform": "github",
                "post_url": thread_url,
                "post_id": None,
                "model": decision.get("model"),
            },
            assigned_style=(style_assignment or {}).get("style"),
            assigned_mode=(style_assignment or {}).get("mode"),
        )
        language = (decision.get("language") or "en").strip().lower()[:5] or "en"

        owner, repo, number = parse_issue_url(thread_url)
        if not owner or not text:
            log(f"SKIP: bad URL or empty text: {thread_url}")
            failed += 1
            _RUN_STATE["failed"] = failed
            continue

        # URL-wrap before sending to GitHub. project for wrapping is the
        # decision-resolved match (e.g., the project whose repo the issue
        # belongs to) or the orchestrator's own project_name. log_post
        # uses the same fallback chain so attribution lines up.
        wrap_project = (decision.get("matched_project") or project_name or "").strip()
        minted_session = None
        # Audience-page detection (2026-05-17). Inspect the unwrapped text for
        # any URL that exactly matches a curated audience-page (e.g.
        # https://s4l.ai/ghostwriting). When found, posts.link_source is
        # stamped 'audience_page:<angle>' for the row. Detection runs BEFORE
        # wrap_text_for_post because wrapping rewrites the URLs to /r/<code>
        # short links; classify_url_as_audience_page() needs the original
        # target URL.
        audience_page_link_source = None
        if wrap_project:
            try:
                for _url_m in re.finditer(r'https?://[^\s)\]>"\']+', text):
                    _raw = _url_m.group(0).rstrip('.,);!?]')
                    _angle = _audience_classify_url(_raw, wrap_project)
                    if _angle:
                        audience_page_link_source = f"audience_page:{_angle}"
                        break
            except Exception as _e:
                log(f"WARNING: audience-page classify raised ({_e})")
        if wrap_project:
            try:
                from dm_short_links import wrap_text_for_post, utm_only_text
                wrap_res = wrap_text_for_post(text=text, platform="github_issues",
                                                project_name=wrap_project)
                if wrap_res.get("ok"):
                    text = wrap_res["text"]
                    minted_session = wrap_res.get("minted_session")
                    if wrap_res.get("codes"):
                        log(f"wrapped {len(wrap_res['codes'])} URL(s): {wrap_res['codes']}")
                else:
                    log(f"WARNING: URL wrap failed ({wrap_res.get('error')}); falling back to UTM-only")
                    text = utm_only_text(text=text, platform="github_issues", project_name=wrap_project)
            except Exception as e:
                log(f"WARNING: URL wrap raised ({e}); falling back to UTM-only")
                try:
                    from dm_short_links import utm_only_text
                    text = utm_only_text(text=text, platform="github_issues", project_name=wrap_project)
                except Exception as ee:
                    log(f"WARNING: UTM-only fallback also failed ({ee}); posting unwrapped")

        log(f"Posting {i + 1}/{len(posts)} -> {owner}/{repo}#{number}: {thread_title[:60]}")
        ok_post, url_or_err = post_comment(owner, repo, number, text)
        if not ok_post:
            log(f"POST FAILED: {url_or_err}")
            failed += 1
            _RUN_STATE["failed"] = failed
            time.sleep(3)
            continue

        new_post_id = log_post(
            thread_url, url_or_err, text,
            decision.get("matched_project") or project_name,
            thread_author, thread_title, github_username,
            engagement_style=engagement_style,
            search_topic=(decision.get("search_topic") or "").strip() or None,
            language=language,
            claude_session_id=claude_session_id,
            # Same trace blob for every post in this run — they all saw
            # the same few-shot context. If the trace file couldn't be
            # built earlier this is None and log_post drops the flag.
            generation_trace_path=generation_trace_path,
            link_source=audience_page_link_source,
        )
        # Stamp post_links.post_id for the URLs minted before posting.
        # Idempotent; no-op when minted_session is None or the dedup path
        # in github_tools.py log-post returned no post_id (e.g., dup thread).
        if minted_session and new_post_id:
            try:
                from dm_short_links import backfill_post_id
                backfill_post_id(minted_session=minted_session, post_id=new_post_id)
            except Exception as e:
                log(f"WARNING: backfill_post_id failed ({e})")
        posted += 1
        # Keep the safety-net counters in sync after each successful post so
        # a SIGTERM mid-loop still emits the partial-but-correct count.
        _RUN_STATE["posted"] = posted
        _RUN_STATE["failed"] = failed
        _RUN_STATE["skipped"] = len(skipped)
        log(f"POSTED: {url_or_err or 'ok'}")

        # ---- Optional self-reply with ONE github blob URL ------------------
        # Restored 2026-05-17 after the April 13 over-correction stripped
        # github of all CTAs (zero link_edit_content rows in May). The bundle
        # is the proven pattern for driving clicks; the model gets explicit
        # license to skip it (self_reply_text=null) when it can't point to a
        # genuinely relevant file in our repos. See the "Self-reply policy"
        # section of build_prompt() for the three-condition skip rule.
        sr_raw = (decision.get("self_reply_text") or "")
        sr_text = sr_raw.strip() if isinstance(sr_raw, str) else ""
        if sr_text and re.search(r"https?://github\.com/", sr_text):
            # 30-90s jitter between parent and child. The March 18 strike pair
            # (3072/3073) posted 0.2s apart, which is the bot-signature
            # timing. Humans don't follow up that fast. Random within the
            # window so we don't accumulate a uniform-timing fingerprint.
            sr_delay = random.randint(30, 90)
            log(f"Self-reply queued after {sr_delay}s delay...")
            time.sleep(sr_delay)

            # URL-wrap the self-reply via dm_short_links so the github blob
            # link gets /r/<code> or UTM-tagged attribution, same as every
            # other URL in the pipeline. CLAUDE.md: "Never post bare URLs."
            sr_minted_session = None
            if wrap_project:
                try:
                    from dm_short_links import wrap_text_for_post, utm_only_text
                    sr_wrap = wrap_text_for_post(text=sr_text, platform="github_issues",
                                                 project_name=wrap_project)
                    if sr_wrap.get("ok"):
                        sr_text = sr_wrap["text"]
                        sr_minted_session = sr_wrap.get("minted_session")
                        if sr_wrap.get("codes"):
                            log(f"self-reply wrapped {len(sr_wrap['codes'])} URL(s): "
                                f"{sr_wrap['codes']}")
                    else:
                        log(f"WARNING: self-reply URL wrap failed "
                            f"({sr_wrap.get('error')}); falling back to UTM-only")
                        sr_text = utm_only_text(text=sr_text, platform="github_issues",
                                                project_name=wrap_project)
                except Exception as e:
                    log(f"WARNING: self-reply URL wrap raised ({e}); "
                        f"falling back to UTM-only")
                    try:
                        from dm_short_links import utm_only_text
                        sr_text = utm_only_text(text=sr_text, platform="github_issues",
                                                project_name=wrap_project)
                    except Exception as ee:
                        log(f"WARNING: self-reply UTM-only fallback also failed ({ee}); "
                            f"posting unwrapped")

            ok_sr, sr_url_or_err = post_comment(owner, repo, number, sr_text)
            if ok_sr:
                sr_post_id = log_post(
                    thread_url, sr_url_or_err, sr_text,
                    decision.get("matched_project") or project_name,
                    thread_author, thread_title, github_username,
                    engagement_style=engagement_style,
                    search_topic=(decision.get("search_topic") or "").strip() or None,
                    language=language,
                    claude_session_id=claude_session_id,
                    generation_trace_path=generation_trace_path,
                    link_source=audience_page_link_source,
                )
                if sr_minted_session and sr_post_id:
                    try:
                        from dm_short_links import backfill_post_id
                        backfill_post_id(minted_session=sr_minted_session,
                                         post_id=sr_post_id)
                    except Exception as e:
                        log(f"WARNING: self-reply backfill_post_id failed ({e})")
                posted += 1
                _RUN_STATE["posted"] = posted
                log(f"SELF-REPLY POSTED: {sr_url_or_err or 'ok'}")
            else:
                log(f"SELF-REPLY FAILED: {sr_url_or_err}")
                failed += 1
                _RUN_STATE["failed"] = failed

        time.sleep(3)

    # Clean up the generation_trace temp file. By this point every post
    # that landed has its trace persisted to posts.generation_trace JSONB,
    # so the on-disk JSON is redundant. macOS would eventually purge
    # /var/folders/, but explicit cleanup keeps temp dirs tidy when this
    # runs every 20 min via launchd.
    _gen_trace.cleanup_trace_tempfile(generation_trace_path)

    total_elapsed = time.time() - run_start
    log(f"=== SUMMARY: elapsed={total_elapsed:.0f}s posted={posted} failed={failed} ===")
    log(f"Tokens: input={usage['input_tokens']} output={usage['output_tokens']} "
        f"cache_read={usage['cache_read']} cache_create={usage['cache_create']}")
    log(f"Cost: ${usage['cost_usd']:.4f}")

    # Final happy-path summary write. Sync the safety-net state in case the
    # last post-loop iteration didn't (e.g. zero candidates kept), then mark
    # emitted so the atexit handler short-circuits.
    _RUN_STATE["posted"] = posted
    _RUN_STATE["failed"] = failed
    _RUN_STATE["skipped"] = len(skipped)
    _RUN_STATE["cost"] = usage["cost_usd"]
    subprocess.run([
        PYTHON, os.path.join(SCRIPTS, "log_run.py"),
        "--script", "post_github",
        "--posted", str(posted),
        "--skipped", str(len(skipped)),
        "--failed", str(failed),
        "--cost", f"{usage['cost_usd']:.4f}",
        "--elapsed", f"{int(total_elapsed)}",
    ])
    _RUN_STATE["emitted"] = True


if __name__ == "__main__":
    main()
