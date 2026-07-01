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
//
// TTL is ~1 minute (a new release must surface within a minute — mirrored in
// scripts/snapshot.py, the copy the menu bar actually renders from; keep the two
// in lockstep). Probe order (measured 2026-07-01 releasing v1.6.188):
//   1. api.github.com releases/latest with a CONDITIONAL request (If-None-Match).
//      The API reflects a new release near-instantly, and GitHub does NOT count
//      304 responses against the unauthenticated 60/h-per-IP quota, so a 1-min
//      cadence is quota-free between releases (each release costs one 200). A
//      plain (unconditional) 1-min API poll would burn the whole quota.
//   2. The website redirect (302 to /releases/tag/vX.Y.Z): un-rate-limited
//      fallback, but GitHub's web tier lagged the API by ~2 minutes, so not primary.
//   3. npm (dev machines only; boxes have no npm).
let cache: { at: number; latest: string | null } | null = null;
const TTL_MS = 55 * 1000;

const RELEASES_LATEST_URL = "https://github.com/m13v/social-autoposter/releases/latest";
const RELEASES_LATEST_API =
  "https://api.github.com/repos/m13v/social-autoposter/releases/latest";

function parseSemverish(v: string): string | null {
  return /^\d+\.\d+\.\d+/.test(v) ? v : null;
}

async function latestFromGithubRedirect(): Promise<string | null> {
  try {
    // releases/latest already excludes drafts and prereleases, so a `--draft`
    // release correctly does NOT trigger the update banner. No -L: read the
    // first response's Location via %{redirect_url} and stop.
    const res = await run(
      "curl",
      ["-fsS", "-m", "10", "-o", "/dev/null", "-w", "%{redirect_url}", RELEASES_LATEST_URL],
      { timeoutMs: 12000, noTee: true }
    );
    const loc = (res.stdout || "").trim();
    if (!loc.includes("/releases/tag/")) return null;
    return parseSemverish(loc.split("/").pop()!.replace(/^v/, "").trim());
  } catch {
    return null;
  }
}

// In-process conditional-request state: long-lived processes send If-None-Match
// on every probe and get free 304s; each new release costs a single 200.
const apiState: { etag: string | null; latest: string | null } = { etag: null, latest: null };

async function latestFromGithubApi(): Promise<string | null> {
  try {
    const args = ["-sS", "-m", "10", "-H", "Accept: application/vnd.github+json"];
    if (apiState.etag) args.push("-H", `If-None-Match: ${apiState.etag}`);
    args.push(
      "-w",
      "\n__CURL_STATUS__:%{http_code}\n__CURL_ETAG__:%header{etag}",
      RELEASES_LATEST_API
    );
    const res = await run("curl", args, { timeoutMs: 12000, noTee: true });
    let status = 0;
    let etag: string | null = null;
    const body: string[] = [];
    for (const line of (res.stdout || "").split("\n")) {
      if (line.startsWith("__CURL_STATUS__:")) status = parseInt(line.slice(16).trim(), 10) || 0;
      else if (line.startsWith("__CURL_ETAG__:")) etag = line.slice(14).trim() || null;
      else body.push(line);
    }
    if (status === 304) return apiState.latest;
    if (status !== 200) return null;
    const tag = (JSON.parse(body.join("\n")) as { tag_name?: unknown }).tag_name;
    const v = typeof tag === "string" ? parseSemverish(tag.replace(/^v/, "").trim()) : null;
    if (v) {
      apiState.etag = etag;
      apiState.latest = v;
    }
    return v;
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
  let latest = await latestFromGithubApi();
  if (latest == null) latest = await latestFromGithubRedirect();
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
