"""Data layer for the S4L menu bar app.

Pure stdlib (no third-party deps; rumps lives only in s4l_menubar.py). Two
sources, in priority order:

  1. The MCP server's loopback panel server, when Claude Desktop is running.
     panel-endpoint.json (written by the server at boot) records its url; we
     POST /tool/<name> to replay the exact same tool handlers the in-chat
     dashboard uses. This gives the full, live snapshot (projects, X handle,
     stats) with zero logic duplication.

  2. Direct reads of the owned state dir, when Claude Desktop is closed. The
     onboarding ledger (onboarding-progress.json) and runtime.json are plain
     files, so setup progress + the current blocker (State B, the whole point
     of the menu bar during onboarding) are available with nothing running.

Everything is best-effort: any failure degrades to "unknown / open Claude".
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

# SAPS_->S4L_ env mirror (brand rename 2026-07-03): old launchd plists still
# export SAPS_*; this module reads S4L_* (STATE_DIR / REPO_DIR / ACTIVITY_TTL).
# The repo dir must be read tolerantly INLINE (old name included) because the
# mirror module itself lives in $REPO/scripts and isn't importable before the
# sys.path insertion below. Best-effort: failure degrades to defaults.
_repo_for_env = os.environ.get("S4L_REPO_DIR") or os.environ.get("SAPS_REPO_DIR")
if _repo_for_env:
    _scripts_for_env = os.path.join(_repo_for_env, "scripts")
    if _scripts_for_env not in sys.path:
        sys.path.insert(0, _scripts_for_env)
try:
    import s4l_env  # noqa: E402

    s4l_env.mirror()
except Exception:
    pass

# Serializes read-modify-write on approved-queue.json. The menu bar's main thread
# (approve click / restart resume) and the post-worker thread (status updates)
# both mutate it; without this a concurrent interleave would drop an approval.
_approved_lock = threading.Lock()

# Mirrors shared/onboarding-ledger.cjs MILESTONES (same order). The ledger's
# OPTIONAL milestones (reddit_connected / reddit_verified) are deliberately NOT
# listed here, same as x_verified: this list drives the offline completeness
# check and the step list, and optional platform milestones must never make an
# X-only box read as "Setting up".
MILESTONES = [
    "environment_checked",
    "runtime_ready",
    "x_connected",
    "profile_scanned",
    "mode_chosen",
    "project_ready",
    "topics_seeded",
    "tasks_scheduled",
]

# Mirrors panel.ts MILESTONE_LABELS. Includes labels for optional milestones so
# server-snapshot rows (which include them once touched) render nicely.
MILESTONE_LABELS = {
    "environment_checked": "Environment checked",
    "runtime_ready": "Runtime ready",
    "x_connected": "X connected",
    "profile_scanned": "Profile scanned",
    "mode_chosen": "Mode chosen",
    "project_ready": "Project ready",
    "topics_seeded": "Topics seeded",
    "tasks_scheduled": "Tasks scheduled",
    "reddit_connected": "Reddit connected",
    "reddit_verified": "Reddit verified",
}

# Mirrors index.ts TWITTER_AUTOPILOT_LABEL.
AUTOPILOT_LABEL = "com.m13v.social-twitter-cycle"


def state_dir() -> str:
    return os.environ.get("S4L_STATE_DIR") or str(
        Path.home() / ".social-autoposter-mcp"
    )


# ---- pause/resume: stop drafting/posting without touching Claude Desktop ---
# Real mechanics (flag file + launchctl bootout/bootstrap), NOT a loopback call
# to the MCP: this needs to work from BOTH the menu bar (s4l_menubar.py) and
# the Claude-independent dashboard_server.py, which share this process but
# are a different process from the MCP server, so there's no way to delegate
# to mcp/src/index.ts's pauseS4L/resumeS4L here. Mirrors those functions
# (same 4 labels, same flag path) for the common pause->resume case. What it
# deliberately does NOT do: mcp/src/index.ts's resumeS4L also re-runs the
# readiness-gated install logic (persona/project/X-verified checks, content-
# aware plist rewrite) via ensure*Installed() — that only matters for a box
# that was never fully set up before Pause, an edge case the next Claude
# boot / project_config call naturally heals once unpaused. This resume just
# reloads the exact plists Pause unloaded (Pause never deletes anything).
PAUSE_TARGET_LABELS = (
    AUTOPILOT_LABEL,
    "com.m13v.social-claude-reaper",
    "com.m13v.social-autopilot-stall-watch",
    "com.m13v.social-memory-snapshot",
)


def pause_flag_path() -> str:
    return os.path.join(state_dir(), "paused.flag")


def is_paused() -> bool:
    return os.path.exists(pause_flag_path())


def pause_s4l() -> dict:
    if is_paused():
        return {"ok": True, "paused": True, "detail": "already paused"}
    try:
        os.makedirs(state_dir(), exist_ok=True)
        with open(pause_flag_path(), "w") as fh:
            fh.write(f"paused at {time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n")
    except Exception as e:
        return {"ok": False, "paused": is_paused(), "detail": f"could not write pause flag: {e}"}
    uid = os.getuid()
    results = []
    for label in PAUSE_TARGET_LABELS:
        try:
            subprocess.run(
                ["launchctl", "bootout", f"gui/{uid}/{label}"],
                capture_output=True, timeout=15,
            )
            results.append(f"{label}: unloaded")
        except Exception as e:
            results.append(f"{label}: {e}")
    return {"ok": True, "paused": True, "detail": "; ".join(results)}


def resume_s4l() -> dict:
    try:
        os.remove(pause_flag_path())
    except FileNotFoundError:
        pass
    except Exception:
        pass
    uid = os.getuid()
    la_dir = str(Path.home() / "Library" / "LaunchAgents")
    results = []
    for label in PAUSE_TARGET_LABELS:
        plist = os.path.join(la_dir, f"{label}.plist")
        if not os.path.exists(plist):
            results.append(f"{label}: no plist, skip")
            continue
        try:
            subprocess.run(["launchctl", "enable", f"gui/{uid}/{label}"], capture_output=True, timeout=15)
            r = subprocess.run(
                ["launchctl", "bootstrap", f"gui/{uid}", plist],
                capture_output=True, timeout=15,
            )
            results.append(f"{label}: rc={r.returncode}")
        except Exception as e:
            results.append(f"{label}: {e}")
    return {"ok": True, "paused": is_paused(), "detail": "; ".join(results)}


def read_json(name: str):
    try:
        return json.loads((Path(state_dir()) / name).read_text())
    except Exception:
        return None


# ---- direct file reads (work with Claude Desktop closed) -------------------
def read_onboarding():
    """Re-derive onboarding-ledger.cjs publicSnapshot() from the raw ledger."""
    d = read_json("onboarding-progress.json")
    if not d or not isinstance(d.get("milestones"), dict):
        return None
    ms = d["milestones"]

    # mode_chosen (added 2026-06-26) won't exist in ledgers written before it.
    # Mirror the server's backfill so adding this milestone never flips an already-
    # onboarded box back to "Setting up…" in the offline view: treat it complete
    # when the user has picked a mode (mode.json exists) OR the install is already
    # past setup (project_ready complete = a legacy onboard).
    def _status(mid):
        st = (ms.get(mid) or {}).get("status")
        if mid == "mode_chosen" and st != "complete":
            mode_picked = (Path(state_dir()) / MODE_FILE).exists()
            past_setup = (ms.get("project_ready") or {}).get("status") == "complete"
            if mode_picked or past_setup:
                return "complete"
        return st

    milestones = [
        {"id": mid, **(ms.get(mid) or {}), "status": _status(mid)} for mid in MILESTONES
    ]
    complete = all(_status(mid) == "complete" for mid in MILESTONES)
    return {
        "complete": complete,
        "milestones": milestones,
        "current_blocker": d.get("current_blocker"),
    }


def runtime_ready() -> bool:
    rt = read_json("runtime.json")
    if not rt or not rt.get("ready"):
        return False
    py = rt.get("python")
    return bool(py and os.path.exists(py))


def autopilot_loaded() -> bool:
    # Autopilot is now the Claude Desktop scheduled task, not the legacy launchd job.
    cfg = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(str(Path.home()), ".claude")
    return os.path.exists(
        os.path.join(cfg, "scheduled-tasks", "social-autoposter-autopilot", "SKILL.md")
    )


# ---- loopback MCP server (live, when an MCP instance is running) -----------
# Used ONLY as a "can posting reach the MCP tool handlers?" gate (the approved-
# drafts resume path). It is NOT a dashboard source: "Open dashboard" always
# serves the menu bar's own dashboard_server (single path, per user 2026-07-03).
# panel-endpoint.json is last-writer-wins across many short-lived MCP instances,
# so treat reachability as best-effort.
#
# Bypass any configured system/env HTTP proxy for these calls. On macOS,
# urllib.request honors the system proxy (via _scproxy) for ALL requests,
# 127.0.0.1 included, unless a proxy is explicitly disabled per-request — a
# plain urlopen() does NOT skip loopback the way browsers/curl typically do.
# A machine with any system-wide proxy configured (corporate VPN client,
# security software, a residential-IP proxy for platform fingerprinting,
# etc.) whose bypass list doesn't happen to include 127.0.0.1/localhost will
# have every loopback health check and post_drafts call silently routed out
# through that proxy instead of hitting the MCP server directly — surfacing
# as an opaque connection error/403 with no indication the proxy is at
# fault. This is a local process talking to its own local server: it must
# never go through any proxy, regardless of what's configured system-wide.
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _endpoint_url():
    ep = read_json("panel-endpoint.json")
    url = (ep or {}).get("url")
    if not url:
        return None
    try:
        with _NO_PROXY_OPENER.open(url + "health", timeout=1.5) as r:
            if r.status == 200:
                return url
    except Exception as e:
        sys.stderr.write(f"[s4l-state] loopback health check failed: {type(e).__name__}: {e}\n")
        sys.stderr.flush()
        return None
    return None


def loopback_reachable() -> bool:
    return _endpoint_url() is not None


def _parse_tool_result(obj):
    """Normalize an MCP tool result (structuredContent or a JSON text block)."""
    if isinstance(obj, dict):
        sc = obj.get("structuredContent")
        if isinstance(sc, dict):
            snap = sc.get("snapshot")
            if isinstance(snap, str):
                try:
                    return json.loads(snap)
                except Exception:
                    pass
            return sc
        content = obj.get("content")
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text" and c.get("text"):
                    try:
                        return json.loads(c["text"])
                    except Exception:
                        return {"_raw": c["text"]}
    return obj


def loopback_tool(name: str, args=None, timeout: float = 20.0):
    url = _endpoint_url()
    if not url:
        return None
    try:
        data = json.dumps(args or {}).encode()
        req = urllib.request.Request(
            url + "tool/" + name,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _NO_PROXY_OPENER.open(req, timeout=timeout) as r:
            return _parse_tool_result(json.loads(r.read().decode()))
    except Exception as e:
        sys.stderr.write(f"[s4l-state] loopback_tool({name!r}) failed: {type(e).__name__}: {e}\n")
        sys.stderr.flush()
        return None


# ---- the snapshot the menu bar renders ------------------------------------
# Background snapshot cache. scripts/snapshot.py reads files but may spawn the
# X-status subprocess (setup_twitter_auth.py -> CDP to Chrome), which must NEVER
# run on the menu bar's UI thread — a hung Chrome would freeze the menu. So a
# daemon thread recomputes and snapshot() returns the last cached value INSTANTLY.
_snap_cache = {"val": None, "at": 0.0}
_snap_lock = threading.Lock()
_snap_refreshing = [False]


def _snapshot_module():
    repo = (
        os.environ.get("S4L_REPO_DIR")
        or str(Path.home() / "social-autoposter")
    )
    scripts = os.path.join(repo, "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import snapshot as _snapshot_mod  # scripts/snapshot.py
    return _snapshot_mod


def _compute_snapshot_full():
    return _snapshot_module().compute()


def ver_key(v):
    """rc-aware version precedence key, delegated to scripts/snapshot.py::
    _ver_key so there is exactly ONE Python implementation (kept in lockstep
    with mcp/src/version.ts::verKey). The update verifier used to carry its
    own third copy, which drifted rc-blind (2026-07-03); do not re-add one."""
    return _snapshot_module()._ver_key(v)


def version():
    """Installed version, delegated to scripts/snapshot.py::_resolve_version so
    there is exactly ONE Python implementation, same pattern as ver_key above."""
    return _snapshot_module()._resolve_version()


def _refresh_snapshot_bg():
    try:
        snap = _compute_snapshot_full()
        if isinstance(snap, dict) and "projects_total" in snap:
            with _snap_lock:
                _snap_cache["val"] = snap
                _snap_cache["at"] = time.time()
    except Exception:
        pass
    finally:
        _snap_refreshing[0] = False


# ---- version/update check: refreshed independently of the full snapshot ----
# compute() calls _x_status() FIRST, which can block up to 90s in a subprocess
# (setup_twitter_auth.py -> CDP to Chrome) whenever a scan job has the browser
# busy. That stalls _refresh_snapshot_bg() for the whole duration, so
# update_available/latest_version stay frozen on a busy box even though the
# GitHub check itself is a ~1s curl. Poll scripts/snapshot.py::version_status()
# on its own short cadence, gated by its own lock, so the "Update available"
# banner never waits on X/Chrome state.
_ver_cache = {"val": None, "at": 0.0}
_ver_refreshing = [False]
_VER_REFRESH_TTL = 20.0


def _refresh_version_bg():
    try:
        val = _snapshot_module().version_status()
        if isinstance(val, dict) and "update_available" in val:
            with _snap_lock:
                _ver_cache["val"] = val
                _ver_cache["at"] = time.time()
    except Exception:
        pass
    finally:
        _ver_refreshing[0] = False


def _version_overlay():
    now = time.time()
    with _snap_lock:
        cached = _ver_cache["val"]
        age = now - _ver_cache["at"]
    if (cached is None or age > _VER_REFRESH_TTL) and not _ver_refreshing[0]:
        _ver_refreshing[0] = True
        threading.Thread(target=_refresh_version_bg, daemon=True).start()
    return cached


def snapshot():
    """Full snapshot computed DIRECTLY from the stateful files via
    scripts/snapshot.py — the SAME single-source module the MCP shells out to, so
    the two surfaces can't diverge. NO loopback / MCP dependency, so a restarting
    or closed Claude can't freeze or stale the menu (the old tier-1 `loopback_tool`
    blocked the UI thread up to 20s and was the freeze). The heavy compute runs on
    a BACKGROUND thread; this returns the last cached result instantly.

    Tiers: (1) the background-computed local snapshot; (2) the server's last
    persisted `status-summary.json`; (3) the onboarding ledger. The version/update
    fields are always overlaid from the independent _version_overlay() cache
    (see above) so they stay live even while tier (1)'s compute() is blocked."""
    now = time.time()
    with _snap_lock:
        cached = _snap_cache["val"]
        age = now - _snap_cache["at"]
    if (cached is None or age > 4.0) and not _snap_refreshing[0]:
        _snap_refreshing[0] = True
        threading.Thread(target=_refresh_snapshot_bg, daemon=True).start()

    if cached is not None:
        out = dict(cached)
        out["_live"] = True
    else:
        summ = read_json("status-summary.json")
        if isinstance(summ, dict) and "projects_total" in summ:
            out = dict(summ)
            out["_live"] = False
            out["_from_summary"] = True
        else:
            ob = read_onboarding()
            out = {
                "_live": False,
                "runtime_ready": runtime_ready(),
                "onboarding": ob,
                "autopilot_on": autopilot_loaded(),
                "x_connected": False,  # unknowable offline; State derives from onboarding
                "x_handle": None,
                "projects_ready": 0,
                "projects_total": 0,
                "version": version(),
                "update_available": False,
                "latest_version": None,
            }

    ver = _version_overlay()
    if ver is not None:
        out["version"] = ver.get("version", out.get("version"))
        out["channel"] = ver.get("channel", out.get("channel"))
        out["latest_version"] = ver.get("latest_version", out.get("latest_version"))
        out["latest_tag"] = ver.get("latest_tag", out.get("latest_tag"))
        out["update_available"] = ver.get("update_available", out.get("update_available"))
    return out


