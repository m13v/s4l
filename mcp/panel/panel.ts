/**
 * Social Autoposter control panel (MCP Apps UI).
 *
 * Renders inside the host's sandboxed iframe. It does NOT duplicate any pipeline
 * logic: every button calls one of the server's existing tools (draft_cycle,
 * autopilot, setup, get_stats) through the host via app.callServerTool, and the
 * host pushes results back. First paint comes from the `panel` tool's own
 * structuredContent snapshot; Refresh re-reads via setup(status) + autopilot.
 */
import {
  applyDocumentTheme,
  applyHostFonts,
  applyHostStyleVariables,
  type McpUiHostContext,
} from "@modelcontextprotocol/ext-apps";
import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { createBridge } from "./bridge";
import "./panel.css";

interface ProjStatus { name: string; ready: boolean; missing_required: string[] }
type StepStatus = "pending" | "running" | "done" | "error";
interface ProgressStep { id: string; label: string; status: StepStatus; detail?: string }
interface InstallProgress {
  running: boolean;
  done: boolean;
  ok: boolean;
  error?: string;
  steps: ProgressStep[];
}
interface Snapshot {
  projects: ProjStatus[];
  projects_ready: number;
  projects_total: number;
  x_connected: boolean;
  x_state: string;
  x_handle?: string | null;
  autopilot_on: boolean;
  auto_update_on?: boolean;
  version: string;
  latest_version: string | null;
  update_available: boolean;
  runtime_ready: boolean;
  runtime_provisioning?: boolean;
}

// ---- result parsing -------------------------------------------------------
// Tools return data either as structuredContent or as a JSON string in the
// first text content block. Normalize both into a plain object.
function parseResult(result: CallToolResult): any {
  const sc = (result as any).structuredContent;
  if (sc && typeof sc === "object") {
    if (typeof sc.snapshot === "string") {
      try { return JSON.parse(sc.snapshot); } catch { /* fall through */ }
    }
    return sc;
  }
  const block = (result.content || []).find((c: any) => c.type === "text") as any;
  if (block?.text) {
    try { return JSON.parse(block.text); } catch { return { _raw: block.text }; }
  }
  return {};
}

// ---- DOM ------------------------------------------------------------------
const $ = (id: string) => document.getElementById(id)!;
const verEl = $("ver");
const stProj = $("st-proj"), stProjSub = $("st-proj-sub");
const stX = $("st-x"), stXSub = $("st-x-sub");
const stApSub = $("st-ap-sub");
const btnSetup = $("btn-setup") as HTMLButtonElement;
const btnDraft = $("btn-draft") as HTMLButtonElement;
const apToggle = $("ap-checkbox") as HTMLInputElement;
const statsGrid = $("stats-grid");
const logEl = $("log");
const installCard = $("install-card");
const installSteps = $("install-steps");
const installErr = $("install-err");
const btnInstall = $("btn-install") as HTMLButtonElement;
const btnLive = $("btn-live") as HTMLButtonElement;
const btnLiveStop = $("btn-live-stop") as HTMLButtonElement;
const btnLiveFront = $("btn-live-front") as HTMLButtonElement;
const liveStatus = $("live-status");
const liveImg = $("live-img") as HTMLImageElement;
const configEditor = $("config-editor") as HTMLTextAreaElement;
const configStatus = $("config-status");
const btnConfigLoad = $("btn-config-load") as HTMLButtonElement;
const btnConfigSave = $("btn-config-save") as HTMLButtonElement;
const btnConfigCancel = $("btn-config-cancel") as HTMLButtonElement;

let state: Snapshot | null = null;
let installPolling = false; // guard against overlapping poll loops
let updating = false; // guard against double-firing the in-header update button
let configLoaded = ""; // last-loaded raw config, for dirty-check + cancel

function log(msg: string) { logEl.textContent = msg; }

