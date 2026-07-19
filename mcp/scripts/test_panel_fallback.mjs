// E2E: spawn the MCP server as a host WITHOUT MCP Apps UI capability, call the
// `dashboard` tool, and verify it returns a fallback_url whose loopback server
// serves the panel HTML (in http-bridge mode) and dispatches tools.
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import path from "node:path";
import os from "node:os";

const REPO = process.env.S4L_REPO_DIR || path.join(os.homedir(), "social-autoposter");
const serverEntry = path.resolve("dist/index.js");

const transport = new StdioClientTransport({
  command: process.execPath,
  args: [serverEntry],
  env: { ...process.env, S4L_REPO_DIR: REPO, S4L_PANEL_NO_OPEN: "1" },
});

// A plain client with NO ui extension capability => host "can't render inline".
const client = new Client({ name: "test-no-ui-host", version: "1.0.0" }, { capabilities: {} });

let fail = (m) => { console.error("FAIL:", m); process.exit(1); };

await client.connect(transport);
console.log("connected to MCP server");

const tools = await client.listTools();
const names = tools.tools.map((t) => t.name);
console.log("tools:", names.join(", "));
if (!names.includes("dashboard")) fail("dashboard tool not registered");

const res = await client.callTool({ name: "dashboard", arguments: {} });
const sc = res.structuredContent || {};
console.log("dashboard structuredContent keys:", Object.keys(sc));
const url = sc.fallback_url;
if (!url) fail("no fallback_url returned (host without UI cap should fall back)");
console.log("fallback_url:", url);

// 1) the loopback serves the panel in http-bridge mode
const html = await (await fetch(url)).text();
if (!html.includes('__S4L_BRIDGE__')) fail("served panel missing __S4L_BRIDGE__ flag");
if (!/__S4L_BRIDGE__\s*=\s*"http"/.test(html)) fail("panel not flipped to http bridge");
console.log("OK: panel served with http bridge flag");

// 2) health endpoint
const health = await (await fetch(new URL("/health", url))).json();
if (!health.ok) fail("/health not ok");
console.log("OK: /health");

// 3) tool dispatch over loopback returns the same snapshot shape
const dRes = await fetch(new URL("/tool/dashboard", url), {
  method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
});
const dJson = await dRes.json();
const snap = JSON.parse(dJson.structuredContent.snapshot);
if (typeof snap.projects_total !== "number") fail("loopback /tool/dashboard snapshot malformed");
console.log("OK: /tool/dashboard ->", snap.projects_total, "projects,",
  "x_connected=" + snap.x_connected, "autopilot=" + snap.autopilot_on);

// 4) unknown tool -> 404 error result
const u = await fetch(new URL("/tool/does_not_exist", url), { method: "POST", body: "{}" });
if (u.status !== 404) fail("unknown tool should 404, got " + u.status);
console.log("OK: unknown tool 404");

await client.close();
console.log("\nALL PASS");
process.exit(0);
