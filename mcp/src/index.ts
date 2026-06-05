#!/usr/bin/env node
// social-autoposter MCP server (X/Twitter rail).
//
// Core tools:
//   draft_cycle  - scan + draft, return all drafts as a numbered table for the
//                  user to review in chat (posts nothing).
//   post_drafts  - post the drafts the user chose by number from a batch.
//   autopilot    - one tool, action = enable | disable | status (launchd job).
//   get_stats    - read-only post + engagement stats.
//
// THIN wrapper. The pipeline brain (scan, score, drafting prompts, posting)
// stays in the Python/shell scripts; we only orchestrate and present.

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
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
  type Plan,
  type PlanCandidate,
} from "./repo.js";
import {
  applySetup,
  resolveProject,
  hasReadyProject,
  listManagedProjectStatus,
  REQUIRED_FIELDS,
  RECOMMENDED_FIELDS,
  configPath,
  type ProjectInput,
} from "./setup.js";
import { xStatus, xConnect, xDetectSources, summarizeXAuth } from "./twitterAuth.js";
import {
  startProvisioning,
  isProvisioning,
  readProgress,
  runtimeReady,
  readRuntime,
  resolvePython,
} from "./runtime.js";
import { VERSION, versionStatus, latestPublishedVersion } from "./version.js";
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

const TWITTER_AUTOPILOT_LABEL = "com.m13v.social-twitter-cycle";
const TWITTER_AUTOPILOT_PLIST = path.join(
  os.homedir(),
  "Library",
  "LaunchAgents",
  `${TWITTER_AUTOPILOT_LABEL}.plist`
);

// Daily self-updater. Enabled alongside autopilot so a hands-free (headless)
// install keeps itself current — the interactive `version` tool only helps when
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

