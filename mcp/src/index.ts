#!/usr/bin/env node
// social-autoposter MCP server (X/Twitter rail).
//
// Core tools:
//   scan_candidates - scan + score X threads, return the top candidates (no draft, no post).
//   submit_drafts   - take the replies you drafted and queue them for review (posts nothing).
//   post_drafts     - post the drafts the user chose by number from a batch.
//   get_stats    - read-only post + engagement stats.
//
// THIN wrapper. The pipeline brain (scan, score, drafting prompts, posting)
// stays in the Python/shell scripts; we only orchestrate and present.

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { execFileSync, type ChildProcess } from "node:child_process";
import { z } from "zod";
import { screencast, bringBrowserToFront } from "./screencast.js";
import os from "node:os";
import path from "node:path";
import fs from "node:fs";
import {
  repoDir,
  runPython,
  run,
  readPlan,
  writePlan,
  planPath,
  scanResultPath,
  type Plan,
  type PlanCandidate,
  type ScanCandidate,
} from "./repo.js";
import {
  applySetup,
  resolveProject,
  hasReadyProject,
  listManagedProjectStatus,
  ensureShortLinksDefault,
  REQUIRED_FIELDS,
  RECOMMENDED_FIELDS,
  configPath,
  type ProjectInput,
} from "./setup.js";
import { xStatus, xConnect, xDetectSources, xScanProfile, summarizeXAuth } from "./twitterAuth.js";
import {
  startProvisioning,
  isProvisioning,
  readProgress,
  runtimeReady,
  readRuntime,
  resolvePython,
  resolveChrome,
  ensureMenubar,
  ensurePipelineCurrent,
  ensureRuntimeProvisioned,
} from "./runtime.js";
import {
  blockOnboardingMilestone,
  completeOnboardingMilestone,
  ensureDoctorPhase,
  onboardingLedger,
  onboardingSnapshot,
  recordOnboardingAttempt,
  runDoctorPhase,
  type DoctorPhase,
} from "./onboarding.js";
import { VERSION, versionStatus, latestPublishedVersion } from "./version.js";
import { initSentry, sendHeartbeat, captureError, flushSentry, startLogStreaming, flushLogs } from "./telemetry.js";
import {
  registerAppTool,
  registerAppResource,
  RESOURCE_MIME_TYPE,
  getUiCapability,
} from "@modelcontextprotocol/ext-apps/server";
import { fileURLToPath } from "node:url";
import http from "node:http";

// MCP Apps control panel. The self-contained HTML is built by vite
// (vite-plugin-singlefile) into dist/panel.html alongside this compiled file.
const DIST_DIR = path.dirname(fileURLToPath(import.meta.url));
const PANEL_URI = "ui://social-autoposter/panel.html";
const PRODUCT_LINK_URI = "ui://social-autoposter/product-link.html";

// Stable id for the accumulating draft review queue. submit_drafts appends each
// run's drafts here (dedup by tweet URL) so the menu-bar cards PILE UP across a
// continuous autopilot instead of each run overwriting the last; post_drafts posts
// the approved subset and marks them posted (filtered out of the cards thereafter).
const REVIEW_QUEUE_ID = "review-queue";

// The Desktop scheduled task onboarding creates for the autopilot. Its presence on
// disk is the single "autopilot is set up" signal the dashboard + menu bar share
// (the legacy launchd autopilot is retired).
const AUTOPILOT_TASK_ID = "social-autoposter-autopilot";

// ---- Queue-backed drafting (2026-06-23) -----------------------------------
// Customer .mcpb boxes have no `claude` CLI, so the deterministic pipeline can't
// run its `claude -p` steps directly. Instead a launchd job kicks the REAL
// pipeline (run-twitter-cycle.sh in DRAFT_ONLY mode with SAPS_CLAUDE_PROVIDER=
// queue); each `claude -p` call enqueues onto scripts/claude_job.py's file queue
// and blocks. Two Claude Desktop scheduled tasks — one per job type — drain that
// queue, run the pipeline's own prompt as a Claude turn, and write the result
// back, unblocking the cycle. This reuses the entire pipeline (styles, voice,
// top-performers, em-dash rules) instead of the old scan_candidates host-draft
// reimplementation. See scripts/claude_job.py + run_claude.sh's provider seam.
const PHASE1_TASK_ID = "saps-phase1-query"; // drains "twitter-query" jobs
const PHASE2B_TASK_ID = "saps-phase2b-draft"; // drains "twitter-prep" jobs

const TWITTER_AUTOPILOT_LABEL = "com.m13v.social-twitter-cycle";
const TWITTER_AUTOPILOT_PLIST = path.join(
  os.homedir(),
  "Library",
  "LaunchAgents",
  `${TWITTER_AUTOPILOT_LABEL}.plist`
);

// Daily self-updater. Enabled alongside autopilot so a hands-free (headless)
// install keeps itself current — the interactive `runtime` tool (action:'update')
// only helps when
// a human-facing agent session is open, which an autopilot box never has.
const UPDATER_LABEL = "com.m13v.social-autoposter-update";
const UPDATER_PLIST = path.join(
  os.homedir(),
  "Library",
  "LaunchAgents",
  `${UPDATER_LABEL}.plist`
);

// A sane PATH for launchd jobs (launchd starts with a bare PATH). Include the
// node bin dir so `npx`/`npm` resolve inside the updater.
const LAUNCHD_PATH = [
  path.dirname(process.execPath),
  "/opt/homebrew/bin",
  "/usr/local/bin",
  "/usr/bin",
  "/bin",
  "/usr/sbin",
  "/sbin",
].join(":");

// Bin dirs the pipeline must resolve FIRST: the owned uv venv (so the scripts'
// bare `python3` hits the provisioned interpreter with pipeline deps, not the
// user's system python) and ~/.local/bin (so `browser-harness`, the CDP scan
// engine, resolves). resolvePython() is dynamic, so this re-derives per call.
function ownedBinDirs(): string[] {
  const dirs: string[] = [];
  const py = resolvePython();
  if (path.isAbsolute(py)) dirs.push(path.dirname(py));
  dirs.push(path.join(os.homedir(), ".local", "bin"));
  return dirs;
}

// PATH for an interactively-spawned pipeline run (draft_cycle): owned bins
// first, then whatever PATH the MCP server inherited.
function pipelinePath(): string {
  return [...ownedBinDirs(), process.env.PATH || LAUNCHD_PATH].join(":");
}

// PATH baked into launchd plists (autopilot/cron): owned bins first, then the
// sane launchd default (launchd starts with a bare PATH).
function launchdPath(): string {
  return [...ownedBinDirs(), LAUNCHD_PATH].join(":");
}

function plistXml(opts: {
  label: string;
  programArgs: string[];
  intervalSecs: number;
  runAtLoad: boolean;
  stdoutLog: string;
  stderrLog: string;
  extraEnv?: Record<string, string>;
}): string {
  const args = opts.programArgs.map((a) => `\t\t<string>${a}</string>`).join("\n");
  // Background (cron/autopilot) runs get the same Chrome the interactive cycle
  // uses, so a no-sudo ~/Applications install (which the shell's own resolver
  // doesn't scan) is still found off-screen. Omitted when Chrome resolves via
  // PATH, so the shell's _resolve_chrome_bin stays the fallback.
  const chrome = resolveChrome();
  const chromeEnv = chrome
    ? `\n\t\t<key>BH_CHROME_BIN</key>\n\t\t<string>${chrome}</string>`
    : "";
  // Caller-supplied env (e.g. the queue kicker's DRAFT_ONLY / SAPS_CLAUDE_PROVIDER).
  // Rendered after the baked-in vars so a caller can also override SAPS_STATE_DIR.
  const extraEnv = opts.extraEnv
    ? Object.entries(opts.extraEnv)
        .map(([k, v]) => `\n\t\t<key>${k}</key>\n\t\t<string>${v}</string>`)
        .join("")
    : "";
  return `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
\t<key>Label</key>
\t<string>${opts.label}</string>
\t<key>ProgramArguments</key>
\t<array>
${args}
\t</array>
\t<key>StartInterval</key>
\t<integer>${opts.intervalSecs}</integer>
\t<key>StandardOutPath</key>
\t<string>${opts.stdoutLog}</string>
\t<key>StandardErrorPath</key>
\t<string>${opts.stderrLog}</string>
\t<key>EnvironmentVariables</key>
\t<dict>
\t\t<key>PATH</key>
\t\t<string>${launchdPath()}</string>
\t\t<key>HOME</key>
\t\t<string>${os.homedir()}</string>
\t\t<key>SAPS_REPO_DIR</key>
\t\t<string>${repoDir()}</string>
\t\t<key>SAPS_PYTHON</key>
\t\t<string>${resolvePython()}</string>${chromeEnv}${extraEnv}
\t</dict>
\t<key>RunAtLoad</key>
\t<${opts.runAtLoad ? "true" : "false"}/>
</dict>
</plist>
`;
}

// Write a plist only if it does not already exist, so we never clobber a
// hand-tuned plist (e.g. a dev box with custom EnvironmentVariables). Returns
// whether it created a new file.
function ensurePlist(p: string, xml: string): boolean {
  if (fs.existsSync(p)) return false;
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, xml, "utf-8");
  return true;
}

async function loadPlist(label: string, plistPath: string, uid: number) {
  let res = await run("launchctl", ["bootstrap", `gui/${uid}`, plistPath], { timeoutMs: 15_000 });
  if (res.code !== 0) {
    res = await run("launchctl", ["load", plistPath], { timeoutMs: 15_000 });
  }
  return res;
}

async function unloadPlist(label: string, plistPath: string, uid: number) {
  let res = await run("launchctl", ["bootout", `gui/${uid}/${label}`], { timeoutMs: 15_000 });
  if (res.code !== 0) {
    res = await run("launchctl", ["unload", plistPath], { timeoutMs: 15_000 });
  }
  return res;
}

// version is resolved at runtime from the real shipped package (see version.ts),
// so serverInfo.version finally reflects what the user actually has installed
// instead of a frozen literal.
const server = new McpServer(
  {
    name: "S4L",
    version: VERSION,
  },
  {
    // Host injects this into the agent's system prompt, so be proactive about
    // WHEN this tool earns its keep, not just what it does.
    instructions:
      "social-autoposter drafts, reviews, and posts X/Twitter replies for the user's product, and " +
      "can run them hands-free on autopilot.\n\n" +
      "ONBOARDING IS A TERMINAL GOAL. When the user asks to set up, install, configure, or onboard " +
      "social-autoposter, keep taking the next safe action until the owned runtime is ready, a " +
      "project is fully configured with seeded search topics, X is connected with its real handle, " +
      "the draft path (`scan_candidates` -> draft -> `submit_drafts`) has verified the pipeline " +
      "without posting, AND the two draft-autopilot scheduled tasks have been created via queue_setup " +
      "(see SCHEDULE THE AUTOPILOT below). The owned runtime (private Python + Chromium) provisions " +
      "ITSELF automatically on boot — you do NOT install it. Just poll runtime action:'status' until it " +
      "reports ready; only call runtime action:'install' or action:'doctor' to TROUBLESHOOT if status " +
      "shows the boot provision failed or stalled. Do not ask whether to inspect " +
      "status, repair a failed runtime, choose an auto-detected browser profile, connect " +
      "X, scan the profile, research the product website, save conservative inferred fields, seed " +
      "topics, retry a recoverable failure, or run draft-only verification. Do those things. The " +
      "explicit setup request authorizes runtime repair and importing ONLY x.com/" +
      "twitter.com session cookies into the managed browser: warn that macOS keychain prompts may " +
      "appear, then proceed without a separate yes/no turn. Ask at most one bundled question, only " +
      "when no product can be identified from config, context, the X profile, or public research, " +
      "or pause for an unavoidable interactive login or OS prompt. Never auto-POST during setup, but " +
      "DO schedule the draft-only autopilot as the final step (it only queues drafts for approval, it " +
      "never posts on its own). When setup reaches done (" +
      "runtime ready, a project configured with seeded topics, X connected, the draft path verified, " +
      "and the two draft-autopilot scheduled tasks created via queue_setup), call the `dashboard` tool so the user " +
      "sees the finished setup rendered, then give " +
      "the completion summary.\n\n" +
      "SCHEDULE THE AUTOPILOT (final onboarding step). The autopilot now reuses the REAL drafting " +
      "pipeline: a launchd job this server installs kicks the pipeline in draft-only mode, and its " +
      "Claude turns are serviced by TWO scheduled tasks that drain a local job queue. To set them up: " +
      "(1) call the `queue_setup` tool — it returns two task specs (saps-phase1-query and " +
      "saps-phase2b-draft), each with a taskId, cronExpression, and a ready-made prompt; (2) for EACH " +
      "spec, call the HOST tool `create_scheduled_task` with that taskId, cronExpression, and the prompt " +
      "VERBATIM (do not edit it — it contains exact local paths). Both fire every minute and ONLY draft, " +
      "never post; drafts pile up in the menu-bar approval cards. Do NOT create the old " +
      "'social-autoposter-autopilot' task — it is deprecated. If the host exposes no " +
      "create_scheduled_task tool, tell the user the autopilot couldn't be scheduled here; the launchd " +
      "kicker still runs the pipeline, but its draft step will wait on the queue until the tasks exist.\n\n" +
      "BE PROACTIVE ABOUT MARKETING MOMENTS. Whenever the user ships, finishes, merges, or releases " +
      "something worth talking about in this session (a new feature, a launch, a long-awaited fix, a " +
      "milestone), don't wait to be asked: offer to tell the world by running a draft cycle " +
      "(`scan_candidates` -> draft the replies -> `submit_drafts`) about it. A feature nobody hears " +
      "about is wasted distribution. Use judgment, suggest it for " +
      "genuinely shareable moments, not routine refactors, chores, or internal cleanup.\n\n" +
      "STAY CURRENT. At the start of a session, and again right after you help the user ship/merge/" +
      "release, call the `runtime` tool (action:'version'). If `update_available` is true, tell the " +
      "user and offer to run `runtime` with action:'update'. The `project_config` tool's status also " +
      "surfaces `update_available` and an `update_hint`.\n\n" +
      "TYPICAL FLOW: `project_config` (configure OR edit the project, and connect X) -> `scan_candidates` " +
      "(get the top threads to reply to) -> draft the replies yourself -> `submit_drafts` (queue them " +
      "for review; nothing posts until the user approves) -> `post_drafts` (post the approved ones) -> " +
      "`get_stats` (see performance). Run `project_config` first; the other tools refuse until a " +
      "project is fully configured. To change anything about a project later, call `project_config` " +
      "again with the project's name and just the changed fields — there is no separate config editor.\n\n" +
      "RENDER THE DASHBOARD AFTER ACTIONS. After any state-changing or results-producing tool call " +
      "(`scan_candidates`, `submit_drafts`, `post_drafts`, `get_stats`), end your turn by " +
      "calling the `dashboard` tool so the user sees the updated state visually. Do NOT call " +
      "`dashboard` after pure Q&A, config explanations, or status-only checks that changed nothing.",
  }
);

// ---------------------------------------------------------------------------
// Tool dispatch capture.
//
// Every tool's handler is recorded in TOOL_HANDLERS at registration time so the
// localhost dashboard-fallback HTTP server (startLocalPanel) can replay the
// EXACT same handler when the host can't render the ui:// resource inline. This
// is the no-duplication guarantee on the backend: there is one set of handlers,
// reached either through MCP (callServerTool over the host bridge) or through
// the loopback HTTP server (fetch from the same panel.html). The wrappers below
// `tool()` / `appTool()` are drop-in replacements for server.registerTool /
// registerAppTool that additionally stash the callback by name.
// ---------------------------------------------------------------------------
type ToolHandler = (args: any, extra?: any) => Promise<any> | any;
const TOOL_HANDLERS: Record<string, ToolHandler> = {};
const baseRegisterTool = server.registerTool.bind(server);
// `tool` is TYPED as server.registerTool so every call site keeps the exact
// same input-schema -> callback-arg inference it had before; the body is `any`
// and just additionally stashes the callback by name. `appTool` drops the
// leading `server` arg of registerAppTool (its callback takes no typed args).
// Tools that take a while: writing activity.json around them makes the menu bar
// show a spinner + label while they run (either invocation path). draft_cycle is
// NOT here — it writes finer scanning/drafting phases itself (see produceDrafts).
const TOOL_ACTIVITY: Record<string, string> = {
  post_drafts: "posting",
  get_stats: "loading stats",
};
function toolActivityLabel(name: string, args: any): string | null {
  const fallback = TOOL_ACTIVITY[name];
  if (!fallback) return null;
  const override =
    typeof args?.__saps_activity_label === "string"
      ? args.__saps_activity_label.replace(/\s+/g, " ").trim().slice(0, 80)
      : "";
  return override || fallback;
}
function withActivity(name: string, cb: ToolHandler): ToolHandler {
  if (!TOOL_ACTIVITY[name]) return cb;
  return async (args: any, extra: any) => {
    const label = toolActivityLabel(name, args) || TOOL_ACTIVITY[name];
    writeActivity("working", label);
    try {
      return await cb(args, extra);
    } finally {
      clearActivity();
    }
  };
}

const tool: typeof server.registerTool = ((name: string, config: any, cb: ToolHandler) => {
  const h = withActivity(name, cb);
  TOOL_HANDLERS[name] = h;
  return (baseRegisterTool as any)(name, config, h);
}) as any;
const appTool = ((name: string, config: any, cb: ToolHandler) => {
  // Wrap every tool handler so any thrown error is reported to Sentry. Single
  // chokepoint for both the MCP SDK path and the local HTTP-panel path (both
  // dispatch through TOOL_HANDLERS / registerAppTool). Re-throws so the caller
  // still formats the error response exactly as before.
  const wrapped = (async (args: any, extra: any) => {
    try {
      return await cb(args, extra);
    } catch (e) {
      captureError(e, { tool: name });
      throw e;
    }
  }) as ToolHandler;
  const h = withActivity(name, wrapped);
  TOOL_HANDLERS[name] = h;
  return registerAppTool(server as any, name, config as any, h as any);
}) as any;

function jsonContent(obj: unknown) {
  return { content: [{ type: "text" as const, text: JSON.stringify(obj, null, 2) }] };
}
function textContent(text: string) {
  return { content: [{ type: "text" as const, text }] };
}

// ---------------------------------------------------------------------------
// Draft production (scan + score + draft -> plan JSON).
//
// The drafting orchestration lives in the locked run-twitter-cycle.sh, which
// today runs scan->score->draft->POST straight through with NO draft-only stop.
// Until that gate exists, produceDrafts reports the decision instead of guessing
// or posting unreviewed. Everything downstream of this (review + post) is real.
// ---------------------------------------------------------------------------
interface DraftResult {
  batchId: string | null;
  blocked?: string;
}

// Map a pipeline failure-reason key (from scripts/classify_run_error.py, emitted
// by run-twitter-cycle.sh as `DRAFT_ONLY_BLOCKED=<reason>`) to a clear,
// actionable message. The most common one on a fresh machine is
// claude_not_logged_in: the background `claude` CLI the pipeline shells out to
// has its OWN login, separate from Claude Desktop, so it can be logged out even
// though this MCP host is signed in. Without this, an auth failure was silently
// reported as a benign empty cycle ("all threads already engaged").
function blockedReasonMessage(reason: string): string {
  switch (reason) {
    case "claude_not_logged_in":
      return (
        "The background Claude CLI on this machine isn't logged in, so the drafting step " +
        "couldn't run. (It DID find and rank threads, it just couldn't draft replies.) This " +
        "CLI uses its own login, separate from Claude Desktop. To fix it, open a terminal and run:\n\n" +
        "    claude\n\n" +
        "then `/login` inside it (or run `claude setup-token`). Once it's logged in, run scan_candidates again."
      );
    case "monthly_limit":
    case "daily_limit":
    case "rate_limit_5h":
      return (
        `The drafting step hit an Anthropic usage limit (${reason}), so no replies were drafted. ` +
        "Wait for the limit to reset, then run scan_candidates again."
      );
    case "no_search_topics":
      return (
        "This project has no search topics yet, so there was nothing to scan. Topics live in the " +
        "DB (project_search_topics) and are seeded from your project's `search_topics` when you " +
        "configure it. Re-run the `project_config` tool for this project with a `search_topics` list " +
        "(comma-separated keywords/phrases your buyers tweet about); it seeds them automatically, then " +
        "run scan_candidates again."
      );
    case "topics_api_unreachable":
      return (
        "Couldn't reach the search-topics service to load this project's topics, so the cycle stopped " +
        "before scanning. This is usually a transient backend/network issue. Try scan_candidates again in a " +
        "moment; if it persists, check connectivity to the autoposter backend."
      );
    case "credit_balance":
      return (
        "The drafting step failed because the Anthropic account is out of credits. " +
        "Add credits, then run scan_candidates again."
      );
    default:
      return (
        `The drafting step failed (${reason}) and produced no drafts. ` +
        "Check skill/logs/twitter-cycle-*.log on this machine for details, then run scan_candidates again."
      );
  }
}

