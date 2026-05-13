#!/usr/bin/env python3
"""CLI wrapper around generation_trace.build_trace / write_trace_tempfile.

Use case: bash pipelines (run-twitter-cycle.sh) that need to write a
generation_trace JSON file before invoking Claude. The bash script
gathers the context (TOP_REPORT, TOP_QUERIES_JSON, etc.) and pipes it
to this script as JSON on stdin; the script writes a tempfile and
prints the path on stdout. The bash script captures that path into a
variable, then forwards it via env var to the downstream post-phase
(twitter_post_plan.py), which appends --generation-trace to log_post.py.

Why this script exists: keeping the trace shape in one place
(scripts/generation_trace.py) is important; bash pipelines can't import
Python, so we shim through this one-liner. Don't expand it; if you need
more pipelines, just call it the same way.

Usage:
    echo '{"platform":"twitter","project_name":"all","prompt_chars":1234,
           "top_performers_text":"...","top_search_topics_text":"...",
           "recent_comment_ids":[],"extras":{"top_queries":[],"supply":[]}}' \\
        | python3 scripts/write_generation_trace.py --prefix twitter_gen_trace_

    Prints the path on stdout; exits 0. On failure exits 1 with a JSON
    error envelope on stderr. Callers should `|| true` if they want to
    swallow failures (trace is nice-to-have).
"""
import argparse
import json
import sys
import os

# scripts/ is on the path via the .py living there.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from generation_trace import build_trace, write_trace_tempfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prefix", default="gen_trace_",
                        help="Tempfile prefix (default gen_trace_).")
    args = parser.parse_args()

    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": "INVALID_STDIN_JSON", "message": str(e)}),
              file=sys.stderr)
        sys.exit(1)

    # Forward every supported kwarg; unknown keys are dropped silently
    # so the caller can over-send without breaking the schema contract.
    trace = build_trace(
        platform=payload.get("platform", ""),
        project_name=payload.get("project_name", ""),
        prompt_chars=int(payload.get("prompt_chars", 0) or 0),
        top_performers_text=payload.get("top_performers_text", "") or "",
        top_search_topics_text=payload.get("top_search_topics_text", "") or "",
        recent_comment_ids=payload.get("recent_comment_ids") or [],
        model=payload.get("model"),
        min_score_floor=payload.get("min_score_floor"),
        extras=payload.get("extras") or {},
    )
    path = write_trace_tempfile(trace, prefix=args.prefix)
    if not path:
        print(json.dumps({"error": "WRITE_FAILED"}), file=sys.stderr)
        sys.exit(1)
    # stdout is the path only — bash captures via $(...) and any extra
    # noise would corrupt the env var downstream.
    print(path)


if __name__ == "__main__":
    main()
