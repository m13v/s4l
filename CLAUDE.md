# Social Autoposter

## Source of truth for active projects: config.json

**Before ANY cross-site work (marketing a new product on multiple sites, adding a CTA, running an audit, generating content), open `~/social-autoposter/config.json` first.** It is the authoritative list of every website we run. Do not `ls ~/` looking for site repos, do not guess domains, do not hardcode a "list of our sites" anywhere.

Each entry under `projects[]` exposes (at minimum):
- `name` (e.g. `fazm`, `mediar`, `assrt`)
- `website` production domain
- `local_repo` path to the product repo (e.g. `~/fazm`)
- `landing_pages.repo` path to the website repo (e.g. `~/fazm-website`) <- use this for marketing pages, blog posts, CTAs
- `landing_pages.github_repo`, `landing_pages.base_url`, `landing_pages.gsc_property`
- `posthog.project_id`, `booking_link`, `get_started_link`, `qualification`

Rules:
- New website? Add it to `config.json` first; SEO pipeline, analytics checker, dashboard, and cross-site marketing scripts pick it up automatically.
- Never hardcode project names, repo paths, or domains outside `config.json`.
- Any script that iterates over "all our websites" MUST read `projects[]`.

## Shared UI library: @m13v/seo-components (~/seo-components)

**`~/seo-components` is where cross-site UI lives.** Published as `@m13v/seo-components`, consumed by every website in `config.json`. Before building a new component on one site (CTA block, newsletter signup, comparison table, FAQ, proof band), check if it already exists in the library, and if not, **add it to the library instead of building it site-local**. One site-local copy today becomes four divergent copies next quarter.

Already shipped (partial): `InlineCta`, `StickyBottomCta`, `BookCallCTA`, `GetStartedCTA`, `NewsletterSignup`, `FullSiteAnalytics`, `ComparisonTable`, `FaqSection`, `RelatedPostsGrid`, `ProofBand`, `GlowCard`, `ShimmerButton`, `BeforeAfter`, `AnimatedDemo`, `BentoGrid`, `Breadcrumbs`, `ArticleMeta`, `MetricsRow`, `TypingAnimation`.

Consumer sites import via the `@seo/components` alias. When adding a new component: build in `~/seo-components/src/components/`, bump version, then update each consumer (the `bump:consumers` script automates this).

## Dashboard colors: black and white only (per user instruction 2026-05-27)

**The dashboard at `bin/server.js` (and any HTML/CSS surface it renders) MUST use only black, white, and shades of gray.** No chromatic colors. No green for "good", no red for "bad", no purple/blue/amber/yellow accents on pills, badges, charts, or text.

Use `var(--text)` for foreground, `var(--muted)` for secondary, `var(--bg)` and `var(--card)` for backgrounds, and `var(--border)` for separators. The existing `pill(label, n)` helper at `bin/server.js` already enforces this shape (`color:var(--muted)` for the label, `color:var(--text)` for the number) and accepts a `_color` arg that is **deliberately ignored**; do not "fix" it to apply the color.

Forbidden in any new code on this dashboard:
- Hardcoded hex colors: `#22c55e` (green), `#ef4444` (red), `#a78bfa` (purple), `#eab308` (amber), `#3b82f6` (blue), etc.
- Tailwind palette classes: `text-green-*`, `bg-red-*`, `border-purple-*`, etc.
- Color-coded "good/bad" pills, status badges, or chart series.

If you need to convey severity, use weight (`font-weight:600`), brackets, parens, or a tooltip line, never color. Example: `age 77.1h (cap 1h, leak)` not `age <span style="color:red">77.1h</span>`.

Existing hardcoded chromatic colors elsewhere in `bin/server.js` are tech debt; do NOT proactively refactor them, but do NOT add new ones, and when you touch a line that has one, drop the color while you are there.

To audit when asked to "remove all colors": grep `bin/server.js` for `#[0-9a-fA-F]{3,8}` and `color:\s*(?!var\(|inherit|transparent|currentColor)` to find remaining chromatic usages.

## No retention pruning, ever (per user instruction 2026-05-08)

**Never delete `*_candidates` rows by age.** The user explicitly requires that every candidate row (`twitter_candidates`, `linkedin_candidates`, `reddit_candidates`, etc.) be kept forever, regardless of `status`. The full history (chosen, skipped, expired, posted) powers analytics on skip reasons, project routing, engagement curves, and growth dynamics; pruning destroys that signal.

