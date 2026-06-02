#!/usr/bin/env node
// social-autoposter MCP server (X/Twitter rail).
//
// Three tools, nothing more:
//   draft_cycle  - scan + draft, surface each thread + drafted reply, run an
//                  elicitation approve/skip per draft, then post the approved ones.
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
import {
  REPO_DIR,
  runPython,
  run,
  readPlan,
  writePlan,
  planPath,
  latestBatchId,
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
  CONFIG_PATH,
  type ProjectInput,
} from "./setup.js";
import { xStatus, xConnect, summarizeXAuth } from "./twitterAuth.js";
import { VERSION, versionStatus, latestPublishedVersion } from "./version.js";

const TWITTER_AUTOPILOT_LABEL = "com.m13v.social-twitter-cycle";
const TWITTER_AUTOPILOT_PLIST = path.join(
  os.homedir(),
  "Library",
  "LaunchAgents",
  `${TWITTER_AUTOPILOT_LABEL}.plist`
);

// version is resolved at runtime from the real shipped package (see version.ts),
// so serverInfo.version finally reflects what the user actually has installed
// instead of a frozen literal.
const server = new McpServer({
  name: "social-autoposter",
  version: VERSION,
});

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
// or posting unreviewed. Everything downstream of this (elicit + post) is real.
// ---------------------------------------------------------------------------
interface DraftResult {
  batchId: string | null;
  blocked?: string;
}

async function produceDrafts(project?: string): Promise<DraftResult> {
  // Run the real pipeline in DRAFT_ONLY mode: scan -> score -> draft -> link-gen,
  // then STOP before posting. The script prints `DRAFT_ONLY_PLAN=<path>` and
  // leaves the plan on disk for us to review + post. SAPS_FORCE_PROJECT scopes
  // the cycle to one project; TWITTER_PAGE_GEN_RATE=0 keeps link-gen sub-second.
  const env: NodeJS.ProcessEnv = {
    DRAFT_ONLY: "1",
    TWITTER_PAGE_GEN_RATE: "0",
  };
  if (project) env.SAPS_FORCE_PROJECT = project;
  const res = await run("bash", ["skill/run-twitter-cycle.sh"], {
    env,
    timeoutMs: 900_000, // scan+draft can take several minutes
  });
  // Prefer the explicit marker; fall back to the newest plan file on disk.
  const marker = /DRAFT_ONLY_PLAN=\/tmp\/twitter_cycle_plan_(.+)\.json/.exec(
    res.stdout + "\n" + res.stderr
  );
  if (marker && marker[1]) return { batchId: marker[1] };
  const existing = latestBatchId();
  if (existing) return { batchId: existing };
  return {
    batchId: null,
    blocked:
      `Draft cycle produced no plan (exit ${res.code}). This usually means scan ` +
      `found no fresh candidates, or the pipeline errored. Tail:\n` +
      res.stderr.split("\n").slice(-12).join("\n"),
  };
}

