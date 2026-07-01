#!/usr/bin/env python3
"""Single source of truth for the S4L status snapshot.

Produces the SAME dict as the MCP's buildSnapshot() (mcp/src/index.ts), but in
Python, reading directly from the stateful files plus two existing Python helpers
(setup_twitter_auth.py for X, schedule_state.py for the draft schedule) and
GitHub releases/latest (npm fallback) for the latest version.

WHY this exists: the menu bar must render with Claude / the MCP fully closed. The
MCP is a Node process tied to Claude Desktop's lifecycle, and it was the ONLY
thing computing the snapshot — so the always-on menu bar had to ask it over a
blocking loopback call, which froze the menu whenever the MCP was restarting.
Moving the compute here lets the menu bar build the snapshot itself from the
files (zero MCP dependency), while the MCP shells out to this SAME module so
there's one implementation, no divergence — the schedule_state.py pattern applied
to the whole snapshot. The source of truth is the FILES; this is just the reader.

PURE READ/COMPUTE: never writes (no onboarding-milestone telemetry, no
persistence) — the MCP keeps those side effects around this. Slow fields (X
session, latest version) are cached per-process with a TTL so a 5s menu-bar tick
that imports this module stays cheap.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

HOME = os.path.expanduser("~")


def _state_dir() -> str:
    return os.environ.get("SAPS_STATE_DIR") or os.path.join(HOME, ".social-autoposter-mcp")


def _repo_dir() -> str:
    return os.environ.get("SAPS_REPO_DIR") or os.path.join(HOME, "social-autoposter")


def _claude_cfg_dir() -> str:
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")


def _config_path() -> str:
    return os.environ.get("SAPS_CONFIG_PATH") or os.path.join(_repo_dir(), "config.json")


# Keep in sync with REQUIRED_FIELDS (mcp/src/setup.ts), QUEUE_WORKERS / UPDATER_LABEL
# / AUTOPILOT_STALL_MS (mcp/src/index.ts).
REQUIRED_FIELDS = ["name", "website", "description", "icp", "voice", "search_topics"]
# Keep in sync with PERSONA_REQUIRED_FIELDS (mcp/src/setup.ts). A personal-brand
# persona has no product website/icp by design; it is "ready" once it has the fields
# the cycle consumes (name, voice, seedable topics). Without this, a personal-brand-
# only setup can NEVER report setup_complete (any_ready requires a managed product),
# leaving the menu bar stuck on "project not set up". (2026-06-30)
PERSONA_REQUIRED_FIELDS = ["name", "description", "voice", "search_topics"]
WORKER_TASK_IDS = ("saps-phase1-query", "saps-phase2b-draft")
UPDATER_LABEL = "com.m13v.social-autoposter-update"
AUTOPILOT_STALL_MS = 180_000

# Milestones overlaid with LIVE state for display (the rest keep their ledger
# value). Mirrors the overlay in buildSnapshot().
_OVERLAY_IDS = ("runtime_ready", "x_connected", "mode_chosen", "project_ready", "tasks_scheduled")


def _read_json(path: str):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


# ---- projects (config.json + setup-state.json + REQUIRED_FIELDS) -----------
def _managed_projects():
    st = _read_json(os.path.join(_state_dir(), "setup-state.json")) or {}
    return st.get("projects") or []


def _project_status(name, cfg_projects):
    proj = next((p for p in cfg_projects if p.get("name") == name), None)
    if proj is None:
        return {"name": name, "ready": False, "missing_required": list(REQUIRED_FIELDS)}
    missing = []
    for f in REQUIRED_FIELDS:
        v = proj.get(f)
        if v is None:
            missing.append(f)
        elif isinstance(v, str) and not v.strip():
            missing.append(f)
        elif isinstance(v, (list, tuple)) and len(v) == 0:
            missing.append(f)
        elif isinstance(v, dict) and len(v) == 0:
            missing.append(f)
    return {"name": name, "ready": len(missing) == 0, "missing_required": missing}


def _projects():
    cfg = _read_json(_config_path()) or {}
    cfg_projects = cfg.get("projects") or []
    return [_project_status(n, cfg_projects) for n in _managed_projects()]


def _persona_status():
    """The personal-brand persona (config.json persona:true) as a project-status
    dict, or None when there's no persona. Validated against PERSONA_REQUIRED_FIELDS
    (a persona has no product website/icp). The persona is excluded from the managed
    scope (_managed_projects) by design, but IS what the cycle drafts in
    personal_brand mode, so it must count toward readiness."""
    cfg = _read_json(_config_path()) or {}
    persona = next((p for p in (cfg.get("projects") or []) if p.get("persona")), None)
    if persona is None:
        return None
    missing = []
    for f in PERSONA_REQUIRED_FIELDS:
        v = persona.get(f)
        if v is None:
            missing.append(f)
        elif isinstance(v, str) and not v.strip():
            missing.append(f)
        elif isinstance(v, (list, tuple)) and len(v) == 0:
            missing.append(f)
        elif isinstance(v, dict) and len(v) == 0:
            missing.append(f)
    return {
        "name": persona.get("name") or "PersonalBrand",
        "ready": len(missing) == 0,
        "missing_required": missing,
        "persona": True,
    }


# ---- runtime / mode / autopilot (all file/launchctl) -----------------------
def _runtime_ready() -> bool:
    rt = _read_json(os.path.join(_state_dir(), "runtime.json")) or {}
    py = rt.get("python")
    return bool(rt.get("ready") and py and os.path.exists(py))


def _runtime_provisioning() -> bool:
    p = _read_json(os.path.join(_state_dir(), "install-progress.json")) or {}
    return str(p.get("status") or "").lower() in ("installing", "in_progress", "running", "provisioning")


def _flags() -> dict:
    """Engagement lane flags {"personal_brand": bool, "promotion": bool}.

    Mirrors scripts/saps_mode.py get_flags(): explicit flag keys win; else map a
    legacy {"mode": ...} string; else default personal ON / promotion OFF."""
    d = _read_json(os.path.join(_state_dir(), "mode.json")) or {}
    if "personal_brand" in d or "promotion" in d:
        return {"personal_brand": bool(d.get("personal_brand")),
                "promotion": bool(d.get("promotion"))}
    m = str(d.get("mode") or "").strip()
    if m == "personal_brand":
        return {"personal_brand": True, "promotion": False}
    if m == "promotion":
        return {"personal_brand": False, "promotion": True}
    return {"personal_brand": True, "promotion": False}


def _mode() -> str:
    # Derived legacy single-mode string (personal wins when on).
    return "personal_brand" if _flags().get("personal_brand") else "promotion"


def _mode_chosen() -> bool:
    return os.path.exists(os.path.join(_state_dir(), "mode.json"))


def _autopilot_on() -> bool:
    base = os.path.join(_claude_cfg_dir(), "scheduled-tasks")
    try:
        return all(os.path.exists(os.path.join(base, t, "SKILL.md")) for t in WORKER_TASK_IDS)
    except Exception:
        return False


def _auto_update_on() -> bool:
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=10).stdout
        return any(UPDATER_LABEL in line for line in out.splitlines())
    except Exception:
        return False


def _autopilot_stalled() -> bool:
    qdir = os.path.join(_state_dir(), "claude-queue")
    ds = _read_json(os.path.join(qdir, "drain-status.json")) or {}
    try:
        if int(ds.get("consecutive_timeouts") or 0) >= 1:
            return True
    except Exception:
        pass
    oldest = None
    try:
        pend = os.path.join(qdir, "pending")
        for sub in os.listdir(pend):
            subp = os.path.join(pend, sub)
            if not os.path.isdir(subp):
                continue
            for f in os.listdir(subp):
                if not f.endswith(".json") or f.endswith(".tmp"):
                    continue
                try:
                    m = os.stat(os.path.join(subp, f)).st_mtime * 1000.0
                    if oldest is None or m < oldest:
                        oldest = m
                except Exception:
                    pass
    except Exception:
        pass
    return oldest is not None and (time.time() * 1000.0 - oldest) > AUTOPILOT_STALL_MS


# ---- schedule_state (reuse the shared module) ------------------------------
def _schedule_state() -> str:
    try:
        scripts = os.path.join(_repo_dir(), "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        import schedule_state  # noqa: E402
        return schedule_state.compute()
    except Exception:
        return "missing"


# ---- X status (setup_twitter_auth.py status), cached -----------------------
_x_cache = {"at": 0.0, "val": None}
_X_TTL = 60.0


def _x_status():
    now = time.time()
    if _x_cache["val"] is not None and now - _x_cache["at"] < _X_TTL:
        return _x_cache["val"]
    val = {"connected": False, "state": "", "handle": None}
    if _runtime_ready():
        try:
            py = os.environ.get("SAPS_PYTHON") or sys.executable or "python3"
            res = subprocess.run(
                [py, os.path.join(_repo_dir(), "scripts", "setup_twitter_auth.py"), "status"],
                capture_output=True, text=True, timeout=90,
            )
            # Mirror twitterAuth.ts::parse — JSON in the last lines of stdout.
            parsed = json.loads("\n".join(res.stdout.strip().splitlines()[-50:]))
            val = {
                "connected": bool(parsed.get("connected")),
                "state": parsed.get("state") or "",
                "handle": parsed.get("handle"),
            }
        except Exception:
            val = {"connected": False, "state": "status_unavailable", "handle": None}
    else:
        val = {"connected": False, "state": "runtime_not_ready", "handle": None}
    _x_cache.update(at=now, val=val)
    return val


# ---- version (resolveVersion + latest release + semver), cached ------------
# Latest-version SOURCE = GitHub releases/latest first, npm only as a fallback.
# This mirrors mcp/src/version.ts::latestPublishedVersion and is load-bearing:
# the .mcpb boxes that render the menu bar have NO npm on PATH (PATH is just
# /usr/bin:/bin:/usr/sbin:/sbin), so an npm-only probe always yields latest=None
# there, update_available is always False, and the "S4L ⬆" banner can never
# fire on a box — even with a new release live. The 2026-07-01 v1.6.182 fix
# closed this in version.ts only; the menu bar computes its snapshot through
# THIS module (mcp/menubar/s4l_state.py tier 1, the loopback tier was removed
# to fix a UI freeze), so the same probe must live here too. curl is at
# /usr/bin/curl on every macOS PATH. GitHub releases/latest is also the SAME
# source the box updater installs from (s4l_box_update.sh / _mcpb_update_work),
# so "update available" and "what an update installs" cannot disagree.
#
# TTL is ~1 minute: a new release must surface in the menu bar within a minute.
# That cadence is only safe because the PRIMARY probe is the plain website
# redirect (github.com/.../releases/latest 302s to /releases/tag/vX.Y.Z), which
# is NOT API-rate-limited. The api.github.com fallback allows 60 unauthenticated
# requests/hour per IP — a 1-min poll would consume that entire quota, so the
# API is only consulted when the redirect probe fails.
_ver_cache = {"at": 0.0, "latest": None, "checked": False}
_VER_TTL = 55.0

_RELEASES_LATEST_URL = "https://github.com/m13v/social-autoposter/releases/latest"
_RELEASES_LATEST_API = "https://api.github.com/repos/m13v/social-autoposter/releases/latest"


def _parse_semverish(v):
    return v if v and v[0].isdigit() and v.count(".") >= 2 else None


def _latest_from_github_redirect():
    # releases/latest already excludes drafts and prereleases. No -L: read the
    # first response's Location via %{redirect_url} and stop.
    try:
        res = subprocess.run(
            ["/usr/bin/curl", "-fsS", "-m", "10", "-o", "/dev/null",
             "-w", "%{redirect_url}", _RELEASES_LATEST_URL],
            capture_output=True, text=True, timeout=12)
        loc = (res.stdout or "").strip()
        if "/releases/tag/" not in loc:
            return None
        return _parse_semverish(loc.rsplit("/", 1)[-1].lstrip("v").strip())
    except Exception:
        return None


def _latest_from_github_api():
    try:
        res = subprocess.run(
            ["/usr/bin/curl", "-fsSL", "-m", "10",
             "-H", "Accept: application/vnd.github+json", _RELEASES_LATEST_API],
            capture_output=True, text=True, timeout=12)
        tag = (json.loads(res.stdout) or {}).get("tag_name")
        return _parse_semverish(tag.lstrip("v").strip()) if isinstance(tag, str) else None
    except Exception:
        return None


def _latest_from_npm():
    try:
        res = subprocess.run(["npm", "view", "social-autoposter", "version"],
                             capture_output=True, text=True, timeout=8)
        line = (res.stdout.strip().splitlines() or [""])[-1].strip()
        return line if line and line[0].isdigit() else None
    except Exception:
        return None


def _resolve_version() -> str:
    for p in (
        os.path.join(_repo_dir(), "mcp", "dist", "version.json"),
        os.path.join(_repo_dir(), "package.json"),
        os.path.join(_repo_dir(), "mcp", "package.json"),
    ):
        v = (_read_json(p) or {}).get("version")
        if isinstance(v, str) and v:
            return v
    return "0.0.0-unknown"


def _latest_published():
    now = time.time()
    # Cache failures (latest=None) too, like version.ts: a menu-bar tick loop
    # re-probing an unreachable/rate-limited GitHub every few seconds would burn
    # the unauthenticated API quota (60/h per IP) and lock itself out for good.
    if _ver_cache["checked"] and now - _ver_cache["at"] < _VER_TTL:
        return _ver_cache["latest"]
    latest = _latest_from_github_redirect()
    if latest is None:
        latest = _latest_from_github_api()
    if latest is None:
        latest = _latest_from_npm()
    _ver_cache.update(at=now, latest=latest, checked=True)
    return latest


def _is_newer(latest, current) -> bool:
    def norm(v):
        return [int(x) if x.isdigit() else 0 for x in str(v).split("-")[0].split("+")[0].split(".")]
    a, b = norm(latest), norm(current)
    for i in range(max(len(a), len(b))):
        x = a[i] if i < len(a) else 0
        y = b[i] if i < len(b) else 0
        if x != y:
            return x > y
    return False


# ---- onboarding ledger + live overlay --------------------------------------
def _onboarding_live(live_status):
    led = _read_json(os.path.join(_state_dir(), "onboarding-progress.json")) or {}
    ms = led.get("milestones")
    # The ledger stores milestones as a dict id->record; the snapshot exposes a
    # list. Mirror onboarding-ledger.cjs publicSnapshot() ordering via MILESTONES.
    order = ["environment_checked", "runtime_ready", "x_connected", "profile_scanned",
             "mode_chosen", "project_ready", "topics_seeded", "tasks_scheduled"]
    out = []
    if isinstance(ms, dict):
        for mid in order:
            rec = dict(ms.get(mid) or {"status": "pending", "attempts": 0})
            rec["id"] = mid
            if mid in live_status:
                rec["status"] = live_status[mid]
            out.append(rec)
    elif isinstance(ms, list):
        for rec in ms:
            rec = dict(rec)
            if rec.get("id") in live_status:
                rec["status"] = live_status[rec["id"]]
            out.append(rec)
    result = dict(led)
    result["milestones"] = out
    result["complete"] = bool(out) and all(m.get("status") == "complete" for m in out)
    return result


def compute() -> dict:
    """Build the full snapshot dict (same shape as buildSnapshot())."""
    projects = _projects()
    rt_ready = _runtime_ready()
    x = _x_status()
    mode = _mode()
    flags = _flags()
    schedule_state = _schedule_state()
    # Personal-brand-only setups have NO managed product project; the persona IS the
    # draftable "project" for the self-promo lane. Surface it as a project row when
    # that lane is on so projects_ready / setup_complete / project_ready reflect a
    # persona-only setup instead of forever reading "not set up". (2026-06-30)
    persona = _persona_status()
    if persona is not None and flags.get("personal_brand"):
        projects = projects + [persona]
    any_ready = any(p["ready"] for p in projects)
    setup_complete = rt_ready and any_ready and bool(x["connected"])

    installed = _resolve_version()
    latest = _latest_published()
    update_available = bool(latest) and _is_newer(latest, installed)

    live_status = {
        "runtime_ready": "complete" if rt_ready else "pending",
        "x_connected": "complete" if x["connected"] else "pending",
        "mode_chosen": "complete" if _mode_chosen() else "pending",
        "project_ready": "complete" if any_ready else "pending",
        "tasks_scheduled": "complete" if schedule_state == "ok" else "pending",
    }

    return {
        "projects": projects,
        "projects_total": len(projects),
        "projects_ready": sum(1 for p in projects if p["ready"]),
        "x_connected": bool(x["connected"]),
        "x_state": x["state"] or "",
        "x_handle": x["handle"],
        "autopilot_on": _autopilot_on(),
        "autopilot_stalled": setup_complete and _autopilot_stalled(),
        "schedule_state": schedule_state,
        "auto_update_on": _auto_update_on(),
        "version": installed,
        "latest_version": latest,
        "update_available": update_available,
        "runtime_ready": rt_ready,
        "runtime_provisioning": _runtime_provisioning(),
        "setup_complete": setup_complete,
        "mode": mode,
        "flags": _flags(),
        "onboarding": _onboarding_live(live_status),
    }


def main() -> int:
    try:
        print(json.dumps(compute()))
    except Exception as e:
        print(json.dumps({"_error": str(e)}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
