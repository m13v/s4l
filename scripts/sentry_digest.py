#!/usr/bin/env python3
"""Sentry issue scanner for the s4l Sentry project (org mediar-n5).

Pull-only step in the sentry-digest pipeline (see skill/sentry-digest.sh and
skill/SENTRY-DIGEST-SKILL.md). This script does NOT investigate, does NOT
email, and does NOT write the ledger. It just:

1. Pulls unresolved level:error/level:fatal issues ("critical Sentry issues"
   per user instruction 2026-07-07; level:warning menubar pings like
   "draft_stuck" are excluded, they're a different kind of event already
   surfaced elsewhere).
2. Fetches distinct install_id impact per issue (Sentry's built-in userCount
   is 0 across the board for this project; installs are tagged via
   install_id instead).
3. Diffs against the ledger to classify each issue as NEW / GROWING / STABLE.
4. Writes the full scan result to a JSON file on disk and prints a one-line
   summary for the bash wrapper to decide whether to spawn Claude for
   investigation.

The actual investigation (read the crashing code, check git log, judge
actionability, draft a human-readable email, send it, write the ledger back)
is done by a spawned Claude Code session per skill/SENTRY-DIGEST-SKILL.md,
mirroring how ~/fazm/inbox/skill/check-query-insights.sh divides labor
between a mechanical pull step and an investigative Claude step.

Usage:
    python3 scripts/sentry_digest.py --out /path/to/scan.json
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

SENTRY_ORG = "mediar-n5"
SENTRY_PROJECT = "s4l"
SENTRY_PROJECT_ID = "4511598804336640"
SENTRY_API = "https://sentry.io/api/0"

# "Critical Sentry issues" = level:error or level:fatal.
ISSUE_QUERY = "is:unresolved level:[error,fatal]"

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(REPO_DIR, "scripts", "state")
LEDGER_PATH = os.path.join(STATE_DIR, "sentry_digest_ledger.json")

# Growth threshold: an issue counts as GROWING if event count grew by at
# least this many events AND by at least this relative fraction since the
# ledger snapshot.
GROWTH_MIN_DELTA = 5
GROWTH_MIN_RATIO = 1.2


def _sentry_token():
    env_token = os.environ.get("SENTRY_AUTH_TOKEN")
    if env_token:
        return env_token
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", "sentry-auth-token", "-w"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return None


def _sentry_get(path, token):
    req = urllib.request.Request(f"{SENTRY_API}{path}", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_critical_issues(token):
    path = (
        f"/projects/{SENTRY_ORG}/{SENTRY_PROJECT}/issues/"
        f"?query={urllib.parse.quote(ISSUE_QUERY)}&statsPeriod=24h&sort=freq&limit=100"
    )
    return _sentry_get(path, token)


def fetch_install_impact(issue_id, token):
    """Distinct install_id count for one issue. Returns 0 if the tag is absent."""
    try:
        data = _sentry_get(f"/issues/{issue_id}/tags/install_id/", token)
        return data.get("uniqueValues", 0)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return 0
        raise
    except Exception:
        return 0


def load_ledger():
    if not os.path.exists(LEDGER_PATH):
        return {"version": 1, "lastUpdated": None, "issues": {}}
    try:
        with open(LEDGER_PATH) as f:
            return json.load(f)
    except Exception:
        return {"version": 1, "lastUpdated": None, "issues": {}}


def issue_link(short_id):
    return f"https://mediar-n5.sentry.io/issues/?project={SENTRY_PROJECT_ID}&query={short_id}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, help="Path to write the scan result JSON.")
    args = parser.parse_args()

    token = _sentry_token()
    if not token:
        print("ERROR: no Sentry auth token (checked SENTRY_AUTH_TOKEN env and keychain sentry-auth-token)", file=sys.stderr)
        sys.exit(1)

    issues = fetch_critical_issues(token)
    if not isinstance(issues, list):
        print(f"ERROR: unexpected Sentry response: {issues}", file=sys.stderr)
        sys.exit(1)

    ledger = load_ledger()
    known = ledger.get("issues", {})
    first_run = len(known) == 0

    scanned = []
    new_count = growing_count = 0

    for issue in issues:
        short_id = issue["shortId"]
        numeric_id = issue["id"]
        count = int(issue.get("count", 0))
        installs = fetch_install_impact(numeric_id, token)
        prev = known.get(short_id)

        if prev is None:
            classification = "new"
            new_count += 1
        else:
            prev_count = int(prev.get("lastEventCount", 0))
            is_growing = (
                not first_run
                and count - prev_count >= GROWTH_MIN_DELTA
                and prev_count > 0
                and count >= prev_count * GROWTH_MIN_RATIO
            )
            if is_growing:
                classification = "growing"
                growing_count += 1
            else:
                classification = "stable"

        scanned.append({
            "shortId": short_id,
            "numericId": numeric_id,
            "title": issue["title"],
            "level": issue.get("level"),
            "count": count,
            "prevCount": int(prev["lastEventCount"]) if prev else None,
            "installs": installs,
            "prevInstalls": prev.get("lastInstallCount") if prev else None,
            "classification": classification,
            "link": issue_link(short_id),
            "lastSeen": issue.get("lastSeen"),
            "firstSeen": issue.get("firstSeen"),
        })

    result = {
        "scannedAt": datetime.now(timezone.utc).isoformat(),
        "firstRun": first_run,
        "totalOpen": len(issues),
        "newCount": new_count,
        "growingCount": growing_count,
        "ledgerPath": LEDGER_PATH,
        "issues": scanned,
    }

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(result, f, indent=2)

    print(f"firstRun={first_run} newCount={new_count} growingCount={growing_count} totalOpen={len(issues)} out={args.out}")


if __name__ == "__main__":
    main()
