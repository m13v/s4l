#!/usr/bin/env node
// Pre-flight gate: is the social-autoposter autopilot scheduled task safe to run
// UNATTENDED, or will one of its tool calls hit a permission prompt and hang?
//
// A headless scheduled run can only use a tool that is ALWAYS-ALLOWED via one of:
//   1. the task's per-task approvedPermissions     (scheduled-tasks.json), OR
//   2. the global ~/.claude/settings.json permissions.allow (inherited, documented), OR
//   3. the task's permissionMode === "bypassPermissions".
// A required tool matching none of those will PROMPT — and a headless run then
// hangs forever (no human to approve), jamming the scheduler.
//
// After the voice-inline + no-improvise changes, the autopilot needs ONLY two
// tools: scan_candidates and submit_drafts. This checks both and EXITS NON-ZERO
// if either would prompt, so a caller (onboarding, CI, a human) can refuse to
// rely on the task until it's safe.
//
// Read-only. Schedules/approves/changes NOTHING.
//
// Usage:
//   node check_autopilot_approvals.mjs [taskId] [tool1,tool2,...]
//   (defaults: taskId=social-autoposter-autopilot, tools=scan_candidates,submit_drafts)
// Exit codes: 0 = ready (all allowed), 1 = NOT ready (some would prompt),
//             2 = task not found / could not read approval state.

import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const TASK_ID = process.argv[2] || "social-autoposter-autopilot";
// Required tools matched by SUFFIX so the `mcp__<server>__` prefix can vary
// (the extension is named "S4L" today, but don't hardcode the prefix).
const REQUIRED = (process.argv[3] || "scan_candidates,submit_drafts")
  .split(",")
  .map((s) => s.trim())
  .filter(Boolean);

function readJSON(p) {
  try {
    return JSON.parse(fs.readFileSync(p, "utf-8"));
  } catch {
    return null;
  }
}

// Does a permission rule string allow the MCP tool with the given suffix, with no
// command/content restriction? Handles `mcp__server__tool`, `mcp__server__*`, and
// `mcp__server__get_*` glob forms (per the documented MCP rule syntax).
function ruleAllowsTool(ruleStr, suffix) {
  const r = String(ruleStr || "");
  const parts = r.split("__");
  if (parts[0] !== "mcp" || parts.length < 3) return false;
  const tool = parts.slice(2).join("__"); // "scan_candidates" | "*" | "get_*"
  if (tool === suffix) return true;
  if (tool === "*") return true; // whole-server glob
  if (tool.endsWith("*") && suffix.startsWith(tool.slice(0, -1))) return true;
  return false;
}

// 2. Global settings.json allow rules (documented; inherited by scheduled tasks).
function globalAllowRules() {
  const s = readJSON(path.join(os.homedir(), ".claude", "settings.json"));
  const allow = s?.permissions?.allow;
  return Array.isArray(allow) ? allow : [];
}

// 1. Find the scheduled task + its approvedPermissions / permissionMode. The
//    location is INTERNAL to Claude Desktop (undocumented), so this is best-effort
//    and walks the account/org dirs to find scheduled-tasks.json.
function findTask() {
  const base = path.join(
    os.homedir(),
    "Library",
    "Application Support",
    "Claude",
    "claude-code-sessions"
  );
  let accts = [];
  try {
    accts = fs.readdirSync(base);
  } catch {
    return null;
  }
  for (const acct of accts) {
    let orgs = [];
    try {
      orgs = fs.readdirSync(path.join(base, acct));
    } catch {
      continue;
    }
    for (const org of orgs) {
      const file = path.join(base, acct, org, "scheduled-tasks.json");
      const j = readJSON(file);
      const tasks = j?.scheduledTasks;
      if (!Array.isArray(tasks)) continue;
      for (const t of tasks) {
        const id = t?.id || t?.taskId || "";
        const fp = String(t?.filePath || "");
        if (id === TASK_ID || fp.includes(TASK_ID)) return { task: t, file };
      }
    }
  }
  return null;
}

const allowRules = globalAllowRules();
const found = findTask();
const approved = Array.isArray(found?.task?.approvedPermissions)
  ? found.task.approvedPermissions
  : [];
const mode = found?.task?.permissionMode || "default";

const results = REQUIRED.map((suffix) => {
  const viaMode = mode === "bypassPermissions";
  // task approvedPermissions: allowed only if no ruleContent restriction.
  const viaApproved = approved.some(
    (e) => !e?.ruleContent && ruleAllowsTool(e?.toolName, suffix)
  );
  const viaSettings = allowRules.some((r) => ruleAllowsTool(r, suffix));
  const allowed = viaMode || viaApproved || viaSettings;
  const via = viaMode
    ? "permissionMode=bypass"
    : viaApproved
      ? "task-approvedPermissions"
      : viaSettings
        ? "settings.json"
        : null;
  return { tool: suffix, allowed, via };
});

const missing = results.filter((r) => !r.allowed).map((r) => r.tool);
const ready = !!found && missing.length === 0;

console.log(
  JSON.stringify(
    {
      taskId: TASK_ID,
      taskFound: !!found,
      permissionMode: mode,
      required: REQUIRED,
      results,
      ready,
      missing,
    },
    null,
    2
  )
);

if (!found) {
  console.error(
    `\n[pre-flight] task "${TASK_ID}" not found — it hasn't been scheduled yet, ` +
      `or the approval state couldn't be read.`
  );
  process.exit(2);
}
if (!ready) {
  console.error(
    `\n[pre-flight] NOT READY — these tools would PROMPT, so an unattended run would hang: ` +
      `${missing.join(", ")}`
  );
  console.error(
    `Fix any one of: (a) Settings > Extensions > social-autoposter > set those tools to "Allow"; ` +
      `(b) run one cycle and click "Always allow"; or (c) add them to ~/.claude/settings.json ` +
      `permissions.allow (e.g. "mcp__S4L__scan_candidates").`
  );
  process.exit(1);
}
console.error(
  `\n[pre-flight] READY — all required tools are always-allowed ` +
    `(${results.map((r) => `${r.tool}:${r.via}`).join(", ")}).`
);
process.exit(0);