function plistXml(opts: {
  label: string;
  programArgs: string[];
  intervalSecs: number;
  runAtLoad: boolean;
  stdoutLog: string;
  stderrLog: string;
}): string {
  const args = opts.programArgs.map((a) => `\t\t<string>${a}</string>`).join("\n");
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
\t\t<string>${LAUNCHD_PATH}</string>
\t\t<key>HOME</key>
\t\t<string>${os.homedir()}</string>
\t\t<key>SAPS_REPO_DIR</key>
\t\t<string>${repoDir()}</string>
\t\t<key>SAPS_PYTHON</key>
\t\t<string>${resolvePython()}</string>
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
    name: "social-autoposter",
    version: VERSION,
  },
  {
    // Host injects this into the agent's system prompt, so be proactive about
    // WHEN this tool earns its keep, not just what it does.
    instructions:
      "social-autoposter drafts, reviews, and posts X/Twitter replies for the user's product, and " +
      "can run them hands-free on autopilot.\n\n" +
      "BE PROACTIVE ABOUT MARKETING MOMENTS. Whenever the user ships, finishes, merges, or releases " +
      "something worth talking about in this session (a new feature, a launch, a long-awaited fix, a " +
      "milestone), don't wait to be asked: offer to tell the world by running `draft_cycle` to post " +
      "about it. A feature nobody hears about is wasted distribution. Use judgment, suggest it for " +
      "genuinely shareable moments, not routine refactors, chores, or internal cleanup.\n\n" +
      "STAY CURRENT. At the start of a session, and again right after you help the user ship/merge/" +
      "release, call the `version` tool (action:'status'). If `update_available` is true, tell the " +
      "user and offer to run `version` with action:'update'. The `setup` tool's status also surfaces " +
      "`update_available` and an `update_hint`.\n\n" +
      "TYPICAL FLOW: `setup` (configure the project once) -> `draft_cycle` (scan + review a batch; the " +
      "user approves / edits / skips every draft in a single form) -> `autopilot` (enable to also turn " +
      "on hands-free background posting AND daily auto-updates) -> `get_stats` (see performance). Run " +
      "`setup` first; the other tools refuse until a project is fully configured.\n\n" +
      "RENDER THE DASHBOARD AFTER ACTIONS. After any state-changing or results-producing tool call " +
      "(`draft_cycle`, `post_drafts`, `autopilot` enable/disable, `get_stats`), end your turn by " +
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
const tool: typeof server.registerTool = ((name: string, config: any, cb: ToolHandler) => {
  TOOL_HANDLERS[name] = cb;
  return (baseRegisterTool as any)(name, config, cb);
}) as any;
const appTool = ((name: string, config: any, cb: ToolHandler) => {
  TOOL_HANDLERS[name] = cb;
  return registerAppTool(server as any, name, config as any, cb as any);
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
        "then `/login` inside it (or run `claude setup-token`). Once it's logged in, run draft_cycle again."
      );
    case "monthly_limit":
    case "daily_limit":
    case "rate_limit_5h":
      return (
        `The drafting step hit an Anthropic usage limit (${reason}), so no replies were drafted. ` +
        "Wait for the limit to reset, then run draft_cycle again."
      );
    case "no_search_topics":
      return (
        "This project has no search topics yet, so there was nothing to scan. Topics live in the " +
        "DB (project_search_topics) and are seeded from your project's `search_topics` during setup. " +
        "Re-run the `setup` tool for this project with a `search_topics` list (comma-separated keywords/" +
        "phrases your buyers tweet about); setup seeds them automatically, then run draft_cycle again."
      );
    case "topics_api_unreachable":
      return (
        "Couldn't reach the search-topics service to load this project's topics, so the cycle stopped " +
        "before scanning. This is usually a transient backend/network issue. Try draft_cycle again in a " +
        "moment; if it persists, check connectivity to the autoposter backend."
      );
    case "credit_balance":
      return (
        "The drafting step failed because the Anthropic account is out of credits. " +
        "Add credits, then run draft_cycle again."
      );
    default:
      return (
        `The drafting step failed (${reason}) and produced no drafts. ` +
        "Check skill/logs/twitter-cycle-*.log on this machine for details, then run draft_cycle again."
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
    // Interactive draft_cycle: launch the harness Chrome ON-SCREEN so the user
    // can watch the scan/scrape happen live. Cron/autopilot do NOT set these, so
    // background runs keep the off-screen default in twitter-backend.sh and don't
    // hijack the screen. (Only affects a fresh Chrome launch; an already-running
    // harness window keeps its current position.)
    BH_WINDOW_POS: "60,60",
    BH_WINDOW_SIZE: "1280,900",
  };
  if (project) env.SAPS_FORCE_PROJECT = project;
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
  if (marker && marker[1]) return { batchId: marker[1] };
  // A real prep-step failure (e.g. the background claude CLI isn't logged in)
  // emits DRAFT_ONLY_BLOCKED=<reason>. Surface that instead of silently falling
  // back to a stale/empty batch and mis-reporting "no fresh candidates".
  const blockedMarker = /DRAFT_ONLY_BLOCKED=([a-z0-9_]+)/.exec(
    res.stdout + "\n" + res.stderr
  );
  if (blockedMarker && blockedMarker[1]) {
    return { batchId: null, blocked: blockedReasonMessage(blockedMarker[1]) };
  }
  // No `DRAFT_ONLY_PLAN=` marker from THIS run => this run produced no drafts.
  // We MUST NOT fall back to the newest plan file on disk (`latestBatchId()`):
  // that's a *previous* run's batch, so a 5-second empty cycle would echo an old
  // 7-draft batch and report phantom success. Report 0 drafts honestly, with the
  // pipeline's own reason (e.g. cold-start project with no seeded queries).
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
    .map((c, i) => {
      const n = i + 1;
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

async function postApproved(batchId: string, plan: Plan) {
  const approved = (plan.candidates || []).filter((c: PlanCandidate) => c.approved === true);
  if (approved.length === 0) return { attempted: 0, exit_code: 0, summary: "nothing approved" };
  const approvedBatch = `${batchId}_approved`;
  writePlan(approvedBatch, { ...plan, candidates: approved });
  // SAPS_SKIP_CAMPAIGN_SUFFIX=1: manual/reviewed posts from this MCP draft_cycle
  // never get the active-campaign suffix (e.g. " written with ai") appended.
  // twitter_browser.py's reply handler reads this env (inherited through
  // twitter_post_plan.py's subprocess). The cron pipeline doesn't set it, so the
  // A/B disclosure experiment keeps running on autopilot/cron and on Reddit.
  const res = await runPython(
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
        // The poster attaches to the twitter-harness Chrome over CDP. The cron
        // pipeline exports this from skill/lib/twitter-backend.sh; the MCP path
        // must set it explicitly or twitter_browser.py fails with "No twitter-
        // harness Chrome reachable". Honor an inherited value (AppMaker / VM
        // BYO-Chrome), else default to the local harness on port 9555.
        TWITTER_CDP_URL: process.env.TWITTER_CDP_URL || "http://127.0.0.1:9555",
      },
    }
  );
  let summary: unknown = res.stdout.trim();
  try {
    const lines = res.stdout.trim().split("\n");
    summary = JSON.parse(lines[lines.length - 1]);
  } catch {
    /* keep raw */
  }
  return {
    attempted: approved.length,
    exit_code: res.code,
    summary,
    stderr_tail: res.stderr.split("\n").slice(-8).join("\n"),
  };
}

// ---- getting-started: discoverable front door (USER-invoked, no side effects)
// This is NOT a tool — the model never auto-calls it. It surfaces in clients
// that render prompts as slash-commands / starters (e.g. Claude Desktop's "/"
// menu). When the user picks it, it injects the message below into the chat,
// which nudges the agent to start the real onboarding via the `setup` tool.
// Deliberately a DUMB POINTER: it names no fields and no steps, so it can never
// drift from REQUIRED_FIELDS / the setup tool's flow. All real logic stays in
// `setup`; this is just a convenience handle to begin.
server.registerPrompt(
  "getting-started",
  {
    title: "Set up social-autoposter",
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
            "I just installed social-autoposter and want to get set up. Call the `setup` tool " +
            "(status mode) to see what's still needed, then walk me through it conversationally — " +
            "ask me about my product and connect my X/Twitter account, one thing at a time. " +
            "Don't dump a form at me, and explain the X connection before doing it.",
        },
      },
    ],
  })
);