// Glyph for each step status. Grayscale only — meaning carried by symbol, never
// color (matches the panel palette rule).
function stepGlyph(s: StepStatus): string {
  switch (s) {
    case "done": return "\u2713";      // check
    case "running": return "\u2026";   // ellipsis (in progress)
    case "error": return "\u00d7";     // cross
    default: return "\u00b7";          // middot (pending)
  }
}

function renderInstallProgress(p: InstallProgress | null) {
  if (!p || !Array.isArray(p.steps)) { installSteps.innerHTML = ""; return; }
  installSteps.innerHTML = p.steps
    .map((s) => {
      const detail = s.detail && s.status !== "pending"
        ? ` <span class="detail">${s.status === "error" ? s.detail : ""}</span>`
        : "";
      return `<li class="${s.status}"><span class="glyph">${stepGlyph(s.status)}</span>` +
        `<span>${s.label}${detail}</span></li>`;
    })
    .join("");
  if (p.error) { installErr.textContent = p.error; installErr.hidden = false; }
  else installErr.hidden = true;
}

function render() {
  if (!state) return;
  // Version + update button. When an update is available the badge is an actual
  // button that installs the latest release (delegated click on verEl, since the
  // button is recreated on every render).
  verEl.innerHTML = state.update_available && state.latest_version
    ? `v${state.version} \u00b7 <button id="btn-update" class="update-btn">Update to ${state.latest_version}</button>`
    : `v${state.version}`;

  // Runtime install gate: until the owned Python/Chromium runtime exists, the
  // Install card is the primary (and only enabled) surface. Everything else is
  // disabled because no pipeline tool can run without the interpreter.
  const needsRuntime = !state.runtime_ready;
  installCard.hidden = !needsRuntime;

  // Projects.
  stProj.textContent = `${state.projects_ready}/${state.projects_total}`;
  stProjSub.textContent = state.projects_total === 0
    ? "none configured"
    : state.projects.map((p) => p.name + (p.ready ? "" : " (incomplete)")).join(", ");

  // X / Twitter. When connected, prefer showing the resolved @handle (the
  // account we post as); fall back to the raw state string. A null handle while
  // connected just means it wasn't resolved this read — never "missing".
  stX.textContent = state.x_connected ? "Connected" : "Not connected";
  const handle = state.x_handle
    ? (state.x_handle.startsWith("@") ? state.x_handle : "@" + state.x_handle)
    : "";
  stXSub.textContent = state.x_connected
    ? (handle || state.x_state || "")
    : (state.x_state || "");

  // Autopilot. Rendered as an on/off switch in the status card rather than a
  // button — checked == hands-free posting is live.
  apToggle.checked = !!state.autopilot_on;
  stApSub.textContent = state.autopilot_on
    ? (state.auto_update_on ? "on \u00b7 auto-update on" : "on")
    : "off";

  // Gate actions on readiness. Nothing below works without the runtime, so when
  // it's missing every action is disabled and the Install card carries the only
  // live button.
  const hasReady = state.projects_ready > 0;
  btnSetup.disabled = needsRuntime;
  btnDraft.disabled = needsRuntime || !hasReady;
  apToggle.disabled = needsRuntime || !hasReady;
  // When nothing is configured yet, Set up is the obvious next action, so
  // promote it to the primary (filled) style and demote draft (which is
  // disabled anyway). Once a project is ready, draft regains the emphasis. While
  // the runtime is missing, neither gets emphasis — the Install button does.
  const needsSetup = !hasReady;
  btnSetup.classList.toggle("primary", !needsRuntime && needsSetup);
  btnDraft.classList.toggle("primary", !needsRuntime && !needsSetup);
}

function applyState(snap: Partial<Snapshot>) {
  state = { ...(state || {} as Snapshot), ...snap } as Snapshot;
  render();
}

// Map setup(status) shape -> snapshot fields.
function fromSetupStatus(o: any): Partial<Snapshot> {
  const projects: ProjStatus[] = Array.isArray(o.projects) ? o.projects : [];
  return {
    projects,
    projects_total: projects.length,
    projects_ready: projects.filter((p) => p.ready).length,
    x_connected: !!o.x_connected,
    x_state: o.x_state || "",
    x_handle: o.x_handle ?? null,
    version: o.mcp_version || state?.version || "",
    latest_version: o.latest_version ?? null,
    update_available: !!o.update_available,
  };
}

