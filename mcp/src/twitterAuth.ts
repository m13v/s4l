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
    // import opens a real Chrome and may navigate to x.com/home a couple times
    timeoutMs: 180_000,
  });
  return parse(res.stdout, res.stderr, res.code);
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
