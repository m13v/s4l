// Twitter/X session bootstrap for the setup flow.
//
// Thin wrapper over scripts/setup_twitter_auth.py, which owns the real work
// (ensure the managed Chrome on CDP 9555, validate the x.com session, and, if
// logged out, import x.com/twitter.com cookies from the user's everyday
// browser via ai_browser_profile.cookies). We only shell out and parse JSON.
//
// Why a separate Python helper instead of doing CDP here: the validation +
// cookie-import primitives already exist and are battle-tested in the repo
// (restore_twitter_session.py for CDP login-check, ai_browser_profile.cookies
// for Keychain-decrypt + CDP inject). Reusing them keeps this MCP a thin client.

import { runPython } from "./repo.js";

export interface XAuthResult {
  ok: boolean;
  connected: boolean;
  // browser_not_running | logged_out | connected | imported | needs_login |
  // browser_launch_failed | error
  state: string;
  // The logged-in @handle when a valid session exists; null = unknown (logged
  // out / browser down), NEVER "missing". setup_twitter_auth.py owns this.
  handle?: string | null;
  source?: string;
  note?: string;
  error?: string;
  attempts?: Array<{ source: string; ok?: boolean; detail?: string }>;
  cdp?: string;
}

function parse(stdout: string, stderr: string, code: number): XAuthResult {
  try {
    return JSON.parse(stdout.trim().split("\n").slice(-50).join("\n")) as XAuthResult;
  } catch {
    return {
      ok: false,
      connected: false,
      state: "error",
      error:
        `setup_twitter_auth.py produced no parseable JSON (exit ${code}).\n` +
        (stderr || stdout).split("\n").slice(-8).join("\n"),
    };
  }
}

// Probe-only: is the managed X session valid right now? Does NOT launch Chrome.
export async function xStatus(): Promise<XAuthResult> {
  const res = await runPython("scripts/setup_twitter_auth.py", ["status"], {
    timeoutMs: 90_000,
  });
  return parse(res.stdout, res.stderr, res.code);
}

// Ensure the browser is up, validate, and import cookies from the user's
// everyday browser if needed. `source` optional (e.g. "arc:Default"); default
// auto-detects chrome/arc/brave/edge.
export async function xConnect(source?: string): Promise<XAuthResult> {
  const args = ["connect"];
  if (source) args.push("--source", source);
  const res = await runPython("scripts/setup_twitter_auth.py", args, {
    // import opens a real Chrome and may pop a macOS Keychain auth dialog the
    // user has to find + click ("Always Allow"). Keep this above the Python
    // cookie-copy timeout (SAPS_COOKIE_COPY_TIMEOUT, default 600s) so the
    // wrapper never kills the dialog before the human can.
    timeoutMs: 660_000,
  });
  return parse(res.stdout, res.stderr, res.code);
}

// ---------------------------------------------------------------------------
// Profile scan: build a "grounding truth" corpus from the connected account.
// ---------------------------------------------------------------------------
// Runs right after connect_x detects the @handle. Reuses the SAME authenticated
// managed-Chrome session to read the user's bio + recent posts + recent replies,
// so the setup conversation can draft voice/icp/topics in the user's own register
// instead of generic marketing copy. Read-only: never posts, clicks, or writes.
export interface XProfileScan {
  ok: boolean;
  state: string; // scanned | browser_not_running | no_handle | error
  handle?: string;
  profile?: {
    name?: string;
    bio?: string;
    location?: string;
    url?: string;
    join?: string;
    followers?: string;
    following?: string;
    pinned?: string;
  };
  posts?: Array<{ text: string; url?: string; id?: string; likes?: number }>;
  comments?: Array<{ text: string; url?: string; id?: string; reply_to?: string }>;
  counts?: { posts: number; comments: number };
  grounding_instructions?: string;
  error?: string;
}

export async function xScanProfile(opts?: {
  handle?: string;
  posts?: number;
  comments?: number;
}): Promise<XProfileScan> {
  const args: string[] = [];
  if (opts?.handle) args.push("--handle", opts.handle);
  args.push("--posts", String(opts?.posts ?? 20));
  args.push("--comments", String(opts?.comments ?? 50));
  // The scan scrolls two timelines; give it room but keep it bounded.
  const res = await runPython("scripts/scan_x_profile.py", args, { timeoutMs: 180_000 });
  try {
    return JSON.parse(res.stdout.trim().split("\n").slice(-1).join("\n")) as XProfileScan;
  } catch {
    return {
      ok: false,
      state: "error",
      error:
        `scan_x_profile.py produced no parseable JSON (exit ${res.code}).\n` +
        (res.stderr || res.stdout).split("\n").slice(-8).join("\n"),
    };
  }
}

// A browser/profile the X session can be imported from (for the panel dropdown).
export interface XSource {
  spec: string;        // e.g. "chrome:Profile 1"
  browser: string;     // chrome | arc | brave | edge | chromium
  profile: string;     // Default | Profile 1 ...
  label: string;       // "Chrome — Profile 1"
  x_session: boolean;  // does this profile already hold an x.com auth_token?
}
export interface XSourcesResult {
  ok: boolean;
  sources: XSource[];
  recommended?: string;
  error?: string;
}

// List browsers/profiles to import from. Read-only: NEVER reads the keychain or
// decrypts a cookie, so it shows no macOS Safe Storage prompt. Used to populate
// the panel's "import from" dropdown and to flag which profile has a live session.
export async function xDetectSources(): Promise<XSourcesResult> {
  const res = await runPython("scripts/setup_twitter_auth.py", ["detect-sources"], {
    timeoutMs: 30_000,
  });
  try {
    return JSON.parse(res.stdout.trim().split("\n").slice(-200).join("\n")) as XSourcesResult;
  } catch {
    return {
      ok: false,
      sources: [],
      error:
        `detect-sources produced no parseable JSON (exit ${res.code}).\n` +
        (res.stderr || res.stdout).split("\n").slice(-8).join("\n"),
    };
  }
}

// One-line human summary for tool output.
export function summarizeXAuth(r: XAuthResult): string {
  switch (r.state) {
    case "connected":
      return "X is connected (the autoposter browser has a valid x.com session).";
    case "imported":
      return `X connected — imported your session from ${r.source ?? "your browser"}.`;
    case "logged_out":
      return "X is not connected: the autoposter browser has no valid x.com session yet.";
    case "browser_not_running":
      return "The autoposter's X browser isn't running yet.";
    case "needs_login":
      // Prefer the helper's note: it says whether the login window actually
      // came to the front and carries the full manual-login instructions.
      return (
        r.note ??
        "Couldn't import a valid X session automatically. A Chrome window is open at " +
          "the X login page — sign in there yourself (username, password, 2FA), then " +
          "run connect_x again to confirm."
      );
    case "browser_launch_failed":
      return r.error ?? "Could not start the autoposter browser.";
    default:
      return r.error ?? `X auth state: ${r.state}`;
  }
}
