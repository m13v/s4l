---
name: social-autoposter-setup
description: "Set up social-autoposter for a new user. Configures the product (website, ICP, voice, search topics), seeds search topics into the backend, connects X/Twitter (auto-detecting the real handle), and verifies with a draft cycle. Use when: 'set up social autoposter', 'install social autoposter', 'configure social posting'."
---

# Social Autoposter Setup

Set up social-autoposter for a new user. Walk them through it conversationally — don't dump a form.

## Architecture (read this first)

- **Config**: `~/social-autoposter/config.json` — `projects[]` (what to post about) and `accounts` (where to post).
- **Data + stats**: a backend HTTP API (`https://s4l.ai`), scoped by a **stable per-install identity** auto-created in `identity.json`. There is **NO local Postgres and no `DATABASE_URL`** to configure — that was the old architecture; ignore any reference to psycopg2 / `SELECT ... FROM posts`.
- **Search topics**: the X cycle's search queries live in the DB table `project_search_topics`, **seeded from each project's `search_topics`** at setup. A project with no seeded topics has nothing to scan and the draft cycle returns empty — so topics are required.

## Which path to use

- **If the social-autoposter MCP is connected** (you can see the tools `setup`, `draft_cycle`, `autopilot`, `get_stats`): use the MCP tools. They write config, **seed topics into the DB**, and **auto-detect the X handle** for you. Do NOT hand-edit `config.json`. This is the primary path — follow "MCP path" below.
- **If only the CLI/skill is installed** (no MCP tools): use the "CLI fallback" at the end.

---

## MCP path (primary)

### Step 1: Interview the user, one question at a time

Gather the fields the `setup` tool needs. Ask conversationally, wait for each answer.

1. **Website** — "What's the product's website?"
2. **Description** — "In 1-3 sentences, what does it do?"
3. **ICP** — "Who's the ideal customer you want to engage on X?"
4. **Voice** — "What tone should replies have? Any words/claims to avoid?"
5. **Differentiator** (recommended) — "What makes it different from the alternatives?"
6. **Search topics** (required) — "What phrases or keywords do your buyers actually tweet about? Give me 5-15, comma-separated." These become the literal X searches the cycle runs. **Without them there is nothing to scan**, so don't skip this.
7. **Get-started link** (recommended) — "Primary call-to-action link (signup / get started)?"

For the voice/angle, it helps to draft a short first-person `voice`/`differentiator` from their answers and confirm it reads like them before saving. Aim for specific (names tools, numbers, real experience), not generic.

### Step 2: Create the project with `setup`

Call the `setup` tool with a short slug `name` plus the fields above. Pass `search_topics` as a comma-separated string or array. You can fill fields incrementally across calls — it merges and reports what's still missing. A project is **ready** only once it has name, website, description, icp, voice, **and search_topics**.

When the project becomes ready, `setup` **automatically seeds its `search_topics` into the DB** (`project_search_topics`) and tells you how many it seeded. You do not run any seed script by hand.

### Step 3: Connect X/Twitter

Call `setup` with `action:'connect_x'` (no `confirm`) first — it returns an explanation of what will happen (it imports your x.com/twitter.com cookies into the autoposter's managed Chrome). Relay that to the user, get their OK, then call again with `action:'connect_x', confirm:true`.

This imports the session **and auto-detects + records your real `@handle`** into `config.json` (`accounts.twitter.handle`). That handle scopes attribution, own-reply skipping, and account-keyed operations — so do not hand-edit it to a placeholder.

### Step 4: Verify with a draft cycle

Run the `draft_cycle` tool. It scans X, drafts replies, and shows them for your approval — it **posts nothing** until you approve. If it comes back empty with a clear reason (e.g. "no search topics"), fix that (re-run `setup` with topics) and try again. A non-empty review form means the pipeline is healthy end-to-end.

### Step 5 (optional): Autopilot

If the user wants hands-free posting, call `autopilot` with `action:'enable'` — it loads the background cycle and daily auto-updates. `action:'status'` reports whether it's loaded; `action:'disable'` turns it off (manual `draft_cycle` still works).

---

## CLI fallback (no MCP)

Only if the MCP tools aren't available.

1. **Install**: `npx social-autoposter init` (creates `config.json` from the template and `.env`; symlinks the skill). Update later with `npx social-autoposter update`.
2. **Configure the project**: edit `~/social-autoposter/config.json` `projects[]` with `name`, `website`, `description`, `icp`, `voice`, and `search_topics` (array). Leave `accounts.twitter.handle` empty — it's filled on connect.
3. **Seed topics into the DB** (the cycle reads the DB, not config): `python3 scripts/seed_search_topics.py --project <name>`.
4. **Connect X**: `python3 scripts/setup_twitter_auth.py connect` — imports the session and records the real handle.
5. **Verify**: `DRAFT_ONLY=1 TWITTER_PAGE_GEN_RATE=0 bash skill/run-twitter-cycle.sh` — drafts without posting; it prints `DRAFT_ONLY_PLAN=<path>` on success.
6. **Automation** (optional): on macOS, symlink + load the launchd plists in `skill/launchd/`; on Linux, add the matching cron entries.

---

## Summary to show the user

```
Social Autoposter Setup Complete

  Installed:   ~/social-autoposter  (via npm)
  Config:      ~/social-autoposter/config.json
  Backend:     s4l.ai HTTP API (per-install identity; no local DB)

  Project:     NAME — ready
  Search topics seeded: N
  X/Twitter:   @HANDLE (auto-detected on connect)

  Verify:      draft_cycle  (drafts for review, posts nothing)
  Autopilot:   autopilot action:'enable'  (hands-free)
  Stats:       https://s4l.ai/stats/HANDLE
  Update:      npx social-autoposter update
```

Tell the user their stats page (`https://s4l.ai/stats/<handle>`) populates after the first real post.
