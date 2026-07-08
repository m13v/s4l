# S4L Sentry Digest Agent (investigate, don't just list)

## Purpose

The raw Sentry alert rule for the s4l project used to email Matt un-triaged
text on every new issue. That's muted now (snoozed via the Sentry API,
2026-07-07). `scripts/sentry_digest.py` (the orchestrator's pull step) already
did the mechanical work: pulled unresolved level:error/level:fatal issues
("critical Sentry issues", level:warning menubar pings excluded), diffed them
against the ledger, and classified each as NEW / GROWING / STABLE.

**Your job is the part that makes this actually useful: investigate, don't
just report numbers.** For every NEW or GROWING issue, read the actual crash
event, find the relevant code in this repo, check recent git history, and
figure out what's really going on. Then send Matt ONE human-readable email
that explains, in plain English, per issue: what broke, why (your best
diagnosis from the code), and what action (if any) he needs to take. This is
not a data table dump. Write it like you're explaining a bug to a colleague,
not rendering a report.

You are triaging and diagnosing, not fixing. Do not edit code. Do not commit
anything. This is read-only investigation.

## Config

- Sentry org: `mediar-n5`, project: `s4l` (project id `4511598804336640`)
- Auth: `SENTRY_AUTH_TOKEN` env, or `security find-generic-password -s sentry-auth-token -w`
- Repo to investigate: `~/social-autoposter` (this is where every crash in
  these issues actually originates; it's the pipeline that produces them)

## Hard safety rule: never grep outside this repo

This repo's CLAUDE.md documents a known hazard: BSD grep on macOS opens named
pipes it encounters (stale `ad_mailbox_*` FIFOs under `/tmp` or `~/`) and
blocks forever in `read()`, hanging the whole session and blocking the next
scheduled run. **Scope every grep/search to `~/social-autoposter` explicitly**
(e.g. `grep -r "pattern" ~/social-autoposter/scripts`), never a bare `~/` or
`/tmp` sweep. `git log` calls are already scoped by running inside the repo.

## Inputs

Your prompt gives you:
- The scan result JSON on disk (path given), written by `sentry_digest.py`.
  Shape:
  ```json
  {
    "scannedAt": "...", "firstRun": false, "totalOpen": 49,
    "newCount": 1, "growingCount": 1,
    "ledgerPath": ".../scripts/state/sentry_digest_ledger.json",
    "issues": [
      {
        "shortId": "S4L-8", "numericId": "7569249847",
        "title": "BrokenPipeError: [Errno 32] Broken pipe",
        "level": "error", "count": 29, "prevCount": 5,
        "installs": 3, "prevInstalls": 1,
        "classification": "growing",
        "link": "https://mediar-n5.sentry.io/issues/?project=...",
        "lastSeen": "...", "firstSeen": "..."
      }
    ]
  }
  ```
- The ledger path (persistent state across runs, same file the scan compared
  against).
- The outcome file path (mandatory to write, see Step 6).

## Ledger (persistent state across runs)

Read `ledgerPath` with the Read tool before anything else. Schema:
```json
{
  "version": 1,
  "lastUpdated": "2026-07-07T19:16:44Z",
  "issues": {
    "S4L-8": {
      "title": "BrokenPipeError: [Errno 32] Broken pipe",
      "lastEventCount": 29,
      "lastInstallCount": 3,
      "firstSeenRun": "2026-07-07T19:16:44Z",
      "lastSeenRun": "2026-07-07T19:29:22Z",
      "verdict": "growing-actionable",
      "diagnosis": "One-line summary of what you found, so the next run's context is cheap to skim."
    }
  }
}
```
`verdict` is one of: `new-actionable`, `new-noise`, `growing-actionable`,
`growing-noise`, `known-noise`, `baseline` (first-run only, see Step 2).

## Workflow

### Step 1: Read the scan result and the ledger

Read both files. Filter the `issues` array to `classification in ("new",
"growing")`; those are the ones you act on this run. Everything else
(`stable`) just needs its counts refreshed in the ledger at the end, no
investigation.

### Step 2: First-run backfill (only if `firstRun: true`)

Do NOT investigate every backlog issue individually; there can be 40-100+.
Write every issue into the ledger with `verdict: "baseline"` (current
count/installs as the snapshot, no `diagnosis`), send ONE short "digest is
now live" email (top 10 by `installs`, plain listing, no deep dive, state
clearly this is a baseline and future runs will investigate NEW/GROWING
issues), then skip straight to Step 6.

### Step 3: Bound the investigation

If more than 10 issues are NEW or GROWING this run, investigate only the top
10 by `installs` (falling back to `count` if installs are tied/zero). For the
rest, note them in the email as "also new/growing this run, not deep-dived:
<list of shortIds>, no action taken" so nothing silently vanishes, but don't
spend investigation budget on them.

### Step 4: Investigate each issue (bounded, a few minutes each)

For each issue you're investigating:

1. **Fetch the latest event** for full context:
   ```bash
   curl -s -H "Authorization: Bearer $SENTRY_AUTH_TOKEN" \
     "https://sentry.io/api/0/issues/{numericId}/events/latest/"
   ```
   Extract: exception type/message, full stack trace
   (`exception.values[].stacktrace.frames`), tags (release, os, component,
   environment, and any pipeline-specific tags like `attempted`,
   `failure_reasons`, `skip_reasons`, `exit_code` if present on this event
   type).

2. **Find the crashing code.** Look at stack frames for paths under
   `/social-autoposter/` (skip stdlib and third-party package frames). Read
   that file with the Read tool at the relevant line. If the title/message
   itself names a script (e.g. "twitter post pipeline issues", "Error:
   post_drafts: X/Y posted"), grep for that script under
   `~/social-autoposter/scripts` and `~/social-autoposter/skill` directly
   (scoped, per the safety rule above) even if there's no Python stack trace
   frame (some of these are structured error reports from bash wrappers, not
   raw exceptions).

3. **Check recent history** on the implicated file:
   ```bash
   cd ~/social-autoposter && git log --oneline --since="2 weeks ago" -- <file>
   ```
   A recent change touching the crashing code is a strong signal of cause.

4. **Check local logs if relevant.** Some issue types map to a known log
   file under `~/social-autoposter/skill/logs/` (e.g. anything about "twitter
   post pipeline" or "browser locked" correlates with `run-twitter-cycle*`
   logs; "autopilot stalled" / "draft jobs are not being drained" correlates
   with the queue/worker logs). If the event's `lastSeen` timestamp gives you
   a time window, a targeted `grep` or `tail` around that time in the
   matching log (if one obviously exists) can confirm or refute your theory.
   Don't go looking for a log file that doesn't obviously exist; skip this
   sub-step rather than guessing.

5. **Cross-reference known context.** This repo's CLAUDE.md documents a lot
   of pipeline behavior (locked files, known gotchas, the browser-lock
   architecture, the strike-alert rail, etc). If an issue matches something
   already documented there (e.g. a `BrokenPipeError` from a known
   subprocess pattern, or "Twitter browser locked" matching the documented
   locking design), say so explicitly rather than re-diagnosing from
   scratch.

6. **Form a verdict.** Rank by **install impact**, not raw event count (a
   single install retry-looping produces high event counts with near-zero
   real reach). Lean toward `-noise` when it's a known transient
   (`TimeoutError`, single-install `TypeError: fetch failed` with no clear
   code regression) that self-recovers on retry. Lean toward `-actionable`
   when: it maps to a real code bug you can point at (file:line), it affects
   several distinct installs, or it's a new failure mode not explainable by
   an existing known pattern.

### Step 5: Write the email

Subject: lead with the strongest signal, under 90 chars, no em/en dashes. Use
commas, colons, semicolons, or separate sentences instead of dashes.
Examples: `"[Sentry] s4l: BrokenPipeError traced to stale CDP socket in
twitter_post_plan.py"`, `"[Sentry] s4l: 1 new issue, looks like known Twitter
lock contention, no action needed"`.

**Body: human-readable prose, one section per investigated issue.** Not a
table dump. Structure per issue:

```
## S4L-8: BrokenPipeError (29 events, 3 installs, growing from 5/1)

What broke: <plain-English description of the failure>

Likely cause: <your diagnosis, citing specific file:line and/or a recent
commit if you found one, or "couldn't pin it down to a specific commit,
but the pattern matches X">

Action needed: <specific instruction, e.g. "worth a look, the retry logic
in scripts/foo.py:120 doesn't handle this case" OR "None. This is the
documented Twitter-browser-lock contention pattern; it self-recovers on
the next cycle.">

Link: <sentry url>
```

If nothing investigated this run is actionable, say so plainly at the top:
"Nothing here needs your attention today, both are expected-shape noise."
Don't manufacture urgency that isn't there.

If issues were skipped due to the Step 3 cap, list them at the end under
"Also new/growing, not investigated this run" with shortId, count, installs,
link only (no diagnosis).

Write the body to a temp file, then send:
```bash
python3 scripts/send_gmail_report.py \
  --to "i@m13v.com" \
  --subject "$SUBJECT" \
  --body-file /tmp/sentry-digest-body.txt
```
(Plain text, not `--html`. Prose reads better as plain text in Gmail than as
forced HTML.)

### Step 6: Write the ledger back

For EVERY issue in the scan result (not just the ones you investigated),
write or update its ledger entry with current `count`/`installs` as
`lastEventCount`/`lastInstallCount`. Only set/overwrite `verdict` and
`diagnosis` for issues you actually investigated this run (Step 4); leave
prior `verdict`/`diagnosis` untouched for `stable` issues you didn't
re-examine. Update `lastUpdated` to now. Write the full ledger JSON back to
the `ledgerPath` given in your prompt.

Issues that disappear from the scan (resolved/ignored in Sentry) can stay in
the ledger; don't prune.

### Step 7: Write the outcome file

**MANDATORY**, path given in your prompt:
```json
{
  "firstRun": false,
  "newCount": 1,
  "growingCount": 1,
  "investigatedCount": 2,
  "actionableCount": 1,
  "reportEmailSent": true,
  "reportEmailTo": "i@m13v.com",
  "ledgerWritten": true,
  "summary": "<one sentence>"
}
```

## Important notes

- Read-only. No code edits, no commits.
- Never grep or search outside `~/social-autoposter` (see safety rule above).
- Rank and judge by distinct install count, never raw event count.
- Bound your investigation per issue: one event fetch, one code read, one git
  log, maybe one log grep. This should take a few minutes per issue, not a
  deep audit.
- If the Sentry API is unreachable or errors, write an outcome file with
  `reportEmailSent: false` and a `summary` explaining the failure. Do not
  retry in a loop; the next scheduled run picks it up.
