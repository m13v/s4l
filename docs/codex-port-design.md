# Codex / ChatGPT App Port: One Server, Three Providers, One Installer

Status: DESIGN (2026-08-06). Verified facts below were tested live on the operator Mac.
Owner context: S4L currently ships to Claude Desktop via .mcpb and to Claude Code via npm.
Goal: the same product working for ChatGPT/Codex users, without forking the pipeline.

## Verified facts (2026-08-06, operator Mac)

- Codex is merged into ChatGPT.app. Bundled CLI: `/Applications/ChatGPT.app/Contents/Resources/codex`
  (v0.146.0-alpha.3.1). The npm `@openai/codex` install is unreliable on macOS: XProtect
  false-positives trash the native binary (openai/codex #31377). Never depend on the npm copy.
- `codex exec` is the `claude -p` equivalent and works headlessly with ChatGPT-app
  subscription auth (no API key). Verified: one-shot prompts, `--json` event stream
  (thread.started / item.completed / turn.completed with usage), `-m` model override,
  `-c 'model_reasoning_effort="..."'`, agentic command execution (`-C <cwd> -s read-only`),
  and resume (`codex exec -s ... resume <thread_id> "prompt"`, flags BEFORE `resume`).
  `--output-schema` exists for structured output (not yet exercised).
- ChatGPT-account models as of 2026-08: gpt-5.6-terra (default), gpt-5.6-luna, gpt-5.5,
  gpt-5.4-mini. Older codex models 400 on ChatGPT accounts.
- MCP client config: `~/.codex/config.toml` `[mcp_servers.*]`; `codex mcp add/get/remove`
  round-trip verified. Stdio transport, same protocol our server already speaks.
- Scheduled tasks: Codex "Automations" (`~/.codex/automations/<id>/automation.toml`,
  RRULE + prompt + model + `execution_environment = "local"`). Same "app must be open"
  constraint as Claude Desktop routines. We deliberately do NOT use them for the worker
  (see providers below); launchd + `codex exec` needs no app open.
- Plugins: `codex plugin add` installs from a configured marketplace
  (`codex plugin marketplace add <source>`); marketplaces can be local or remote catalogs.
  Public one-click needs the universal directory (submission; possibly Work-plan gated).

## Architecture: one server + host adapter

The MCP server (`mcp/dist/index.js`, stdio) is already host-agnostic. Do NOT fork it and
do NOT create a separate binary. Add a host adapter that branches only where the host
actually differs:

- Host detection: MCP `initialize` handshake `clientInfo.name`, plus env
  (`CLAUDECODE`/`CLAUDE_CONFIG_DIR` vs `CODEX_HOME`/`CODEX_*`).
- Branch points: `queue_setup` (worker wiring), version/self-update messaging, and the
  menubar quit/relaunch modals (relaunch ChatGPT vs Claude). Everything else
  (project_config, approve_drafts, stats, review queue, posting, launchd lanes) is
  untouched: it never depended on Claude.
- Open question to test early: does the ChatGPT app render our MCP Apps panel
  (`ui://social-autoposter/panel.html`)? Fallback if not: menubar + loopback HTTP server,
  both already host-independent.

## Provider matrix (who drains the draft queue)

All providers speak the identical contract: poll `claude_job.py next --type any`,
feed the job's self-contained prompt (+ schema) to the provider, post back via
`claude_job.py result` with the same claude-shaped JSON envelope. The pipeline never
knows which provider answered.

| Provider | Worker runtime | App open? | Auth | Notes |
|---|---|---|---|---|
| `claude-desktop-task` | Claude Desktop routine (today's `s4l-worker`) | YES | Desktop login | Compatibility floor for pure-.mcpb installs. Carries the scheduler wedge, warm-session pileup, per-account registry orphaning. |
| `claude-p` | launchd lane, `claude -p ... --output-format json` per job | no | Claude Code CLI (Pro/Max) | Operator Mac already proves claude-p-under-launchd + keychain works. Escape hatch that retires the wedge/reaper for any customer with the CLI. |
| `codex-exec` | launchd lane, `codex exec --json --output-schema` per job | no | ChatGPT app login | Use the app-bundled binary path, never npm. Default model gpt-5.6-terra. |

Selection at setup:
- Installed from a Codex chat -> `codex-exec`.
- Installed from a Claude Code chat -> `claude-p`.
- .mcpb double-click, no CLI on box -> `claude-desktop-task` (current path).

Single-driver guard (MANDATORY): record the chosen provider in the state dir
(`provider.json` or a field in `mode.json`). `queue_setup` must refuse or cleanly take
over when a different provider owns the lane. Same bug class as the launchd-vs-autopilot
double-post incident; do not skip this.

Failover UX (later): stall-watch/deadman may offer "switch drafting to the always-on
lane" when the desktop-task lane wedges and a working CLI login exists.

## Installer: the curl lane (Lane 1)

Non-technical users paste one sentence into their agent chat (Codex or Claude, same
sentence):

    Install S4L: run `curl -fsSL https://s4l.ai/install | sh` and follow its output.

No npm, no node, no git required. macOS ships /usr/bin/curl and /usr/bin/unzip; the
.mcpb already vendors its own Node runtime. The machinery mostly exists:
fixed release URLs (scripts/get-latest-staging-mcpb.sh; releases/latest for stable) and
the curl+unzip pattern (scripts/s4l_box_update.sh).

install.sh (~60 lines, hosted at s4l.ai/install on the Vercel marketing site):
1. curl the .mcpb (a zip) from the fixed release URL.
2. unzip into the durable, host-neutral dir `~/.social-autoposter-mcp/app/`
   (NOT the Claude extension dir).
3. exec the bundled Node against a new setup entry point:
   `vendor/node-darwin-arm64/bin/node dist/index.js --setup`.
4. `--setup` runs host detection + provider selection, registers the MCP server
   (`codex mcp add` / `~/.claude.json`), installs the chosen worker lane, prints the
   "restart the app" instruction.

Updates: unchanged. The menubar self-updater already pulls GitHub releases and
re-extracts; curl-installed boxes behave exactly like .mcpb boxes.

Distribution lanes, in order: (1) curl lane now; (2) self-hosted Codex plugin
marketplace (plugin bundling the same server; clean updates, no OpenAI approval);
(3) universal plugin directory submission for true one-click (verify plan gating first).

Scope: macOS only (darwin-arm64 vendored Node, launchd, menubar). Codex-on-Windows is
explicitly out of scope for v1.

## Phases

0. De-risk (before any product code):
   a. PoC worker bridge: script polls `claude_job.py next`, runs `codex exec --json
      --output-schema`, posts `result`. Prove the envelope round-trips on a sandbox job.
   b. Register the S4L MCP server in `~/.codex/config.toml` on the operator Mac; open the
      ChatGPT app; test tools + whether the MCP Apps panel renders.
   c. Draft-quality eval: replay real candidates through gpt-5.6-terra via the prompt
      sandbox and compare against Claude drafts. Drafts are the product; the model swap
      is a voice change, not just plumbing.
1. Provider abstraction: worker bridge with provider adapter (`claude-p` + `codex-exec`),
   `provider.json` + single-driver guard, `queue_setup` branching, host detection.
2. Installer: `--setup` entry in dist/index.js, install.sh, release-mcpb.sh step to
   publish it, s4l.ai/install route.
3. Polish: menubar host-awareness (relaunch ChatGPT vs Claude), docs, staging rc,
   remote-QA on the box, then stable.

## Cautions

- The 90s host-inactivity timeout and 900s single-tool-call ceiling baked into the
  worker prompt are Claude-Desktop-empirical; the launchd bridge providers don't need
  them, and Codex-side limits must be measured, not assumed.
- Locked pipeline files stay locked. The seam (`claude_job.py`, worker bridge, queue_setup
  in mcp/src) is all in unlocked files; if a locked file seems to need changes, stop and
  surface it.
- Never reintroduce a provider env var read at post time; provider choice lives in state,
  stamped once (same principle as the experiments-stamp-at-source rule).