# set_autopilot() (the launchd toggle) was removed: the autopilot is now the Claude
# Desktop scheduled task `social-autoposter-autopilot`, managed in the Scheduled tab,
# so the menu bar no longer toggles a launchd job (it mirrors the dashboard instead).


# ---- Accessibility (TCC) permission ---------------------------------------
# Posting keystrokes via AppleScript needs the Accessibility permission, granted
# PER responsible-process identity. So this must be called from inside the menu
# bar process to reflect the menu bar (not some parent). AXIsProcessTrusted() is
# TCC's own check — the reliable signal, reached via ctypes (no third-party dep).
def accessibility_trusted() -> bool:
    try:
        import ctypes
        import ctypes.util

        lib = ctypes.util.find_library("ApplicationServices")
        if not lib:
            return False
        ap = ctypes.cdll.LoadLibrary(lib)
        ap.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(ap.AXIsProcessTrusted())
    except Exception:
        return False


# ---- draft review (pop-up cards) ------------------------------------------
# draft_cycle writes review-request.json when a fresh batch is ready; we read
# the linked plan file (the /tmp/twitter_cycle_plan_<batch>.json the pipeline
# produced), present the cards, then post the approved subset via the loopback
# post_drafts tool. The chat-table review still works in parallel; both surfaces
# de-dup on the plan's per-candidate `posted` flag.
# How long an activity signal may go un-refreshed before the menu bar treats it
# as idle. This is the SELF-HEAL for a frozen spinner: a writer can set a label
# (e.g. the queue worker writing "drafting replies" on job-claim, or a kicker
# writing "scanning") and then die WITHOUT clearing it — the leaked-worker reaper
# SIGKILLs a draft worker before it can call `claude_job.py result`, a divergent
# lane runs the cycle with no exit-trap clear, or a process crashes mid-phase. In
# every such case the clear never runs and the old code showed the label forever.
# Live work keeps `since` fresh well under this window (the queue provider's poll
# loop heartbeats every ~10s, the kicker re-stamps "scanning" every ~30s, and the
# poster writes per post), so a signal older than this can only be a stuck stamp.
ACTIVITY_TTL_SECONDS = float(os.environ.get("S4L_ACTIVITY_TTL_S", "120"))


