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

# Serializes read-modify-write on approved-queue.json. The menu bar's main thread
# (approve click / restart resume) and the post-worker thread (status updates)
# both mutate it; without this a concurrent interleave would drop an approval.
_approved_lock = threading.Lock()

# Mirrors shared/onboarding-ledger.cjs MILESTONES (same order).
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

# Mirrors panel.ts MILESTONE_LABELS.
MILESTONE_LABELS = {
    "environment_checked": "Environment checked",
    "runtime_ready": "Runtime ready",
    "x_connected": "X connected",
    "profile_scanned": "Profile scanned",
    "mode_chosen": "Mode chosen",
    "project_ready": "Project ready",
    "topics_seeded": "Topics seeded",
    "tasks_scheduled": "Tasks scheduled",
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
    # Autopilot is now the Claude Desktop scheduled task, not the legacy launchd job.
    cfg = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(str(Path.home()), ".claude")
    return os.path.exists(
        os.path.join(cfg, "scheduled-tasks", "social-autoposter-autopilot", "SKILL.md")
    )


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
# Background snapshot cache. scripts/snapshot.py reads files but may spawn the
# X-status subprocess (setup_twitter_auth.py -> CDP to Chrome), which must NEVER
# run on the menu bar's UI thread — a hung Chrome would freeze the menu. So a
# daemon thread recomputes and snapshot() returns the last cached value INSTANTLY.
_snap_cache = {"val": None, "at": 0.0}
_snap_lock = threading.Lock()
_snap_refreshing = [False]


def _compute_snapshot_full():
    repo = os.environ.get("SAPS_REPO_DIR") or str(Path.home() / "social-autoposter")
    scripts = os.path.join(repo, "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import snapshot as _snapshot_mod  # scripts/snapshot.py
    return _snapshot_mod.compute()


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


def snapshot():
    """Full snapshot computed DIRECTLY from the stateful files via
    scripts/snapshot.py — the SAME single-source module the MCP shells out to, so
    the two surfaces can't diverge. NO loopback / MCP dependency, so a restarting
    or closed Claude can't freeze or stale the menu (the old tier-1 `loopback_tool`
    blocked the UI thread up to 20s and was the freeze). The heavy compute runs on
    a BACKGROUND thread; this returns the last cached result instantly.

    Tiers: (1) the background-computed local snapshot; (2) the server's last
    persisted `status-summary.json`; (3) the onboarding ledger."""
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
        return out
    summ = read_json("status-summary.json")
    if isinstance(summ, dict) and "projects_total" in summ:
        summ["_live"] = False
        summ["_from_summary"] = True
        return summ
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


# set_autopilot() (the launchd toggle) was removed: the autopilot is now the Claude
# Desktop scheduled task `social-autoposter-autopilot`, managed in the Scheduled tab,
# so the menu bar no longer toggles a launchd job (it mirrors the dashboard instead).


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
ACTIVITY_TTL_SECONDS = float(os.environ.get("SAPS_ACTIVITY_TTL_S", "120"))


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
    if not plan_path:
        plan_path = "/tmp/twitter_cycle_plan_review-queue.json"
    plan = read_plan(plan_path)
    cands = (plan or {}).get("candidates")
    if not cands:
        return None
    return sum(1 for c in cands if c.get("posted") is True)


def review_drafts(plan, batch="review-queue"):
    """Flatten a plan into the card model: only UNDECIDED candidates. A card that's
    posted, terminal (rejected/dead), or already approved is a settled decision and
    must never be re-presented for review (approved ones proceed to post).

    Also excludes cards already sitting in the durable approved-queue (queued or
    posting). The main plan's `approved`/`posted` flags are only stamped once a
    post LANDS, so without this a card the user just approved would re-present
    while it drains — and after a restart the whole un-posted approval backlog
    would re-present (the exact "I approved these already" bug)."""
    approved_ns = approved_queue_active_ns(batch)
    out = []
    for i, c in enumerate(((plan or {}).get("candidates") or [])):
        if c.get("posted") is True or c.get("terminal") is True or c.get("approved") is True:
            continue
        if (i + 1) in approved_ns:
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


# ---- Engagement mode (2026-06-26) -----------------------------------------
# The menu-bar toggle flips between "promotion" (default product-marketing
# pipeline) and "personal_brand" (link-free organic engagement for the user's
# own brand). State is ONE file the cycle wrapper also reads via
# scripts/saps_mode.py; keep the filename + shape in lockstep with it.
MODE_FILE = "mode.json"
MODE_PROMOTION = "promotion"
MODE_PERSONAL_BRAND = "personal_brand"
_VALID_MODES = (MODE_PROMOTION, MODE_PERSONAL_BRAND)


def read_mode():
    """Current engagement mode, defaulting to promotion (prior behavior)."""
    d = read_json(MODE_FILE)
    if isinstance(d, dict):
        m = str(d.get("mode") or "").strip()
        if m in _VALID_MODES:
            return m
    return MODE_PROMOTION


def write_mode(mode):
    """Persist the mode atomically. Returns the written mode, or the current one
    on an invalid value (never raises — a menu click must not crash the app)."""
    if mode not in _VALID_MODES:
        return read_mode()
    try:
        p = Path(state_dir()) / MODE_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"mode": mode}))
        os.replace(str(tmp), str(p))
    except Exception:
        pass
    return mode


