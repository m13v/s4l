# Log Consumer Contract (do-not-break)

`bin/server.js` (the dashboard) is a **downstream consumer** of the Python pipeline's
file logs. The logs are a load-bearing API, not just diagnostics. This file is the
explicit contract: what the dashboard depends on, so observability work (OTel, trace
ids, new fields) stays **additive** and never breaks a panel.

## Golden rules

1. **Append-only.** You may ADD fields, tokens, or new marker lines. You may NEVER
   rename a log file, change a filename timestamp shape, remove a marker string, or
   reorder the `run_monitor.log` token grammar.
2. **New observability goes at the MCP/Node layer** (OTel spans -> local file
   exporter). The dashboard does not read that layer today, so it is pure addition.
3. **Python file logs stay exactly as they are.** The only Python change allowed for
   observability is stamping an extra `trace_id` field; existing regexes don't match
   on it, so it's safe.
4. When you migrate a consumer to a unified trace view, ADD a new reader behind the
   same join key. Never swap an existing reader out.

## Join key (trace id)

The pipeline already has correlation ids. Reuse them as the OTel trace id where they
exist; mint a fresh OTel id only for non-batch tool calls (`get_stats`, `config`).

- Twitter cycle batch id: `twcycle-YYYYMMDD-HHMMSS`  (server.js:1687)
- Reddit cycle batch id:  `rdcycle-YYYYMMDD-HHMMSS`  (server.js:1701, 2670)

## A. Directories / files the dashboard reads

| Const | Path | Purpose |
|-------|------|---------|
| `LOG_DIR` | `skill/logs/` | per-run cycle logs (server.js:31) |
| `RUN_MONITOR_PATH` | `skill/logs/run_monitor.log` | one-line-per-run ledger (server.js:696) |
| `SEO_LOG_ROOT` | `seo/logs/` | per-product SEO attempt logs (server.js:2825) |
| `ACTIVE_CLAUDE_DIR` | claude jsonl dir | live run detection (server.js:323) |
| (tmp) | `/tmp/<name>/pid` | running-run pid probe (server.js:274) |

`launchd-*.log` files are explicitly excluded everywhere (`f.startsWith('launchd-')`).

## B. Filename patterns (naming is part of the contract)

- `twitter-cycle-YYYY-MM-DD_HHMMSS.log`            (server.js:1535, 1725)
- `run-reddit-search-YYYY-MM-DD_HHMMSS.log`        (server.js:2243, 2345)
- generic job log: `<logPrefix>YYYY-MM-DD_HHMMSS.log` or bare `YYYY-MM-DD_HHMMSS.log` (server.js:373-380, 4866-4868)
- SEO subdir-ts:  `seo/logs/<product>/<phase>/YYYYMMDD-HHMMSS.log`       (`SEO_LOG_TS_RE`, server.js:2848)
- SEO root-slug:  `seo/logs/<product>/YYYYMMDD-HHMMSS_<slug>.log`        (`SEO_LOG_TS_SLUG_RE`, server.js:2850)
- SEO roundup:    `seo/logs/roundup/YYYY-MM-DD_HHMMSS_<product>.log`     (`SEO_ROUNDUP_LANE_RE`, server.js:2852)

## C. run_monitor.log ledger grammar (most fragile — strict token order)

`RUN_LINE_RE` (server.js:735). Pipe-delimited, leading tokens required, trailing
tokens optional but **positionally ordered**. Adding a new token = append a new
optional group at the END, never insert in the middle.

```
YYYY-MM-DDTHH:MM:SS | <job> | posted=N skipped=N failed=N
  [replies_refreshed=N] [checked=N updated=N removed=N] [unavailable=N]
  [not_found=N] [scanned=N] [changed=N] [views_refreshed=N] [salvaged=N]
  [discover=<tok>] [scan=<tok>] [invent=<tok>]
  cost=$X.XX elapsed=Ns
  [failure_reasons=<tok>] [skip_reasons=<tok>] [escape_hatch=N] [escape_hatch_details=<tok>]
```

Required core: `... | <job> | posted= skipped= failed= ... cost=$ elapsed=Ns`.

## D. Twitter cycle body markers (server.js:1561-1580, 2008-2021)

