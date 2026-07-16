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

# SAPS_->S4L_ env mirror (brand rename 2026-07-03): old launchd plists and
# scheduled-task prompts still export SAPS_*; this process reads S4L_*.
import s4l_env  # noqa: E402  (lives next to this file in scripts/)

s4l_env.mirror()

HOME = os.path.expanduser("~")


def _state_dir() -> str:
    return os.environ.get("S4L_STATE_DIR") or os.path.join(HOME, ".social-autoposter-mcp")


def _repo_dir() -> str:
    return os.environ.get("S4L_REPO_DIR") or os.path.join(HOME, "social-autoposter")


def _claude_cfg_dir() -> str:
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")


def _config_path() -> str:
    return os.environ.get("S4L_CONFIG_PATH") or os.path.join(_repo_dir(), "config.json")


# Keep in sync with REQUIRED_FIELDS (mcp/src/setup.ts), QUEUE_WORKERS / UPDATER_LABEL
# / AUTOPILOT_STALL_MS (mcp/src/index.ts).
REQUIRED_FIELDS = ["name", "website", "description", "icp", "voice", "search_topics"]
# Keep in sync with PERSONA_REQUIRED_FIELDS (mcp/src/setup.ts). A personal-brand
# persona has no product website/icp by design; it is "ready" once it has the fields
# the cycle consumes (name, voice, seedable topics). Without this, a personal-brand-
# only setup can NEVER report setup_complete (any_ready requires a managed product),
# leaving the menu bar stuck on "project not set up". (2026-06-30)
PERSONA_REQUIRED_FIELDS = ["name", "description", "voice", "search_topics"]
# Current installs run ONE universal worker task (s4l-worker); the phase pair
# is the retired legacy shape. Checking ONLY the legacy pair made every current
# install read "autopilot off" forever (Karol, 2026-07-03: worker fired every
# minute while this check reported the tasks missing). Keep in sync with
# scripts/schedule_state.py and mcp/menubar/s4l_menubar.py.
CURRENT_WORKER_TASK_IDS = ("s4l-worker", "saps-worker")
LEGACY_WORKER_TASK_IDS = ("saps-phase1-query", "saps-phase2b-draft")
UPDATER_LABEL = "com.m13v.social-autoposter-update"
AUTOPILOT_STALL_MS = 1_200_000

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

    Mirrors scripts/s4l_mode.py get_flags(): explicit flag keys win; else map a
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


def _personal_brand_share() -> float:
    """Share of both-lanes-on cycles that run personal_brand (0.0-1.0).

    Mirrors scripts/s4l_mode.py get_split(): missing/invalid key -> 0.5."""
    d = _read_json(os.path.join(_state_dir(), "mode.json")) or {}
    try:
        share = float(d.get("personal_brand_share"))
    except (TypeError, ValueError):
        return 0.5
    return min(1.0, max(0.0, share))


def _mode_chosen() -> bool:
    return os.path.exists(os.path.join(_state_dir(), "mode.json"))


def _autopilot_on() -> bool:
    base = os.path.join(_claude_cfg_dir(), "scheduled-tasks")
    try:
        if any(
            os.path.exists(os.path.join(base, t, "SKILL.md"))
            for t in CURRENT_WORKER_TASK_IDS
        ):
            return True
        return all(
            os.path.exists(os.path.join(base, t, "SKILL.md"))
            for t in LEGACY_WORKER_TASK_IDS
        )
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
            py = os.environ.get("S4L_PYTHON") or sys.executable or "python3"
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


# ---- Reddit status (setup_reddit_auth.py status), cached --------------------
# Mirrors _x_status for the optional Reddit platform. The reddit status command
# is deliberately CHEAP (reddit_session cookie presence over CDP or on-disk
# profile check; it never navigates the shared harness tab), so polling it on
# the same TTL as X is safe.
_reddit_cache = {"at": 0.0, "val": None}


