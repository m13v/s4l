#!/usr/bin/env python3
"""GitHub CLI tools for Claude to call via Bash.

Commands:
    python3 scripts/github_tools.py search "QUERY" [--limit 10]
    python3 scripts/github_tools.py view OWNER/REPO NUMBER
    python3 scripts/github_tools.py already-posted "THREAD_URL"
    python3 scripts/github_tools.py log-post THREAD_URL OUR_URL OUR_TEXT PROJECT THREAD_AUTHOR THREAD_TITLE [--account m13v] [--engagement-style STYLE]
"""

import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get, api_post
from version import read_version as read_autoposter_version
try:
    from account_resolver import resolve as _resolve_account
except Exception:
    def _resolve_account(_platform):  # type: ignore[unused-arg]
        return None


def _github_account_filter():
    """Return (sql_fragment, params) for a github our_account scope.

    Empty tuple of params means no scoping is applied (legacy behavior).
    Used so the same query shape works with and without a configured handle.
    """
    h = _resolve_account("github")
    if h:
        return (" AND our_account = %s", [h])
    return ("", [])

# THE canonical config loader (scripts/config.py): S4L_CONFIG_PATH / state-dir /
# S4L_REPO_DIR aware, mtime-cached. Replaces this file's hand-rolled loader and
# its hardcoded config path (the S4L-4H dead-path class on customer boxes).
import os as _cfg_os, sys as _cfg_sys
_cfg_sys.path.insert(0, _cfg_os.path.dirname(_cfg_os.path.abspath(__file__)))
from config import config_path as _canonical_config_path, load_config as _load_config
CONFIG_PATH = _canonical_config_path()




def _excluded_repos_and_authors(config):
    exclusions = config.get("exclusions", {})
    repos = {r.lower() for r in exclusions.get("github_repos", [])}
    authors = {a.lower() for a in exclusions.get("authors", [])}
    return repos, authors


# Auto-blocklist: any owner where >= DYNAMIC_BLOCK_THRESHOLD of our github
# posts under that owner have been moderated (status='deleted' OR
# deletion_detect_count > 0) within the last DYNAMIC_BLOCK_WINDOW_DAYS days.
# One strike = stop posting under that owner. The cost of one extra burned
# comment is much higher than the cost of skipping a borderline-friendly
# repo. Tuned 2026-05-01 after the antiwork/gumroad block: deletion of #4677
# alone should have stopped us before #4915. Tightened 2->1 on 2026-06-04
# after rausermack22-dotcom content-farm repo burned 2 mk0r comments before
# the owner hit the threshold.
DYNAMIC_BLOCK_THRESHOLD = 1
DYNAMIC_BLOCK_WINDOW_DAYS = 90


_REPO_GONE_CACHE_PATH = os.path.expanduser(
    "~/social-autoposter/skill/cache/github_repo_state.json"
)
_REPO_GONE_TTL_SEC = 24 * 3600  # 24h is plenty: a deleted repo stays deleted


