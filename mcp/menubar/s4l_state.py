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
import urllib.request
from pathlib import Path

# Mirrors shared/onboarding-ledger.cjs MILESTONES (same order).
MILESTONES = [
    "environment_checked",
    "runtime_ready",
    "x_connected",
    "profile_scanned",
    "project_ready",
    "topics_seeded",
    "draft_verified",
]

# Mirrors panel.ts MILESTONE_LABELS.
MILESTONE_LABELS = {
    "environment_checked": "Environment checked",
    "runtime_ready": "Runtime ready",
    "x_connected": "X connected",
    "profile_scanned": "Profile scanned",
    "project_ready": "Project ready",
    "topics_seeded": "Topics seeded",
    "draft_verified": "Draft cycle verified",
}

# Mirrors index.ts TWITTER_AUTOPILOT_LABEL.
AUTOPILOT_LABEL = "com.m13v.social-twitter-cycle"


def state_dir() -> str:
    return os.environ.get("SAPS_STATE_DIR") or str(
        Path.home() / ".social-autoposter-mcp"
    )


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
    milestones = [{"id": mid, **(ms.get(mid) or {})} for mid in MILESTONES]
    complete = all(
        (ms.get(mid) or {}).get("status") == "complete" for mid in MILESTONES
    )
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


def version():
    ep = read_json("panel-endpoint.json") or {}
    return ep.get("version")


def _launchctl_list() -> str:
    try:
        return subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, timeout=10
        ).stdout
    except Exception:
        return ""


def autopilot_loaded() -> bool:
    return AUTOPILOT_LABEL in _launchctl_list()


# ---- loopback panel server (live, when Claude Desktop is running) ----------
def _endpoint_url():
    ep = read_json("panel-endpoint.json")
    url = (ep or {}).get("url")
    if not url:
        return None
    try:
        with urllib.request.urlopen(url + "health", timeout=1.5) as r:
            if r.status == 200:
                return url
    except Exception:
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
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return _parse_tool_result(json.loads(r.read().decode()))
    except Exception:
        return None


# ---- the snapshot the menu bar renders ------------------------------------
def snapshot():
    """Full live snapshot via the loopback dashboard tool, else a file-built
    fallback covering the essentials (runtime, onboarding, autopilot)."""
    snap = loopback_tool("dashboard", {})
    if isinstance(snap, dict) and "projects_total" in snap:
        snap["_live"] = True
        return snap
    ob = read_onboarding()
    return {
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


def stats_7d():
    """7-day post stats; loopback only (the DB read needs the owned runtime)."""
    res = loopback_tool("get_stats", {"days": 7})
    if not isinstance(res, dict):
        return None
    projects = res.get("projects")
    proj = projects[0] if isinstance(projects, list) and projects else None
    p = (proj or {}).get("posts")
    if not p:
        return None
    return {
        "posts": p.get("total", 0),
        "views": p.get("views_period_total", p.get("views", 0)),
        "replies": p.get("comments_period_total", p.get("comments", 0)),
    }


def set_autopilot(enable: bool) -> bool:
    """Toggle background posting. Prefer the loopback tool (it creates the plist
    correctly on first enable); fall back to launchctl against the existing
    plist when Claude Desktop is closed. Returns False if it couldn't act."""
    res = loopback_tool("autopilot", {"action": "enable" if enable else "disable"})
    if res is not None:
        return True
    plist = str(
        Path.home() / "Library" / "LaunchAgents" / (AUTOPILOT_LABEL + ".plist")
    )
    uid = os.getuid()
    try:
        if enable:
            if not os.path.exists(plist):
                return False  # first enable needs the server to write the plist
            subprocess.run(
                ["launchctl", "bootstrap", f"gui/{uid}", plist],
                capture_output=True,
                timeout=15,
            )
        else:
            subprocess.run(
                ["launchctl", "bootout", f"gui/{uid}/{AUTOPILOT_LABEL}"],
                capture_output=True,
                timeout=15,
            )
        return True
    except Exception:
        return False


def panel_url():
    """The loopback dashboard url if reachable, else None."""
    return _endpoint_url()


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


def request_accessibility() -> bool:
    """Pop the system Accessibility prompt for THIS process (registers it in the
    list so the user can toggle it on) and open the Settings pane. Returns the
    current trust state. Safe to call when already trusted (no prompt shown)."""
    trusted = False
    try:
        import ctypes
        import ctypes.util

        cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
        ap = ctypes.cdll.LoadLibrary(ctypes.util.find_library("ApplicationServices"))
        prompt_key = ctypes.c_void_p.in_dll(ap, "kAXTrustedCheckOptionPrompt")
        cf_true = ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue")
        cf.CFDictionaryCreate.restype = ctypes.c_void_p
        cf.CFDictionaryCreate.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        keys = (ctypes.c_void_p * 1)(prompt_key)
        vals = (ctypes.c_void_p * 1)(cf_true)
        d = cf.CFDictionaryCreate(None, keys, vals, 1, None, None)
        ap.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
        ap.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
        trusted = bool(ap.AXIsProcessTrustedWithOptions(d))
    except Exception:
        pass
    try:
        subprocess.run(
            [
                "open",
                "x-apple.systempreferences:com.apple.preference.security"
                "?Privacy_Accessibility",
            ],
            capture_output=True,
            timeout=10,
        )
    except Exception:
        pass
    return trusted


# ---- draft review (pop-up cards) ------------------------------------------
# draft_cycle writes review-request.json when a fresh batch is ready; we read
# the linked plan file (the /tmp/twitter_cycle_plan_<batch>.json the pipeline
# produced), present the cards, then post the approved subset via the loopback
# post_drafts tool. The chat-table review still works in parallel; both surfaces
# de-dup on the plan's per-candidate `posted` flag.
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


def review_drafts(plan):
    """Flatten a plan into the card model: only candidates not already posted."""
    out = []
    for i, c in enumerate(((plan or {}).get("candidates") or [])):
        if c.get("posted") is True:
            continue
        out.append(
            {
                "n": i + 1,  # 1-based, matches post_drafts numbering
                "thread_author": c.get("thread_author"),
                "thread_text": c.get("thread_text"),
                "reply_text": c.get("reply_text") or "",
                "link_url": c.get("link_url"),
            }
        )
    return out


def post_drafts(batch_id, post=None, edits=None, timeout=900):
    """Post approved drafts via the loopback tool. `post` = 1-based numbers to
    post as-is; `edits` = [{n, text}] to rewrite then post. Returns the parsed
    result, or None if the loopback is unreachable (Claude Desktop closed)."""
    return loopback_tool(
        "post_drafts",
        {"batch_id": batch_id, "post": post or [], "edits": edits or []},
        timeout=timeout,
    )
