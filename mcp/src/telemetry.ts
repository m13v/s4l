// Telemetry for the .mcpb desktop client: install-lane heartbeat + Sentry error
// reporting. Both are best-effort and MUST never throw into the MCP server.
//
// Why this exists: the npx install lane registers a launchd heartbeat
// (com.m13v.social-autoposter-heartbeat) so installs show up in the
// install-lane digest. The .mcpb (Claude Desktop extension) had no equivalent,
// so .mcpb installs were invisible (and their errors uncollected). This module
// closes both gaps. Mirrors the Fazm app's Sentry posture (org `mediar-n5`).

import * as Sentry from "@sentry/node";
import path from "node:path";
import fs from "node:fs";
import { repoDir, runPython } from "./repo.js";
import { VERSION } from "./version.js";

// Sentry DSN is a client-side identifier (safe to embed, same posture as Fazm's
// hardcoded Swift DSN). Overridable via env for dev. Empty -> Sentry disabled.
const EMBEDDED_DSN = "https://4d44ac907262c6545cf8681703528d04@o4507617161314304.ingest.us.sentry.io/4511598804336640";
const SENTRY_DSN = process.env.SAPS_SENTRY_DSN || EMBEDDED_DSN;

let sentryReady = false;

export function initSentry(): void {
  if (sentryReady || !SENTRY_DSN) return;
  try {
    Sentry.init({
      dsn: SENTRY_DSN,
      release: `social-autoposter-mcp@${VERSION}`,
      environment:
        process.env.SAPS_ENV === "development" || process.env.NODE_ENV === "development"
          ? "development"
          : "production",
      // Errors only; no performance tracing (keeps the bundle's overhead minimal
      // and avoids the OpenTelemetry --import requirement under ESM).
      tracesSampleRate: 0,
      sendDefaultPii: false,
    });
    sentryReady = true;
    void tagInstall();
  } catch {
    /* never let telemetry init break the server */
  }
}

// Attach the stable install_id so Sentry events are attributable to an install
// (and cross-referenceable with the install-lane digest). Best-effort.
async function tagInstall(): Promise<void> {
  try {
    const idScript = path.join(repoDir(), "scripts", "identity.py");
    if (!fs.existsSync(idScript)) return;
    const res = await runPython("scripts/identity.py", ["show"], { timeoutMs: 10_000 });
    if (res.code !== 0) return;
    const id = JSON.parse(res.stdout || "{}");
    if (id.install_id) Sentry.setTag("install_id", String(id.install_id));
    if (id.hostname) Sentry.setTag("hostname", String(id.hostname));
  } catch {
    /* best-effort */
  }
}

export function captureError(err: unknown, tags?: Record<string, string>): void {
  try {
    if (sentryReady) Sentry.captureException(err, tags ? { tags } : undefined);
  } catch {
    /* swallow */
  }
}

export async function flushSentry(ms = 2000): Promise<void> {
  try {
    if (sentryReady) await Sentry.flush(ms);
  } catch {
    /* swallow */
  }
}

// Phone home so .mcpb installs show up in the install-lane digest, parity with
// the npx launchd heartbeat. Best-effort; never throws.
export async function sendHeartbeat(reason: string): Promise<void> {
  try {
    const idScript = path.join(repoDir(), "scripts", "identity.py");
    if (!fs.existsSync(idScript)) return; // runtime not unpacked yet (pre-install)
    const res = await runPython("scripts/identity.py", ["header"], { timeoutMs: 10_000 });
    const header = (res.stdout || "").trim();
    if (res.code !== 0 || !header) return;
    const base = (process.env.AUTOPOSTER_API_BASE || "https://s4l.ai").replace(/\/+$/, "");
    const resp = await fetch(`${base}/api/v1/installations/heartbeat`, {
      method: "POST",
      headers: { "X-Installation": header, "content-type": "application/json" },
      body: "{}",
      signal: AbortSignal.timeout(15_000),
    });
    if (!resp.ok) console.error(`[social-autoposter-mcp] heartbeat http ${resp.status}`);
  } catch (err: any) {
    captureError(err, { component: "heartbeat", reason });
    console.error("[social-autoposter-mcp] heartbeat failed:", err?.message || err);
  }
}

