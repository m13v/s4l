---
name: social-autoposter-setup
description: "Set up social-autoposter for a new user end to end. Installs/repairs the runtime, discovers product and voice context, connects X/Twitter, seeds search topics, schedules the draft autopilot, and verifies it produces a draft. Use when: 'set up social autoposter', 'install social autoposter', 'configure social posting'."
---

# Social Autoposter Setup

Set up social-autoposter end to end. Treat the user's setup request as a terminal
goal, not as the beginning of an interview.

## Operating contract

- Keep taking the next safe setup action until the system is working end to end.
- Do not ask whether to run setup, install a dependency, inspect status, connect
  X, scan the profile, research the website, save inferred fields, seed topics,
  or retry a recoverable failure. Do them.
- An explicit request to set up social-autoposter authorizes its owned local
  runtime installation and importing only x.com/twitter.com session cookies
  into its managed browser. Briefly warn that macOS keychain prompts may appear,
  then proceed; do not wait for another yes/no reply.
- Use existing configuration, the user's message/context, their connected X
  profile, public website pages, and reasonable factual inference before asking
  for information.
- Ask at most one bundled blocking question, and only when required information
  cannot be discovered or safely inferred. The usual legitimate blocker is:
  there is no configured project, no clear product URL in context or the X
  profile, and no way to identify what product to market.
- Never post a draft during setup unless the user explicitly asked for that.
- Never hand-run the X cycle. Do not run `run-twitter-cycle.sh`,
  `claude_job.py`, or any cycle script directly, and never offer to "trigger a
  cycle" or "run one now" so a draft appears faster. Only the launchd kicker may
  fire the cycle, with its required environment. A manual kick produces an
  empty-plan artifact and blocks the autopilot. Verification means scheduling
  the autopilot and letting the kicker drive it; you wait and poll, you never
  trigger.
- Do not edit the MCP server, plugin source, or an unrelated user workspace to
  work around setup failures. Use the product's setup/install tools.

## Definition of done

Do not report setup complete until all of these are true:

1. The owned runtime is installed and ready.
2. At least one project is ready with name, website, description, ICP, voice,
   and search topics. For the personal-brand persona, the search topics and the
   grounding corpus come from the user's dictation interview (below), not from
   the profile scan alone.
3. Search topics have been seeded into the backend. Seeding is POSTPONED until
   after the dictation interview so the topics reflect what the user wants to be
   in conversations about, not only what they already posted.
4. X is connected and the real handle has been auto-detected.
5. The draft autopilot is scheduled and has produced a draft card without
   posting. A returned/pending review batch is the strongest success signal. If X
   simply has no matching supply, report that precise result only after
   configuration/auth/runtime checks pass.

## Architecture

- **Config**: `~/social-autoposter/config.json` — `projects[]` (what to post
  about) and `accounts` (where to post).
- **Data + stats**: backend HTTP API at `https://s4l.ai`, scoped by a stable
  per-install identity in `identity.json`. There is no local Postgres or
  `DATABASE_URL`.
- **Search topics**: the X cycle reads `project_search_topics`, seeded
  automatically from each project's `search_topics`. No topics means nothing
  to scan. `search_topics` is the ONLY field that changes what gets scanned;
  every other persona field only shapes the draft after a thread is found. For
  the persona, the profile scan is backward-looking (only what the user already
  posted), so topics are sourced primarily from the dictation interview.

## Choose the path

- If the social-autoposter MCP tools are connected (`project_config`, `runtime`,
  `queue_setup`, `post_drafts`, `get_stats`, `dashboard`), use the MCP path.
  Do not hand-edit `config.json`.
- If only the CLI/skill is installed, use the CLI fallback.

## MCP path

### 1. Inspect and repair the environment

Call `project_config` in status mode and `runtime` (action:'status') immediately.

If the runtime is not ready:

1. Call `runtime` with action:'install'.
2. Poll `runtime` (action:'status') until it succeeds or returns a concrete failure.
3. For a recoverable/partial failure, call `runtime` action:'install' again and continue.
   Do not send the user away to install Chrome, Python, uv, Chromium, or
   browser-harness manually; the owned installer handles them.