Every line is prefixed `[HH:MM:SS] `. Phase timing is derived from these exact strings:

- `=== Twitter Cycle`
- `[lock] acquired twitter-browser pid=N at HH:MM:SS waited=Ns`
- `Phase 1: drafting`  /  `Phase 1 complete`
- `Variant X: sleeping Ns before T1`
- `Phase 2a: re-polling`
- `Phase 2b-prep: Claude reading`  /  `Phase 2b-prep complete`
- `Phase 2b-gen:`
- `Re-acquiring twitter-browser lock for Phase 2b-post`
- `Phase 2b-post: posting`
- `=== Cycle complete`
- `[stale_age_skip] ` (counted, server.js:2013)
- `Selected projects: <...>` (server.js:2021)
- `Phase 0: salvaged N orphaned pending rows` (server.js:1730, 2008)

## E. Reddit cycle body markers (server.js:2350-2485)

- `[post_reddit] Claude drafted N post`
- `[post_reddit] POSTED:`
- `[post_reddit] phase=post ... posted=N failed=N`
- `[post_reddit] phase=post project=X posted=N failed=N`
- `[post_reddit] SALVAGED N candidate(s) ... project=X`
- `[post_reddit] Project: X`
- `[post_reddit] Discover found|harvested N candidate`
- `[post_reddit] Draft produced N post`
- `[reddit_search] ... raw=N returned=N`
- `[ripen] summary input=N survivors=N drops=N floor=F w_comments=F window_sec=N best_composite=... best_d_up=... best_d_co=...`
- `[ripen] no thread_urls | empty plan | WARN: 0 of N T0 fetches`
- `Ripen phase: 0 survivors; skipping post phase`
- `--- Iteration N/` (prefixed `[HH:MM:SS]`)
- `reddit_tools.py search ` / `reddit_tools.py fetch ` (+ `tool: Bash`)
- `Plan phase: Claude failed` / `Plan phase: rate-limited`
- `Discover phase: Claude failed`
- `Drafting comments for N survivor` / `Draft phase: Claude failed`
- `Salvage lane: posted=N failed=N | nothing to salvage`
- `Discover lane: posted=N failed=N | Claude failed`
- `[post_reddit] Claude FAILED: <reason>` (reason parsed: `out of extra usage`->credits, `Not logged in`->logged_out)
- `[post_reddit] CDP FAILED: <word>`
- `Cycle batch_id=rdcycle-YYYYMMDD-HHMMSS`

## F. SEO log classifier markers (server.js:2920-3079, _classifySeoLog 2955)

- inline result JSON: `"status":"<status>"`
- `"type":"result"` (JSONL result line, server.js:2920)
- `"type":"rate_limit_event"` + `"status":"rejected"` (server.js:2935)
- `"files_modified":[...]` (server.js:3079)
- `SKIP: ... no pageviews in last 24h` -> no_traffic
- `HogQL failed: HTTP 429` (+ `available in N seconds`) -> posthog_throttle
- `HogQL failed` -> posthog_error
- `already running for` -> locked
- `ERROR building brief` -> brief_error
- `hit your limit | rate limit` (server.js:3059) -> limit

## G. Incident diagnosis: pulling a client's logs (memory / reaper lane)

Sections A-F are the DASHBOARD's read contract. This section is the RUNBOOK for
diagnosing a client freeze/leak remotely (the "Karol" class: box climbs to hundreds
of `claude` procs and OOM-freezes). Two remote data sources, both keyed on `install_id`.

### 1. Cloud Logging (client log relay)

Every client tees its pipeline stream to a Cloud Run relay. Query by install_id:

```
gcloud logging read \
  'jsonPayload.install_id="<uuid>"' \
  --project=s4l-app-prod --freshness=6h --limit=500 --format=json
```

- `install_id` is the client's install uuid (host path is the tell: `/Users/<name>/`).
- The relay carries `memory_snapshot.py` heartbeats + pipeline stdout. It does NOT
  carry the reaper's stderr (separate launchd job, never tee'd), so a dead reaper is
  invisible here by design; read `reaper-status.json` (below) instead.
