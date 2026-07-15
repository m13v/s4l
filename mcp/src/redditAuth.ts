// Reddit session bootstrap for the setup flow (mirrors twitterAuth.ts).
//
// Thin wrapper over scripts/setup_reddit_auth.py, which owns the real work
// (ensure the managed reddit-harness Chrome on CDP 9557, validate the
// reddit.com session via api/me.json fetched same-origin from inside a
// logged-in page, and, if logged out, import reddit.com cookies from the
// user's everyday browser via the vendored copy_browser_cookies.py). We only
// shell out and parse JSON.
//
// Why a separate Python helper instead of doing CDP here: the cookie-import
// and browser-fetch primitives already exist and are battle-tested in the
// repo (copy_browser_cookies.py, reddit_browser_fetch.py). Reusing them keeps
// this MCP a thin client and keeps the reddit-specific launch-flag parity
// (NO --use-mock-keychain on the reddit-harness profile) in one place.

import { runPython } from "./repo.js";
import { captureError } from "./telemetry.js";

export interface RedditAuthResult {
  ok: boolean;
  connected: boolean;
  // browser_not_running | logged_out | connected | connected_idle | imported |
  // needs_login | browser_launch_failed | keychain_locked | suspended | error
  // connected_idle = a session exists (on-disk profile cookies, or an active
  // posting drain owns the tab) but the live me.json probe didn't run this
  // moment. Treated as connected by callers.
  state: string;
  // The logged-in reddit username when a valid session exists; null = unknown
  // (logged out / browser down), NEVER "missing".
  username?: string | null;
  // Account context from me.json so onboarding can set expectations: fresh
  // low-karma accounts get AutoMod-gated in most subreddits. `warning` is a
  // ready-to-relay sentence when that applies; it never blocks the connect.
  account_age_days?: number | null;
  comment_karma?: number | null;
  total_karma?: number | null;
  warning?: string | null;
  source?: string;
  note?: string;
  error?: string;
  error_type?: string;
  attempts?: Array<{ source: string; ok?: boolean; detail?: string }>;
  cdp?: string;
}

function parse(stdout: string, stderr: string, code: number): RedditAuthResult {
  try {
    return JSON.parse(stdout.trim().split("\n").slice(-80).join("\n")) as RedditAuthResult;
  } catch (e) {
    captureError(e, { component: "reddit_auth", phase: "parse", exit: String(code) });
    return {
      ok: false,
      connected: false,
      state: "error",
      error:
        `setup_reddit_auth.py produced no parseable JSON (exit ${code}).\n` +
        (stderr || stdout).split("\n").slice(-8).join("\n"),
    };
  }
}

// Probe-only: is the managed reddit session valid right now? Does NOT launch
// Chrome. Falls back to the on-disk profile-cookie check (connected_idle) when
// the harness is down, and short-circuits while a posting drain owns the tab.
export async function redditStatus(): Promise<RedditAuthResult> {
  const res = await runPython("scripts/setup_reddit_auth.py", ["status"], {
    timeoutMs: 90_000,
  });
  return parse(res.stdout, res.stderr, res.code);
}

// Ensure the harness browser is up, validate, and import reddit.com cookies
// from the user's everyday browser if needed. `source` optional (e.g.
// "arc:Default"); default auto-detects chrome/arc/brave/edge.
export async function redditConnect(
  source?: string,
  manualLogin?: boolean
): Promise<RedditAuthResult> {
  const args = ["connect"];
  if (source) args.push("--source", source);
  // Only pop a visible Reddit login window when the user explicitly asked to
  // sign in by hand (mirrors connect_x's no-surprise-window discipline).
  if (manualLogin) args.push("--manual-login");
  const res = await runPython("scripts/setup_reddit_auth.py", args, {
    // Import may pop a macOS Keychain dialog the user has to find + click, and
    // the manual-login wait is up to 300s. Keep this above the Python-side
    // cookie-copy timeout (S4L_COOKIE_COPY_TIMEOUT, default 600s).
    timeoutMs: 660_000,
  });
  return parse(res.stdout, res.stderr, res.code);
}

// A browser/profile the Reddit session can be imported from.
export interface RedditSource {
  spec: string;            // e.g. "chrome:Profile 1"
  browser: string;         // chrome | arc | brave | edge | chromium
  profile: string;         // Default | Profile 1 ...
  label: string;           // "Chrome — Profile 1"
  reddit_session: boolean; // does this profile already hold a reddit.com session?
}
export interface RedditSourcesResult {
  ok: boolean;
  sources: RedditSource[];
  recommended?: string;
  error?: string;
}

// List browsers/profiles to import from. Read-only: never reads the keychain
// or decrypts a cookie, so it shows no macOS Safe Storage prompt.
export async function redditDetectSources(): Promise<RedditSourcesResult> {
  const res = await runPython("scripts/setup_reddit_auth.py", ["detect-sources"], {
    timeoutMs: 30_000,
  });
  try {
    return JSON.parse(res.stdout.trim().split("\n").slice(-200).join("\n")) as RedditSourcesResult;
  } catch (e) {
    captureError(e, {
      component: "reddit_auth",
      phase: "detect_sources",
      exit: String(res.code),
    });
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
export function summarizeRedditAuth(r: RedditAuthResult): string {
  switch (r.state) {
    case "connected":
      return (
        "Reddit is connected" +
        (r.username ? ` as u/${r.username}` : "") +
        " (the autoposter's reddit browser has a valid session)."
      );
    case "connected_idle":
      return (
        "Reddit is connected (your session is saved in the autoposter's own browser " +
        "profile). The reddit browser isn't running this moment; the next pipeline " +
        "run restores it automatically, no action needed."
      );
    case "imported":
      return (
        `Reddit connected${r.username ? ` as u/${r.username}` : ""}; imported your ` +
        `session from ${r.source ?? "your browser"}.`
      );
    case "logged_out":
      return "Reddit is not connected: the autoposter's reddit browser has no valid session yet.";
    case "browser_not_running":
      return "The autoposter's reddit browser isn't running yet.";
    case "suspended":
      return (
        `The reddit account${r.username ? ` u/${r.username}` : ""} is suspended; ` +
        "posting is not possible. Connect a different account or resolve the suspension."
      );
    case "needs_login":
      return (
        r.note ??
        "Couldn't import a valid Reddit session automatically. Sign in yourself in the " +
          "autoposter's Chrome window at the Reddit login page, then run connect_reddit again."
      );
    case "keychain_locked":
      return r.note ?? "Cookie import blocked: the macOS keychain is not accessible from this session.";
    case "browser_launch_failed":
      return r.error ?? "Could not start the autoposter's reddit browser.";
    default:
      return r.error ?? `Reddit auth state: ${r.state}`;
  }
}