def _activity_is_stale(act) -> bool:
    """True when act['since'] is older than ACTIVITY_TTL_SECONDS. A missing/unparsable
    `since` is treated as NOT stale (fail open: never hide a label we can't age)."""
    try:
        import datetime

        since = (act or {}).get("since")
        if not since:
            return False
        s = since.replace("Z", "+00:00")
        ts = datetime.datetime.fromisoformat(s)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=datetime.timezone.utc)
        age = (datetime.datetime.now(datetime.timezone.utc) - ts).total_seconds()
        return age > ACTIVITY_TTL_SECONDS
    except Exception:
        return False


def read_activity():
    """What the server is doing right now: {state, label} or None when idle.
    Written by long-running tools (scanning/drafting/posting/…); drives the
    menu-bar loading spinner.

    Stale signals are reported as idle (None): see ACTIVITY_TTL_SECONDS. This is
    what keeps the spinner from freezing on a label whose writer died before
    clearing it."""
    act = read_json("activity.json")
    if act and _activity_is_stale(act):
        return None
    return act


def write_activity(state: str, label: str):
    """Best-effort local activity update. The MCP server normally owns this file,
    but the menu-bar posting queue knows the whole approved-card burst while the
    server only sees one post_drafts call at a time."""
    try:
        p = Path(state_dir()) / "activity.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(
                {
                    "state": state,
                    "label": label,
                    "since": time_iso(),
                }
            )
            + "\n"
        )
    except Exception:
        pass


def time_iso():
    try:
        import datetime

        return datetime.datetime.now(datetime.timezone.utc).isoformat()
    except Exception:
        return ""


def read_review_request():
    return read_json("review-request.json")


def clear_review_request():
    try:
        p = Path(state_dir()) / "review-request.json"
        if p.exists():
            p.unlink()
    except Exception:
        pass


def read_plan(plan_path):
    try:
        return json.loads(Path(plan_path).read_text())
    except Exception:
        return None