// Turn a raw run-twitter-cycle.sh stdout line into a short, user-facing
// progress message — or null when the line isn't a milestone worth surfacing.
// The cycle script logs every phase via `log()` (tee'd to stdout), so we can
// follow along live instead of going dark for the minutes Phase 2b-prep takes.
// Keep this list tight: only lines a *user* benefits from seeing, phrased for
// someone who has no idea what "phase2a" means.
function cycleProgressMessage(line: string): string | null {
  const l = line.trim();
  let m: RegExpExecArray | null;
  if (/=== Twitter Cycle \(batch=/.test(l)) return "Starting draft cycle…";
  // NB: lines carry a `[HH:MM:SS] ` timestamp prefix, so don't anchor on ^.
  if ((m = /Selected projects?:\s*(.+)$/.exec(l))) return `Selected project: ${m[1]}`;
  if (/phase=phase1\b/.test(l) || /Phase 1: drafting queries/.test(l))
    return "Searching X for fresh threads…";
  if ((m = /Phase 1 complete.*?has (\d+) candidates?/.exec(l)))
    return `Found ${m[1]} candidate thread${m[1] === "1" ? "" : "s"} — ranking them…`;
  if (/phase=phase2a\b/.test(l) || /candidates by virality_score selected/.test(l))
    return "Scoring and ranking candidates…";
  if (/Phase 2b-prep: Claude reading threads and drafting replies/.test(l))
    return "Drafting replies (the long step — this can take a few minutes)…";
  if ((m = /Engagement style assigned:.*?style=(\S+)/.exec(l)))
    return `Drafting in style: ${m[1]}…`;
  if (/DRAFT_ONLY_PLAN=/.test(l)) return "Drafts ready — assembling the review table…";
  if ((m = /DRAFT_ONLY_BLOCKED=([a-z0-9_]+)/.exec(l))) return `Cycle stopped (${m[1]}).`;
  return null;
}

// Start the twitter-harness on-screen overlay watcher if it isn't already up.
// The overlay (status banner) only renders WHILE `harness_overlay.py watch`
// runs. The supervisor script is idempotent (pgrep
// guard), so calling this on every draft_cycle / autopilot-enable / show-browser
// is safe: it spawns at most one detached watcher and is a fast no-op otherwise.
//
// We thread SAPS_PYTHON (the owned uv runtime, so the watcher resolves a
// playwright-capable interpreter on Lane B / .mcpb installs that have no system
// python) and SAPS_LOG_DIR (the materialized repo's skill/logs, so the watcher
// reads the SAME cycle logs this run writes to decide busy/idle). Fire-and-forget:
// a failure here must never break the cycle it's decorating.
async function ensureOverlayWatch(): Promise<void> {
  try {
    await run("bash", ["skill/run-overlay-watch.sh"], {
      timeoutMs: 20_000,
      env: {
        SAPS_PYTHON: resolvePython(),
        SAPS_LOG_DIR: path.join(repoDir(), "skill", "logs"),
        TWITTER_CDP_URL: process.env.TWITTER_CDP_URL || "http://127.0.0.1:9555",
      },
    });
  } catch {
    /* best-effort: the overlay is a nicety, never a blocker */
  }
}

async function produceDrafts(
  project?: string,
  onProgress?: (message: string, step: number) => void
): Promise<DraftResult> {
  // Run the real pipeline in DRAFT_ONLY mode: scan -> score -> draft -> link-gen,
  // then STOP before posting. The script prints `DRAFT_ONLY_PLAN=<path>` and
  // leaves the plan on disk for us to review + post. SAPS_FORCE_PROJECT scopes
  // the cycle to one project; TWITTER_PAGE_GEN_RATE=0 keeps link-gen sub-second.
  const env: NodeJS.ProcessEnv = {
    DRAFT_ONLY: "1",
    TWITTER_PAGE_GEN_RATE: "0",
    // Point the cycle at the resolved repo (a bare .mcpb materializes it under
    // the state dir, NOT ~/social-autoposter); run-twitter-cycle.sh honors
    // SAPS_REPO_DIR for its REPO_DIR. And put the owned runtime + ~/.local/bin
    // first on PATH so the script's bare `python3` and `browser-harness` resolve.
    SAPS_REPO_DIR: repoDir(),
    PATH: pipelinePath(),
    // Interactive draft_cycle: launch the harness Chrome ON-SCREEN so the user
    // can watch the scan/scrape happen live. Cron/autopilot do NOT set these, so
    // background runs keep the off-screen default in twitter-backend.sh and don't
    // hijack the screen. (Only affects a fresh Chrome launch; an already-running
    // harness window keeps its current position.)
    BH_WINDOW_POS: "60,60",
    BH_WINDOW_SIZE: "1280,900",
  };
  if (project) env.SAPS_FORCE_PROJECT = project;
  // Point the harness at the Chrome the runtime detected/installed. The cycle's
  // own _resolve_chrome_bin doesn't scan ~/Applications (our no-sudo fallback
  // install target), so without this a non-admin .mcpb install would have Chrome
  // on disk yet still report "no Chrome/Chromium binary found." Only set when
  // resolved; otherwise let the shell resolve Chrome from its own probe list.
  const chrome = resolveChrome();
  if (chrome) env.BH_CHROME_BIN = chrome;
  // Bring the on-screen overlay up alongside the live harness window so the user
  // watching the scan/scrape sees status + queued drafts. Idempotent + detached.
  await ensureOverlayWatch();
  let step = 0;
  let lastMsg = "";
  // ONE predictable, host-independent place to watch a draft_cycle run, so any
  // agent (or human) debugging "the cycle looks stuck" has an obvious path:
  //   ~/social-autoposter/skill/logs/draft_cycle-mcp.log
  // It lives right next to the cycle's own twitter-cycle-*.log. We append the
  // full live cycle output here (not just milestones) plus a clear run banner.
  // Best-effort: a logging failure must never break the cycle.
  const mcpLog = path.join(repoDir(), "skill", "logs", "draft_cycle-mcp.log");
  const appendLog = (s: string) => {
    try {
      fs.appendFileSync(mcpLog, s);
    } catch {
      /* ignore — never fail the cycle over a log write */
    }
  };
  try {
    fs.mkdirSync(path.dirname(mcpLog), { recursive: true });
  } catch {
    /* ignore */
  }
  appendLog(
    `\n===== draft_cycle start ${new Date().toISOString()} ` +
      `project=${project ?? "(default)"} =====\n`
  );
  // Menu-bar status: scanning first, then drafting once the prep phase begins
  // (switched in onLine below). Cleared before every return.
  writeActivity("scanning", "scanning X");
  const res = await run("bash", ["skill/run-twitter-cycle.sh"], {
    env,
    timeoutMs: 900_000, // scan+draft can take several minutes
    // Fan every cycle line out to THREE sinks so progress is never a black box:
    //   1. draft_cycle-mcp.log  — the stable, documented, host-independent file.
    //   2. this server's stderr — lands in the host's MCP server log
    //      (mcp-server-social-autoposter.log on Desktop), which used to show
    //      only the JSON-RPC handshake.
    //   3. the live progress sink — milestone messages under the chat spinner.
    onLine: (line) => {
      const t = line.replace(/\s+$/, "");
      if (t.trim()) {
        appendLog(`${t}\n`);
        console.error(`[draft_cycle] ${t}`);
      }
      if (/Phase 2b-prep/.test(t)) writeActivity("drafting", "drafting replies");
      if (!onProgress) return;
      const msg = cycleProgressMessage(t);
      // Skip consecutive duplicates (a phase can log a couple matching lines).
      if (msg && msg !== lastMsg) {
        lastMsg = msg;
        onProgress(msg, ++step);
      }
    },
  });
  appendLog(
    `===== draft_cycle end ${new Date().toISOString()} exit=${res.code} =====\n`
  );
  // Prefer the explicit marker; fall back to the newest plan file on disk.
  const marker = /DRAFT_ONLY_PLAN=\/tmp\/twitter_cycle_plan_(.+)\.json/.exec(
    res.stdout + "\n" + res.stderr
  );
  if (marker && marker[1]) { clearActivity(); return { batchId: marker[1] }; }
  // A real prep-step failure (e.g. the background claude CLI isn't logged in)
  // emits DRAFT_ONLY_BLOCKED=<reason>. Surface that instead of silently falling
  // back to a stale/empty batch and mis-reporting "no fresh candidates".
  const blockedMarker = /DRAFT_ONLY_BLOCKED=([a-z0-9_]+)/.exec(
    res.stdout + "\n" + res.stderr
  );
  if (blockedMarker && blockedMarker[1]) {
    clearActivity();
    return { batchId: null, blocked: blockedReasonMessage(blockedMarker[1]) };
  }
  // No `DRAFT_ONLY_PLAN=` marker from THIS run => this run produced no drafts.
  // We MUST NOT fall back to the newest plan file on disk (`latestBatchId()`):
  // that's a *previous* run's batch, so a 5-second empty cycle would echo an old
  // 7-draft batch and report phantom success. Report 0 drafts honestly, with the
  // pipeline's own reason (e.g. cold-start project with no seeded queries).
  clearActivity();
  return {
    batchId: null,
    blocked:
      `This run produced no drafts (exit ${res.code}). The scan found no fresh ` +
      `candidates for the selected project — usually a cold-start project with ` +
      `no seeded search queries/topics, or a pipeline error. This is NOT a ` +
      `previous batch. Tail:\n` +
      res.stderr.split("\n").slice(-12).join("\n"),
  };
}

// Render every draft in a batch as a numbered, human-readable table. This IS the
// review surface now: the model relays this table to the user and asks which
// numbers to post / edit, then posts the chosen ones via the `post_drafts` tool.
//
// We used to gather approvals through MCP elicitation (a checkbox form), but the
// desktop "Code tab" host doesn't advertise the `elicitation` capability (only
// `io.modelcontextprotocol/ui`), so the form never rendered and cycles silently
// posted nothing. Approval is conversational instead — numbers in chat.
function renderDraftsTable(plan: Plan): string {
  const candidates = plan.candidates || [];
  return candidates
    // Number by FULL-array index (matches post_drafts + the menu bar), then drop
    // already-finished entries so the cards only show what's still pending.
    .map((c, i) => ({ c, n: i + 1 }))
    .filter((e) => e.c.posted !== true && e.c.terminal !== true && e.c.approved !== true)
    // The queue is append-only; newest drafts have the highest stable index.
    // Show those first so review starts with likely-live tweets instead of stale
    // low-number drafts that have been sitting around for hours.
    .sort((a, b) => b.n - a.n)
    .map(({ c, n }) => {
      const author = c.thread_author ? `@${c.thread_author}` : "(unknown thread)";
      const style = c.engagement_style ?? "?";
      const reply = c.reply_text ?? "(empty)";
      // The literal tail URL is NOT known yet: at post time a short link is minted
      // from this target (e.g. fazm.ai/cc -> s4l.ai/r/<code>). Approved drafts
      // always carry the link (post_drafts forces TWITTER_TAIL_LINK_RATE=1.0), so
      // this is the target that WILL be appended. Show the TARGET only; never
      // pre-mint the real /r/ code (that would waste pool codes / split clicks).
      const link = c.link_url
        ? `\n    + link (appended as a short link at post time): ${c.link_url}`
        : "";
      // The original tweet we're replying to — context the reviewer needs to judge
      // the draft. Already in the plan; just surface it.
      const threadText = c.thread_text
        ? `\n    in reply to: ${c.thread_text.replace(/\s+/g, " ").trim().slice(0, 280)}`
        : "";
      return (
        `[${n}] ${author}  (style: ${style})` +
        `${threadText}\n` +
        `    draft: ${reply.replace(/\n/g, "\n    ")}` +
        `${link}\n` +
        `    thread url: ${c.candidate_url ?? "?"}`
      );
    })
    .join("\n\n");
}

interface PostCandidateResult {
  candidate_id: string;
  outcome: "posted" | "skipped" | "failed";
  reason?: string;
  our_url?: string;
}

function parsePostCandidateResults(stdout: string): PostCandidateResult[] {
  const byId = new Map<string, PostCandidateResult>();
  const upsert = (
    candidateId: string,
    outcome: PostCandidateResult["outcome"],
    reason?: string,
    ourUrl?: string
  ) => {
    const prev = byId.get(candidateId);
    // A landed post wins over any earlier noisy line for the same candidate.
    if (prev?.outcome === "posted" && outcome !== "posted") return;
    byId.set(candidateId, {
      candidate_id: candidateId,
      outcome,
      ...(reason ? { reason } : {}),
      ...(ourUrl ? { our_url: ourUrl } : {}),
    });
  };

  for (const line of stdout.split("\n")) {
    let m = /\[post\] candidate (\d+) posted as (\S+) \(post_id=/.exec(line);
    if (m) {
      upsert(m[1], "posted", undefined, m[2]);
      continue;
    }
    m = /\[post\] candidate (\d+): pre-post dedup hit\b/.exec(line);
    if (m) {
      upsert(m[1], "skipped", "duplicate_thread_pre_post");
      continue;
    }
    m = /\[post\] candidate (\d+) reply failed: ([A-Za-z0-9_:-]+)/.exec(line);
    if (m) {
      upsert(m[1], "skipped", m[2]);
      continue;
    }
    m = /\[post\] candidate (\d+) reply succeeded but reply_url invalid:/.exec(line);
    if (m) {
      upsert(m[1], "skipped", "no_reply_url_captured");
      continue;
    }
    m = /\[post\] candidate (\d+): empty reply_text; skipping/.exec(line);
    if (m) {
      upsert(m[1], "skipped", "empty_reply_text");
      continue;
    }
    m = /\[post\] candidate (\d+) crashed:/.exec(line);
    if (m) upsert(m[1], "failed", "exception");
  }
  return [...byId.values()];
}

// Resolve the configured posting handle the SAME way account_resolver.py does:
// AUTOPOSTER_TWITTER_HANDLE env first, then config.json accounts.twitter.handle.
// Returns the bare handle (no @) or null. The post preflight uses it so a missing
// handle fails ONCE, loudly, instead of as N silent per-reply no_account_configured
// skips (twitter_browser.py refuses to post with no handle — no impersonation).
function readConfiguredTwitterHandle(): string | null {
  const env = (process.env.AUTOPOSTER_TWITTER_HANDLE || "").trim().replace(/^@/, "");
  if (env) return env;
  try {
    const cfg = JSON.parse(fs.readFileSync(path.join(repoDir(), "config.json"), "utf-8"));
    const h = cfg?.accounts?.twitter?.handle;
    const s = (typeof h === "string" ? h : "").trim().replace(/^@/, "");
    return s || null;
  } catch {
    return null;
  }
}

// Self-heal a missing handle: read the live logged-in @handle from the managed
// Chrome and persist it to config.json accounts.twitter.handle. This is ground
// truth (the poster posts through that exact session), NOT a guess — so it's safe
// where a hardcoded fallback would not be. Closes the onboarding gap where
// connect_x's best-effort handle capture silently no-op'd and left posting dead.
// Best-effort; never throws — the caller re-checks and refuses loudly if still unset.
async function ensurePostingHandle(): Promise<void> {
  try {
    await runPython("scripts/setup_twitter_auth.py", ["resolve-handle"], {
      timeoutMs: 60_000,
      env: { SAPS_REPO_DIR: repoDir(), PATH: pipelinePath() },
    });
  } catch {
    /* best effort */
  }
}

async function postApproved(batchId: string, plan: Plan) {
  // Post every card the user APPROVED that hasn't already landed or been ruled out.
  // `approved` is now a DURABLE decision (sticky, never cleared by a later call), so
  // filtering out posted/terminal here makes this idempotent: re-running it only
  // drains the not-yet-posted approved backlog (e.g. a card a restart interrupted),
  // never re-posts a done one. This is what lets the startup backlog-drain and the
  // per-card menu-bar calls share one code path safely.
  const approved = (plan.candidates || []).filter(
    (c: PlanCandidate) => c.approved === true && c.posted !== true && c.terminal !== true
  );
  if (approved.length === 0) return { attempted: 0, exit_code: 0, summary: "nothing approved" };
  // PREFLIGHT: posting needs a configured @handle, or twitter_browser.py refuses
  // EVERY reply with no_account_configured and the whole batch skips — invisibly.
  // If onboarding never persisted it, self-heal from the live session; if even that
  // can't determine it, refuse here with a clear reason rather than launching a
  // poster that silently burns the whole batch.
  if (!readConfiguredTwitterHandle()) await ensurePostingHandle();
  if (!readConfiguredTwitterHandle()) {
    return {
      attempted: 0,
      exit_code: 0,
      posted: 0,
      summary: "no_account_configured",
      error:
        "X is connected but no posting @handle is configured, so every reply would be refused " +
        "(no_account_configured). Re-run project_config action:'connect_x' to capture the handle, " +
        "or set accounts.twitter.handle in config.json.",
    };
  }
  // Mark posting active so scan_candidates DEFERS launching any scan for the
  // duration of this batch (+ grace). This is the source-level mutual exclusion
  // that actually fixes the hijack: the autopilot never launches a scan to race
  // the post for the browser. Reset is guaranteed by scheduleShellLockRelease()
  // in the finally below, so an early/failed post can't wedge scanning.
  postingActive = true;
  startPostingFlagHeartbeat(); // cross-instance: a sibling MCP's scan defers too
  // Posting is a priority over scanning: abort any in-flight plugin scan so the
  // approved post takes the browser immediately instead of waiting on the lock.
  // Plugin pipeline only — never affects the plist autopilot.
  preemptScanForPost();
  // Hold the /tmp shell browser lock (the one the scanner respects) for the WHOLE
  // batch so the every-minute autopilot scan queues behind the post instead of
  // seizing Chrome mid-batch — the root cause of approved batches landing 0/N.
  const heldShellLock = await acquireShellBrowserLock();
  const approvedBatch = `${batchId}_approved`;
  writePlan(approvedBatch, { ...plan, candidates: approved });
  // SAPS_SKIP_CAMPAIGN_SUFFIX=1: manual/reviewed posts from this MCP draft_cycle
  // never get the active-campaign suffix (e.g. " written with ai") appended.
  // twitter_browser.py's reply handler reads this env (inherited through
  // twitter_post_plan.py's subprocess). The cron pipeline doesn't set it, so the
  // A/B disclosure experiment keeps running on autopilot/cron and on Reddit.
  const res = await (async () => {
    try {
      return await runPython(
        "scripts/twitter_post_plan.py",
        ["--plan", planPath(approvedBatch)],
        {
          timeoutMs: 900_000,
          env: {
            SAPS_SKIP_CAMPAIGN_SUFFIX: "1",
            // Manual approval is an EXCEPTION to the tail-link A/B. The cron pipeline
            // runs TWITTER_TAIL_LINK_RATE=0.9 (from .env) so ~10% of autopilot posts
            // ship link-less as an experiment arm. But when the user hand-reviews a
            // draft, sees the link target in the table, and approves it, dropping the
            // link is surprising and unwanted. Force 1.0 here so every approved draft
            // carries its link. This wins over .env / process.env because run() spreads
            // opts.env AFTER process.env, and twitter_post_plan.py never load_dotenv's
            // with override, so nothing clobbers it. Cron is untouched (it never goes
            // through this MCP path), so the 0.9 experiment keeps running there.
            TWITTER_TAIL_LINK_RATE: "1.0",
            // Plugin flow only: skip the link_tail Claude call. It just rewords
            // prose around the URL (the minted short link comes from the
            // deterministic wrap step), and on .mcpb boxes there's no `claude`
            // binary so it wastes ~35s/post of run_claude.sh retry backoff before
            // falling back to the mechanical concat anyway. link_tail.py honors
            // this and short-circuits to that concat instantly. The local
            // cron/plist autopilot never sets this, so it keeps generating the
            // bridge sentence.
            SAPS_SKIP_LINK_TAIL: "1",
            // The poster attaches to the twitter-harness Chrome over CDP. The cron
            // pipeline exports this from skill/lib/twitter-backend.sh; the MCP path
            // must set it explicitly or twitter_browser.py fails with "No twitter-
            // harness Chrome reachable". Honor an inherited value (AppMaker / VM
            // BYO-Chrome), else default to the local harness on port 9555.
            TWITTER_CDP_URL: process.env.TWITTER_CDP_URL || "http://127.0.0.1:9555",
          },
          // Stream the poster's output live (like scan_candidates does) so HANDLED
          // failures — e.g. every reply refused with no_account_configured, which
          // returns a reason instead of throwing — surface in main.log + telemetry
          // in real time. Without this the poster's stdout was buffered in-process
          // and only flushed to post-*.log at the END, so a 0/N batch was invisible
          // while the menu bar showed "posting N/89" climbing.
          onLine: (line: string) => {
            const t = line.replace(/\s+$/, "");
            if (t.trim()) console.error(`[post] ${t}`);
          },
        }
      );
    } finally {
      // Always schedule the grace release (even if the lock acquire failed): the
      // timer both frees the lock AND clears postingActive, so scanning resumes
      // SHELL_LOCK_GRACE_MS after the last card. Holding through the grace lets the
      // NEXT approved card reuse one continuous hold (mirrors the plist holding the
      // lock through the whole posting phase, then releasing at the end).
      scheduleShellLockRelease();
    }
  })();
  // Persist the poster's own stdout/stderr to a dated log. Without this the post
  // run was invisible: twitter_post_plan.py's output streamed to this MCP
  // instance's stderr and was never tee'd anywhere on disk, so a 0/N batch left
  // no on-box trace to debug. Best-effort; never breaks posting.
  try {
    const postLogDir = path.join(repoDir(), "skill", "logs");
    fs.mkdirSync(postLogDir, { recursive: true });
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    fs.writeFileSync(
      path.join(postLogDir, `post-${stamp}.log`),
      `# post_drafts batch=${batchId} approved=${approved.length} exit=${res.code} ` +
        `shell_lock=${heldShellLock}\n\n=== stdout ===\n${res.stdout}\n\n=== stderr ===\n${res.stderr}\n`
    );
  } catch {
    /* best effort */
  }
  let summary: unknown = res.stdout.trim();
  try {
    const lines = res.stdout.trim().split("\n");
    summary = JSON.parse(lines[lines.length - 1]);
  } catch {
    /* keep raw */
  }
  // Real posted count from the pipeline summary — NOT the approved count. A run
  // can exit 0 yet post nothing (every reply hit reply_box_not_found, etc.), so
  // trusting approved.length here reported phantom successes ("posted: 1" when 0
  // landed). Fall back to approved.length only when the summary is unparseable
  // AND the process exited clean.
  const summObj = (summary && typeof summary === "object") ? (summary as Record<string, unknown>) : null;
  const realPosted: number =
    summObj && typeof summObj.posted === "number"
      ? (summObj.posted as number)
      : res.code === 0 && !summObj
        ? approved.length
        : 0;
  // Mark candidates according to the poster's per-candidate outcome. This keeps
  // the review queue honest: posted drafts disappear as posted, terminal skips
  // (dedup, deleted tweet, no captured URL) disappear without being counted as
  // posted, and multi-approval batches no longer smear one posted count across
  // every approved draft.
  const resultRowsFromSummary = Array.isArray(summObj?.candidate_results)
    ? (summObj?.candidate_results as Array<Record<string, unknown>>)
    : [];
  const resultRows: PostCandidateResult[] = resultRowsFromSummary.length
    ? resultRowsFromSummary
        .map((r) => ({
          candidate_id: String(r.candidate_id ?? ""),
          outcome: String(r.outcome || "") as PostCandidateResult["outcome"],
          reason: typeof r.reason === "string" ? r.reason : undefined,
          our_url: typeof r.our_url === "string" ? r.our_url : undefined,
        }))
        .filter((r) => r.candidate_id && ["posted", "skipped", "failed"].includes(r.outcome))
    : parsePostCandidateResults(res.stdout);
  const approvedById = new Map<string, PlanCandidate>();
  approved.forEach((c) => {
    if (c.candidate_id !== undefined && c.candidate_id !== null)
      approvedById.set(String(c.candidate_id), c);
  });
  let touchedPlan = false;
  if (resultRows.length) {
    resultRows.forEach((r, idx) => {
      const c = approvedById.get(r.candidate_id) || approved[idx];
      if (!c) return;
      if (r.outcome === "posted") {
        c.posted = true;
        c.terminal = false;
        if (r.our_url) c.our_url = r.our_url;
        touchedPlan = true;
      } else if (r.outcome === "skipped" || r.outcome === "failed") {
        c.terminal = true;
        c.terminal_reason = r.reason || r.outcome;
        touchedPlan = true;
      }
    });
  } else if (realPosted > 0 || (res.code === 0 && !summObj)) {
    // Legacy fallback for older poster output without parseable per-candidate
    // lines. Mark only when we have no finer-grained signal.
    for (const c of approved) c.posted = true;
    touchedPlan = true;
  }
  if (touchedPlan) {
    try {
      writePlan(batchId, plan);
    } catch {
      /* best effort */
    }
  }
  // Post failures are HANDLED in the pipeline (it returns a count, never throws),
  // so they never reach Sentry on their own. Capture an explicit event whenever
  // the run exited non-zero OR fewer drafts posted than were approved. This is
  // the only telemetry channel that reaches a customer .mcpb install (their cycle
  // log lives on their machine). install_id/hostname are auto-tagged.
  if (res.code !== 0 || realPosted < approved.length) {
    captureError(
      new Error(`post_drafts: ${realPosted}/${approved.length} posted (exit=${res.code})`),
      {
        component: "post",
        exit_code: String(res.code),
        attempted: String(approved.length),
        posted: String(realPosted),
        failure_reasons: String((summObj?.failure_reasons as string) || ""),
        skip_reasons: String((summObj?.skip_reasons as string) || ""),
        stderr_tail: res.stderr.split("\n").slice(-5).join(" | ").slice(0, 500),
      }
    );
    void flushSentry(2000);
  }
  void flushLogs();
  return {
    attempted: approved.length,
    posted: realPosted,
    exit_code: res.code,
    summary,
    stderr_tail: res.stderr.split("\n").slice(-8).join("\n"),
  };
}

// ---- getting-started: discoverable front door (USER-invoked, no side effects)
// This is NOT a tool — the model never auto-calls it. It surfaces in clients
// that render prompts as slash-commands / starters (e.g. Claude Desktop's "/"
// menu). When the user picks it, it injects the message below into the chat,
// which nudges the agent to start the real onboarding via the `project_config` tool.
// Deliberately a DUMB POINTER: it names no fields and no steps, so it can never
// drift from REQUIRED_FIELDS / the project_config tool's flow. All real logic stays
// in `project_config`; this is just a convenience handle to begin.
server.registerPrompt(
  "getting-started",
  {
    title: "Set up S4L",
    description:
      "Start here. Walks you through configuring a product and connecting your X/Twitter " +
      "account so the autoposter can draft and post for you.",
  },
  async () => ({
    messages: [
      {
        role: "user" as const,
        content: {
          type: "text" as const,
          text:
            "Set up social-autoposter end to end now. Treat this as a terminal goal: inspect status, " +
            "install or repair the owned runtime, auto-detect and connect my X session, scan my " +
            "profile, discover and research the product I most clearly represent, infer and save a " +
            "conservative complete project with search topics, seed them, and run a draft-only " +
            "verification. Keep going without asking me to approve each safe setup step. A brief " +
            "heads-up before macOS keychain prompts is enough; proceed immediately. Ask only if an " +
            "interactive login is unavoidable or no product can be identified from config, context, " +
            "my X profile, or public research. Do not post or enable autopilot unless I explicitly ask. " +
            "Keep every reply to me extremely concise: a few short sentences at most, no step-by-step " +
            "narration or long status walls. If you must ask me something (e.g. the product URL), make " +
            "it one short question.",
        },
      },
    ],
  })
);

// Instruction (NOT a script) the agent follows to research the product website
// after the profile scan. The agent uses ITS OWN browser/fetch tools — the MCP
// ships no scraper. The goal is to fill the PRODUCT half of the config (what it
// does, how it's different, who it's for, the CTA link, claims to avoid) from the
// site itself, written in the user's voice captured by the profile scan.
const WEBSITE_RESEARCH_INSTRUCTIONS =
  "PRODUCT RESEARCH (do this before saving the product fields):\n" +
  "1. Discover the product URL from existing config, the conversation, the connected X profile " +
  "(bio, links, and recent posts), or public research. Use the clearest supported product without " +
  "asking. Ask one blocking question only if no defensible product can be identified.\n" +
  "2. Visit it with your OWN browser/fetch tools (no scraper is provided) and read " +
  "AT LEAST 5 pages if the site has them — follow the internal nav/footer links. " +
  "Prioritize: homepage, pricing, features/product, about, docs or changelog or blog, " +
  "FAQ, customers/testimonials/case-studies. Read as many as you can find (5+ is the " +
  "floor, not the cap) to learn the product deeply.\n" +
  "3. From what you actually read, extract the PRODUCT fields: `description` (what it " +
  "does, concretely), `differentiator` (how it's genuinely different from alternatives), " +
  "`icp` (who it's for — cross-check against who the user engages with on X), " +
  "`get_started_link` (the primary signup/CTA URL), and `content_guardrails` (claims, " +
  "competitors, or wording the site avoids — never overclaim beyond the site).\n" +
  "4. WRITE these fields in the USER'S voice from the profile scan (their phrasing, " +
  "register, vibe) while keeping every product CLAIM factual to the site. Don't invent " +
  "features, metrics, or guarantees the site doesn't state.\n" +
  "5. Save the best conservative factual draft without adding a confirmation round-trip. Call " +
  "project_config with name + the product fields (plus voice/search_topics from the profile scan), AND " +
  "expand those topics into a `search_queries` array of ~30 concrete X advanced-search strings in the " +
  "SAME call — YOU are the model, so do the expansion in-session; it seeds directly with no `claude -p`. " +
  "If the site is thin or unreachable, use only supported facts and leave optional detail conservative; " +
  "ask the user only if a required field is genuinely unknowable.";

// ---- project_config: per-project config (the "brain": project, website, voice) -----
// Run this FIRST. The action tools refuse until at least one project is ready.
// You can set up MULTIPLE products and fill each project's fields INCREMENTALLY
// across several calls — readiness is derived from config.json, never a stored
// flag. Call with status:true (or just no name) to list every project this
// install manages and what each still needs.
tool(
  "project_config",
  {
    title: "Configure or edit a project",
    description:
      "The ONE tool for a project's whole lifecycle: create it, EDIT it later, and connect its X " +
      "account. There is no separate raw-config editor — every project change goes through here so " +
      "it validates, merges, and re-seeds the search-topic universe the cycle reads. To CHANGE an " +
      "existing project (its website, voice, icp, differentiator, search_topics, guardrails, CTA " +
      "link), call this with that project's `name` and ONLY the fields you want to change; it merges " +
      "onto what's already saved and never clobbers untouched fields. Run it FIRST before any " +
      "drafting or autopilot. A user's request to set up social-autoposter is a request to finish " +
      "the workflow end to end, not to interview them step by step: resume from current status, " +
      "infer discoverable fields, and keep taking safe actions until runtime, project, X connection, " +
      "topic seeding, and draft-only verification are complete.\n" +
      "Two jobs:\n" +
      "1) Configure (or edit) a project this install posts for: its website, what it does " +
      "(description), who to target (icp), and brand voice. To fill the PRODUCT fields, discover the " +
      "product URL from config, conversation context, the connected X profile, or public research, " +
      "then visit it with your own browser/fetch tools — read 5+ pages (home, pricing, features, " +
      "about, docs/blog, FAQ) to learn it deeply, rather than guessing from the name. Set up MULTIPLE " +
      "products (call once per product, identified by name); fill or edit a project's fields " +
      "INCREMENTALLY across several calls — pass whatever you have, it merges and tells you what's " +
      "still missing.\n" +
      "2) Connect X/Twitter (action:'connect_x'): the autoposter posts through its OWN managed Chrome, " +
      "which needs your logged-in x.com session. This imports x.com/twitter.com cookies from your " +
      "everyday browser (Chrome/Arc/Brave/Edge, auto-detected) into that browser — nothing else is " +
      "touched. An explicit setup/connect request is authorization: briefly warn that macOS Safe " +
      "Storage prompts may appear, then call action:'connect_x', confirm:true immediately. Use " +
      "action:'detect_x_sources' first and choose its recommendation instead of asking the user.\n" +
      "Call with status:true (or no name) to list every configured project, its remaining fields, AND " +
      "whether X is connected. Use config, conversation context, profile_scan, and website research " +
      "before asking for fields. Ask only if no product can be identified or an interactive login is " +
      "unavoidable. The scan_candidates and get_stats tools refuse to run until a project is " +
      "fully set up.",
    inputSchema: {
      status: z.boolean().optional(),
      action: z
        .enum(["connect_x", "detect_x_sources", "profile_scan"])
        .optional()
        .describe(
          "connect_x = import/validate your X session in the autoposter's managed browser. " +
            "With an explicit setup/connect request, warn about possible keychain prompts and call " +
            "with confirm:true without waiting for another yes/no reply. Without confirm:true it " +
            "only previews the operation for users who asked to inspect it rather than run it. " +
            "detect_x_sources = list the browsers/profiles the X session can be imported from " +
            "(read-only, no keychain prompt) so the user can pick the right one; returns " +
            "{sources:[{spec,label,x_session}], recommended}. " +
            "profile_scan = AFTER connect_x, read the connected account's bio + recent posts + recent " +
            "replies to build a 'grounding truth' corpus. Use it to draft voice/icp/search_topics in " +
            "the USER'S OWN register (their phrases, vibe, profession), then save a conservative best " +
            "draft without requiring a confirmation round-trip. Returns {profile, posts, comments, " +
            "grounding_instructions}."
        ),
      confirm: z
        .boolean()
        .optional()
        .describe("Set true to run the import. An explicit setup/connect request counts as authorization."),
      x_source: z
        .string()
        .optional()
        .describe(
          "Optional browser profile to import the X session from, e.g. 'arc:Default', 'chrome:Profile 1'. " +
            "Default: auto-detect chrome/arc/brave/edge."
        ),
      x_manual_login: z
        .boolean()
        .optional()
        .describe(
          "Set true ONLY when the user explicitly wants to sign into X by hand. It opens a focused " +
            "X login window and waits for them to log in. By default (false), connect_x does NOT pop a " +
            "browser window on an auto-import miss; it returns needs_login and you offer manual login as " +
            "an opt-in. The login window still opens automatically if the user DENIED the keychain prompt."
        ),
      name: z
        .string()
        .optional()
        .describe("Short machine slug for the project, e.g. 'nicia' (lowercase, no spaces). The key that identifies which project to create/update."),
      website: z.string().optional().describe("The product's website URL"),
      description: z.string().optional().describe("What the product does, 1-3 sentences"),
      icp: z
        .string()
        .optional()
        .describe("Ideal customer / target audience to engage on X"),
      voice: z.string().optional().describe("Brand voice / tone for the replies"),
      differentiator: z
        .string()
        .optional()
        .describe("What makes it different from alternatives (recommended)"),
      search_topics: z
        .union([z.array(z.string()), z.string()])
        .optional()
        .describe("Topics/keywords to monitor on X (comma-separated or array)"),
      search_queries: z
        .array(z.string())
        .optional()
        .describe(
          "Cold-start X search-query bank YOU expand from search_topics, in this same call. " +
            "Fan each topic into a few concrete X advanced-search strings (aim ~30 total, e.g. " +
            "'mac menu bar app -filter:replies', 'screen recording privacy lang:en') so the cycle " +
            "fans out instead of running one crude topic-as-query. Seeded directly with NO `claude " +
            "-p` — you are the model doing the expansion, so setup never needs the claude CLI."
        ),
      get_started_link: z
        .string()
        .optional()
        .describe("Primary call-to-action link (signup / get started)"),
      content_guardrails: z
        .string()
        .optional()
        .describe("Anything the posts must avoid saying / claiming"),
      fields: z
        .record(z.string(), z.any())
        .optional()
        .describe(
          "Escape hatch to edit ANY other project field the named props above don't cover — e.g. " +
            "weight, platform, voice_relationship, booking_link, qualification, subreddit_bans, " +
            "short_links_host, short_links_live, content_angle, messaging, landing_pages, posthog. " +
            "Pass {name:'<project>', fields:{<key>:<value>, ...}}; each key SHALLOW-merges onto the " +
            "project, REPLACING that key's whole value (read the current value via status:true first if " +
            "you only want to tweak part of a nested object, then pass the full new value). A value of " +
            "null DELETES the key. 'name' is ignored here (can't rename through this path). This is how " +
            "you edit advanced config without any raw whole-file overwrite."
        ),
    },
  },
  async (args) => {
    // ---- List import sources (for the panel dropdown) ---------------------
    // Read-only browser/profile detection. Never reads the keychain or decrypts
    // a cookie, so it shows no macOS Safe Storage prompt. Lets the user pick the
    // exact browser+profile that holds their X session.
    if (args.action === "detect_x_sources") {
      const r = await xDetectSources();
      return jsonContent({
        action: "detect_x_sources",
        ok: r.ok,
        sources: r.sources,
        recommended: r.recommended,
        error: r.error,
      });
    }

    // ---- Connect X/Twitter: import the user's session into our browser ----
    // Preview-or-run: a call without confirm describes the operation. During an
    // explicit end-to-end setup request the agent gives a short keychain heads-up
    // and calls confirm:true immediately; no extra yes/no round-trip is needed.
    if (args.action === "connect_x") {
      if (args.confirm !== true) {
        // Cheap probe so the explanation reflects current state (no Chrome launch).
        const cur = await xStatus();
        if (cur.connected) {
          return jsonContent({
            action: "connect_x",
            already_connected: true,
            state: cur.state,
            note: "X is already connected in the autoposter's browser. Nothing to import.",
          });
        }
        return jsonContent({
          action: "connect_x",
          requires_confirmation: true,
          current_state: cur.state,
          what_will_happen:
            "To post for you, the autoposter uses its OWN managed Google Chrome (separate from your " +
            "everyday browser). It needs your logged-in X/Twitter session. If you confirm, it will: " +
            "(1) start that managed Chrome if it isn't running, (2) copy ONLY your x.com and twitter.com " +
            "cookies from your everyday browser (Chrome/Arc/Brave/Edge, auto-detected) into it, and " +
            "(3) verify you're logged in. No other site's cookies are read, and your passwords are never " +
            "seen. If it can't import a valid session, a Chrome window will open for you to sign in once.",
          keychain_prompt:
            "Reading the saved session requires macOS to unlock the browser's encrypted cookie store, so " +
            "one or more keychain prompts will appear (\u201c... wants to use your confidential information " +
            "stored in '... Safe Storage' in your keychain\u201d). This is expected. The user enters their Mac " +
            "login password and clicks Allow (or Always Allow to avoid repeats). If they use more than one " +
            "browser, the prompt can appear a few times, once per browser.",
          say_to_user:
            "Heads up: your Mac will pop up a keychain prompt asking to use your browser's Safe Storage. " +
            "That's just us reading your saved X login, nothing else. Type your Mac login password and click " +
            "Allow (or Always Allow). If you use more than one browser you may see it a couple of times, " +
            "once per browser.",
          how_to_proceed:
            "If the user explicitly requested setup or connection, relay the say_to_user line as a brief " +
            "heads-up and immediately call project_config again with action:'connect_x', confirm:true; do not wait " +
            "for another yes/no reply. Optionally pass the recommended x_source. If the user only asked " +
            "what connection would do, stop after this preview.",
        });
      }
      recordOnboardingAttempt("x_connected", {
        state: args.x_source ? "source_selected" : "auto_detect",
      });
      const r = await xConnect(args.x_source, args.x_manual_login);
      let doctorReport = null;
      if (r.connected) {
        completeOnboardingMilestone("x_connected", { state: r.state });
        // The pre-connect Doctor intentionally treats missing X/cookie artifacts
        // as expected. Once connect_x succeeds, run the full phase immediately
        // to verify persistence, CDP, and the durable cookie mirror.
        doctorReport = await runDoctorPhase("full");
      } else {
        blockOnboardingMilestone(
          "x_connected",
          `x_${r.state || "not_connected"}`,
          r.error || r.note || summarizeXAuth(r),
          { state: r.state || "not_connected" }
        );
      }
      return jsonContent({
        action: "connect_x",
        connected: r.connected,
        state: r.state,
        source: r.source,
        summary: summarizeXAuth(r),
        note: r.note,
        attempts: r.attempts,
        doctor: doctorReport
          ? {
              phase: doctorReport.phase,
              ok: doctorReport.ok,
              summary: doctorReport.summary,
            }
          : undefined,
        onboarding: onboardingSnapshot(),
        next_step: r.connected
          ? "X is connected. Next, run project_config action:'profile_scan' to read this account's bio + recent " +
            "posts + replies and draft the project's voice/icp/search_topics in the user's own register " +
            "before saving. Then run a draft cycle (scan_candidates -> draft -> submit_drafts) once the project is fully set up."
          : r.state === "needs_login"
            ? "The user must finish signing in to x.com in the Chrome window that just opened. Tell " +
              "them that single required action, then call project_config action:'connect_x', confirm:true again."
            : "X is not connected yet. " + summarizeXAuth(r),
      });
    }

    // ---- Profile scan: grounding-truth corpus from the connected account ----
    // Reuses the authenticated managed-Chrome session (so it must run AFTER a
    // successful connect_x) to read the user's bio + recent posts + recent
    // replies. Returns the raw corpus plus grounding_instructions; synthesis of
    // voice/icp/topics happens IN THIS CONVERSATION (no nested model), then the
    // agent confirms with the user and calls project_config to persist. Read-only.
    if (args.action === "profile_scan") {
      // Handle is auto-detected from the live logged-in session by the scanner.
      recordOnboardingAttempt("profile_scanned");
      const scan = await xScanProfile();
      if (!scan.ok) {
        const hint =
          scan.state === "browser_not_running" || scan.state === "no_handle"
            ? " Run project_config action:'connect_x' (confirm:true) first so the account is connected, then retry profile_scan."
            : "";
        blockOnboardingMilestone(
          "profile_scanned",
          `profile_${scan.state || "failed"}`,
          scan.error || "profile scan failed",
          { state: scan.state || "failed" }
        );
        return jsonContent({
          action: "profile_scan",
          ok: false,
          state: scan.state,
          error: (scan.error || "profile scan failed") + hint,
          onboarding: onboardingSnapshot(),
        });
      }
      completeOnboardingMilestone("profile_scanned", {
        state: scan.state,
      });
      return jsonContent({
        action: "profile_scan",
        ok: true,
        handle: scan.handle,
        profile: scan.profile,
        counts: scan.counts,
        posts: scan.posts,
        comments: scan.comments,
        grounding_instructions: scan.grounding_instructions,
        website_research_instructions: WEBSITE_RESEARCH_INSTRUCTIONS,
        onboarding: onboardingSnapshot(),
        next_step:
          "TWO steps, in order. FIRST (voice, from this scan): read the bio, posts, and comments " +
          "as GROUND TRUTH and, per grounding_instructions, extract their profession/identity, " +
          "voice & vibe (tone, phrasing, casing, tics), 2-4 verbatim golden-rule example replies, " +
          "a phrase bank + things they avoid, their icp, and recurring themes -> search_topics. " +
          "SECOND (product, from their website): then follow website_research_instructions — discover " +
          "the product URL from config, context, profile links/posts, or public research and read 5+ " +
          "of its pages to fill description, " +
          "differentiator, icp, get_started_link, and content_guardrails, written in the voice you " +
          "just captured. Save the best conservative supported fields without a confirmation " +
          "round-trip. Ask only if no product can be identified or a required field is unknowable.",
      });
    }

    // Status / discovery mode: no project name supplied, or explicitly asked.
    if (args.status === true || !args.name) {
      const projects = listManagedProjectStatus();
      const rtReady = runtimeReady();
      // On a bare .mcpb install the runtime step also materializes the pipeline
      // source that xStatus shells into. Status must still work before that first
      // install, otherwise the agent cannot discover that installation is the
      // next milestone. Avoid probing Python until the owned runtime is ready.
      const x = rtReady
        ? await xStatus().catch(() => ({ connected: false, state: "status_unavailable" }) as any)
        : ({ connected: false, state: "runtime_not_ready" } as any);
      await ensureDoctorPhase(x.connected ? "full" : "pre_connect");
      const ver = await versionStatus();
      const configured = projects.some((p) => p.ready);
      if (rtReady) completeOnboardingMilestone("runtime_ready");
      if (x.connected) {
        completeOnboardingMilestone("x_connected", { state: x.state || "connected" });
      }
      if (configured) {
        completeOnboardingMilestone("project_ready", {
          missing_count: 0,
        });
      }
      return jsonContent({
        configured,
        projects,
        runtime_ready: rtReady,
        x_connected: x.connected,
        x_state: x.state,
        x_handle: x.handle ?? null,
        mcp_version: ver.installed,
        latest_version: ver.latest,
        update_available: ver.update_available,
        update_hint: ver.update_available
          ? `A newer version (${ver.latest}) is available — you're on ${ver.installed}. ` +
            `Tell the user and offer to run the \`runtime\` tool with action:'update' ` +
            `(or \`npx social-autoposter@latest update\`).`
          : undefined,
        required_fields: REQUIRED_FIELDS,
        recommended_fields: RECOMMENDED_FIELDS,
        config_path: configPath(),
        ready_for_verification: rtReady && configured && x.connected,
        onboarding: onboardingSnapshot(),
        next_step:
          !rtReady
            ? "Runtime is not ready yet. It provisions automatically on boot — poll runtime action:'status' until ready (only call runtime action:'install' to retry if status shows the boot provision failed or stalled), then continue setup automatically."
            : projects.length === 0
            ? "No projects yet. Discover the product from conversation context and the connected X profile; research its website, infer a conservative complete project, and call project_config. Ask only if no product can be identified." +
              (x.connected ? "" : " X is not connected yet either — detect_x_sources, warn about keychain prompts, then run connect_x with confirm:true without a separate permission turn.")
            : projects.every((p) => p.ready)
              ? (x.connected
                  ? "All configured projects are ready and X is connected. Run scan_candidates, draft a reply or two, and submit_drafts now to verify end to end without posting. Then SCHEDULE THE AUTOPILOT: call the queue_setup tool and create each returned task with create_scheduled_task (prompt verbatim); do NOT create the deprecated 'social-autoposter-autopilot' task. Then call the `dashboard` tool so the user sees the finished setup."
                  : "All configured projects are ready, but X is NOT connected — posting needs a logged-in " +
                    "x.com session. Detect sources and run project_config action:'connect_x', confirm:true; do not ask whether to proceed.")
              : "Some projects are missing required fields (see each project's missing_required). Derive them from config, context, profile_scan, and website research, then call project_config again. Ask only if a required field is genuinely unknowable." +
                (x.connected ? "" : " X is also not connected yet; detect sources and run connect_x with confirm:true."),
      });
    }

    // Apply mode (incremental): merge whatever fields were supplied onto the
    // named project, then report whether it's now ready or still missing fields.
    try {
      recordOnboardingAttempt("project_ready", {
        missing_count: 0,
      });
      const result = applySetup(args as ProjectInput);
      if (result.ready) {
        completeOnboardingMilestone("project_ready", { missing_count: 0 });
      } else {
        blockOnboardingMilestone(
          "project_ready",
          "missing_required_fields",
          `Project '${result.project}' still needs: ${result.missing_required.join(", ")}`,
          { missing_count: result.missing_required.length }
        );
      }
      // Seed this project's search_topics into the DB universe the cycle reads
      // (project_search_topics). Without this a freshly-configured project has
      // topics in config.json but ZERO rows in the DB, so draft_cycle's topic
      // picker raises and the cycle silently returns nothing. Best-effort: a
      // seed hiccup never fails setup — the cycle's fail-loud path still tells
      // the user if topics are missing. Only runs once the project is ready
      // (i.e. it actually has search_topics to seed). (2026-06-02)
      let seedNote = "";
      let topicsSeeded = false;
      let topicCount = 0;
      let searchQueries: Array<{ query: string; topic: string }> = [];
      if (result.ready) {
        recordOnboardingAttempt("topics_seeded");
        const seed = await runPython(
          "scripts/seed_search_topics.py",
          ["--project", result.project],
          { timeoutMs: 60_000 }
        );
        if (seed.code === 0) {
          const m = /planned=(\d+)\s+inserted=(\d+)\s+updated=(\d+)/.exec(seed.stdout);
          topicCount = m ? Number(m[1]) : 0;
          topicsSeeded = true;
          completeOnboardingMilestone("topics_seeded", {
            topic_count: topicCount,
          });
          seedNote = m
            ? ` Seeded ${m[1]} search topic(s) into the DB (new: ${m[2]}, updated: ${m[3]}), so scan_candidates has a topic universe to work with.`
            : " Seeded search topics into the DB so scan_candidates has a topic universe to work with.";
        } else {
          const tail = (seed.stderr || seed.stdout).trim().split("\n").slice(-1)[0] || "unknown error";
          blockOnboardingMilestone(
            "topics_seeded",
            "topic_seed_failed",
            tail,
            { exit_code: seed.code }
          );
          seedNote = ` (Heads up: couldn't seed search topics into the DB yet — ${tail}. scan_candidates will tell you clearly if topics are missing.)`;
        }

        // Cold-start QUERY supply: fan the seeded topics out into >=30 real X
        // search queries (project_search_queries) so the deterministic Phase 1
        // bank (qualified_query_bank.py) has something to run on day one.
        // Without this, a freshly-configured project's bank is empty and the
        // cycle falls back to ONE crude topic-as-query.
        //
        // CLAUDE-FREE: the in-session agent (you) expands topics -> queries and
        // passes them as `search_queries` in THIS call. We seed them directly via
        // --queries-json, so setup never shells out to `claude -p` (which isn't
        // installed in the Desktop / .mcpb lane and was the FileNotFoundError
        // users hit). If the agent didn't supply queries, we skip expansion
        // entirely — the topic-as-query fallback still runs, just narrower — and
        // nudge the agent to re-run with search_queries. (2026-06-19)
        const agentQueries = Array.isArray(args.search_queries)
          ? (args.search_queries as string[]).map((q) => String(q).trim()).filter(Boolean)
          : [];
        if (seed.code === 0 && agentQueries.length) {
          try {
            const qfile = path.join(
              os.tmpdir(),
              `saps-queries-${result.project}-${Date.now()}.json`
            );
            fs.writeFileSync(
              qfile,
              JSON.stringify({ queries: agentQueries.map((q) => ({ query: q, topic: "" })) })
            );
            const qseed = await runPython(
              "scripts/seed_search_queries.py",
              ["--project", result.project, "--queries-json", qfile,
                "--supply-test", "auto", "--emit-json"],
              { timeoutMs: 600_000 }
            );
            try { fs.unlinkSync(qfile); } catch { /* best-effort cleanup */ }
            const qm = /seeded=(\d+)\s+inserted=(\d+)\s+updated=(\d+)/.exec(qseed.stdout);
            const qjson = qseed.stdout.split("===QUERIES_JSON===")[1];
            if (qjson) {
              try {
                searchQueries = (JSON.parse(qjson.trim()).queries ?? []) as typeof searchQueries;
              } catch {
                /* leave empty; count note still informs the user */
              }
            }
            if (qseed.code === 0 && qm) {
              const n = searchQueries.length || Number(qm[1]);
              seedNote += ` Seeded ${n} search quer${n === 1 ? "y" : "ies"} so the cycle can fan out instead of running a single query.`;
            } else if (qseed.code !== 0) {
              const qtail = (qseed.stderr || qseed.stdout).trim().split("\n").slice(-1)[0] || "unknown error";
              seedNote += ` (Search queries not seeded yet — ${qtail}. The cycle still runs off the seeded topics.)`;
            }
          } catch (e) {
            seedNote += ` (Search-query seeding skipped — ${(e as Error).message}.)`;
          }
        } else if (seed.code === 0) {
          seedNote += ` (No search_queries supplied, so the cycle will run off the seeded topics one at a time. To fan out, re-call project_config with a search_queries array of ~30 X search strings you expand from these topics — it seeds them directly, no claude CLI.)`;
        }
      }
      // Surface any advanced (escape-hatch) field edits in the note so the
      // agent can confirm exactly what changed to the user.
      let advancedNote = "";
      if (result.fields_set.length || result.fields_removed.length) {
        const parts: string[] = [];
        if (result.fields_set.length) parts.push(`set ${result.fields_set.join(", ")}`);
        if (result.fields_removed.length) parts.push(`removed ${result.fields_removed.join(", ")}`);
        advancedNote = ` Advanced fields updated: ${parts.join("; ")}.`;
      }
      return jsonContent({
        ok: true,
        project: result.project,
        action: result.created ? "created" : "updated",
        ready: result.ready,
        missing_required: result.missing_required,
        topics_seeded: topicsSeeded,
        topic_count: topicCount,
        search_queries: searchQueries,
        fields_set: result.fields_set,
        fields_removed: result.fields_removed,
        config_path: configPath(),
        onboarding: onboardingSnapshot(),
        note: (result.ready
          ? `Project '${result.project}' is fully configured.${seedNote} Next: if X is not connected, ` +
            `detect sources, warn about keychain prompts, and call project_config with ` +
            `action:'connect_x', confirm:true immediately. Once X is connected, run scan_candidates -> submit_drafts ` +
            `to verify without posting, then schedule the autopilot: call queue_setup and create each ` +
            `returned task with create_scheduled_task (prompt verbatim).`
          : `Saved what you provided for '${result.project}'. Still need: ${result.missing_required.join(", ")}. ` +
            `First derive those fields from existing context, profile_scan, and website research, then ` +
            `call project_config again with name='${result.project}'. Ask only if a required field is genuinely unknowable.`) +
          advancedNote,
      });
    } catch (e) {
      return textContent(`Setup failed: ${(e as Error).message}`);
    }
  }
);

// ---- draft_cycle: DEPRECATED 2026-06-20 (registration removed) -------------
// Replaced by the scan_candidates -> (agent drafts) -> submit_drafts flow, so the
// AI drafting now runs in the calling session (on the user's plan) instead of a
// spawned `claude -p`. post_drafts (below) still posts the approved subset, and
// submit_drafts completes the onboarding "draft_verified" milestone that this
// tool used to. The underlying pipeline (run-twitter-cycle.sh) and the
// produceDrafts() helper are kept as the source those tools reuse; only the
// draft_cycle tool registration was removed here.

// ---- post_drafts: post the user's chosen drafts from a batch ---------------
// Second half of the manual loop. The user reviewed the table from draft_cycle
// and said which numbers to post / edit; this posts exactly those. Editing a
// draft implies posting it. Indices are 1-based, matching the table.
tool(
  "post_drafts",
  {
    title: "Post chosen drafts",
    description:
      "Post the drafts the user approved from a submit_drafts batch. Pass the batch_id from " +
      "submit_drafts and the user's decision by NUMBER (1-based, matching the table): `post` is " +
      "the list of draft numbers to post as drafted; `edits` rewrites a draft's text before " +
      "posting it (editing implies posting); `post_all` posts every draft. Only the chosen " +
      "drafts post; anything not listed is left unposted. Call this ONLY after the user has " +
      "told you which drafts they want. After posting, call the `dashboard` tool so the user " +
      "sees the updated state.",
    inputSchema: {
      batch_id: z.string().describe("The batch_id returned by submit_drafts."),
      post: z
        .array(z.number().int().positive())
        .optional()
        .describe("1-based draft numbers to post as drafted, e.g. [1, 3, 5]."),
      edits: z
        .array(z.object({ n: z.number().int().positive(), text: z.string() }))
        .optional()
        .describe("Rewrites: each {n, text} replaces draft n's wording, then posts it."),
      post_all: z.boolean().optional().describe("Post every draft in the batch."),
      reject: z
        .array(z.number().int().positive())
        .optional()
        .describe(
          "1-based draft numbers the user REJECTED. They are marked done and never " +
            "shown for review again, and are not posted."
        ),
    },
  },
  async ({ batch_id, post, edits, post_all, reject }) => {
    const plan = readPlan(batch_id);
    if (!plan || !(plan.candidates && plan.candidates.length)) {
      return textContent(
        `No drafts found for batch ${batch_id}. Run scan_candidates then submit_drafts again to produce a fresh batch.`
      );
    }
    const candidates = plan.candidates;
    const total = candidates.length;
    const warnings: string[] = [];
    const inRange = (n: number) => n >= 1 && n <= total;

    // ---- Rejections: durable + final --------------------------------------
    // A rejected draft is marked terminal so it NEVER re-appears for review and is
    // never posted. A reject overrides any earlier approve on the same card.
    const rejected: number[] = [];
    (reject || []).forEach((n) => {
      if (!inRange(n)) {
        warnings.push(`ignored reject #${n}: out of range (1-${total})`);
        return;
      }
      const c = candidates[n - 1];
      if (c.posted === true) {
        warnings.push(`#${n} already posted; not rejecting`);
        return;
      }
      c.terminal = true;
      c.terminal_reason = "rejected";
      c.approved = false;
      rejected.push(n);
    });

    // Apply edits first; an edited draft is always posted.
    const approve = new Set<number>();
    let editedCount = 0;
    (edits || []).forEach((e) => {
      if (!inRange(e.n)) {
        warnings.push(`ignored edit for #${e.n}: out of range (1-${total})`);
        return;
      }
      const text = (e.text ?? "").trim();
      if (!text) {
        warnings.push(`ignored empty edit for #${e.n}`);
        return;
      }
      candidates[e.n - 1].reply_text = text;
      approve.add(e.n);
      editedCount++;
    });

    if (post_all) {
      for (let i = 1; i <= total; i++) approve.add(i);
    }
    (post || []).forEach((n) => {
      if (inRange(n)) approve.add(n);
      else warnings.push(`ignored #${n}: out of range (1-${total})`);
    });

    // Cross-surface de-dup: chat and the menu-bar pop-ups can both approve, so
    // never re-post a candidate the other surface already posted OR ruled out.
    const alreadyDone: number[] = [];
    for (const n of Array.from(approve)) {
      if (candidates[n - 1]?.posted === true || candidates[n - 1]?.terminal === true) {
        approve.delete(n);
        alreadyDone.push(n);
      }
    }
    if (alreadyDone.length) {
      warnings.push(`already posted/decided (skipped): ${alreadyDone.sort((a, b) => a - b).join(", ")}`);
    }

    // STICKY approve: record the approval DURABLY and never clear another card's
    // prior approval. The old `c.approved = approve.has(i+1)` reset every card on
    // each call, so a later post_drafts for a different card dropped a
    // restart-interrupted approved card back into "pending". postApproved filters
    // posted/terminal, so the approved set only ever drains what's genuinely left.
    approve.forEach((n) => {
      const c = candidates[n - 1];
      if (c) c.approved = true;
    });
    writePlan(batch_id, plan);

    if (approve.size === 0) {
      return jsonContent({
        batch_id,
        drafted: total,
        posted: 0,
        rejected: rejected.length,
        skipped: total,
        edited: editedCount,
        note: rejected.length
          ? `Rejected ${rejected.length} draft(s); they won't be shown for review again. Nothing was posted.`
          : "No drafts selected to post. Nothing was posted.",
        warnings,
      });
    }

    const result = await postApproved(batch_id, plan);
    // Report the REAL posted count from the pipeline, not the approved count.
    // A run can approve N yet land 0 (browser/session failure); reporting
    // approve.size here told the agent "posted: N" on a total failure.
    const actuallyPosted = typeof result.posted === "number" ? result.posted : approve.size;
    if (actuallyPosted < approve.size) {
      warnings.push(
        `only ${actuallyPosted}/${approve.size} actually posted (exit=${result.exit_code}); ` +
          `see result.summary / result.stderr_tail for the reason`
      );
    }
    return jsonContent({
      batch_id,
      drafted: total,
      posted: actuallyPosted,
      approved: approve.size,
      rejected: rejected.length,
      skipped: total - actuallyPosted,
      edited: editedCount,
      result,
      warnings,
    });
  }
);

// ---- autopilot: MCP tool removed ------------------------------------------
// The `autopilot` MCP tool (enable/disable/status) was intentionally removed:
// hands-free background posting is no longer toggled from the agent/tool surface.
// The underlying launchd cycle job + plist (com.m13v.social-twitter-cycle) and
// the daily self-updater are NOT touched here — an already-loaded job keeps
// running, and the plist files stay on disk. The plist helpers above
// (ensurePlist / plistXml / loadPlist / unloadPlist) and the constants are kept
// as the underlying source for that job; the `dashboard` snapshot still reports
// the job's loaded state via autopilotLoaded(). To enable/disable the job now,
// use launchctl directly or re-add a tool here.

// ---- get_stats: read-only -------------------------------------------------
tool(
  "get_stats",
  {
    title: "Get X/Twitter stats",
    description:
      "Read-only post + engagement stats for the X/Twitter rail over the last N days. " +
      "Wraps project_stats_json.py. Use to show the user how their posts are performing. " +
      "After returning the numbers, call the `dashboard` tool so the user sees them rendered.",
    inputSchema: {
      days: z.number().int().min(1).max(90).default(7),
      project: z
        .string()
        .optional()
        .describe("Which configured project to report on. Optional when only one project is set up; required when several are."),
    },
  },
  async ({ days, project }) => {
    const r = resolveProject(project);
    if (!r.ok) return textContent(r.message!);
    const proj = r.project!;
    const args = ["--posts-only", "--platform", "twitter", "--days", String(days)];
    if (proj) args.push("--project", proj);
    const res = await runPython("scripts/project_stats_json.py", args, { timeoutMs: 120_000 });
    if (res.code !== 0) {
      return textContent(`stats failed (exit ${res.code}):\n${res.stderr || res.stdout}`);
    }
    try {
      return jsonContent(JSON.parse(res.stdout));
    } catch {
      return textContent(res.stdout);
    }
  }
);

// ---- version: report installed version + deliver updates on demand ---------
// ---- runtime: install + version/update + diagnostics ----------------------
// ONE plumbing tool for the whole local-runtime lifecycle, action-based like
// project_config and autopilot. The pipeline runs Python locally; rather than
// depend on the user's system Python (the #1 source of install failures), the
// first run provisions a fully OWNED uv runtime: standalone CPython + owned venv
// + deps + Chromium. It also reports/installs new releases and runs the Doctor.
// Plain (non-UI) so EVERY host can drive it — the panel's Install card and
// Update button are just skins that call action:'install' then poll
// action:'status'. See runtime.ts for the provisioning + progress contract.
//
// Actions:
//   status (default) — is the owned runtime installed? + in-progress step detail
//   install          — start provisioning in the background; poll status to follow
//   version          — installed vs latest published, whether an update is available
//   update           — pull + install the latest release (npx social-autoposter@latest update)
//   doctor           — run structured environment diagnostics (phase: pre_connect|full)
//   doctor_status    — last persisted Doctor result without re-running checks
tool(
  "runtime",
  {
    title: "Runtime: status, update & diagnostics",
    description:
      "The ONE plumbing tool for the autoposter's local runtime lifecycle. The runtime PROVISIONS " +
      "ITSELF automatically when the server boots, so you normally never call action:'install' — just " +
      "poll action:'status'. action:'status' (default) " +
      "reports whether the self-contained Python/Chromium runtime is installed and, mid-install, the " +
      "per-step progress (uv, Python, venv, dependencies, Chromium) — poll it to watch boot " +
      "provisioning finish. action:'install' is a TROUBLESHOOTING retry that re-provisions that runtime " +
      "(a private Python via uv, NOT your system Python, plus " +
      "deps and Chromium); it runs in the background, returns immediately, is safe to call " +
      "repeatedly, and is a no-op once installed — only reach for it if status shows the boot provision " +
      "failed or stalled. action:'version' shows installed vs latest published " +
      "and whether an update is available; action:'update' pulls and installs the latest release (runs " +
      "`npx social-autoposter@latest update`, taking effect after the client reconnects/restarts). " +
      "action:'doctor' runs structured environment diagnostics (phase:'pre_connect' is safe at " +
      "onboarding start and treats the missing X session/cookies as expected; phase:'full' verifies the " +
      "completed environment after X is connected); action:'doctor_status' returns the last persisted " +
      "Doctor result without re-running. Use action:'status' to confirm readiness during setup; reach " +
      "for action:'install'/'doctor' only when status or another tool reports the runtime isn't ready " +
      "or to diagnose a broken environment; use action:'version'/'update' for version checks.",
    inputSchema: {
      action: z
        .enum(["status", "install", "version", "update", "doctor", "doctor_status"])
        .optional(),
      phase: z
        .enum(["pre_connect", "full"])
        .optional()
        .describe("Only for action:'doctor' — which diagnostic phase to run (default pre_connect)."),
    },
  },
  async ({ action, phase }: { action?: "status" | "install" | "version" | "update" | "doctor" | "doctor_status"; phase?: DoctorPhase }) => {
    // ---- install: start provisioning the owned runtime --------------------
    if (action === "install") {
      if (runtimeReady()) {
        completeOnboardingMilestone("runtime_ready");
        return jsonContent({ already_installed: true, ...runtimeSnapshot() });
      }
      recordOnboardingAttempt("runtime_ready");
      const progress = startProvisioning();
      return jsonContent({
        started: true,
        runtime_ready: false,
        note: "Runtime install started. Poll runtime action:'status' every ~1.5s for progress.",
        progress,
      });
    }

    // ---- version: installed vs latest published ---------------------------
    if (action === "version") {
      const v = await versionStatus();
      return jsonContent({
        installed: v.installed,
        latest_published: v.latest,
        update_available: v.update_available,
        update_command: "npx social-autoposter@latest update",
        note:
          v.latest == null
            ? "Could not reach npm to check for a newer version (offline or registry error)."
            : v.update_available
              ? `A newer version (${v.latest}) is available. Run this tool with action:'update' ` +
                "to install it, or run `npx social-autoposter@latest update` in a terminal."
              : "You are on the latest published version.",
      });
    }

    // ---- update: pull + install the latest release ------------------------
    if (action === "update") {
      // Overwrites mcp/dist/ (including this running file — safe; the loaded
      // process keeps old code) and re-runs install.mjs to re-register the
      // client config. npx is non-interactive so it can't stall on a confirm.
      const before = VERSION;
      const res = await run("npx", ["-y", "social-autoposter@latest", "update"], {
        timeoutMs: 600_000,
      });
      const latest = await latestPublishedVersion(); // bust the cache
      return jsonContent({
        action: "update",
        ran: "npx social-autoposter@latest update",
        exit_code: res.code,
        installed_before: before,
        latest_published: latest,
        ok: res.code === 0,
        takes_effect:
          "after the MCP server restarts — reconnect the client / restart Claude Desktop or " +
          "Claude Code. This process keeps running the previous version until then.",
        output_tail: (res.stdout + "\n" + res.stderr).trim().split("\n").slice(-20).join("\n"),
      });
    }

    // ---- doctor: run structured diagnostics -------------------------------
    if (action === "doctor") {
      const selected = phase || "pre_connect";
      const report = await runDoctorPhase(selected);
      return jsonContent({ doctor: report, onboarding: onboardingSnapshot() });
    }

    // ---- doctor_status: last persisted Doctor result ----------------------
    if (action === "doctor_status") {
      return jsonContent({
        doctor: onboardingLedger()?.doctor?.latest ?? null,
        onboarding: onboardingSnapshot(),
      });
    }

    // ---- status (default): runtime install snapshot -----------------------
    const snapshot = runtimeSnapshot();
    if (snapshot.runtime_ready) {
      completeOnboardingMilestone("runtime_ready");
    } else if (snapshot.progress?.done && !snapshot.progress.ok) {
      blockOnboardingMilestone(
        "runtime_ready",
        "runtime_install_failed",
        snapshot.progress.error || "Runtime installation failed",
        { outcome: "failed" }
      );
    }
    return jsonContent({ ...snapshot, onboarding: onboardingSnapshot() });
  }
);

function runtimeSnapshot() {
  const rt = readRuntime();
  const progress = readProgress();
  return {
    runtime_ready: runtimeReady(),
    provisioning: isProvisioning(),
    python: rt?.python ?? null,
    python_version: rt?.python_version ?? null,
    progress: progress ?? null,
    onboarding: onboardingSnapshot(),
  };
}

// ---- queue_setup: hand the agent the two worker-task specs -----------------
// The customer-box autopilot is now two single-purpose scheduled tasks that
// drain the pipeline's claude -p job queue (see the queue-worker section below).
// The agent can't author their prompts (baked absolute paths to python +
// claude_job.py), so this tool returns the EXACT specs to pass straight to the
// host tool create_scheduled_task. Calling it also eagerly pre-approves the
// worker tools, so the tasks never stall on a permission prompt. Read-only +
// idempotent.
tool(
  "queue_setup",
  {
    title: "Get autopilot scheduled-task specs",
    description:
      "Returns the two scheduled tasks that run the hands-free draft autopilot on this machine " +
      "(saps-phase1-query and saps-phase2b-draft). For EACH returned task, call the host tool " +
      "create_scheduled_task with its taskId, cronExpression, and prompt VERBATIM (do not edit the " +
      "prompt — it contains exact local paths). These tasks drain the local drafting queue that the " +
      "real pipeline feeds; the pipeline itself is kicked by a launchd job this server installs. Use " +
      "this as the final onboarding step instead of the old single autopilot task.",
    inputSchema: {},
  },
  async () => {
    ensureQueueWorkerToolsAllowed();
    const tasks = QUEUE_WORKERS.map((spec) => ({
      taskId: spec.taskId,
      cronExpression: "* * * * *",
      prompt: queueWorkerBody(spec),
    }));
    return jsonContent({
      tasks,
      next_step:
        "Create each task with the host tool create_scheduled_task (taskId, cronExpression, prompt " +
        "verbatim). Both fire every minute and only draft — they never post. The pipeline that feeds " +
        "them is run by a launchd job this server already (re)installs on boot. Do NOT also create the " +
        "old 'social-autoposter-autopilot' task; it is deprecated.",
    });
  }
);

// ---- panel: MCP Apps control surface --------------------------------------
// A self-contained HTML view rendered by hosts that support MCP Apps (Claude
// desktop/web, etc.). It duplicates NO pipeline logic: each button calls one of
// the tools above (draft_cycle / project_config / get_stats) through the host
// and re-reads status. The tool itself returns the first-paint snapshot so the
// view has data the instant it loads.

// Is either launchd job (cycle / daily updater) currently loaded?
// "Autopilot" is now the Claude Desktop scheduled task `social-autoposter-autopilot`
// (created during onboarding via create_scheduled_task), NOT the legacy launchd job.
// We can't read the host's enabled/paused flag, but the task's presence on disk is the
// single signal the dashboard AND the menu bar key off of, so they stay aligned.
async function autopilotLoaded(): Promise<{ autopilot_on: boolean; auto_update_on: boolean }> {
  let autopilot_on = false;
  try {
    const cfg = process.env.CLAUDE_CONFIG_DIR || path.join(os.homedir(), ".claude");
    autopilot_on = fs.existsSync(
      path.join(cfg, "scheduled-tasks", AUTOPILOT_TASK_ID, "SKILL.md")
    );
  } catch {
    /* leave false */
  }
  let auto_update_on = false;
  try {
    const res = await run("launchctl", ["list"], { timeoutMs: 10_000 });
    auto_update_on = res.stdout.split("\n").some((l) => l.includes(UPDATER_LABEL));
  } catch {
    /* leave false */
  }
  return { autopilot_on, auto_update_on };
}

// ---- Autopilot prompt: keep the scheduled task's SKILL.md current ------------
// The scheduled task's prompt (SKILL.md) is owned by the host and does NOT update
// when the plugin updates. So a behavior change (the no-improvise / voice-inline /
// stop-cleanly rules) would never reach an already-scheduled task. We close that
// gap by REWRITING the task's SKILL.md on server boot when its embedded prompt
// version is older than what this build ships. The host reads the file fresh on
// each run, so the next fire picks up the new prompt — no host tool, no user
// action. Bump AUTOPILOT_PROMPT_VERSION whenever the prompt below changes.
const AUTOPILOT_PROMPT_VERSION = 2;
const AUTOPILOT_PROMPT_MARKER = "saps_autopilot_prompt_version";

function autopilotSkillPath(): string {
  const cfg = process.env.CLAUDE_CONFIG_DIR || path.join(os.homedir(), ".claude");
  return path.join(cfg, "scheduled-tasks", AUTOPILOT_TASK_ID, "SKILL.md");
}

// The canonical draft-only autopilot prompt: uses ONLY scan_candidates +
// submit_drafts, drafts from the INLINE project_voices, improvises nothing, and
// stops cleanly if its tools aren't available.
function autopilotSkillMd(project: string): string {
  const body = [
    `You are running the Social Autoposter draft-only autopilot for the project "${project}". ` +
      `Run ONE cycle, then stop. DRAFT-ONLY: you must NEVER post to X/Twitter — you only queue ` +
      `drafts for the user's approval. Use ONLY two tools — scan_candidates and submit_drafts — ` +
      `and IMPROVISE NOTHING ELSE. Specifically: NEVER run Bash, Read, Write, Edit, python, or any ` +
      `shell/file tool, for ANY reason — not to debug, inspect a tool result, verify an outcome, or ` +
      `investigate something surprising. This run is unattended: any tool that is not pre-approved ` +
      `STALLS the whole pipeline waiting on a human who is not there, so reaching for one is never ` +
      `worth it. If something looks off, report it in one line and stop.`,
    ``,
    `Steps:`,
    `1. Call scan_candidates with project "${project}". It long-polls: if it returns a "Scan in ` +
      `progress" status, call scan_candidates again (same args) and keep re-calling until it ` +
      `returns candidates. Never sleep or use background waits. If it returns no candidates, stop ` +
      `cleanly — nothing to do this cycle.`,
    `2. The result includes a project_voices field — the brand voice + guardrails for each ` +
      `candidate's project. Draft from project_voices[<matched_project>] (voice, description, ` +
      `differentiator, content_guardrails). NEVER read config files, call project_config, or run ` +
      `Bash/Read to find the voice — everything you need is already in the scan result.`,
    `3. For each candidate you judge genuinely worth engaging, write ONE on-brand reply (<=250 ` +
      `chars, match the thread's language, genuinely helpful — not spammy, no hard selling). Skip ` +
      `the rest.`,
    `4. Call submit_drafts with the batch_id from scan_candidates and a drafts array of ` +
      `{candidate_id, reply_text}. This queues them for the menu-bar approval UI. Do NOT call ` +
      `post_drafts — posting is the user's decision.`,
    `5. You are now DONE: report in ONE short line how many drafts you queued, then stop. ` +
      `"Queued 0 new draft(s)" is a NORMAL, expected result — it means the threads were already ` +
      `queued in an earlier cycle (dedup), NOT a failure. Treat ANY count, including zero, as ` +
      `success. NEVER re-read, parse, or inspect the submit_drafts result; NEVER open files under ` +
      `tool-results/ or anywhere else; NEVER run Bash/Read/python to confirm what happened. A ` +
      `zero or surprising count is not a reason to investigate — just report it and stop.`,
    ``,
    `HARD GUARD: if scan_candidates is NOT available (ToolSearch returns no matching tool — can ` +
      `happen when a run spawns before the extension's MCP server has reconnected), STOP immediately ` +
      `and report "S4L tools unavailable, skipping this cycle." Do NOT search the connector registry, ` +
      `do NOT call list_connectors, do NOT improvise any other tool.`,
  ].join("\n");
  return (
    `---\n` +
    `name: ${AUTOPILOT_TASK_ID}\n` +
    `description: Social Autoposter draft-only autopilot for project ${project} — scans X, drafts ` +
    `on-brand replies (voice inline), queues them for approval. Never posts.\n` +
    `---\n\n` +
    body +
    `\n\n<!-- ${AUTOPILOT_PROMPT_MARKER}: ${AUTOPILOT_PROMPT_VERSION} -->\n`
  );
}

// Read the project name back from an existing autopilot SKILL.md so a rewrite
// keeps the same project. Null if it can't be determined (then leave it alone).
function detectAutopilotProject(skillMd: string): string | null {
  const m1 = /project[s]?\s+"([^"]+)"/i.exec(skillMd);
  if (m1) return m1[1];
  const m2 = /for project\s+([A-Za-z0-9_-]+)/i.exec(skillMd);
  if (m2) return m2[1];
  return null;
}

