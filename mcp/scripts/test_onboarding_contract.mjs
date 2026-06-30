import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const read = (file) => fs.readFileSync(path.join(root, file), "utf8");

const skill = read("setup/SKILL.md");
const server = read("mcp/src/index.ts");
const panel = read("mcp/panel/panel.ts");
const panelHtml = read("mcp/panel/panel.html");
const guide = read("GETTING_STARTED.md");
const privacy = read("PRIVACY.md");
const manifest = JSON.parse(read("mcp/manifest.json"));

assert.match(skill, /Treat the user's setup request as a terminal\s+goal/);
assert.match(skill, /Do not ask whether to run setup/);
assert.match(skill, /run the draft-only verification/);
assert.match(server, /ONBOARDING IS A TERMINAL GOAL/);
assert.match(server, /confirm:true without waiting for another yes\/no reply/);
assert.match(server, /ready_for_verification/);
assert.match(server, /state: "runtime_not_ready"/);
assert.match(server, /runDoctorPhase\("full"\)/);
assert.match(panel, /Set up S4L plugin end to end now/);
assert.match(panel, /btnSetup\.disabled = false/);
assert.match(panelHtml, /The Set up button handles this automatically/);
assert.match(panelHtml, /id="onboarding-details"/);
assert.match(guide, /gives the agent a\s+terminal goal/);
assert.match(guide, /onboarding-progress\.json/);
assert.match(privacy, /Doctor check IDs and pass\/fail\/expected status/);
assert.ok(manifest.tools.some((tool) => tool.name === "runtime"));

for (const [file, text] of [
  ["setup/SKILL.md", skill],
  ["mcp/src/index.ts", server],
  ["mcp/panel/panel.ts", panel],
  ["GETTING_STARTED.md", guide],
]) {
  assert.doesNotMatch(text, /one question at a time/i, `${file} reintroduced interview-style setup`);
  assert.doesNotMatch(text, /Walk me through it step by step/i, `${file} reintroduced step-by-step prompting`);
  assert.doesNotMatch(text, /Ask if that's OK/i, `${file} reintroduced a redundant consent turn`);
}

console.log("autonomous onboarding contract: ok");