// One BATCHED elicitation: present every draft at once as a checkbox grid with
// an optional inline edit per draft, submitted in a single round trip. The user
// ticks which to post (all pre-checked, so the common path is just "submit"),
// optionally rewrites any reply, and confirms ONCE — collapsing N popups into 1.
//
// MCP elicitation schemas must be FLAT primitives (no arrays / nested objects),
// so we generate one `post_<n>` boolean + one `edit_<n>` string per draft.
async function reviewDrafts(
  plan: Plan
): Promise<{ approved: number; skipped: number; edited: number; aborted: boolean }> {
  const candidates = plan.candidates || [];
  if (candidates.length === 0) return { approved: 0, skipped: 0, edited: 0, aborted: false };

  // Build the flat schema + a human-readable table for the form header.
  // Each property is a primitive elicitation schema (boolean checkbox or
  // optional string), the only shapes MCP elicitation allows.
  type ElicitProp =
    | { type: "boolean"; title?: string; description?: string; default?: boolean }
    | { type: "string"; title?: string; description?: string };
  const properties: Record<string, ElicitProp> = {};
  const rows: string[] = [];
  candidates.forEach((c, i) => {
    const n = i + 1;
    const author = c.thread_author ? `@${c.thread_author}` : "(unknown thread)";
    const style = c.engagement_style ?? "?";
    const reply = c.reply_text ?? "(empty)";
    const link = c.link_url ? `  ·  link: ${c.link_url}` : "";
    rows.push(
      `[${n}] ${author}  (style: ${style})${link}\n` +
        `    ${reply.replace(/\n/g, "\n    ")}\n` +
        `    thread: ${c.candidate_url ?? "?"}`
    );
    const preview = reply.length > 140 ? `${reply.slice(0, 140)}…` : reply;
    properties[`post_${n}`] = {
      type: "boolean",
      title: `Post [${n}] ${author}`,
      description: preview,
      default: true,
    };
    properties[`edit_${n}`] = {
      type: "string",
      title: `Rewrite [${n}] (optional)`,
      description:
        "Leave blank to post as drafted. Type your own wording to replace this reply before posting.",
    };
  });

  const message =
    `Review ${candidates.length} drafted ` +
    `${candidates.length === 1 ? "reply" : "replies"}. ` +
    `Every draft is pre-checked to post — untick the ones you don't want, ` +
    `optionally rewrite any, then submit once.\n\n` +
    rows.join("\n\n");

  let res;
  try {
    res = await server.server.elicitInput({
      message,
      requestedSchema: { type: "object", properties, required: [] },
    });
  } catch (e) {
    // Host doesn't support elicitation (some Claude Desktop builds). Bail out
    // rather than silently posting or silently skipping everything.
    return { approved: 0, skipped: 0, edited: 0, aborted: true };
  }
  if (res.action !== "accept") {
    // User cancelled/declined the whole review -> post nothing.
    candidates.forEach((c) => (c.approved = false));
    return { approved: 0, skipped: 0, edited: 0, aborted: res.action === "cancel" };
  }

  const content = (res.content as Record<string, unknown>) || {};
  let approved = 0;
  let skipped = 0;
  let edited = 0;
  candidates.forEach((c, i) => {
    const n = i + 1;
    // Pre-checked (default:true): treat anything but an explicit false as "post".
    const wantPost = content[`post_${n}`] !== false;
    const rawEdit = content[`edit_${n}`];
    const edit = typeof rawEdit === "string" ? rawEdit.trim() : "";
    if (edit) {
      c.reply_text = edit; // inline correction replaces the drafted reply
      edited++;
    }
    if (wantPost) {
      c.approved = true;
      approved++;
    } else {
      c.approved = false;
      skipped++;
    }
  });
  return { approved, skipped, edited, aborted: false };
}

