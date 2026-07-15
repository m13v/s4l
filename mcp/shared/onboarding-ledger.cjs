"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const SCHEMA_VERSION = 1;
const MILESTONES = [
  "environment_checked",
  "runtime_ready",
  "x_connected",
  "x_verified",
  "profile_scanned",
  "mode_chosen",
  "project_ready",
  "topics_seeded",
  "tasks_scheduled",
  // Optional platform add-ons (see OPTIONAL_MILESTONES below). Listed here so
  // assertMilestone/appendEvent accept them, but they never gate `complete`.
  "reddit_connected",
  "reddit_verified",
];

// Milestones that are offered but NOT required for onboarding completion.
// Reddit is an optional platform: most installs never connect it, and adding
// these ids must never regress an already-complete (or fresh X-only) box back
// to "Setting up". publicSnapshot() therefore (a) excludes these from the
// `complete` computation and (b) omits them from the milestones array while
// they are still pristine "pending" (so progress bars and step lists are
// unchanged for installs that never touch reddit); the same
// never-regress-legacy-ledgers principle as the mode_chosen backfill.
const OPTIONAL_MILESTONES = ["reddit_connected", "reddit_verified"];

function stateDir(opts = {}) {
  return (
    opts.stateDir ||
    path.join(os.homedir(), ".social-autoposter-mcp")
  );
}

function ledgerPath(opts = {}) {
  return path.join(stateDir(opts), "onboarding-progress.json");
}

function now() {
  return new Date().toISOString();
}

function freshMilestone() {
  return {
    status: "pending",
    attempts: 0,
  };
}

function freshLedger() {
  const at = now();
  return {
    schema_version: SCHEMA_VERSION,
    started_at: at,
    updated_at: at,
    current_blocker: null,
    milestones: Object.fromEntries(MILESTONES.map((id) => [id, freshMilestone()])),
    doctor: {
      latest: null,
      runs: [],
    },
    events: [],
  };
}

function normalizeLedger(value) {
  const base = freshLedger();
  if (!value || typeof value !== "object") return base;
  const input = value;
  base.started_at =
    typeof input.started_at === "string" ? input.started_at : base.started_at;
  base.updated_at =
    typeof input.updated_at === "string" ? input.updated_at : base.updated_at;
  base.current_blocker =
    input.current_blocker && typeof input.current_blocker === "object"
      ? input.current_blocker
      : null;
  for (const id of MILESTONES) {
    const old =
      input.milestones && typeof input.milestones === "object"
        ? input.milestones[id]
        : null;
    if (!old || typeof old !== "object") continue;
    base.milestones[id] = {
      ...freshMilestone(),
      ...old,
      attempts: Number.isFinite(Number(old.attempts))
        ? Math.max(0, Number(old.attempts))
        : 0,
    };
  }
  if (input.doctor && typeof input.doctor === "object") {
    base.doctor.latest = input.doctor.latest || null;
    base.doctor.runs = Array.isArray(input.doctor.runs)
      ? input.doctor.runs
      : [];
  }
  base.events = Array.isArray(input.events) ? input.events : [];
  return base;
}

function readLedger(opts = {}) {
  try {
    const file = ledgerPath(opts);
    if (!fs.existsSync(file)) return freshLedger();
    return normalizeLedger(JSON.parse(fs.readFileSync(file, "utf8")));
  } catch {
    return freshLedger();
  }
}

