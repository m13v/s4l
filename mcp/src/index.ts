#!/usr/bin/env node
// social-autoposter MCP server (X/Twitter rail).
//
// Manual mode: surface drafted replies, let the user edit/approve, then post
// only the approved ones via the existing twitter_post_plan.py.
// Autopilot: load/unload the launchd job so the cycle fires in the background.
// Stats: read-only project stats via project_stats_json.py.
//
// This server is a THIN wrapper. The pipeline brain (scan, score, drafting
// prompts, posting) stays in the Python/shell scripts; we only orchestrate
// and present.

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
  version: "0.0.1",
});

function jsonContent(obj: unknown) {
  return { content: [{ type: "text" as const, text: JSON.stringify(obj, null, 2) }] };
}
function textContent(text: string) {
  return { content: [{ type: "text" as const, text }] };
}

function resolveBatch(batchId?: string): string | null {
  return batchId && batchId.trim() ? batchId.trim() : latestBatchId();
}

// ---- Stats (read-only, works today) ---------------------------------------
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
    const args = ["scripts/project_stats_json.py", "--posts-only", "--platform", "twitter", "--days", String(days)];
    if (project) args.push("--project", project);
    const res = await runPython(args[0], args.slice(1), { timeoutMs: 120_000 });
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

// ---- List drafted replies awaiting review ---------------------------------
server.registerTool(
  "list_drafts",
  {
    title: "List drafted X replies",
    description:
      "List the drafted replies in the current (or a specified) draft batch, with their " +
      "approval state. Render these to the user for edit/approve before posting. " +
      "Reads the plan JSON produced by the drafting phase.",
    inputSchema: {
      batch_id: z.string().optional(),
    },
  },
  async ({ batch_id }) => {
    const batch = resolveBatch(batch_id);
    if (!batch) return textContent("No draft batch found. Run start_draft_cycle first.");
    const plan = readPlan(batch);
    if (!plan) return textContent(`No plan file for batch ${batch}.`);
    const drafts = (plan.candidates || []).map((c, i) => ({
      index: i,
      candidate_id: c.candidate_id,
      author: c.thread_author,
      url: c.candidate_url,
      style: c.engagement_style,
      reply_text: c.reply_text,
      link_url: c.link_url,
      approved: c.approved === true,
    }));
    return jsonContent({ batch_id: batch, count: drafts.length, drafts });
  }
);

// ---- Edit a draft ----------------------------------------------------------
server.registerTool(
  "edit_draft",
  {
    title: "Edit a drafted X reply",
    description:
      "Replace the reply text of one draft (by index) in a batch. Does not post; only " +
      "updates the pending draft so the user can refine it before approving.",
    inputSchema: {
      index: z.number().int().min(0),
      reply_text: z.string().min(1),
      batch_id: z.string().optional(),
    },
  },
  async ({ index, reply_text, batch_id }) => {
    const batch = resolveBatch(batch_id);
    if (!batch) return textContent("No draft batch found.");
    const plan = readPlan(batch);
    if (!plan || !plan.candidates || !plan.candidates[index]) {
      return textContent(`No draft at index ${index} in batch ${batch}.`);
    }
    plan.candidates[index].reply_text = reply_text;
    writePlan(batch, plan);
    return textContent(`Updated draft ${index} in batch ${batch}.`);
  }
);

// ---- Approve / reject ------------------------------------------------------
server.registerTool(
  "approve_draft",
  {
    title: "Approve drafted X reply/replies",
    description:
      "Mark one draft (by index) or all drafts in a batch as approved. Approved drafts are " +
      "the only ones post_approved will publish.",
    inputSchema: {
      index: z.number().int().min(0).optional(),
      all: z.boolean().default(false),
      batch_id: z.string().optional(),
    },
  },
  async ({ index, all, batch_id }) => {
    const batch = resolveBatch(batch_id);
    if (!batch) return textContent("No draft batch found.");
    const plan = readPlan(batch);
    if (!plan || !plan.candidates) return textContent(`No plan for batch ${batch}.`);
    if (all) {
      plan.candidates.forEach((c) => (c.approved = true));
    } else if (index !== undefined && plan.candidates[index]) {
      plan.candidates[index].approved = true;
    } else {
      return textContent("Provide either index or all=true.");
    }
    writePlan(batch, plan);
    const n = plan.candidates.filter((c) => c.approved).length;
    return textContent(`Approved ${all ? "all" : `draft ${index}`}. ${n} approved in batch ${batch}.`);
  }
);

server.registerTool(
  "reject_draft",
  {
    title: "Reject a drafted X reply",
    description: "Mark one draft (by index) as not approved so it will be skipped at posting time.",
    inputSchema: {
      index: z.number().int().min(0),
      batch_id: z.string().optional(),
    },
  },
  async ({ index, batch_id }) => {
    const batch = resolveBatch(batch_id);
    if (!batch) return textContent("No draft batch found.");
    const plan = readPlan(batch);
    if (!plan || !plan.candidates || !plan.candidates[index]) {
      return textContent(`No draft at index ${index} in batch ${batch}.`);
    }
    plan.candidates[index].approved = false;
    writePlan(batch, plan);
    return textContent(`Rejected draft ${index} in batch ${batch}.`);
  }
);