Preserve any already-ready project or X connection. Resume from the first
incomplete milestone instead of restarting the interview.

### 2. Connect X and learn the user's voice

If X is not connected:

1. Call `project_config` with `action:'detect_x_sources'`.
2. Choose `recommended`, preferring a source whose `x_session` is present.
   Do not ask the user to choose a browser profile when the tool can choose.
3. Tell the user in one short progress update that macOS may show browser Safe
   Storage prompts and they should enter their Mac password and click **Allow**
   or **Always Allow**.
4. Immediately call `project_config` with `action:'connect_x', confirm:true` and the
   selected `x_source`. The setup request itself is authorization; do not add a
   separate consent round-trip.
5. If the result is transient, retry. If it opens managed Chrome in
   `needs_login`, tell the user to finish signing in to x.com in that window.
   This is an unavoidable user action, not a product-choice question. Re-run
   `connect_x` after sign-in and continue.

Once connected, call `project_config` with `action:'profile_scan'`. Treat the returned
bio, links, recent posts, and replies as grounding truth for the VOICE, not as the
primary source of topics (the scan only shows what they already posted):

- profession/identity;
- voice, casing, phrasing, and tone;
- ICP;
- wording or claims the user avoids;
- recurring themes (reinforcement for topics, not the origin).

Do not ask the user to approve the inferred voice during initial setup. Save a
specific, conservative best draft and mention afterward that it can be edited.

