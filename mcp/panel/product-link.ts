/**
 * S4L "add your product" widget (MCP Apps UI).
 *
 * A focused, standalone widget — its OWN ui:// resource, separate from the
 * dashboard panel — that captures the user's product URL and submits it through
 * the host bridge. Two paths, chosen from the current project status:
 *
 *  - A project already exists but still needs a website -> write it
 *    DETERMINISTICALLY via project_config (callServerTool), then re-read status.
 *    This is the editable-form round-trip the config editor will reuse.
 *  - True cold start (no projects yet) -> hand the URL to the model via
 *    sendMessage so it researches the site and runs onboarding end to end.
 *
 * Mirrors panel.ts's bridge handshake and reuses the same grayscale tokens; it
 * duplicates no pipeline logic. Runs both inline (App/postMessage) and over the
 * loopback HTTP bridge (createBridge picks based on window.__S4L_BRIDGE__).
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
import "./product-link.css";

interface ProjStatus { name: string; ready: boolean; missing_required: string[] }

// Tools return data either as structuredContent or as a JSON string in the first
// text content block. Normalize both (same shape as panel.ts::parseResult).
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

const $ = (id: string) => document.getElementById(id)!;
const form = $("pl-form") as HTMLFormElement;
const urlInput = $("pl-url") as HTMLInputElement;
const submitBtn = $("pl-submit") as HTMLButtonElement;
const subEl = $("pl-sub");
const statusEl = $("pl-status");
const logEl = $("pl-log");

const app = createBridge();

function applyHostContext(ctx: McpUiHostContext) {
  if (ctx.theme) applyDocumentTheme(ctx.theme);
  if (ctx.styles?.variables) applyHostStyleVariables(ctx.styles.variables);
  if (ctx.styles?.css?.fonts) applyHostFonts(ctx.styles.css.fonts);
}
app.onhostcontextchanged = applyHostContext;
app.onerror = (e) => console.error(e);

// Which project (if any) this submit writes the website onto. null => cold start
// (no projects yet) -> hand off to the model via sendMessage.
let target: ProjStatus | null = null;
let coldStart = true;

function log(msg: string) { logEl.textContent = msg; }
function showStatus(msg: string) { statusEl.textContent = msg; statusEl.hidden = false; }
function busy(on: boolean, label?: string) {
  submitBtn.disabled = on;
  if (label) submitBtn.textContent = label;
}

// A light URL sanity check (the field is type=url but we don't rely on native
// validation since the form is novalidate). Adds https:// if the user omits it.
function normalizeUrl(raw: string): string | null {
  let s = raw.trim();
  if (!s) return null;
  if (!/^https?:\/\//i.test(s)) s = "https://" + s;
  try { return new URL(s).href; } catch { return null; }
}

function render(data: any) {
  const projects: ProjStatus[] = Array.isArray(data?.projects) ? data.projects : [];
  // Prefer a project that explicitly still needs a website; else the first
  // not-ready project; else the first project overall.
  target =
    projects.find((p) => (p.missing_required || []).includes("website")) ||
    projects.find((p) => !p.ready) ||
    projects[0] ||
    null;
  coldStart = projects.length === 0;

  if (coldStart || !target) {
    subEl.textContent =
      "Paste the link to your product to begin setup. S4L reads it to learn what to post about.";
    submitBtn.textContent = "Start setup";
  } else {
    subEl.textContent =
      `Set the product website for “${target.name}”. S4L re-reads it to keep posts on-message.`;
    submitBtn.textContent = target.ready ? "Update website" : "Save website";
  }
}

async function refresh() {
  try {
    const res = await app.callServerTool({ name: "project_config", arguments: { status: true } });
    render(parseResult(res));
  } catch (e: any) {
    // Even if status can't be read, allow cold-start usage.
    render({ projects: [] });
    log("Couldn’t read current status: " + (e?.message || e));
  }
}

// Cold start: no project exists yet -> let the model run end-to-end onboarding
// (research the site, derive a slug, seed topics). sendMessage injects a real
// user turn; over the loopback bridge it's unavailable, so we say so.
async function startSetup(url: string) {
  busy(true, "Starting…");
  try {
    const res = await app.sendMessage({
      role: "user",
      content: [{
        type: "text",
        text:
          `Set up social-autoposter for my product at ${url}. Research the site, infer a complete ` +
          "project (derive a short slug from the site), connect my X session, seed search topics, " +
          "and continue setup end to end. Keep replies to me very concise.",
      }],
    } as any);
    if ((res as any)?.isError) {
      showStatus("This host can’t start setup from the widget — paste your product link in the chat instead.");
    } else {
      showStatus("Setup started. Watch the chat — S4L is researching your product now.");
    }
  } catch (e: any) {
    showStatus("Couldn’t start setup from here. Paste your product link in the chat instead.");
    log(String(e?.message || e));
  } finally {
    busy(false);
    render({ projects: [] }); // restore the button label
  }
}

// A project exists: write the website deterministically. This is the editable-
// form round-trip — callServerTool -> project_config merge -> re-read status.
async function saveWebsite(url: string, proj: ProjStatus) {
  busy(true, "Saving…");
  try {
    const res = await app.callServerTool({
      name: "project_config",
      arguments: { name: proj.name, website: url },
    });
    const data = parseResult(res);
    if ((res as any)?.isError) {
      showStatus("Save failed: " + (data?._raw || "see the chat for details."));
    } else {
      showStatus(`Saved ${url} for “${proj.name}”.`);
      urlInput.value = "";
      await refresh(); // reflect the new state (website no longer in missing_required)
    }
  } catch (e: any) {
    showStatus("Save failed: " + (e?.message || e));
  } finally {
    busy(false);
  }
}

form.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const url = normalizeUrl(urlInput.value);
  if (!url) { showStatus("Enter a valid product URL, e.g. https://yourproduct.com"); urlInput.focus(); return; }
  statusEl.hidden = true;
  log("");
  if (coldStart || !target) await startSetup(url);
  else await saveWebsite(url, target);
});

app.connect()
  .then(() => {
    const ctx = app.getHostContext();
    if (ctx) applyHostContext(ctx as McpUiHostContext);
    void refresh();
  })
  .catch((e) => {
    log("Bridge connect failed: " + (e?.message || e));
    render({ projects: [] }); // still allow cold-start usage
  });