def _reddit_status():
    now = time.time()
    if _reddit_cache["val"] is not None and now - _reddit_cache["at"] < _X_TTL:
        return _reddit_cache["val"]
    val = {"connected": False, "state": "", "username": None}
    if _runtime_ready():
        try:
            py = os.environ.get("S4L_PYTHON") or sys.executable or "python3"
            res = subprocess.run(
                [py, os.path.join(_repo_dir(), "scripts", "setup_reddit_auth.py"), "status"],
                capture_output=True, text=True, timeout=90,
            )
            parsed = json.loads("\n".join(res.stdout.strip().splitlines()[-50:]))
            val = {
                "connected": bool(parsed.get("connected")),
                "state": parsed.get("state") or "",
                "username": parsed.get("username"),
            }
        except Exception:
            val = {"connected": False, "state": "status_unavailable", "username": None}
    else:
        val = {"connected": False, "state": "runtime_not_ready", "username": None}
    _reddit_cache.update(at=now, val=val)
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
# Probe order (measured 2026-07-01 releasing v1.6.188):
#   1. api.github.com releases/latest with a CONDITIONAL request (If-None-Match).
#      The API reflects a new release near-instantly, and GitHub does NOT count
#      304 responses against the unauthenticated 60/h-per-IP quota, so a 1-min
#      cadence is quota-free between releases (each new release costs one 200).
#      A plain (unconditional) 1-min API poll would burn the whole quota.
#   2. The website redirect (github.com/.../releases/latest 302s to
#      /releases/tag/vX.Y.Z): un-rate-limited fallback, but GitHub's web tier
#      lagged the API by ~2 minutes after the release, so it is not primary.
#   3. npm (dev machines only; boxes have no npm).
#
# CHANNEL (2026-07-02): a box on the `staging` channel tracks the newest release
# OVERALL (prereleases included), resolved from the releases LIST endpoint,
# instead of releases/latest (which excludes prereleases). The `stable` path is
# byte-for-byte the historical behavior. Keep resolution + the rc-aware compare
# in lockstep with mcp/src/version.ts.
_ver_cache = {"at": 0.0, "latest": None, "tag": None, "channel": None, "checked": False}
_VER_TTL = 55.0

# SHARED CROSS-PROCESS CACHE (2026-07-13): <state dir>/latest-release.json.
# Every surface that resolves the newest release (this module, mcp/src/
# version.ts, scripts/s4l_box_update.sh) reads and writes THIS one file, so a
# box makes at most one real GitHub probe per _SHARED_TTL no matter how many
# short-lived processes spin up (MCP servers respawn per s4l-worker session,
# buildSnapshot shells this module as a fresh subprocess, the menu bar ticks).
# The persisted ETag makes even those probes quota-free between releases (304s
# do not count against the anonymous 60/h-per-IP quota, and before this cache
# each process held its OWN in-process ETag that died with the process, so
# every respawn paid a full 200). Added after the box's aggregate probing
# (~80-100 req/h across processes) burned the quota on 2026-07-13 and silenced
# the update banner. Failures (version=None) are cached too, same rationale as
# _ver_cache. Keep the file shape in lockstep with version.ts::SharedCache:
# {"at": <epoch s>, "channel": ..., "version": ..., "tag": ..., "etag": ...}
_SHARED_TTL = 120.0  # banner latency ceiling: a new release surfaces within ~2 min.
# Safe because probes are conditional (If-None-Match): with a persisted ETag a
# probe is a free 304, so frequency barely matters. The TTL is the worst-case
# request rate for the ETAG-LESS modes (staging list-endpoint fallback writes
# etag=null): 120s -> 30 req/h, half the anonymous 60/h-per-IP quota. Do NOT
# drop to 60s: etag-less probing would then consume the ENTIRE quota, which is
# the exact 2026-07-13 failure mode this cache was built to prevent.
_PROBE_LOCK_STALE = 30.0


def _shared_cache_path():
    return os.path.join(_state_dir(), "latest-release.json")


