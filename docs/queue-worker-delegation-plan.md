# Queue worker delegation plan (v9, NOT YET IMPLEMENTED)

Status 2026-07-08: **design validated via throwaway probe tasks, zero production
code changed.** `QUEUE_WORKER_PROMPT_VERSION` in `mcp/src/index.ts` is still `8`.
The real `s4l-worker` SKILL.md still contains zero mention of delegation or
sub-agents; it is the same single-shot "claim, draft it yourself, submit" prompt
it has always been. This doc exists so the validated design survives even if the
chat session that produced it is gone.

## The idea

Today (v8) `s4l-worker` fires every minute via cron, claims at most one job from
`claude_job.py`'s file queue, drafts it itself in the same session (Bash/Read/Write
only), writes the result, and stops. See the `queueWorkerBody()` comment block for
the current design.

The proposed v9 change: the worker session claims a job, then **delegates the
actual drafting/execution to a fresh sub-agent** (Task-tool style, `model: "opus"`)
instead of doing the work inline, then loops: claim -> delegate -> wait -> claim
next, repeated within one continuous worker session instead of exiting after one
job.

Why: context isolation. The heavy token cost of drafting lives in a disposable
child session; the parent worker's own context stays thin across many claimed
jobs instead of accumulating history turn over turn.

## What was validated (probe, 2026-07-07/08, throwaway scheduled tasks since deleted)

- **Context isolation works**: worker claims a job, hands drafting to a fresh
  `opus` sub-agent, worker context stays thin.
- **The loop pattern holds**: claim -> delegate -> wait -> claim next, 10
  iterations in a row inside one continuous worker session, no drift, no
  duplicate claims.
- **Delegation survives realistic durations**: up to ~210 seconds of continuous
  work, proven twice — once with artificially-paced chained blocking calls,
  once with genuinely real content (composing actual replies to 5 fake
  candidates and persisting each one).
- **The one failure mode found, and it's well understood**: if the delegated
  sub-agent ever fully idles (backgrounds a task and waits on a `Monitor`-style
  notification instead of doing continuous real work), the host kills the whole
  chain — parent and child — within roughly 1-3 minutes, regardless of how long
  the real job would have taken. Confirmed with an empty, never-completed
  background-process output file and no surviving live process. As long as the
  sub-agent never idle-waits like that, delegation is safe.

## What is NOT validated yet

- **No real production job has ever gone through delegation.** Two attempts to
  race the live worker for a real job both missed on pure timing (jobs landed
  40-80s outside the poll window), not because anything failed. Everything
  above is simulated-but-realistic content, not an actual live twitter-prep
  batch with its real embedded pacing instructions and real candidates.
- **Reaper interaction unverified**: whether `scripts/reap_stale_claude_sessions.py`'s
  session-sparing logic (which walks the process tree to protect an in-flight
  claim) correctly identifies the right process when execution is split across
  a parent and a delegated sub-agent, instead of the one process it assumes
  today.
- **Cross-install unverified**: whether `model: "opus"` sub-agent dispatch works
  the same way on other harness versions (e.g. a customer's Desktop-bundled
  install). Everything above was tested on one operator box.
- **Duplicate-notification handling is not an enforced rule yet.** During the
  probe, the model noticed and ignored a repeat notification on its own, once,
  as an emergent behavior from a prompt instruction. For production this needs
  to be a hard rule the prompt enforces, not a hope.

## Next step to actually implement

1. Bump `QUEUE_WORKER_PROMPT_VERSION` to `9` in `mcp/src/index.ts`.
2. Rewrite `queueWorkerBody()` / `TYPE_TO_WORKER_NOTES` (`scripts/claude_job.py`)
   to add the delegation step, with the one hard rule proven above: **the
   delegated sub-agent must never idle-wait; it must always chain real,
   continuous work.**
3. Add explicit, enforced idempotent duplicate-notification handling (not
   emergent prompt behavior).
4. Ship through the normal sanctioned flow: `bash scripts/release-mcpb.sh --staging`.
5. First real live-fire validation happens once a staging install's worker
   actually picks up the new prompt and processes a real queued job.

## Appendix: verbatim validated prompts (not a paraphrase)

The throwaway probe `SKILL.md`/task bodies were deleted from
`~/.claude/scheduled-tasks/` after testing, but the literal text survives inside
the `mcp__scheduled-tasks__create_scheduled_task` tool calls in the probe
session's own transcript (`~/.claude/projects/-Users-matthewdi-s4l/5ea09799-1485-4728-ab2d-f25760f60726.jsonl`).
Pulled verbatim from there, not reconstructed from the narrative summary above.

This is the most mature version tested (`s4l-delegate-real-draft-probe`,
2026-07-07T19:47:30-07:00) — realistic drafting content, no artificial delays,
includes the duplicate-notification guard. It is the direct template a v9
`queueWorkerBody()` should adapt (swap the throwaway test-queue paths for the
real `claude_job.py` invocation and state dir, drop the "this is a test"
framing):

