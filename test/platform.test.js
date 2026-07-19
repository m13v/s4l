'use strict';

const assert = require('assert');
const platform = require('../bin/platform');

function test(name, fn) {
  try {
    fn();
    console.log(`ok   ${name}`);
  } catch (e) {
    console.error(`FAIL ${name}: ${e.message}`);
    process.exitCode = 1;
  }
}

test('detect returns a known platform on this host', () => {
  const p = platform.detect();
  assert(['darwin', 'linux', 'win32'].includes(p), `unexpected: ${p}`);
});

test('scheduler maps darwin -> launchd', () => {
  assert.strictEqual(platform.scheduler('darwin'), 'launchd');
});

test('scheduler rejects unsupported platforms', () => {
  assert.strictEqual(platform.scheduler('linux'), null);
});

test('statMtimeCmd darwin uses -f %m', () => {
  assert.deepStrictEqual(platform.statMtimeCmd('darwin'), ['stat', '-f', '%m']);
});

test('statMtimeCmd rejects unsupported platforms', () => {
  assert.strictEqual(platform.statMtimeCmd('linux'), null);
});

test('agentsDir darwin -> ~/Library/LaunchAgents', () => {
  const d = platform.agentsDir('darwin', '/Users/t');
  assert.strictEqual(d, '/Users/t/Library/LaunchAgents');
});

test('agentsDir rejects unsupported platforms', () => {
  assert.strictEqual(platform.agentsDir('linux', '/home/t'), null);
});

test('notifier darwin -> osascript', () => {
  assert.strictEqual(platform.notifier('darwin'), 'osascript');
});

test('notifier rejects unsupported platforms', () => {
  assert.strictEqual(platform.notifier('linux'), null);
});

test('launchdPath includes nodeBin first', () => {
  const p = platform.launchdPath('/opt/nvm/node/bin');
  assert(p.startsWith('/opt/nvm/node/bin:'), `unexpected: ${p}`);
  assert(p.includes('/usr/bin'));
  assert(p.includes('/bin'));
});