def _read_shared_cache(channel):
    d = _read_json(_shared_cache_path())
    if not isinstance(d, dict) or d.get("channel") != channel:
        return None
    if not isinstance(d.get("at"), (int, float)):
        return None
    return {
        "at": float(d["at"]),
        "channel": channel,
        "version": d.get("version") if isinstance(d.get("version"), str) else None,
        "tag": d.get("tag") if isinstance(d.get("tag"), str) else None,
        "etag": d.get("etag") if isinstance(d.get("etag"), str) else None,
    }


def _write_shared_cache(channel, version, tag, etag):
    try:
        os.makedirs(_state_dir(), exist_ok=True)
        p = _shared_cache_path()
        tmp = "%s.tmp.%d" % (p, os.getpid())
        with open(tmp, "w") as f:
            json.dump({"at": time.time(), "channel": channel,
                       "version": version, "tag": tag, "etag": etag}, f)
        os.replace(tmp, p)
    except Exception:
        pass  # best effort; worst case another process re-probes


def _try_probe_lock():
    """Single-flight guard so N concurrent processes with an expired shared
    cache don't all probe at once. Returns (acquired, lock_path). acquired is
    False ONLY when another probe holds a FRESH lock (< _PROBE_LOCK_STALE s);
    any other failure returns (True, None) so we probe anyway rather than go
    blind on a broken state dir."""
    lock = os.path.join(_state_dir(), "latest-release.lock")
    try:
        os.makedirs(_state_dir(), exist_ok=True)
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return True, lock
    except FileExistsError:
        try:
            if time.time() - os.path.getmtime(lock) > _PROBE_LOCK_STALE:
                os.unlink(lock)  # stale: holder died mid-probe
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                return True, lock
        except Exception:
            pass
        return False, None
    except Exception:
        return True, None

_RELEASES_LATEST_URL = "https://github.com/m13v/s4l/releases/latest"
_RELEASES_LATEST_API = "https://api.github.com/repos/m13v/s4l/releases/latest"
# Staging resolves the newest release OVERALL from the releases LIST, since
# releases/latest deliberately excludes prereleases.
_RELEASES_LIST_API = (
    "https://api.github.com/repos/m13v/s4l/releases?per_page=30"
)


