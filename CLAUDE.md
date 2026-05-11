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

## Analytics wiring check

`scripts/check_analytics_wiring.py` audits every project in `config.json` for correct PostHog + `@m13v/seo-components` wiring. Catches silent-failure bugs where `window.posthog` is never set and helpers (NewsletterSignup, trackScheduleClick) no-op.

- Run on demand: `python3 scripts/check_analytics_wiring.py`
- Exits 1 on any BROKEN project; safe for pre-commit or CI.
- Preferred fix: mount `<FullSiteAnalytics>` from `@m13v/seo-components` (handles init + `window.posthog` + `<SeoAnalyticsProvider>`).

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
- `scripts/linkedin_api.py`, `scripts/discover_linkedin_candidates.py`, `scripts/score_linkedin_candidates.py`, `scripts/linkedin_browser.py`, `scripts/linkedin_url.py`, `scripts/log_linkedin_search_attempts.py`, `scripts/top_linkedin_queries.py`, `scripts/top_dud_linkedin_queries.py`
- `seo/generate_page.py`, `seo/escalate.py`, `seo/resume_escalations.py`
- `scripts/ingest_human_seo_replies.py`, `scripts/scan_dm_candidates.py`
- `skill/dm-outreach-reddit.sh`, `skill/dm-outreach-twitter.sh`, `skill/dm-outreach-linkedin.sh`
- `scripts/twitter_browser.py`, `scripts/scan_twitter_thread_followups.py`, `skill/scan-twitter-followups.sh`
- `scripts/scan_twitter_mentions_browser.py` (browser-based mention discovery, replaces deprecated API path)
- `scripts/_li_discover_pending.py`, `scripts/li_discover_insert.py` (LinkedIn DM discovery, hardcoded EXCLUDED_AUTHORS guardrail)
- `scripts/reddit_chat_sync.py` (Reddit Chat IndexedDB reader, brittle external coupling)
- `scripts/update_stats.py` (central stats engine; flips `posts.status='deleted'`; GraphQL isMinimized pre-pass for github)
- `scripts/strike_alert.py`, `skill/strike-alert.sh` (strike escalation rail; emails i@m13v.com on every newly-detected `status='deleted'` or `'removed'` post)
- `scripts/watchdog_hung_runs.py`, `skill/stats.sh`
- `skill/stats-linkedin.sh`, `scripts/scrape_linkedin_comment_stats.py`, `scripts/update_linkedin_stats_from_feed.py` (unified LinkedIn stats pipeline 2026-05-11: one scrape of `/in/me/recent-activity/comments/` via CDP-attach to the linkedin-agent MCP Chrome, single writer into `posts` table. Replaces the deprecated `scrape_linkedin_stats_browser.py` and the retired `stats-linkedin-comments.sh` + `update_linkedin_comment_stats_from_feed.py` pair. Do NOT re-introduce a per-permalink scrape, a second Chrome launch, or any Voyager-API path.)


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
