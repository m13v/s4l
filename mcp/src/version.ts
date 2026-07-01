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

// Best-effort latest released version, cached per-process with a TTL. Never throws.
//
// SOURCE = GitHub releases/latest, NOT npm. This is deliberate and load-bearing:
//   • The .mcpb boxes that run the menu-bar have NO npm/npx on PATH (PATH is just
//     /usr/bin:/bin:/usr/sbin:/sbin). The old `npm view` probe threw there, so
//     `latest` was always null, `update_available` was always false, and the
//     "⬆ Update available" banner could NEVER fire on a box — even when a new
//     release was live. (That is exactly the bug this replaced: box stuck on
//     1.6.177 while 1.6.181 was out, banner silent.)
//   • GitHub releases/latest is the SAME source the box updater installs from
//     (scripts/s4l_box_update.sh + menu-bar `_mcpb_update_work` pull the .mcpb
//     from releases/latest/download). Detecting from the same place the update
//     comes from means "update available" and "what an update installs" can
//     never disagree (npm publish and the GitHub release step could otherwise
//     drift). curl lives at /usr/bin/curl, present in every PATH, so this works
//     with zero npm dependency.
// npm stays as a fallback only for the rare case GitHub is unreachable (and for
// dev machines checking a version that is on npm but not yet released).
let cache: { at: number; latest: string | null } | null = null;
const TTL_MS = 10 * 60 * 1000;

const RELEASES_LATEST_API =
  "https://api.github.com/repos/m13v/social-autoposter/releases/latest";

async function latestFromGithub(): Promise<string | null> {
  try {
    // -f: fail on HTTP error, -sSL: quiet + follow redirects, -m: hard timeout.
    // GitHub's releases/latest already excludes drafts and prereleases, so a
    // `--draft` release correctly does NOT trigger the update banner.
    const res = await run(
      "curl",
      ["-fsSL", "-m", "10", "-H", "Accept: application/vnd.github+json", RELEASES_LATEST_API],
      { timeoutMs: 12000, noTee: true }
    );
    const tag = (JSON.parse(res.stdout) as { tag_name?: unknown }).tag_name;
    const v = typeof tag === "string" ? tag.replace(/^v/, "").trim() : "";
    return /^\d+\.\d+\.\d+/.test(v) ? v : null;
  } catch {
    return null;
  }
}

async function latestFromNpm(): Promise<string | null> {
  try {
    const res = await run("npm", ["view", "social-autoposter", "version"], { timeoutMs: 8000 });
    const line = res.stdout.trim().split("\n").pop()?.trim() ?? "";
    return /^\d+\.\d+\.\d+/.test(line) ? line : null;
  } catch {
    return null;
  }
}

export async function latestPublishedVersion(): Promise<string | null> {
  const now = Date.now();
  if (cache && now - cache.at < TTL_MS) return cache.latest;
  // GitHub first (works on boxes, matches the updater), npm only as a fallback.
  let latest = await latestFromGithub();
  if (latest == null) latest = await latestFromNpm();
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
