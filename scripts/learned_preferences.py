#!/usr/bin/env python3
"""Per-project learned_preferences: the agent-owned config block distilled from
human card decisions (review_events).

The feedback loop:
  1. The menubar review card ships every approve/reject (with reason chips,
     free-text note, link-click interactions, dwell) to /api/v1/review-events.
  2. scripts/feedback_digest.py (scheduled) claims unprocessed events per
     project and asks Claude for a conservative mutation plan.
  3. apply_mutations() here writes that plan into config.json under the
     project's `learned_preferences` block, whitelist-enforced, with flock +
     backup + atomic replace.
  4. Enforcement is SOFT (prompt-level, never a deterministic filter): the
     twitter prep prompt embeds every project entry verbatim via
     ALL_PROJECTS_JSON, so the block (with its self-describing _instruction)
     reaches the judging/drafting model automatically. prompt_block() renders
     the same content for prompts that want an explicit section.

Block shape (inside a config.json project entry):

  "learned_preferences": {
    "_instruction": "<how the drafting model should apply this block>",
    "enabled": true,
    "audience_avoid":    ["crypto/web3-native authors ..."],
    "audience_prefer":   [],
    "thread_avoid":      ["engagement-bait question threads ..."],
    "draft_style_notes": [],
    "updated_at": "...",
    "history": [{"ts", "change", "rationale", "source_events": [ids]}]
  }

WHITELIST: this module writes ONLY learned_preferences plus (append-only)
voice.never and content_guardrails.do_not. Nothing else in a project entry is
touchable through this path; unknown keys in a mutation plan are dropped and
counted, never applied. Facts (content_angle, links, identity fields) are
deliberately unreachable so a bad digest can never poison grounding.
"""
from __future__ import annotations

import datetime
import fcntl
import json
import os
import shutil
import sys
from pathlib import Path

# Lists the digest may fully manage inside learned_preferences.
MANAGED_LISTS = ("audience_avoid", "audience_prefer", "thread_avoid", "draft_style_notes")
# Existing config fields the digest may APPEND to (never remove from).
APPEND_ONLY_FIELDS = ("voice_never_add", "guardrails_do_not_add")

MAX_ENTRIES_PER_LIST = 10
MAX_ENTRY_CHARS = 200
MAX_HISTORY = 50

# Travels inside the JSON the prep prompt embeds (ALL_PROJECTS_JSON is the
# full project entry), so the drafting model reads its own operating manual
# for the block. Soft steering by design: judgment, not a hard ban.
DEFAULT_INSTRUCTION = (
    "Human review feedback for this project, distilled from the user's "
    "approve/reject decisions on draft cards. When judging candidates and "
    "drafting replies: treat audience_avoid and thread_avoid as strong "
    "negative signals (prefer rejecting matching candidates, citing "
    "'learned_preference' in the reason), treat audience_prefer as a "
    "positive signal, and follow draft_style_notes when writing. These are "
    "preferences, not hard bans; use judgment when a candidate is "
    "exceptionally on-topic despite a match."
)


def config_path() -> str:
    explicit = os.environ.get("SAPS_CONFIG_PATH")
    if explicit:
        return explicit
    repo = os.environ.get("SAPS_REPO_DIR")
    if repo:
        return os.path.join(repo, "config.json")
    return os.path.expanduser("~/social-autoposter/config.json")


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def normalized(block) -> dict:
    """Coerce whatever is in config into the canonical block shape."""
    b = block if isinstance(block, dict) else {}
    out = {
        "_instruction": str(b.get("_instruction") or DEFAULT_INSTRUCTION),
        "enabled": b.get("enabled", True) is not False,
    }
    for key in MANAGED_LISTS:
        vals = b.get(key)
        out[key] = [str(v).strip()[:MAX_ENTRY_CHARS] for v in vals if str(v).strip()] if isinstance(vals, list) else []
    out["updated_at"] = b.get("updated_at")
    hist = b.get("history")
    out["history"] = list(hist)[-MAX_HISTORY:] if isinstance(hist, list) else []
    return out


def get_block(project_cfg) -> dict:
    return normalized((project_cfg or {}).get("learned_preferences"))


def prompt_block(project_cfg) -> str:
    """Explicit prompt section for callers that don't embed the raw project
    JSON (mirrors the engagement_styles STYLES_BLOCK pattern). Empty string
    when the block is disabled or has no entries."""
    b = get_block(project_cfg)
    if not b["enabled"]:
        return ""
    lines = []
    labels = {
        "audience_avoid": "Avoid audiences/authors like",
        "audience_prefer": "Prefer audiences/authors like",
        "thread_avoid": "Avoid threads like",
        "draft_style_notes": "Drafting notes",
    }
    for key in MANAGED_LISTS:
        for v in b[key]:
            lines.append(f"- {labels[key]}: {v}")
    if not lines:
        return ""
    return (
        "## LEARNED PREFERENCES (from this user's own approve/reject decisions)\n"
        + b["_instruction"]
        + "\n"
        + "\n".join(lines)
        + "\n"
    )


