// Durable onboarding ledger + structured Doctor integration.
//
// The ledger is local and authoritative. Backend delivery is deliberately
// best-effort: only redacted event metadata is sent, and an offline API never
// blocks setup. The shared CommonJS modules are also consumed by bin/cli.js, so
// CLI and MCP use the exact same Doctor checks and JSON file contract.

import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { repoDir, run } from "./repo.js";
import { resolvePython } from "./runtime.js";

const require = createRequire(import.meta.url);

export const ONBOARDING_MILESTONES = [
  "environment_checked",
  "runtime_ready",
  "x_connected",
  "x_verified",
  "profile_scanned",
  "mode_chosen",
  "project_ready",
  "topics_seeded",
  "tasks_scheduled",
] as const;

export type OnboardingMilestone = (typeof ONBOARDING_MILESTONES)[number];
export type DoctorPhase = "pre_connect" | "full";
export type DoctorCheckStatus = "pass" | "fail" | "expected" | "warn";

export interface DoctorCheck {
  id: string;
  name: string;
  status: DoctorCheckStatus;
  detail?: string;
  fix?: string;
  duration_ms: number;
}

export interface DoctorReport {
  schema_version: number;
  phase: DoctorPhase;
  ok: boolean;
  started_at: string;
  completed_at: string;
  duration_ms: number;
  summary: {
    pass: number;
    fail: number;
    expected: number;
    warn: number;
    total: number;
  };
  checks: DoctorCheck[];
}

interface LedgerEvent {
  event_id: string;
  occurred_at: string;
  milestone: OnboardingMilestone;
  type: "attempt" | "completed" | "blocked" | "doctor";
  attempt: number;
  status?: string;
  code?: string;
  metadata?: Record<string, unknown>;
  backend_sent_at?: string | null;
}

interface LedgerApi {
  readLedger(): any;
  publicSnapshot(): any;
  recordAttempt(
    milestone: OnboardingMilestone,
    metadata?: Record<string, unknown>
  ): any;
  completeMilestone(
    milestone: OnboardingMilestone,
    metadata?: Record<string, unknown>
  ): any;
  blockMilestone(
    milestone: OnboardingMilestone,
    code: string,
    message: string,
    metadata?: Record<string, unknown>
  ): any;
  recordDoctorReport(report: DoctorReport): any;
  pendingBackendEvents(): LedgerEvent[];
  markBackendEventsSent(eventIds: string[]): any;
}

interface DoctorApi {
  runDoctorSync(opts: {
    phase: DoctorPhase;
    repoDir: string;
    python: string;
  }): DoctorReport;
}

const ledgerApi = require("../shared/onboarding-ledger.cjs") as LedgerApi;
const doctorApi = require("../shared/doctor.cjs") as DoctorApi;

export function onboardingSnapshot() {
  return ledgerApi.publicSnapshot();
}

export function onboardingLedger() {
  return ledgerApi.readLedger();
}

export function recordOnboardingAttempt(
  milestone: OnboardingMilestone,
  metadata: Record<string, unknown> = {}
) {
  const ledger = ledgerApi.recordAttempt(milestone, metadata);
  void flushOnboardingEvents();
  return ledger;
}

export function completeOnboardingMilestone(
  milestone: OnboardingMilestone,
  metadata: Record<string, unknown> = {}
) {
  const ledger = ledgerApi.completeMilestone(milestone, metadata);
  void flushOnboardingEvents();
  return ledger;
}

export function blockOnboardingMilestone(
  milestone: OnboardingMilestone,
  code: string,
  message: string,
  metadata: Record<string, unknown> = {}
) {
  const ledger = ledgerApi.blockMilestone(milestone, code, message, metadata);
  void flushOnboardingEvents();
  return ledger;
}

export async function runDoctorPhase(phase: DoctorPhase): Promise<DoctorReport> {
  const report = doctorApi.runDoctorSync({
    phase,
    repoDir: repoDir(),
    python: resolvePython(),
  });
  ledgerApi.recordDoctorReport(report);
  await flushOnboardingEvents();
  return report;
}

// Run each phase once automatically during onboarding. A direct doctor tool call
// can still force another historical run.
export async function ensureDoctorPhase(
  phase: DoctorPhase
): Promise<DoctorReport> {
  const runs = onboardingLedger()?.doctor?.runs;
  const existing = Array.isArray(runs)
    ? [...runs].reverse().find((run) => run?.phase === phase)
    : null;
  return existing || runDoctorPhase(phase);
}

function safeNumber(value: unknown): number | undefined {
  const n = Number(value);
  return Number.isFinite(n) ? n : undefined;
}