// ---- Post approved drafts --------------------------------------------------
server.registerTool(
  "post_approved",
  {
    title: "Post approved X replies",
    description:
      "Publish ONLY the approved drafts in a batch. Builds a filtered plan and runs the " +
      "existing twitter_post_plan.py against it. Irreversible: this posts to X.",
    inputSchema: {
      batch_id: z.string().optional(),
    },
  },
  async ({ batch_id }) => {
    const batch = resolveBatch(batch_id);
    if (!batch) return textContent("No draft batch found.");
    const plan = readPlan(batch);
    if (!plan || !plan.candidates) return textContent(`No plan for batch ${batch}.`);
    const approved = plan.candidates.filter((c: PlanCandidate) => c.approved === true);
    if (approved.length === 0) {
      return textContent("No approved drafts to post. Approve at least one first.");
    }
    const filtered = { ...plan, candidates: approved };
    const approvedBatch = `${batch}_approved`;
    writePlan(approvedBatch, filtered);
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
    return jsonContent({
      batch_id: batch,
      attempted: approved.length,
      exit_code: res.code,
      summary,
      stderr_tail: res.stderr.split("\n").slice(-8).join("\n"),
    });
  }
);

// ---- Autopilot control -----------------------------------------------------
server.registerTool(
  "autopilot_status",
  {
    title: "X autopilot status",
    description: "Report whether the background X/Twitter posting job (launchd) is loaded.",
    inputSchema: {},
  },
  async () => {
    const res = await run("launchctl", ["list"], { timeoutMs: 10_000 });
    const loaded = res.stdout
      .split("\n")
      .some((l) => l.includes(TWITTER_AUTOPILOT_LABEL));
    return jsonContent({ label: TWITTER_AUTOPILOT_LABEL, loaded });
  }
);

server.registerTool(
  "enable_autopilot",
  {
    title: "Enable X autopilot",
    description:
      "Turn on background posting: load the launchd job so the X/Twitter cycle fires " +
      "automatically. Requires the plist to already be generated by install/init.",
    inputSchema: {},
  },
  async () => {
    const uid = process.getuid ? process.getuid() : 0;
    // Prefer modern bootstrap; fall back to legacy load.
    let res = await run("launchctl", ["bootstrap", `gui/${uid}`, TWITTER_AUTOPILOT_PLIST], {
      timeoutMs: 15_000,
    });
    if (res.code !== 0) {
      res = await run("launchctl", ["load", TWITTER_AUTOPILOT_PLIST], { timeoutMs: 15_000 });
    }
    const ok = res.code === 0;
    return textContent(
      ok
        ? `Autopilot enabled (${TWITTER_AUTOPILOT_LABEL} loaded).`
        : `Failed to enable autopilot (exit ${res.code}): ${res.stderr || res.stdout}\n` +
            `Check the plist exists at ${TWITTER_AUTOPILOT_PLIST}.`
    );
  }
);

server.registerTool(
  "disable_autopilot",
  {
    title: "Disable X autopilot",
    description: "Turn off background posting: unload the launchd job. Manual mode still works.",
    inputSchema: {},
  },
  async () => {
    const uid = process.getuid ? process.getuid() : 0;
    let res = await run("launchctl", ["bootout", `gui/${uid}/${TWITTER_AUTOPILOT_LABEL}`], {
      timeoutMs: 15_000,
    });
    if (res.code !== 0) {
      res = await run("launchctl", ["unload", TWITTER_AUTOPILOT_PLIST], { timeoutMs: 15_000 });
    }
    const ok = res.code === 0;
    return textContent(
      ok ? `Autopilot disabled (${TWITTER_AUTOPILOT_LABEL} unloaded).` : `Failed (exit ${res.code}): ${res.stderr || res.stdout}`
    );
  }
);

// ---- Draft cycle (BLOCKED on a decision; see reply) ------------------------
// run-twitter-cycle.sh runs scan->score->draft->POST straight through and is on
// the locked-files list, so there is no draft-only stop point yet. Until we add
// a DRAFT_ONLY=1 gate (needs unlock) or drive the phases individually, this tool
// reports the blocker instead of silently doing nothing or posting unreviewed.
server.registerTool(
  "start_draft_cycle",
  {
    title: "Start an X draft cycle (not yet wired)",
    description:
      "Run scan + score + draft and STOP before posting, so the user can review. " +
      "Pending an architecture decision (DRAFT_ONLY gate in the locked run-twitter-cycle.sh).",
    inputSchema: {
      project: z.string().optional(),
    },
  },
  async () => {
    return textContent(
      "start_draft_cycle is not wired yet. The drafting phase lives in the locked " +
        "run-twitter-cycle.sh, which posts straight through with no draft-only stop. " +
        "Decision needed: (a) unlock it to add a DRAFT_ONLY=1 gate that exits after writing " +
        "/tmp/twitter_cycle_plan_<batch>.json, or (b) drive scan/score/draft phases from the " +
        "wrapper. Once chosen, this tool will produce a batch and you can list_drafts on it."
    );
  }
);

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  // stderr only; stdout is the MCP channel.
  console.error(`[social-autoposter-mcp] connected. repo=${REPO_DIR}`);
}

main().catch((err) => {
  console.error("[social-autoposter-mcp] fatal:", err);
  process.exit(1);
});
