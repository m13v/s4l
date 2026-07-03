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
import time
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
        "purgeable_mb": pages_mb("pages_purgeable"),
        "compressed_mb": pages_mb("pages_occupied_by_compressor"),
        "swapins": pages.get("swapins"),
        "swapouts": pages.get("swapouts"),
        "pages": pages,
    }


def memory_pressure_pct_free() -> float | None:
    """macOS-authoritative availability signal.

    `memory_pressure` prints "System-wide memory free percentage: N%". THIS is the
    number to trust for "is the box starved?" — NOT vm_stat "pages free", which sits
    near-zero by design (macOS hoards RAM as cached/inactive/compressed pages, so a
    tiny "free" is the normal healthy state, not starvation). Best-effort; returns
    None if the tool is unavailable so the caller can fall back to a pages estimate.
    """
    out = run(["/usr/bin/memory_pressure"], timeout=6.0)
    if not out:
        return None
    m = re.search(r"free percentage:\s*([\d.]+)", out)
    return round(float(m.group(1)), 1) if m else None


def swap_used_mb() -> float | None:
    """Active swap in MB from vm.swapusage (a real-pressure corroborator)."""
    sysctl_bin = "/usr/sbin/sysctl" if Path("/usr/sbin/sysctl").exists() else "sysctl"
    out = run([sysctl_bin, "-n", "vm.swapusage"], timeout=2.0)
    m = re.search(r"used\s*=\s*([\d.]+)M", out or "")
    return round(float(m.group(1)), 1) if m else None


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


S4L_MCP_ENTRYPOINT = str(REPO_DIR / "mcp" / "dist" / "index.js")


def _command(row: dict[str, Any]) -> str:
    return str(row.get("_command_raw", ""))


def _node_running_script(command: str, script_path: str) -> bool:
    return bool(
        re.search(
            rf"(^|\s)(?:/[^ \t]+/)?node\s+{re.escape(script_path)}(?:\s|$)",
            command,
        )
    )


def _is_social_autoposter_mcp_server(row: dict[str, Any]) -> bool:
    return _node_running_script(_command(row), S4L_MCP_ENTRYPOINT)


def _is_configured_with_social_autoposter_mcp(row: dict[str, Any]) -> bool:
    command = _command(row)
    return S4L_MCP_ENTRYPOINT in command and not _is_social_autoposter_mcp_server(row)


def _is_dashboard_server(row: dict[str, Any]) -> bool:
    command = _command(row)
    return (
        _node_running_script(command, str(REPO_DIR / "bin" / "server.js"))
        or bool(re.search(r"(^|\s)(?:/[^ \t]+/)?node\s+bin/server\.js(?:\s|$)", command))
    )


def _is_claude_cli(row: dict[str, Any]) -> bool:
    command = _command(row)
    return (
        "/claude.app/Contents/MacOS/claude" in command
        or bool(re.search(r"(^|\s)(?:/[^ \t]+/)?claude(?:\s|$)", command))
    )


def _is_browser_harness(row: dict[str, Any]) -> bool:
    return ".claude/browser-profiles/browser-harness" in _command(row)


def _is_remote_macos_mcp_server(row: dict[str, Any]) -> bool:
    command = _command(row)
    if "SkyComputerUseService" in command or "SkyComputerUseClient" in command:
        return True
    if bool(re.search(r"(^|\s)(?:/[^ \t]+/)?mcp-server-macos-use(?:\s|$)", command)):
        return True
    if "mcp-server-macos-use" in command and re.match(r"^(?:/[^ \t]+/)?ssh(?:\s|$)", command):
        return True
    return bool(
        re.search(
            r"(^|\s)(?:/bin/(?:bash|sh|zsh)\s+)?[^ \t]*macos-use-remote[^ \t]*(?:\s|$)",
            command,
        )
        and not _is_claude_cli(row)
    )


def _is_configured_with_remote_macos_mcp(row: dict[str, Any]) -> bool:
    command = _command(row)
    return (
        any(
            needle in command
            for needle in ("macos-use-remote", "mcp-server-macos-use", "mcp__computer-use")
        )
        and not _is_remote_macos_mcp_server(row)
    )


def _is_twitter_browser_pipeline(row: dict[str, Any]) -> bool:
    command = _command(row)
    return "twitter_browser.py" in command or "run-twitter-cycle" in command