// Refresh the scheduled task's SKILL.md when this build ships a newer prompt than
// what's on disk. Best-effort, synchronous, never throws. Only touches an EXISTING
// task (onboarding creates the first one), only when stale, and only when the
// project reads back, so we never write a wrong/blank project.
function ensureAutopilotPromptCurrent(): void {
  try {
    const skillPath = autopilotSkillPath();
    if (!fs.existsSync(skillPath)) return; // no scheduled task yet
    const cur = fs.readFileSync(skillPath, "utf-8");
    const m = new RegExp(`${AUTOPILOT_PROMPT_MARKER}:\\s*(\\d+)`).exec(cur);
    const curVer = m ? parseInt(m[1], 10) : 0;
    if (curVer >= AUTOPILOT_PROMPT_VERSION) return; // already current
    const project = detectAutopilotProject(cur);
    if (!project) {
      console.error(
        `[autopilot] prompt is stale (v${curVer}) but project unreadable from SKILL.md; leaving it`
      );
      return;
    }
    fs.writeFileSync(skillPath, autopilotSkillMd(project), "utf-8");
    console.error(
      `[autopilot] refreshed scheduled-task prompt -> v${AUTOPILOT_PROMPT_VERSION} (was v${curVer}), project=${project}`
    );
  } catch (e: any) {
    console.error(`[autopilot] ensureAutopilotPromptCurrent error: ${e?.message || e}`);
  }
}