// ---- App wiring -----------------------------------------------------------
// Picks the MCP Apps bridge (inline render) or the HTTP bridge (localhost
// fallback) based on the flag the loopback server injects. Same code either way.
const app = createBridge();

function applyHostContext(ctx: McpUiHostContext) {
  if (ctx.theme) applyDocumentTheme(ctx.theme);
  if (ctx.styles?.variables) applyHostStyleVariables(ctx.styles.variables);
  if (ctx.styles?.css?.fonts) applyHostFonts(ctx.styles.css.fonts);
}
app.onhostcontextchanged = applyHostContext;
app.onerror = (e) => console.error(e);

// The `panel` tool that spawned this view returns the initial snapshot.
app.ontoolresult = (result) => {
  const data = parseResult(result as CallToolResult);
  if (data && typeof data.projects_total === "number") {
    applyState(data as Snapshot);
    if (data.runtime_ready) {
      // Stats need the runtime; load them once it's confirmed ready.
      void loadStats();
      if (!data.x_connected) void populateXSources();
    } else if (data.runtime_provisioning) {
      // An install is already underway (another surface / prior open) — resume
      // following it without waiting for a click.
      void pollInstall();
    }
  }
};

async function call(name: string, args: Record<string, unknown> = {}): Promise<any> {
  const res = await app.callServerTool({ name, arguments: args });
  return parseResult(res as CallToolResult);
}

// Populate the "import from" dropdown with the browser/profile to pull the X
// session from. Read-only detection (no keychain prompt). The profile that
// already holds an x.com session is marked and pre-selected, so the user (and
// the keychain) only deal with the right browser instead of all of them.
let xSourcesLoaded = false;
async function populateXSources(force = false) {
  if (xSourcesLoaded && !force) return;
  try {
    const r = await call("setup", { action: "detect_x_sources" });
    const sources: Array<{ spec: string; label: string; x_session: boolean }> =
      Array.isArray(r.sources) ? r.sources : [];
    if (!sources.length) { xSourceSel.hidden = true; return; }
    xSourceSel.innerHTML = sources
      .map((s) => {
        const tag = s.x_session ? "  (X session found)" : "";
        const sel = s.spec === r.recommended ? " selected" : "";
        return `<option value="${s.spec}"${sel}>${s.label}${tag}</option>`;
      })
      .join("");
    xSourcesLoaded = true;
    // Only reveal it when X isn't connected (nothing to import otherwise).
    xSourceSel.hidden = !!state?.x_connected;
  } catch {
    xSourceSel.hidden = true;
  }
}

async function refresh() {
  log("Refreshing\u2026");
  try {
    // install_status is cheap and tells us whether the runtime gate is cleared;
    // pull it alongside the usual status so a refresh re-evaluates the gate.
    const [setupStatus, ap, rt] = await Promise.all([
      call("setup", { status: true }),
      call("autopilot", { action: "status" }),
      call("install_status").catch(() => ({})),
    ]);
    applyState({
      ...fromSetupStatus(setupStatus),
      autopilot_on: !!ap.loaded,
      auto_update_on: !!ap.auto_update_loaded,
      ...(typeof rt.runtime_ready === "boolean" ? { runtime_ready: rt.runtime_ready } : {}),
    });
    if (state && !state.runtime_ready && rt.provisioning) pollInstall();
    // Once the runtime is ready and X still isn't connected, offer the
    // import-source dropdown (read-only detection, no keychain prompt).
    if (state?.runtime_ready && !state.x_connected) void populateXSources();
    log("");
    void loadStats();
  } catch (e: any) {
    log("Refresh failed: " + (e?.message || e));
  }
}

