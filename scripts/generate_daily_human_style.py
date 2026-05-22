#!/usr/bin/env python3
"""Daily synthesizer: distill ONE engagement style from the top human Twitter
replies captured in the last 24h.

Pipeline
--------
1. Pull every `thread_top_replies` row from the last 24h on platform='twitter'
   with `has_link = false` (human replies, not link-tail spam).
2. Score by likes (the only reliable proxy in the window, since `views` is
   missing for most rows). Take the top REPLY_POOL_SIZE.
3. Build a Claude prompt that lists each reply with its like-count + thread
   URL and asks the model to synthesize ONE new engagement style following
   the seed-style schema (name / description / example / best_in / note).
4. Parse the JSON, INSERT a new row into `engagement_styles_human_derived`
   (status='active'), and emit the chosen name + style id to stdout.

The picker (scripts/engagement_styles.py) reads the most recent active row
from this table on a 5% chance per Twitter reply. See the migration file
2026-05-22_engagement_styles_human_derived.sql for the table schema and
the why.

Run manually: python3 scripts/generate_daily_human_style.py
Cron entry  : skill/run-generate-daily-style.sh (wraps this via run_claude.sh).
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_DIR, "scripts"))

from db import get_conn  # noqa: E402

REPLY_POOL_SIZE = 10            # top N human replies fed to Claude
WINDOW_HOURS = 24
MIN_LIKES = 5                   # exclude noise-floor replies
CLAUDE_MODEL_DEFAULT = None     # inherit from settings.json
RUN_CLAUDE_PATH = os.path.join(REPO_DIR, "scripts", "run_claude.sh")
SCRIPT_TAG = "daily-human-style"


def fetch_top_human_replies(conn, limit=REPLY_POOL_SIZE, hours=WINDOW_HOURS):
    """Top human Twitter replies from the last <hours>, ordered by likes."""
    cur = conn.execute(
        """
        SELECT
            ttr.id,
            ttr.reply_url,
            ttr.thread_url,
            ttr.reply_author_handle,
            ttr.reply_content,
            COALESCE(ttr.likes, 0) AS likes,
            COALESCE(ttr.replies, 0) AS replies_count,
            COALESCE(ttr.retweets, 0) AS retweets,
            ttr.captured_at
        FROM thread_top_replies ttr
        WHERE ttr.platform = 'twitter'
          AND COALESCE(ttr.has_link, false) = false
          AND ttr.captured_at >= NOW() - (%s || ' hours')::INTERVAL
          AND COALESCE(ttr.likes, 0) >= %s
          AND ttr.reply_content IS NOT NULL
          AND LENGTH(TRIM(ttr.reply_content)) > 0
        ORDER BY COALESCE(ttr.likes, 0) DESC,
                 COALESCE(ttr.replies, 0) DESC
        LIMIT %s
        """,
        (str(hours), MIN_LIKES, limit),
    )
    rows = cur.fetchall()
    cols = [
        "id", "reply_url", "thread_url", "reply_author_handle",
        "reply_content", "likes", "replies_count", "retweets",
        "captured_at",
    ]
    return [dict(zip(cols, r)) for r in rows]


def load_existing_style_names(conn):
    """Names already taken in the hardcoded registry or the human-derived
    table, so the model doesn't propose a collision."""
    names = set()
    try:
        from engagement_styles import get_all_styles  # noqa: WPS433
        names.update(get_all_styles().keys())
    except Exception:
        pass
    cur = conn.execute(
        "SELECT name FROM engagement_styles_human_derived WHERE status='active'"
    )
    for r in cur.fetchall():
        names.add(r[0])
    return names


def build_prompt(replies, reserved_names):
    lines = []
    lines.append(
        "You are analyzing the top-performing human Twitter replies from the "
        f"last {WINDOW_HOURS} hours and distilling ONE new engagement style "
        "we can use for our own replies."
    )
    lines.append("")
    lines.append("## What you're looking for")
    lines.append("")
    lines.append(
        "These replies all WON the thread (top of the conversation by likes). "
        "Find the shared pattern that makes them work — the rhetorical move, "
        "the structural shape, the relationship to the OP. Most winners "
        "share ONE pattern; that pattern is your new engagement style."
    )
    lines.append("")
    lines.append(
        "Ignore: replies that win because of follower count, fame, or "
        "non-repeatable luck. Focus on the structural move that we (a "
        "small account) could imitate and have a chance of replicating."
    )
    lines.append("")
    lines.append(f"## Top {len(replies)} human replies (by likes)")
    lines.append("")
    for i, r in enumerate(replies, 1):
        # Pull the OP tweet text from the URL? We don't have it. Show the
        # thread URL so the model can infer context from the reply alone.
        lines.append(
            f"### #{i} (likes={r['likes']}, replies={r['replies_count']}, "
            f"rt={r['retweets']})"
        )
        lines.append(f"Thread: {r['thread_url']}")
        lines.append(f"Reply by @{r['reply_author_handle']}:")
        lines.append(f"> {r['reply_content']}")
        lines.append("")

    lines.append("## Schema (match exactly)")
    lines.append("")
    lines.append(
        "Return ONE JSON object describing a single new engagement style. "
        "No prose around it, no markdown code fence. The object MUST have "
        "every field below:"
    )
    lines.append("")
    lines.append("```")
    lines.append("{")
    lines.append('  "name": "<snake_case_name>",')
    lines.append('  "description": "<one to three sentences describing the style>",')
    lines.append('  "example": "<one short OP + reply pair demonstrating the style>",')
    lines.append('  "best_in": {')
    lines.append('    "twitter": ["<short context label>", ...],')
    lines.append('    "reddit":  [],')
    lines.append('    "linkedin": []')
    lines.append("  },")
    lines.append('  "note": "<one to two sentences: when to use, when not to>"')
    lines.append("}")
    lines.append("```")
    lines.append("")
    lines.append("## Rules")
    lines.append("")
    lines.append(
        "1. The name must be unique. Reserved (do NOT propose these): "
        f"{sorted(reserved_names)}."
    )
    lines.append(
        "2. The name should be 2 to 4 snake_case tokens, descriptive of the "
        "MOVE (e.g. `mirror_and_extend`, `flip_to_alt`, not `good_reply`)."
    )
    lines.append(
        "3. The description should make the style copyable: a future model "
        "reading just that one sentence should know what to write."
    )
    lines.append(
        "4. The example should be a realistic OP + reply pair, not lifted "
        "verbatim from the inputs."
    )
    lines.append(
        "5. best_in.twitter is required (at least one context label). reddit "
        "and linkedin can be empty arrays if the style is Twitter-specific."
    )
    lines.append(
        "6. NEVER propose a style about including a product, a URL, or a "
        "mechanism. Our link-tail layer handles that downstream. The style "
        "is about the text BEFORE the link."
    )
    return "\n".join(lines)


