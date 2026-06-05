// uv-owned Python runtime provisioning for the social-autoposter MCP.
//
// The pipeline is ~60k lines of Python. New users don't have a usable Python
// env (missing / wrong version / externally-managed / Xcode CLT prompt), which
// is the #1 source of install failures. This module provisions a fully OWNED
// runtime that never touches the user's system Python or PATH:
//
//   1. uv (Astral's standalone Python launcher)
//   2. a standalone CPython via `uv python install` (NOT the user's python)
//   3. an owned venv at ~/.social-autoposter-mcp/runtime/.venv
//   4. the pipeline deps (requirements.txt) synced into that venv
//   5. the Playwright Chromium binary
//
// The absolute interpreter path is written to runtime.json; the server reads it
// for SAPS_PYTHON. No PATH lookup, no venv activation, no system python — so the
// whole "Python environment + paths" class of bug disappears.
//
// Progress is written to install-progress.json as a JSON object the panel polls
// via the `install_status` tool (host-agnostic; survives the iframe sandbox).

import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

// Pin the standalone CPython series the venv is built from. Bump deliberately.
const PYTHON_VERSION = "3.12";

// dist/runtime.js -> repo root is two levels up (mcp/dist -> mcp -> repo root).
const __dirname = path.dirname(fileURLToPath(import.meta.url));

// Everything we own lives under one state dir (next to setup-state.json).
const STATE_DIR =
  process.env.SAPS_STATE_DIR || path.join(os.homedir(), ".social-autoposter-mcp");
const RUNTIME_DIR = path.join(STATE_DIR, "runtime");
const VENV_DIR = path.join(RUNTIME_DIR, ".venv");
const RUNTIME_JSON = path.join(STATE_DIR, "runtime.json");
const PROGRESS_JSON = path.join(STATE_DIR, "install-progress.json");

// The venv's interpreter, by absolute path (no activation needed).
const VENV_PYTHON =
  process.platform === "win32"
    ? path.join(VENV_DIR, "Scripts", "python.exe")
    : path.join(VENV_DIR, "bin", "python3");

// Where the pipeline source is materialized for a bare .mcpb install (no clone).
// The embedded tarball is the EXACT `npm pack` output (same `files` allowlist as
// the published package), so the unpacked source is byte-identical to what npm
// users run (no second curation list, no drift). npm tarballs unpack under a
// top-level `package/` dir, so the repo root is REPO_MATERIALIZED/package.
const REPO_MATERIALIZE_DIR = path.join(STATE_DIR, "repo");
const MATERIALIZED_REPO = path.join(REPO_MATERIALIZE_DIR, "package");
// dist/runtime.js sits beside the embedded tarball produced at build time.
const EMBEDDED_TARBALL = path.join(__dirname, "pipeline.tgz");

// A directory is a usable pipeline clone only if it carries requirements.txt
// (the deps manifest) AND scripts/ (the pipeline). Guards against pointing at an
// empty extension dir or a half-deleted state dir.
function looksLikeRepo(dir: string | undefined): boolean {
  if (!dir) return false;
  return (
    fs.existsSync(path.join(dir, "requirements.txt")) &&
    fs.existsSync(path.join(dir, "scripts"))
  );
}

// Resolve the pipeline repo the server shells out to, preferring (in order):
//   1. SAPS_REPO_DIR when it's a real clone (npm/git install, Story A); never
//      overwritten, power users keep their working tree.
//   2. runtime.json's repo_dir (the materialized repo from a .mcpb install).
//   3. the materialized path on disk even if runtime.json is missing.
//   4. SAPS_REPO_DIR as-is, then the two-levels-up dev default.
// Dynamic (not a load-time const) so a first-run materialize is picked up
// without a server restart (same property resolvePython() relies on).
export function resolveRepoDir(): string {
  const env = process.env.SAPS_REPO_DIR;
  if (looksLikeRepo(env)) return env as string;
  const rt = readRuntime();
  if (rt && rt.repo_dir && looksLikeRepo(rt.repo_dir)) return rt.repo_dir;
  if (looksLikeRepo(MATERIALIZED_REPO)) return MATERIALIZED_REPO;
  if (env) return env;
  return path.resolve(__dirname, "..", "..");
}