function writeLedger(ledger, opts = {}) {
  const dir = stateDir(opts);
  const file = ledgerPath(opts);
  fs.mkdirSync(dir, { recursive: true });
  ledger.schema_version = SCHEMA_VERSION;
  ledger.updated_at = now();
  const tmp = `${file}.${process.pid}.${crypto.randomUUID()}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(ledger, null, 2) + "\n", {
    encoding: "utf8",
    mode: 0o600,
  });
  fs.renameSync(tmp, file);
  try {
    fs.chmodSync(file, 0o600);
  } catch {
    // Best effort on filesystems that do not expose POSIX permissions.
  }
  return ledger;
}

function assertMilestone(id) {
  if (!MILESTONES.includes(id)) {
    throw new Error(`unknown onboarding milestone: ${id}`);
  }
}

function appendEvent(ledger, event) {
  const milestone = ledger.milestones[event.milestone];
  ledger.events.push({
    event_id: crypto.randomUUID(),
    occurred_at: now(),
    milestone: event.milestone,
    type: event.type,
    attempt: milestone ? milestone.attempts : 0,
    status: milestone ? milestone.status : undefined,
    code: event.code,
    metadata: event.metadata || {},
    backend_sent_at: null,
  });
}

function mutate(opts, fn) {
  const ledger = readLedger(opts);
  fn(ledger);
  return writeLedger(ledger, opts);
}

function recordAttempt(id, metadata = {}, opts = {}) {
  assertMilestone(id);
  return mutate(opts, (ledger) => {
    const at = now();
    const milestone = ledger.milestones[id];
    milestone.attempts += 1;
    milestone.last_attempt_at = at;
    milestone.first_started_at = milestone.first_started_at || at;
    if (milestone.status !== "complete") milestone.status = "in_progress";
    if (ledger.current_blocker?.milestone === id) ledger.current_blocker = null;
    appendEvent(ledger, { milestone: id, type: "attempt", metadata });
  });
}

function completeMilestone(id, metadata = {}, opts = {}) {
  assertMilestone(id);
  return mutate(opts, (ledger) => {
    const milestone = ledger.milestones[id];
    if (milestone.status === "complete") return;
    const at = now();
    milestone.status = "complete";
    milestone.completed_at = at;
    milestone.last_attempt_at = milestone.last_attempt_at || at;
    milestone.first_started_at = milestone.first_started_at || at;
    if (milestone.attempts === 0) milestone.attempts = 1;
    if (ledger.current_blocker?.milestone === id) ledger.current_blocker = null;
    appendEvent(ledger, { milestone: id, type: "completed", metadata });
  });
}

function blockMilestone(id, code, message, metadata = {}, opts = {}) {
  assertMilestone(id);
  return mutate(opts, (ledger) => {
    const at = now();
    const milestone = ledger.milestones[id];
    if (milestone.attempts === 0) milestone.attempts = 1;
    milestone.status = "blocked";
    milestone.last_attempt_at = at;
    milestone.first_started_at = milestone.first_started_at || at;
    milestone.last_error_code = code;
    milestone.last_error = message;
    ledger.current_blocker = {
      milestone: id,
      code,
      message,
      at,
      attempt: milestone.attempts,
    };
    appendEvent(ledger, {
      milestone: id,
      type: "blocked",
      code,
      metadata,
    });
  });
}

function recordDoctorReport(report, opts = {}) {
  return mutate(opts, (ledger) => {
    const milestone = ledger.milestones.environment_checked;
    const at = now();
    const fullFailure =
      report && report.phase === "full" && report.ok === false;
    const preserveFullFailure =
      report &&
      report.phase === "pre_connect" &&
      ledger.current_blocker?.milestone === "environment_checked" &&
      ledger.current_blocker?.code === "doctor_full_failed";
    milestone.attempts += 1;
    milestone.last_attempt_at = at;
    milestone.first_started_at = milestone.first_started_at || at;
    milestone.status = fullFailure || preserveFullFailure ? "blocked" : "complete";
    milestone.completed_at = fullFailure || preserveFullFailure
      ? milestone.completed_at
      : milestone.completed_at || at;
    milestone.last_doctor_ok = Boolean(report && report.ok);
    milestone.last_doctor_phase = report && report.phase;
    if (fullFailure) {
      milestone.last_error_code = "doctor_full_failed";
      milestone.last_error = `Full Doctor found ${report.summary?.fail ?? 1} failing check(s).`;
      ledger.current_blocker = {
        milestone: "environment_checked",
        code: "doctor_full_failed",
        message: milestone.last_error,
        at,
        attempt: milestone.attempts,
      };
    } else if (
      !preserveFullFailure &&
      ledger.current_blocker?.milestone === "environment_checked"
    ) {
      ledger.current_blocker = null;
    }
    ledger.doctor.latest = report;
    ledger.doctor.runs.push(report);
    appendEvent(ledger, {
      milestone: "environment_checked",
      type: "doctor",
      code: report && report.ok ? "doctor_ok" : "doctor_issues",
      metadata: {
        phase: report && report.phase,
        ok: Boolean(report && report.ok),
        summary: report && report.summary,
        check_statuses: Object.fromEntries(
          Array.isArray(report && report.checks)
            ? report.checks.map((check) => [check.id, check.status])
            : []
        ),
      },
    });
  });
}

function pendingBackendEvents(opts = {}) {
  return readLedger(opts).events.filter((event) => !event.backend_sent_at);
}

function markBackendEventsSent(eventIds, opts = {}) {
  const ids = new Set(eventIds || []);
  if (ids.size === 0) return readLedger(opts);
  return mutate(opts, (ledger) => {
    const sentAt = now();
    for (const event of ledger.events) {
      if (ids.has(event.event_id)) event.backend_sent_at = sentAt;
    }
  });
}

function publicSnapshot(opts = {}) {
  const ledger = readLedger(opts);
  return {
    schema_version: ledger.schema_version,
    started_at: ledger.started_at,
    updated_at: ledger.updated_at,
    // Optional milestones (reddit) never gate completion.
    complete: MILESTONES.filter((id) => !OPTIONAL_MILESTONES.includes(id)).every(
      (id) => ledger.milestones[id].status === "complete"
    ),
    // Optional milestones appear only once touched (attempted/completed/
    // blocked); a pristine pending optional row would read as an unfinished
    // setup step on installs that never opted into that platform.
    milestones: MILESTONES.filter(
      (id) =>
        !OPTIONAL_MILESTONES.includes(id) ||
        ledger.milestones[id].status !== "pending"
    ).map((id) => ({
      id,
      ...ledger.milestones[id],
      ...(OPTIONAL_MILESTONES.includes(id) ? { optional: true } : {}),
    })),
    current_blocker: ledger.current_blocker,
    doctor: ledger.doctor.latest
      ? {
          phase: ledger.doctor.latest.phase,
          ok: ledger.doctor.latest.ok,
          completed_at: ledger.doctor.latest.completed_at,
          summary: ledger.doctor.latest.summary,
          checks: (ledger.doctor.latest.checks || []).map((check) => ({
            id: check.id,
            name: check.name,
            status: check.status,
            detail: check.detail,
            fix: check.fix,
          })),
        }
      : null,
  };
}

module.exports = {
  MILESTONES,
  OPTIONAL_MILESTONES,
  blockMilestone,
  completeMilestone,
  ledgerPath,
  markBackendEventsSent,
  pendingBackendEvents,
  publicSnapshot,
  readLedger,
  recordAttempt,
  recordDoctorReport,
  writeLedger,
};
