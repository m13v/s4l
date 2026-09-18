// Repo + process helpers for the social-autoposter MCP wrapper.
// This MCP is a THIN client: it shells out to the existing pipeline scripts in
// the social-autoposter repo and never reimplements pipeline logic.

import { spawn, type ChildProcess } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";
import fs from "node:fs";
import { resolvePython, resolveRepoDir } from "./runtime.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// The pipeline repo the wrapper shells out to. Resolved DYNAMICALLY per call (a
// getter, not a load-time const) because a bare .mcpb double-click materializes
// the repo AFTER the server boots; the very next shell-out must pick it up
// without a restart. resolveRepoDir() prefers a real S4L_REPO_DIR clone, then
// the materialized repo recorded in runtime.json, then a dev fallback.
export function repoDir(): string {
  return resolveRepoDir();
}

// Python used by the pipeline. Resolved DYNAMICALLY per call (not a load-time
// const) because the owned uv runtime can be provisioned AFTER the server boots
// — the first-run installer writes runtime.json and the very next runPython
// call must pick up the owned interpreter without a server restart.
// resolvePython() prefers the owned runtime, then S4L_PYTHON, then python3.

// The locked pipeline script (run-twitter-cycle.sh) writes the draft plan to a
// HARDCODED /tmp path (`PLAN_FILE="/tmp/twitter_cycle_plan_<batch>.json"`), and the
// `DRAFT_ONLY_PLAN=` marker the wrapper parses is rooted at /tmp too. We MUST read
// and write plans from the same directory. os.tmpdir() is NOT /tmp on macOS (it
// honors $TMPDIR, e.g. /var/folders/.../T, or /tmp/claude-501 inside Claude Code),
// which silently stranded every draft and made draft_cycle always report
// "No drafts in batch ...". Default to /tmp to match the script; allow an explicit
// override for non-standard installs.
export const TMP_DIR = process.env.S4L_TMP_DIR || "/tmp";

export interface RunResult {
  code: number;
  stdout: string;
  stderr: string;
}

// ---- Raw-line telemetry sink -----------------------------------------------
// Every subprocess (locked pipeline scripts included) flows through run(), so
// this is the ONE boundary where we can tee the verbatim stdout/stderr stream
// off-box for troubleshooting. telemetry.ts registers a sink here at boot; when
// none is registered (default, dev, or S4L_LOG_STREAM=0) the tee is a no-op.
// The sink must never throw and must never block the child's I/O — it only
// buffers a line in memory.
export type LineSink = (
  line: string,
  stream: "stdout" | "stderr",
  context: string
) => void;

let lineSink: LineSink | null = null;

export function setLineSink(fn: LineSink | null): void {
  lineSink = fn;
}