# ---- durable review store ---------------------------------------------------
# The review queue is ONE candidate-keyed store: ~/.social-autoposter-mcp/
# review-queue.json (written by merge_review_queue.py, decision-stamped here,
# posted/our_url-stamped by the MCP server). It replaces the old split where
# drafts lived in an EPHEMERAL /tmp plan (killed by every reboot/tmp sweep,
# numbering restarting at 1) while decisions lived FOREVER in approved-queue
# .json keyed by plan index — a mismatch that both lost pending drafts on
# reboot and let stale ledger entries swallow every new card after a reset.
#
# /tmp/twitter_cycle_plan_review-queue.json remains as a SYMLINK to the store:
# the MCP server (repo.ts planPath) and the locked pipeline scripts resolve
# that path, and fs.writeFileSync / Path.write_text follow symlinks, so they
# keep working unchanged. The link is recreated at menubar boot and on every
# merge; a reboot only ever removes the LINK, never the data.

REVIEW_STORE = "review-queue.json"


def store_path():
    return str(Path(state_dir()) / REVIEW_STORE)


def ensure_store_symlink(batch="review-queue"):
    """Recreate the /tmp compatibility symlink if a reboot swept it. A REAL file
    at the legacy path (old code wrote it post-reboot) is left for
    merge_review_queue.py to absorb; only a missing or wrong link is fixed."""
    try:
        base = os.environ.get("S4L_TMP_DIR") or "/tmp"
        link = str(Path(base) / f"twitter_cycle_plan_{batch}.json")
        sp = store_path()
        if not Path(sp).exists():
            return
        if os.path.islink(link):
            if os.readlink(link) == sp:
                return
            os.unlink(link)
        elif os.path.exists(link):
            return  # real legacy file: merge_review_queue absorbs it first
        tmp_link = f"{link}.lnk.{os.getpid()}"
        os.symlink(sp, tmp_link)
        os.replace(tmp_link, link)
    except Exception:
        pass


_store_thread_lock = threading.Lock()


def _store_update(mutate):
    """Locked read-modify-write of the review store. An fcntl.flock on a sidecar
    .lock file serializes cross-process python writers; the in-process lock
    serializes menubar threads; os.replace keeps readers atomic. The MCP
    server's writePlan does NOT take this lock (interim race, absorbed by the
    menubar's decision reconciliation on every timer tick). Returns mutate's
    return value; raises nothing (returns None on failure)."""
    import fcntl

    sp = store_path()
    try:
        with _store_thread_lock:
            with open(sp + ".lock", "w") as lk:
                fcntl.flock(lk, fcntl.LOCK_EX)
                try:
                    try:
                        data = json.loads(Path(sp).read_text())
                    except Exception:
                        data = {"candidates": []}
                    rv = mutate(data)
                    tmp = f"{sp}.tmp.{os.getpid()}"
                    Path(tmp).write_text(json.dumps(data, indent=2))
                    os.replace(tmp, sp)
                    return rv
                finally:
                    fcntl.flock(lk, fcntl.LOCK_UN)
    except Exception:
        return None


def _match_candidate(data, n, candidate_id):
    """Locate a candidate by durable identity first (candidate_id), plan index
    second. Returns the candidate dict or None."""
    cands = data.get("candidates") or []
    if candidate_id is not None:
        for c in cands:
            if c.get("candidate_id") == candidate_id:
                return c
    if isinstance(n, int) and 1 <= n <= len(cands):
        return cands[n - 1]
    return None


def candidate_state(c):
    """Canonical lifecycle state of ONE review-queue candidate. Use this instead
    of ad hoc flag checks — "not posted" is NOT "awaiting review".

    The review queue (review-queue.json) is an APPEND-FOREVER LEDGER: handled
    candidates are never removed, they are flag-stamped in place, so most rows
    in an old queue are retired. States, in precedence order:

      posted          — approved and successfully posted (our_url set).
      terminal        — retired without a post; terminal_reason says why
                        (rejected, human_rejected, human_discarded_all,
                        duplicate_thread_pre_post, ...; None on older rows).
      post_failed     — approved but the post attempt errored (post_error says
                        why). Settled from the cards' point of view; the resume
                        path retries only after a fresh approval clears it.
      approved        — human approved, post not yet attempted/confirmed.
      awaiting_review — none of the above. The ONLY state cards present, and
                        the only honest "pending" count (review-request.json's
                        .count mirrors it at merge time).
    """
    if c.get("posted") is True:
        return "posted"
    if c.get("terminal") is True:
        return "terminal"
    if c.get("post_failed"):
        return "post_failed"
    if c.get("approved") is True:
        return "approved"
    return "awaiting_review"


def store_stamp_decision(batch, decision):
    """Write a card decision INTO the store the instant the user clicks. This is
    the durable record (the old approved-queue.json ledger is no longer
    written): approve stamps approved=True + the decision payload (text/edits
    ride along so a restart can resume the post); reject stamps terminal=True.
    The MCP server later confirms posted=True/our_url via its own write.
    Returns True when the stamp landed."""

    def mutate(data):
        c = _match_candidate(data, decision.get("n"), decision.get("candidate_id"))
        if c is None:
            return False
        payload = {
            "approved": bool(decision.get("approved")),
            "text": decision.get("text") or "",
            "edited": bool(decision.get("edited")),
            "drop_link": bool(decision.get("drop_link")),
            "loved": bool(decision.get("loved")),
            "reject_category": decision.get("reject_category"),
            # Two-draft pairwise record (chosen vs unchosen + hover dwell),
            # None on single-draft cards. Durable locally so the choice
            # survives even if the review-events flush never lands.
            "draft_choice": decision.get("draft_choice"),
            "decided_at": time_iso(),
        }
        if decision.get("approved"):
            c["approved"] = True
            c.pop("post_failed", None)
            c.pop("post_error", None)
        else:
            c["terminal"] = True
            c["terminal_reason"] = "human_rejected"
        c["decision"] = payload
        return True

    return bool(_store_update(mutate))


def discard_all_pending(drafts):
    """Bulk-reject an entire pending set (the menu bar's "Discard all pending
    drafts"), durably and atomically in ONE lock acquisition. Each candidate is
    stamped terminal with NO reason (reject_category=None), same as a card's
    "Reject, no reason" button. Deliberately distinct from store_stamp_decision:
    this never ships a review event, by design, so a bulk "clear the backlog"
    click never reaches the review-events/feedback-digest rail and can't
    pollute learned_preferences with a non-judgment. Returns how many were
    stamped."""

    def mutate(data):
        done = 0
        for d in drafts:
            c = _match_candidate(data, d.get("n"), d.get("candidate_id"))
            if c is None or candidate_state(c) in ("posted", "terminal"):
                continue
            c["terminal"] = True
            c["terminal_reason"] = "human_discarded_all"
            c["decision"] = {
                "approved": False,
                "text": c.get("reply_text") or "",
                "edited": False,
                "drop_link": False,
                "loved": False,
                "reject_category": None,
                "decided_at": time_iso(),
            }
            done += 1
        return done

    return _store_update(mutate) or 0


