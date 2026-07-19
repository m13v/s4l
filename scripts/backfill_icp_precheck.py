#!/usr/bin/env python3
"""Backfill dms.icp_matches by scoring every replied thread against every
project that has a qualification block in config.json.

Scope: dms rows where message_count > 1 (prospect replied). By default only
rows with an empty icp_matches array are processed; pass --force to rescore
everything regardless.

For each row we ask claude-haiku to judge the prospect + conversation against
each project's must_have / disqualify lists, then upsert one icp_matches
entry per project via dm_conversation.set_icp_precheck.

Usage:
    python3 scripts/backfill_icp_precheck.py --dry-run
    python3 scripts/backfill_icp_precheck.py --platform reddit --limit 20
    python3 scripts/backfill_icp_precheck.py --force     # rescore all eligible rows
"""
import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as dbmod
from dm_conversation import set_icp_precheck

# THE canonical config loader (scripts/config.py): S4L_CONFIG_PATH / state-dir /
# S4L_REPO_DIR aware, mtime-cached. Replaces this file's hand-rolled loader and
# its hardcoded config path (the S4L-4H dead-path class on customer boxes).
import os as _cfg_os, sys as _cfg_sys
_cfg_sys.path.insert(0, _cfg_os.path.dirname(_cfg_os.path.abspath(__file__)))
from config import config_path as _canonical_config_path, load_config
CONFIG_PATH = _canonical_config_path()
LABELS = ["icp_match", "icp_miss", "disqualified", "unknown"]

RUBRIC = """You are an ICP qualifier. For each DM row you will score the prospect + conversation against EVERY listed project's qualification criteria and return one verdict per (dm, project) pair.

Labels:
- icp_match - prospect + conversation satisfy at least one must_have AND trigger NO disqualify item for that project.
- icp_miss - no must_have is clearly satisfied for that project, but nothing actively disqualifies them either.
- disqualified - the prospect clearly triggers at least one disqualify item for that project (competitor, wrong platform, enterprise procurement gate, etc.).
- unknown - not enough signal in profile or conversation to judge this project either way.

Rules:
- Be strict: if the profile is empty or shallow and the conversation is one generic line, label unknown, not icp_match.
- Disqualify beats match: a single clear disqualify item flips the verdict for that project.
- Score each (dm, project) pair independently; a prospect can be icp_match for one project and disqualified for another.
- Notes must be ONE short sentence (<= 140 chars) referencing specific evidence from the profile or conversation.
"""




def project_criteria_index(config):
    """{project_name: {must_have: [...], disqualify: [...]}} for all projects with qualification."""
    out = {}
    for p in config.get("projects", []) or []:
        name = p.get("name")
        q = p.get("qualification") or {}
        if not name or not q:
            continue
        mh = [x for x in (q.get("must_have") or []) if isinstance(x, str) and x.strip()]
        dq = [x for x in (q.get("disqualify") or []) if isinstance(x, str) and x.strip()]
        if mh or dq:
            out[name] = {"must_have": mh, "disqualify": dq}
    return out


def fetch_rows(conn, platform=None, limit=None, force=False):
    q = """
        SELECT d.id, d.platform, d.their_author, d.target_project,
               d.their_content, d.comment_context,
               pr.headline, pr.bio, pr.company, pr.role,
               pr.follower_count, pr.recent_activity, pr.notes,
               (SELECT string_agg(
                    CASE WHEN m.direction = 'inbound' THEN 'THEM' ELSE 'US' END
                    || ': ' || COALESCE(m.content, ''),
                    E'\n' ORDER BY m.message_at ASC)
                FROM dm_messages m WHERE m.dm_id = d.id) AS convo
        FROM dms d
        LEFT JOIN prospects pr ON pr.id = d.prospect_id
        WHERE d.message_count > 1
    """
    if not force:
        q += " AND d.icp_matches = '[]'::jsonb"
    params = []
    if platform in ("x", "twitter"):
        q += " AND d.platform IN ('x','twitter')"
    elif platform:
        q += " AND d.platform = %s"
        params.append(platform)
    q += " ORDER BY d.id"
    if limit:
        q += f" LIMIT {int(limit)}"
    return conn.execute(q, params).fetchall()


def render_projects(criteria):
    lines = ["## Projects to score against"]
    for name, c in criteria.items():
        lines.append(f"- {name}:")
        if c["must_have"]:
            lines.append(f"    must_have: {' ; '.join(c['must_have'])}")
        if c["disqualify"]:
            lines.append(f"    disqualify: {' ; '.join(c['disqualify'])}")
    return "\n".join(lines)


