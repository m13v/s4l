# Getting started with social-autoposter

This is the paste-able bootstrap prompt for a brand-new machine. After restart,
one setup request drives the second stage through a draft-only verification.

## How to use it

1. Open **Claude Code** (the terminal CLI), NOT the Claude Desktop app. Desktop's
   shell runs in a throwaway Linux container that can't touch your Mac, so it
   cannot install anything locally. Claude Code has a real shell on your machine.
2. Paste the prompt below. Let the agent execute every safe installation and
   repair step without asking you to choose implementation details.
3. When it finishes, relaunch Claude, then send
   "Set me up on social-autoposter end to end."

---

## The prompt

```
You are helping me set up social-autoposter on THIS Mac. social-autoposter is an
open-source X/Twitter automation tool published on npm by m13v
(https://www.npmjs.com/package/social-autoposter). The `init` command clones the
pipeline, installs its runtime (uv + a bundled browser-harness), and registers a
local MCP server into Claude Desktop and Claude Code so you can drive it
conversationally. I'm asking you to run it end to end. Keep taking the next safe
action without asking whether to continue; report concise progress and stop
only for an unavoidable OS prompt or login.

PHASE 1 - ENVIRONMENT (needs a real shell, so do this in Claude Code, not Desktop)
Check each prerequisite, install whatever's missing via Homebrew, and tell me
what was already present vs. what you installed:
  - Homebrew (if missing, install from https://brew.sh)
  - node    (brew install node)    -> `node -v` must work
  - python3 (brew install python)  -> prefer /opt/homebrew/bin/python3
  - Google Chrome (the autoposter posts through its own managed Chrome)

PHASE 2 - INSTALL + REGISTER (one command)
Run:  npx social-autoposter init
This provisions the pipeline, installs the MCP runtime deps, and registers the
`social-autoposter` MCP into BOTH Claude Desktop and Claude Code. It's idempotent;
safe to re-run. Then confirm `social-autoposter` appears under `mcpServers` in
~/.claude.json AND in
~/Library/Application Support/Claude/claude_desktop_config.json.

TROUBLESHOOTING (handle these yourself, don't make me debug)
  - init fails on the npm step inside mcp/: re-run
    `cd ~/social-autoposter/mcp && npm install --omit=dev && node install.mjs`.
  - MCP tools don't appear after restart: re-check the two config files above; if
    an entry's command/args point at a missing path, re-run
    `node ~/social-autoposter/mcp/install.mjs`.
  - python errors: make sure the MCP's SAPS_PYTHON points at
    /opt/homebrew/bin/python3, not /usr/bin/python3.

When everything above succeeds, tell me the next step in your own words: I need
to fully quit and relaunch Claude (Cmd+Q, not just close the window) so the new
MCP loads, then send "Set me up on social-autoposter end to end."
```

---

## What happens after they restart

Sending **"Set me up on social-autoposter end to end"** (or picking the `/`
slash-command "Set up social-autoposter" in Claude Desktop) gives the agent a
terminal goal. It:

1. Runs the pre-connect Doctor, shows a durable setup checklist, installs or
   repairs the owned runtime, and resumes existing progress.
2. Auto-detects the best browser profile, warns about possible macOS keychain
   prompts, then imports only x.com/twitter.com cookies into managed Chrome.
3. Scans the connected X profile, discovers the most clearly associated
   product, researches its public website, and infers a conservative project,
   ICP, voice, and search topics without an interview.
4. Saves the project and seeds its search topics.
5. Runs `draft_cycle` to verify the pipeline without posting.

The checklist and full Doctor results are stored locally in
`~/.social-autoposter-mcp/onboarding-progress.json`. After X connects, the full
Doctor runs again to verify the session and durable cookie mirror.

It pauses only if the user must sign in interactively or no product can be
identified. Autopilot stays off until explicitly requested.
