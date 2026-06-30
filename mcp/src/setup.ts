// Setup / multi-project config for the social-autoposter MCP.
//
// Source of truth = config.json projects[] (the pipeline reads it). Readiness is
// DERIVED per-project from whether its required fields are present, never stored
// as a boolean (so the saved config and the reported status can't disagree).
//
// The only thing persisted outside config.json is a small scoping list: which
// project names were set up via THIS install. That list exists so (a) multi-
// project disambiguation works and (b) we don't surface unrelated projects that
// happen to live in config.json. It is NOT the source of truth for readiness.

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { repoDir } from "./repo.js";

// Per-install scoping list lives outside the repo so it survives repo updates.
const STATE_DIR =
  process.env.SAPS_STATE_DIR || path.join(os.homedir(), ".social-autoposter-mcp");
const STATE_PATH = path.join(STATE_DIR, "setup-state.json");

// The pipeline reads projects[] from config.json. Override for tests / custom
// installs; defaults to the (dynamically resolved) repo's config.json. Resolved
// per call, not a load-time const, because a bare .mcpb install materializes the
// repo after boot and setup must write config.json into THAT repo.
export function configPath(): string {
  return process.env.SAPS_CONFIG_PATH || path.join(repoDir(), "config.json");
}

// Fields the X drafting prompts genuinely consume. Required ones must all be
// present before a project is "ready"; recommended ones improve draft quality.
export const REQUIRED_FIELDS = [
  "name",
  "website",
  "description",
  "icp",
  "voice",
  // search_topics is required: the cycle's topic picker reads the DB universe
  // (project_search_topics) seeded FROM these on setup. With zero topics the
  // picker raises and the whole draft cycle silently returns nothing, so a
  // project is NOT ready until it has at least one topic to seed. (2026-06-02)
  "search_topics",
] as const;
export const RECOMMENDED_FIELDS = [
  "differentiator",
  "get_started_link",
  "content_guardrails",
] as const;