def _channel():
    """Release channel for this box (stable|staging). Prefer the sibling
    s4l_channel module; fall back to reading the marker directly so snapshot has
    no hard import-order dependency. Unknown/absent = stable (fail-safe)."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import s4l_channel  # noqa: E402
        return s4l_channel.read_channel()
    except Exception:
        v = (_read_json(os.path.join(_state_dir(), "channel.json")) or {}).get("channel")
        return v if v in ("stable", "staging") else "stable"


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


# Conditional-request state lives in the SHARED cache file (latest-release.json)
# so the ETag survives process boundaries: short-lived shell-outs used to pay a
# full 200 per process; now every probe sends If-None-Match and gets a free 304
# between releases.
def _curl_conditional(url, etag):
    """GET url with optional If-None-Match. Returns (status, new_etag, body)."""
    args = ["/usr/bin/curl", "-sS", "-m", "10",
            "-H", "Accept: application/vnd.github+json"]
    if etag:
        args += ["-H", "If-None-Match: %s" % etag]
    args += ["-w", "\n__CURL_STATUS__:%{http_code}\n__CURL_ETAG__:%header{etag}", url]
    res = subprocess.run(args, capture_output=True, text=True, timeout=12)
    status, new_etag, body = 0, None, []
    for line in (res.stdout or "").splitlines():
        if line.startswith("__CURL_STATUS__:"):
            status = int(line.split(":", 1)[1].strip() or 0)
        elif line.startswith("__CURL_ETAG__:"):
            new_etag = line.split(":", 1)[1].strip() or None
        else:
            body.append(line)
    return status, new_etag, "\n".join(body)


def _latest_from_github_api(etag=None, cached=None):
    """Stable probe (releases/latest). Returns (version, etag). On 304 serves
    the caller-supplied cached version with the same etag; If-None-Match is only
    sent when there IS a cached value to serve."""
    try:
        status, new_etag, body = _curl_conditional(
            _RELEASES_LATEST_API, etag if cached else None)
        if status == 304:
            return cached, etag
        if status != 200:
            return None, None
        tag = (json.loads(body) or {}).get("tag_name")
        v = _parse_semverish(tag.lstrip("v").strip()) if isinstance(tag, str) else None
        return (v, new_etag) if v else (None, None)
    except Exception:
        return None, None


def _latest_from_github_list_staging(etag=None, cached=None):
    """Staging channel: newest release OVERALL (prereleases included) from the
    releases LIST endpoint. Returns (version, tag, etag) or (None, None, None).
    Drafts are skipped. 'Newest' is by the rc-aware key so 1.6.193 outranks
    1.6.193-rc.N and rc.2 outranks rc.1. On 304 serves the caller-supplied
    cached (version, tag) with the same etag."""
    try:
        cached_v, cached_tag = cached if isinstance(cached, tuple) else (None, None)
        status, new_etag, body = _curl_conditional(
            _RELEASES_LIST_API, etag if (cached_v and cached_tag) else None)
        if status == 304:
            return cached_v, cached_tag, etag
        if status != 200:
            return None, None, None
        rels = json.loads(body or "[]")
        if not isinstance(rels, list):
            return None, None, None
        best_v, best_tag, best_key = None, None, None
        for r in rels:
            if not isinstance(r, dict) or r.get("draft"):
                continue
            tag = r.get("tag_name")
            if not isinstance(tag, str):
                continue
            v = _parse_semverish(tag.lstrip("v").strip())
            if not v:
                continue
            k = _ver_key(v)
            if best_key is None or k > best_key:
                best_v, best_tag, best_key = v, tag, k
        if best_v is None:
            return None, None, None
        return best_v, best_tag, new_etag
    except Exception:
        return None, None, None


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


def _latest_published(channel=None):
    """(version, tag) for the newest release on this box's channel. The tag is
    what the staging download URL is built from; stable callers can ignore it and
    use releases/latest/download. Two cache layers, both keyed by channel so a
    mid-process flip re-probes instead of serving the other channel's value:
      1. in-process _ver_cache (55s) — spares the file read on tight tick loops;
      2. shared latest-release.json (_SHARED_TTL) — spares the NETWORK probe
         across ALL processes on the box.
    Failures (latest=None) are cached in both, like version.ts: a tick loop
    re-probing an unreachable/rate-limited GitHub every few seconds would burn
    the unauthenticated API quota (60/h per IP) and lock itself out for good."""
    if channel is None:
        channel = _channel()
    now = time.time()
    if (_ver_cache["checked"] and _ver_cache["channel"] == channel
            and now - _ver_cache["at"] < _VER_TTL):
        return _ver_cache["latest"], _ver_cache["tag"]
    shared = _read_shared_cache(channel)
    if shared and now - shared["at"] < _SHARED_TTL:
        _ver_cache.update(at=now, latest=shared["version"], tag=shared["tag"],
                          channel=channel, checked=True)
        return shared["version"], shared["tag"]
    acquired, lock = _try_probe_lock()
    if not acquired:
        # Another process is probing right now; serve the stale shared value
        # (or None) instead of doubling the request. Short in-process TTL means
        # we pick up its fresh result within a minute.
        latest = shared["version"] if shared else None
        tag = shared["tag"] if shared else None
        _ver_cache.update(at=now, latest=latest, tag=tag, channel=channel, checked=True)
        return latest, tag
    etag = shared["etag"] if shared else None
    try:
        if channel == "staging":
            latest, tag, new_etag = _latest_from_github_list_staging(
                etag, (shared["version"], shared["tag"]) if shared else None)
            # Fallback to the stable probes if the list endpoint fails, so a
            # staging box degrades to "at least track stable" than going blind.
            # new_etag stays None: a releases/latest etag must never be replayed
            # against the LIST endpoint on the next staging probe.
            if latest is None:
                latest, _ = _latest_from_github_api()
                new_etag = None
                if latest is None:
                    latest = _latest_from_github_redirect()
                tag = ("v" + latest) if latest else None
        else:
            latest, new_etag = _latest_from_github_api(
                etag, shared["version"] if shared else None)
            if latest is None:
                latest, new_etag = _latest_from_github_redirect(), None
            if latest is None:
                latest = _latest_from_npm()
            tag = ("v" + latest) if latest else None
        _write_shared_cache(channel, latest, tag, new_etag)
    finally:
        if lock:
            try:
                os.unlink(lock)
            except Exception:
                pass
    _ver_cache.update(at=now, latest=latest, tag=tag, channel=channel, checked=True)
    return latest, tag


# Precedence key for an rc-aware semver compare, matching mcp/src/version.ts::
# verKey. A full release outranks any prerelease of the SAME core version
# (1.6.193 > 1.6.193-rc.2 > 1.6.193-rc.1). For stable (no prereleases ever
# compared) this reduces to a plain numeric core compare, so behavior there is
# unchanged.
def _ver_key(v):
    import re
    s = str(v).strip().lstrip("v")
    core, _, pre = s.partition("-")
    core = core.split("+", 1)[0]
    nums = [int(x) if x.isdigit() else 0 for x in core.split(".")]
    while len(nums) < 3:
        nums.append(0)
    if not pre:
        return (nums[0], nums[1], nums[2], 1, 0)
    m = re.findall(r"\d+", pre)
    return (nums[0], nums[1], nums[2], 0, int(m[-1]) if m else 0)


def _is_newer(latest, current) -> bool:
    return _ver_key(latest) > _ver_key(current)


def version_status() -> dict:
    """Version/update fields ONLY, independent of the rest of compute(). compute()
    calls _x_status() first, which can block up to 90s in the setup_twitter_auth.py
    subprocess (CDP to Chrome) whenever a scan job is holding the browser busy —
    that freezes update_available/latest_version on an otherwise-busy box even
    though the GitHub check itself is a ~1s curl. Callers that want the "Update
    available" banner to stay live regardless of X/runtime state should poll this
    directly instead of waiting on the full compute()."""
    installed = _resolve_version()
    channel = _channel()
    latest, latest_tag = _latest_published(channel)
    return {
        "version": installed,
        "channel": channel,
        "latest_version": latest,
        "latest_tag": latest_tag,
        "update_available": bool(latest) and _is_newer(latest, installed),
    }


# ---- onboarding ledger + live overlay --------------------------------------
def _onboarding_live(live_status):
    led = _read_json(os.path.join(_state_dir(), "onboarding-progress.json")) or {}
    ms = led.get("milestones")
    # The ledger stores milestones as a dict id->record; the snapshot exposes a
    # list. Mirror onboarding-ledger.cjs publicSnapshot() ordering via MILESTONES.
    order = ["environment_checked", "runtime_ready", "x_connected", "profile_scanned",
             "mode_chosen", "project_ready", "topics_seeded", "tasks_scheduled"]
    # Optional reddit milestones render only once touched; they never gate
    # completion (mirror of onboarding-ledger.cjs OPTIONAL_MILESTONES).
    optional = ["reddit_connected", "reddit_verified"]

    def _st(mid):
        rec = ms.get(mid) if isinstance(ms, dict) else None
        st = (rec or {}).get("status", "pending")
        return live_status.get(mid, st)

    # Any-of platform completion (mirror of onboarding-ledger.cjs
    # milestoneSatisfied): a required X milestone also counts when its Reddit
    # counterpart is complete, and profile_scanned (an X-account scan) is
    # required only while X is the connected platform.
    def _satisfied(mid):
        if _st(mid) == "complete":
            return True
        if mid == "x_connected":
            return _st("reddit_connected") == "complete"
        if mid == "profile_scanned":
            return _st("x_connected") != "complete" and _st("reddit_connected") == "complete"
        return False

    out = []
    if isinstance(ms, dict):
        for mid in order:
            # Omit a pristine-pending required row that the other platform
            # satisfies (reddit-only install), same as the ledger snapshot.
            if _st(mid) == "pending" and _satisfied(mid):
                continue
            rec = dict(ms.get(mid) or {"status": "pending", "attempts": 0})
            rec["id"] = mid
            rec["status"] = _st(mid)
            out.append(rec)
        for mid in optional:
            if _st(mid) == "pending":
                continue
            rec = dict(ms.get(mid) or {"status": "pending", "attempts": 0})
            rec["id"] = mid
            rec["status"] = _st(mid)
            rec["optional"] = True
            out.append(rec)
    elif isinstance(ms, list):
        for rec in ms:
            rec = dict(rec)
            if rec.get("id") in live_status:
                rec["status"] = live_status[rec["id"]]
            out.append(rec)
    result = dict(led)
    result["milestones"] = out
    if isinstance(ms, dict):
        result["complete"] = all(_satisfied(mid) for mid in order)
    else:
        result["complete"] = bool(out) and all(m.get("status") == "complete" for m in out)
    # Blocker suppression (mirror of onboarding-ledger.cjs publicSnapshot): a
    # blocker on an optional reddit milestone (failed reddit attempt on an X
    # box) or on a milestone the other platform satisfies (failed X attempt on
    # a reddit-only box) must not flag attention on the dashboard.
    blocker = result.get("current_blocker")
    if isinstance(blocker, dict):
        bmid = blocker.get("milestone")
        if bmid in optional:
            result["current_blocker"] = None
        elif isinstance(ms, dict) and bmid in order and _satisfied(bmid):
            result["current_blocker"] = None
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
    # Any-of platform completion (2026-07-15): setup is complete with AT LEAST
    # ONE platform connected. X stays the default suggestion; a reddit-only
    # install must not read "not set up" forever.
    reddit = _reddit_status()
    setup_complete = rt_ready and any_ready and (bool(x["connected"]) or bool(reddit["connected"]))

    installed = _resolve_version()
    channel = _channel()
    latest, latest_tag = _latest_published(channel)
    update_available = bool(latest) and _is_newer(latest, installed)

    live_status = {
        "runtime_ready": "complete" if rt_ready else "pending",
        "x_connected": "complete" if x["connected"] else "pending",
        "mode_chosen": "complete" if _mode_chosen() else "pending",
        "project_ready": "complete" if any_ready else "pending",
        "tasks_scheduled": "complete" if schedule_state == "ok" else "pending",
    }
    # Live reddit overlay only when connected: never force a pending overlay
    # onto a ledger-complete reddit milestone from a transient status miss.
    if reddit["connected"]:
        live_status["reddit_connected"] = "complete"

    return {
        "projects": projects,
        "projects_total": len(projects),
        "projects_ready": sum(1 for p in projects if p["ready"]),
        "x_connected": bool(x["connected"]),
        "x_state": x["state"] or "",
        "x_handle": x["handle"],
        "reddit_connected": bool(reddit["connected"]),
        "reddit_state": reddit["state"] or "",
        "reddit_username": reddit["username"],
        "autopilot_on": _autopilot_on(),
        "autopilot_stalled": setup_complete and _autopilot_stalled(),
        "schedule_state": schedule_state,
        "auto_update_on": _auto_update_on(),
        "version": installed,
        "latest_version": latest,
        "latest_tag": latest_tag,
        "channel": channel,
        "update_available": update_available,
        "runtime_ready": rt_ready,
        "runtime_provisioning": _runtime_provisioning(),
        "setup_complete": setup_complete,
        "mode": mode,
        "flags": _flags(),
        "personal_brand_share": _personal_brand_share(),
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