// ---- Pre-approve the autopilot's own tools in ~/.claude/settings.json --------
// A Desktop scheduled task created via create_scheduled_task defaults to "Ask"
// permission mode, and per the Desktop docs an un-pre-approved tool STALLS the
// run until a human approves it. Allow rules from ~/.claude/settings.json DO
// apply to scheduled-task sessions, so we ship pre-approval for THIS server's
// tools (the only ones the autopilot is allowed to use) into that file on boot.
// This makes the legitimate scan -> submit path never stall, independent of
// whether anyone clicked "Always allow" during onboarding. ALLOW-only by design:
// it pre-approves our tools; it does NOT (and on this lane cannot) restrict any
// others — that is the prompt's job. Merge-in-place: read, add only missing
// entries, write back; never overwrite a user's settings on a parse error.
//
// The tool id is mcp__<server>__<tool>. A .mcpb install registers this server
// under the manifest name ("social-autoposter"); we also list the protocol name
// ("S4L") so pre-approval matches however the host loaded it. Extra entries that
// don't match the live namespace are harmless no-ops.
const AUTOPILOT_ALLOWED_TOOLS = [
  "mcp__social-autoposter__scan_candidates",
  "mcp__social-autoposter__submit_drafts",
  "mcp__S4L__scan_candidates",
  "mcp__S4L__submit_drafts",
];

