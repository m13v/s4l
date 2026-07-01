/**
 * Live browser preview via CDP screencast.
 *
 * Attaches to a running managed Chrome over the Chrome DevTools Protocol (on the
 * harness's remote-debugging port) and runs `Page.startScreencast`, buffering the
 * most recent JPEG frame. The `show_browser_to_user` tool hands that frame to the
 * panel, which polls for fresh frames and paints them — a low-latency "watch what
 * the bot is doing" view.
 *
 * The frames travel back to the panel through the NORMAL MCP tool-result channel
 * as a `data:` URL, which the default panel CSP already permits. So this needs no
 * CSP widening, no localhost network access from the iframe, and no extra
 * dependency: it uses Node's built-in global `WebSocket` (Node >= 21) and `fetch`.
 *
 * A future high-FPS upgrade (panel opens a `ws://` straight to a local relay) is a
 * separate step gated on the host honoring a `connectDomains` localhost entry;
 * this module is the robust baseline that works regardless.
 */

import { execFile } from "node:child_process";

// Untyped indirection: Node ships a global WebSocket at runtime (>=21) but
// @types/node doesn't always declare it as a value, and MessageEvent isn't typed
// without the DOM lib. Reach for it dynamically and keep the event handlers `any`.
const WS: any = (globalThis as any).WebSocket;

interface CdpTarget {
  id: string;
  type: string;
  title: string;
  url: string;
  webSocketDebuggerUrl?: string;
}

// Ports we manage a Chrome on, most-likely-active first. TWITTER_CDP_URL (the
// twitter harness) wins if set; the rest cover reddit / browser-harness / assrt.
function candidatePorts(): number[] {
  const ports: number[] = [];
  const env = process.env.TWITTER_CDP_URL || "";
  const m = env.match(/:(\d+)/);
  if (m) ports.push(Number(m[1]));
  for (const p of [9555, 9556, 9557, 9222, 9223, 9755]) {
    if (!ports.includes(p)) ports.push(p);
  }
  return ports;
}

async function fetchJson(url: string, timeoutMs = 1500): Promise<any | null> {
  try {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), timeoutMs);
    const res = await fetch(url, { signal: ctrl.signal });
    clearTimeout(t);
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

// Pick a real, visible page target on a port (skip devtools:// and extension
// targets, which have no useful screencast).
async function findPageTarget(port: number): Promise<CdpTarget | null> {
  const list = await fetchJson(`http://127.0.0.1:${port}/json`);
  if (!Array.isArray(list)) return null;
  const pages = list.filter(
    (t: any) =>
      t &&
      t.type === "page" &&
      typeof t.webSocketDebuggerUrl === "string" &&
      !String(t.url || "").startsWith("devtools://")
  );
  if (!pages.length) return null;
  // Prefer an actual http(s) page over about:blank / chrome:// scaffolding.
  return pages.find((t: any) => /^https?:/i.test(t.url)) || pages[0];
}

export async function findActivePort(): Promise<{ port: number; target: CdpTarget } | null> {
  for (const port of candidatePorts()) {
    const target = await findPageTarget(port);
    if (target) return { port, target };
  }
  return null;
}

class Screencast {
  private ws: any = null;
  private msgId = 1;
  private latest: string | null = null; // base64 JPEG, no data: prefix
  private lastFrameAt = 0;
  private connecting = false;
  port = 0;
  targetTitle = "";
  targetUrl = "";

  get running(): boolean {
    return !!this.ws && this.ws.readyState === 1 /* OPEN */;
  }

  // Ensure a screencast is running. Reuses an existing connection; otherwise
  // resolves a target (explicit port, else auto-detect) and connects.
  async ensure(port?: number): Promise<{ ok: boolean; error?: string }> {
    if (this.running || this.connecting) return { ok: true };
    if (!WS) return { ok: false, error: "no_websocket" };
    this.connecting = true;
    try {
      let chosenPort = port;
      let target: CdpTarget | null = null;
      if (chosenPort) target = await findPageTarget(chosenPort);
      if (!target) {
        const found = await findActivePort();
        if (!found) return { ok: false, error: "no_browser" };
        chosenPort = found.port;
        target = found.target;
      }
      await this.connect(target.webSocketDebuggerUrl as string);
      this.port = chosenPort as number;
      this.targetTitle = target.title || "";
      this.targetUrl = target.url || "";
      return { ok: true };
    } catch (e: any) {
      return { ok: false, error: String(e?.message || e) };
    } finally {
      this.connecting = false;
    }
  }

  private connect(wsUrl: string): Promise<void> {
    return new Promise((resolve, reject) => {
      let ws: any;
      try {
        ws = new WS(wsUrl);
      } catch (e) {
        reject(e);
        return;
      }
      let opened = false;
      const to = setTimeout(() => {
        if (!opened) {
          try { ws.close(); } catch { /* ignore */ }
          reject(new Error("cdp_ws_timeout"));
        }
      }, 5000);
      ws.onopen = () => {
        opened = true;
        clearTimeout(to);
        this.ws = ws;
        this.send("Page.enable");
        // Activate this tab first. Chrome only streams screencast frames for a
        // page whose RenderWidget is visible; a background tab (or one behind
        // another window) emits zero frames, which strands the panel on
        // "Connecting…". bringToFront makes the attached tab the foreground tab
        // so startScreencast has something to render.
        this.send("Page.bringToFront");
        this.send("Page.startScreencast", {
          format: "jpeg",
          quality: 55,
          maxWidth: 1280,
          maxHeight: 800,
          everyNthFrame: 1,
        });
        resolve();
      };
      ws.onmessage = (ev: any) => this.onMessage(ev);
      ws.onerror = () => { /* surfaced via onclose */ };
      ws.onclose = () => {
        clearTimeout(to);
        if (this.ws === ws) {
          this.ws = null;
          this.latest = null;
        }
        if (!opened) reject(new Error("cdp_ws_closed"));
      };
    });
  }

