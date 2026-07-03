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
import os from "node:os";
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
let cache: {
  at: number;
  latest: string | null;
  tag: string | null;
  channel: "stable" | "staging";
} | null = null;
const TTL_MS = 55 * 1000;

const RELEASES_LATEST_URL = "https://github.com/m13v/s4l/releases/latest";
const RELEASES_LATEST_API =
  "https://api.github.com/repos/m13v/s4l/releases/latest";
// Staging channel resolves the newest release OVERALL (prereleases included)
// from the releases LIST, since releases/latest excludes prereleases. Keep this
// and verKey/isNewer in lockstep with scripts/snapshot.py.
const RELEASES_LIST_API =
  "https://api.github.com/repos/m13v/s4l/releases?per_page=30";

// Per-box release channel. `staging` boxes track the newest release overall
// (RCs first); everything else is `stable` (releases/latest, the historical
// default). Single source of truth shared with snapshot.py / s4l_channel.py:
// <state dir>/channel.json. Read fresh each call (cheap file read) so a channel
// flip takes effect on the next probe with no restart.
const STATE_DIR =
  process.env.S4L_STATE_DIR || process.env.SAPS_STATE_DIR || path.join(os.homedir(), ".social-autoposter-mcp");

export function releaseChannel(): "stable" | "staging" {
  try {
    const raw = fs.readFileSync(path.join(STATE_DIR, "channel.json"), "utf-8");
    const v = (JSON.parse(raw) || {}).channel;
    if (v === "staging" || v === "stable") return v;
  } catch {
    /* absent/corrupt -> stable (fail-safe: never silently push a box to prerelease) */
  }
  return "stable";
}

function parseSemverish(v: string): string | null {
  return /^\d+\.\d+\.\d+/.test(v) ? v : null;
}

// Precedence key for an rc-aware compare: a full release outranks any prerelease
// of the SAME core version (1.6.193 > 1.6.193-rc.2 > 1.6.193-rc.1). For stable
// (no prereleases compared) this reduces to a plain numeric core compare, so
// behavior there is unchanged. Mirrors snapshot.py::_ver_key.
function verKey(v: string): [number, number, number, number, number] {
  const s = String(v).trim().replace(/^v/, "");
  const [coreRaw, pre = ""] = s.split("-", 2);
  const core = coreRaw.split("+")[0];
  const nums = core.split(".").map((n) => parseInt(n, 10) || 0);
  while (nums.length < 3) nums.push(0);
  if (!pre) return [nums[0], nums[1], nums[2], 1, 0];
  const m = pre.match(/\d+/g);
  return [nums[0], nums[1], nums[2], 0, m ? parseInt(m[m.length - 1], 10) : 0];
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

// Staging channel: newest release OVERALL (prereleases included) from the
// releases LIST, since releases/latest excludes prereleases. Drafts are skipped;
// "newest" is by the rc-aware verKey. Returns {version, tag} or null.
async function latestFromGithubListStaging(): Promise<{ version: string; tag: string } | null> {
  try {
    const res = await run(
      "curl",
      ["-sS", "-m", "10", "-H", "Accept: application/vnd.github+json", RELEASES_LIST_API],
      { timeoutMs: 12000, noTee: true }
    );
    const rels = JSON.parse(res.stdout || "[]");
    if (!Array.isArray(rels)) return null;
    let best: { version: string; tag: string; key: number[] } | null = null;
    for (const r of rels) {
      if (!r || typeof r !== "object" || r.draft) continue;
      const tag = (r as { tag_name?: unknown }).tag_name;
      if (typeof tag !== "string") continue;
      const v = parseSemverish(tag.replace(/^v/, "").trim());
      if (!v) continue;
      const key = verKey(v);
      if (best == null || cmpKey(key, best.key) > 0) best = { version: v, tag, key };
    }
    return best ? { version: best.version, tag: best.tag } : null;
  } catch {
    return null;
  }
}

function cmpKey(a: number[], b: number[]): number {
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    const x = a[i] ?? 0;
    const y = b[i] ?? 0;
    if (x !== y) return x > y ? 1 : -1;
  }
  return 0;
}

// Resolve the newest release for this box's channel, cached (with the channel,
// so a mid-process flip re-probes). Returns the version AND the release tag: the
// staging download URL is built from the tag, stable uses releases/latest.
async function resolveLatest(): Promise<{
  version: string | null;
  tag: string | null;
  channel: "stable" | "staging";
}> {
  const channel = releaseChannel();
  const now = Date.now();
  if (cache && cache.channel === channel && now - cache.at < TTL_MS)
    return { version: cache.latest, tag: cache.tag, channel };
  let version: string | null = null;
  let tag: string | null = null;
  if (channel === "staging") {
    const s = await latestFromGithubListStaging();
    if (s) {
      version = s.version;
      tag = s.tag;
    } else {
      // Degrade to the stable probes so a staging box tracks at least stable
      // rather than going blind when the list endpoint fails.
      version = (await latestFromGithubApi()) ?? (await latestFromGithubRedirect());
      tag = version ? `v${version}` : null;
    }
  } else {
    version = await latestFromGithubApi();
    if (version == null) version = await latestFromGithubRedirect();
    if (version == null) version = await latestFromNpm();
    tag = version ? `v${version}` : null;
  }
  cache = { at: now, latest: version, tag, channel };
  return { version, tag, channel };
}

export async function latestPublishedVersion(): Promise<string | null> {
  return (await resolveLatest()).version;
}

// rc-aware compare: true when `latest` is strictly newer than `current`. A full
// release outranks any prerelease of the same core version, so on the staging
// channel one RC correctly supersedes another (1.6.193-rc.2 > 1.6.193-rc.1) and
// the promoted full release supersedes its RCs. Mirrors snapshot.py::_is_newer.
export function isNewer(latest: string, current: string): boolean {
  return cmpKey(verKey(latest), verKey(current)) > 0;
}

// One-shot convenience: installed + latest + whether an update is available,
// plus the resolved channel and release tag (the staging updater needs the tag).
export async function versionStatus(): Promise<{
  installed: string;
  latest: string | null;
  latest_tag: string | null;
  channel: "stable" | "staging";
  update_available: boolean;
}> {
  const { version: latest, tag, channel } = await resolveLatest();
  return {
    installed: VERSION,
    latest,
    latest_tag: tag,
    channel,
    update_available: !!latest && isNewer(latest, VERSION),
  };
}
