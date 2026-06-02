#!/usr/bin/env python3
"""Hand a picked-page brief to a Claude session running inside the target
repo and let it experiment with the page content.

Intentionally open-minded. The prompt does not restrict the model to SEO
pages, to specific component libraries, or to a fixed list of editable
files. Claude is told what the page is, how it is performing, where to
find it in the repo, and what the product positioning looks like, and is
free to WebSearch for fresh landing-page patterns, trending copy angles,
or conversion ideas, then Read/Edit whatever it decides improves the page.

Lifecycle for each invocation:

  1. Record a 'running' row in seo_page_improvements with the brief snapshot.
  2. Launch `claude -p ...` with cwd set to the product repo, capturing
     stream-json events for auditability and cost tracking.
  3. Require a final JSON envelope on stdout summarising the change.
  4. Diff HEAD before/after to confirm a commit actually landed; write
     commit_sha, files_modified, diff_summary, rationale back to the row
     and flip status to 'committed' / 'no_change' / 'failed'.

Usage:
    python3 seo/improve_page.py --brief /tmp/brief_pieline.json
    python3 seo/improve_page.py --brief /tmp/brief_cyrano.json --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent

ENV_PATH = ROOT_DIR / ".env"
if ENV_PATH.exists():
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

sys.path.insert(0, str(ROOT_DIR / "scripts"))
from http_api import api_post, api_patch  # noqa: E402

sys.path.insert(0, str(SCRIPT_DIR))
from claude_wait import wait_for_claude  # noqa: E402
from generate_page import render_content_guardrails  # noqa: E402


CLAUDE_TIMEOUT_SECONDS = 1800  # 30 minutes; research + multiple edits + commit


PROMPT_TEMPLATE = """You are improving a live web page that real visitors hit right now. You have full read/write access to the website repo (cwd = repo root). Your job is to improve the page on its own terms, not to apply a generic SEO recipe.

# Context

{brief_block}

# Step 1: Understand what this page is before changing anything

Read the live page file(s) first. Most products put the home page at `src/app/page.tsx` or `src/app/(main)/page.tsx`; other paths map to `src/app/<path>/page.tsx` or similar. Use Glob/Grep liberally. Also skim sibling pages so you understand the site's existing design language.

Then classify the page's purpose. Common types (illustrative, not exhaustive — call it whatever genuinely fits):

- homepage / brand entry point — establish what this product is, route visitors to signup/booking
- SEO landing (e.g. /<keyword>/, /<city>/, /alternatives/<x>) — rank for a specific query and convert that searcher
- pricing — answer cost objections, drive plan selection
- feature / use-case / solution — explain a capability deeply, push to trial or booking
- comparison / alternatives / vs page — clarify positioning against a named competitor
- blog post / article — rank for a topic, capture email or move readers to product
- docs / changelog / help — serve existing users, reduce support load
- something else entirely — name it in your own words

State in one sentence what THIS specific page's job is. Everything below is judged against that sentence.

# Step 2: Pick success metrics that match the page's job

Two forces are in play, and they are not always aligned:

- **search relevance** rewards depth, breadth, keyword coverage, FAQ blocks, internal links, schema markup, long-form content
- **conversion** rewards focus, a single primary CTA, fewer exits, hero clarity, trust signals above the fold

They overlap on fundamentals (clear positioning, page speed, headlines that match intent, real proof) but diverge on structure: an FAQ that helps an SEO landing rank can shove a homepage's signup CTA below the fold; a blog post that links out to four related articles is good for clustering and bad for direct conversion.

Pick a **primary** metric and a **secondary** metric weighted by the page's job. Reasonable defaults (override when the page tells you otherwise):

- homepage → primary: conversion clarity & CTA click-through. Secondary: brand keywords + clean meta. SEO is hygiene only here, not a content-depth target. Do NOT bolt on FAQ / comparison / related-posts blocks just because the kit has them.
- SEO landing → primary: search relevance for the target query (depth, intent match, schema). Secondary: a soft conversion CTA that does not crowd the hero.
- pricing / feature → primary: conversion. Secondary: light SEO on the relevant keyword.
- blog post / article → primary: topical depth + ranking. Secondary: email capture or a contextual product link.
- docs / changelog → primary: clarity for the existing user. SEO and conversion are both downstream.