def _validate_add_list(raw, cap=MAX_ENTRIES_PER_LIST):
    out = []
    if not isinstance(raw, list):
        return out
    for v in raw:
        s = str(v).strip()
        if s:
            out.append(s[:MAX_ENTRY_CHARS])
        if len(out) >= cap:
            break
    return out


def apply_mutations(project_name: str, plan: dict, source_event_ids=None, cfg_path: str | None = None) -> dict:
    """Apply a digest mutation plan to config.json. Returns a summary dict:
    {ok, applied: [change strings], dropped: [reasons], project_found: bool}.

    plan shape (all optional):
      {"changes": {<managed list>: {"add": [...], "remove": [...]}},
       "voice_never_add": [...], "guardrails_do_not_add": [...],
       "rationale": "..."}

    flock on <config>.lock serializes against other writers (setup tools, a
    concurrent digest); backup + atomic replace mirrors mcp/src/setup.ts
    applySetup(). Unknown keys are DROPPED, never applied: the whitelist is
    enforced here in code, not in the digest prompt.
    """
    cfg_path = cfg_path or config_path()
    applied, dropped = [], []
    plan = plan if isinstance(plan, dict) else {}
    changes = plan.get("changes") if isinstance(plan.get("changes"), dict) else {}
    rationale = str(plan.get("rationale") or "").strip()[:500]

    lock_path = cfg_path + ".lock"
    Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            cfg = json.loads(Path(cfg_path).read_text())
        except Exception as e:
            return {"ok": False, "error": f"config unreadable: {e}", "applied": [], "dropped": [], "project_found": False}
        projects = cfg.get("projects") or []
        proj = next((p for p in projects if p.get("name") == project_name), None)
        if proj is None:
            return {"ok": False, "error": "project not in config", "applied": [], "dropped": [], "project_found": False}

        block = get_block(proj)

        for key, ops in changes.items():
            if key not in MANAGED_LISTS:
                dropped.append(f"unknown list '{key}'")
                continue
            if not isinstance(ops, dict):
                dropped.append(f"bad ops for '{key}'")
                continue
            for v in _validate_add_list(ops.get("remove")):
                # Fuzzy-tolerant remove: exact match only; a miss is not an error.
                if v in block[key]:
                    block[key].remove(v)
                    applied.append(f"{key} removed: {v}")
            for v in _validate_add_list(ops.get("add")):
                if v in block[key]:
                    continue
                if len(block[key]) >= MAX_ENTRIES_PER_LIST:
                    dropped.append(f"{key} at cap ({MAX_ENTRIES_PER_LIST}), skipped: {v}")
                    continue
                block[key].append(v)
                applied.append(f"{key} added: {v}")

        # Append-only extensions of existing curated fields.
        for v in _validate_add_list(plan.get("voice_never_add"), cap=3):
            voice = proj.setdefault("voice", {})
            never = voice.setdefault("never", [])
            if isinstance(never, list) and v not in never:
                never.append(v)
                applied.append(f"voice.never added: {v}")
        for v in _validate_add_list(plan.get("guardrails_do_not_add"), cap=3):
            guard = proj.setdefault("content_guardrails", {})
            do_not = guard.setdefault("do_not", [])
            if isinstance(do_not, list) and v not in do_not:
                do_not.append(v)
                applied.append(f"content_guardrails.do_not added: {v}")

        for key in set(plan.keys()) - {"changes", "voice_never_add", "guardrails_do_not_add", "rationale", "project"}:
            dropped.append(f"unknown top-level key '{key}'")

        if not applied:
            return {"ok": True, "applied": [], "dropped": dropped, "project_found": True}

        block["updated_at"] = _now_iso()
        block["history"] = (block["history"] + [
            {
                "ts": _now_iso(),
                "change": "; ".join(applied)[:1000],
                "rationale": rationale,
                "source_events": list(source_event_ids or [])[:100],
            }
        ])[-MAX_HISTORY:]
        proj["learned_preferences"] = block

        # Backup + atomic replace (same shape as setup.ts applySetup).
        stamp = _now_iso().replace(":", "-").replace(".", "-")
        try:
            if os.path.exists(cfg_path):
                shutil.copyfile(cfg_path, f"{cfg_path}.bak-{stamp}")
        except Exception:
            pass
        tmp = cfg_path + ".tmp"
        Path(tmp).write_text(json.dumps(cfg, indent=2) + "\n")
        os.replace(tmp, cfg_path)

    return {"ok": True, "applied": applied, "dropped": dropped, "project_found": True}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Inspect/render learned_preferences")
    ap.add_argument("command", choices=["show", "block"], help="show = raw JSON, block = prompt block")
    ap.add_argument("project")
    args = ap.parse_args()
    cfg = json.loads(Path(config_path()).read_text())
    proj = next((p for p in (cfg.get("projects") or []) if p.get("name") == args.project), None)
    if proj is None:
        print(f"project {args.project!r} not found", file=sys.stderr)
        sys.exit(1)
    if args.command == "show":
        print(json.dumps(get_block(proj), indent=2))
    else:
        print(prompt_block(proj))