- Noise caveat: `identity.py` heartbeats can flood ~21k lines/hr. Filter to the
  `memory_snapshot` / `[claude-reaper]` payloads, do not scroll raw.

### 2. installation_resource_samples (Cloud SQL, per-minute curve)

The authoritative leak curve. One redacted snapshot per minute per install, created
2026-06-27. Query the LIVE Cloud SQL DB (not the retired Neon `.env.production.local`).

```
SELECT created_at, groups->'claude_cli'->>'count'                       AS claude,
       groups->'sessions_configured_remote_macos_mcp'->>'count'         AS mcp_sessions,
       swap_used_mb, consecutive_timeouts
FROM installation_resource_samples
WHERE install_id='<uuid>' AND created_at > now() - interval '2 hours'
ORDER BY created_at;
```

Reading the curve:
- **Leak signature**: `claude` and `mcp_sessions` climb +4/min in lockstep, none exiting
  (`claude ~= mcp_sessions + 7`). That is the double leak: each leaked worker drags one
  `mcp-server-macos-use` child; kill the worker, orphan the child (reaper 1.6.176+ reaps
  both).
- `remote_macos_mcp_servers` is ALWAYS 0 here (standalone servers, not the leak). Do not
  key alerts on it. The leak lives entirely in `claude_cli` + `sessions_configured_remote_macos_mcp`.
- A row showing `0 0` mid-climb = the sampler's own `ps` timed out under swap thrash
  (the death-spiral trigger, same failure that blinds the reaper). `consecutive_timeouts`
  rising confirms it.

### 3. Local telemetry shipped 1.6.178 (read on the box, or off the wire)

These closed the exact blind spots this class of incident exposed. When SSH'd into a box:

- **`claude_desktop_version`** stamped on every heartbeat + JSONL snapshot
  (`memory_snapshot.py` reads `Claude.app/Contents/Info.plist` CFBundleShortVersionString).
  Slice leaks by Desktop version to catch "Anthropic changed the session path" the day it
  lands. We could NOT answer Karol's version before this existed.
- **`reaper-status.json`** (written every cycle by `reap_stale_claude_sessions.py`) +
  a per-cycle `[claude-reaper]` stderr marker. Fields: `candidates_seen`, `claude_killed`,
  `mcp_orphans_killed`, `ps_timed_out`, `snapshot_empty`, `parse_misses` (NO_UUID_MATCH),
  `max_group`. `ps_timed_out=true` or `snapshot_empty=true` or a nonzero `parse_misses`
  = a blinded reaper (previously silent). A rising `parse_misses` is the smoking gun for
  a session-path format change.
- **Leak-rate Sentry alert** (`memory_snapshot.py::_maybe_leak_alert`): fires an event
  tagged with `install_id` when `claude_cli` OR `sessions_configured_remote_macos_mcp`
  climbs monotonically for N cycles with zero exits. Flips this from "user reports a
  freeze after the fact" to "we get paged while it climbs."

### 4. The mechanism (why it leaks at all)

Customer `.mcpb` boxes have no `claude` CLI, so the pipeline's `claude -p` calls are
intercepted by the queue provider seam (`S4L_CLAUDE_PROVIDER=queue`): each becomes a
file-queue job, and the real Claude turn is run by a **Claude Desktop scheduled task**
(agent-mode stream-json session), NOT a `claude -p` subprocess. Those Desktop sessions
finish their one queue turn but never exit (Desktop keeps the stream-json session warm),
and Desktop starts a NEW one every ~1 min regardless of whether the prior finished. That
is the pileup. (Our launchd autopilot uses `StartInterval`, which coalesces and does not
double-fire, so the MacStadium box does not leak this way; a customer's Desktop scheduler
does.) `S4L_REAPER_MAX_GROUP` (default 12) is a per-session-uuid count-cap backstop, not
a per-task-type limit; it only fires when the queue-aware spare signal is stale. The real
fix is source-level (worker exits after one queue iteration); until then the reaper is the
containment.

---

When adding OTel: instrument `mcp/src/index.ts` tool handlers, export spans to a local
file under `skill/logs/` (new filename, NOT colliding with the patterns above), thread
the batch id / trace id into the `runPython` env so it lands in the Python file logs as
an additive field. Touch nothing in sections B-F.
