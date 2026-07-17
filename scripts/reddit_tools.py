#!/usr/bin/env python3
"""Reddit CLI tools for Claude to call via Bash.

Commands:
    python3 scripts/reddit_tools.py search "security cameras" [--limit 10] [--sort relevance] [--time week]
    python3 scripts/reddit_tools.py search "automation" --subreddits AI_Agents,SaaS,smallbusiness --time month
    python3 scripts/reddit_tools.py fetch <thread_url>
    python3 scripts/reddit_tools.py log-post <thread_url> <our_permalink> <our_text> <project> <thread_author> <thread_title>
    python3 scripts/reddit_tools.py already-posted <thread_url>
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from http_api import api_get, api_post
from version import read_version as read_autoposter_version
try:
    from account_resolver import resolve as _resolve_account
except Exception:
    def _resolve_account(_platform):  # type: ignore[unused-arg]
        return None

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"

# Persistent rate limit file to share state across invocations
RATELIMIT_FILE = "/tmp/reddit_ratelimit.json"


def _read_ratelimit():
    try:
        with open(RATELIMIT_FILE) as f:
            return json.load(f)
    except Exception:
        return {"remaining": 100, "reset_at": 0}


def _write_ratelimit(remaining, reset_seconds):
    reset_at = time.time() + reset_seconds
    with open(RATELIMIT_FILE, "w") as f:
        json.dump({"remaining": remaining, "reset_at": reset_at}, f)


class RateLimitedError(Exception):
    """Raised when Reddit API returns 429. Contains reset seconds."""
    def __init__(self, reset_seconds):
        self.reset_seconds = reset_seconds
        super().__init__(f"rate_limited_wait_{int(reset_seconds)}s")


# Maximum time a single tool invocation is allowed to wait for rate limit to clear.
# Longer waits are returned as errors so Claude can skip and try something else.
# 90s stays under Claude's default 120s bash timeout while absorbing the common
# short-reset case (resets are usually 10-60s after a single burst).
MAX_INLINE_WAIT_SECONDS = 90


def _wait_if_needed():
    rl = _read_ratelimit()
    if rl["remaining"] <= 2 and rl["reset_at"] > time.time():
        wait = int(rl["reset_at"] - time.time()) + 2
        if wait > MAX_INLINE_WAIT_SECONDS:
            raise RateLimitedError(wait)
        print(f"Rate limit near zero, waiting {wait}s...", file=sys.stderr)
        time.sleep(wait)


def _fetch_via_browser(url):
    """Fetch a Reddit URL through the reddit-harness logged-in Chrome.

    Returns the raw response body (str) on HTTP 200, else None so the caller
    falls back to urllib. This is the 2026-05-29 transport swap: Reddit began
    403ing urllib/curl on *.json from residential IPs on 2026-05-28, but a
    same-origin fetch() from inside the logged-in harness browser returns 200.

    Gated by REDDIT_FETCH_BACKEND: default ("harness") uses the browser first;
    set REDDIT_FETCH_BACKEND=urllib to force the legacy path (e.g. for debugging).
    Also short-circuits to None when REDDIT_CDP_URL is unset AND no harness is
    expected, so plain `urllib`-only environments are unaffected.
    """
    if os.environ.get("REDDIT_FETCH_BACKEND", "harness").lower() == "urllib":
        return None
    try:
        from reddit_browser_fetch import browser_get_json
    except Exception as e:
        sys.stderr.write(f"[reddit_tools] browser fetch unavailable ({e}); urllib fallback\n")
        return None
    try:
        body, status = browser_get_json(url)
        if status == 200 and body:
            return body
        sys.stderr.write(f"[reddit_tools] browser fetch status={status} for {url[:80]}; urllib fallback\n")
    except Exception as e:
        sys.stderr.write(f"[reddit_tools] browser fetch error ({e}); urllib fallback\n")
    return None


def _do_request(url):
    """Make a Reddit API request with rate limit handling.

    Primary transport is the reddit-harness browser (see _fetch_via_browser);
    urllib is the silent fallback. On 429 (urllib path): raises RateLimitedError
    immediately if the reset would require a long wait, else absorbs short waits.
    """
    _wait_if_needed()
    # Browser-first (bypasses Reddit's urllib 403 wall). Falls through to urllib
    # if the harness is down or returns a non-200.
    _body = _fetch_via_browser(url)
    if _body is not None:
        try:
            return json.loads(_body)
        except Exception:
            sys.stderr.write(f"[reddit_tools] browser body not JSON for {url[:80]}; urllib fallback\n")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        resp = urllib.request.urlopen(req, timeout=20)
        remaining = float(resp.headers.get("X-Ratelimit-Remaining", 100))
        reset = float(resp.headers.get("X-Ratelimit-Reset", 0))
        _write_ratelimit(remaining, reset)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            reset = float(e.headers.get("X-Ratelimit-Reset", 60))
            _write_ratelimit(0, reset)
            if reset > MAX_INLINE_WAIT_SECONDS:
                raise RateLimitedError(reset)
            print(f"Rate limited. Waiting {int(reset)+2}s...", file=sys.stderr)
            time.sleep(int(reset) + 2)
            # Retry once
            resp = urllib.request.urlopen(req, timeout=20)
            remaining = float(resp.headers.get("X-Ratelimit-Remaining", 100))
            reset2 = float(resp.headers.get("X-Ratelimit-Reset", 0))
            _write_ratelimit(remaining, reset2)
            return json.loads(resp.read())
        raise


def batch_fetch_info(thing_ids, user_agent=USER_AGENT):
    """Fetch metadata for up to 100 Reddit thing IDs in a single API call.

    Args:
        thing_ids: list of full thing IDs like ["t3_abc123", "t3_def456", "t1_xyz"]
        user_agent: User-Agent header

    Returns:
        dict mapping thing_id -> post/comment data dict
    """
    results = {}
    # Process in chunks of 100 (Reddit's max per request)
    for i in range(0, len(thing_ids), 100):
        chunk = thing_ids[i:i + 100]
        ids_str = ",".join(chunk)
        url = f"https://old.reddit.com/api/info.json?id={ids_str}"
        _wait_if_needed()
        # Browser-first transport (Reddit 403s urllib on *.json). urllib fallback.
        _body = _fetch_via_browser(url)
        if _body is not None:
            try:
                data = json.loads(_body)
                for child in data.get("data", {}).get("children", []):
                    cd = child.get("data", {})
                    name = cd.get("name")
                    if name:
                        results[name] = cd
                continue
            except Exception:
                sys.stderr.write("[reddit_tools] browser info.json not JSON; urllib fallback\n")
        req = urllib.request.Request(url, headers={"User-Agent": user_agent})
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            remaining = float(resp.headers.get("X-Ratelimit-Remaining", 100))
            reset = float(resp.headers.get("X-Ratelimit-Reset", 0))
            _write_ratelimit(remaining, reset)
            data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                reset = float(e.headers.get("X-Ratelimit-Reset", 60))
                _write_ratelimit(0, reset)
                if reset > MAX_INLINE_WAIT_SECONDS:
                    raise RateLimitedError(reset)
                print(f"Rate limited. Waiting {int(reset)+2}s...", file=sys.stderr)
                time.sleep(int(reset) + 2)
                resp = urllib.request.urlopen(req, timeout=30)
                remaining = float(resp.headers.get("X-Ratelimit-Remaining", 100))
                reset2 = float(resp.headers.get("X-Ratelimit-Reset", 0))
                _write_ratelimit(remaining, reset2)
                data = json.loads(resp.read())
            else:
                raise

        for child in data.get("data", {}).get("children", []):
            d = child.get("data", {})
            name = d.get("name", "")
            results[name] = d

    return results


def _ban_entry_to_slug(entry):
    """Extract the sub slug from a comment_blocked / thread_blocked entry.

    Entries are either bare strings (pre-2026-05-11 shape) or audit dicts
    {"sub": "foo", "added_at": ..., "reason": ..., "project": ...}.
    Returns lowercased slug or None.
    """
    if isinstance(entry, str):
        s = entry.strip().lower()
        return s or None
    if isinstance(entry, dict):
        s = (entry.get("sub") or "").strip().lower()
        return s or None
    return None


def _load_comment_blocked_subs(project_name=None):
    """Load subreddits where we cannot post comments.

    Reads subreddit_bans.comment_blocked plus exclusions.subreddits. Used by
    search/fetch so the comment-drafting agent never sees these subs as
    candidates in the first place.

    subreddit_bans.thread_blocked is NOT read here — a sub can block new
    thread creation while still allowing comments, so it must not leak into
    the comment pipeline.

    Per-project layer (added 2026-05-11): when project_name is provided, also
    pulls active `subreddit:<slug>` excludes from project_search_excludes
    (platform='reddit'). These are LLM-proposed and have cleared the 2-batch
    activation gate. Failures here MUST NOT break search: if project_excludes
    import / DB call fails for any reason, we fall back to the global list
    alone so the pipeline degrades gracefully.

    Scope model (2026-05-19 cleanup):
      - subreddit_bans.comment_blocked entries are ALWAYS account-level.
        The ONLY scope dimension is the entry's `account` field. An entry
        tagged with a specific account applies only on machines posting
        as that account; entries with account=null apply globally (back-
        compat with pre-2026-05-15 data). The legacy `project` field on
        these entries is IGNORED — the gate is account-level by nature
        (sub automod strips the comment form for the account, not the
        project). The originating project is preserved on the entry as
        `noticed_by_project` for audit only.
      - Project-specific relevance rejects (e.g. "studyly thinks
        r/medicalschool is off-topic") live in project_search_excludes
        (the per-project layer above), NOT in comment_blocked.

    Handles both ban-list shapes: bare-string entries (pre-2026-05-11) and
    {"sub": ..., "added_at": ..., "reason": ..., "account": ...} audit dicts.
    """
    try:
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.json")
        with open(config_path) as f:
            config = json.load(f)
        # Per-account scoping (2026-05-15): a ban applies only to the account
        # that triggered it. Different machines may post the same project as
        # different accounts (laptop=Deep_Ad1959, sandbox VM=StreetRefuse7512);
        # without this filter, account A's real ban would suppress a sub for
        # account B that has no such ban. Entries with account=null are
        # treated as global (apply regardless), preserving pre-2026-05-15 data.
        current_account = (config.get("reddit_account") or {}).get("username") or None
        blocked = set()
        bans = config.get("subreddit_bans") or {}
        if isinstance(bans, dict):
            for entry in bans.get("comment_blocked") or []:
                slug = _ban_entry_to_slug(entry)
                if not slug:
                    continue
                entry_account = None
                if isinstance(entry, dict):
                    entry_account = entry.get("account") or None
                # Account filter is the ONLY scope dimension (2026-05-19
                # cleanup). If entry is tagged with a specific account and
                # it's not the current one, this ban doesn't apply on this
                # machine — different accounts have different automod
                # fingerprints. Entries with account=null are global
                # (apply on every account; pre-2026-05-15 back-compat).
                #
                # The legacy `project` field is intentionally ignored:
                # comment_blocked is an ACCOUNT-LEVEL gate by definition.
                # If a sub silently strips this account's comment form,
                # every project running this account hits the same gate.
                # Project-specific relevance rejects live in
                # project_search_excludes, not here. The writer now stores
                # the originating project as `noticed_by_project` for
                # audit only.
                if (entry_account is not None and current_account is not None
                        and entry_account.lower() != current_account.lower()):
                    continue
                blocked.add(slug)
        blocked.update(s.lower() for s in config.get("exclusions", {}).get("subreddits", []))

        # Per-project self-improving sub denylist (2026-05-11). Reads
        # project_search_excludes where platform='reddit' and term starts
        # with 'subreddit:'. Only active terms (passed the 2-batch gate) are
        # returned by active_excludes_by_kind, so a one-off false reject can't
        # mute a sub.
        if project_name:
            try:
                import project_excludes as _pe
                split = _pe.active_excludes_by_kind('reddit', project_name)
                for sub in (split.get('subreddit') or []):
                    if sub:
                        blocked.add(sub.lower())
            except Exception as e:
                print(f"[reddit_search] WARN: project_excludes load failed: {e}",
                      file=sys.stderr, flush=True)
        return blocked
    except Exception:
        return set()


def _load_config_subreddits():
    """Load the subreddit list from config.json for scoped searches."""
    try:
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.json")
        with open(config_path) as f:
            config = json.load(f)
        return config.get("subreddits", [])
    except Exception:
        return []


def _build_search_url(query, sort, limit, time_filter, subreddits=None):
    """Build Reddit search URL with optional subreddit scoping."""
    quality_suffix = " self:yes nsfw:no"
    full_query = query + quality_suffix
    encoded = urllib.parse.quote(full_query)
    params = f"q={encoded}&sort={sort}&t={time_filter}&limit={limit}&type=link&raw_json=1"
    if subreddits:
        multi_sub = "+".join(subreddits)
        return f"https://www.reddit.com/r/{multi_sub}/search.json?{params}&restrict_sr=on"
    return f"https://www.reddit.com/search.json?{params}"


def _parse_search_results(data, already_posted, blocked_subs):
    """Parse Reddit search JSON into thread list.

    Returns (threads, stats) where stats counts the per-reason drops so the
    caller (cmd_search) can emit a `[reddit_search]` marker to stderr that the
    dashboard's reddit-run enricher parses to surface raw/passed/dropped pills
    (mirroring linkedin_search_attempts.candidates_dropped_below_floor and
    twitter_search_attempts.tweets_found, see bin/server.js enrichers).
    """
    threads = []
    stats = {"raw": 0, "blocked_sub": 0, "archived": 0, "locked": 0, "too_old": 0,
             "already_posted_flagged": 0}
    top_score = 0
    top_comments = 0
    for child in data.get("data", {}).get("children", []):
        post = child.get("data", {})
        stats["raw"] += 1
        subreddit = post.get("subreddit", "").lower()
        if subreddit in blocked_subs:
            stats["blocked_sub"] += 1
            continue
        created = post.get("created_utc", 0)
        age_hours = (datetime.now(timezone.utc).timestamp() - created) / 3600 if created else 999
        permalink = f"https://old.reddit.com{post.get('permalink', '')}"
        already = permalink in already_posted
        entry = {
            "subreddit": f"r/{post.get('subreddit', '')}",
            "url": permalink,
            "title": post.get("title", ""),
            "author": post.get("author", ""),
            "score": post.get("score", 0),
            "num_comments": post.get("num_comments", 0),
            "age_hours": round(age_hours, 1),
            "selftext": post.get("selftext", ""),
            "already_posted": already,
        }
        if already:
            # HARD FILTER (added 2026-05-08): drop already-posted threads at parse
            # time. Previously this only attached a SKIP marker to the entry and
            # let it flow through to the LLM gate, which cost ~$0.20/thread to
            # confirm "yep, already posted" — observed 6+/cycle on studyly. The
            # set of permalinks comes from `posts.thread_url` (every comment
            # we've ever landed), so an entry here means we definitely already
            # engaged this thread; posting again = obvious astroturfing.
            stats["already_posted_flagged"] += 1
            continue
        if post.get("archived"):
            stats["archived"] += 1
            continue
        if age_hours > 4320:
            stats["too_old"] += 1
            continue
        if post.get("locked"):
            stats["locked"] += 1
            continue
        if entry["score"] > top_score:
            top_score = entry["score"]
        if entry["num_comments"] > top_comments:
            top_comments = entry["num_comments"]
        threads.append(entry)
    stats["returned"] = len(threads)
    stats["top_score"] = top_score
    stats["top_comments"] = top_comments
    return threads, stats


def _log_search_and_attach_deltas(query, subreddits_csv, project_name, batch_id, threads, stats):
    """Dual-write feedback loop side effect of cmd_search.

    1. Inserts ONE reddit_search_attempts row capturing (query, subreddits,
       project, raw count, post-filter count, top metrics) so
       top_dud_reddit_queries.py can later surface phrases that consistently
       return zero candidates.
    2. UPSERTs one reddit_thread_snapshots row per returned thread keyed by
       thread_url. On second sight, computes delta_score / delta_comments /
       delta_window_min from first_seen_* and mutates the threads list in
       place, attaching those fields to each thread dict so the LLM sees:
           "+15 upvotes / +4 comments since first seen 32min ago"
       This is the entire delta-gating loop — no separate T1 fetch job.

    Failures here MUST NOT break the search command. The whole point is to be
    a passive side effect; dropping a snapshot row is preferable to failing the
    whole call and starving the post pipeline.
    """
    try:
        # 1) Server computes deltas + persists thread snapshots in one call,
        #    then returns the threads array with delta_score / delta_comments /
        #    delta_window_min / sightings / first_seen_at attached. We mutate
        #    in place to preserve the prior contract (caller's `threads` list).
        snap_payload = [
            {
                "url": t.get("url"),
                "score": int(t.get("score") or 0),
                "num_comments": int(t.get("num_comments") or 0),
                "subreddit": (t.get("subreddit") or "").lstrip("r/"),
                "title": (t.get("title") or "")[:500],
            }
            for t in threads
            if t.get("url")
        ]
        if snap_payload:
            resp = api_post(
                "/api/v1/reddit-thread-snapshots",
                {"threads": snap_payload},
            )
            enriched = ((resp or {}).get("data") or {}).get("threads") or []
            by_url = {e.get("url"): e for e in enriched if e.get("url")}
            for t in threads:
                u = t.get("url")
                if not u:
                    continue
                e = by_url.get(u)
                if not e:
                    continue
                for k in ("delta_score", "delta_comments", "delta_window_min",
                          "sightings", "first_seen_at"):
                    if k in e:
                        t[k] = e[k]

        # 2) One row per query attempt
        api_post(
            "/api/v1/reddit-search-attempts",
            {
                "query": query,
                "subreddits": subreddits_csv or None,
                "project_name": project_name or None,
                "candidates_raw": int(stats.get("raw") or 0),
                "candidates_post_filter": int(stats.get("returned") or 0),
                "top_score": int(stats.get("top_score") or 0),
                "top_comments": int(stats.get("top_comments") or 0),
                "batch_id": batch_id or None,
            },
        )
    except Exception as e:
        # Side-effect-only logging: never raise. Print once to stderr so
        # the run log shows the failure without breaking the search.
        print(f"[reddit_search] WARN: feedback log failed: {e}", file=sys.stderr, flush=True)


def cmd_search(args):
    """Search Reddit and return threads as JSON.

    Uses sort=relevance by default for topically relevant results.
    Supports --subreddits to scope search to specific subs via restrict_sr.
    Supports --time to filter by recency (hour, day, week, month, year, all).

    Side effects (introduced 2026-05-05):
    - Logs one row to reddit_search_attempts per call (project + batch_id are
      pulled from env so the LLM tool-call signature stays unchanged).
    - Upserts one row to reddit_thread_snapshots per returned thread; attaches
      delta_score / delta_comments / delta_window_min to each thread in the
      stdout JSON when the same thread reappears across cycles. This feeds
      Claude a "thread is gaining traction" gating signal without a Twitter-
      style 2-phase staging refactor.
    """
    query = args.query
    time_filter = args.time

    # Load already-posted URLs for filtering via /api/v1/posts/thread-urls.
    # Scope per-account so two machines running different Reddit identities
    # (e.g. Deep_Ad1959 on Mac, Sea_Comparison_1799 on mk0r VM) don't skip
    # threads on each other's behalf. Falls back to unscoped when the
    # resolver can't pin a handle (legacy single-machine behavior).
    _reddit_account = _resolve_account("reddit")
    _probe_q = {"platform": "reddit"}
    if _reddit_account:
        _probe_q["our_account"] = _reddit_account
    try:
        resp = api_get("/api/v1/posts/thread-urls", query=_probe_q)
        urls = ((resp or {}).get("data") or {}).get("thread_urls") or []
        already_posted = {u for u in urls if u}
    except Exception as e:
        print(f"[reddit_search] WARN: thread-urls fetch failed: {e}", file=sys.stderr)
        already_posted = set()

    # Read project env BEFORE building the blocked-subs set so per-project
    # excludes (subreddit:<slug> rows in project_search_excludes) layer onto
    # the global denylist. The same env var is reused below for the feedback-
    # log side effect, so this reordering is free.
    project_env = os.environ.get("S4L_REDDIT_PROJECT") or None
    batch_env = os.environ.get("S4L_REDDIT_BATCH_ID") or None

    # Compute global vs project-augmented denylist sizes so the stderr marker
    # below shows how much of the block bucket came from the per-project
    # layer. Empty diff means project_search_excludes had no active sub rows
    # for this project (which is the normal state for new projects).
    blocked_subs_global = _load_comment_blocked_subs(project_name=None)
    blocked_subs = _load_comment_blocked_subs(project_name=project_env)
    project_block_extra = len(blocked_subs) - len(blocked_subs_global)

    # Determine subreddit scoping
    target_subs = None
    if args.subreddits:
        target_subs = [s.lstrip("r/") for s in args.subreddits.split(",")]

    url = _build_search_url(query, args.sort, args.limit, time_filter, subreddits=target_subs)
    data = _do_request(url)
    threads, stats = _parse_search_results(data, already_posted, blocked_subs)
    stats["project_block_extra"] = project_block_extra
    _log_search_and_attach_deltas(
        query, args.subreddits, project_env, batch_env, threads, stats,
    )

    # Emit a single-line marker on stderr so post_reddit.py can forward it into
    # run-reddit-search-*.log, where the dashboard's enrichPostCommentsRedditRuns
    # parses it for the raw/passed pills. Stdout JSON contract extended with
    # delta_* keys per thread (additive, parsers ignore unknown keys).
    safe_q = query.replace('"', '\\"')[:120]
    print(
        f'[reddit_search] q="{safe_q}" raw={stats["raw"]} returned={stats["returned"]} '
        f'blocked_sub={stats["blocked_sub"]} archived={stats["archived"]} '
        f'locked={stats["locked"]} too_old={stats["too_old"]} '
        f'already_posted_flagged={stats["already_posted_flagged"]} '
        f'top_score={stats["top_score"]} top_comments={stats["top_comments"]} '
        f'project_block_extra={stats.get("project_block_extra", 0)}',
        file=sys.stderr, flush=True,
    )

    # Opaque-results discover mode (post 2026-05-07 refactor): when
    # S4L_REDDIT_DUMP_DIR is set, write the full threads JSON to a unique
    # file in that directory and print ONLY a one-line summary to stdout.
    # This prevents Claude (running this tool from the discover prompt) from
    # ever seeing thread content, which it would otherwise filter despite
    # explicit "emit every thread" instructions. The orchestrator
    # (_discover_iteration in post_reddit.py) globs the dump dir after Claude
    # exits and reads every dumped file directly into the candidate plan.
    dump_dir = os.environ.get("S4L_REDDIT_DUMP_DIR")
    if dump_dir and os.path.isdir(dump_dir):
        import tempfile as _tempfile
        fd, dump_path = _tempfile.mkstemp(
            dir=dump_dir, prefix="result-", suffix=".json"
        )
        try:
            with os.fdopen(fd, "w") as df:
                json.dump({"query": query, "threads": threads, "stats": stats}, df)
        except Exception as e:
            # If dump fails, fall back to stdout so the cycle isn't silently broken.
            print(f"[reddit_search] WARN: dump failed, falling back to stdout: {e}",
                  file=sys.stderr, flush=True)
            print(json.dumps(threads, indent=2))
            return
        # Tell Claude only the count, not the content. No file path so Claude
        # can't `cat` it. The stderr [reddit_search] line above already gives
        # the full breakdown (raw/returned/blocked/etc.) for query-quality
        # decisions.
        print(f"OK: {stats['returned']} threads passed to ripen pipeline (results not shown)")
        return

    print(json.dumps(threads, indent=2))


def _html_postable_check(thread_url):
    """Second-opinion check against old.reddit.com HTML.

    Reddit's JSON `locked` and `archived` flags sometimes miss HTML-only
    lock states. Concretely seen on r/Entrepreneur where AutoMod renders
    `.locked-tagline` on the thread page while the JSON payload reports
    `locked=false`. This is cheap: one unauthenticated GET, ~1s, counts
    against the same rate-limit window as the JSON call above.

    Returns one of: "locked", "archived", "ok", or None on network error.
    """
    import re as _re
    try:
        url = thread_url.replace("www.reddit.com", "old.reddit.com").rstrip("/") + "/"
        _wait_if_needed()
        # Browser-first transport (Reddit 403s urllib). urllib fallback below.
        html = _fetch_via_browser(url)
        if html is None:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            resp = urllib.request.urlopen(req, timeout=15)
            remaining = float(resp.headers.get("X-Ratelimit-Remaining", 100))
            reset = float(resp.headers.get("X-Ratelimit-Reset", 0))
            _write_ratelimit(remaining, reset)
            html = resp.read().decode("utf-8", errors="ignore")
        # Scope the lock check to the post header only. r/Entrepreneur (and
        # similar subs) sticky an AutoMod comment that is itself locked,
        # rendering `<span class="locked-tagline">locked comment</span>`
        # inside the `.commentarea` div. Matching against that produces a
        # false-positive on every thread in the sub and silently kills all
        # candidates (see 2026-05-13 PieLine run: 10 ripen-survivors, 10
        # false html_locked drops). Slice to the prefix before the
        # comments section.
        ca_idx = html.find('class="commentarea"')
        if ca_idx < 0:
            # Couldn't isolate the header (empty thread, stripped page, etc.).
            # Trust the JSON `locked`/`archived` flags from cmd_repoll
            # instead of fail-closing every thread on a layout edge case.
            return "ok"
        header_html = html[:ca_idx]
        # Match only the tagline CSS classes, not the archived-popup template
        # that old.reddit.com preloads on every page.
        if _re.search(r'class="[^"]*\blocked-tagline\b', header_html):
            return "locked"
        if _re.search(r'class="[^"]*\barchived-tagline\b', header_html):
            return "archived"
        return "ok"
    except Exception:
        return None


def cmd_fetch(args):
    """Fetch a thread's comments via Reddit JSON API."""
    # Check if subreddit is blocked. Honors per-project excludes via the
    # S4L_REDDIT_PROJECT env var (same shape as cmd_search), so a sub on
    # a project's private denylist (or in project_search_excludes) returns
    # the same `subreddit_blocked` error and the LLM stops fetching it.
    import re as _re
    sub_match = _re.search(r'/r/([^/]+)', args.url)
    if sub_match:
        project_env = os.environ.get("S4L_REDDIT_PROJECT") or None
        blocked = _load_comment_blocked_subs(project_name=project_env)
        if sub_match.group(1).lower() in blocked:
            print(json.dumps({"error": "subreddit_blocked", "subreddit": sub_match.group(1)}))
            return

    # Convert URL to .json endpoint
    url = args.url.rstrip("/")
    # Handle old.reddit.com or www.reddit.com
    if not url.endswith(".json"):
        url = url + ".json"
    url = url + "?limit=20&sort=top"

    data = _do_request(url)

    if not isinstance(data, list) or len(data) < 2:
        print(json.dumps({"error": "unexpected response format"}))
        return

    # Thread info
    thread_data = data[0]["data"]["children"][0]["data"]
    thread = {
        "title": thread_data.get("title", ""),
        "author": thread_data.get("author", ""),
        "selftext": thread_data.get("selftext", ""),
        "score": thread_data.get("score", 0),
        "num_comments": thread_data.get("num_comments", 0),
        "subreddit": f"r/{thread_data.get('subreddit', '')}",
        "url": args.url,
    }

    if thread_data.get("archived") or thread_data.get("locked"):
        status = "archived" if thread_data.get("archived") else "locked"
        print(json.dumps({"error": f"thread_{status}", "thread": thread}))
        return

    html_state = _html_postable_check(args.url)
    if html_state in ("locked", "archived"):
        print(json.dumps({"error": f"thread_{html_state}", "thread": thread,
                          "detected_via": "html"}))
        return

    # Top comments (flatten one level)
    comments = []
    for child in data[1]["data"]["children"][:15]:
        if child.get("kind") != "t1":
            continue
        c = child.get("data", {})
        comment = {
            "id": c.get("name", ""),  # full thing ID like t1_abc123
            "author": c.get("author", ""),
            "body": c.get("body", ""),
            "score": c.get("score", 0),
            "permalink": f"https://old.reddit.com{c.get('permalink', '')}",
        }
        comments.append(comment)

    print(json.dumps({"thread": thread, "comments": comments}, indent=2))


def cmd_repoll(args):
    """Re-fetch current score/comments for a list of thread URLs.

    Used by ripen_reddit_plan.py to compute T1 - T0 deltas after a 5-min
    sleep, then gate posts by composite delta score.

    Reads JSON on stdin: {"urls": ["https://old.reddit.com/r/.../comments/.../...", ...]}
    Writes JSON to stdout: {"results": {"<url>": {"ok": true, "score": N, "comments": M} | {"ok": false, "error": "..."}}}

    Failures (network, rate limit, deleted thread) are returned per-url with
    ok=false so the caller can fail-closed and drop those candidates.
    """
    import re as _re
    raw = sys.stdin.read().strip()
    if not raw:
        print(json.dumps({"results": {}}))
        return
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"bad_json: {e}"}))
        sys.exit(1)
    urls = payload.get("urls") or []
    results = {}
    for url in urls:
        try:
            base = url.rstrip("/")
            if not base.endswith(".json"):
                base = base + ".json"
            data = _do_request(base + "?limit=1&sort=top")
            if not isinstance(data, list) or len(data) < 1:
                results[url] = {"ok": False, "error": "unexpected_response"}
                continue
            td = data[0]["data"]["children"][0]["data"]
            # Catch JSON-level locks/archives before reporting ok=True.
            # Note: Reddit's JSON locked flag sometimes misreports for HTML-only
            # AutoMod locks (see _html_postable_check). Those are caught later
            # in ripen via the check-locked subcommand for T1 survivors.
            if td.get("locked"):
                results[url] = {"ok": False, "error": "thread_locked"}
                continue
            if td.get("archived"):
                results[url] = {"ok": False, "error": "thread_archived"}
                continue
            results[url] = {
                "ok": True,
                "score": int(td.get("score") or 0),
                "comments": int(td.get("num_comments") or 0),
            }
        except RateLimitedError as e:
            results[url] = {"ok": False, "error": f"rate_limited:{int(e.reset_seconds)}"}
        except Exception as e:
            results[url] = {"ok": False, "error": f"{type(e).__name__}:{str(e)[:80]}"}
    print(json.dumps({"results": results}))


