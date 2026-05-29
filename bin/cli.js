#!/usr/bin/env node
'use strict';

const path = require('path');
const fs = require('fs');
const os = require('os');
const { spawnSync } = require('child_process');

const platform = require('./platform');
const scheduler = require('./scheduler');

const DEST = path.join(os.homedir(), 'social-autoposter');
const PKG_ROOT = path.join(__dirname, '..');
const HOME = os.homedir();

// Files/dirs to copy from npm package to ~/social-autoposter
const COPY_TARGETS = [
  'scripts',
  'schema-postgres.sql',
  'config.example.json',
  'requirements.txt',
  'SKILL.md',
  'skill',
  'setup',
  'browser-agent-configs',
  'mcp-servers',
];

const ENV_TEMPLATE = `# social-autoposter environment variables
# Fill in your values below.

# Moltbook API key (required for Moltbook posting/scanning)
# Get it from: https://www.moltbook.com/settings/api
MOLTBOOK_API_KEY=

# Postgres connection string. Bring your own Postgres DB, apply schema with:
#   psql "$DATABASE_URL" -f schema-postgres.sql
# Format: postgresql://<user>:<password>@<host>/<db>?sslmode=require
DATABASE_URL=
`;

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
// AppMaker VM self-bootstrap. Single entry point that the appmaker template
// startup.sh calls on every fresh sandbox boot. Reads the stable sessionKey
// from /run/mk0r-session.json (which the appmaker bridge rewrites on every
// session bind, and which survives E2B sandbox substitution — only the
// sandboxId changes), then asks the social-autoposter HTTP API which Twitter
// account this VM is bound to (handle + stored login cookies, keyed by
// social_accounts.vm_session_key). With that single DB answer it sets up
// everything: env file (with the DB-sourced handle), profile symlink, MCP
// config (BH_PORT=9222), uuid-runtime, then restores the Twitter login by
// re-injecting the stored cookies via CDP.
//
// This is the "one proper fix" for sandbox substitution: the VM holds no
// per-VM state on disk — the DB does, keyed by the stable sessionKey. So
// any fresh sandbox can rebuild itself by reading /run/mk0r-session.json
// and calling one route.
function bootstrapVm() {
  if (!isAppMakerVm()) {
    console.error('bootstrap-vm: not an AppMaker VM (no /opt/startup.sh + CDP :9222). Use `init` or `update` on dev boxes.');
    process.exit(2);
  }
  console.log('  AppMaker VM bootstrap: resolving identity from DB by sessionKey...');

  let sessionKey;
  try {
    const raw = fs.readFileSync('/run/mk0r-session.json', 'utf8');
    sessionKey = (JSON.parse(raw) || {}).sessionKey;
  } catch (err) {
    console.error(`bootstrap-vm: cannot read /run/mk0r-session.json: ${err.message}`);
    process.exit(3);
  }
  if (!sessionKey) {
    console.error('bootstrap-vm: /run/mk0r-session.json has no sessionKey');
    process.exit(3);
  }
  console.log(`    sessionKey=${sessionKey}`);

  // Get the X-Installation header via identity.py (same Python helper http_api.py
  // uses, so auth stays single-sourced).
  const identityPath = path.join(PKG_ROOT, 'scripts', 'identity.py');
  const headerRes = spawnSync('/usr/bin/python3', [identityPath, 'header'], {
    encoding: 'utf8',
  });
  if (headerRes.status !== 0) {
    console.error(`bootstrap-vm: identity.py header failed: ${headerRes.stderr || headerRes.error}`);
    process.exit(4);
  }
  const installHeader = (headerRes.stdout || '').trim();

  const base = (process.env.AUTOPOSTER_API_BASE || 'https://s4l.ai').replace(/\/+$/, '');
  const url = `${base}/api/v1/twitter/vm-session?session_key=${encodeURIComponent(sessionKey)}`;
  console.log(`    GET ${url}`);

  // Use curl (always present on the appmaker template) so we don't pull in
  // a Node HTTP dep here.
  const curl = spawnSync('curl', [
    '-sS', '--max-time', '15',
    '-H', `X-Installation: ${installHeader}`,
    '-H', 'Content-Type: application/json',
    url,
  ], { encoding: 'utf8' });
  if (curl.status !== 0) {
    console.error(`bootstrap-vm: curl failed: ${curl.stderr || curl.error}`);
    process.exit(5);
  }
  let payload;
  try {
    payload = JSON.parse(curl.stdout || '{}');
  } catch (err) {
    console.error(`bootstrap-vm: bad JSON from API: ${curl.stdout.slice(0, 300)}`);
    process.exit(6);
  }
  if (!payload.ok || !payload.data) {
    console.error(`bootstrap-vm: API error: ${JSON.stringify(payload).slice(0, 300)}`);
    process.exit(7);
  }
  const { handle, cookies, vm_project_id } = payload.data;
  if (!handle) {
    console.error('bootstrap-vm: API returned no handle. social_accounts.vm_session_key may be unset for this VM.');
    process.exit(8);
  }
  console.log(`    bound to @${handle} (vm_project_id=${vm_project_id || 'none'}, cookies=${(cookies || []).length})`);

  // Write env file with DB-sourced handle (durable across `social-autoposter update`).
  writeAppMakerEnvFile(handle);

  // Existing setup steps. installBrowserHarness already installs uuid-runtime,
  // symlinks the profile, and patches the MCP config — call it directly.
  installBrowserHarness();

  // Install Python deps from requirements.txt. installBrowserHarness only
  // installs uv + mcp; it does NOT read requirements.txt, so without this the
  // VM is missing websocket-client (restore_twitter_session.py aborts on
  // import) plus psycopg2-binary/playwright that the cycle scripts need.
  installPythonDeps();

  // Restore the Twitter login if we have stored cookies and the Chrome is
  // up. No-op when Chrome isn't reachable yet (startup ordering); the cycle
  // preflight will run restore_twitter_session.py on its next tick.
  if ((cookies || []).length > 0) {
    const restorePath = path.join(HOME, 'social-autoposter', 'scripts', 'restore_twitter_session.py');
    if (fs.existsSync(restorePath)) {
      console.log('    invoking restore_twitter_session.py to re-inject cookies...');
      // Source the env file so AUTOPOSTER_TWITTER_HANDLE / TWITTER_CDP_URL are set.
      const r = spawnSync('bash', ['-lc',
        `source ${HOME}/.social-autoposter-env 2>/dev/null; /usr/bin/python3 ${restorePath} || true`,
      ], { stdio: 'inherit' });
      void r;
    }
  } else {
    console.log('    no stored cookies; manual login still required this once.');
  }

  console.log('  bootstrap-vm: done.');
}

