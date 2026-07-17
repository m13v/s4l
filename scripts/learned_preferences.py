#!/usr/bin/env python3
"""learned_preferences: the agent-owned config block distilled from human
card decisions (review_events).

**Single install-wide block (2026-07-08).** Earlier this lived once PER
PROJECT. In practice every entry ever learned was a fact about the human
reviewer (their voice, their quality bar, their reviewing habits), never
about one product's audience specifically, so a multi-project install (every
install gets at least a persona project plus, usually, a product project)
just relearned the same reviewer-level facts independently in each project.
Consolidated into ONE top-level `learned_preferences_global` block so a
correction made anywhere reaches every project's drafting/judging prompt.
migrate_to_global() performs the one-time move from the old per-project
shape and runs automatically (idempotent) from feedback_digest.py.

The feedback loop:
  1. The menubar review card ships every approve/reject (with reason chips,
     free-text note, link-click interactions, dwell) to /api/v1/review-events.
  2. scripts/feedback_digest.py (scheduled) claims unprocessed events per
     project and asks Claude for a conservative mutation plan.
  3. apply_mutations() here writes that plan into config.json's single
     `learned_preferences_global` block, whitelist-enforced, with flock +
     backup + atomic replace.
  4. Enforcement is SOFT (prompt-level, never a deterministic filter): the
     twitter prep prompt embeds the block exactly ONCE via
     GLOBAL_LEARNED_PREFS_JSON (the 'Global learned preferences' line under
     PROJECT ROUTING; per-project stamping retired 2026-07-14), so the block
     (with its self-describing _instruction) reaches the judging/drafting
     model automatically. `history` is stripped at prompt-build time: it is
     a config.json-only audit changelog with no reader in any prompt.
     prompt_block() renders the same content for callers that want an
     explicit section instead of the raw embed.

Block shape (config.json top-level key `learned_preferences_global`):

  "learned_preferences_global": {
    "_instruction": "<how the drafting model should apply this block>",
    "enabled": true,
    "audience_avoid":    ["crypto/web3-native authors ..."],
    "audience_prefer":   [],
    "thread_avoid":      ["engagement-bait question threads ..."],
    "draft_style_notes": [],
    "edit_examples":     [{"original", "final", "ts"}],
    "updated_at": "...",
    "history": [{"ts", "change", "rationale", "source_events": [ids]}]
  }

WHITELIST: this module writes ONLY learned_preferences_global plus
(add + remove, still per-project) voice.never and content_guardrails.do_not.
Nothing else in a project entry is touchable through this path; unknown keys
in a mutation plan are dropped and counted, never applied. Facts
(content_angle, links, identity fields) are deliberately unreachable so a
bad digest can never poison grounding.
"""
from __future__ import annotations

import datetime
import fcntl
import json
import os
import re
import shutil
import sys
from pathlib import Path

# Lists the digest may fully manage inside learned_preferences.
MANAGED_LISTS = ("audience_avoid", "audience_prefer", "thread_avoid", "draft_style_notes")

MAX_HISTORY = 1000

# Edit examples: (original, final) pairs from the user's own card rewrites,
# recorded DETERMINISTICALLY by feedback_digest via record_edit_examples()
# (never by the digest LLM's mutation plan). As of 2026-07-17 these are read
# ONLY by the feedback digest, which distills recurring corrections into
# draft_style_notes (optionally embedding a short before/after fragment as an
# anchor); the drafting/judging prompts NO LONGER embed them. Kept newest-first
# and capped at MAX_EDIT_EXAMPLES so the digest sees a deep rewrite history.
# No per-entry length cap: a rewrite is the user's exact words, stored verbatim.
MAX_EDIT_EXAMPLES = 100

# Travels inside the global block the prep prompt embeds (once, via
# GLOBAL_LEARNED_PREFS_JSON), so the drafting model reads its own operating
# manual for the block. Split semantics (2026-07-03, after the persona lane kept
# recycling a rejected draft structure): candidate JUDGING signals stay soft
# (judgment, not a hard ban), but draft_style_notes is MANDATORY when
# writing; if it doesn't beat the engagement style's structural template,
# user corrections never stick.
DEFAULT_INSTRUCTION = (
    "Human review feedback for this project, distilled from the user's "
    "approve/reject decisions on draft cards. When judging candidates: "
    "treat audience_avoid and thread_avoid as strong negative signals "
    "(prefer rejecting matching candidates, citing 'learned_preference' in "
    "the reason) and audience_prefer as a positive signal; those are "
    "preferences, not hard bans; use judgment when a candidate is "
    "exceptionally on-topic despite a match. When WRITING a draft, "
    "draft_style_notes is MANDATORY, not advisory: follow every entry, and "
    "on conflict it overrides the engagement style's structural template. "
    "Some draft_style_notes carry a short before/after example of a real "
    "user rewrite; match the 'after' phrasing and never reproduce the "
    "'before'."
)