def flip_discarded_candidates_skipped(candidate_ids):
    """Flip each bulk-discarded candidate's twitter_candidates row to
    status='skipped', skip_reason='human_discarded_all' — the same DB effect a
    normal reject gets for free as a side effect of its review-events insert
    (see review-events/route.ts). The bulk-discard path deliberately never
    ships a review event (that's the whole point — it must not reach the
    feedback digest), so without this direct call the row would stay
    'pending' until the independent age-based freshness gate happens to expire
    it, wide open to re-discovery/re-drafting of the exact draft a human just
    discarded. One PATCH per candidate via the SAME direct-HTTP path
    flush_review_events already uses (http_api), so this never touches
    review_events. Best-effort: a candidate that already expired/posted by the
    time this runs (404, no longer 'pending') is a no-op, not a failure."""
    if not candidate_ids:
        return
    try:
        from http_api import api_patch
    except Exception as err:
        sys.stderr.write(
            f"[s4l-state] flip_discarded_candidates_skipped: http_api unavailable "
            f"({type(err).__name__}: {err}); {len(candidate_ids)} candidate(s) left 'pending'\n"
        )
        sys.stderr.flush()
        return
    for cid in candidate_ids:
        try:
            api_patch(
                "/api/v1/twitter-candidates/by-id",
                {"id": cid, "action": "mark_skipped", "reason": "human_discarded_all"},
                ok_on_404=True,
            )
        except Exception as err:
            sys.stderr.write(
                f"[s4l-state] flip_discarded_candidates_skipped: PATCH failed for "
                f"candidate_id={cid} ({type(err).__name__}: {err})\n"
            )
            sys.stderr.flush()


def store_mark_post_failed(batch, n, candidate_id=None, error=None):
    """A decided post that FAILED surfaces via notification/dashboard, not by
    re-presenting the card and not by endless resume retries."""

    def mutate(data):
        c = _match_candidate(data, n, candidate_id)
        if c is None:
            return False
        c["post_failed"] = True
        if error:
            c["post_error"] = str(error)[:200]
        return True

    return bool(_store_update(mutate))


def store_pending_posts(batch="review-queue"):
    """Approved-but-unposted candidates, for the restart resume path. Skips
    posted/terminal/failed rows; each entry carries everything post_drafts
    needs (n, final text, drop_link)."""
    plan = read_plan(store_path())
    out = []
    for i, c in enumerate((plan or {}).get("candidates") or []):
        if candidate_state(c) != "approved":
            continue
        d = c.get("decision") or {}
        out.append(
            {
                "batch": batch,
                "n": i + 1,
                "candidate_id": c.get("candidate_id"),
                "text": d.get("text") or c.get("reply_text") or "",
                "edited": bool(d.get("edited")),
                "drop_link": bool(d.get("drop_link")),
            }
        )
    return out


def store_reconcile_decisions(batch, decisions):
    """Re-stamp any of this session's decisions the store no longer shows. The
    MCP server's post_drafts does a whole-file read-modify-write while a batch
    posts (minutes), so a decision stamped mid-drain can be clobbered by its
    stale rewrite; the menubar calls this every timer tick with its in-memory
    decision list so the store always converges back to what the user chose.
    Returns how many had to be re-stamped."""
    fixed = 0
    try:
        plan = read_plan(store_path()) or {}
        cands = plan.get("candidates") or []
        by_id = {c.get("candidate_id"): c for c in cands if c.get("candidate_id") is not None}
        for d in decisions or []:
            c = by_id.get(d.get("candidate_id"))
            if c is None:
                n = d.get("n")
                c = cands[n - 1] if isinstance(n, int) and 1 <= n <= len(cands) else None
            if c is None:
                continue
            settled = (
                c.get("posted") is True
                or c.get("terminal") is True
                or (c.get("approved") is True if d.get("approved") else False)
            )
            if not settled:
                if store_stamp_decision(batch, d):
                    fixed += 1
    except Exception:
        pass
    return fixed


def review_queue_posted_count():
    """Posts that have LANDED in the review-queue plan — the durable, cross-process
    truth. Independent of the menu bar's in-memory burst queue (which dies on a
    restart) and of WHICH process is posting (the menu bar worker, the autopilot,
    or a host agent draining via post_drafts). Returns the posted count, or None
    when the plan can't be read. Drives the menu-bar posting indicator so progress
    stays visible regardless of how the drain is driven."""
    plan_path = None
    req = read_review_request()
    if req:
        plan_path = req.get("plan_path")
    if not plan_path or not Path(plan_path).exists():
        plan_path = store_path()
    if not Path(plan_path).exists():
        plan_path = "/tmp/twitter_cycle_plan_review-queue.json"
    plan = read_plan(plan_path)
    cands = (plan or {}).get("candidates")
    if not cands:
        return None
    return sum(1 for c in cands if candidate_state(c) == "posted")


def _plan_generation(batch):
    """created_at of the CURRENT review plan for this batch (stamped by
    merge_review_queue.py when it starts a fresh plan), or None for plans
    written before generation stamping existed.

    Why this matters: the plan lives in /tmp and dies on every reboot or tmp
    sweep, and its candidate numbering restarts at 1, while approved-queue.json
    lives in the state dir forever. Without a generation marker, ledger entries
    from a dead plan match the new plan's low indices and every fresh draft is
    treated as already-decided: the 2026-07-03 "unapproved cards never show up"
    bug (30 stale entries silently swallowed a whole day of drafts)."""
    try:
        p = Path(store_path())
        if not p.exists():
            base = os.environ.get("S4L_TMP_DIR") or "/tmp"
            p = Path(base) / f"twitter_cycle_plan_{batch}.json"
        return json.loads(p.read_text()).get("created_at") or None
    except Exception:
        return None


def _ts_before(a, b):
    """True when iso timestamp a is strictly before b. Tolerates the two stamp
    shapes in play ('...Z' from merge_review_queue, '+00:00' from time_iso)."""
    try:
        import datetime

        def parse(s):
            return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))

        return parse(a) < parse(b)
    except Exception:
        return False


def _stale_for_plan(it, gen):
    """A ledger item that predates the current plan generation refers to a DEAD
    plan's numbering; its `n` must never match against the live plan."""
    return bool(gen) and bool(it.get("ts")) and _ts_before(it.get("ts"), gen)


def _ledger_items_for_plan(batch, gen):
    """Decided ledger items that belong to the CURRENT plan generation."""
    return [
        it
        for it in read_approved_queue()["items"]
        if it.get("batch") == batch
        and it.get("status") in ("queued", "posting", "posted", "failed", "rejected")
        and not _stale_for_plan(it, gen)
    ]