If you find an idea that helps one metric but hurts the other, name the tradeoff explicitly in your rationale and pick the side aligned with the page's primary metric.

# Step 3: Do fresh research, then decide on ONE change set

Use WebSearch for:
- pages of this same type (not just "landing pages") that are doing well right now for this category
- headline / hero / structure patterns trending for this buyer
- positioning angles, objections, or proof points worth surfacing
- competitor pages that currently rank for this page's intent (when relevance matters)

Pull at least two distinct external sources. Synthesize, do not copy verbatim.

Then pick ONE substantive change set whose expected impact on the primary metric is highest. Be creative — examples, not a checklist:

- rewrite the hero headline / subhead / primary CTA for sharper positioning
- add or remove a section so the strongest argument lands first
- replace weak proof with stronger proof from project_config
- tighten dense paragraphs into scannable structure (or expand thin sections where depth genuinely serves the page's job)
- add internal links where they serve the reader, not just for SEO
- improve meta title/description

You are NOT restricted to any particular component library. The repo likely uses `@seo/components` / `@m13v/seo-components` plus local components under `src/components/`. Reuse them when they genuinely fit the page's job; build inline TSX/JSX when they don't. Reaching for a kit component just because the rest of the site uses it is the wrong reason.

# Step 4: Edit + typecheck

1. Edit the files. Keep changes focused and high-signal; if you find yourself changing 10 files you are probably refactoring, stop.
2. Run the repo's typecheck / build if one exists under `package.json` scripts and it is cheap. If a quick check fails due to your edit, fix it before moving on.

# Step 5: Visual QA on localhost (MANDATORY — do NOT skip)

Before committing, you MUST verify the changes look correct in a real browser via the **`assrt` MCP** against the local dev server. This catches misalignment, broken layouts, off-screen elements, text overflow, missing images, and console errors that typecheck cannot.

## 5a. Start the dev server

Try in order: `pnpm dev`, `npm run dev`, `yarn dev`. Run it in the background (`nohup ... &` or via the Bash tool's `run_in_background: true`). Wait until it logs that it's listening (usually `localhost:3000`, but read the actual port from the dev server output — some repos use 3001, 4000, 5173, etc.).

## 5b. Run `assrt_test` (use exactly these arguments)

Call `mcp__assrt__assrt_test` against `http://localhost:<port><page_path>` (the exact `Page path` from the Context block above) with:

- **`stopOnFirstFailure: true`** — abort the remaining scenarios as soon as one fails. Stops the run from burning the timeout on Cases 2-5 when Case 1 already proved the page is broken.
- **`timeout: 600`** — 10 minutes. The default is too tight for multi-case plans; long pages plus assrt's scroll/screenshot overhead easily exceeds 300s.
- **`viewport: "desktop"`** — desktop preset (1440x900). Run a second call with `viewport: "mobile"` ONLY if your change touches above-the-fold layout.
- **`autoOpenPlayer: false`** — pipeline mode, don't open the video player.
- **`plan`** — keep to **2-3 focused cases max**. Each case is 3-5 steps. More cases means more wall-clock burn for less marginal coverage. Suggested cases:
  - Case 1: page renders cleanly (no console errors, H1 visible, primary content area populated, no off-screen primary CTA)
  - Case 2: the specific section you ADDED in this run renders and links correctly
  - (Optional) Case 3: layout integrity at the bottom (footer, related-posts strip, CTA — no overflow, no broken images)
- **`passCriteria`** — one paragraph naming what success means for this run, including "no JavaScript console errors" explicitly.

## 5c. Read the result correctly

The MCP response contains: `passed` (bool), `passedCount`, `failedCount`, `aborted` (bool, present only when run was cut short), `abortReason`, `scenarios` (array with per-scenario `passed`/`assertions[]`/`evidence`/`droppedAssertions[]`/`expectedAssertions`), and `logFile` (path to the full execution log on disk). The `droppedAssertions[]` field lists any mandatory verify bullets (lines starting with Verify/Check/Assert/Confirm/Ensure) that the inner agent skipped — if it is non-empty for any scenario, that scenario auto-fails for coverage reasons even if every executed assertion individually passed.

**Read order:**

1. If `passed: true` → visual QA passed. Proceed to commit.
2. If `passed: false`:
   - **`Read` the `logFile` path** (always). Grep for `[FAIL]` and `[PASS]` lines. The execution log has the ground truth for every assertion the inner agent ran, with the actual evidence text. The top-level `scenarios` summary can lose detail if `aborted: true`.
   - Categorize each `[FAIL]`:
     - **(A) caused by your edits in this run** (e.g. a section you added overflows, an import path you changed breaks rendering, a missing slug in CLAUDE_METER_SLUGS produces a 500) → `assrt_diagnose` if needed, fix the source file, retry `assrt_test`. Up to 3 fix/retry attempts.
     - **(B) pre-existing on this page before your edits** (e.g. a console error that fires on every blog post because of a shared component, a layout quirk in the footer that ships on every page) → verify by `git stash`-ing your changes, reloading the page, confirming the same failure reproduces, then `git stash pop`. If pre-existing, you MAY proceed to commit but you MUST: (i) set `visual_qa_status="failed_preexisting"`, (ii) write the specific failure into `visual_qa_findings`, (iii) include the verbatim `[FAIL]` line from the execution log. Do NOT silently mark passed.
   - **`aborted: true`** by itself is NOT a pass or a fail — it just means the run was cut short (by timeout or `stopOnFirstFailure`). The completed scenarios still contain real assertion data; trust the per-scenario `passed` field on those, plus the `logFile` ground truth.

## 5d. Strict integrity rule

`visual_qa_status` is one of `passed | failed | failed_preexisting | skipped_no_dev_server` and MUST match the evidence:

- **`passed`**: only when `assrt_test` returned `passed: true`, OR when (a) `passed: false` was due solely to a wall-clock `Timeout` scenario AND (b) every completed scenario in `scenarios[]` has `passed: true` AND (c) the `logFile` shows no `[FAIL]` lines.
- **`failed`**: any new visual bug your edits caused that you could not fix in 3 attempts. Set `status="failed"` at the envelope level, DO NOT commit.
- **`failed_preexisting`**: a real `[FAIL]` exists in the log AND you verified it reproduces on `HEAD~1` (pre-edit). Commit is allowed; envelope `status="committed"` is fine, but `visual_qa_status` flags the issue.
- **`skipped_no_dev_server`**: only when the dev server genuinely could not be started (missing deps, port conflict you can't resolve, no `dev` script). Commit is allowed if typecheck passed, flag loudly in `rationale`.

**Do not claim `passed` because curl-ing the rendered HTML shows your content rendered.** Curl proves the response body has bytes; it does not prove the browser renders without console errors, paints correctly, or has clean layout. The only source of truth is `mcp__assrt__assrt_test`'s `passed` field cross-checked against the `logFile`.

## 5e. Evidence discipline (no fabricated context)

Every claim you write in `visual_qa_findings` or `notes_for_next_run` MUST be backed by something concrete: a verbatim `[FAIL]` line from the assrt `logFile`, an entry in the response's `scenarios[i].assertions[]`, a `droppedAssertions[]` entry, or output from a tool call you ran in this session (Bash, Read, Grep, browser).

- Do NOT write phrases like "a pre-existing console error fires on this page" unless you actually opened the page in this session and observed the error, OR the assrt response surfaced it. If you only suspect it exists, omit the claim entirely.
- Do NOT classify any bug as "pre-existing" without running the verification: `git stash`, reload, confirm same failure, `git stash pop`. If you did not run the verification, the bug is NOT pre-existing for the purposes of this run — treat it as caused by your edits (Category A).
- If assrt's response includes `droppedAssertions` (mandatory verify bullets the inner agent skipped), that is a coverage failure: do NOT set `visual_qa_status="passed"`. Re-run `assrt_test` with the dropped bullets re-emphasized; if it happens twice, set `visual_qa_status="failed"` and abort the commit.
- Quote evidence verbatim. If you write a `[FAIL]` line into `visual_qa_findings`, copy it character-for-character from the `logFile`. Do not paraphrase.

`visual_qa_findings` is what the dashboard reads. Anything important about visual QA goes there, NOT in `notes_for_next_run`. `notes_for_next_run` is for forward-looking hints (which models to watch for, what to update if X ships), not for hidden caveats about this run.

Record in the final JSON (see Output): `visual_qa_status`, `visual_qa_findings` (2-4 sentences, include the verbatim `[FAIL]` evidence if any), `visual_qa_test_url`.

# Step 6: Commit

Stage and commit ALL your changes with a single commit:

```
git add -A
git commit -m "improve: {commit_subject}" -m "<one short paragraph of rationale, including 'visual QA: passed via assrt' or the skip reason>"
```

Do NOT push. Do NOT amend prior commits. Do NOT run `git reset --hard`, `git checkout`, or delete branches. The repo's auto-commit agent handles pushing.

# Output

After committing, end your final assistant message with EXACTLY one fenced JSON block, nothing after it:

```json
{{
  "status": "committed" | "no_change" | "failed",
  "page_purpose": "homepage | seo_landing | pricing | feature | comparison | blog | docs | <your-own-label>",
  "page_job": "one sentence: what is this specific page's job",
  "primary_metric": "what you optimized for",
  "secondary_metric": "what you also weighted, lower",
  "tradeoffs_considered": "any change you rejected because it helped one metric but hurt the other; empty string if none",
  "files_modified": ["path/relative/to/repo", ...],
  "diff_summary": "one-paragraph plain-English summary of what changed and why",
  "rationale": "the single most important reason you expect this change to move the primary metric",
  "web_sources_used": ["url1", "url2", ...],
  "commit_sha": "<short sha or empty>",
  "visual_qa_status": "passed" | "failed" | "failed_preexisting" | "skipped_no_dev_server",
  "visual_qa_findings": "what assrt saw; empty string if passed clean",
  "visual_qa_test_url": "http://localhost:<port><page_path> or empty if skipped",
  "notes_for_next_run": "anything you want the next run to know (tests ran, hypotheses to validate, etc.)"
}}
```

If you genuinely cannot improve the page (it already does its job well and any change would be noise), set status="no_change" and explain in rationale. Do not ship busywork.
"""


def _render_brief_block(brief: dict) -> str:
    """Render the brief as a compact structured block Claude can skim.

    We pass the full project_config as pretty JSON at the bottom so the model
    has the source of truth (voice, positioning, qualification, proof points,
    pricing) without us having to re-synthesize it.
    """
    m = brief.get("metrics") or {}
    m24 = m.get("24h") or {}
    m7 = m.get("7d_avg_per_day") or {}
    m30 = m.get("30d_avg_per_day") or {}

    def _fmt_metric(row):
        if not row:
            return "n/a"
        parts = []
        for k in ("pageviews", "email_signups", "schedule_clicks", "get_started_clicks", "bookings"):
            v = row.get(k)
            parts.append(f"{k}={v}")
        return ", ".join(parts)

    hist_rows = brief.get("history") or []
    if hist_rows:
        hist = "\n".join(
            f"  - {h.get('at','?')} [{h.get('status','?')}] "
            f"{(h.get('diff_summary') or '').strip()}"
            for h in hist_rows
        )
    else:
        hist = "  (none; this page has not been touched by this pipeline before)"

    sel = brief.get("selection") or {}
    sel_reason = sel.get("reason") or "highest-traffic page in last 24h"
    skipped = sel.get("skipped_on_cooldown") or []
    if skipped:
        skipped_block = "\n".join(
            f"  - {s.get('path')}  ({s.get('views_24h')} views, {s.get('reason')})"
            for s in skipped
        )
        rotation_block = (
            f"Selection: {sel_reason}\n"
            f"Pages skipped on cooldown:\n{skipped_block}\n"
        )
    else:
        rotation_block = f"Selection: {sel_reason}\n"

    project_cfg = brief.get("project_config") or {}
    cfg_json = json.dumps(project_cfg, indent=2, ensure_ascii=False)
    guardrails_block = render_content_guardrails(project_cfg)
    guardrails_section = f"\n{guardrails_block}\n" if guardrails_block else ""
    return (
        f"Product: {brief.get('product')}\n"
        f"Domain: {brief.get('domain')}\n"
        f"Page path: {brief.get('page_path')}\n"
        f"Live URL: {brief.get('page_url')}\n"
        f"Repo (cwd): {brief.get('repo_path')}\n"
        f"\n"
        f"{rotation_block}"
        f"\n"
        f"Traffic + funnel:\n"
        f"  last 24h       : {_fmt_metric(m24)}\n"
        f"  7d avg / day   : {_fmt_metric(m7)}  (totals={json.dumps(m7.get('totals') or {})})\n"
        f"  30d avg / day  : {_fmt_metric(m30)}  (totals={json.dumps(m30.get('totals') or {})})\n"
        f"\n"
        f"Prior improvement runs on THIS page:\n{hist}\n"
        f"{guardrails_section}"
        f"\n"
        f"Full product config (authoritative source for voice, positioning, qualification, proof, pricing):\n"
        f"```json\n{cfg_json}\n```\n"
    )


def _git_head_sha(repo_path: str) -> str:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path, capture_output=True, text=True, timeout=15,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _git_changed_files(repo_path: str, base_sha: str) -> list[str]:
    if not base_sha:
        return []
    try:
        r = subprocess.run(
            ["git", "diff", "--name-only", base_sha, "HEAD"],
            cwd=repo_path, capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            return []
        return [line.strip() for line in r.stdout.splitlines() if line.strip()]
    except Exception:
        return []


def _extract_final_json(text: str) -> dict:
    """Pull the last fenced ```json ... ``` block or last {...} block."""
    if not text:
        return {}
    # prefer fenced
    fence = "```json"
    idx = text.rfind(fence)
    if idx != -1:
        rest = text[idx + len(fence):]
        end = rest.find("```")
        if end != -1:
            body = rest[:end].strip()
            try:
                return json.loads(body)
            except Exception:
                pass
    # fall back to last { ... } balanced block
    last_close = text.rfind("}")
    if last_close != -1:
        depth = 0
        for i in range(last_close, -1, -1):
            ch = text[i]
            if ch == "}":
                depth += 1
            elif ch == "{":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[i:last_close + 1])
                    except Exception:
                        break
    return {}


def _run_claude(prompt: str, cwd: str, log_path: Path, session_id: str) -> dict:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "claude", "-p", prompt,
        "--session-id", session_id,
        "--output-format", "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
    ]
    tool_counts: dict[str, int] = {}
    final_text = ""
    orch_cost = None
    start = time.time()

    # Bridge the Claude Code auto-update unlink window before spawning.
    if not wait_for_claude():
        return {"exit_code": 127, "final_text": "", "tool_counts": {},
                "error": "claude CLI not on PATH after wait_for_claude timeout"}

    with open(log_path, "w") as log_f:
        try:
            proc = subprocess.Popen(
                cmd, cwd=cwd,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1,
            )
        except FileNotFoundError:
            return {"exit_code": 127, "final_text": "", "tool_counts": {},
                    "error": "claude CLI not on PATH"}

        assert proc.stdout is not None
        while True:
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                continue
            log_f.write(line); log_f.flush()
            if time.time() - start > CLAUDE_TIMEOUT_SECONDS:
                proc.kill()
                return {"exit_code": 124, "final_text": final_text,
                        "tool_counts": tool_counts,
                        "error": f"timeout after {CLAUDE_TIMEOUT_SECONDS}s"}
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "assistant":
                for block in (ev.get("message") or {}).get("content") or []:
                    if block.get("type") == "tool_use":
                        name = block.get("name") or "unknown"
                        tool_counts[name] = tool_counts.get(name, 0) + 1
            elif ev.get("type") == "result":
                final_text = ev.get("result") or ""
                c = ev.get("total_cost_usd")
                if isinstance(c, (int, float)) and c > 0:
                    orch_cost = float(c)
        proc.wait()

    # fire-and-forget cost logging so runs show up in claude_sessions.
    # Forward the SDK-reported total_cost_usd from the result event as
    # --orchestrator-cost-usd so the SDK lane gets populated (SDK-only cost
    # mode, 2026-05-15); without it the row would have a NULL orchestrator
    # cost and the dashboard would show "missing SDK".
    logger = ROOT_DIR / "scripts" / "log_claude_session.py"
    if logger.exists():
        log_args = ["python3", str(logger), "--session-id", session_id,
                    "--script", "seo_improve_page"]
        if orch_cost is not None and orch_cost > 0:
            log_args.extend(["--orchestrator-cost-usd", str(orch_cost)])
        try:
            subprocess.run(log_args, capture_output=True, text=True, timeout=30)
        except Exception:
            pass

    return {
        "exit_code": proc.returncode,
        "final_text": final_text,
        "tool_counts": tool_counts,
    }


def _insert_running_row(brief: dict) -> int:
    resp = api_post("/api/v1/seo/page-improvements", {
        "product": brief["product"],
        "domain": brief["domain"],
        "page_path": brief["page_path"],
        "page_url": brief["page_url"],
        "metrics_24h": brief["metrics"]["24h"],
        "metrics_7d_avg": brief["metrics"]["7d_avg_per_day"],
        "metrics_30d_avg": brief["metrics"]["30d_avg_per_day"],
        "brief_json": brief,
    })
    return int((resp.get("data") or {}).get("id"))


def _finish_row(row_id: int, **fields):
    if not fields:
        return
    body = {"id": row_id}
    body.update(fields)
    api_patch("/api/v1/seo/page-improvements", body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brief", required=True, help="Path to brief JSON from pick_top_page.py")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the rendered prompt and exit; do not launch Claude or write to DB")
    args = ap.parse_args()

    brief = json.loads(Path(args.brief).read_text())
    repo_path = brief["repo_path"]
    if not os.path.isdir(repo_path):
        raise SystemExit(f"ERROR: repo_path does not exist: {repo_path}")

    # Belt-and-suspenders homepage guard. pick_top_page.py already filters the
    # homepage out of the candidate list when project_config.landing_pages.
    # homepage_protected is true, but operators sometimes invoke improve_page.py
    # directly with a hand-built brief. Refuse to run on `/` when the source
    # project says the homepage is off-limits.
    page_path = (brief.get("page_path") or "").strip()
    project_cfg = brief.get("project_config") or {}
    lp = project_cfg.get("landing_pages") or {}
    if lp.get("homepage_protected") and page_path in ("/", "", "/index", "/index.html"):
        raise SystemExit(
            f"ERROR: refusing to improve homepage of '{brief.get('product')}' "
            f"(page_path={page_path!r}); landing_pages.homepage_protected=true"
        )

    commit_subject = f"{brief['page_path']} (top-traffic improve run)"
    prompt = PROMPT_TEMPLATE.format(
        brief_block=_render_brief_block(brief),
        commit_subject=commit_subject,
    )

    if args.dry_run:
        sys.stdout.write(prompt)
        return

    session_id = str(uuid.uuid4())
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    safe_slug = (brief["page_path"] or "root").strip("/").replace("/", "_") or "root"
    log_dir = SCRIPT_DIR / "logs" / brief["product"].lower() / "improve"
    log_path = log_dir / f"{ts}_{safe_slug}_stream.jsonl"

    row_id = _insert_running_row(brief)
    base_sha = _git_head_sha(repo_path)

    result = _run_claude(prompt=prompt, cwd=repo_path, log_path=log_path, session_id=session_id)
    head_sha = _git_head_sha(repo_path)
    changed_files = _git_changed_files(repo_path, base_sha) if base_sha else []

    final_json = _extract_final_json(result.get("final_text") or "")
    model_status = (final_json.get("status") or "").lower() if final_json else ""

    # Ground truth: did the commit actually move?
    committed = bool(base_sha and head_sha and head_sha != base_sha)
    if result.get("exit_code") not in (0,):
        status = "failed"
    elif committed:
        status = "committed"
    elif model_status == "no_change":
        status = "no_change"
    else:
        # Model claimed success but HEAD didn't move. Flag as no_change — auto
        # commit agent may still pick up uncommitted changes below, but that's
        # a separate story.
        status = "no_change"

    err = result.get("error") or None

    _finish_row(
        row_id,
        claude_session_id=session_id,
        run_log_path=str(log_path),
        tool_summary=result.get("tool_counts") or {},
        final_result_text=(result.get("final_text") or ""),
        commit_sha=head_sha if committed else None,
        files_modified=changed_files or (final_json.get("files_modified") or []),
        diff_summary=(final_json.get("diff_summary") or "") if final_json else None,
        rationale=(final_json.get("rationale") or "") if final_json else None,
        status=status,
        error=err,
    )

    print(json.dumps({
        "row_id": row_id,
        "session_id": session_id,
        "status": status,
        "exit_code": result.get("exit_code"),
        "commit_sha": head_sha if committed else None,
        "files_modified": changed_files,
        "log_path": str(log_path),
    }, indent=2))


if __name__ == "__main__":
    main()
