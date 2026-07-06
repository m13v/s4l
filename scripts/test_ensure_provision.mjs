// Verifies ensureRuntimeProvisioned() gating logic against the built dist.
// Policy = provision-on-boot (option a): a fresh, not-ready install MUST kick the
// full provision; a second immediate call must NOT double-kick (inFlight guard).
// Runs in an isolated temp S4L_STATE_DIR and process.exit's right after the
// synchronous decision so the background provision() never reaches a network step.
import os from "os";
import fs from "fs";
import path from "path";

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "s4l-prov-test-"));
process.env.S4L_STATE_DIR = tmp;
// Real repo so a kicked provision's step 0 is a no-op (no untar, no network)
// before we exit; we never let it reach the uv/python/download steps.
process.env.S4L_REPO_DIR = path.join(os.homedir(), "social-autoposter");

const rt = await import(path.join(os.homedir(), "social-autoposter/mcp/dist/runtime.js"));

let failed = false;
const check = (name, cond) => {
  console.log(`${cond ? "PASS" : "FAIL"}  ${name}`);
  if (!cond) failed = true;
};

// --- Fresh install, runtime not ready => MUST provision everything on boot ----
const r1 = rt.ensureRuntimeProvisioned();
const p1 = rt.readProgress();
check("fresh install returns true (provisions on boot)", r1 === true);
check("fresh install wrote a running progress (download started)", !!p1 && p1.running === true);

// --- Re-entrancy: a second immediate call must NOT kick a second run ----------
const r2 = rt.ensureRuntimeProvisioned();
check("second call returns false (no double-kick while in flight)", r2 === false);

console.log(failed ? "\nRESULT: FAILED" : "\nRESULT: ALL PASS");
process.exit(failed ? 1 : 0); // halt before background provision touches the network
