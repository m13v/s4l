"""Shared helpers for writing the generation_trace audit blob.

Every post-drafting pipeline (post_github.py, post_reddit.py,
run-twitter-cycle.sh + twitter_post_plan.py) builds a small JSON snapshot
of "what Claude saw" — top_performers report, top_search_topics report,
recent-comments cluster, prompt size, model, scoring formula — and
persists it to posts.generation_trace JSONB. This module owns the shape
and the file-handoff dance so the three pipelines stay consistent.

Why a shared module instead of duplicating the dict in each pipeline:
the trace shape is a contract with the audit consumer (a future "show
me which examples produced post #123" query). Drift between pipelines
makes that query impossible. Centralizing here means a `version`
bump migrates every writer at once.

Shape v1 — must match the comment block at the top of
migrations/2026-05-12_generation_trace.sql. Bump `TRACE_SHAPE_VERSION`
and add a migration if the contract changes.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from typing import Iterable, Optional

TRACE_SHAPE_VERSION = 1

# API cap (matches src/app/api/v1/posts/route.ts). We do NOT truncate
# the report bodies — top_performers.py and top_search_topics.py
# already pre-summarize their output and rarely cross 10 KB combined.
# If we ever DO cross 64 KB the API returns badRequest and the post
# still ships (log_post just drops the trace field and warns).
MAX_TRACE_BYTES = 64 * 1024


def build_trace(
    *,
    platform: str,
    project_name: str,
    prompt_chars: int,
    top_performers_text: str = "",
    top_search_topics_text: str = "",
    recent_comment_ids: Optional[Iterable[int]] = None,
    model: Optional[str] = None,
    min_score_floor: Optional[int] = None,
    extras: Optional[dict] = None,
) -> dict:
    """Construct the canonical trace dict for one Claude drafting run.

    All examples-strings are stored verbatim. We deliberately do NOT
    re-derive structure (e.g. "parse top_performers_text and pull out
    each example as a sub-object") — the bytes-for-bytes report is the
    only audit-faithful representation of what landed in the prompt.

    `extras` is a per-pipeline escape hatch (twitter passes top_queries
    + supply_signal; reddit passes dud_queries). Stored under
    `examples.extras` so the schema stays stable.
    """
    return {
        "version": TRACE_SHAPE_VERSION,
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "model": model or "",
        "platform": platform,
        "project": project_name,
        "prompt_chars": int(prompt_chars or 0),
        "examples": {
            "top_performers_text": top_performers_text or "",
            "top_search_topics_text": top_search_topics_text or "",
            "recent_comment_ids": [int(x) for x in (recent_comment_ids or [])],
            "extras": dict(extras or {}),
        },
        "scoring": {
            "score_formula": "clicks*10 + comments*3 + upvotes_net",
            "min_score_floor": int(min_score_floor or 0),
            "click_aware_since": "2026-05-12",
        },
    }


def write_trace_tempfile(trace: dict, *, prefix: str = "gen_trace_") -> Optional[str]:
    """Persist trace dict to a NamedTemporaryFile and return the path.

    Returns None on any IO failure — the caller must treat the trace as
    nice-to-have, never block the post on a failed trace write.
    delete=False so the file survives the with-block close; the child
    process consuming --generation-trace reads it and the parent
    cleans up via cleanup_trace_tempfile() at end-of-run.
    """
    try:
        tf = tempfile.NamedTemporaryFile(
            prefix=prefix, suffix=".json",
            mode="w", delete=False, encoding="utf-8",
        )
        try:
            json.dump(trace, tf, ensure_ascii=False)
        finally:
            tf.close()
        return tf.name
    except (OSError, TypeError, ValueError):
        return None


def cleanup_trace_tempfile(path: Optional[str]) -> None:
    """Best-effort delete of a trace temp file. Safe to call with None."""
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def trace_bytes(trace: dict) -> int:
    """Serialized size in bytes. Useful for guard checks before write."""
    try:
        return len(json.dumps(trace, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return 0