export interface RuntimeInfo {
  python: string;
  uv: string;
  python_version: string;
  // Absolute path to the pipeline repo the server shells out to. For a npm /
  // git install this is the user's clone (SAPS_REPO_DIR); for a bare .mcpb
  // double-click it's the repo we materialize from the embedded tarball under
  // the state dir. Persisted so resolveRepoDir() finds it on later boots.
  repo_dir?: string;
  ready: boolean;
  provisioned_at: string;
}

export type StepStatus = "pending" | "running" | "done" | "error";
export interface ProgressStep {
  id: string;
  label: string;
  status: StepStatus;
  detail?: string;
}
export interface InstallProgress {
  running: boolean;
  done: boolean;
  ok: boolean;
  error?: string;
  steps: ProgressStep[];
  started_at: string;
  updated_at: string;
}

const STEP_DEFS: Array<{ id: string; label: string }> = [
  { id: "repo", label: "Unpack pipeline source" },
  { id: "uv", label: "Install uv (Python launcher)" },
  { id: "python", label: `Download standalone Python ${PYTHON_VERSION}` },
  { id: "venv", label: "Create owned virtual environment" },
  { id: "deps", label: "Install pipeline dependencies" },
  { id: "chromium", label: "Download Chromium browser (~150MB)" },
];

// ---------------------------------------------------------------------------
// runtime.json (the durable result the server reads for SAPS_PYTHON).
// ---------------------------------------------------------------------------
export function readRuntime(): RuntimeInfo | null {
  try {
    if (!fs.existsSync(RUNTIME_JSON)) return null;
    return JSON.parse(fs.readFileSync(RUNTIME_JSON, "utf-8")) as RuntimeInfo;
  } catch {
    return null;
  }
}

// The runtime is "ready" only if runtime.json says so, the interpreter it
// points at still exists on disk (catches a half-deleted state dir), AND a
// usable pipeline repo resolves. The repo check catches a pre-Story-B runtime
// that was marked ready before Step 0 ("Unpack pipeline source") existed: the
// venv is present so the old check passed, but the embedded tarball was never
// materialized, so the server shells out to an empty repo and the panel reads a
// blank "0/1, not set up" world. Returning false here forces install_runtime to
// re-provision, which runs Step 0 and materializes the repo (idempotent).
export function runtimeReady(): boolean {
  const rt = readRuntime();
  if (!(rt && rt.ready && rt.python && fs.existsSync(rt.python))) return false;
  return looksLikeRepo(resolveRepoDir());
}

// Resolve the interpreter the pipeline should run under, preferring the owned
// uv runtime, then the install-pinned SAPS_PYTHON, then bare python3. This is
// the single seam that moves the pipeline off the user's system Python.
export function resolvePython(): string {
  const rt = readRuntime();
  if (rt && rt.python && fs.existsSync(rt.python)) return rt.python;
  return process.env.SAPS_PYTHON || "python3";
}

// ---------------------------------------------------------------------------
// install-progress.json (polled by the panel via install_status).
// ---------------------------------------------------------------------------
export function readProgress(): InstallProgress | null {
  try {
    if (!fs.existsSync(PROGRESS_JSON)) return null;
    return JSON.parse(fs.readFileSync(PROGRESS_JSON, "utf-8")) as InstallProgress;
  } catch {
    return null;
  }
}

function writeProgress(p: InstallProgress): void {
  p.updated_at = new Date().toISOString();
  fs.mkdirSync(STATE_DIR, { recursive: true });
  fs.writeFileSync(PROGRESS_JSON, JSON.stringify(p, null, 2) + "\n", "utf-8");
}

function freshProgress(): InstallProgress {
  const now = new Date().toISOString();
  return {
    running: true,
    done: false,
    ok: false,
    steps: STEP_DEFS.map((s) => ({ id: s.id, label: s.label, status: "pending" as StepStatus })),
    started_at: now,
    updated_at: now,
  };
}