def toggle_mode():
    """Flip to the other mode and return the new value."""
    new = (
        MODE_PROMOTION
        if read_mode() == MODE_PERSONAL_BRAND
        else MODE_PERSONAL_BRAND
    )
    return write_mode(new)


def approved_queue_add(batch, n, text="", edited=False, candidate_url="", drop_link=False):
    """Record an approval the INSTANT the user clicks, before any posting. Dedups
    on (batch, n): re-approving a card that's still queued/posting/posted is a
    no-op; a previously FAILED card is reset to queued so it retries.

    drop_link carries the user's "I deleted the link while editing" intent so a
    restart-resumed post honors it too (else the poster re-appends the link)."""
    with _approved_lock:
        d = read_approved_queue()
        for it in d["items"]:
            if it.get("batch") == batch and it.get("n") == n:
                if it.get("status") == "failed":
                    it.update(status="queued", text=text, edited=bool(edited),
                              drop_link=bool(drop_link), error=None, ts=time_iso())
                    _write_approved_queue(d)
                return
        d["items"].append({
            "batch": batch, "n": n, "text": text, "edited": bool(edited),
            "drop_link": bool(drop_link),
            "candidate_url": candidate_url, "status": "queued",
            "error": None, "ts": time_iso(),
        })
        _write_approved_queue(d)


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


def approved_queue_active_ns(batch):
    """Plan indices the user has already approved for this batch — review_drafts()
    excludes these so an approved card is never re-shown. Covers queued/posting
    (in flight) AND posted: relying on the plan's posted flag alone leaves a window
    (and breaks if the plan is regenerated), so the durable queue excludes posted
    cards independently. `failed` is intentionally NOT excluded, so a failed post
    falls back to manual review rather than silently vanishing."""
    return {it.get("n") for it in read_approved_queue()["items"]
            if it.get("batch") == batch and it.get("status") in ("queued", "posting", "posted")}


def post_drafts(batch_id, post=None, edits=None, reject=None, clear_link=None, timeout=900, activity_label=None):
    """Post / reject drafts via the loopback tool. `post` = 1-based numbers to post
    as-is; `edits` = [{n, text}] to rewrite then post; `reject` = numbers to mark
    DONE so they're never shown for review again (not posted); `clear_link` =
    numbers whose link the user removed while editing, so the poster clears
    link_url and does NOT re-append it. Returns the parsed result, or None if the
    loopback is unreachable (Claude Desktop closed)."""
    args = {"batch_id": batch_id, "post": post or [], "edits": edits or [], "reject": reject or [], "clear_link": clear_link or []}
    if activity_label:
        args["__saps_activity_label"] = activity_label
    return loopback_tool("post_drafts", args, timeout=timeout)
