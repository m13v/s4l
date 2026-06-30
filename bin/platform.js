'use strict';

const os = require('os');
const path = require('path');
const fs = require('fs');

function detect() {
  const p = process.platform;
  if (p === 'darwin') return 'darwin';
  return p;
}

function scheduler(platform = detect()) {
  if (platform === 'darwin') return 'launchd';
  return null;
}

function agentsDir(platform = detect(), home = os.homedir()) {
  if (platform === 'darwin') return path.join(home, 'Library', 'LaunchAgents');
  return null;
}

function statMtimeCmd(platform = detect()) {
  if (platform === 'darwin') return ['stat', '-f', '%m'];
  return null;
}

function notifier(platform = detect()) {
  if (platform === 'darwin') return 'osascript';
  return null;
}

function brewPrefix() {
  const candidates = ['/opt/homebrew/bin', '/usr/local/bin'];
  for (const c of candidates) {
    if (fs.existsSync(c)) return c;
  }
  return null;
}

function launchdPath(nodeBin) {
  const dirs = new Set([nodeBin]);
  const brew = brewPrefix();
  if (brew) dirs.add(brew);
  dirs.add('/usr/local/bin');
  dirs.add('/usr/bin');
  dirs.add('/bin');
  return [...dirs].join(':');
}

module.exports = {
  detect,
  scheduler,
  agentsDir,
  statMtimeCmd,
  notifier,
  brewPrefix,
  launchdPath,
};
