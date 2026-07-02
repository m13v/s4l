#!/usr/bin/env node
'use strict';

const path = require('path');
const fs = require('fs');
const os = require('os');
const { spawnSync } = require('child_process');

const scheduler = require('./scheduler');
const { formatDoctorReport, runDoctorSync } = require('../mcp/shared/doctor.cjs');
const { recordDoctorReport } = require('../mcp/shared/onboarding-ledger.cjs');

const DEST = path.join(os.homedir(), 'social-autoposter');
const PKG_ROOT = path.join(__dirname, '..');
const HOME = os.homedir();

// Files/dirs to copy from npm package to ~/social-autoposter
const COPY_TARGETS = [
  'scripts',
  'config.example.json',
  'requirements.txt',
  'SKILL.md',
  'skill',
  'setup',
  'browser-agent-configs',
  'mcp-servers',
  'mcp',
];

// Never overwrite these user files during update
const USER_FILES = new Set(['config.json', '.env', 'SKILL.md']);

// Browser agent config templates -> install path under ~/.claude/browser-agent-configs/
// twitter-harness replaces the retired twitter-agent (2026-05-19). The harness
// runs a CDP-driven real Chrome on port 9555 backed by an MCP stdio server at
// ~/.claude/mcp-servers/browser-harness/server.py. installBrowserHarness()
// below provisions the supporting bits (uv, browser-harness CLI, mcp pkg).
const BROWSER_AGENT_CONFIGS = [
  'reddit-agent-mcp.json',
  'reddit-agent.json',
  'linkedin-agent-mcp.json',
  'linkedin-agent.json',
  'twitter-harness-mcp.json',
  'linkedin-harness-mcp.json',
  'all-agents-mcp.json',
];

const BROWSER_PROFILES = ['reddit', 'linkedin', 'browser-harness'];

function copyDir(src, dest) {
  fs.mkdirSync(dest, { recursive: true });
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    const srcPath = path.join(src, entry.name);
    const destPath = path.join(dest, entry.name);
    if (entry.isDirectory()) {
      copyDir(srcPath, destPath);
    } else {
      fs.copyFileSync(srcPath, destPath);
    }
  }
}

function linkOrRelink(target, linkPath) {
  try { fs.rmSync(linkPath, { recursive: true, force: true }); } catch {}
  fs.symlinkSync(target, linkPath);
}

// Locate uv (Astral's Python launcher). The browser-harness MCP server is
// shebanged through uv so it can pull `mcp` on first run without polluting
// the system Python. Returns the absolute path if found, or empty string.
function findUvBin() {
  const candidates = [
    path.join(HOME, '.local', 'bin', 'uv'),
    '/opt/homebrew/bin/uv',
    '/usr/local/bin/uv',
    '/usr/bin/uv',
  ];
  for (const c of candidates) {
    if (fs.existsSync(c)) return c;
  }
  const which = spawnSync('command', ['-v', 'uv'], { shell: true, encoding: 'utf8' });
  const found = (which.stdout || '').trim().split('\n')[0];
  return found && fs.existsSync(found) ? found : '';
}

// Locate the Python the MCP server is actually configured to run (SAPS_PYTHON).
// mcp/install.mjs picks /opt/homebrew/bin/python3 (or /usr/local/bin/python3)
// and stamps it into the MCP config, so Python deps MUST be installed into that
// SAME interpreter. Bare `pip3`/`python3` on macOS usually resolves to the
// Xcode CLT system python (3.9.x with pip 21.x), which is both the wrong target
// and too old to understand --break-system-packages. Falls back to `python3`.
function findPythonBin() {
  const candidates = ['/opt/homebrew/bin/python3', '/usr/local/bin/python3'];
  for (const c of candidates) {
    if (fs.existsSync(c)) return c;
  }
  const which = spawnSync('command', ['-v', 'python3'], { shell: true, encoding: 'utf8' });
  const found = (which.stdout || '').trim().split('\n')[0];
  return found && fs.existsSync(found) ? found : 'python3';
}

// True if `<pythonBin> -m pip` is new enough (pip >= 23.0) to accept
// --break-system-packages. Older pips treat the flag as an unknown option and
// hard-fail, so we must not pass it blindly on the retry.
function pipSupportsBreakSystemPackages(pythonBin) {
  const v = spawnSync(pythonBin, ['-m', 'pip', '--version'], { encoding: 'utf8' });
  const m = (v.stdout || '').match(/pip\s+(\d+)\.(\d+)/);
  if (!m) return false;
  return parseInt(m[1], 10) >= 23;
}

// True if the interpreter carries a PEP 668 EXTERNALLY-MANAGED marker in its
// stdlib dir (Homebrew python, Debian/Ubuntu 23+). On these, a bare
// `pip install` is GUARANTEED to fail with a loud "externally-managed-environment"
// wall of text. Detecting it up front lets pipInstall skip that doomed first
// attempt and go straight to --break-system-packages, so init output stays clean
// and doesn't falsely look like a failed dependency install when it recovers.
function pipIsExternallyManaged(pythonBin) {
  const r = spawnSync(pythonBin, ['-c',
    "import os,sys,sysconfig\n" +
    "p=os.path.join(sysconfig.get_path('stdlib'),'EXTERNALLY-MANAGED')\n" +
    "sys.exit(0 if os.path.exists(p) else 1)",
  ]);
  return r.status === 0;
}

// Install Python packages into a specific interpreter via `<py> -m pip install`.
// Behaviour by environment:
//   - PEP 668 externally-managed interpreter (Homebrew python, Debian/Ubuntu 23+)
//     with pip>=23: go STRAIGHT to --break-system-packages. The bare attempt
//     would always fail loudly with externally-managed-environment, which made
//     init look like "Python deps failed" even though the (silent) retry actually
//     installed everything. No doomed first attempt, no false-alarm output.
//   - Everything else: bare attempt, then retry with --break-system-packages only
//     if it failed and pip supports the flag.
// Returns the spawnSync result of the last attempt.
function pipInstall(pythonBin, args) {
  const base = ['-m', 'pip', 'install', ...args];
  if (pipIsExternallyManaged(pythonBin) && pipSupportsBreakSystemPackages(pythonBin)) {
    return spawnSync(pythonBin, [...base, '--break-system-packages'], { stdio: 'inherit' });
  }
  let r = spawnSync(pythonBin, base, { stdio: 'inherit' });
  if (r.status !== 0 && pipSupportsBreakSystemPackages(pythonBin)) {
    r = spawnSync(pythonBin, [...base, '--break-system-packages'], { stdio: 'inherit' });
  }
  return r;
}