function ensureAutopilotToolsAllowed(): void {
  try {
    // Only touch settings.json for installs that actually have the autopilot
    // scheduled — mirrors the prompt-refresh gate, so we never edit a user's
    // global settings on a machine that doesn't use the scheduled task.
    if (!fs.existsSync(autopilotSkillPath())) return;
    const cfg = process.env.CLAUDE_CONFIG_DIR || path.join(os.homedir(), ".claude");
    const settingsPath = path.join(cfg, "settings.json");
    let settings: any = {};
    if (fs.existsSync(settingsPath)) {
      try {
        settings = JSON.parse(fs.readFileSync(settingsPath, "utf-8")) || {};
      } catch (e: any) {
        // Malformed user settings: do NOT clobber — log and bail.
        console.error(
          `[autopilot] settings.json unparseable; skipping tool pre-approval: ${e?.message || e}`
        );
        return;
      }
    }
    if (typeof settings !== "object" || Array.isArray(settings)) return;
    const perms = (settings.permissions ??= {});
    if (typeof perms !== "object" || Array.isArray(perms)) return;
    const allow: string[] = Array.isArray(perms.allow) ? perms.allow : (perms.allow = []);
    let added = 0;
    for (const t of AUTOPILOT_ALLOWED_TOOLS) {
      if (!allow.includes(t)) {
        allow.push(t);
        added++;
      }
    }
    if (added === 0) return;
    fs.mkdirSync(cfg, { recursive: true });
    fs.writeFileSync(settingsPath, JSON.stringify(settings, null, 2) + "\n", "utf-8");
    console.error(
      `[autopilot] pre-approved ${added} autopilot tool(s) in ${settingsPath} (allow-only)`
    );
  } catch (e: any) {
    console.error(`[autopilot] ensureAutopilotToolsAllowed error: ${e?.message || e}`);
  }
}

// ===========================================================================
// Queue-worker scheduled tasks + launchd kicker (2026-06-23)
//
// Replaces the scan_candidates -> host-draft -> submit_drafts autopilot. The
// REAL pipeline runs in DRAFT_ONLY mode under launchd; its `claude -p` calls go
// through scripts/claude_job.py's file queue (run_claude.sh provider seam); two
// scheduled tasks drain that queue. Each task is single-purpose (one job type),
// fires every minute, claims ONE job, runs the pipeline's own prompt as its
// Claude turn, writes the result back, and stops.
// ===========================================================================
const QUEUE_WORKER_PROMPT_VERSION = 1;
const QUEUE_WORKER_PROMPT_MARKER = "saps_queue_worker_prompt_version";

// One spec per worker task. queueType MUST match scripts/claude_job.py TAG_TO_TYPE.
const QUEUE_WORKERS: { taskId: string; queueType: string; human: string }[] = [
  { taskId: PHASE1_TASK_ID, queueType: "twitter-query", human: "Phase 1 X search-query drafting" },
  { taskId: PHASE2B_TASK_ID, queueType: "twitter-prep", human: "Phase 2b reply drafting" },
];

function scheduledTaskSkillPath(taskId: string): string {
  const cfg = process.env.CLAUDE_CONFIG_DIR || path.join(os.homedir(), ".claude");
  return path.join(cfg, "scheduled-tasks", taskId, "SKILL.md");
}

// The queue dir the worker reads/writes. MUST equal what the launchd kicker sets
// (kickerEnv below) and what claude_job.py uses, so both ends meet on one path.
function queueDir(): string {
  return path.join(sapsStateDir(), "claude-queue");
}

// A single worker task's SKILL.md. Bash-only: claim -> follow the job's own
// prompt -> write JSON -> submit. Paths are baked in at generation time because
// the unattended Bash session can't resolve our env. The job's `prompt` field is
// the pipeline's real Phase-1/Phase-2b prompt (full styles/voice/em-dash rules),
// so drafting quality is identical to the legacy `claude -p` path.
function queueWorkerBody(spec: { taskId: string; queueType: string; human: string }): string {
  const py = resolvePython();
  const job = path.join(repoDir(), "scripts", "claude_job.py");
  const sd = sapsStateDir();
  const outDir = queueDir();
  return [
    `You are the S4L "${spec.human}" queue worker. Run ONE iteration, then STOP.`,
    ``,
    `The deterministic posting pipeline runs on this Mac. When it needs a Claude ` +
      `turn it drops a job on a local file queue. Your only job: pick up the next ` +
      `"${spec.queueType}" job, do EXACTLY what its prompt says, hand the result back. ` +
      `You do this with Bash and Write, and NOTHING else. This run is unattended — ` +
      `reaching for any other tool, or trying to "investigate", STALLS it forever.`,
    ``,
    `Steps:`,
    `1. Claim the next job. Run this EXACT Bash command:`,
    `     ${py} ${job} next --type ${spec.queueType} --state-dir ${sd}`,
    `   It prints one line of JSON. If it prints "{}" (empty), there is NO work — ` +
      `report "no jobs" in one line and STOP. You are done.`,
    `2. Otherwise it prints {"job_id":"...","prompt":"...","schema":...}. The "prompt" ` +
      `field is a complete, self-contained instruction the pipeline wrote for you. ` +
      `Follow it EXACTLY and produce the SINGLE JSON object it asks for. If "schema" ` +
      `is present, your JSON MUST satisfy it. Output ONLY that JSON object — no prose, ` +
      `no markdown, no code fences.`,
    `3. Submit it. Write your JSON object to ${outDir}/out-<job_id>.json using the ` +
      `Write tool (substitute the real job_id), then run this EXACT Bash command:`,
    `     ${py} ${job} result --job <job_id> --result-file ${outDir}/out-<job_id>.json --state-dir ${sd}`,
    `   If it reports the result was rejected (bad JSON / missing keys), fix your JSON ` +
      `and submit again — at most twice. If it still fails, run ` +
      `\`${py} ${job} result --job <job_id> --error --state-dir ${sd}\` (type a one-line ` +
      `reason, then Ctrl-D) and STOP.`,
    `4. Report in ONE short line what you did, then STOP. Do NOT claim another job, ` +
      `do NOT loop, do NOT read other files, do NOT call any other tool.`,
    ``,
    `HARD RULES: ONLY the Bash tool (to run claude_job.py) and the Write tool (to ` +
      `write the result file). NEVER run any other shell command. NEVER edit, post, ` +
      `or touch anything else. An empty queue is the NORMAL, expected case most ` +
      `minutes — it is success, not a problem to debug.`,
  ].join("\n");
}

// Full canonical SKILL.md (frontmatter + body + version marker) the MCP writes
// to keep the task current. queueWorkerBody() is what the agent passes to
// create_scheduled_task at onboarding (already complete + correct, baked paths);
// this wrapper just adds the frontmatter + marker the refresh-on-boot gate reads.
function queueWorkerSkillMd(spec: { taskId: string; queueType: string; human: string }): string {
  return (
    `---\n` +
    `name: ${spec.taskId}\n` +
    `description: S4L ${spec.human} queue worker — claims one ${spec.queueType} job ` +
    `from the local pipeline queue, drafts it, writes the result back. Never posts.\n` +
    `---\n\n` +
    queueWorkerBody(spec) +
    `\n\n<!-- ${QUEUE_WORKER_PROMPT_MARKER}: ${QUEUE_WORKER_PROMPT_VERSION} -->\n`
  );
}

// Refresh each worker task's SKILL.md when this build ships a newer prompt than
// what's on disk. Mirrors ensureAutopilotPromptCurrent: best-effort, only touches
// an EXISTING task (onboarding creates them), only when stale. Also rewrites when
// the baked-in paths (python/repo) would have changed, since a stale absolute
// path would break the Bash commands; we detect that by always rewriting on a
// version bump and trust the version gate otherwise.
function ensureQueueWorkerPromptsCurrent(): void {
  for (const spec of QUEUE_WORKERS) {
    try {
      const skillPath = scheduledTaskSkillPath(spec.taskId);
      if (!fs.existsSync(skillPath)) continue; // task not created yet
      const cur = fs.readFileSync(skillPath, "utf-8");
      const m = new RegExp(`${QUEUE_WORKER_PROMPT_MARKER}:\\s*(\\d+)`).exec(cur);
      const curVer = m ? parseInt(m[1], 10) : 0;
      if (curVer >= QUEUE_WORKER_PROMPT_VERSION) continue;
      fs.writeFileSync(skillPath, queueWorkerSkillMd(spec), "utf-8");
      console.error(
        `[queue-worker] refreshed ${spec.taskId} prompt -> v${QUEUE_WORKER_PROMPT_VERSION} (was v${curVer})`
      );
    } catch (e: any) {
      console.error(`[queue-worker] ensure ${spec.taskId} prompt error: ${e?.message || e}`);
    }
  }
}