// name is the key (identifies the project); everything else is optional so
// setup can fill fields incrementally across several calls.
export interface ProjectInput {
  name: string;
  website?: string;
  description?: string;
  icp?: string;
  voice?: string;
  differentiator?: string;
  search_topics?: string[] | string;
  get_started_link?: string;
  content_guardrails?: string;
  // Escape hatch for ANY other project field the modeled props above don't
  // cover (e.g. weight, platform, booking_link, qualification, subreddit_bans,
  // short_links_host/short_links_live, content_angle, messaging, landing_pages,
  // posthog, voice_relationship). Each key is shallow-merged onto the project,
  // replacing that key's whole value. A value of null DELETES the key. Lets the
  // single project_config tool edit any field without a raw whole-file overwrite.
  fields?: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// Scoping list (which projects this install manages). NOT readiness truth.
// ---------------------------------------------------------------------------
interface ScopeState {
  projects: string[];
  last_project?: string;
}

function readScope(): ScopeState {
  try {
    if (!fs.existsSync(STATE_PATH)) return { projects: [] };
    const s = JSON.parse(fs.readFileSync(STATE_PATH, "utf-8")) as Record<string, unknown>;
    // New shape.
    if (Array.isArray(s.projects)) {
      return { projects: s.projects as string[], last_project: s.last_project as string | undefined };
    }
    // Migrate old single-project shape { configured, project }.
    if (typeof s.project === "string") {
      return { projects: [s.project], last_project: s.project };
    }
    return { projects: [] };
  } catch {
    return { projects: [] };
  }
}

function writeScope(s: ScopeState): void {
  fs.mkdirSync(STATE_DIR, { recursive: true });
  fs.writeFileSync(STATE_PATH, JSON.stringify(s, null, 2) + "\n", "utf-8");
}

export function managedProjects(): string[] {
  return readScope().projects;
}

export function recordManagedProject(name: string): void {
  const s = readScope();
  if (!s.projects.includes(name)) s.projects.push(name);
  s.last_project = name;
  writeScope(s);
}

// ---------------------------------------------------------------------------
// config.json read + project upsert.
// ---------------------------------------------------------------------------
interface ConfigFile {
  projects?: Array<Record<string, unknown>>;
  [k: string]: unknown;
}

function readConfig(): ConfigFile {
  const cfgPath = configPath();
  if (!fs.existsSync(cfgPath)) return { projects: [] };
  const raw = fs.readFileSync(cfgPath, "utf-8").trim();
  if (!raw) return { projects: [] };
  return JSON.parse(raw) as ConfigFile;
}

export function projectExists(name: string): boolean {
  try {
    return (readConfig().projects || []).some((p) => p.name === name);
  } catch {
    return false;
  }
}

// Strip stray JSON-array syntax that leaks into a single topic when a
// stringified array was split on commas (leading "[", trailing "]", and the
// surrounding quotes on each element). Without this, topics like '["AI video
// generation"' and '"ex-Google"]' get seeded verbatim and poison every search
// query with literal [, ] and " characters (Karol, 2026-06-30).
function stripTopicSyntax(x: string): string {
  let s = String(x).trim();
  s = s.replace(/^\[+/, "").replace(/\]+$/, "").trim();
  s = s.replace(/^["']+/, "").replace(/["']+$/, "").trim();
  return s;
}

// Normalize a list field the model may pass as a real array, a stringified
// JSON array, or a comma/newline-delimited string, into a clean string[].
// Used for BOTH search_topics and search_queries: the model frequently passes
// a stringified JSON array for either, and the naive comma-split baked [, ] and
// " into each element (Karol, 2026-06-30 — corrupted topics AND a silently
// skipped query seed). Returns undefined only for nullish input.
export function normalizeStringList(
  t: string[] | string | undefined | null
): string[] | undefined {
  if (t == null) return undefined;
  let raw: unknown[];
  if (Array.isArray(t)) {
    raw = t;
  } else {
    const s = String(t).trim();
    let parsed: unknown = null;
    if (s.startsWith("[")) {
      try {
        parsed = JSON.parse(s);
      } catch {
        parsed = null;
      }
    }
    raw = Array.isArray(parsed) ? parsed : s.split(/[,\n]/);
  }
  return raw.map((x) => stripTopicSyntax(String(x))).filter(Boolean);
}

function normalizeTopics(t: string[] | string | undefined): string[] | undefined {
  return normalizeStringList(t);
}

// The fields the user actually supplies via setup. Only fields that are present
// AND non-empty get written, so an incremental call merges just what it carries
// and never blanks an existing field or clobbers weight/platform/links/github.
// name is always included (it's the match key).
function userFields(input: Partial<ProjectInput>): Record<string, unknown> {
  const fields: Record<string, unknown> = {};
  const setStr = (k: keyof ProjectInput) => {
    const v = input[k];
    if (v != null && String(v).trim() !== "") fields[k] = v;
  };
  if (input.name != null) fields.name = input.name;
  setStr("website");
  setStr("description");
  setStr("icp");
  setStr("voice");
  setStr("differentiator");
  const topics = normalizeTopics(input.search_topics);
  if (topics && topics.length) fields.search_topics = topics;
  setStr("get_started_link");
  setStr("content_guardrails");
  return fields;
}

// Apply the generic `fields` escape hatch onto a project object IN PLACE.
// Shallow per-key: each key replaces that key's whole value; a value of null
// (or undefined) DELETES the key. Returns the list of keys touched so callers
// can report exactly what changed. `name` is protected — it's the match key and
// renaming via this path would orphan the entry, so it's ignored here.
export function applyExtraFields(
  target: Record<string, unknown>,
  fields: Record<string, unknown> | undefined
): { set: string[]; removed: string[] } {
  const set: string[] = [];
  const removed: string[] = [];
  if (!fields || typeof fields !== "object") return { set, removed };
  for (const [k, v] of Object.entries(fields)) {
    if (k === "name") continue; // never rename through the escape hatch
    if (v === null || v === undefined) {
      if (k in target) {
        delete target[k];
        removed.push(k);
      }
      continue;
    }
    target[k] = v;
    set.push(k);
  }
  return { set, removed };
}

// A brand-new project entry: user fields plus the defaults a fresh X-rail
// project needs. Applied ONLY on create, never on update.
export function buildProjectEntry(input: ProjectInput): Record<string, unknown> {
  const entry: Record<string, unknown> = {
    weight: 10,
    platform: "twitter",
    voice_relationship: "first_party",
    // A freshly onboarded customer has NOT shipped the @m13v/seo-components
    // /r/[code] resolver on their own domain, so a wrapped short link minted
    // against project.website would 404 ("this link doesn't exist"). Default
    // new projects to route /r/<code> through the social-autoposter-owned
    // resolver at https://s4l.ai instead: short_links_live=false makes
    // _project_short_links_host / getProjectWrapperHost fall back to
    // DEFAULT_FALLBACK_HOST. The customer flips this to true (or removes it)
    // only AFTER they ship their own /r/[code] handler. See the "URL wrapping"
    // section in CLAUDE.md.
    short_links_live: false,
    ...userFields(input),
  };
  // Generic fields can override the defaults above (e.g. platform/weight) and
  // set any advanced field at creation time too.
  applyExtraFields(entry, input.fields);
  return entry;
}

// Upsert the project into config.json projects[] (match by name), incrementally
// merging only the supplied fields, and record it as managed by this install.
// Backs up config.json first. Does NOT require all fields — readiness is checked
// separately, so a project can be filled out over several calls.
export function applySetup(input: ProjectInput): {
  project: string;
  created: boolean;
  ready: boolean;
  missing_required: string[];
  fields_set: string[];
  fields_removed: string[];
} {
  const cfg = readConfig();
  cfg.projects = cfg.projects || [];
  const idx = cfg.projects.findIndex((p) => p.name === input.name);
  let created: boolean;
  let fields_set: string[] = [];
  let fields_removed: string[] = [];
  if (idx >= 0) {
    // Update: merge ONLY supplied modeled fields; keep every other existing
    // field. Then apply the generic `fields` escape hatch (set/delete any key).
    const merged = { ...cfg.projects[idx], ...userFields(input) };
    const r = applyExtraFields(merged, input.fields);
    fields_set = r.set;
    fields_removed = r.removed;
    cfg.projects[idx] = merged;
    created = false;
  } else {
    const entry = buildProjectEntry(input);
    // Report which advanced keys the create call set via the escape hatch.
    fields_set = Object.keys(input.fields ?? {}).filter(
      (k) => k !== "name" && (input.fields as Record<string, unknown>)[k] != null
    );
    cfg.projects.push(entry);
    created = true;
  }
  const cfgPath = configPath();
  if (fs.existsSync(cfgPath)) {
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    fs.copyFileSync(cfgPath, `${cfgPath}.bak-${stamp}`);
  }
  fs.mkdirSync(path.dirname(cfgPath), { recursive: true });
  fs.writeFileSync(cfgPath, JSON.stringify(cfg, null, 2) + "\n", "utf-8");

  recordManagedProject(input.name);
  const missing = missingForProject(input.name) ?? [];
  return {
    project: input.name,
    created,
    ready: missing.length === 0,
    missing_required: missing,
    fields_set,
    fields_removed,
  };
}

// ---------------------------------------------------------------------------
// Personal-brand PERSONA project (2026-06-26).
//
// The persona is a project the autopilot can draft for in personal_brand mode:
// link-free organic engagement in the user's own voice (the revived 2026-02
// flow). It is NOT a product, so it is deliberately kept OUT of the managed-
// products scope list (no recordManagedProject) — that keeps it off the product-
// readiness counts and the "all projects ready" gate. It is identified by
// `persona: true`, runs with `enabled: false` (so the normal weighted pick never
// touches it) and is force-selected via SAPS_FORCE_PROJECT only when the mode
// toggle is on. Keep these defaults in lockstep with scripts/saps_mode.py and the
// hand-authored PersonalBrand entry in config.json.
export const PERSONA_DEFAULT_NAME = "PersonalBrand";

const PERSONA_DEFAULTS: Record<string, unknown> = {
  persona: true,
  enabled: false,
  weight: 10, // for the FUTURE personal/promo percentage blend; ignored while enabled:false
  voice_relationship: "first_party",
  description:
    "The user's own personal brand. Not a product. Goal is pure organic " +
    "engagement that grows the user's authority and following by adding genuine " +
    "value to conversations they have real experience with. No company, no " +
    "signup, no pitch.",
  content_angle:
    "Ground every reply in the user's actual first-hand experience. Only engage " +
    "a thread when there is a concrete, specific angle from that lived " +
    "experience; otherwise skip it.",
  voice: {
    tone:
      "write like you're texting a coworker. lowercase is fine, sentence " +
      "fragments are fine. first person and specific. reply to high-signal " +
      "comments, not just OP. match the thread's energy and length (1-2 " +
      "sentences is ideal).",
    never: [
      "self-promotion, links, or feature lists",
      "mentioning a product or company unless it directly solves OP's problem",
      "opening with 'Makes sense', 'The nuance here is', or 'What everyone here is describing'",
      "sounding like a blog post, a thought-leader, or an AI",
      "generic advice with no specific personal angle",
      "em dashes or en dashes",
    ],
  },
  content_guardrails: {
    summary:
      "This is personal-brand growth, not marketing. Follow a 60/30/10 mix: " +
      "~60% humor (self-deprecating dev stories, funny bugs, relatable pain), " +
      "~30% inspirational (cool technical wins, 'look what's possible'), ~10% " +
      "light personal mention only when it genuinely fits. NEVER attach a link " +
      "or a CTA. NEVER list features or name a product to sell it. Only reply " +
      "when the user has a real, specific angle from their own work; if the " +
      "thread doesn't connect to something they've actually done, skip it. Be a " +
      "value-adding peer, not a promoter.",
  },
  search_topics: [
    "AI agents",
    "Claude Code",
    "coding agents",
    "AI coding tools",
    "LLM developer workflow",
    "prompt engineering",
    "MCP servers",
    "macOS automation",
    "browser automation",
    "desktop app development",
    "building in public",
    "indie hacking",
    "shipping solo",
    "API costs",
    "developer productivity",
  ],
};

export interface PersonaGrounding {
  description?: string;
  content_angle?: string;
  voice?: unknown;
  search_topics?: string[] | string;
}

// The persona project entry (the one with persona:true), or null if none yet.
export function findPersonaProject(): { name: string } | null {
  try {
    const proj = (readConfig().projects || []).find((p) => p.persona === true);
    return proj ? { name: String(proj.name) } : null;
  } catch {
    return null;
  }
}

// Create the persona project if it doesn't exist; otherwise merge ONLY the
// supplied grounding fields (from the profile scan) onto the existing entry,
// never touching persona/enabled/weight. Writes config.json (backup + full
// rewrite, same as applySetup). Deliberately does NOT recordManagedProject.
export function ensurePersonaProject(
  grounding?: PersonaGrounding
): { name: string; created: boolean } {
  const cfg = readConfig();
  cfg.projects = cfg.projects || [];

  const g: Record<string, unknown> = {};
  if (grounding) {
    if (grounding.description && String(grounding.description).trim())
      g.description = grounding.description;
    if (grounding.content_angle && String(grounding.content_angle).trim())
      g.content_angle = grounding.content_angle;
    if (grounding.voice && typeof grounding.voice === "object")
      g.voice = grounding.voice;
    const topics = normalizeTopics(grounding.search_topics);
    if (topics && topics.length) g.search_topics = topics;
  }

  const existing = cfg.projects.find((p) => p.persona === true);
  let name: string;
  let created: boolean;
  if (existing) {
    Object.assign(existing, g); // merge only provided grounding; keep persona flags
    name = String(existing.name);
    created = false;
  } else {
    cfg.projects.push({ name: PERSONA_DEFAULT_NAME, ...PERSONA_DEFAULTS, ...g });
    name = PERSONA_DEFAULT_NAME;
    created = true;
  }

  const cfgPath = configPath();
  if (fs.existsSync(cfgPath)) {
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    fs.copyFileSync(cfgPath, `${cfgPath}.bak-${stamp}`);
  }
  fs.mkdirSync(path.dirname(cfgPath), { recursive: true });
  fs.writeFileSync(cfgPath, JSON.stringify(cfg, null, 2) + "\n", "utf-8");
  return { name, created };
}

// Heal installs that onboarded BEFORE short_links_live defaulted to false.
// Such a project has neither short_links_host nor an explicit short_links_live,
// so the wrapper host resolves to project.website — but the customer never
// shipped a /r/[code] resolver there, so every minted short link 404s. Set
// short_links_live=false (route /r/<code> through s4l.ai) for those projects.
//
// Scoped to projects this install actually manages, so a hand-maintained dev
// config with branded-resolver projects isn't rewritten. Idempotent: a project
// that already has either short-link field set is left untouched (someone made
// a deliberate choice). Best-effort; never throws.
export function ensureShortLinksDefault(): { healed: string[] } {
  const healed: string[] = [];
  try {
    const cfg = readConfig();
    const projects = cfg.projects || [];
    if (!projects.length) return { healed };
    const managed = new Set(managedProjects());
    let changed = false;
    for (const p of projects) {
      const name = typeof p.name === "string" ? p.name : "";
      if (!name || !managed.has(name)) continue;
      const hasHost =
        typeof p.short_links_host === "string" && p.short_links_host.trim() !== "";
      const hasLiveFlag =
        p.short_links_live === true || p.short_links_live === false;
      if (hasHost || hasLiveFlag) continue; // deliberate config: leave it.
      p.short_links_live = false;
      healed.push(name);
      changed = true;
    }
    if (changed) {
      const cfgPath = configPath();
      if (fs.existsSync(cfgPath)) {
        const stamp = new Date().toISOString().replace(/[:.]/g, "-");
        fs.copyFileSync(cfgPath, `${cfgPath}.bak-${stamp}`);
      }
      fs.mkdirSync(path.dirname(cfgPath), { recursive: true });
      fs.writeFileSync(cfgPath, JSON.stringify(cfg, null, 2) + "\n", "utf-8");
    }
  } catch {
    // best-effort heal; a failure here must never block boot.
  }
  return { healed };
}

// ---------------------------------------------------------------------------
// Readiness (derived from config.json, never stored).
// ---------------------------------------------------------------------------

// Which required fields are missing on the persisted project in config.json.
// Returns null when the named project can't be found/read.
export function missingForProject(name: string | undefined): string[] | null {
  if (!name) return null;
  try {
    const proj = (readConfig().projects || []).find((p) => p.name === name);
    if (!proj) return null;
    return REQUIRED_FIELDS.filter((f) => {
      const v = proj[f];
      if (v == null) return true;
      if (typeof v === "string") return v.trim() === "";
      if (Array.isArray(v)) return v.length === 0;
      if (typeof v === "object") return Object.keys(v).length === 0;
      return false;
    });
  } catch {
    return null;
  }
}

export interface ProjectStatus {
  name: string;
  in_config: boolean;
  ready: boolean;
  missing_required: string[];
}

export function projectStatus(name: string): ProjectStatus {
  const missing = missingForProject(name);
  if (missing === null) {
    return { name, in_config: false, ready: false, missing_required: [...REQUIRED_FIELDS] };
  }
  return { name, in_config: true, ready: missing.length === 0, missing_required: missing };
}

// Status of every project this install manages.
export function listManagedProjectStatus(): ProjectStatus[] {
  return managedProjects().map(projectStatus);
}

export function hasReadyProject(): boolean {
  return listManagedProjectStatus().some((s) => s.ready);
}

// The personal-brand persona project (persona:true) is intentionally kept OUT of
// the managed-products scope, so listManagedProjectStatus()/hasReadyProject() never
// count it. But in personal_brand mode the cycle DOES draft for it (force-selected
// via SAPS_FORCE_PROJECT). Without this helper a personal-brand-only ("self promo")
// setup looks like "no project configured" everywhere — the autopilot kicker never
// installs and the doctor can't see it. Reports whether the persona exists AND is
// fully configured (has every required field, incl. seeded topics). (2026-06-30)
export function personaReady(): boolean {
  const persona = findPersonaProject();
  if (!persona) return false;
  const missing = missingForProject(persona.name);
  return missing !== null && missing.length === 0;
}

// ---------------------------------------------------------------------------
// Gate the action tools call to resolve + validate the target project.
// ---------------------------------------------------------------------------
export interface Resolved {
  ok: boolean;
  project?: string;
  message?: string;
}

const SETUP_REQUIRED_MESSAGE =
  "No project is set up yet. Run the `project_config` tool first: collect from the user their website " +
  "URL, what the product does (description), who to target (icp), and brand voice/tone, then " +
  "call project_config with a short name plus those fields. You can set up multiple products; each is " +
  "configured independently and you fill the fields incrementally.";

export function resolveProject(requested?: string): Resolved {
  if (requested) {
    const st = projectStatus(requested);
    if (!st.in_config) {
      return {
        ok: false,
        message:
          `Project '${requested}' isn't set up yet. Run project_config with name='${requested}' plus its ` +
          `website, description, icp, and voice.`,
      };
    }
    if (!st.ready) {
      return {
        ok: false,
        message:
          `Project '${requested}' still needs: ${st.missing_required.join(", ")}. Ask the user ` +
          `for those and call project_config again with name='${requested}'.`,
      };
    }
    return { ok: true, project: requested };
  }
  const statuses = listManagedProjectStatus();
  const ready = statuses.filter((s) => s.ready).map((s) => s.name);
  if (ready.length === 1) return { ok: true, project: ready[0] };
  if (ready.length > 1) {
    return {
      ok: false,
      message: `Multiple projects are set up (${ready.join(", ")}). Tell me which one to use (pass the project name).`,
    };
  }
  const partial = statuses.filter((s) => !s.ready).map((s) => s.name);
  if (partial.length) {
    return { ok: false, message: `No project is fully set up yet. Finish setup for: ${partial.join(", ")}.` };
  }
  return { ok: false, message: SETUP_REQUIRED_MESSAGE };
}
