#!/usr/bin/env python3
"""Kill launchd-parented skill/*.sh processes that have been running too long.

Matches the hang pattern flagged in CLAUDE.md: a run-*.sh spawns `claude -p`
which blocks indefinitely (e.g. BSD grep on stale /tmp FIFOs), preventing
launchd from re-firing the job on its StartInterval.

For every kill, emits a synthetic log_run.py entry so the stuck run surfaces
as a failed job in the dashboard's job-history table (run_monitor.log), and
appends a line to skill/logs/watchdog.log for the kill trail.
"""

import os
import subprocess
import time
from pathlib import Path

REPO = Path("/Users/matthewdi/social-autoposter")
LOG_RUN_PY = REPO / "scripts" / "log_run.py"
SKILL_PATH_MARKER = "/social-autoposter/skill/"
MAX_AGE_SEC = 45 * 60
# Per-script cap overrides for pipelines that legitimately run longer than
# the 45 min global (stats.py over ~4-5k posts + rate-limit sleeps).
# Key is (script_file, platform_or_None). Lookup order: (script, platform),
# (script, None), then global MAX_AGE_SEC. Raised 2026-04-24 after the global
# 45 min cap was killing stats.sh reddit at ~90% and github-engage every 2h.
PER_SCRIPT_CAP_SEC = {
    ("github-engage.sh", None): 120 * 60,
    ("stats.sh", "reddit"): 120 * 60,
    # 2026-04-27: extend 120 min cap to remaining stats / audit / link-edit jobs.
    # 45 min was killing audit-twitter mid-run and starving link-edit-* of time
    # to actually post replies + verify SEO deploys.
    ("stats.sh", "twitter"): 120 * 60,
    ("stats.sh", "linkedin"): 120 * 60,
    ("stats.sh", "moltbook"): 120 * 60,
    ("audit.sh", None): 120 * 60,
    ("audit-twitter.sh", None): 120 * 60,
    ("audit-reddit.sh", None): 120 * 60,
    ("audit-moltbook.sh", None): 120 * 60,
    ("audit-linkedin.sh", None): 120 * 60,
    ("audit-reddit-resurrect.sh", None): 120 * 60,
    ("audit-dm-staleness.sh", None): 120 * 60,
    # link-edit-twitter.sh retired 2026-05-07 (link embedded in primary reply
    # by twitter_post_plan.py + suffix wrap; no separate sweep needed).
    ("link-edit-reddit.sh", None): 120 * 60,
    # link-edit-linkedin.sh retired 2026-05-29 (link embedded in original comment
    # at composition by run-linkedin.sh + engage-linkedin.sh; no separate sweep).
    ("link-edit-moltbook.sh", None): 120 * 60,
    ("link-edit-github.sh", None): 120 * 60,
    ("precompute-stats.sh", None): 120 * 60,
    # 2026-05-10: bumped 60 min → 90 min → 120 min for all post-* runners.
    # Rationale: a single cycle = discover claude (~5 min) + ripen sleep
    # (30 min hardcoded) + draft claude (~9-10 min) + post phase (~12-15 min
    # for 4 posts × 3-min inter-post sleep) + phase0/salvage overhead, with
    # extra headroom for browser-lock contention with peer pipelines, slow
    # CDP launches, and platform rate-limit retries. 120 min keeps premature
    # kills from costing us drafted-but-unposted work; the lease-based
    # reddit-browser lock + draft-aware salvage gate (both shipped 2026-05-10)
    # mean a kill at the cap loses at most one cycle's posting work, never
    # the drafts themselves. All post-runners share this cap so behavior is
    # uniform across platforms.
    ("run-reddit-search.sh", None): 120 * 60,
    ("run-reddit-threads.sh", None): 120 * 60,
    # 2026-05-19: raised 120 → 180 min after two consecutive cycles died at the
    # 120 cap mid-Phase-2b-gen with `phase2b_silent:1`. Combined with
    # twitter_gen_links.GEN_TIMEOUT_SEC drop (3000 → 900s) and
    # MAX_AB_HITS_PER_CYCLE cap (4), gen phase now has a 60min worst-case
    # ceiling, leaving 120min for scan + T1 sleep + prep + post.
    ("run-twitter-cycle.sh", None): 180 * 60,
    # 2026-05-28: launchd spawns the singleton WRAPPER, not the inner cycle, so
    # the wrapper is the ppid==1 process main() matches; the inner cycle is its
    # child (ppid != 1) and gets skipped. Without this entry the wrapper fell back
    # to the 45 min global and killed healthy Variant-A cycles (20 min ripen sleep
    # + scan + draft + gen + post routinely exceed 45 min) mid-Phase-2b, stamped
    # phase2b_silent. Mirror the inner cycle's 180 min cap. The
    # ("run-twitter-cycle.sh", None) entry above is now effectively unreachable but
    # kept for clarity / in case the wrapper is ever bypassed.
    ("run-twitter-cycle-singleton.sh", None): 180 * 60,
    ("run-linkedin.sh", None): 120 * 60,
    ("run-moltbook.sh", None): 120 * 60,
    ("run-github.sh", None): 120 * 60,
}
WATCHDOG_LOG = REPO / "skill" / "logs" / "watchdog.log"
RUN_MONITOR_LOG = REPO / "skill" / "logs" / "run_monitor.log"
TRAP_GRACE_SEC = 5


