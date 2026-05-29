# Spec: let `mark_posted` rewrite candidate project/topic on cross-route

**Status:** proposed (server-side, s4l.ai app repo — NOT in social-autoposter)
**Date:** 2026-05-29
**Owner:** whoever maintains the s4l.ai API (`app.s4l.ai`, Cloud Run `sa-dashboard`, project `s4l-app-prod`)

## Problem

The Twitter cycle's Phase 2b prep step re-routes a candidate to a better-fitting
project than the Phase 1 query that surfaced it (the PROJECT ROUTING rule in
`skill/run-twitter-cycle.sh`). Example seen 2026-05-29: a broad invented **Podlog**
query (`NotebookLM for documentation OR codebase`) surfaced Claude Code threads
that prep correctly routed to **fazm**.

When that happens:
- `posts.project_name` follows the new project (fazm) — correct.
- `twitter_candidates.matched_project` stays the **origin** (Podlog) — stale.
- `twitter_candidates.search_topic` keeps the origin topic — wrong for the new project.

Root cause: `scripts/twitter_post_plan.py::update_candidate_posted` PATCHes
`/api/v1/twitter-candidates/by-id` with `action=mark_posted`, but the route
**ignores** any `matched_project` / `search_topic` in the body (verified live
2026-05-29: PATCHing those fields left the DB row unchanged).

## Already mitigated (client side, shipped)

The two loop-feeding consumers no longer trust the stale candidate row; they key
conversions on the post's actual project:
- `scripts/qualified_query_bank.py::_fetch_rows` — `AND (p.project_name IS NULL OR lower(p.project_name)=lower(a.project_name))`
- `scripts/top_search_topics.py::_query_twitter` — every posted-conversion aggregate guarded by `_posted` (post project must match candidate matched_project)
- `skill/run-twitter-cycle.sh` prep prompt — emits `search_topic=""` when re-routing

Historical rows were backfilled once via `scripts/backfill_crossroute_attribution.py`
(84 candidates re-pointed, 23 posts' topics cleared).

So this route change is **not** required for loop correctness. It is required so
that dashboards / any consumer reading `twitter_candidates.matched_project`
directly stop mis-filing re-routed posts, and so future rows stay correct without
a periodic backfill.

## Requested change

In the `PATCH /api/v1/twitter-candidates/by-id` handler, `action=mark_posted`
branch: when the body includes `matched_project` and/or `search_topic`, write
them onto the row in the same UPDATE that sets `status='posted'`, `post_id`,
`posted_at`, `batch_id`.

- `matched_project`: set to the provided value (the routed project).
- `search_topic`: set to the provided value; treat empty string `""` as `NULL`
  (the re-route case sends `""` meaning "no topic for this project").
- Both optional and backward-compatible: when absent, behave exactly as today.
- Keep install-scoping / ownership checks identical to the existing branch.

## Client change to pair with it (in this repo, `twitter_post_plan.py`)

Once the route honors the fields, extend `update_candidate_posted(cid, post_id)`
(or its caller) to pass them from the plan entry:

```python
body = {"id": int(cid), "action": "mark_posted", "post_id": int(post_id)}
if batch_id:
    body["batch_id"] = batch_id
# cross-route writeback (pairs with the by-id route change)
if matched_project:
    body["matched_project"] = matched_project
body["search_topic"] = search_topic or ""   # "" -> NULL server-side
api_patch("/api/v1/twitter-candidates/by-id", body)
```

`matched_project` and `search_topic` are already in scope in `post_one`
(`project = c["matched_project"]`, `search_topic = c.get("search_topic")`).
`twitter_post_plan.py` is locked (`chflags uchg`); unlock → edit → relock.

## Verification

After deploy, post one cross-routed candidate and confirm in the DB:
`twitter_candidates.matched_project == posts.project_name` and
`twitter_candidates.search_topic IS NULL` for that row. Then re-run
`scripts/backfill_crossroute_attribution.py` (dry-run) — it should report 0 rows.
