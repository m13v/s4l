// Type declarations for onboarding-ledger.cjs — the SINGLE source of truth for
// onboarding milestones, shared verbatim (no build step) between bin/cli.js
// (plain CommonJS) and the compiled MCP server. This file exists so the TS side
// can derive its types from the one real MILESTONES array instead of hand
// -maintaining a second, driftable copy (see mcp/src/onboarding.ts).

export const MILESTONES: readonly [
  "environment_checked",
  "runtime_ready",
  "x_connected",
  "x_verified",
  "profile_scanned",
  "mode_chosen",
  "project_ready",
  "topics_seeded",
  "tasks_scheduled",
];

export function blockMilestone(
  id: string,
  code: string,
  message: string,
  metadata?: Record<string, unknown>,
  opts?: Record<string, unknown>
): any;

export function completeMilestone(
  id: string,
  metadata?: Record<string, unknown>,
  opts?: Record<string, unknown>
): any;

export function ledgerPath(opts?: Record<string, unknown>): string;

export function markBackendEventsSent(
  eventIds: string[],
  opts?: Record<string, unknown>
): any;

export function pendingBackendEvents(opts?: Record<string, unknown>): any[];

export function publicSnapshot(opts?: Record<string, unknown>): any;

export function readLedger(opts?: Record<string, unknown>): any;

export function recordAttempt(
  id: string,
  metadata?: Record<string, unknown>,
  opts?: Record<string, unknown>
): any;

export function recordDoctorReport(report: any, opts?: Record<string, unknown>): any;

export function writeLedger(ledger: any, opts?: Record<string, unknown>): any;
