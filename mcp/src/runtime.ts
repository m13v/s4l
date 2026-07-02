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

import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { captureError } from "./telemetry.js";

// Pin the standalone CPython series the venv is built from. Bump deliberately.
const PYTHON_VERSION = "3.12";

// The CDP scan engine the twitter cycle shells out to (~/.local/bin/browser-harness).
// The npm front door (bin/cli.js) clones + `uv tool install -e` this; a bare
// .mcpb install never did, so draft_cycle's scan found no engine and produced
// zero drafts. KEEP BROWSER_HARNESS_PIN IN SYNC WITH bin/cli.js (pinned so
// upstream drift can't reach users untested).
const BROWSER_HARNESS_PIN = "6d20866664ea3d9691b27bbf64f42ae097437dc3";
const BROWSER_HARNESS_REPO = "https://github.com/browser-use/browser-harness";
const HARNESS_DIR = path.join(os.homedir(), "Developer", "browser-harness");
const HARNESS_BIN = path.join(os.homedir(), ".local", "bin", "browser-harness");

// The harness drives a REAL Google Chrome over CDP (see twitter-backend.sh
// _resolve_chrome_bin). Nothing installs Chrome, the runtime only ever
// downloaded Playwright's Chromium (which the cycle does NOT use), so a .mcpb
// install on a Chrome-less Mac green-lit every step and then died mid-cycle with
// "no Chrome/Chromium binary found." These are the paths twitter-backend.sh
// probes (plus ~/Applications, the no-sudo fallback target we install into).
const GOOGLE_CHROME_DMG =
  "https://dl.google.com/chrome/mac/universal/stable/GGRO/googlechrome.dmg";
const CHROME_CANDIDATES = [
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  path.join(
    os.homedir(),
    "Applications",
    "Google Chrome.app",
    "Contents",
    "MacOS",
    "Google Chrome"
  ),
  "/Applications/Chromium.app/Contents/MacOS/Chromium",
  "/usr/bin/google-chrome",
  "/usr/bin/google-chrome-stable",
  "/usr/bin/chromium",
  "/usr/bin/chromium-browser",
  "/snap/bin/chromium",
];

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

// ---- menu bar app (macOS status-bar mini-dashboard) ------------------------
// Provisioned as install Step 8 and re-ensured at server boot. The rumps app
// runs from the owned venv as a KeepAlive LaunchAgent, pointed at a STABLE copy
// under the state dir (NOT the extension dir, which is replaced on every
// update). KEEP MENUBAR_LABEL in sync with menubar/s4l_state.py and
// scripts/reset-test-machine.sh.
export const MENUBAR_LABEL = "com.m13v.social-autoposter.menubar";
export const MENUBAR_PLIST = path.join(
  os.homedir(),
  "Library",
  "LaunchAgents",
  `${MENUBAR_LABEL}.plist`
);
const MENUBAR_DIR = path.join(STATE_DIR, "menubar");
const MENUBAR_ENTRY = path.join(MENUBAR_DIR, "s4l_menubar.py");
const MENUBAR_OUT_LOG = path.join(MENUBAR_DIR, "menubar.out.log");
const MENUBAR_ERR_LOG = path.join(MENUBAR_DIR, "menubar.err.log");
// Stop sentinel: the menu bar's Quit flow writes this file (and boots itself
// out) to record that the USER explicitly stopped S4L. Every auto-start path
// (boot-time ensureMenubar, runtime provision) must respect it, otherwise the
// tray is guaranteed back on the next Claude restart — the exact bug users hit
// after Quit. Only an explicit start action (restart_menubar tool, queue_setup
// re-arm) clears it. KEEP the filename in sync with menubar/s4l_menubar.py.
const MENUBAR_STOP_FLAG = path.join(STATE_DIR, "stopped.flag");

export function menubarStopped(): boolean {
  try {
    return fs.existsSync(MENUBAR_STOP_FLAG);
  } catch {
    return false;
  }
}

export function clearMenubarStop(): void {
  try {
    fs.rmSync(MENUBAR_STOP_FLAG, { force: true });
  } catch {
    /* best-effort */
  }
}

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

// ---- Stray git-checkout detection -------------------------------------------
// A pipeline repo that is a git checkout is legitimate ONLY when it is the
// working tree the running server itself was built in (dev registration:
// `node <checkout>/mcp/dist/index.js`). Any other .git-bearing repo on a
// shipped install is a "stray" clone (someone git-cloned the public repo during
// troubleshooting): every self-update lane deliberately refuses to touch a
// checkout, so the box silently freezes at the clone's version forever while
// the menu bar keeps re-showing the update banner (Nhat's box, 2026-07-01,
// stuck on 1.6.175 with 1.6.189 out). We only reject a checkout when there is
// something to serve instead (a materialized repo, or the embedded tarball to
// make one); otherwise legacy behavior is preserved.
function hasGit(dir: string): boolean {
  try {
    return fs.existsSync(path.join(dir, ".git"));
  } catch {
    return false;
  }
}

