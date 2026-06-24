#!/usr/bin/env python3
"""Append one redacted memory/process snapshot as JSONL.

This is intentionally short-lived. A scheduler can run it once per minute and
the process exits after writing a single line, so the observer does not become
another resident background service.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any


REPO_DIR = Path(os.environ.get("REPO_DIR", Path(__file__).resolve().parents[1]))
DEFAULT_OUTPUT = REPO_DIR / "skill" / "logs" / "memory-snapshots.jsonl"

SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_-]+"),
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]+"),
    re.compile(r"gh[pousr]_[A-Za-z0-9_]+"),
    re.compile(r"xox[abprs]-[A-Za-z0-9-]+"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)(\"?(?:api[_-]?key|token|secret|password|authorization|anthropic_api_key)\"?\s*[:=]\s*\"?)([^\"\\s,}]+)"),
]


def run(args: list[str], timeout: float = 5.0) -> str:
    try:
        proc = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.stdout or ""
    except Exception:
        return ""


def redact(value: str) -> str:
    out = value
    for pattern in SECRET_PATTERNS:
        if pattern.groups >= 2:
            out = pattern.sub(lambda m: f"{m.group(1)}REDACTED", out)
        elif pattern.groups == 1:
            out = pattern.sub(lambda m: f"{m.group(1)}REDACTED", out)
        else:
            out = pattern.sub("REDACTED", out)
    return out


def shorten(value: str, max_len: int = 360) -> str:
    value = " ".join(redact(value).split())
    if len(value) <= max_len:
        return value
    return value[: max_len - 3] + "..."


def mb(kb: int | float) -> float:
    return round(float(kb) / 1024.0, 1)


def parse_ps() -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]], dict[int, list[int]]]:
    rows: list[dict[str, Any]] = []
    by_pid: dict[int, dict[str, Any]] = {}
    children: dict[int, list[int]] = {}
    out = run(["ps", "-axo", "pid=,ppid=,pgid=,pcpu=,rss=,command="], timeout=8.0)
    for line in out.splitlines():
        parts = line.strip().split(None, 5)
        if len(parts) < 6:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
            pgid = int(parts[2])
            cpu = float(parts[3])
            rss_kb = int(parts[4])
        except ValueError:
            continue
        command = parts[5]
        row = {
            "pid": pid,
            "ppid": ppid,
            "pgid": pgid,
            "cpu_pct": cpu,
            "rss_mb": mb(rss_kb),
            "rss_kb": rss_kb,
            "cmd": shorten(command),
            "_command_raw": command,
        }
        rows.append(row)
        by_pid[pid] = row
        children.setdefault(ppid, []).append(pid)
    return rows, by_pid, children


def process_tree(root_pid: int, by_pid: dict[int, dict[str, Any]], children: dict[int, list[int]]) -> dict[str, Any] | None:
    root = by_pid.get(root_pid)
    if not root:
        return None
    seen: set[int] = set()
    stack = [root_pid]
    pids: list[int] = []
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        if pid not in by_pid:
            continue
        pids.append(pid)
        stack.extend(children.get(pid, []))
    total_kb = sum(int(by_pid[pid]["rss_kb"]) for pid in pids)
    return {
        "pid": root_pid,
        "rss_mb": root["rss_mb"],
        "tree_rss_mb": mb(total_kb),
        "descendant_count": max(0, len(pids) - 1),
        "cmd": root["cmd"],
        "pids": sorted(pids),
    }


def parse_vm_stat() -> dict[str, Any]:
    out = run(["vm_stat"], timeout=5.0)
    if not out:
        return {}
    page_size = 4096
    first = out.splitlines()[0] if out.splitlines() else ""
    match = re.search(r"page size of (\d+) bytes", first)
    if match:
        page_size = int(match.group(1))
    pages: dict[str, int] = {}
    for line in out.splitlines()[1:]:
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        key = key.strip().strip('"').lower().replace(" ", "_")
        num_match = re.search(r"(-?\d+)", raw.replace(".", ""))
        if num_match:
            pages[key] = int(num_match.group(1))
    sysctl_bin = "/usr/sbin/sysctl" if Path("/usr/sbin/sysctl").exists() else "sysctl"
    total_bytes_raw = run([sysctl_bin, "-n", "hw.memsize"], timeout=2.0).strip()
    try:
        total_mb = round(int(total_bytes_raw) / 1024 / 1024, 1)
    except ValueError:
        total_mb = None

    def pages_mb(name: str) -> float:
        return round(pages.get(name, 0) * page_size / 1024 / 1024, 1)

    return {
        "page_size": page_size,
        "total_mb": total_mb,
        "free_mb": pages_mb("pages_free"),
        "active_mb": pages_mb("pages_active"),
        "inactive_mb": pages_mb("pages_inactive"),
        "speculative_mb": pages_mb("pages_speculative"),
        "wired_mb": pages_mb("pages_wired_down"),
        "compressed_mb": pages_mb("pages_occupied_by_compressor"),
        "swapins": pages.get("swapins"),
        "swapouts": pages.get("swapouts"),
        "pages": pages,
    }


def launchd_jobs(by_pid: dict[int, dict[str, Any]], children: dict[int, list[int]]) -> list[dict[str, Any]]:
    out = run(["launchctl", "list"], timeout=5.0)
    jobs: list[dict[str, Any]] = []
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) != 3:
            continue
        pid_s, status_s, label = parts
        if not label.startswith("com.m13v."):
            continue
        try:
            pid = int(pid_s)
        except ValueError:
            pid = None
        try:
            status: int | str = int(status_s)
        except ValueError:
            status = status_s
        job: dict[str, Any] = {"label": label, "pid": pid, "status": status}
        if pid is not None:
            tree = process_tree(pid, by_pid, children)
            if tree:
                job.update(tree)
        jobs.append(job)
    jobs.sort(key=lambda j: (j.get("pid") is None, j["label"]))
    return jobs


GROUPS = {
    "social_autoposter_repo": ["social-autoposter"],
    "social_autoposter_mcp": ["/social-autoposter/mcp/dist/index.js"],
    "dashboard_server": ["social-autoposter/bin/server.js", " node bin/server.js", "/node bin/server.js"],
    "claude_cli": ["/claude ", " claude --", "/claude.app/Contents/MacOS/claude"],
    "browser_harness": [".claude/browser-profiles/browser-harness"],
    "remote_macos_mcp": ["macos-use-remote", "mcp-server-macos-use", "Codex Computer Use.app"],
    "twitter_browser_pipeline": ["twitter_browser.py", "run-twitter-cycle"],
}


def group_summaries(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summaries: dict[str, Any] = {}
    for name, needles in GROUPS.items():
        matched = [row for row in rows if any(needle in row["_command_raw"] for needle in needles)]
        total_kb = sum(int(row["rss_kb"]) for row in matched)
        top = sorted(matched, key=lambda row: int(row["rss_kb"]), reverse=True)[:10]
        summaries[name] = {
            "count": len(matched),
            "rss_mb": mb(total_kb),
            "top_pids": [
                {
                    "pid": row["pid"],
                    "ppid": row["ppid"],
                    "rss_mb": row["rss_mb"],
                    "cmd": row["cmd"],
                }
                for row in top
            ],
        }
    return summaries


def active_claude_sidecars(by_pid: dict[int, dict[str, Any]], children: dict[int, list[int]]) -> list[dict[str, Any]]:
    sidecars: list[dict[str, Any]] = []
    for path in sorted(Path("/tmp/sa-active-claude").glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except Exception as exc:
            sidecars.append({"path": str(path), "error": str(exc)})
            continue
        wrapper_pid = data.get("wrapper_pid")
        if isinstance(wrapper_pid, int):
            data["wrapper_tree"] = process_tree(wrapper_pid, by_pid, children)
        data["path"] = str(path)
        sidecars.append(data)
    return sidecars


def _json_file_metadata(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
    except OSError:
        return {"path": str(path), "exists": False}
    meta: dict[str, Any] = {
        "path": str(path),
        "size_bytes": stat.st_size,
        "mtime": dt.datetime.fromtimestamp(stat.st_mtime, dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
    }
    try:
        data = json.loads(path.read_text())
    except Exception as exc:
        meta["error"] = str(exc)
        return meta
    for key in ("job_id", "type", "tag", "created_at", "status", "error"):
        if key in data:
            meta[key] = shorten(str(data[key]), 160)
    if isinstance(data.get("created_at"), (int, float)):
        meta["age_sec"] = round(max(0.0, dt.datetime.now().timestamp() - float(data["created_at"])), 1)
    return meta


def claude_queue_summary() -> dict[str, Any]:
    root = Path(os.environ.get("SAPS_STATE_DIR", str(Path.home() / ".social-autoposter-mcp"))) / "claude-queue"
    summary: dict[str, Any] = {
        "path": str(root),
        "exists": root.exists(),
        "pending_total": 0,
        "pending_by_type": {},
        "running_total": 0,
        "result_total": 0,
        "oldest_age_sec": None,
        "running_jobs": [],
        "oldest_pending": [],
    }
    if not root.exists():
        return summary

    ages: list[float] = []
    pending_root = root / "pending"
    if pending_root.exists():
        for qtype_dir in sorted(p for p in pending_root.iterdir() if p.is_dir()):
            files = sorted(qtype_dir.glob("*.json"))
            summary["pending_by_type"][qtype_dir.name] = len(files)
            summary["pending_total"] += len(files)
            for path in files[:5]:
                meta = _json_file_metadata(path)
                if isinstance(meta.get("age_sec"), (int, float)):
                    ages.append(float(meta["age_sec"]))
                if len(summary["oldest_pending"]) < 10:
                    summary["oldest_pending"].append(meta)

    running_files = sorted((root / "running").glob("*.json")) if (root / "running").exists() else []
    result_files = sorted((root / "result").glob("*.json")) if (root / "result").exists() else []
    summary["running_total"] = len(running_files)
    summary["result_total"] = len(result_files)
    for path in running_files[:10]:
        meta = _json_file_metadata(path)
        if isinstance(meta.get("age_sec"), (int, float)):
            ages.append(float(meta["age_sec"]))
        summary["running_jobs"].append(meta)
    summary["oldest_age_sec"] = max(ages) if ages else None
    provider_log = root / "provider.log"
    if provider_log.exists():
        try:
            stat = provider_log.stat()
            summary["provider_log"] = {
                "path": str(provider_log),
                "size_bytes": stat.st_size,
                "mtime": dt.datetime.fromtimestamp(stat.st_mtime, dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
            }
        except OSError:
            pass
    return summary


def lock_queue_summary(by_pid: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    now = dt.datetime.now().timestamp()
    locks: list[dict[str, Any]] = []
    names: set[str] = set()
    for path in Path("/tmp").glob("social-autoposter-*.lock"):
        if path.is_dir():
            names.add(path.name.removeprefix("social-autoposter-").removesuffix(".lock"))
    for path in Path("/tmp").glob("social-autoposter-*.lock.queue"):
        if path.is_dir():
            names.add(path.name.removeprefix("social-autoposter-").removesuffix(".lock.queue"))

    for name in sorted(names):
        lock_dir = Path("/tmp") / f"social-autoposter-{name}.lock"
        queue_dir = Path("/tmp") / f"social-autoposter-{name}.lock.queue"
        item: dict[str, Any] = {"name": name, "locked": lock_dir.exists(), "queue_depth": 0}
        if lock_dir.exists():
            try:
                stat = lock_dir.stat()
                item["age_sec"] = round(max(0.0, now - stat.st_mtime), 1)
            except OSError:
                pass
            try:
                holder_pid = int((lock_dir / "pid").read_text().strip())
                item["holder_pid"] = holder_pid
                item["holder_alive"] = holder_pid in by_pid
                if holder_pid in by_pid:
                    item["holder_rss_mb"] = by_pid[holder_pid]["rss_mb"]
                    item["holder_cmd"] = by_pid[holder_pid]["cmd"]
            except Exception:
                item["holder_pid"] = None
            try:
                expires_at = int((lock_dir / "expires_at").read_text().strip())
                item["expires_in_sec"] = expires_at - int(now)
            except Exception:
                pass
        if queue_dir.exists():
            tickets = sorted(p for p in queue_dir.iterdir() if p.is_file())
            item["queue_depth"] = len(tickets)
            queued: list[dict[str, Any]] = []
            for ticket in tickets[:10]:
                entry: dict[str, Any] = {"ticket": ticket.name}
                try:
                    pid = int(ticket.read_text().strip())
                    entry["pid"] = pid
                    entry["alive"] = pid in by_pid
                    if pid in by_pid:
                        entry["cmd"] = by_pid[pid]["cmd"]
                except Exception:
                    pass
                queued.append(entry)
            item["queued"] = queued
        locks.append(item)
    return locks


def scheduled_tasks_summary() -> dict[str, Any]:
    summary: dict[str, Any] = {
        "skill_files": [],
        "registries": [],
        "enabled_total": 0,
        "disabled_total": 0,
    }
    scheduled_root = Path.home() / ".claude" / "scheduled-tasks"
    if scheduled_root.exists():
        for path in sorted(scheduled_root.glob("*/SKILL.md")):
            summary["skill_files"].append({"id": path.parent.name, "path": str(path)})

    sessions_root = Path.home() / "Library" / "Application Support" / "Claude" / "claude-code-sessions"
    if not sessions_root.exists():
        return summary
    for registry in sorted(sessions_root.glob("**/scheduled-tasks.json"))[:50]:
        reg: dict[str, Any] = {"path": str(registry), "tasks": []}
        try:
            data = json.loads(registry.read_text())
        except Exception as exc:
            reg["error"] = str(exc)
            summary["registries"].append(reg)
            continue
        for task in data.get("scheduledTasks", [])[:30]:
            enabled = bool(task.get("enabled"))
            if enabled:
                summary["enabled_total"] += 1
            else:
                summary["disabled_total"] += 1
            reg["tasks"].append({
                "id": task.get("id"),
                "enabled": enabled,
                "fireAt": task.get("fireAt"),
                "lastRunAt": task.get("lastRunAt"),
                "lastScheduledFor": task.get("lastScheduledFor"),
                "cwd": shorten(str(task.get("cwd", "")), 220),
                "filePath": shorten(str(task.get("filePath", "")), 220),
            })
        summary["registries"].append(reg)
    return summary


def queues_summary(by_pid: dict[int, dict[str, Any]]) -> dict[str, Any]:
    return {
        "claude_queue": claude_queue_summary(),
        "social_locks": lock_queue_summary(by_pid),
        "scheduled_tasks": scheduled_tasks_summary(),
    }


def rotate_log(path: Path, max_bytes: int, keep: int = 3) -> None:
    if max_bytes <= 0:
        return
    try:
        if not path.exists() or path.stat().st_size < max_bytes:
            return
        for idx in range(keep - 1, 0, -1):
            src = path.with_name(f"{path.name}.{idx}")
            dst = path.with_name(f"{path.name}.{idx + 1}")
            if src.exists():
                src.replace(dst)
        path.replace(path.with_name(f"{path.name}.1"))
    except Exception:
        return


def build_snapshot(top_n: int) -> dict[str, Any]:
    rows, by_pid, children = parse_ps()
    top = sorted(rows, key=lambda row: int(row["rss_kb"]), reverse=True)[:top_n]
    return {
        "ts": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
        "hostname": socket.gethostname(),
        "repo_dir": str(REPO_DIR),
        "memory": parse_vm_stat(),
        "process_count": len(rows),
        "top_rss": [
            {
                "pid": row["pid"],
                "ppid": row["ppid"],
                "pgid": row["pgid"],
                "cpu_pct": row["cpu_pct"],
                "rss_mb": row["rss_mb"],
                "cmd": row["cmd"],
            }
            for row in top
        ],
        "groups": group_summaries(rows),
        "launchd_jobs": launchd_jobs(by_pid, children),
        "active_claude_sidecars": active_claude_sidecars(by_pid, children),
        "queues": queues_summary(by_pid),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=os.environ.get("SAPS_MEMORY_SNAPSHOT_LOG", str(DEFAULT_OUTPUT)))
    parser.add_argument("--top", type=int, default=int(os.environ.get("SAPS_MEMORY_TOP_N", "30")))
    parser.add_argument("--max-bytes", type=int, default=int(os.environ.get("SAPS_MEMORY_MAX_BYTES", str(100 * 1024 * 1024))))
    args = parser.parse_args()

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    rotate_log(output, args.max_bytes)

    snapshot = build_snapshot(max(1, args.top))
    with output.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(snapshot, sort_keys=True, separators=(",", ":")) + "\n")

    groups = snapshot.get("groups", {})
    print(
        "memory_snapshot "
        f"ts={snapshot['ts']} "
        f"social_repo_mb={groups.get('social_autoposter_repo', {}).get('rss_mb', 0)} "
        f"mcp_mb={groups.get('social_autoposter_mcp', {}).get('rss_mb', 0)} "
        f"output={output}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
