#!/usr/bin/env python3
"""Email-alert when any external_short_links pool runs low.

For every project with `external_short_links: true` in config.json, checks the
(project, platform) pool depth against two thresholds:

  WARN     -- available / total <= 0.20  (i.e., 80% of the pool has been claimed)
  CRITICAL -- available == 0             (pool exhausted, next post returns
                                          {ok: false, error: 'pool_exhausted'})

Emails go to i@m13v.com via the Gmail DWD lane. State lives in the
`external_pool_alerts` table so we don't spam: same (project, platform,
severity) is suppressed for 24h after a send.

Designed to run on launchd every 30 min. The 20% threshold gives 7-30 days of
runway warning before a CRITICAL fires (at typical 5-15 posts/day burn). The
CRITICAL alert is the "on error" case the user asked for; it fires at most
once per 24h per (project, platform).

Usage:
  python3 scripts/check_external_pool_depth.py            # check + alert
  python3 scripts/check_external_pool_depth.py --dry-run  # report only, no email/state writes
  python3 scripts/check_external_pool_depth.py --force    # ignore 24h cooldown
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
import base64

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_DIR, 'scripts'))
sys.path.insert(0, os.path.expanduser('~/gmail-api'))

from http_api import api_get, api_post  # noqa: E402

WARN_REMAINING_RATIO = 0.20
COOLDOWN_HOURS = 24
NOTIFICATION_EMAIL = os.environ.get('NOTIFICATION_EMAIL', 'i@m13v.com')
PLATFORMS = ['reddit', 'twitter', 'linkedin', 'github_issues', 'moltbook']
CONFIG_PATH = os.path.join(REPO_DIR, 'config.json')


def _scrub_dashes(s: str) -> str:
    return s.replace('—', ',').replace('–', ',') if s else s


def _load_external_projects() -> list[dict]:
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    return [p for p in cfg.get('projects', []) if p.get('external_short_links')]


def _pool_depth(project: str, platform: str) -> tuple[int, int]:
    resp = api_get(
        "/api/v1/post-links/pool-depth",
        query={"project_name": project, "platform": platform},
    )
    d = resp.get("data") or {}
    return int(d.get("available") or 0), int(d.get("total") or 0)


def _recent_alert_exists(project: str, platform: str, severity: str) -> bool:
    resp = api_get(
        "/api/v1/external-pool-alerts",
        query={
            "project_name": project,
            "platform": platform,
            "severity": severity,
            "within_hours": COOLDOWN_HOURS,
        },
    )
    return bool((resp.get("data") or {}).get("recent"))


def _record_alert(project: str, platform: str, severity: str,
                  available: int, total: int, ratio: float) -> None:
    api_post(
        "/api/v1/external-pool-alerts",
        {
            "project_name": project,
            "platform": platform,
            "severity": severity,
            "available": available,
            "total": total,
            "ratio": ratio,
        },
    )


def _gmail_send(subject: str, body: str) -> None:
    from gmail_dwd_client import gmail_for
    msg = EmailMessage()
    msg['Subject'] = _scrub_dashes(subject)
    msg['From'] = 'social-autoposter <i@m13v.com>'
    msg['To'] = NOTIFICATION_EMAIL
    msg.set_content(_scrub_dashes(body))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode('ascii')
    client = gmail_for('i@m13v.com')
    client.service.users().messages().send(userId='me', body={'raw': raw}).execute()


def _format_subject(project: str, platform: str, severity: str,
                    available: int, total: int) -> str:
    pct = f"{(available / total * 100):.0f}%" if total else "0%"
    return f"[POOL {severity}] {project}/{platform}: {available}/{total} left ({pct})"


def _format_body(project: str, platform: str, severity: str,
                 available: int, total: int, ratio: float,
                 destinations: list[dict]) -> str:
    lines = [
        f"Severity:     {severity}",
        f"Project:      {project}",
        f"Platform:     {platform}",
        f"Available:    {available}",
        f"Total minted: {total}",
        f"Remaining:    {ratio*100:.1f}%",
        "",
    ]
    if severity == 'CRITICAL':
        lines += [
            "The pool is exhausted. The next post for this (project, platform) will",
            "return {ok: false, error: 'pool_exhausted'} and skip. Refill IMMEDIATELY:",
            "",
        ]
    else:
        lines += [
            f"Pool has dropped under {int(WARN_REMAINING_RATIO*100)}% remaining. Schedule a refill",
            "in the next few days to avoid hitting pool_exhausted on the next cycle.",
            "",
        ]
    lines += [
        "Refill commands (in ~/social-autoposter):",
        "  python3 scripts/mint_kent_pool.py        # Kent clients (Runner/Agora/Podlog)",
        "  # for other external clients: extend mint_kent_pool.py SITE_CONFIG first",
        "",
        "Pool status snapshot:",
        "  python3 scripts/mint_kent_pool.py --status",
        "",
    ]
    if destinations:
        lines += ["Per-destination breakdown for this slice:"]
        for d in destinations[:20]:
            lines.append(
                f"  {d['minted_session'][-65:]:<65} "
                f"avail={d['available']:>5} claimed={d['claimed']:>5}"
            )
        if len(destinations) > 20:
            lines.append(f"  ... and {len(destinations) - 20} more destinations")
        lines.append("")
    lines += [
        f"Cooldown:     {COOLDOWN_HOURS}h per (project, platform, severity)",
        f"Re-fire:      python3 scripts/check_external_pool_depth.py --force",
    ]
    return "\n".join(lines)


def _destinations_for_slice(project: str, platform: str) -> list[dict]:
    resp = api_get(
        "/api/v1/post-links/pool-depth",
        query={
            "project_name": project,
            "platform": platform,
            "with_destinations": "1",
        },
    )
    return (resp.get("data") or {}).get("destinations") or []


def check(dry_run: bool = False, force: bool = False,
          warn_ratio: float = WARN_REMAINING_RATIO,
          limit: int | None = None) -> dict:
    projects = _load_external_projects()
    fired: list[dict] = []
    skipped_cooldown: list[dict] = []
    healthy: list[dict] = []
    for p in projects:
        project_name = p['name']
        for platform in PLATFORMS:
            available, total = _pool_depth(project_name, platform)
            if total == 0:
                continue
            ratio = available / total if total > 0 else 0.0
            if available == 0:
                severity = 'CRITICAL'
            elif ratio <= warn_ratio:
                severity = 'WARN'
            else:
                healthy.append({
                    'project': project_name, 'platform': platform,
                    'available': available, 'total': total, 'ratio': ratio,
                })
                continue
            key = (project_name, platform, severity)
            if not force and _recent_alert_exists(*key):
                skipped_cooldown.append({
                    'project': project_name, 'platform': platform,
                    'severity': severity, 'available': available, 'total': total,
                })
                continue
            fired_row = {
                'project': project_name, 'platform': platform,
                'severity': severity, 'available': available, 'total': total,
                'ratio': ratio,
            }
            fired.append(fired_row)
            if dry_run:
                continue
            if limit is not None and len(fired) > limit:
                continue
            destinations = _destinations_for_slice(project_name, platform)
            subject = _format_subject(project_name, platform, severity, available, total)
            body = _format_body(project_name, platform, severity, available, total,
                                ratio, destinations)
            try:
                _gmail_send(subject, body)
                _record_alert(project_name, platform, severity,
                              available, total, ratio)
            except Exception as e:
                fired_row['send_error'] = str(e)
                print(f"[pool-check] email send failed for {project_name}/{platform}: {e}",
                      file=sys.stderr)
    return {
        'checked_at': datetime.now(timezone.utc).isoformat(),
        'fired': fired,
        'skipped_cooldown': skipped_cooldown,
        'healthy_count': len(healthy),
        'dry_run': dry_run,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--dry-run', action='store_true',
                    help='compute and report, do not email or write state')
    ap.add_argument('--force', action='store_true',
                    help='ignore 24h cooldown, re-fire matching alerts')
    ap.add_argument('--warn-ratio', type=float, default=WARN_REMAINING_RATIO,
                    help=f'WARN threshold for available/total (default {WARN_REMAINING_RATIO})')
    ap.add_argument('--limit', type=int, default=None,
                    help='cap the number of alert emails per run (smoke testing)')
    args = ap.parse_args()
    result = check(dry_run=args.dry_run, force=args.force,
                   warn_ratio=args.warn_ratio, limit=args.limit)
    print(json.dumps(result, indent=2, default=str))


if __name__ == '__main__':
    main()