function installBrowserHarness() {
  const onAppMaker = isAppMakerVm();
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
  const harnessDir = path.join(HOME, 'Developer', 'browser-harness');
  if (!fs.existsSync(harnessDir)) {
    fs.mkdirSync(path.dirname(harnessDir), { recursive: true });
    console.log('    cloning browser-harness from GitHub...');
    const clone = spawnSync('git', ['clone', '--depth', '1', 'https://github.com/browser-use/browser-harness', harnessDir], { stdio: 'inherit' });
    if (clone.status !== 0) {
      console.warn('    WARNING: git clone failed; twitter-harness will not work until you clone manually.');
    }
  } else {
    console.log(`    browser-harness clone exists -> ${harnessDir}`);
  }

  if (uvBin && fs.existsSync(harnessDir)) {
    console.log('    installing browser-harness CLI via uv tool...');
    const install = spawnSync(uvBin, ['tool', 'install', '-e', harnessDir], { stdio: 'inherit' });
    if (install.status !== 0) {
      console.warn('    WARNING: `uv tool install -e .` failed; check the output above.');
    }
  }

  // Step 4: ensure mcp Python package available (server.py uses `from mcp.server.fastmcp ...`).
  // server.py is shebanged through `uv run --with mcp ...` so this is belt-and-suspenders;
  // we install it into the system Python too so a plain `python3 server.py` also works.
  console.log('    ensuring mcp>=1.0.0 Python package is importable...');
  let pip = spawnSync('pip3', ['install', '-q', 'mcp>=1.0.0'], { stdio: 'inherit' });
  if (pip.status !== 0) {
    pip = spawnSync('pip3', ['install', '-q', 'mcp>=1.0.0', '--break-system-packages'], { stdio: 'inherit' });
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
  ];

  const driver = scheduler.driverFor();
  const env = driver.defaultEnv({ home: HOME, nodeBin });
  const kind = platform.scheduler();
  const outDir = path.join(DEST, kind === 'systemd' ? 'systemd' : 'launchd');
  driver.generate({ jobs, outDir, env });
  console.log(`  generated ${kind} units at ${outDir}`);
}