def review_drafts(plan, batch="review-queue"):
    """Flatten a plan into the card model: only UNDECIDED candidates. A card that's
    posted, terminal (rejected/dead), or already approved is a settled decision and
    must never be re-presented for review (approved ones proceed to post).

    Also excludes cards with ANY durable decision (approved, edited, rejected, or a
    decided-but-failed post) via _ledger_items_for_plan() below. approve/reject/edit
    each write a durable local record via store_stamp_decision() the INSTANT the
    user clicks (see s4l_menubar.py::_on_card_decision), so a
    decided card never re-presents even if the loopback (Claude Desktop) is down
    when the decision's plan-flag write is attempted. The main plan's
    `approved`/`terminal`/`posted` flags are only stamped once the loopback write
    lands, so without this a card the user just decided would re-present (the exact
    "I already decided these" bug)."""
    gen = (plan or {}).get("created_at") or _plan_generation(batch)
    items = _ledger_items_for_plan(batch, gen)
    settled_ids = {
        it.get("candidate_id") for it in items if it.get("candidate_id") is not None
    }
    settled_ns = {it.get("n") for it in items if it.get("candidate_id") is None}
    out = []
    for i, c in enumerate(((plan or {}).get("candidates") or [])):
        # Only awaiting_review rows become cards. This also skips post_failed
        # rows (decided-but-failed is settled per the docstring above).
        if candidate_state(c) != "awaiting_review":
            continue
        cid = c.get("candidate_id")
        if cid is not None and cid in settled_ids:
            continue
        if (i + 1) in settled_ns:
            continue
        out.append(
            {
                "n": i + 1,  # 1-based, matches post_drafts numbering
                # Platform tag (reddit cards, 2026-07-14): absent/None means
                # twitter (every plan written before the field shipped). The
                # card renders the platform mark and profile link from this.
                "platform": c.get("platform"),
                "thread_author": c.get("thread_author"),
                "thread_text": c.get("thread_text"),
                "reply_text": c.get("reply_text") or "",
                # English translations stamped at draft time (prep step) when
                # language != en. Display-only: the card shows them so the
                # operator can understand a non-English draft; the ORIGINAL
                # reply_text is what gets edited and posted. Absent on English
                # drafts and on plans written before the field shipped.
                "reply_text_en": c.get("reply_text_en"),
                "thread_text_en": c.get("thread_text_en"),
                "link_url": c.get("link_url"),
                # Ride-along context for the review-events feedback rail: the
                # card copies these onto each decision so the shipped event can
                # be joined back to the twitter_candidates row and scoped to a
                # project without re-reading the plan.
                "candidate_id": c.get("candidate_id"),
                "project": c.get("matched_project") or c.get("project"),
                # Thread permalink + discovery-time stats (author followers,
                # thread engagement), stamped by merge_review_queue.py from data
                # the pipeline already captured. The card renders these as
                # profile/thread links and a stats line; both may be absent on
                # plans written before the enrichment shipped.
                "thread_url": c.get("candidate_url")
                or c.get("tweet_url")
                or c.get("thread_url"),
                "stats": c.get("stats") or {},
                # Drafting metadata for the reply row's details popover: how the
                # draft came to be (style, discovery query, link choice). All
                # already on the plan candidate; absent on plans written before
                # each field shipped, and the card omits what's missing.
                "engagement_style": c.get("engagement_style"),
                "style_description": (
                    (c.get("new_style") or {}).get("description")
                    if isinstance(c.get("new_style"), dict)
                    else None
                ),
                "search_topic": c.get("search_topic"),
                "language": c.get("language"),
                "link_source": c.get("link_source"),
                "link_keyword": c.get("link_keyword"),
                # {name: variant} of every experiment/scenario arm active when
                # the draft was written, stamped by merge_review_queue.py via
                # scripts/active_experiments.py. Generic: the card renders
                # whatever is here, so new experiments surface with no UI work.
                "experiments": c.get("experiments") or {},
                # Two-draft cards (2026-07-07; no-recommendation pass
                # 2026-07-08): present only when the prep step wrote two
                # independent drafts (fresh candidate, not a reused stale
                # draft). The card shows both, defaulting to Draft A (slot 0,
                # no model recommendation involved); absent on reused/legacy
                # candidates, which fall back to the single-draft UI above.
                "drafts": c.get("drafts"),
            }
        )
    # The review queue is append-only, so the highest stable index is newest and
    # most likely to still be live on X.
    out.sort(key=lambda d: d["n"], reverse=True)
    return out


# ---- durable approved-card queue ------------------------------------------
# Card approvals MUST survive a menu bar / Claude restart. The in-memory post
# queue does not: a restart strands every approved-but-unposted card, which then
# re-presents for approval (the system had no record the user already approved
# it). This file is the durable record, owned SOLELY by the menu bar — persisting
# the approval in the main plan instead would race with the autopilot, which
# rewrites that plan continuously and would silently drop the flag. Status flow:
# queued -> posting -> posted | failed. review_drafts() excludes queued/posting
# so an approved card is never re-shown while it drains; a restart re-enqueues
# queued/posting items instead of re-presenting them.
APPROVED_QUEUE = "approved-queue.json"


def read_approved_queue():
    d = read_json(APPROVED_QUEUE)
    if not isinstance(d, dict) or not isinstance(d.get("items"), list):
        return {"items": []}
    return d


def _write_approved_queue(d):
    try:
        p = Path(state_dir()) / APPROVED_QUEUE
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d))
        os.replace(str(tmp), str(p))  # atomic: a crash never leaves a half file
    except Exception:
        pass


# ---- Engagement mode (2026-06-26, dual-flag 2026-06-29) -------------------
# Two INDEPENDENT lanes the menu bar toggles separately:
#   personal_brand (default ON)  -> link-free organic engagement for the user's
#                                   own brand (forced persona project)
#   promotion      (default OFF) -> the product-marketing pipeline (link replies)
# Both can be ON; the cycle then splits per `personal_brand_share` (default
# 0.5). State is ONE file the cycle wrapper also reads via
# scripts/s4l_mode.py; keep the shape in lockstep with it.
MODE_FILE = "mode.json"
MODE_PROMOTION = "promotion"
MODE_PERSONAL_BRAND = "personal_brand"
_VALID_MODES = (MODE_PROMOTION, MODE_PERSONAL_BRAND)
# 2026-06-29 default flip: personal brand on out of the box, promotion opt-in.
_DEFAULT_FLAGS = {"personal_brand": True, "promotion": False}


def read_flags():
    """Current lane flags {"personal_brand": bool, "promotion": bool}.

    Mirrors scripts/s4l_mode.py get_flags(): explicit flag keys win; else map a
    legacy {"mode": ...} string; else the default (personal ON / promotion OFF).
    """
    d = read_json(MODE_FILE)
    if isinstance(d, dict):
        if "personal_brand" in d or "promotion" in d:
            return {
                "personal_brand": bool(d.get("personal_brand")),
                "promotion": bool(d.get("promotion")),
            }
        m = str(d.get("mode") or "").strip()
        if m == MODE_PERSONAL_BRAND:
            return {"personal_brand": True, "promotion": False}
        if m == MODE_PROMOTION:
            return {"personal_brand": False, "promotion": True}
    return dict(_DEFAULT_FLAGS)