function isDevCheckout(dir: string): boolean {
  try {
    return path.resolve(__dirname, "..", "..") === path.resolve(dir);
  } catch {
    return false;
  }
}

function isStrayCheckout(dir: string | undefined | null): boolean {
  if (!dir) return false;
  if (!hasGit(dir) || isDevCheckout(dir)) return false;
  return looksLikeRepo(MATERIALIZED_REPO) || fs.existsSync(EMBEDDED_TARBALL);
}

// Resolve the pipeline repo the server shells out to, preferring (in order):
//   1. SAPS_REPO_DIR when it's a real clone (npm/git install, Story A); never
//      overwritten, power users keep their working tree.
//   2. runtime.json's repo_dir (the materialized repo from a .mcpb install).
//   3. the materialized path on disk even if runtime.json is missing.
//   4. SAPS_REPO_DIR as-is, then the two-levels-up dev default.
// Dynamic (not a load-time const) so a first-run materialize is picked up
// without a server restart (same property resolvePython() relies on).
// Stray git checkouts (see isStrayCheckout) are skipped at every step: a clone
// the running server does NOT live in can never be the pipeline repo on a
// shipped install, because no update lane will ever advance it.
export function resolveRepoDir(): string {
  const env = process.env.SAPS_REPO_DIR;
  if (looksLikeRepo(env) && !isStrayCheckout(env)) return env as string;
  const rt = readRuntime();
  if (rt && rt.repo_dir && looksLikeRepo(rt.repo_dir) && !isStrayCheckout(rt.repo_dir))
    return rt.repo_dir;
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
  // Absolute path to the Google Chrome (or Chromium) binary the harness drives
  // over CDP. Detected if already installed, else installed by provision() on
  // macOS. Persisted so the server can export it as BH_CHROME_BIN for the cycle
  // (the cycle's own _resolve_chrome_bin doesn't scan ~/Applications, our
  // no-sudo fallback target). Absent on a host where Chrome resolves via PATH.
  chrome?: string;
  ready: boolean;
  provisioned_at: string;
  // The .mcpb version whose pipeline.tgz was last materialized into repo_dir.
  // Lets ensurePipelineCurrent() detect a plugin update that refreshed dist/
  // (this server) but left the materialized pipeline stale, and re-extract just
  // the pipeline so server/pipeline fixes actually take effect on update.
  pipeline_version?: string;
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
  { id: "harness", label: "Install browser-harness (CDP scan engine)" },
  { id: "chrome", label: "Install Google Chrome (browser the scanner drives)" },
  { id: "menubar", label: "Install menu bar app" },
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

// The .mcpb version this running server was built from. The release script
// stamps dist/version.json next to this compiled module. Returns null for a dev
// build with no stamp (in which case we leave the working tree untouched).
const VERSION_JSON = path.join(__dirname, "version.json");
function bundledVersion(): string | null {
  try {
    const v = JSON.parse(fs.readFileSync(VERSION_JSON, "utf-8"));
    return typeof v?.version === "string" ? v.version : null;
  } catch {
    return null;
  }
}

// Re-materialize the pipeline source when a plugin UPDATE shipped a newer
// pipeline.tgz than what's on disk. The .mcpb update refreshes dist/ (this
// server) but does NOT re-extract the embedded tarball, so without this the box
// keeps running the pipeline it first materialized and server/pipeline fixes
// silently never take effect on update. Cheap: a version compare, and a tar only
// when stale.
//
// Unlike provision()'s Step 0, this does NOT wipe the repo first — it extracts
// OVER it, so the project's config.json and the logs/ dir (neither of which is in
// the tarball) survive. Best-effort and synchronous: meant to run at server
// startup BEFORE any tool shells out to the pipeline; never throws.
export function ensurePipelineCurrent(): void {
  try {
    // A real clone (npm/git install) is the user's working tree — never touch
    // it. A STRAY checkout (git clone nobody opted into, see isStrayCheckout)
    // does not qualify: fall through so healStrayCheckout() can reclaim.
    const env = process.env.SAPS_REPO_DIR;
    if (looksLikeRepo(env) && !isStrayCheckout(env)) return;
    healStrayCheckout();
    // Nothing materialized yet, or no tarball to extract from: provision() owns
    // the first materialize; this only refreshes an existing one.
    if (!looksLikeRepo(MATERIALIZED_REPO)) return;
    if (!fs.existsSync(EMBEDDED_TARBALL)) return;

    const bundled = bundledVersion();
    if (!bundled) return; // dev build, no stamp — leave the materialized repo alone.
    const rt = readRuntime();
    if (rt?.pipeline_version === bundled) return; // already current.
    const prevVer = rt?.pipeline_version ?? "unrecorded"; // capture before we mutate rt below.

    // Stale (or never recorded): extract the new pipeline OVER the materialized
    // repo. No rmSync, so config.json + logs are preserved.
    fs.mkdirSync(REPO_MATERIALIZE_DIR, { recursive: true });
    const r = spawnSync("tar", ["xzf", EMBEDDED_TARBALL, "-C", REPO_MATERIALIZE_DIR], {
      timeout: 120000,
    });
    if (r.status !== 0 || !looksLikeRepo(MATERIALIZED_REPO)) {
      console.error(
        `[runtime] pipeline re-materialize failed (exit ${r.status}); keeping existing pipeline`
      );
      return;
    }
    // Record the new version so we don't re-extract on every boot.
    const next = rt ?? readRuntime();
    if (next) {
      next.pipeline_version = bundled;
      try {
        fs.writeFileSync(RUNTIME_JSON, JSON.stringify(next, null, 2) + "\n", "utf-8");
      } catch {
        /* best effort — worst case we re-extract next boot */
      }
    }
    console.error(`[runtime] re-materialized pipeline -> ${bundled} (was ${prevVer})`);
  } catch (e: any) {
    console.error(`[runtime] ensurePipelineCurrent error: ${e?.message || e}`);
  }
}

// Reclaim a box whose pipeline repo resolution landed on a stray git checkout
// (either SAPS_REPO_DIR baked into an old registration/plist, or runtime.json's
// repo_dir). Non-destructive: the checkout stays on disk untouched; we simply
// stop using it. Steps: materialize the bundled pipeline if it isn't on disk
// yet, migrate the user state the checkout accumulated while it was live
// (config.json, .env; the project setup lives there), and re-point
// runtime.json so every later resolveRepoDir()/plist rewrite agrees. Runs at
// server boot from ensurePipelineCurrent(); best-effort, never throws.
function healStrayCheckout(): void {
  try {
    const env = process.env.SAPS_REPO_DIR;
    const rt = readRuntime();
    const stray =
      (looksLikeRepo(env) && isStrayCheckout(env) && (env as string)) ||
      (rt?.repo_dir && looksLikeRepo(rt.repo_dir) && isStrayCheckout(rt.repo_dir) && rt.repo_dir) ||
      null;
    if (!stray) return;
    if (!looksLikeRepo(MATERIALIZED_REPO)) {
      if (!fs.existsSync(EMBEDDED_TARBALL)) return; // nothing to serve instead
      fs.mkdirSync(REPO_MATERIALIZE_DIR, { recursive: true });
      const r = spawnSync("tar", ["xzf", EMBEDDED_TARBALL, "-C", REPO_MATERIALIZE_DIR], {
        timeout: 120000,
      });
      if (r.status !== 0 || !looksLikeRepo(MATERIALIZED_REPO)) {
        console.error(
          `[runtime] stray-checkout heal: materialize failed (exit ${r.status}); keeping ${stray}`
        );
        return;
      }
    }
    for (const f of ["config.json", ".env"]) {
      try {
        const src = path.join(stray, f);
        const dst = path.join(MATERIALIZED_REPO, f);
        if (!fs.existsSync(src)) continue;
        if (!fs.existsSync(dst) || fs.statSync(src).mtimeMs > fs.statSync(dst).mtimeMs) {
          fs.copyFileSync(src, dst);
        }
      } catch {
        /* per-file best effort */
      }
    }
    if (rt && rt.repo_dir !== MATERIALIZED_REPO) {
      rt.repo_dir = MATERIALIZED_REPO;
      try {
        fs.writeFileSync(RUNTIME_JSON, JSON.stringify(rt, null, 2) + "\n", "utf-8");
      } catch {
        /* best effort; resolveRepoDir falls back to MATERIALIZED_REPO anyway */
      }
    }
    console.error(
      `[runtime] pipeline repo was a stray git checkout (${stray}); re-pointed to ` +
        `${MATERIALIZED_REPO}. The checkout was left on disk but is no longer used.`
    );
  } catch (e: any) {
    console.error(`[runtime] healStrayCheckout error: ${e?.message || e}`);
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

// First Chrome/Chromium binary that exists AND is executable, from the same
// paths twitter-backend.sh probes (plus ~/Applications). Returns null when none
// is on disk (the cycle's own PATH-based resolver may still find one).
function detectChromeBin(): string | null {
  const cands = [process.env.BH_CHROME_BIN, ...CHROME_CANDIDATES];
  for (const c of cands) {
    if (!c) continue;
    try {
      fs.accessSync(c, fs.constants.X_OK);
      return c;
    } catch {
      /* not present / not executable; try next */
    }
  }
  return null;
}

// Resolve the Chrome binary the cycle should drive: the provisioned path from
// runtime.json first (catches our ~/Applications fallback install, which the
// shell's _resolve_chrome_bin doesn't scan), then live detection, then the env
// override. null means "let the shell resolve it from PATH."
export function resolveChrome(): string | null {
  const rt = readRuntime();
  if (rt && rt.chrome) {
    try {
      fs.accessSync(rt.chrome, fs.constants.X_OK);
      return rt.chrome;
    } catch {
      /* recorded path went away; fall through to live detection */
    }
  }
  return detectChromeBin() || process.env.BH_CHROME_BIN || null;
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

// Boot-time deterministic provisioning: bring the owned runtime to ready on
// every server start WITHOUT relying on the agent to call `runtime
// action:'install'`. Called from main() on every server start, which the host
// spawns when the plugin loads — so the env starts installing the moment the
// plugin is active, before any agent turn.
//
// Provision-on-boot policy (option a): auto-fire whenever the runtime is not
// ready, fresh install or interrupted one alike. A brand-new install downloads
// and installs everything it needs (uv, Python, venv, deps, Chromium, harness,
// Chrome — whatever the megabytes) up front; an install that died mid-way (a
// failed step, or Claude restarted between steps) resumes. provision() is
// idempotent, so this re-checks done steps and skips them, attempting only
// what's missing. The single deterministic trigger is server boot, not agent
// reasoning. Returns true if it kicked a run. Best-effort, never throws.
export function ensureRuntimeProvisioned(): boolean {
  try {
    if (runtimeReady()) return false; // fully provisioned already
    if (isProvisioning()) return false; // a run is in flight in this process
    // Not ready: provision everything now (startProvisioning is idempotent and
    // re-entrant — a no-op if a run is already in flight).
    startProvisioning();
    return true;
  } catch {
    return false; // best-effort; a boot provision must never break startup
  }
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
    // Every fatal install-step failure (repo unpack, uv, python, venv, deps,
    // chromium, harness, chrome) was previously only written to the local
    // install-progress.json, invisible to us. Report it so a failed runtime
    // install becomes a real Sentry event, tagged with the step that failed.
    const failedStep = progress.steps.find((s) => s.status === "running");
    captureError(new Error(msg), {
      component: "install",
      ...(failedStep ? { step: failedStep.id } : {}),
    });
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
  if (
    looksLikeRepo(process.env.SAPS_REPO_DIR) &&
    !isStrayCheckout(process.env.SAPS_REPO_DIR)
  ) {
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
    // Compatibility symlink: the pipeline scripts (run-twitter-cycle.sh and ~40
    // siblings) hardcode $HOME/social-autoposter for REPO_DIR. A bare .mcpb
    // materializes the repo under the state dir, so those paths don't resolve.
    // Plant ~/social-autoposter -> materialized repo so every hardcoded
    // reference resolves at once. Only when the path is entirely free: never
    // clobber a real npm/git clone or any pre-existing entry (lstat catches
    // dirs, files, and dangling symlinks; existsSync would miss a dangling one).
    try {
      const compat = path.join(os.homedir(), "social-autoposter");
      let occupied = true;
      try {
        fs.lstatSync(compat);
      } catch {
        occupied = false;
      }
      if (!occupied) fs.symlinkSync(MATERIALIZED_REPO, compat);
    } catch {
      /* best effort; SAPS_REPO_DIR + the run-*.sh fallback also resolve the repo */
    }
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
    // Smoke-test the EXACT gate the pipeline's post path runs at use time
    // (twitter_post_plan.py preflight): the owned interpreter must import
    // playwright. The reply step is the only Playwright importer, so a deps
    // sync that left it unimportable was invisible until the first real post
    // died with no_reply_json in production (Karol, 2026-06-22). Fail the
    // install LOUDLY here instead.
    const smoke = await sh(VENV_PYTHON, ["-c", "import playwright"], {
      timeoutMs: 60000,
    });
    if (smoke.code !== 0) {
      return fail(
        `runtime smoke test failed: ${VENV_PYTHON} cannot import playwright ` +
          `(exit ${smoke.code}). ${smoke.out.slice(-400)}`
      );
    }
  }
  setStep("chromium", "done");

  // --- Step 6: browser-harness CLI -----------------------------------------
  // The twitter cycle (run-twitter-cycle.sh) drives Chrome over CDP by shelling
  // out to ~/.local/bin/browser-harness. The npm installer (bin/cli.js) clones
  // browser-use/browser-harness and `uv tool install -e`s it; a bare .mcpb
  // install never provisioned it, so the scan engine was missing and every
  // draft_cycle returned "no candidates". This brings .mcpb to parity with npm.
  setStep("harness", "running");
  {
    // Clone if absent (mkdir parent first), else reuse the checkout.
    if (!fs.existsSync(HARNESS_DIR)) {
      fs.mkdirSync(path.dirname(HARNESS_DIR), { recursive: true });
      const clone = await sh(
        "git",
        ["clone", "--depth", "1", BROWSER_HARNESS_REPO, HARNESS_DIR],
        { timeoutMs: 180000 }
      );
      if (clone.code !== 0) {
        return fail(`browser-harness clone failed (exit ${clone.code}). ${clone.out.slice(-400)}`);
      }
    }
    // Pin to the known-good commit (fetch the exact SHA, hard-reset). Best
    // effort: a transient fetch failure falls back to the existing checkout.
    await sh("git", ["-C", HARNESS_DIR, "fetch", "--depth", "1", "origin", BROWSER_HARNESS_PIN], {
      timeoutMs: 120000,
    });
    await sh("git", ["-C", HARNESS_DIR, "reset", "--hard", "FETCH_HEAD"], { timeoutMs: 60000 });
    // Install the CLI via uv tool (lands at ~/.local/bin/browser-harness).
    // --force so a refreshed source / changed entry point is reinstalled.
    const inst = await sh(uv, ["tool", "install", "--force", "-e", HARNESS_DIR], {
      env: uvEnv,
      timeoutMs: 300000,
    });
    if (inst.code !== 0 || !fs.existsSync(HARNESS_BIN)) {
      return fail(
        `browser-harness CLI install failed (exit ${inst.code}); ${HARNESS_BIN} missing. ` +
          `${inst.out.slice(-400)}`
      );
    }
    // Drop the harness daemon's cached code so the next run loads fresh (best effort).
    await sh(HARNESS_BIN, ["--reload"], { timeoutMs: 30000 });
  }
  setStep("harness", "done", HARNESS_BIN);

  // --- Step 7: Google Chrome (the browser the harness drives over CDP) ------
  // The harness scans/scrapes X by steering a REAL Chrome over CDP. The runtime
  // never installed one (Step 5's Playwright Chromium is a different binary the
  // cycle doesn't use), so a Chrome-less Mac passed every step then died with
  // "no Chrome/Chromium binary found." Detect an existing Chrome first; if none,
  // install on macOS via the official DMG using plain `cp` (no sudo, no GUI
  // prompt): try /Applications (group-writable for admins), else ~/Applications
  // (always user-writable). The resolved path is recorded for BH_CHROME_BIN.
  setStep("chrome", "running");
  let chromeBin = detectChromeBin();
  if (chromeBin) {
    setStep("chrome", "done", `found: ${chromeBin}`);
  } else if (process.platform === "darwin") {
    // One self-contained script: download DMG, mount, copy to the first
    // writable Applications dir, unmount, clean up. Echoes INSTALLED:<path> on
    // success so we record the exact binary (handles the /Applications vs
    // ~/Applications branch without re-detecting spaces-in-path quirks).
    const script = [
      "set -e",
      'DMG="$(mktemp -t saps-gchrome).dmg"',
      'MNT="$(mktemp -d -t saps-gchrome-mnt)"',
      'cleanup() { hdiutil detach "$MNT" -quiet 2>/dev/null || true; rm -f "$DMG"; rmdir "$MNT" 2>/dev/null || true; }',
      "trap cleanup EXIT",
      `curl -fsSL -o "$DMG" "${GOOGLE_CHROME_DMG}"`,
      'hdiutil attach "$DMG" -nobrowse -quiet -mountpoint "$MNT"',
      'if cp -R "$MNT/Google Chrome.app" /Applications/ 2>/dev/null; then',
      '  echo "INSTALLED:/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"',
      "else",
      '  mkdir -p "$HOME/Applications"',
      '  cp -R "$MNT/Google Chrome.app" "$HOME/Applications/"',
      '  echo "INSTALLED:$HOME/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"',
      "fi",
    ].join("\n");
    const r = await bash(script, 300000);
    const m = r.out.match(/INSTALLED:(.+)/);
    const installed = m ? m[1].trim() : "";
    if (r.code === 0 && installed) {
      try {
        fs.accessSync(installed, fs.constants.X_OK);
        chromeBin = installed;
      } catch {
        /* copied but not executable? fall through to re-detect */
      }
    }
    if (!chromeBin) chromeBin = detectChromeBin();
    if (chromeBin) {
      setStep("chrome", "done", `installed: ${chromeBin}`);
    } else {
      return fail(
        `Google Chrome install failed (exit ${r.code}). The scanner drives ` +
          `Chrome over CDP and none was found. Install Google Chrome from ` +
          `https://www.google.com/chrome/ and re-run setup. ${r.out.slice(-300)}`
      );
    }
  } else {
    // Non-macOS (managed Linux VMs): we don't auto-install. The cycle's own
    // PATH-based _resolve_chrome_bin may still find one at run time, so this is
    // a soft note, not a hard fail.
    setStep(
      "chrome",
      "done",
      "no Chrome found; on non-macOS the host must provide google-chrome/chromium on PATH"
    );
  }

  // --- Step 8: menu bar app (macOS status-bar mini-dashboard) --------------
  // Non-fatal: a menu bar failure must never block a usable runtime, so on any
  // problem we mark the step errored and still persist runtime.json below.
  setStep("menubar", "running");
  if (process.platform !== "darwin") {
    setStep("menubar", "done", "skipped (macOS only)");
  } else if (menubarStopped()) {
    // A runtime repair/re-provision must not resurrect a tray the user
    // explicitly quit; an explicit start clears the flag first.
    setStep("menubar", "done", "skipped (user stopped the menu bar)");
  } else {
    const mb = await installMenubar(uv, uvEnv, VENV_PYTHON);
    setStep("menubar", mb.ok ? "done" : "error", mb.detail);
    // Non-fatal step, so the only prior signal of a menu bar install failure was
    // a local install-progress.json entry (invisible to us). Report it so "menu
    // bar didn't start" becomes a real Sentry event with the failing detail.
    if (!mb.ok) {
      captureError(new Error(`menubar install failed: ${mb.detail}`), {
        component: "menubar",
        phase: "install",
      });
    }
  }

  // --- Persist the result ---------------------------------------------------
  const info: RuntimeInfo = {
    python: VENV_PYTHON,
    uv,
    python_version: PYTHON_VERSION,
    repo_dir: resolvedRepo,
    chrome: chromeBin || undefined,
    ready: true,
    provisioned_at: new Date().toISOString(),
    // Stamp the just-materialized pipeline version so ensurePipelineCurrent()
    // can detect a later update without re-extracting on every boot. Only
    // meaningful for the materialized (.mcpb) repo, not a SAPS_REPO_DIR clone.
    pipeline_version:
      resolvedRepo === MATERIALIZED_REPO ? bundledVersion() ?? undefined : undefined,
  };
  fs.mkdirSync(STATE_DIR, { recursive: true });
  fs.writeFileSync(RUNTIME_JSON, JSON.stringify(info, null, 2) + "\n", "utf-8");

  progress.running = false;
  progress.done = true;
  progress.ok = true;
  writeProgress(progress);
  return progress;
}

// ---------------------------------------------------------------------------
// Menu bar app provisioning.
//
// installMenubar copies the rumps app to a stable state-dir location, installs
// rumps into the owned venv, and (re)loads a KeepAlive LaunchAgent. ensureMenubar
// is the cheap, idempotent boot-time path: a no-op when it's already installed
// and loaded, so existing installs pick up the menu bar on the next Claude
// restart without re-running the whole provision.
// ---------------------------------------------------------------------------

// The bundled menu bar source: <ext>/menubar in a packed .mcpb (dist/runtime.js
// -> ../menubar), the same path for a tsc dev build (mcp/dist -> mcp/menubar),
// and the repo clone for an npm install.
function menubarSourceDir(): string | null {
  const candidates = [
    path.join(__dirname, "..", "menubar"),
    path.join(resolveRepoDir(), "mcp", "menubar"),
  ];
  for (const c of candidates) {
    try {
      if (fs.existsSync(path.join(c, "s4l_menubar.py"))) return c;
    } catch {
      /* try next */
    }
  }
  return null;
}

// KeepAlive { SuccessfulExit: false } so a clean Quit (exit 0) stays quit until
// next login (RunAtLoad), while a crash relaunches. No StartInterval — this is a
// long-running agent, not a cron job.
function menubarPlistXml(python: string): string {
  const menubarPath = [
    path.dirname(python),
    path.join(os.homedir(), ".local", "bin"),
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
  ].join(":");
  return `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
\t<key>Label</key>
\t<string>${MENUBAR_LABEL}</string>
\t<key>ProgramArguments</key>
\t<array>
\t\t<string>${python}</string>
\t\t<string>${MENUBAR_ENTRY}</string>
\t</array>
\t<key>RunAtLoad</key>
\t<true/>
\t<key>KeepAlive</key>
\t<dict>
\t\t<key>SuccessfulExit</key>
\t\t<false/>
\t</dict>
\t<key>ProcessType</key>
\t<string>Interactive</string>
\t<key>StandardOutPath</key>
\t<string>${MENUBAR_OUT_LOG}</string>
\t<key>StandardErrorPath</key>
\t<string>${MENUBAR_ERR_LOG}</string>
\t<key>EnvironmentVariables</key>
\t<dict>
\t\t<key>PATH</key>
\t\t<string>${menubarPath}</string>
\t\t<key>HOME</key>
\t\t<string>${os.homedir()}</string>
\t\t<key>SAPS_STATE_DIR</key>
\t\t<string>${STATE_DIR}</string>
\t\t<key>SAPS_PYTHON</key>
\t\t<string>${python}</string>
\t\t<key>SAPS_REPO_DIR</key>
\t\t<string>${resolveRepoDir()}</string>
\t</dict>
</dict>
</plist>
`;
}

export async function menubarLoaded(): Promise<boolean> {
  const r = await sh("launchctl", ["list"], { timeoutMs: 10_000 });
  return r.out.includes(MENUBAR_LABEL);
}

// Is the menu bar app expected to be up right now? Only meaningful once the
// runtime is provisioned (the LaunchAgent isn't installed before that) and only
// on macOS. Any failure defaults to `true` so the panel never shows a spurious
// "menu bar down" banner from a flaky launchctl read.
export async function menubarRunning(): Promise<boolean> {
  if (process.platform !== "darwin") return true;
  if (!runtimeReady()) return true;
  // User explicitly quit the tray: it is down ON PURPOSE, so report "fine" —
  // the dashboard banner must not nag about a state the user chose.
  if (menubarStopped()) return true;
  try {
    return await menubarLoaded();
  } catch {
    return true;
  }
}

export async function installMenubar(
  uv: string,
  uvEnv: NodeJS.ProcessEnv,
  python: string
): Promise<{ ok: boolean; detail: string }> {
  if (process.platform !== "darwin") return { ok: true, detail: "skipped (macOS only)" };
  const src = menubarSourceDir();
  if (!src) return { ok: false, detail: "menu bar source not found in bundle" };

  // 1. Copy the python to a stable location that survives extension updates.
  try {
    fs.mkdirSync(MENUBAR_DIR, { recursive: true });
    for (const f of fs.readdirSync(src)) {
      if (f.endsWith(".py")) {
        fs.copyFileSync(path.join(src, f), path.join(MENUBAR_DIR, f));
      }
    }
  } catch (e: any) {
    return { ok: false, detail: `copy failed: ${e?.message || e}` };
  }
  if (!fs.existsSync(MENUBAR_ENTRY)) return { ok: false, detail: "entry not copied" };

  // 2. Install rumps into the owned venv (pulls pyobjc-framework-Cocoa).
  const r = await sh(uv, ["pip", "install", "--python", python, "rumps"], {
    env: uvEnv,
    timeoutMs: 300_000,
  });
  if (r.code !== 0) {
    return { ok: false, detail: `rumps install failed (exit ${r.code}). ${r.out.slice(-300)}` };
  }

  // 3. Write + (re)load the LaunchAgent (bootout any prior instance first).
  try {
    fs.mkdirSync(path.dirname(MENUBAR_PLIST), { recursive: true });
    fs.writeFileSync(MENUBAR_PLIST, menubarPlistXml(python), "utf-8");
  } catch (e: any) {
    return { ok: false, detail: `plist write failed: ${e?.message || e}` };
  }
  const uid = process.getuid ? process.getuid() : 0;
  await sh("launchctl", ["bootout", `gui/${uid}/${MENUBAR_LABEL}`], { timeoutMs: 15_000 });
  let lr = await sh("launchctl", ["bootstrap", `gui/${uid}`, MENUBAR_PLIST], {
    timeoutMs: 15_000,
  });
  if (lr.code !== 0) {
    lr = await sh("launchctl", ["load", MENUBAR_PLIST], { timeoutMs: 15_000 });
  }
  return { ok: true, detail: MENUBAR_ENTRY };
}

export async function ensureMenubar(): Promise<{
  ok: boolean;
  detail: string;
  skipped?: boolean;
}> {
  if (process.platform !== "darwin") return { ok: true, skipped: true, detail: "non-macOS" };
  // The user clicked Quit in the tray: stay stopped across Claude restarts,
  // regardless of runtime state. Explicit start paths (restart_menubar tool,
  // queue_setup) clear the flag before calling this.
  if (menubarStopped()) {
    return { ok: true, skipped: true, detail: "user stopped the menu bar (stopped.flag)" };
  }
  if (!runtimeReady()) return { ok: false, skipped: true, detail: "runtime not ready" };
  if (
    fs.existsSync(MENUBAR_ENTRY) &&
    fs.existsSync(MENUBAR_PLIST) &&
    (await menubarLoaded())
  ) {
    // Installed — but refresh the stable copy if the bundled menu bar is newer.
    // An .mcpb update ships new menu bar code, yet the running menu bar runs from
    // this stable copy (so it survives extension replacement); without this it
    // would stay the version it first installed at. Cheap: content-compare, and
    // a kickstart only when something actually changed.
    return await refreshMenubarIfStale();
  }
  const uv = findUv();
  if (!uv) return { ok: false, detail: "uv not found" };
  const uvEnv: NodeJS.ProcessEnv = {
    UV_PYTHON_INSTALL_DIR: path.join(RUNTIME_DIR, "python"),
  };
  return installMenubar(uv, uvEnv, resolvePython());
}

// Re-copy the bundled menu bar python into the stable dir when it differs from
// what's installed, and kickstart the launchd job so the running menu bar picks
// up the new code. Content-compare keeps this a no-op on an unchanged boot, so
// it's cheap to call on every ensureMenubar(). This is what makes menu bar
// changes (e.g. the "Please update now" button) actually ship on an .mcpb update.
async function refreshMenubarIfStale(): Promise<{ ok: boolean; detail: string; skipped?: boolean }> {
  const src = menubarSourceDir();
  if (!src) return { ok: true, skipped: true, detail: "no bundle source to refresh from" };
  let changed = false;
  try {
    for (const f of fs.readdirSync(src)) {
      if (!f.endsWith(".py")) continue;
      const s = fs.readFileSync(path.join(src, f));
      const dPath = path.join(MENUBAR_DIR, f);
      const d = fs.existsSync(dPath) ? fs.readFileSync(dPath) : Buffer.alloc(0);
      if (!s.equals(d)) {
        fs.writeFileSync(dPath, s);
        changed = true;
      }
    }
  } catch (e: any) {
    return { ok: true, skipped: true, detail: `menu bar refresh check failed: ${e?.message || e}` };
  }
  // The plist bakes SAPS_REPO_DIR/SAPS_PYTHON at write time and was historically
  // never rewritten, so a box whose repo resolution changed (e.g. a stray git
  // checkout healed at boot) kept feeding the menu bar (and every snapshot.py
  // it spawns) the stale repo, which is what pins the displayed version and the
  // update banner. Regenerate and compare; a drifted plist needs a full
  // bootout/bootstrap (env changes don't apply on kickstart).
  let plistChanged = false;
  try {
    const want = menubarPlistXml(resolvePython());
    let have = "";
    try {
      have = fs.readFileSync(MENUBAR_PLIST, "utf-8");
    } catch {
      have = "";
    }
    if (want !== have) {
      fs.mkdirSync(path.dirname(MENUBAR_PLIST), { recursive: true });
      fs.writeFileSync(MENUBAR_PLIST, want, "utf-8");
      plistChanged = true;
    }
  } catch {
    /* best effort; a failed plist refresh must not block the .py refresh */
  }
  if (!changed && !plistChanged)
    return { ok: true, skipped: true, detail: "menu bar already current" };
  const uid = process.getuid ? process.getuid() : 0;
  if (plistChanged) {
    await sh("launchctl", ["bootout", `gui/${uid}/${MENUBAR_LABEL}`], { timeoutMs: 15_000 });
    const lr = await sh("launchctl", ["bootstrap", `gui/${uid}`, MENUBAR_PLIST], {
      timeoutMs: 15_000,
    });
    if (lr.code !== 0) {
      await sh("launchctl", ["load", MENUBAR_PLIST], { timeoutMs: 15_000 });
    }
    return { ok: true, detail: "menu bar plist refreshed + agent reloaded" };
  }
  await sh("launchctl", ["kickstart", "-k", `gui/${uid}/${MENUBAR_LABEL}`], { timeoutMs: 15_000 });
  return { ok: true, detail: "menu bar refreshed + restarted to bundled version" };
}