  private send(method: string, params?: any): void {
    if (!this.ws) return;
    try {
      this.ws.send(JSON.stringify({ id: this.msgId++, method, params: params || {} }));
    } catch { /* ignore */ }
  }

  private onMessage(ev: any): void {
    let msg: any;
    try {
      const raw = typeof ev?.data === "string" ? ev.data : String(ev?.data ?? "");
      msg = JSON.parse(raw);
    } catch {
      return;
    }
    if (msg && msg.method === "Page.screencastFrame") {
      const data = msg.params?.data;
      const sid = msg.params?.sessionId;
      if (typeof data === "string") {
        this.latest = data;
        this.lastFrameAt = Date.now();
      }
      // Must ack every frame or Chrome stops sending them.
      if (sid != null) this.send("Page.screencastFrameAck", { sessionId: sid });
    }
  }

  stop(): void {
    if (this.ws) {
      this.send("Page.stopScreencast");
      try { this.ws.close(); } catch { /* ignore */ }
    }
    this.ws = null;
    this.latest = null;
  }

  frame(): string | null {
    return this.latest;
  }

  status() {
    return {
      running: this.running,
      port: this.port || null,
      title: this.targetTitle,
      url: this.targetUrl,
      age_ms: this.lastFrameAt ? Date.now() - this.lastFrameAt : null,
    };
  }
}

export const screencast = new Screencast();

// ---- bring browser to front ------------------------------------------------
// Raise the managed Chrome above other apps so the user can interact with it
// directly. Two steps: (1) CDP Page.bringToFront raises the active TAB inside
// the browser; (2) on macOS, raise the browser's OS WINDOW above Claude Desktop
// by activating the process that owns the CDP port. Without (2), the tab would
// be focused but the window could still sit behind the panel.

// Fire a single CDP command on a target's debugger websocket and resolve once it
// acknowledges (or times out). Used for one-shot commands like Page.bringToFront
// where we don't need a persistent connection.
function cdpCommand(wsUrl: string, method: string, params?: any, timeoutMs = 3000): Promise<boolean> {
  return new Promise((resolve) => {
    if (!WS) { resolve(false); return; }
    let ws: any;
    try { ws = new WS(wsUrl); } catch { resolve(false); return; }
    let done = false;
    const finish = (ok: boolean) => {
      if (done) return;
      done = true;
      try { ws.close(); } catch { /* ignore */ }
      resolve(ok);
    };
    const to = setTimeout(() => finish(false), timeoutMs);
    ws.onopen = () => {
      try { ws.send(JSON.stringify({ id: 1, method, params: params || {} })); }
      catch { clearTimeout(to); finish(false); }
    };
    ws.onmessage = (ev: any) => {
      try {
        const raw = typeof ev?.data === "string" ? ev.data : String(ev?.data ?? "");
        const msg = JSON.parse(raw);
        if (msg && msg.id === 1) { clearTimeout(to); finish(!msg.error); }
      } catch { /* ignore */ }
    };
    ws.onerror = () => { clearTimeout(to); finish(false); };
    ws.onclose = () => { clearTimeout(to); finish(done); };
  });
}

// macOS only: activate the GUI process that owns the CDP port so its window
// rises above everything else. We find the PID via the listener on the port
// (not the established CDP client connections) and activate it by unix id, which
// works regardless of the app's display name (Chrome vs Chromium vs harness).
function raiseMacWindow(port: number): Promise<void> {
  return new Promise((resolve) => {
    execFile("lsof", ["-ti", `tcp:${port}`, "-sTCP:LISTEN"], { timeout: 2500 }, (_err, stdout) => {
      const pid = String(stdout || "").split(/\s+/).filter(Boolean)[0];
      if (!pid) { resolve(); return; }
      const osa = `tell application "System Events" to set frontmost of (first process whose unix id is ${pid}) to true`;
      // execFile (no shell) passes the AppleScript as one argv, so its inner
      // double quotes need no escaping.
      execFile("osascript", ["-e", osa], { timeout: 2500 }, () => resolve());
    });
  });
}

export async function bringBrowserToFront(
  port?: number
): Promise<{ ok: boolean; error?: string; port?: number }> {
  let chosenPort = port || screencast.port || 0;
  let target = chosenPort ? await findPageTarget(chosenPort) : null;
  if (!target) {
    const found = await findActivePort();
    if (!found) return { ok: false, error: "no_browser" };
    chosenPort = found.port;
    target = found.target;
  }
  if (target.webSocketDebuggerUrl) {
    await cdpCommand(target.webSocketDebuggerUrl, "Page.bringToFront");
  }
  if (process.platform === "darwin") {
    await raiseMacWindow(chosenPort);
  }
  return { ok: true, port: chosenPort };
}
