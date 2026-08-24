import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const ledger = require("../shared/onboarding-ledger.cjs");
const doctor = require("../shared/doctor.cjs");

const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "s4l-onboarding-"));
const opts = { stateDir };

try {
  ledger.recordAttempt("runtime_ready", { outcome: "install_started" }, opts);
  ledger.blockMilestone(
    "runtime_ready",
    "runtime_install_failed",
    "full local error with /private/path",
    { exit_code: 1 },
    opts,
  );
  ledger.recordAttempt("runtime_ready", {}, opts);
  ledger.completeMilestone("runtime_ready", {}, opts);

  const report = doctor.runDoctorSync({
    phase: "pre_connect",
    home: stateDir,
    repoDir: path.join(stateDir, "missing-repo"),
  });
  assert.equal(report.checks.length, 12);
  assert.equal(
    report.checks.find((check) => check.id === "x_cookie_mirror")?.status,
    "expected",
  );
  ledger.recordDoctorReport(report, opts);

  ledger.recordDoctorReport(
    {
      ...report,
      phase: "full",
      ok: false,
      summary: { ...report.summary, fail: 1 },
    },
    opts,
  );
  assert.equal(
    ledger.readLedger(opts).current_blocker.code,
    "doctor_full_failed",
  );
  // A later pre-connect probe must not erase a failed full verification.
  ledger.recordDoctorReport(report, opts);
  assert.equal(
    ledger.readLedger(opts).current_blocker.code,
    "doctor_full_failed",
  );
  ledger.recordDoctorReport(
    {
      ...report,
      phase: "full",
      ok: true,
      summary: { ...report.summary, fail: 0 },
    },
    opts,
  );

  const saved = ledger.readLedger(opts);
  assert.equal(saved.milestones.runtime_ready.status, "complete");
  assert.equal(saved.milestones.runtime_ready.attempts, 2);
  assert.equal(saved.milestones.environment_checked.status, "complete");
  assert.equal(saved.doctor.runs.length, 4);
  assert.equal(saved.current_blocker, null);
  assert.ok(saved.events.length >= 5);
  assert.ok(saved.events.every((event) => event.backend_sent_at === null));

  const first = saved.events[0].event_id;
  ledger.markBackendEventsSent([first], opts);
  assert.equal(ledger.pendingBackendEvents(opts).length, saved.events.length - 1);

  const snapshot = ledger.publicSnapshot(opts);
  assert.equal(snapshot.milestones.length, 9);
  assert.equal(snapshot.doctor.phase, "full");
  assert.ok(fs.existsSync(path.join(stateDir, "onboarding-progress.json")));
} finally {
  fs.rmSync(stateDir, { recursive: true, force: true });
}

console.log("onboarding ledger + shared doctor: ok");
