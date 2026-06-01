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

const TWITTER_AUTOPILOT_LABEL = "com.m13v.social-twitter-cycle";
const TWITTER_AUTOPILOT_PLIST = path.join(
  os.homedir(),
  "Library",
  "LaunchAgents",
  `${TWITTER_AUTOPILOT_LABEL}.plist`
);

const server = new McpServer({
  name: "social-autoposter",
  version: "0.1.0",
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

// One elicitation per draft: approve or skip. Returns count approved.
async function reviewDrafts(plan: Plan): Promise<{ approved: number; skipped: number; aborted: boolean }> {
  const candidates = plan.candidates || [];
  let approved = 0;
  let skipped = 0;
  for (let i = 0; i < candidates.length; i++) {
    const c = candidates[i];
    const msg =
      `Draft ${i + 1} of ${candidates.length}\n` +
      `Thread: @${c.thread_author ?? "?"}  ${c.candidate_url ?? ""}\n` +
      `Style: ${c.engagement_style ?? "?"}\n\n` +
      `Drafted reply:\n${c.reply_text ?? "(empty)"}` +
      (c.link_url ? `\n\nLink: ${c.link_url}` : "");
    let res;
    try {
      res = await server.server.elicitInput({
        message: msg,
        requestedSchema: {
          type: "object",
          properties: {
            decision: {
              type: "string",
              enum: ["approve", "skip"],
              description: "approve = post this reply, skip = discard it",
            },
          },
          required: ["decision"],
        },
      });
    } catch (e) {
      // Host doesn't support elicitation (some Claude Desktop builds). Bail out
      // rather than silently posting or silently skipping everything.
      return { approved, skipped, aborted: true };
    }
    if (res.action !== "accept") {
      // User cancelled/declined the whole review.
      c.approved = false;
      return { approved, skipped, aborted: res.action === "cancel" };
    }
    const decision = (res.content as { decision?: string } | undefined)?.decision;
    if (decision === "approve") {
      c.approved = true;
      approved++;
    } else {
      c.approved = false;
      skipped++;
    }
  }
  return { approved, skipped, aborted: false };
}

async function postApproved(batchId: string, plan: Plan) {
  const approved = (plan.candidates || []).filter((c: PlanCandidate) => c.approved === true);
  if (approved.length === 0) return { attempted: 0, exit_code: 0, summary: "nothing approved" };
  const approvedBatch = `${batchId}_approved`;
  writePlan(approvedBatch, { ...plan, candidates: approved });
  const res = await runPython(
    "scripts/twitter_post_plan.py",
    ["--plan", path.join(os.tmpdir(), `twitter_cycle_plan_${approvedBatch}.json`)],
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
      "Run this FIRST, before any drafting or autopilot. Configures a project this install posts " +
      "for: its website, what it does (description), who to target (icp), and brand voice. You can " +
      "set up MULTIPLE products (call once per product, identified by name) and you can fill a " +
      "project's fields INCREMENTALLY across several calls — pass whatever you have, it merges and " +
      "tells you what's still missing. Call with status:true (or no name) to list every configured " +
      "project and its remaining fields. Ask the user conversationally; don't dump a form. The " +
      "draft_cycle, autopilot, and get_stats tools refuse to run until a project is fully set up.",
    inputSchema: {
      status: z.boolean().optional(),
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
    // Status / discovery mode: no project name supplied, or explicitly asked.
    if (args.status === true || !args.name) {
      const projects = listManagedProjectStatus();
      return jsonContent({
        configured: projects.some((p) => p.ready),
        projects,
        required_fields: REQUIRED_FIELDS,
        recommended_fields: RECOMMENDED_FIELDS,
        config_path: CONFIG_PATH,
        next_step:
          projects.length === 0
            ? "No projects yet. Ask the user about a product (website, what it does, who to target, " +
              "brand voice), then call setup with a short name plus those fields. Repeat per product."
            : projects.every((p) => p.ready)
              ? "All configured projects are ready. Call setup again with a new name to add another product, " +
                "or with an existing name + updated fields to change one."
              : "Some projects are missing required fields (see each project's missing_required). Ask the " +
                "user for those and call setup again with that project's name plus the missing fields.",
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
          ? `Project '${result.project}' is fully set up. You can now run draft_cycle, autopilot, and ` +
            `get_stats for it.`
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
      "Scan X, draft replies on this machine, then walk you through each one (approve or " +
      "skip) and post only the approved ones. The entire manual loop in one call: discover " +
      "-> draft -> review -> post. Nothing posts without your approval.",
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