function installBrowserAgentConfigs() {
  const nodeBin = path.dirname(process.execPath);
  const uvBin = findUvBin() || path.join(HOME, '.local', 'bin', 'uv');
  const srcDir = path.join(PKG_ROOT, 'browser-agent-configs');
  const destDir = path.join(HOME, '.claude', 'browser-agent-configs');
  fs.mkdirSync(destDir, { recursive: true });

  let installed = 0;
  let skipped = 0;
  for (const name of BROWSER_AGENT_CONFIGS) {
    const src = path.join(srcDir, name);
    const dest = path.join(destDir, name);
    if (!fs.existsSync(src)) continue;
    if (fs.existsSync(dest)) {
      skipped++;
      continue;
    }
    const tpl = fs.readFileSync(src, 'utf8');
    const out = tpl
      .replace(/__HOME__/g, HOME)
      .replace(/__NODE_BIN__/g, nodeBin)
      .replace(/__UV_BIN__/g, uvBin);
    fs.writeFileSync(dest, out);
    installed++;
  }
  console.log(`  browser agent configs -> ${destDir} (installed ${installed}, skipped ${skipped} existing)`);

  // Create empty persistent profile dirs so Playwright has somewhere to land cookies
  const profilesDir = path.join(HOME, '.claude', 'browser-profiles');
  fs.mkdirSync(profilesDir, { recursive: true });
  for (const p of BROWSER_PROFILES) {
    fs.mkdirSync(path.join(profilesDir, p), { recursive: true });
  }
  console.log(`  browser profile dirs ready -> ${profilesDir}/{${BROWSER_PROFILES.join(',')}}`);
}

// Detect whether we are running inside an AppMaker E2B VM. AppMaker provisions
// a Chromium on port 9222 behind the SOAX residential proxy at 127.0.0.1:3003,
// and that Chromium is the one the user logs into via the AppMaker UI (profile
// /root/.chromium-profile). The browser-harness Chrome on port 9555 with its
// own (logged-out, un-proxied) profile is wrong for this host, so we:
//   1. skip installBrowserHarness() entirely (saves disk + avoids a second
//      headless Chrome ever spawning).
//   2. write ~/.social-autoposter-env so skill/lib/twitter-backend.sh sources
//      TWITTER_CDP_URL=http://127.0.0.1:9222 instead of the default 9555.
// Detection: presence of /opt/startup.sh (the AppMaker bootstrap script that
// only exists on these VMs) AND a live HTTP response on 127.0.0.1:9222.
function isAppMakerVm() {
  if (process.platform !== 'linux') return false;
  if (!fs.existsSync('/opt/startup.sh')) return false;
  // Probe Chromium DevTools on 9222. 2s timeout; if it answers, we're on AppMaker.
  const probe = spawnSync('curl', ['-sf', '--max-time', '2', '-o', '/dev/null', 'http://127.0.0.1:9222/json/version'], { stdio: 'ignore' });
  return probe.status === 0;
}

// VM / AppMaker support is strictly opt-in. A normal `init`/`update` (the
// macOS user path) installs none of it — no apt-get, no :9222 CDP env file, no
// AppMaker MCP port overrides. It activates only when explicitly requested:
//   - env  SA_VM=1  (or SOCIAL_AUTOPOSTER_VM=1)
//   - flag --vm     on the command line
//   - a persisted marker written by `bootstrap-vm` (so later `update`s on the
//     same VM stay in VM mode without re-passing the flag)
//   - a genuine AppMaker VM (linux + /opt/startup.sh + live :9222) — kept as a
//     fallback so the existing mk0r bootstrap keeps working untouched. This can
//     never be true on a user's Mac.
const VM_MARKER = path.join(HOME, '.social-autoposter', 'vm-mode');
function vmModeEnabled() {
  if (process.env.SA_VM === '1' || process.env.SOCIAL_AUTOPOSTER_VM === '1') return true;
  if (process.argv.includes('--vm')) return true;
  try { if (fs.existsSync(VM_MARKER)) return true; } catch { /* ignore */ }
  return isAppMakerVm();
}
function enableVmMode() {
  try {
    fs.mkdirSync(path.dirname(VM_MARKER), { recursive: true });
    fs.writeFileSync(VM_MARKER, 'enabled\n');
  } catch { /* best-effort; vmModeEnabled() still honors env/flag/probe */ }
}