def cmd_check_locked(args):
    """Lightweight HTML-only lock check for a single thread URL.

    Used by ripen_reddit_plan.py after the delta gate to catch AutoMod
    HTML-only locks that the JSON API misreports as locked=false (known
    issue on r/Entrepreneur and others). One unauthenticated GET, ~1s.

    Returns {"url": "...", "state": "ok"|"locked"|"archived"|"error"}
    """
    state = _html_postable_check(args.url)
    print(json.dumps({"url": args.url, "state": state or "error"}))


def cmd_already_posted(args):
    """Check if we already posted in a thread via /api/v1/posts/lookup.

    Scoped per-account so multiple machines running different Reddit
    identities (e.g. Deep_Ad1959 on Mac, Sea_Comparison_1799 on mk0r VM)
    don't see each other's posts as their own. Falls back to unscoped
    when no handle is configured (legacy single-machine behavior).
    """
    q = {"platform": "reddit", "thread_url": args.url}
    acct = _resolve_account("reddit")
    if acct:
        q["our_account"] = acct
    resp = api_get("/api/v1/posts/lookup", query=q)
    post = ((resp or {}).get("data") or {}).get("post")
    if post:
        print(json.dumps({
            "already_posted": True,
            "post_id": post.get("id"),
            "content_preview": post.get("our_content"),
        }))
    else:
        print(json.dumps({"already_posted": False}))


