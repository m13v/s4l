# social-autoposter

Open-source repo behind **[S4L (s4lai)](https://s4l.ai)**: an automated social posting pipeline for Reddit, X/Twitter, LinkedIn, and Moltbook. Ships as a Claude Code skill plus a set of standalone Python helpers and macOS launchd jobs.

> The hosted managed version is **S4L** (written `s4lai`, domain `s4l.ai`): done-for-you Reddit and Twitter brand-awareness, $1/1K impressions, $50/1K site visits. See https://s4l.ai.

State (posts, replies, candidates, stats) is read and written through the hosted S4L HTTP API (`AUTOPOSTER_API_BASE` + an install key in `~/social-autoposter/.env`); no database to provision. Each platform drives its own persistent Playwright MCP browser profile, so logins survive across runs.

## Prerequisites

A new machine needs all of these before the pipeline can run end to end:

- **macOS** (the launchd plists are mac-only; Linux users can crib the cron snippets from `setup/SKILL.md` Step 7)
- **Node.js 16+** (for `npx`, the installer, and `@playwright/mcp` at runtime)
- **Python 3.9+** with `pip3` (helper scripts; deps auto-installed by the installer)
- **Claude Code CLI** on `PATH` (the cron scripts shell out to `claude -p` with a per-platform MCP config)
- One Chromium install per platform (created on first run by `@playwright/mcp` against the persistent profile dirs)

Optional:

- `MOLTBOOK_API_KEY` in `.env` for Moltbook posting and scanning
- `RESEND_API_KEY` and `NOTIFICATION_EMAIL` in `.env` for DM-escalation emails

## Install

```bash
npx social-autoposter init
```

`bin/cli.js` does all of the wiring in one shot:

1. Copies `scripts/`, `skill/`, `setup/`, `SKILL.md`, and `browser-agent-configs/` into `~/social-autoposter/`
2. Creates `config.json` from `config.example.json` and writes a blank `.env` template (fill in your S4L API key and optional `MOLTBOOK_API_KEY`)
3. Installs the Python helper deps via `pip3` if missing
4. Generates launchd plists in `~/social-autoposter/launchd/` with the user's actual `HOME` and `PATH`
5. Installs the Playwright MCP configs to `~/.claude/browser-agent-configs/` (twitter, reddit, linkedin) with `__HOME__` and `__NODE_BIN__` placeholders substituted. Existing files are left alone, so any window-position tweaks survive `npx social-autoposter update`.
6. Creates empty persistent browser profile dirs at `~/.claude/browser-profiles/{twitter,reddit,linkedin}`
7. Symlinks `~/.claude/skills/social-autoposter` and `~/.claude/skills/social-autoposter-setup` to the install dir

To refresh code without touching user files (`config.json`, `.env`, `SKILL.md`, or any browser config you customized):

```bash
npx social-autoposter update
```

## Configure

Tell your Claude agent: **"set me up on social-autoposter end to end"**. The
setup skill treats that as a terminal goal:

1. Inspect and repair the owned runtime.
2. Auto-detect the best browser profile and connect X/Twitter. macOS may require
   a Safe Storage approval; a logged-out account may require one manual sign-in.
3. Scan the X profile, discover and research the user's product, and infer a
   conservative project, ICP, voice, and search topics without an interview.
4. Save the project and seed its topics into the backend.
5. Run a draft-only cycle to verify the pipeline without posting.

The agent pauses only for an unavoidable login or when no product can be
identified. Autopilot remains off until explicitly requested.

## How the runtime is wired

```
launchd  ──▶  skill/run-{platform}.sh  ──▶  claude -p  --strict-mcp-config  --mcp-config ~/.claude/browser-agent-configs/{platform}-agent-mcp.json
                       │                                        │
                       │                                        └──▶  @playwright/mcp@latest
                       │                                                       │
                       │                                                       └──▶  ~/.claude/browser-profiles/{platform}/  (persistent userDataDir)
                       │
                       ├──▶  scripts/find_threads.py, top_twitter_queries.py  (no browser, API dedup)
                       ├──▶  scripts/pick_project.py            (weighted project rotation)
                       ├──▶  scripts/top_performers.py          (feedback report from past stats)
                       └──▶  S4L HTTP API                       (AUTOPOSTER_API_BASE in .env)
```

Optional X/Twitter source import from TweetClaw:

```bash
openclaw plugins install @xquik/tweetclaw
python3 ~/social-autoposter/scripts/tweetclaw_candidates.py \
  --file /path/to/reviewed-tweetclaw-results.json \
  --project "PROJECT_NAME" \
  --search-topic "agent workflows" \
  --query "agent workflows min_faves:10" \
  | python3 ~/social-autoposter/scripts/score_twitter_candidates.py
```

Use this when an OpenClaw run has already reviewed TweetClaw search tweets,
search tweet replies, user lookup, follower export, media links, monitor tweets,
or webhook evidence and you want those public X/Twitter records scored by the
existing candidate pipeline. The importer only normalizes local JSON and prints
candidate rows; the existing scorer in the second command performs the normal
scoring and upsert step. The importer does not post tweets, post tweet replies,
send direct messages, upload media, call the S4L API, or drive the browser.

Each `skill/run-*.sh`:

1. Controlled by launchd (load/unload). Use the dashboard Pause All / Resume All button, or `launchctl unload/load` directly
2. Acquires a per-platform lock from `skill/lock.sh` (waits up to 60 min for any prior run)
3. Sources `~/social-autoposter/.env`
4. Picks a project, builds a feedback report, fetches `llms.txt` for product context
5. Calls `find_*.py` for API-side candidates already deduped against the DB
6. Spawns a child Claude process with `--strict-mcp-config` so it only sees the one platform's browser MCP

The launchd schedules generated by `bin/cli.js` on install:

| Job | Cadence |
|-----|---------|
| `com.m13v.social-stats` (`stats.sh`) | every 21600 s (6 h) |
| `com.m13v.social-engage` (`engage.sh`) | every 21600 s (6 h) |

All per-platform plists live in `launchd/` (reddit-search, reddit-threads, twitter-cycle, linkedin, moltbook, github, octolens, audit, dm-replies-*, link-edit-*, scan-reddit-replies, scan-moltbook-replies, etc.) and use either `StartInterval` or `StartCalendarInterval` for fixed wall-clock times. Activate any of them with:

```bash
ln -sf ~/social-autoposter/launchd/com.m13v.social-twitter-cycle.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.m13v.social-twitter-cycle.plist
```

## Skill commands

| Command | What it does |
|---------|-------------|
| `/social-autoposter` | Comment run: find threads, draft, post, log (cron-safe) |
| `/social-autoposter post` | Create an original post or thread (manual only) |
| `/social-autoposter stats` | Update engagement stats via API |
| `/social-autoposter engage` | Scan and reply to responses on our posts |
| `/social-autoposter audit` | Full browser audit of all posts |

View live stats at `https://s4l.ai/stats/<your-handle>` once posts start landing.

## Repo layout

```
social-autoposter/
├── SKILL.md                  the playbook (locked, immutable)
├── bin/cli.js                installer + dashboard launcher
├── browser-agent-configs/    Playwright MCP templates (twitter/reddit/linkedin)
├── config.example.json       config template
├── setup/SKILL.md            autonomous end-to-end setup skill (locked)
├── scripts/                  Python and JS helpers (no browser, no LLM)
│   └── tweetclaw_candidates.py optional TweetClaw JSON import for X/Twitter candidates
├── skill/                    shell wrappers invoked by launchd
└── launchd/                  generated macOS LaunchAgent plists
```

## For other AI agents

The skill works with any agent that has shell access, browser automation, and an LLM. The Python and JS helpers in `scripts/` handle thread discovery, reply scanning, and stats updates without needing a browser. `SKILL.md` is the playbook; any agent can read it and execute the workflows with its own tools.

## Pause and resume

Use the dashboard at `localhost:3141` (Pause All / Resume All button), or manually:

```bash
# Pause: unload all jobs + kill running processes
for plist in ~/Library/LaunchAgents/com.m13v.social-*.plist; do launchctl unload "$plist"; done

# Resume: reload all jobs
for plist in ~/social-autoposter/launchd/com.m13v.social-*.plist; do
  ln -sf "$plist" ~/Library/LaunchAgents/
  launchctl load ~/Library/LaunchAgents/$(basename "$plist")
done
```
