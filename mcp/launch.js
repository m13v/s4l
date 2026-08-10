#!/bin/sh
':' //; S4L_ROOT="$(cd "$(dirname "$0")" && pwd)"; case "$(uname -m)" in x86_64|amd64) S4L_NODE_ARCH=x64 ;; *) S4L_NODE_ARCH=arm64 ;; esac; S4L_NODE="$S4L_ROOT/vendor/node-darwin-$S4L_NODE_ARCH/bin/node"; [ -x "$S4L_NODE" ] || S4L_NODE="$S4L_ROOT/vendor/node-darwin-arm64/bin/node"; [ -x "$S4L_NODE" ] || S4L_NODE="$(command -v node || echo node)"; exec "$S4L_NODE" "$S4L_ROOT/dist/index.js" "$@"; exit 127

// S4L launcher: one file, two runtimes.
//
// Claude Desktop only treats a node-type extension as COMPATIBLE when its
// mcp_config.command is something it recognizes as Node: the bare string
// "node", or a path ending in .js (checked before install, in the app's
// extension-compatibility gate). Our previous command pointed at the vendored
// node BINARY, which that gate does not recognize, so every Mac without a
// system Node.js got "this extension is incompatible" on install. This file
// exists to pass that gate while still running our own vendored runtime.
//
// The same file must serve three spawn paths:
//   1. Desktop chat bridge: runs this file with Claude's built-in Node
//      (Electron UtilityProcess). The JS below re-execs the vendored,
//      arch-matched Node so behavior matches every other path.
//   2. Agent-mode/Cowork and any literal spawner: exec()s this file directly.
//      The #!/bin/sh shebang runs the one-line sh half above, which picks the
//      vendored Node by `uname -m` and never needs a system Node.
//   3. A plain `node launch.js` (dev, install.sh): same as path 1.
//
// The `':' //;` line is the standard sh/JS polyglot: sh sees a no-op `:`
// followed by real commands ending in exec (so sh never parses the JS below);
// JS sees a string literal and a line comment.

const REEXEC_ENV = "S4L_LAUNCHER_REEXEC";

Promise.resolve().then(async () => {
  const path = await import("node:path");
  const fs = await import("node:fs");
  const { spawn } = await import("node:child_process");
  const { pathToFileURL } = await import("node:url");

  const root = path.dirname(path.resolve(process.argv[1] || "."));
  const serverEntry = path.join(root, "dist", "index.js");
  const runInProcess = () => import(pathToFileURL(serverEntry).href);

  const arch = process.arch === "x64" ? "x64" : "arm64";
  let vendored = path.join(root, "vendor", `node-darwin-${arch}`, "bin", "node");
  if (!fs.existsSync(vendored)) {
    vendored = path.join(root, "vendor", "node-darwin-arm64", "bin", "node");
  }

  if (
    process.env[REEXEC_ENV] === "1" ||
    process.platform !== "darwin" ||
    !fs.existsSync(vendored)
  ) {
    return runInProcess();
  }

  const child = spawn(vendored, [serverEntry, ...process.argv.slice(2)], {
    stdio: "inherit",
    env: { ...process.env, [REEXEC_ENV]: "1" },
  });

  let spawned = false;
  child.once("spawn", () => {
    spawned = true;
  });
  // ENOENT/EPERM before the child ever starts (e.g. wrong-arch binary on an
  // exotic host): the stdio pipes are still untouched, so serving the MCP
  // session in-process under the current runtime is safe.
  child.once("error", (err) => {
    if (!spawned) {
      runInProcess().catch((e) => {
        console.error("[s4l-launch] fallback failed:", e, "after spawn error:", err);
        process.exit(1);
      });
    } else {
      console.error("[s4l-launch] vendored node crashed:", err);
      process.exit(1);
    }
  });
  child.once("exit", (code, signal) => {
    process.exit(signal ? 1 : code ?? 0);
  });
  for (const sig of ["SIGINT", "SIGTERM", "SIGHUP"]) {
    process.on(sig, () => child.kill(sig));
  }
});