// ---- setup: per-project config (the "brain": project, website, voice) -----
// Run this FIRST. The action tools refuse until at least one project is ready.
// You can set up MULTIPLE products and fill each project's fields INCREMENTALLY
// across several calls — readiness is derived from config.json, never a stored
// flag. Call with status:true (or just no name) to list every project this
// install manages and what each still needs.
tool(
  "setup",
  {
    title: "Set up a project",
    description:
      "Run this FIRST, before any drafting or autopilot. Two jobs:\n" +
      "1) Configure a project this install posts for: its website, what it does (description), who " +
      "to target (icp), and brand voice. Set up MULTIPLE products (call once per product, identified " +
      "by name); fill a project's fields INCREMENTALLY across several calls — pass whatever you have, " +
      "it merges and tells you what's still missing.\n" +
      "2) Connect X/Twitter (action:'connect_x'): the autoposter posts through its OWN managed Chrome, " +
      "which needs your logged-in x.com session. This imports x.com/twitter.com cookies from your " +
      "everyday browser (Chrome/Arc/Brave/Edge, auto-detected) into that browser — nothing else is " +
      "touched. ALWAYS explain what will happen and get the user's OK first: call with action:'connect_x' " +
      "(no confirm) to get the explanation, relay it, then call again with action:'connect_x', confirm:true.\n" +
      "Call with status:true (or no name) to list every configured project, its remaining fields, AND " +
      "whether X is connected. Ask the user conversationally; don't dump a form. The draft_cycle, " +
      "autopilot, and get_stats tools refuse to run until a project is fully set up.",
    inputSchema: {
      status: z.boolean().optional(),
      action: z
        .enum(["connect_x", "detect_x_sources", "reseed_queries"])
        .optional()
        .describe(
          "connect_x = import/validate your X session in the autoposter's managed browser. " +
            "Without confirm:true it only EXPLAINS what it will do (so you can tell the user first). " +
            "detect_x_sources = list the browsers/profiles the X session can be imported from " +
            "(read-only, no keychain prompt) so the user can pick the right one; returns " +
            "{sources:[{spec,label,x_session}], recommended}. " +
            "reseed_queries = (re-)expand an EXISTING project's search topics into ~30 real X search " +
            "queries and return them. Use for projects configured before query-expansion existed, or " +
            "to refresh the query bank. Requires name. Returns the resulting query list."
        ),
      confirm: z
        .boolean()
        .optional()
        .describe("Set true with action:'connect_x' to actually run the import after the user has agreed."),
      x_source: z
        .string()
        .optional()
        .describe(
          "Optional browser profile to import the X session from, e.g. 'arc:Default', 'chrome:Profile 1'. " +
            "Default: auto-detect chrome/arc/brave/edge."
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
      get_started_link: z
        .string()
        .optional()
        .describe("Primary call-to-action link (signup / get started)"),
      content_guardrails: z
        .string()
        .optional()
        .describe("Anything the posts must avoid saying / claiming"),
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
    // Explain-then-confirm: the first call (no confirm) describes exactly what
    // will happen so the agent can get the user's OK; confirm:true runs it.
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
          how_to_proceed:
            "Tell the user the above in your own words and ask if that's OK. If yes, call setup again with " +
            "action:'connect_x', confirm:true (optionally x_source:'arc:Default' etc. if they use a non-Chrome browser).",
        });
      }
      const r = await xConnect(args.x_source);
      return jsonContent({
        action: "connect_x",
        connected: r.connected,
        state: r.state,
        source: r.source,
        summary: summarizeXAuth(r),
        note: r.note,
        attempts: r.attempts,
        next_step: r.connected
          ? "X is connected. You can run draft_cycle (and enable autopilot) once a project is fully set up."
          : r.state === "needs_login"
            ? "Ask the user to sign in to x.com in the Chrome window that just opened, then call setup " +
              "action:'connect_x', confirm:true again to confirm."
            : "X is not connected yet. " + summarizeXAuth(r),
      });
    }

    // ---- Reseed search queries: (re-)expand an existing project's topics ----
    // For projects configured before query-expansion shipped (their topics live
    // in the DB but the seed-query bank is empty, so the cycle cold-starts on one
    // crude query), or to refresh the bank on demand. Idempotent: the seeder
    // dedups against what's already there. Returns the resulting queries so the
    // agent can show the user exactly what the cycle will fan out over.
    if (args.action === "reseed_queries") {
      if (!args.name) {
        return jsonContent({
          action: "reseed_queries",
          error: "name is required — tell me which configured project to reseed.",
        });
      }
      try {
        const qseed = await runPython(
          "scripts/seed_search_queries.py",
          ["--project", args.name, "--supply-test", "auto", "--emit-json"],
          { timeoutMs: 600_000 }
        );
        const qm = /seeded=(\d+)\s+inserted=(\d+)\s+updated=(\d+)/.exec(qseed.stdout);
        let queries: Array<{ query: string; topic: string }> = [];
        const jm = qseed.stdout.split("===QUERIES_JSON===")[1];
        if (jm) {
          try {
            queries = (JSON.parse(jm.trim()).queries ?? []) as typeof queries;
          } catch {
            /* leave queries empty; count note below still informs the user */
          }
        }
        if (qseed.code !== 0 && queries.length === 0) {
          const qtail =
            (qseed.stderr || qseed.stdout).trim().split("\n").slice(-1)[0] ||
            "unknown error";
          return jsonContent({
            action: "reseed_queries",
            project: args.name,
            ok: false,
            error: qtail,
            note:
              `Couldn't expand search queries for '${args.name}' — ${qtail}. ` +
              `Common causes: the project has no search topics seeded yet (run setup with name='${args.name}' ` +
              `and its search_topics first), or X/Claude wasn't reachable. The cycle still runs off the seeded topics.`,
          });
        }
        return jsonContent({
          action: "reseed_queries",
          project: args.name,
          ok: true,
          seeded: qm ? Number(qm[1]) : queries.length,
          inserted: qm ? Number(qm[2]) : undefined,
          updated: qm ? Number(qm[3]) : undefined,
          query_count: queries.length,
          queries,
          note:
            `Expanded '${args.name}' into ${queries.length} active search quer` +
            `${queries.length === 1 ? "y" : "ies"}. The cycle now fans out over these instead of ` +
            `running a single query. Show the user the list so they can confirm they look on-target; ` +
            `re-run this anytime to refresh, or run draft_cycle to use them.`,
        });
      } catch (e) {
        return jsonContent({
          action: "reseed_queries",
          project: args.name,
          ok: false,
          error: (e as Error).message,
        });
      }
    }

    // Status / discovery mode: no project name supplied, or explicitly asked.
    if (args.status === true || !args.name) {
      const projects = listManagedProjectStatus();
      const x = await xStatus();
      const ver = await versionStatus();
      return jsonContent({
        configured: projects.some((p) => p.ready),
        projects,
        x_connected: x.connected,
        x_state: x.state,
        x_handle: x.handle ?? null,
        mcp_version: ver.installed,
        latest_version: ver.latest,
        update_available: ver.update_available,
        update_hint: ver.update_available
          ? `A newer version (${ver.latest}) is available — you're on ${ver.installed}. ` +
            `Tell the user and offer to run the \`version\` tool with action:'update' ` +
            `(or \`npx social-autoposter@latest update\`).`
          : undefined,
        required_fields: REQUIRED_FIELDS,
        recommended_fields: RECOMMENDED_FIELDS,
        config_path: configPath(),
        next_step:
          projects.length === 0
            ? "No projects yet. Ask the user about a product (website, what it does, who to target, " +
              "brand voice), then call setup with a short name plus those fields. Repeat per product." +
              (x.connected ? "" : " X is not connected yet either — also run setup action:'connect_x'.")
            : projects.every((p) => p.ready)
              ? (x.connected
                  ? "All configured projects are ready and X is connected. Call setup again with a new name " +
                    "to add another product, or run draft_cycle to post."
                  : "All configured projects are ready, but X is NOT connected — posting needs a logged-in " +
                    "x.com session. Run setup action:'connect_x' to import it from the user's browser.")
              : "Some projects are missing required fields (see each project's missing_required). Ask the " +
                "user for those and call setup again with that project's name plus the missing fields." +
                (x.connected ? "" : " X is also not connected yet (run setup action:'connect_x')."),
      });
    }

    // Apply mode (incremental): merge whatever fields were supplied onto the
    // named project, then report whether it's now ready or still missing fields.
    try {
      const result = applySetup(args as ProjectInput);
      // Seed this project's search_topics into the DB universe the cycle reads
      // (project_search_topics). Without this a freshly-configured project has
      // topics in config.json but ZERO rows in the DB, so draft_cycle's topic
      // picker raises and the cycle silently returns nothing. Best-effort: a
      // seed hiccup never fails setup — the cycle's fail-loud path still tells
      // the user if topics are missing. Only runs once the project is ready
      // (i.e. it actually has search_topics to seed). (2026-06-02)
      let seedNote = "";
      let searchQueries: Array<{ query: string; topic: string }> = [];
      if (result.ready) {
        const seed = await runPython(
          "scripts/seed_search_topics.py",
          ["--project", result.project],
          { timeoutMs: 60_000 }
        );
        if (seed.code === 0) {
          const m = /planned=(\d+)\s+inserted=(\d+)\s+updated=(\d+)/.exec(seed.stdout);
          seedNote = m
            ? ` Seeded ${m[1]} search topic(s) into the DB (new: ${m[2]}, updated: ${m[3]}), so draft_cycle has a topic universe to work with.`
            : " Seeded search topics into the DB so draft_cycle has a topic universe to work with.";
        } else {
          const tail = (seed.stderr || seed.stdout).trim().split("\n").slice(-1)[0] || "unknown error";
          seedNote = ` (Heads up: couldn't seed search topics into the DB yet — ${tail}. draft_cycle will tell you clearly if topics are missing.)`;
        }

        // Cold-start QUERY supply: fan the seeded topics out into >=30 real X
        // search queries (project_search_queries) so the deterministic Phase 1
        // bank (qualified_query_bank.py) has something to run on day one.
        // Without this, a freshly-configured project's bank is empty and the
        // cycle falls back to ONE crude topic-as-query. Best-effort: a failure
        // here never fails setup; the topic-as-query fallback still works, just
        // narrower. Supply-test is auto (only if the harness is up), so this
        // stays fast when X isn't connected yet. (2026-06-04)
        if (seed.code === 0) {
          try {
            const qseed = await runPython(
              "scripts/seed_search_queries.py",
              ["--project", result.project, "--supply-test", "auto", "--emit-json"],
              { timeoutMs: 600_000 }
            );
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
              seedNote += ` Expanded them into ${n} search quer${n === 1 ? "y" : "ies"} so the cycle can fan out instead of running a single query.`;
            } else if (qseed.code !== 0) {
              const qtail = (qseed.stderr || qseed.stdout).trim().split("\n").slice(-1)[0] || "unknown error";
              seedNote += ` (Search queries not expanded yet — ${qtail}. The cycle still runs off the seeded topics.)`;
            }
          } catch (e) {
            seedNote += ` (Search-query expansion skipped — ${(e as Error).message}.)`;
          }
        }
      }
      return jsonContent({
        ok: true,
        project: result.project,
        action: result.created ? "created" : "updated",
        ready: result.ready,
        missing_required: result.missing_required,
        topics_seeded: result.ready,
        search_queries: searchQueries,
        config_path: configPath(),
        note: result.ready
          ? `Project '${result.project}' is fully set up.${seedNote} Next: connect X so the autoposter can post — ` +
            `call setup with action:'connect_x' (it explains itself, then run again with confirm:true). ` +
            `Once X is connected you can run draft_cycle, autopilot, and get_stats.`
          : `Saved what you provided for '${result.project}'. Still need: ${result.missing_required.join(", ")}. ` +
            `Ask the user for those and call setup again with name='${result.project}' and the missing fields.`,
      });
    } catch (e) {
      return textContent(`Setup failed: ${(e as Error).message}`);
    }
  }
);

