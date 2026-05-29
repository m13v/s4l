#!/usr/bin/env python3
"""X API supply pre-check for the Twitter cycle's lean Phase 1.

Before the cycle spends ~$0.50 of Opus drafting a search query for a project,
probe X's near-free `GET /2/tweets/counts/recent` endpoint (counts only, no
tweet bodies, no model cost) over the SAME freshness window the scraper will
enforce. If a picked topic has ZERO fresh tweets in that window, drafting a
query for it can only ever return 0 candidates, so we drop it before paying
the drafter. For NightOwl-style thin niches (~0 fresh on-topic tweets/hour)
this turns a ~$2.65/run "5 drafts, 0 results" loop into 5 free probes + 0
drafts.

Design contract (read before changing):

- BROADER-than-draft probe. The probe is `<topic> lang:en -is:retweet` with NO
  `min_faves:` and NONE of the project's `excludes_for_search` -term filters.
  The model's real query is always NARROWER (it adds min_faves and excludes),
  so probe_count >= model_query_count. Therefore probe==0 ⟹ the model's query
  would also be 0. The gate can only skip topics that are PROVABLY dry; it
  never produces a false skip.

- FAIL OPEN. Any error (missing token, network, rate-limit, 4xx/5xx, malformed
  body) keeps the project (count reported as -1, treated as "has supply").
  A cost-optimization gate must never silently halt drafting because the API
  hiccuped. Only a confirmed integer 0 skips a topic.

- explore_invent passthrough. Projects in explore_invent mode have no topic yet
  (`search_topic` is null), so there is nothing to probe — they are always
  kept. Invention must not be gated.

- Freshness is NOT a tunable here. The probe window == `--freshness-hours`,
  which the caller passes from FRESHNESS_HOURS_DISCOVER. This script never
  widens it.

Output (stdout, single JSON object):
  {
    "all_dry": bool,                 # true iff nothing is left to draft for
    "kept": [ <project dict>, ... ], # PROJECTS_JSON filtered to drawable rows
    "probes": [ {project, search_topic, query, count, mode}, ... ]
  }
`count` is the X-reported total for the window, or -1 when the probe failed
open (kept anyway). Exit code is always 0 unless argument parsing fails; the
caller reads `all_dry`/`kept` from the JSON, never the exit code.
"""

import argparse
import datetime
import json
import os
import sys
import urllib.parse
import urllib.request

COUNTS_URL = "https://api.twitter.com/2/tweets/counts/recent"


def _load_bearer_token():
    """Return the X API bearer token, env var first then ~/social-autoposter/.env."""
    tok = (os.environ.get("TWITTER_BEARER_TOKEN") or "").strip()
    if tok:
        return tok
    env_path = os.path.join(os.path.expanduser("~/social-autoposter"), ".env")
    try:
        with open(env_path) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("TWITTER_BEARER_TOKEN="):
                    val = line.split("=", 1)[1].strip()
                    return val.strip("\"'")
    except OSError:
        pass
    return ""


def _build_probe_query(search_topic):
    """Broadest reasonable query for the topic: bare terms, English, no retweets.

    Deliberately omits min_faves and excludes_for_search so the probe count is
    an upper bound on what the model's narrower draft could return.
    """
    topic = (search_topic or "").strip()
    return f"{topic} lang:en -is:retweet" if topic else ""


def _count_recent(token, query, freshness_hours):
    """Return X's total_tweet_count for `query` over the last freshness_hours.

    Returns a non-negative int on success, or -1 on ANY failure (fail open).
    """
    start = datetime.datetime.utcnow() - datetime.timedelta(hours=freshness_hours)
    start_iso = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    params = urllib.parse.urlencode(
        {"query": query, "start_time": start_iso, "granularity": "hour"}
    )
    req = urllib.request.Request(
        f"{COUNTS_URL}?{params}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — fail open on every error
        sys.stderr.write(f"supply_precheck: probe error for {query!r}: {exc}\n")
        return -1
    meta = body.get("meta") or {}
    total = meta.get("total_tweet_count")
    if not isinstance(total, int):
        sys.stderr.write(
            f"supply_precheck: no integer total_tweet_count for {query!r} "
            f"(meta={meta!r}); failing open\n"
        )
        return -1
    return total


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--projects", required=True, help="PROJECTS_JSON array string")
    ap.add_argument("--freshness-hours", type=int, default=1)
    args = ap.parse_args()

    try:
        projects = json.loads(args.projects or "[]")
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"supply_precheck: bad --projects JSON: {exc}; failing open\n")
        # Fail open: keep everything, draft as normal.
        print(json.dumps({"all_dry": False, "kept": [], "probes": []}))
        return 0
    if not isinstance(projects, list):
        projects = []

    token = _load_bearer_token()
    if not token:
        sys.stderr.write("supply_precheck: no TWITTER_BEARER_TOKEN; failing open\n")
        print(json.dumps({"all_dry": False, "kept": projects, "probes": []}))
        return 0

    kept = []
    probes = []
    for p in projects:
        name = p.get("name") if isinstance(p, dict) else None
        topic = (p.get("search_topic") if isinstance(p, dict) else None) or ""
        topic = topic.strip()
        mode = (p.get("topic_picked_mode") if isinstance(p, dict) else None) or "use"

        # explore_invent / no-topic rows: nothing to probe, always keep.
        if not topic:
            kept.append(p)
            probes.append(
                {"project": name, "search_topic": None, "query": None,
                 "count": -1, "mode": "explore_invent"}
            )
            continue

        query = _build_probe_query(topic)
        count = _count_recent(token, query, args.freshness_hours)
        probes.append(
            {"project": name, "search_topic": topic, "query": query,
             "count": count, "mode": mode}
        )
        # Keep on confirmed supply (>0) OR fail-open (-1). Skip only on a hard 0.
        if count != 0:
            kept.append(p)

    all_dry = len(kept) == 0
    print(json.dumps({"all_dry": all_dry, "kept": kept, "probes": probes}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
