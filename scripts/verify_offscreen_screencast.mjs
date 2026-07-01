// Throwaway validation for the "Connecting…" screencast fix.
// Launches Chrome OFF-SCREEN (like the plugin's twitter-backend.sh default
// window-position 3042,-1032) on a throwaway port + temp profile, attaches a
// CDP screencast exactly like mcp/src/screencast.ts, and counts frames over a
// short window — once WITHOUT the occlusion flags, once WITH. Does NOT touch
// port 9555, the real profile, or the pipeline.

import { spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const PORT = 9591;
const WS = globalThis.WebSocket;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function fetchJson(url) {
  try { const r = await fetch(url); return r.ok ? await r.json() : null; } catch { return null; }
}

async function countFrames(withFlags) {
  const profile = mkdtempSync(join(tmpdir(), "saps-scast-"));
  const args = [
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${profile}`,
    "--no-first-run", "--no-default-browser-check",
    "--password-store=basic", "--use-mock-keychain",
    "--window-position=3042,-1032",     // off-screen, mirrors the pipeline
    "--window-size=1024,1013",
  ];
  if (withFlags) {
    args.push("--disable-features=ChromeWhatsNewUI,CalculateNativeWinOcclusion");
    args.push("--disable-backgrounding-occluded-windows");
  } else {
    args.push("--disable-features=ChromeWhatsNewUI");
  }
  // Animated page: a rAF loop that repaints every frame. rAF + compositing are
  // both throttled to ~zero when Chrome considers the window occluded, so the
  // ongoing frame count is a direct readout of "is this window composited?".
  const html = "<body style=margin:0><div id=b style='width:100px;height:100px;background:red;position:absolute'></div><script>let x=0;function f(){x=(x+7)%900;const b=document.getElementById('b');b.style.left=x+'px';b.style.background='hsl('+x+',90%,50%)';requestAnimationFrame(f)}requestAnimationFrame(f)</script></body>";
  args.push("data:text/html," + encodeURIComponent(html));
  const proc = spawn(CHROME, args, { stdio: "ignore", detached: true });

  // wait for CDP
  let target = null;
  for (let i = 0; i < 20 && !target; i++) {
    await sleep(500);
    const list = await fetchJson(`http://127.0.0.1:${PORT}/json`);
    if (Array.isArray(list)) {
      const pages = list.filter((t) => t.type === "page" && t.webSocketDebuggerUrl && !String(t.url || "").startsWith("devtools://"));
      if (pages.length) target = pages[0];
    }
  }
  if (!target) { try { process.kill(-proc.pid, "SIGKILL"); } catch {} rmSync(profile, { recursive: true, force: true }); return "no_target"; }

  let frames = 0;
  const ws = new WS(target.webSocketDebuggerUrl);
  let id = 1;
  const send = (method, params) => ws.send(JSON.stringify({ id: id++, method, params: params || {} }));
  await new Promise((res) => { ws.onopen = res; });
  send("Page.enable");
  if (withFlags) send("Page.bringToFront");   // the screencast.ts companion fix
  send("Page.startScreencast", { format: "jpeg", quality: 55, maxWidth: 1280, maxHeight: 800, everyNthFrame: 1 });
  ws.onmessage = (ev) => {
    const msg = JSON.parse(typeof ev.data === "string" ? ev.data : String(ev.data));
    if (msg.method === "Page.screencastFrame") {
      frames++;
      if (msg.params?.sessionId != null) send("Page.screencastFrameAck", { sessionId: msg.params.sessionId });
    }
  };
  await sleep(3500);
  try { ws.close(); } catch {}
  try { process.kill(-proc.pid, "SIGKILL"); } catch {}
  await sleep(400);
  rmSync(profile, { recursive: true, force: true });
  return frames;
}

const without = await countFrames(false);
console.log(`OFF-SCREEN, no occlusion flags  -> frames: ${without}`);
const withF = await countFrames(true);
console.log(`OFF-SCREEN, WITH occlusion flags -> frames: ${withF}`);
