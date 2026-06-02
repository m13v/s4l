#!/usr/bin/env node
// Installs the social-autoposter MCP server into BOTH Claude Desktop and Claude Code.
//
//   node install.mjs            # install into both clients
//   node install.mjs --uninstall  # remove from both clients
//
// Idempotent: re-running overwrites the existing entry. Each config file is
// backed up (timestamped) before it is touched, and missing files/dirs are created.

import { execSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const SERVER_KEY = "social-autoposter";
const UNINSTALL = process.argv.includes("--uninstall");

// ---- resolve the absolute paths we want pinned into the spawn env ----------
const here = path.dirname(new URL(import.meta.url).pathname);
const repoDir = path.resolve(here, "..");
const distEntry = path.join(here, "dist", "index.js");

function whichNode() {
  // Prefer the stable symlink over a versioned Cellar path so a `brew upgrade
  // node` doesn't break the pinned command.
  for (const p of ["/opt/homebrew/bin/node", "/usr/local/bin/node"]) {
    if (fs.existsSync(p)) return p;
  }
  return process.execPath; // the node running this installer
}

const nodeBin = whichNode();

function whichPython() {
  // Prefer a real Homebrew python (has the pipeline's deps) over the macOS
  // /usr/bin/python3 stub, which can trigger an Xcode CLT install prompt and
  // often lacks psycopg2 etc.
  const candidates = ["/opt/homebrew/bin/python3", "/usr/local/bin/python3"];
  try {
    const found = execSync("command -v python3 2>/dev/null", { shell: "/bin/bash" })
      .toString()
      .trim();
    if (found) candidates.push(found);
  } catch {}
  candidates.push("/usr/bin/python3");
  for (const p of candidates) {
    if (p && fs.existsSync(p)) return p;
  }
  return "python3"; // last resort: rely on PATH
}

const pythonBin = whichPython();

const serverEntry = {
  command: nodeBin,
  args: [distEntry],
  env: {
    SAPS_PYTHON: pythonBin,
    SAPS_REPO_DIR: repoDir,
    PATH: `${path.dirname(nodeBin)}:${path.dirname(pythonBin)}:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin`,
  },
};

// ---- the two client config targets ----------------------------------------
const home = os.homedir();
const targets = [
  {
    name: "Claude Desktop",
    file: path.join(
      home,
      "Library",
      "Application Support",
      "Claude",
      "claude_desktop_config.json",
    ),
  },
  {
    name: "Claude Code",
    file: path.join(home, ".claude.json"),
  },
];

function readJson(file) {
  if (!fs.existsSync(file)) return {};
  const raw = fs.readFileSync(file, "utf8").trim();
  if (!raw) return {};
  return JSON.parse(raw);
}

function backup(file) {
  if (!fs.existsSync(file)) return null;
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const bak = `${file}.bak-${stamp}`;
  fs.copyFileSync(file, bak);
  return bak;
}

function writeJson(file, obj) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, JSON.stringify(obj, null, 2) + "\n", "utf8");
}

let ok = 0;
for (const t of targets) {
  try {
    const config = readJson(t.file);
    config.mcpServers = config.mcpServers || {};

    if (UNINSTALL) {
      if (!(SERVER_KEY in config.mcpServers)) {
        console.log(`• ${t.name}: not present, nothing to remove`);
        ok++;
        continue;
      }
      const bak = backup(t.file);
      delete config.mcpServers[SERVER_KEY];
      writeJson(t.file, config);
      console.log(`✓ ${t.name}: removed "${SERVER_KEY}"  (backup: ${bak})`);
      ok++;
      continue;
    }

    const existed = SERVER_KEY in config.mcpServers;
    const bak = backup(t.file);
    config.mcpServers[SERVER_KEY] = serverEntry;
    writeJson(t.file, config);
    console.log(
      `✓ ${t.name}: ${existed ? "updated" : "added"} "${SERVER_KEY}"` +
        (bak ? `  (backup: ${bak})` : "  (created new file)"),
    );
    console.log(`    -> ${t.file}`);
    ok++;
  } catch (err) {
    console.error(`✗ ${t.name}: ${err.message}`);
  }
}

console.log("");
if (ok === targets.length) {
  if (UNINSTALL) {
    console.log("Done. Removed from both clients.");
    console.log("");
    console.log(
      "Fully QUIT and relaunch each client (Cmd+Q for Claude Desktop; restart any open",
    );
    console.log(
      "Claude Code session) so the removal takes effect. MCP servers load at launch.",
    );
  } else {
    console.log("Done. Registered in both clients.");
    console.log("node:   " + nodeBin);
    console.log("python: " + pythonBin);
    console.log("server: " + distEntry);
    console.log("");
    console.log(
      "The MCP server is registered but not yet loaded (MCP servers load at launch,",
    );
    console.log("not per-tab). To finish setup:");
    console.log("");
    console.log("  1. Fully quit Claude (Cmd+Q; closing the window is not enough).");
    console.log("  2. Reopen Claude.");
    console.log('  3. Send: "Set me up on social-autoposter."');
  }
} else {
  console.error(
    `Partial: ${ok}/${targets.length} clients updated. See errors above.`,
  );
  process.exit(1);
}