// Poll install_status until the runtime is ready or the install errors out.
// Single active loop (installPolling guard); the panel renders each step's
// progress as it lands.
async function pollInstall() {
  if (installPolling) return;
  installPolling = true;
  btnInstall.disabled = true;
  btnInstall.textContent = "Installing\u2026";
  try {
    for (;;) {
      const rt = await call("install_status").catch(() => ({} as any));
      renderInstallProgress(rt.progress ?? null);
      if (rt.runtime_ready) {
        applyState({ runtime_ready: true });
        log("Runtime installed; you're ready to set up.");
        void refresh();
        return;
      }
      const p: InstallProgress | null = rt.progress ?? null;
      if (p && p.done && !p.ok) {
        btnInstall.disabled = false;
        btnInstall.textContent = "Retry install";
        log("Install failed; see the step above, then Retry.");
        return;
      }
      await new Promise((r) => setTimeout(r, 1500));
    }
  } finally {
    installPolling = false;
  }
}

async function loadStats() {
  try {
    const data = await call("get_stats", { days: 7 });
    const proj = Array.isArray(data.projects) ? data.projects[0] : null;
    const p = proj?.posts;
    if (!p) { statsGrid.innerHTML = `<div class="muted">No stats yet.</div>`; return; }
    const cells: Array<[string, number | string]> = [
      ["Posts", p.total ?? 0],
      ["Active", p.active ?? 0],
      ["Views", p.views_period_total ?? p.views ?? 0],
      ["Replies", p.comments_period_total ?? p.comments ?? 0],
      ["Clicks", p.post_clicks_period_total ?? 0],
    ];
    statsGrid.innerHTML = cells
      .map(([l, n]) => `<div class="stat"><div class="n">${n}</div><div class="l">${l}</div></div>`)
      .join("");
  } catch (e: any) {
    statsGrid.innerHTML = `<div class="muted">Stats unavailable: ${e?.message || e}</div>`;
  }
}

function busy(btn: HTMLButtonElement, label: string, fn: () => Promise<void>) {
  const prev = btn.textContent;
  btn.disabled = true; btn.textContent = label;
  // Re-enable BEFORE render() so render() can re-apply readiness gating to the
  // buttons it owns; buttons it doesn't own (Refresh, config) just come back on.
  fn().finally(() => { btn.disabled = false; btn.textContent = prev; render(); });
}

// ---- button handlers ------------------------------------------------------
// Setup is the one action that genuinely needs the model in the loop (it reads
// setup/SKILL.md and drives the browser-login wizard). Unlike the other buttons
// (which call server tools directly via callServerTool, no model involved), this
// injects a real user turn into the host conversation via sendMessage, so Claude
// takes a turn and runs the wizard right there in the chat.
btnSetup.addEventListener("click", () => busy(btnSetup, "Starting\u2026", async () => {
  log("Asking Claude to run setup\u2026");
  try {
    const res = await app.sendMessage({
      role: "user",
      content: [{
        type: "text",
        text:
          "Run the social autoposter setup wizard: configure my project " +
          "(website, what I do, who to target, brand voice) and connect my X/Twitter account. " +
          "Walk me through it step by step.",
      }],
    });
    if ((res as any)?.isError) log("The host rejected the setup request \u2014 type \u201cset up social autoposter\u201d in the chat instead.");
    else log("Setup started in the chat \u2014 follow the prompts there, then hit Refresh.");
  } catch (e: any) {
    log("Couldn\u2019t start setup: " + (e?.message || e));
  }
}));

btnDraft.addEventListener("click", () => busy(btnDraft, "Drafting\u2026", async () => {
  log("Drafting\u2026 the draft list appears in the chat for review.");
  try {
    const r = await call("draft_cycle");
    const n = r.drafted ?? 0;
    if (n) log(`Drafted ${n} \u2014 review them in the chat and choose which to post.`);
    else log("No drafts produced.");
    void loadStats();
  } catch (e: any) { log("Draft cycle failed: " + (e?.message || e)); }
}));

