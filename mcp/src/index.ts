#!/usr/bin/env node
// social-autoposter MCP server (X/Twitter rail).
//
// Core tools:
//   queue_setup     - return the two draft-autopilot scheduled-task specs to register
//                     via the host create_scheduled_task. The autopilot then drafts
//                     on its own (launchd kicker + queue worker); there is no manual
//                     "draft now" tool.
//   approve_drafts     - post the drafts the user chose by number from a batch.
//   get_stats    - read-only post + engagement stats.
//
// THIN wrapper. The pipeline brain (scan, score, drafting prompts, posting)
// stays in the Python/shell scripts; we only orchestrate and present.

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { execFileSync } from "node:child_process";
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
  TMP_DIR,
  type Plan,
  type PlanCandidate,
} from "./repo.js";
import {
  applySetup,
  resolveProject,
  hasReadyProject,
  personaReady,
  listManagedProjectStatus,
  listProjectSettings,
  ensureShortLinksDefault,
  ensurePersonaProject,
  findPersonaProject,
  REQUIRED_FIELDS,
  RECOMMENDED_FIELDS,
  configPath,
  ensureConfigInStateDir,
  normalizeStringList,
  recordRedditAccount,
  type ProjectInput,
} from "./setup.js";
import { xStatus, xConnect, xDetectSources, xScanProfile, summarizeXAuth } from "./twitterAuth.js";
import {
  redditStatus,
  redditConnect,
  redditDetectSources,
  summarizeRedditAuth,
} from "./redditAuth.js";
import {
  startProvisioning,
  isProvisioning,
  readProgress,
  runtimeReady,
  readRuntime,
  resolvePython,
  resolveChrome,
  ensureMenubar,
  menubarRunning,
  clearMenubarStop,
  ensurePipelineCurrent,
  ensureRuntimeProvisioned,
  ensureHarnessPatched,
  retryProvisionIfStalled,
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
import { initSentry, sendHeartbeat, sendStateSnapshot, captureError, captureMessage, flushSentry, startLogStreaming, flushLogs, logLine, checkVersionChange } from "./telemetry.js";
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

// Stable id for the accumulating draft review queue. Each draft cycle appends its
// drafts here (dedup by tweet URL) so the menu-bar cards PILE UP across a
// continuous autopilot instead of each run overwriting the last; approve_drafts posts
// the approved subset and marks them posted (filtered out of the cards thereafter).
const REVIEW_QUEUE_ID = "review-queue";

// ---- Queue-backed drafting (2026-06-23) -----------------------------------
// Customer .mcpb boxes have no `claude` CLI, so the deterministic pipeline can't
// run its `claude -p` steps directly. Instead a launchd job kicks the REAL
// pipeline (run-twitter-cycle.sh in DRAFT_ONLY mode); its `claude -p` steps
// carry queue-mapped script tags, so run_claude.sh routes each one onto
// scripts/claude_job.py's file queue and blocks (routing is by tag via
// claude_job.py TAG_TO_TYPE — the S4L_CLAUDE_PROVIDER env var was removed
// 2026-07-06). Claude Desktop scheduled tasks drain that queue, run the
// pipeline's own prompt as a Claude turn, and write the result back,
// unblocking the cycle. This reuses the entire pipeline (styles, voice,
// top-performers, em-dash rules). See scripts/claude_job.py + run_claude.sh's
// routing seam.
// Universal type-blind queue worker (2026-07-02): ONE scheduled task drains
// EVERY job type (`claude_job.py next --type any`). Per-type execution notes
// ride in the job's prompt sidecar (claude_job.py TYPE_TO_WORKER_NOTES), so
// the worker prompt never mentions types. Task ids are USER-VISIBLE (Routines
// UI), so they carry the S4L brand — never the internal "saps" prefix.
const WORKER_TASK_ID = "s4l-worker";
// Legacy workers from earlier installs. Not created anymore; their SKILL.md is
// refreshed to the same universal body on boot (so old boxes keep draining —
// interchangeable workers racing the same claim is safe, the claim is an
// atomic rename) until the menubar's one-restart self-heal consolidates them
// into s4l-worker. "saps-worker" existed only on staging (rc.2/rc.3) before
// the brand rename.
const LEGACY_UNIVERSAL_TASK_ID = "saps-worker";
const PHASE1_TASK_ID = "saps-phase1-query"; // legacy (was: "twitter-query" only)
const PHASE2B_TASK_ID = "saps-phase2b-draft"; // legacy (was: "twitter-prep" only)

const TWITTER_AUTOPILOT_LABEL = "com.m13v.social-twitter-cycle";
const TWITTER_AUTOPILOT_PLIST = path.join(
  os.homedir(),
  "Library",
  "LaunchAgents",
  `${TWITTER_AUTOPILOT_LABEL}.plist`
);

// Reddit discovery kicker (optional platform, 2026-07-15). Runs the SAME
// launchd label the operator Mac has used by hand since May
// (com.m13v.social-reddit-search) so there is one canonical name fleet-wide,
// but ensureRedditKickerInstalled() refuses to touch a plist whose program
// path points outside the managed package (the operator's hand-built plist
// stays untouched). Installed only when reddit is connected AND a project is
// configured; never part of onboarding completion.
const REDDIT_SEARCH_LABEL = "com.m13v.social-reddit-search";
const REDDIT_SEARCH_PLIST = path.join(
  os.homedir(),
  "Library",
  "LaunchAgents",
  `${REDDIT_SEARCH_LABEL}.plist`
);
const REDDIT_SEARCH_INTERVAL_SECS = 900;
const REDDIT_CDP_URL_DEFAULT = "http://127.0.0.1:9557";

// Self-healing reaper for leaked Claude agent-mode worker sessions. The queue
// autopilot fires two scheduled tasks every ~1 min; each fire spawns a ~200 MB
// `claude` agent-mode session that finishes its one queue turn but never exits
// (Desktop keeps the stream-json session warm), so they pile up — 226 procs /
// 22.5 GB on the test box in ~1h, load 75, near-OOM. We can't change Desktop's
// teardown, so a 60s launchd job kills the leaked sessions (see the script for
// the uuid-grouping safety that spares real interactive sessions).
const REAPER_LABEL = "com.m13v.social-claude-reaper";
const REAPER_PLIST = path.join(
  os.homedir(),
  "Library",
  "LaunchAgents",
  `${REAPER_LABEL}.plist`
);

// Feedback digest: distills the user's card approve/reject decisions
// (review_events, shipped by the menubar with reason chips + link clicks)
// into the project's learned_preferences block in config.json, which the
// prep prompt then reads via ALL_PROJECTS_JSON. Hourly; exits immediately
// when there are no unprocessed events for this install.
const FEEDBACK_DIGEST_LABEL = "com.m13v.social-feedback-digest";
const FEEDBACK_DIGEST_PLIST = path.join(
  os.homedir(),
  "Library",
  "LaunchAgents",
  `${FEEDBACK_DIGEST_LABEL}.plist`
);

// Periodic host-resource sampler. Appends one redacted memory/process snapshot
// per minute to skill/logs/memory-snapshots.jsonl (rotated) so we have local
// history when SSHing into a box, and so the heartbeat's --summary path has a
// warm picture. The plist is fully templated (repoDir/$HOME/resolvePython), so
// unlike the legacy dev-box plist in launchd/ it runs anywhere. Cheap +
// short-lived (one ps/vm_stat pass, then exits).
const MEMORY_SNAPSHOT_LABEL = "com.m13v.social-memory-snapshot";
const MEMORY_SNAPSHOT_PLIST = path.join(
  os.homedir(),
  "Library",
  "LaunchAgents",
  `${MEMORY_SNAPSHOT_LABEL}.plist`
);
const MEMORY_SNAPSHOT_INTERVAL_SECS = 60;

// Autopilot stall watchdog (fleet backstop). The draft autopilot's two scheduled-
// task routines stop draining the queue when the user switches Claude Desktop
// accounts (the routines are registered per-account; their global SKILL.md files
// survive, so the presence-based "autopilot_on" reads a false green). The menu bar
// surfaces this to the user (S4L ⚠ + Re-arm); this launchd job is the part the
// user can't see — it emits a Sentry event on a sustained stall so we catch it
// fleet-wide. Runs off the venv python (needs sentry-sdk). See
// scripts/autopilot_stall_watch.py.
const STALL_WATCH_LABEL = "com.m13v.social-autopilot-stall-watch";
const STALL_WATCH_PLIST = path.join(
  os.homedir(),
  "Library",
  "LaunchAgents",
  `${STALL_WATCH_LABEL}.plist`
);
const STALL_WATCH_INTERVAL_SECS = 120;

// On-screen overlay watcher. The harness status overlay ("S4L running" / idle
// banner) only renders WHILE `harness_overlay.py watch` is alive. That watcher
// is fire-and-forget with no supervisor of its own, so when it dies (or the
// harness Chrome restarts) nothing brings it back and the overlay silently
// disappears. Promote it to a first-class launchd job, but run the long-lived
// watcher in the FOREGROUND under KeepAlive (NOT a StartInterval that re-invokes
// a spawn-and-exit supervisor). The supervisor pattern races launchd on macOS:
// the instant the kicker shell exits, launchd SIGKILLs the whole job process
// group and reaps the just-spawned watcher before it can detach. Running the
// watcher AS the job's main process makes launchd supervise it directly:
// RunAtLoad starts it at boot, KeepAlive restarts it if it ever exits, and on
// unload its SIGTERM handler clears the overlay cleanly. Disable with
// S4L_OVERLAY_WATCH=0.
const OVERLAY_WATCH_LABEL = "com.m13v.social-overlay-watch";
const OVERLAY_WATCH_PLIST = path.join(
  os.homedir(),
  "Library",
  "LaunchAgents",
  `${OVERLAY_WATCH_LABEL}.plist`
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
  // When true, the program is a long-lived foreground process: emit KeepAlive
  // (launchd restarts it whenever it exits) instead of StartInterval (which
  // re-invokes a short-lived command on a timer). intervalSecs is ignored.
  keepAlive?: boolean;
  // When true, launchd leaves the job's surviving children alone when the main
  // process exits. Without it, launchd SIGKILLs the job's whole process group
  // on exit — which reaped the harness Chrome the cycle had just launched, so
  // the NEXT cycle relaunched Chrome and stole the user's focus (2026-07-12).
  // Set on the kicker; the setsid wrapper in skill/lib/*-backend.sh is the
  // primary fix, this is the launchd-side backstop.
  abandonProcessGroup?: boolean;
}): string {
  const args = opts.programArgs.map((a) => `\t\t<string>${a}</string>`).join("\n");
  const schedule = opts.keepAlive
    ? `\t<key>KeepAlive</key>\n\t<true/>`
    : `\t<key>StartInterval</key>\n\t<integer>${opts.intervalSecs}</integer>`;
  const abandon = opts.abandonProcessGroup
    ? `\n\t<key>AbandonProcessGroup</key>\n\t<true/>`
    : "";
  // Background (cron/autopilot) runs get the same Chrome the interactive cycle
  // uses, so a no-sudo ~/Applications install (which the shell's own resolver
  // doesn't scan) is still found off-screen. Omitted when Chrome resolves via
  // PATH, so the shell's _resolve_chrome_bin stays the fallback.
  const chrome = resolveChrome();
  const chromeEnv = chrome
    ? `\n\t\t<key>BH_CHROME_BIN</key>\n\t\t<string>${chrome}</string>`
    : "";
  // Caller-supplied env (e.g. the queue kicker's DRAFT_ONLY).
  // Rendered after the baked-in vars so a caller can also override S4L_STATE_DIR.
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
${schedule}${abandon}
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
\t\t<key>S4L_REPO_DIR</key>
\t\t<string>${repoDir()}</string>
\t\t<key>S4L_PYTHON</key>
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

// Per-label failure backoff for launchd loads. Karol's box (2026-07-03) looped
// `bootstrap -> Input/output error 5` + `load -> error 5` several times per heal
// tick for HOURS, with no label, no stderr detail, and no cooldown: pure log
// flood, zero diagnosis. launchd's error 5 is a catch-all; the most common
// FIXABLE cause is the service being disabled in the gui domain, so loadPlist
// now (a) best-effort `launchctl enable`s the label first, (b) on double
// failure emits ONE structured relay line carrying the label, plist, both
// stderr tails, and whether the label appears in the domain's disabled list,
// and (c) backs off for 6 hours after 3 consecutive failures per label.
const plistLoadFailures = new Map<string, { count: number; skipUntil: number }>();

async function loadPlist(label: string, plistPath: string, uid: number) {
  const back = plistLoadFailures.get(label);
  if (back && back.skipUntil > Date.now()) {
    return {
      code: 1,
      stdout: "",
      stderr: `launchd-load backoff: ${label} failed ${back.count}x; next attempt after ${new Date(back.skipUntil).toISOString()}`,
    };
  }
  // Clears a disabled override when that's the blocker; harmless otherwise.
  await run("launchctl", ["enable", `gui/${uid}/${label}`], { timeoutMs: 15_000, noTee: true });
  let res = await run("launchctl", ["bootstrap", `gui/${uid}`, plistPath], { timeoutMs: 15_000 });
  const bootstrapErr = res.code !== 0 ? lastLine(res.stderr || res.stdout) : "";
  if (res.code !== 0) {
    res = await run("launchctl", ["load", plistPath], { timeoutMs: 15_000 });
  }
  if (res.code !== 0) {
    const loadErr = lastLine(res.stderr || res.stdout);
    let disabledEntry = "unknown";
    try {
      const disabled = await run("launchctl", ["print-disabled", `gui/${uid}`], {
        timeoutMs: 15_000,
        noTee: true,
      });
      disabledEntry =
        (disabled.stdout || "")
          .split("\n")
          .find((l) => l.includes(label))
          ?.trim() || "not-listed";
    } catch {
      /* diagnostic only */
    }
    const detail = JSON.stringify({
      label,
      plist: plistPath,
      bootstrap_err: bootstrapErr,
      load_err: loadErr,
      disabled_entry: disabledEntry,
    });
    console.error(`[launchd-load] failed: ${detail}`);
    logLine("stderr", detail, "launchd-load");
    const prev = plistLoadFailures.get(label) ?? { count: 0, skipUntil: 0 };
    prev.count += 1;
    if (prev.count >= 3) {
      prev.skipUntil = Date.now() + 6 * 3600_000;
      const msg = JSON.stringify({ label, backoff_hours: 6, consecutive_failures: prev.count });
      console.error(`[launchd-load] backing off: ${msg}`);
      logLine("stderr", `backing off: ${msg}`, "launchd-load");
    }
    plistLoadFailures.set(label, prev);
  } else {
    plistLoadFailures.delete(label);
  }
  return res;
}

function lastLine(s: string): string {
  return (s || "").trim().split("\n").slice(-1)[0] || "";
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
      "S4L (the social-autoposter server) drafts, reviews, and posts X/Twitter replies for the user's product, and " +
      "can run them hands-free on autopilot.\n\n" +
      "ONBOARDING IS A TERMINAL GOAL. When the user asks to set up, install, configure, or onboard " +
      "social-autoposter, keep taking the next safe action until the owned runtime is ready, a " +
      "project is fully configured with seeded search topics, AT LEAST ONE platform is connected " +
      "(ASK the user up front which platform(s) to connect — X, Reddit, or both — before connecting " +
      "anything; X is a reasonable default to SUGGEST, but do not connect it, or any platform, without " +
      "the user's answer. Connect exactly what they choose via project_config action:'connect_x' " +
      "and/or action:'connect_reddit'), AND " +
      "the two draft-autopilot scheduled tasks have been created via queue_setup and are firing " +
      "(see SCHEDULE THE AUTOPILOT below). The owned runtime (private Python + Chromium) provisions " +
      "ITSELF automatically on boot — you do NOT install it. Just poll runtime action:'status' until it " +
      "reports ready; only call runtime action:'install' or action:'doctor' to TROUBLESHOOT if status " +
      "shows the boot provision failed or stalled. Do not ask whether to inspect " +
      "status, repair a failed runtime, choose an auto-detected browser profile, scan the profile, " +
      "research the product website, save conservative inferred fields, or seed " +
      "topics. Do those things. The " +
      "explicit setup request authorizes runtime repair and importing ONLY x.com/" +
      "twitter.com session cookies into the managed browser: warn that macOS keychain prompts may " +
      "appear, then proceed without a separate yes/no turn. Besides the platform-choice question " +
      "above, ask at most one MORE bundled question, only " +
      "when no product can be identified from config, context, the X profile, or public research, " +
      "or pause for an unavoidable interactive login or OS prompt. Never auto-POST during setup. " +
      "DO schedule the draft autopilot as the final step (once running it only queues drafts for " +
      "approval, it never posts on its own). When setup reaches done (" +
      "runtime ready, a project configured with seeded topics, at least one platform connected, and the " +
      "two scheduled tasks " +
      "created and firing), call the `dashboard` tool so the user " +
      "sees the finished setup rendered, then give " +
      "the completion summary.\n\n" +
      "SCHEDULE THE AUTOPILOT (final onboarding step). There is ONE drafting pipeline: a launchd " +
      "job this server installs runs the real cycle in draft-only mode, its Phase-2b Claude turn is " +
      "serviced by a scheduled task that drains a local job queue, and the result is merged into the " +
      "menu-bar approval cards. Drafting is the queue + worker path, fired automatically by a launchd " +
      "kicker this server installs (every few minutes) — it runs on its own once the tasks exist; " +
      "onboarding does NOT need to wait for or verify a draft. Steps, in order: " +
      "(1) call `queue_setup` — it pre-writes the worker task prompt and returns the task spec " +
      "(s4l-worker, the universal queue worker); " +
      "(2) for EACH returned spec, call the HOST tool `create_scheduled_task` with its taskId, cronExpression, " +
      "prompt, and notifyOnCompletion VERBATIM — notifyOnCompletion MUST be false, or the per-minute worker " +
      "pops a 'no jobs' notification every run (if it says the task already exists, that's fine — do not " +
      "recreate, but DO call `update_scheduled_task` with the same taskId and notifyOnCompletion:false to " +
      "silence an older install's per-run notifications, then move on); " +
      "(3) the launchd kicker + scheduled tasks now run the pipeline on their own — call the `dashboard` " +
      "tool to confirm the schedule is firing (schedule_state 'ok'), then summarize. Do NOT create the " +
      "old 'social-autoposter-autopilot' task — it is deprecated. Do NOT ask the user to review drafts " +
      "or pause for input mid-onboarding; once the tasks are created and firing, setup is done. If the " +
      "host exposes no create_scheduled_task tool, tell the user the autopilot couldn't be scheduled here.\n\n" +
      "REDDIT IS AN EQUAL PLATFORM CHOICE, ASKED ALONGSIDE X. Setup is complete with AT LEAST ONE " +
      "platform connected: X-only, Reddit-only, or both — whichever the user picked when you asked " +
      "up front. If they chose both, connect them one at a time (either order) in the same setup " +
      "flow. `project_config` action:'connect_reddit' imports the user's reddit.com session into the " +
      "autoposter's managed reddit browser, same confirm-first flow and keychain heads-up as " +
      "connect_x. Once connected, reddit discovery and drafting run on the same draft-only queue and " +
      "review cards as X; nothing posts without approval. If the result carries a `warning` (fresh or " +
      "low-karma account), relay it so the user knows AutoMod may gate early replies. Never make " +
      "either platform a requirement, and never nag a user who deliberately chose only one about the " +
      "other.\n\n" +
      "BE PROACTIVE ABOUT MARKETING MOMENTS. Whenever the user ships, finishes, merges, or releases " +
      "something worth talking about in this session (a new feature, a launch, a long-awaited fix, a " +
      "milestone), don't wait to be asked: point it out to the user — the draft autopilot runs " +
      "continuously and will surface relevant posts to approve. A feature nobody hears " +
      "about is wasted distribution. Use judgment, suggest it for " +
      "genuinely shareable moments, not routine refactors, chores, or internal cleanup.\n\n" +
      "STAY CURRENT. At the start of a session, and again right after you help the user ship/merge/" +
      "release, call the `runtime` tool (action:'version'). If `update_available` is true, tell the " +
      "user and offer to run `runtime` with action:'update'. The `project_config` tool's status also " +
      "surfaces `update_available` and an `update_hint`.\n\n" +
      "TYPICAL FLOW: ASK which platform(s) to connect (X, Reddit, or both) -> `project_config` " +
      "(connect the chosen platform(s) + scan the profile) -> `engagement_mode` (after the " +
      "profile scan: personal-brand is ON by default, so ASK the user the ONE question — do they ALSO " +
      "want to promote a product? — and call action:'set' with personal_brand:true and " +
      "promotion:true|false; this provisions the persona) -> IF they wanted promotion, `project_config` " +
      "(configure the product project) -> `queue_setup` + " +
      "`create_scheduled_task` (set up the draft autopilot once) -> the autopilot then runs on its own " +
      "(scans, drafts via the queue + worker, and merges into the approval cards; nothing posts) -> the " +
      "user approves in the menu bar -> `approve_drafts` (post the approved ones) -> `get_stats` (see " +
      "performance). Run `project_config` first; the other tools refuse until a " +
      "project is fully configured. To change anything about a project later, call `project_config` " +
      "again with the project's name and just the changed fields — there is no separate config editor.\n\n" +
      "RENDER THE DASHBOARD AFTER ACTIONS. After any state-changing or results-producing tool call " +
      "(`approve_drafts`, `get_stats`, `project_config`), end your turn by " +
      "calling the `dashboard` tool so the user sees the updated state visually. Do NOT call " +
      "`dashboard` after pure Q&A, config explanations, or status-only checks that changed nothing.\n\n" +
      "REPORT FIELD DIAGNOSES. Whenever an S4L setup, healing, or troubleshooting flow FAILS, or " +
      "succeeds only after a failure (schedule re-arm, runtime repair, stuck drafts, rate limits, " +
      "anything the user pasted a fix-it prompt for), call `report_diagnosis` with a short markdown " +
      "report (symptom, root cause, actions taken, current state) before ending the turn. Do this " +
      "automatically — the developers only learn about field failures through these reports.",
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
  approve_drafts: "posting",
  get_stats: "stats…",
};
function toolActivityLabel(name: string, args: any): string | null {
  const fallback = TOOL_ACTIVITY[name];
  if (!fallback) return null;
  const override =
    typeof args?.__s4l_activity_label === "string"
      ? args.__s4l_activity_label.replace(/\s+/g, " ").trim().slice(0, 80)
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

// Tool-call telemetry: one structured relay line at the start and end of every
// tool invocation (context "tool-call" in Cloud Logging). This is the record
// that was missing on 2026-07-03, when reconstructing WHAT the setup agent
// actually called (and which calls the client abandoned at its hard 60s
// timeout) required inference from subprocess side effects. Start+end pairs
// make abandoned/long calls visible: a start line with no end line inside the
// expected window means the handler is still running or died. Argument VALUES
// are never logged (they can carry persona/voice text); only the action field
// and the argument key names.
function withToolLog(name: string, cb: ToolHandler): ToolHandler {
  return async (args: any, extra: any) => {
    const action = typeof args?.action === "string" ? args.action : undefined;
    const argKeys = args && typeof args === "object" ? Object.keys(args).slice(0, 30) : [];
    const startedAt = Date.now();
    logLine(
      "stdout",
      JSON.stringify({ ev: "start", tool: name, action, arg_keys: argKeys }),
      "tool-call"
    );
    try {
      const result = await cb(args, extra);
      logLine(
        "stdout",
        JSON.stringify({ ev: "end", tool: name, action, ok: true, ms: Date.now() - startedAt }),
        "tool-call"
      );
      return result;
    } catch (e: any) {
      logLine(
        "stderr",
        JSON.stringify({
          ev: "end",
          tool: name,
          action,
          ok: false,
          ms: Date.now() - startedAt,
          error: String(e?.message || e).slice(0, 500),
        }),
        "tool-call"
      );
      throw e;
    }
  };
}

const tool: typeof server.registerTool = ((name: string, config: any, cb: ToolHandler) => {
  const h = withToolLog(name, withActivity(name, cb));
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
  const h = withToolLog(name, withActivity(name, wrapped));
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
        "then `/login` inside it (or run `claude setup-token`). Once it's logged in, the autopilot will retry on its next scheduled cycle."
      );
    case "monthly_limit":
    case "daily_limit":
    case "rate_limit_5h":
      return (
        `The drafting step hit an Anthropic usage limit (${reason}), so no replies were drafted. ` +
        "Wait for the limit to reset, then the autopilot will retry on its next scheduled cycle."
      );
    case "no_search_topics":
      return (
        "This project has no search topics yet, so there was nothing to scan. Topics live in the " +
        "DB (project_search_topics) and are seeded from your project's `search_topics` when you " +
        "configure it. Re-run the `project_config` tool for this project with a `search_topics` list " +
        "(comma-separated keywords/phrases your buyers tweet about); it seeds them automatically, then " +
        "the autopilot will retry on its next scheduled cycle."
      );
    case "topics_api_unreachable":
      return (
        "Couldn't reach the search-topics service to load this project's topics, so the cycle stopped " +
        "before scanning. This is usually a transient backend/network issue. It should clear on the " +
        "autopilot's next scheduled cycle; if it persists, check connectivity to the autoposter backend."
      );
    case "credit_balance":
      return (
        "The drafting step failed because the Anthropic account is out of credits. " +
        "Add credits, then the autopilot will retry on its next scheduled cycle."
      );
    default:
      return (
        `The drafting step failed (${reason}) and produced no drafts. ` +
        "Check skill/logs/twitter-cycle-*.log on this machine for details, then the autopilot will retry on its next scheduled cycle."
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
// We thread S4L_PYTHON (the owned uv runtime, so the watcher resolves a
// playwright-capable interpreter on Lane B / .mcpb installs that have no system
// python) and S4L_LOG_DIR (the materialized repo's skill/logs, so the watcher
// reads the SAME cycle logs this run writes to decide busy/idle). Fire-and-forget:
// a failure here must never break the cycle it's decorating.
async function ensureOverlayWatch(): Promise<void> {
  try {
    await run("bash", ["skill/run-overlay-watch.sh"], {
      timeoutMs: 20_000,
      env: ({
        S4L_PYTHON: resolvePython(),
        S4L_LOG_DIR: path.join(repoDir(), "skill", "logs"),
        TWITTER_CDP_URL: process.env.TWITTER_CDP_URL || "http://127.0.0.1:9555",
      }),
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
  // leaves the plan on disk for us to review + post. S4L_FORCE_PROJECT scopes
  // the cycle to one project; TWITTER_PAGE_GEN_RATE=0 keeps link-gen sub-second.
  const env: NodeJS.ProcessEnv = {
    DRAFT_ONLY: "1",
    TWITTER_PAGE_GEN_RATE: "0",
    // Point the cycle at the resolved repo (a bare .mcpb materializes it under
    // the state dir, NOT ~/social-autoposter); run-twitter-cycle.sh honors
    // S4L_REPO_DIR for its REPO_DIR. And put the owned runtime + ~/.local/bin
    // first on PATH so the script's bare `python3` and `browser-harness` resolve.
    S4L_REPO_DIR: repoDir(),
    PATH: pipelinePath(),
    // Interactive draft_cycle: launch the harness Chrome ON-SCREEN so the user
    // can watch the scan/scrape happen live. Cron/autopilot do NOT set these, so
    // background runs keep the off-screen default in twitter-backend.sh and don't
    // hijack the screen. (Only affects a fresh Chrome launch; an already-running
    // harness window keeps its current position.)
    BH_WINDOW_POS: "60,60",
    BH_WINDOW_SIZE: "1280,900",
  };
  if (project) env.S4L_FORCE_PROJECT = project;
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
  // Granular scan progress for the menu-bar label. Phase 1 logs one
  // `executing N queries` line (the total), then one `ok/err project=… kept=K`
  // line per query. We count those to paint `scan N/M +K` (K = kept) instead
  // of a static "scan…". Best-effort: missing total falls back to a plain
  // count, and any parse miss just leaves the prior label up.
  let scanTotal = 0;
  let scanDone = 0;
  let scanKept = 0;
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
  writeActivity("scanning", "scan…");
  const res = await run("bash", ["skill/run-twitter-cycle.sh"], {
    env: env as Record<string, string>,
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
      // Per-query scan progress -> granular menu-bar label. These lines only
      // appear during Phase 1 (before 2b-prep), so they never fight the
      // "drafting" label below.
      let sm: RegExpExecArray | null;
      if ((sm = /executing (\d+) quer/.exec(t))) {
        scanTotal = parseInt(sm[1], 10) || 0;
      } else if ((sm = /^\s*(?:ok|err)\s+project=/.exec(t))) {
        scanDone += 1;
        const km = /kept=(\d+)/.exec(t);
        if (km) scanKept += parseInt(km[1], 10) || 0;
        const prog = scanTotal ? `${scanDone}/${scanTotal}` : `${scanDone}`;
        writeActivity("scanning", `scan ${prog} +${scanKept}`);
      }
      if (/Phase 2b-prep/.test(t)) writeActivity("drafting", "drafting");
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
// numbers to post / edit, then posts the chosen ones via the `approve_drafts` tool.
//
// We used to gather approvals through MCP elicitation (a checkbox form), but the
// desktop "Code tab" host doesn't advertise the `elicitation` capability (only
// `io.modelcontextprotocol/ui`), so the form never rendered and cycles silently
// posted nothing. Approval is conversational instead — numbers in chat.
function renderDraftsTable(plan: Plan): string {
  const candidates = plan.candidates || [];
  return candidates
    // Number by FULL-array index (matches approve_drafts + the menu bar), then drop
    // already-finished entries so the cards only show what's still pending.
    .map((c, i) => ({ c, n: i + 1 }))
    // awaiting_review is the ONLY state the review surfaces present (same rule
    // as the menubar cards and the review-request.json count).
    .filter((e) => candidateState(e.c) === "awaiting_review")
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
      // always carry the link (approve_drafts forces TWITTER_TAIL_LINK_RATE=1.0), so
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
    m = /\[post\] candidate (\d+) log_post\.py did not return post_id/.exec(line);
    if (m) {
      // The reply IS live on X; only the posts-row INSERT failed (e.g. the
      // 2026-07-06 draft_prompt_variant validation 400). Mark the card POSTED
      // so the drain never re-attempts a live reply — re-posting slams into
      // X's duplicate guard at best and double-posts at worst. The missing
      // posts row is recoverable from the post-*.log (reply_url + final_text).
      upsert(m[1], "posted", "log_post_no_id");
      continue;
    }
    m = /\[post\] candidate (\d+) crashed:/.exec(line);
    if (m) upsert(m[1], "failed", "exception");
  }
  return [...byId.values()];
}

// Resolve the posting @handle through the ONE resolver (scripts/account_resolver.py:
// env AUTOPOSTER_TWITTER_HANDLE -> config.json accounts.twitter.handle -> the durable
// connect-time cookie mirror). Deferring to it, instead of re-reading config.json here,
// keeps a single resolution path shared with the poster (twitter_browser.our_handle),
// and keeps the handle in a single durable home (the cookie mirror) rather than a
// second copy in config.json. Returns the bare handle (no @) or null; the preflight
// refuses loudly on null so a missing handle fails ONCE, not as N silent per-reply
// no_account_configured skips (and never as a hardcoded impersonation fallback).
async function resolvePostingHandle(): Promise<string | null> {
  const env = (process.env.AUTOPOSTER_TWITTER_HANDLE || "").trim().replace(/^@/, "");
  if (env) return env;
  try {
    const r = await runPython("scripts/account_resolver.py", ["twitter"], {
      timeoutMs: 30_000,
      env: ({ S4L_REPO_DIR: repoDir(), PATH: pipelinePath() }),
    });
    const h = (r.stdout || "").trim().replace(/^@/, "");
    return h || null;
  } catch {
    return null;
  }
}

async function ensureTwitterBrowserForPost() {
  const chrome = resolveChrome();
  const env: NodeJS.ProcessEnv = {
    S4L_REPO_DIR: repoDir(),
    S4L_PYTHON: resolvePython(),
    PATH: pipelinePath(),
    TWITTER_CDP_URL: process.env.TWITTER_CDP_URL || "http://127.0.0.1:9555",
  };
  if (chrome) env.BH_CHROME_BIN = chrome;
  return run(
    "bash",
    ["-lc", ". skill/lib/twitter-backend.sh && ensure_twitter_browser_for_backend"],
    {
      timeoutMs: 90_000,
      env: env as Record<string, string>,
      onLine: (line: string) => {
        const t = line.replace(/\s+$/, "");
        if (t.trim()) console.error(`[post-browser] ${t}`);
      },
    }
  );
}

// A terminal stamp written by merge_review_queue.py's backend sync for a row the
// freshness gate merely EXPIRED (not posted, not skipped) records "nobody decided
// this in time" — an explicit human approval outranks it. The poster's own
// at-post-time tweet_unavailable check remains the real gate on whether the
// thread still exists. Without this override, a card approved while (or just
// before) the sync stamped it is refused as already-decided and the approval
// silently no-ops (2 of 3 approvals lost on 2026-07-10).
// Give-up bound for approved cards whose post attempts keep failing
// transiently (browser lock contention, timeouts). 5 attempts spans several
// drain cycles — plenty for genuine transients to clear — while guaranteeing
// no card can retry forever (2026-07-17 zombie-card incident).
const MAX_POST_ATTEMPTS = 5;

function expiredStampOverridable(c: PlanCandidate): boolean {
  return (
    c.terminal === true &&
    c.posted !== true &&
    c.discard_reason === "backend_status_expired"
  );
}

// Prompt-sandbox replay cards (run-twitter-cycle.sh S4L_SANDBOX_CANDIDATES_FILE)
// carry experiments.sandbox=true; older sandbox rows predate that stamp but all
// use the synthetic >=900,000,000 id range twitter_prompt_sandbox.py assigns.
function isSandboxCandidate(c: PlanCandidate): boolean {
  const exps = (c as Record<string, unknown>).experiments;
  if (exps && typeof exps === "object" && (exps as Record<string, unknown>).sandbox)
    return true;
  const id = Number(c.candidate_id);
  return Number.isFinite(id) && id >= 900_000_000;
}

// Canonical review-queue candidate lifecycle state — the TS mirror of
// mcp/menubar/s4l_state.py::candidate_state(). The two MUST stay in lockstep:
// every divergence between a hand-rolled raw-flag filter here and the Python
// state machine has produced a real incident (menubar skipped post_failed cards
// the drain swept, get_stats lane blindness — the "never hand-roll review-queue
// state" rule has now recurred three times). READ state through this function;
// stamping outcomes (writes) still sets the raw flags directly.
// Precedence: posted > terminal > post_failed > approved > awaiting_review.
type CandidateState = "posted" | "terminal" | "post_failed" | "approved" | "awaiting_review";
function candidateState(c: PlanCandidate): CandidateState {
  if (c.posted === true) return "posted";
  if (c.terminal === true) return "terminal";
  if (c.post_failed) return "post_failed";
  if (c.approved === true) return "approved";
  return "awaiting_review";
}

// Write field patches into the review-queue store UNDER ITS LOCK by shelling
// to scripts/store_patch.py, which takes the same fcntl.flock the menubar's
// _store_update and merge_review_queue.py hold around their read-modify-write.
// Node has no native flock, and this process writing the store directly was
// the last unlocked writer (the race that erased posted stamps on 2026-07-17).
// Returns false on any failure so callers can fall back to the legacy write.
async function patchReviewStore(patches: object[]): Promise<boolean> {
  if (!patches.length) return true;
  const tmp = path.join(
    TMP_DIR,
    `s4l-store-patches-${process.pid}-${Date.now()}.json`
  );
  try {
    fs.writeFileSync(tmp, JSON.stringify({ patches }), "utf-8");
    const res = await runPython("scripts/store_patch.py", [tmp], {
      timeoutMs: 30_000,
      env: { S4L_REPO_DIR: repoDir(), PATH: pipelinePath() },
    });
    return res.code === 0;
  } catch {
    return false;
  } finally {
    try {
      fs.unlinkSync(tmp);
    } catch {
      /* best effort */
    }
  }
}

async function mergeApprovedStampsIntoStore(batchId: string, plan: Plan, stamped: PlanCandidate[]) {
  // Merge posted/terminal stamps into a FRESH read of the store instead of
  // rewriting the whole plan from the copy taken minutes ago. The old
  // whole-file write was last-writer-wins: while a batch posted, the menubar
  // (decision re-stamps) and any peer drain also wrote the store, and
  // whichever run finished last erased the others' posted flags (2026-07-06:
  // card 344877 posted at 00:29Z ended `posted=None,
  // terminal=duplicate_thread_pre_post` after a later run's stale write).
  // Merge rules: `posted` is sticky and wins over terminal; `terminal` never
  // overwrites a fresh `posted=true`. Fallback: candidates without a
  // candidate_id can't be matched into the fresh copy, so keep the legacy
  // whole-plan write for those older plans.
  //
  // Review-queue store: go through the LOCKED patch path (store_patch.py)
  // first. The fresh-read merge below closes most of the race window but not
  // all of it — a menubar decision landing between our readPlan and writePlan
  // still gets erased. The locked path holds the store's flock for the whole
  // read-mutate-replace, applies to every sibling row sharing a candidate_id,
  // and enforces the same posted-sticky rules. Legacy path stays as the
  // fallback and for per-batch /tmp plans (single writer, no lock needed).
  try {
    const mergeableForPatch = stamped.every(
      (c) => c.candidate_id !== undefined && c.candidate_id !== null
    );
    if (batchId === REVIEW_QUEUE_ID && mergeableForPatch) {
      const patches = stamped.map((c) => {
        const set: Record<string, unknown> = {};
        const unset: string[] = [];
        if (c.posted === true) {
          set.posted = true;
          set.terminal = false;
          if (c.our_url) set.our_url = c.our_url;
          // Clear a stale failure stamp too: the menubar's per-card call can
          // see posted=0 (the batch drain posted it under a different call)
          // and stamp post_failed on a card that IS live (seen on 565462,
          // 2026-07-17). posted is the settled truth; the residue just adds a
          // false "didn't post" signal to dashboards/notifications.
          unset.push("discard_reason", "post_failed", "post_error");
        } else if (c.terminal === true) {
          set.terminal = true;
          set.terminal_reason = c.terminal_reason ?? null;
          // See the zombie-card note in the legacy branch below: a terminal
          // from a real post attempt must clear the overridable expiry stamp.
          unset.push("discard_reason");
        }
        if (typeof c.post_attempts === "number") set.post_attempts = c.post_attempts;
        return { candidate_id: c.candidate_id, set, unset };
      });
      if (await patchReviewStore(patches)) return;
      console.error(
        "[post] store_patch.py failed; falling back to unlocked stamp merge"
      );
    }
  } catch {
    /* fall through to the legacy write */
  }
  try {
    const mergeable = stamped.every(
      (c) => c.candidate_id !== undefined && c.candidate_id !== null
    );
    const fresh = mergeable ? readPlan(batchId) : null;
    if (fresh && Array.isArray(fresh.candidates)) {
      // candidate_id is NOT unique in the review-queue store: sandbox reruns
      // and re-merged drafts append sibling rows with the same id. Stamping
      // only one sibling (the old Map single-slot) left the others matching
      // the drain's approved && !posted && !terminal filter, so the backlog
      // re-drained the same candidate every heartbeat forever (2026-07-17
      // incident: 23-card loop at 60s cadence). Stamp EVERY row with the id.
      const freshById = new Map<string, PlanCandidate[]>();
      fresh.candidates.forEach((c: PlanCandidate) => {
        if (c.candidate_id !== undefined && c.candidate_id !== null) {
          const key = String(c.candidate_id);
          const list = freshById.get(key);
          if (list) list.push(c);
          else freshById.set(key, [c]);
        }
      });
      for (const c of stamped) {
        for (const f of freshById.get(String(c.candidate_id)) ?? []) {
          if (c.posted === true) {
            f.posted = true;
            f.terminal = false;
            if (c.our_url) f.our_url = c.our_url;
            // A post outcome closes the card's history: the pre-approval
            // freshness stamp must not survive it. Same for a stale
            // post_failed from a peer call's posted=0 misattribution (see
            // the locked-patch branch above).
            delete f.discard_reason;
            delete (f as Record<string, unknown>).post_failed;
            delete (f as Record<string, unknown>).post_error;
          } else if (c.terminal === true && f.posted !== true) {
            f.terminal = true;
            f.terminal_reason = c.terminal_reason;
            // CRITICAL (2026-07-17 Nhat zombie-card incident, 438 retries over
            // 5 days): a stale discard_reason="backend_status_expired" left on
            // the fresh copy makes expiredStampOverridable() treat THIS
            // post-outcome terminal as overridable, so every later drain
            // resurrects the card, re-posts, dedup-skips, and re-stamps —
            // forever. A terminal that came from an actual post attempt is
            // final; delete the expiry stamp so the override can never fire
            // on it again. (The drain's own `delete c.discard_reason` happens
            // on an in-memory copy that is never the write source; this line
            // is the one that persists.)
            delete f.discard_reason;
          }
          // Carry the retry counter so the give-up bound survives across
          // drains (each drain reads the store fresh).
          if (typeof c.post_attempts === "number" && c.post_attempts > ((f.post_attempts as number) || 0))
            f.post_attempts = c.post_attempts;
        }
      }
      writePlan(batchId, fresh);
    } else {
      writePlan(batchId, plan);
    }
  } catch {
    try {
      writePlan(batchId, plan);
    } catch {
      /* best effort */
    }
  }
}

async function postApproved(batchId: string, plan: Plan) {
  // Drain serialization (2026-07-06 incident). Every call drains the WHOLE
  // approved backlog, so overlapping drains are pure waste and actively harmful:
  // a Claude restart mid-drain at 00:25Z left 10 landed replies unstamped, then
  // restart recovery + per-approval calls launched 8 concurrent approved=14
  // drains that re-attempted already-replied threads, fought over the browser
  // lock, and clobbered each other's posted stamps. Wait for any in-flight drain
  // (ours via `postingActive`, a sibling MCP's via the posting flag on disk)
  // instead of stacking a new one. 8-minute cap: comfortably above a normal
  // drain, comfortably below the menu bar's 900s loopback timeout so a waiting
  // call still returns to its caller. After a wait, RE-READ the plan so the
  // peer's posted/terminal stamps shrink our backlog before we attempt anything.
  {
    const gateDeadline = Date.now() + 8 * 60_000;
    let waited = false;
    let waitStartedAt = 0;
    while ((postingActive || isPeerDrainActive()) && Date.now() < gateDeadline) {
      if (!waited) {
        waitStartedAt = Date.now();
        // This loop was previously silent end-to-end (2026-07-08 forensics: a
        // ~1:45 gap in a user's approval batch had zero trace anywhere). Log
        // once on entry (not per 5s tick, to avoid spamming the file) so a
        // later "why did card N take so long" question has a starting point.
        logPostEvent(`postApproved_wait_start batch=${batchId} blocked_by=${describePostingBlocker()}`);
      }
      waited = true;
      await sleepMs(5000);
    }
    const timedOut = postingActive || isPeerDrainActive();
    if (waited) {
      logPostEvent(
        `postApproved_wait_end batch=${batchId} waited_ms=${Date.now() - waitStartedAt} timed_out=${timedOut}`
      );
    }
    if (timedOut) {
      return {
        attempted: 0,
        exit_code: 0,
        summary:
          "another posting drain has been running for 8+ minutes; approved cards stay " +
          "queued in the review store — re-run approve_drafts once it finishes",
      };
    }
    if (waited) {
      const fresh = readPlan(batchId);
      if (fresh) plan = fresh;
    }
  }
  // Arm the cross-instance posting flag HERE, before the Reddit drain, not just
  // before the Twitter phase below. Root cause (found 2026-07-16): this flag
  // used to get set only right before ensureTwitterBrowserForPost(), AFTER the
  // whole Reddit drain loop above (one runPython("post_reddit.py") call per
  // approved Reddit card, each with real CDP/browser overhead — a batch of a
  // handful of cards routinely runs minutes). For that entire window this
  // instance's isPeerDrainActive() reported false, so a SECOND MCP instance's
  // postApproved() (e.g. a fresh one-shot worker boot running its own startup
  // backlog drain) could pass the SAME gate check above and start its own
  // drain concurrently. Both eventually reached their own Twitter phase and
  // both spawned scripts/twitter_post_plan.py with S4L_LOCK_ROLE=post — the
  // ONLY code path that ever sets that role. Two live "post" role holders
  // can't preempt each other (browser_mutex.py case 6: correct, preempting a
  // peer mid-post risks a double-post), so one always waited 45s and gave up
  // — the "Twitter browser locked by session python:<pid> ... waited 45s,
  // giving up" failures that silently killed 11 real approved drafts over
  // 2026-07-15/16. Arming the flag for the FULL function closes that window:
  // a peer's gate check now blocks for the entire drain, Reddit included, so
  // only one instance ever reaches the Twitter phase at a time.
  postingActive = true;
  startPostingFlagHeartbeat();
  // Post every card the user APPROVED that hasn't already landed or been ruled out.
  // `approved` is now a DURABLE decision (sticky, never cleared by a later call), so
  // filtering out posted/terminal here makes this idempotent: re-running it only
  // drains the not-yet-posted approved backlog (e.g. a card a restart interrupted),
  // never re-posts a done one. This is what lets the startup backlog-drain and the
  // per-card menu-bar calls share one code path safely.
  // An approved card whose only blocker is an overridable backend-expiry stamp is
  // included: approval outranks the freshness gate (see expiredStampOverridable),
  // and the stamp is cleared so every downstream terminal check agrees it's live.
  // Drain-eligible, in canonical state terms (candidateState — keep in lockstep
  // with s4l_state.candidate_state). DELIBERATE deviations from a plain
  // state === "approved" check, each load-bearing:
  //   - post_failed cards DO drain again (the transient-retry path, bounded by
  //     MAX_POST_ATTEMPTS at result time). The menubar's restart-resume path
  //     (store_pending_posts) skips them by design; this drain is where they
  //     get their bounded retries.
  //   - an approved card whose ONLY terminal stamp is the freshness gate's
  //     backend_status_expired is resurrected: the human approval outranks the
  //     expiry (2 of 3 approvals lost on 2026-07-10 without this).
  //   - sandbox replays never drain (twitter_post_plan.py post_one()
  //     hard-refuses them, so draining one is pure churn and can loop forever
  //     if its terminal stamp loses a store-write race).
  const approved = (plan.candidates || []).filter((c: PlanCandidate) => {
    if (isSandboxCandidate(c)) return false;
    if (c.approved !== true) return false;
    const s = candidateState(c);
    return (
      s === "approved" ||
      s === "post_failed" ||
      (s === "terminal" && expiredStampOverridable(c))
    );
  });
  for (const c of approved) {
    if (c.terminal === true) {
      c.terminal = false;
      delete c.discard_reason;
    }
  }
  if (approved.length === 0) {
    postingActive = false;
    stopPostingFlagHeartbeat();
    return { attempted: 0, exit_code: 0, summary: "nothing approved" };
  }

  // ---- Reddit cards (2026-07-14): drain them FIRST, independently ----------
  // A reddit card carries the verbatim draft decision (reddit_decision) plus
  // plan metadata (reddit_plan_meta), so approval reconstructs a one-decision
  // plan and reuses `post_reddit.py --phase post` unchanged — per-row
  // reddit-browser lease, URL wrapping, campaign suffixes, and log_post all
  // stay in the one battle-tested poster. The twitter handle preflight and
  // twitter-browser lock ceremony below are twitter-only, so reddit must not
  // be gated on them (and an all-reddit batch returns before any of it).
  const approvedReddit = approved.filter(
    (c: PlanCandidate) => (c as Record<string, unknown>).platform === "reddit"
  );
  const approvedTwitter = approved.filter(
    (c: PlanCandidate) => (c as Record<string, unknown>).platform !== "reddit"
  );
  let redditPosted = 0;
  let redditFailed = 0;
  let redditSkippedPeerDrain = false;
  if (approvedReddit.length) {
    // Cross-instance drain gate (2026-07-14 drain storm): every one-shot MCP
    // boot (the queue worker fires ~every minute; each boot runs the startup
    // backlog drain) drained the SAME sticky approvals concurrently — 26
    // drain plans in ~30 minutes, all fighting over the one reddit tab and
    // false-positiving each other's posts. reddit-posting-active.json is the
    // arbiter: post_reddit.py heartbeats it per row, and we stamp it around
    // the whole drain here. A fresh flag (<120s) means a peer is mid-drain:
    // leave the cards for it — approvals are sticky, nothing is lost.
    const redditFlagPath = path.join(s4lStateDir(), "reddit-posting-active.json");
    let redditPeerFresh = false;
    try {
      redditPeerFresh = Date.now() - fs.statSync(redditFlagPath).mtimeMs < 120_000;
    } catch {
      redditPeerFresh = false;
    }
    const stampRedditFlag = () => {
      try {
        fs.writeFileSync(
          redditFlagPath,
          JSON.stringify({ pid: process.pid, hb: Math.floor(Date.now() / 1000) })
        );
      } catch {
        /* best effort */
      }
    };
    if (redditPeerFresh) {
      redditSkippedPeerDrain = true;
      logPostEvent(
        `postApproved_reddit_skip batch=${batchId} reason=peer_drain_active cards=${approvedReddit.length}`
      );
    } else {
      stampRedditFlag();
      try {
    for (const c of approvedReddit) {
      stampRedditFlag();
      const cc = c as unknown as Record<string, unknown>;
      const dec = cc.reddit_decision as Record<string, unknown> | undefined;
      const meta = (cc.reddit_plan_meta || {}) as Record<string, unknown>;
      if (!dec) {
        c.terminal = true;
        c.terminal_reason = "reddit_decision_missing";
        redditFailed++;
        continue;
      }
      // The card's reply_text is the CANONICAL text (approve_drafts edits write
      // there); the embedded decision still carries the draft-time original.
      // Posting dec.text verbatim would silently discard a human edit.
      const decPost: Record<string, unknown> = {
        ...dec,
        text: (typeof cc.reply_text === "string" && cc.reply_text.trim())
          ? cc.reply_text
          : dec.text,
      };
      if (cc.engagement_style) decPost.engagement_style = cc.engagement_style;
      // Two-draft cards (2026-07-15): a human draft-switch (see the generic
      // edit-handling above, `if (e.variant && Array.isArray(c.drafts))`)
      // stamps the CHOSEN draft's assigned_style/assigned_mode onto the card
      // (cc). Forward both into decPost so post_reddit.py's _post_iteration
      // validates/logs against whichever draft is ACTUALLY posting, not
      // whichever was recommended at plan-write time — mirrors
      // twitter_post_plan.py's identical per-candidate override.
      if (cc.assigned_style !== undefined) decPost.assigned_style = cc.assigned_style;
      if (cc.assigned_mode !== undefined) decPost.assigned_mode = cc.assigned_mode;
      const miniPlan = {
        project_name: meta.project_name || cc.matched_project,
        batch_id: cc.reddit_batch_id || meta.batch_id || "reddit-mcp-approval",
        phase: "draft",
        decisions: [decPost],
        style_assignment: meta.style_assignment || {},
        generation_trace_path: meta.generation_trace_path,
        session_id: meta.session_id || meta.draft_session_id,
      };
      const tmpPlan = path.join(
        process.env.S4L_TMP_DIR || "/tmp",
        `reddit_mcp_post_${Date.now()}_${Math.floor(Math.random() * 1e6)}.json`
      );
      let r: { code: number; stdout: string; stderr: string };
      try {
        fs.writeFileSync(tmpPlan, JSON.stringify(miniPlan));
        r = await runPython("scripts/post_reddit.py", ["--phase", "post", "--in", tmpPlan], {
          timeoutMs: 900_000,
          env: {
            REDDIT_CDP_URL: process.env.REDDIT_CDP_URL || "http://127.0.0.1:9557",
            // Reviewed posts never get the active-campaign suffix (same rule
            // as the twitter manual-approval path below).
            S4L_SKIP_CAMPAIGN_SUFFIX: "1",
          },
          onLine: (line: string) => {
            const t = line.replace(/\s+$/, "");
            if (t.trim()) console.error(`[post-reddit] ${t}`);
          },
        });
      } catch (err) {
        r = { code: -1, stdout: "", stderr: String(err) };
      } finally {
        try {
          fs.unlinkSync(tmpPlan);
        } catch {
          /* best effort */
        }
      }
      // post_reddit.py's summary marker: `[post_reddit] phase=post ... posted=N failed=M`
      const out = `${r.stdout}\n${r.stderr}`;
      const postedN = Number((/posted=(\d+)/.exec(out) || [])[1] || 0);
      if (r.code === 0 && postedN > 0) {
        c.posted = true;
        c.terminal = false;
        const urlMatch = /(https:\/\/(?:old\.|www\.)?reddit\.com\/r\/\S+)/.exec(
          (/\[post_reddit\][^\n]*posted[^\n]*/i.exec(out) || [""])[0]
        );
        if (urlMatch) (c as Record<string, unknown>).our_url = urlMatch[1];
        redditPosted++;
      } else {
        // Leave the approval sticky (approved && !posted && !terminal) so the
        // next approve_drafts call retries, mirroring twitter's failed-drain
        // semantics; only stamp terminal on a conclusive CDP refusal. This
        // list MUST stay a superset of post_reddit.py's own
        // _PERMANENT_CDP_ERRORS (account_blocked_in_sub, no_permalink) —
        // otherwise post_reddit.py already gives up on the row (marks the DB
        // candidate permanently failed, e.g. adds the sub to
        // subreddit_bans.comment_blocked) while this card stays non-terminal
        // and the drain retries the same doomed post forever (found
        // 2026-07-16: an approved r/tifu card retried every cycle for over
        // an hour after the sub was already in subreddit_bans.comment_blocked).
        const cdpReason = (/\[post_reddit\] CDP FAILED: ([a-z_]+)/.exec(out) || [])[1];
        if (
          cdpReason &&
          [
            "thread_locked",
            "thread_archived",
            "thread_not_found",
            "blocked_by_author",
            "account_blocked_in_sub",
            "no_permalink",
          ].includes(cdpReason)
        ) {
          c.terminal = true;
          c.terminal_reason = `reddit_${cdpReason}`;
        }
        redditFailed++;
      }
    }
      } finally {
        // Clear only OUR stamp; a peer that took over mid-drain keeps its own.
        try {
          const cur = JSON.parse(fs.readFileSync(redditFlagPath, "utf-8"));
          if (cur && cur.pid === process.pid) fs.unlinkSync(redditFlagPath);
        } catch {
          /* best effort */
        }
      }
      logPostEvent(
        `postApproved_reddit batch=${batchId} attempted=${approvedReddit.length} posted=${redditPosted} failed=${redditFailed}`
      );
    }
  }
  if (approvedTwitter.length === 0) {
    // All-reddit batch: persist the stamps and return without touching the
    // twitter preflight/lock path. Must clear the flag armed above ourselves —
    // this branch never reaches the Twitter phase's scheduleShellLockRelease().
    postingActive = false;
    stopPostingFlagHeartbeat();
    if (redditSkippedPeerDrain) {
      return {
        attempted: 0,
        posted: 0,
        exit_code: 0,
        summary: "reddit: skipped (peer drain active); cards stay approved for the next drain",
      };
    }
    if (approvedReddit.length) await mergeApprovedStampsIntoStore(batchId, plan, approvedReddit);
    return {
      attempted: approvedReddit.length,
      posted: redditPosted,
      exit_code: redditFailed ? 1 : 0,
      summary: `reddit: posted=${redditPosted} failed=${redditFailed}`,
    };
  }
  // PREFLIGHT: posting needs a configured @handle, or twitter_browser.py refuses
  // EVERY reply with no_account_configured and the whole batch skips — invisibly.
  // If onboarding never persisted it, self-heal from the live session; if even that
  // can't determine it, refuse here with a clear reason rather than launching a
  // poster that silently burns the whole batch.
  const postingHandle = await resolvePostingHandle();
  if (!postingHandle) {
    postingActive = false;
    stopPostingFlagHeartbeat();
    return {
      attempted: 0,
      exit_code: 0,
      posted: 0,
      summary: "no_account_configured",
      error:
        "X is connected but no posting @handle could be resolved (env, config, or the " +
        "connect-time cookie mirror), so every reply would be refused (no_account_configured). " +
        "Re-run project_config action:'connect_x' to re-capture the handle.",
    };
  }
  // Mark posting active so the draft-cycle scan DEFERS launching any scan for the
  // duration of this batch (+ grace). This is the source-level mutual exclusion
  // that actually fixes the hijack: the autopilot never launches a scan to race
  // the post for the browser. Reset is guaranteed by scheduleShellLockRelease()
  // in the finally below, so an early/failed post can't wedge scanning.
  postingActive = true;
  startPostingFlagHeartbeat(); // cross-instance: a sibling MCP's scan defers too
  // Posting is a priority over scanning: abort any in-flight pipeline scan so the
  // approved post takes the browser immediately instead of waiting on the lock.
  preemptScanForPost();
  // Hold the /tmp shell browser lock (the one the scanner respects) for the WHOLE
  // batch so the every-minute autopilot scan queues behind the post instead of
  // seizing Chrome mid-batch — the root cause of approved batches landing 0/N.
  const heldShellLock = await acquireShellBrowserLock();
  if (!heldShellLock) {
    // acquireShellBrowserLock now preempts (SIGKILLs) whatever holds this lock
    // unconditionally, so reaching `false` here means even reclaiming the dir
    // across 8 attempts didn't stick (e.g. something is re-taking it faster than
    // we can write our own pid) — a pathological edge case, not the normal path.
    // Bail out rather than proceed without ever confirming we hold it. Approved
    // cards stay sticky (approved && !posted && !terminal), so the very next
    // approve_drafts call — the next approval, or this same tool retried — picks
    // them straight back up; nothing is lost or re-queued.
    postingActive = false;
    stopPostingFlagHeartbeat();
    return {
      attempted: 0,
      exit_code: 0,
      summary:
        "couldn't pin down the twitter-browser lock after repeated attempts; approved cards " +
        "stay queued — re-run approve_drafts to retry",
    };
  }
  const approvedBatch = `${batchId}_approved`;
  writePlan(approvedBatch, { ...plan, candidates: approvedTwitter });
  // S4L_SKIP_CAMPAIGN_SUFFIX=1: manual/reviewed posts from this MCP draft_cycle
  // never get the active-campaign suffix (e.g. " written with ai") appended.
  // twitter_browser.py's reply handler reads this env (inherited through
  // twitter_post_plan.py's subprocess). The cron pipeline doesn't set it, so the
  // A/B disclosure experiment keeps running on autopilot/cron and on Reddit.
  const res = await (async () => {
    try {
      const browser = await ensureTwitterBrowserForPost();
      if (browser.code !== 0) {
        const failure = {
          posted: 0,
          skipped: 0,
          failed: approvedTwitter.length,
          failure_reasons: "browser_bootstrap_failed",
          skip_reasons: "",
        };
        return {
          code: browser.code,
          stdout: `${JSON.stringify(failure)}\n`,
          stderr: [browser.stderr, browser.stdout].filter(Boolean).join("\n"),
        };
      }
      return await runPython(
        "scripts/twitter_post_plan.py",
        ["--plan", planPath(approvedBatch)],
        {
          // Scale with batch size: a mass-approval drain runs ~15-20s per card,
          // so a fixed 15min ceiling SIGTERMed any batch over ~50 cards mid-post
          // (Karol 2026-07-09: 0/131 posted, exit=-1). 60s/card headroom covers
          // slow candidates; the 2h cap bounds a hung poster (the browser-lock
          // expiry and per-reply subprocess timeouts still fire underneath).
          timeoutMs: Math.min(7_200_000, Math.max(900_000, approvedTwitter.length * 60_000)),
          env: ({
            S4L_SKIP_CAMPAIGN_SUFFIX: "1",
            // Manual approval is an EXCEPTION to the tail-link A/B. The cron pipeline
            // runs TWITTER_TAIL_LINK_RATE=0.9 (from .env) so ~10% of autopilot posts
            // ship link-less as an experiment arm. But when the user hand-reviews a
            // draft, sees the link target in the table, and approves it, dropping the
            // link is surprising and unwanted. Force 1.0 here so every approved draft
            // carries its link. This wins over .env / process.env because run() spreads
            // opts.env AFTER process.env, and twitter_post_plan.py never load_dotenv's
            // with override, so nothing clobbers it. Cron is untouched (it never goes
            // through this MCP path), so the 0.9 experiment keeps running there.
            //
            // 2026-07-06: the tail-link decision (link vs no_link) and the Claude
            // bridge call both moved to DRAFT time (scripts/twitter_gen_links.py's
            // Phase 2b-gen step, which stamps tail_link_variant + finalizes
            // reply_text before the review card is ever shown — see
            // twitter_post_plan.py's guard on tail_link_variant). That step reads
            // DRAFT_ONLY (forced to rate=1.0 there) to guarantee a hand-approved
            // card never drops the link it already shows. So both env vars below
            // are now no-ops for the normal path — every approved candidate
            // already carries tail_link_variant by the time it reaches this MCP
            // tool. They're left in place as a defense-in-depth fallback for the
            // rare case a candidate reaches approve_drafts unstamped (e.g. a plan
            // already in flight from before this change): S4L_SKIP_LINK_TAIL=1
            // still guarantees approve_drafts (a synchronous call the user is
            // waiting on) never makes a blocking Claude/queue call at post time,
            // no matter what.
            TWITTER_TAIL_LINK_RATE: "1.0",
            S4L_SKIP_LINK_TAIL: "1",
            // The poster attaches to the twitter-harness Chrome over CDP. The cron
            // pipeline exports this from skill/lib/twitter-backend.sh; the MCP path
            // must set it explicitly or twitter_browser.py fails with "No twitter-
            // harness Chrome reachable". Honor an inherited value (AppMaker / VM
            // BYO-Chrome), else default to the local harness on port 9555.
            TWITTER_CDP_URL: process.env.TWITTER_CDP_URL || "http://127.0.0.1:9555",
          }),
          // Stream the poster's output live so HANDLED
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
      `# approve_drafts batch=${batchId} approved=${approvedTwitter.length} exit=${res.code} ` +
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
        ? approvedTwitter.length
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
  approvedTwitter.forEach((c) => {
    if (c.candidate_id !== undefined && c.candidate_id !== null)
      approvedById.set(String(c.candidate_id), c);
  });
  let touchedPlan = false;
  if (resultRows.length) {
    resultRows.forEach((r, idx) => {
      const c = approvedById.get(r.candidate_id) || approvedTwitter[idx];
      if (!c) return;
      if (r.outcome === "posted") {
        c.posted = true;
        c.terminal = false;
        if (r.our_url) c.our_url = r.our_url;
        touchedPlan = true;
      } else if (r.outcome === "skipped") {
        // twitter_post_plan.py only returns "skipped" for reasons it has
        // already decided are conclusive (reply_restricted, tweet_unavailable,
        // blocked_by_author, tweet_not_found, rate_limited, reply_box_not_found
        // — see its own PERMANENT/skip comment) or a captured-URL edge case
        // it explicitly doesn't want re-attempted (duplicate risk). Terminal
        // is correct here.
        c.terminal = true;
        c.terminal_reason = r.reason || r.outcome;
        touchedPlan = true;
      } else if (r.outcome === "failed") {
        // "failed" is twitter_post_plan.py's catch-all for everything it did
        // NOT classify as conclusive — timeouts, parse errors, exceptions,
        // and browser_mutex contention ("Twitter browser locked by session
        // ... waited 45s, giving up."). None of those mean the draft is
        // doomed, only that this attempt didn't get a turn with the shared
        // browser. Leave the card sticky (approved && !posted && !terminal)
        // so the next approve_drafts call retries it, mirroring Reddit's
        // _TRANSIENT_CDP_ERRORS handling. Unconditionally marking terminal
        // here (found 2026-07-16) silently and permanently discarded 7 real
        // approved drafts on nothing worse than lock contention — Reddit's
        // equivalent transient failures self-healed on the very next drain.
        //
        // BOUNDED (2026-07-17): sticky is right, sticky-forever is not — an
        // unbounded retry is a zombie generator (one card retried 438 times
        // over 5 days on the Nhat install). Count the transient failures and
        // give up loudly after MAX_POST_ATTEMPTS; the terminal_reason keeps
        // the last failure visible so the give-up is diagnosable, and the
        // on-disk post-events trail records it for forensics.
        const attempts = (typeof c.post_attempts === "number" ? c.post_attempts : 0) + 1;
        c.post_attempts = attempts;
        if (attempts >= MAX_POST_ATTEMPTS) {
          c.terminal = true;
          c.terminal_reason = `gave_up_after_${attempts}_failed_attempts:${r.reason || "failed"}`;
          console.error(
            `[post] giving up on candidate ${r.candidate_id} after ${attempts} failed attempts (last: ${r.reason || "failed"})`
          );
          logPostEvent(`retry_budget_exhausted candidate=${r.candidate_id} attempts=${attempts} last=${r.reason || "failed"}`);
        }
        touchedPlan = true;
      }
    });
  } else if (realPosted > 0 || (res.code === 0 && !summObj)) {
    // Legacy fallback for older poster output without parseable per-candidate
    // lines. Mark only when we have no finer-grained signal.
    for (const c of approvedTwitter) c.posted = true;
    touchedPlan = true;
  }
  // Reddit stamps (set in the reddit drain above) merge alongside the twitter
  // ones: `approved` here spans both platforms.
  if (touchedPlan || redditPosted || redditFailed) {
    await mergeApprovedStampsIntoStore(batchId, plan, approved);
  }
  // Post failures are HANDLED in the pipeline (it returns a count, never throws),
  // so they never reach Sentry on their own. Capture an explicit event whenever
  // the run exited non-zero OR a REAL failure happened. This is the only
  // telemetry channel that reaches a customer .mcpb install (their cycle log
  // lives on their machine). install_id/hostname are auto-tagged.
  //
  // Gated on failure_reasons (not just realPosted < approved.length): the
  // Python pipeline already reports every shortfall to Sentry itself
  // (twitter_post_plan.py's capture_message), including benign skips like a
  // deleted target tweet. Re-reporting the SAME benign skip here as a second,
  // independently-fingerprinted `Error` (this capture group) doubled every
  // such event into two distinct Sentry issues. Only add this Node-side
  // capture when there's a real failure_reasons entry or a non-zero exit,
  // i.e. something the Python capture wouldn't already have flagged as an
  // actionable error on its own.
  const hasRealFailure = res.code !== 0 || Boolean(summObj?.failure_reasons);
  if (hasRealFailure) {
    captureError(
      new Error(`approve_drafts: ${realPosted}/${approvedTwitter.length} posted (exit=${res.code})`),
      {
        component: "post",
        exit_code: String(res.code),
        attempted: String(approvedTwitter.length),
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
    posted: realPosted + redditPosted,
    exit_code: res.code,
    summary:
      approvedReddit.length > 0
        ? { twitter: summary, reddit: { posted: redditPosted, failed: redditFailed } }
        : summary,
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
            "Set up social-autoposter plugin end to end now. Treat this as a terminal goal: inspect status, " +
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
// Cold-start QUERY supply, SHARED by both setup paths (product project_config
// AND personal-brand engagement_mode). Fans the agent-supplied search_queries
// into project_search_queries so the deterministic Phase 1 bank
// (qualified_query_bank.py) has a real bank on day one; without it the cycle
// falls back to ONE crude topic-as-query. The model may pass queries as a real
// array, a stringified JSON array, or a comma list (normalizeStringList handles
// all three — the Array.isArray-only gate silently dropped stringified arrays,
// leaving Karol's personal-brand bank empty, 2026-06-30). CLAUDE-FREE: seeds
// directly via --queries-json, never shells out to `claude -p`. Returns a human
// note for the setup summary plus the seeded query rows. Only call this once the
// project's TOPICS are seeded (the persona/product topic seed must succeed
// first), matching the product path's `seed.code === 0` guard.
type SeededQuery = { query: string; topic?: string };
// Background query-seeding state. The seed run (dedup + optional live
// supply-test against the X browser) can take 3-10+ minutes when the
// twitter-browser lock is contended, but Claude Desktop kills any MCP tool
// call at a hard 60s. Awaiting the seed inside set therefore GUARANTEED a
// client timeout, and each retry stacked another seed process on the browser
// lock (Karol, 2026-07-03). So the seed now runs fire-and-forget: `set`
// returns as soon as the durable writes land, and retries while a seed is
// in flight are cheap no-ops.
const seedInFlight = new Map<string, number>(); // project -> startedAt ms
async function seedSearchQueriesForProject(
  project: string,
  rawQueries: string[] | string | undefined
): Promise<{ note: string; queries: SeededQuery[] }> {
  const agentQueries = normalizeStringList(rawQueries) ?? [];
  if (!agentQueries.length) {
    return {
      note:
        " (No search_queries supplied, so the cycle will run off the seeded topics one at a time. " +
        "To fan out, re-run with a search_queries array of ~30 X search strings you expand from these " +
        "topics — it seeds them directly, no claude CLI.)",
      queries: [],
    };
  }
  // Echo the supplied queries back so callers can show the user the bank
  // without waiting for persistence.
  const queries: SeededQuery[] = agentQueries.map((q) => ({ query: q }));
  // A retry after a client-side timeout must NOT queue another seed process on
  // the twitter-browser lock. 20 min covers the worst case (600s lock wait +
  // the ~3 min live run); a stale entry past that is assumed dead.
  const started = seedInFlight.get(project);
  if (started && Date.now() - started < 20 * 60_000) {
    return {
      note:
        ` Query seeding for '${project}' is already running in the background from a previous call; ` +
        "this retry is a safe no-op. The bank will be live within a few minutes — do NOT re-run.",
      queries,
    };
  }
  try {
    const qfile = path.join(os.tmpdir(), `s4l-queries-${project}-${Date.now()}.json`);
    fs.writeFileSync(
      qfile,
      JSON.stringify({ queries: agentQueries.map((q) => ({ query: q, topic: "" })) })
    );
    seedInFlight.set(project, Date.now());
    // Fire-and-forget: runPython keeps the output on the repo.ts tee (so the
    // whole run still lands in the Cloud Logging relay), but the tool response
    // does not wait for it. The script is idempotent (dedup by normalized
    // core), so even a duplicate run after the in-flight window is harmless.
    void runPython(
      "scripts/seed_search_queries.py",
      ["--project", project, "--queries-json", qfile, "--supply-test", "auto", "--emit-json"],
      { timeoutMs: 900_000 }
    )
      .then((qseed) => {
        const qm = /seeded=(\d+)\s+inserted=(\d+)\s+updated=(\d+)/.exec(qseed.stdout);
        console.error(
          `[seed_search_queries] background seed for '${project}' finished: ` +
            (qseed.code === 0
              ? qm
                ? `seeded=${qm[1]} inserted=${qm[2]} updated=${qm[3]}`
                : "ok"
              : `exit ${qseed.code}: ${(qseed.stderr || qseed.stdout).trim().split("\n").slice(-1)[0] || "unknown error"}`)
        );
      })
      .catch((e) => {
        console.error(
          `[seed_search_queries] background seed for '${project}' failed:`,
          (e as Error)?.message || e
        );
        captureError(e, { component: "seed_search_queries", project });
      })
      .finally(() => {
        seedInFlight.delete(project);
        try {
          fs.unlinkSync(qfile);
        } catch {
          /* best-effort cleanup */
        }
      });
    return {
      note:
        ` Queued ${agentQueries.length} search quer${agentQueries.length === 1 ? "y" : "ies"} for ` +
        "background seeding (dedup + live supply-test). They persist automatically within a few " +
        "minutes and the cycle picks them up on its own — no need to wait, verify, or re-run this call.",
      queries,
    };
  } catch (e) {
    seedInFlight.delete(project);
    return { note: ` (Search-query seeding skipped — ${(e as Error).message}.)`, queries };
  }
}

// After a project save, persist the profile scan's engagement-ranked top
// replies into that project's voice.examples (and the persona_corpus.txt
// exemplar section when the project is the persona). Every drafting prompt on
// every platform already mirrors voice.examples, so this ONE write feeds them
// all. scripts/voice_exemplars.py reads the last_profile_scan.json sidecar
// scan_x_profile.py wrote and only quotes the user's own public replies
// verbatim (mechanical ranking, no synthesis). Best-effort by design: no scan
// yet, no usable replies, or hand-written voice.examples (exit 3, respected)
// all return null and never block the save.
async function applyScannedVoiceExamples(project: string): Promise<string | null> {
  try {
    const res = await runPython("scripts/voice_exemplars.py", ["apply", "--project", project], {
      timeoutMs: 30_000,
    });
    const last = res.stdout.trim().split("\n").slice(-1)[0] || "";
    const parsed = JSON.parse(last) as { ok?: boolean; voice_examples_written?: number };
    if (parsed.ok && parsed.voice_examples_written) {
      return `Stored ${parsed.voice_examples_written} of their top-performing real replies (ranked by engagement, with the threads they answered) as voice.examples — every drafter now mirrors them.`;
    }
    return null;
  } catch {
    return null;
  }
}

// ---- engagement_mode: choose personal-brand vs product (setup-time) --------
// Part of onboarding: AFTER X connect + profile_scan, BEFORE product config, the
// agent asks the user which mode they want and calls this. It persists the mode
// (scripts/s4l_mode.py, the single source of truth the cycle reads) and
// provisions the persona project (grounded in the profile scan), then the agent
// continues to product setup — a product is always configured regardless of mode.
tool(
  "engagement_mode",
  {
    title: "Choose engagement lanes (personal brand + optional product promotion)",
    description:
      "Set or read the engagement LANES the autopilot drafts in. There are TWO independent lanes that " +
      "can BOTH be on (the cycle then splits per the configurable lane split, default 50/50 — see " +
      "action:'split'): PERSONAL BRAND (organic, link-free engagement in " +
      "the user's own voice — ON by default) and PRODUCT PROMOTION (the marketing pipeline, link " +
      "replies — OFF by default, opt-in). This is a SETUP step: AFTER X is connected, the profile " +
      "is scanned (for VOICE), and the user has answered the DICTATION interview (for TOPICS + corpus), " +
      "personal-brand is already the default, so ASK the user the ONE question: do they " +
      "ALSO want to promote a product? Then call action:'set' with personal_brand:true and " +
      "promotion:true|false. Pass the voice/description you captured from the scan, the search_topics " +
      "you extracted PRIMARILY from the dictation, and the raw dictation transcript as content_corpus, " +
      "so the persona is grounded in who they actually are, AND expand those topics into a search_queries " +
      "array of ~30 concrete X advanced-search strings in the SAME call (identical to project_config) " +
      "so the personal-brand cycle has a real query bank on day one instead of running one crude " +
      "topic-as-query. If they want promotion too, continue to configure the product project with " +
      "project_config afterward. The user flips either lane any time from the menu-bar checkmarks.",
    inputSchema: {
      action: z
        .enum(["get", "set", "toggle", "split"])
        .optional()
        .describe("get = read current lane flags + persona status + lane split. set = record the user's chosen lanes (provisions the persona). toggle = lightweight flip of ONE lane (pass `lane`); mode.json only, no persona work — the dashboard/menu-bar quick toggle. split = set the personal-brand share of both-lanes-on cycles (pass `split`); mode.json only — the dashboard slider."),
      personal_brand: z
        .boolean()
        .optional()
        .describe("action:'set' — turn the personal-brand lane on/off. Defaults to true (the out-of-the-box lane)."),
      promotion: z
        .boolean()
        .optional()
        .describe("action:'set' — turn the product-promotion lane on/off. Defaults to false; set true when the user says they also want to promote a product."),
      lane: z
        .enum(["personal_brand", "promotion"])
        .optional()
        .describe("action:'toggle' — which single lane to flip."),
      split: z
        .union([z.number(), z.string()])
        .optional()
        .describe(
          "action:'split' (or alongside action:'set') — the personal-brand SHARE of cycles when both " +
            "lanes are on: 0.7, 70, or '70%' all mean 70% personal brand / 30% promotion. Clamped to " +
            "0..1; ignored while only one lane is on (single-lane states run that lane every cycle)."
        ),
      mode: z
        .enum(["personal_brand", "promotion"])
        .optional()
        .describe(
          "LEGACY (compat). Single-lane shorthand for action:'set': turns the named lane ON and the " +
            "other OFF. Prefer the explicit personal_brand/promotion booleans."
        ),
      description: z
        .string()
        .optional()
        .describe("Persona grounding from the scan: 2-3 sentences on who this person is as a builder/voice."),
      content_angle: z
        .string()
        .optional()
        .describe("Persona grounding: a paragraph of concrete first-hand experience the persona speaks from, synthesized from the DICTATION interview (contrarian takes, earned expertise) with the scan as backup."),
      content_corpus: z
        .string()
        .optional()
        .describe(
          "The RAW voice-memo transcript from the onboarding dictation interview, VERBATIM (do NOT " +
            "paraphrase or summarize). Persisted to the persona_corpus.txt sidecar (never config.json), " +
            "capped ~100000 chars. This is the grounding pool the drafter quotes real specifics from " +
            "(actual projects, numbers, opinions, phrasing), so keep it dense and first-hand."
        ),
      voice: z
        .any()
        .optional()
        .describe("Persona voice object {tone, never:[...]} captured from how they actually write (the profile scan) and calibrated by the dictation (who they like/hate reading, phrases they overuse, off-limits)."),
      search_topics: z
        .union([z.array(z.string()), z.string()])
        .optional()
        .describe("~15 topics the persona has genuine experience with. Sourced PRIMARILY from the DICTATION interview (the 'subjects you could talk about for an hour' answer), with recurring themes from the profile scan as reinforcement. This is the ONLY field that changes what gets SCANNED on X, so it must reflect what the user WANTS to be in conversations about, not just what they already posted."),
      search_queries: z
        .union([z.array(z.string()), z.string()])
        .optional()
        .describe(
          "Cold-start X search-query bank YOU expand from search_topics, in THIS same call — same " +
            "as project_config. Fan each persona topic into a few concrete X advanced-search strings " +
            "(aim ~30 total, e.g. 'mac menu bar app -filter:replies', 'screen recording lang:en') so " +
            "the personal-brand cycle fans out instead of running one crude topic-as-query. Seeded " +
            "directly with NO `claude -p`. Without it the persona bank is empty on day one."
        ),
    },
  },
  async (args: any) => {
    const action = args.action || "get";

    const readFlags = async (): Promise<{ personal_brand: boolean; promotion: boolean }> => {
      const cur = await runPython("scripts/s4l_mode.py", ["flags"], { timeoutMs: 15_000 });
      try {
        const f = JSON.parse((cur.stdout || "").trim());
        return { personal_brand: !!f.personal_brand, promotion: !!f.promotion };
      } catch {
        return { personal_brand: true, promotion: false };
      }
    };

    const readSplit = async (): Promise<number> => {
      const r = await runPython("scripts/s4l_mode.py", ["split"], { timeoutMs: 15_000 });
      try {
        const v = Number(JSON.parse((r.stdout || "").trim()).personal_brand_share);
        return Number.isFinite(v) ? v : 0.5;
      } catch {
        return 0.5;
      }
    };

    if (action === "get") {
      const flags = await readFlags();
      const persona = findPersonaProject();
      const mode = flags.personal_brand ? "personal_brand" : "promotion";
      return jsonContent({
        flags,
        mode,
        personal_brand_share: await readSplit(),
        persona: persona ? persona.name : null,
      });
    }

    // Set the both-lanes-on split (the dashboard slider): just rewrite
    // mode.json via s4l_mode.py — NO persona provisioning, same weight class
    // as action:'toggle'.
    if (action === "split") {
      if (args.split === undefined || args.split === null || args.split === "") {
        return jsonContent({ personal_brand_share: await readSplit() });
      }
      const res = await runPython("scripts/s4l_mode.py", ["split", String(args.split)], {
        timeoutMs: 15_000,
      });
      if (res.code !== 0) {
        const tail = (res.stderr || res.stdout).trim().split("\n").slice(-1)[0] || "unknown error";
        return textContent(`Could not set the lane split: ${tail}`);
      }
      try {
        return jsonContent(JSON.parse((res.stdout || "").trim()));
      } catch {
        return jsonContent({ personal_brand_share: await readSplit() });
      }
    }

    // Lightweight flip of ONE lane (the dashboard/menu-bar quick toggle): just
    // rewrite mode.json via s4l_mode.py — NO persona provisioning. Mirrors the
    // menu bar's pure-local _toggle_lane so flipping from either surface is cheap.
    if (action === "toggle") {
      const lane = args.lane === "promotion" ? "promotion" : "personal_brand";
      const res = await runPython("scripts/s4l_mode.py", ["toggle", lane], { timeoutMs: 15_000 });
      if (res.code !== 0) {
        const tail = (res.stderr || res.stdout).trim().split("\n").slice(-1)[0] || "unknown error";
        return textContent(`Could not switch lane: ${tail}`);
      }
      try {
        return jsonContent({ flags: JSON.parse((res.stdout || "").trim()) });
      } catch {
        return jsonContent({ flags: await readFlags() });
      }
    }

    // action === 'set'. Resolve the two lane flags. Explicit booleans win; the
    // legacy `mode` shorthand maps to single-lane; default is personal ON.
    let personalBrand: boolean;
    let promotion: boolean;
    if (args.mode === "personal_brand" || args.mode === "promotion") {
      personalBrand = args.mode === "personal_brand";
      promotion = args.mode === "promotion";
    } else {
      personalBrand = args.personal_brand === undefined ? true : !!args.personal_brand;
      promotion = !!args.promotion;
    }
    if (!personalBrand && !promotion) {
      return textContent(
        "At least one lane must be on. personal_brand is the default; set promotion:true if the user " +
          "also wants product promotion (both on -> the cycle splits per the lane split, default 50/50)."
      );
    }
    const mode = personalBrand ? "personal_brand" : "promotion";

    recordOnboardingAttempt("mode_chosen", { personal_brand: personalBrand, promotion });

    const setRes = await runPython(
      "scripts/s4l_mode.py",
      ["set-flags", personalBrand ? "1" : "0", promotion ? "1" : "0"],
      { timeoutMs: 15_000 }
    );
    if (setRes.code !== 0) {
      const tail = (setRes.stderr || setRes.stdout).trim().split("\n").slice(-1)[0] || "unknown error";
      blockOnboardingMilestone("mode_chosen", "mode_set_failed", tail, { personal_brand: personalBrand, promotion });
      return textContent(`Couldn't save the engagement lanes: ${tail}`);
    }

    // Optional lane split rider on 'set' (best-effort: the lanes are saved
    // either way, and the split keeps its previous/default value on failure).
    if (args.split !== undefined && args.split !== null && args.split !== "") {
      await runPython("scripts/s4l_mode.py", ["split", String(args.split)], { timeoutMs: 15_000 });
    }

    // NOTE (2026-07-06): the draft-only flag (mode.json {"draft_only": false} —
    // promotion cycles POST autonomously instead of drafting cards) is
    // DELIBERATELY NOT exposed on this tool or any user surface. It is an
    // operator-only switch, set via `scripts/s4l_mode.py draft-only on|off`.
    // run-draft-and-publish.sh reads it per cycle. Do not add a tool param,
    // menubar toggle, or onboarding step for it.

    // Provision the persona (grounded from the scan when supplied) regardless of
    // mode, so the toggle always has a real persona to flip to.
    let personaName: string;
    let personaCreated = false;
    try {
      const r = ensurePersonaProject({
        description: args.description,
        content_angle: args.content_angle,
        voice: args.voice,
        search_topics: args.search_topics,
        content_corpus: args.content_corpus,
      });
      personaName = r.name;
      personaCreated = r.created;
    } catch (e: any) {
      blockOnboardingMilestone("mode_chosen", "persona_provision_failed", e?.message || String(e), { mode });
      return textContent(
        `Mode saved as ${mode}, but provisioning the persona project failed: ${e?.message || e}. ` +
          `Retry engagement_mode action:'set'.`
      );
    }

    // Persist the profile scan's top-performing real replies as the persona's
    // voice.examples + the persona_corpus.txt exemplar section (best-effort;
    // see applyScannedVoiceExamples).
    const personaExemplarNote = await applyScannedVoiceExamples(personaName);

    // Seed the persona's topics into the DB universe the cycle reads (best-effort;
    // the cycle's own fail-loud path still reports if topics are missing).
    let personaTopicsSeeded = false;
    let personaTopicCount = 0;
    const seed = await runPython("scripts/seed_search_topics.py", ["--project", personaName], {
      timeoutMs: 60_000,
    });
    if (seed.code === 0) {
      const m = /planned=(\d+)\s+inserted=(\d+)\s+updated=(\d+)/.exec(seed.stdout);
      personaTopicCount = m ? Number(m[1]) : 0;
      personaTopicsSeeded = true;
    }

    // Seed the persona's search QUERIES too — identical to the product path
    // (project_config). Personal-brand-only setups used to seed topics but never
    // queries, so their Phase 1 bank was empty and the cycle ran one crude
    // topic-as-query (Karol, 2026-06-30). Only after the topic seed succeeds.
    let personaQueryCount = 0;
    let personaQueryNote = "";
    if (personaTopicsSeeded) {
      const qr = await seedSearchQueriesForProject(
        personaName,
        args.search_queries as string[] | string | undefined
      );
      personaQueryCount = qr.queries.length;
      personaQueryNote = qr.note;
    }

    completeOnboardingMilestone("mode_chosen", { personal_brand: personalBrand, promotion, persona: personaName });

    // Personal-brand-only is a first-class setup path: the persona is the draftable
    // project, so seeding its topics IS the topics_seeded milestone. Without this the
    // product path (project_config) is the only place that completes it, leaving a
    // persona-only checklist stuck at "topics pending" even though topics are live.
    if (personalBrand && personaTopicsSeeded) {
      completeOnboardingMilestone("topics_seeded", {
        project: personaName,
        topic_count: personaTopicCount,
        persona: true,
      });
    }

    // Install/refresh the launchd kicker NOW. For a personal-brand-only setup the
    // persona is the only draftable project (no managed product), so nothing else
    // would trigger the install until a later queue-worker boot — leaving the user
    // with no autopilot and no drafts. ensureQueueKickerInstalled is persona-aware
    // (see its gate); fire it best-effort so the kicker is live the moment the
    // persona is seeded. (2026-06-30) Skipped when promotion-only, since the
    // product project isn't configured yet (it stays gated until project_config).
    let kickerInstall: { ok: boolean; detail: string } | null = null;
    if (personalBrand && isPaused()) {
      kickerInstall = { ok: false, detail: "skip (paused)" };
    } else if (personalBrand) {
      try {
        kickerInstall = await ensureQueueKickerInstalled();
        console.error(
          `[engagement_mode] launchd kicker: ${kickerInstall.ok ? "ok" : "skip"} (${kickerInstall.detail})`
        );
      } catch (e: any) {
        kickerInstall = { ok: false, detail: e?.message || String(e) };
        console.error("[engagement_mode] kicker install failed:", e?.message || e);
      }
    }

    const bothOn = personalBrand && promotion;
    const next_step = promotion
      ? (bothOn
          ? "Personal brand + product promotion are BOTH on (the cycle splits per the lane split, " +
            "default 50/50 — adjustable via action:'split' or the dashboard slider), and the persona " +
            "is provisioned + topic-seeded. "
          : "Product promotion is on and the persona is provisioned. ") +
        "NOW CONTINUE SETUP: configure the product project with project_config (research the product " +
        "site and fill description, icp, voice, search_topics, search_queries)."
      : (personaQueryCount > 0
          ? `Personal-brand lane is on (the default); the persona is provisioned, topic-seeded, and ${personaQueryCount} search quer${personaQueryCount === 1 ? "y" : "ies"} seeded, so there `
          : "Personal-brand lane is on (the default); the persona is provisioned + topic-seeded (but NO search_queries were supplied, so it will run one topic-as-query at a time — re-call engagement_mode action:'set' with a search_queries array of ~30 X search strings expanded from the persona topics to fan out). There ") +
        "is nothing more to configure (no product project is needed). NOW SCHEDULE THE AUTOPILOT: call " +
        "queue_setup and create each returned task with create_scheduled_task (prompt and " +
        "notifyOnCompletion verbatim — notifyOnCompletion must be false; 'already exists' is fine, but then " +
        "call update_scheduled_task with notifyOnCompletion:false to silence per-run notifications), " +
        "then call the dashboard tool to confirm the schedule is firing. " +
        "That is the final setup step — do NOT stop before the autopilot is scheduled.";

    return jsonContent({
      ok: true,
      flags: { personal_brand: personalBrand, promotion },
      mode,
      persona: personaName,
      persona_created: personaCreated,
      persona_topics_seeded: personaTopicsSeeded,
      persona_topic_count: personaTopicCount,
      persona_query_count: personaQueryCount,
      persona_query_note: personaQueryNote || null,
      persona_voice_examples: personaExemplarNote,
      kicker_installed: kickerInstall ? kickerInstall.ok : null,
      kicker_detail: kickerInstall ? kickerInstall.detail : null,
      onboarding: onboardingSnapshot(),
      next_step,
    });
  }
);

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
      "2) Connect X/Twitter (action:'connect_x'), ONLY once the user has told you they want X connected " +
      "(the top-level setup flow asks which platform(s) — X, Reddit, or both — before this tool is " +
      "called for either): the autoposter posts through its OWN managed Chrome, " +
      "which needs your logged-in x.com session. This imports x.com/twitter.com cookies from your " +
      "everyday browser (Chrome/Arc/Brave/Edge, auto-detected) into that browser — nothing else is " +
      "touched. Once the user has said they want X, briefly warn that macOS Safe " +
      "Storage prompts may appear, then call action:'connect_x', confirm:true immediately — no further " +
      "round-trip needed for THIS decision, it was already made. Use " +
      "action:'detect_x_sources' first and choose its recommendation instead of asking the user which " +
      "browser profile to import from.\n" +
      "Call with status:true (or no name) to list every configured project, its remaining fields, AND " +
      "whether X is connected. Use config, conversation context, profile_scan, and website research " +
      "before asking for fields. Ask only if no product can be identified or an interactive login is " +
      "unavoidable. The get_stats tool refuses to run until a project is " +
      "fully set up.",
    inputSchema: {
      status: z.boolean().optional(),
      action: z
        .enum([
          "get",
          "connect_x",
          "detect_x_sources",
          "profile_scan",
          "connect_reddit",
          "detect_reddit_sources",
        ])
        .optional()
        .describe(
          "get = read the CURRENT SAVED VALUES of every project's editable fields (website, " +
            "description, icp, voice, differentiator, search_topics, get_started_link, " +
            "content_guardrails, content_angle) plus readiness — the read companion to editing. " +
            "Use it before tweaking part of a nested value; the panel's Project settings section " +
            "is built on it. " +
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
            "grounding_instructions}. " +
            "connect_reddit = call ONLY once the user has said they want Reddit connected (setup " +
            "completes with X-only, Reddit-only, or both — whichever they picked when asked up front; " +
            "if X connection fails after they chose it, offer Reddit as a fallback): import/validate " +
            "the user's reddit.com session in the " +
            "autoposter's managed reddit browser, same confirm-first shape as connect_x. " +
            "detect_reddit_sources = list the browsers/profiles the Reddit session can be imported " +
            "from (read-only, no keychain prompt)."
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
      reddit_source: z
        .string()
        .optional()
        .describe(
          "Optional browser profile to import the Reddit session from, e.g. 'arc:Default', " +
            "'chrome:Profile 1'. Default: auto-detect the browser that holds a reddit.com session."
        ),
      reddit_manual_login: z
        .boolean()
        .optional()
        .describe(
          "Set true ONLY when the user explicitly wants to sign into Reddit by hand. Opens a focused " +
            "Reddit login window in the autoposter's reddit browser and waits. Same opt-in discipline " +
            "as x_manual_login."
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
        .union([z.array(z.string()), z.string()])
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
            "project, REPLACING that key's whole value (read the current value via action:'get' first if " +
            "you only want to tweak part of a nested object, then pass the full new value). A value of " +
            "null DELETES the key. 'name' is ignored here (can't rename through this path). This is how " +
            "you edit advanced config without any raw whole-file overwrite."
        ),
    },
  },
  async (args) => {
    // ---- Read current saved values (the panel's Project settings source) ---
    // Whitelisted field values + readiness for every managed project and the
    // persona. Read-only; the write path stays the validated merge below.
    if (args.action === "get") {
      return jsonContent({
        action: "get",
        projects: listProjectSettings(),
        config_path: configPath(),
        note:
          "Current saved values of each project's editable fields. To change one, call project_config " +
          "with {name, <field>: <new value>} — it merges onto what's saved and re-seeds topics. " +
          "extra_keys lists advanced keys editable only via the `fields` escape hatch.",
      });
    }

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
        // Only mark x_verified once doctor confirms all X checks pass (session valid,
        // cookies persisted, CDP responding, keychain accessible). This prevents the
        // autopilot from starting if X connection didn't actually persist.
        if (doctorReport?.ok) {
          completeOnboardingMilestone("x_verified", {
            x_session: doctorReport.checks?.find(c => c.id === "x_session")?.status,
            x_cookies: doctorReport.checks?.find(c => c.id === "x_cookie_sqlite")?.status,
          });
        } else {
          recordOnboardingAttempt("x_verified", { doctor_ok: false });
        }
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
            "before saving. Then set up the autopilot (queue_setup + create_scheduled_task) once the project is fully set up; it then drafts on its own."
          : r.state === "needs_login"
            ? "The user must finish signing in to x.com in the Chrome window that just opened. Tell " +
              "them that single required action, then call project_config action:'connect_x', confirm:true again."
            : "X is not connected yet. " + summarizeXAuth(r),
      });
    }

    // ---- List Reddit import sources (read-only, no keychain prompt) --------
    if (args.action === "detect_reddit_sources") {
      const r = await redditDetectSources();
      return jsonContent({
        action: "detect_reddit_sources",
        ok: r.ok,
        sources: r.sources,
        recommended: r.recommended,
        error: r.error,
      });
    }

    // ---- Connect Reddit: OPTIONAL platform, mirrors connect_x --------------
    // Preview-or-run, same confirm-first shape as connect_x. Reddit is never
    // required for onboarding completion; offer it only after X setup is done
    // and the user wants it.
    if (args.action === "connect_reddit") {
      if (args.confirm !== true) {
        // Cheap probe so the explanation reflects current state (no Chrome launch).
        const cur = await redditStatus();
        if (cur.connected) {
          return jsonContent({
            action: "connect_reddit",
            already_connected: true,
            state: cur.state,
            username: cur.username,
            note: "Reddit is already connected in the autoposter's reddit browser. Nothing to import.",
          });
        }
        return jsonContent({
          action: "connect_reddit",
          requires_confirmation: true,
          current_state: cur.state,
          what_will_happen:
            "To engage on Reddit for you, the autoposter uses its OWN managed Google Chrome (separate " +
            "from your everyday browser and from the X one). It needs your logged-in reddit.com " +
            "session. If you confirm, it will: (1) start that managed reddit browser if it isn't " +
            "running, (2) copy ONLY your reddit.com cookies (reddit_session, token_v2) from your " +
            "everyday browser (Chrome/Arc/Brave/Edge, auto-detected) into it, and (3) verify you're " +
            "logged in via Reddit's own me.json. No other site's cookies are read, and your passwords " +
            "are never seen. If it can't import a valid session, a Chrome window will open for you to " +
            "sign in once.",
          keychain_prompt:
            "Reading the saved session requires macOS to unlock the browser's encrypted cookie store, so " +
            "one or more keychain prompts will appear (“... wants to use your confidential information " +
            "stored in '... Safe Storage' in your keychain”). This is expected. The user enters their Mac " +
            "login password and clicks Allow (or Always Allow to avoid repeats). If they use more than one " +
            "browser, the prompt can appear a few times, once per browser.",
          say_to_user:
            "Heads up: your Mac will pop up a keychain prompt asking to use your browser's Safe Storage. " +
            "That's just us reading your saved Reddit login, nothing else. Type your Mac login password and " +
            "click Allow (or Always Allow). If you use more than one browser you may see it a couple of " +
            "times, once per browser.",
          how_to_proceed:
            "If the user explicitly requested connecting Reddit, relay the say_to_user line as a brief " +
            "heads-up and immediately call project_config again with action:'connect_reddit', confirm:true; " +
            "do not wait for another yes/no reply. Optionally pass a reddit_source from " +
            "detect_reddit_sources. If the user only asked what connection would do, stop after this " +
            "preview. Reddit is OPTIONAL: never treat it as required for setup completion.",
        });
      }
      recordOnboardingAttempt("reddit_connected", {
        state: args.reddit_source ? "source_selected" : "auto_detect",
      });
      const r = await redditConnect(args.reddit_source, args.reddit_manual_login);
      let verified = false;
      let configNote: string | undefined;
      let redditKicker: { ok: boolean; detail: string } | undefined;
      if (r.connected) {
        completeOnboardingMilestone("reddit_connected", { state: r.state });
        // Persist the discovered username through the server's config-write
        // path (accounts.reddit.username, the field account_resolver reads),
        // never overwriting a deliberately-set value.
        if (r.username) {
          const w = recordRedditAccount(r.username);
          configNote = w.detail;
        }
        // Doctor-style persistence re-check before reddit_verified: a fresh
        // read-only status probe confirms the session actually persisted
        // (live me.json or the on-disk profile cookie row) rather than
        // trusting the connect call's own success.
        const recheck = await redditStatus();
        if (recheck.connected) {
          completeOnboardingMilestone("reddit_verified", { state: recheck.state });
          verified = true;
        } else {
          recordOnboardingAttempt("reddit_verified", {
            state: recheck.state || "unverified",
          });
        }
        // Reddit is live: install the discovery kicker (idempotent; gated on
        // runtime + draftable-lane readiness inside, and it never touches a
        // hand-built plist pointing outside the managed package). Await it and
        // surface the result on the response so a skip (e.g. no ready project AND
        // no active persona) is VISIBLE instead of failing silently to stderr.
        try {
          redditKicker = await ensureRedditKickerInstalled();
          console.error(
            `[reddit-kicker] post-connect install: ${redditKicker.ok ? "ok" : "skip"} (${redditKicker.detail})`
          );
        } catch (e: any) {
          redditKicker = { ok: false, detail: e?.message || String(e) };
          console.error("[reddit-kicker] post-connect install failed:", e?.message || e);
        }
      } else {
        blockOnboardingMilestone(
          "reddit_connected",
          `reddit_${r.state || "not_connected"}`,
          r.error || r.note || summarizeRedditAuth(r),
          { state: r.state || "not_connected" }
        );
      }
      return jsonContent({
        action: "connect_reddit",
        connected: r.connected,
        state: r.state,
        username: r.username,
        account_age_days: r.account_age_days,
        comment_karma: r.comment_karma,
        // Ready-to-relay expectation setter for fresh/low-karma accounts
        // (AutoMod gates those in most subreddits). Advisory only; never a
        // reason to block or undo the connect.
        warning: r.warning,
        verified,
        config: configNote,
        summary: summarizeRedditAuth(r),
        note: r.note,
        attempts: r.attempts,
        // The launchd discovery kicker's install result (or skip reason), so the
        // agent can SEE whether reddit drafting is actually scheduled rather than
        // assuming "connected" means "producing drafts".
        discovery_job: redditKicker,
        onboarding: onboardingSnapshot(),
        next_step: r.connected
          ? (redditKicker?.ok
              ? "Reddit is connected and the discovery job is scheduled (runs every ~15 min); " +
                "drafts flow into the same review cards as X and nothing posts without approval."
              : "Reddit is connected, but the discovery job is NOT scheduled yet (" +
                (redditKicker?.detail || "unknown") + "). It installs automatically once a product " +
                "project is ready OR personal_brand mode is on with a ready persona; until then no " +
                "reddit drafts will appear. Drafts, once flowing, land in the same review cards as X " +
                "and nothing posts without approval.") +
            (r.warning ? " Relay the `warning` field to the user so expectations are set." : "")
          : r.state === "needs_login"
            ? "The user must finish signing in to reddit.com in the Chrome window that opened. Tell " +
              "them that single required action, then call project_config action:'connect_reddit', " +
              "confirm:true again."
            : "Reddit is not connected yet. " + summarizeRedditAuth(r),
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
        top_posts: scan.top_posts,
        top_replies: scan.top_replies,
        grounding_instructions: scan.grounding_instructions,
        website_research_instructions: WEBSITE_RESEARCH_INSTRUCTIONS,
        onboarding: onboardingSnapshot(),
        next_step:
          "FOUR steps, in order. FIRST (VOICE, from this scan): read the bio, posts, and comments " +
          "as GROUND TRUTH and, per grounding_instructions, extract their profession/identity, " +
          "voice & vibe (tone, phrasing, casing, tics), verbatim golden-rule example replies (the " +
          "scan pre-ranks these by real engagement in top_replies/top_posts, with stats, parent " +
          "tweets, and thread continuations; persist them into the project's voice.examples so " +
          "every drafter mirrors them), " +
          "a phrase bank + things they avoid, and their icp. The scan is BACKWARD-LOOKING (only what " +
          "they already posted) so it is the source for VOICE, not the primary source for topics. " +
          "SECOND (the DICTATION interview — this is where TOPICS + grounding corpus come from, do NOT " +
          "skip it and do NOT infer topics from the scan alone): invite the user to answer the " +
          "following in ONE spoken dictation (the Claude input box already supports dictation, so they " +
          "just talk once and you split the answers into fields). KEEP THE FRAMING CHILL: this is a " +
          "casual brain-dump, not a form. No pressure; they can answer as much or as little as they " +
          "like, skip anything, and come back to the rest whenever they feel like it. Preface the list " +
          "with one short low-key line saying exactly that, then ask verbatim, as a single numbered " +
          "list:\n" +
          "  1. Who are you, and what do you want to be known for? (-> description)\n" +
          "  2. What subjects could you talk about for an hour, work and non-work? (-> search_topics: " +
          "this is the LOAD-BEARING answer, it is the ONLY thing that decides what gets scanned on X, " +
          "so it must capture what they WANT to be in conversations about)\n" +
          "  3. Your most contrarian takes — what does everyone in your field get wrong, and what did " +
          "you used to believe that you have reversed on? (-> content_angle + corpus)\n" +
          "  4. What can you explain in 5 minutes that took you years, and what mistake do you watch " +
          "beginners make over and over? (-> content_angle + corpus)\n" +
          "  5. Best or worst thing that happened to you recently, and a failure you learned the most " +
          "from? (-> corpus, keeps drafts current)\n" +
          "  6. Who do you love or hate reading online, and any lines or phrases you say a lot? " +
          "(-> voice calibration)\n" +
          "  7. Anything off-limits (topics, companies, people), and how spicy can we get — safe, " +
          "opinionated, or provocative? (-> content_guardrails + voice.never)\n" +
          "Then SYNTHESIZE the fields from their dictation: search_topics comes PRIMARILY from answer 2 " +
          "(fold in recurring scan themes only as reinforcement); description/content_angle/voice from " +
          "the rest. Keep their RAW transcript VERBATIM as content_corpus (do NOT paraphrase; their " +
          "actual numbers, opinions, and phrasing are what make drafts sound like them). If they " +
          "answer only some questions, take what they gave, continue without nagging, and mention once " +
          "that they can answer the rest any time later to make drafts sound more like them. If the " +
          "user declines or gives nothing usable, fall back to scan-derived topics. " +
          "THIRD (engagement lanes — ASK THE USER, do not infer): the PERSONAL BRAND lane (organic, " +
          "link-free engagement in their own voice) is ON by default, so ask the ONE question — do they " +
          "ALSO want to PROMOTE a PRODUCT (the marketing lane, link replies)? Both lanes can run (the " +
          "cycle splits per the configurable lane split, default 50/50). Call the `engagement_mode` tool " +
          "action:'set' with personal_brand:true, " +
          "promotion:true|false AND the voice/description/search_topics you synthesized PLUS the raw " +
          "dictation transcript as content_corpus (this provisions the persona and seeds topics). Only " +
          "NOW are topics seeded — postponed until the dictation is in. " +
          "FOURTH (product, ONLY if they wanted promotion): follow " +
          "website_research_instructions — discover the product URL from config, context, profile " +
          "links/posts, or public research and read 5+ of its pages to fill description, " +
          "differentiator, icp, get_started_link, and content_guardrails, written in the voice you " +
          "just captured. Save the best conservative supported fields without a confirmation " +
          "round-trip. Ask only if no product can be identified or a required field is unknowable. If " +
          "they only want personal brand, SKIP the product step.",
      });
    }

    // Status / discovery mode: no project name supplied, or explicitly asked.
    if (args.status === true || !args.name) {
      // ONE compute path: buildSnapshot -> scripts/snapshot.py, the same source
      // the menu bar and browser dashboard read. This branch used to recompute
      // projects/X/version inline (listManagedProjectStatus + xStatus), which
      // excluded the persona project and carried no setup_complete — so the
      // panel's refresh() disagreed with the dashboard tool and the menu bar on
      // persona-only setups. Do not reintroduce a parallel compute here.
      const snap = await buildSnapshot();
      const projects: Array<{ name: string; ready: boolean; missing_required: string[] }> =
        Array.isArray(snap.projects) ? snap.projects : [];
      const rtReady = !!snap.runtime_ready;
      const xConnected = !!snap.x_connected;
      const redditConnected = !!snap.reddit_connected;
      // Any-of platform completion: X or Reddit satisfies the platform leg.
      const anyPlatform = xConnected || redditConnected;
      const configured = (snap.projects_ready || 0) > 0;
      return jsonContent({
        configured,
        projects,
        runtime_ready: rtReady,
        x_connected: xConnected,
        x_state: snap.x_state,
        x_handle: snap.x_handle ?? null,
        reddit_connected: redditConnected,
        reddit_state: snap.reddit_state,
        reddit_username: snap.reddit_username ?? null,
        setup_complete: !!snap.setup_complete,
        mcp_version: snap.version,
        latest_version: snap.latest_version,
        update_available: !!snap.update_available,
        mode: snap.mode,
        flags: snap.flags,
        personal_brand_share: snap.personal_brand_share,
        update_hint: snap.update_available
          ? `A newer version (${snap.latest_version}) is available — you're on ${snap.version}. ` +
            `Tell the user and offer to run the \`runtime\` tool with action:'update' ` +
            `(or \`npx social-autoposter@latest update\`).`
          : undefined,
        required_fields: REQUIRED_FIELDS,
        recommended_fields: RECOMMENDED_FIELDS,
        config_path: configPath(),
        ready_for_verification: !!snap.setup_complete,
        onboarding: snap.onboarding,
        next_step:
          !rtReady
            ? "Runtime is not ready yet. It provisions automatically on boot — poll runtime action:'status' until ready (only call runtime action:'install' to retry if status shows the boot provision failed or stalled), then continue setup automatically."
            : projects.length === 0
            ? "No projects yet. Discover the product from conversation context and the connected X profile; research its website, infer a conservative complete project, and call project_config. Ask only if no product can be identified." +
              (anyPlatform ? "" : " No platform is connected yet either. X is the default: detect_x_sources, warn about keychain prompts, then run connect_x with confirm:true without a separate permission turn. If the user prefers Reddit (or X can't be connected), connect_reddit works standalone.")
            : projects.every((p) => p.ready)
              ? (anyPlatform
                  ? "All configured projects are ready and a platform is connected" +
                    (xConnected ? "" : " (Reddit; X was skipped, which is fine)") +
                    ". SCHEDULE THE AUTOPILOT: (1) call queue_setup and create each returned task with create_scheduled_task (prompt and notifyOnCompletion verbatim — notifyOnCompletion must be false; 'already exists' is fine, but then call update_scheduled_task with notifyOnCompletion:false to silence per-run notifications); (2) the autopilot then runs on its own (launchd kicker + queue worker). Call the `dashboard` tool to confirm the schedule is firing (schedule_state 'ok') — that is the terminal step; do NOT wait for or verify a draft card. Do NOT pause to ask the user to review drafts."
                  : "All configured projects are ready, but NO platform is connected yet; posting needs a logged-in session. X is the default: detect sources and run project_config action:'connect_x', confirm:true; do not ask whether to proceed. If the user prefers Reddit or X can't be connected, run action:'connect_reddit' instead; ONE connected platform completes setup.")
              : "Some projects are missing required fields (see each project's missing_required). Derive them from config, context, profile_scan, and website research, then call project_config again. Ask only if a required field is genuinely unknowable." +
                (anyPlatform ? "" : " No platform is connected yet either; X is the default (connect_x with confirm:true), or connect_reddit if the user prefers Reddit."),
      });
    }

    // Apply mode (incremental): merge whatever fields were supplied onto the
    // named project, then report whether it's now ready or still missing fields.
    try {
      // Editing the persona project must not touch the PRODUCT onboarding
      // milestone (a persona is never "project_ready" in the product sense; it
      // validates against PERSONA_REQUIRED_FIELDS inside applySetup).
      const editingPersona = findPersonaProject()?.name === args.name;
      if (!editingPersona) {
        recordOnboardingAttempt("project_ready", {
          missing_count: 0,
        });
      }
      const result = applySetup(args as ProjectInput);
      // Persist the profile scan's engagement-ranked exemplars into this
      // project's voice.examples (+ the persona corpus section when the target
      // is the persona). Reads the last_profile_scan.json sidecar the scanner
      // wrote; verbatim quotes of the user's own public replies, so no
      // synthesis happens here. Best-effort: no scan yet or hand-written
      // examples present (exit 3) are both fine, and never block the save.
      const exemplarNote = await applyScannedVoiceExamples(result.project);
      if (result.persona) {
        // no-op on the onboarding ledger; readiness is reported below as usual.
      } else if (result.ready) {
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
            ? ` Seeded ${m[1]} search topic(s) into the DB (new: ${m[2]}, updated: ${m[3]}), so the draft cycle has a topic universe to work with.`
            : " Seeded search topics into the DB so the draft cycle has a topic universe to work with.";
        } else {
          const tail = (seed.stderr || seed.stdout).trim().split("\n").slice(-1)[0] || "unknown error";
          blockOnboardingMilestone(
            "topics_seeded",
            "topic_seed_failed",
            tail,
            { exit_code: seed.code }
          );
          seedNote = ` (Heads up: couldn't seed search topics into the DB yet — ${tail}. The autopilot will report clearly if topics are missing.)`;
        }

        // Cold-start QUERY supply (shared with the persona/engagement_mode path
        // via seedSearchQueriesForProject): fan the agent-supplied search_queries
        // into project_search_queries so the Phase 1 bank fans out on day one
        // instead of running ONE crude topic-as-query. Only after the topic seed
        // succeeds, matching the persona path's guard.
        if (seed.code === 0) {
          const qr = await seedSearchQueriesForProject(
            result.project,
            args.search_queries as string[] | string | undefined
          );
          seedNote += qr.note;
          searchQueries = qr.queries as typeof searchQueries;
        }
      }
      // Install/refresh the launchd kicker NOW, the moment a product project is
      // ready — identical to the persona path (engagement_mode). Before this, a
      // promotion-only setup never installed the kicker at setup time (the persona
      // path explicitly skips promotion-only, and project_config didn't pick it
      // up), so drafting didn't start until a later Claude/queue-worker boot ran
      // the boot-time install. ensureQueueKickerInstalled is idempotent + product/
      // persona-aware, so calling it from both setup paths is safe. Best-effort:
      // a kicker hiccup never fails setup. (2026-06-30)
      let kickerInstall: { ok: boolean; detail: string } | null = null;
      if (result.ready && isPaused()) {
        kickerInstall = { ok: false, detail: "skip (paused)" };
      } else if (result.ready) {
        try {
          kickerInstall = await ensureQueueKickerInstalled();
          console.error(
            `[project_config] launchd kicker: ${kickerInstall.ok ? "ok" : "skip"} (${kickerInstall.detail})`
          );
        } catch (e: any) {
          kickerInstall = { ok: false, detail: e?.message || String(e) };
          console.error("[project_config] kicker install failed:", e?.message || e);
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
        persona: result.persona,
        ready: result.ready,
        missing_required: result.missing_required,
        topics_seeded: topicsSeeded,
        topic_count: topicCount,
        search_queries: searchQueries,
        kicker_installed: kickerInstall ? kickerInstall.ok : null,
        kicker_detail: kickerInstall ? kickerInstall.detail : null,
        fields_set: result.fields_set,
        fields_removed: result.fields_removed,
        voice_examples: exemplarNote,
        config_path: configPath(),
        onboarding: onboardingSnapshot(),
        note: (result.persona
          ? `Persona '${result.project}' updated.${seedNote}` +
            (result.ready ? "" : ` Still needs: ${result.missing_required.join(", ")}.`)
          : result.ready
          ? `Project '${result.project}' is fully configured.${seedNote} Next: if X is not connected, ` +
            `detect sources, warn about keychain prompts, and call project_config with ` +
            `action:'connect_x', confirm:true immediately. Once X is connected, schedule the autopilot ` +
            `(queue_setup + create_scheduled_task per task, passing each spec's fields verbatim including ` +
            `notifyOnCompletion:false); the autopilot then drafts on its own. Call the ` +
            `dashboard to confirm the schedule is firing (schedule_state 'ok') — that is the final step, ` +
            `no need to wait for or verify a draft card.`
          : `Saved what you provided for '${result.project}'. Still need: ${result.missing_required.join(", ")}. ` +
            `First derive those fields from existing context, profile_scan, and website research, then ` +
            `call project_config again with name='${result.project}'. Ask only if a required field is genuinely unknowable.`) +
          advancedNote +
          (exemplarNote ? ` ${exemplarNote}` : ""),
      });
    } catch (e) {
      return textContent(`Setup failed: ${(e as Error).message}`);
    }
  }
);

// ---- approve_drafts: post the user's chosen drafts from a batch ---------------
// Second half of the manual loop. The user reviewed the menu-bar cards a draft
// cycle produced and said which numbers to post / edit; this posts exactly those.
// Editing a draft implies posting it. Indices are 1-based, matching the table.
tool(
  "approve_drafts",
  {
    title: "Post chosen drafts",
    description:
      "Post the drafts the user approved from a draft cycle. Pass the batch_id from the " +
      "approval cards and the user's decision by NUMBER (1-based, matching the table): `post` is " +
      "the list of draft numbers to post as drafted; `edits` rewrites a draft's text before " +
      "posting it (editing implies posting); `post_all` posts every draft. Only the chosen " +
      "drafts post; anything not listed is left unposted. Call this ONLY after the user has " +
      "told you which drafts they want. After posting, call the `dashboard` tool so the user " +
      "sees the updated state.",
    inputSchema: {
      batch_id: z.string().describe("The batch_id of the draft batch (from the approval cards)."),
      post: z
        .array(z.number().int().positive())
        .optional()
        .describe("1-based draft numbers to post as drafted, e.g. [1, 3, 5]."),
      edits: z
        .array(
          z.object({
            n: z.number().int().positive(),
            text: z.string(),
            variant: z
              .enum(["a", "b"])
              .optional()
              .describe(
                "Two-draft cards only: which of candidate n's `drafts` entries this text came " +
                  "from, so the candidate's style/assigned_style/assigned_mode are switched to " +
                  "match (otherwise engagement_style stats would be attributed to the wrong style)."
              ),
          })
        )
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
      clear_link: z
        .array(z.number().int().positive())
        .optional()
        .describe(
          "1-based draft numbers whose link the user removed while editing. Their " +
            "link_url is cleared so the poster does not silently re-append it."
        ),
    },
  },
  async ({ batch_id, post, edits, post_all, reject, clear_link }) => {
    const plan = readPlan(batch_id);
    if (!plan || !(plan.candidates && plan.candidates.length)) {
      return textContent(
        `No drafts found for batch ${batch_id}. The autopilot produces a fresh batch on its next scheduled cycle.`
      );
    }
    const candidates = plan.candidates;
    const total = candidates.length;
    const warnings: string[] = [];
    const inRange = (n: number) => n >= 1 && n <= total;

    // Review-queue store: snapshot every row now so the write below can be a
    // field-level DIFF applied under the store lock (store_patch.py) instead
    // of an unlocked whole-file replace. This function holds its in-memory
    // plan across user think-time; a whole-file write here erased any menubar
    // decision or merge that landed in between (the 2026-07-17 truth-loss
    // family). Non-store batches keep the plain write: single writer.
    const isStoreBatch = batch_id === REVIEW_QUEUE_ID;
    const rowsBefore: string[] = isStoreBatch
      ? candidates.map((c) => JSON.stringify(c))
      : [];

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
      if (candidateState(c) === "posted") {
        warnings.push(`#${n} already posted; not rejecting`);
        return;
      }
      c.terminal = true;
      // Preserve a more specific reason the menu bar already stamped locally
      // (store_stamp_decision writes "human_rejected" for a card reject,
      // discard_all_pending writes "human_discarded_all" for the bulk-discard
      // button) BEFORE this loopback call ever fires, so by the time it lands
      // here the local reason is already the more informative one. Only
      // default to "rejected" when nothing more specific got there first
      // (e.g. a reject driven straight from chat, with no menu-bar card
      // involved at all).
      if (!c.terminal_reason) {
        c.terminal_reason = "rejected";
      }
      c.approved = false;
      rejected.push(n);
      // Reddit cards: also retire the reddit_candidates row (permanent
      // mark_attempt) so Phase 0 salvage never re-pulls and re-drafts a
      // thread a human just rejected. Twitter gets this for free via the
      // review-events row flip; reddit's id-keyed flows are deliberately
      // firewalled (rd- prefixed ids), so this direct by-thread-url PATCH is
      // the reddit equivalent. Fire-and-forget: a failure leaves the row
      // pending, which at worst re-drafts one card next cycle.
      const ccr = c as unknown as Record<string, unknown>;
      if (ccr.platform === "reddit" && (ccr.thread_url || ccr.candidate_url)) {
        const rejUrl = String(ccr.thread_url || ccr.candidate_url);
        void runPython(
          "-c",
          [
            "import sys; sys.path.insert(0, 'scripts')\n" +
              "from http_api import api_patch\n" +
              "api_patch('/api/v1/reddit-candidates/by-thread-url', " +
              "{'thread_url': sys.argv[1], 'action': 'mark_attempt', " +
              "'reason': 'human_rejected', 'permanent': True}, ok_on_404=True)",
            rejUrl,
          ],
          { timeoutMs: 30_000 }
        ).catch(() => {
          /* best effort */
        });
      }
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
      const c = candidates[e.n - 1];
      c.reply_text = text;
      // Two-draft cards: the human switched to the OTHER draft (with or
      // without further hand-editing its text). Carry that draft's own
      // style/assigned_style/assigned_mode onto the candidate so
      // twitter_post_plan.py's per-candidate drift-coercion posts under (and
      // logs) the style that's ACTUALLY posting, not whichever draft was
      // recommended at plan-write time.
      if (e.variant && Array.isArray(c.drafts)) {
        const chosen = c.drafts.find((d) => d.variant === e.variant);
        if (chosen) {
          c.engagement_style = chosen.style ?? c.engagement_style;
          c.assigned_style = chosen.assigned_style ?? null;
          c.assigned_mode = chosen.assigned_mode ?? c.assigned_mode;
        }
      }
      approve.add(e.n);
      editedCount++;
    });

    // Honor "user deleted the link while editing": clear the link fields so the
    // poster (which runs with forced TWITTER_TAIL_LINK_RATE=1.0 on this path)
    // does NOT silently re-append a link the user intentionally removed. Without
    // this, link_url survives on the candidate row and the poster revives it.
    (clear_link || []).forEach((n) => {
      if (!inRange(n)) {
        warnings.push(`ignored clear_link #${n}: out of range (1-${total})`);
        return;
      }
      const c = candidates[n - 1];
      c.link_url = undefined;
      c.link_keyword = undefined;
      c.link_slug = undefined;
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
    // Exception: an overridable backend-expiry stamp yields to this explicit
    // approval (see expiredStampOverridable) — clear it and post.
    const alreadyDone: number[] = [];
    for (const n of Array.from(approve)) {
      const c = candidates[n - 1];
      if (c && expiredStampOverridable(c)) {
        c.terminal = false;
        delete c.discard_reason;
        continue;
      }
      if (c?.posted === true || c?.terminal === true) {
        approve.delete(n);
        alreadyDone.push(n);
      }
    }
    if (alreadyDone.length) {
      warnings.push(`already posted/decided (skipped): ${alreadyDone.sort((a, b) => a - b).join(", ")}`);
    }

    // STICKY approve: record the approval DURABLY and never clear another card's
    // prior approval. The old `c.approved = approve.has(i+1)` reset every card on
    // each call, so a later approve_drafts for a different card dropped a
    // restart-interrupted approved card back into "pending". postApproved filters
    // posted/terminal, so the approved set only ever drains what's genuinely left.
    approve.forEach((n) => {
      const c = candidates[n - 1];
      if (c) c.approved = true;
    });
    // Persist the decision mutations. Store batch: diff each row against its
    // snapshot and apply only the changed fields under the store lock, so a
    // concurrent menubar decision or merge is never erased. Anything else
    // (per-batch /tmp plans): plain write, single writer.
    let storeWriteDone = false;
    if (isStoreBatch) {
      const patches: object[] = [];
      candidates.forEach((c, i) => {
        const beforeRaw = rowsBefore[i];
        const afterRaw = JSON.stringify(c);
        if (beforeRaw === afterRaw) return;
        const before = JSON.parse(beforeRaw ?? "{}") as Record<string, unknown>;
        const after = JSON.parse(afterRaw) as Record<string, unknown>;
        const set: Record<string, unknown> = {};
        const unset: string[] = [];
        for (const k of new Set([...Object.keys(before), ...Object.keys(after)])) {
          if (!(k in after) || after[k] === undefined) {
            if (k in before) unset.push(k);
          } else if (JSON.stringify(before[k]) !== JSON.stringify(after[k])) {
            set[k] = after[k];
          }
        }
        if (Object.keys(set).length || unset.length)
          patches.push({ candidate_id: c.candidate_id ?? null, n: i + 1, set, unset });
      });
      storeWriteDone = await patchReviewStore(patches);
      if (!storeWriteDone)
        console.error("[approve_drafts] store_patch.py failed; falling back to unlocked plan write");
    }
    if (!storeWriteDone) writePlan(batch_id, plan);

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

    // postApproved now arms the cross-instance posting flag before the Reddit
    // drain (see its own comment), not just before the Twitter phase. Every
    // op inside that drain is already individually best-effort try/caught, so
    // this is belt-and-suspenders: if something still throws past all of
    // that, clear the flag before it escapes rather than leave this MCP
    // instance's posting permanently gated behind a flag nothing will ever
    // reset.
    let result;
    try {
      result = await postApproved(batch_id, plan);
    } catch (e) {
      postingActive = false;
      stopPostingFlagHeartbeat();
      throw e;
    }
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
      "With no `project` it reports EVERY configured lane, including the personal-brand " +
      "persona (which usually carries most of the volume) — prefer that default. " +
      "After returning the numbers, call the `dashboard` tool so the user sees them rendered.",
    inputSchema: {
      days: z.number().int().min(1).max(90).default(7),
      project: z
        .string()
        .optional()
        .describe(
          "Scope to one configured project (the persona lane's name works too). " +
            "Omit to report all lanes — products AND the personal-brand persona."
        ),
    },
  },
  async ({ days, project }) => {
    // Explicit project: validate it (projectStatus is persona-aware, so the
    // persona lane resolves). No project: report EVERY lane rather than
    // resolving to a single product — the old single-project resolution made
    // the persona lane (often 90% of activity) invisible in stats.
    let proj: string | undefined;
    if (project) {
      const r = resolveProject(project);
      if (!r.ok) return textContent(r.message!);
      proj = r.project!;
    } else if (!hasReadyProject() && !personaReady()) {
      const r = resolveProject();
      return textContent(r.message!);
    }
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
    let snapshot = runtimeSnapshot();
    if (snapshot.runtime_ready) {
      completeOnboardingMilestone("runtime_ready");
    } else if (snapshot.progress?.done && !snapshot.progress.ok) {
      // A prior provision failed. Kick a bounded auto-retry so status polling
      // self-heals a transient failure instead of parking until the next boot;
      // the provisioner cleans its own partial artifacts, so a retry is safe.
      // Only surface the failure to the onboarding ledger once retries are spent.
      if (retryProvisionIfStalled()) {
        snapshot = runtimeSnapshot(); // reflect the restarted, in-flight run
      } else {
        blockOnboardingMilestone(
          "runtime_ready",
          "runtime_install_failed",
          snapshot.progress.error || "Runtime installation failed",
          { outcome: "failed" }
        );
      }
    }
    return jsonContent({
      ...snapshot,
      menubar_running: await menubarRunning(),
      paused: isPaused(),
      onboarding: onboardingSnapshot(),
    });
  }
);

// ---- restart_menubar: relaunch the always-on tray app ----------------------
// The menu bar app is a KeepAlive LaunchAgent that a full Quit boots out. This
// re-runs the same ensureMenubar() the boot path uses (install if missing, load
// the LaunchAgent), so the panel can offer a one-click "restart menu bar" when
// the snapshot reports menubar_running:false. Returns the fresh running state so
// the panel can drop the banner without a round-trip.
tool(
  "restart_menubar",
  {
    title: "Restart the S4L menu bar app",
    description:
      "Relaunch the always-on S4L menu bar (tray) app after it was quit. Re-loads its " +
      "LaunchAgent (installing the menu bar first if needed). Use when the dashboard reports the menu " +
      "bar is not running, or the user asks to start S4L, restart S4L, or bring the S4L tray icon " +
      "back. Does NOT touch the draft " +
      "schedule, X connection, or any posting — it only restarts the tray UI.",
    inputSchema: {},
  },
  async () => {
    // Explicit user intent to start: lift the stop sentinel a tray Quit wrote,
    // otherwise ensureMenubar() would no-op forever.
    clearMenubarStop();
    const res = await ensureMenubar();
    const running = await menubarRunning();
    return jsonContent({
      ok: res.ok,
      skipped: res.skipped ?? false,
      detail: res.detail,
      menubar_running: running,
    });
  }
);

// ---- run_drafting: run the drafting pipeline immediately -------------------
// The drafting pipeline (the twitter cycle) is what the user triggers: it SCANS
// for threads first, then hands off to the drafter, so from the user's view this
// is "run drafting now". It runs on the launchd kicker's fixed StartInterval;
// this nudges launchd to fire it RIGHT NOW via `launchctl kickstart` — the EXACT
// same mechanism the first-run onboarding kick uses (ensureQueueKickerInstalled):
// the job runs THROUGH launchd with its full baked env + the run-*-singleton.sh
// lock, so it is NOT a bare `bash run-twitter-cycle.sh` (which would leave an
// empty-plan artifact). launchd keeps a single instance, so if a run is already
// underway this is a harmless no-op; while paused the kicker is unloaded, so we
// report that rather than kick nothing.
//
// This is deliberately NOT the removed run_draft_cycle / draft_cycle tool: it
// spawns no warm in-chat Claude draft session, it only asks launchd to run the
// already-scheduled cycle one interval early. Do NOT re-introduce a draft_cycle.
tool(
  "run_drafting",
  {
    title: "Run the drafting pipeline now",
    description:
      "Run the drafting pipeline ONE time immediately instead of waiting for the next scheduled run. " +
      "The pipeline scans for threads and then drafts replies, so this is the 'run now' control behind " +
      "the menu bar + dashboard button. It nudges the existing launchd job to run one interval early; " +
      "it does NOT change the schedule. If a run is already underway it's a no-op, and if S4L is paused " +
      "it reports that (nothing runs while paused). Use when the user asks to run drafting, draft now, " +
      "run the pipeline, run now, or check for new threads immediately.",
    inputSchema: {},
  },
  async () => {
    if (isPaused()) {
      return jsonContent({
        ok: false,
        paused: true,
        detail: "S4L is paused — resume it first, then drafting can run.",
      });
    }
    const uid = process.getuid ? process.getuid() : 0;
    const kick = await run(
      "launchctl",
      ["kickstart", `gui/${uid}/${TWITTER_AUTOPILOT_LABEL}`],
      { timeoutMs: 15_000 }
    );
    const ok = kick.code === 0;
    return jsonContent({
      ok,
      detail: ok
        ? "Drafting started — new drafts land in a few minutes."
        : `Could not start drafting (launchctl rc=${kick.code}).`,
    });
  }
);

// ---- drafting_status: live pipeline status + next-run countdown ------------
// A cheap, pure READ the dashboard widget polls (~5s) to keep its drafting status
// pill + countdown live between heavier snapshot refreshes. Returns the SAME
// fields the menu bar renders (scripts/live_status.py — activity_state,
// next_run_secs, …), plus `paused`, so the two surfaces can't drift. No side
// effects: safe to poll and to replay over the loopback /tool/ endpoint.
tool(
  "drafting_status",
  {
    title: "Live drafting-pipeline status + next-run countdown",
    description:
      "Read-only: what the drafting pipeline is doing right now (scanning/drafting/posting/idle) and " +
      "how many seconds until the next scheduled run. Backs the dashboard's live status pill + " +
      "countdown. Pure read; safe to poll frequently.",
    inputSchema: {},
  },
  async () => {
    let live: Record<string, any> = {};
    try {
      const res = await runPython("scripts/live_status.py", [], { timeoutMs: 10_000 });
      live = JSON.parse((res.stdout || "").trim().split("\n").slice(-1)[0] || "{}");
    } catch {
      live = {};
    }
    return jsonContent({ ...live, paused: isPaused() });
  }
);

// ---- posting_volume: per-install posting-volume mode (virality bar) --------
// Server-side throttle (2026-07-13): installations.posting_mode maps to a
// virality-bar percentile on the API (high~0.90, medium~0.97, low~0.995) and
// OVERRIDES the cycle driver's hardcoded percentile, so a change applies on
// the next cycle with no client update. 'get' also returns per-mode estimated
// posts/day replayed from THIS install's trailing-7d candidate pool.
tool(
  "posting_volume",
  {
    title: "Read or set posting volume (Aggressive / Steady / Chill)",
    description:
      "Read or set this install's posting-volume mode, the quality bar that decides how many drafts " +
      "per day the twitter cycle produces. Three modes, shown to users as Aggressive (~100+ posts/day), " +
      "Steady (~30/day, the default every install starts on), and Chill (~5/day, only the very best " +
      "candidates). Internally the modes are high|medium|low and both spellings are accepted. " +
      "action:'get' returns the current mode plus per-mode estimated " +
      "posts/day computed from this install's own recent candidate pool (show those numbers with the " +
      "Aggressive/Steady/Chill names when the user is choosing). Use when the user asks to post more, " +
      "post less, slow down, be more aggressive, chill out, raise the quality bar, or change posting " +
      "volume. Takes effect on the next cycle; in draft-review mode it equally paces how many review " +
      "cards appear.",
    inputSchema: {
      action: z.enum(["get", "set"]).default("get").describe("get = read mode + rates; set = change it"),
      mode: z
        .enum(["aggressive", "steady", "chill", "high", "medium", "low"])
        .optional()
        .describe("Required for action:'set'. aggressive=high, steady=medium, chill=low."),
    },
  },
  async (args: any) => {
    const action = args.action || "get";
    // Friendly display names map onto the stored high|medium|low enum.
    const ALIAS: Record<string, string> = { aggressive: "high", steady: "medium", chill: "low" };
    if (action === "set") {
      if (!args.mode) {
        return jsonContent({ error: "mode is required for action:'set' (aggressive|steady|chill)" });
      }
      const stored = ALIAS[String(args.mode)] || String(args.mode);
      const r = await runPython("scripts/s4l_posting_mode.py", ["set", stored], {
        timeoutMs: 30_000,
      });
      try {
        return jsonContent(JSON.parse((r.stdout || "").trim()));
      } catch {
        return jsonContent({ error: `posting-mode set failed: ${(r.stderr || r.stdout || "").slice(0, 300)}` });
      }
    }
    const r = await runPython("scripts/s4l_posting_mode.py", ["get"], { timeoutMs: 30_000 });
    try {
      return jsonContent(JSON.parse((r.stdout || "").trim()));
    } catch {
      return jsonContent({ error: `posting-mode get failed: ${(r.stderr || r.stdout || "").slice(0, 300)}` });
    }
  }
);

// ---- pause_s4l: temporarily stop drafting/posting, reversibly --------------
// The lighter alternative to Quit: unloads only the launchd jobs that scan,
// draft, and post (plus their support daemons), leaving Claude Desktop, the
// S4L tray, X connection, and the draft schedule registration untouched.
// Fully reversible — action:'resume' reinstalls the exact same daemons via the
// same idempotent ensure*Installed() functions boot uses. NOT "autopilot": S4L
// is draft-first (a human approves every post), so this pauses/resumes the
// draft pipeline itself, not some autonomous posting mode.
tool(
  "pause_s4l",
  {
    title: "Pause or resume S4L",
    description:
      "Pause temporarily stops S4L's own draft pipeline (the launchd kicker that scans/drafts/posts, " +
      "plus its reaper/stall-watch/memory-snapshot support jobs) WITHOUT touching Claude Desktop, the " +
      "S4L tray, your X connection, or the draft schedule registration — nothing is deleted, so it " +
      "survives a Claude Desktop restart and Resume brings it right back. Use when the user asks to " +
      "pause, stop, or temporarily disable S4L, or to resume/unpause it. This is NOT the same as Quit " +
      "(which kills and restarts Claude Desktop and removes the draft schedule) — Pause is the lighter, " +
      "fully reversible option. action:'status' (default) reports whether it's currently paused.",
    inputSchema: {
      action: z.enum(["status", "pause", "resume"]).optional(),
    },
  },
  async ({ action }: { action?: "status" | "pause" | "resume" }) => {
    if (action === "pause") {
      const res = await pauseS4L();
      return jsonContent({ action: "pause", paused: isPaused(), ...res });
    }
    if (action === "resume") {
      const res = await resumeS4L();
      return jsonContent({ action: "resume", paused: isPaused(), ...res });
    }
    return jsonContent({ action: "status", paused: isPaused() });
  }
);

// ---- report_diagnosis: ship a field diagnosis to the developers -------------
// First-class MCP wrapper over scripts/send_diagnostic_report.py (the same
// Sentry lane the menubar "Diagnose & fix" prompt uses). Before this existed
// (2026-07-06) field diagnoses only reached us when the user clicked the
// menubar button AND their Claude ran the script via Bash; troubleshooting done
// directly in chat left no trace. The server instructions tell the agent to
// call this automatically after any failed (or recovered-after-failure)
// setup/heal/troubleshooting flow.
tool(
  "report_diagnosis",
  {
    title: "Send a diagnosis report to the S4L developers",
    description:
      "Ship a short markdown field-diagnosis report (symptom, root cause, actions taken, current " +
      "state) to the S4L developers' telemetry. Call this AUTOMATICALLY after any S4L " +
      "setup/heal/troubleshooting flow that failed, or that succeeded only after a failure — do not " +
      "wait for the user to ask. Contains no secrets; keep the report factual and under a page.",
    inputSchema: {
      report_markdown: z.string().describe("The diagnosis report, markdown, under ~6000 chars"),
      reason: z
        .string()
        .optional()
        .describe("Short reason code, e.g. schedule_missing, runtime_repair, rate_limited"),
    },
  },
  async ({ report_markdown, reason }: { report_markdown: string; reason?: string }) => {
    try {
      const dir = path.join(s4lStateDir(), "diagnostics");
      fs.mkdirSync(dir, { recursive: true });
      const file = path.join(dir, `report-${Date.now()}.md`);
      fs.writeFileSync(file, report_markdown, "utf-8");
      const res = await runPython(
        "scripts/send_diagnostic_report.py",
        [file, reason || "mcp_tool"],
        { timeoutMs: 20_000 }
      );
      return jsonContent({
        ok: res.code === 0,
        detail: res.code === 0 ? "report shipped" : (res.stderr || res.stdout || "").slice(0, 300),
        saved_to: file,
      });
    } catch (e: any) {
      return jsonContent({ ok: false, detail: String(e?.message || e).slice(0, 300) });
    }
  }
);

// ---- client_event: lightweight UI telemetry ping from the dashboard panel --
// The panel iframe is a browser context with no Sentry SDK and no server-side
// telemetry access of its own. Before this, a panel button click (e.g. "Set up
// draft schedule") had NO record anywhere — report_diagnosis needs a full
// markdown report from an agent turn, which doesn't fit a plain click, so the
// panel's rearm button silently had zero telemetry while its menu-bar sibling
// did (see s4l_menubar.py _capture_msg). Not for agent use: the panel calls
// this directly via app.callServerTool, never through chat.
tool(
  "client_event",
  {
    title: "Log a lightweight client UI event",
    description:
      "Internal telemetry hook for the dashboard panel to report a UI event (e.g. a button click). " +
      "Not intended for the agent to call from chat.",
    inputSchema: {
      event: z.string().describe("Short event name, e.g. rearm_clicked"),
      surface: z.string().optional().describe("UI surface the event came from, e.g. panel"),
    },
  },
  async ({ event, surface }: { event: string; surface?: string }) => {
    captureMessage(`S4L client event: ${event}`, {
      level: "info",
      tags: { component: "panel", event, surface: surface || "panel" },
    });
    return jsonContent({ ok: true });
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
      "Returns the scheduled task that runs the hands-free draft autopilot on this machine " +
      "(s4l-worker, the universal queue worker). For EACH returned task, call the host tool " +
      "create_scheduled_task with its taskId, cronExpression, prompt, and notifyOnCompletion " +
      "VERBATIM (do not edit the prompt — it contains exact local paths; notifyOnCompletion MUST be " +
      "false or the per-minute worker spams a notification every run). The task drains the local job queue that the " +
      "real pipeline feeds (all job types); the pipeline itself is kicked by launchd jobs this server " +
      "installs. Use this as the final onboarding step instead of the old per-type worker tasks.",
    inputSchema: {},
  },
  async () => {
    ensureQueueWorkerToolsAllowed();
    // Re-arming the autopilot is an explicit "start S4L" action: lift a prior
    // tray Quit so the review cards have a surface again. Best-effort and
    // async — task specs must return regardless of tray state.
    clearMenubarStop();
    void ensureMenubar();
    // Write each worker's canonical SKILL.md to disk NOW, before the agent calls
    // create_scheduled_task. The host's create_scheduled_task can report a task
    // "already exists" (e.g. a stale Routines registration left after a reset) and
    // then NOT write the prompt file — leaving a registered-but-promptless task
    // that fires and does nothing. Pre-writing the file means the prompt is always
    // present and correct regardless of what the host create does. (2026-06-24)
    for (const spec of QUEUE_WORKERS) {
      try {
        const p = scheduledTaskSkillPath(spec.taskId);
        fs.mkdirSync(path.dirname(p), { recursive: true });
        fs.writeFileSync(p, queueWorkerSkillMd(spec), "utf-8");
      } catch (e: any) {
        console.error(`[queue_setup] could not pre-write ${spec.taskId} SKILL.md: ${e?.message || e}`);
      }
    }
    // Pre-create the dedicated worker folder so the host can set it as each task's
    // working directory at creation; this keeps the per-minute worker sessions out
    // of the user's interactive `claude --resume` picker (see queueWorkerCwd()).
    const workerFolder = queueWorkerCwd();
    try {
      fs.mkdirSync(workerFolder, { recursive: true });
      // Trust it now, before the routines point at it — otherwise the first
      // unattended fire stalls at Claude's per-folder checkTrust on a headless box.
      ensureWorkerFolderTrusted();
    } catch (e: any) {
      console.error(`[queue_setup] could not create worker folder ${workerFolder}: ${e?.message || e}`);
    }
    const tasks = QUEUE_WORKERS.map((spec) => ({
      taskId: spec.taskId,
      cronExpression: "* * * * *",
      folder: workerFolder,
      prompt: queueWorkerBody(spec),
      // The host defaults notifyOnCompletion to TRUE, which fires a Notification
      // Center toast after EVERY run — for a per-minute worker whose normal
      // report is "no jobs", that's up to 60 "couldn't find anything" popups an
      // hour. A customer disabled the autopilot over exactly this (2026-07-06).
      notifyOnCompletion: false,
    }));
    return jsonContent({
      tasks,
      worker_folder: workerFolder,
      // Plain-language "what happens next" the agent MUST relay to the user once
      // the tasks are created, so the setup experience sets expectations about
      // cadence (a draft batch roughly every 5 min), what the jobs make (reply
      // drafts for review), and that nothing posts on its own. (2026-06-30)
      expectations: [
        "What happens next (relay these lines to the user, in their words, right after the tasks are created):",
        "- About every 5 minutes a background draft cycle scans X for posts that match your search topics and drafts replies in your voice.",
        "- Drafts show up as review cards. The first one usually lands within a few minutes when there is matching supply on X; quiet topics mean fewer or no cards until something relevant is posted.",
        "- Nothing is posted automatically. You approve each draft yourself (from the dashboard or the menu bar); posting autopilot stays OFF until you explicitly turn it on. Today it only drafts.",
        "- One helper job (s4l-worker) runs every minute to drain the background work queue. Leave it enabled; it only drafts, it never posts.",
        "- You can edit your voice, topics, or the drafts themselves at any time, and check status on the dashboard.",
      ],
      next_step:
        "For EACH task, call the host tool create_scheduled_task (taskId, cronExpression, prompt, " +
        "notifyOnCompletion verbatim) to register its schedule — the prompt file is already written, so creating it just " +
        "wires up the cron. notifyOnCompletion:false is REQUIRED: the host default is true, and a " +
        "per-minute worker whose normal result is 'no jobs' would otherwise pop a notification every " +
        "run. If create_scheduled_task reports a task already exists, that is FINE — the " +
        "schedule is registered and the prompt is current; do NOT delete/recreate, do NOT investigate — " +
        "but DO silence its run notifications once: call the host tool update_scheduled_task with the " +
        "same taskId and notifyOnCompletion:false (existing installs were created before this default " +
        "was overridden), then move on. It fires every minute and only drafts — it never posts. The pipeline that feeds " +
        "it runs from launchd jobs this server installs. Do NOT create the old " +
        "'social-autoposter-autopilot' task; it is deprecated. If the older per-type tasks " +
        "(saps-phase1-query / saps-phase2b-draft) exist from a previous version, LEAVE them — they are " +
        "compatible with the universal queue and drain the same jobs. " +
        "After the tasks are registered, relay the `expectations` lines to the user so they know the " +
        "cadence (a draft batch about every 5 minutes), that the jobs only draft and never post, and " +
        "that they approve each card themselves. " +
        "Do NOT instruct the user to set each task's working folder by hand in the Routines Edit form — " +
        "that edit does not stick (Claude Desktop caches the schedule registry in memory and clobbers a " +
        `live edit on the next fire). The S4L menu-bar app relocates these tasks to ${workerFolder} ` +
        "automatically: it detects the wrong folder, asks the user once with a modal, then restarts Claude " +
        "once while it is down to apply the change (the only reliable way). So you do NOT need to set any " +
        "folder here — just create the task; the menu bar handles keeping its once-a-minute runs " +
        "out of the user's `claude --resume` history.",
    });
  }
);

// NOTE: the `run_draft_cycle` tool was REMOVED (2026-06-28, per user). The
// autopilot drafts on its own — the launchd kicker (ensureQueueKickerInstalled)
// fires a DRAFT_ONLY cycle every ~5 min and the queue worker drains it — so a
// manual "draft now" tool is redundant. Onboarding now verifies by polling the
// `dashboard` pending-draft count after the scheduled tasks are created (the
// scheduled cycle produces the first card within a few minutes). Do NOT
// re-introduce a run_draft_cycle / draft_cycle tool.

// ---- panel: MCP Apps control surface --------------------------------------
// A self-contained HTML view rendered by hosts that support MCP Apps (Claude
// desktop/web, etc.). It duplicates NO pipeline logic: each button calls one of
// the tools above (project_config / get_stats / dashboard) through the host
// and re-reads status. The tool itself returns the first-paint snapshot so the
// view has data the instant it loads.

// Is either launchd job (cycle / daily updater) currently loaded?
// "Autopilot" is now the pair of Claude Desktop queue-worker scheduled tasks
// (saps-phase1-query + saps-phase2b-draft, created during onboarding via
// create_scheduled_task) that drain the draft queue, NOT the legacy launchd job.
// We can't read the host's enabled/paused flag, but the tasks' presence on disk is the
// single signal the dashboard AND the menu bar key off of, so they stay aligned.
async function autopilotLoaded(): Promise<{ autopilot_on: boolean; auto_update_on: boolean }> {
  let autopilot_on = false;
  try {
    // Autopilot is "on" once a COMPLETE worker set that services the pipeline's
    // queued `claude -p` calls has its SKILL.md on disk: the universal
    // s4l-worker, the transitional saps-worker (staging rc.2/rc.3), or (legacy
    // installs) both per-type workers.
    autopilot_on =
      QUEUE_WORKERS.every((spec) => fs.existsSync(scheduledTaskSkillPath(spec.taskId))) ||
      fs.existsSync(scheduledTaskSkillPath(LEGACY_UNIVERSAL_TASK_ID)) ||
      [PHASE1_TASK_ID, PHASE2B_TASK_ID].every((id) => fs.existsSync(scheduledTaskSkillPath(id)));
  } catch {
    /* leave false */
  }
  let auto_update_on = false;
  try {
    // noTee: this status probe dumps the entire launchd job table (hundreds of
    // lines) and fires on every dashboard/status poll — teeing it flooded Cloud
    // Logging (~98% of an install's log volume). We only need the substring
    // check, so keep the output in-memory and out of the relay. (2026-06-28)
    const res = await run("launchctl", ["list"], { timeoutMs: 10_000, noTee: true });
    auto_update_on = res.stdout.split("\n").some((l) => l.includes(UPDATER_LABEL));
  } catch {
    /* leave false */
  }
  return { autopilot_on, auto_update_on };
}

// ===========================================================================
// Queue-worker scheduled tasks + launchd kicker (2026-06-23)
//
// The single drafting path. The REAL pipeline runs in DRAFT_ONLY mode under
// launchd; its `claude -p` calls go
// through scripts/claude_job.py's file queue (run_claude.sh provider seam); two
// scheduled tasks drain that queue. Each task is single-purpose (one job type),
// fires every minute, claims ONE job, runs the pipeline's own prompt as its
// Claude turn, writes the result back, and stops.
// ===========================================================================
const QUEUE_WORKER_PROMPT_VERSION = 9; // v9 (2026-07-17): poll window widened 240s -> 900s (see QUEUE_WORKER_POLL_SECONDS); version bump forces the prompt refresh that carries the new --wait-seconds onto existing installs. v8: worker polls internally (claude_job.py next --wait-seconds) instead of single-shot check-then-die. Empirically verified (2026-07-06) that a single long-running Bash call survives well past the host's ~90s between-tool-call inactivity kill — that timer only fires on MODEL silence, not on one in-flight tool call — so one Bash call can safely poll for QUEUE_WORKER_POLL_SECONDS before giving up. This cuts the every-minute spin-up-empty-then-die husk cycle down to roughly one session per poll window instead of one per cron tick. v7: universal type-blind worker. ONE task claims `--type any`; per-type execution notes (e.g. the v6 incremental-draft pacing for twitter-prep) moved into claude_job.py TYPE_TO_WORKER_NOTES and ride the prompt sidecar, so the worker prompt never mentions job types. Legacy per-type tasks get this same body on refresh and become interchangeable universal workers.
// v10 (PLANNED, NOT IMPLEMENTED): delegate the actual drafting to a fresh
// sub-agent per claimed job (claim -> delegate -> wait -> claim next, looped
// within one continuous worker session) instead of drafting inline. Validated
// via throwaway probe tasks 2026-07-07/08 (10 loop iterations, ~210s of real
// delegated work, survives); the one hard constraint proven: the delegated
// sub-agent must never fully idle-wait (e.g. background + wait on a Monitor
// notification) or the host kills the whole parent+child chain in 1-3 min.
// Never live-fire tested against a real production job. Full design, what's
// validated vs not, and the implementation steps: docs/queue-worker-delegation-plan.md
// Bump this constant to 10 only once that plan is actually implemented.
const QUEUE_WORKER_PROMPT_MARKER = "s4l_queue_worker_prompt_version";
// How long ONE `next --wait-seconds` call polls before giving up and exiting.
// 900s (15 min, per Matthew 2026-07-17, up from 240s): sits AT the single-
// Bash-call survival ceiling verified live on 2026-07-06 (the host's ~90s
// inactivity kill fires only on model silence, and one in-flight tool call
// survived a full 900s probe). This covers the ~8min average real job
// inter-arrival gap outright, so most jobs are claimed by an already-polling
// session instead of paying a fresh spin-up, and MCP boot side effects
// (backfill checks, backlog drains) run 1/15min instead of 1/5min. Watch
// point: 900s has zero margin below the verified ceiling — if workers start
// dying mid-poll with no reaper kill recorded, the host clipped the call;
// back off to 600s. The cron's `* * * * *` cadence remains the outer safety
// net for whatever the poll window doesn't catch.
// COUPLING: scripts/reap_stale_claude_sessions.py's S4L_REAPER_CLAIM_GRACE_SEC
// default MUST stay >= this value + margin — a claimless session inside this
// poll window is legitimately still working, not a husk, and a too-tight
// claim_grace would SIGTERM it mid-poll before it ever gets to claim.
// (Bumped to 1020s alongside this change.)
const QUEUE_WORKER_POLL_SECONDS = 900;

// One spec per worker task. queueType MUST match scripts/claude_job.py TAG_TO_TYPE.
const QUEUE_WORKERS: { taskId: string; queueType: string; human: string }[] = [
  { taskId: WORKER_TASK_ID, queueType: "any", human: "universal queue" },
];
// Earlier installs created these instead. Never created anymore; their
// SKILL.md is refreshed to the universal body on boot (see
// ensureQueueWorkerPromptsCurrent) so they keep draining every job type until
// the menubar self-heal consolidates them into s4l-worker.
const LEGACY_QUEUE_WORKER_TASK_IDS = [LEGACY_UNIVERSAL_TASK_ID, PHASE1_TASK_ID, PHASE2B_TASK_ID];

function scheduledTaskSkillPath(taskId: string): string {
  const cfg = process.env.CLAUDE_CONFIG_DIR || path.join(os.homedir(), ".claude");
  return path.join(cfg, "scheduled-tasks", taskId, "SKILL.md");
}

// The queue dir the worker reads/writes. MUST equal what the launchd kicker sets
// (kickerEnv below) and what claude_job.py uses, so both ends meet on one path.
function queueDir(): string {
  return path.join(s4lStateDir(), "claude-queue");
}

// A draft job left unclaimed in pending/ this long (ms) means no scheduled-task
// routine is draining the queue — the worker would claim within a minute if it
// were firing. This is the liveness signal that survives a Claude account switch
// (which orphans the routines while their global SKILL.md files stay put, so the
// SKILL.md-presence check in autopilotLoaded() reads a FALSE green). Mirrors
// HEALTHY_DRAIN_MAX_SECONDS in scripts/schedule_state.py, the single source of
// truth every Python stall detector derives from; this TS constant can't import
// it, so keep it in sync by hand when retuning there.
const AUTOPILOT_STALL_MS = 1_200_000;

// True when no scheduled-task routine is draining the draft queue. Two signals,
// OR'd (keep in lockstep with mcp/menubar/s4l_menubar.py::_autopilot_stalled and
// scripts/autopilot_stall_watch.py):
//   (1) LATCHED: the producer's drain-status.json shows >=1 consecutive timeout
//       with no drain since. Survives the between-cycle gap (no pending file then),
//       so the signal is CONTINUOUS, not flickery. The durable signal.
//   (2) FAST: a draft job has sat unclaimed past AUTOPILOT_STALL_MS — catches a
//       fresh stall before the first full producer timeout has latched (1).
// False-positive free: an idle queue (no candidates) has no pending job and the
// producer clears the latch on every successful drain.
function autopilotStalled(): boolean {
  // (1) latched producer drain-status
  try {
    const ds = JSON.parse(fs.readFileSync(path.join(queueDir(), "drain-status.json"), "utf-8"));
    if (Number(ds?.consecutive_timeouts || 0) >= 1) return true;
  } catch {
    /* no marker yet */
  }
  // (2) fast pending-age
  try {
    const pendRoot = path.join(queueDir(), "pending");
    let oldest = Infinity;
    for (const sub of fs.readdirSync(pendRoot, { withFileTypes: true })) {
      if (!sub.isDirectory()) continue;
      // feedback-digest jobs are latency-insensitive (every-minute kicker,
      // retried forever) and may legitimately queue behind a multi-minute
      // draft job; aging past the draft threshold there is NOT an autopilot stall.
      if (sub.name === "feedback-digest") continue;
      const subPath = path.join(pendRoot, sub.name);
      for (const f of fs.readdirSync(subPath)) {
        if (!f.endsWith(".json") || f.endsWith(".tmp")) continue;
        try {
          const m = fs.statSync(path.join(subPath, f)).mtimeMs;
          if (m < oldest) oldest = m;
        } catch {
          /* skip */
        }
      }
    }
    if (oldest !== Infinity && Date.now() - oldest > AUTOPILOT_STALL_MS) return true;
  } catch {
    /* no pending dir */
  }
  return false;
}

// Dedicated working directory the queue-worker scheduled tasks should RUN in.
//
// Claude Code/Desktop buckets every session under
// ~/.claude/projects/<encoded-run-cwd>/, and the interactive resume/history
// picker is scoped to the CURRENT folder's bucket by default. The two workers
// fire every minute, so if they run in the user's project folder they flood that
// folder's `claude --resume` picker with `<scheduled-task ...>` sessions
// (~2,880/day, mostly empty no-ops). Pointing them at a dedicated folder the user
// never opens interactively keeps those sessions in a SEPARATE bucket
// (-Users-<user>--s4l-worker), leaving the project's picker clean. Safe because
// the worker body uses absolute paths and the MCP + settings.json allow-rules are
// global, not folder-scoped, so the run cwd is functionally irrelevant — only the
// session bucketing changes. The "autopilot on" signal keys off the SKILL.md under
// the config dir (scheduledTaskSkillPath), not the run folder, so it is unaffected.
//
// NOTE: the host tool create_scheduled_task exposes no `folder` param, so the run
// folder is set host-side at creation (the onboarding session's folder) or via the
// Routines UI -> Edit -> Folder. queue_setup surfaces this path + the instruction.
function queueWorkerCwd(): string {
  return path.join(process.env.HOME || os.homedir(), ".s4l-worker");
}

// A single worker task's SKILL.md. Bash-only: claim -> follow the job's own
// prompt -> write JSON -> submit. Paths are baked in at generation time because
// the unattended Bash session can't resolve our env. TYPE-BLIND BY DESIGN: the
// worker claims `--type any` and never knows what kinds of jobs exist. The
// job's prompt sidecar is fully self-contained — the pipeline's real prompt
// plus any per-type WORKER EXECUTION NOTES that claude_job.py prepends at
// claim time (pacing, persist cadence). Adding a new job type touches ONLY
// claude_job.py; this prompt and the scheduled task never change.
function queueWorkerBody(spec: { taskId: string; queueType: string; human: string }): string {
  const py = resolvePython();
  const job = path.join(repoDir(), "scripts", "claude_job.py");
  const sd = s4lStateDir();
  const outDir = queueDir();
  return [
    `You are the S4L queue worker. Run ONE iteration, then STOP.`,
    ``,
    `The deterministic pipeline runs on this Mac. When it needs a Claude turn it ` +
      `drops a job on a local file queue. Your only job: pick up the next job, do ` +
      `EXACTLY what its prompt says, hand the result back. You do this with Bash, ` +
      `Read, and Write, and NOTHING else. This run is unattended — reaching for any ` +
      `other tool, or trying to "investigate", STALLS it forever.`,
    ``,
    `PACING — CRITICAL: this unattended session is terminated ~90 seconds after ` +
      `your LAST tool call (a host inactivity timeout). That clock only runs BETWEEN ` +
      `tool calls, not during one — step 1 below is a single Bash call that can ` +
      `legitimately take several minutes to return, and that is fine. Make your ` +
      `first tool call promptly, and once you are drafting (step 2), if the job's ` +
      `prompt gives you per-item persist commands to run (its own quick Bash calls), ` +
      `run them as you complete each item instead of working silently — those calls ` +
      `are what keep the session alive. The prompt file may begin with a WORKER ` +
      `EXECUTION NOTES header; follow it exactly.`,
    ``,
    `Steps:`,
    `1. Look for the next job. Run this EXACT Bash command and let it run to ` +
      `completion — it polls internally for up to ${Math.round(QUEUE_WORKER_POLL_SECONDS / 60)} ` +
      `minutes before giving up, so it may take a while to return. That is normal: ` +
      `do NOT interrupt it and do NOT make any other tool call while it is running.`,
    `     ${py} ${job} next --type any --prompt-file --wait-seconds ${QUEUE_WORKER_POLL_SECONDS} --state-dir ${sd}`,
    `   It prints one line of JSON once it returns. If it prints "{}" (empty), no ` +
      `job showed up during the whole poll window — report "no jobs" in one line ` +
      `and STOP. You are done.`,
    `2. Otherwise it prints {"job_id":"...","prompt_file":"...","schema_file":...}. ` +
      `Use the Read tool to read prompt_file; it is the complete, self-contained ` +
      `instruction the pipeline wrote for you. If the Read result says it is partial ` +
      `or truncated, keep reading the same file with offsets until EOF. If schema_file ` +
      `is not null, read it too. Follow the prompt EXACTLY and produce the SINGLE JSON ` +
      `object it asks for. If a schema is present, your JSON MUST satisfy it. Output ` +
      `ONLY that JSON object — no prose, no markdown, no code fences.`,
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
    `HARD RULES: use ONLY the Bash tool (to run claude_job.py AND any persist ` +
      `commands the job's prompt explicitly gives you), the Read tool (the ` +
      `prompt/schema sidecar + the SKILL/config files the prompt names), and the ` +
      `Write tool (the result file). NEVER post, reply, open a browser, or run any ` +
      `command the prompt does not explicitly give you. An empty queue is the ` +
      `NORMAL, expected case most minutes — it is success, not a problem to debug.`,
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
    `description: S4L queue worker — claims the next job from the local pipeline ` +
    `queue, drafts it, writes the result back. Never posts.\n` +
    `---\n\n` +
    queueWorkerBody(spec) +
    `\n\n<!-- ${QUEUE_WORKER_PROMPT_MARKER}: ${QUEUE_WORKER_PROMPT_VERSION} -->\n`
  );
}

// Refresh each worker task's SKILL.md when this build ships a newer prompt than
// what's on disk. Best-effort, only touches
// an EXISTING task (onboarding creates them), only when stale. Also rewrites when
// the baked-in paths (python/repo) would have changed, since a stale absolute
// path would break the Bash commands; we detect that by always rewriting on a
// version bump and trust the version gate otherwise.
function ensureQueueWorkerPromptsCurrent(): void {
  for (const spec of QUEUE_WORKERS) {
    try {
      const skillPath = scheduledTaskSkillPath(spec.taskId);
      // Write the prompt on boot if it's MISSING (not just when stale). This makes
      // the worker SKILL.md ALWAYS present, so re-arm only ever needs the host
      // create_scheduled_task (which points filePath at it) — it never depends on
      // queue_setup being callable. Previously we skipped when absent, which left a
      // freshly-switched/onboarded account with no prompt file and forced the
      // queue_setup path (broken when the tool isn't exposed). create-if-missing.
      if (!fs.existsSync(skillPath)) {
        fs.mkdirSync(path.dirname(skillPath), { recursive: true });
        fs.writeFileSync(skillPath, queueWorkerSkillMd(spec), "utf-8");
        console.error(`[queue-worker] wrote missing ${spec.taskId} prompt -> v${QUEUE_WORKER_PROMPT_VERSION}`);
        continue;
      }
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
  // Legacy per-type workers (pre-universal installs): refresh their SKILL.md to
  // the SAME universal body, but ONLY when the file already exists — we never
  // create them anymore. This upgrades an old box's two tasks into two
  // interchangeable universal workers with zero re-onboarding (the host task
  // registration keeps firing; only the prompt file changes).
  for (const taskId of LEGACY_QUEUE_WORKER_TASK_IDS) {
    try {
      const skillPath = scheduledTaskSkillPath(taskId);
      if (!fs.existsSync(skillPath)) continue;
      const cur = fs.readFileSync(skillPath, "utf-8");
      const m = new RegExp(`${QUEUE_WORKER_PROMPT_MARKER}:\\s*(\\d+)`).exec(cur);
      const curVer = m ? parseInt(m[1], 10) : 0;
      if (curVer >= QUEUE_WORKER_PROMPT_VERSION) continue;
      fs.writeFileSync(
        skillPath,
        queueWorkerSkillMd({ taskId, queueType: "any", human: "universal queue" }),
        "utf-8"
      );
      console.error(
        `[queue-worker] refreshed legacy ${taskId} prompt -> universal v${QUEUE_WORKER_PROMPT_VERSION} (was v${curVer})`
      );
    } catch (e: any) {
      console.error(`[queue-worker] ensure legacy ${taskId} prompt error: ${e?.message || e}`);
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
    // Blanket Bash. The scheduled-task runner only auto-approves a permission
    // request if every suggested rule is in the task's approvedPermissions store
    // (which we cannot populate from here); otherwise the unattended session hangs
    // on the prompt and is SIGTERM-killed at ~90s. The fix is to make the CLI
    // auto-allow EVERY Bash phrasing up front so no request is ever emitted. The
    // scoped rules below missed model phrasings like `cd … && python3 …` or odd
    // quoting on log_draft.py's --text, which caused intermittent draft timeouts.
    // This worker is single-purpose and its SKILL.md tightly scopes what it runs.
    "Bash",
    // Kept for clarity / belt-and-suspenders (tightest match first).
    `Bash(${resolvePython()} ${job}:*)`,
    `Bash(python3 ${job}:*)`,
    `Bash(${job}:*)`,
    "Bash(python3:*)",
    "Bash(python:*)",
    // File tools the worker uses (Write) + ones it might reach for without stalling.
    "Write",
    "Read",
    "Edit",
    "Glob",
    "Grep",
    // This server's tools, both namespaces (manifest name + protocol name).
    "mcp__social-autoposter__queue_setup",
    "mcp__social-autoposter__approve_drafts",
    "mcp__social-autoposter__project_config",
    "mcp__social-autoposter__get_stats",
    "mcp__social-autoposter__dashboard",
    "mcp__S4L__queue_setup",
    "mcp__S4L__approve_drafts",
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

// Mark the dedicated worker folder as trusted in ~/.claude.json so the unattended
// scheduled-task sessions can actually START there. Claude Code/Desktop gates every
// session behind a per-folder trust check (hasTrustDialogAccepted). A brand-new
// folder like ~/.s4l-worker has no project entry, so on a headless box the worker
// session stalls at checkTrust forever — there is no human to click "trust the files
// in this folder" — and the queue never drains. We create the folder ourselves
// (boot + queue_setup), so we own trusting it too. Without this, repointing the two
// routines at the dedicated folder silently wedges the WHOLE pipeline: seen 2026-06-26
// when a box's worker cwd switched to ~/.s4l-worker and every worker session died at
// checkTrust (Starting/Mapping never logged), producing 0 drafts for hours. Idempotent
// and atomic; never throws. (The already-trusted onboarding project folder works
// because the setup session triggered the trust dialog there once.)
function ensureWorkerFolderTrusted(): void {
  try {
    const home = process.env.HOME || os.homedir();
    const cfgPath = path.join(home, ".claude.json");
    if (!fs.existsSync(cfgPath)) return; // Claude Code not initialised yet; nothing to merge into
    let cfg: any;
    try {
      cfg = JSON.parse(fs.readFileSync(cfgPath, "utf-8"));
    } catch (e: any) {
      console.error(`[queue-worker] ~/.claude.json unparseable; skip trust: ${e?.message || e}`);
      return;
    }
    if (typeof cfg !== "object" || Array.isArray(cfg) || cfg === null) return;
    const projects = (cfg.projects ??= {});
    if (typeof projects !== "object" || Array.isArray(projects)) return;
    const folder = queueWorkerCwd();
    const existing = projects[folder];
    if (existing && existing.hasTrustDialogAccepted === true) return; // already trusted; no write
    // Preserve any fields a prior interactive open wrote; only force the trust flag.
    const entry =
      existing && typeof existing === "object" && !Array.isArray(existing)
        ? { ...existing }
        : {
            allowedTools: [],
            disabledMcpjsonServers: [],
            enabledMcpjsonServers: [],
            hasClaudeMdExternalIncludesApproved: false,
            hasClaudeMdExternalIncludesWarningShown: false,
            mcpContextUris: [],
            projectOnboardingSeenCount: 0,
          };
    entry.hasTrustDialogAccepted = true;
    projects[folder] = entry;
    // Atomic write: ~/.claude.json is large and read by every CLI session; a torn
    // write would brick Claude Code. Stage a temp sibling, then rename over it.
    const tmp = `${cfgPath}.s4l-trust.${process.pid}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(cfg, null, 2) + "\n", "utf-8");
    fs.renameSync(tmp, cfgPath);
    console.error(`[queue-worker] trusted worker folder in ~/.claude.json: ${folder}`);
  } catch (e: any) {
    console.error(`[queue-worker] ensureWorkerFolderTrusted error: ${e?.message || e}`);
  }
}

// Register this .mcpb server into ~/.claude.json `mcpServers` so the embedded
// Cowork/Code agent discovers S4L too. The Chat tab loads S4L via Desktop's
// LocalMcpServerManager (.mcpb extensions); the Cowork/Code tab is a SEPARATE,
// real `claude-code` binary launched with `--setting-sources=user,project,local`
// that only reads MCP servers from its setting sources + plugin dirs and NEVER
// sees .mcpb extensions. So S4L shows up in Chat but is absent in Cowork no matter
// how many restarts — the two surfaces don't share MCP state (confirmed
// 2026-06-30 from the embedded process args on the box: empty user `mcpServers`,
// S4L only present as a .mcpb). Writing a user-scoped `mcpServers` entry is the
// path `--setting-sources=user` honors, so Cowork picks it up on its next session.
// We point the entry at THIS running server's own dist/index.js (absolute,
// install-location-agnostic) so both npm and .mcpb installs self-register the
// correct path. Idempotent (writes only when missing/drifted), atomic (every CLI
// session reads this file; a torn write would brick Claude Code), never throws.
// Runs on every boot, so a box whose ~/.claude.json didn't exist yet self-heals on
// the next restart once a Code/Cowork session has created it. Kill switch:
// S4L_COWORK_MCP=0.
function ensureCoworkMcpRegistered(): void {
  try {
    if ((process.env.S4L_COWORK_MCP) === "0") return;
    const home = process.env.HOME || os.homedir();
    const cfgPath = path.join(home, ".claude.json");
    if (!fs.existsSync(cfgPath)) return; // Claude Code not initialised yet; retry next boot
    let cfg: any;
    try {
      cfg = JSON.parse(fs.readFileSync(cfgPath, "utf-8"));
    } catch (e: any) {
      console.error(`[cowork-mcp] ~/.claude.json unparseable; skip register: ${e?.message || e}`);
      return;
    }
    if (typeof cfg !== "object" || Array.isArray(cfg) || cfg === null) return;
    const servers = (cfg.mcpServers ??= {});
    if (typeof servers !== "object" || Array.isArray(servers)) return;
    const serverEntry = path.join(DIST_DIR, "index.js");
    const desired = {
      command: "node",
      args: [serverEntry],
      env: { PATH: "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" },
    };
    const current = servers["social-autoposter"];
    // Skip the write when the entry already matches, to avoid churning a file every
    // CLI session reads. Re-write when missing or drifted (install moved, older
    // version registered a different path/env).
    if (current && JSON.stringify(current) === JSON.stringify(desired)) return;
    servers["social-autoposter"] = desired;
    const tmp = `${cfgPath}.s4l-cowork.${process.pid}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(cfg, null, 2) + "\n", "utf-8");
    fs.renameSync(tmp, cfgPath);
    console.error(`[cowork-mcp] registered S4L in ~/.claude.json mcpServers -> ${serverEntry}`);
  } catch (e: any) {
    console.error(`[cowork-mcp] ensureCoworkMcpRegistered error: ${e?.message || e}`);
  }
}

// ---- launchd kicker: run the REAL pipeline in queue mode ---------------------
// Reinstates com.m13v.social-twitter-cycle as the customer-box kicker. It runs
// run-twitter-cycle.sh straight through (scan -> score -> draft -> link-gen); its
// `claude -p` steps carry queue-mapped tags, so run_claude.sh routes them through
// the job queue (claude_job.py TAG_TO_TYPE) for the scheduled-task workers to
// service. The per-cycle DRAFT_ONLY value is NOT
// baked here anymore: run-draft-and-publish.sh decides it from mode.json
// (draft-only flag, 2026-07-06) — while draft-only is ON (default) cycles stop
// before posting and merge into review cards; with draft-only OFF (operator
// opt-out) promotion cycles post autonomously behind the virality bar. The DRAFT_ONLY=1 below is only the safe baseline an OLD wrapper (which
// never overrides it) inherits.
// 60s tick (2026-07-06): parity with the retired claude -p twitter-cycle plist.
// The preflight slot gate (max-1) makes this safe — a tick that fires while a
// cycle is still running skips in milliseconds — so the effective cadence is
// back-to-back ~10-min cycles with ≤1 min idle, not one cycle per minute.
const QUEUE_KICKER_INTERVAL_SECS = 60;

function kickerEnv(): Record<string, string> {
  return {
    DRAFT_ONLY: "1",
    // S4L_CLAUDE_PROVIDER is gone (2026-07-06): queue routing is by script tag
    // (claude_job.py TAG_TO_TYPE), so the plist carries no routing switch.
    S4L_STATE_DIR: s4lStateDir(),
    TWITTER_PAGE_GEN_RATE: "0",
    // Thread-media context for the drafter (2026-07-06): parity with the
    // retired CLI twitter-cycle plist. Without it the prep step drafts blind to
    // images in the candidate threads.
    S4L_TWITTER_CAPTURE_MEDIA: "1",
    // NOTE: TWITTER_TAIL_LINK_RATE is deliberately NOT set here (2026-07-06).
    // The persona lane exports =0 per cycle via s4l_mode.py env (link-free
    // organic replies); promotion cycles keep the script's own default so
    // draft-only-OFF posts carry the project link per the A/B gate, and card posts
    // still force =1.0 inside approve_drafts.
    // Virality bar percentile is NOT set here: it is hardcoded to 0.90 in
    // skill/run-twitter-cycle.sh (single source of truth, no env dependency).
  };
}

// Shared draftability gate for BOTH autopilot kickers (X queue + Reddit search).
// A launchd discovery/draft kicker only makes sense to schedule when SOMETHING is
// draftable, and the definition of "draftable" is identical across platforms — so
// it lives here as the single source of truth. Two paths qualify:
//   (a) a managed product project is ready (promotion lane), OR
//   (b) personal_brand mode is on AND the persona is ready (self-promo lane).
// Path (b) is easy to miss: the persona is deliberately excluded from the
// managed-products scope (see ensurePersonaProject), so a personal-brand-only
// install has an empty managedProjects() and a product-only check is always
// false. That gap silently sank the X kicker (fixed 2026-06-30) and then the
// Reddit kicker (fixed 2026-07-16, which reused the product-only check instead of
// this persona-aware one). Keeping both kickers on this ONE helper means the
// personal-brand path can never regress on one platform but not the other.
function draftableLaneStatus(): { draftable: boolean; detail: string } {
  const productReady = listManagedProjectStatus().some((p) => p.ready);
  if (productReady) return { draftable: true, detail: "ready project" };
  const personaActive = currentFlags().personal_brand && personaReady();
  if (personaActive) {
    return { draftable: true, detail: "active persona (personal_brand)" };
  }
  return { draftable: false, detail: "no ready project or active persona yet" };
}

async function ensureQueueKickerInstalled(): Promise<{ ok: boolean; detail: string }> {
  try {
    if (process.platform !== "darwin") return { ok: false, detail: "not macOS" };
    if (!runtimeReady()) return { ok: false, detail: "runtime not ready" };
    // Gate: install the kicker only when SOMETHING is draftable (product lane OR
    // active persona). See draftableLaneStatus — shared with the Reddit kicker.
    const lane = draftableLaneStatus();
    if (!lane.draftable) {
      return { ok: false, detail: lane.detail };
    }
    // Additional gate: X must be connected and verified before autopilot starts.
    // This prevents wasted cycles against a logged-out x.com if X connection didn't
    // persist after import/login. x_verified milestone only completes after doctor
    // confirms X session and cookies are valid.
    const onboardingState = onboardingSnapshot();
    const xVerified = onboardingState?.milestones?.some(
      (m: any) => m.id === "x_verified" && m.status === "complete"
    );
    if (!xVerified) {
      return {
        ok: false,
        detail: "X not yet verified (awaiting explicit pre-flight confirmation)",
      };
    }
    const logDir = path.join(repoDir(), "skill", "logs");
    try {
      fs.mkdirSync(logDir, { recursive: true });
    } catch {
      /* best-effort */
    }
    const xml = plistXml({
      label: TWITTER_AUTOPILOT_LABEL,
      // Run the DRAFT-AND-PUBLISH wrapper, NOT run-twitter-cycle.sh directly:
      // it runs the cycle (DRAFT_ONLY + queue) then MERGES the plan into the
      // review-queue cards. The cycle alone leaves drafts in an orphan /tmp plan
      // nobody reads (the 2026-06-24 merge gap). This is the ONLY card producer.
      programArgs: ["bash", path.join(repoDir(), "skill", "run-draft-and-publish.sh")],
      intervalSecs: QUEUE_KICKER_INTERVAL_SECS,
      runAtLoad: false, // don't fire a heavy cycle the instant Claude launches
      stdoutLog: path.join(logDir, "launchd-twitter-cycle-stdout.log"),
      stderrLog: path.join(logDir, "launchd-twitter-cycle-stderr.log"),
      extraEnv: kickerEnv(),
      // Don't let launchd reap the harness Chrome the cycle launches when the
      // kicker shell exits (2026-07-12 foreground-steal loop).
      abandonProcessGroup: true,
    });
    // Content-aware install: an existing box has the OLD kicker plist pointing at
    // run-twitter-cycle.sh (no merge step). ensurePlist won't overwrite, so detect
    // a drifted plist and rewrite + reload it. Otherwise the merge fix never
    // reaches an already-installed kicker.
    const uid = process.getuid ? process.getuid() : 0;
    let cur: string | null = null;
    try {
      cur = fs.readFileSync(TWITTER_AUTOPILOT_PLIST, "utf-8");
    } catch {
      cur = null;
    }
    let detail: string;
    if (cur === xml) {
      const res = await loadPlist(TWITTER_AUTOPILOT_LABEL, TWITTER_AUTOPILOT_PLIST, uid);
      detail = `current (load rc=${res.code})`;
    } else {
      if (cur !== null) {
        await unloadPlist(TWITTER_AUTOPILOT_LABEL, TWITTER_AUTOPILOT_PLIST, uid);
      }
      fs.mkdirSync(path.dirname(TWITTER_AUTOPILOT_PLIST), { recursive: true });
      fs.writeFileSync(TWITTER_AUTOPILOT_PLIST, xml, "utf-8");
      const res = await loadPlist(TWITTER_AUTOPILOT_LABEL, TWITTER_AUTOPILOT_PLIST, uid);
      detail = cur === null ? "installed + loaded" : `rewritten + reloaded (rc=${res.code})`;
      // First-EVER install (cur === null): fire ONE immediate cycle so a brand-new
      // user gets their first drafts at setup completion instead of waiting up to a
      // full QUEUE_KICKER_INTERVAL_SECS tick. `launchctl kickstart` runs the job
      // THROUGH launchd (full baked env + the run-*-singleton.sh lock), so it is
      // NOT a bare manual kick — it cannot produce the empty-plan artifact that a
      // hand-run of run-twitter-cycle.sh would. We keep RunAtLoad=false so this
      // does NOT re-fire on every later Claude launch or on a drift-rewrite; the
      // cur === null gate restricts the kick to true first-time onboarding only.
      if (cur === null) {
        // First-run boost marker: run-draft-and-publish.sh reads this and widens
        // the first draft cycle(s) to a 48h discovery window with the top-1 card
        // cap lifted (top 5), so a brand-new user's first review batch shows
        // SEVERAL real drafts instead of one (or none). The wrapper deletes the
        // marker as soon as a merge delivers cards, or after 24h without any, so
        // every later cycle runs the standard 24h + top-1 logic. Best-effort: a
        // failed write just means a standard first cycle.
        try {
          const stateDir = s4lStateDir();
          fs.mkdirSync(stateDir, { recursive: true });
          fs.writeFileSync(
            path.join(stateDir, "first-run-boost.json"),
            JSON.stringify({ created_at: new Date().toISOString() }) + "\n",
            "utf-8"
          );
        } catch (e: any) {
          console.error(
            "[social-autoposter-mcp] first-run boost marker write failed:",
            e?.message || e
          );
        }
        const kick = await run(
          "launchctl",
          ["kickstart", `gui/${uid}/${TWITTER_AUTOPILOT_LABEL}`],
          { timeoutMs: 15_000 }
        );
        detail += ` + first-run kick (rc=${kick.code})`;
      }
    }
    return { ok: true, detail };
  } catch (e: any) {
    return { ok: false, detail: e?.message || String(e) };
  }
}

// ---- Reddit discovery kicker (optional platform) ----------------------------
// Mirrors ensureQueueKickerInstalled for the reddit lane: a 15-minute launchd
// job that runs skill/run-reddit-search-launchd.sh (the detach wrapper; the
// real cycle honors the global draft-only flag via s4l_mode.py, and its
// drafting turn rides the same s4l-worker queue as X). Gates:
//   - runtime ready
//   - reddit connected (connected OR connected_idle; the profile persists)
//   - a managed project is configured and ready
// It is NEVER an onboarding requirement: installs that never connect reddit
// simply never get this plist.
//
// OPERATOR-MAC SAFETY: a hand-built com.m13v.social-reddit-search plist
// pointing at ~/social-autoposter (StartCalendarInterval, custom env) exists
// on the dev box. If an existing plist's program path is not the managed
// package's wrapper, we log and leave it completely alone: no rewrite, no
// reload, no unload.
async function ensureRedditKickerInstalled(): Promise<{ ok: boolean; detail: string }> {
  try {
    if (process.platform !== "darwin") return { ok: false, detail: "not macOS" };
    if (!runtimeReady()) return { ok: false, detail: "runtime not ready" };
    // Gate: install the kicker only when SOMETHING is draftable. Mirror the X
    // queue kicker EXACTLY via the shared draftableLaneStatus — a personal-brand-
    // only install has no managed product, so the old product-only check here made
    // this kicker permanently uninstallable (no reddit drafts) even with a ready
    // persona (2026-07-16 bug). Both kickers now share the one persona-aware gate.
    const lane = draftableLaneStatus();
    if (!lane.draftable) return { ok: false, detail: lane.detail };
    // Reddit session gate. status is read-only (never launches Chrome): a live
    // me.json validation when the harness is up, the on-disk profile cookie
    // check (connected_idle) when it isn't.
    const rs = await redditStatus();
    if (!rs.connected) {
      return { ok: false, detail: `reddit not connected (${rs.state})` };
    }

    const managedWrapper = path.join(repoDir(), "skill", "run-reddit-search-launchd.sh");
    // Foreign-plist guard BEFORE any write: if a plist already exists and its
    // ProgramArguments do not point at the managed package's wrapper, it is a
    // hand-built / operator arrangement. Leave it untouched.
    let cur: string | null = null;
    try {
      cur = fs.readFileSync(REDDIT_SEARCH_PLIST, "utf-8");
    } catch {
      cur = null;
    }
    if (cur !== null && !cur.includes(`<string>${managedWrapper}</string>`)) {
      const msg = `existing ${REDDIT_SEARCH_LABEL} plist points outside the managed package; leaving it untouched`;
      console.error(`[reddit-kicker] ${msg}`);
      return { ok: true, detail: `skip: ${msg}` };
    }

    const logDir = path.join(repoDir(), "skill", "logs");
    try {
      fs.mkdirSync(logDir, { recursive: true });
    } catch {
      /* best-effort */
    }
    const xml = plistXml({
      label: REDDIT_SEARCH_LABEL,
      programArgs: ["bash", managedWrapper],
      intervalSecs: REDDIT_SEARCH_INTERVAL_SECS,
      runAtLoad: false, // never fire a discovery cycle the instant Claude launches
      stdoutLog: path.join(logDir, "launchd-reddit-search-stdout.log"),
      stderrLog: path.join(logDir, "launchd-reddit-search-stderr.log"),
      // S4L_REPO_DIR + S4L_PYTHON are baked by plistXml. State dir + CDP url
      // pin the wrapper's children to the managed state and the harness port.
      extraEnv: {
        S4L_STATE_DIR: s4lStateDir(),
        REDDIT_CDP_URL: REDDIT_CDP_URL_DEFAULT,
      },
      // The wrapper double-forks the real cycle into its own session already,
      // but keep launchd away from the harness Chrome all the same.
      abandonProcessGroup: true,
    });
    const uid = process.getuid ? process.getuid() : 0;
    if (cur === xml) {
      const res = await loadPlist(REDDIT_SEARCH_LABEL, REDDIT_SEARCH_PLIST, uid);
      return { ok: true, detail: `current (load rc=${res.code})` };
    }
    if (cur !== null) {
      await unloadPlist(REDDIT_SEARCH_LABEL, REDDIT_SEARCH_PLIST, uid);
    }
    fs.mkdirSync(path.dirname(REDDIT_SEARCH_PLIST), { recursive: true });
    fs.writeFileSync(REDDIT_SEARCH_PLIST, xml, "utf-8");
    const res = await loadPlist(REDDIT_SEARCH_LABEL, REDDIT_SEARCH_PLIST, uid);
    // No first-run kickstart on purpose: reddit discovery is a background
    // add-on, and the connect flow just finished using the harness browser.
    // The first StartInterval tick (≤15 min) is soon enough.
    return {
      ok: true,
      detail: cur === null ? "installed + loaded" : `rewritten + reloaded (rc=${res.code})`,
    };
  } catch (e: any) {
    return { ok: false, detail: e?.message || String(e) };
  }
}

// ---- launchd reaper: kill leaked agent-mode claude worker sessions ----------
// Independent guardrail (NOT gated on a project being ready): the leak happens
// whenever the scheduled-task workers fire, and a no-leak run is a cheap no-op.
// Runs the stdlib-only reaper under SYSTEM python (always present, zero deps) so
// it works even before the owned runtime provisions. Content-aware install so an
// already-installed box picks up a changed interval/path on the next Claude boot.
const REAPER_INTERVAL_SECS = 60; // match the ~1/min worker spawn cadence

async function ensureClaudeReaperInstalled(): Promise<{ ok: boolean; detail: string }> {
  try {
    if (process.platform !== "darwin") return { ok: false, detail: "not macOS" };
    const logDir = path.join(repoDir(), "skill", "logs");
    try {
      fs.mkdirSync(logDir, { recursive: true });
    } catch {
      /* best-effort */
    }
    const xml = plistXml({
      label: REAPER_LABEL,
      programArgs: ["/usr/bin/python3", path.join(repoDir(), "scripts", "reap_stale_claude_sessions.py")],
      intervalSecs: REAPER_INTERVAL_SECS,
      runAtLoad: true, // clean up an existing backlog the instant Claude launches
      stdoutLog: path.join(logDir, "launchd-claude-reaper-stdout.log"),
      stderrLog: path.join(logDir, "launchd-claude-reaper-stderr.log"),
    });
    const uid = process.getuid ? process.getuid() : 0;
    let cur: string | null = null;
    try {
      cur = fs.readFileSync(REAPER_PLIST, "utf-8");
    } catch {
      cur = null;
    }
    let detail: string;
    if (cur === xml) {
      const res = await loadPlist(REAPER_LABEL, REAPER_PLIST, uid);
      detail = `current (load rc=${res.code})`;
    } else {
      if (cur !== null) {
        await unloadPlist(REAPER_LABEL, REAPER_PLIST, uid);
      }
      fs.mkdirSync(path.dirname(REAPER_PLIST), { recursive: true });
      fs.writeFileSync(REAPER_PLIST, xml, "utf-8");
      const res = await loadPlist(REAPER_LABEL, REAPER_PLIST, uid);
      detail = cur === null ? "installed + loaded" : `rewritten + reloaded (rc=${res.code})`;
    }
    return { ok: true, detail };
  } catch (e: any) {
    return { ok: false, detail: e?.message || String(e) };
  }
}

// ---- launchd feedback digest: card decisions -> learned_preferences ---------
// Every minute, same cadence as com.m13v.social-twitter-cycle (the drafting
// producer), stdlib-only under SYSTEM python (http_api + learned_preferences
// use urllib/json only; run_claude.sh resolves the claude CLI itself). A run
// with no unprocessed review_events for this install is a cheap no-op, so the
// job is installed unconditionally like the reaper. Content-aware install so
// an already-installed box picks up changed args on the next Claude boot.
// (Was hourly from the day this shipped, 2026-07-02, on purpose: the digest
// was designed as a standalone scheduled batch job, not a per-event trigger.
// Changed 2026-07-06 to close the gap with edits/feedback sitting unprocessed
// for up to an hour: every-minute checks are still cheap no-ops between
// actionable events, since digest_project() only calls Claude when at least
// one fetched event is actionable.)
const FEEDBACK_DIGEST_INTERVAL_SECS = 60;

async function ensureFeedbackDigestInstalled(): Promise<{ ok: boolean; detail: string }> {
  try {
    if (process.platform !== "darwin") return { ok: false, detail: "not macOS" };
    const logDir = path.join(repoDir(), "skill", "logs");
    try {
      fs.mkdirSync(logDir, { recursive: true });
    } catch {
      /* best-effort */
    }
    const xml = plistXml({
      label: FEEDBACK_DIGEST_LABEL,
      programArgs: ["/usr/bin/python3", path.join(repoDir(), "scripts", "feedback_digest.py")],
      intervalSecs: FEEDBACK_DIGEST_INTERVAL_SECS,
      runAtLoad: false, // no boot-time Claude runs; the every-minute tick is enough
      stdoutLog: path.join(logDir, "launchd-feedback-digest-stdout.log"),
      stderrLog: path.join(logDir, "launchd-feedback-digest-stderr.log"),
    });
    const uid = process.getuid ? process.getuid() : 0;
    let cur: string | null = null;
    try {
      cur = fs.readFileSync(FEEDBACK_DIGEST_PLIST, "utf-8");
    } catch {
      cur = null;
    }
    let detail: string;
    if (cur === xml) {
      const res = await loadPlist(FEEDBACK_DIGEST_LABEL, FEEDBACK_DIGEST_PLIST, uid);
      detail = `current (load rc=${res.code})`;
    } else {
      if (cur !== null) {
        await unloadPlist(FEEDBACK_DIGEST_LABEL, FEEDBACK_DIGEST_PLIST, uid);
      }
      fs.mkdirSync(path.dirname(FEEDBACK_DIGEST_PLIST), { recursive: true });
      fs.writeFileSync(FEEDBACK_DIGEST_PLIST, xml, "utf-8");
      const res = await loadPlist(FEEDBACK_DIGEST_LABEL, FEEDBACK_DIGEST_PLIST, uid);
      detail = cur === null ? "installed + loaded" : `rewritten + reloaded (rc=${res.code})`;
    }
    return { ok: true, detail };
  } catch (e: any) {
    return { ok: false, detail: e?.message || String(e) };
  }
}

// Install/refresh the autopilot stall watchdog launchd job. Runs off the owned
// venv python so scripts/autopilot_stall_watch.py can import sentry_init +
// sentry-sdk. RunAtLoad so a box that boots already-stalled reports promptly.
async function ensureStallWatchInstalled(): Promise<{ ok: boolean; detail: string }> {
  try {
    if (process.platform !== "darwin") return { ok: false, detail: "not macOS" };
    if ((process.env.S4L_STALL_WATCH) === "0") return { ok: false, detail: "disabled (S4L_STALL_WATCH=0)" };
    const logDir = path.join(repoDir(), "skill", "logs");
    try {
      fs.mkdirSync(logDir, { recursive: true });
    } catch {
      /* best-effort */
    }
    const xml = plistXml({
      label: STALL_WATCH_LABEL,
      programArgs: [resolvePython(), path.join(repoDir(), "scripts", "autopilot_stall_watch.py")],
      intervalSecs: STALL_WATCH_INTERVAL_SECS,
      runAtLoad: true,
      stdoutLog: path.join(logDir, "launchd-stall-watch-stdout.log"),
      stderrLog: path.join(logDir, "launchd-stall-watch-stderr.log"),
    });
    const uid = process.getuid ? process.getuid() : 0;
    let cur: string | null = null;
    try {
      cur = fs.readFileSync(STALL_WATCH_PLIST, "utf-8");
    } catch {
      cur = null;
    }
    let detail: string;
    if (cur === xml) {
      const res = await loadPlist(STALL_WATCH_LABEL, STALL_WATCH_PLIST, uid);
      detail = `current (load rc=${res.code})`;
    } else {
      if (cur !== null) {
        await unloadPlist(STALL_WATCH_LABEL, STALL_WATCH_PLIST, uid);
      }
      fs.mkdirSync(path.dirname(STALL_WATCH_PLIST), { recursive: true });
      fs.writeFileSync(STALL_WATCH_PLIST, xml, "utf-8");
      const res = await loadPlist(STALL_WATCH_LABEL, STALL_WATCH_PLIST, uid);
      detail = cur === null ? "installed + loaded" : `rewritten + reloaded (rc=${res.code})`;
    }
    return { ok: true, detail };
  } catch (e: any) {
    return { ok: false, detail: e?.message || String(e) };
  }
}

async function ensureMemorySnapshotInstalled(): Promise<{ ok: boolean; detail: string }> {
  try {
    if (process.platform !== "darwin") return { ok: false, detail: "not macOS" };
    if ((process.env.S4L_MEMORY_SNAPSHOT) === "0") return { ok: false, detail: "disabled (S4L_MEMORY_SNAPSHOT=0)" };
    const logDir = path.join(repoDir(), "skill", "logs");
    try {
      fs.mkdirSync(logDir, { recursive: true });
    } catch {
      /* best-effort */
    }
    const xml = plistXml({
      label: MEMORY_SNAPSHOT_LABEL,
      programArgs: ["/bin/bash", path.join(repoDir(), "skill", "memory-snapshot.sh")],
      intervalSecs: MEMORY_SNAPSHOT_INTERVAL_SECS,
      runAtLoad: true,
      stdoutLog: path.join(logDir, "launchd-memory-snapshot-stdout.log"),
      stderrLog: path.join(logDir, "launchd-memory-snapshot-stderr.log"),
    });
    const uid = process.getuid ? process.getuid() : 0;
    let cur: string | null = null;
    try {
      cur = fs.readFileSync(MEMORY_SNAPSHOT_PLIST, "utf-8");
    } catch {
      cur = null;
    }
    let detail: string;
    if (cur === xml) {
      const res = await loadPlist(MEMORY_SNAPSHOT_LABEL, MEMORY_SNAPSHOT_PLIST, uid);
      detail = `current (load rc=${res.code})`;
    } else {
      if (cur !== null) {
        await unloadPlist(MEMORY_SNAPSHOT_LABEL, MEMORY_SNAPSHOT_PLIST, uid);
      }
      fs.mkdirSync(path.dirname(MEMORY_SNAPSHOT_PLIST), { recursive: true });
      fs.writeFileSync(MEMORY_SNAPSHOT_PLIST, xml, "utf-8");
      const res = await loadPlist(MEMORY_SNAPSHOT_LABEL, MEMORY_SNAPSHOT_PLIST, uid);
      detail = cur === null ? "installed + loaded" : `rewritten + reloaded (rc=${res.code})`;
    }
    return { ok: true, detail };
  } catch (e: any) {
    return { ok: false, detail: e?.message || String(e) };
  }
}

// ---- pause/resume: stop drafting/posting without touching Claude Desktop ---
// Unlike Quit (which kills + relaunches Claude Desktop and deletes the draft
// schedule), Pause only unloads the launchd jobs that actually DO work — the
// kicker that scans/drafts/posts (TWITTER_AUTOPILOT_LABEL) plus its reaper,
// stall-watch, and memory-snapshot support daemons — while leaving Claude
// Desktop, the tray, and the Claude-native s4l-worker scheduled task alone.
// The scheduled task still fires on its own cadence; claude_job.py's `next`
// checks the same flag file and reports "no work" instantly instead of
// draining the queue, so nothing drafts or posts while paused even if a
// worker session wakes up mid-pause. A flag file (nothing is deleted) is what
// makes Resume a plain reinstall of the same 4 daemons — see resumeS4L.
function pauseFlagPath(): string {
  return path.join(s4lStateDir(), "paused.flag");
}

function isPaused(): boolean {
  try {
    return fs.existsSync(pauseFlagPath());
  } catch {
    return false;
  }
}

const PAUSE_TARGETS: Array<{ label: string; plist: string }> = [
  { label: TWITTER_AUTOPILOT_LABEL, plist: TWITTER_AUTOPILOT_PLIST },
  { label: REAPER_LABEL, plist: REAPER_PLIST },
  { label: STALL_WATCH_LABEL, plist: STALL_WATCH_PLIST },
  { label: MEMORY_SNAPSHOT_LABEL, plist: MEMORY_SNAPSHOT_PLIST },
];

async function pauseS4L(): Promise<{ ok: boolean; detail: string }> {
  if (isPaused()) return { ok: true, detail: "already paused" };
  try {
    fs.mkdirSync(path.dirname(pauseFlagPath()), { recursive: true });
    fs.writeFileSync(pauseFlagPath(), `paused at ${new Date().toISOString()}\n`, "utf-8");
  } catch (e: any) {
    return { ok: false, detail: `could not write pause flag: ${e?.message || e}` };
  }
  const uid = process.getuid ? process.getuid() : 0;
  const results: string[] = [];
  for (const { label, plist } of PAUSE_TARGETS) {
    try {
      const res = await unloadPlist(label, plist, uid);
      results.push(`${label}: unloaded (rc=${res.code})`);
    } catch (e: any) {
      results.push(`${label}: ${e?.message || e}`);
    }
  }
  return { ok: true, detail: results.join("; ") };
}

async function resumeS4L(): Promise<{ ok: boolean; detail: string }> {
  try {
    fs.rmSync(pauseFlagPath(), { force: true });
  } catch {
    /* best-effort */
  }
  const kicker = await ensureQueueKickerInstalled();
  const reaper = await ensureClaudeReaperInstalled();
  const stall = await ensureStallWatchInstalled();
  const mem = await ensureMemorySnapshotInstalled();
  const detail = [
    `kicker: ${kicker.ok ? "ok" : "skip"} (${kicker.detail})`,
    `reaper: ${reaper.ok ? "ok" : "skip"} (${reaper.detail})`,
    `stall-watch: ${stall.ok ? "ok" : "skip"} (${stall.detail})`,
    `memory-snapshot: ${mem.ok ? "ok" : "skip"} (${mem.detail})`,
  ].join("; ");
  return { ok: true, detail };
}

// Install/refresh the on-screen overlay watcher launchd job. Promotes the
// harness status overlay from a best-effort, fired-from-other-tools nicety to a
// first-class self-healing job. We run `harness_overlay.py watch` directly in
// the FOREGROUND under KeepAlive (RunAtLoad starts it at boot; launchd restarts
// it if it ever exits) rather than a StartInterval that re-fires a spawn-and-exit
// supervisor: on macOS that supervisor races launchd, which SIGKILLs the job's
// process group the instant the kicker shell exits and reaps the just-spawned
// watcher before it can detach (verified on the box: the watcher caught the
// group SIGTERM and cleared the overlay every cycle). harness_overlay.py holds a
// singleton flock so the MCP's best-effort run-overlay-watch.sh lane can never
// double-paint. S4L_PYTHON is baked by plistXml; we add S4L_LOG_DIR (so the
// watcher reads the same cycle logs to decide busy/idle) and the harness CDP
// URL. Disable with S4L_OVERLAY_WATCH=0.
async function ensureOverlayWatchInstalled(): Promise<{ ok: boolean; detail: string }> {
  try {
    if (process.platform !== "darwin") return { ok: false, detail: "not macOS" };
    if ((process.env.S4L_OVERLAY_WATCH) === "0") return { ok: false, detail: "disabled (S4L_OVERLAY_WATCH=0)" };
    const logDir = path.join(repoDir(), "skill", "logs");
    try {
      fs.mkdirSync(logDir, { recursive: true });
    } catch {
      /* best-effort */
    }
    const xml = plistXml({
      label: OVERLAY_WATCH_LABEL,
      programArgs: [resolvePython(), path.join(repoDir(), "scripts", "harness_overlay.py"), "watch"],
      intervalSecs: 0,
      keepAlive: true,
      runAtLoad: true,
      stdoutLog: path.join(logDir, "launchd-overlay-watch-stdout.log"),
      stderrLog: path.join(logDir, "launchd-overlay-watch-stderr.log"),
      extraEnv: {
        S4L_LOG_DIR: logDir,
        TWITTER_CDP_URL: process.env.TWITTER_CDP_URL || "http://127.0.0.1:9555",
      },
    });
    const uid = process.getuid ? process.getuid() : 0;
    let cur: string | null = null;
    try {
      cur = fs.readFileSync(OVERLAY_WATCH_PLIST, "utf-8");
    } catch {
      cur = null;
    }
    let detail: string;
    if (cur === xml) {
      const res = await loadPlist(OVERLAY_WATCH_LABEL, OVERLAY_WATCH_PLIST, uid);
      detail = `current (load rc=${res.code})`;
    } else {
      if (cur !== null) {
        await unloadPlist(OVERLAY_WATCH_LABEL, OVERLAY_WATCH_PLIST, uid);
      }
      fs.mkdirSync(path.dirname(OVERLAY_WATCH_PLIST), { recursive: true });
      fs.writeFileSync(OVERLAY_WATCH_PLIST, xml, "utf-8");
      const res = await loadPlist(OVERLAY_WATCH_LABEL, OVERLAY_WATCH_PLIST, uid);
      detail = cur === null ? "installed + loaded" : `rewritten + reloaded (rc=${res.code})`;
    }
    return { ok: true, detail };
  } catch (e: any) {
    return { ok: false, detail: e?.message || String(e) };
  }
}

// Install/refresh the daily self-updater launchd job. This used to be bundled
// into the now-deleted `autopilot` MCP tool (removed 2026-06-19, 88bd1cb9):
// calling `autopilot enable` installed this job as a side effect, so removing
// the tool silently orphaned it — UPDATER_LABEL/UPDATER_PLIST and the
// auto_update_on status check survived (buildSnapshot still reports them), but
// nothing has installed the plist since, on ANY box provisioned after that
// commit (auto_update_on reads false forever). Restored here as its own
// deterministic boot-time job, same pattern as its five siblings above.
//
// Points at scripts/s4l_box_update.sh, NOT skill/social-autoposter-update.sh:
// the latter is the npm-lane updater (npm view + npx update) and is a silent
// no-op on a .mcpb box, which has no npm/npx on PATH (see version.ts). The
// .mcpb-lane equivalent downloads the .mcpb directly from the channel-resolved
// GitHub release and unpacks it over the extension dir — see that script's own
// header for the channel/no-downgrade/retry guards. Default mode there
// downloads + unpacks + restarts Claude Desktop with NO human in the loop,
// matching the original bundled updater's intent ("keeps a headless install
// current"); RunAtLoad so a box that boots already-behind checks promptly.
async function ensureUpdaterInstalled(): Promise<{ ok: boolean; detail: string }> {
  try {
    if (process.platform !== "darwin") return { ok: false, detail: "not macOS" };
    if ((process.env.S4L_AUTO_UPDATE) === "0") return { ok: false, detail: "disabled (S4L_AUTO_UPDATE=0)" };
    const logDir = path.join(repoDir(), "skill", "logs");
    try {
      fs.mkdirSync(logDir, { recursive: true });
    } catch {
      /* best-effort */
    }
    const xml = plistXml({
      label: UPDATER_LABEL,
      programArgs: ["/bin/bash", path.join(repoDir(), "scripts", "s4l_box_update.sh")],
      intervalSecs: 86_400,
      runAtLoad: true,
      stdoutLog: path.join(logDir, "launchd-self-update-stdout.log"),
      stderrLog: path.join(logDir, "launchd-self-update-stderr.log"),
    });
    const uid = process.getuid ? process.getuid() : 0;
    let cur: string | null = null;
    try {
      cur = fs.readFileSync(UPDATER_PLIST, "utf-8");
    } catch {
      cur = null;
    }
    let detail: string;
    if (cur === xml) {
      const res = await loadPlist(UPDATER_LABEL, UPDATER_PLIST, uid);
      detail = `current (load rc=${res.code})`;
    } else {
      if (cur !== null) {
        await unloadPlist(UPDATER_LABEL, UPDATER_PLIST, uid);
      }
      fs.mkdirSync(path.dirname(UPDATER_PLIST), { recursive: true });
      fs.writeFileSync(UPDATER_PLIST, xml, "utf-8");
      const res = await loadPlist(UPDATER_LABEL, UPDATER_PLIST, uid);
      detail = cur === null ? "installed + loaded" : `rewritten + reloaded (rc=${res.code})`;
    }
    return { ok: true, detail };
  } catch (e: any) {
    return { ok: false, detail: e?.message || String(e) };
  }
}

// Is the draft schedule registered AND running for the LIVE account?
//   'ok'       — worker tasks present+enabled and FIRING (host actively running).
//   'disabled' — present but a worker task is disabled.
//   'missing'  — not firing anywhere (orphaned / not registered for the live
//                account) -> dashboard offers "Set up draft schedule".
// The algorithm (live-account detection via config.json's lastKnownAccountUuid,
// firing window, etc.) lives in ONE place: scripts/schedule_state.py. The Python menu bar imports
// that module in-process; we shell out to it here. Keeping a single implementation
// is the whole point — the two surfaces can no longer drift. The script is
// stdlib-only and resolvePython() falls back to system python3, so this works even
// before the owned runtime is provisioned. Any failure -> "missing" (safe: a
// schedule we can't read is treated as not-firing, which only ever surfaces the
// re-arm affordance, never a false "ok").
async function scheduleState(): Promise<"missing" | "disabled" | "stalled" | "ok"> {
  try {
    const res = await runPython("scripts/schedule_state.py", [], { timeoutMs: 15_000 });
    const state = JSON.parse(res.stdout.trim()).state;
    if (state === "ok" || state === "disabled" || state === "stalled") return state;
    return "missing";
  } catch {
    return "missing";
  }
}

// Assemble everything the panel needs in one shot (projects + X + autopilot +
// version). Resilient: any probe that throws degrades to a safe default rather
// than failing the whole snapshot.
async function buildSnapshot() {
  // Single source of truth: scripts/snapshot.py computes the snapshot PURELY from
  // the stateful files (the SAME module the always-on menu bar imports directly,
  // so the two surfaces can't diverge — and the menu bar no longer depends on this
  // Node process being up). We shell out for the data, then layer on the MCP-only
  // side effects snapshot.py deliberately omits (it is a pure reader): the doctor
  // phase, onboarding-milestone telemetry, and persistence.
  let snap: Record<string, any>;
  try {
    const res = await runPython("scripts/snapshot.py", [], { timeoutMs: 95_000 });
    snap = JSON.parse(res.stdout.trim().split("\n").slice(-50).join("\n"));
    if (snap && snap._error) throw new Error(String(snap._error));
  } catch {
    // Never fail the whole panel: fall back to a minimal locally-derived snapshot.
    snap = {
      projects: [], projects_total: 0, projects_ready: 0,
      x_connected: false, x_state: "", x_handle: null,
      autopilot_on: false, autopilot_stalled: false, schedule_state: "missing",
      auto_update_on: false, version: VERSION, latest_version: null,
      update_available: false, runtime_ready: runtimeReady(),
      runtime_provisioning: isProvisioning(), setup_complete: false,
      mode: currentMode(), flags: currentFlags(), onboarding: onboardingSnapshot(),
    };
  }
  // MCP-only side effects (snapshot.py is a pure reader and does none of these):
  // the onboarding LEDGER writes here are telemetry/history; the live DISPLAY
  // statuses already come from snapshot.py's overlay.
  // Is the always-on menu bar app actually loaded? snapshot.py can't answer this
  // (it's a launchctl check the Node side owns), so layer it on here. The panel
  // uses it to offer a one-click "restart menu bar" when the tray was quit.
  snap.menubar_running = await menubarRunning();
  snap.paused = isPaused();
  await ensureDoctorPhase(snap.x_connected ? "full" : "pre_connect");
  if (snap.runtime_ready) completeOnboardingMilestone("runtime_ready");
  if (snap.x_connected) completeOnboardingMilestone("x_connected", { state: snap.x_state || "connected" });
  // Reddit (optional platform) heal: a live connected reading on a LATER poll
  // is itself persistence evidence, so it may also complete reddit_verified
  // for an install whose post-connect verify probe transiently failed.
  if (snap.reddit_connected) {
    completeOnboardingMilestone("reddit_connected", { state: snap.reddit_state || "connected" });
    completeOnboardingMilestone("reddit_verified", { source: "live_snapshot" });
  }
  if ((snap.projects_ready || 0) > 0) completeOnboardingMilestone("project_ready", { missing_count: 0 });
  if (snap.schedule_state === "ok") completeOnboardingMilestone("tasks_scheduled");
  // mode_chosen completes when the user explicitly picked a mode (mode.json
  // exists) OR this is a legacy install already past setup (a ready project),
  // so adding this step never regresses an already-onboarded box. (Moved here
  // from project_config's status branch so ALL milestone truth lives in one
  // place next to the snapshot.)
  if (modeChosen() || (snap.projects_ready || 0) > 0) {
    completeOnboardingMilestone("mode_chosen", {
      source: modeChosen() ? "chosen" : "backfilled_legacy",
    });
  }
  // profile_scanned has no file signal of its own, but a provisioned persona
  // project can only come from the scan+dictation flow — treat its presence as
  // the completed scan so old installs can't stick at "profile pending".
  if ((Array.isArray(snap.projects) ? snap.projects : []).some((p: any) => p?.persona)) {
    completeOnboardingMilestone("profile_scanned", { backfill: true });
  }
  // topics_seeded has no cheap live signal (it's a DB fact, not a file fact), so
  // installs that finished setup before the persona-path completion landed stay
  // stuck at 7/8 forever. Heal by re-running the idempotent seed for the ready
  // project(s) and completing the milestone on success. Fire-and-forget: the
  // ledger flip shows on the next snapshot.
  if ((snap.projects_ready || 0) > 0) void healTopicsSeeded(snap);
  // Persist this snapshot so the menu bar can answer "set up?" the SAME way when
  // the loopback server is unreachable (Claude Desktop closed or mid-restart)
  // instead of falling back to a divergent local rule. Refreshed on every
  // dashboard call (≈1s while the menu bar polls online), so the on-disk copy is
  // never more than a poll stale. Best-effort; never fails the snapshot.
  persistStatusSummary(snap);
  return snap;
}

// One attempt per process: a failed seed (offline DB) retries on the next MCP
// launch, not on every dashboard poll.
let topicsSeedHealAttempted = false;

async function healTopicsSeeded(snap: Record<string, any>): Promise<void> {
  if (topicsSeedHealAttempted) return;
  topicsSeedHealAttempted = true;
  try {
    const ledger = onboardingLedger();
    if (ledger?.milestones?.topics_seeded?.status === "complete") return;
    const ready = (Array.isArray(snap.projects) ? snap.projects : []).filter(
      (p: any) => p && p.ready && typeof p.name === "string"
    );
    for (const p of ready) {
      const seed = await runPython("scripts/seed_search_topics.py", ["--project", p.name], {
        timeoutMs: 60_000,
      });
      if (seed.code === 0) {
        const m = /planned=(\d+)\s+inserted=(\d+)\s+updated=(\d+)/.exec(seed.stdout);
        completeOnboardingMilestone("topics_seeded", {
          project: p.name,
          topic_count: m ? Number(m[1]) : 0,
          backfill: true,
        });
        return;
      }
    }
  } catch (e: any) {
    console.error("[snapshot] topics_seeded heal failed:", e?.message || e);
  }
}

// ---- dashboard localhost fallback -----------------------------------------
// When the connected host doesn't support MCP Apps UI (Claude Code / Cowork
// today), serve the SAME dist/panel.html from a loopback HTTP server. The page
// detects it's running over HTTP (window.__S4L_BRIDGE__) and routes every
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
  const inject = `<script>window.__S4L_BRIDGE__=${JSON.stringify("http")};</script>`;
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
    // Optional fixed port (S4L_PANEL_PORT) for deterministic addressing; default
    // is an OS-assigned ephemeral port.
    const wantPort = Number(process.env.S4L_PANEL_PORT) || 0;
    srv.listen(wantPort, "127.0.0.1", () => {
      const addr = srv.address();
      const port = typeof addr === "object" && addr ? addr.port : 0;
      localPanel = { url: `http://127.0.0.1:${port}/`, server: srv };
      writePanelUrl(localPanel.url);
      resolve(localPanel.url);
    });
  });
}

function panelEndpointPath(): string {
  return path.join(process.env.HOME || os.homedir(), ".social-autoposter-mcp", "panel-endpoint.json");
}

function isPidAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

function readPanelEndpoint(): { url?: string; pid?: number } | null {
  try {
    return JSON.parse(fs.readFileSync(panelEndpointPath(), "utf-8"));
  } catch {
    return null;
  }
}

// Publish the loopback URL to stable files so out-of-process readers can find
// the ephemeral port without scraping `lsof`:
//   - panel-url            plain text, for the Claude Code side-panel reverse proxy.
//   - panel-endpoint.json  richer (url + version + pid), for the menu bar app,
//                          which POSTs /tool/<name> here for live data.
// Best-effort: a write failure never blocks the panel (readers re-check /health).
//
// panel-endpoint.json is a SINGLE shared file that every S4L MCP process writes
// to on boot (Claude Desktop/Cowork, a Claude Code side-panel session, the
// ~/.s4l-worker queue runner, ...). Startup here is eager (see main(), "Eagerly
// start the loopback panel server") so even a one-shot queue-worker invocation
// that lives for well under a minute claims this file, then exits and leaves it
// pointing at a dead pid until some other process happens to overwrite it. The
// menu bar depends on this file to find a server to POST approved drafts to
// (s4l_state.py loopback_tool), so a dead pointer silently strands approved
// drafts (post_failed: loopback_unreachable) until pure chance fixes the file.
// Fix: don't steal the slot from an existing registrant that's still alive —
// last-writer-wins only among writers that found a dead (or absent) entry.
function writePanelUrl(url: string): void {
  try {
    const dir = path.join(process.env.HOME || os.homedir(), ".social-autoposter-mcp");
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, "panel-url"), url, "utf-8");
    const existing = readPanelEndpoint();
    if (existing?.pid && existing.pid !== process.pid && isPidAlive(existing.pid)) {
      // Someone else already holds a live registration (most likely a longer-
      // lived session than us) — don't clobber it. Our own panel is still up
      // and fully usable via `url` for anything that already has it (e.g. this
      // process's own Code side-panel proxy); we just don't publish ourselves
      // as THE shared menu-bar target.
      logPanelEvent(`panel_cede existing_pid=${existing.pid}`);
      return;
    }
    logPanelEvent(
      existing?.pid
        ? `panel_takeover dead_pid=${existing.pid}`
        : `panel_claim previous=none`
    );
    fs.writeFileSync(
      panelEndpointPath(),
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

// Relinquish panel-endpoint.json on clean exit if we currently own it, so a
// short-lived process (typical for the ~/.s4l-worker queue runner or a one-off
// Claude Code session) never leaves a dead pid lingering as a false-positive
// registrant — the next process to check sees "nothing registered" (clean,
// correctly reported as unreachable) rather than a stale pointer that only
// gets fixed by chance when something else happens to boot.
process.on("exit", () => {
  try {
    const existing = readPanelEndpoint();
    if (existing?.pid === process.pid) {
      fs.unlinkSync(panelEndpointPath());
    }
  } catch {
    // best-effort; nothing to do if it's already gone or unreadable
  }
});

// The owned state dir, honoring S4L_STATE_DIR (matches menubar/s4l_state.py).
function s4lStateDir(): string {
  return (
    process.env.S4L_STATE_DIR ||
    path.join(process.env.HOME || os.homedir(), ".social-autoposter-mcp")
  );
}

// Has the user explicitly chosen an engagement mode? mode.json is written by the
// engagement_mode tool (setup) and the menu-bar toggle. Used to complete the
// mode_chosen onboarding milestone. (Source of truth: scripts/s4l_mode.py.)
function modeChosen(): boolean {
  try {
    return fs.existsSync(path.join(s4lStateDir(), "mode.json"));
  } catch {
    return false;
  }
}

// The current engagement lane flags, surfaced in the snapshot so the dashboard
// AND menu bar read them from ONE place (mode.json, the same file s4l_mode.py
// writes). Mirrors s4l_mode.py get_flags(): explicit flag keys win; else map a
// legacy {"mode": ...} string; else default personal ON / promotion OFF.
function currentFlags(): { personal_brand: boolean; promotion: boolean } {
  try {
    const d = JSON.parse(fs.readFileSync(path.join(s4lStateDir(), "mode.json"), "utf-8"));
    if ("personal_brand" in d || "promotion" in d) {
      return { personal_brand: !!d.personal_brand, promotion: !!d.promotion };
    }
    const m = (d.mode || "").trim();
    if (m === "personal_brand") return { personal_brand: true, promotion: false };
    if (m === "promotion") return { personal_brand: false, promotion: true };
  } catch {
    /* fall through to default */
  }
  return { personal_brand: true, promotion: false };
}

// Derived legacy single-mode string (personal wins when on). Defaults to
// personal_brand when unset (2026-06-29 default flip).
function currentMode(): "promotion" | "personal_brand" {
  return currentFlags().personal_brand ? "personal_brand" : "promotion";
}

// ---- Cross-instance "posting active" flag ----------------------------------
// posting-active.json in the shared state dir is the CROSS-MCP-INSTANCE version
// of the in-process `postingActive` flag. The autopilot scan and the post
// sometimes run in the SAME MCP (the in-process flag covers that) and sometimes
// in TWO SEPARATE MCP instances (different agent sessions each spawn their own).
// A file every instance's draft-cycle scan reads makes the mutual exclusion hold
// regardless of which topology Claude Desktop happens to use. Heartbeat'd with a
// short TTL so a crashed poster's flag self-clears and never wedges scanning.
const POSTING_FLAG_TTL_MS = 45_000;
let postingFlagHeartbeat: ReturnType<typeof setInterval> | null = null;
function postingFlagPath(): string {
  return path.join(s4lStateDir(), "posting-active.json");
}
function writePostingFlag(): void {
  try {
    fs.mkdirSync(s4lStateDir(), { recursive: true });
    fs.writeFileSync(
      postingFlagPath(),
      JSON.stringify({ pid: process.pid, expires_at: Date.now() + POSTING_FLAG_TTL_MS }) + "\n",
      "utf-8"
    );
  } catch (e) {
    // The 2026-06-23 saga flagged "posting-active.json never writes" as open,
    // and this silent catch is why nobody could tell. Say it, both places.
    console.error(`[post] posting-active flag write FAILED: ${String(e)}`);
    logPostEvent(`posting_flag_write_failed err=${String(e)}`);
  }
}
function startPostingFlagHeartbeat(): void {
  writePostingFlag();
  try {
    if (!fs.existsSync(postingFlagPath())) {
      logPostEvent(`posting_flag_missing_after_write path=${postingFlagPath()}`);
    }
  } catch {
    /* best effort */
  }
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
// == not posting. This is what makes a sibling instance's draft-cycle scan defer.
function isPostingFlagFresh(): boolean {
  try {
    const j = JSON.parse(fs.readFileSync(postingFlagPath(), "utf-8"));
    return typeof j?.expires_at === "number" && j.expires_at > Date.now();
  } catch {
    return false;
  }
}

// True when a DIFFERENT process holds a fresh posting flag — i.e. a sibling MCP
// instance is mid-drain. Our own fresh flag doesn't count: same-process
// re-entrancy is covered by the in-memory `postingActive`, and a leftover flag
// from our own earlier drain (a crash before the finally cleared it) must not
// deadlock us against ourselves.
function isPeerDrainActive(): boolean {
  try {
    const j = JSON.parse(fs.readFileSync(postingFlagPath(), "utf-8"));
    if (typeof j?.expires_at !== "number" || j.expires_at <= Date.now()) return false;
    return typeof j?.pid === "number" && j.pid !== process.pid;
  } catch {
    return false;
  }
}

// Human-readable reason for the postApproved wait gate, used only for the
// wait_start log line — distinguishes "our own drain hasn't hit its grace
// release yet" from "a peer MCP instance (different pid, e.g. a separate
// Claude session/queue-worker) is mid-drain," and names the peer pid + how
// much longer its flag has left so a log reader doesn't have to guess.
function describePostingBlocker(): string {
  if (postingActive) return "own_in_process_flag";
  try {
    const j = JSON.parse(fs.readFileSync(postingFlagPath(), "utf-8"));
    if (typeof j?.expires_at === "number" && j.expires_at > Date.now() && typeof j?.pid === "number") {
      return `peer_pid=${j.pid} expires_in_ms=${j.expires_at - Date.now()}`;
    }
  } catch {
    /* best effort */
  }
  return "unknown";
}

// activity.json: a tiny "what's running right now" signal the menu bar reads to
// show a loading spinner + label (scanning / drafting / posting / …). Written by
// long-running tools, cleared when they finish. Best-effort; absence == idle.
let _activityLast: { state: string; label: string } | null = null;
let _activityHb: ReturnType<typeof setInterval> | null = null;
function _writeActivityFile(state: string, label: string): void {
  try {
    const dir = s4lStateDir();
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
function writeActivity(state: string, label: string): void {
  _activityLast = { state, label };
  _writeActivityFile(state, label);
  // Heartbeat: re-stamp `since` so the menu bar's staleness TTL (s4l_state.py
  // ACTIVITY_TTL_SECONDS) never ages out a genuinely-running tool whose current
  // phase emits no further updates — e.g. a silent multi-minute `claude -p` draft
  // turn between "Phase 2b-prep" and the next marker. Without this, the spinner
  // would wrongly blink to idle mid-work; with it, the label is fresh exactly
  // while the tool runs and the TTL only expires it once clearActivity stops the
  // heartbeat (or the writer dies). Single shared interval; tracks the latest label.
  if (!_activityHb) {
    _activityHb = setInterval(() => {
      if (_activityLast) _writeActivityFile(_activityLast.state, _activityLast.label);
    }, 30_000);
    if (typeof _activityHb.unref === "function") _activityHb.unref();
  }
}
function clearActivity(): void {
  _activityLast = null;
  if (_activityHb) {
    clearInterval(_activityHb);
    _activityHb = null;
  }
  try {
    fs.rmSync(path.join(s4lStateDir(), "activity.json"), { force: true });
  } catch {
    /* best effort */
  }
}

// status-summary.json: the server's last-known dashboard snapshot, persisted so
// the menu bar's OFFLINE path (loopback unreachable) reads a precomputed answer
// instead of re-deriving setup_complete with its own copy of the rules. One
// producer (buildSnapshot), one consumer (menubar/s4l_state.py snapshot()).
// Written atomically so a 1s poll never sees a half-written file.
function persistStatusSummary(snap: Record<string, unknown>): void {
  try {
    const dir = s4lStateDir();
    fs.mkdirSync(dir, { recursive: true });
    const tmp = path.join(dir, `status-summary.json.${process.pid}.tmp`);
    fs.writeFileSync(
      tmp,
      JSON.stringify({ ...snap, written_at: new Date().toISOString() }) + "\n",
      "utf-8"
    );
    fs.renameSync(tmp, path.join(dir, "status-summary.json"));
  } catch {
    /* best effort: a status cache write must never break the dashboard */
  }
}

// Signal the menu bar that a fresh draft batch is ready for pop-up review. The
// chat-table review path is unchanged and still works; this just ALSO lets the
// corner cards drive review (both surfaces de-dup via the plan's `posted` flag).
// The menu bar reads review-request.json, presents the cards, posts via the
// loopback approve_drafts tool, then clears the file. Best-effort: a write failure
// just means no pop-ups this batch (chat review still works).
function writeReviewRequest(req: {
  batch_id: string;
  project: string;
  count: number;
  plan_path: string;
  created_at: string;
}): void {
  try {
    const dir = s4lStateDir();
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
// S4L_PANEL_OPEN_BROWSER=1 to restore the old auto-open behavior. (The URL is
// always returned to the caller regardless, so nothing is lost when we don't open.)
async function openInBrowser(url: string): Promise<void> {
  if (!(process.env.S4L_PANEL_OPEN_BROWSER)) return;
  const cmd =
    process.platform === "darwin" ? "open" : process.platform === "win32" ? "cmd" : "xdg-open";
  const args = process.platform === "win32" ? ["/c", "start", "", url] : [url];
  try {
    await run(cmd, args, { timeoutMs: 10_000 });
  } catch (e: any) {
    console.error("[social-autoposter-mcp] openInBrowser failed:", e?.message || e);
  }
}

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
// The scan that actually holds the browser is a run-twitter-cycle.sh process —
// usually a SIBLING (the every-minute launchd cycle), which we have no
// ChildProcess for. So we bridge to the lock the scanner truly respects: read
// its /tmp pid file, and if a
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
// Timestamped on-disk trail for every scan preemption and posting-flag failure.
// The console.error lines land in Claude Desktop's per-profile MCP log, which has
// proven hard to even LOCATE during incidents (2026-07-06 forensics had to infer
// two of six scan kills from bash job-control lines). This file lives next to the
// post-*.log dumps so one directory tells the whole posting story. Best-effort.
function logPostEvent(msg: string): void {
  try {
    const dir = path.join(repoDir(), "skill", "logs");
    fs.mkdirSync(dir, { recursive: true });
    fs.appendFileSync(
      path.join(dir, "post-preempt-events.log"),
      `${new Date().toISOString()} pid=${process.pid} ${msg}\n`
    );
  } catch {
    /* best effort */
  }
}

// Timestamped on-disk trail of panel-endpoint.json ownership handoffs. Mirrors
// logPostEvent's rationale (per-process console.error is hard to even locate
// across ephemeral, per-host-session MCP instances), for the panel-election
// side instead of the posting side. writePanelUrl() runs on every process's
// eager startup, so this also captures the mundane "claimed with nothing
// registered" and "ceded to an already-alive owner" cases, not just handoffs
// — that's what makes it possible to tell "a fresh instance took over from a
// dead registrant" apart from "an approve click found nothing registered at
// all" after the fact, instead of inferring it from timing alone.
function logPanelEvent(msg: string): void {
  try {
    const dir = path.join(repoDir(), "skill", "logs");
    fs.mkdirSync(dir, { recursive: true });
    fs.appendFileSync(path.join(dir, "panel-events.log"), `${new Date().toISOString()} pid=${process.pid} ${msg}\n`);
  } catch {
    /* best effort */
  }
}

function sigkillScanTree(pid: number): void {
  logPostEvent(`sigkill_scan_tree target=${pid}`);
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
// posting phase. The MCP posts per approved card (separate approve_drafts calls), and
// the old code acquired+released the lock PER CARD — leaving a release window
// BETWEEN every card that a parked scan stale-reclaimed (the hijack). Instead we
// keep the lock and only release it after SHELL_LOCK_GRACE_MS of no posting, so the
// hold EXPANDS as more cards get approved and there is never a gap between cards.
const SHELL_LOCK_GRACE_MS = Number(process.env.S4L_POST_LOCK_GRACE_MS) || 60_000;
let shellLockReleaseTimer: ReturnType<typeof setTimeout> | null = null;
// True from the start of a post batch until SHELL_LOCK_GRACE_MS after the last
// card. The draft-cycle scan checks this and DEFERS launching a scan while it's set —
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

// SIGKILL whatever live process holds the shell browser lock so the post takes
// the browser at once. Universal preemption (2026-07-07, explicit user call):
// posting always wins over ANY other CLI Twitter job — the discovery scan,
// DM engagement, DM outreach, thread posting, follow-up scans, everything.
// This is a deliberate, informed tradeoff, not an oversight: unlike the scan
// (read-only, relaunches every minute, nothing to lose), several of these
// jobs are mid-*send* when they hold this lock (engage-twitter.sh Phase B
// replies, dm-outreach-twitter.sh / engage-dm-replies.sh send DMs,
// run-twitter-threads.sh posts a multi-tweet thread). Killing one of those at
// the wrong instant can leave an action landed on X with its "we did this"
// bookkeeping never written, so it silently retries and double-sends next
// cycle — the same class of bug this file's ghost-post handling exists to
// avoid, just now possible for DMs/threads too. Accepted in exchange for
// posting never waiting on anything. Best-effort; never throws.
function preemptScanHoldingBrowser(): void {
  try {
    const pid = shellLockHolderPid();
    if (pid && pidAlive(pid)) {
      const label = pidIsScan(pid) ? "scan" : "peer job";
      console.error(
        `[post] preempting cross-process ${label} holding the twitter-browser lock (pid ${pid}) — SIGKILL tree`
      );
      logPostEvent(`preempt_holder_holding_browser pid=${pid} kind=${label}`);
      sigkillScanTree(pid);
    }
  } catch {
    /* best effort */
  }
}

// Take (or extend) the shell browser lock for the batch, so posting is aware of
// EVERY CLI Twitter job, not just the discovery scan. The lock dir itself is the
// source of truth: 8+ scripts (engage-twitter.sh, dm-outreach-twitter.sh,
// run-twitter-threads.sh, engage-dm-replies.sh, scan-twitter-followups.sh,
// refresh-twitter-following.sh, audit.sh, invent-supply-test.sh, in addition to
// run-twitter-cycle.sh) all take this exact dir before touching the shared
// harness Chrome, so whoever holds it is doing real browser work by construction
// — there is no per-script allowlist to maintain. 2026-07-07 incident: only
// run-twitter-cycle.sh was ever recognized as preemptable, so posting fell
// through to "proceed unguarded" against every OTHER script and collided with a
// LIVE engage-twitter.sh (DM/mentions engagement) mid-reply — both processes
// reused the same open x.com tab (get_browser_and_page prefers a reusable
// Twitter tab), so engage-twitter.sh's own navigation yanked the composer away
// mid-type and got one candidate wrongly classified tweet_unavailable.
//
// UNIVERSAL PREEMPTION (2026-07-07, explicit user call, superseding the
// wait-for-non-scan-peers version that briefly shipped in rc.13): posting
// SIGKILLs whatever holds this lock, full stop — no waiting on anyone,
// scan or not. Traded away deliberately: several of these jobs are mid-*send*
// when they hold the lock (DM outreach/replies, thread posting, mention
// replies), so killing one at the wrong instant can leave an action landed on
// X with its own "we did this" bookkeeping never written -> a silent retry
// double-sends next cycle, the same class of bug this file's ghost-post
// handling exists to guard against. Accepted in exchange for posting never
// blocking on anything else running.
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
        `[post] holding twitter-browser shell lock pid=${process.pid} — every other CLI Twitter job yields`
      );
      return true;
    } catch {
      // Dir exists. Reclaim if the holder is dead; SIGKILL-preempt unconditionally
      // otherwise — scan or not, posting always wins.
      const pid = shellLockHolderPid();
      if (!pid || !pidAlive(pid)) {
        rmShellLockDir();
      } else {
        logPostEvent(`preempt_holder_on_lock_acquire pid=${pid} attempt=${attempt}`);
        sigkillScanTree(pid); // SIGKILL — these jobs don't reliably yield to SIGTERM
        await sleepMs(300);
        rmShellLockDir();
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
// in-flight scan so the browser frees up at once. The scan that actually holds the
// shared Chrome is a live run-twitter-cycle.sh (the every-minute launchd cycle);
// kill it cross-process via the /tmp shell lock it truly respects. Best-effort;
// never throws; never touches a locked pipeline script.
function preemptScanForPost(): void {
  preemptScanHoldingBrowser();
}

appTool(
  "dashboard",
  {
    title: "S4L dashboard",
    description:
      "Render the S4L dashboard in chat: a visual surface showing project setup, X " +
      "connection, autopilot state, and 7-day stats, with buttons to set up the schedule, connect X, " +
      "and refresh. Use when the user asks to see the dashboard, panel, " +
      "status, or controls. ALSO call this at the end of any state-changing or results-producing " +
      "action (approve_drafts, get_stats, project_config) so the user sees the " +
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
            ? "This runtime has no WebSocket support, so a live screencast can't be opened. Use 'Bring to front' to see the browser window instead."
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

// REMOVED (2026-07-17): drainApprovedBacklog. It ran 30s after EVERY MCP boot,
// which was sane when boots meant "user launched Claude Desktop" — but each
// queue-worker session boots its own MCP server, so the drain had silently
// become a ~5-minute cron running across up to 4 concurrent MCP instances.
// Combined with universal posting preemption, every drain wakeup SIGKILLed
// whatever held the twitter-browser lock (profile scans included), and any
// stamp bug turned into an infinite retry loop (438 retries over 5 days on
// one Nhat card). Backlog recovery is now owned by ONE long-lived process:
// the menubar's _resume_approved_queue, which runs on loopback-reachable and
// periodically thereafter (mcp/menubar/s4l_menubar.py). Do NOT re-add a
// boot-time drain here; if the menubar is dead, ensureMenubar() below revives
// it and its resume covers the backlog.

async function main() {
  initSentry();
  // Detect a self-update (old_version -> new_version) as the very first thing
  // after Sentry is up, before anything else that could restart/exit. See
  // checkVersionChange's own docstring for why this exists.
  try {
    checkVersionChange();
  } catch (e: any) {
    console.error("[social-autoposter-mcp] version-change check failed:", e?.message || e);
  }
  // Tee the verbatim stdout/stderr of every pipeline subprocess to the s4l
  // Cloud Run relay (-> Cloud Logging) so we can troubleshoot/rescue any user
  // scenario (silent stalls, partial onboarding) without asking them to ship a
  // log file. Best-effort; disabled with S4L_LOG_STREAM=0.
  startLogStreaming();
  // A plugin UPDATE refreshes this server (dist/) but not the materialized
  // pipeline. Re-extract the bundled pipeline.tgz when it's newer than what's on
  // disk, BEFORE serving, so the very first scan uses the shipped pipeline (not
  // the version first materialized at install). Synchronous + best-effort.
  ensurePipelineCurrent();
  // Config's canonical home is the state dir (2026-07-13): migrate a legacy
  // in-repo config.json up on first boot after update, keep the legacy path
  // working via symlink, and self-heal a missing/wrong link on every boot.
  // Must run AFTER ensurePipelineCurrent() so repoDir() points at the final
  // materialized repo.
  console.error(`[social-autoposter-mcp] config home: ${ensureConfigInStateDir()}`);
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
  // Vendored harness patch for ALREADY-ready runtimes: provision() is skipped
  // above when the runtime is ready, so a patch shipped in an rc would never
  // reach an existing install's harness checkout without this. Idempotent
  // (marker check) and best-effort; never blocks boot.
  void ensureHarnessPatched()
    .then((r) => {
      if (!(r.ok && r.detail === "already patched")) {
        console.error(`[social-autoposter-mcp] harness patch: ${r.ok ? "ok" : "skip"} (${r.detail})`);
      }
    })
    .catch(() => {});
  // Queue-backed drafting (2026-06-23): keep the two worker-task prompts current,
  // pre-approve their tools EAGERLY (before onboarding even creates the tasks, so
  // the first unattended fire can't stall), and (re)install the launchd kicker
  // that runs the real DRAFT_ONLY pipeline whose claude -p calls feed the queue.
  // All best-effort; none may block boot.
  ensureQueueWorkerPromptsCurrent();
  ensureQueueWorkerToolsAllowed();
  // Pre-create the dedicated worker folder so a box that already has the tasks can
  // be re-pointed at it (Routines -> Edit -> Folder) without the folder missing.
  // Keeps the per-minute worker sessions out of the project's interactive
  // `claude --resume` picker once the folder is set. Best-effort.
  try {
    fs.mkdirSync(queueWorkerCwd(), { recursive: true });
    // Trust the folder too — without this the per-minute worker sessions stall at
    // Claude's per-folder checkTrust on a headless box and never drain the queue.
    ensureWorkerFolderTrusted();
  } catch (e: any) {
    console.error(`[queue-worker] could not create worker folder: ${e?.message || e}`);
  }
  // The 4 pipeline daemons (kicker, reaper, stall-watch, memory-snapshot) are
  // skipped at boot while paused.flag is present, so a Claude Desktop restart
  // during a Pause doesn't silently un-pause the pipeline — see pauseS4L/
  // resumeS4L. Feedback-digest is NOT gated: it only distills past review
  // decisions, not drafting/posting, so it's harmless to keep running.
  if (!isPaused()) {
    void ensureQueueKickerInstalled()
      .then((r) => console.error(`[queue-worker] launchd kicker: ${r.ok ? "ok" : "skip"} (${r.detail})`))
      .catch((e) => console.error("[queue-worker] kicker install failed:", e?.message || e));
  } else {
    console.error("[queue-worker] launchd kicker: skip (paused)");
  }
  // Reddit discovery kicker (optional platform): installs only when reddit is
  // connected AND a draftable lane exists (product ready OR active persona, via
  // the shared draftableLaneStatus). A box that never connected reddit is a cheap
  // no-op skip. This boot-time call also SELF-HEALS installs broken by the old
  // product-only gate: once the gate passes, the plist lands on the next boot
  // (worker sessions boot ~1/min). Paused-gated like the X kicker (produces drafts).
  if (!isPaused()) {
    void ensureRedditKickerInstalled()
      .then((r) => console.error(`[reddit-kicker] launchd kicker: ${r.ok ? "ok" : "skip"} (${r.detail})`))
      .catch((e) => console.error("[reddit-kicker] kicker install failed:", e?.message || e));
  } else {
    console.error("[reddit-kicker] launchd kicker: skip (paused)");
  }
  // Self-healing reaper for the agent-mode session leak the queue autopilot
  // produces (finished `claude` worker sessions Desktop never tears down). A
  // standalone guardrail; install unconditionally so it caps memory even on a
  // box whose project isn't ready yet. Best-effort; must never block boot.
  if (!isPaused()) {
    void ensureClaudeReaperInstalled()
      .then((r) => console.error(`[claude-reaper] launchd reaper: ${r.ok ? "ok" : "skip"} (${r.detail})`))
      .catch((e) => console.error("[claude-reaper] reaper install failed:", e?.message || e));
  } else {
    console.error("[claude-reaper] launchd reaper: skip (paused)");
  }
  // Feedback digest: hourly distillation of the user's card approve/reject
  // decisions into learned_preferences (see scripts/feedback_digest.py).
  // Best-effort; a box with no review events runs a no-op.
  void ensureFeedbackDigestInstalled()
    .then((r) => console.error(`[feedback-digest] launchd digest: ${r.ok ? "ok" : "skip"} (${r.detail})`))
    .catch((e) => console.error("[feedback-digest] digest install failed:", e?.message || e));
  // Autopilot stall watchdog: fleet-side Sentry alert when the draft routines stop
  // draining (most often an account switch orphaning them). The menu bar shows the
  // user the Re-arm action; this is the part we see. Best-effort; never blocks boot.
  if (!isPaused()) {
    void ensureStallWatchInstalled()
      .then((r) => console.error(`[stall-watch] launchd watchdog: ${r.ok ? "ok" : "skip"} (${r.detail})`))
      .catch((e) => console.error("[stall-watch] watchdog install failed:", e?.message || e));
  } else {
    console.error("[stall-watch] launchd watchdog: skip (paused)");
  }
  // Periodic host-resource sampler (memory/process snapshot -> local JSONL). Gives
  // us per-box resource history to diagnose RAM blowups (e.g. the agent-mode
  // session leak). Best-effort; never blocks boot. Disable with S4L_MEMORY_SNAPSHOT=0.
  if (!isPaused()) {
    void ensureMemorySnapshotInstalled()
      .then((r) => console.error(`[memory-snapshot] launchd sampler: ${r.ok ? "ok" : "skip"} (${r.detail})`))
      .catch((e) => console.error("[memory-snapshot] sampler install failed:", e?.message || e));
  } else {
    console.error("[memory-snapshot] launchd sampler: skip (paused)");
  }
  // On-screen overlay watcher supervisor. The harness status overlay only renders
  // while the watcher process is alive, and that watcher had no supervisor — when
  // it died nothing respawned it and the overlay silently vanished. Install it as
  // a first-class self-healing launchd job (RunAtLoad + 60s idempotent re-invoke).
  // Best-effort; the overlay is a nicety and must never block boot.
  void ensureOverlayWatchInstalled()
    .then((r) => console.error(`[overlay-watch] launchd supervisor: ${r.ok ? "ok" : "skip"} (${r.detail})`))
    .catch((e) => console.error("[overlay-watch] supervisor install failed:", e?.message || e));
  // Daily self-updater: restored 2026-07-08 after 88bd1cb9 ("Remove autopilot
  // tool") silently dropped its only install path (it used to be bundled into
  // the deleted `autopilot enable` action). Best-effort; never blocks boot.
  // Disable with S4L_AUTO_UPDATE=0.
  void ensureUpdaterInstalled()
    .then((r) => console.error(`[self-update] launchd updater: ${r.ok ? "ok" : "skip"} (${r.detail})`))
    .catch((e) => console.error("[self-update] updater install failed:", e?.message || e));
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
  // Make S4L visible in the Cowork/Code tab, not just the Chat tab: register this
  // server into ~/.claude.json `mcpServers` so the embedded claude-code (launched
  // with --setting-sources=user) discovers it. Synchronous, idempotent, atomic,
  // best-effort; never blocks boot. See ensureCoworkMcpRegistered for the why.
  ensureCoworkMcpRegistered();
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error(`[social-autoposter-mcp] connected. v=${VERSION} repo=${repoDir()}`);
  // Eagerly start the loopback panel server so the Claude Code side panel (and any
  // reverse proxy in front of it) always has a backend to hit, without waiting for
  // a first `dashboard` call. Best-effort: a bind failure must never block boot.
  void startLocalPanel()
    .then((url) => console.error(`[social-autoposter-mcp] panel loopback ready at ${url}`))
    .catch((e) => console.error("[social-autoposter-mcp] panel loopback start failed:", e?.message || e));
  // NOTE (2026-07-17): the boot-time drainApprovedBacklog() call that lived
  // here is gone — backlog recovery is owned by the menubar's periodic
  // _resume_approved_queue (single drainer; see the removal note above).
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
  // Ship Claude session transcripts (scheduled queue-worker runs + s4l repo
  // sessions) to the Cloud Logging relay so a user's session can be
  // reconstructed remotely (the artifact that was missing for the 2026-07-03
  // Karol setup investigation). The script is incremental (per-file byte
  // offsets), self-locking, and scope-limited to s4l-related project dirs.
  // Best-effort; opt out with S4L_TRANSCRIPT_RELAY=0.
  if ((process.env.S4L_TRANSCRIPT_RELAY ?? "1") !== "0") {
    let transcriptRelayRunning = false;
    const relayTranscripts = () => {
      if (transcriptRelayRunning) return;
      transcriptRelayRunning = true;
      runPython("scripts/relay_session_transcripts.py", ["--max-lines", "600"], {
        timeoutMs: 120_000,
      })
        .catch((e: any) => {
          console.error("[social-autoposter-mcp] transcript relay failed:", e?.message || e);
        })
        .finally(() => {
          transcriptRelayRunning = false;
        });
    };
    const trBoot = setTimeout(relayTranscripts, 90_000); // off the boot hot path
    trBoot.unref();
    const tr = setInterval(relayTranscripts, 5 * 60_000);
    tr.unref();
  }
  // Relay the queue producer/consumer log (claude-queue/provider.log) so a
  // stranded/orphaned draft batch — a producer that died after its worker wrote
  // the result but before consuming it — is visible in Cloud Logging remotely,
  // not just on the box (context="queue-provider"). Incremental (byte offset),
  // self-locking, forward-only baseline. Best-effort; opt out S4L_PROVIDER_LOG_RELAY=0.
  if ((process.env.S4L_PROVIDER_LOG_RELAY ?? "1") !== "0") {
    let providerRelayRunning = false;
    const relayProviderLog = () => {
      if (providerRelayRunning) return;
      providerRelayRunning = true;
      runPython("scripts/relay_provider_log.py", ["--max-lines", "500"], {
        timeoutMs: 120_000,
      })
        .catch((e: any) => {
          console.error("[social-autoposter-mcp] provider-log relay failed:", e?.message || e);
        })
        .finally(() => {
          providerRelayRunning = false;
        });
    };
    const prBoot = setTimeout(relayProviderLog, 95_000); // off the boot hot path
    prBoot.unref();
    const pr = setInterval(relayProviderLog, 5 * 60_000);
    pr.unref();
  }
  // Sync the install's configuration state (config.json, persona corpus, mode,
  // queues, onboarding ledger) to the backend. Hash-gated on the interval, so
  // the recurring tick only POSTs when something actually changed; setup.ts
  // additionally fires it right after every config write.
  void sendStateSnapshot("startup");
  const ss = setInterval(() => void sendStateSnapshot("interval"), 15 * 60_000);
  ss.unref();

  // Voice-exemplar catch-up + periodic refresh, checked on every boot
  // (2026-07-16, extended from the original one-shot-for-legacy-installs
  // version): rescans the connected X profile and re-stores voice.examples +
  // the persona_corpus.txt exemplar section whenever
  // scripts/voice_exemplars.py's _needs_rescan() says so -- never scanned,
  // scanned by an older SCANNER_VERSION (so a scraper fix like the 2026-07-15
  // stall-detection bug reaches already-scanned installs on their next boot
  // instead of silently never re-running), or the last scan is more than
  // RESCAN_MAX_AGE_DAYS (14) old. Additive only (regenerates just its own
  // marked corpus section; respects hand-written examples). Each boot is
  // cheap when nothing needs it (stamped examples_scanned_at +
  // examples_scanner_version make it a no-op until either goes stale), and
  // when something DOES need it, it WAITS politely on the twitter-browser
  // lock, polling while holding nothing, until cycles/DM runs free the
  // browser, up to 12h before deferring to the next boot. Delayed so
  // boot-time work (runtime provision, kicker install) settles first.
  const backfill = setTimeout(() => {
    if (isPaused()) return;
    // timeout covers the 12h lock wait plus generous room for the scan itself
    void runPython("scripts/voice_exemplars.py", ["backfill"], { timeoutMs: 13 * 3600_000 })
      .then((r) => {
        const last = r.stdout.trim().split("\n").slice(-1)[0] || "";
        console.error(`[social-autoposter-mcp] voice-exemplars backfill: ${last}`);
      })
      .catch((e) => {
        console.error("[social-autoposter-mcp] voice-exemplars backfill failed:", e?.message || e);
      });
  }, 3 * 60_000);
  backfill.unref();
}

main().catch(async (err) => {
  console.error("[social-autoposter-mcp] fatal:", err);
  captureError(err, { component: "main" });
  await flushLogs();
  await flushSentry();
  process.exit(1);
});
