#!/usr/bin/env python3
"""
claude_job.py — queue-backed substitute for `claude -p` on boxes without the
Claude CLI (customer .mcpb installs).

The deterministic pipeline never calls `claude` directly; every invocation goes
through scripts/run_claude.sh. When SAPS_CLAUDE_PROVIDER=queue is set (only on
customer boxes — your own machines leave it unset and keep calling claude -p
directly), run_claude.sh delegates here instead of exec'ing the `claude` binary.
The pipeline is otherwise untouched: it enqueues the same prompt + json-schema it
would have passed to claude, blocks until a result appears, and gets back bytes
shaped exactly like claude's `--output-format json` envelope, so the existing
parsers don't change.

Three roles:
  provider  — (producer side, called by run_claude.sh) extract the prompt (stdin
              or trailing arg) + --json-schema, enqueue a typed job, BLOCK until a
              result lands, then print a claude-json-shaped envelope to stdout.
  next      — (consumer side, called by a Claude Desktop scheduled task) atomically
              claim the oldest pending job of a given type and print it as JSON.
  result    — (consumer side) store the JSON the task produced (validated) and
              unblock the waiting provider.

Queue = plain files under <state_dir>/claude-queue/. No DB, no network.
  state_dir = $SAPS_STATE_DIR or ~/.social-autoposter-mcp

Job-type mapping is by run_claude.sh script_tag. Only the PURE text->JSON calls
are queue-eligible; anything else exits non-zero so the caller's own fallback
runs (e.g. link_tail's mechanical concat). twitter-link-tail is intentionally
NOT mapped: the customer flow skips it for now.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid

# Best-effort menu-bar activity narration. Importable because this script's own
# directory (scripts/) is on sys.path[0] when run as `python3 .../claude_job.py`.
# A failure to import (or to write) must NEVER affect the queue's real work.
try:
    import saps_activity as _activity  # type: ignore
except Exception:  # pragma: no cover - cosmetic only
    _activity = None

# script_tag -> queue type. ONLY pure text->JSON claude calls belong here.
TAG_TO_TYPE = {
    "run-twitter-cycle-queries": "twitter-query",
    "run-twitter-cycle-prep": "twitter-prep",
}

# queue type -> (activity state, label) the menu bar shows while the job is in
# flight. Phase-1 queries drive the X search ("finding threads"); Phase-2b prep is
# the reply drafting. Both the launchd provider (which blocks for minutes) and the
# scheduled-task worker (which does the LLM turn) narrate from this one map.
TYPE_TO_ACTIVITY = {
    "twitter-query": ("scanning", "finding threads"),
    "twitter-prep": ("drafting", "drafting replies"),
}


def _act_write(qtype: str) -> None:
    if _activity is None:
        return
    sl = TYPE_TO_ACTIVITY.get(qtype)
    if not sl:
        return
    try:
        _activity.write(sl[0], sl[1])
    except Exception:
        pass


def _act_clear() -> None:
    if _activity is None:
        return
    try:
        _activity.clear()
    except Exception:
        pass

# claude flags that consume the following argv token as their value, so the
# value is never mistaken for the positional prompt.
VALUE_FLAGS = {
    "--mcp-config",
    "--json-schema",
    "--output-format",
    "--input-format",
    "--model",
    "--fallback-model",
    "--system-prompt",
    "--append-system-prompt",
    "--permission-mode",
    "--allowedTools",
    "--disallowedTools",
    "--add-dir",
    "--session-id",
    "--settings",
}

POLL_INTERVAL_S = 2.0
# Per-call budget the producer waits for ONE claude job (a query or a draft-prep
# reasoning turn). Was 600s, which sat right at the edge of the draft call's real
# ~9-10 min need: on the QA box ~41% of twitter-prep jobs breached 600s and got
# dropped (each drop = a lost draft AND an orphaned over-running worker that
# becomes a leaked agent-mode session). The DIRECT launchd `claude -p` lane has no
# such per-call cap — its draft call just runs inside the 180-min cycle watchdog —
# so 600s here made the queue lane diverge and silently fail where the direct lane
# would not. 1800s (30 min) = 3x the real draft need, matching the sibling Twitter
# engagement claude cap (engage-twitter Phase B gtimeout 1800), which removes the
# drops while staying a bounded per-call value (not the whole-cycle budget).
# COUPLING: reap_stale_claude_sessions.py reaps leaked workers at 2x THIS value and
# MUST stay >= it (a lower reaper would SIGKILL a draft the producer is still
# waiting on). Both read SAPS_CLAUDE_QUEUE_TIMEOUT and both default to 1800 — keep
# the two defaults in lockstep if you change either.
DEFAULT_TIMEOUT_S = int(os.environ.get("SAPS_CLAUDE_QUEUE_TIMEOUT", "1800"))
# Jobs older than this (pending or running) are swept — a job nobody drained in
# this long is a leftover from a timed-out producer or a dead worker, and keeping
# it would feed a stale prompt to a worker much later. Default 2x the timeout.
STALE_TTL_S = int(os.environ.get("SAPS_CLAUDE_QUEUE_STALE_TTL", str(DEFAULT_TIMEOUT_S * 2)))


# --------------------------------------------------------------------------- #
# Queue layout                                                                #
# --------------------------------------------------------------------------- #
def _apply_state_dir_override(ns) -> None:
    """`--state-dir` wins over $SAPS_STATE_DIR. The scheduled-task worker passes
    it explicitly so it always reads the SAME queue the launchd kicker writes to,
    regardless of what env the task session inherits."""
    sd = getattr(ns, "state_dir", None)
    if sd:
        os.environ["SAPS_STATE_DIR"] = sd


def state_dir() -> str:
    return os.environ.get("SAPS_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".social-autoposter-mcp"
    )


def queue_root() -> str:
    return os.path.join(state_dir(), "claude-queue")


def pending_dir(qtype: str) -> str:
    return os.path.join(queue_root(), "pending", qtype)


def running_dir() -> str:
    return os.path.join(queue_root(), "running")


def result_dir() -> str:
    return os.path.join(queue_root(), "result")


# --------------------------------------------------------------------------- #
# Opt-in worker self-reap (2026-06-27)                                         #
# --------------------------------------------------------------------------- #
# A scheduled-task worker turn finishes its one queue iteration but Claude
# Desktop keeps the agent-mode `claude` process warm (`--input-format
# stream-json`), so finished workers pile up and leak RAM. The launchd reaper
# (reap_stale_claude_sessions.py) is the GUARANTEE that bounds this. This opt-in
# path is a faster, source-side trim: once THIS worker is provably done (no work
# to do, or its result is already on disk), terminate OUR OWN session so it never
# becomes part of the standing pool. Strictly off unless SAPS_WORKER_SELF_REAP is
# set, so it ships dormant and cannot destabilize the default behaviour.
#
# Safety properties:
#   * No-op unless the env flag is set.
#   * Only ever targets a process in OUR OWN ancestry that matches the reaper's
#     worker signature (claude-code agent-mode session). The producer side
#     (run-twitter-cycle.sh -> python) has no such ancestor, so a misplaced call
#     there finds nothing and does nothing.
#   * Detached + delayed: a double-forked grandchild waits a few seconds (so the
#     current turn returns and prints its final line normally) before signalling.
#   * Re-verifies the target's cmdline right before SIGTERM, so a recycled PID is
#     never signalled.
#   * Best-effort throughout: never raises into the caller, never changes the
#     worker's exit code, never touches the result already written to disk.
_SELF_REAP_SIG = (
    "claude-code/",
    "/Contents/MacOS/claude ",
    "--input-format stream-json",
    "local-agent-mode-sessions",
)
_SELF_REAP_UUID_RE = re.compile(r"local-agent-mode-sessions/[0-9a-fA-F-]{36}")


def _self_reap_enabled() -> bool:
    return os.environ.get("SAPS_WORKER_SELF_REAP", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _ps_pid_map() -> dict:
    """pid -> (ppid, command) for every process. Empty dict on any failure."""
    out: dict = {}
    try:
        res = subprocess.run(
            ["/bin/ps", "-axo", "pid=,ppid=,command="],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return out
    for line in res.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        out[pid] = (ppid, parts[2])
    return out


def _find_own_session(psmap: dict):
    """Walk OUR ancestry to the nearest claude agent-mode worker session that
    matches the reaper signature. Returns (pid, uuid_token) or None."""
    pid = os.getpid()
    seen: set = set()
    for _ in range(40):  # bounded climb up the process tree
        if pid in seen:
            break
        seen.add(pid)
        ent = psmap.get(pid)
        if not ent:
            break
        ppid, cmd = ent
        if all(t in cmd for t in _SELF_REAP_SIG) and "Helpers/disclaimer" not in cmd:
            m = _SELF_REAP_UUID_RE.search(cmd)
            if m:
                return pid, m.group(0)
        pid = ppid
        if pid <= 1:
            break
    return None


def _maybe_self_reap(delay: float = 6.0) -> None:
    """Opt-in: terminate our own finished worker session. See block comment above."""
    if not _self_reap_enabled():
        return
    try:
        found = _find_own_session(_ps_pid_map())
        if not found:
            return
        target_pid, token = found
        if os.fork() != 0:
            return  # the worker continues + exits normally
    except Exception:
        return
    # child -> detach into its own session, then exit, orphaning the grandchild
    try:
        os.setsid()
        if os.fork() != 0:
            os._exit(0)
    except Exception:
        os._exit(0)
    # grandchild (fully detached): wait, re-verify, signal
    try:
        time.sleep(delay)
        cur = _ps_pid_map().get(target_pid)
        if cur and token in cur[1]:  # same session, not a recycled PID
            try:
                os.kill(target_pid, signal.SIGTERM)
            except OSError:
                pass
    except Exception:
        pass
    os._exit(0)


def _ensure_dirs(qtype: str | None = None) -> None:
    for d in (running_dir(), result_dir()):
        os.makedirs(d, exist_ok=True)
    if qtype:
        os.makedirs(pending_dir(qtype), exist_ok=True)


def _atomic_write(path: str, obj) -> None:
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def _atomic_write_text(path: str, text: str) -> None:
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def _sweep_stale() -> int:
    """Remove pending/running job files older than STALE_TTL_S. Returns count."""
    removed = 0
    now = time.time()
    roots = [running_dir()]
    pend = os.path.join(queue_root(), "pending")
    if os.path.isdir(pend):
        roots += [os.path.join(pend, d) for d in os.listdir(pend)]
    for d in roots:
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if not name.endswith(".json"):
                continue
            fp = os.path.join(d, name)
            try:
                with open(fp) as f:
                    created = json.load(f).get("created_at", 0)
                if now - float(created) > STALE_TTL_S:
                    os.remove(fp)
                    removed += 1
            except Exception:
                continue
    return removed


# --------------------------------------------------------------------------- #
# provider (producer side, run by run_claude.sh)                              #
# --------------------------------------------------------------------------- #
def _parse_claude_args(args: list[str]) -> tuple[str | None, str | None]:
    """Return (trailing_prompt, schema_path) from the verbatim claude argv."""
    schema_path = None
    positionals: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--json-schema":
            schema_path = args[i + 1] if i + 1 < len(args) else None
            i += 2
            continue
        if a in VALUE_FLAGS:
            i += 2
            continue
        if a.startswith("-"):
            i += 1  # boolean flag (-p, --strict-mcp-config, --verbose, ...)
            continue
        positionals.append(a)
        i += 1
    prompt = positionals[-1] if positionals else None
    return prompt, schema_path


def _plog(msg: str) -> None:
    """Provider diagnostics go to a log file, NEVER stderr.

    The pipeline captures this wrapper's output with `2>&1` and parses the FIRST
    JSON value (raw_decode). Anything we print to stderr BEFORE the envelope (e.g.
    an "enqueued, waiting" line) lands ahead of the JSON and breaks the parse with
    "Expecting value: line 1 column 2". So stdout carries ONLY the final envelope
    and stderr stays silent; humans read provider.log instead. (fix 2026-06-24)
    """
    try:
        os.makedirs(queue_root(), exist_ok=True)
        with open(os.path.join(queue_root(), "provider.log"), "a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} pid={os.getpid()} {msg}\n")
    except Exception:
        pass


def cmd_provider(ns) -> int:
    _apply_state_dir_override(ns)
    qtype = TAG_TO_TYPE.get(ns.tag)
    if not qtype:
        # Not a queue-eligible call. Exit non-zero so run_claude.sh's caller
        # treats it as a claude failure and runs its own fallback path.
        _plog(f"tag '{ns.tag}' is not queue-eligible; no provider")
        return 1

    stdin_text = ""
    if not sys.stdin.isatty():
        try:
            stdin_text = sys.stdin.read()
        except Exception:
            stdin_text = ""

    trailing_prompt, schema_path = _parse_claude_args(ns.claude_args)
    prompt = stdin_text if stdin_text.strip() else (trailing_prompt or "")
    if not prompt.strip():
        _plog("empty prompt; nothing to enqueue")
        return 1

    schema_text = None
    if schema_path and os.path.exists(schema_path):
        try:
            with open(schema_path) as f:
                schema_text = f.read()
        except Exception:
            schema_text = None

    job_id = uuid.uuid4().hex
    created = time.time()
    _ensure_dirs(qtype)
    _sweep_stale()  # clear leftovers from prior timed-out producers before enqueuing
    job = {
        "job_id": job_id,
        "type": qtype,
        "tag": ns.tag,
        "prompt": prompt,
        "schema": schema_text,
        "created_at": created,
    }
    # Filename is <created_ns>_<job_id>.json so a plain sorted() listing is FIFO.
    fname = f"{int(created * 1e9):020d}_{job_id}.json"
    pending_path = os.path.join(pending_dir(qtype), fname)
    running_path = os.path.join(running_dir(), fname)
    _atomic_write(pending_path, job)
    _plog(f"enqueued {qtype} job {job_id}; waiting for a scheduled task (timeout {ns.timeout}s)")
    # Narrate the (multi-minute) block to the menu bar. The launchd draft lane has
    # no other activity writer, so without this the box looks idle while it works.
    # Cleared by run-draft-and-publish.sh's exit trap at cycle end (and by the
    # worker's cmd_result), so we deliberately do NOT clear on the success path
    # here — that would flicker the indicator off between the cycle's claude calls.
    _act_write(qtype)

    res_path = os.path.join(result_dir(), f"{job_id}.json")
    deadline = created + ns.timeout
    while time.time() < deadline:
        if os.path.exists(res_path):
            try:
                with open(res_path) as f:
                    res = json.load(f)
            except Exception:
                time.sleep(POLL_INTERVAL_S)
                continue
            os.remove(res_path)
            if res.get("status") == "error":
                _plog(f"job {job_id} returned error: {res.get('error', 'unknown')}")
                return 1
            obj = res.get("result")
            # Emit a claude `--output-format json` shaped envelope so the
            # pipeline's existing raw_decode + structured_output/result parser
            # is byte-compatible.
            envelope = {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "structured_output": obj,
                "result": json.dumps(obj) if not isinstance(obj, str) else obj,
            }
            sys.stdout.write(json.dumps(envelope))
            sys.stdout.flush()
            return 0
        time.sleep(POLL_INTERVAL_S)

    # Don't leak the job: remove it from pending/running so it can't be drafted
    # later with a stale prompt (and so /tmp doesn't accumulate stuck jobs).
    for p in (pending_path, running_path):
        try:
            os.remove(p)
        except OSError:
            pass
    _plog(f"timed out after {ns.timeout}s waiting for job {job_id} ({qtype}); removed the job")
    return 79  # mirror run_claude.sh's "blocked, skip cleanly" exit code


# --------------------------------------------------------------------------- #
# next (consumer side, run by a scheduled task)                               #
# --------------------------------------------------------------------------- #
def cmd_next(ns) -> int:
    _apply_state_dir_override(ns)
    qtype = ns.type
    _ensure_dirs(qtype)
    pend = pending_dir(qtype)
    try:
        names = sorted(os.listdir(pend))
    except FileNotFoundError:
        names = []
    for name in names:
        if not name.endswith(".json") or name.endswith(".tmp"):
            continue
        src = os.path.join(pend, name)
        dst = os.path.join(running_dir(), name)
        try:
            os.rename(src, dst)  # atomic claim; loser of a race gets FileNotFound
        except FileNotFoundError:
            continue
        try:
            with open(dst) as f:
                job = json.load(f)
        except Exception:
            continue
        prompt_file = None
        schema_file = None
        if ns.prompt_file:
            prompt_file = os.path.join(queue_root(), f"prompt-{job['job_id']}.md")
            _atomic_write_text(prompt_file, job.get("prompt") or "")
            job["prompt_file"] = prompt_file
            schema = job.get("schema")
            if schema:
                schema_file = os.path.join(queue_root(), f"schema-{job['job_id']}.json")
                _atomic_write_text(schema_file, schema)
                job["schema_file"] = schema_file
            _atomic_write(dst, job)
        # Narrate the scheduled-task worker's drafting turn to the menu bar. This
        # is the lane that actually runs the LLM; it persists until cmd_result
        # clears it (or the kicker's exit trap does). Covers the box's autopilot.
        _act_write(job.get("type") or qtype)
        # Hand the consumer exactly what it needs to do the work and report back.
        payload = {"job_id": job["job_id"], "type": job["type"]}
        if ns.prompt_file:
            payload["prompt_file"] = prompt_file
            payload["schema_file"] = schema_file
        else:
            payload["prompt"] = job["prompt"]
            payload["schema"] = job.get("schema")
        print(json.dumps(payload))
        return 0
    print(json.dumps({}))  # no work
    return 0


# --------------------------------------------------------------------------- #
# result (consumer side, run by a scheduled task)                             #
# --------------------------------------------------------------------------- #
def _validate_against_schema(obj, schema_text: str | None) -> str | None:
    """Lenient validation. Returns an error string or None if acceptable.

    We deliberately avoid a jsonschema dependency (not guaranteed on the box).
    Enforce only what matters: the result is a JSON object and carries the
    schema's top-level required keys. The prompt itself describes the full shape.
    """
    if schema_text:
        try:
            schema = json.loads(schema_text)
        except Exception:
            schema = None
        if isinstance(schema, dict):
            if schema.get("type") == "object" and not isinstance(obj, dict):
                return "result must be a JSON object"
            required = schema.get("required")
            if isinstance(required, list) and isinstance(obj, dict):
                missing = [k for k in required if k not in obj]
                if missing:
                    return f"result missing required keys: {missing}"
    return None


def cmd_result(ns) -> int:
    _apply_state_dir_override(ns)
    _ensure_dirs()
    # The worker's drafting turn ends here (success or failure); drop the menu-bar
    # label so nothing lingers. The provider's next enqueue re-asserts the right
    # label for the cycle's following claude call, if any.
    _act_clear()
    job_id = ns.job
    # Read the produced result (JSON object) from a file or stdin.
    if ns.result_file and ns.result_file != "-":
        with open(ns.result_file) as f:
            raw = f.read()
    else:
        raw = sys.stdin.read()
    raw = raw.strip()

    running = None
    # Locate the claimed job to recover its schema (filename carries job_id).
    schema_text = None
    cleanup_files: list[str] = []
    try:
        for name in os.listdir(running_dir()):
            if name.endswith(f"_{job_id}.json"):
                with open(os.path.join(running_dir(), name)) as f:
                    job = json.load(f)
                    schema_text = job.get("schema")
                    cleanup_files = [
                        p for p in (job.get("prompt_file"), job.get("schema_file")) if p
                    ]
                running = os.path.join(running_dir(), name)
                break
    except FileNotFoundError:
        running = None

    if ns.error:
        _atomic_write(
            os.path.join(result_dir(), f"{job_id}.json"),
            {"status": "error", "error": raw or "unspecified"},
        )
        if running and os.path.exists(running):
            os.remove(running)
        for p in cleanup_files:
            try:
                os.remove(p)
            except OSError:
                pass
        print(f"[claude_job] recorded error for job {job_id}", file=sys.stderr)
        return 0

    try:
        obj = json.loads(raw)
    except Exception as e:
        print(
            f"[claude_job] result for job {job_id} is not valid JSON: {e}",
            file=sys.stderr,
        )
        return 2

    err = _validate_against_schema(obj, schema_text)
    if err:
        print(f"[claude_job] result for job {job_id} rejected: {err}", file=sys.stderr)
        return 3

    _atomic_write(
        os.path.join(result_dir(), f"{job_id}.json"),
        {"status": "done", "result": obj},
    )
    if running and os.path.exists(running):
        os.remove(running)
    for p in cleanup_files:
        try:
            os.remove(p)
        except OSError:
            pass
    print(f"[claude_job] stored result for job {job_id}", file=sys.stderr)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="claude -p queue shim")
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("provider", help="enqueue + block-poll (run by run_claude.sh)")
    pp.add_argument("--tag", required=True)
    pp.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    pp.add_argument("--state-dir", default=None, help="override $SAPS_STATE_DIR")
    pp.add_argument("claude_args", nargs=argparse.REMAINDER)
    pp.set_defaults(func=cmd_provider)

    pn = sub.add_parser("next", help="claim oldest pending job of a type")
    pn.add_argument("--type", required=True)
    pn.add_argument("--state-dir", default=None, help="override $SAPS_STATE_DIR")
    pn.add_argument(
        "--prompt-file",
        action="store_true",
        help="write the prompt/schema to sidecar files and print their paths",
    )
    pn.set_defaults(func=cmd_next)

    pr = sub.add_parser("result", help="store a job's result")
    pr.add_argument("--job", required=True)
    pr.add_argument("--result-file", default="-", help="path to JSON, or - for stdin")
    pr.add_argument("--error", action="store_true", help="record a failure")
    pr.add_argument("--state-dir", default=None, help="override $SAPS_STATE_DIR")
    pr.set_defaults(func=cmd_result)

    ns = p.parse_args()
    # argparse.REMAINDER keeps a leading "--"; drop it.
    if getattr(ns, "claude_args", None) and ns.claude_args and ns.claude_args[0] == "--":
        ns.claude_args = ns.claude_args[1:]
    return ns.func(ns)


if __name__ == "__main__":
    sys.exit(main())
