#!/usr/bin/env python3
"""Dedicated, isolated logging for the Twitter follow-gate.

The follow-gate in score_twitter_candidates.py drops candidate threads whose
author we already follow. Its `[follow_gate]` stderr markers land in the giant
mixed twitter-cycle log; this helper ALSO writes a clean, timestamped, greppable
record to skill/logs/follow-gate.log so you can `tail -f` exactly what the filter
loads and catches each cycle, without digging through 20MB of cycle output.

All functions are best-effort: they NEVER raise, so logging can never break the
fail-open gate. If the log can't be written, the gate proceeds silently.

Line formats (one CYCLE line per scoring run, one SKIP line per dropped author):
  <iso8601> <our_account> CYCLE loaded=<N> source=<ok|404|error|unresolved> checked=<M> skipped=<K> batch=<id>
  <iso8601> <our_account> SKIP @<handle> url=<url> batch=<id>

Read it with:  tail -f ~/social-autoposter/skill/logs/follow-gate.log
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

LOG_PATH = os.path.expanduser("~/social-autoposter/skill/logs/follow-gate.log")


def _now() -> str:
    try:
        return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    except Exception:
        return "?"


def _append(line: str) -> None:
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a") as fh:
            fh.write(line.rstrip("\n") + "\n")
    except Exception:
        # Best-effort: never let logging break the fail-open gate.
        pass


def record_cycle(our_account, loaded, source, checked, skipped, batch_id=None) -> None:
    """One line per scoring run: did the gate load the set (loaded>0, source=ok),
    how many candidates it checked, and how many it skipped this run."""
    _append(
        f"{_now()} {our_account or '(unresolved)'} CYCLE "
        f"loaded={loaded} source={source} checked={checked} "
        f"skipped={skipped} batch={batch_id or '-'}"
    )


def record_skip(our_account, handle, url, batch_id=None) -> None:
    """One line per dropped candidate (author we already follow)."""
    _append(
        f"{_now()} {our_account or '(unresolved)'} SKIP "
        f"@{handle} url={url} batch={batch_id or '-'}"
    )
