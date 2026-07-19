#!/usr/bin/env python3
"""Operator fleet report: per-install plugin version + latest resource sample.

Answers the two questions we kept not being able to answer:
  - which version is each user on?
  - who is leaking RAM / still lacks the agent-mode-session reaper (<1.6.111)?

Reads the installations table directly via psql + DATABASE_URL from .env (this
is an operator-box tool, not shipped to customers). app_version and the latest
resource_sample only populate once a box runs a build that ships them, so rows
predating this feature show "?" until their next heartbeat on the new build.

Usage:
    python3 scripts/install_fleet.py              # all installs, newest-seen first
    python3 scripts/install_fleet.py --active 7   # only seen in the last 7 days
    python3 scripts/install_fleet.py --leaking    # only rows over the RAM threshold
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[1]
REAPER_FIX_VERSION = (1, 6, 111)  # social-autoposter@1.6.111 shipped the leak reaper
LEAK_RAM_MB = 12000  # our-process RSS over this on a box smells like the session leak


def _database_url() -> str:
    env = REPO_DIR / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip()
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    sys.exit("DATABASE_URL not found in .env or environment")


def _ver_tuple(v: str | None) -> tuple[int, ...] | None:
    if not v:
        return None
    parts = v.strip().lstrip("v").split(".")
    try:
        return tuple(int(p) for p in parts[:3])
    except ValueError:
        return None


def _reaper_flag(v: str | None) -> str:
    t = _ver_tuple(v)
    if t is None:
        return "?(pre-telemetry)"
    return "ok" if t >= REAPER_FIX_VERSION else "BEHIND<1.6.111"


def _fnum(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--active", type=int, default=0, help="only installs seen in the last N days")
    ap.add_argument("--leaking", action="store_true", help="only rows whose our-process RSS exceeds the leak threshold")
    ap.add_argument("--json", action="store_true", help="emit raw JSON rows instead of the table")
    args = ap.parse_args()

    where = ""
    if args.active > 0:
        where = f"WHERE last_seen_at > NOW() - interval '{args.active} days'"

    # Tab-separated so we can parse jsonb columns intact (no embedded tabs in our samples).
    query = f"""
      SELECT
        install_id,
        COALESCE(hostname, '?'),
        COALESCE(app_version, ''),
        COALESCE(git_email, '?'),
        COALESCE(last_city, '?') || '/' || COALESCE(last_country, '?'),
        EXTRACT(EPOCH FROM (NOW() - last_seen_at))::bigint,
        request_count,
        COALESCE(resource_sample::text, ''),
        COALESCE(EXTRACT(EPOCH FROM (NOW() - resource_sampled_at))::bigint::text, '')
      FROM installations
      {where}
      ORDER BY last_seen_at DESC;
    """

    out = subprocess.run(
        ["psql", _database_url(), "-t", "-A", "-F", "\t", "-c", query],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30,
    )
    if out.returncode != 0:
        sys.exit(f"psql failed: {out.stderr.strip()}")

    rows = []
    for line in out.stdout.splitlines():
        if not line.strip():
            continue
        cols = line.split("\t")
        if len(cols) < 9:
            continue
        iid, host, appv, email, geo, age_s, reqs, sample_json, sample_age_s = cols[:9]
        sample = {}
        if sample_json:
            try:
                sample = json.loads(sample_json)
            except Exception:
                sample = {}
        mem = sample.get("mem") or {}
        groups = sample.get("groups") or {}

        def grp_mb(name: str) -> float | None:
            g = groups.get(name) or {}
            return _fnum(g.get("rss_mb"))

        # "our footprint" = the S4L MCP servers + the Claude Desktop sessions that
        # have our MCP loaded (the bucket Karol saw as "the S4L scheduled task").
        our_mb = sum(
            v for v in (
                grp_mb("social_autoposter_mcp_servers"),
                grp_mb("sessions_configured_social_autoposter_mcp"),
            ) if v is not None
        ) or None
        claude_grp = groups.get("claude_cli") or {}
        rows.append({
            "install_id": iid,
            "hostname": host,
            "app_version": appv or None,
            "reaper": _reaper_flag(appv or None),
            "git_email": email,
            "geo": geo,
            "last_seen_age_h": round(int(age_s) / 3600, 1) if age_s else None,
            "requests": int(reqs) if reqs.isdigit() else reqs,
            "mem_used_mb": _fnum(mem.get("used_mb")),
            "mem_total_mb": _fnum(mem.get("total_mb")),
            "our_mb": round(our_mb, 1) if our_mb is not None else None,
            "claude_cli_mb": _fnum(claude_grp.get("rss_mb")),
            "claude_cli_n": claude_grp.get("count"),
            "sample_age_min": round(int(sample_age_s) / 60, 1) if sample_age_s else None,
        })

    if args.leaking:
        rows = [r for r in rows if (r["our_mb"] or 0) >= LEAK_RAM_MB]

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    if not rows:
        print("no installs match")
        return 0

    hdr = (
        f"{'hostname':<24} {'ver':<9} {'reaper':<16} {'used/total GB':<14} "
        f"{'ourGB':<7} {'claude(n)':<11} {'seen':<8} {'sample':<8} email"
    )
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        used = r["mem_used_mb"]
        total = r["mem_total_mb"]
        used_total = f"{used/1024:.1f}/{total/1024:.0f}" if used and total else "?"
        our = f"{r['our_mb']/1024:.1f}" if r["our_mb"] else "?"
        claude = (
            f"{r['claude_cli_mb']/1024:.1f}({r['claude_cli_n']})"
            if r["claude_cli_mb"] is not None else "?"
        )
        seen = f"{r['last_seen_age_h']}h" if r["last_seen_age_h"] is not None else "?"
        sample = f"{r['sample_age_min']}m" if r["sample_age_min"] is not None else "none"
        print(
            f"{r['hostname'][:24]:<24} {(r['app_version'] or '?'):<9} {r['reaper']:<16} "
            f"{used_total:<14} {our:<7} {claude:<11} {seen:<8} {sample:<8} {r['git_email']}"
        )
    print(f"\n{len(rows)} install(s). 'reaper=BEHIND' lacks the 1.6.111 session-leak fix; "
          f"'?' = hasn't heartbeat'd on a telemetry-capable build yet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