// ---- draft_cycle: scan + draft, then hand the batch to the user for review.
// Posting is a SEPARATE step (post_drafts) so the user picks by number in chat.
// This host doesn't support elicitation, so there is no in-tool form: the model
// relays the table and asks which to post / edit, then calls post_drafts.
tool(
  "draft_cycle",
  {
    title: "Draft an X reply cycle",
    description:
      "Scan X and draft replies on this machine, then return ALL drafts as a numbered table " +
      "for review. This tool POSTS NOTHING. Show the table to the user and ask which numbers " +
      "to post and which to rewrite, then call `post_drafts` with their decision and the " +
      "returned batch_id. The table MUST show, per draft: the thread being replied to " +
      "(thread_text), the draft reply, and the link target (link_url) when present; never " +
      "drop those columns. Flow: discover -> draft -> review in chat -> post_drafts. " +
      "After returning the table, call the `dashboard` tool so the user sees the updated state.",
    inputSchema: {
      project: z
        .string()
        .optional()
        .describe("Which configured project to draft for. Optional when only one project is set up; required when several are."),
    },
  },
  async ({ project }, extra) => {
    const r = resolveProject(project);
    if (!r.ok) return textContent(r.message!);
    const proj = r.project!;

    // Live progress so the chat doesn't sit on a frozen spinner for minutes.
    // Two channels, both best-effort (a sink failure must never fail the cycle):
    //   1. notifications/message — a log line; the host records it (and some
    //      clients show it in a log view). Works with no client opt-in.
    //   2. notifications/progress — drives the status text under the running
    //      tool. Only valid when the client supplied a progressToken on the
    //      request, so it's guarded on that.
    const progressToken = extra?._meta?.progressToken;
    const sendProgress = async (message: string, step: number) => {
      try {
        await extra.sendNotification({
          method: "notifications/message",
          params: { level: "info", logger: "draft_cycle", data: message },
        });
      } catch {
        /* ignore */
      }
      if (progressToken !== undefined) {
        try {
          await extra.sendNotification({
            method: "notifications/progress",
            params: { progressToken, progress: step, message },
          });
        } catch {
          /* ignore */
        }
      }
    };

    const drafted = await produceDrafts(proj, (message, step) => {
      void sendProgress(message, step);
    });
    if (drafted.blocked || !drafted.batchId) {
      return textContent(drafted.blocked ?? "No drafts produced.");
    }
    const plan = readPlan(drafted.batchId);
    if (!plan || !(plan.candidates && plan.candidates.length)) {
      return textContent(`No drafts in batch ${drafted.batchId}.`);
    }
    const count = plan.candidates.length;
    const table = renderDraftsTable(plan);
    const message =
      `Drafted ${count} ${count === 1 ? "reply" : "replies"} for "${proj}" ` +
      `(batch ${drafted.batchId}). NOTHING has been posted yet.\n\n` +
      `${table}\n\n` +
      `Show this list to the user and ask which to post and which to edit. When you render ` +
      `the table, ALWAYS include, for every draft: the thread it replies to (in reply to / ` +
      `thread_text), the draft text, and the link target (link_url) if present. Do NOT drop ` +
      `the thread or link columns. Note the literal short link is appended at post time and ` +
      `an A/B gate may omit it, so present link_url as the target, not a guaranteed final URL. ` +
      `They can reply however is natural, e.g. "post 1, 3 and 5", "edit 2: <new wording>", ` +
      `"post all", or "skip all". Editing a draft also posts it. Then call the post_drafts ` +
      `tool with batch_id "${drafted.batchId}" and their decision (post: [numbers], ` +
      `edits: [{n, text}], or post_all: true). Do not post anything the user didn't ask for.`;
    return {
      content: [{ type: "text" as const, text: message }],
      structuredContent: {
        batch_id: drafted.batchId,
        drafted: count,
        status: "awaiting_decision",
        // Include the actual draft text here, not just a count. Some hosts
        // (e.g. Claude Desktop) surface ONLY structuredContent to the model and
        // drop the human-readable `content` table — which left the agent saying
        // "drafted: 2" with no way to show the drafts. Carrying the drafts in
        // structuredContent makes them available regardless of host behavior.
        drafts: (plan.candidates || []).map((c: PlanCandidate, i: number) => ({
          n: i + 1,
          author: c.thread_author,
          tweet_url: c.candidate_url,
          // The original tweet being replied to — reviewer context. Hosts that
          // surface ONLY structuredContent (Claude Desktop, Fazm Code tab) need
          // this here or the relayed table loses the thread it's responding to.
          thread_text: c.thread_text,
          reply_text: c.reply_text,
          // Target link only. The literal /r/<code> short link is minted at post
          // time and an A/B gate may omit it; do not pre-mint here.
          link_url: c.link_url,
          style: c.engagement_style,
          language: c.language,
        })),
      },
    };
  }
);