// ---- Pre-approve tools for the unattended scheduled tasks --------------------
// Scheduled tasks default to "Ask" mode; an un-pre-approved tool STALLS forever
// (no human to click allow). settings.json allow-rules DO apply to scheduled-task
// sessions. Per the user's directive, pre-approve GENEROUSLY so a worker never
// wedges even if it reaches for something unexpected: the exact claude_job.py
// command, python broadly, the file tools it legitimately uses, and this server's
// own tools. Allow-only + merge-in-place; never clobbers a user's settings.
function queueWorkerAllowedTools(): string[] {
  const job = path.join(repoDir(), "scripts", "claude_job.py");
  return [
    // The worker's real commands (tightest match first).
    `Bash(${resolvePython()} ${job}:*)`,
    `Bash(python3 ${job}:*)`,
    `Bash(${job}:*)`,
    // Broad-but-scoped fallbacks so an unexpected phrasing still doesn't stall.
    "Bash(python3:*)",
    "Bash(python:*)",
    // File tools the worker uses (Write) + ones it might reach for without stalling.
    "Write",
    "Read",
    "Edit",
    "Glob",
    "Grep",
    // This server's tools, both namespaces (manifest name + protocol name).
    "mcp__social-autoposter__scan_candidates",
    "mcp__social-autoposter__submit_drafts",
    "mcp__social-autoposter__post_drafts",
    "mcp__social-autoposter__project_config",
    "mcp__social-autoposter__get_stats",
    "mcp__social-autoposter__dashboard",
    "mcp__S4L__scan_candidates",
    "mcp__S4L__submit_drafts",
    "mcp__S4L__post_drafts",
    "mcp__S4L__project_config",
    "mcp__S4L__get_stats",
    "mcp__S4L__dashboard",
  ];
}

// Merge a list of allow-rules into ~/.claude/settings.json. Returns count added.
// Shared by the autopilot + queue-worker pre-approvers. Never throws.
function mergeSettingsAllow(tools: string[]): number {
  try {
    const cfg = process.env.CLAUDE_CONFIG_DIR || path.join(os.homedir(), ".claude");
    const settingsPath = path.join(cfg, "settings.json");
    let settings: any = {};
    if (fs.existsSync(settingsPath)) {
      try {
        settings = JSON.parse(fs.readFileSync(settingsPath, "utf-8")) || {};
      } catch (e: any) {
        console.error(`[pre-approve] settings.json unparseable; skipping: ${e?.message || e}`);
        return 0;
      }
    }
    if (typeof settings !== "object" || Array.isArray(settings)) return 0;
    const perms = (settings.permissions ??= {});
    if (typeof perms !== "object" || Array.isArray(perms)) return 0;
    const allow: string[] = Array.isArray(perms.allow) ? perms.allow : (perms.allow = []);
    let added = 0;
    for (const t of tools) {
      if (!allow.includes(t)) {
        allow.push(t);
        added++;
      }
    }
    if (added === 0) return 0;
    fs.mkdirSync(cfg, { recursive: true });
    fs.writeFileSync(settingsPath, JSON.stringify(settings, null, 2) + "\n", "utf-8");
    return added;
  } catch (e: any) {
    console.error(`[pre-approve] mergeSettingsAllow error: ${e?.message || e}`);
    return 0;
  }
}

// Pre-approve the worker tools EAGERLY — NOT gated on a task existing — so the
// settings are already in place before onboarding even creates the tasks, and
// the very first unattended fire can never stall. Allow-only, idempotent.
function ensureQueueWorkerToolsAllowed(): void {
  const added = mergeSettingsAllow(queueWorkerAllowedTools());
  if (added > 0) {
    console.error(`[queue-worker] pre-approved ${added} tool rule(s) in settings.json (allow-only)`);
  }
}

// ---- launchd kicker: run the REAL pipeline in DRAFT_ONLY + queue mode --------
// Reinstates com.m13v.social-twitter-cycle as the customer-box kicker. It runs
// run-twitter-cycle.sh straight through (scan -> score -> draft -> link-gen) but
// STOPS before posting (DRAFT_ONLY=1), writing the plan to the review-queue the
// approval cards read. Its `claude -p` steps route through the job queue
// (SAPS_CLAUDE_PROVIDER=queue) for the scheduled-task workers to service.
// link_tail is skipped for now (TWITTER_TAIL_LINK_RATE=0); the short link is
// still baked by twitter_gen_links.py (pure Python).
const QUEUE_KICKER_INTERVAL_SECS = 300; // a fresh draft cycle every 5 min

function kickerEnv(): Record<string, string> {
  return {
    DRAFT_ONLY: "1",
    SAPS_CLAUDE_PROVIDER: "queue",
    SAPS_STATE_DIR: sapsStateDir(),
    TWITTER_TAIL_LINK_RATE: "0",
    TWITTER_PAGE_GEN_RATE: "0",
  };
}

async function ensureQueueKickerInstalled(): Promise<{ ok: boolean; detail: string }> {
  try {
    if (process.platform !== "darwin") return { ok: false, detail: "not macOS" };
    if (!runtimeReady()) return { ok: false, detail: "runtime not ready" };
    const anyReady = listManagedProjectStatus().some((p) => p.ready);
    if (!anyReady) return { ok: false, detail: "no configured project yet" };
    const logDir = path.join(repoDir(), "skill", "logs");
    try {
      fs.mkdirSync(logDir, { recursive: true });
    } catch {
      /* best-effort */
    }
    const xml = plistXml({
      label: TWITTER_AUTOPILOT_LABEL,
      programArgs: ["bash", path.join(repoDir(), "skill", "run-twitter-cycle.sh")],
      intervalSecs: QUEUE_KICKER_INTERVAL_SECS,
      runAtLoad: false, // don't fire a heavy cycle the instant Claude launches
      stdoutLog: path.join(logDir, "launchd-twitter-cycle-stdout.log"),
      stderrLog: path.join(logDir, "launchd-twitter-cycle-stderr.log"),
      extraEnv: kickerEnv(),
    });
    const created = ensurePlist(TWITTER_AUTOPILOT_PLIST, xml);
    const uid = process.getuid ? process.getuid() : 0;
    const res = await loadPlist(TWITTER_AUTOPILOT_LABEL, TWITTER_AUTOPILOT_PLIST, uid);
    // bootstrap returns non-zero when already loaded; that's fine (idempotent).
    const detail = created ? "installed + loaded" : `present (load rc=${res.code})`;
    return { ok: true, detail };
  } catch (e: any) {
    return { ok: false, detail: e?.message || String(e) };
  }
}

// Assemble everything the panel needs in one shot (projects + X + autopilot +
// version). Resilient: any probe that throws degrades to a safe default rather
// than failing the whole snapshot.
async function buildSnapshot() {
  const projects = listManagedProjectStatus().map((p) => ({
    name: p.name,
    ready: p.ready,
    missing_required: p.missing_required,
  }));
  const rtReady = runtimeReady();
  const [x, ap, ver] = await Promise.all([
    rtReady
      ? xStatus().catch(() => ({ connected: false, state: "" }) as any)
      : Promise.resolve({ connected: false, state: "runtime_not_ready" } as any),
    autopilotLoaded(),
    versionStatus().catch(() => ({ installed: VERSION, latest: null, update_available: false }) as any),
  ]);
  await ensureDoctorPhase(x.connected ? "full" : "pre_connect");
  if (rtReady) completeOnboardingMilestone("runtime_ready");
  if (x.connected) {
    completeOnboardingMilestone("x_connected", { state: x.state || "connected" });
  }
  if (projects.some((project) => project.ready)) {
    completeOnboardingMilestone("project_ready", { missing_count: 0 });
  }
  return {
    projects,
    projects_total: projects.length,
    projects_ready: projects.filter((p) => p.ready).length,
    x_connected: !!x.connected,
    x_state: x.state || "",
    x_handle: x.handle ?? null,
    autopilot_on: ap.autopilot_on,
    auto_update_on: ap.auto_update_on,
    version: ver.installed || VERSION,
    latest_version: ver.latest ?? null,
    update_available: !!ver.update_available,
    // Runtime install gate: the panel shows the Install card (and disables the
    // action buttons) until the owned Python/Chromium runtime is provisioned.
    runtime_ready: rtReady,
    runtime_provisioning: isProvisioning(),
    onboarding: onboardingSnapshot(),
  };
}

// ---- dashboard localhost fallback -----------------------------------------
// When the connected host doesn't support MCP Apps UI (Claude Code / Cowork
// today), serve the SAME dist/panel.html from a loopback HTTP server. The page
// detects it's running over HTTP (window.__SAPS_BRIDGE__) and routes every
// app.callServerTool through POST /tool/<name>, which replays the exact captured
// handler in TOOL_HANDLERS. No pipeline or front-end logic is duplicated.

// True if the host advertised it can render our ui:// HTML resource inline.
function hostRendersAppUi(): boolean {
  try {
    const caps = (server.server.getClientCapabilities?.() ?? null) as any;
    const uiCap = getUiCapability(caps);
    return !!uiCap?.mimeTypes?.includes(RESOURCE_MIME_TYPE);
  } catch {
    return false;
  }
}

let localPanel: { url: string; server: http.Server } | null = null;

// Read the built panel.html and flip it into HTTP-bridge mode by injecting a
// flag the front-end reads at boot. Same bytes as the inline ui:// resource,
// minus the postMessage host (there's none over loopback).
function widgetHtmlForHttp(file: string): string {
  const html = fs.readFileSync(path.join(DIST_DIR, file), "utf-8");
  const inject = `<script>window.__SAPS_BRIDGE__=${JSON.stringify("http")};</script>`;
  if (html.includes("</head>")) return html.replace("</head>", inject + "</head>");
  return inject + html;
}
function panelHtmlForHttp(): string {
  return widgetHtmlForHttp("panel.html");
}

function readBody(req: http.IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    req.on("data", (c) => chunks.push(Buffer.from(c)));
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf-8")));
    req.on("error", reject);
  });
}

// Start (or reuse) the loopback HTTP server that serves the dashboard plus a
// /tool/<name> dispatch endpoint backed by TOOL_HANDLERS. Bound to 127.0.0.1 on
// an OS-assigned ephemeral port so nothing is exposed off-box.
function startLocalPanel(): Promise<string> {
  if (localPanel) return Promise.resolve(localPanel.url);
  return new Promise((resolve, reject) => {
    const srv = http.createServer(async (req, res) => {
      try {
        const url = new URL(req.url || "/", "http://127.0.0.1");
        if (
          req.method === "GET" &&
          (url.pathname === "/" || url.pathname === "/panel" || url.pathname === "/index.html")
        ) {
          res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
          res.end(panelHtmlForHttp());
          return;
        }
        if (
          req.method === "GET" &&
          (url.pathname === "/product-link" || url.pathname === "/product-link.html")
        ) {
          res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
          res.end(widgetHtmlForHttp("product-link.html"));
          return;
        }
        if (req.method === "GET" && url.pathname === "/health") {
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ ok: true }));
          return;
        }
        if (req.method === "POST" && url.pathname.startsWith("/tool/")) {
          const name = decodeURIComponent(url.pathname.slice("/tool/".length));
          const handler = TOOL_HANDLERS[name];
          if (!handler) {
            res.writeHead(404, { "Content-Type": "application/json" });
            res.end(
              JSON.stringify({ isError: true, content: [{ type: "text", text: `Unknown tool: ${name}` }] })
            );
            return;
          }
          const raw = await readBody(req);
          let args: any = {};
          if (raw.trim()) { try { args = JSON.parse(raw); } catch { args = {}; } }
          let result: any;
          try {
            result = await handler(args ?? {}, {});
          } catch (e: any) {
            result = { isError: true, content: [{ type: "text", text: String(e?.message || e) }] };
          }
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(JSON.stringify(result ?? {}));
          return;
        }
        res.writeHead(404, { "Content-Type": "text/plain" });
        res.end("not found");
      } catch (e: any) {
        try {
          res.writeHead(500, { "Content-Type": "text/plain" });
          res.end(String(e?.message || e));
        } catch { /* response already sent */ }
      }
    });
    srv.on("error", reject);
    // Optional fixed port (SAPS_PANEL_PORT) for deterministic addressing; default
    // is an OS-assigned ephemeral port.
    const wantPort = Number(process.env.SAPS_PANEL_PORT) || 0;
    srv.listen(wantPort, "127.0.0.1", () => {
      const addr = srv.address();
      const port = typeof addr === "object" && addr ? addr.port : 0;
      localPanel = { url: `http://127.0.0.1:${port}/`, server: srv };
      writePanelUrl(localPanel.url);
      resolve(localPanel.url);
    });
  });
}

// Publish the loopback URL to stable files so out-of-process readers can find
// the ephemeral port without scraping `lsof`:
//   - panel-url            plain text, for the Claude Code side-panel reverse proxy.
//   - panel-endpoint.json  richer (url + version + pid), for the menu bar app,
//                          which POSTs /tool/<name> here for live data.
// Best-effort: a write failure never blocks the panel (readers re-check /health).
function writePanelUrl(url: string): void {
  try {
    const dir = path.join(process.env.HOME || os.homedir(), ".social-autoposter-mcp");
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, "panel-url"), url, "utf-8");
    fs.writeFileSync(
      path.join(dir, "panel-endpoint.json"),
      JSON.stringify(
        { url, pid: process.pid, version: VERSION, started_at: new Date().toISOString() },
        null,
        2
      ) + "\n",
      "utf-8"
    );
  } catch (e: any) {
    console.error("[social-autoposter-mcp] writePanelUrl failed:", e?.message || e);
  }
}

// The owned state dir, honoring SAPS_STATE_DIR (matches menubar/s4l_state.py).
function sapsStateDir(): string {
  return (
    process.env.SAPS_STATE_DIR ||
    path.join(process.env.HOME || os.homedir(), ".social-autoposter-mcp")
  );
}

// ---- Cross-instance "posting active" flag ----------------------------------
// posting-active.json in the shared state dir is the CROSS-MCP-INSTANCE version
// of the in-process `postingActive` flag. The autopilot scan and the post
// sometimes run in the SAME MCP (the in-process flag covers that) and sometimes
// in TWO SEPARATE MCP instances (different agent sessions each spawn their own).
// A file every instance's scan_candidates reads makes the mutual exclusion hold
// regardless of which topology Claude Desktop happens to use. Heartbeat'd with a
// short TTL so a crashed poster's flag self-clears and never wedges scanning.
const POSTING_FLAG_TTL_MS = 45_000;
let postingFlagHeartbeat: ReturnType<typeof setInterval> | null = null;
function postingFlagPath(): string {
  return path.join(sapsStateDir(), "posting-active.json");
}
function writePostingFlag(): void {
  try {
    fs.mkdirSync(sapsStateDir(), { recursive: true });
    fs.writeFileSync(
      postingFlagPath(),
      JSON.stringify({ pid: process.pid, expires_at: Date.now() + POSTING_FLAG_TTL_MS }) + "\n",
      "utf-8"
    );
  } catch {
    /* best effort */
  }
}
function startPostingFlagHeartbeat(): void {
  writePostingFlag();
  if (postingFlagHeartbeat) return;
  // Refresh well within the TTL so a long batch stays flagged, but a dead poster
  // expires within POSTING_FLAG_TTL_MS.
  postingFlagHeartbeat = setInterval(() => {
    if (postingActive) writePostingFlag();
  }, Math.floor(POSTING_FLAG_TTL_MS / 2));
  if (typeof postingFlagHeartbeat.unref === "function") postingFlagHeartbeat.unref();
}
function stopPostingFlagHeartbeat(): void {
  if (postingFlagHeartbeat) {
    clearInterval(postingFlagHeartbeat);
    postingFlagHeartbeat = null;
  }
  try {
    fs.rmSync(postingFlagPath(), { force: true });
  } catch {
    /* best effort */
  }
}
// True when ANY MCP instance has a FRESH posting flag on disk. Absent or expired
// == not posting. This is what makes a sibling instance's scan_candidates defer.
function isPostingFlagFresh(): boolean {
  try {
    const j = JSON.parse(fs.readFileSync(postingFlagPath(), "utf-8"));
    return typeof j?.expires_at === "number" && j.expires_at > Date.now();
  } catch {
    return false;
  }
}

// activity.json: a tiny "what's running right now" signal the menu bar reads to
// show a loading spinner + label (scanning / drafting / posting / …). Written by
// long-running tools, cleared when they finish. Best-effort; absence == idle.
function writeActivity(state: string, label: string): void {
  try {
    const dir = sapsStateDir();
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(
      path.join(dir, "activity.json"),
      JSON.stringify({ state, label, since: new Date().toISOString() }) + "\n",
      "utf-8"
    );
  } catch {
    /* best effort: a status write must never break the work it's narrating */
  }
}
function clearActivity(): void {
  try {
    fs.rmSync(path.join(sapsStateDir(), "activity.json"), { force: true });
  } catch {
    /* best effort */
  }
}

// Signal the menu bar that a fresh draft batch is ready for pop-up review. The
// chat-table review path is unchanged and still works; this just ALSO lets the
// corner cards drive review (both surfaces de-dup via the plan's `posted` flag).
// The menu bar reads review-request.json, presents the cards, posts via the
// loopback post_drafts tool, then clears the file. Best-effort: a write failure
// just means no pop-ups this batch (chat review still works).
function writeReviewRequest(req: {
  batch_id: string;
  project: string;
  count: number;
  plan_path: string;
  created_at: string;
}): void {
  try {
    const dir = sapsStateDir();
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(
      path.join(dir, "review-request.json"),
      JSON.stringify(req, null, 2) + "\n",
      "utf-8"
    );
  } catch (e: any) {
    console.error("[social-autoposter-mcp] writeReviewRequest failed:", e?.message || e);
  }
}

// Open a URL in the user's default browser, cross-platform. Opening is OPT-IN:
// by default we do NOT pop a browser tab. The dashboard already surfaces in-host
// (MCP Apps inline) or via the Claude Code side panel / returned loopback URL, so
// auto-opening the OS browser on every dashboard call is unwanted noise. Set
// SAPS_PANEL_OPEN_BROWSER=1 to restore the old auto-open behavior. (The URL is
// always returned to the caller regardless, so nothing is lost when we don't open.)
async function openInBrowser(url: string): Promise<void> {
  if (!process.env.SAPS_PANEL_OPEN_BROWSER) return;
  const cmd =
    process.platform === "darwin" ? "open" : process.platform === "win32" ? "cmd" : "xdg-open";
  const args = process.platform === "win32" ? ["/c", "start", "", url] : [url];
  try {
    await run(cmd, args, { timeoutMs: 10_000 });
  } catch (e: any) {
    console.error("[social-autoposter-mcp] openInBrowser failed:", e?.message || e);
  }
}

// ---- Desktop-session autopilot: scan_candidates + submit_drafts ------------
// These two tools move the cycle's AI step OUT of `claude -p` and into the
// CALLING agent. Intended use: a Claude Desktop scheduled task (which runs on
// the user's OWN plan) calls scan_candidates, drafts each on-brand reply
// ITSELF, then submit_drafts hands the drafts to the SAME review plan the
// menu-bar approval UI and post_drafts already consume. Nothing posts until the
// user approves. The legacy launchd + `claude -p` autopilot is untouched and
// keeps running in parallel; this is an additive, opt-in second path.

interface ScanResult {
  batchId: string | null;
  candidates: ScanCandidate[];
  blocked?: string;
  // True when the scan couldn't run because ANOTHER scan holds the pipeline's
  // max=1 slot (e.g. the legacy claude -p autopilot). Contention, not an empty
  // result — the caller should retry rather than conclude "no candidates".
  busy?: boolean;
}

async function runScanCandidates(
  project?: string,
  onProgress?: (message: string, step: number) => void
): Promise<ScanResult> {
  // SCAN_ONLY=1: run scan -> score -> top-N select, then STOP before drafting.
  // No DRAFT_ONLY, no `claude -p` drafting. TWITTER_PHASE1_LLM_DRAFT=0 forces the
  // deterministic query bank so the whole scan is claude-free (the agent does ALL
  // the AI). Off-screen by default (no BH_WINDOW_* / overlay): a scheduled run has
  // no human watching the Chrome.
  const env: NodeJS.ProcessEnv = {
    SCAN_ONLY: "1",
    TWITTER_PHASE1_LLM_DRAFT: "0",
    SAPS_REPO_DIR: repoDir(),
    PATH: pipelinePath(),
  };
  if (project) env.SAPS_FORCE_PROJECT = project;
  const chrome = resolveChrome();
  if (chrome) env.BH_CHROME_BIN = chrome;
  let step = 0;
  let lastMsg = "";
  // Specific phase label for the menu bar for the whole multi-minute scan, so the
  // long-poll doesn't leave it on a generic "working" (or flicker to idle between
  // re-calls). Cleared by the handler when the scan resolves.
  writeActivity("scanning", project ? `scanning X for ${project}` : "scanning X");
  // Single-flight: kill any pre-existing run-twitter-cycle.sh (zombies that
  // survived a prior preempt, or stale waiters parked behind a post) before
  // launching, so only ONE scan ever exists. The plist gets this from
  // run-twitter-cycle-singleton.sh; the MCP's direct launch must enforce it here.
  sigkillAllScans();
  const res = await run("bash", ["skill/run-twitter-cycle.sh"], {
    env,
    timeoutMs: 900_000,
    onSpawn: (c) => {
      // Track the PLUGIN's own scan process so an approved post can abort exactly
      // this scan (posting preempts scanning). This is the plugin pipeline only —
      // the plist autopilot's scan is a separate launchd process the MCP server
      // never spawns and has no handle to, so it can never be touched here.
      scanChild = c;
    },
    onLine: (line) => {
      const t = line.replace(/\s+$/, "");
      if (t.trim()) console.error(`[scan_candidates] ${t}`);
      if (!onProgress) return;
      const msg = cycleProgressMessage(t);
      if (msg && msg !== lastMsg) {
        lastMsg = msg;
        onProgress(msg, ++step);
      }
    },
  });
  scanChild = null; // scan finished on its own; nothing to preempt.
  const marker = /SCAN_ONLY_RESULT=(\/\S+\.json)/.exec(res.stdout + "\n" + res.stderr);
  if (marker && marker[1]) {
    try {
      const data = JSON.parse(fs.readFileSync(marker[1], "utf-8"));
      return {
        batchId: data.batch_id ?? null,
        candidates: (data.candidates ?? []) as ScanCandidate[],
      };
    } catch (e: any) {
      return {
        batchId: null,
        candidates: [],
        blocked: `Scan finished but its result file was unreadable: ${e?.message || e}`,
      };
    }
  }
  // If query-gen (when enabled) hits a real failure, the cycle still emits
  // DRAFT_ONLY_BLOCKED=<reason>. Surface it rather than "no candidates".
  const blockedMarker = /DRAFT_ONLY_BLOCKED=([a-z0-9_]+)/.exec(res.stdout + "\n" + res.stderr);
  if (blockedMarker && blockedMarker[1]) {
    return { batchId: null, candidates: [], blocked: blockedReasonMessage(blockedMarker[1]) };
  }
  // The pipeline's single-flight guard prints too_many_inflight and exits 0 when
  // ANOTHER scan already holds the twitter-cycle slot (commonly the legacy
  // claude -p autopilot, which we deliberately keep running). That is CONTENTION,
  // not an empty batch — flag it so the caller retries instead of concluding
  // "no candidates" (a clean empty scan returns a batch_id with count:0).
  if (/too_many_inflight/.test(res.stdout + res.stderr)) {
    return {
      batchId: null,
      candidates: [],
      busy: true,
      blocked:
        "Another scan already holds the pipeline slot (likely the legacy autopilot). Contention, " +
        "not an empty result — retry scan_candidates shortly.",
    };
  }
  return {
    batchId: null,
    candidates: [],
    blocked:
      `This scan produced no candidates (exit ${res.code}). Usually a cold-start ` +
      `project with no seeded search topics, or nothing fresh on-theme right now. Tail:\n` +
      res.stderr.split("\n").slice(-12).join("\n"),
  };
}