def cap_for(script_file, platform):
    return (
        PER_SCRIPT_CAP_SEC.get((script_file, platform))
        or PER_SCRIPT_CAP_SEC.get((script_file, None))
        or MAX_AGE_SEC
    )

# Map skill/*.sh filename -> script label used by the script's own log_run.py
# calls. Keeps dashboard job-history grouping consistent (e.g. a killed
# run-twitter-cycle.sh shows under the same "Post · Twitter" row as a normal
# post_twitter run). Unknown scripts fall through to a watchdog_killed_* label.
# Shared scripts (stats.sh, audit.sh, octolens.sh, engage.sh) dispatch on
# `--platform X`; the watchdog appends the platform to the label at kill time.
SHARED_SCRIPT_PREFIX = {
    "stats.sh": "stats_",
    "audit.sh": "audit-",
    "octolens.sh": "octolens-",
    "engage.sh": "engage_",
}

SCRIPT_LABELS = {
    "run-twitter-cycle.sh": "post_twitter",
    # 2026-05-28: wrapper is the launchd-parented process now; label its kills
    # under the same post_twitter dashboard row as a normal cycle.
    "run-twitter-cycle-singleton.sh": "post_twitter",
    "run-linkedin.sh": "post_linkedin",
    "run-moltbook.sh": "post_moltbook",
    "run-reddit-threads.sh": "post_reddit",
    "run-reddit-search.sh": "post_reddit",
    "run-github.sh": "post_github",
    "run-scan-moltbook-replies.sh": "scan_moltbook_replies",
    "engage-reddit.sh": "engage_reddit",
    "scan-twitter-followups.sh": "scan_twitter_followups",
    "engage-twitter.sh": "engage_twitter",
    "engage-linkedin.sh": "engage_linkedin",
    "engage-moltbook.sh": "engage_moltbook",
    "engage.sh": "engage_reddit",
    "github-engage.sh": "engage_github",
    "engage-dm-replies-twitter.sh": "dm_replies_twitter",
    "engage-dm-replies-linkedin.sh": "dm_replies_linkedin",
    "engage-dm-replies-reddit.sh": "dm_replies_reddit",
    "engage-dm-replies.sh": "dm_replies_reddit",
    "dm-outreach-twitter.sh": "dm_outreach_twitter",
    "dm-outreach-linkedin.sh": "dm_outreach_linkedin",
    "dm-outreach-reddit.sh": "dm_outreach_reddit",
    # link-edit-twitter.sh retired 2026-05-07; link-edit-linkedin.sh retired 2026-05-29.
    "link-edit-moltbook.sh": "link_edit_moltbook",
    "link-edit-reddit.sh": "link_edit_reddit",
    "link-edit-github.sh": "link_edit_github",
    "audit-twitter.sh": "audit-twitter",
    "audit-linkedin.sh": "audit-linkedin",
    "audit-moltbook.sh": "audit-moltbook",
    "audit-reddit.sh": "audit-reddit",
    "audit-reddit-resurrect.sh": "audit-reddit-resurrect",
    "audit-dm-staleness.sh": "audit-dm-staleness",
    "octolens-twitter.sh": "octolens-twitter",
    "octolens-linkedin.sh": "octolens-linkedin",
    "octolens-reddit.sh": "octolens-reddit",
    "stats-twitter.sh": "stats_twitter",
    "stats-linkedin.sh": "stats_linkedin",
    "stats-moltbook.sh": "stats_moltbook",
    "stats-reddit.sh": "stats_reddit",
}


def watchdog_log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    line = f"{ts} | {msg}\n"
    WATCHDOG_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(WATCHDOG_LOG, "a") as f:
        f.write(line)
    print(line, end="")


