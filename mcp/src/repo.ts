// Repo + process helpers for the social-autoposter MCP wrapper.
// This MCP is a THIN client: it shells out to the existing pipeline scripts in
// the social-autoposter repo and never reimplements pipeline logic.

import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";
import fs from "node:fs";
import { resolvePython, resolveRepoDir } from "./runtime.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// The pipeline repo the wrapper shells out to. Resolved DYNAMICALLY per call (a
// getter, not a load-time const) because a bare .mcpb double-click materializes
// the repo AFTER the server boots; the very next shell-out must pick it up
// without a restart. resolveRepoDir() prefers a real SAPS_REPO_DIR clone, then
// the materialized repo recorded in runtime.json, then a dev fallback.
export function repoDir(): string {
  return resolveRepoDir();
}

// Python used by the pipeline. Resolved DYNAMICALLY per call (not a load-time
// const) because the owned uv runtime can be provisioned AFTER the server boots
// — the first-run installer writes runtime.json and the very next runPython
// call must pick up the owned interpreter without a server restart.
// resolvePython() prefers the owned runtime, then SAPS_PYTHON, then python3.

// The locked pipeline script (run-twitter-cycle.sh) writes the draft plan to a
// HARDCODED /tmp path (`PLAN_FILE="/tmp/twitter_cycle_plan_<batch>.json"`), and the
// `DRAFT_ONLY_PLAN=` marker the wrapper parses is rooted at /tmp too. We MUST read
// and write plans from the same directory. os.tmpdir() is NOT /tmp on macOS (it
// honors $TMPDIR, e.g. /var/folders/.../T, or /tmp/claude-501 inside Claude Code),
// which silently stranded every draft and made draft_cycle always report
// "No drafts in batch ...". Default to /tmp to match the script; allow an explicit
// override for non-standard installs.
export const TMP_DIR = process.env.SAPS_TMP_DIR || "/tmp";

export interface RunResult {
  code: number;
  stdout: string;
  stderr: string;
}

// Spawn a process inside the repo, inheriting the repo env (API base + keys
// come from the install's environment / .env loaded by the scripts themselves).
//
// `onLine` (optional) fires once per COMPLETE line as the child emits output,
// so a long-running script (e.g. run-twitter-cycle.sh, which can churn for
// minutes) can be followed live instead of going dark until it exits. The full
// buffered stdout/stderr are still returned unchanged, so existing callers are
// unaffected. A throwing sink never breaks the run.
export function run(
  cmd: string,
  args: string[],
  opts: {
    cwd?: string;
    timeoutMs?: number;
    env?: NodeJS.ProcessEnv;
    onLine?: (line: string, stream: "stdout" | "stderr") => void;
  } = {}
): Promise<RunResult> {
  return new Promise((resolve) => {
    const child = spawn(cmd, args, {
      cwd: opts.cwd || repoDir(),
      env: { ...process.env, ...(opts.env || {}) },
    });
    let stdout = "";
    let stderr = "";
    // Per-stream partial-line buffers so onLine fires on whole lines only,
    // regardless of how the OS chunks the pipe reads.
    let outBuf = "";
    let errBuf = "";
    const pump = (chunk: string, which: "stdout" | "stderr", buf: string): string => {
      if (!opts.onLine) return buf;
      buf += chunk;
      let nl: number;
      while ((nl = buf.indexOf("\n")) !== -1) {
        const line = buf.slice(0, nl);
        buf = buf.slice(nl + 1);
        try {
          opts.onLine(line, which);
        } catch {
          /* a progress sink must never break the wrapped command */
        }
      }
      return buf;
    };
    let timer: NodeJS.Timeout | undefined;
    if (opts.timeoutMs) {
      timer = setTimeout(() => {
        child.kill("SIGTERM");
      }, opts.timeoutMs);
    }
    child.stdout.on("data", (d) => {
      const s = d.toString();
      stdout += s;
      outBuf = pump(s, "stdout", outBuf);
    });
    child.stderr.on("data", (d) => {
      const s = d.toString();
      stderr += s;
      errBuf = pump(s, "stderr", errBuf);
    });
    child.on("close", (code) => {
      if (timer) clearTimeout(timer);
      // Flush any trailing partial line (output with no terminating newline).
      if (opts.onLine) {
        if (outBuf)
          try {
            opts.onLine(outBuf, "stdout");
          } catch {
            /* ignore */
          }
        if (errBuf)
          try {
            opts.onLine(errBuf, "stderr");
          } catch {
            /* ignore */
          }
      }
      resolve({ code: code ?? -1, stdout, stderr });
    });
    child.on("error", (err) => {
      if (timer) clearTimeout(timer);
      resolve({ code: -1, stdout, stderr: stderr + String(err) });
    });
  });
}