// ---- Long-poll job registry for scan_candidates ----------------------------
// runScanCandidates drives run-twitter-cycle.sh (SCAN_ONLY), which runs a real
// browser for 1-3 minutes — longer than the MCP client's ~60s request timeout.
// Awaiting the whole scan in one tool call therefore ALWAYS tripped the client
// timeout (-32001 Request timed out), and the agent's retry started a SECOND
// run-twitter-cycle.sh that collided with the pipeline's max=1 single-flight lock
// (too_many_inflight) and came back as a false "no candidates". Instead we run
// ONE scan as a shared background job and LONG-POLL it: each scan_candidates call
// waits up to SCAN_POLL_WAIT_MS (kept under the client timeout) for the in-flight
// job to finish, then returns either the candidates or a "scan in progress"
// status. The agent simply re-calls scan_candidates until it returns — no client
// timeouts, no second scan racing the lock, and no sleeping/background waits.
interface ScanJob {
  project?: string;
  startedAt: number;
  done: boolean;
  promise: Promise<ScanResult>;
  result?: ScanResult;
}

// At most one scan runs at a time (mirrors the pipeline's own max=1 lock). Kept
// across calls so a later poll attaches to the SAME running scan instead of
// starting a new one.
let currentScanJob: ScanJob | null = null;

// The PLUGIN's currently-spawned scan subprocess (captured via run()'s onSpawn),
// so an approved post can abort exactly this scan and take the browser
// immediately. Only ever references a scan the MCP server itself launched — never
// the plist autopilot's launchd scan.
let scanChild: ChildProcess | null = null;

// ---- Cross-process browser-lock bridge (the REAL posting-priority fix) ------
// The SCANNER (run-twitter-cycle.sh) serializes browser access on a mkdir-based
// DIRECTORY lock at /tmp/social-autoposter-twitter-browser.lock (skill/lock.sh).
// The POSTER (twitter_post_plan.py / twitter_browser.py) serializes on a totally
// SEPARATE json file lock (~/.claude/twitter-browser-lock.json) with role:"post"
// preemption. The two locks never reference each other, so a post launched from
// THIS MCP (or a sibling MCP instance — every autopilot agent session spawns its
// own) never actually excluded a live scan: both held "their" lock and drove the
// one shared harness Chrome at once, so an approved batch landed 0/N while a scan
// churned 118 queries for ~10min (proven live on the remote box 2026-06-23:
// /tmp lock pid=scanner AND json lock python:poster role=post, simultaneously).
//
// preemptScanForPost's old body only killed scanChild — a process-LOCAL handle to
// a scan THIS instance launched. The scan that actually holds the browser is
// almost always a SIBLING instance's, which we have no ChildProcess for. So we
// bridge to the lock the scanner truly respects: read its /tmp pid file, and if a
// live run-twitter-cycle.sh holds it, signal it cross-process. Then the post
// HOLDS that same /tmp lock for the whole batch so the every-minute autopilot
// scan queues behind us (its acquire_lock waits on our live pid) instead of
// seizing Chrome mid-post. skill/lock.sh's ownership guard + kill-0 liveness +
// 3h stale-reclaim recover the dir if we ever leak it. Never touches a locked
// pipeline script or the python json lock.
const TW_BROWSER_LOCK_DIR = "/tmp/social-autoposter-twitter-browser.lock";

function shellLockHolderPid(): number | null {
  try {
    const pid = parseInt(
      fs.readFileSync(path.join(TW_BROWSER_LOCK_DIR, "pid"), "utf-8").trim(),
      10
    );
    return Number.isFinite(pid) && pid > 0 ? pid : null;
  } catch {
    return null; // no dir / no pid file == lock is free
  }
}
function pidAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}
// True ONLY when pid is a run-twitter-cycle.sh scan — the one holder a post is
// allowed to preempt. Never preempt another poster or an unknown holder.
function pidIsScan(pid: number): boolean {
  try {
    const cmd = execFileSync("ps", ["-o", "command=", "-p", String(pid)], {
      encoding: "utf-8",
      timeout: 4000,
    });
    return /run-twitter-cycle\.sh/.test(cmd);
  } catch {
    return false;
  }
}
function rmShellLockDir(): void {
  try {
    fs.rmSync(TW_BROWSER_LOCK_DIR, { recursive: true, force: true });
  } catch {
    /* best effort */
  }
}
const sleepMs = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

// SIGKILL a scan's WHOLE process tree (the bash + its browser-harness/tee
// children). run-twitter-cycle.sh traps SIGTERM/INT/HUP (skill/lock.sh installs
// `trap _sa_release_locks ... TERM`), so a SIGTERM runs the cleanup handler and
// the script KEEPS GOING — the scan never dies, still drives Chrome, and the next
// autopilot tick stacks another on top (the zombie pileup that stale-reclaimed the
// lock mid-post). SIGKILL can't be trapped. Kill children first so the harness CDP
// driver lets go of Chrome immediately.
function sigkillScanTree(pid: number): void {
  try {
    const out = execFileSync("pgrep", ["-P", String(pid)], { encoding: "utf-8", timeout: 4000 });
    for (const cstr of out.split(/\s+/)) {
      const c = parseInt(cstr, 10);
      if (Number.isFinite(c) && c > 0) {
        try {
          process.kill(c, "SIGKILL");
        } catch {
          /* gone */
        }
      }
    }
  } catch {
    /* no children / pgrep unavailable */
  }
  try {
    process.kill(pid, "SIGKILL");
  } catch {
    /* gone */
  }
}

// Single-flight: SIGKILL every run-twitter-cycle.sh on the box before launching a
// fresh scan, so a zombie that survived a prior SIGTERM (or a stale waiter parked
// behind a post) can never accumulate. Mirrors the plist's run-twitter-cycle-
// singleton.sh "one cycle at a time" guarantee, which the MCP's direct launch
// bypassed. Best-effort; never throws.
function sigkillAllScans(): void {
  try {
    const out = execFileSync("pgrep", ["-f", "skill/run-twitter-cycle.sh"], {
      encoding: "utf-8",
      timeout: 4000,
    });
    for (const pstr of out.split(/\s+/)) {
      const p = parseInt(pstr, 10);
      if (Number.isFinite(p) && p > 0) sigkillScanTree(p);
    }
  } catch {
    /* none running */
  }
}

// ---- Lock grace-hold: hold the /tmp lock CONTINUOUSLY across per-card posts ----
// The plist pipeline acquires the browser lock ONCE and holds it through the whole
// posting phase. The MCP posts per approved card (separate post_drafts calls), and
// the old code acquired+released the lock PER CARD — leaving a release window
// BETWEEN every card that a parked scan stale-reclaimed (the hijack). Instead we
// keep the lock and only release it after SHELL_LOCK_GRACE_MS of no posting, so the
// hold EXPANDS as more cards get approved and there is never a gap between cards.
const SHELL_LOCK_GRACE_MS = Number(process.env.SAPS_POST_LOCK_GRACE_MS) || 60_000;
let shellLockReleaseTimer: ReturnType<typeof setTimeout> | null = null;
// True from the start of a post batch until SHELL_LOCK_GRACE_MS after the last
// card. scan_candidates checks this and DEFERS launching a scan while it's set —
// the real fix: posting and scanning are mutually exclusive at the SOURCE (both
// are children of THIS one MCP), so we never even launch a scan that would race
// the post for the browser lock. Having the post fight scans for the lock (the
// prior approach) lost the race because the autopilot relaunches scans faster
// than the post can hold the dir. Reset is guaranteed by the grace timer below,
// so it can never wedge scanning permanently.
let postingActive = false;
function cancelScheduledShellLockRelease(): void {
  if (shellLockReleaseTimer) {
    clearTimeout(shellLockReleaseTimer);
    shellLockReleaseTimer = null;
  }
}
function scheduleShellLockRelease(): void {
  cancelScheduledShellLockRelease();
  shellLockReleaseTimer = setTimeout(() => {
    shellLockReleaseTimer = null;
    postingActive = false; // posting drained -> the autopilot may scan again
    stopPostingFlagHeartbeat(); // clear the cross-instance flag too
    releaseShellBrowserLock();
  }, SHELL_LOCK_GRACE_MS);
}

// SIGKILL a live scan holding the shell browser lock so the post takes the browser
// at once. Best-effort; only ever targets a run-twitter-cycle.sh.
function preemptScanHoldingBrowser(): void {
  try {
    const pid = shellLockHolderPid();
    if (pid && pidAlive(pid) && pidIsScan(pid)) {
      console.error(
        `[post] preempting cross-process scan holding the twitter-browser lock (pid ${pid}) — SIGKILL tree`
      );
      sigkillScanTree(pid);
    }
  } catch {
    /* best effort */
  }
}

// Take (or extend) the shell browser lock for the batch. Preempts a scan holder
// with SIGKILL; never steals from a live non-scan holder (a peer poster) — there
// it returns false and posting proceeds unguarded (no worse than before).
async function acquireShellBrowserLock(): Promise<boolean> {
  // A new post cancels any pending grace-release and EXTENDS the existing hold.
  cancelScheduledShellLockRelease();
  // Already ours? Refresh the pid + expiry and keep holding — this is the "expand
  // the lock as more cards get approved" path: consecutive per-card posts reuse
  // ONE continuous hold instead of churning the lock, which is what left a window
  // a parked scan stale-reclaimed between cards.
  if (shellLockHolderPid() === process.pid) {
    try {
      fs.writeFileSync(path.join(TW_BROWSER_LOCK_DIR, "pid"), String(process.pid));
      fs.writeFileSync(
        path.join(TW_BROWSER_LOCK_DIR, "expires_at"),
        String(Math.floor(Date.now() / 1000) + 1800)
      );
    } catch {
      /* best effort */
    }
    return true;
  }
  for (let attempt = 0; attempt < 8; attempt++) {
    try {
      fs.mkdirSync(TW_BROWSER_LOCK_DIR); // atomic mutex — only one winner
      // Write the pid IMMEDIATELY (sync) so the dir is never observably pid-less.
      fs.writeFileSync(path.join(TW_BROWSER_LOCK_DIR, "pid"), String(process.pid));
      fs.writeFileSync(
        path.join(TW_BROWSER_LOCK_DIR, "expires_at"),
        String(Math.floor(Date.now() / 1000) + 1800)
      );
      console.error(
        `[post] holding twitter-browser shell lock pid=${process.pid} — scans queue behind the post`
      );
      return true;
    } catch {
      // Dir exists. Reclaim if the holder is dead; SIGKILL-preempt if it's a scan;
      // otherwise (a live peer poster) leave it and post unguarded.
      const pid = shellLockHolderPid();
      if (!pid || !pidAlive(pid)) {
        rmShellLockDir();
      } else if (pidIsScan(pid)) {
        sigkillScanTree(pid); // SIGKILL — scans trap SIGTERM and survive it
        await sleepMs(300);
        rmShellLockDir();
      } else {
        return false; // a real peer holds it — don't steal; proceed
      }
      await sleepMs(200);
    }
  }
  return false;
}

// Release only if it's still OURS (mirror skill/lock.sh's ownership guard) so we
// never wipe a scan that legitimately re-acquired after the batch finished.
function releaseShellBrowserLock(): void {
  try {
    if (shellLockHolderPid() === process.pid) {
      rmShellLockDir();
      console.error(`[post] released twitter-browser shell lock pid=${process.pid}`);
    }
  } catch {
    /* best effort */
  }
}

// Posting takes priority over scanning. When the user approves a post, abort any
// in-flight scan so the browser frees up at once. Two layers: (1) kill scanChild
// if THIS instance launched the scan, and (2) cross-process — kill whoever holds
// the /tmp shell lock if it's a scan (covers sibling MCP instances we have no
// handle to). Best-effort; never throws; never touches a locked pipeline script.
function preemptScanForPost(): void {
  try {
    if (scanChild && scanChild.pid && scanChild.exitCode === null) {
      console.error(
        `[post] preempting in-flight plugin scan (pid ${scanChild.pid}) so the approved post takes the browser — SIGKILL tree`
      );
      sigkillScanTree(scanChild.pid); // SIGKILL — scan bash traps SIGTERM
    }
  } catch {
    /* best effort */
  }
  // Drop the long-poll job so a queued poller stops waiting on the aborted scan;
  // the next scan_candidates call starts fresh.
  currentScanJob = null;
  scanChild = null;
  // Cross-process: the scan that actually holds the shared Chrome is usually a
  // sibling MCP instance's, not ours. Kill it via the lock it truly respects.
  preemptScanHoldingBrowser();
}

// Per-call long-poll window. Must stay under the ~60s MCP client request timeout
// so every scan_candidates call returns a real response before the client gives
// up. Override with SAPS_SCAN_POLL_WAIT_MS for non-standard clients.
const SCAN_POLL_WAIT_MS = Number(process.env.SAPS_SCAN_POLL_WAIT_MS) || 45_000;

// After a contention hit (too_many_inflight), wait this long before launching a
// fresh scan, so polls don't hammer the pipeline with back-to-back no-op scans
// while the other holder finishes. Polls during the cooldown just report "busy".
const SCAN_BUSY_COOLDOWN_MS = Number(process.env.SAPS_SCAN_BUSY_COOLDOWN_MS) || 20_000;
let scanBusyUntil = 0;

function scanBusy() {
  return textContent(
    "Another scan is already running on this machine (likely the legacy autopilot holding the " +
      "twitter-cycle slot). This is CONTENTION, not an empty result. Call scan_candidates again in " +
      "~20s to retry — do NOT conclude there are no candidates, and do NOT sleep; just re-call."
  );
}

// A post batch is in progress: scanning is deferred so posting has exclusive use
// of the one shared browser (mutual exclusion at the source). Not an error.
function scanDeferredForPost() {
  return textContent(
    "A post is in progress on this machine, so scanning is deferred — only one task drives X at a " +
      "time and posting has priority. This is EXPECTED, not an error or an empty result. Call " +
      "scan_candidates again shortly; it runs as soon as the current post batch finishes. Do NOT " +
      "sleep or conclude there are no candidates; just re-call."
  );
}

// Resolve to {done:true,value} the moment `p` settles, or {done:false} after
// `ms` — whichever is first. The job promise keeps running either way, so a
// later poll re-attaches to it.
function waitUpTo<T>(
  p: Promise<T>,
  ms: number
): Promise<{ done: true; value: T } | { done: false }> {
  return Promise.race([
    p.then((value) => ({ done: true as const, value })),
    new Promise<{ done: false }>((resolve) => setTimeout(() => resolve({ done: false }), ms)),
  ]);
}

function startScanJob(project?: string): ScanJob {
  const job: ScanJob = {
    project,
    startedAt: Date.now(),
    done: false,
    promise: undefined as unknown as Promise<ScanResult>,
  };
  job.promise = runScanCandidates(project)
    .then((r) => {
      job.result = r;
      job.done = true;
      return r;
    })
    .catch((e: any) => {
      const r: ScanResult = {
        batchId: null,
        candidates: [],
        blocked:
          `The scan crashed before producing a result: ${String(e?.message || e)}. ` +
          `Call scan_candidates again to retry.`,
      };
      job.result = r;
      job.done = true;
      return r;
    });
  return job;
}

// Read the on-brand drafting guidance for the given projects from config.json so
// scan_candidates can hand it to the agent INLINE. Without this the autopilot run
// has to read the config itself (project_config returns status, not the voice
// fields) — and a headless run improvises Bash/Read for that, hitting an
// un-approved tool and hanging. Returning the voice here removes the need to read
// any file. Best-effort: a missing/unreadable config yields {} and the agent
// drafts from thread context, same as before.
function readProjectVoices(projectNames: string[]): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  const want = new Set(projectNames.filter(Boolean));
  if (!want.size) return out;
  try {
    const cfg = JSON.parse(fs.readFileSync(path.join(repoDir(), "config.json"), "utf-8"));
    const projects: any[] = Array.isArray(cfg?.projects) ? cfg.projects : [];
    for (const p of projects) {
      if (!p?.name || !want.has(p.name)) continue;
      // Only the fields needed to draft on-brand — keep it lean.
      out[p.name] = {
        voice: p.voice,
        voice_relationship: p.voice_relationship,
        description: p.description,
        differentiator: p.differentiator,
        content_guardrails: p.content_guardrails,
        website: p.website,
        get_started_link: p.get_started_link,
      };
    }
  } catch {
    /* best effort — no inline voice, agent falls back to thread context */
  }
  return out;
}

function formatScanResult(scan: ScanResult) {
  if (!scan.batchId) {
    return textContent(scan.blocked || "No candidates found.");
  }
  // The brand voice for every project in this batch, INLINE, so the agent never
  // has to read a config file (that improvisation is what hangs headless runs).
  const projects = Array.from(
    new Set(scan.candidates.map((c) => c.matched_project).filter(Boolean) as string[])
  );
  const project_voices = readProjectVoices(projects);
  return jsonContent({
    batch_id: scan.batchId,
    count: scan.candidates.length,
    candidates: scan.candidates,
    project_voices,
    next_step:
      `Draft an on-brand reply (<=250 chars, match the thread's language) for each candidate you ` +
      `judge genuinely worth engaging; skip the rest. The brand voice + guardrails for each ` +
      `candidate's project are in project_voices[<matched_project>] (voice, description, ` +
      `differentiator, content_guardrails) — draft from THAT. Do NOT read config files or call any ` +
      `other tool to find the voice; everything you need is in this result. Then call submit_drafts ` +
      `with batch_id "${scan.batchId}" and one entry per drafted reply ({candidate_id, reply_text}). ` +
      `Nothing posts until the user approves.`,
  });
}

function scanInProgress(job: ScanJob) {
  const elapsed = Math.round((Date.now() - job.startedAt) / 1000);
  return textContent(
    `Scan in progress (${elapsed}s elapsed). The X scan drives a real browser and usually takes ` +
      `1-3 minutes. Call scan_candidates again RIGHT NOW to keep waiting — each call long-polls up ` +
      `to ~${Math.round(SCAN_POLL_WAIT_MS / 1000)}s and returns the candidates the moment the scan ` +
      `finishes. Do NOT sleep, run a background command, or do other work to bridge the gap; just ` +
      `re-call scan_candidates until it returns candidates or reports no candidates.`
  );
}

tool(
  "scan_candidates",
  {
    title: "Scan X for reply candidates (no drafting, no posting)",
    description:
      "Step 1 of a hands-free / scheduled autopilot run. Runs the scan+score half of the pipeline " +
      "and returns the top scored X/Twitter threads worth replying to — WITHOUT drafting or posting, " +
      "and without spending any `claude -p` budget. You (this session) then draft an on-brand reply " +
      "for each good candidate YOURSELF and submit them with `submit_drafts`. Each candidate includes " +
      "its candidate_id (pass it back), the thread text/author, the matched project, and engagement " +
      "metrics. The result ALSO includes `project_voices` — the brand voice + guardrails for each " +
      "project in the batch — so you draft on-brand WITHOUT reading config files or calling other " +
      "tools (do not improvise a config read). Optional `project` scopes the scan to one configured " +
      "project. The scan drives a real " +
      "browser and can take 1-3 minutes: this call long-polls and may return a `Scan in progress` " +
      "status instead of candidates — if so, just call scan_candidates again (same args) and keep " +
      "re-calling until it returns candidates. Never sleep or use a background wait to bridge the gap.",
    inputSchema: { project: z.string().optional() },
  },
  async (args: { project?: string }) => {
    // MUTUAL EXCLUSION (the real hijack fix): while a post batch is in progress
    // (or its grace-hold is active), DEFER scanning entirely rather than launching
    // a scan that races the post for the one shared browser. Both the scan and the
    // post are children of THIS MCP, so this in-process gate is exact — the
    // autopilot never even starts a scan to interrupt a post. Posting always wins;
    // the autopilot just re-calls and runs once the batch drains.
    if (postingActive || isPostingFlagFresh()) return scanDeferredForPost();
    // Long-poll the single in-flight scan job (see the ScanJob registry above).
    // A finished-but-unconsumed job: hand back its result and clear the slot so a
    // later call starts a fresh scan.
    if (currentScanJob?.done) {
      const finished = currentScanJob;
      currentScanJob = null;
      if (finished.result?.busy) {
        scanBusyUntil = Date.now() + SCAN_BUSY_COOLDOWN_MS;
        return scanBusy();
      }
      return formatScanResult(finished.result!);
    }
    // Contention cooldown: another scan holds the slot — report busy without
    // launching yet another no-op scan against the locked pipeline.
    if (!currentScanJob && Date.now() < scanBusyUntil) {
      return scanBusy();
    }
    // Nothing running -> start one scan (single-flight: never spawn a second
    // run-twitter-cycle.sh that would just hit the pipeline's too_many_inflight).
    if (!currentScanJob) {
      currentScanJob = startScanJob(args?.project);
    }
    // Wait for the in-flight scan, but no longer than the client timeout allows.
    const waited = await waitUpTo(currentScanJob.promise, SCAN_POLL_WAIT_MS);
    if (waited.done) {
      currentScanJob = null;
      if (waited.value.busy) {
        scanBusyUntil = Date.now() + SCAN_BUSY_COOLDOWN_MS;
        return scanBusy();
      }
      return formatScanResult(waited.value);
    }
    // Still scanning — tell the agent to simply call scan_candidates again.
    return scanInProgress(currentScanJob);
  }
);