// Derive a short, stable context label for a spawned command so log lines can
// be grouped by which script produced them (e.g. "scripts/seed_search_topics.py",
// "skill/run-twitter-cycle.sh"). Best-effort; never throws.
function deriveContext(cmd: string, args: string[]): string {
  try {
    const base = cmd.split("/").pop() || cmd;
    if (/python/i.test(base) || base === "bash" || base === "sh" || base === "node") {
      const first = args.find((a) => !a.startsWith("-"));
      if (first) return first;
    }
    return [base, args[0] ?? ""].join(" ").trim();
  } catch {
    return cmd;
  }
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
    // Optional override for the telemetry context label (defaults to a value
    // derived from cmd/args). Callers can set a friendlier name (e.g. the tool
    // that invoked the run) without changing the line stream itself.
    logContext?: string;
    // Suppress teeing this command's stdout/stderr to the Cloud Logging relay.
    // For high-volume status probes (e.g. `launchctl list`) whose output is a
    // big useless dump — they still return stdout to the caller in-memory, they
    // just don't flood Cloud Logging. Buffered stdout/stderr are unaffected.
    noTee?: boolean;
    onLine?: (line: string, stream: "stdout" | "stderr") => void;
    // Hands the spawned child to the caller so a long-running job (e.g. a scan)
    // can be aborted out-of-band — used to preempt an in-flight scan when the
    // user approves a post (posting takes the browser immediately).
    onSpawn?: (child: ChildProcess) => void;
    // Written to the child's stdin, then stdin is closed. Used to pass secrets
    // (e.g. an Authorization header via curl -H @-) without exposing them in
    // argv / `ps` output.
    stdin?: string;
  } = {}
): Promise<RunResult> {
  return new Promise((resolve) => {
    const child = spawn(cmd, args, {
      cwd: opts.cwd || repoDir(),
      env: { ...process.env, ...(opts.env || {}) },
    });
    try {
      opts.onSpawn?.(child);
    } catch {
      /* a spawn observer must never break the run */
    }
    if (opts.stdin != null) {
      // Swallow async pipe errors (e.g. EPIPE when the child exits before
      // reading) — an unhandled stream error would crash the whole process.
      child.stdin.on("error", () => {});
      try {
        child.stdin.write(opts.stdin);
        child.stdin.end();
      } catch {
        /* a closed stdin must never break the run */
      }
    }
    let stdout = "";
    let stderr = "";
    // Cap captured output to a tail. run() used to accumulate a child's full
    // stdout+stderr unbounded for the child's lifetime; long-lived spawns (the
    // 13h voice backfill) and stuck children pinned those strings and were the
    // main driver of multi-GB server heaps. Callers that parse output read
    // either whole small payloads or the LAST line, so keeping the tail
    // preserves semantics. 8 MB per stream is far above any legitimate payload.
    const MAX_CAPTURE_BYTES = 8 * 1024 * 1024;
    const capTail = (s: string): string =>
      s.length > MAX_CAPTURE_BYTES ? s.slice(s.length - (MAX_CAPTURE_BYTES >> 1)) : s;
    // The line-splitter buffers below only shed at '\n', so a child emitting
    // long newline-free output (carriage-return progress bars, one huge line)
    // would grow them unbounded for its lifetime — the same heap pathology the
    // capture cap exists for. Keep only a tail of an oversized partial line
    // (telemetry truncates lines to 8KB anyway).
    const MAX_LINEBUF_BYTES = 1024 * 1024;
    const capLineBuf = (buf: string): string =>
      buf.length > MAX_LINEBUF_BYTES ? buf.slice(-8192) : buf;
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
      return capLineBuf(buf);
    };
    // Parallel whole-line splitter that tees to the telemetry sink (if any),
    // kept separate from the onLine pump so neither path can affect the other.
    const logCtx = opts.logContext || deriveContext(cmd, args);
    let sinkOutBuf = "";
    let sinkErrBuf = "";
    const sinkPump = (chunk: string, which: "stdout" | "stderr", buf: string): string => {
      const sink = opts.noTee ? null : lineSink;
      if (!sink) return buf;
      buf += chunk;
      let nl: number;
      while ((nl = buf.indexOf("\n")) !== -1) {
        const line = buf.slice(0, nl);
        buf = buf.slice(nl + 1);
        try {
          sink(line, which, logCtx);
        } catch {
          /* the telemetry sink must never break the wrapped command */
        }
      }
      return capLineBuf(buf);
    };
    let settled = false;
    let timer: NodeJS.Timeout | undefined;
    let killTimer: NodeJS.Timeout | undefined;
    if (opts.timeoutMs) {
      timer = setTimeout(() => {
        child.kill("SIGTERM");
        // A child that ignores SIGTERM used to live (and hold its pipes and our
        // buffers) forever. Escalate — but GENEROUSLY: children trap SIGTERM on
        // purpose to finish critical bookkeeping (twitter_post_plan.py finishes
        // the current candidate's post + recording, which can take minutes, so a
        // fast SIGKILL would reopen the posted-but-unrecorded double-post
        // window). 15 min covers every graceful path; only a truly wedged child
        // gets hard-killed.
        killTimer = setTimeout(() => {
          try {
            child.kill("SIGKILL");
          } catch {
            /* already gone */
          }
        }, 15 * 60_000);
        killTimer.unref();
      }, opts.timeoutMs);
    }
    child.stdout.on("data", (d) => {
      if (settled) return;
      const s = d.toString();
      stdout = capTail(stdout + s);
      outBuf = pump(s, "stdout", outBuf);
      sinkOutBuf = sinkPump(s, "stdout", sinkOutBuf);
    });
    child.stderr.on("data", (d) => {
      if (settled) return;
      const s = d.toString();
      stderr = capTail(stderr + s);
      errBuf = pump(s, "stderr", errBuf);
      sinkErrBuf = sinkPump(s, "stderr", sinkErrBuf);
    });
    const finish = (code: number | null) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      if (killTimer) clearTimeout(killTimer);
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
      const sink = opts.noTee ? null : lineSink;
      if (sink) {
        if (sinkOutBuf)
          try {
            sink(sinkOutBuf, "stdout", logCtx);
          } catch {
            /* ignore */
          }
        if (sinkErrBuf)
          try {
            sink(sinkErrBuf, "stderr", logCtx);
          } catch {
            /* ignore */
          }
      }
      resolve({ code: code ?? -1, stdout, stderr });
    };
    child.on("close", (code) => finish(code));
    child.on("exit", (code) => {
      // 'close' waits for the stdio pipes to drain, which never happens when a
      // grandchild inherited them and outlives the child — the promise (and the
      // captured output) then leaked for the grandchild's lifetime. After the
      // child itself exits, give the pipes a short grace to flush, then settle.
      const drain = setTimeout(() => finish(code), 5_000);
      drain.unref();
    });
    child.on("error", (err) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      if (killTimer) clearTimeout(killTimer);
      resolve({ code: -1, stdout, stderr: stderr + String(err) });
    });
  });
}