// On Linux we translate every shipped launchd plist into a systemd
// .service + .timer pair at install time. Plists remain the source of truth
// so the macOS pipeline is untouched; the systemd/ dir is derived.
function generateSystemdFromPlists() {
  const launchdDriver = scheduler.driverFor('launchd');
  const systemdDriver = scheduler.driverFor('systemd');
  const srcDir = path.join(DEST, 'launchd');
  const outDir = path.join(DEST, 'systemd');
  if (!fs.existsSync(srcDir)) return 0;
  const plists = fs.readdirSync(srcDir).filter(f => f.endsWith('.plist'));
  const nodeBin = path.dirname(process.execPath);
  const env = systemdDriver.defaultEnv({ home: HOME, nodeBin });

  const jobs = [];
  let skipped = 0;
  for (const f of plists) {
    const xml = fs.readFileSync(path.join(srcDir, f), 'utf8');
    const { label, scriptPath } = launchdDriver.parseUnit(xml);
    if (!label || !scriptPath) { skipped++; continue; }
    const sched = launchdDriver.scheduleFromUnit(xml);
    if (!sched.intervalSecs) {
      console.log(`  skip ${f}: calendar schedule not yet translated to OnCalendar`);
      skipped++;
      continue;
    }
    // Plists ship with the publisher's absolute paths baked in. Rebuild
    // paths against the current user's DEST so any user on any host gets
    // correct units without us having to re-ship plists per install target.
    const scriptBase = path.basename(scriptPath);
    const stdoutMatch = (xml.match(/<key>StandardOutPath<\/key>\s*<string>([^<]+)<\/string>/) || [])[1];
    const stderrMatch = (xml.match(/<key>StandardErrorPath<\/key>\s*<string>([^<]+)<\/string>/) || [])[1];
    const shortLabel = label.replace(/^com\.m13v\.social-/, '');
    const stdout = `${DEST}/skill/logs/${stdoutMatch ? path.basename(stdoutMatch) : `launchd-${shortLabel}-stdout.log`}`;
    const stderr = `${DEST}/skill/logs/${stderrMatch ? path.basename(stderrMatch) : `launchd-${shortLabel}-stderr.log`}`;
    const runAtLoad = /<key>RunAtLoad<\/key>\s*<true\s*\/>/.test(xml);
    jobs.push({
      file: f,
      label,
      script: `${DEST}/skill/${scriptBase}`,
      interval: sched.intervalSecs,
      runAtLoad,
      stdoutLog: stdout,
      stderrLog: stderr,
    });
  }
  systemdDriver.generate({ jobs, outDir, env });
  console.log(`  translated ${jobs.length} launchd plists -> systemd units (skipped ${skipped})`);
  return jobs.length;
}

