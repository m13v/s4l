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
import { repoDir, runPython, setLineSink } from "./repo.js";
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

// ---- Raw subprocess log streaming ------------------------------------------
// Tees the verbatim stdout/stderr of every pipeline subprocess (via the
// repo.ts run() boundary) to the s4l backend, so we can troubleshoot and
// rescue any user scenario without asking them to ship a log file. Lines are
// buffered in memory and flushed in small batches under the same X-Installation
// identity the heartbeat uses. Best-effort: this NEVER throws into the server,
// never blocks the child's I/O, and drops on overflow rather than growing
// unbounded. Disable with SAPS_LOG_STREAM=0.

const LOG_STREAM_ENABLED = process.env.SAPS_LOG_STREAM !== "0";
const LOG_MAX_LINE_LEN = 8192; // mirror the backend cap
const LOG_MAX_BUFFER = 1000; // drop oldest beyond this (overflow protection)
const LOG_FLUSH_BATCH = 100; // flush eagerly once we have this many lines
const LOG_MAX_PER_POST = 200; // backend accepts 1-200 per request
const LOG_FLUSH_MS = 3000; // otherwise flush on this cadence

type BufferedLine = { ts: string; stream: string; line: string; context: string };

const logBuffer: BufferedLine[] = [];
let logDropped = 0; // count of lines dropped on overflow (surfaced periodically)
let logFlushing = false;
let logTimer: NodeJS.Timeout | undefined;
let cachedInstallHeader: string | null = null;
let logStreamingStarted = false;

async function installHeader(): Promise<string | null> {
  if (cachedInstallHeader) return cachedInstallHeader;
  try {
    const idScript = path.join(repoDir(), "scripts", "identity.py");
    if (!fs.existsSync(idScript)) return null;
    const res = await runPython("scripts/identity.py", ["header"], { timeoutMs: 10_000 });
    const header = (res.stdout || "").trim();
    if (res.code === 0 && header) {
      cachedInstallHeader = header;
      return header;
    }
  } catch {
    /* best-effort */
  }
  return null;
}

// Buffer one raw line. Called from the repo.ts line sink, so it must be cheap
// and total non-throwing.
export function logLine(stream: "stdout" | "stderr", line: string, context: string): void {
  if (!LOG_STREAM_ENABLED) return;
  try {
    logBuffer.push({
      ts: new Date().toISOString(),
      stream,
      line: line.length > LOG_MAX_LINE_LEN ? line.slice(0, LOG_MAX_LINE_LEN) : line,
      context: context || "",
    });
    if (logBuffer.length > LOG_MAX_BUFFER) {
      // Drop oldest to bound memory; the newest lines are the most useful.
      logDropped += logBuffer.length - LOG_MAX_BUFFER;
      logBuffer.splice(0, logBuffer.length - LOG_MAX_BUFFER);
    }
    if (logBuffer.length >= LOG_FLUSH_BATCH) void flushLogs();
  } catch {
    /* never throw into the run() boundary */
  }
}

export async function flushLogs(): Promise<void> {
  if (!LOG_STREAM_ENABLED) return;
  if (logFlushing || logBuffer.length === 0) return;
  logFlushing = true;
  try {
    const header = await installHeader();
    if (!header) return; // runtime not unpacked yet; keep buffering
    const base = (process.env.AUTOPOSTER_API_BASE || "https://s4l.ai").replace(/\/+$/, "");
    // Drain in <=200-line POSTs until the buffer empties (or a POST fails).
    while (logBuffer.length > 0) {
      const batch = logBuffer.splice(0, LOG_MAX_PER_POST);
      const lines = batch.map((b) => ({
        ts: b.ts,
        stream: b.stream,
        line: b.line,
        context: b.context || undefined,
      }));
      try {
        const resp = await fetch(`${base}/api/v1/installations/logs`, {
          method: "POST",
          headers: { "X-Installation": header, "content-type": "application/json" },
          body: JSON.stringify({ lines }),
          signal: AbortSignal.timeout(15_000),
        });
        if (!resp.ok) {
          // Drop this batch (don't re-buffer): a persistent 4xx/5xx would grow
          // the buffer unbounded. The raw stream is best-effort.
          console.error(`[social-autoposter-mcp] log flush http ${resp.status}`);
          break;
        }
      } catch (err: any) {
        // Network blip: drop this batch, stop draining, try again next tick.
        console.error("[social-autoposter-mcp] log flush failed:", err?.message || err);
        break;
      }
    }
    if (logDropped > 0) {
      console.error(`[social-autoposter-mcp] log stream dropped ${logDropped} line(s) on overflow`);
      logDropped = 0;
    }
  } finally {
    logFlushing = false;
  }
}

// Register the repo.ts line sink and start the periodic flush. Idempotent.
export function startLogStreaming(): void {
  if (!LOG_STREAM_ENABLED || logStreamingStarted) return;
  logStreamingStarted = true;
  try {
    setLineSink((line, stream, context) => logLine(stream, line, context));
    logTimer = setInterval(() => void flushLogs(), LOG_FLUSH_MS);
    logTimer.unref();
  } catch (err: any) {
    console.error("[social-autoposter-mcp] log streaming start failed:", err?.message || err);
  }
}
