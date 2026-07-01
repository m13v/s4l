/**
 * S4L control panel (MCP Apps UI).
 *
 * Renders inside the host's sandboxed iframe. It does NOT duplicate any pipeline
 * logic: every button calls one of the server's existing tools (draft_cycle,
 * setup, get_stats) through the host via app.callServerTool, and the host pushes
 * results back. First paint comes from the `panel` tool's own structuredContent
 * snapshot; Refresh re-reads via setup(status) + runtime(status).
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
type MilestoneStatus = "pending" | "in_progress" | "complete" | "blocked";
interface OnboardingMilestone {
  id: string;
  status: MilestoneStatus;
  attempts: number;
  completed_at?: string;
  last_error?: string;
}
interface OnboardingSnapshot {
  complete: boolean;
  milestones: OnboardingMilestone[];
  current_blocker?: {
    milestone: string;
    code: string;
    message: string;
    at: string;
    attempt: number;
  } | null;
  doctor?: {
    phase: string;
    ok: boolean;
    summary: { pass: number; fail: number; expected: number; total: number };
  } | null;
}
interface Snapshot {
  projects: ProjStatus[];
  projects_ready: number;
  projects_total: number;
  x_connected: boolean;
  x_state: string;
  x_handle?: string | null;
  version: string;
  latest_version: string | null;
  update_available: boolean;
  runtime_ready: boolean;
  runtime_provisioning?: boolean;
  // Schedule state for the CURRENT Claude account: 'missing'/'disabled' means the
  // draft tasks aren't registered here (e.g. after an account switch) and the
  // "Set up draft schedule" button should show.
  schedule_state?: "missing" | "disabled" | "ok" | "unknown";
  // Engagement lanes (single source: mode.json via the server snapshot). Two
  // INDEPENDENT lanes; both can be on (the cycle then splits 50/50). `mode` is
  // the derived legacy mirror kept for back-compat.
  mode?: "promotion" | "personal_brand";
  flags?: { personal_brand?: boolean; promotion?: boolean };
  onboarding?: OnboardingSnapshot;
  // False only when the always-on tray app was quit while the runtime is ready
  // (macOS only). Drives the "Restart menu bar" banner.
  menubar_running?: boolean;
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
const btnSetup = $("btn-setup") as HTMLButtonElement;
const btnSchedule = $("btn-schedule") as HTMLButtonElement;
const statsGrid = $("stats-grid");
const statsToggle = $("stats-toggle") as HTMLButtonElement;
const logEl = $("log");
const installCard = $("install-card");
const setupSummary = $("setup-summary") as HTMLButtonElement;
const onboardingDetails = $("onboarding-details");
const onboardingSteps = $("onboarding-steps");
const onboardingBlocker = $("onboarding-blocker");
const onboardingCount = $("onboarding-count");
const onboardingBarFill = $("onboarding-bar-fill");
const liveCard = $("live-card");
const statsCard = $("stats-card");
const installSteps = $("install-steps");
const installErr = $("install-err");
const btnInstall = $("btn-install") as HTMLButtonElement;
const menubarBanner = $("menubar-banner");
const btnMenubarRestart = $("btn-menubar-restart") as HTMLButtonElement;
const btnLive = $("btn-live") as HTMLButtonElement;
const btnLiveStop = $("btn-live-stop") as HTMLButtonElement;
const btnLiveFront = $("btn-live-front") as HTMLButtonElement;
const liveStatus = $("live-status");
const liveImg = $("live-img") as HTMLImageElement;
const btnMode = $("btn-mode") as HTMLButtonElement;
const modeCurrent = $("mode-current");
const modeSub = $("mode-sub");

let state: Snapshot | null = null;
let installPolling = false; // guard against overlapping poll loops
let setupPolling = false; // guard the live setup-progress poll started by Set up
let updating = false; // guard against double-firing the in-header update button
let setupDetailsOpen = false; // header setup dropdown expanded state
let statsOpen = false; // "Last 7 days stats" dropdown expanded state

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

const MILESTONE_LABELS: Record<string, string> = {
  environment_checked: "Environment checked",
  runtime_ready: "Runtime ready",
  x_connected: "X connected",
  profile_scanned: "Profile scanned",
  project_ready: "Project ready",
  topics_seeded: "Topics seeded",
  tasks_scheduled: "Tasks scheduled",
};

function milestoneGlyph(status: MilestoneStatus): string {
  switch (status) {
    case "complete": return "\u2713";
    case "in_progress": return "\u2026";
    case "blocked": return "\u00d7";
    default: return "\u00b7";
  }
}

// Reflect the setupDetailsOpen flag onto the dropdown button + details panel.
function applySetupDetails() {
  onboardingDetails.hidden = !setupDetailsOpen;
  setupSummary.setAttribute("aria-expanded", String(setupDetailsOpen));
  setupSummary.classList.toggle("expanded", setupDetailsOpen);
}

function renderOnboarding(progress?: OnboardingSnapshot) {
  if (!progress || !Array.isArray(progress.milestones)) {
    setupSummary.hidden = true;
    onboardingDetails.hidden = true;
    return;
  }
  // The header always carries the setup dropdown once a ledger exists, so the
  // milestone details stay reachable even after setup completes.
  setupSummary.hidden = false;

  const total = progress.milestones.length;
  const completed = progress.milestones.filter((m) => m.status === "complete").length;
  const blocked = !!progress.current_blocker && !progress.complete;
  setupSummary.classList.toggle("complete", progress.complete);
  setupSummary.classList.toggle("blocked", blocked);

  // The "N/total" counter is shown only while setup is incomplete; once complete
  // it collapses to a bare "Setup ▾" dropdown (progress no longer surfaced inline).
  onboardingCount.hidden = progress.complete;
  onboardingCount.textContent = blocked
    ? `${completed}/${total} · needs you`
    : setupPolling
      ? `${completed}/${total} · setting up…`
      : `${completed}/${total}`;
  onboardingBarFill.style.width =
    total > 0 ? `${Math.round((completed / total) * 100)}%` : "0%";

  onboardingSteps.innerHTML = progress.milestones
    .map((milestone) => {
      const label = MILESTONE_LABELS[milestone.id] || milestone.id;
      const attempts = milestone.attempts > 1
        ? ` <span class="detail">${milestone.attempts} attempts</span>`
        : "";
      return (
        `<li class="${milestone.status}">` +
        `<span class="glyph">${milestoneGlyph(milestone.status)}</span>` +
        `<span>${label}${attempts}</span></li>`
      );
    })
    .join("");
  if (progress.current_blocker) {
    onboardingBlocker.textContent =
      `Current blocker: ${progress.current_blocker.message}`;
    onboardingBlocker.hidden = false;
    // A blocker needs the user — force the details open so it isn't buried in a
    // collapsed dropdown.
    setupDetailsOpen = true;
  } else {
    onboardingBlocker.hidden = true;
  }
  applySetupDetails();
}

function render() {
  if (!state) return;
  renderOnboarding(state.onboarding);
  // Version + update button. When an update is available the badge is an actual
  // button that installs the latest release (delegated click on verEl, since the
  // button is recreated on every render).
  verEl.innerHTML = state.update_available && state.latest_version
    ? `v${state.version} \u00b7 <button id="btn-update" class="update-btn">Update to ${state.latest_version}</button>`
    : `v${state.version}`;

  // Show runtime status until the owned Python/Chromium runtime exists. The
  // end-to-end Set up action remains enabled: the agent owns installation too.
  // The direct install button is only a manual repair/fallback surface.
  const needsRuntime = !state.runtime_ready;
  installCard.hidden = !needsRuntime;

  // "Menu bar not running" banner: only once the runtime exists (before that the
  // tray isn't installed and the install card owns the view) and only when the
  // snapshot explicitly reports it down (undefined = unknown = don't nag).
  menubarBanner.hidden = needsRuntime || state.menubar_running !== false;

  // "Setup complete" == the pipeline can actually run a draft cycle: the runtime
  // exists, at least one project is fully configured, and the X session is
  // connected. Until all three hold, the panel is intentionally minimal — just
  // the Set up button (or the Install card while the runtime is missing) — and
  // Run draft cycle is hidden. Once complete, Set up disappears and Run draft
  // cycle is the single primary action.
  const hasReady = state.projects_ready > 0;
  const setupComplete = !needsRuntime && hasReady && state.x_connected;

  // Before setup completes, Set up is the primary action. After it completes the
  // autopilot drafts on its own (no manual "draft now" button) — the only action
  // that can surface is "Set up draft schedule" when the tasks aren't scheduled on
  // THIS Claude account (e.g. after an account switch, which orphans them).
  btnSetup.hidden = setupComplete;
  btnSetup.disabled = false;
  btnSetup.classList.toggle("primary", !setupComplete);

  const needsSchedule =
    setupComplete && (state.schedule_state === "missing" || state.schedule_state === "disabled");
  btnSchedule.hidden = !needsSchedule;
  btnSchedule.classList.toggle("primary", needsSchedule);

  // Engagement lanes — always shown, mirroring the menu bar. Two independent
  // lanes; both can be on (cycle splits 50/50). Single source: state.flags (the
  // server snapshot's mode.json read), with state.mode as a legacy fallback.
  const flags = state.flags || (state.mode === "promotion"
    ? { personal_brand: false, promotion: true }
    : { personal_brand: true, promotion: false });
  const personalOn = !!flags.personal_brand;
  const promoOn = !!flags.promotion;
  const lanes: string[] = [];
  if (personalOn) lanes.push("Personal brand");
  if (promoOn) lanes.push("Promotion");
  modeCurrent.textContent = lanes.length ? lanes.join(" + ") : "None";
  modeSub.textContent =
    personalOn && promoOn
      ? "Both lanes on: cycles split 50/50 between link-free brand posts and product promotion."
      : personalOn
        ? "Organic, link-free engagement in your own voice."
        : promoOn
          ? "Marketing your configured products."
          : "No lane on; the cycle falls back to personal brand.";
  // The panel button toggles the PRODUCT-PROMOTION lane (personal brand is the
  // default base lane; turn it off from the menu-bar checkmark if needed).
  btnMode.textContent = promoOn ? "Turn off product promotion" : "Also promote a product";

  // Secondary surfaces (live browser, 7-day stats) are only meaningful once the
  // product is configured and posting. Hide them until setup is complete so the
  // pre-setup view stays a minimal "just set up" interface; the Install card
  // (gated above) is the only thing shown while the runtime is still installing.
  liveCard.hidden = !setupComplete;
  statsCard.hidden = !setupComplete;
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
    mode: o.mode ?? state?.mode,
    flags: o.flags ?? state?.flags,
    onboarding: o.onboarding,
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

async function refresh() {
  log("Refreshing\u2026");
  try {
    // install_status is cheap and tells us whether the runtime gate is cleared;
    // pull it alongside the usual status so a refresh re-evaluates the gate.
    const [setupStatus, rt] = await Promise.all([
      call("project_config", { status: true }),
      call("runtime", { action: "status" }).catch(() => ({})),
    ]);
    applyState({
      ...fromSetupStatus(setupStatus),
      ...(typeof rt.runtime_ready === "boolean" ? { runtime_ready: rt.runtime_ready } : {}),
      ...(typeof rt.menubar_running === "boolean" ? { menubar_running: rt.menubar_running } : {}),
      onboarding: rt.onboarding || setupStatus.onboarding || state?.onboarding,
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
      const rt = await call("runtime", { action: "status" }).catch(() => ({} as any));
      renderInstallProgress(rt.progress ?? null);
      if (rt.onboarding) applyState({ onboarding: rt.onboarding });
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

// Follow the autonomous setup the agent runs in the chat. The onboarding ledger
// advances as the agent completes each milestone (connect X, scan profile, save
// project, seed topics, draft-verify), so we poll the cheap install_status —
// which carries both the runtime install progress and the onboarding snapshot —
// and repaint the Setup progress card live until every milestone is done. This
// is pollInstall's twin, extended past runtime_ready to the end of setup.
async function pollSetup() {
  if (setupPolling) return;
  setupPolling = true;
  render(); // flip the header to "setting up…" immediately
  const startedAt = Date.now();
  const MAX_MS = 20 * 60 * 1000; // safety stop: setup is autonomous, not infinite
  try {
    for (;;) {
      const rt = await call("runtime", { action: "status" }).catch(() => ({} as any));
      if (rt.progress) renderInstallProgress(rt.progress);
      const patch: Partial<Snapshot> = {};
      if (typeof rt.runtime_ready === "boolean") patch.runtime_ready = rt.runtime_ready;
      if (rt.onboarding) patch.onboarding = rt.onboarding;
      if (Object.keys(patch).length) applyState(patch);
      if ((rt.onboarding as OnboardingSnapshot | undefined)?.complete) {
        // Final full read flips the gating (Set up -> Run draft cycle), reveals
        // the status/stats cards, and loads 7-day stats.
        await refresh();
        log("Setup complete.");
        break;
      }
      if (Date.now() - startedAt > MAX_MS) break;
      await new Promise((r) => setTimeout(r, 2000));
    }
  } finally {
    setupPolling = false;
    render(); // drop the "setting up…" hint
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
// setup/SKILL.md and drives autonomous discovery plus any unavoidable login).
// Unlike the other buttons
// (which call server tools directly via callServerTool, no model involved), this
// injects a real user turn into the host conversation via sendMessage, so Claude
// takes a turn and runs the end-to-end setup flow right there in the chat.
btnSetup.addEventListener("click", () => busy(btnSetup, "Starting\u2026", async () => {
  log("Asking Claude to run setup\u2026");
  try {
    const res = await app.sendMessage({
      role: "user",
      content: [{
        type: "text",
        text:
          "Set up S4L plugin end to end now. Inspect and repair the runtime, auto-detect " +
          "and connect my X session, scan my profile, discover and research my product, then infer " +
          "and save a complete project with seeded search topics. Keep going without asking me to " +
          "approve each safe setup step. Ask only if I must interactively sign in or no product " +
          "can be identified. Keep every reply to me extremely concise: a few short sentences at " +
          "most, no step-by-step narration or long status walls. If you must ask me something (e.g. " +
          "the product URL), make it one short question.",
      }],
    });
    if ((res as any)?.isError) log("The host rejected the setup request \u2014 type \u201cset up S4L\u201d in the chat instead.");
    else {
      log("Setup is running in the chat. It will only stop for an unavoidable login or missing product.");
      // Follow the agent's progress live and repaint the Setup progress card as
      // each milestone lands, instead of waiting for a manual Refresh.
      void pollSetup();
    }
  } catch (e: any) {
    log("Couldn\u2019t start setup: " + (e?.message || e));
  }
}));

// Set up draft schedule for THIS account. Like Setup, this needs the model in the
// loop (only the host create_scheduled_task can register a routine, and it runs
// inside a chat turn), so we inject a user turn via sendMessage. The worker prompt
// files already exist on disk (the server writes them on boot), so the agent just
// registers them via create_scheduled_task \u2014 no dependency on queue_setup.
btnSchedule.addEventListener("click", () => busy(btnSchedule, "Setting up\u2026", async () => {
  log("Asking Claude to schedule the draft tasks for this account\u2026");
  try {
    const res = await app.sendMessage({
      role: "user",
      content: [{
        type: "text",
        text:
          "Set up the social-autoposter draft autopilot schedule for this Claude account. " +
          "If queue_setup is available, call it; then for EACH of saps-phase1-query and " +
          "saps-phase2b-draft call the host tool create_scheduled_task with taskId, " +
          "cronExpression \"* * * * *\", and the prompt \u2014 read it from " +
          "~/.claude/scheduled-tasks/<taskId>/SKILL.md (already on disk). Do NOT redo my X " +
          "connection or project setup. Keep replies to me very short.",
      }],
    });
    if ((res as any)?.isError) log("The host rejected it \u2014 type \u201cset up the draft schedule\u201d in the chat instead.");
    else log("Scheduling is running in the chat. The draft tasks will register for this account.");
  } catch (e: any) {
    log("Couldn\u2019t start scheduling: " + (e?.message || e));
  }
}));

// Engagement-lane toggle: lightweight flip of the PROMOTION lane via
// engagement_mode action:'toggle' lane:'promotion' (mode.json only, no persona
// provisioning), then refresh. Mirrors the menu-bar checkmarks (same single
// source, mode.json). Personal brand is toggled from the menu bar.
btnMode.addEventListener("click", () => busy(btnMode, "Switching…", async () => {
  try {
    const res = await call("engagement_mode", { action: "toggle", lane: "promotion" });
    if (res && res.flags) applyState({ flags: res.flags });
    await refresh();
  } catch (e: any) {
    log("Couldn’t switch lane: " + (e?.message || e));
  }
}));

// ---- collapsible sections -------------------------------------------------
// The header setup dropdown and the "Last 7 days stats" header are the only two
// expand/collapse controls. Both just flip a local boolean and re-apply it; the
// setup details panel also auto-opens on a blocker (handled in renderOnboarding).
setupSummary.addEventListener("click", () => {
  setupDetailsOpen = !setupDetailsOpen;
  applySetupDetails();
});

statsToggle.addEventListener("click", () => {
  statsOpen = !statsOpen;
  statsGrid.hidden = !statsOpen;
  statsToggle.setAttribute("aria-expanded", String(statsOpen));
  statsToggle.classList.toggle("expanded", statsOpen);
});

// (The "Run draft cycle" button was removed \u2014 the autopilot drafts on its own via
// the launchd kicker + queue worker, so there is no manual draft-now action.)

// In-header update button. Created fresh by render() whenever an update is
// available, so the click is delegated off verEl rather than bound to the
// (recreated) button element. Calls `version` action:update, which pulls + installs
// the latest release; it takes effect after the client restarts.
verEl.addEventListener("click", (e) => {
  const t = e.target as HTMLElement | null;
  if (t && t.id === "btn-update") void runUpdate();
});

async function runUpdate() {
  if (updating) return;
  updating = true;
  const btn = document.getElementById("btn-update") as HTMLButtonElement | null;
  if (btn) { btn.disabled = true; btn.textContent = "Updating\u2026"; }
  log("Installing the latest release\u2026 this can take a minute.");
  try {
    const r = await call("runtime", { action: "update" });
    if (r.ok) {
      log(`Updated to ${r.latest_published || "the latest version"}. ${r.takes_effect || "Restart the client to apply."}`);
      if (btn) btn.textContent = "Update installed \u2014 restart to apply";
    } else {
      log("Update failed (exit " + (r.exit_code ?? "?") + "). Try `npx social-autoposter@latest update` in a terminal.");
      if (btn) { btn.disabled = false; btn.textContent = "Retry update"; }
    }
  } catch (e: any) {
    log("Update failed: " + (e?.message || e));
    if (btn) { btn.disabled = false; btn.textContent = "Retry update"; }
  } finally {
    updating = false;
  }
}

btnInstall.addEventListener("click", async () => {
  installErr.hidden = true;
  btnInstall.disabled = true;
  btnInstall.textContent = "Starting\u2026";
  log("Installing the runtime \u2014 this is a one-time download (~150MB+).");
  try {
    const r = await call("runtime", { action: "install" });
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

// Restart the always-on tray app after a Quit. Calls the restart_menubar tool
// (re-loads its LaunchAgent server-side) and applies the fresh running state it
// returns, so the banner drops without waiting for the next refresh.
btnMenubarRestart.addEventListener("click", () =>
  busy(btnMenubarRestart, "Restarting\u2026", async () => {
    log("Restarting the S4L menu bar\u2026");
    try {
      const r = await call("restart_menubar");
      if (typeof r.menubar_running === "boolean") applyState({ menubar_running: r.menubar_running });
      log(
        r.menubar_running
          ? "Menu bar restarted."
          : "Couldn\u2019t confirm the menu bar came back" + (r.detail ? ": " + r.detail : ".")
      );
    } catch (e: any) {
      log("Couldn\u2019t restart the menu bar: " + (e?.message || e));
    }
  })
);

// ---- config view / edit ---------------------------------------------------
// Removed: raw config.json view/edit. All project changes now go through the
// `project_config` tool (validates, merges, re-seeds topics) — there is no
// raw-overwrite surface in the panel anymore.

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

// ---- boot -----------------------------------------------------------------
app.connect().then(() => {
  const ctx = app.getHostContext();
  if (ctx) applyHostContext(ctx);
  // Stats load from ontoolresult once the first snapshot confirms the runtime is
  // ready (the pipeline can't run without it), so nothing to do here.
});
