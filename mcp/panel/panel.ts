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
  App,
  applyDocumentTheme,
  applyHostFonts,
  applyHostStyleVariables,
  type McpUiHostContext,
} from "@modelcontextprotocol/ext-apps";
import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
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
const stAp = $("st-ap"), stApSub = $("st-ap-sub");
const btnSetup = $("btn-setup") as HTMLButtonElement;
const btnDraft = $("btn-draft") as HTMLButtonElement;
const btnAuto = $("btn-autopilot") as HTMLButtonElement;
const btnX = $("btn-connectx") as HTMLButtonElement;
const btnRefresh = $("btn-refresh") as HTMLButtonElement;
const statsGrid = $("stats-grid");
const logEl = $("log");
const installCard = $("install-card");
const installSteps = $("install-steps");
const installErr = $("install-err");
const btnInstall = $("btn-install") as HTMLButtonElement;

let state: Snapshot | null = null;
let xConfirmPending = false; // two-step connect-X (explain -> confirm)
let installPolling = false; // guard against overlapping poll loops

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
  // Version + update badge.
  verEl.innerHTML = state.update_available && state.latest_version
    ? `v${state.version} \u00b7 <span class="update">update to ${state.latest_version}</span>`
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

  // X / Twitter.
  stX.textContent = state.x_connected ? "Connected" : "Not connected";
  stXSub.textContent = state.x_state || "";
  btnX.hidden = state.x_connected;
  if (!xConfirmPending) btnX.textContent = "Connect X";

  // Autopilot.
  stAp.textContent = state.autopilot_on ? "On" : "Off";
  stApSub.textContent = state.auto_update_on ? "auto-update on" : "";
  btnAuto.textContent = state.autopilot_on ? "Disable autopilot" : "Enable autopilot";

  // Gate actions on readiness. Nothing below works without the runtime, so when
  // it's missing every action is disabled and the Install card carries the only
  // live button.
  const hasReady = state.projects_ready > 0;
  btnSetup.disabled = needsRuntime;
  btnDraft.disabled = needsRuntime || !hasReady;
  btnAuto.disabled = needsRuntime || !hasReady;
  btnX.disabled = needsRuntime;
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
    version: o.mcp_version || state?.version || "",
    latest_version: o.latest_version ?? null,
    update_available: !!o.update_available,
  };
}

// ---- App wiring -----------------------------------------------------------
const app = new App({ name: "Social Autoposter Panel", version: "1.0.0" });

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
  if (data && typeof data.projects_total === "number") applyState(data as Snapshot);
};

async function call(name: string, args: Record<string, unknown> = {}): Promise<any> {
  const res = await app.callServerTool({ name, arguments: args });
  return parseResult(res as CallToolResult);
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
        log("Runtime installed \u2014 you're ready to set up.");
        void refresh();
        return;
      }
      const p: InstallProgress | null = rt.progress ?? null;
      if (p && p.done && !p.ok) {
        btnInstall.disabled = false;
        btnInstall.textContent = "Retry install";
        log("Install failed \u2014 see the step above, then Retry.");
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
  fn().finally(() => { btn.textContent = prev; render(); });
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

btnAuto.addEventListener("click", () => busy(btnAuto, "Working\u2026", async () => {
  const action = state?.autopilot_on ? "disable" : "enable";
  try {
    const r = await call("autopilot", { action });
    const on = action === "enable" ? !!(r.autopilot?.loaded) : !(r.autopilot_unloaded);
    applyState({ autopilot_on: on });
    log(`Autopilot ${on ? "enabled" : "disabled"}.`);
  } catch (e: any) { log("Autopilot toggle failed: " + (e?.message || e)); }
}));

btnX.addEventListener("click", () => busy(btnX, "Working\u2026", async () => {
  try {
    if (!xConfirmPending) {
      const r = await call("setup", { action: "connect_x" });
      if (r.already_connected) { applyState({ x_connected: true }); log("X already connected."); return; }
      xConfirmPending = true;
      btnX.textContent = "Confirm: import X session";
      log(r.what_will_happen || "This imports your x.com cookies into the autoposter's browser. Click again to confirm.");
    } else {
      const r = await call("setup", { action: "connect_x", confirm: true });
      xConfirmPending = false;
      applyState({ x_connected: !!r.connected, x_state: r.state || "" });
      log(r.summary || (r.connected ? "X connected." : "X not connected \u2014 see chat."));
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

btnRefresh.addEventListener("click", () => busy(btnRefresh, "Refreshing\u2026", refresh));

// ---- boot -----------------------------------------------------------------
app.connect().then(() => {
  const ctx = app.getHostContext();
  if (ctx) applyHostContext(ctx);
  // Stats aren't in the spawn snapshot; pull them once on open.
  void loadStats();
});
