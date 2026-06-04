#!/usr/bin/env node
// CLI entry for provisioning the owned Python/Chromium runtime.
//
// This is the terminal-side twin of the panel's "Install runtime" button and
// the `install_runtime` MCP tool: all three call the SAME provisioning logic in
// dist/runtime.js, so there is one implementation and one source of truth. The
// panel polls install_status; this script polls readProgress() and prints each
// step transition to stdout so an agent (or a human) can install head-less when
// the UI panel isn't available (Claude Code/Cowork, CI, a bare VM).
//
// Exit code: 0 when the runtime is ready, 1 on any step failure.

import {
  startProvisioning,
  readProgress,
  runtimeReady,
  readRuntime,
} from "./dist/runtime.js";

const GLYPH = { pending: "·", running: "…", done: "✓", error: "×" };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  if (runtimeReady()) {
    const rt = readRuntime();
    console.log(`Runtime already installed; nothing to do.`);
    console.log(`  python: ${rt?.python}`);
    console.log(`  uv:     ${rt?.uv}`);
    return 0;
  }

  console.log("Installing the social-autoposter runtime (uv, Python, Chromium).");
  console.log("This is a one-time download; nothing touches your system Python.\n");

  startProvisioning();

  // Print each step the first time it leaves "pending", and again when it
  // finishes, so the terminal shows live forward motion instead of going dark.
  const printed = new Map(); // step id -> last status printed
  for (;;) {
    const p = readProgress();
    if (p) {
      for (const s of p.steps) {
        if (printed.get(s.id) !== s.status && s.status !== "pending") {
          const detail = s.status === "error" && s.detail ? `  ${s.detail}` : "";
          console.log(`  ${GLYPH[s.status] || "·"} ${s.label}${detail}`);
          printed.set(s.id, s.status);
        }
      }
      if (p.done) {
        if (p.ok) {
          const rt = readRuntime();
          console.log(`\nRuntime ready.`);
          console.log(`  python: ${rt?.python}`);
          return 0;
        }
        console.log(`\nInstall failed: ${p.error || "see the step marked × above."}`);
        return 1;
      }
    }
    await sleep(1000);
  }
}

main()
  .then((code) => process.exit(code))
  .catch((err) => {
    console.error(`install-runtime crashed: ${err?.stack || err}`);
    process.exit(1);
  });
