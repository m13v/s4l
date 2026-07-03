# S4L rename plan (SAPS_* and "social-autoposter" retirement)

Status 2026-07-03: **Tier 1 shipped** (user-visible surfaces). Tiers 2 and 3
are designed but NOT executed; each needs an explicit go from Matthew because
they touch locked files and externally published names.

Branding rule (standing, per Matthew 2026-07-02): anything a user can SEE
carries the S4L brand. The internal "saps" prefix and the "social-autoposter"
package machinery may exist only where users never look, and only until the
tiers below retire them.

## Tier 1 — user-visible surfaces (DONE)

- Scheduled task id `s4l-worker` (canonical; `saps-worker` + the phase pair are
  legacy ids consolidated away by the menubar one-restart self-heal).
- Chat-visible prompts (menubar SETUP/UPDATE/REARM, panel schedule button) say
  "the S4L plugin" / "the S4L draft autopilot schedule".
- MCP server instructions lead with "S4L".
- Notifications say "S4L", never "autoposter".
- manifest.json display_name/author were already S4L.

What deliberately did NOT change: `npx social-autoposter@latest` command hints
(functional until Tier 3), the manifest `name` (host config identity), tool
namespace `mcp__social-autoposter__*`, launchd labels, paths.

## Tier 2 — internal SAPS_* env vars -> S4L_* (designed, needs go + unlocks)

383 references in 65 files; 7 are LOCKED (`run-twitter-cycle.sh` 45 refs,
`post_reddit.py` 12, `reddit_tools.py` 6, `twitter_browser.py` 6,
`engage_reddit.py` 3, `run-reddit-search.sh` 3, `stats.py` 2). Requires
explicit unlock instruction per repo rules.

Plan (one release, plus one cleanup release later):
1. Add a shim helper (`scripts/saps_env.py` + shell equivalent): readers check
   `S4L_<NAME>` first, fall back to `SAPS_<NAME>`; writers/exporters set BOTH.
2. Mechanical rename of all readers/writers through the shim.
3. Plist/SKILL.md generators emit both prefixes (existing customer plists keep
   baked SAPS_* values until regenerated; the shim makes them keep working).
4. After the fleet is on the shimmed release (heartbeat `app_version` tells us),
   a cleanup release drops the SAPS_* fallback.

## Tier 3 — external anchors (designed, needs go; partly irreversible)

| Anchor | Move | Risk |
|---|---|---|
| npm package `social-autoposter` | Publish as `s4l` (name availability unverified) or `@s4l/cli`; keep dual-publishing `social-autoposter` as a thin re-export until fleet migrates (npm has NO renames/redirects) | Old installs update via the old name forever unless dual-published |
| GitHub repo `m13v/social-autoposter` | Rename to `m13v/s4l` (GitHub redirects old URLs) | Update scripts/docs referencing the old URL; redirects mask breakage until a fork/second repo squats the old name |
| Repo dir `~/social-autoposter` | Keep on disk (path baked into locked prompts, customer plists, runtime.json); new installs could use `~/s4l` after Tier 2 | High churn, low visibility — LAST |
| State dir `~/.social-autoposter-mcp` | Same: migrate via symlink + one-release dual-read | Queues/outboxes/identity live here; a botched move orphans approvals |
| launchd labels `com.m13v.social-*` (71 plists) | New `ai.s4l.*` labels written by the same generators; boot code bootout old label, bootstrap new | Needs the double-driver caution on the operator Mac |
| MCP manifest `name` + `mcp__social-autoposter__*` tool namespace | Renaming breaks host configs and every allowed-tools list on customer machines | Do LAST, or never — display_name already S4L |

Recommended order if Tier 3 is approved: GitHub rename (redirects) -> npm
dual-publish -> launchd labels -> dirs -> manifest name (or keep).
