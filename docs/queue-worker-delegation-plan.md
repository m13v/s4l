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
