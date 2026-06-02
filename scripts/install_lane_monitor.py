#!/usr/bin/env python3
"""Watch the install lane canary in real time.

Run any time:
    python3 scripts/install_lane_monitor.py

Checks:
  1. Heartbeat freshness — alerts if last beat > 30min old.
  2. Install attribution coverage — % of replies created in the last 24h
     for each platform that have install_id stamped (canary github should
     trend to ~100%; the other 3 should stay at 0% until we flip them).
  3. Stuck-in-processing — replies left in 'processing' > 30min are the
     classic failure mode for the new lane (server claimed the row, then
     a downstream step failed silently).
  4. Recent install lane errors — scans launchd-heartbeat logs for FAIL.

Exit code 0 if everything green, 1 if any check fails. Safe in cron.
"""
import os, sys, subprocess
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from http_api import api_get, load_env

load_env()
_digest = (api_get("/api/v1/install-lane/digest", query={"within_hours": 24}).get("data") or {})

OK   = "\033[32m✓\033[0m"
WARN = "\033[33m!\033[0m"
FAIL = "\033[31m✗\033[0m"
status = 0

print("=" * 64)
HTTP_LANE_PLATFORMS = {"github", "reddit", "x", "linkedin", "moltbook"}
print(f"INSTALL LANE CANARY (HTTP: {', '.join(sorted(HTTP_LANE_PLATFORMS))} / others SQL)")
print("=" * 64)

# 1. Heartbeat freshness
rows = _digest.get("heartbeat") or []
print("\n[1] HEARTBEAT")
if not rows:
    print(f"  {FAIL} no installations rows yet")
    status = 1
else:
    head = rows[0]
    age = head["age_sec"]
    age_disp = f"{age}s" if age < 120 else f"{age // 60}m {age % 60}s"
    flag = OK if age < 1800 else (WARN if age < 3600 else FAIL)
    if age >= 1800:
        status = 1
    print(f"  {flag} latest beat {age_disp} ago")
    print(f"     install_id  {head['install_id']}")
    print(f"     hostname    {head['hostname']}")
    print(f"     beats total {head['request_count']}")
    print(f"     last_ip     {head['last_ip']} ({head['last_city'] or '?'} / {head['last_country'] or '?'})")
    if len(rows) > 1:
        print(f"     +{len(rows)-1} other install(s) seen recently")

# 2. Per-platform attribution coverage (last 24h created replies)
cur = db.execute("""
    SELECT platform,
           COUNT(*)                                   AS total,
           COUNT(install_id)                          AS attributed,
           COUNT(CASE WHEN status='replied'  THEN 1 END) AS replied,
           COUNT(CASE WHEN status='skipped'  THEN 1 END) AS skipped,
           COUNT(CASE WHEN status='processing' THEN 1 END) AS processing,
           COUNT(CASE WHEN status='pending'  THEN 1 END) AS pending
    FROM replies
    WHERE discovered_at >= NOW() - INTERVAL '24 hours'
    GROUP BY platform
    ORDER BY total DESC
""")
rows = cur.fetchall()
print(f"\n[2] LAST 24H REPLIES BY PLATFORM (HTTP-lane: {', '.join(sorted(HTTP_LANE_PLATFORMS))})")
print(f"     {'platform':<10} {'total':>5} {'attrib':>7} {'rep':>5} {'skp':>5} {'prc':>5} {'pnd':>5}  notes")
for r in rows:
    plat, total, attrib, replied, skipped, proc, pend = r
    pct = (attrib / total * 100) if total else 0
    note = ""
    if plat in HTTP_LANE_PLATFORMS:
        if total == 0:
            note = "(no traffic yet)"
        elif pct < 80:
            note = f"  {WARN} only {pct:.0f}% attributed; expected ~100%"
            status = 1
        else:
            note = f"  {OK} {pct:.0f}% attributed"
    else:
        if attrib > 0:
            note = f"  {WARN} {attrib} unexpected install_id rows on a SQL-lane platform"
    print(f"     {plat:<10} {total:>5} {attrib:>7} {replied:>5} {skipped:>5} {proc:>5} {pend:>5}{note}")

# 3. Stuck in 'processing' > 30min — the canonical failure mode of a new claim path
cur = db.execute("""
    SELECT id, platform, install_id,
           EXTRACT(EPOCH FROM (NOW() - processing_at))::int AS age_sec
    FROM replies
    WHERE status='processing'
      AND processing_at IS NOT NULL
      AND processing_at < NOW() - INTERVAL '30 minutes'
    ORDER BY processing_at ASC
    LIMIT 10
""")
rows = cur.fetchall()
print("\n[3] STUCK IN 'processing' > 30min")
if not rows:
    print(f"  {OK} none")
else:
    print(f"  {WARN} {len(rows)} stuck rows (revert with: UPDATE replies SET status='pending' WHERE id IN (...))")
    for r in rows:
        rid, plat, iid, age = r
        age_disp = f"{age // 60}m" if age < 7200 else f"{age // 3600}h"
        print(f"     id={rid:<6} {plat:<8} iid={(iid or '-')[:8]}  {age_disp} ago")

# 4. Heartbeat log errors in the last 100 lines
log_path = os.path.expanduser("~/social-autoposter/skill/logs/heartbeat.log")
print("\n[4] HEARTBEAT LOG (recent FAILs)")
if os.path.exists(log_path):
    try:
        out = subprocess.check_output(["tail", "-200", log_path], text=True, timeout=5)
        fails = [ln for ln in out.splitlines() if "FAIL" in ln]
        if not fails:
            print(f"  {OK} no failures in last 200 lines")
        else:
            print(f"  {FAIL} {len(fails)} failures:")
            for ln in fails[-5:]:
                print(f"     {ln}")
            status = 1
    except Exception as e:
        print(f"  {WARN} couldn't read log: {e}")
else:
    print(f"  {WARN} log not yet created at {log_path}")

print()
sys.exit(status)
