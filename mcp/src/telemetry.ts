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
    // Attach a slim host-resource sample so a leaking box (the agent-mode
    // session pile-up that can balloon RAM to tens of GB) is visible centrally
    // without us SSHing in. Best-effort: any failure falls back to "{}" so the
    // heartbeat itself never depends on the sampler succeeding.
    let body = "{}";
    try {
      const mem = await runPython("scripts/memory_snapshot.py", ["--summary"], { timeoutMs: 12_000 });
      const out = (mem.stdout || "").trim();
      if (mem.code === 0 && out) {
        body = JSON.stringify({ resource: JSON.parse(out) });
      }
    } catch {
      /* keep body = "{}" */
    }
    const resp = await fetch(`${base}/api/v1/installations/heartbeat`, {
      method: "POST",
      headers: { "X-Installation": header, "content-type": "application/json" },
      body,
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
// repo.ts run() boundary) to the s4l Cloud Run relay, which simply
// console.log()s each line so Cloud Run's runtime ships it to Cloud Logging.
// No database, no service-account key on the client — the relay is the only
// thing authenticated to GCP, and it authenticates implicitly via its Cloud
// Run runtime identity. Lines are buffered in memory and flushed in small
// batches under the same X-Installation identity the heartbeat uses.
//
// Best-effort: NEVER throws into the server, never blocks the child's I/O, and
// drops on overflow rather than growing unbounded. Disable with
// SAPS_LOG_STREAM=0.
//
// IMPORTANT: logs go to the CLOUD RUN host (AUTOPOSTER_LOG_BASE, default
// app.s4l.ai), NOT the Vercel host (AUTOPOSTER_API_BASE / s4l.ai) the heartbeat
// and onboarding-events use. Cloud Run's native stdout -> Cloud Logging path is
// the whole point of this lane.

const LOG_STREAM_ENABLED = process.env.SAPS_LOG_STREAM !== "0";
const LOG_MAX_LINE_LEN = 8192; // mirror the relay cap
const LOG_MAX_BUFFER = 1000; // drop oldest beyond this (overflow protection)
const LOG_FLUSH_BATCH = 100; // flush eagerly once we have this many lines
const LOG_MAX_PER_POST = 200; // relay accepts 1-200 per request
const LOG_FLUSH_MS = 3000; // otherwise flush on this cadence

// Drop genuinely useless high-volume lines before they ever buffer, so a chatty
// run doesn't crowd out the signal (and to keep Cloud Logging volume sane).
// Empty/whitespace-only lines plus an env-extensible regex of obvious dump
// signatures. Deliberately conservative: real pipeline output is the value, so
// we only filter clear noise. Extend via SAPS_LOG_NOISE_RE (a JS regex source).
let logNoiseRe: RegExp | null = null;
try {
  const extra = (process.env.SAPS_LOG_NOISE_RE || "").trim();
  const sources = [
    extra,
  ].filter(Boolean);
  logNoiseRe = sources.length ? new RegExp(sources.join("|")) : null;
} catch {
  logNoiseRe = null;
}

// The X-Installation header (identity.py `header` output) is a single long base64
// blob printed on stdout. Every heartbeat + every log flush shells identity.py to
// mint it, and that stdout was being tee'd straight back into the log stream, which
// re-triggered a flush — a self-referential loop that flooded Cloud Logging with
// ~21k identical base64 lines/hour and buried real pipeline output. Karol's box was
// impossible to read through it. Drop any line that is nothing but a long run of
// base64 chars (no spaces): real pipeline output is never shaped like this, so the
// filter is safe. Kept separate from logNoiseRe so an env override can't disable it.
const BASE64_BLOB_RE = /^[A-Za-z0-9+/=_-]{120,}$/;

function isNoise(line: string): boolean {
  if (!line || !line.trim()) return true; // blank / whitespace-only
  if (BASE64_BLOB_RE.test(line.trim())) return true; // X-Installation header echo
  if (logNoiseRe && logNoiseRe.test(line)) return true;
  return false;
}

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
    if (isNoise(line)) return;
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
    // Cloud Run relay host (NOT the Vercel API host). app.s4l.ai serves
    // bin/server.js, whose POST /api/v1/installations/logs console.log()s each
    // line into Cloud Logging.
    const base = (
      process.env.AUTOPOSTER_LOG_BASE || "https://app.s4l.ai"
    ).replace(/\/+$/, "");
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