// Write ~/.social-autoposter-env so skill/lib/twitter-backend.sh picks up the
// AppMaker-specific TWITTER_CDP_URL before its `${VAR:-default}` fallback hits.
// Idempotent: rewrites the file every invocation so a config edit on the VM
// can't drift away from what cli.js intends.
function writeAppMakerEnvFile(handleFromDb) {
  const envPath = path.join(HOME, '.social-autoposter-env');
  // Source of truth for the handle is the DB (social_accounts.handle keyed by
  // vm_session_key). bootstrap-vm passes it in. Fallback: preserve a previously
  // set value across rewrites if no DB-sourced handle was provided (matters
  // when this runs from `social-autoposter update` without a fresh DB fetch).
  let preservedHandle = String(handleFromDb || '').trim().replace(/^@/, '');
  if (!preservedHandle) {
    try {
      const prev = fs.readFileSync(envPath, 'utf8');
      const m = prev.match(/^\s*export\s+AUTOPOSTER_TWITTER_HANDLE=(.+)\s*$/m);
      if (m) preservedHandle = m[1].trim();
    } catch { /* no prior file */ }
  }

  const lines = [
    '# social-autoposter per-host env overrides',
    '# Auto-generated by social-autoposter init/update on AppMaker E2B VMs.',
    '# Edit by hand only if you know what you are doing; it gets rewritten on every update.',
    '',
    '# Point twitter pipeline at AppMaker\'s proxied Chromium (SOAX residential exit',
    '# at 127.0.0.1:3003) instead of the harness Chrome on 9555. The Chromium on',
    '# 9222 is the one the user logs into via the AppMaker UI.',
    'export TWITTER_CDP_URL="http://127.0.0.1:9222"',
    '',
    '# AppMaker VMs run as root and the appmaker template sets Claude defaultMode',
    '# to bypassPermissions. Claude CLI refuses bypassPermissions under root for',
    '# security reasons UNLESS IS_SANDBOX=1 is set. Without this, every `claude -p`',
    '# call in the pipeline exits immediately with no output (cost=$0.00, 16s) and',
    '# Phase 1 reports envelope parse error / phase1_no_tweets.',
    'export IS_SANDBOX=1',
    '',
  ];
  if (preservedHandle) {
    lines.push(
      '# Which Twitter handle this sandbox posts as. Durable home for the handle',
      '# because config.json is reseeded on E2B sandbox substitution. Read by',
      '# twitter_account.resolve_handle() (cycle scoping + session restore).',
      `export AUTOPOSTER_TWITTER_HANDLE=${preservedHandle}`,
      '',
    );
  }
  const body = lines.join('\n');
  fs.writeFileSync(envPath, body);
  console.log(`  AppMaker VM detected -> wrote ${envPath} (TWITTER_CDP_URL=http://127.0.0.1:9222${preservedHandle ? `, AUTOPOSTER_TWITTER_HANDLE=${preservedHandle}` : ''})`);
}

// AppMaker VMs: symlink /root/.chromium-profile → ~/.claude/browser-profiles/browser-harness
// so the appmaker-managed Chrome on port 9222 (launched by /opt/startup.sh with
// --user-data-dir=/root/.chromium-profile) actually opens the HARNESS profile,
// which is where our @<handle> Twitter login lives. Without this symlink, the
// appmaker Chrome opens a fresh empty profile and the pipeline talks to a
// logged-out browser. Combined with ENABLE_ROOT_VOLUME=1 on the Cloud Run
// host, the profile (and its cookies) now survives sandbox substitution.
// Idempotent: if already symlinked correctly, no-op. If a real directory
// exists, back it up (so any local-only browser cache isn't lost) and replace.
function linkAppMakerHarnessProfile() {
  const harnessProfile = path.join(HOME, '.claude', 'browser-profiles', 'browser-harness');
  const appmakerProfile = '/root/.chromium-profile';
  try {
    fs.mkdirSync(harnessProfile, { recursive: true });
    let stat = null;
    try { stat = fs.lstatSync(appmakerProfile); } catch { /* not present */ }
    if (stat && stat.isSymbolicLink()) {
      const target = fs.readlinkSync(appmakerProfile);
      if (target === harnessProfile) {
        console.log(`    AppMaker profile already symlinked: ${appmakerProfile} -> ${harnessProfile}`);
        return;
      }
      fs.unlinkSync(appmakerProfile);
    } else if (stat && stat.isDirectory()) {
      const backup = `${appmakerProfile}.replaced-by-symlink-${Date.now()}`;
      fs.renameSync(appmakerProfile, backup);
      console.log(`    backed up existing ${appmakerProfile} -> ${backup}`);
    }
    fs.symlinkSync(harnessProfile, appmakerProfile);
    console.log(`    symlinked ${appmakerProfile} -> ${harnessProfile} (login persists across sandbox substitution)`);
  } catch (e) {
    console.warn(`    WARNING: failed to symlink AppMaker profile: ${e.message}`);
  }
}

// AppMaker VMs also need the twitter-harness MCP server (browser-harness/server.py)
// to drive port 9222, not its default 9555. That's a SECOND path the env file alone
// doesn't cover, because the MCP server is spawned by Claude as a subprocess with
// an env block taken from the MCP config file (--strict-mcp-config replaces the
// inherited env, so a parent BH_PORT export wouldn't reach it). So we patch the
// MCP config in-place to bake BH_PORT=9222 into its env block.
// Idempotent: parses the JSON, sets env.BH_PORT, rewrites. Safe to re-run.
function applyAppMakerMcpConfigOverrides() {
  const cfgPath = path.join(HOME, '.claude', 'browser-agent-configs', 'twitter-harness-mcp.json');
  if (!fs.existsSync(cfgPath)) {
    console.log(`  AppMaker MCP override: ${cfgPath} not found, skipping (will be picked up next run)`);
    return;
  }
  let cfg;
  try {
    cfg = JSON.parse(fs.readFileSync(cfgPath, 'utf8'));
  } catch (e) {
    console.warn(`  AppMaker MCP override: failed to parse ${cfgPath}: ${e.message}`);
    return;
  }
  const srv = cfg?.mcpServers?.['twitter-harness'];
  if (!srv) {
    console.warn(`  AppMaker MCP override: ${cfgPath} has no mcpServers.twitter-harness entry`);
    return;
  }
  if (!srv.env || typeof srv.env !== 'object') srv.env = {};
  if (srv.env.BH_PORT === '9222') {
    console.log(`  AppMaker MCP override: BH_PORT=9222 already set in ${cfgPath}`);
    return;
  }
  srv.env.BH_PORT = '9222';
  fs.writeFileSync(cfgPath, JSON.stringify(cfg, null, 2) + '\n');
  console.log(`  AppMaker MCP override: set BH_PORT=9222 in ${cfgPath}`);
}

// Provision the browser-harness toolchain that backs the twitter-harness MCP:
//   1. install uv (Astral) if missing
//   2. git-clone browser-use/browser-harness
//   3. uv tool install -e . (provides the `browser-harness` CLI)
//   4. ensure `mcp` Python package is importable for server.py
//   5. copy our shipped server.py into ~/.claude/mcp-servers/browser-harness/
// All steps are idempotent.
//
// AppMaker VMs: the toolchain is STILL needed (the MCP server.py is what
// Claude invokes during Phase 1's tweet scan, and it requires uv + mcp +
// browser-harness CLI to run). The AppMaker-specific deltas are:
//   (a) writeAppMakerEnvFile() points TWITTER_CDP_URL at 9222 for posting
//   (b) applyAppMakerMcpConfigOverrides() injects BH_PORT=9222 so server.py
//       drives the AppMaker Chromium instead of trying to launch its own
//       Chrome on 9555. server.py's ensure_chrome() short-circuits when CDP
//       is already alive on PORT, so no double-Chrome ever spawns.
// Previously we early-returned here on AppMaker, which left the VM without
// uv installed and broke Phase 1's Claude scan (the MCP server's `command:
// /root/.local/bin/uv` resolved to ENOENT, Claude got no tools, returned an
// empty envelope).

