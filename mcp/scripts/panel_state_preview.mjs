// Throwaway visual-test harness: serves dist/panel.html in HTTP-bridge mode and
// returns a mock dashboard snapshot chosen by ?state= so we can screenshot the
// pre-setup / installing / complete layouts. Not shipped.
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DIST = path.join(__dirname, "..", "dist");

const SNAPS = {
  presetup: { // runtime ready, nothing configured yet
    projects: [], projects_total: 0, projects_ready: 0,
    x_connected: false, x_state: "", x_handle: null,
    autopilot_on: false, auto_update_on: false,
    version: "1.6.66", latest_version: null, update_available: false,
    runtime_ready: true, runtime_provisioning: false,
  },
  installing: { // runtime missing
    projects: [], projects_total: 0, projects_ready: 0,
    x_connected: false, x_state: "", x_handle: null,
    autopilot_on: false, auto_update_on: false,
    version: "1.6.66", latest_version: null, update_available: false,
    runtime_ready: false, runtime_provisioning: false,
  },
  complete: { // runtime ready, project ready, X connected
    projects: [{ name: "fazm", ready: true, missing_required: [] }],
    projects_total: 1, projects_ready: 1,
    x_connected: true, x_state: "connected", x_handle: "@m13v_",
    autopilot_on: false, auto_update_on: false,
    version: "1.6.66", latest_version: null, update_available: false,
    runtime_ready: true, runtime_provisioning: false,
  },
};

let CURRENT = "presetup";

function panelHtml() {
  const html = fs.readFileSync(path.join(DIST, "panel.html"), "utf-8");
  const inject = `<script>window.__SAPS_BRIDGE__="http";</script>`;
  return html.includes("</head>") ? html.replace("</head>", inject + "</head>") : inject + html;
}

const server = http.createServer((req, res) => {
  const u = new URL(req.url, "http://x");
  if (req.method === "GET" && (u.pathname === "/" || u.pathname === "/index.html")) {
    const st = u.searchParams.get("state");
    if (st && SNAPS[st]) CURRENT = st;
    res.writeHead(200, { "Content-Type": "text/html" });
    res.end(panelHtml());
    return;
  }
  if (req.method === "POST" && u.pathname.startsWith("/tool/")) {
    const name = decodeURIComponent(u.pathname.slice("/tool/".length));
    const snap = SNAPS[CURRENT];
    let payload = {};
    if (name === "dashboard") payload = { structuredContent: { snapshot: JSON.stringify(snap) } };
    else if (name === "setup") payload = { structuredContent: { snapshot: JSON.stringify({ ...snap, mcp_version: snap.version }) } };
    else if (name === "install_status") payload = { structuredContent: { snapshot: JSON.stringify({ runtime_ready: snap.runtime_ready, provisioning: snap.runtime_provisioning, steps: [] }) } };
    else if (name === "autopilot") payload = { structuredContent: { snapshot: JSON.stringify({ loaded: snap.autopilot_on, auto_update_loaded: snap.auto_update_on }) } };
    else if (name === "get_stats") payload = { structuredContent: { snapshot: JSON.stringify({ rows: [] }) } };
    else payload = { structuredContent: { snapshot: JSON.stringify({}) } };
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify(payload));
    return;
  }
  res.writeHead(404); res.end("nope");
});

server.listen(8799, "127.0.0.1", () => console.log("preview on http://127.0.0.1:8799/?state=presetup"));
