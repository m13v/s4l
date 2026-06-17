# Twitter browser session lock: defects + fix + how to verify (2026-06-16)

**Audience: a future agent with NO prior context.** This file tells you (1) what was
broken, (2) what the fix is and where, (3) how to confirm the fix is *still in the code*,
(4) how to confirm it is *working in production*, and (5) the exact signatures that mean
it *regressed*. If you are debugging "Twitter pipelines fight over the same browser tab"
or "dm-replies keeps giving up on the browser lock", start here before re-investigating.

---

## 0. 30-second verification

```bash
cd ~/social-autoposter
# (1) fix present in code?
grep -q "_is_python_holder_alive" scripts/twitter_browser.py && grep -q "O_EXCL" scripts/twitter_browser.py && echo "code: present" || echo "code: REVERTED"
# (2) the unsafe shell workaround is gone? anchor on ^ so the explanatory COMMENT
#     (which contains the literal phrase) is not miscounted. expect 0.
grep -REc '^[[:space:]]*rm -f .*twitter-browser-lock\.json' skill/run-twitter-cycle.sh skill/engage-twitter.sh | awk -F: '{s+=$2} END{print "live rm -f count:", s, (s==0?"(good)":"(REGRESSED)")}'
# (3) behavior still correct?
/opt/homebrew/bin/python3 scripts/test_browser_lock.py | tail -1   # expect: RESULT: ALL PASS
```
If all three say good / present / ALL PASS, the fix is intact. Done.

---

## 1. The bug (why Twitter pipelines fought over one tab)

There are **two** serialization layers for Twitter browser work:

- **Shell FIFO lock** `skill/lock.sh` (`acquire_lock "twitter-browser"`): a `mkdir`-atomic,
  ownership-checked, FIFO-fair lock. Most pipelines take it. **It works; it is not the bug.**
  (Verified 2026-06-16: peers wait politely, e.g. a 15:43 engage waited 340s for the cycle.)
- **Python session mutex** `~/.claude/twitter-browser-lock.json`, managed in
  `scripts/twitter_browser.py::_acquire_browser_lock`. This is the **universal** mutex —
  *every* browser op routes through `get_browser_and_page` -> `_acquire_browser_lock`,
  including ops that skip the shell lock (cross-pipeline handoff races, MCP-driven posts).
  **All the bugs were here.**

Three linked defects in the session mutex:

- **(a) Dead `python:PID` holders were irreclaimable for 300s.** `_acquire_browser_lock`
  only liveness-checked *UUID* holders (a legacy format). The holders actually written today
  are `python:<pid>`, and those got **no** PID-liveness check. A python op killed without
  running its `atexit` release (SIGKILL/OOM/watchdog SIGTERM/hang) held the lock until the
  `LOCK_EXPIRY` (300s) failsafe. Every peer in that window waited `LOCK_WAIT_MAX` (45s) and
  `sys.exit(1)` — i.e. **dropped its browser op**. Log signature: a holder whose age climbs
  monotonically and never refreshes, with peers logging "waited 45s, giving up".

- **(b) A shell `rm -f` of the lockfile deleted LIVE peers' locks.** Four pipeline sites did
  `release_lock "twitter-browser"` (give up shell exclusivity) immediately followed by an
  **unconditional, ownership-blind** `rm -f ~/.claude/twitter-browser-lock.json`. It existed
  to paper over defect (a) ("covers SIGKILL of the wrapper"). But sequenced *after* the shell
  release, under a handoff it deleted a freshly-acquired peer's mutex -> **two browser ops on
  one X tab = the "fighting / back-and-forth" you saw**. Note python's own
  `_release_browser_lock` *is* ownership-checked; the shell `rm -f` bypassed that check.

- **(c) Acquisition was non-atomic (TOCTOU).** It did `os.path.exists()` then a separate
  `open(LOCK_FILE, "w")`. Two acquirers that both saw "no file" both wrote -> both believed
  they held it. Rare on its own, but it compounds (b): right after the `rm -f`, two waiters
  race in and both win.

`release_lock` in `skill/lock.sh` is itself ownership-blind (`rm -rf` with no `pid==$$`
check), but its call sites guarantee the caller is the current holder and the re-acquire
*blocks* rather than clobbers, so it does **not** clobber peers. That dead end was
investigated and ruled out — don't re-chase it.

## 2. The fix (what changed, where)

All in `scripts/twitter_browser.py::_acquire_browser_lock` and two new helpers:

- **(a)** `_is_python_holder_alive(holder)` — parses `python:<pid>` and `os.kill(pid, 0)`.
  A holder we can prove dead is reclaimed **immediately** (priority 3 in the acquire loop),
  not after 300s. Mirrors the existing `_is_holder_alive` UUID probe. Errs toward NOT
  stealing (returns True on any ambiguity), so the worst case degrades to the old
  `LOCK_EXPIRY` failsafe.
- **(c)** `_try_take_lock()` — claims the file with `os.open(..., O_CREAT|O_EXCL|O_WRONLY)`,
  a single atomic syscall. Two acquirers can never both win; the loser re-loops.