function installBrowserHarness() {
  const onAppMaker = vmModeEnabled();
  if (onAppMaker) {
    console.log('  AppMaker VM detected -> installing harness toolchain (deps); MCP will be pointed at port 9222');
    writeAppMakerEnvFile();
    // scripts/run_claude.sh uses `uuidgen` for session IDs on AUP-retry. The
    // base image ships libuuid1 (shared lib) but not the CLI tool — the
    // package is `uuid-runtime`. Without it, run_claude.sh's session_id
    // generation falls back to empty string and claude --session-id breaks.
    console.log('    installing uuid-runtime (uuidgen) for run_claude.sh...');
    spawnSync('bash', ['-lc', 'command -v uuidgen >/dev/null 2>&1 || DEBIAN_FRONTEND=noninteractive apt-get install -y -qq uuid-runtime'], { stdio: 'inherit' });
    linkAppMakerHarnessProfile();
  }
  console.log('  setting up browser-harness (twitter-harness MCP backend)...');

  // Step 1: uv. Try the official installer first; fall back to pip.
  let uvBin = findUvBin();
  if (!uvBin) {
    console.log('    uv not found -> installing via Astral installer');
    const sh = spawnSync('bash', ['-lc', 'curl -LsSf https://astral.sh/uv/install.sh | sh'], { stdio: 'inherit' });
    if (sh.status !== 0) {
      console.log('    Astral installer failed; falling back to pip3 install uv');
      let pip = spawnSync('pip3', ['install', '-q', 'uv'], { stdio: 'inherit' });
      if (pip.status !== 0) {
        pip = spawnSync('pip3', ['install', '-q', 'uv', '--break-system-packages'], { stdio: 'inherit' });
      }
    }
    uvBin = findUvBin();
  }
  if (!uvBin) {
    console.warn('  WARNING: uv install failed; twitter-harness MCP server.py will not start.');
    console.warn('    Install manually: curl -LsSf https://astral.sh/uv/install.sh | sh');
  } else {
    console.log(`    uv -> ${uvBin}`);
  }

  // Step 2 + 3: clone + `uv tool install -e .` browser-harness.
  //
  // PINNED to a known-good upstream commit instead of tracking origin/HEAD.
  // The installer used to fetch+reset --hard to HEAD on every run, so any
  // upstream change shipped to users untested (this is how the two-blank-tab
  // regression in upstream daemon.py attach behavior could reach users). Our
  // launch-at-real-URL fix in server.py/twitter-backend.sh neutralizes that
  // class of bug regardless, but pinning stops surprise upstream drift. Bump
  // BROWSER_HARNESS_PIN deliberately after validating a newer upstream against
  // the shipped server.py contract.
  const BROWSER_HARNESS_PIN = '6d20866664ea3d9691b27bbf64f42ae097437dc3';
  const harnessDir = path.join(HOME, 'Developer', 'browser-harness');
  const pinHarness = () => {
    // Fetch the exact pinned commit (GitHub serves arbitrary SHAs) and hard-
    // reset onto it. Works for a fresh clone and an existing checkout alike.
    const fetch = spawnSync('git', ['-C', harnessDir, 'fetch', '--depth', '1', 'origin', BROWSER_HARNESS_PIN], { stdio: 'inherit' });
    if (fetch.status !== 0) {
      console.warn(`    WARNING: could not fetch pinned browser-harness commit ${BROWSER_HARNESS_PIN.slice(0, 9)}; using existing checkout.`);
      return;
    }
    const reset = spawnSync('git', ['-C', harnessDir, 'reset', '--hard', 'FETCH_HEAD'], { stdio: 'inherit' });
    if (reset.status !== 0) {
      console.warn('    WARNING: could not reset browser-harness clone to pinned commit; using existing checkout.');
    }
  };
  if (!fs.existsSync(harnessDir)) {
    fs.mkdirSync(path.dirname(harnessDir), { recursive: true });
    console.log('    cloning browser-harness from GitHub...');
    const clone = spawnSync('git', ['clone', '--depth', '1', 'https://github.com/browser-use/browser-harness', harnessDir], { stdio: 'inherit' });
    if (clone.status !== 0) {
      console.warn('    WARNING: git clone failed; twitter-harness will not work until you clone manually.');
    } else {
      console.log(`    pinning browser-harness to ${BROWSER_HARNESS_PIN.slice(0, 9)}...`);
      pinHarness();
    }
  } else {
    console.log(`    browser-harness clone exists -> ${harnessDir}; pinning to ${BROWSER_HARNESS_PIN.slice(0, 9)}...`);
    pinHarness();
  }

  if (uvBin && fs.existsSync(harnessDir)) {
    console.log('    installing browser-harness CLI via uv tool...');
    // --force so a refreshed source / changed entry point is reinstalled even
    // when the tool is already present (a plain re-install is otherwise a no-op).
    const install = spawnSync(uvBin, ['tool', 'install', '--force', '-e', harnessDir], { stdio: 'inherit' });
    if (install.status !== 0) {
      console.warn('    WARNING: `uv tool install -e .` failed; check the output above.');
    }
    // The harness daemon caches imported code in a long-running process; drop it
    // so the next bh_run loads the freshly-installed CLI instead of stale code.
    const harnessBin = path.join(HOME, '.local', 'bin', 'browser-harness');
    if (fs.existsSync(harnessBin)) {
      spawnSync(harnessBin, ['--reload'], { stdio: 'inherit' });
    }

    // Contract check: server.py pipes the script to browser-harness via stdin.
    // Upstream supports two banner shapes — older builds advertise `-c <script>`
    // and newer builds advertise the `<<'PY' ... PY` heredoc form. Either is
    // fine for our use case (we pass the script via stdin, which both accept).
    // Fail loudly if the installed binary advertises NEITHER, which usually
    // means an offline/partial clone left a broken CLI that will silently make
    // every bh_run look like "CDP not connected".
    if (fs.existsSync(harnessBin)) {
      const probe = spawnSync(harnessBin, [], { stdio: 'pipe', encoding: 'utf8', timeout: 15000 });
      const usage = `${probe.stdout || ''}${probe.stderr || ''}`;
      const supportsDashC = /\b-c\b/.test(usage);
      const supportsStdin = /<<'PY'|<<"PY"|<<PY\b/.test(usage);
      if (!supportsDashC && !supportsStdin) {
        console.error('    ERROR: installed browser-harness CLI advertises neither `-c` nor a stdin heredoc.');
        console.error('    This usually means a partial/corrupted install. The twitter-harness MCP will');
        console.error('    return a usage banner / "CDP not connected" on every call.');
        console.error(`    Fix: rm -rf ${harnessDir} && re-run \`social-autoposter init\` while online,`);
        console.error('    or manually: git clone https://github.com/browser-use/browser-harness ' + harnessDir +
          ' && ' + uvBin + ' tool install --force -e ' + harnessDir);
      } else {
        const shape = supportsStdin ? 'stdin heredoc' : '-c flag';
        console.log(`    browser-harness CLI verified (${shape}).`);
      }
    }
  }

  // Step 4: ensure mcp Python package available (server.py uses `from mcp.server.fastmcp ...`).
  // server.py is shebanged through `uv run --with mcp ...` so this is belt-and-suspenders;
  // we install it into the SAPS_PYTHON interpreter (the same Homebrew python the MCP
  // server is configured to use), NOT bare pip3 which targets the Xcode CLT system python.
  const harnessPython = findPythonBin();
  console.log(`    ensuring mcp>=1.0.0 Python package is importable (${harnessPython})...`);
  const pip = pipInstall(harnessPython, ['-q', 'mcp>=1.0.0']);
  if (pip.status !== 0) {
    console.warn('    WARNING: could not install mcp Python package; server.py still runs via `uv run --with mcp`.');
  }

  // Step 5: copy our shipped server.py into the canonical install location.
  const srcServer = path.join(PKG_ROOT, 'mcp-servers', 'browser-harness', 'server.py');
  const destServer = path.join(HOME, '.claude', 'mcp-servers', 'browser-harness', 'server.py');
  if (fs.existsSync(srcServer)) {
    fs.mkdirSync(path.dirname(destServer), { recursive: true });
    fs.copyFileSync(srcServer, destServer);
    try { fs.chmodSync(destServer, 0o755); } catch {}
    console.log(`    server.py -> ${destServer}`);
  } else {
    console.warn(`    WARNING: package missing mcp-servers/browser-harness/server.py (${srcServer})`);
  }
}

