/**
 * Panel host bridge.
 *
 * The dashboard UI (panel.ts) talks to its host through ONE small surface:
 * callServerTool / sendMessage / getHostContext / connect plus a few event
 * setters. That surface has two implementations so the SAME panel.html runs in
 * two places with zero front-end duplication:
 *
 *  - AppsBridge  — the real MCP Apps `App` (postMessage to the host iframe).
 *                  Used when a host renders the ui:// resource inline.
 *  - HttpBridge  — a thin fetch shim. Used when the identical panel.html is
 *                  served from the loopback HTTP server (startLocalPanel in the
 *                  MCP server). Every callServerTool becomes POST /tool/<name>,
 *                  which replays the exact same captured tool handler.
 *
 * createBridge() picks based on a flag the loopback server injects into the
 * page (window.__SAPS_BRIDGE__). Inline (ui://) renders never see that flag and
 * get the AppsBridge.
 */
import { App } from "@modelcontextprotocol/ext-apps";

export interface PanelBridge {
  onhostcontextchanged?: (ctx: any) => void;
  onerror?: (e: any) => void;
  ontoolresult?: (result: any) => void;
  connect(): Promise<unknown>;
  getHostContext(): any;
  callServerTool(params: { name: string; arguments?: Record<string, unknown> }): Promise<any>;
  sendMessage(params: any): Promise<{ isError?: boolean } & Record<string, unknown>>;
}

// HTTP transport for the localhost fallback. Same-origin (the page is served by
// the loopback server itself), so relative URLs are correct and there is no CORS
// concern. callServerTool maps 1:1 onto POST /tool/<name>; the body IS the
// tool's `arguments` object, matching what the MCP handlers receive.
class HttpBridge implements PanelBridge {
  onhostcontextchanged?: (ctx: any) => void;
  onerror?: (e: any) => void;
  ontoolresult?: (result: any) => void;

  async connect(): Promise<void> {
    // First paint: in inline mode the host pushes the spawning tool's result via
    // ontoolresult. Over HTTP there is no host push, so we actively fetch the
    // dashboard snapshot once and fire the same callback — identical first paint.
    try {
      const result = await this.callServerTool({ name: "dashboard", arguments: {} });
      this.ontoolresult?.(result);
    } catch (e) {
      this.onerror?.(e);
    }
  }

  // No host theming over loopback; the page uses its own CSS defaults.
  getHostContext(): any {
    return undefined;
  }

  async callServerTool(params: { name: string; arguments?: Record<string, unknown> }): Promise<any> {
    const res = await fetch(`/tool/${encodeURIComponent(params.name)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(params.arguments ?? {}),
    });
    if (!res.ok) {
      // Surface a result-shaped error so parseResult / callers behave the same
      // as they would for a tool that returned an error payload.
      let text = `HTTP ${res.status}`;
      try { text = (await res.text()) || text; } catch { /* ignore */ }
      return { isError: true, content: [{ type: "text", text }] };
    }
    return res.json();
  }

  // Setup is the one action that genuinely needs the model in the loop, which a
  // loopback page has no path to. Degrade to an error result; panel.ts already
  // handles isError by telling the user to run setup from the chat.
  async sendMessage(): Promise<{ isError: boolean }> {
    return { isError: true };
  }
}

export function createBridge(): PanelBridge {
  const mode = (globalThis as any).__SAPS_BRIDGE__;
  if (mode === "http") return new HttpBridge();
  // Inline MCP Apps host: the real App satisfies PanelBridge structurally.
  return new App({ name: "Social Autoposter Panel", version: "1.0.0" }) as unknown as PanelBridge;
}