// ---- post_drafts: post the user's chosen drafts from a batch ---------------
// Second half of the manual loop. The user reviewed the table from draft_cycle
// and said which numbers to post / edit; this posts exactly those. Editing a
// draft implies posting it. Indices are 1-based, matching the table.
tool(
  "post_drafts",
  {
    title: "Post chosen drafts",
    description:
      "Post the drafts the user approved from a draft_cycle batch. Pass the batch_id from " +
      "draft_cycle and the user's decision by NUMBER (1-based, matching the table): `post` is " +
      "the list of draft numbers to post as drafted; `edits` rewrites a draft's text before " +
      "posting it (editing implies posting); `post_all` posts every draft. Only the chosen " +
      "drafts post; anything not listed is left unposted. Call this ONLY after the user has " +
      "told you which drafts they want. After posting, call the `dashboard` tool so the user " +
      "sees the updated state.",
    inputSchema: {
      batch_id: z.string().describe("The batch_id returned by draft_cycle."),
      post: z
        .array(z.number().int().positive())
        .optional()
        .describe("1-based draft numbers to post as drafted, e.g. [1, 3, 5]."),
      edits: z
        .array(z.object({ n: z.number().int().positive(), text: z.string() }))
        .optional()
        .describe("Rewrites: each {n, text} replaces draft n's wording, then posts it."),
      post_all: z.boolean().optional().describe("Post every draft in the batch."),
    },
  },
  async ({ batch_id, post, edits, post_all }) => {
    const plan = readPlan(batch_id);
    if (!plan || !(plan.candidates && plan.candidates.length)) {
      return textContent(
        `No drafts found for batch ${batch_id}. Run draft_cycle again to produce a fresh batch.`
      );
    }
    const candidates = plan.candidates;
    const total = candidates.length;
    const warnings: string[] = [];
    const inRange = (n: number) => n >= 1 && n <= total;

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

    candidates.forEach((c, i) => (c.approved = approve.has(i + 1)));
    writePlan(batch_id, plan);

    if (approve.size === 0) {
      return jsonContent({
        batch_id,
        drafted: total,
        posted: 0,
        skipped: total,
        edited: editedCount,
        note: "No drafts selected to post. Nothing was posted.",
        warnings,
      });
    }

    const result = await postApproved(batch_id, plan);
    return jsonContent({
      batch_id,
      drafted: total,
      posted: approve.size,
      skipped: total - approve.size,
      edited: editedCount,
      result,
      warnings,
    });
  }
);

