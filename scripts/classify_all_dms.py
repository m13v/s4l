#!/usr/bin/env python3
"""Batch-classify unlabeled DMs into interest_level buckets via `claude -p`.

Usage:
    python3 scripts/classify_all_dms.py --limit 10 --dry-run
    python3 scripts/classify_all_dms.py                # run all unlabeled

Uses `claude -p` CLI (OAuth subscription), not the API key. Requires
DATABASE_URL in .env.
"""
import argparse
import json
import os
import subprocess
import sys
import time

import psycopg2
import psycopg2.extras

LABELS = ["no_response", "general_discussion", "cold", "warm", "hot", "declined", "not_our_prospect"]

RUBRIC = """Classify the prospect's current interest in our products/topic based on the LATEST inbound message and the full conversation arc.

Labels (pick the single best fit):
- no_response — we messaged them and they never replied. This classifier only sees threads with at least one inbound message, so DO NOT pick this label here.
- general_discussion — default baseline AFTER they have replied, BEFORE any product-relevant signal has appeared. Early-stage threads where the topic hasn't yet touched anything our products solve, no product has been mentioned by either side, and you're still getting to know each other.
- hot — explicit buying or trial signals DIRECTED AT ONE OF OUR PRODUCTS (Terminator, Fazm, PieLine, Cyrano, vipassana.cool, Octolens): asked for the link/demo/trial/pricing for our product, said "tell me more" about our product, said they already use or want to use our product, booked a call to discuss our product, gave us an email for follow-up about our product. A call offer or demo request about THEIR own product/workflow/tooling is NOT hot — use warm (if relevant domain) or not_our_prospect.
- warm — engaged and problem-aware: asking substantive follow-up questions, describing their exact pain in detail, comparing tools, acknowledging the use case, multi-turn back-and-forth where they keep the thread alive AND the thread is in a domain one of our products could serve.
- cold — polite but shallow AFTER the conversation already touched a relevant topic: one-liners ("cool", "thanks", "will check it out", "interesting"), they disengaged from a thread that had product relevance, conversational small talk that used to have a product angle and no longer does. (If the thread never had a product angle, use general_discussion instead.)
- not_our_prospect — engaged but in the wrong direction: they're pitching US (offering services, leads, a sale), they treat us as a potential customer/buyer, they work in an unrelated domain, or it's a peer/colleague exchange with no realistic buyer fit.
- declined — explicit negative: "not interested", "stop messaging", "this isn't for me", confrontational tone, accused us of being a bot/spam, asked us to leave them alone.

Our projects: desktop automation (Terminator, Fazm/fazm.ai), AI voice for restaurants (PieLine), AI security camera detection (Cyrano), meditation (vipassana.cool), content engagement (Octolens).
"""

BATCH_SIZE = 25


def fetch_unlabeled(conn, limit=None):
    q = """
        SELECT d.id, d.platform, d.their_author, d.tier, d.project_name
        FROM dms d
        WHERE d.interest_level IS NULL
          AND EXISTS (SELECT 1 FROM dm_messages m WHERE m.dm_id = d.id AND m.direction = 'inbound')
        ORDER BY d.id
    """
    if limit:
        q += f" LIMIT {limit}"
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(q)
    return cur.fetchall()


def fetch_messages(conn, dm_id):
    cur = conn.cursor()
    cur.execute(
        "SELECT direction, author, content, message_at FROM dm_messages WHERE dm_id=%s ORDER BY message_at",
        (dm_id,),
    )
    return cur.fetchall()


def render_conversation(dm, msgs):
    lines = [
        f"DM #{dm['id']} | platform={dm['platform']} | their_author={dm['their_author']} | tier={dm['tier']} | project={dm['project_name'] or 'none'}"
    ]
    for d, a, c, t in msgs:
        prefix = "THEM" if d == "inbound" else "US"
        lines.append(f"  {prefix} ({a}): {(c or '').strip()}")
    return "\n".join(lines)


def classify_batch(batch):
    prompt_parts = [RUBRIC, "\n## Conversations to classify\n"]
    for dm, msgs in batch:
        prompt_parts.append(render_conversation(dm, msgs))
        prompt_parts.append("")
    ids = [dm["id"] for dm, _ in batch]
    prompt_parts.append(
        f"\nReturn ONLY a JSON object mapping each dm id to exactly one label from {LABELS}. "
        f"Ids to label: {ids}. "
        f'Format: {{"<id>": "<label>", ...}}. No prose, no markdown fences.'
    )
    prompt = "\n".join(prompt_parts)

    # Route through run_claude.sh (2026-07-14) for session cost accounting;
    # tag classify-dms is unmapped in TAG_TO_TYPE so this stays a direct
    # claude -p. The haiku pin is deliberate cost engineering for bulk
    # classification (NOT the global-default drift the no-hardcoded-model
    # rule targets); env-overridable so a central flip stays possible.
    _model = os.environ.get("S4L_CLASSIFY_MODEL") or "claude-haiku-4-5-20251001"
    _run_claude_sh = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_claude.sh")
    result = subprocess.run(
        ["bash", _run_claude_sh, "classify-dms", "-p", "--model", _model, prompt],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode != 0:
        print(f"  claude exited {result.returncode}: {result.stderr[:300]}", file=sys.stderr)
        return {}
    text = result.stdout.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip("` \n")
    try:
        return json.loads(text)
    except Exception as e:
        print(f"  JSON parse failed: {e}\n  raw: {text[:400]}", file=sys.stderr)
        return {}


def apply_labels(conn, labels):
    cur = conn.cursor()
    for dm_id, label in labels.items():
        if label not in LABELS:
            print(f"  SKIP id={dm_id}: invalid label '{label}'", file=sys.stderr)
            continue
        cur.execute(
            "UPDATE dms SET interest_level = %s WHERE id = %s AND interest_level IS NULL",
            (label, int(dm_id)),
        )
    conn.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="Max DMs to classify")
    ap.add_argument("--dry-run", action="store_true", help="Print labels but don't write to DB")
    args = ap.parse_args()

    db_url = os.environ["DATABASE_URL"]
    conn = psycopg2.connect(db_url)

    dms = fetch_unlabeled(conn, args.limit)
    print(f"Unlabeled DMs with inbound: {len(dms)}")
    if not dms:
        return

    batch = []
    total_labeled = 0
    counter = {lbl: 0 for lbl in LABELS}

    for i, dm in enumerate(dms):
        msgs = fetch_messages(conn, dm["id"])
        batch.append((dm, msgs))
        if len(batch) >= BATCH_SIZE or i == len(dms) - 1:
            labels = classify_batch(batch)
            for dm_id, lbl in labels.items():
                counter[lbl] = counter.get(lbl, 0) + 1
                if args.dry_run:
                    print(f"  DM #{dm_id} -> {lbl}")
            if not args.dry_run:
                apply_labels(conn, labels)
            total_labeled += len(labels)
            print(f"  Batch done ({total_labeled}/{len(dms)})")
            batch = []
            time.sleep(0.3)  # gentle on rate limit

    print("\n=== Summary ===")
    for lbl in LABELS:
        print(f"  {lbl}: {counter.get(lbl, 0)}")
    print(f"  Total labeled: {total_labeled}")


if __name__ == "__main__":
    main()