export function runPython(
  scriptRelPath: string,
  args: string[],
  opts: {
    timeoutMs?: number;
    env?: NodeJS.ProcessEnv;
    // Pass-throughs to run(): onLine lets callers stream a python script's output
    // live (e.g. the poster, so handled failures surface in main.log + telemetry
    // instead of staying buffered); onSpawn hands back the child for preemption.
    onLine?: (line: string, stream: "stdout" | "stderr") => void;
    onSpawn?: (child: ChildProcess) => void;
  } = {}
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
  matched_project?: string;
  search_topic?: string;
  language?: string;
  approved?: boolean;
  // Set true once this candidate has actually been posted, so the two review
  // surfaces (chat + menu-bar pop-ups) can't post the same draft twice.
  posted?: boolean;
  // Set once the poster reached a terminal non-post outcome (dedup, deleted
  // tweet, no reply URL captured, etc.). Kept separate from posted so reporting
  // can stay honest while review cards stop re-offering dead drafts.
  terminal?: boolean;
  terminal_reason?: string;
  // Approved but the post attempt errored (post_error says why). Stamped by the
  // menubar (store_mark_post_failed) and cleared by a fresh approval or a posted
  // outcome. Read through candidateState() in index.ts — never raw.
  post_failed?: boolean;
  post_error?: string;
  // Count of post attempts that ended in a transient "failed" outcome. Sticky
  // approved cards retry across drains; when this reaches the give-up bound
  // (see MAX_POST_ATTEMPTS in index.ts) the card flips terminal instead of
  // retrying forever (2026-07-17: unbounded stickiness is how a single card
  // retried 438 times over 5 days on the Nhat install).
  post_attempts?: number;
  our_url?: string;
  // Two-draft cards (2026-07-07; no-recommendation pass 2026-07-08): a
  // fresh candidate carries both drafts (one per assigned engagement style
  // this cycle). The model doesn't rank them, the card defaults to Draft A
  // (index 0) and the reviewer's own click can switch to Draft B. Absent on
  // reused/stale-draft candidates and legacy plans, the card then falls
  // back to the single-draft UI driven by reply_text/engagement_style
  // above. assigned_style/assigned_mode per draft feed twitter_post_plan.py's
  // per-candidate drift-coercion override.
  drafts?: Array<{
    variant: "a" | "b";
    text: string;
    style?: string;
    text_en?: string | null;
    assigned_style?: string | null;
    assigned_mode?: string | null;
  }>;
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
  // Compact (no indent): the review store reached 65 MB with pretty-printing
  // (2026-08-02 lag incident) and every reader pays the parse.
  fs.writeFileSync(planPath(batchId), JSON.stringify(plan), "utf-8");
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
