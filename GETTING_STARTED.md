# Getting started with social-autoposter

This is the paste-able onboarding prompt for a brand-new machine.

## How to use it

1. Open **Claude Code** (the terminal CLI), NOT the Claude Desktop app. Desktop's
   shell runs in a throwaway Linux container that can't touch your Mac, so it
   cannot install anything locally. Claude Code has a real shell on your machine.
2. Paste the prompt below.
3. When it finishes, it tells you (in bold) exactly what to send next to begin.

---

## The prompt

```
You are helping me set up social-autoposter on THIS Mac. social-autoposter is an
open-source X/Twitter automation tool published on npm by m13v
(https://www.npmjs.com/package/social-autoposter). The `init` command clones the
pipeline, installs its runtime (uv + a bundled browser-harness), and registers a
local MCP server into Claude Desktop and Claude Code so you can drive it
conversationally. I'm asking you to run it; show me each command and its result
as you go so I can see what's happening.

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

When everything above succeeds, your FINAL message to me must end with this line,
in bold, on its own:

**Done. Fully quit and relaunch Claude (Cmd+Q), then send me: "Set me up on social-autoposter" to begin.**
```

---

## What happens after they restart

Sending **"Set me up on social-autoposter"** (or picking the `/` slash-command
"Set up social-autoposter" in Claude Desktop) triggers the MCP's `setup` tool,
which walks the user through:

1. Their product (website, what it does, ICP, voice) - one question at a time.
2. Connecting X: `setup action:connect_x` imports their x.com cookies from their
   everyday browser into the managed Chrome; if that fails it opens a login
   window for a one-time manual sign-in.
3. `draft_cycle` - scans X, drafts replies, approve/skip before posting.
4. `autopilot enable` - background posting once they trust it.