tool(
  "submit_drafts",
  {
    title: "Submit drafted replies for review",
    description:
      "Step 2 of a hands-free / scheduled autopilot run. Hand back the replies you drafted for the " +
      "candidates returned by scan_candidates. Writes them into the SAME review plan the menu-bar " +
      "approval UI and post_drafts already use — nothing is posted until the user approves. Provide " +
      "batch_id (from scan_candidates) and a `drafts` array; each entry needs candidate_id and " +
      "reply_text (engagement_style, language, link_keyword optional).",
    inputSchema: {
      batch_id: z.string(),
      drafts: z
        .array(
          z.object({
            candidate_id: z.union([z.string(), z.number()]),
            reply_text: z.string(),
            engagement_style: z.string().optional(),
            language: z.string().optional(),
            link_keyword: z.string().optional(),
          })
        )
        .min(1),
    },
  },
  async (args: {
    batch_id: string;
    drafts: Array<{
      candidate_id: string | number;
      reply_text: string;
      engagement_style?: string;
      language?: string;
      link_keyword?: string;
    }>;
  }) => {
    // Reload the scan candidates for thread metadata (url / author / text /
    // project / topic). If the scan file is gone (e.g. /tmp cleared), we still
    // build a plan from the drafts alone; the review cards just lack context.
    const scanById = new Map<string, ScanCandidate>();
    try {
      const raw = JSON.parse(fs.readFileSync(scanResultPath(args.batch_id), "utf-8"));
      for (const c of (raw.candidates ?? []) as ScanCandidate[]) {
        scanById.set(String(c.id), c);
      }
    } catch {
      /* scan file missing — proceed with draft-only context */
    }
    const candidates: PlanCandidate[] = args.drafts.map((d) => {
      const sc = scanById.get(String(d.candidate_id));
      return {
        candidate_id: d.candidate_id,
        candidate_url: sc?.tweet_url,
        thread_author: sc?.author_handle,
        thread_text: sc?.tweet_text,
        reply_text: d.reply_text,
        engagement_style: d.engagement_style,
        language: d.language,
        link_keyword: d.link_keyword,
        search_topic: sc?.search_topic,
        matched_project: sc?.matched_project,
      } as PlanCandidate;
    });
    const firstSc = scanById.get(String(args.drafts[0].candidate_id));
    const project =
      (candidates.map((c) => c.matched_project).find((p): p is string => !!p) ||
        firstSc?.matched_project ||
        "default") as string;
    // Stage the new drafts under the scan batch id and bake link targets into them
    // (sub-second at TWITTER_PAGE_GEN_RATE=0). Best-effort: posting falls back to the
    // plain project URL per-candidate if gen is skipped.
    writePlan(args.batch_id, { candidates });
    try {
      await runPython("scripts/twitter_gen_links.py", ["--plan", planPath(args.batch_id)], {
        timeoutMs: 120_000,
        env: { TWITTER_PAGE_GEN_RATE: "0", SAPS_REPO_DIR: repoDir(), PATH: pipelinePath() },
      });
    } catch {
      /* best effort — plan still posts with a plain-URL fallback */
    }
    const staged = (readPlan(args.batch_id)?.candidates as PlanCandidate[]) ?? candidates;

    // Accumulate into ONE persistent review queue so a continuous autopilot's drafts
    // PILE UP in the menu-bar cards instead of each run overwriting the last. New
    // drafts are appended; a thread already in the queue (by URL) is skipped (one
    // draft per thread). Posted entries are KEPT in place so the 1-based card
    // numbering stays stable across runs — the menu bar, the chat table, and
    // post_drafts all index the full array and filter finished rows.
    const queue: PlanCandidate[] = [
      ...((readPlan(REVIEW_QUEUE_ID)?.candidates as PlanCandidate[]) ?? []),
    ];
    const seen = new Set(queue.map((c) => c.candidate_url).filter((u): u is string => !!u));
    let added = 0;
    for (const nc of staged) {
      if (nc.candidate_url && seen.has(nc.candidate_url)) continue;
      queue.push(nc);
      if (nc.candidate_url) seen.add(nc.candidate_url);
      added++;
    }
    writePlan(REVIEW_QUEUE_ID, { candidates: queue });
    // Pending = NOT YET DECIDED. A card that's posted, terminal (rejected/dead), OR
    // already approved is a settled decision and must never be re-presented for
    // review — approved ones just proceed to post (see drainApprovedBacklog).
    const pending = queue.filter(
      (c) => c.posted !== true && c.terminal !== true && c.approved !== true
    );

    // Drafts queued = the pipeline verified end-to-end without posting. This is the
    // onboarding "draft_verified" terminal goal (formerly completed by draft_cycle).
    if (added > 0)
      completeOnboardingMilestone("draft_verified", { outcome: "review_batch", draft_count: added });

    // Point the menu-bar review cards at the accumulated queue.
    writeReviewRequest({
      batch_id: REVIEW_QUEUE_ID,
      project,
      count: pending.length,
      plan_path: planPath(REVIEW_QUEUE_ID),
      created_at: new Date().toISOString(),
    });
    return textContent(
      `Queued ${added} new draft(s); ${pending.length} now awaiting approval in the menu-bar cards ` +
        `(review queue "${REVIEW_QUEUE_ID}"). Nothing posts until approved.\n\n` +
        renderDraftsTable({ candidates: queue }) +
        `\n\nTo post: the user approves in the menu bar, or call post_drafts with batch_id ` +
        `"${REVIEW_QUEUE_ID}" and the numbers to post.`
    );
  }
);

appTool(
  "dashboard",
  {
    title: "S4L dashboard",
    description:
      "Render the S4L dashboard in chat: a visual surface showing project setup, X " +
      "connection, autopilot state, and 7-day stats, with buttons to run a draft cycle, connect X, " +
      "and refresh. Use when the user asks to see the dashboard, panel, " +
      "status, or controls. ALSO call this at the end of any state-changing or results-producing " +
      "action (scan_candidates, submit_drafts, post_drafts, get_stats) so the user sees the " +
      "updated dashboard. Hosts without UI support get the same data as text.",
    inputSchema: {},
    // fallback_url is set only when the host can't render the ui:// resource and
    // we open the dashboard via the loopback HTTP server instead. Declared
    // optional so the SDK's strict output-schema check accepts both shapes.
    outputSchema: { snapshot: z.string(), fallback_url: z.string().optional() },
    _meta: { ui: { resourceUri: PANEL_URI } },
  },
  async () => {
    const snap = await buildSnapshot();
    const human =
      `S4L v${snap.version}` +
      (snap.update_available && snap.latest_version ? ` (update to ${snap.latest_version})` : "") +
      ` — projects ${snap.projects_ready}/${snap.projects_total} ready, ` +
      `X ${snap.x_connected ? "connected" : "not connected"}, ` +
      `autopilot ${snap.autopilot_on ? "on" : "off"}.`;
    const base = {
      content: [{ type: "text" as const, text: human }],
      structuredContent: { snapshot: JSON.stringify(snap) },
    };
    // If the host can render MCP Apps UI inline, the _meta.ui.resourceUri above
    // makes it paint the panel. Don't ALSO emit the human text line: the host
    // shows tool-result content next to the rendered panel, so returning `human`
    // here duplicates the dashboard as an annoying text "fallback" beside it.
    // Keep the snapshot in structuredContent (the model still reads it) and emit
    // no text content so the chat shows ONLY the panel.
    if (hostRendersAppUi()) {
      return { content: [], structuredContent: { snapshot: JSON.stringify(snap) } };
    }
    // Host CAN'T render inline (Claude Code / Cowork today): serve the identical
    // panel.html from a loopback HTTP server. We do NOT auto-open a browser tab
    // (see openInBrowser — opt-in only); the dashboard is shown in the Claude Code
    // side panel, and the loopback URL is returned for anyone who wants to open it.
    try {
      const url = await startLocalPanel();
      await openInBrowser(url);
      return {
        content: [{
          type: "text" as const,
          text:
            human +
            `\n\nThis host can't render the dashboard inline. It's available in the side panel; loopback URL: ${url}`,
        }],
        structuredContent: { snapshot: JSON.stringify(snap), fallback_url: url },
      };
    } catch (e: any) {
      // Loopback server failed to start; degrade to the text-only snapshot.
      console.error("[social-autoposter-mcp] local panel fallback failed:", e?.message || e);
      return base;
    }
  }
);

// ---- add your product: focused single-field onboarding widget --------------
// A standalone ui:// widget (separate from the dashboard panel) that captures
// the user's product URL. The widget itself reads project status and either
// writes the website via project_config (callServerTool) or, on a cold start,
// hands the URL to the model via sendMessage. Same inline/loopback duality as
// `dashboard`.
appTool(
  "connect_product",
  {
    title: "Add your product",
    description:
      "Render the 'add your product' widget in chat: a single-field form where the user pastes " +
      "their product's website. Use at the START of onboarding when you need the product URL, " +
      "instead of asking for it in plain prose. If a project already needs a website the widget " +
      "saves it directly; on a cold start it kicks off end-to-end setup. Hosts without UI support " +
      "get a loopback URL.",
    inputSchema: {},
    outputSchema: { snapshot: z.string(), fallback_url: z.string().optional() },
    _meta: { ui: { resourceUri: PRODUCT_LINK_URI } },
  },
  async () => {
    const snap = await buildSnapshot();
    // Inline-capable host: paint the resource named by _meta.ui.resourceUri.
    // Emit no text content so the chat shows only the widget (see `dashboard`).
    if (hostRendersAppUi()) {
      return { content: [], structuredContent: { snapshot: JSON.stringify(snap) } };
    }
    // No inline UI: serve the identical product-link.html from the loopback
    // server at /product-link and return its URL.
    try {
      const base = await startLocalPanel();
      const url = base.replace(/\/$/, "") + "/product-link";
      await openInBrowser(url);
      return {
        content: [{
          type: "text" as const,
          text:
            "Add your product: paste your product's website to begin setup.\n\n" +
            `This host can't render the widget inline. Loopback URL: ${url}`,
        }],
        structuredContent: { snapshot: JSON.stringify(snap), fallback_url: url },
      };
    } catch (e: any) {
      console.error("[social-autoposter-mcp] product-link fallback failed:", e?.message || e);
      return {
        content: [{ type: "text" as const, text: "Paste your product's website in the chat to begin setup." }],
        structuredContent: { snapshot: JSON.stringify(snap) },
      };
    }
  }
);

// ---- show browser to user: live CDP screencast ----------------------------
// Streams a live view of the autoposter's managed Chrome into the panel. Frames
// travel back through the normal tool-result channel as a data: URL (which the
// default panel CSP already permits), so this needs no CSP widening and no
// direct network access from the iframe. The panel polls action:"frame".
//
// This is a PLAIN tool (not appTool): it renders nothing of its own, it only
// feeds frames into the existing `dashboard` panel via callServerTool. Registering
// it as an app-tool requires a `_meta.ui.resourceUri`; without one,
// registerAppTool throws "Cannot read properties of undefined (reading 'ui')" at
// startup and the whole server fails to connect. So keep it a regular tool.
tool(
  "show_browser_to_user",
  {
    title: "Show browser to user",
    description:
      "Show the user a LIVE view of the autoposter's managed Chrome (what the bot " +
      "is doing in the browser right now). Attaches a CDP screencast to the active " +
      "browser session and returns the newest frame as a data: image. Actions: " +
      "'start' begins the screencast, 'frame' returns the latest frame (poll this on " +
      "a short interval to animate), 'stop' ends it, 'front' raises the real browser " +
      "window above everything else so the user can interact with it directly. Use when " +
      "the user asks to see / watch the browser, or to bring the browser to the front.",
    inputSchema: {
      action: z.enum(["start", "frame", "stop", "front"]).optional(),
      port: z.number().int().optional().describe("CDP debugging port to attach to; auto-detected if omitted."),
    },
  },
  async (args: any) => {
    const action = args?.action || "frame";
    if (action === "stop") {
      screencast.stop();
      return jsonContent({ ok: true, running: false });
    }
    if (action === "front") {
      const res = await bringBrowserToFront(typeof args?.port === "number" ? args.port : undefined);
      if (!res.ok) {
        const message =
          res.error === "no_browser"
            ? "No managed Chrome is running right now, so there's nothing to bring to the front. Start a draft cycle or autopilot first."
            : "Couldn't bring the browser to the front: " + String(res.error);
        return jsonContent({ ok: false, brought_to_front: false, message });
      }
      return jsonContent({ ok: true, brought_to_front: true, port: res.port });
    }
    // If the user is about to watch the live browser, make sure the on-screen
    // overlay watcher is up too so the harness window carries its status banner.
    if (action === "start") await ensureOverlayWatch();
    const ensured = await screencast.ensure(typeof args?.port === "number" ? args.port : undefined);
    if (!ensured.ok) {
      const message =
        ensured.error === "no_browser"
          ? "No managed Chrome is running right now. Start a draft cycle or autopilot so there's a live browser session to show."
          : ensured.error === "no_websocket"
            ? "This Node runtime has no WebSocket support (needs Node 21+), so a screencast can't be opened."
            : "Couldn't attach to the browser: " + String(ensured.error);
      return jsonContent({ ok: false, running: false, frame: null, message });
    }
    // On a fresh start the first frame takes a beat to arrive; wait briefly so the
    // caller's first poll already has something to paint.
    let frame = screencast.frame();
    for (let i = 0; i < 12 && !frame; i++) {
      await new Promise((r) => setTimeout(r, 120));
      frame = screencast.frame();
    }
    const st = screencast.status();
    return jsonContent({
      ok: true,
      running: st.running,
      port: st.port,
      title: st.title,
      url: st.url,
      age_ms: st.age_ms,
      frame: frame ? `data:image/jpeg;base64,${frame}` : null,
    });
  }
);

registerAppResource(
  server,
  "S4L panel",
  PANEL_URI,
  { mimeType: RESOURCE_MIME_TYPE },
  async () => ({
    contents: [
      {
        uri: PANEL_URI,
        mimeType: RESOURCE_MIME_TYPE,
        text: fs.readFileSync(path.join(DIST_DIR, "panel.html"), "utf-8"),
      },
    ],
  })
);

registerAppResource(
  server,
  "S4L product link",
  PRODUCT_LINK_URI,
  { mimeType: RESOURCE_MIME_TYPE },
  async () => ({
    contents: [
      {
        uri: PRODUCT_LINK_URI,
        mimeType: RESOURCE_MIME_TYPE,
        text: fs.readFileSync(path.join(DIST_DIR, "product-link.html"), "utf-8"),
      },
    ],
  })
);

// Post any cards the user APPROVED that never landed — e.g. a restart killed the
// batch mid-way. "Proceed to post the already-approved items." postApproved is
// idempotent (it filters posted/terminal), so this only drains the genuine
// backlog and never double-posts. Best-effort; never throws.
async function drainApprovedBacklog(): Promise<void> {
  try {
    const plan = readPlan(REVIEW_QUEUE_ID);
    const cands = (plan?.candidates as PlanCandidate[]) || [];
    const backlog = cands.filter(
      (c) => c.approved === true && c.posted !== true && c.terminal !== true
    );
    if (!backlog.length) return;
    console.error(
      `[post] draining ${backlog.length} approved-but-unposted card(s) left from before`
    );
    await postApproved(REVIEW_QUEUE_ID, plan!);
  } catch (e: any) {
    console.error("[post] drainApprovedBacklog error:", e?.message || e);
  }
}

async function main() {
  initSentry();
  // Tee the verbatim stdout/stderr of every pipeline subprocess to the s4l
  // Cloud Run relay (-> Cloud Logging) so we can troubleshoot/rescue any user
  // scenario (silent stalls, partial onboarding) without asking them to ship a
  // log file. Best-effort; disabled with SAPS_LOG_STREAM=0.
  startLogStreaming();
  // A plugin UPDATE refreshes this server (dist/) but not the materialized
  // pipeline. Re-extract the bundled pipeline.tgz when it's newer than what's on
  // disk, BEFORE serving, so the very first scan uses the shipped pipeline (not
  // the version first materialized at install). Synchronous + best-effort.
  ensurePipelineCurrent();
  // Deterministically provision the owned runtime on boot: whenever it isn't
  // ready (a fresh install, or one interrupted mid-way because a step failed or
  // Claude/the host died mid-install) kick the full install in the background
  // instead of waiting for the agent to call `runtime action:'install'`. The
  // host spawns this server when the plugin loads, so the env starts installing
  // the moment the plugin is active. Idempotent: it re-checks done steps and
  // attempts only the missing ones; the background provision() updates
  // install-progress.json as it goes.
  if (ensureRuntimeProvisioned()) {
    console.error(
      "[social-autoposter-mcp] owned runtime not ready; provisioning on boot"
    );
  }
  // Same problem for the scheduled task's prompt: the host owns its SKILL.md and
  // a plugin update never touches it. Rewrite it on boot when this build ships a
  // newer prompt version, so behavior changes (no-improvise / voice-inline /
  // stop-cleanly) reach an already-scheduled task on its next fire. Best-effort.
  ensureAutopilotPromptCurrent();
  // Pre-approve THIS server's tools in ~/.claude/settings.json so the unattended
  // scan -> submit path never stalls on an "Ask mode" permission prompt (see the
  // helper's note). Best-effort, allow-only, gated on the task existing.
  ensureAutopilotToolsAllowed();
  // Queue-backed drafting (2026-06-23): keep the two worker-task prompts current,
  // pre-approve their tools EAGERLY (before onboarding even creates the tasks, so
  // the first unattended fire can't stall), and (re)install the launchd kicker
  // that runs the real DRAFT_ONLY pipeline whose claude -p calls feed the queue.
  // All best-effort; none may block boot.
  ensureQueueWorkerPromptsCurrent();
  ensureQueueWorkerToolsAllowed();
  void ensureQueueKickerInstalled()
    .then((r) => console.error(`[queue-worker] launchd kicker: ${r.ok ? "ok" : "skip"} (${r.detail})`))
    .catch((e) => console.error("[queue-worker] kicker install failed:", e?.message || e));
  // Heal installs onboarded before short_links_live defaulted to false: such a
  // project wraps short links against the customer's own domain, which has no
  // /r/[code] resolver, so every minted link 404s. Re-point them at the s4l.ai
  // resolver. Idempotent, scoped to managed projects, best-effort.
  try {
    const r = ensureShortLinksDefault();
    if (r.healed.length) {
      console.error(
        `[social-autoposter-mcp] short-links heal: routed ${r.healed.join(", ")} through s4l.ai (short_links_live=false)`
      );
    }
  } catch (e) {
    console.error("[social-autoposter-mcp] short-links heal failed:", (e as Error)?.message || e);
  }
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error(`[social-autoposter-mcp] connected. v=${VERSION} repo=${repoDir()}`);
  // Eagerly start the loopback panel server so the Claude Code side panel (and any
  // reverse proxy in front of it) always has a backend to hit, without waiting for
  // a first `dashboard` call. Best-effort: a bind failure must never block boot.
  void startLocalPanel()
    .then((url) => console.error(`[social-autoposter-mcp] panel loopback ready at ${url}`))
    .catch((e) => console.error("[social-autoposter-mcp] panel loopback start failed:", e?.message || e));
  // Resume posting any approved-but-unposted cards a prior run/restart left behind.
  // Delayed so the runtime + harness Chrome have settled; never blocks boot.
  {
    const t = setTimeout(() => void drainApprovedBacklog(), 30_000);
    if (typeof t.unref === "function") t.unref();
  }
  // Ensure the macOS menu bar mini-dashboard is installed + running. Idempotent
  // and cheap when already present, so existing installs pick it up on the next
  // Claude restart without re-provisioning. Best-effort: never blocks boot.
  void ensureMenubar()
    .then((r) => {
      console.error(
        `[social-autoposter-mcp] menubar: ${r.skipped ? "skip" : r.ok ? "ok" : "fail"} (${r.detail})`
      );
      // A non-skipped failure here is the boot-time "menu bar didn't come up"
      // path (e.g. uv missing, rumps reinstall failed on an existing install).
      // Report it; a skip (non-macOS / runtime not ready) is expected, not an error.
      if (!r.ok && !r.skipped) {
        captureError(new Error(`menubar ensure failed: ${r.detail}`), {
          component: "menubar",
          phase: "ensure",
        });
      }
    })
    .catch((e) => {
      console.error("[social-autoposter-mcp] menubar ensure failed:", e?.message || e);
      captureError(e, { component: "menubar", phase: "ensure" });
    });
  // Phone home so this .mcpb install is visible in the install-lane digest
  // (parity with the npx launchd heartbeat). Once on startup, then every 15m
  // while the desktop app keeps the server alive. unref() so it never holds the
  // process open past a normal exit.
  void sendHeartbeat("startup");
  const hb = setInterval(() => void sendHeartbeat("interval"), 15 * 60_000);
  hb.unref();
}

main().catch(async (err) => {
  console.error("[social-autoposter-mcp] fatal:", err);
  captureError(err, { component: "main" });
  await flushLogs();
  await flushSentry();
  process.exit(1);
});