def read_mode():
    """Derived legacy single-mode string (personal_brand wins when on). Kept so
    older menu-bar callers that expect one value keep working."""
    f = read_flags()
    return MODE_PERSONAL_BRAND if f.get("personal_brand") else MODE_PROMOTION


def write_flags(personal_brand, promotion):
    """Persist both lane flags atomically (plus the derived legacy `mode`).

    Preserves any OTHER keys already in mode.json (`draft_only`,
    `personal_brand_share`, ...) so a lane flip can never silently reset an
    unrelated setting — same contract as scripts/s4l_mode.py write_flags().
    Returns the written flags. Never raises — a menu click must not crash."""
    flags = {"personal_brand": bool(personal_brand), "promotion": bool(promotion)}
    try:
        payload = read_json(MODE_FILE)
        if not isinstance(payload, dict):
            payload = {}
        payload.update(flags)
        payload["mode"] = MODE_PERSONAL_BRAND if flags["personal_brand"] else MODE_PROMOTION
        p = Path(state_dir()) / MODE_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(str(tmp), str(p))
    except Exception:
        pass
    return flags


def read_split():
    """Personal-brand share of both-lanes-on cycles (0.0-1.0, default 0.5).
    Mirrors scripts/s4l_mode.py get_split()."""
    d = read_json(MODE_FILE)
    try:
        share = float(d.get("personal_brand_share"))
    except (TypeError, ValueError, AttributeError):
        return 0.5
    return min(1.0, max(0.0, share))


def write_split(share):
    """Persist the personal-brand share, preserving every other mode.json key.
    Returns the written share. Never raises — a menu click must not crash."""
    try:
        share = min(1.0, max(0.0, float(share)))
    except (TypeError, ValueError):
        return read_split()
    try:
        payload = read_json(MODE_FILE)
        if not isinstance(payload, dict):
            payload = {}
        payload["personal_brand_share"] = share
        flags = read_flags()
        payload.setdefault("personal_brand", flags["personal_brand"])
        payload.setdefault("promotion", flags["promotion"])
        payload["mode"] = MODE_PERSONAL_BRAND if flags["personal_brand"] else MODE_PROMOTION
        p = Path(state_dir()) / MODE_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(str(tmp), str(p))
    except Exception:
        pass
    return share


# Draft-card reveal cadence: minimum seconds between FRESH card pop-ups
# (0 = present as soon as drafts are ready). Drafting itself is untouched;
# drafts keep accumulating in the review store and the menubar just paces the
# pop-up. Lives in mode.json (every writer preserves unknown keys). Presets
# are the menubar's REVEAL_CADENCE_PRESETS; any non-negative value is valid.
REVEAL_CADENCE_DEFAULT = 3600.0


def read_reveal_cadence():
    """Seconds between draft-card reveals (0 = immediately, default 1 hour)."""
    d = read_json(MODE_FILE)
    try:
        secs = float(d.get("reveal_cadence_secs"))
    except (TypeError, ValueError, AttributeError):
        return REVEAL_CADENCE_DEFAULT
    return max(0.0, secs)


def write_reveal_cadence(secs):
    """Persist the reveal cadence, preserving every other mode.json key.
    Returns the written value. Never raises (a menu click must not crash)."""
    try:
        secs = max(0.0, float(secs))
    except (TypeError, ValueError):
        return read_reveal_cadence()
    try:
        payload = read_json(MODE_FILE)
        if not isinstance(payload, dict):
            payload = {}
        payload["reveal_cadence_secs"] = secs
        p = Path(state_dir()) / MODE_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(str(tmp), str(p))
    except Exception:
        pass
    return secs


# --- Posting volume mode (2026-07-13) ---------------------------------------
# Server-side per-install throttle for the twitter cycle's virality bar:
# high|medium|low|None (None = driver default). Source of truth is
# installations.posting_mode via /api/v1/installations/posting-mode; unlike
# the lane flags there is NO local mode.json copy, because the whole point is
# remote adjustability (dashboard and menubar must agree with the server).
# Reads are cached so the menu rebuild tick never blocks on the network: a
# stale/missing cache kicks one background refresh and returns the last-known
# value (None until the first refresh lands; the next rebuild shows the mark).
POSTING_MODE_TTL_SECS = 600
_posting_mode_cache = {"mode": None, "known": False, "at": 0.0}
_posting_mode_lock = threading.Lock()