def cmd_log_post(args):
    """Log a posted comment via /api/v1/posts POST.

    The route enforces the (platform, thread_url) dedup server-side and
    returns 409 with existing_post_id when the thread is already in the
    table; ok_on_conflict=True surfaces that as a structured body.
    """
    session_id = os.environ.get("CLAUDE_SESSION_ID") or None
    # Generation trace: opaque JSONB blob captured by post_reddit.py
    # before invoking Claude. Loaded from a file path (--generation-trace)
    # because the JSON can be several KB; passing inline blows past
    # macOS ARG_MAX. Failure to read just nulls the field — never
    # blocks the INSERT, since losing the audit row for one post is
    # preferable to losing the post.
    generation_trace_blob = None
    trace_path = getattr(args, "generation_trace", None)
    if trace_path:
        try:
            with open(trace_path, "r", encoding="utf-8") as tf:
                generation_trace_blob = json.load(tf)
        except (OSError, json.JSONDecodeError) as e:
            # Stderr only — stdout is reserved for the JSON envelope
            # that post_reddit.py:log_post() parses.
            print(f"WARNING: could not load generation_trace {trace_path}: {e}",
                  file=sys.stderr)
    body = {
        "platform": "reddit",
        "thread_url": args.thread_url,
        "thread_author": args.thread_author,
        "thread_title": args.thread_title,
        "our_url": args.our_url,
        "our_content": args.our_text,
        "our_account": args.account,
        "project": args.project,
        "engagement_style": getattr(args, "engagement_style", None),
        "search_topic": getattr(args, "search_topic", None),
        # draft_prompt A/B arm that shaped this draft (2026-07-16); same
        # posts.draft_prompt_variant column log_post.py stamps for twitter.
        "draft_prompt_variant": getattr(args, "draft_prompt_variant", None),
        "claude_session_id": session_id,
        "language": None,
        "is_recommendation": False,
    }
    if generation_trace_blob is not None:
        body["generation_trace"] = generation_trace_blob
    # link_source (2026-05-17): tags audience-page traffic (e.g.
    # 'audience_page:founder-ghostwriting') so the dashboard can break out
    # curated landing-page hits from generic homepage links. Set by
    # post_reddit.py based on which URL Claude baked into the reply text.
    if getattr(args, "link_source", None):
        body["link_source"] = args.link_source
    # autoposter_version: social-autoposter package.json version at the moment
    # we posted. Powers per-release attribution: "did 1.5.0 outperform 1.4.x
    # on Reddit?". None when package.json + env are both missing.
    autoposter_version = read_autoposter_version()
    if autoposter_version:
        body["autoposter_version"] = autoposter_version
    resp = api_post("/api/v1/posts", body, ok_on_conflict=True)
    err = resp.get("error") if isinstance(resp, dict) else None
    if err:
        details = (err.get("details") or {}) if isinstance(err, dict) else {}
        print(json.dumps({
            "error": "DUPLICATE_THREAD",
            "message": "Already posted in this thread",
            "existing_post_id": details.get("existing_post_id"),
            "content_preview": details.get("content_preview"),
        }))
        return
    post = ((resp or {}).get("data") or {}).get("post") or {}
    print(json.dumps({
        "logged": True,
        "post_id": post.get("id"),
        "claude_session_id": session_id,
    }))