def _is_social_autoposter_repo_process(row: dict[str, Any]) -> bool:
    command = _command(row)
    repo = str(REPO_DIR)
    if _is_configured_with_social_autoposter_mcp(row):
        return False
    if (
        _is_social_autoposter_mcp_server(row)
        or _is_dashboard_server(row)
        or _is_twitter_browser_pipeline(row)
    ):
        return True
    return any(
        f"{repo}/{subdir}/" in command
        for subdir in ("bin", "mcp", "scripts", "setup", "skill")
    )


GROUP_MATCHERS = {
    "social_autoposter_repo_processes": _is_social_autoposter_repo_process,
    "social_autoposter_mcp_servers": _is_social_autoposter_mcp_server,
    "sessions_configured_social_autoposter_mcp": _is_configured_with_social_autoposter_mcp,
    "dashboard_server": _is_dashboard_server,
    "claude_cli": _is_claude_cli,
    "browser_harness": _is_browser_harness,
    "remote_macos_mcp_servers": _is_remote_macos_mcp_server,
    "sessions_configured_remote_macos_mcp": _is_configured_with_remote_macos_mcp,
    "twitter_browser_pipeline": _is_twitter_browser_pipeline,
}


def group_summaries(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summaries: dict[str, Any] = {}
    for name, matcher in GROUP_MATCHERS.items():
        matched = [row for row in rows if matcher(row)]
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
    root = Path(os.environ.get("S4L_STATE_DIR", str(Path.home() / ".social-autoposter-mcp"))) / "claude-queue"
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
    # The producer's drain latch: consecutive_timeouts>=1 means the scheduled-task
    # worker stopped draining (the definitive phase2b-stall signal). Surfacing it
    # here lets the heartbeat carry it server-side, so a stall is visible centrally
    # without SSHing the box. See claude_job.py _bump_drain_timeout / _clear_drain.
    drain_path = root / "drain-status.json"
    if drain_path.exists():
        try:
            ds = json.loads(drain_path.read_text())
            summary["drain_status"] = {
                "consecutive_timeouts": int(ds.get("consecutive_timeouts", 0) or 0),
                "last_success_at": ds.get("last_success_at"),
                "last_timeout_at": ds.get("last_timeout_at"),
            }
        except (OSError, ValueError, TypeError):
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

    # "Claude*": the host app can run with a custom --user-data-dir (per-account
    # dirs like "Claude-mediar"), putting registries outside plain "Claude/".
    # Keep in sync with scripts/schedule_state.py::SCHED_REGISTRY_GLOB.
    app_support = Path.home() / "Library" / "Application Support"
    registries = sorted(
        app_support.glob("Claude*/claude-code-sessions/**/scheduled-tasks.json")
    )
    if not registries:
        return summary
    for registry in registries[:50]:
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
        "claude_desktop_version": claude_desktop_version(),
        "reaper": reaper_status(),
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


def build_summary() -> dict[str, Any]:
    """Slim, cheap snapshot for the heartbeat body.

    Skips the heavier sections (launchd jobs, sidecars, lock queues, per-group
    top_pids) so the MCP can compute it inline on every 15-min heartbeat. Just
    the host memory totals, per-group RSS counts, the single biggest process,
    and the claude-queue depth — enough to spot a leaking box centrally.
    """
    rows, by_pid, children = parse_ps()
    mem = parse_vm_stat()
    total = mem.get("total_mb")
    # macOS memory accounting: "available" headroom is what can be handed to a
    # process WITHOUT paging — free + inactive + speculative + purgeable, all of
    # which the OS reclaims on demand. The real footprint is total - available.
    # Do NOT use vm_stat "pages free" as the headline: it is near-zero by design
    # (macOS keeps RAM full of reclaimable cache), so total-minus-free reads ~99%
    # and falsely looks like starvation. That trap caused a wrong OOM call once.
    avail_parts = [mem.get(k) for k in ("free_mb", "inactive_mb", "speculative_mb", "purgeable_mb")]
    available = (
        round(sum(p for p in avail_parts if isinstance(p, (int, float))), 1) if mem else None
    )
    used = (
        round(float(total) - float(available), 1)
        if isinstance(total, (int, float)) and isinstance(available, (int, float))
        else None
    )
    # pct_free is kept CONSISTENT with the MB figures above (available / total) so a
    # reader never sees two contradictory percentages. `pressure_pct` is the separate
    # OS pressure gauge from `memory_pressure` (counts evictable file cache as free, so
    # it reads higher) — it is the most robust starvation detector, so `health` is
    # derived from it, falling back to pct_free only when the tool is unavailable.
    pct_free = (
        round(available / total * 100, 1)
        if isinstance(total, (int, float)) and isinstance(available, (int, float)) and total
        else None
    )
    pressure_pct = memory_pressure_pct_free()
    basis = pressure_pct if pressure_pct is not None else pct_free
    if basis is None:
        health = "unknown"
    elif basis < 10:
        health = "critical"
    elif basis < 20:
        health = "warn"
    else:
        health = "ok"
    swap_used = swap_used_mb()
    slim_groups = {
        name: {"count": g["count"], "rss_mb": g["rss_mb"]}
        for name, g in group_summaries(rows).items()
    }
    top = sorted(rows, key=lambda row: int(row["rss_kb"]), reverse=True)[:1]
    top_proc = (
        {"pid": top[0]["pid"], "rss_mb": top[0]["rss_mb"], "cmd": top[0]["cmd"]}
        if top
        else None
    )
    cq = claude_queue_summary()
    ds = cq.get("drain_status") or {}
    oldest = cq.get("oldest_age_sec")
    consec = int(ds.get("consecutive_timeouts", 0) or 0)
    # Mirror the MCP's autopilotStalled(): a latched producer timeout, OR a draft
    # job that has sat unclaimed past 180s, means no scheduled-task worker is
    # draining the queue. Carrying this on the heartbeat makes a phase2b stall
    # visible in installation_resource_samples without SSHing the box.
    stalled = bool(consec >= 1 or (isinstance(oldest, (int, float)) and oldest > 180))
    return {
        "ts": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
        "hostname": socket.gethostname(),
        "app_version": _app_version(),
        "claude_desktop_version": claude_desktop_version(),
        "reaper": reaper_status(),
        "twitter_cycle": twitter_cycle_status(),
        "process_count": len(rows),
        "mem": {
            "total_mb": total,
            "used_mb": used,
            "available_mb": available,
            "pct_free": pct_free,
            "pressure_pct": pressure_pct,
            "health": health,
            "wired_mb": mem.get("wired_mb"),
            "compressed_mb": mem.get("compressed_mb"),
            "swap_used_mb": swap_used,
            "swapouts": mem.get("swapouts"),
        },
        "groups": slim_groups,
        "top": top_proc,
        "claude_queue": {
            "pending": cq.get("pending_total", 0),
            "running": cq.get("running_total", 0),
            "oldest_age_sec": oldest,
            "stalled": stalled,
            "consecutive_timeouts": consec,
            "last_success_at": ds.get("last_success_at"),
        },
    }


def _app_version() -> str | None:
    """Plugin version from manifest.json / package.json at the repo root."""
    for name in ("manifest.json", "package.json"):
        try:
            data = json.loads((REPO_DIR / name).read_text())
        except Exception:
            continue
        v = data.get("version")
        if v:
            return str(v).strip() or None
    return None


def claude_desktop_version() -> str | None:
    """CFBundleShortVersionString of the Claude Desktop app, or None if not found.

    This is the ONE variable we could not answer for Karol: the reaper's blind spot
    (a newer Claude Code changed the session-path shape so UUID_RE stopped matching)
    is version-correlated, so we now stamp the Desktop version on every heartbeat +
    snapshot. Reading Info.plist via plistlib is more robust than shelling `defaults`
    (works headless, no user-defaults cache). Checks both the system-wide and the
    per-user install locations. Best-effort: never raises."""
    candidates = [
        Path("/Applications/Claude.app/Contents/Info.plist"),
        Path.home() / "Applications" / "Claude.app" / "Contents" / "Info.plist",
    ]
    for plist in candidates:
        try:
            if not plist.exists():
                continue
            import plistlib

            with plist.open("rb") as f:
                data = plistlib.load(f)
            v = data.get("CFBundleShortVersionString") or data.get("CFBundleVersion")
            if v:
                return str(v).strip() or None
        except Exception:
            continue
    return None


def reaper_status() -> dict[str, Any] | None:
    """Last cycle written by reap_stale_claude_sessions.py::write_status(), or None.

    The reaper is a SEPARATE launchd job (com.m13v.social-claude-reaper) whose stderr
    only lands in a local file, so its outcome was invisible centrally. It now drops a
    reaper-status.json each cycle; we carry it on the heartbeat so a stuck/blind reaper
    (e.g. ps_timed_out, or unparsed_worker_procs climbing while it kills nothing — the
    Karol failure mode) is visible in installation_resource_samples. Also surfaces
    staleness: if the file has not been touched recently the reaper itself may be dead."""
    path = (
        Path(os.environ.get("S4L_STATE_DIR", str(Path.home() / ".social-autoposter-mcp")))
        / "claude-queue"
        / "reaper-status.json"
    )
    try:
        if not path.exists():
            return None
        ds = json.loads(path.read_text())
        age = None
        try:
            age = round(time.time() - path.stat().st_mtime, 1)
        except OSError:
            pass
        return {
            "ts": ds.get("ts"),
            "age_sec": age,  # seconds since the reaper last wrote — >120s hints it is dead
            "mode": ds.get("mode"),
            "claude_killed": ds.get("claude_killed"),
            "macos_mcp_killed": ds.get("macos_mcp_killed"),
            "worker_probe_seen": ds.get("worker_probe_seen"),
            "reapable_workers": ds.get("reapable_workers"),
            "unparsed_worker_procs": ds.get("unparsed_worker_procs"),
            "unparsed_samples": ds.get("unparsed_samples"),
            "cwd_fallback_admitted": ds.get("cwd_fallback_admitted"),
            "s4l_worker_cwd_seen": ds.get("s4l_worker_cwd_seen"),
            "macos_mcp_seen": ds.get("macos_mcp_seen"),
            "leaked_groups": ds.get("leaked_groups"),
            "ps_timed_out": ds.get("ps_timed_out"),
            "snapshot_empty": ds.get("snapshot_empty"),
        }
    except (OSError, ValueError, TypeError):
        return None


def twitter_cycle_status() -> dict[str, Any] | None:
    """Tail of the newest twitter-cycle log, carried on the heartbeat.

    The launchd-driven run-twitter-cycle.sh logs ONLY to a local file, so the
    cycle's phase progress was invisible centrally: the 2026-07-03 Karol
    first-draft investigation had a 27-minute blind window (cycle start 22:30 ->
    cards 22:57) with no way to see which phase the time went to. This block
    makes "where is the cycle right now" a one-query answer. Best-effort."""
    try:
        logs = sorted(
            (REPO_DIR / "skill" / "logs").glob("twitter-cycle-20*.log"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not logs:
            return None
        p = logs[0]
        lines = [ln.strip() for ln in _tail_lines(p, 8) if ln.strip()]
        return {
            "log": p.name,
            "age_sec": round(time.time() - p.stat().st_mtime, 1),
            "last_lines": [ln[:200] for ln in lines[-3:]],
        }
    except Exception:
        return None


def _tail_lines(path: Path, n: int, approx_line_bytes: int = 4096) -> list[str]:
    """Return the last `n` lines of a possibly-large file without reading it all.
    Reads a bounded tail window (n * approx_line_bytes) from the end. Best-effort."""
    try:
        size = path.stat().st_size
        want = min(size, n * approx_line_bytes)
        with path.open("rb") as f:
            f.seek(size - want)
            data = f.read()
        text = data.decode("utf-8", "replace")
        lines = text.splitlines()
        # Drop a possibly-truncated first line when we did not start at byte 0.
        if want < size and lines:
            lines = lines[1:]
        return lines[-n:]
    except Exception:
        return []


def _maybe_leak_alert(output: Path, current: dict[str, Any]) -> None:
    """Fire a Sentry event when a monitored process group climbs monotonically for
    N consecutive snapshots — the leak SHAPE that took down Karol's box (claude
    workers + remote-macos-use MCP servers ratcheting up unbounded). This catches a
    leak while it is GROWING, hours before the box freezes, instead of us finding out
    from a support ticket. Best-effort + rate-limited by a cooldown file so a genuine
    ongoing leak pages once per window, not every minute.

    Runs on the JSONL path (every ~minute), reading its own recent history from the
    file just written, so it needs no extra state beyond a small cooldown marker."""
    # Watch claude_cli (the runaway worker fan-out) and the claude sessions
    # CONFIGURED with the remote-macos-use MCP. Karol's 06-30 double-leak lived
    # entirely in these two: claude_cli 289 + sessions_configured_remote_macos_mcp
    # 280 at peak, while remote_macos_mcp_servers (the standalone server procs)
    # stayed 0 the whole time. Watching the server group would have been blind.
    groups_to_watch = ("claude_cli", "sessions_configured_remote_macos_mcp")
    samples = _env_int("S4L_LEAK_ALERT_SAMPLES", 5)      # consecutive climbs required
    floor = _env_int("S4L_LEAK_ALERT_FLOOR", 20)          # ignore below this count
    climb_min = _env_int("S4L_LEAK_ALERT_CLIMB_MIN", 12)  # min first->last growth
    cooldown_s = _env_int("S4L_LEAK_ALERT_COOLDOWN", 1800)
    if samples < 3:
        samples = 3

    tail = _tail_lines(output, samples)
    series: list[dict[str, Any]] = []
    for line in tail:
        try:
            series.append(json.loads(line))
        except Exception:
            continue
    if len(series) < samples:
        return

    def counts(name: str) -> list[int]:
        vals = []
        for snap in series[-samples:]:
            g = (snap.get("groups") or {}).get(name) or {}
            c = g.get("count")
            vals.append(int(c) if isinstance(c, (int, float)) else 0)
        return vals

    leaking: list[str] = []
    for name in groups_to_watch:
        vals = counts(name)
        if len(vals) < samples:
            continue
        monotonic = all(vals[i] <= vals[i + 1] for i in range(len(vals) - 1))
        grew = (vals[-1] - vals[0]) >= climb_min
        if monotonic and grew and vals[-1] >= floor:
            leaking.append(f"{name} {vals[0]}->{vals[-1]} over {samples} samples")

    if not leaking:
        return

    # Cooldown: one page per window even if the leak persists for hours.
    state = Path(os.environ.get("S4L_STATE_DIR", str(Path.home() / ".social-autoposter-mcp"))) / "claude-queue"
    cooldown = state / "leak-alert.cooldown"
    now = time.time()
    try:
        if cooldown.exists() and (now - cooldown.stat().st_mtime) < cooldown_s:
            return
    except OSError:
        pass

    reason = "; ".join(leaking)
    # Always emit the stderr marker (parsed into the dashboard even without Sentry).
    print(f"LEAK_ALERT {reason}", file=sys.stderr)
    try:
        import sentry_init

        sentry_init.init()
        sentry_init.capture_message(
            f"process-group leak climbing: {reason}",
            level="warning",
            tags={
                "component": "leak_detector",
                "hostname": socket.gethostname(),
                "claude_desktop_version": claude_desktop_version() or "unknown",
                "app_version": _app_version() or "unknown",
            },
        )
        sentry_init.flush(3.0)
    except Exception:
        pass
    try:
        state.mkdir(parents=True, exist_ok=True)
        cooldown.write_text(str(now))
    except Exception:
        pass


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=os.environ.get("S4L_MEMORY_SNAPSHOT_LOG", str(DEFAULT_OUTPUT)))
    parser.add_argument("--top", type=int, default=int(os.environ.get("S4L_MEMORY_TOP_N", "30")))
    parser.add_argument("--max-bytes", type=int, default=int(os.environ.get("S4L_MEMORY_MAX_BYTES", str(100 * 1024 * 1024))))
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print a slim JSON summary to stdout and exit (no JSONL write). Used by the heartbeat.",
    )
    args = parser.parse_args()

    if args.summary:
        sys.stdout.write(json.dumps(build_summary(), separators=(",", ":")))
        return 0

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    rotate_log(output, args.max_bytes)

    snapshot = build_snapshot(max(1, args.top))
    with output.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(snapshot, sort_keys=True, separators=(",", ":")) + "\n")

    # Proactive leak page: reads the tail of the JSONL we just appended to, so no
    # extra state. Best-effort; never blocks the snapshot write.
    _maybe_leak_alert(output, snapshot)

    groups = snapshot.get("groups", {})
    queues = snapshot.get("queues", {})
    claude_queue = queues.get("claude_queue", {}) if isinstance(queues, dict) else {}
    print(
        "memory_snapshot "
        f"ts={snapshot['ts']} "
        f"social_repo_processes_mb={groups.get('social_autoposter_repo_processes', {}).get('rss_mb', 0)} "
        f"saps_mcp_servers={groups.get('social_autoposter_mcp_servers', {}).get('count', 0)} "
        f"saps_mcp_servers_mb={groups.get('social_autoposter_mcp_servers', {}).get('rss_mb', 0)} "
        f"saps_configured_sessions={groups.get('sessions_configured_social_autoposter_mcp', {}).get('count', 0)} "
        f"remote_macos_mcp_servers_mb={groups.get('remote_macos_mcp_servers', {}).get('rss_mb', 0)} "
        f"claude_queue_pending={claude_queue.get('pending_total', 0)} "
        f"claude_queue_running={claude_queue.get('running_total', 0)} "
        f"output={output}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