def config_path() -> str:
    """Delegates to scripts/config.py, THE config resolver (state-dir first,
    symlinks resolved to their real target). This module's os.replace()
    writers previously flattened the operator Mac's config symlink into a
    plain file whenever S4L_CONFIG_PATH was missing from the environment
    (2026-07-11 and 2026-07-13 incidents); the shared resolver's realpath
    makes that impossible regardless of env."""
    try:
        from config import config_path as _shared

        return _shared()
    except Exception:
        # Direct-invocation fallback (scripts/ not importable): same order.
        explicit = os.environ.get("S4L_CONFIG_PATH")
        if explicit:
            p = os.path.expanduser(explicit)
        else:
            state_dir = os.environ.get("S4L_STATE_DIR") or "~/.social-autoposter-mcp"
            sp = os.path.join(os.path.expanduser(state_dir), "config.json")
            if os.path.exists(sp):
                p = sp
            else:
                repo = os.environ.get("S4L_REPO_DIR")
                p = (
                    os.path.join(repo, "config.json")
                    if repo
                    else os.path.expanduser("~/social-autoposter/config.json")
                )
        return os.path.realpath(p) if os.path.exists(p) else p


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def normalized(block) -> dict:
    """Coerce whatever is in config into the canonical block shape."""
    b = block if isinstance(block, dict) else {}
    out = {
        # Always stamp the code-owned instruction so semantics updates reach
        # every config on the digest's next write (the old text otherwise
        # persists forever; nothing legitimately customizes this per project).
        "_instruction": DEFAULT_INSTRUCTION,
        "enabled": b.get("enabled", True) is not False,
    }
    for key in MANAGED_LISTS:
        vals = b.get(key)
        out[key] = [str(v).strip() for v in vals if str(v).strip()] if isinstance(vals, list) else []
    # Few-shot before/after pairs from the user's card edits (newest first).
    # Preserved through normalization so apply_mutations round-trips never
    # drop them; only record_edit_examples() writes this list.
    ex = b.get("edit_examples")
    out["edit_examples"] = [
        {
            "original": str(e.get("original") or ""),
            "final": str(e.get("final") or ""),
            "ts": e.get("ts"),
        }
        for e in (ex if isinstance(ex, list) else [])
        if isinstance(e, dict) and str(e.get("original") or "").strip() and str(e.get("final") or "").strip()
    ][:MAX_EDIT_EXAMPLES]
    out["updated_at"] = b.get("updated_at")
    hist = b.get("history")
    out["history"] = list(hist)[-MAX_HISTORY:] if isinstance(hist, list) else []
    return out


def get_global_block(cfg: dict | None = None, cfg_path: str | None = None) -> dict:
    """Load + normalize the single install-wide learned_preferences block.

    If `cfg` (an already-parsed config.json dict) isn't supplied, reads
    config.json fresh from disk — the same "re-read on every call" pattern
    already used elsewhere (e.g. link_tail.py's project loader), so callers
    that only ever had a single project's dict slice (not the whole config)
    can still reach the global block with no signature change on their end.
    """
    if cfg is None:
        try:
            cfg = json.loads(Path(cfg_path or config_path()).read_text())
        except Exception:
            cfg = {}
    return normalized(cfg.get("learned_preferences_global"))


def get_block(project_cfg=None) -> dict:
    """Back-compat shim (2026-07-08): learned_preferences is now a SINGLE
    install-wide block, not one per project — see module docstring.
    `project_cfg` is accepted but ignored; kept so every existing caller
    (post_reddit.py, engage_reddit.py, post_github.py, engage_github.py,
    link_tail.py — 4 of those 5 files are chflags-uchg locked) needs zero
    changes. Callers that already have the full parsed config in scope
    should call get_global_block(cfg) directly instead (apply_mutations and
    record_edit_examples below do)."""
    return get_global_block()