async function postApproved(batchId: string, plan: Plan) {
  const approved = (plan.candidates || []).filter((c: PlanCandidate) => c.approved === true);
  if (approved.length === 0) return { attempted: 0, exit_code: 0, summary: "nothing approved" };
  const approvedBatch = `${batchId}_approved`;
  writePlan(approvedBatch, { ...plan, candidates: approved });
  const res = await runPython(
    "scripts/twitter_post_plan.py",
    ["--plan", planPath(approvedBatch)],
    { timeoutMs: 900_000 }
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
server.registerTool(
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
        .enum(["connect_x"])
        .optional()
        .describe(
          "connect_x = import/validate your X session in the autoposter's managed browser. " +
            "Without confirm:true it only EXPLAINS what it will do (so you can tell the user first)."
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
        config_path: CONFIG_PATH,
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
      return jsonContent({
        ok: true,
        project: result.project,
        action: result.created ? "created" : "updated",
        ready: result.ready,
        missing_required: result.missing_required,
        config_path: CONFIG_PATH,
        note: result.ready
          ? `Project '${result.project}' is fully set up. Next: connect X so the autoposter can post — ` +
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

// ---- draft_cycle: the whole manual loop in one tool -----------------------
server.registerTool(
  "draft_cycle",
  {
    title: "Draft an X reply cycle",
    description:
      "Scan X, draft replies on this machine, then show ALL drafts at once in a single " +
      "checkbox form: tick which to post (every draft pre-checked), optionally rewrite any " +
      "reply inline, and submit once. Only the ticked ones post. The entire manual loop in " +
      "one call: discover -> draft -> review -> post. Nothing posts without your approval.",
    inputSchema: {
      project: z
        .string()
        .optional()
        .describe("Which configured project to draft for. Optional when only one project is set up; required when several are."),
    },
  },
  async ({ project }) => {
    const r = resolveProject(project);
    if (!r.ok) return textContent(r.message!);
    const proj = r.project!;
    const drafted = await produceDrafts(proj);
    if (drafted.blocked || !drafted.batchId) {
      return textContent(drafted.blocked ?? "No drafts produced.");
    }
    const plan = readPlan(drafted.batchId);
    if (!plan || !(plan.candidates && plan.candidates.length)) {
      return textContent(`No drafts in batch ${drafted.batchId}.`);
    }
    const review = await reviewDrafts(plan);
    writePlan(drafted.batchId, plan);
    if (review.aborted && review.approved === 0) {
      return jsonContent({
        batch_id: drafted.batchId,
        drafted: plan.candidates.length,
        review_aborted: true,
        note:
          "Review did not complete (host may not support elicitation, or you cancelled). " +
          "Nothing was posted.",
      });
    }
    const posted = await postApproved(drafted.batchId, plan);
    return jsonContent({
      batch_id: drafted.batchId,
      drafted: plan.candidates.length,
      approved: review.approved,
      skipped: review.skipped,
      edited: review.edited,
      posted,
    });
  }
);

// ---- autopilot: one tool, three actions -----------------------------------
server.registerTool(
  "autopilot",
  {
    title: "X autopilot",
    description:
      "Control background X/Twitter posting. action=enable loads the launchd job so the " +
      "cycle fires automatically; action=disable unloads it (manual draft_cycle still works); " +
      "action=status reports whether it is loaded.",
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
    if (action === "status") {
      const res = await run("launchctl", ["list"], { timeoutMs: 10_000 });
      const loaded = res.stdout.split("\n").some((l) => l.includes(TWITTER_AUTOPILOT_LABEL));
      return jsonContent({ label: TWITTER_AUTOPILOT_LABEL, loaded });
    }
    if (action === "enable") {
      let res = await run("launchctl", ["bootstrap", `gui/${uid}`, TWITTER_AUTOPILOT_PLIST], {
        timeoutMs: 15_000,
      });
      if (res.code !== 0) {
        res = await run("launchctl", ["load", TWITTER_AUTOPILOT_PLIST], { timeoutMs: 15_000 });
      }
      return textContent(
        res.code === 0
          ? `Autopilot enabled (${TWITTER_AUTOPILOT_LABEL} loaded).`
          : `Failed to enable autopilot (exit ${res.code}): ${res.stderr || res.stdout}\n` +
              `Check the plist exists at ${TWITTER_AUTOPILOT_PLIST}.`
      );
    }
    // disable
    let res = await run("launchctl", ["bootout", `gui/${uid}/${TWITTER_AUTOPILOT_LABEL}`], {
      timeoutMs: 15_000,
    });
    if (res.code !== 0) {
      res = await run("launchctl", ["unload", TWITTER_AUTOPILOT_PLIST], { timeoutMs: 15_000 });
    }
    return textContent(
      res.code === 0
        ? `Autopilot disabled (${TWITTER_AUTOPILOT_LABEL} unloaded).`
        : `Failed (exit ${res.code}): ${res.stderr || res.stdout}`
    );
  }
);

// ---- get_stats: read-only -------------------------------------------------
server.registerTool(
  "get_stats",
  {
    title: "Get X/Twitter stats",
    description:
      "Read-only post + engagement stats for the X/Twitter rail over the last N days. " +
      "Wraps project_stats_json.py. Use to show the user how their posts are performing.",
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

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error(`[social-autoposter-mcp] connected. repo=${REPO_DIR}`);
}

main().catch((err) => {
  console.error("[social-autoposter-mcp] fatal:", err);
  process.exit(1);
});