// Register the three browser-agent MCP servers with Claude so they show up
// under user scope (writes to ~/.claude.json). Idempotent: parses the output
// of `claude mcp list` and only calls `add-json` for missing entries.
// If the `claude` CLI is not on PATH, prints manual instructions and returns.
function registerBrowserAgentMcpServers() {
  const configDir = path.join(HOME, '.claude', 'browser-agent-configs');
  // twitter-agent retired 2026-05-19, replaced by twitter-harness (CDP-driven
  // real Chrome on port 9555 via the browser-harness MCP server).
  const servers = [
    { name: 'reddit-agent', file: path.join(configDir, 'reddit-agent-mcp.json') },
    { name: 'linkedin-agent', file: path.join(configDir, 'linkedin-agent-mcp.json') },
    { name: 'twitter-harness', file: path.join(configDir, 'twitter-harness-mcp.json') },
  ];

  const claudeBin = spawnSync('claude', ['--version'], { stdio: 'pipe' });
  if (claudeBin.status !== 0) {
    console.log('  claude CLI not on PATH; skipping MCP registration.');
    console.log('  Once Claude Code is installed, register manually with:');
    for (const s of servers) {
      console.log(`    claude mcp add-json ${s.name} "$(jq -c .mcpServers['\\"'${s.name}'\\"'] ${s.file})"`);
    }
    return;
  }

  const list = spawnSync('claude', ['mcp', 'list'], { encoding: 'utf8' });
  const existing = list.status === 0 ? list.stdout : '';

  let added = 0;
  let skipped = 0;
  for (const s of servers) {
    if (!fs.existsSync(s.file)) {
      console.warn(`  MCP config missing: ${s.file}`);
      continue;
    }
    // `claude mcp list` prints one server per line starting with the name.
    // Use a word-boundary check so e.g. reddit-agent does not false-match linkedin-agent.
    const re = new RegExp(`(^|\\s)${s.name.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&')}(:|\\s|$)`, 'm');
    if (re.test(existing)) {
      skipped++;
      continue;
    }
    const tpl = JSON.parse(fs.readFileSync(s.file, 'utf8'));
    const stanza = tpl.mcpServers && tpl.mcpServers[s.name];
    if (!stanza) {
      console.warn(`  ${s.file} has no mcpServers.${s.name} stanza; skipping`);
      continue;
    }
    const r = spawnSync('claude', ['mcp', 'add-json', s.name, JSON.stringify(stanza)], { stdio: 'pipe', encoding: 'utf8' });
    if (r.status === 0) {
      added++;
    } else {
      console.warn(`  claude mcp add-json ${s.name} failed: ${(r.stderr || r.stdout || '').trim()}`);
    }
  }
  console.log(`  MCP servers registered with Claude (added ${added}, already present ${skipped})`);
}

