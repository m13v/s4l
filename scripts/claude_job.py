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
import sys
import time
import uuid

# script_tag -> queue type. ONLY pure text->JSON claude calls belong here.
TAG_TO_TYPE = {
    "run-twitter-cycle-queries": "twitter-query",
    "run-twitter-cycle-prep": "twitter-prep",
}

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
DEFAULT_TIMEOUT_S = int(os.environ.get("SAPS_CLAUDE_QUEUE_TIMEOUT", "600"))


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


def cmd_provider(ns) -> int:
    _apply_state_dir_override(ns)
    qtype = TAG_TO_TYPE.get(ns.tag)
    if not qtype:
        # Not a queue-eligible call. Exit non-zero so run_claude.sh's caller
        # treats it as a claude failure and runs its own fallback path.
        print(
            f"[claude_job] tag '{ns.tag}' is not queue-eligible; no provider",
            file=sys.stderr,
        )
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
        print("[claude_job] empty prompt; nothing to enqueue", file=sys.stderr)
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
    _atomic_write(os.path.join(pending_dir(qtype), fname), job)
    print(
        f"[claude_job] enqueued {qtype} job {job_id}; waiting for a scheduled "
        f"task to process it (timeout {ns.timeout}s)",
        file=sys.stderr,
    )

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
                print(
                    f"[claude_job] job {job_id} returned error: "
                    f"{res.get('error', 'unknown')}",
                    file=sys.stderr,
                )
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

    print(
        f"[claude_job] timed out after {ns.timeout}s waiting for job {job_id} "
        f"({qtype}); is the {qtype} scheduled task running?",
        file=sys.stderr,
    )
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
        # Hand the consumer exactly what it needs to do the work and report back.
        print(
            json.dumps(
                {
                    "job_id": job["job_id"],
                    "type": job["type"],
                    "prompt": job["prompt"],
                    "schema": job.get("schema"),
                }
            )
        )
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
    try:
        for name in os.listdir(running_dir()):
            if name.endswith(f"_{job_id}.json"):
                with open(os.path.join(running_dir(), name)) as f:
                    schema_text = json.load(f).get("schema")
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
    print(f"[claude_job] stored result for job {job_id}", file=sys.stderr)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="claude -p queue shim")
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("provider", help="enqueue + block-poll (run by run_claude.sh)")
    pp.add_argument("--tag", required=True)
    pp.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    pp.add_argument("claude_args", nargs=argparse.REMAINDER)
    pp.set_defaults(func=cmd_provider)

    pn = sub.add_parser("next", help="claim oldest pending job of a type")
    pn.add_argument("--type", required=True)
    pn.set_defaults(func=cmd_next)

    pr = sub.add_parser("result", help="store a job's result")
    pr.add_argument("--job", required=True)
    pr.add_argument("--result-file", default="-", help="path to JSON, or - for stdin")
    pr.add_argument("--error", action="store_true", help="record a failure")
    pr.set_defaults(func=cmd_result)

    ns = p.parse_args()
    # argparse.REMAINDER keeps a leading "--"; drop it.
    if getattr(ns, "claude_args", None) and ns.claude_args and ns.claude_args[0] == "--":
        ns.claude_args = ns.claude_args[1:]
    return ns.func(ns)


if __name__ == "__main__":
    sys.exit(main())