function safeString(value: unknown, max = 80): string | undefined {
  if (typeof value !== "string") return undefined;
  const clean = value.trim().replace(/\s+/g, "_").slice(0, max);
  return clean || undefined;
}

// Strict allowlist: local events can contain useful human-readable diagnostics,
// but the API receives only coarse states/counts/codes and Doctor check statuses.
function redactMetadata(event: LedgerEvent): Record<string, unknown> {
  const source = event.metadata || {};
  const out: Record<string, unknown> = {};
  for (const key of ["phase", "outcome", "state"]) {
    const value = safeString(source[key]);
    if (value) out[key] = value;
  }
  for (const key of [
    "missing_count",
    "topic_count",
    "draft_count",
    "exit_code",
  ]) {
    const value = safeNumber(source[key]);
    if (value !== undefined) out[key] = value;
  }
  if (typeof source.ok === "boolean") out.ok = source.ok;
  const summary = source.summary;
  if (summary && typeof summary === "object") {
    const s = summary as Record<string, unknown>;
    out.summary = Object.fromEntries(
      ["pass", "fail", "expected", "warn", "total"]
        .map((key) => [key, safeNumber(s[key])])
        .filter((entry): entry is [string, number] => entry[1] !== undefined)
    );
  }
  const statuses = source.check_statuses;
  if (statuses && typeof statuses === "object") {
    out.check_statuses = Object.fromEntries(
      Object.entries(statuses as Record<string, unknown>)
        .map(([key, value]) => [safeString(key), safeString(value)])
        .filter(
          (entry): entry is [string, string] =>
            Boolean(entry[0]) && Boolean(entry[1])
        )
    );
  }
  return out;
}

function redactEvent(event: LedgerEvent) {
  return {
    event_id: event.event_id,
    occurred_at: event.occurred_at,
    milestone: event.milestone,
    type: event.type,
    attempt: event.attempt,
    status: safeString(event.status),
    code: safeString(event.code),
    metadata: redactMetadata(event),
  };
}

async function identityHeader(): Promise<string | null> {
  const script = path.join(repoDir(), "scripts", "identity.py");
  if (!fs.existsSync(script)) return null;
  const result = await run(resolvePython(), [script, "header"], {
    timeoutMs: 15_000,
  });
  const header = result.stdout.trim();
  return result.code === 0 && header ? header : null;
}

let flushInFlight: Promise<{
  sent: number;
  pending: number;
  error?: string;
}> | null = null;

export function flushOnboardingEvents(): Promise<{
  sent: number;
  pending: number;
  error?: string;
}> {
  if (flushInFlight) return flushInFlight;
  flushInFlight = (async () => {
    const header = await identityHeader();
    if (!header) {
      const pending = ledgerApi.pendingBackendEvents().length;
      return {
        sent: 0,
        pending,
        error: "installation identity unavailable",
      };
    }
    // Onboarding milestones go to the CLOUD RUN host (AUTOPOSTER_LOG_BASE,
    // default app.s4l.ai), the same GCP-logging lane as the raw log stream: the
    // relay console.log()s each event so Cloud Run's runtime ships it to Cloud
    // Logging. NOT the Vercel host (AUTOPOSTER_API_BASE / s4l.ai) the heartbeat
    // still uses — these events are not a DB row anymore.
    const base = (
      process.env.AUTOPOSTER_LOG_BASE || "https://app.s4l.ai"
    ).replace(/\/+$/, "");
    let sent = 0;
    // Re-read after every batch. This catches milestone events appended while a
    // prior network request was in flight, so the final onboarding event is not
    // stranded waiting for some unrelated future tool call.
    for (let batch = 0; batch < 10; batch += 1) {
      const pending = ledgerApi.pendingBackendEvents().slice(0, 50);
      if (pending.length === 0) return { sent, pending: 0 };
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 5000);
      try {
        const response = await fetch(
          `${base}/api/v1/installations/onboarding-events`,
          {
            method: "POST",
            headers: {
              "content-type": "application/json",
              "x-installation": header,
            },
            body: JSON.stringify({ events: pending.map(redactEvent) }),
            signal: controller.signal,
          }
        );
        if (!response.ok) {
          return {
            sent,
            pending: ledgerApi.pendingBackendEvents().length,
            error: `backend returned ${response.status}`,
          };
        }
        ledgerApi.markBackendEventsSent(pending.map((event) => event.event_id));
        sent += pending.length;
      } catch (error) {
        return {
          sent,
          pending: ledgerApi.pendingBackendEvents().length,
          error: error instanceof Error ? error.message : String(error),
        };
      } finally {
        clearTimeout(timeout);
      }
    }
    return { sent, pending: ledgerApi.pendingBackendEvents().length };
  })().finally(() => {
    flushInFlight = null;
  });
  return flushInFlight;
}