def list_skill_shell_processes():
    """Return [(pid, ppid, etimes_sec, script_filename, platform)] for skill/*.sh bash procs."""
    res = subprocess.run(
        ["ps", "-A", "-o", "pid=,ppid=,etime=,command="],
        capture_output=True, text=True, check=True,
    )
    procs = []
    for raw in res.stdout.splitlines():
        parts = raw.strip().split(None, 3)
        if len(parts) < 4:
            continue
        pid_s, ppid_s, etime_s, command = parts
        try:
            pid = int(pid_s)
            ppid = int(ppid_s)
        except ValueError:
            continue
        if SKILL_PATH_MARKER not in command:
            continue
        script_name = None
        tokens = command.split()
        for tok in tokens:
            if tok.endswith(".sh") and SKILL_PATH_MARKER in tok:
                script_name = os.path.basename(tok)
                break
        if not script_name:
            continue
        etimes = _parse_etime(etime_s)
        if etimes is None:
            continue
        platform = None
        if "--platform" in tokens:
            idx = tokens.index("--platform")
            if idx + 1 < len(tokens):
                platform = tokens[idx + 1]
        procs.append((pid, ppid, etimes, script_name, platform))
    return procs


def _parse_etime(s: str):
    """Parse ps etime format ([[DD-]HH:]MM:SS) into seconds."""
    try:
        days = 0
        if "-" in s:
            d, s = s.split("-", 1)
            days = int(d)
        parts = s.split(":")
        parts = [int(p) for p in parts]
        if len(parts) == 2:
            h, m, sec = 0, parts[0], parts[1]
        elif len(parts) == 3:
            h, m, sec = parts
        else:
            return None
        return days * 86400 + h * 3600 + m * 60 + sec
    except Exception:
        return None


def descendants(pid: int):
    out = [pid]
    i = 0
    while i < len(out):
        try:
            r = subprocess.run(
                ["pgrep", "-P", str(out[i])],
                capture_output=True, text=True,
            )
            for tok in r.stdout.split():
                if tok.isdigit():
                    out.append(int(tok))
        except Exception:
            pass
        i += 1
    return out


def kill_tree(root_pid: int) -> list:
    pids = descendants(root_pid)
    for p in reversed(pids):
        try:
            os.kill(p, 15)
        except ProcessLookupError:
            pass
        except PermissionError:
            pass
    time.sleep(TRAP_GRACE_SEC)
    for p in reversed(pids):
        try:
            os.kill(p, 9)
        except ProcessLookupError:
            pass
        except PermissionError:
            pass
    return pids


def resolve_label(script_file, platform):
    prefix = SHARED_SCRIPT_PREFIX.get(script_file)
    if prefix and platform:
        return prefix + platform
    if script_file in SCRIPT_LABELS:
        return SCRIPT_LABELS[script_file]
    return "watchdog_killed_" + script_file.replace(".sh", "").replace("-", "_")


def recent_emit_exists(label, since_epoch):
    """True if run_monitor.log has an entry for `label` at or after since_epoch.

    The bash EXIT trap in scripts like run-twitter-cycle.sh runs log_run.py on
    SIGTERM, so a fresh entry here means the watchdog's own emit would be a
    duplicate.
    """
    try:
        with open(RUN_MONITOR_LOG) as f:
            tail = f.readlines()[-80:]
    except FileNotFoundError:
        return False
    for raw in tail:
        parts = raw.split("|", 2)
        if len(parts) < 2:
            continue
        ts_str = parts[0].strip()
        script = parts[1].strip()
        if script != label:
            continue
        try:
            ts = time.mktime(time.strptime(ts_str, "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            continue
        if ts >= since_epoch:
            return True
    return False


def emit_job_log(label, elapsed_sec):
    subprocess.run(
        [
            "python3", str(LOG_RUN_PY),
            "--script", label,
            "--posted", "0",
            "--skipped", "0",
            "--failed", "1",
            "--cost", "0",
            "--elapsed", str(elapsed_sec),
        ],
        check=False,
    )


def main() -> None:
    procs = list_skill_shell_processes()
    for pid, ppid, etimes, script_file, platform in procs:
        if ppid != 1:
            continue
        cap = cap_for(script_file, platform)
        if etimes < cap:
            continue
        label = resolve_label(script_file, platform)
        plat_tag = f" platform={platform}" if platform else ""
        watchdog_log(
            f"KILL {script_file}{plat_tag} pid={pid} elapsed={etimes}s cap={cap}s label={label}"
        )
        kill_started = time.time() - 1
        killed = kill_tree(pid)
        watchdog_log(f"  killed pids: {killed}")
        if recent_emit_exists(label, kill_started):
            watchdog_log(f"  script trap already logged {label} — skipping watchdog emit")
        else:
            emit_job_log(label, etimes)


if __name__ == "__main__":
    main()
