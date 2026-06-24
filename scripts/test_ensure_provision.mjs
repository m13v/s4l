// Verifies ensureRuntimeProvisioned() gating logic against the built dist.
// Runs in an isolated temp SAPS_STATE_DIR so it touches no real runtime state.
// We process.exit immediately after the synchronous decision so the background
// provision() (if kicked) never reaches any network step.
import os from "os";
import fs from "fs";
import path from "path";

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "saps-prov-test-"));
process.env.SAPS_STATE_DIR = tmp;
// Point at the real repo so a kicked provision's step 0 is a no-op (no untar,
// no network) before we exit. We never let it reach the uv/python steps.
process.env.SAPS_REPO_DIR = path.join(os.homedir(), "social-autoposter");

const rt = await import(path.join(os.homedir(), "social-autoposter/mcp/dist/runtime.js"));

let failed = false;
const check = (name, cond) => {
  console.log(`${cond ? "PASS" : "FAIL"}  ${name}`);
  if (!cond) failed = true;
};

// --- Scenario A: fresh install, no progress file => must NOT provision -------
const aRet = rt.ensureRuntimeProvisioned();
const aProg = rt.readProgress();
check("A: fresh install returns false (no auto-kick)", aRet === false);
check("A: fresh install writes NO progress file (no surprise downloads)", aProg === null);

// --- Scenario B: prior FAILED provision on disk, runtime not ready => resume -
fs.writeFileSync(
  path.join(tmp, "install-progress.json"),
  JSON.stringify({
    running: false,
    done: true,
    ok: false,
    error: "simulated prior failure",
    steps: [{ id: "uv", label: "Install uv", status: "error" }],
    started_at: "2026-01-01T00:00:00.000Z",
    updated_at: "2026-01-01T00:00:00.000Z",
  })
);
const bRet = rt.ensureRuntimeProvisioned();
const bProg = rt.readProgress();
check("B: interrupted install returns true (auto-resumes)", bRet === true);
check("B: startProvisioning wrote a fresh running progress", !!bProg && bProg.running === true);

console.log(failed ? "\nRESULT: FAILED" : "\nRESULT: ALL PASS");
process.exit(failed ? 1 : 0); // halt before background provision touches the network
