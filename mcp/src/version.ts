// Version resolution + update checks for the social-autoposter MCP.
//
// The "real" version is the top-level `social-autoposter` npm package version
// (e.g. 1.6.x) — that is what actually bundles this MCP's prebuilt dist/. The
// MCP's own package.json and manifest are stamped to the same version at release
// time (scripts/release-mcpb.sh step 3b), but historically they were frozen at
// 0.0.1, so this module still resolves the true version from the most
// authoritative source available at runtime (and tolerates a stale co-located
// package.json on an old bundle). It can also check npm for a newer published
// release so we can deliver updates on demand.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { repoDir, run } from "./repo.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

function readJsonVersion(p: string): string | null {
  try {
    const v = (JSON.parse(fs.readFileSync(p, "utf-8")) as { version?: unknown }).version;
    return typeof v === "string" && v.length > 0 ? v : null;
  } catch {
    return null;
  }
}

// Resolve the REAL shipped version. Priority, most authoritative first:
//   1. dist/version.json  — stamped by the CLI installer (bin/cli.js installMcp)
//      from the npm package version at every init/update. Authoritative on a
//      real user install, where the top-level package.json is NOT copied.
//   2. <repo>/package.json — git checkout / dev machine: the meaningful 1.6.x.
//   3. mcp/package.json    — co-located last resort (release-stamped to match,
//      but may be stale on an older bundle).
export function resolveVersion(): string {
  return (
    readJsonVersion(path.join(__dirname, "version.json")) ||
    readJsonVersion(path.join(repoDir(), "package.json")) ||
    readJsonVersion(path.join(__dirname, "..", "package.json")) ||
    "0.0.0-unknown"
  );
}

export const VERSION = resolveVersion();

// Best-effort latest published version from npm, cached per-process with a TTL.
// Uses `npm view` (already a hard dependency of the installer) instead of fetch
// so we need no DOM lib types and no extra network plumbing. Never throws.
let cache: { at: number; latest: string | null } | null = null;
const TTL_MS = 10 * 60 * 1000;

export async function latestPublishedVersion(): Promise<string | null> {
  const now = Date.now();
  if (cache && now - cache.at < TTL_MS) return cache.latest;
  let latest: string | null = null;
  try {
    const res = await run("npm", ["view", "social-autoposter", "version"], { timeoutMs: 8000 });
    const line = res.stdout.trim().split("\n").pop()?.trim() ?? "";
    if (/^\d+\.\d+\.\d+/.test(line)) latest = line;
  } catch {
    latest = null;
  }
  cache = { at: now, latest };
  return latest;
}

// semver-ish compare: true when `latest` is strictly newer than `current`.
// Ignores prerelease/build suffixes; good enough for "is an update available".
export function isNewer(latest: string, current: string): boolean {
  const norm = (v: string) =>
    v.split(/[-+]/)[0].split(".").map((n) => parseInt(n, 10) || 0);
  const a = norm(latest);
  const b = norm(current);
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    const x = a[i] ?? 0;
    const y = b[i] ?? 0;
    if (x !== y) return x > y;
  }
  return false;
}

// One-shot convenience: installed + latest + whether an update is available.
export async function versionStatus(): Promise<{
  installed: string;
  latest: string | null;
  update_available: boolean;
}> {
  const latest = await latestPublishedVersion();
  return {
    installed: VERSION,
    latest,
    update_available: !!latest && isNewer(latest, VERSION),
  };
}