// ---------------------------------------------------------------------------
// Spawning helper. Captures output; never throws (returns code + tail).
// ---------------------------------------------------------------------------
function sh(
  cmd: string,
  args: string[],
  opts: { env?: NodeJS.ProcessEnv; timeoutMs?: number } = {}
): Promise<{ code: number; out: string }> {
  return new Promise((resolve) => {
    const child = spawn(cmd, args, {
      env: { ...process.env, ...(opts.env || {}) },
    });
    let out = "";
    const cap = (d: Buffer) => {
      out += d.toString();
      if (out.length > 20000) out = out.slice(-20000); // keep a tail, bound memory
    };
    let timer: NodeJS.Timeout | undefined;
    if (opts.timeoutMs) {
      timer = setTimeout(() => child.kill("SIGTERM"), opts.timeoutMs);
    }
    child.stdout?.on("data", cap);
    child.stderr?.on("data", cap);
    child.on("close", (code) => {
      if (timer) clearTimeout(timer);
      resolve({ code: code ?? -1, out });
    });
    child.on("error", (err) => {
      if (timer) clearTimeout(timer);
      resolve({ code: -1, out: out + String(err) });
    });
  });
}

function bash(script: string, timeoutMs: number): Promise<{ code: number; out: string }> {
  return sh("bash", ["-lc", script], { timeoutMs });
}

// Locate uv (Astral installs to ~/.local/bin/uv; Homebrew to /opt/homebrew/bin).
function findUv(): string | null {
  const candidates = [
    path.join(os.homedir(), ".local", "bin", "uv"),
    "/opt/homebrew/bin/uv",
    "/usr/local/bin/uv",
    "/usr/bin/uv",
  ];
  for (const c of candidates) {
    if (fs.existsSync(c)) return c;
  }
  return null;
}

// ---------------------------------------------------------------------------
// Provisioning. Idempotent: re-running re-derives any missing piece. A module
// guard prevents two concurrent runs within the same process.
// ---------------------------------------------------------------------------
let inFlight: Promise<InstallProgress> | null = null;

export function isProvisioning(): boolean {
  return inFlight !== null;
}

// Kick off provisioning (or return the in-flight run). Returns immediately with
// the initial progress snapshot; the panel polls install_status for updates.
export function startProvisioning(): InstallProgress {
  if (!inFlight) {
    const progress = freshProgress();
    writeProgress(progress);
    inFlight = provision(progress).finally(() => {
      inFlight = null;
    });
  }
  return readProgress() ?? freshProgress();
}