Forbidden patterns anywhere in this repo (Python, shell, SQL, schedulers):

```sql
-- DO NOT REINTRODUCE
DELETE FROM <table>_candidates
 WHERE status IN ('posted', 'expired', 'skipped')
   AND discovered_at < NOW() - INTERVAL 'N days';
```

This was previously present in `scripts/score_twitter_candidates.py` (7-day prune, two call sites) and `scripts/score_linkedin_candidates.py` (`PRUNE_TERMINAL_AFTER_DAYS = 7` via `expire_and_prune()`); both were removed on 2026-05-08. Do not add a `PRUNE_TERMINAL_AFTER_DAYS` / `RETENTION_DAYS` constant or a "delete old" cleanup job back. No 30-day, 90-day, or any other retention window is acceptable. If the table grows enough to hurt query performance, add an index or a partitioned archive table; never delete rows.

What IS allowed: status flips (e.g. `UPDATE ... SET status='expired' WHERE status='pending' AND discovered_at < NOW() - INTERVAL 'N hours'`). Those are freshness gates that prevent stale candidates from burning judgment tokens; they do not lose data.

If a future agent (including the auto-commit agent) reintroduces a `DELETE ... FROM <table>_candidates` by-age, revert immediately and surface to the user.

## After any DB migration: realign sequences

After any `pg_dump`/`pg_restore` cutover (e.g. Neon -> Cloud SQL on 2026-05-21), run `python3 scripts/realign_sequences.py` once. Otherwise serial sequences lag behind restored row ids and every INSERT 500s with `duplicate key value violates unique constraint "*_pkey"` until natural collision-walk catches up.

## Testing /api/v1/* routes from this machine

Base URL is `https://s4l.ai` (NOT `app.s4l.ai`); auth header is `X-Installation: $(/usr/bin/python3 ~/social-autoposter/scripts/identity.py header)`. A `Bearer` token (real or fake) returns 401 `missing_token`, not a real test.

## Analytics wiring check

`scripts/check_analytics_wiring.py` audits every project in `config.json` for correct PostHog + `@m13v/seo-components` wiring. Catches silent-failure bugs where `window.posthog` is never set and helpers (NewsletterSignup, trackScheduleClick) no-op.

- Run on demand: `python3 scripts/check_analytics_wiring.py`
- Exits 1 on any BROKEN project; safe for pre-commit or CI.
- Preferred fix: mount `<FullSiteAnalytics>` from `@m13v/seo-components` (handles init + `window.posthog` + `<SeoAnalyticsProvider>`).

## Dashboard users + weekly reports: dashboard_users table (2026-05-14)

The `dashboard_users` Postgres table is the single source of truth for BOTH dashboard
access (Firebase custom claims) and weekly-report routing. Columns: `email`,
`firebase_uid`, `admin`, `projects[]` (config.json-cased names), `report_enabled`,
`name`. Do NOT put report recipients in config.json.

Onboard a new client (one email, N projects):
1. Add a row to `dashboard_users` (see `scripts/seed_dashboard_users.py` for the shape).
2. `node scripts/dashboard_provision.js create <email>` — creates the Firebase user,
   pushes the row's `admin`/`projects` into custom claims, writes `firebase_uid` back.
3. `python3 scripts/send_dashboard_invite.py <email>` — sends the app.s4l.ai magic-link
   invite (reads name/projects from the table).

Other `dashboard_provision.js` subcommands: `list` (DB<->Firebase diff), `sync` (reconcile
claims + UIDs for all rows), `magic <email>` (print a one-shot sign-in URL, does not send).

Weekly reports fan out from this table: `scripts/daily_stats_email.py` (social, legacy
filename) and `seo/daily_report.py` (SEO). Each recipient gets a project-scoped email;
admins (`projects` empty) get the unscoped master view. Quiet-week rule: a recipient with
zero posts (social) / zero pages (SEO) in the 7d window is skipped. Both support
`--sample`, `--dry-run`, `--only <email>`. Schedule: launchd `com.m13v.social-weekly-report`
+ `com.m13v.seo-weekly-report`, Monday 09:00.

## URL wrapping: short_links_live + canonical UTM scheme (2026-05-14, updated 2026-05-22)