Then run the DICTATION INTERVIEW before extracting topics or seeding. This is
where the persona's `search_topics` and grounding corpus come from. Keep the
framing CHILL: this is a casual brain-dump, not a form. Invite the user to
answer the following in ONE spoken dictation (the Claude input box already
supports dictation, so they talk once and you split the answers into fields),
and make clear there is no pressure: they can answer as much or as little as
they like, skip anything, and come back to the rest whenever they feel like it.
Preface the list with one short low-key line saying exactly that (e.g. "No
pressure here, ramble as much or as little as you want; you can always come
back to this later."), then ask verbatim as a single numbered list:

1. Who are you, and what do you want to be known for? (→ description)
2. What subjects could you talk about for an hour, work and non-work? (→
   `search_topics`; this is the load-bearing answer, it is the only thing that
   decides what gets scanned on X)
3. Your most contrarian takes — what does everyone in your field get wrong, and
   what did you used to believe that you have reversed on? (→ content_angle +
   corpus)
4. What can you explain in 5 minutes that took you years, and what mistake do you
   watch beginners make over and over? (→ content_angle + corpus)
5. Best or worst thing that happened to you recently, and a failure you learned
   the most from? (→ corpus, keeps drafts current)
6. Who do you love or hate reading online, and any lines or phrases you say a
   lot? (→ voice calibration)
7. Anything off-limits (topics, companies, people), and how spicy can we get —
   safe, opinionated, or provocative? (→ content_guardrails + voice.never)

From the dictation, synthesize the fields: `search_topics` primarily from answer
2 (fold in recurring scan themes only as reinforcement), the rest from the other
answers. Keep the RAW transcript VERBATIM as the persona corpus (do not
paraphrase; the actual numbers, opinions, and phrasing are what make drafts sound
like them). Pass it as `content_corpus` when you call `engagement_mode` (or
`project_config`); it is stored in the `persona_corpus.txt` sidecar, not
config.json. If the user answers only some questions, take what they gave,
continue setup without nagging for the rest, and mention once that they can
answer the remaining questions any time later to make drafts sound more like
them. If the user declines or gives nothing usable, fall back to scan-derived
topics.

Only after the dictation is captured do you set the engagement mode / save the
persona, which seeds the topics. Do not seed topics from the scan before the
dictation.

### 3. Discover and research the product

Find the product URL in this order:

1. an existing project/config;
2. the user's setup request and conversation context;
3. the connected X profile URL/bio/recent posts;
4. a clearly associated public product discovered with web research.

When one product is clearly supported by the evidence, use it without asking.
If several are plausible and no primary product is evident, choose the one most
prominent in the current context/profile. Ask one bundled blocking question
only if no defensible product can be identified.

Visit the product site with your own browser/fetch tools and read at least five
pages when available: homepage, pricing, features/product, about, docs,
changelog/blog, FAQ, and customer/case-study pages. From what you actually read,
derive:

- `name`: short lowercase machine slug;
- `website`;
- `description`;
- `differentiator`;
- `icp`;
- `get_started_link`;
- `content_guardrails`;
- `voice` and `search_topics`, grounded in the profile scan.

Do not invent features, metrics, customers, or guarantees. If the site is thin,
save a conservative factual description rather than stopping for optional
details.

### 4. Save and seed

Call `project_config` once with the complete inferred project whenever possible. It
merges fields, reports missing required fields, seeds `search_topics` into
`project_search_topics`, and expands them into search queries.

If required fields remain, first attempt to derive them from the sources already
collected. Ask the user only if a genuinely unknowable required field blocks
readiness. Optional/recommended fields never justify stopping setup.

### 5. Schedule the autopilot + verify end to end

Schedule the draft autopilot: call `queue_setup`, then for EACH returned task
call the host tool `create_scheduled_task` (taskId, cronExpression, prompt
verbatim; "already exists" is fine). The autopilot then drafts on its own — the
launchd kicker fires a draft-only cycle and the queue worker drafts the replies;
nothing posts. Then wait: poll the `dashboard` tool until the pending-draft
count rises; a returned review batch is the strongest success signal.

Do not hand-run a cycle to make this happen faster, and do not offer the user to
trigger one. Scheduling the autopilot is the only sanctioned way to produce the
verification draft; the kicker drives it on its own minute-by-minute schedule.

If no card appears, diagnose the fixable reason, fix it, and let the autopilot
retry on its next scheduled cycle:

- missing topics: derive/add topics through `project_config`, then retry;
- runtime/browser-harness/Chrome issue: run/repair the owned runtime, then retry;
- stale X session: reconnect X, then retry;
- transient backend/network issue: retry once;
- Claude CLI login/usage limit or an interactive X login: report the exact
  blocker and the single user action required.

Do not enable autopilot automatically. Offer it only after setup is verified,
or enable it when the user's original request explicitly included hands-free
posting.

Once verification passes (or you reach a precise blocker), call the `dashboard`
tool so the user sees the finished setup rendered visually, then give the
completion summary below.

## CLI fallback

Use only when MCP tools are unavailable. Execute the flow yourself rather than
turning it into instructions for the user.

1. Install/repair: `npx -y social-autoposter@latest init`
2. Inspect `~/social-autoposter/config.json` and preserve existing projects.
3. Discover the product and voice using the same evidence order above. For a
   personal-brand persona, run the dictation interview (the 7 questions above)
   and take `search_topics` + the raw corpus from it, scan themes as backup.
4. Write a complete project with name, website, description, ICP, voice, and
   `search_topics`.
5. Seed topics:
   `python3 ~/social-autoposter/scripts/seed_search_topics.py --project <name>`
6. Connect X:
   `python3 ~/social-autoposter/scripts/setup_twitter_auth.py connect`
   The user may need to approve a macOS keychain prompt or sign in once in the
   managed browser; continue automatically afterward.
7. Do not load launchd/autopilot jobs unless explicitly requested, and never
   hand-run `run-twitter-cycle.sh` or any cycle script to "verify." A manual
   kick produces an empty-plan artifact and blocks the autopilot. The cycle runs
   only when the launchd autopilot is scheduled and the kicker fires it. In CLI
   fallback, setup is configured once the project is saved, topics are seeded,
   and X is connected; the first draft appears after the autopilot is scheduled.

## Completion summary

Render the `dashboard` tool once setup is verified, then report outcomes (not a
recap of every prompt):

```text
Social Autoposter Setup Complete

Runtime:       ready
Project:       NAME — ready
Topics seeded: N
X/Twitter:     @HANDLE
Verification:  autopilot produced a draft (not posted)
Autopilot:     off
Stats:         https://s4l.ai/stats/HANDLE
```

If setup is blocked, do not call it complete. State the exact completed
milestones, the blocker, and the one user action needed to resume.