def prompt_block(project_cfg=None) -> str:
    """Explicit prompt section for callers that don't embed the raw project
    JSON (mirrors the engagement_styles STYLES_BLOCK pattern). Empty string
    when the block is disabled or has no entries. `project_cfg` is accepted
    but ignored (see get_block())."""
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
    # edit_examples are intentionally NOT rendered here (2026-07-17): they feed
    # only the feedback digest now, which distills them into draft_style_notes.
    if not lines:
        return ""
    return (
        "## LEARNED PREFERENCES (from this user's own approve/reject decisions)\n"
        + b["_instruction"]
        + "\n"
        + "\n".join(lines)
        + "\n"
    )


def _validate_add_list(raw, cap=None):
    out = []
    if not isinstance(raw, list):
        return out
    for v in raw:
        s = str(v).strip()
        if s:
            out.append(s)
        if cap is not None and len(out) >= cap:
            break
    return out


def record_edit_examples(project_name: str, pairs, cfg_path: str | None = None) -> dict:
    """Deterministically record (original, final) card-edit pairs as few-shot
    examples in the single learned_preferences_global.edit_examples (newest
    first, capped at MAX_EDIT_EXAMPLES). This is CODE-owned, not part of the
    digest LLM's mutation plan: an edit the user made IS the ground truth, no
    judgment call needed to keep it (mirrors the s4l-email inbox repo, which
    injects the last 5 human-edited drafts into every draft prompt). Same
    flock + backup + atomic-replace mechanics as apply_mutations().

    `project_name` is validated against config.json (catches a caller typo)
    but no longer selects a per-project block to write into — every project's
    edits feed the same global few-shot pool (2026-07-08).

    pairs: iterable of {"original": str, "final": str} (ts optional).
    Returns {ok, recorded: int}.
    """
    cfg_path = cfg_path or config_path()
    clean = []
    for p in pairs or []:
        if not isinstance(p, dict):
            continue
        orig = str(p.get("original") or "").strip()
        final = str(p.get("final") or "").strip()
        if not orig or not final or orig == final:
            continue
        clean.append({
            "original": orig,
            "final": final,
            "ts": str(p.get("ts") or _now_iso()),
        })
    if not clean:
        return {"ok": True, "recorded": 0}

    lock_path = cfg_path + ".lock"
    Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            cfg = json.loads(Path(cfg_path).read_text())
        except Exception as e:
            return {"ok": False, "error": f"config unreadable: {e}", "recorded": 0}
        projects = cfg.get("projects") or []
        proj = next((p for p in projects if p.get("name") == project_name), None)
        if proj is None:
            return {"ok": False, "error": "project not in config", "recorded": 0}

        block = get_global_block(cfg)
        existing = block["edit_examples"]
        # Dedup on the final text (a retried digest re-records the same batch).
        seen = {e["final"] for e in existing}
        fresh = [e for e in clean if e["final"] not in seen]
        if not fresh:
            return {"ok": True, "recorded": 0}
        block["edit_examples"] = (fresh + existing)[:MAX_EDIT_EXAMPLES]
        block["updated_at"] = _now_iso()
        block["history"] = (block["history"] + [
            {
                "ts": _now_iso(),
                "change": f"edit_examples recorded: {len(fresh)}",
                "rationale": "user rewrote the draft on the review card",
                "source_events": [],
                "project": project_name,
            }
        ])[-MAX_HISTORY:]
        cfg["learned_preferences_global"] = block

        stamp = _now_iso().replace(":", "-").replace(".", "-")
        try:
            if os.path.exists(cfg_path):
                shutil.copyfile(cfg_path, f"{cfg_path}.bak-{stamp}")
        except Exception:
            pass
        tmp = cfg_path + ".tmp"
        Path(tmp).write_text(json.dumps(cfg, indent=2) + "\n")
        os.replace(tmp, cfg_path)

    return {"ok": True, "recorded": len(fresh)}