// The autopilot switch flips state directly. We disable it during the round-trip
// and revert the checkbox if the tool call fails, so it never shows a state the
// server didn't confirm.
apToggle.addEventListener("change", async () => {
  if (!state) return;
  const desired = apToggle.checked;
  const action = desired ? "enable" : "disable";
  apToggle.disabled = true;
  log(desired ? "Enabling autopilot\u2026" : "Disabling autopilot\u2026");
  try {
    const r = await call("autopilot", { action });
    const on = action === "enable" ? !!(r.autopilot?.loaded) : !(r.autopilot_unloaded);
    applyState({ autopilot_on: on });
    log(`Autopilot ${on ? "enabled" : "disabled"}.`);
  } catch (e: any) {
    apToggle.checked = state.autopilot_on; // revert to last confirmed state
    log("Autopilot toggle failed: " + (e?.message || e));
  } finally {
    render(); // re-applies readiness gating to the switch
  }
});

btnX.addEventListener("click", () => busy(btnX, "Working\u2026", async () => {
  try {
    if (!xConfirmPending) {
      const r = await call("setup", { action: "connect_x" });
      if (r.already_connected) { applyState({ x_connected: true }); log("X already connected."); return; }
      xConfirmPending = true;
      btnX.textContent = "Confirm: import X session";
      log(r.what_will_happen || "This imports your x.com cookies into the autoposter's browser. Click again to confirm.");
    } else {
      // Pass the chosen browser/profile (if the dropdown is populated) so the
      // import targets the right one and the keychain prompts only for it.
      const chosen = !xSourceSel.hidden && xSourceSel.value ? xSourceSel.value : undefined;
      const r = await call("setup", {
        action: "connect_x",
        confirm: true,
        ...(chosen ? { x_source: chosen } : {}),
      });
      xConfirmPending = false;
      applyState({ x_connected: !!r.connected, x_state: r.state || "" });
      log(r.summary || (r.connected ? "X connected." : "X not connected \u2014 see chat."));
      // The connect result doesn't carry the @handle; re-read status so the X
      // card shows which account we're now posting as.
      if (r.connected) void refresh();
    }
  } catch (e: any) { xConfirmPending = false; log("Connect X failed: " + (e?.message || e)); }
}));

btnInstall.addEventListener("click", async () => {
  installErr.hidden = true;
  btnInstall.disabled = true;
  btnInstall.textContent = "Starting\u2026";
  log("Installing the runtime \u2014 this is a one-time download (~150MB+).");
  try {
    const r = await call("install_runtime");
    if (r.runtime_ready) { applyState({ runtime_ready: true }); void refresh(); return; }
    renderInstallProgress(r.progress ?? null);
    void pollInstall();
  } catch (e: any) {
    btnInstall.disabled = false;
    btnInstall.textContent = "Retry install";
    installErr.textContent = "Couldn't start install: " + (e?.message || e);
    installErr.hidden = false;
  }
});

// ---- config view / edit ---------------------------------------------------
// Read-only by default; the textarea opens on "View config" and becomes
// editable. Save round-trips through the `config` tool, which validates JSON and
// writes a timestamped backup before overwriting config.json.
function showConfigEditing(on: boolean) {
  configEditor.hidden = !on;
  btnConfigSave.hidden = !on;
  btnConfigCancel.hidden = !on;
  btnConfigLoad.hidden = on;
}

btnConfigLoad.addEventListener("click", () => busy(btnConfigLoad, "Loading\u2026", async () => {
  configStatus.textContent = "";
  try {
    const r = await call("config", { action: "get" });
    if (!r.ok) { configStatus.textContent = "Couldn't load config: " + (r.error || "unknown error"); return; }
    configLoaded = r.content || "";
    configEditor.value = configLoaded;
    showConfigEditing(true);
    configStatus.textContent = `Loaded ${r.bytes ?? configLoaded.length} bytes. Edit and Save, or Cancel.`;
    // The config card sits near the bottom of a tall panel; in a constrained
    // host viewport the freshly-opened editor lands below the fold, so the click
    // looks like a no-op. Pull it into view so the user actually sees it open.
    try { configEditor.scrollIntoView({ behavior: "smooth", block: "center" }); } catch { /* older host */ }
  } catch (e: any) {
    configStatus.textContent = "Couldn't load config: " + (e?.message || e);
  }
}));

