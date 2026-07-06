"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const PHASES = new Set(["pre_connect", "full"]);

function existsExecutable(file) {
  if (!file || !fs.existsSync(file)) return false;
  try {
    fs.accessSync(file, fs.constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

function findFirst(candidates) {
  return candidates.find(existsExecutable) || "";
}

function findPython(home, preferred) {
  return (
    findFirst([
      preferred,
      path.join(home, ".social-autoposter-mcp", "runtime", ".venv", "bin", "python3"),
      "/opt/homebrew/bin/python3",
      "/usr/local/bin/python3",
      "/usr/bin/python3",
    ]) || "python3"
  );
}

function findUv(home) {
  return findFirst([
    path.join(home, ".local", "bin", "uv"),
    "/opt/homebrew/bin/uv",
    "/usr/local/bin/uv",
    "/usr/bin/uv",
  ]);
}

function result(status, detail, fix) {
  return { status, detail, ...(fix ? { fix } : {}) };
}

function runDoctorSync(options = {}) {
  const phase = PHASES.has(options.phase) ? options.phase : "full";
  const home = options.home || os.homedir();
  const repoDir =
    options.repoDir ||
    path.join(home, "social-autoposter");
  const python = findPython(home, options.python);
  const harness = path.join(home, ".local", "bin", "browser-harness");
  const cookiesDb = path.join(
    home,
    ".claude",
    "browser-profiles",
    "browser-harness",
    "Default",
    "Cookies"
  );
  const mirrorPath = path.join(
    home,
    ".claude",
    "browser-profiles",
    "browser-harness.x-cookies.json"
  );
  const setupScript = path.join(repoDir, "scripts", "setup_twitter_auth.py");
  const startedAt = new Date();
  const checks = [];
  const add = (id, name, runner) => checks.push({ id, name, runner });

  const mirrorCount = () => {
    try {
      const data = JSON.parse(fs.readFileSync(mirrorPath, "utf8"));
      return Array.isArray(data.cookies) ? data.cookies.length : 0;
    } catch {
      return -1;
    }
  };

  add("node", "Node.js available", () =>
    result("pass", process.version)
  );

  add("python", "Python 3 available", () => {
    const r = spawnSync(python, ["--version"], {
      encoding: "utf8",
      timeout: 15000,
    });
    if (r.status === 0) {
      return result("pass", `${(r.stdout || r.stderr).trim()} (${python})`);
    }
    return result(
      "fail",
      "python3 not found",
      "run the owned runtime installer"
    );
  });

  add("uv", "uv Python launcher installed", () => {
    const uv = findUv(home);
    return uv
      ? result("pass", uv)
      : result("fail", "uv not found", "run the owned runtime installer");
  });

  add("browser_harness", "browser-harness CLI installed", () =>
    fs.existsSync(harness)
      ? result("pass", harness)
      : result(
          "fail",
          `not found at ${harness}`,
          "run the owned runtime installer"
        )
  );

  add("browser_harness_shape", "browser-harness CLI shape", () => {
    if (!fs.existsSync(harness)) {
      return result("fail", "binary missing", "run the owned runtime installer");
    }
    const probe = spawnSync(harness, [], {
      encoding: "utf8",
      timeout: 15000,
    });
    const usage = `${probe.stdout || ""}${probe.stderr || ""}`;
    const dashC = /\b-c\b/.test(usage);
    const stdin = /<<'PY'|<<"PY"|<<PY\b/.test(usage);
    if (!dashC && !stdin) {
      return result(
        "fail",
        "CLI advertises neither supported invocation shape",
        "reinstall browser-harness with the owned runtime installer"
      );
    }
    return result("pass", stdin ? "stdin heredoc" : "-c flag");
  });

  add("chrome_safe_storage", "Chrome Safe Storage readable", () => {
    if (process.platform !== "darwin") {
      return result("pass", "not applicable on this platform");
    }
    if (phase === "pre_connect") {
      return result(
        "expected",
        "deferred until X connection to avoid an unexpected keychain prompt"
      );
    }
    const r = spawnSync(
      "security",
      [
        "find-generic-password",
        "-s",
        "Chrome Safe Storage",
        "-a",
        "Chrome",
        "-w",
      ],
      { encoding: "utf8", timeout: 10000 }
    );
    if (r.status === 0) {
      return result("pass", "accessible (cookie import can decrypt Chrome data)");
    }
    const tail =
      (r.stderr || "").trim().split("\n").slice(-1)[0] || `exit ${r.status}`;
    return result(
      "fail",
      tail,
      "unlock the login keychain, then reconnect X"
    );
  });

  add("chrome_cdp", "Managed Chrome CDP responding", () => {
    const probe = spawnSync(
      "curl",
      [
        "-sf",
        "--max-time",
        "2",
        "-o",
        "/dev/null",
        "http://127.0.0.1:9555/json/version",
      ],
      { encoding: "utf8" }
    );
    if (probe.status === 0) return result("pass", "CDP responding on :9555");
    if (phase === "pre_connect") {
      return result(
        "expected",
        "managed Chrome is not running yet; connect_x launches it"
      );
    }
    return result(
      "fail",
      "no CDP response on :9555",
      "re-run connect_x to launch managed Chrome"
    );
  });

  add("x_session", "X session valid in managed Chrome", () => {
    if (!fs.existsSync(setupScript)) {
      return phase === "pre_connect"
        ? result("expected", "X status script becomes available after runtime installation")
        : result("fail", `setup script missing at ${setupScript}`, "repair the runtime");
    }
    const r = spawnSync(python, [setupScript, "status"], {
      encoding: "utf8",
      timeout: 90000,
    });
    let out = null;
    try {
      out = JSON.parse((r.stdout || "").trim());
    } catch {
      out = null;
    }
    if (out && out.connected) {
      return result(
        "pass",
        `state=${out.state}${out.handle ? ` handle=@${out.handle}` : ""}`
      );
    }
    const state = out && out.state ? out.state : "unavailable";
    return phase === "pre_connect"
      ? result("expected", `state=${state}; X is connected later in onboarding`)
      : result(
          "fail",
          `state=${state}`,
          "run connect_x and finish any interactive X login"
        );
  });

  add("x_cookie_sqlite", "X cookies persisted to Chrome SQLite", () => {
    if (!fs.existsSync(cookiesDb)) {
      return phase === "pre_connect"
        ? result("expected", "Chrome Cookies database has not been created yet")
        : result("fail", `${cookiesDb} missing`, "re-run connect_x");
    }
    const r = spawnSync(
      python,
      [
        "-c",
        `import sqlite3; c=sqlite3.connect(${JSON.stringify(
          cookiesDb
        )}); print(c.execute("SELECT COUNT(*) FROM cookies WHERE host_key LIKE '%x.com' OR host_key LIKE '%twitter.com'").fetchone()[0])`,
      ],
      { encoding: "utf8", timeout: 10000 }
    );
    const count = Number.parseInt((r.stdout || "0").trim(), 10);
    if (count > 0) {
      return result("pass", `${count} x.com/twitter.com rows persisted`);
    }
    return phase === "pre_connect"
      ? result("expected", "no X cookie rows yet; connect_x imports them")
      : result("fail", "0 x.com/twitter.com rows", "re-run connect_x");
  });

  add("x_cookie_mirror", "Durable X cookie mirror populated", () => {
    const count = mirrorCount();
    if (count > 0) return result("pass", `${count} cookies mirrored`);
    if (phase === "pre_connect") {
      return result(
        "expected",
        count === 0
          ? "mirror exists but is empty until X is connected"
          : "mirror is created during X connection"
      );
    }
    return result(
      "fail",
      count === 0
        ? "mirror file is empty"
        : `no mirror at ${mirrorPath}`,
      "re-run connect_x to populate the durable mirror"
    );
  });

  add("keychain_autolock", "Login keychain auto-lock is covered", () => {
    if (process.platform !== "darwin") {
      return result("pass", "not applicable on this platform");
    }
    const keychain = path.join(
      home,
      "Library",
      "Keychains",
      "login.keychain-db"
    );
    const r = spawnSync("security", ["show-keychain-info", keychain], {
      encoding: "utf8",
      timeout: 10000,
    });
    const output = `${r.stdout || ""}${r.stderr || ""}`;
    const match = output.match(/timeout=(\d+)s/);
    if (!match) return result("pass", "no auto-lock timeout detected");
    const seconds = Number.parseInt(match[1], 10);
    if (mirrorCount() > 0) {
      return result(
        "pass",
        `auto-locks after ${seconds}s; durable cookie mirror covers relaunches`
      );
    }
    return phase === "pre_connect"
      ? result(
          "expected",
          `auto-locks after ${seconds}s; connect_x will create the protective mirror`
        )
      : result(
          "fail",
          `auto-locks after ${seconds}s with no durable mirror`,
          "re-run connect_x to create the mirror"
        );
  });

  add("autopilot_kicker", "Autopilot draft kicker installed + loaded", () => {
    if (process.platform !== "darwin") {
      return result("pass", "not applicable on this platform");
    }
    const label = "com.m13v.social-twitter-cycle";
    const plist = path.join(
      home,
      "Library",
      "LaunchAgents",
      `${label}.plist`
    );
    const onDisk = fs.existsSync(plist);
    const uid = typeof process.getuid === "function" ? process.getuid() : 0;
    const printed = spawnSync(
      "launchctl",
      ["print", `gui/${uid}/${label}`],
      { encoding: "utf8", timeout: 10000 }
    );
    const loaded = printed.status === 0;
    if (loaded && onDisk) {
      return result("pass", "kicker plist installed and loaded in launchd");
    }
    if (phase === "pre_connect") {
      return result(
        "expected",
        "kicker installs after a project (or the personal-brand persona) is ready"
      );
    }
    if (onDisk && !loaded) {
      return result(
        "fail",
        "kicker plist exists but is NOT loaded in launchd (no drafts will be produced)",
        "run runtime action:'install', or bootstrap the plist: " +
          `launchctl bootstrap gui/${uid} ${plist}`
      );
    }
    // plist missing: could be a legitimately-unconfigured box, so warn (non-blocking)
    // rather than hard-fail — but make it visible so a stuck autopilot is caught.
    return result(
      "warn",
      "autopilot kicker not installed (no drafts until it is)",
      "finish setup: choose a mode (engagement_mode) and schedule the autopilot " +
        "(queue_setup). The kicker auto-installs once a project or the persona is ready."
    );
  });

  const completedChecks = checks.map((check) => {
    const checkStarted = Date.now();
    try {
      return {
        id: check.id,
        name: check.name,
        ...check.runner(),
        duration_ms: Date.now() - checkStarted,
      };
    } catch (error) {
      return {
        id: check.id,
        name: check.name,
        status: "fail",
        detail: error instanceof Error ? error.message : String(error),
        duration_ms: Date.now() - checkStarted,
      };
    }
  });
  const summary = { pass: 0, fail: 0, expected: 0, warn: 0, total: checks.length };
  for (const check of completedChecks) {
    if (Object.prototype.hasOwnProperty.call(summary, check.status)) {
      summary[check.status] += 1;
    }
  }
  const completedAt = new Date();
  return {
    schema_version: 1,
    phase,
    ok: summary.fail === 0,
    started_at: startedAt.toISOString(),
    completed_at: completedAt.toISOString(),
    duration_ms: completedAt.getTime() - startedAt.getTime(),
    summary,
    checks: completedChecks,
  };
}

function formatDoctorReport(report) {
  const glyph = {
    pass: "OK",
    fail: "FAIL",
    expected: "WAIT",
    warn: "WARN",
  };
  const lines = [
    `social-autoposter doctor — ${report.phase.replace("_", " ")} phase`,
    "",
  ];
  for (const check of report.checks) {
    lines.push(
      `  [${(glyph[check.status] || check.status.toUpperCase()).padEnd(4)}] ${
        check.name
      }: ${check.detail || ""}`
    );
    if (check.fix) lines.push(`         fix: ${check.fix}`);
  }
  lines.push(
    "",
    `${report.summary.pass}/${report.summary.total} passed, ` +
      `${report.summary.expected} expected, ${report.summary.fail} failed.`
  );
  if (!report.ok) {
    lines.push(
      "Address the failures above and re-run `npx social-autoposter doctor`."
    );
  }
  return lines.join("\n");
}

module.exports = {
  formatDoctorReport,
  runDoctorSync,
};