export function runPython(
  scriptRelPath: string,
  args: string[],
  opts: { timeoutMs?: number; env?: NodeJS.ProcessEnv } = {}
): Promise<RunResult> {
  return run(resolvePython(), [scriptRelPath, ...args], opts);
}

// ---- Plan file helpers (the manual-mode draft envelope) --------------------
// Drafts produced by Phase 2b-prep live at /tmp/twitter_cycle_plan_<batch>.json
// We add a per-candidate `approved` flag (sidecar field) to drive the manual
// review loop without touching the pipeline's own fields.

export interface PlanCandidate {
  candidate_id?: string | number;
  candidate_url?: string;
  thread_author?: string;
  thread_text?: string;
  reply_text?: string;
  engagement_style?: string;
  link_url?: string;
  link_keyword?: string;
  search_topic?: string;
  language?: string;
  approved?: boolean;
  // Set true once this candidate has actually been posted, so the two review
  // surfaces (chat + menu-bar pop-ups) can't post the same draft twice.
  posted?: boolean;
  [k: string]: unknown;
}

export interface Plan {
  candidates?: PlanCandidate[];
  rejected?: unknown[];
  session_id?: string;
  assigned_style?: string;
  assigned_mode?: string;
  [k: string]: unknown;
}

export function planPath(batchId: string): string {
  return path.join(TMP_DIR, `twitter_cycle_plan_${batchId}.json`);
}

export function readPlan(batchId: string): Plan | null {
  const p = planPath(batchId);
  if (!fs.existsSync(p)) return null;
  try {
    return JSON.parse(fs.readFileSync(p, "utf-8")) as Plan;
  } catch {
    return null;
  }
}

export function writePlan(batchId: string, plan: Plan): void {
  fs.writeFileSync(planPath(batchId), JSON.stringify(plan, null, 2), "utf-8");
}

// Find the newest plan file when no batch id is supplied.
export function latestBatchId(): string | null {
  let files: string[];
  try {
    files = fs.readdirSync(TMP_DIR);
  } catch {
    return null;
  }
  const matches = files
    .map((f) => /^twitter_cycle_plan_(.+)\.json$/.exec(f))
    .filter((m): m is RegExpExecArray => !!m && !m[1].endsWith("_approved"))
    .map((m) => ({
      batchId: m[1],
      mtime: fs.statSync(path.join(TMP_DIR, m[0])).mtimeMs,
    }))
    .sort((a, b) => b.mtime - a.mtime);
  return matches.length ? matches[0].batchId : null;
}

// ---- Scan/draft split helpers (Desktop-session autopilot) ------------------
// scan_candidates runs the pipeline in SCAN_ONLY mode (scan -> score -> select)
// and the cycle writes the chosen candidates as JSON to scanResultPath(batchId).
// A Claude Desktop scheduled-task session reads them, drafts replies ITSELF (on
// the user's plan — no `claude -p`), then hands them back via submit_drafts,
// which writes the SAME plan file draft_cycle / post_drafts / the menu bar use.

export interface ScanCandidate {
  id: number;
  tweet_url: string;
  author_handle: string;
  tweet_text: string;
  virality_score: number;
  delta_score: number;
  matched_project: string;
  search_topic: string;
  likes: number;
  retweets: number;
  replies: number;
  views: number;
  author_followers: number;
  age_hours: number;
  existing_draft?: string;
  existing_draft_style?: string;
}

// Hardcoded under TMP_DIR to mirror the cycle script's SCAN_ONLY writer (same
// coupling planPath has: run-twitter-cycle.sh writes the file to /tmp directly).
export function scanResultPath(batchId: string): string {
  return path.join(TMP_DIR, `saps_scan_candidates_${batchId}.json`);
}
