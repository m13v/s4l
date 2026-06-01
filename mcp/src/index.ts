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

async function produceDrafts(_project?: string): Promise<DraftResult> {
  // TODO(decision): wire scan+draft once the DRAFT_ONLY gate lands.
  //   (a) unlock run-twitter-cycle.sh, add DRAFT_ONLY=1 that exits after writing
  //       /tmp/twitter_cycle_plan_<batch>.json, then call it here; or
  //   (b) drive the scan/score/draft phases directly from this wrapper.
  // For now, operate on the most recent existing plan if one is present so the
  // review+post half is fully testable.
  const existing = latestBatchId();
  if (existing) return { batchId: existing };
  return {
    batchId: null,
    blocked:
      "Scan+draft is not wired yet: the drafting phase lives in the locked " +
      "run-twitter-cycle.sh, which posts straight through with no draft-only stop. " +
      "Decide: (a) unlock it to add a DRAFT_ONLY=1 gate that exits after writing the " +
      "plan JSON, or (b) drive scan/score/draft phases from this wrapper. Once chosen, " +
      "draft_cycle will produce a batch, walk you through approve/skip, and post.",
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
      project: z.string().optional(),
    },
  },
  async ({ project }) => {
    const drafted = await produceDrafts(project);
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
      project: z.string().optional(),
    },
  },
  async ({ days, project }) => {
    const args = ["--posts-only", "--platform", "twitter", "--days", String(days)];
    if (project) args.push("--project", project);
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