def call_claude(prompt):
    cmd = [RUN_CLAUDE_PATH, SCRIPT_TAG, "-p", prompt, "--output-format", "json"]
    if CLAUDE_MODEL_DEFAULT:
        cmd.extend(["--model", CLAUDE_MODEL_DEFAULT])
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        sys.stderr.write(
            f"[generate_daily_human_style] claude rc={result.returncode}\n"
            f"stderr: {result.stderr[:2000]}\n"
        )
        raise RuntimeError(f"claude wrapper failed: rc={result.returncode}")
    # --output-format json: stdout is a single JSON envelope with `result` text.
    envelope = json.loads(result.stdout)
    return envelope.get("result", "") or ""


_JSON_OBJ_RE = re.compile(r"\{[\s\S]*\}")


def extract_json(text):
    """Tolerant of code fences or stray prose around the JSON object."""
    fence = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
    if fence:
        return json.loads(fence.group(1))
    m = _JSON_OBJ_RE.search(text)
    if not m:
        raise ValueError("No JSON object found in model output")
    return json.loads(m.group(0))


def validate_style(style, reserved_names):
    """Sanity-check the model output before we INSERT."""
    required = {"name", "description", "example", "best_in", "note"}
    missing = required - set(style.keys())
    if missing:
        raise ValueError(f"missing fields: {sorted(missing)}")

    name = style["name"]
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{2,40}", name):
        raise ValueError(f"bad name: {name!r}")
    if name in reserved_names:
        raise ValueError(f"name collision: {name!r}")

    best_in = style["best_in"]
    if not isinstance(best_in, dict):
        raise ValueError("best_in must be object")
    for platform in ("twitter", "reddit", "linkedin"):
        if platform not in best_in or not isinstance(best_in[platform], list):
            raise ValueError(f"best_in.{platform} must be a list")
    if not best_in["twitter"]:
        raise ValueError("best_in.twitter cannot be empty (source platform)")

    for field in ("description", "example", "note"):
        if not isinstance(style[field], str) or not style[field].strip():
            raise ValueError(f"{field} must be non-empty string")


def insert_style(conn, style, replies, prompt_chars):
    source_post_ids = [r["id"] for r in replies]
    window_end = datetime.now(timezone.utc)
    window_start_sql = "NOW() - INTERVAL '%s hours'" % WINDOW_HOURS
    gen_log = (
        f"Synthesized {window_end.isoformat(timespec='seconds')} from "
        f"top {len(replies)} human twitter replies in last {WINDOW_HOURS}h. "
        f"Prompt size: {prompt_chars} chars. "
        f"Reply id range: {min(source_post_ids)}-{max(source_post_ids)}."
    )

    cur = conn.execute(
        f"""
        INSERT INTO engagement_styles_human_derived
            (name, description, example, best_in, note,
             source_window_start, source_window_end,
             source_post_ids, generation_log, status)
        VALUES (%s, %s, %s, %s, %s,
                {window_start_sql}, NOW(),
                %s, %s, 'active')
        RETURNING id, name, generated_at
        """,
        (
            style["name"], style["description"], style["example"],
            json.dumps(style["best_in"]), style["note"],
            source_post_ids, gen_log,
        ),
    )
    row = cur.fetchone()
    conn.commit()
    return {"id": row[0], "name": row[1], "generated_at": row[2]}


def main():
    conn = get_conn()
    try:
        replies = fetch_top_human_replies(conn)
        if len(replies) < 3:
            sys.stderr.write(
                f"[generate_daily_human_style] only {len(replies)} replies "
                f"in last {WINDOW_HOURS}h; need >=3. Skipping.\n"
            )
            return 0
        reserved = load_existing_style_names(conn)
        prompt = build_prompt(replies, reserved)
        sys.stderr.write(
            f"[generate_daily_human_style] prompt {len(prompt)} chars, "
            f"{len(replies)} replies, reserved={len(reserved)} names\n"
        )
        text = call_claude(prompt)
        if not text.strip():
            sys.stderr.write("[generate_daily_human_style] empty claude output\n")
            return 1
        style = extract_json(text)
        validate_style(style, reserved)
        inserted = insert_style(conn, style, replies, len(prompt))
        print(json.dumps({
            "status": "ok",
            "inserted": {**inserted,
                         "generated_at": inserted["generated_at"].isoformat()},
            "source_count": len(replies),
            "source_likes_top": replies[0]["likes"],
            "source_likes_bottom": replies[-1]["likes"],
        }, indent=2))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
