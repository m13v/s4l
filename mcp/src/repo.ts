// Repo + process helpers for the social-autoposter MCP wrapper.
// This MCP is a THIN client: it shells out to the existing pipeline scripts in
// the social-autoposter repo and never reimplements pipeline logic.

import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";
import os from "node:os";
import fs from "node:fs";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// dist/repo.js -> repo root is two levels up (mcp/dist -> mcp -> repo root).
// Override with SAPS_REPO_DIR for non-standard installs.
export const REPO_DIR =
  process.env.SAPS_REPO_DIR || path.resolve(__dirname, "..", "..");

// Python used by the pipeline (psycopg2 etc). Override per-install.
export const PYTHON = process.env.SAPS_PYTHON || "python3";

export const TMP_DIR = os.tmpdir();

export interface RunResult {
  code: number;
  stdout: string;
  stderr: string;
}

// Spawn a process inside the repo, inheriting the repo env (DATABASE_URL etc
// come from the install's environment / .env loaded by the scripts themselves).
export function run(
  cmd: string,
  args: string[],
  opts: { cwd?: string; timeoutMs?: number; env?: NodeJS.ProcessEnv } = {}
): Promise<RunResult> {
  return new Promise((resolve) => {
    const child = spawn(cmd, args, {
      cwd: opts.cwd || REPO_DIR,
      env: { ...process.env, ...(opts.env || {}) },
    });
    let stdout = "";
    let stderr = "";
    let timer: NodeJS.Timeout | undefined;
    if (opts.timeoutMs) {
      timer = setTimeout(() => {
        child.kill("SIGTERM");
      }, opts.timeoutMs);
    }
    child.stdout.on("data", (d) => (stdout += d.toString()));
    child.stderr.on("data", (d) => (stderr += d.toString()));
    child.on("close", (code) => {
      if (timer) clearTimeout(timer);
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
  return run(PYTHON, [scriptRelPath, ...args], opts);
}

// ---- Plan file helpers (the manual-mode draft envelope) --------------------
// Drafts produced by Phase 2b-prep live at /tmp/twitter_cycle_plan_<batch>.json
// We add a per-candidate `approved` flag (sidecar field) to drive the manual
// review loop without touching the pipeline's own fields.

export interface PlanCandidate {
  candidate_id?: string | number;
  candidate_url?: string;
  thread_author?: string;
  reply_text?: string;
  engagement_style?: string;
  link_url?: string;
  link_keyword?: string;
  search_topic?: string;
  approved?: boolean;
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