// Link every DEST/systemd/*.{service,timer} into ~/.config/systemd/user/ and
// reload the user daemon. Caller is expected to `systemctl --user enable --now
// <timer>` for each timer they actually want running; this mirrors how macOS
// setup leaves loading to the user via the SKILL.md wizard.
function installSystemdUnits() {
  const driver = scheduler.driverFor('systemd');
  const unitDir = path.join(DEST, 'systemd');
  const agentsDir = platform.agentsDir();
  if (!fs.existsSync(unitDir)) return;
  fs.mkdirSync(agentsDir, { recursive: true });
  const services = fs.readdirSync(unitDir).filter(f => f.endsWith('.service'));
  let linked = 0;
  for (const f of services) {
    if (driver.install(path.join(unitDir, f), agentsDir)) linked++;
  }
  const r = spawnSync('systemctl', ['--user', 'daemon-reload'], { encoding: 'utf8' });
  if (r.status === 0) {
    console.log(`  linked ${linked} unit pair(s) into ${agentsDir}; systemctl --user daemon-reload OK`);
  } else {
    console.warn(`  linked ${linked} unit pair(s); daemon-reload failed: ${(r.stderr || '').trim()}`);
  }
  const linger = spawnSync('loginctl', ['show-user', os.userInfo().username, '--property=Linger'], { encoding: 'utf8' });
  if (!/Linger=yes/.test(linger.stdout || '')) {
    console.log('  note: run `sudo loginctl enable-linger $USER` so timers fire when nobody is logged in');
  }
  console.log('  next: systemctl --user enable --now <timer> for each job you want scheduled');
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

  // On Linux, derive systemd units from every plist and link them into
  // ~/.config/systemd/user/. macOS install is unchanged.
  if (platform.scheduler() === 'systemd') {
    generateSystemdFromPlists();
    installSystemdUnits();
  }

  // Provision the browser-harness toolchain BEFORE writing harness configs so
  // findUvBin() picks up a freshly-installed uv on first run.
  installBrowserHarness();
  // Install browser agent MCP configs + profile dirs (skips existing files)
  installBrowserAgentConfigs();
  // On AppMaker VMs, patch the twitter-harness MCP config so its server.py
  // drives port 9222 (AppMaker Chromium) instead of the default 9555.
  if (isAppMakerVm()) applyAppMakerMcpConfigOverrides();
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

  // .env — only if it doesn't exist. Written from an in-package template so
  // the NPM tarball no longer ships a credential-bearing .env.example file.
  const envDest = path.join(DEST, '.env');
  if (!fs.existsSync(envDest)) {
    fs.writeFileSync(envDest, ENV_TEMPLATE);
    console.log('  created .env from template (fill in DATABASE_URL and MOLTBOOK_API_KEY)');
  } else {
    console.log('  .env exists — skipping');
  }

  installPythonDeps();
  removeLegacyEngagementStylesSidecar();

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
  console.log('  1. Edit ~/social-autoposter/config.json with your accounts');
  console.log('  2. Tell your Claude agent: "set up social autoposter"');
  console.log('     (uses the setup/SKILL.md wizard for browser login verification)');
  console.log('  3. Posts are logged to the shared Postgres DB (DATABASE_URL in .env)');
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

  // Refresh systemd units on Linux so plist changes propagate.
  if (platform.scheduler() === 'systemd') {
    generateSystemdFromPlists();
    installSystemdUnits();
  }

  // Provision browser-harness (uv + clone + uv tool install + mcp pkg + server.py).
  // Idempotent: skips steps that are already done.
  installBrowserHarness();
  // Top up browser agent configs (won't overwrite user customizations)
  installBrowserAgentConfigs();
  // On AppMaker VMs, patch the twitter-harness MCP config so its server.py
  // drives port 9222 (AppMaker Chromium) instead of the default 9555.
  if (isAppMakerVm()) applyAppMakerMcpConfigOverrides();
  // Register any newly added MCP servers with Claude (idempotent).
  registerBrowserAgentMcpServers();

  // Refresh Python deps every update so version-bumps land on existing installs
  // and the candidate-style sidecar gets merged (preserves VM-side candidates).
  installPythonDeps();
  removeLegacyEngagementStylesSidecar();

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
  const base = fs.existsSync(reqPath)
    ? ['install', '-r', reqPath, '-q']
    : ['install', '-q', 'psycopg2-binary', 'playwright'];
  console.log('  installing Python deps (psycopg2-binary, playwright, ...)');
  // Debian/Ubuntu 23+ ship a PEP 668 marker that blocks pip3 against the
  // system Python without --break-system-packages. Try without first
  // (safer on macOS) and retry with the flag if the marker fires.
  let r = spawnSync('pip3', base, { stdio: 'inherit' });
  if (r.status !== 0) {
    console.log('  retrying with --break-system-packages (PEP 668 environments)');
    r = spawnSync('pip3', [...base, '--break-system-packages'], { stdio: 'inherit' });
  }
  if (r.status !== 0) {
    console.warn('  WARNING: pip3 install failed — run manually:');
    console.warn(`    pip3 ${base.join(' ')} --break-system-packages`);
    return;
  }
  // Playwright needs its browser binary downloaded separately. Chromium
  // is the only engine the repo uses today; skip Firefox/WebKit.
  console.log('  installing Playwright Chromium binary (one-time, ~150MB)...');
  const pw = spawnSync('python3', ['-m', 'playwright', 'install', 'chromium'], { stdio: 'inherit' });
  if (pw.status !== 0) {
    console.warn('  WARNING: playwright install chromium failed — run manually:');
    console.warn('    python3 -m playwright install chromium');
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

const cmd = process.argv[2];
if (cmd === 'init') {
  init();
} else if (cmd === 'update') {
  update();
} else if (cmd === 'bootstrap-vm') {
  bootstrapVm();
} else if (cmd === 'export-cookies') {
  // Forward to cookie-helper with 'export' + remaining args
  process.argv = [process.argv[0], process.argv[1], 'export', ...process.argv.slice(3)];
  require('./cookie-helper.js');
} else if (cmd === 'import-cookies') {
  // Forward to cookie-helper with 'import' + remaining args
  process.argv = [process.argv[0], process.argv[1], 'import', ...process.argv.slice(3)];
  require('./cookie-helper.js');
} else if (!cmd) {
  require('./server.js');
} else {
  console.log('social-autoposter — automated social posting for Claude agents');
  console.log('');
  console.log('Usage:');
  console.log('  npx social-autoposter              open the dashboard');
  console.log('  npx social-autoposter init          first-time setup');
  console.log('  npx social-autoposter update        update scripts, preserve config');
  console.log('  npx social-autoposter bootstrap-vm  AppMaker VM self-bootstrap (DB-driven)');
  console.log('  npx social-autoposter export-cookies [dir]  export browser cookies');
  console.log('  npx social-autoposter import-cookies [dir]  import browser cookies');
}