def _load_repo_gone_cache():
    try:
        with open(_REPO_GONE_CACHE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_repo_gone_cache(cache):
    try:
        os.makedirs(os.path.dirname(_REPO_GONE_CACHE_PATH), exist_ok=True)
        with open(_REPO_GONE_CACHE_PATH, "w") as f:
            json.dump(cache, f)
    except Exception:
        pass


def _fetch_repo_state(owner, repo, _mem={}, _disk={"loaded": False, "data": {}}):
    """Fetch and cache (gone, has_issues, has_discussions) for owner/repo.
    Two-tier cache (in-process + 24h on-disk JSON). Returns a dict
    {gone: bool, has_issues: bool, has_discussions: bool}."""
    key = f"{owner}/{repo}".lower()
    if key in _mem:
        return _mem[key]
    if not _disk["loaded"]:
        _disk["data"] = _load_repo_gone_cache()
        _disk["loaded"] = True
    entry = _disk["data"].get(key)
    now = int(time.time())
    if (entry and (now - int(entry.get("checked_at", 0))) < _REPO_GONE_TTL_SEC
            and "has_issues" in entry):
        state = {
            "gone": bool(entry.get("gone")),
            "has_issues": bool(entry.get("has_issues", True)),
            "has_discussions": bool(entry.get("has_discussions", True)),
        }
        _mem[key] = state
        return state
    try:
        proc = subprocess.run(
            ["gh", "api", f"repos/{owner}/{repo}"],
            capture_output=True, text=True, timeout=20,
        )
    except Exception:
        state = {"gone": False, "has_issues": True, "has_discussions": True}
        _mem[key] = state
        return state
    if proc.returncode == 0:
        try:
            data = json.loads(proc.stdout or "{}")
        except Exception:
            data = {}
        state = {
            "gone": False,
            "has_issues": bool(data.get("has_issues", True)),
            "has_discussions": bool(data.get("has_discussions", True)),
        }
    else:
        err = ((proc.stderr or "") + (proc.stdout or "")).lower()
        gone = ("not found" in err or "http 404" in err)
        state = {"gone": gone, "has_issues": True, "has_discussions": True}
    _mem[key] = state
    _disk["data"][key] = {
        "gone": state["gone"],
        "has_issues": state["has_issues"],
        "has_discussions": state["has_discussions"],
        "checked_at": now,
    }
    _save_repo_gone_cache(_disk["data"])
    return state


def _repo_is_gone(owner, repo):
    """Back-compat alias. Returns True iff the parent repo 404s. Callers
    that want the broader 'this URL is unreachable for non-moderation
    reasons' should use _post_is_collateral(thread_url) instead."""
    return _fetch_repo_state(owner, repo)["gone"]


def _post_is_collateral(thread_url):
    """Returns True iff this thread_url died for a non-moderation reason:
    the whole repo 404'd, OR the repo is alive but the feature this URL
    lived on (issues, discussions) has been disabled by the owner. Both
    cases mean every comment under that URL vanished at once and ours is
    not a targeted strike."""
    if not thread_url:
        return False
    from urllib.parse import urlparse as _urlparse
    parts = _urlparse(thread_url).path.strip("/").split("/")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return False
    owner, repo = parts[0], parts[1]
    state = _fetch_repo_state(owner, repo)
    if state["gone"]:
        return True
    if len(parts) >= 3:
        if parts[2] == "issues" and not state["has_issues"]:
            return True
        if parts[2] == "discussions" and not state["has_discussions"]:
            return True
    return False


def _dynamic_owner_blocklist(threshold=DYNAMIC_BLOCK_THRESHOLD,
                              days=DYNAMIC_BLOCK_WINDOW_DAYS):
    """Return lowercased owner names with >=threshold moderated posts in the
    last `days` days. Posts whose entire parent repo is 404 OR whose host
    feature (Issues/Discussions) has been turned off on the repo are excluded
    from the count: owner restructured the project, not a hostility signal.
    Caller unions with static config exclusions before filtering candidates."""
    # Dynamic owner blocklist is scoped per-account so the @matt_diak
    # autoposter doesn't inherit @m13v_'s strike history (or vice versa).
    # Falls back to unscoped when no handle is configured. The moderation
    # filter (status='deleted' OR deletion_detect_count>0, inside the window)
    # is applied server-side via moderated_within_days; the owner-counting and
    # collateral exclusion stay local.
    query = {"platform": "github", "moderated_within_days": str(int(days))}
    handle = _resolve_account("github")
    if handle:
        query["our_account"] = handle
    try:
        resp = api_get("/api/v1/posts/thread-urls", query=query)
        rows = ((resp or {}).get("data") or {}).get("thread_urls") or []
    except Exception:
        return set()
    from collections import Counter
    from urllib.parse import urlparse
    counts = Counter()
    for url in rows:
        if not url:
            continue
        parts = urlparse(url).path.strip("/").split("/")
        if len(parts) < 2 or not parts[0] or not parts[1]:
            continue
        if _post_is_collateral(url):
            # Repo gone or feature disabled: drop from strike count.
            continue
        counts[parts[0].lower()] += 1
    blocked = {owner for owner, n in counts.items() if n >= threshold}
    if blocked:
        print(
            f"[github_blocklist] threshold={threshold} window_days={days} "
            f"blocked={sorted(blocked)}",
            file=sys.stderr,
        )
    return blocked


def _is_excluded_repo(repo_full, excluded_repos):
    """repo_full is 'owner/name'. Match if either owner or name or full is in excluded list."""
    if not repo_full:
        return False
    rl = repo_full.lower()
    owner = rl.split("/", 1)[0] if "/" in rl else rl
    name = rl.split("/", 1)[1] if "/" in rl else rl
    return rl in excluded_repos or owner in excluded_repos or name in excluded_repos


def cmd_search(args):
    """Search GitHub for issues via gh CLI. Filters out excluded repos/authors and already-posted threads."""
    try:
        out = subprocess.check_output(
            ["gh", "search", "issues", args.query,
             "--limit", str(args.limit),
             "--state", "open",
             "--sort", "updated",
             "--json", "number,title,repository,author,state,updatedAt,url,body"],
            text=True, timeout=30, stderr=subprocess.STDOUT,
        )
        items = json.loads(out)
    except subprocess.CalledProcessError as e:
        print(json.dumps({"error": "gh_search_failed", "message": (e.output or str(e))[:300]}))
        sys.exit(2)
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        print(json.dumps({"error": "gh_search_failed", "message": str(e)[:300]}))
        sys.exit(2)

    config = _load_config()
    excluded_repos, excluded_authors = _excluded_repos_and_authors(config)

    excluded_repos = excluded_repos | _dynamic_owner_blocklist()
    # Per-account dedupe: only filter against threads THIS handle posted in.
    _tu_query = {"platform": "github"}
    _handle = _resolve_account("github")
    if _handle:
        _tu_query["our_account"] = _handle
    _tu_resp = api_get("/api/v1/posts/thread-urls", query=_tu_query)
    already_posted = set(((_tu_resp or {}).get("data") or {}).get("thread_urls") or [])

    results = []
    for item in items:
        repo = item.get("repository", {}) or {}
        repo_full = repo.get("nameWithOwner") or (
            f"{repo.get('owner', {}).get('login', '')}/{repo.get('name', '')}"
            if repo.get("owner") else ""
        )
        author = (item.get("author") or {}).get("login", "")

        if _is_excluded_repo(repo_full, excluded_repos):
            continue
        if author.lower() in excluded_authors:
            continue

        url = item.get("url", "")
        already = url in already_posted
        entry = {
            "url": url,
            "title": item.get("title", ""),
            "author": author,
            "repo": repo_full,
            "number": item.get("number"),
            "updated_at": item.get("updatedAt", ""),
            "body_preview": (item.get("body") or ""),
            "already_posted": already,
        }
        if already:
            entry["SKIP"] = ">>> ALREADY POSTED IN THIS THREAD - DO NOT POST AGAIN <<<"
        results.append(entry)

    print(json.dumps(results, indent=2))


def cmd_view(args):
    """Fetch issue body and comments via gh CLI. Returns compact JSON."""
    # args.repo is 'owner/repo', args.number is the issue number
    try:
        out = subprocess.check_output(
            ["gh", "issue", "view", str(args.number), "-R", args.repo,
             "--json", "title,body,author,state,comments,url"],
            text=True, timeout=30, stderr=subprocess.STDOUT,
        )
        thread = json.loads(out)
    except subprocess.CalledProcessError as e:
        print(json.dumps({"error": "gh_view_failed", "message": (e.output or str(e))[:300]}))
        return
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        print(json.dumps({"error": "gh_view_failed", "message": str(e)[:300]}))
        return

    comments = []
    for c in (thread.get("comments") or []):
        comments.append({
            "author": (c.get("author") or {}).get("login", ""),
            "body": (c.get("body") or ""),
        })

    compact = {
        "url": thread.get("url", ""),
        "title": thread.get("title", ""),
        "state": thread.get("state", ""),
        "author": (thread.get("author") or {}).get("login", ""),
        "body": (thread.get("body") or ""),
        "comments": comments,
    }

    text = json.dumps(compact, indent=2)
    print(text)


def cmd_already_posted(args):
    """Check if we already posted in a GitHub issue thread.

    Scoped per-account so multi-machine setups don't false-positive on
    each other's posts. Falls back to unscoped when no handle is configured.
    """
    query = {"platform": "github", "thread_url": args.url}
    handle = _resolve_account("github")
    if handle:
        query["our_account"] = handle
    resp = api_get("/api/v1/posts/lookup", query=query)
    row = ((resp or {}).get("data") or {}).get("post")
    if row:
        print(json.dumps({"already_posted": True, "post_id": row.get("id"),
                          "content_preview": row.get("our_content")}))
    else:
        print(json.dumps({"already_posted": False}))


def cmd_log_post(args):
    """Log a posted GitHub comment to the database.

    Enforces two dedup rules:
      1. Same comment URL is never logged twice (our_url hard dedup).
      2. Only one post per GitHub issue thread (thread_url hard dedup).
    """
    # our_url stays globally unique (it's a permalink to a specific comment,
    # and two accounts can't physically produce the same one). thread_url
    # dedup is scoped per-account so two handles can each comment once in
    # the same upstream issue thread.
    handle = _resolve_account("github")
    if args.our_url:
        resp = api_get("/api/v1/posts/lookup",
                       query={"platform": "github", "our_url": args.our_url})
        existing = ((resp or {}).get("data") or {}).get("post")
        if existing:
            print(json.dumps({"error": "DUPLICATE_URL", "message": "Already logged this comment URL", "existing_post_id": existing.get("id")}))
            return

    _tq = {"platform": "github", "thread_url": args.thread_url}
    if handle:
        _tq["our_account"] = handle
    resp = api_get("/api/v1/posts/lookup", query=_tq)
    existing = ((resp or {}).get("data") or {}).get("post")
    if existing:
        print(json.dumps({
            "error": "DUPLICATE_THREAD",
            "message": "Already posted in this thread",
            "existing_post_id": existing.get("id"),
            "content_preview": existing.get("our_content"),
        }))
        return

    # claude_session_id may come either via --claude-session-id or via the
    # CLAUDE_SESSION_ID env var (set by run_claude.sh). CLI arg wins.
    session_id = (getattr(args, "claude_session_id", None)
                  or os.environ.get("CLAUDE_SESSION_ID")
                  or None)
    # Generation trace: opaque JSON blob captured by the generator before
    # invoking Claude. Loaded from a file path (--generation-trace) because
    # the JSON can be several KB and passing it inline via argv blows past
    # macOS ARG_MAX. Passed to the API as a parsed object (the POST route
    # serializes + caps at 1 MB). Failure to read just nulls the column —
    # never blocks the post, since losing the audit row for one post is
    # preferable to losing the post.
    generation_trace_obj = None
    trace_path = getattr(args, "generation_trace", None)
    if trace_path:
        try:
            with open(trace_path, "r", encoding="utf-8") as tf:
                generation_trace_obj = json.load(tf)
        except (OSError, json.JSONDecodeError) as e:
            # Stderr only — stdout is reserved for the JSON envelope
            # that post_github.py:log_post() parses.
            print(f"WARNING: could not load generation_trace {trace_path}: {e}",
                  file=sys.stderr)

    payload = {
        "platform": "github",
        "thread_url": args.thread_url,
        "thread_author": args.thread_author,
        "thread_title": args.thread_title,
        "thread_content": "",
        "our_url": args.our_url,
        "our_content": args.our_text,
        "our_account": args.account,
        "source_summary": "",
        "project": args.project,
        "engagement_style": getattr(args, "engagement_style", None),
        "search_topic": getattr(args, "search_topic", None),
        "language": (getattr(args, "language", None) or "en"),
        "claude_session_id": session_id,
        "link_source": getattr(args, "link_source", None),
        "autoposter_version": read_autoposter_version(),
    }
    if generation_trace_obj is not None:
        payload["generation_trace"] = generation_trace_obj
    resp = api_post("/api/v1/posts", payload, ok_on_conflict=True)
    if not (resp or {}).get("ok"):
        # Backstop: the POST route dedups (platform, thread_url) globally and
        # 409s. Our per-account pre-check above already caught the common case;
        # a 409 here is a cross-account thread collision. Surface DUPLICATE_THREAD.
        e = (resp or {}).get("error") or {}
        print(json.dumps({
            "error": "DUPLICATE_THREAD",
            "message": e.get("message") or "already posted in this thread",
            "existing_post_id": (resp or {}).get("existing_post_id") or e.get("existing_post_id"),
        }))
        return
    new_id = (((resp or {}).get("data") or {}).get("post") or {}).get("id")
    # post_id surfaced so post_github.py:log_post can backfill post_links
    # for click attribution. Shape mirrors log_post.py's INSERT envelope.
    print(json.dumps({"logged": True, "post_id": new_id}))


def main():
    parser = argparse.ArgumentParser(description="GitHub tools for Claude")
    sub = parser.add_subparsers(dest="command")

    # search
    p_search = sub.add_parser("search", help="Search GitHub issues")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=10)

    # view
    p_view = sub.add_parser("view", help="Fetch issue body + comments")
    p_view.add_argument("repo", help="owner/repo")
    p_view.add_argument("number", help="Issue number")

    # already-posted
    p_ap = sub.add_parser("already-posted", help="Check if we posted in this thread")
    p_ap.add_argument("url")

    # log-post
    p_log = sub.add_parser("log-post", help="Log a posted comment to DB")
    p_log.add_argument("thread_url")
    p_log.add_argument("our_url")
    p_log.add_argument("our_text")
    p_log.add_argument("project")
    p_log.add_argument("thread_author")
    p_log.add_argument("thread_title")
    p_log.add_argument("--account", default="m13v")
    p_log.add_argument("--engagement-style", dest="engagement_style", default=None)
    p_log.add_argument("--search-topic", dest="search_topic", default=None,
                       help="The seed topic/query used to find this issue (feedback loop input)")
    p_log.add_argument("--language", dest="language", default=None,
                       help="ISO 639-1 language code of the issue (defaults to en if omitted)")
    p_log.add_argument("--claude-session-id", dest="claude_session_id", default=None,
                       help="UUID of the Claude session that drafted this post (falls back to CLAUDE_SESSION_ID env var)")
    p_log.add_argument("--generation-trace", dest="generation_trace", default=None,
                       help="Path to a JSON file with the few-shot context Claude "
                            "saw before drafting (top_performers report, recent "
                            "comments, top_search_topics, model, prompt size). "
                            "Stored in posts.generation_trace JSONB for audit. "
                            "See migrations/2026-05-12_generation_trace.sql for "
                            "the shape contract.")
    p_log.add_argument("--link-source", dest="link_source", default=None,
                       help="Optional tag for posts.link_source so the dashboard "
                            "can break out audience-page traffic (e.g. "
                            "'audience_page:founder-ghostwriting') from generic "
                            "homepage links.")

    args = parser.parse_args()
    if args.command == "search":
        cmd_search(args)
    elif args.command == "view":
        cmd_view(args)
    elif args.command == "already-posted":
        cmd_already_posted(args)
    elif args.command == "log-post":
        cmd_log_post(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
