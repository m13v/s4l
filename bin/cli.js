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

# Neon Postgres connection string. Bring your own Neon DB — apply schema with:
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

// Provision the browser-harness toolchain that backs the twitter-harness MCP:
//   1. install uv (Astral) if missing
//   2. git-clone browser-use/browser-harness
//   3. uv tool install -e . (provides the `browser-harness` CLI)
//   4. ensure `mcp` Python package is importable for server.py
//   5. copy our shipped server.py into ~/.claude/mcp-servers/browser-harness/
// All steps are idempotent.
function installBrowserHarness() {
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
  installEngagementStylesSidecar();

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
  console.log('  3. Posts are logged to the shared Neon DB (DATABASE_URL in .env)');
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
  // Register any newly added MCP servers with Claude (idempotent).
  registerBrowserAgentMcpServers();

  // Refresh Python deps every update so version-bumps land on existing installs
  // and the candidate-style sidecar gets merged (preserves VM-side candidates).
  installPythonDeps();
  installEngagementStylesSidecar();

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

// Copy the candidate-style sidecar JSON into ~/social-autoposter/scripts/
// if missing; merge if present so VM-side invented candidates survive
// across updates. Promoted (status=active) entries from the shipped baseline
// always win.
function installEngagementStylesSidecar() {
  const src = path.join(PKG_ROOT, 'scripts', 'engagement_styles_extra.json');
  const dest = path.join(DEST, 'scripts', 'engagement_styles_extra.json');
  if (!fs.existsSync(src)) return;

  if (!fs.existsSync(dest)) {
    fs.mkdirSync(path.dirname(dest), { recursive: true });
    fs.copyFileSync(src, dest);
    console.log('  installed scripts/engagement_styles_extra.json');
    return;
  }

  let shipped = {};
  let local = {};
  try { shipped = JSON.parse(fs.readFileSync(src, 'utf8')) || {}; } catch {}
  try { local = JSON.parse(fs.readFileSync(dest, 'utf8')) || {}; } catch {}

  // Start from local (preserves VM-only candidates), overlay shipped active
  // entries so newly promoted styles always land. Shipped wins on conflict.
  const merged = { ...local };
  for (const [name, entry] of Object.entries(shipped)) {
    merged[name] = entry;
  }
  fs.writeFileSync(dest, JSON.stringify(merged, null, 2) + '\n');
  console.log('  merged scripts/engagement_styles_extra.json (shipped wins on conflict)');
}

const cmd = process.argv[2];
if (cmd === 'init') {
  init();
} else if (cmd === 'update') {
  update();
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
  console.log('  npx social-autoposter export-cookies [dir]  export browser cookies');
  console.log('  npx social-autoposter import-cookies [dir]  import browser cookies');
}
