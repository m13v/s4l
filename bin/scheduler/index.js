'use strict';

const platform = require('../platform');
const launchd = require('./launchd');

function driverFor(name) {
  const key = name || platform.scheduler();
  if (key === 'launchd') return launchd;
  throw new Error(`no scheduler driver for: ${key}`);
}

module.exports = { driverFor, launchd };