// ---- autopilot: one tool, three actions -----------------------------------
tool(
  "autopilot",
  {
    title: "X autopilot",
    description:
      "Control background X/Twitter posting. action=enable loads the launchd job so the " +
      "cycle fires automatically; action=disable unloads it (manual draft_cycle still works); " +
      "action=status reports whether it is loaded. After enable/disable, call the `dashboard` " +
      "tool so the user sees the updated autopilot state.",
    inputSchema: {
      action: z.enum(["enable", "disable", "status"]),
    },
  },
  async ({ action }) => {
    if (action !== "status" && !hasReadyProject()) {
      return textContent(
        "No project is fully set up yet, so autopilot has nothing to post. Run the `setup` tool " +
          "first. Note: autopilot runs the background cycle across all configured projects; it is " +
          "not scoped to one project."
      );
    }
    const uid = process.getuid ? process.getuid() : 0;
    const logDir = path.join(repoDir(), "skill", "logs");

    if (action === "status") {
      const res = await run("launchctl", ["list"], { timeoutMs: 10_000 });
      const lines = res.stdout.split("\n");
      const loaded = lines.some((l) => l.includes(TWITTER_AUTOPILOT_LABEL));
      const updaterLoaded = lines.some((l) => l.includes(UPDATER_LABEL));
      return jsonContent({
        label: TWITTER_AUTOPILOT_LABEL,
        loaded,
        auto_update_label: UPDATER_LABEL,
        auto_update_loaded: updaterLoaded,
      });
    }

    if (action === "enable") {
      // 1) Cycle plist. Write one pointing at the self-update guard ONLY if no
      //    plist exists yet; never overwrite a hand-tuned/dev plist.
      const createdCycle = ensurePlist(
        TWITTER_AUTOPILOT_PLIST,
        plistXml({
          label: TWITTER_AUTOPILOT_LABEL,
          programArgs: ["/bin/bash", path.join(repoDir(), "skill", "run-cycle-update-guard.sh")],
          intervalSecs: 60,
          runAtLoad: false,
          stdoutLog: path.join(logDir, "launchd-twitter-cycle-stdout.log"),
          stderrLog: path.join(logDir, "launchd-twitter-cycle-stderr.log"),
        })
      );
      const cycleRes = await loadPlist(TWITTER_AUTOPILOT_LABEL, TWITTER_AUTOPILOT_PLIST, uid);

      // 2) Daily self-updater. Keeps a headless install current with no human
      //    in the loop. RunAtLoad so it also checks shortly after enable.
      const createdUpdater = ensurePlist(
        UPDATER_PLIST,
        plistXml({
          label: UPDATER_LABEL,
          programArgs: ["/bin/bash", path.join(repoDir(), "skill", "social-autoposter-update.sh")],
          intervalSecs: 86_400,
          runAtLoad: true,
          stdoutLog: path.join(logDir, "launchd-self-update-stdout.log"),
          stderrLog: path.join(logDir, "launchd-self-update-stderr.log"),
        })
      );
      const updaterRes = await loadPlist(UPDATER_LABEL, UPDATER_PLIST, uid);

      return jsonContent({
        action: "enable",
        autopilot: {
          loaded: cycleRes.code === 0,
          plist: TWITTER_AUTOPILOT_PLIST,
          created: createdCycle,
          error: cycleRes.code === 0 ? null : (cycleRes.stderr || cycleRes.stdout).trim(),
        },
        auto_update: {
          loaded: updaterRes.code === 0,
          plist: UPDATER_PLIST,
          created: createdUpdater,
          note:
            "Daily updater enabled. It self-updates real npm installs and is a no-op on dev/source " +
            "checkouts (refuses to clobber a .git working tree).",
          error: updaterRes.code === 0 ? null : (updaterRes.stderr || updaterRes.stdout).trim(),
        },
      });
    }

    // disable — unload both jobs (leave the plist files in place for re-enable)
    const cycleOff = await unloadPlist(TWITTER_AUTOPILOT_LABEL, TWITTER_AUTOPILOT_PLIST, uid);
    const updaterOff = await unloadPlist(UPDATER_LABEL, UPDATER_PLIST, uid);
    return jsonContent({
      action: "disable",
      autopilot_unloaded: cycleOff.code === 0,
      auto_update_unloaded: updaterOff.code === 0,
      note:
        cycleOff.code === 0
          ? "Autopilot and daily auto-update unloaded. Manual draft_cycle still works."
          : `Autopilot disable reported exit ${cycleOff.code}: ${(cycleOff.stderr || cycleOff.stdout).trim()}`,
    });
  }
);

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
tool(
  "version",
  {
    title: "Version & updates",
    description:
      "Report the installed social-autoposter version and check npm for a newer release. " +
      "action:'status' (default) shows installed vs latest published and whether an update is " +
      "available. action:'update' pulls and installs the latest release (runs " +
      "`npx social-autoposter@latest update`); the new MCP code takes effect after the client " +
      "reconnects / restarts (this running process keeps the old code until then). Use this when " +
      "the user asks what version they're on, or to push the latest update to their machine.",
    inputSchema: {
      action: z.enum(["status", "update"]).optional(),
    },
  },
  async ({ action }) => {
    if (action === "update") {
      // Pull + install the latest published release. This overwrites mcp/dist/
      // (including this running file — safe; the loaded process keeps old code)
      // and re-runs install.mjs to re-register the client config. npx is run
      // non-interactively so it can't stall on a confirm prompt.
      const before = VERSION;
      const res = await run("npx", ["-y", "social-autoposter@latest", "update"], {
        timeoutMs: 600_000,
      });
      // Bust the latest-version cache so the post-update number is fresh.
      const latest = await latestPublishedVersion();
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
);

// ---- runtime installer ----------------------------------------------------
// The pipeline runs Python locally. Rather than depend on the user's system
// Python (the #1 source of install failures), the first run provisions a fully
// OWNED uv runtime: standalone CPython + owned venv + deps + Chromium. These two
// tools drive it. They are plain (non-UI) tools so EVERY host can install — the
// panel's Install card is just a skin that calls install_runtime then polls
// install_status. See runtime.ts for the provisioning + progress contract.

function runtimeSnapshot() {
  const rt = readRuntime();
  const progress = readProgress();
  return {
    runtime_ready: runtimeReady(),
    provisioning: isProvisioning(),
    python: rt?.python ?? null,
    python_version: rt?.python_version ?? null,
    progress: progress ?? null,
  };
}

tool(
  "install_runtime",
  {
    title: "Install the Python runtime",
    description:
      "One-time setup that provisions the self-contained runtime the autoposter needs: a private " +
      "Python (via uv, not your system Python), its dependencies, and the Chromium browser. Runs in " +
      "the background and returns immediately; poll `install_status` for progress. Safe to call " +
      "repeatedly; it resumes/repairs and is a no-op once everything is installed. Use this the " +
      "first time the user sets up, or if other tools report the runtime isn't ready.",
    inputSchema: {},
  },
  async () => {
    if (runtimeReady()) {
      return jsonContent({ already_installed: true, ...runtimeSnapshot() });
    }
    const progress = startProvisioning();
    return jsonContent({
      started: true,
      runtime_ready: false,
      note: "Runtime install started. Poll install_status every ~1.5s for progress.",
      progress,
    });
  }
);

tool(
  "install_status",
  {
    title: "Runtime install status",
    description:
      "Report whether the self-contained Python/Chromium runtime is installed and, if an install is " +
      "in progress, the per-step progress (uv, Python, venv, dependencies, Chromium). Poll this after " +
      "install_runtime to follow the install to completion.",
    inputSchema: {},
  },
  async () => jsonContent(runtimeSnapshot())
);

// ---- config: read / edit the raw config.json ------------------------------
// The panel renders the full config and lets the user edit it. Writing is
// guarded: the new content must parse as JSON, and we always drop a timestamped
// backup next to config.json before overwriting, so a bad paste is recoverable.
tool(
  "config",
  {
    title: "View or edit config.json",
    description:
      "Read or update the autoposter's config.json (the source of truth for every project, the X/" +
      "Reddit/LinkedIn account handles, topics, and exclusions). action:'get' (default) returns the " +
      "full raw JSON; action:'save' validates the supplied `content` as JSON, writes a timestamped " +
      "backup, then overwrites config.json. Use when the user asks to see, edit, or fix their config.",
    inputSchema: {
      action: z.enum(["get", "save"]).optional(),
      content: z.string().optional(),
    },
  },
  async (args: { action?: "get" | "save"; content?: string }) => {
    const action = args.action || "get";
    const cfgPath = configPath();
    if (action === "get") {
      try {
        const content = fs.readFileSync(cfgPath, "utf-8");
        return jsonContent({ ok: true, path: cfgPath, bytes: content.length, content });
      } catch (e: any) {
        return jsonContent({ ok: false, path: cfgPath, error: String(e?.message || e) });
      }
    }
    // save
    const content = args.content;
    if (typeof content !== "string" || content.trim() === "") {
      return jsonContent({ ok: false, error: "Nothing to save: `content` was empty." });
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(content);
    } catch (e: any) {
      // Don't write a config that won't parse — every pipeline reads this file.
      return jsonContent({ ok: false, error: "Invalid JSON, not saved: " + String(e?.message || e) });
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return jsonContent({ ok: false, error: "Top level of config.json must be a JSON object." });
    }
    try {
      const stamp = new Date().toISOString().replace(/[:.]/g, "-");
      const backup = `${cfgPath}.bak-panel-${stamp}`;
      try {
        fs.copyFileSync(cfgPath, backup);
      } catch {
        /* first-write / missing original is non-fatal */
      }
      // Re-serialize the parsed object so what lands on disk is canonical,
      // 2-space-indented JSON with a trailing newline (matches the Python
      // writers), regardless of how the user formatted their paste.
      const out = JSON.stringify(parsed, null, 2) + "\n";
      fs.writeFileSync(cfgPath, out, "utf-8");
      return jsonContent({ ok: true, path: cfgPath, bytes: out.length, backup });
    } catch (e: any) {
      return jsonContent({ ok: false, error: "Write failed: " + String(e?.message || e) });
    }
  }
);

// ---- panel: MCP Apps control surface --------------------------------------
// A self-contained HTML view rendered by hosts that support MCP Apps (Claude
// desktop/web, etc.). It duplicates NO pipeline logic: each button calls one of
// the tools above (draft_cycle / autopilot / setup / get_stats) through the host
// and re-reads status. The tool itself returns the first-paint snapshot so the
// view has data the instant it loads.

// Is either launchd job (cycle / daily updater) currently loaded?
async function autopilotLoaded(): Promise<{ autopilot_on: boolean; auto_update_on: boolean }> {
  try {
    const res = await run("launchctl", ["list"], { timeoutMs: 10_000 });
    const lines = res.stdout.split("\n");
    return {
      autopilot_on: lines.some((l) => l.includes(TWITTER_AUTOPILOT_LABEL)),
      auto_update_on: lines.some((l) => l.includes(UPDATER_LABEL)),
    };
  } catch {
    return { autopilot_on: false, auto_update_on: false };
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
  const [x, ap, ver] = await Promise.all([
    xStatus().catch(() => ({ connected: false, state: "" }) as any),
    autopilotLoaded(),
    versionStatus().catch(() => ({ installed: VERSION, latest: null, update_available: false }) as any),
  ]);
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
    runtime_ready: runtimeReady(),
    runtime_provisioning: isProvisioning(),
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
function panelHtmlForHttp(): string {
  const html = fs.readFileSync(path.join(DIST_DIR, "panel.html"), "utf-8");
  const inject = `<script>window.__SAPS_BRIDGE__=${JSON.stringify("http")};</script>`;
  if (html.includes("</head>")) return html.replace("</head>", inject + "</head>");
  return inject + html;
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
    srv.listen(0, "127.0.0.1", () => {
      const addr = srv.address();
      const port = typeof addr === "object" && addr ? addr.port : 0;
      localPanel = { url: `http://127.0.0.1:${port}/`, server: srv };
      resolve(localPanel.url);
    });
  });
}

// Open a URL in the user's default browser, cross-platform. Honors
// SAPS_PANEL_NO_OPEN (set on headless autopilot boxes or in tests) to skip the
// actual open while still returning the URL to the caller.
async function openInBrowser(url: string): Promise<void> {
  if (process.env.SAPS_PANEL_NO_OPEN) return;
  const cmd =
    process.platform === "darwin" ? "open" : process.platform === "win32" ? "cmd" : "xdg-open";
  const args = process.platform === "win32" ? ["/c", "start", "", url] : [url];
  try {
    await run(cmd, args, { timeoutMs: 10_000 });
  } catch (e: any) {
    console.error("[social-autoposter-mcp] openInBrowser failed:", e?.message || e);
  }
}

appTool(
  "dashboard",
  {
    title: "Social Autoposter dashboard",
    description:
      "Render the Social Autoposter dashboard in chat: a visual surface showing project setup, X " +
      "connection, autopilot state, and 7-day stats, with buttons to run a draft cycle, toggle " +
      "autopilot, connect X, and refresh. Use when the user asks to see the dashboard, panel, " +
      "status, or controls. ALSO call this at the end of any state-changing or results-producing " +
      "action (draft_cycle, post_drafts, autopilot enable/disable, get_stats) so the user sees the " +
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
      `Social Autoposter v${snap.version}` +
      (snap.update_available && snap.latest_version ? ` (update to ${snap.latest_version})` : "") +
      ` — projects ${snap.projects_ready}/${snap.projects_total} ready, ` +
      `X ${snap.x_connected ? "connected" : "not connected"}, ` +
      `autopilot ${snap.autopilot_on ? "on" : "off"}.`;
    const base = {
      content: [{ type: "text" as const, text: human }],
      structuredContent: { snapshot: JSON.stringify(snap) },
    };
    // If the host can render MCP Apps UI inline, the _meta.ui.resourceUri above
    // makes it paint the panel; just return the snapshot. If it CAN'T (Claude
    // Code / Cowork today), fall back to serving the identical panel.html from a
    // loopback HTTP server and opening it in the browser, so the user still gets
    // the visual surface instead of a wall of text.
    if (hostRendersAppUi()) return base;
    try {
      const url = await startLocalPanel();
      await openInBrowser(url);
      return {
        content: [{
          type: "text" as const,
          text:
            human +
            `\n\nThis host can't render the dashboard inline, so I opened it in your browser: ${url}`,
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

registerAppResource(
  server,
  "Social Autoposter panel",
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

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error(`[social-autoposter-mcp] connected. v=${VERSION} repo=${repoDir()}`);
}

main().catch((err) => {
  console.error("[social-autoposter-mcp] fatal:", err);
  process.exit(1);
});