function generatePlists() {
  const nodeBin = path.dirname(process.execPath);
  const jobs = [
    {
      file: 'com.m13v.social-stats.plist',
      label: 'com.m13v.social-stats',
      script: `${DEST}/skill/stats.sh`,
      interval: 21600,
      runAtLoad: false,
      stdoutLog: `${DEST}/skill/logs/launchd-stats-stdout.log`,
      stderrLog: `${DEST}/skill/logs/launchd-stats-stderr.log`,
    },
    {
      // Daily self-updater. Pulls + installs the latest published release so a
      // hands-free / headless install never drifts stale. The script refuses to
      // touch a .git dev checkout, so it is a safe no-op on a source box.
      file: 'com.m13v.social-autoposter-update.plist',
      label: 'com.m13v.social-autoposter-update',
      script: `${DEST}/skill/social-autoposter-update.sh`,
      interval: 86400,
      runAtLoad: true,
      stdoutLog: `${DEST}/skill/logs/launchd-self-update-stdout.log`,
      stderrLog: `${DEST}/skill/logs/launchd-self-update-stderr.log`,
    },
    {
      // On-screen overlay watcher supervisor. The overlay (harness status banner)
      // only renders WHILE harness_overlay.py watch runs. The supervisor is
      // idempotent (pgrep guard), so a 60s StartInterval
      // is a no-op while the watcher is up and re-spawns it within a minute if it
      // ever dies. RunAtLoad starts it right after install. This is what makes the
      // overlay appear on headless / remote installs (Lane A); the MCP covers the
      // pure-.mcpb lane by calling the same script on draft_cycle / autopilot.
      file: 'com.m13v.social-overlay-watch.plist',
      label: 'com.m13v.social-overlay-watch',
      script: `${DEST}/skill/run-overlay-watch.sh`,
      interval: 60,
      runAtLoad: true,
      stdoutLog: `${DEST}/skill/logs/launchd-overlay-watch-stdout.log`,
      stderrLog: `${DEST}/skill/logs/launchd-overlay-watch-stderr.log`,
    },
  ];

  const driver = scheduler.driverFor();
  const env = driver.defaultEnv({ home: HOME, nodeBin });
  const outDir = path.join(DEST, 'launchd');
  driver.generate({ jobs, outDir, env });
  console.log(`  generated launchd units at ${outDir}`);
}

function init() {
  console.log('Setting up social-autoposter in', DEST);
  fs.mkdirSync(DEST, { recursive: true });

  // Copy all package files
  for (const f of COPY_TARGETS) {
    const src = path.join(PKG_ROOT, f);
    const dest = path.join(DEST, f);
    if (!fs.existsSync(src)) continue;
    const stat = fs.statSync(src);
    if (stat.isDirectory()) {
      copyDir(src, dest);
    } else {
      fs.copyFileSync(src, dest);
    }
    console.log('  copied', f);
  }

  // Generate launchd plists with user's actual HOME
  generatePlists();

  // Provision the browser-harness toolchain BEFORE writing harness configs so
  // findUvBin() picks up a freshly-installed uv on first run.
  installBrowserHarness();
  // Install browser agent MCP configs + profile dirs (skips existing files)
  installBrowserAgentConfigs();
  // On AppMaker VMs, patch the twitter-harness MCP config so its server.py
  // drives port 9222 (AppMaker Chromium) instead of the default 9555.
  if (vmModeEnabled()) applyAppMakerMcpConfigOverrides();
  // Register those MCP servers with Claude so they show up in `claude mcp list`.
  registerBrowserAgentMcpServers();

  // config.json — only if it doesn't exist
  const configDest = path.join(DEST, 'config.json');
  if (!fs.existsSync(configDest)) {
    fs.copyFileSync(path.join(PKG_ROOT, 'config.example.json'), configDest);
    console.log('  created config.json from template');
  } else {
    console.log('  config.json exists — skipping');
  }

  // No .env is created. X/Twitter and the rest of the pipeline run with zero
  // keys — state syncs through the s4l.ai HTTP API and the browser session
  // lives in the harness Chrome profile. Optional integrations read their keys
  // straight from the environment when set (MOLTBOOK_API_KEY for Moltbook,
  // AUTOPOSTER_API_KEY only if your s4l.ai install uses a bearer token); every
  // script guards `.env` with `[ -f .env ]`, so its absence is a no-op.

  installPythonDeps();
  removeLegacyEngagementStylesSidecar();
  installMcp();

  // Remove stale skill/SKILL.md if it exists (SKILL.md lives at repo root only)
  const skillMd = path.join(DEST, 'skill', 'SKILL.md');
  try { fs.rmSync(skillMd, { force: true }); } catch {}

  // Skill symlinks — point to repo root so Claude loads SKILL.md directly
  const skillsDir = path.join(os.homedir(), '.claude', 'skills');
  fs.mkdirSync(skillsDir, { recursive: true });
  linkOrRelink(DEST, path.join(skillsDir, 'social-autoposter'));
  console.log('  ~/.claude/skills/social-autoposter ->', DEST);
  linkOrRelink(path.join(DEST, 'setup'), path.join(skillsDir, 'social-autoposter-setup'));
  console.log('  ~/.claude/skills/social-autoposter-setup ->', path.join(DEST, 'setup'));

  console.log('');
  console.log('Done! Next steps:');
  console.log('  1. Fully quit and relaunch Claude so the MCP loads');
  console.log('  2. Tell your Claude agent: "set me up on social-autoposter plugin end to end"');
  console.log('     The agent will configure the product, connect X, seed topics, and verify a draft cycle');
  console.log('  3. Posts and all pipeline state sync via the s4l.ai HTTP API (no Postgres required)');
}