**Never post bare URLs.** Every URL in any post text goes through `dm_short_links.wrap_text_for_post`, which always yields a `/r/<code>` short link (form 1) under normal operation; UTM-only (form 2) is now reserved for runtime failures of the mint API / pool only.

1. `https://<host>/r/<code>` (short link). `<host>` is resolved per-project in this order:
   - **Explicit** `short_links_host` from `config.json` (e.g. `"https://s4l.ai"`).
   - **Auto fallback** `https://s4l.ai` when `short_links_live: false` and no explicit host is set. This is the social-autoposter-owned resolver (`@m13v/seo-components` -> `app.s4l.ai/api/short-links/<code>`), used whenever the customer has not deployed `/r/[code]` on their own domain yet.
   - **Customer\'s own domain** (`project.website`) when `short_links_live` is unset/true.
2. `https://<host>?utm_source=s4l&utm_medium=post&utm_campaign=<slug>&utm_term=<platform>&utm_content=post_<session>` (UTM URL). Now ONLY emitted on runtime failures of the mint pipeline (`pool_exhausted`, `api_unreachable`, `code_collision_after_8_tries`, `mint_api_error`). Never on policy.

**The 2026-05-22 change** (commit landing the same day): made `https://s4l.ai/r/<code>` the implicit default whenever `short_links_live: false`. Previously, `live=false` without an explicit `short_links_host` silently dropped to UTM-only and lost first-party `post_link_clicks` logging; that hole closed after the NightOwl regression on 2026-05-19. Onboarding a new customer where the resolver is not shipped yet now requires ONLY `short_links_live: false` in `config.json` (or no flag at all if you want the customer's own domain treated as live). The constants live at `scripts/dm_short_links.py::DEFAULT_FALLBACK_HOST` and `bin/server.js::SHORT_LINK_FALLBACK_HOST`; keep them in sync. Once a customer ships their own `@m13v/seo-components` `/r/[code]` resolver (or the static CSV from `mint_external_pool.py --export-csv`), flip `short_links_live` back to `true` (or remove the key) and remove any explicit `short_links_host` to route through their own domain.

The UTM scheme is **canonical and global**, not per-project. All four URL builders (`dm_short_links._build_target_url`, `_build_target_url_for_post`, `mint_external_pool._build_target`, `mint_kent_pool._build_target`) emit:

- `utm_source = "s4l"` (the agency). Customer-side analytics see "this traffic came from S4L" consistently.
- `utm_medium = "post"` (post rail) or `"dm"` (DM rail). Don't change; `bin/server.js` and `project_stats_json.py` parse `utm_content LIKE 'dm_%'` for booking attribution.
- `utm_campaign = <project_slug>` (runner, agora, podlog, nightowl, fazm, etc.).
- `utm_term = <platform>` (reddit, twitter, linkedin, github_issues). Platform was historically `utm_source`; moved here when `utm_source` became `s4l`.
- `utm_content = post_<minted_session>` (post rail), `dm_<dm_id>` (DM rail), or `<code>` (pool rail). Shape consumed downstream by `backfill_real_clicks.py`, `bin/server.js` regex `/^dm_(\d+)$/`, `project_stats_json.py`. Do not change.

Don't add per-project UTM templates to config.json. The scheme is fixed.

Caller exception branches must call `utm_only_text(text=..., platform=..., project_name=...)` from `dm_short_links` rather than posting unwrapped, so even if `wrap_text_for_post` itself raises, no bare URL escapes. Pattern lives in `post_reddit.py`, `engage_reddit.py`, `post_github.py`, `twitter_post_plan.py` (all four hardened 2026-05-14).

## Releasing: `bash scripts/release-mcpb.sh` only

ONE command releases npm + the GitHub `.mcpb` in lockstep from a `package.json` bump. Never the old manual `npm publish` + `gh release create` flow.
- **Default = staging:** `bash scripts/release-mcpb.sh --staging` (an `-rc.N` pre-release only staging boxes pull; npm `latest` and `releases/latest` stay put). Go stable only when the user explicitly asks.
- **Stable = clean digits (user rule):** commit + push, then `bash scripts/release-mcpb.sh --version X.Y.Z` (the rc's base version, or next patch if taken). Never promote an `-rc.N` tag in place; the script blocks it and prints the recipe.
- **Never re-release an existing version** (`--no-bump`): npm is immutable, so the lanes diverge. A fix always gets a fresh patch bump.
- **Before releasing:** if npm `latest` or the GitHub latest tag shows a version you didn't publish, a parallel agent released; bump again.

## SKILL.md - Single File, No Copies

`SKILL.md` lives at the repo root. There is no `skill/SKILL.md`.

- `~/.claude/skills/social-autoposter` -> `~/social-autoposter/` (repo root)
- Claude loads `~/social-autoposter/SKILL.md` directly
- Shell scripts in `skill/` reference `$REPO_DIR/SKILL.md` (the root)
- `bin/cli.js` deletes any stale `skill/SKILL.md` during init/update
- `setup/SKILL.md` is a separate file (setup wizard, not the main skill)

### Rules

- **NEVER create `skill/SKILL.md`** - not as a file, not as a symlink
- **NEVER edit, modify, or write to `SKILL.md`** - it is locked with `chflags uchg` (macOS immutable flag). Do not attempt to unlock it. Do not run `chflags nouchg`. Do not run `chmod`. Just read it and follow the workflow exactly as written.
- **NEVER edit, modify, or write to `setup/SKILL.md`** - it is also locked with `chflags uchg`. Same rules as above: do not unlock, do not modify, do not "fix" it.
- **Do NOT add rate limits to ANY file** - do not add "Max N posts per 24 hours" or any posting rate limit language to SKILL.md, setup/SKILL.md, shell scripts, or any other file. There is NO posting rate limit. If a file does not contain a rate limit, that is intentional.
- **Do NOT "improve" or "fix" the workflow** - do not renumber steps, do not add safety checks, do not restructure the file.

## Locked pipeline files: NEVER unlock without explicit user instruction

Many pipeline scripts are locked with `chflags uchg` to prevent agents from "simplifying" or reverting data-driven improvements. An agent did exactly this on 2026-04-28: it ran `chflags nouchg`, stripped critical guardrails (two-lane grounding rule, Moltbook AUP context clearing), then relocked the files.

**NEVER run `chflags nouchg` on any file in this repo without the user explicitly saying "unlock X and change Y".** The lock is not a suggestion. It is a hard stop. If you think a locked file needs to change, stop and tell the user instead.

Locked files (do NOT unlock or edit without explicit user instruction):
- `scripts/engagement_styles.py` (grounding rule, tier weights, platform weights)
- `scripts/engage_reddit.py` (Moltbook context clearing, grounding rule in prompt)
- `skill/run-reddit-search.sh`, `skill/run-twitter-cycle.sh`, `skill/run-github.sh`, `skill/run-linkedin.sh`
- `scripts/top_performers.py`, `scripts/post_reddit.py`, `scripts/post_github.py`, `scripts/github_tools.py`
- `scripts/qualified_query_bank.py`, `scripts/top_search_topics.py` (Twitter Phase 1 query bank + per-topic ranking; both carry the 2026-05-29 cross-route guard: a query/topic is credited to a project ONLY when the resulting post's `posts.project_name` matched the issuing project. Removing the `p.project_name = a.project_name` / `_posted` guards re-opens the bug where a broad invented query for project A that the prep step re-routes to project B gets "qualified" for A on B's conversion.)
- `scripts/linkedin_api.py`, `scripts/discover_linkedin_candidates.py`, `scripts/score_linkedin_candidates.py`, `scripts/linkedin_browser.py`, `scripts/linkedin_url.py`, `scripts/log_linkedin_search_attempts.py`, `scripts/top_linkedin_queries.py`, `scripts/top_dud_linkedin_queries.py`
- `seo/generate_page.py`, `seo/escalate.py`, `seo/resume_escalations.py`
- `scripts/ingest_human_seo_replies.py`, `scripts/scan_dm_candidates.py`
- `skill/dm-outreach-reddit.sh`, `skill/dm-outreach-twitter.sh`, `skill/dm-outreach-linkedin.sh`
- `scripts/twitter_browser.py`, `scripts/scan_twitter_thread_followups.py`, `skill/scan-twitter-followups.sh`
- `scripts/scan_twitter_mentions_browser.py` (browser-based mention discovery, replaces deprecated API path)
- `scripts/_li_discover_pending.py`, `scripts/li_discover_insert.py` (LinkedIn DM discovery, hardcoded EXCLUDED_AUTHORS guardrail)
- `scripts/reddit_chat_sync.py` (Reddit Chat IndexedDB reader, brittle external coupling)
- `scripts/reddit_tools.py` (Reddit search/fetch CLI; carries per-project sub-denylist merge from project_search_excludes — see Reddit project-scoped excludes section below)
- `scripts/stats.py` (central stats engine; flips `posts.status='deleted'`; GraphQL isMinimized pre-pass for github)
- `scripts/strike_alert.py`, `skill/strike-alert.sh` (strike escalation rail; emails i@m13v.com on every newly-detected `status='deleted'` or `'removed'` post)
- `scripts/watchdog_hung_runs.py`, `skill/stats.sh`
- `skill/stats-linkedin.sh`, `scripts/scrape_linkedin_comment_stats.py`, `scripts/update_linkedin_stats_from_feed.py` (unified LinkedIn stats pipeline 2026-05-11: one scrape of `/in/me/recent-activity/comments/` via CDP-attach to the linkedin-agent MCP Chrome, single writer into `posts` table. Replaces the deprecated `scrape_linkedin_stats_browser.py` and the retired `stats-linkedin-comments.sh` + `update_linkedin_comment_stats_from_feed.py` pair. Do NOT re-introduce a per-permalink scrape, a second Chrome launch, or any Voyager-API path.)


## Reddit project-scoped excludes (Option C, 2026-05-11)

Self-improving per-project subreddit denylist for Reddit, mirroring the Twitter cycle's keyword-exclude pattern. Same DB table (`project_search_excludes`), same activation gate (≥2 distinct batches), same 60-day decay. The wiring lives in four files:

- `scripts/project_excludes.py` — adds typed-term forms (`subreddit:<slug>`, `keyword:<word>`) alongside the legacy twitter bare-keyword form. Platform gate (`ALLOWED_KINDS`) prevents cross-contamination: twitter can only write `bare`, reddit can only write `subreddit:` / `keyword:`. New helper `active_excludes_by_kind('reddit', project)` returns `{subreddit:[…], keyword:[…], bare:[…]}`. New CLI subcommand `active-split` exposes the same.
- `scripts/reddit_tools.py` — `_load_comment_blocked_subs(project_name=...)` reads (1) `config.json: subreddit_bans.comment_blocked` with optional `.project` field for per-project scoping, and (2) active `subreddit:` rows from `project_search_excludes`. Server-side enforcement at parse time in `cmd_search` and `cmd_fetch` via the `S4L_REDDIT_PROJECT` env var. New `project_block_extra` counter on the `[reddit_search]` stderr marker shows how many of the blocked subs came from the per-project layer.
- `scripts/post_reddit.py` — draft prompt now accepts `action="reject"` JSON lines with a `proposed_excludes: ["subreddit:<slug>"]` array; `parse_reject_decisions()` + `_propose_excludes_from_rejects()` forward each into `project_excludes.propose('reddit', project, term, batch_id, ...)`. Discover phase logs `[project_excludes] platform=reddit project=… active_subs=N active_keywords=N subs=[…]` and calls `mark_used` to keep decay honest.
- `skill/run-reddit-search.sh` — documentation-only update; enforcement is fully Python.

Why subreddit-form, not keyword-form, as the primary lever: Reddit false positives are 90% structural-subreddit mismatches (`r/bestofredditorupdates` family drama matching on "alternative", `r/hfy` sci-fi matching on "spaced", `r/superstonk` meme stocks matching on stray words), not keyword collisions. Twitter is the inverse (brand-name collisions like `cricket`/`kohli` for Vipassana). Both forms are supported on both platforms via the schema but only the platform-natural form is wired into the prompts today.

Forbidden patterns (don't reintroduce):
- Bare-keyword `term` on reddit (e.g. writing `"anki"` as a reddit exclude). The platform gate rejects it (`rejected_invalid`); the post_reddit prompt instructs Claude to always emit typed form. A bare reddit term would have no callsite to read it back.
- Auto-proposing a top-performing sub for any project (e.g. `subreddit:medicalschool` for studyly). The prompt's "WRONG proposals" examples cover this, and `_load_reserved_terms_for_project` keeps the keyword side of the reserved list intact, but the **subreddit form bypasses keyword-reserved checks by design** — the sub name shares tokens with search topics legitimately. Trust the 2-batch activation gate to filter false rejects.

## LinkedIn stats pipeline architecture (2026-05-11)

LinkedIn stats follow the Twitter logic shape: ALL engagement (top-level posts AND engagement-comments) lives in the `posts` table, identified uniquely by `our_url`. There is no LinkedIn-specific replies table anymore. The 2026-05-11 migration moved the 257 legacy `replies` rows into `posts` and marked the originals `status='migrated'`; the dashboard feed query already filters `WHERE r.status='replied'` so migrated rows naturally drop out of the replies surface and re-appear under the `posts` surface as `posted_comment` events.

URL convention for LinkedIn `posts.our_url`:
- Top-level post: `https://www.linkedin.com/feed/update/urn:li:{share|activity|ugcPost}:<post_id>/`
- Engagement-comment on someone else's post: `.../urn:li:{ns}:<parent_id>/?commentUrn=urn%3Ali%3Acomment%3A%28<ns>%3A<parent_id>%2C<our_comment_id>%29`

The `?commentUrn=...` suffix is what makes the post-stats updater able to read OUR comment's stats from the activity feed instead of leaking the parent post's reactions / comments. `linkedin_api.py:comment_on_post` was patched on 2026-05-11 to embed it (mirroring what `reply_to_comment` already did). Posts written before that patch (~1,022 rows where `our_url == thread_url`) cannot be backfilled without per-permalink scraping (banned), so their stats stay frozen at the parent-post leak until LinkedIn naturally re-surfaces them on the activity tab.

### Migration day cleanup (2026-05-11)

- Migrated 257 LinkedIn rows from `replies` to `posts` (225 inserts + 32 dedup-skipped); originals flagged `status='migrated'`.
- Deleted ONE true duplicate: `posts.id=5081` (placeholder "comment on MCP post") was the same logical comment as `posts.id=24872` (which has the real content). Re-pointed `replies.id=5288.post_id` from 5081 to 24872 before deletion so the FK stayed valid.
- 4 placeholder-content rows (`5512` "comment on DR growth post", `5513` "comment on Overseer post", `5514` "comment on AI building post", `5515` "comment on Sora shutdown post") were NOT deleted; they are the only record of those engagement-comments and their dashboard-paired 24xxx rows differ by `replyUrn` (so they are follow-up sub-replies, not duplicates).
- 6 "duplicate-looking" pairs (5459/24895, 5484/24894, 6808/24956, 6809/24957, 6810/24958, 6811/24959) are actually distinct comments in the same thread: the 5xxx row holds the original comment and the 24xxx row carries a `&replyUrn=` for a follow-up sub-reply. NOT duplicates; both kept.
- 2 same-URL-different-content pairs (24952/24953, 24960/24965) are a remaining data-quality anomaly worth a future look but not blocking; both rows have real content.

## Known unresolved issue: hung runs from BSD grep on /tmp FIFOs

A `run-*.sh` can occasionally hang indefinitely because the model invokes `grep -r` across `/tmp` (or `~/`) during a session. macOS BSD `grep` opens named pipes it encounters (e.g. stale `ad_mailbox_*` FIFOs left by Apple daemons) and blocks forever in `read()`, which freezes the shell, the `claude -p` parent, and prevents launchd from re-firing the job. No automatic recovery is in place: wrapping `claude` in `timeout` was rejected, and neither FIFO sweeps nor switching to GNU grep fully eliminates the class of problem. For now, if a posting run stops making progress, kill the stuck `run-*.sh` tree manually.


## LinkedIn: flagged patterns (DO NOT REINTRODUCE)

2026-04-17 the account was restricted after a patch added Voyager-API scraping and per-permalink scroll-and-expand loops. Volume (2-3 posts/day) was NOT the cause, behavioral fingerprinting of scripted browser activity was. Banned in this repo:

- `/voyager/api/*` calls of any kind (Python, `fetch()`, `page.evaluate`). That is the internal web-client backend, not the public API.
- Loops that open each post permalink to scrape reactions/comments, or combine `scrollBy` with clicks on "Show more comments" / "Load earlier replies".
- Python Playwright/CDP helpers that drive *posting, replying, scrolling, multi-page navigation, or programmatic `login()` flows*. The 17 Apr restriction was caused by behavioral fingerprinting of those patterns, not by Python existing in the call stack.

Allowed: `scripts/linkedin_api.py` (OAuth `api.linkedin.com/v2/socialActions/*`, documented) for posting, and `mcp__linkedin-agent__*` (real headed Chrome) for any browser work, driven by Claude inside the shell pipelines. Session checks are passive: if login/checkpoint appears, print `SESSION_INVALID` and stop.

**Carve-out (2026-04-29): read-only sidebar pre-checks via Python Playwright are allowed under strict conditions.** `scripts/linkedin_browser.py` may attach to the linkedin-agent's persistent profile (`~/.claude/browser-profiles/linkedin`) in **headed** mode for cost-saving "is anything unread?" gates ahead of the Claude-driven engage-dm-replies pipeline. Allowed inside this helper:

- ONE `page.goto('/messaging/')` per invocation.
- ONE `page.evaluate()` to read sidebar conversation rows + unread badges from the DOM.
- Headed Chromium only (`headless=False`). Headless fingerprints differently.
- Inherit the same persistent profile so cookies/session/fingerprint match the MCP agent.

Banned inside this helper, no exceptions:

- `/voyager/api/*` (still). The pre-check reads only DOM that the user themselves would see.
- Multi-page loops, permalink scrapes, scroll-and-expand on threads, "Show more" clicks.
- Any clicks, types, or form interactions. Read-only.
- Programmatic login. If `_is_login_or_checkpoint(url)` matches, return `session_invalid` and stop.

New LinkedIn capability that *acts* (posts, replies, edits, scrolls multiple pages)? Extend `linkedin_api.py` or add a Claude-driven `mcp__linkedin-agent__` step. Do not write a new Python CDP *action* helper.

## Engagement Styles System (DO NOT REMOVE)

All posting and engagement scripts use `scripts/engagement_styles.py` to generate a `STYLES_BLOCK` variable injected into prompts. This is an A/B testing system that tracks which comment style gets the best engagement.

- **NEVER remove `STYLES_BLOCK`** from any `skill/run-*.sh` or `skill/engage*.sh` script
- **NEVER remove `engagement_style`** from DB logging (reply_db.py calls, INSERT statements)
- **NEVER remove or simplify style definitions** in `scripts/engagement_styles.py`
- **NEVER inline style definitions** back into individual scripts; the shared module is the single source of truth

## Experiment arms: stamp at source, surface on review cards (2026-07-07)

Every experiment/scenario arm that affects DRAFTING is stamped ONCE, at the
source, and read everywhere else. The user has explicitly rejected fallback
layering here (no "env first, plan second" resolution anywhere).

How it works:
- `run-twitter-cycle.sh`'s plan writer calls `scripts/active_experiments.py::collect()`
  in the cycle process (where arms are assigned) and stamps an `experiments`
  {name: variant} dict onto EVERY plan candidate at plan-write time.
- That per-candidate record is the ONLY source downstream: `merge_review_queue.py`
  carries it through untouched, the review card's details-eye popover renders
  every entry generically (`s4l_card._details_lines`), and `twitter_post_plan.py`
  stamps `posts.draft_prompt_variant` from `c["experiments"]["draft_prompt"]`.
  The poster does NOT read `S4L_DRAFT_PROMPT_VARIANT` from env at post time;
  that env read caused the queue-review lane to stamp NULL (the MCP-spawned
  poster has no cycle env) while autopilot stamped the arm. Do not add it back.

Adding a new drafting experiment (the whole convention):
- Export `S4L_EXP_<NAME>=<variant>` in the process chain that runs the cycle.
  Done: it auto-stamps onto candidates, auto-renders on cards, and is readable
  at post time. Do NOT wire per-experiment code into `s4l_card.py`, the merge,
  or the poster.
- Scope limits: arms are cycle-scoped (one dict for the whole plan batch). A
  per-candidate experiment needs its own candidate field (like
  `engagement_style` has). Post-time experiments (e.g. `tail_link_variant`,
  coin-flipped in the poster after approval) cannot appear on cards and keep
  their own posts-column recording.

Forbidden (v1 design, removed 2026-07-07; do not reintroduce):
- Merge-time stamping / `--cycle-out` stdout parsing in `merge_review_queue.py`.
- `S4L_EXPERIMENT name=... variant=...` stdout markers.
- Any post-time env read or env-vs-plan fallback for arm resolution.

Note: as of 2026-07-06 the `personal_brand` persona directive in
run-twitter-cycle.sh is ARM-AWARE (`treatment_v2` adds the concede-then-reverse
skeleton ban, `control_v2` keeps the plain persona directive), so the assigned
`draft_prompt` arm DOES affect persona drafts. `active_experiments.collect()`
therefore KEEPS `draft_prompt` for `lane=personal_brand` (it used to drop it, back
when the persona directive overrode both arms wholesale), so the arm stamps onto
persona review cards and the per-arm readout covers both lanes.

## Debugging a customer install (READ THIS FIRST when a customer reports "S4L not working")

Everything below is queryable from this machine, no SSH to the customer. Canonical playbook; the project memories reference this section instead of duplicating it.

**Key architecture fact behind most confusion:** the launchd lanes (cycle producer, posting, review cards) run WITHOUT Claude; only draft generation needs the `s4l-worker` scheduled task, which fires ONLY while Claude Desktop is open and logged into the account that owns the registry entry. "It's posting but shows a warning" = worker dead, launchd alive.

**Known #1 root cause (Karol, 2026-07-06): the Desktop scheduler wedge.** Claude Desktop's warm-session platform bug (finished stream-json worker sessions never exit) makes the scheduler's overlap guard think a run is still in flight, so it silently stops firing the task forever: task stays enabled, lastRunAt freezes, Claude keeps running. Customer fix: FULLY QUIT and reopen Claude Desktop. The reaper is a red herring (check its kill count: 0). Detail: project memory `gotcha_menubar_worker_killed_scheduler_stopped_firing`.

Data lanes, in the order to check them:

1. **Registry truth: `installations.scheduled_tasks_sample`** (Postgres, keychain `s4l-database-url`). Heartbeat-shipped summary of every scheduled-tasks.json on the box (task id, enabled, cwd_tail, last_run_at, registries/worker_tasks counts), fresh while Claude Desktop is open (`scheduled_tasks_sampled_at`). Frozen `last_run_at` + enabled + Claude up = the scheduler wedge above. `worker_tasks < registries` + frozen = possible account-switch orphan.
2. **Client logs + Claude session transcripts: GCP Cloud Logging, project `s4l-app-prod`.** MUST pass `--account=i@m13v.com` (default gcloud account is often matt@mediar.ai which gets PERMISSION_DENIED). Query: `gcloud logging read 'jsonPayload.install_id="<uuid>" AND <filter>' --project=s4l-app-prod --account=i@m13v.com --freshness=2h`. Filters: `jsonPayload.context:"transcript:"` = compacted worker/s4l-dir Claude session transcripts (1.6.199+, shipped every 5 min); `jsonPayload.logType="client-pipeline"` = subprocess stdout/stderr (MCP-spawned only, launchd cycle internals do NOT stream); `jsonPayload.logType="onboarding-milestone"` = setup ledger; `jsonPayload.context="tool-call"` = per-MCP-tool invocations; `jsonPayload.context="launchd-load"` = launchctl failures. Cycle-phase visibility rides the `twitter_cycle` block inside `context="scripts/memory_snapshot.py"` messages.
3. **Sentry, org `mediar-n5`, project `social-autoposter`** (token: keychain `sentry-auth-token`). Menubar warnings ("S4L draft autopilot needs attention: missing|disabled|draft_stuck|rate_limited"), field diagnosis reports (component=diagnose_fix), post_drafts errors. Filter by tag `install_id`. Discover API `statsPeriod` accepts only 24h/14d.
4. **Postgres history**: `installation_state_snapshots` (jsonb; `runtime.pipeline_version` dates updates; reason=startup rows = MCP server restarts), `twitter_batches` by `owner_host` (batch ids are in the CUSTOMER's local time; stuck at `phase2b-prep` = worker not servicing queue jobs), `posts` by `install_id`, `review_events` by `install_id::text` (proves user was at the machine).
5. **Find the install_id**: `installations` table (hostname/git_email ILIKE), or Sentry event tags. Karol: `ba6519ca-edaf-4fee-95b9-446da86bd346`.