def main():
    parser = argparse.ArgumentParser(description="Reddit tools for Claude")
    sub = parser.add_subparsers(dest="command")

    # search
    p_search = sub.add_parser("search", help="Search Reddit for threads")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("--limit", type=int, default=15, help="Max results")
    p_search.add_argument("--sort", default="relevance", help="Sort order (relevance, new, hot, top, comments)")
    p_search.add_argument("--time", default="week", help="Time filter (hour, day, week, month, year, all)")
    p_search.add_argument("--subreddits", default=None, help="Comma-separated subreddits to scope search (e.g. AI_Agents,SaaS,smallbusiness)")

    # fetch
    p_fetch = sub.add_parser("fetch", help="Fetch thread + comments")
    p_fetch.add_argument("url", help="Thread URL")

    # repoll (T1 fetch for ripen)
    sub.add_parser("repoll", help="Re-fetch score/comments for a list of thread URLs (JSON on stdin)")

    # check-locked (HTML-based lock check, used by ripen for T1 survivors)
    p_cl = sub.add_parser("check-locked", help="Check if a thread is locked via old.reddit.com HTML")
    p_cl.add_argument("url", help="Thread URL")

    # already-posted
    p_ap = sub.add_parser("already-posted", help="Check if already posted in thread")
    p_ap.add_argument("url", help="Thread URL")

    # log-post
    p_log = sub.add_parser("log-post", help="Log a posted comment to DB")
    p_log.add_argument("thread_url")
    p_log.add_argument("our_url")
    p_log.add_argument("our_text")
    p_log.add_argument("project")
    p_log.add_argument("thread_author")
    p_log.add_argument("thread_title")
    p_log.add_argument("--account", default="Deep_Ad1959")
    p_log.add_argument("--engagement-style", default=None)
    p_log.add_argument("--search-topic", dest="search_topic", default=None,
                       help="The seed topic/query used to find this thread (feedback loop input)")
    p_log.add_argument("--generation-trace", dest="generation_trace", default=None,
                       help="Path to a JSON file with the few-shot context Claude "
                            "saw before drafting (top_performers report, recent "
                            "comments, model, prompt size). Stored in "
                            "posts.generation_trace JSONB for audit. See "
                            "migrations/2026-05-12_generation_trace.sql for the "
                            "shape contract.")
    p_log.add_argument("--link-source", dest="link_source", default=None,
                       help="Optional tag for posts.link_source so the dashboard "
                            "can break out audience-page traffic (e.g. "
                            "'audience_page:founder-ghostwriting') from generic "
                            "homepage links.")
    p_log.add_argument("--draft-prompt-variant", dest="draft_prompt_variant",
                       default=None,
                       help="draft_prompt A/B arm that shaped this draft "
                            "(treatment_v4/control_v4); stored in "
                            "posts.draft_prompt_variant, same column the "
                            "twitter lane stamps via log_post.py.")

    args = parser.parse_args()
    try:
        if args.command == "search":
            cmd_search(args)
        elif args.command == "fetch":
            cmd_fetch(args)
        elif args.command == "repoll":
            cmd_repoll(args)
        elif args.command == "check-locked":
            cmd_check_locked(args)
        elif args.command == "already-posted":
            cmd_already_posted(args)
        elif args.command == "log-post":
            cmd_log_post(args)
        else:
            parser.print_help()
    except RateLimitedError as e:
        # Return a clean JSON error so Claude can skip and try another action
        print(json.dumps({
            "error": "rate_limited",
            "wait_seconds": int(e.reset_seconds),
            "message": f"Reddit API rate limit hit. Skip this query and try a different topic or command.",
        }))
        sys.exit(2)


if __name__ == "__main__":
    main()