- **(b)** the four shell `rm -f` lines were **removed** (replaced with a comment that says
  why). Sites: `skill/run-twitter-cycle.sh` (was lines ~1436/1893/1955) and
  `skill/engage-twitter.sh` (was ~121). With (a) self-reclaiming dead holders, the workaround
  is obsolete *and* was the cause of (b).
- A re-entrant guard (holder == us) refreshes the timestamp and proceeds, fixing a latent
  self-deadlock on PID reuse.
- Verifiable markers added (stderr, additive per `docs/log-consumer-contract.md`):
  `[browser_lock] reclaimed holder=... reason=dead_python|dead_uuid|expired age=Ns -> pid=N`.
- The giveup message now reads `...locked by session <holder> (Ns, peer alive); ...`. The
  substring `locked by session` is **preserved** (parsed by `scripts/post_reddit.py:1554`
  for the sibling reddit path); `peer alive` is appended so reaching giveup provably means
  real contention, not defect-(a) starvation.

## 3. Confirm the fix is STILL PRESENT (catch a silent revert)

The relevant files are `chflags uchg`-locked, but the background auto-commit agent or a
future edit could still revert them. Fingerprints:

```bash
grep -n "_is_python_holder_alive\|_try_take_lock\|O_EXCL\|reason=dead_python" scripts/twitter_browser.py
#   -> all four must be present.
grep -REn 'rm -f .*twitter-browser-lock\.json' skill/run-twitter-cycle.sh skill/engage-twitter.sh
#   -> must match ONLY the explanatory comment in run-twitter-cycle.sh (the literal phrase
#      'NO `rm -f twitter-browser-lock.json` here'), never an actual command line.
```
Then run the regression test (committed, no browser needed):
```bash
/opt/homebrew/bin/python3 scripts/test_browser_lock.py   # expect: RESULT: ALL PASS
```
The test self-checks the fix is present (`fix_present:` lines) and exercises reclaim,
atomicity, live-peer giveup, re-entrancy, dead-UUID, and expiry.

## 4. Confirm it is WORKING in production (live logs)

Pipeline logs live in `skill/logs/` (per `docs/log-consumer-contract.md`). Python stderr,
including the `[browser_lock]` markers, lands in the per-run cycle logs and the
`launchd-*-stderr.log` files.

```bash
# Fix actively preventing defect-(a) starvation (rare event; empty is fine if nothing crashed):
grep -rho '\[browser_lock\] reclaimed .*reason=dead_python' skill/logs/ 2>/dev/null | tail
#   Each hit = a dead holder that USED to starve the fleet for up to 300s, now cleared in ~0s.

# Healthy contention (NOT a bug): a giveup that names a LIVE peer.
grep -rho 'locked by session .*peer alive.*giving up' skill/logs/ 2>/dev/null | tail
```
Because the overlap itself ("two ops on one tab") leaves no single log line, the working
proxy is: (1) reclaim markers fire when holders die, (2) every giveup says `peer alive`,
(3) the unit test passes, (4) no live `rm -f` in the shells. If the user watches the browser
stream, the tab should no longer flip between two pipelines' actions.

## 5. Signatures that mean it REGRESSED

- A twitter giveup message **without** `peer alive` (old format `locked by session X (Ns);
  waited 45s, giving up.`) -> the python fix was reverted; defect (a) is back.
- Any **actual** `rm -f ...twitter-browser-lock.json` command (not the comment) in any
  `skill/*.sh` -> defect (b) was re-introduced. Detect with the anchored grep
  `grep -REn '^[[:space:]]*rm -f .*twitter-browser-lock\.json' skill/` (expect no hits; an
  unanchored grep also matches the explanatory comment). The auto-commit agent has historically
  "simplified" locked files; revert it and tell the user.
- `scripts/test_browser_lock.py` prints `RESULT: N FAILED` -> read the FAIL lines; the path
  they name (atomicity / reclaim / etc.) tells you which defect returned.
- dm-replies-twitter logs again showing a holder whose `age` climbs and never refreshes while
  peers wait 45s and drop -> defect (a) starvation; check the python fingerprints in §3.

## 6. Follow-up (NOT done here; Twitter-only was the ask)

`scripts/linkedin_browser.py` and `scripts/reddit_browser.py` carry their **own copies** of
this lock pattern ("Mirror twitter_browser._acquire_browser_lock semantics"). LinkedIn's copy
has the same latent defect (a)/(c). Reddit uses a more elaborate lease/heartbeat scheme
(`scripts/reddit_browser_lock.py` + `scripts/mcp_lock_proxy.py`), so verify before assuming.
If LinkedIn shows the same starvation, port `_is_python_holder_alive` + `_try_take_lock` there.

## 7. Do NOT re-add the `rm -f`

It looks like harmless defense-in-depth. It is not: it deletes live peers' mutexes. Dead
holders are reclaimed in `_acquire_browser_lock` now. The locked-file comments and this doc
exist specifically to stop a future agent (human or auto-commit) from "restoring" it.
