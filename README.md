# S4L

S4L is a desktop plugin for running a Reddit and X reply-drafting workflow from
your own machine.

It watches the conversations your buyers already read, finds threads with real
momentum and fit, drafts contribution-first replies in your voice, and gives you
a review queue before anything goes live. The product is positioned as a
self-serve desktop plugin: subscribe, install, connect your accounts, review the
drafts, and track the results.

- Website: https://s4l.ai
- Pricing: https://s4l.ai/pricing
- Source: https://github.com/m13v/s4l

## What S4L does

Most social tools start after you already know what to post. S4L starts earlier:
it looks for conversations worth entering.

- Finds high-traffic Reddit and X threads with enough context to join well.
- Ranks opportunities by momentum, intent, community norms, and product fit.
- Drafts replies that answer the thread first and mention your product only when
  it belongs.
- Keeps project-specific voice, claim language, competitors, sensitive topics,
  and hard lines in the drafting context.
- Presents drafts in a review workflow so you can approve, reject, or edit before
  posting.
- Tracks views, upvotes, clicks, deletions, and misses so each run learns from
  the previous one.

Nothing is posted automatically by default. Posting autopilot stays off until you
explicitly turn it on.

## Who it is for

S4L is for founders and small teams who want an AI-assisted social workflow
without handing their accounts to an agency or living inside mention alerts all
day.

You bring the accounts, product context, voice, and judgment. The plugin handles
thread discovery, draft generation, review cards, scheduling, and result memory.

## How users install it

The production path is the desktop plugin download from S4L.

1. Subscribe at https://s4l.ai/pricing.
2. Download the plugin link emailed after checkout.
3. Install the plugin in Claude Desktop.
4. Start a new Claude chat and send:

```text
Set me up on S4L plugin end to end
```

The setup flow repairs the local runtime, connects your X browser session,
discovers product and voice context, seeds search topics, schedules draft
generation, and verifies that draft cards appear without posting.

## What is in this repo

This repository contains the open-source runtime behind the S4L plugin:

```text
social-autoposter/
|-- mcp/                    Desktop plugin / MCP server, panel UI, release bundle
|-- scripts/                Discovery, drafting, stats, telemetry, and queue helpers
|-- skill/                  Shell entrypoints used by scheduled jobs
|-- setup/                  End-to-end setup skill used by Claude
|-- browser-agent-configs/  Browser automation profile templates
|-- launchd/                macOS LaunchAgent templates
|-- mcp-servers/            Local MCP helpers used by the runtime
|-- config.example.json     Example project/account configuration
`-- SKILL.md                Legacy social-autoposter agent playbook
```

The `mcp/` package is the plugin users interact with. The rest of the repo is the
pipeline it bundles, installs, and drives.

## Architecture

```text
Claude Desktop plugin
  -> S4L MCP server
  -> local runtime + menu bar review UI
  -> scheduled queue worker
  -> browser profiles you control
  -> S4L API for install-scoped queues, stats, and configuration
```

Important properties:

- Runs locally on macOS.
- Uses the user's own logged-in browser profiles.
- Stores install state locally and scopes API calls by install identity.
- Keeps secrets such as `.env`, `config.json`, browser profiles, logs, and local
  databases out of Git.
- Uses approval-first drafting as the default operating mode.

## Optional TweetClaw source import

[TweetClaw](https://github.com/Xquik-dev/tweetclaw) can provide reviewed public
X records to the existing candidate scorer. Install it separately, export the
results to JSON, then run:

```bash
python3 scripts/tweetclaw_candidates.py \
  --file /path/to/reviewed-tweetclaw-results.json \
  --project "PROJECT_NAME" \
  --search-topic "agent workflows" \
  --query "agent workflows min_faves:10" \
  | python3 scripts/score_twitter_candidates.py
```

The importer only normalizes local JSON. It does not post, send messages, call
the S4L API, or control a browser.

## Develop from source

For normal users, use the plugin download from S4L. These steps are for working
on the repo itself.

Prerequisites:

- macOS
- Node.js 16+
- Python 3.9+
- Claude Desktop or Claude Code for MCP testing

Install dependencies:

```bash
npm install
cd mcp
npm install
```

Build the plugin server and panel:

```bash
cd mcp
npm run build
```

Register this checkout with Claude Desktop and Claude Code for local testing:

```bash
cd mcp
node install.mjs
```

Then fully quit and reopen Claude so MCP servers reload.

Run the test suite:

```bash
npm test
```

Build a local `.mcpb` artifact without publishing:

```bash
bash scripts/release-mcpb.sh --no-bump --no-npm --no-release
```

That command packs the current pipeline into `mcp/dist/pipeline.tgz`, builds the
plugin, creates `mcp/social-autoposter.mcpb`, and runs the release checks without
touching npm or GitHub releases.

## Runtime commands

The plugin exposes MCP tools for the user-facing workflow:

- `project_config` configures projects, products, voice, topics, and X auth.
- `engagement_mode` chooses personal-brand and product-promotion lanes.
- `dashboard` opens the review dashboard.
- `approve_drafts` posts only the drafts the user selected.
- `get_stats` reads X/Twitter stats.
- `pause_s4l` pauses or resumes scheduled S4L jobs.
- `runtime` installs, updates, and diagnoses the local runtime.
- `report_diagnosis` sends a support report to the S4L team.

The legacy `/social-autoposter` skill and npm package remain in the repo because
the plugin bundles and reuses the same pipeline scripts.

## Public-repo hygiene

This repo is public. Do not commit local customer data, browser state, generated
media, private automation experiments, `.mcpb` bundles, `.env` files, databases,
or logs. The root `.gitignore` intentionally keeps those out of source control.

If a workflow needs private scratch space, keep it outside the repo or under an
ignored directory.
