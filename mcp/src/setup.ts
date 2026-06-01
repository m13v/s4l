// Setup / first-run state for the social-autoposter MCP.
//
// Two jobs:
//   1. A persistent state file so the MCP knows on EVERY boot whether setup is
//      done, which project it configured, and when. This is the guardrail the
//      action tools check before doing anything.
//   2. Writing the collected project ("brain": website, what it does, who to
//      target, brand voice) into config.json projects[] — the pipeline's single
//      source of truth — so drafting actually uses it.

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { REPO_DIR } from "./repo.js";

// Per-install state lives outside the repo so it survives repo edits/updates.
const STATE_DIR =
  process.env.SAPS_STATE_DIR || path.join(os.homedir(), ".social-autoposter-mcp");
const STATE_PATH = path.join(STATE_DIR, "setup-state.json");

// The pipeline reads projects[] from config.json. Override for tests / custom
// installs; defaults to the repo's config.json.
export const CONFIG_PATH =
  process.env.SAPS_CONFIG_PATH || path.join(REPO_DIR, "config.json");

// Fields the X drafting prompts genuinely consume. Required ones must be present
// before we let any action tool run; recommended ones improve draft quality.
export const REQUIRED_FIELDS = [
  "name",
  "website",
  "description",
  "icp",
  "voice",
] as const;
export const RECOMMENDED_FIELDS = [
  "differentiator",
  "search_topics",
  "get_started_link",
  "content_guardrails",
] as const;

export interface ProjectInput {
  name: string;
  website: string;
  description: string;
  icp: string;
  voice: string;
  differentiator?: string;
  search_topics?: string[] | string;
  get_started_link?: string;
  content_guardrails?: string;
}

export interface SetupState {
  configured: boolean;
  project?: string;
  completed_at?: string;
  config_path?: string;
}

export function getSetupState(): SetupState {
  try {
    if (!fs.existsSync(STATE_PATH)) return { configured: false };
    const s = JSON.parse(fs.readFileSync(STATE_PATH, "utf-8")) as SetupState;
    // Self-heal: if the state claims configured but the project vanished from
    // config.json, treat setup as incomplete again.
    if (s.configured && s.project && !projectExists(s.project)) {
      return { configured: false, project: s.project };
    }
    return s;
  } catch {
    return { configured: false };
  }
}

function writeState(s: SetupState): void {
  fs.mkdirSync(STATE_DIR, { recursive: true });
  fs.writeFileSync(STATE_PATH, JSON.stringify(s, null, 2) + "\n", "utf-8");
}

interface ConfigFile {
  projects?: Array<Record<string, unknown>>;
  [k: string]: unknown;
}

function readConfig(): ConfigFile {
  if (!fs.existsSync(CONFIG_PATH)) return { projects: [] };
  const raw = fs.readFileSync(CONFIG_PATH, "utf-8").trim();
  if (!raw) return { projects: [] };
  return JSON.parse(raw) as ConfigFile;
}

export function projectExists(name: string): boolean {
  try {
    const cfg = readConfig();
    return (cfg.projects || []).some((p) => p.name === name);
  } catch {
    return false;
  }
}

function normalizeTopics(t: string[] | string | undefined): string[] | undefined {
  if (t == null) return undefined;
  if (Array.isArray(t)) return t.map((x) => String(x).trim()).filter(Boolean);
  return String(t)
    .split(/[,\n]/)
    .map((x) => x.trim())
    .filter(Boolean);
}

// Build a config.json project entry from the collected input. voice_relationship
// is forced to "first_party" because a customer is posting for their OWN product
// (drives the get_voice_relationship_rule() prompt voice). platform=twitter
// scopes this install to the X rail.
export function buildProjectEntry(input: ProjectInput): Record<string, unknown> {
  const entry: Record<string, unknown> = {
    name: input.name,
    weight: 10,
    platform: "twitter",
    website: input.website,
    description: input.description,
    icp: input.icp,
    voice: input.voice,
    voice_relationship: "first_party",
  };
  if (input.differentiator) entry.differentiator = input.differentiator;
  const topics = normalizeTopics(input.search_topics);
  if (topics && topics.length) entry.search_topics = topics;
  if (input.get_started_link) entry.get_started_link = input.get_started_link;
  if (input.content_guardrails) entry.content_guardrails = input.content_guardrails;
  return entry;
}

// Upsert the project into config.json projects[] (match by name) and mark setup
// complete in the state file. Backs up config.json first.
export function applySetup(input: ProjectInput): { project: string; created: boolean } {
  const cfg = readConfig();
  cfg.projects = cfg.projects || [];
  const entry = buildProjectEntry(input);
  const idx = cfg.projects.findIndex((p) => p.name === input.name);
  let created: boolean;
  if (idx >= 0) {
    // Merge: keep any existing extra fields, override with the new values.
    cfg.projects[idx] = { ...cfg.projects[idx], ...entry };
    created = false;
  } else {
    cfg.projects.push(entry);
    created = true;
  }
  // Backup before writing.
  if (fs.existsSync(CONFIG_PATH)) {
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    fs.copyFileSync(CONFIG_PATH, `${CONFIG_PATH}.bak-${stamp}`);
  }
  fs.mkdirSync(path.dirname(CONFIG_PATH), { recursive: true });
  fs.writeFileSync(CONFIG_PATH, JSON.stringify(cfg, null, 2) + "\n", "utf-8");

  writeState({
    configured: true,
    project: input.name,
    completed_at: new Date().toISOString(),
    config_path: CONFIG_PATH,
  });
  return { project: input.name, created };
}

// Which required fields are missing from a partial input (for status reporting).
export function missingRequired(partial: Partial<ProjectInput>): string[] {
  return REQUIRED_FIELDS.filter((f) => {
    const v = partial[f];
    return v == null || String(v).trim() === "";
  });
}

export interface SetupGate {
  ok: boolean;
  state: SetupState;
  message?: string;
}

// The guardrail action tools call before doing anything.
export function requireSetup(): SetupGate {
  const state = getSetupState();
  if (state.configured) return { ok: true, state };
  return {
    ok: false,
    state,
    message:
      "Setup required: this install has no project configured yet. Run the `setup` tool " +
      "first. To set up, collect from the user: their website URL, a short description of " +
      "what the product does, who their target audience/customer is (icp), their preferred " +
      "brand voice/tone, and optionally their main call-to-action link and any topics worth " +
      "monitoring on X. Then call `setup` with those fields.",
  };
}