function update() {
  if (!fs.existsSync(DEST)) {
    console.error('Not installed. Run: npx social-autoposter init');
    process.exit(1);
  }

  console.log('Updating social-autoposter...');

  for (const f of COPY_TARGETS) {
    if (USER_FILES.has(f)) {
      console.log('  skipping', f, '(user file)');
      continue;
    }
    const src = path.join(PKG_ROOT, f);
    const dest = path.join(DEST, f);
    if (!fs.existsSync(src)) continue;
    const stat = fs.statSync(src);
    if (stat.isDirectory()) {
      copyDir(src, dest);
    } else {
      fs.copyFileSync(src, dest);
    }
    console.log('  updated', f);
  }

  // Regenerate launchd plists with correct paths
  generatePlists();

  // Provision browser-harness (uv + clone + uv tool install + mcp pkg + server.py).
  // Idempotent: skips steps that are already done.
  installBrowserHarness();
  // Top up browser agent configs (won't overwrite user customizations)
  installBrowserAgentConfigs();
  // On AppMaker VMs, patch the twitter-harness MCP config so its server.py
  // drives port 9222 (AppMaker Chromium) instead of the default 9555.
  if (vmModeEnabled()) applyAppMakerMcpConfigOverrides();
  // Register any newly added MCP servers with Claude (idempotent).
  registerBrowserAgentMcpServers();

  // Refresh Python deps every update so version-bumps land on existing installs
  // and the candidate-style sidecar gets merged (preserves VM-side candidates).
  installPythonDeps();
  removeLegacyEngagementStylesSidecar();
  installMcp();

  // Remove stale skill/SKILL.md if it exists (SKILL.md lives at repo root only)
  const skillMd = path.join(DEST, 'skill', 'SKILL.md');
  try { fs.rmSync(skillMd, { force: true }); } catch {}

  // Re-symlink skills — point to repo root so Claude loads SKILL.md directly
  const skillsDir = path.join(os.homedir(), '.claude', 'skills');
  try {
    linkOrRelink(DEST, path.join(skillsDir, 'social-autoposter'));
    console.log('  re-linked ~/.claude/skills/social-autoposter');
  } catch {}
  try {
    linkOrRelink(path.join(DEST, 'setup'), path.join(skillsDir, 'social-autoposter-setup'));
    console.log('  re-linked ~/.claude/skills/social-autoposter-setup');
  } catch {}

  console.log('');
  console.log('Update complete. config.json was preserved.');
}

// Install Python deps from requirements.txt (preferred) or fall back to the
// hardcoded list. Idempotent — pip3 install is a no-op when the package is
// already at the requested version. Playwright also needs the Chromium
// browser binary; we run `playwright install chromium` after the pip install.
function installPythonDeps() {
  const reqPath = path.join(PKG_ROOT, 'requirements.txt');
  const args = fs.existsSync(reqPath)
    ? ['-r', reqPath, '-q']
    : ['-q', 'playwright'];
  // Install into the SAME interpreter the MCP server runs (SAPS_PYTHON =
  // Homebrew python), NOT bare pip3 which on macOS targets the Xcode CLT system
  // python — deps installed there are invisible to the scripts at runtime.
  // pipInstall() also gates --break-system-packages on pip>=23 so it doesn't
  // hard-fail against the ancient system pip.
  const pythonBin = findPythonBin();
  console.log(`  installing Python deps (playwright, ...) into ${pythonBin}`);
  const r = pipInstall(pythonBin, args);
  if (r.status !== 0) {
    console.warn('  WARNING: pip install failed — run manually:');
    console.warn(`    ${pythonBin} -m pip install ${args.join(' ')} --break-system-packages`);
    return;
  }
  // Playwright needs its browser binary downloaded separately. Chromium
  // is the only engine the repo uses today; skip Firefox/WebKit.
  console.log('  installing Playwright Chromium binary (one-time, ~150MB)...');
  const pw = spawnSync(pythonBin, ['-m', 'playwright', 'install', 'chromium'], { stdio: 'inherit' });
  if (pw.status !== 0) {
    console.warn('  WARNING: playwright install chromium failed — run manually:');
    console.warn(`    ${pythonBin} -m playwright install chromium`);
  }
}

// Set up the social-autoposter MCP server (the X/Twitter draft/autopilot/stats
// surface for Claude Desktop + Claude Code). The package ships a prebuilt
// mcp/dist/, so we only install the runtime deps (@modelcontextprotocol/sdk +
// zod) and register the server into both clients. REPO_DIR auto-resolves to
// ~/social-autoposter (mcp/../..) so no env wiring is needed beyond what
// install.mjs pins. Idempotent; safe on both init and update.
function installMcp() {
  const mcpDest = path.join(DEST, 'mcp');
  if (!fs.existsSync(path.join(mcpDest, 'package.json'))) {
    console.warn('  WARNING: mcp/ missing from install — skipping MCP setup');
    return;
  }
  // Stamp the REAL shipped version (this npm package's version) into the MCP so
  // it can report itself accurately at runtime. The top-level package.json is
  // NOT copied into the install, so without this the MCP can't see its true
  // version. mcp/src/version.ts reads dist/version.json first. Refreshed on
  // every init/update.
  try {
    const pkgVersion = require('../package.json').version;
    const distDir = path.join(mcpDest, 'dist');
    fs.mkdirSync(distDir, { recursive: true });
    fs.writeFileSync(
      path.join(distDir, 'version.json'),
      JSON.stringify({ version: pkgVersion, installedAt: new Date().toISOString() }, null, 2)
    );
    console.log('  stamped MCP version', pkgVersion);
  } catch (e) {
    console.warn('  WARNING: could not stamp MCP version:', e && e.message);
  }
  console.log('  installing MCP runtime deps (npm install --omit=dev in mcp/)');
  const npmRes = spawnSync('npm', ['install', '--omit=dev', '--no-audit', '--no-fund'], {
    cwd: mcpDest,
    stdio: 'inherit',
  });
  if (npmRes.status !== 0) {
    console.warn('  WARNING: npm install in mcp/ failed — run manually:');
    console.warn('    (cd ~/social-autoposter/mcp && npm install --omit=dev)');
    return;
  }
  console.log('  registering social-autoposter MCP with Claude Desktop + Claude Code');
  const reg = spawnSync('node', ['install.mjs'], { cwd: mcpDest, stdio: 'inherit' });
  if (reg.status !== 0) {
    console.warn('  WARNING: MCP client registration failed — run manually:');
    console.warn('    (cd ~/social-autoposter/mcp && node install.mjs)');
  }
}

// Sweep the legacy candidate-style sidecar JSON + lock file off every install.
// The taxonomy lives in Postgres `engagement_styles_registry` now (single
// source of truth for all installs, no per-machine JSON drift); see
// scripts/migrate_engagement_styles_to_db.py for the cutover. We keep this
// helper around for a release or two so existing installs auto-clean the
// dead files on next `init` / `update`, then it can go.
function removeLegacyEngagementStylesSidecar() {
  const targets = [
    path.join(DEST, 'scripts', 'engagement_styles_extra.json'),
    path.join(DEST, 'scripts', 'engagement_styles_extra.json.lock'),
  ];
  for (const p of targets) {
    if (fs.existsSync(p)) {
      try {
        fs.rmSync(p, { force: true });
        console.log(`  removed legacy ${path.relative(DEST, p)} (registry is now in Postgres)`);
      } catch (e) {
        console.warn(`  WARNING: could not remove ${p}: ${e.message}`);
      }
    }
  }
}