def _scripts_on_path():
    repo = os.environ.get("S4L_REPO_DIR") or str(Path.home() / "social-autoposter")
    scripts = os.path.join(repo, "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)


def _refresh_posting_mode():
    try:
        _scripts_on_path()
        from http_api import api_get

        r = api_get("/api/v1/installations/posting-mode")
        d = (r or {}).get("data") or {}
        mode = d.get("mode")
        with _posting_mode_lock:
            _posting_mode_cache.update(
                mode=mode if mode in ("high", "medium", "low") else None,
                known=True,
                at=time.time(),
            )
    except Exception as e:
        sys.stderr.write(f"[s4l-state] posting-mode refresh failed: {e}\n")
        with _posting_mode_lock:
            # Throttle retries to the TTL window; keep last-known value.
            _posting_mode_cache["at"] = time.time()


def read_posting_mode():
    """Last-known posting mode ('high'|'medium'|'low') or None (default /
    unknown). Non-blocking: kicks a background refresh when stale."""
    with _posting_mode_lock:
        fresh = (time.time() - _posting_mode_cache["at"]) < POSTING_MODE_TTL_SECS
        mode = _posting_mode_cache["mode"]
    if not fresh:
        threading.Thread(target=_refresh_posting_mode, daemon=True).start()
    return mode


def write_posting_mode(mode):
    """Set (or clear, mode=None) the server-side posting mode. Blocking POST;
    call from a menu click, not from the rebuild tick. Returns the stored
    mode on success; raises on network failure so the caller can notify."""
    _scripts_on_path()
    from http_api import api_post

    r = api_post("/api/v1/installations/posting-mode", {"mode": mode})
    d = (r or {}).get("data") or {}
    stored = d.get("mode")
    with _posting_mode_lock:
        _posting_mode_cache.update(
            mode=stored if stored in ("high", "medium", "low") else None,
            known=True,
            at=time.time(),
        )
    return stored


def write_mode(mode):
    """Legacy single-mode setter: named lane ON, the other OFF (compat)."""
    if mode not in _VALID_MODES:
        return read_flags()
    return write_flags(
        personal_brand=(mode == MODE_PERSONAL_BRAND),
        promotion=(mode == MODE_PROMOTION),
    )


def toggle_lane(lane):
    """Flip ONE lane (personal_brand|promotion) and return the new flags."""
    if lane not in _VALID_MODES:
        return read_flags()
    f = read_flags()
    f[lane] = not f.get(lane)
    return write_flags(f["personal_brand"], f["promotion"])


def approved_queue_set_status(batch, n, status, error=None):
    with _approved_lock:
        d = read_approved_queue()
        changed = False
        for it in d["items"]:
            if it.get("batch") == batch and it.get("n") == n:
                it.update(status=status, error=error, ts=time_iso())
                changed = True
        if changed:
            _write_approved_queue(d)


def approved_queue_pending():
    """Approvals not yet confirmed posted (queued or posting). Re-enqueued by the
    menu bar on startup so a restart RESUMES the drain instead of re-presenting."""
    return [it for it in read_approved_queue()["items"]
            if it.get("status") in ("queued", "posting")]


def post_drafts(batch_id, post=None, edits=None, reject=None, clear_link=None, timeout=900, activity_label=None):
    """Post / reject drafts via the loopback tool. `post` = 1-based numbers to post
    as-is; `edits` = [{n, text}] to rewrite then post; `reject` = numbers to mark
    DONE so they're never shown for review again (not posted); `clear_link` =
    numbers whose link the user removed while editing, so the poster clears
    link_url and does NOT re-append it. Returns the parsed result, or None if the
    loopback is unreachable (Claude Desktop closed)."""
    args = {"batch_id": batch_id, "post": post or [], "edits": edits or [], "reject": reject or [], "clear_link": clear_link or []}
    if activity_label:
        args["__s4l_activity_label"] = activity_label
    return loopback_tool("post_drafts", args, timeout=timeout)


# ---- review-events outbox (2026-07-02) --------------------------------------
# Every card decision (approve/reject, with reason chips, link-click
# interactions, and dwell time) ships to POST /api/v1/review-events so the
# feedback-digest job can distill human rejections into each project's
# learned_preferences config block. The outbox JSONL is the durability layer:
# append locally first, flush to the API in the background with retry. Events
# carry a client-generated event_uuid and the server upserts ON CONFLICT DO
# NOTHING, so a crash between POST and truncate only produces duplicates the
# server drops — never lost events, never double rows.
REVIEW_EVENTS_OUTBOX = "review-events-outbox.jsonl"
_outbox_lock = threading.Lock()
_outbox_flush_lock = threading.Lock()


def _outbox_path():
    return Path(state_dir()) / REVIEW_EVENTS_OUTBOX


def review_event_add(event):
    """Append one decision event to the durable outbox and kick an async flush.
    Never raises — a telemetry failure must not break the card flow."""
    import uuid

    ev = dict(event or {})
    ev.setdefault("event_uuid", str(uuid.uuid4()))
    ev.setdefault("client_ts", time_iso())
    try:
        with _outbox_lock:
            p = _outbox_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a") as f:
                f.write(json.dumps(ev) + "\n")
    except Exception as err:
        # Was a silent `pass` — a card's approve/reject decision could vanish
        # with zero trace anywhere (2026-07-08: two approved-and-posted cards
        # had no review_events row at all, and this was one of the candidate
        # causes with nothing to confirm or rule it out). Never never write to
        # the outbox again for THIS event, so it's worth saying loudly.
        sys.stderr.write(
            f"[s4l-state] review_event_add: failed to append to outbox ({type(err).__name__}: {err}); "
            f"event DROPPED event_uuid={ev.get('event_uuid')} decision={ev.get('decision')} "
            f"candidate_id={ev.get('candidate_id')}\n"
        )
        sys.stderr.flush()
    flush_review_events_async()


def flush_review_events_async():
    threading.Thread(target=flush_review_events, daemon=True).start()


def flush_review_events():
    """Flush the outbox to /api/v1/review-events in batches. Failed batches stay
    in the outbox for the next kick (next decision, review close, or menubar
    start). Serialized: a second concurrent flush returns immediately."""
    if not _outbox_flush_lock.acquire(blocking=False):
        return
    try:
        try:
            with _outbox_lock:
                p = _outbox_path()
                if not p.exists():
                    return
                lines = [ln for ln in p.read_text().splitlines() if ln.strip()]
        except Exception as err:
            sys.stderr.write(f"[s4l-state] flush_review_events: failed to read outbox: {type(err).__name__}: {err}\n")
            sys.stderr.flush()
            return
        events = []
        for ln in lines:
            try:
                ev = json.loads(ln)
            except Exception as err:
                sys.stderr.write(
                    f"[s4l-state] flush_review_events: dropping unparseable outbox line "
                    f"({type(err).__name__}: {err}): {ln[:200]!r}\n"
                )
                sys.stderr.flush()
                continue  # corrupt line: dropped on the next rewrite
            if isinstance(ev, dict) and ev.get("event_uuid"):
                events.append(ev)
            else:
                sys.stderr.write(
                    f"[s4l-state] flush_review_events: dropping malformed outbox line "
                    f"(not a dict, or missing event_uuid): {ln[:200]!r}\n"
                )
                sys.stderr.flush()
        if not events:
            if lines:  # only corrupt lines left — clear the file
                _outbox_remove(set())
            return
        # scripts/ is on sys.path (S4L_REPO_DIR insertion at menubar boot);
        # import lazily so a missing pipeline repo degrades to buffer-only.
        try:
            from http_api import api_post
        except Exception as err:
            sys.stderr.write(
                f"[s4l-state] flush_review_events: http_api unavailable ({type(err).__name__}: {err}); "
                f"{len(events)} event(s) staying buffered in the outbox\n"
            )
            sys.stderr.flush()
            return
        shipped = set()
        for i in range(0, len(events), 100):
            batch = events[i : i + 100]
            try:
                api_post("/api/v1/review-events", {"events": batch})
                shipped.update(e["event_uuid"] for e in batch)
            except Exception as err:
                sys.stderr.write(
                    f"[s4l-state] flush_review_events: POST failed ({type(err).__name__}: {err}); "
                    f"{len(batch)} event(s) in this batch (and any after it) staying buffered for the next kick\n"
                )
                sys.stderr.flush()
                break  # network/API down: keep the rest for the next kick
        _outbox_remove(shipped, keep_only_valid=True)
    finally:
        _outbox_flush_lock.release()


def _outbox_remove(shipped_uuids, keep_only_valid=False):
    """Rewrite the outbox dropping shipped (and, optionally, corrupt) lines.
    Runs under _outbox_lock so appends that landed mid-flush are preserved."""
    try:
        with _outbox_lock:
            p = _outbox_path()
            if not p.exists():
                return
            remaining = []
            for ln in p.read_text().splitlines():
                if not ln.strip():
                    continue
                try:
                    ev = json.loads(ln)
                except Exception:
                    if not keep_only_valid:
                        remaining.append(ln)
                    continue
                if not isinstance(ev, dict) or ev.get("event_uuid") in shipped_uuids:
                    continue
                remaining.append(json.dumps(ev))
            tmp = p.with_suffix(".jsonl.tmp")
            tmp.write_text("\n".join(remaining) + ("\n" if remaining else ""))
            os.replace(str(tmp), str(p))
    except Exception:
        pass