async function provision(progress: InstallProgress): Promise<InstallProgress> {
  const setStep = (id: string, status: StepStatus, detail?: string) => {
    const st = progress.steps.find((s) => s.id === id);
    if (st) {
      st.status = status;
      if (detail !== undefined) st.detail = detail;
    }
    writeProgress(progress);
  };
  const fail = (msg: string): InstallProgress => {
    progress.running = false;
    progress.done = true;
    progress.ok = false;
    progress.error = msg;
    writeProgress(progress);
    return progress;
  };

  fs.mkdirSync(RUNTIME_DIR, { recursive: true });

  // --- Step 0: materialize the pipeline repo --------------------------------
  // If SAPS_REPO_DIR is already a real clone (npm/git install), use it untouched.
  // Otherwise (bare .mcpb double-click) unpack the embedded npm tarball so the
  // pipeline source lands on disk and every later step + the server agree on one
  // repo path. requirements.txt MUST exist after this for the deps step.
  setStep("repo", "running");
  let resolvedRepo: string;
  if (looksLikeRepo(process.env.SAPS_REPO_DIR)) {
    resolvedRepo = process.env.SAPS_REPO_DIR as string;
    setStep("repo", "done", `using existing clone: ${resolvedRepo}`);
  } else {
    if (!fs.existsSync(EMBEDDED_TARBALL)) {
      return fail(
        `no pipeline source: SAPS_REPO_DIR is not a clone and the embedded ` +
          `tarball is missing (${EMBEDDED_TARBALL}). Reinstall the extension or ` +
          `set SAPS_REPO_DIR to a social-autoposter clone.`
      );
    }
    // Clean any half-unpacked previous attempt, then extract fresh (idempotent).
    try {
      fs.rmSync(MATERIALIZED_REPO, { recursive: true, force: true });
    } catch {
      /* best effort */
    }
    fs.mkdirSync(REPO_MATERIALIZE_DIR, { recursive: true });
    const r = await sh("tar", ["xzf", EMBEDDED_TARBALL, "-C", REPO_MATERIALIZE_DIR], {
      timeoutMs: 120000,
    });
    if (r.code !== 0 || !looksLikeRepo(MATERIALIZED_REPO)) {
      return fail(`unpacking pipeline source failed (exit ${r.code}). ${r.out.slice(-400)}`);
    }
    resolvedRepo = MATERIALIZED_REPO;
    setStep("repo", "done", `unpacked to ${resolvedRepo}`);
  }

  // --- Step 1: uv -----------------------------------------------------------
  setStep("uv", "running");
  let uv = findUv();
  if (!uv) {
    const r = await bash("curl -LsSf https://astral.sh/uv/install.sh | sh", 180000);
    uv = findUv();
    if (!uv) {
      return fail(`uv install failed (exit ${r.code}). ${r.out.slice(-400)}`);
    }
  }
  setStep("uv", "done", uv);

  // Pin uv's cache/data inside our state dir so the standalone Python and the
  // venv resolve consistently and don't depend on ambient UV_* env.
  const uvEnv: NodeJS.ProcessEnv = {
    UV_PYTHON_INSTALL_DIR: path.join(RUNTIME_DIR, "python"),
  };

  // --- Step 2: standalone CPython ------------------------------------------
  setStep("python", "running");
  {
    const r = await sh(uv, ["python", "install", PYTHON_VERSION], {
      env: uvEnv,
      timeoutMs: 300000,
    });
    if (r.code !== 0) {
      return fail(`uv python install ${PYTHON_VERSION} failed (exit ${r.code}). ${r.out.slice(-400)}`);
    }
  }
  setStep("python", "done");

  // --- Step 3: owned venv ---------------------------------------------------
  setStep("venv", "running");
  {
    const r = await sh(uv, ["venv", "--python", PYTHON_VERSION, VENV_DIR], {
      env: uvEnv,
      timeoutMs: 120000,
    });
    if (r.code !== 0 || !fs.existsSync(VENV_PYTHON)) {
      return fail(`uv venv failed (exit ${r.code}). ${r.out.slice(-400)}`);
    }
  }
  setStep("venv", "done", VENV_PYTHON);

  // --- Step 4: pipeline deps ------------------------------------------------
  setStep("deps", "running");
  {
    const reqPath = path.join(resolvedRepo, "requirements.txt");
    const args = fs.existsSync(reqPath)
      ? ["pip", "install", "--python", VENV_PYTHON, "-r", reqPath]
      : ["pip", "install", "--python", VENV_PYTHON, "playwright", "websocket-client", "cryptography"];
    const r = await sh(uv, args, { env: uvEnv, timeoutMs: 600000 });
    if (r.code !== 0) {
      return fail(`dependency install failed (exit ${r.code}). ${r.out.slice(-400)}`);
    }
  }
  setStep("deps", "done");

  // --- Step 5: Playwright Chromium -----------------------------------------
  setStep("chromium", "running");
  {
    const r = await sh(VENV_PYTHON, ["-m", "playwright", "install", "chromium"], {
      timeoutMs: 600000,
    });
    if (r.code !== 0) {
      return fail(`playwright install chromium failed (exit ${r.code}). ${r.out.slice(-400)}`);
    }
  }
  setStep("chromium", "done");

  // --- Persist the result ---------------------------------------------------
  const info: RuntimeInfo = {
    python: VENV_PYTHON,
    uv,
    python_version: PYTHON_VERSION,
    repo_dir: resolvedRepo,
    ready: true,
    provisioned_at: new Date().toISOString(),
  };
  fs.mkdirSync(STATE_DIR, { recursive: true });
  fs.writeFileSync(RUNTIME_JSON, JSON.stringify(info, null, 2) + "\n", "utf-8");

  progress.running = false;
  progress.done = true;
  progress.ok = true;
  writeProgress(progress);
  return progress;
}