btnConfigCancel.addEventListener("click", () => {
  configEditor.value = configLoaded;
  showConfigEditing(false);
  configStatus.textContent = "";
});

btnConfigSave.addEventListener("click", () => busy(btnConfigSave, "Saving\u2026", async () => {
  const content = configEditor.value;
  if (content === configLoaded) { configStatus.textContent = "No changes to save."; return; }
  // Client-side parse first so an obvious typo is caught before the round-trip.
  try { JSON.parse(content); }
  catch (e: any) { configStatus.textContent = "Invalid JSON, not saved: " + (e?.message || e); return; }
  try {
    const r = await call("config", { action: "save", content });
    if (!r.ok) { configStatus.textContent = "Save failed: " + (r.error || "unknown error"); return; }
    configLoaded = content;
    showConfigEditing(false);
    configStatus.textContent = `Saved ${r.bytes ?? content.length} bytes. Backup: ${r.backup || "(none)"}`;
    // Project list / handle may have changed; re-read status.
    void refresh();
  } catch (e: any) {
    configStatus.textContent = "Save failed: " + (e?.message || e);
  }
}));

// ---- live browser view ----------------------------------------------------
// Polls the show_browser_to_user tool, which keeps a CDP screencast of the
// active managed Chrome and returns the newest frame as a data: URL. We just
// swap it into an <img> on a short interval — the screencast runs at ~30fps on
// the server, the panel refresh rate is bounded by the tool round-trip.
let liveTimer: ReturnType<typeof setInterval> | null = null;
let liveTicking = false;

async function liveTick() {
  if (liveTicking) return; // don't stack calls if one round-trip is slow
  liveTicking = true;
  try {
    const r = await call("show_browser_to_user", { action: "frame" });
    if (!r.ok) {
      liveStatus.textContent = r.message || "No active browser session.";
      stopLive(false);
      return;
    }
    if (r.frame) { liveImg.src = r.frame; liveImg.hidden = false; }
    const where = r.title || r.url || (r.port ? "port " + r.port : "");
    liveStatus.textContent = r.frame
      ? "Watching" + (where ? ": " + where : "")
      : "Connecting\u2026";
  } catch (e: any) {
    liveStatus.textContent = "Live view error: " + (e?.message || e);
  } finally {
    liveTicking = false;
  }
}

function startLive() {
  btnLive.hidden = true;
  btnLiveStop.hidden = false;
  liveStatus.textContent = "Attaching to the browser\u2026";
  void liveTick();
  liveTimer = setInterval(liveTick, 450);
}

function stopLive(tellServer = true) {
  if (liveTimer != null) { clearInterval(liveTimer); liveTimer = null; }
  btnLive.hidden = false;
  btnLiveStop.hidden = true;
  liveImg.hidden = true;
  liveImg.removeAttribute("src");
  if (tellServer) void call("show_browser_to_user", { action: "stop" }).catch(() => {});
}

btnLive.addEventListener("click", startLive);
btnLiveStop.addEventListener("click", () => { stopLive(true); liveStatus.textContent = ""; });

// "Bring to front": close the in-panel live view and raise the real browser
// window above everything so the user can interact with it directly.
btnLiveFront.addEventListener("click", () => busy(btnLiveFront, "Bringing\u2026", async () => {
  stopLive(true);
  const r = await call("show_browser_to_user", { action: "front" });
  liveStatus.textContent = r?.ok
    ? "Brought the browser to the front."
    : (r?.message || "Couldn't bring the browser to the front.");
}));

btnRefresh.addEventListener("click", () => busy(btnRefresh, "Refreshing\u2026", refresh));

// ---- boot -----------------------------------------------------------------
app.connect().then(() => {
  const ctx = app.getHostContext();
  if (ctx) applyHostContext(ctx);
  // Stats load from ontoolresult once the first snapshot confirms the runtime is
  // ready (the pipeline can't run without it), so nothing to do here.
});
