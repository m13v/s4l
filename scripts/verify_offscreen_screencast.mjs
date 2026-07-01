// Throwaway validation for the "Connecting…" screencast fix.
// Reproduces the real plugin scenario: the harness Chrome window is COVERED by
// another opaque window (the Claude Desktop panel). macOS native-window
// occlusion then pauses Chrome's compositing, so Page.startScreencast stops
// emitting frames and the panel is stranded on "Connecting…".
//
// Test: launch an ANIMATED Chrome ON-screen, cover it with a second full-screen
// opaque Chrome window, and count screencast frames over ~3.5s — once WITHOUT
// the occlusion flags, once WITH. Throwaway ports (9591/9592) + temp profiles;
// never touches 9555, the real profile, or the pipeline.

import { spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const PORT = 9591;
const COVER_PORT = 9592;
const WS = globalThis.WebSocket;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function fetchJson(url) {
  try { const r = await fetch(url); return r.ok ? await r.json() : null; } catch { return null; }
}

const ANIM = "<body style=margin:0;background:white><div id=b style='width:100px;height:100px;background:red;position:absolute'></div><script>let x=0;function f(){x=(x+7)%700;const b=document.getElementById('b');b.style.left=x+'px';b.style.background='hsl('+x+',90%,50%)';requestAnimationFrame(f)}requestAnimationFrame(f)</script></body>";

function launch(port, profile, extraArgs, url, pos, size) {
  const args = [
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profile}`,
    "--no-first-run", "--no-default-browser-check",
    "--password-store=basic", "--use-mock-keychain",
    `--window-position=${pos}`, `--window-size=${size}`,
    ...extraArgs,
    "data:text/html," + encodeURIComponent(url),
  ];
  return spawn(CHROME, args, { stdio: "ignore", detached: true });
}

async function firstPage(port) {
  for (let i = 0; i < 20; i++) {
    await sleep(500);
    const list = await fetchJson(`http://127.0.0.1:${port}/json`);
    if (Array.isArray(list)) {
      const pages = list.filter((t) => t.type === "page" && t.webSocketDebuggerUrl && !String(t.url || "").startsWith("devtools://"));
      if (pages.length) return pages[0];
    }
  }
  return null;
}

async function run(withFlags) {
  const p1 = mkdtempSync(join(tmpdir(), "saps-anim-"));
  const p2 = mkdtempSync(join(tmpdir(), "saps-cover-"));
  const flags = withFlags
    ? ["--disable-features=ChromeWhatsNewUI,CalculateNativeWinOcclusion", "--disable-backgrounding-occluded-windows"]
    : ["--disable-features=ChromeWhatsNewUI"];
  // Animated window, small, top-left.
  const anim = launch(PORT, p1, flags, ANIM, "100,100", "700,500");
  const target = await firstPage(PORT);
  if (!target) { kill(anim); rmSync(p1, { recursive: true, force: true }); rmSync(p2, { recursive: true, force: true }); return "no_target"; }

  // Attach screencast BEFORE covering, so we know frames were flowing.
  let frames = 0, framesBeforeCover = 0;
  const ws = new WS(target.webSocketDebuggerUrl);
  let id = 1;
  const send = (m, p) => ws.send(JSON.stringify({ id: id++, method: m, params: p || {} }));
  await new Promise((res) => { ws.onopen = res; });
  send("Page.enable");
  if (withFlags) send("Page.bringToFront");
  send("Page.startScreencast", { format: "jpeg", quality: 55, maxWidth: 1280, maxHeight: 800, everyNthFrame: 1 });
  ws.onmessage = (ev) => {
    const msg = JSON.parse(typeof ev.data === "string" ? ev.data : String(ev.data));
    if (msg.method === "Page.screencastFrame") {
      frames++;
      if (msg.params?.sessionId != null) send("Page.screencastFrameAck", { sessionId: msg.params.sessionId });
    }
  };
  await sleep(1500);
  framesBeforeCover = frames;
  // Now COVER it with a full-screen opaque window (mimics Claude Desktop on top).
  const cover = launch(COVER_PORT, p2, ["--disable-features=ChromeWhatsNewUI"], "<body style=background:black></body>", "0,0", "3456,2234");
  await firstPage(COVER_PORT);
  await sleep(9000);          // let macOS occlusion debounce (~8s) fully engage
  frames = 0;                 // then count steady-state frames while covered
  await sleep(4000);
  const framesWhileCovered = frames + " (per 4s, steady-state occluded)";

  try { ws.close(); } catch {}
  kill(anim); kill(cover);
  await sleep(400);
  rmSync(p1, { recursive: true, force: true });
  rmSync(p2, { recursive: true, force: true });
  return { framesBeforeCover, framesWhileCovered };
}

function kill(proc) { try { process.kill(-proc.pid, "SIGKILL"); } catch {} }

const a = await run(false);
console.log("NO occlusion flags  :", JSON.stringify(a));
await sleep(1000);
const b = await run(true);
console.log("WITH occlusion flags:", JSON.stringify(b));