// `doctor` is a structured diagnostic engine shared with MCP onboarding.
// Human-readable output remains the default; --json gives setup tools and CI a
// stable machine-readable report. --phase pre_connect treats the not-yet-created
// X session/cookie artifacts as expected, while full verifies the completed
// environment after connect_x.
function doctor() {
  const args = process.argv.slice(3);
  const json = args.includes('--json');
  const phaseArg = args.find((arg) => arg.startsWith('--phase='));
  const phaseIndex = args.indexOf('--phase');
  const phase =
    (phaseArg && phaseArg.slice('--phase='.length)) ||
    (phaseIndex >= 0 ? args[phaseIndex + 1] : null) ||
    'full';
  if (!['pre_connect', 'full'].includes(phase)) {
    console.error("doctor: --phase must be 'pre_connect' or 'full'");
    process.exit(2);
  }
  const report = runDoctorSync({
    phase,
    home: HOME,
    repoDir: fs.existsSync(DEST) ? DEST : PKG_ROOT,
    python: findPythonBin(),
  });
  // Doctor runs are durable even when invoked directly from the CLI. MCP uses
  // this same ledger, so a later onboarding session can show the historical run.
  recordDoctorReport(report);
  console.log(json ? JSON.stringify(report, null, 2) : formatDoctorReport(report));
  if (!report.ok) process.exitCode = 1;
}

// Provision the owned Python/Chromium runtime from the terminal. This is the
// panel-free path: it runs the EXACT same provisioning logic the panel's
// "Install runtime" button and the install_runtime MCP tool use (mcp/src/
// runtime.ts -> dist/runtime.js), via the thin ESM wrapper mcp/install-runtime.mjs.
// Use it when the UI panel can't render (Claude Code/Cowork), on a bare VM, or
// when an agent wants to install head-less. Idempotent: re-running repairs.
function installRuntime() {
  const wrapper = path.join(__dirname, '..', 'mcp', 'install-runtime.mjs');
  if (!fs.existsSync(wrapper)) {
    console.error(`Cannot find ${wrapper}. Re-run \`npx social-autoposter update\` to repair the install.`);
    process.exit(1);
  }
  // process.execPath is the Node already running this CLI, so we reuse it
  // rather than hunting for a node on PATH.
  const res = spawnSync(process.execPath, [wrapper], { stdio: 'inherit' });
  process.exit(res.status == null ? 1 : res.status);
}

// Wipe a social-autoposter install back to factory-fresh (test machines /
// uninstall). Shells out to the bundled scripts/reset-test-machine.sh, which is
// the single source of truth: it removes the owned state dir (~/.social-
// autoposter-mcp), packaged Chrome profiles + imported cookies, the browser-
// harness clone/CLI/server.py, the global npm package, and the MCP registration.
// DEFAULT IS A DRY RUN (prints what WOULD be removed); pass --yes to apply, and
// --deep to also remove the shared uv toolchain + Chromium cache. The script's
// one standard path quits Claude Desktop, wipes, settles, then relaunches
// Claude Desktop fresh. Forwarding to the shell script keeps npm and .mcpb
// installs behaving identically.
function reset() {
  const script = path.join(PKG_ROOT, 'scripts', 'reset-test-machine.sh');
  if (!fs.existsSync(script)) {
    console.error(`Cannot find ${script}. Re-run \`npx social-autoposter update\` to repair the install.`);
    process.exit(1);
  }
  // Forward everything after `reset` (e.g. --yes, --deep) straight to the script.
  const res = spawnSync('bash', [script, ...process.argv.slice(3)], { stdio: 'inherit' });
  process.exit(res.status == null ? 1 : res.status);
}

const cmd = process.argv[2];
if (cmd === 'init') {
  init();
} else if (cmd === 'update') {
  update();
} else if (cmd === 'doctor') {
  doctor();
} else if (cmd === 'install-runtime') {
  installRuntime();
} else if (cmd === 'reset' || cmd === 'uninstall') {
  reset();
} else if (cmd === 'export-cookies') {
  // Forward to cookie-helper with 'export' + remaining args
  process.argv = [process.argv[0], process.argv[1], 'export', ...process.argv.slice(3)];
  require('./cookie-helper.js');
} else if (cmd === 'import-cookies') {
  // Forward to cookie-helper with 'import' + remaining args
  process.argv = [process.argv[0], process.argv[1], 'import', ...process.argv.slice(3)];
  require('./cookie-helper.js');
} else if (!cmd) {
  // The dashboard server (bin/server.js) is a local-only operator tool and is
  // NOT shipped in the published package (it talks directly to Postgres). When
  // it's absent, fall back to usage help instead of crashing on a missing require.
  if (fs.existsSync(path.join(__dirname, 'server.js'))) {
    require('./server.js');
  } else {
    console.log('social-autoposter — automated social posting for Claude agents');
    console.log('');
    console.log('The local dashboard is not part of the published package.');
    console.log('Run `npx social-autoposter init` to set up, then drive it from your Claude agent.');
  }
} else {
  console.log('social-autoposter — automated social posting for Claude agents');
  console.log('');
  console.log('Usage:');
  console.log('  npx social-autoposter              open the dashboard');
  console.log('  npx social-autoposter init          first-time setup');
  console.log('  npx social-autoposter update        update scripts, preserve config');
  console.log('  npx social-autoposter doctor [--json] [--phase pre_connect|full]');
  console.log('  npx social-autoposter install-runtime  provision owned Python + Chromium (panel-free)');
  console.log('  npx social-autoposter export-cookies [dir]  export browser cookies');
  console.log('  npx social-autoposter import-cookies [dir]  import browser cookies');
  console.log('  npx social-autoposter reset [--yes] [--deep]  uninstall/wipe the install (dry-run unless --yes)');
}