def apply_mutations(project_name: str, plan: dict, source_event_ids=None, cfg_path: str | None = None) -> dict:
    """Apply a digest mutation plan to the single learned_preferences_global
    block. Returns a summary dict:
    {ok, applied: [change strings], dropped: [reasons], project_found: bool}.

    plan shape (all optional):
      {"changes": {<managed list>: {"add": [...], "remove": [...]}},
       "voice_never_add": [...], "guardrails_do_not_add": [...],
       "rationale": "..."}

    `project_name` is validated against config.json (catches a caller typo)
    and recorded in the history entry's rationale, but no longer selects a
    per-project block: every project's digest writes into the same global
    block now (2026-07-08). `voice_never_add`/`guardrails_do_not_add` stay
    per-project (proj['voice']/proj['content_guardrails']) since those are a
    separate, narrower whitelist unrelated to this consolidation.

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

        block = get_global_block(cfg)

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
                # Never let the digest (re)learn a link/punctuation suppression
                # note: it contradicts the tail-link bridge feature. Same guard
                # migrate_to_global uses, now enforced on every digest write so
                # reading the edit_examples pool can't resurrect URL-stripping.
                if key == "draft_style_notes":
                    reason = _excluded_note_reason(v)
                    if reason:
                        dropped.append(f"{key} rejected ({reason}): {v}")
                        continue
                block[key].append(v)
                applied.append(f"{key} added: {v}")

        # Append-only extensions of existing curated fields.
        for v in _validate_add_list(plan.get("voice_never_add")):
            voice = proj.setdefault("voice", {})
            never = voice.setdefault("never", [])
            if isinstance(never, list) and v not in never:
                never.append(v)
                applied.append(f"voice.never added: {v}")
        for v in _validate_add_list(plan.get("voice_never_remove")):
            never = (proj.get("voice") or {}).get("never")
            if isinstance(never, list) and v in never:
                never.remove(v)
                applied.append(f"voice.never removed: {v}")
        for v in _validate_add_list(plan.get("guardrails_do_not_add")):
            guard = proj.setdefault("content_guardrails", {})
            do_not = guard.setdefault("do_not", [])
            if isinstance(do_not, list) and v not in do_not:
                do_not.append(v)
                applied.append(f"content_guardrails.do_not added: {v}")
        for v in _validate_add_list(plan.get("guardrails_do_not_remove")):
            do_not = (proj.get("content_guardrails") or {}).get("do_not")
            if isinstance(do_not, list) and v in do_not:
                do_not.remove(v)
                applied.append(f"content_guardrails.do_not removed: {v}")

        for key in set(plan.keys()) - {"changes", "voice_never_add", "voice_never_remove", "guardrails_do_not_add", "guardrails_do_not_remove", "rationale", "project"}:
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
                "project": project_name,
            }
        ])[-MAX_HISTORY:]
        cfg["learned_preferences_global"] = block

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


# --- Per-project -> global migration (2026-07-08) -------------------------
# Two categories of existing per-project draft_style_notes are EXCLUDED
# (dropped, not carried forward) rather than merged into the new global
# block, per explicit user instruction: notes that tell the drafter to
# suppress/strip the trailing product link, and notes about avoiding a
# trailing period/punctuation. The link-suppression notes directly
# contradict the tail-link bridge feature (scripts/link_tail.py), whose
# entire job is appending that link — when fed into the bridge prompt as a
# MANDATORY learned preference they produced degraded output (e.g. a
# duplicated discourse-marker artifact traced 2026-07-08). Everything else
# (audience_avoid/prefer, thread_avoid, other draft_style_notes,
# edit_examples, and each project's own history[]) is carried forward
# verbatim — nothing else is dropped, and every excluded/dropped entry is
# still recorded in the merged history for audit purposes.
_LINK_SUPPRESSION_RE = re.compile(
    r"do not (append|add)\b.{0,60}\b(link|url)\b"
    r"|\b(link|url)\b.{0,60}\bdo not (append|add)\b"
    r"|leave\b.{0,40}\bout\b.{0,40}\b(link|url|product url)\b"
    r"|\b(link|url)\b.{0,60}\b(strip|remove)\b"
    r"|\bstrip(s|ped)?\b.{0,60}\b(link|url)\b"
    r"|let the copy stand alone"
    r"|end (right after|on) the (sentence|product name)",
    re.IGNORECASE,
)
_PUNCTUATION_SUPPRESSION_RE = re.compile(
    r"\b(trailing|no|without a?|avoid|remove|do not (end|use))\b.{0,40}\b(period|dot|full stop|punctuation)\b",
    re.IGNORECASE,
)


def _excluded_note_reason(text: str) -> str | None:
    """Reason string if this draft_style_notes entry should be dropped
    during migration (not carried into the global block), else None."""
    if _LINK_SUPPRESSION_RE.search(text):
        return "contradicts the tail-link bridge feature (link/url suppression note)"
    if _PUNCTUATION_SUPPRESSION_RE.search(text):
        return "trailing punctuation suppression note"
    return None


def migrate_to_global(cfg_path: str | None = None) -> dict:
    """One-time, idempotent consolidation of every project's
    learned_preferences into the single learned_preferences_global block,
    then deletes the per-project key. Safe to call on every process start
    (feedback_digest.py does): a config where no project carries a
    `learned_preferences` key is already migrated and this is a cheap no-op.

    Returns {ok, migrated: bool, projects_merged: [...], excluded: [...],
    dropped_at_cap: [...]}.
    """
    cfg_path = cfg_path or config_path()
    lock_path = cfg_path + ".lock"
    Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            cfg = json.loads(Path(cfg_path).read_text())
        except Exception as e:
            return {"ok": False, "error": f"config unreadable: {e}"}

        projects = cfg.get("projects") or []
        if not any(isinstance(p.get("learned_preferences"), dict) for p in projects):
            return {"ok": True, "migrated": False, "projects_merged": [], "excluded": [], "dropped_at_cap": []}

        global_block = normalized(cfg.get("learned_preferences_global"))
        merged_history = list(global_block["history"])
        excluded = []
        dropped_at_cap = []
        projects_merged = []

        for proj in projects:
            raw = proj.pop("learned_preferences", None)
            if not isinstance(raw, dict):
                continue
            pname = proj.get("name", "?")
            block = normalized(raw)
            touched = False
            for key in MANAGED_LISTS:
                for v in block[key]:
                    if key == "draft_style_notes":
                        reason = _excluded_note_reason(v)
                        if reason:
                            excluded.append({"project": pname, "key": key, "value": v, "reason": reason})
                            continue
                    if v in global_block[key]:
                        continue
                    global_block[key].append(v)
                    touched = True
            existing_finals = {e["final"] for e in global_block["edit_examples"]}
            for e in block["edit_examples"]:
                if e["final"] not in existing_finals:
                    global_block["edit_examples"].append(e)
                    existing_finals.add(e["final"])
                    touched = True
            for h in block["history"]:
                merged_history.append({**h, "project": pname})
                touched = True
            if touched:
                projects_merged.append(pname)

        # Keep only the newest MAX_EDIT_EXAMPLES, most-recent ts first.
        global_block["edit_examples"].sort(key=lambda e: e.get("ts") or "", reverse=True)
        global_block["edit_examples"] = global_block["edit_examples"][:MAX_EDIT_EXAMPLES]

        summary = f"migrated {len(projects_merged)} project block(s) into learned_preferences_global"
        if excluded:
            summary += f"; excluded {len(excluded)} note(s) (link/punctuation suppression)"
        merged_history.append({
            "ts": _now_iso(),
            "change": summary,
            "rationale": "learned_preferences is now a single install-wide block, not per-project "
                         "(2026-07-08); every reviewer-level fact ever learned applied to the "
                         "reviewer, not to one product's audience, so per-project storage just "
                         "caused the same corrections to be relearned independently in every project.",
            "source_events": [],
            "excluded": excluded,
            "dropped_at_cap": dropped_at_cap,
        })
        global_block["history"] = merged_history[-MAX_HISTORY:]
        global_block["updated_at"] = _now_iso()
        cfg["learned_preferences_global"] = global_block

        stamp = _now_iso().replace(":", "-").replace(".", "-")
        try:
            if os.path.exists(cfg_path):
                shutil.copyfile(cfg_path, f"{cfg_path}.bak-{stamp}")
        except Exception:
            pass
        tmp = cfg_path + ".tmp"
        Path(tmp).write_text(json.dumps(cfg, indent=2) + "\n")
        os.replace(tmp, cfg_path)

    return {
        "ok": True, "migrated": True, "projects_merged": projects_merged,
        "excluded": excluded, "dropped_at_cap": dropped_at_cap,
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Inspect/render learned_preferences")
    ap.add_argument("command", choices=["show", "block", "migrate"],
                     help="show = raw JSON, block = prompt block, migrate = run migrate_to_global()")
    ap.add_argument("project", nargs="?", help="unused for 'migrate'; kept for show/block back-compat")
    args = ap.parse_args()

    if args.command == "migrate":
        print(json.dumps(migrate_to_global(), indent=2))
        sys.exit(0)

    if not args.project:
        print("show/block require a project name (used only to validate against config.json; "
              "the rendered block is now global)", file=sys.stderr)
        sys.exit(1)
    cfg = json.loads(Path(config_path()).read_text())
    proj = next((p for p in (cfg.get("projects") or []) if p.get("name") == args.project), None)
    if proj is None:
        print(f"project {args.project!r} not found", file=sys.stderr)
        sys.exit(1)
    if args.command == "show":
        print(json.dumps(get_global_block(cfg), indent=2))
    else:
        print(prompt_block(proj))