def render_row(row):
    parts = [
        f"DM #{row['id']} | platform={row['platform']} | target_project={row['target_project'] or 'none'}",
        "PROSPECT PROFILE:",
        f"  headline: {(row['headline'] or '').strip()}",
        f"  company: {(row['company'] or '').strip()}",
        f"  role: {(row['role'] or '').strip()}",
        f"  bio: {(row['bio'] or '').strip()}",
        f"  followers: {row['follower_count'] if row['follower_count'] is not None else 'unknown'}",
        f"  recent_activity: {(row['recent_activity'] or '').strip()}",
        f"  notes: {(row['notes'] or '').strip()}",
        "CONVERSATION:",
        (row["convo"] or ""),
    ]
    return "\n".join(parts)


def classify_one(row, criteria, project_names):
    prompt = "\n\n".join([
        RUBRIC,
        render_projects(criteria),
        "## DM row to score",
        render_row(row),
        (
            "Return ONLY a JSON array, one object per project in "
            f"{project_names}. Each object shape: "
            '{"project": "<project name>", "label": "<one of '
            f"{LABELS}>" + '", "notes": "<short sentence>"}. '
            "No prose, no markdown fences."
        ),
    ])

    # Route through run_claude.sh (2026-07-14) for session cost accounting;
    # tag icp-precheck is unmapped in TAG_TO_TYPE so this stays a direct
    # claude -p. The haiku pin is deliberate cost engineering for bulk
    # classification (NOT the global-default drift the no-hardcoded-model
    # rule targets); env-overridable so a central flip stays possible.
    _model = os.environ.get("S4L_CLASSIFY_MODEL") or "claude-haiku-4-5-20251001"
    _run_claude_sh = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_claude.sh")
    result = subprocess.run(
        ["bash", _run_claude_sh, "icp-precheck", "-p", "--model", _model],
        input=prompt,
        capture_output=True,
        text=True,
        timeout=240,
    )
    if result.returncode != 0:
        print(f"  claude exited {result.returncode}: stderr={result.stderr[:400]!r} stdout={result.stdout[:200]!r}",
              file=sys.stderr)
        return []
    text = result.stdout.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip("` \n")
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            parsed = [parsed]
        return parsed
    except Exception as e:
        print(f"  DM #{row['id']}: JSON parse failed: {e}\n  raw: {text[:400]}", file=sys.stderr)
        return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", choices=["reddit", "linkedin", "twitter", "x"], default=None)
    ap.add_argument("--limit", type=int, default=None, help="Max rows total")
    ap.add_argument("--force", action="store_true",
                    help="Rescore all eligible rows (default: only rows with empty icp_matches)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    dbmod.load_env()
    conn = dbmod.get_conn()
    config = load_config()
    criteria = project_criteria_index(config)
    if not criteria:
        print("ERROR: no projects with qualification blocks in config.json", file=sys.stderr)
        sys.exit(1)
    project_names = list(criteria.keys())
    print(f"Scoring against {len(project_names)} projects: {', '.join(project_names)}")

    rows = fetch_rows(conn, platform=args.platform, limit=args.limit, force=args.force)
    print(f"Rows to backfill: {len(rows)}")
    if not rows:
        return

    counts = {lbl: 0 for lbl in LABELS}
    total_entries = 0
    total_rows = 0
    for row in rows:
        verdicts = classify_one(row, criteria, project_names)
        if not verdicts:
            print(f"  DM #{row['id']}: no verdicts returned")
            continue
        by_project = {}
        for v in verdicts:
            proj = v.get("project")
            if proj in criteria:
                by_project[proj] = v
        for proj in project_names:
            v = by_project.get(proj)
            if not v:
                continue
            label = v.get("label")
            notes = (v.get("notes") or "").strip()
            if label not in LABELS:
                continue
            counts[label] += 1
            total_entries += 1
            print(f"  DM #{row['id']} [{row['platform']}] {proj} -> {label}: {notes}")
            if not args.dry_run:
                set_icp_precheck(conn, row["id"], label, proj, notes or None)
        if not args.dry_run:
            conn.commit()
        total_rows += 1
        print(f"  [{total_rows}/{len(rows)}] DM #{row['id']} done")
        time.sleep(0.3)

    print("\n=== Summary ===")
    for lbl in LABELS:
        print(f"  {lbl}: {counts[lbl]}")
    print(f"  Total entries written: {total_entries}")
    print(f"  Rows processed: {total_rows}/{len(rows)}")


if __name__ == "__main__":
    main()