**Parent worker loop** (claim -> delegate -> wait -> claim next):

```
Run this loop for up to <N> iterations. Keep a running mental list of
{iteration, job_id, outcome} — do NOT write it to a file.

STEP A — CLAIM: Run the EXACT Bash command "<CLAIM_CMD>" and let it run to
completion (do NOT interrupt it, do NOT make any other tool call while it
runs). It prints exactly one line of JSON.

STEP B — CHECK EMPTY: If it printed "{}" (empty), the queue is drained. Stop
looping right now, skip to FINAL REPORT.

STEP C — DELEGATE: Otherwise it printed something like
{"job_id":"...","prompt_file":"...","schema_file":"..."}. Do NOT read the
prompt file yourself. Do NOT execute the job yourself. Launch exactly ONE
sub-agent using the Task tool (may be named "Agent"). Use subagent_type
"general-purpose" and model "opus". The sub-agent's prompt must be the
following, with <job_id>, <prompt_file>, <schema_file> replaced by this
iteration's real values (if schema_file is null, drop that sentence):

--- [sub-agent prompt template below] ---

STEP D — WAIT: You (the parent) must make ZERO tool calls of your own while
the sub-agent works — no sleep, no poll, no Monitor, no filler Bash, nothing.
Just end your turn and wait for the sub-agent's natural completion
notification, however long that takes.

STEP E — HANDLE DUPLICATE NOTIFICATIONS: A completion notification for the
same sub-agent may arrive more than once. If you get one for an iteration
already recorded as done, silently ignore it — do not re-loop, do not launch
another sub-agent for it.

STEP F — RECORD: Note this iteration's outcome (iteration number, job_id,
outcome verbatim).

STEP G — LOOP: If you have not yet done <N> iterations and the previous claim
was NOT empty, go back to STEP A for the next iteration. Otherwise proceed to
FINAL REPORT.

FINAL REPORT: print ONE compact report with:
1. Total iterations run and total sub-agents launched.
2. Whether each wait resolved cleanly via the natural notification, with no
   gaps in your own continuity.
3. A numbered list, one line per iteration, of
   "iteration N: job <job_id> -> <outcome verbatim>".
Then STOP.
```

**Delegated sub-agent prompt** (the actual worker per job):

```
You are executing ONE queued job for a local file-based pipeline. Read the
file <prompt_file> with the Read tool; if the result says it is partial or
truncated, keep reading with offsets until EOF. Also read <schema_file>; the
JSON you produce MUST satisfy that schema. Follow the job prompt EXACTLY — it
asks you to compose a genuine reply for each candidate and persist it
immediately via a real Bash command before moving to the next candidate. Do
this for real, one candidate at a time, with no sleeping or waiting of any
kind between them. After all candidates are drafted and persisted, produce
the SINGLE JSON object the job prompt asks for — no prose, no markdown, no
code fences. Write that JSON object to <result_file_path> using the Write
tool. Then submit it by running this EXACT Bash command:

python3 <path-to>/claude_job.py result --job <job_id> --result-file
<result_file_path> --state-dir <state_dir>

If it reports the result was rejected, fix your JSON file and submit again,
at most twice. Your final message must be exactly one line: "DONE <job_id>"
on success, or "FAILED <job_id>: <short reason>" on failure.
```

**Why this exact wording matters** (don't simplify it away when adapting):
- "Do NOT read the prompt file yourself" on the parent — keeps the parent's
  context thin; the sub-agent is the one that reads the (potentially large)
  job prompt and schema.
- "make ZERO tool calls of your own while the sub-agent works" — this is the
  proven condition. An earlier variant (`s4l-delegate-slow-loop-probe`) that
  let the parent do nothing AND let the sub-agent idle-wait too, failed: the
  host killed the whole chain. `s4l-delegate-paced-probe` isolated the fix —
  the sub-agent must stay continuously busy (never idle-wait via Monitor or
  backgrounding for its own internal pacing); as long as it does, the parent
  survives making zero calls of its own for the whole span (proven up to ~210s).
- STEP E's duplicate-notification guard is the only place this was ever
  enforced, and it's a prompt instruction the model happened to follow
  correctly in testing, not a code-level guarantee. This is the specific gap
  called out above under "What is NOT validated yet" — a v9 implementation
  should make this a hard rule (e.g. dedupe against an already-recorded
  job_id at the script level), not rely on the model remembering.

Full text of every probe variant tested (including the earlier, less-refined
`s4l-delegate-loop-probe` and `s4l-delegate-paced-probe` iterations that led
here) is recoverable the same way: grep the probe transcript above for
`mcp__scheduled-tasks__create_scheduled_task` tool_use blocks.
