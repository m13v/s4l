#!/usr/bin/env python3
"""Byte-diff harness: prove draft_prompt_core.render_twitter_prompt()
reproduces run-twitter-cycle.sh's PREP_PROMPT exactly.

How: extract the LIVE shell fragments (directive/divergence/persona
assembly, CORPUS_BLOCK, ALL_PROJECTS_JSON + GLOBAL_LEARNED_PREFS_JSON, and
the PREP_PROMPT heredoc) from skill/run-twitter-cycle.sh, eval them in a
sandboxed bash with sentinel ingredient values passed via environment, and
byte-compare against the Python renderer fed the same sentinels. Runs the
full arm x lane matrix. Exits non-zero with a unified diff on mismatch.

This is the pre-flip gate for migrating the shell onto the core, and stays
as a regression test until the heredoc is deleted from the shell (after
which the shell fragment no longer exists and this harness retires in favor
of golden-file checks on the core's own output).

Usage: python3 scripts/test_draft_prompt_core.py
"""

import difflib
import os
import re
import subprocess
import sys
import tempfile

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CYCLE_SH = os.path.join(REPO_DIR, "skill", "run-twitter-cycle.sh")
sys.path.insert(0, os.path.join(REPO_DIR, "scripts"))

SENTINELS = {
    "CANDIDATE_BLOCK": '\n---\nCandidate ID: 101\nURL: https://x.com/u/status/1\nAuthor: @tester (42 followers)\nText: sentinel "quoted" text with $pecial chars\nVirality: 12 | Delta (5min): 3 | Likes: 1 | RTs: 0 | Replies: 2 | Views: 99 | Age: 1.5h\nSearch query: sentinel topic\nProject match: sentinelproj\n',
    "MEDIA_BLOCK": "MEDIA sentinel block",
    "TOP_REPORT": "TOP_REPORT sentinel A",
    "TOP_REPORT_B": "TOP_REPORT sentinel B",
    "STYLES_BLOCK": "STYLES sentinel A",
    "STYLES_BLOCK_B": "STYLES sentinel B",
    "RECENT_SELF_BLOCK": "RECENT SELF sentinel",
    "PICKED_STYLE": "sentinel_style_a",
    "PICKED_MODE": "use",
    "PICKED_STYLE_B": "sentinel_style_b",
    "PICKED_MODE_B": "use",
    "BATCH_ID": "bytediff-batch",
    "TW_ENGINE_PREFIX": "",
}


def _read_cycle():
    with open(CYCLE_SH, "r", encoding="utf-8") as f:
        return f.read().split("\n")


def _extract(lines, start_re, end_re, include_end=True, start_idx=0):
    start = end = None
    for i in range(start_idx, len(lines)):
        if start is None and re.match(start_re, lines[i]):
            start = i
            continue
        if start is not None and re.match(end_re, lines[i]):
            end = i
            break
    if start is None or end is None:
        raise SystemExit(f"harness: fragment not found ({start_re!r} .. {end_re!r})")
    return "\n".join(lines[start:end + (1 if include_end else 0)])


def extract_fragments():
    lines = _read_cycle()
    # Directive + divergence + persona assembly: first unindented arm `if`
    # through the line before the anti-sameness comment.
    frag_directive = _extract(
        lines,
        r'^if \[ "\$S4L_DRAFT_PROMPT_VARIANT" = "treatment_v4" \]; then$',
        r"^# 2026-07-10 anti-sameness",
        include_end=False,
    )
    # ALL_PROJECTS_JSON + GLOBAL_LEARNED_PREFS_JSON: from the first assign
    # to the second closing `" ... || echo "{}")`.
    start = next(i for i, l in enumerate(lines) if l.startswith("ALL_PROJECTS_JSON=$(python3"))
    closes = [i for i, l in enumerate(lines) if l.endswith('|| echo "{}")') and i >= start]
    frag_json = "\n".join(lines[start:closes[1] + 1])
    # CORPUS_BLOCK assembly.
    frag_corpus = _extract(
        lines,
        r'^CORPUS_BLOCK=""$',
        r'^log "Phase 2b-prep: Claude reading threads',
        include_end=False,
    )
    # The PREP_PROMPT heredoc itself.
    frag_prompt = _extract(
        lines,
        r'^PREP_PROMPT="',
        r'^- Reply in the SAME LANGUAGE as the parent tweet\."$',
    )
    return frag_directive, frag_json, frag_corpus, frag_prompt


def shell_render(arm, lane):
    frag_directive, frag_json, frag_corpus, frag_prompt = extract_fragments()
    env = dict(os.environ)
    env.update(SENTINELS)
    env["S4L_DRAFT_PROMPT_VARIANT"] = arm
    env["S4L_REPO_DIR"] = REPO_DIR
    env["REPO_DIR"] = REPO_DIR
    env["SKILL_FILE"] = os.path.join(REPO_DIR, "SKILL.md")
    if lane:
        env["S4L_ACTIVE_LANE"] = lane
    else:
        env.pop("S4L_ACTIVE_LANE", None)
    # treatment_v4 blanks the top reports in the shell BEFORE the heredoc
    # (the if-block at the TOP_REPORT computation); emulate that gate here
    # since we don't eval the top_performers.py fragment (DB-dependent).
    if arm == "treatment_v4":
        env["TOP_REPORT"] = ""
        env["TOP_REPORT_B"] = ""
    script = "\n".join(
        [
            "set -u",
            "log() { :; }",
            frag_directive,
            frag_json,
            frag_corpus,
            frag_prompt,
            'printf \'%s\' "$PREP_PROMPT"',
        ]
    )
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
        f.write(script)
        path = f.name
    try:
        out = subprocess.run(["bash", path], env=env, capture_output=True, text=True, timeout=120)
        if out.returncode != 0:
            raise SystemExit(f"harness: shell fragment failed rc={out.returncode}\n{out.stderr[-2000:]}")
        return out.stdout
    finally:
        os.unlink(path)


def python_render(arm, lane):
    os.environ["S4L_DRAFT_PROMPT_VARIANT"] = arm
    if lane:
        os.environ["S4L_ACTIVE_LANE"] = lane
    else:
        os.environ.pop("S4L_ACTIVE_LANE", None)
    os.environ["S4L_REPO_DIR"] = REPO_DIR
    import importlib
    import draft_prompt_core
    importlib.reload(draft_prompt_core)
    ing = {
        "batch_id": SENTINELS["BATCH_ID"],
        "skill_file": os.path.join(REPO_DIR, "SKILL.md"),
        "repo_dir": REPO_DIR,
        "picked_style": SENTINELS["PICKED_STYLE"],
        "picked_mode": SENTINELS["PICKED_MODE"],
        "picked_style_b": SENTINELS["PICKED_STYLE_B"],
        "picked_mode_b": SENTINELS["PICKED_MODE_B"],
        "candidate_block": SENTINELS["CANDIDATE_BLOCK"],
        "media_block": SENTINELS["MEDIA_BLOCK"],
        "top_report": SENTINELS["TOP_REPORT"],
        "top_report_b": SENTINELS["TOP_REPORT_B"],
        "styles_block": SENTINELS["STYLES_BLOCK"],
        "styles_block_b": SENTINELS["STYLES_BLOCK_B"],
        "recent_self_block": SENTINELS["RECENT_SELF_BLOCK"],
        "prefix": SENTINELS["TW_ENGINE_PREFIX"],
        "arm": arm,
        "lane": lane or "",
    }
    return draft_prompt_core.render_twitter_prompt(ing)


def main():
    matrix = [
        ("control_v4", ""),
        ("treatment_v4", ""),
        ("control_v4", "personal_brand"),
        ("treatment_v4", "personal_brand"),
    ]
    failures = 0
    for arm, lane in matrix:
        want = shell_render(arm, lane)
        got = python_render(arm, lane)
        tag = f"arm={arm} lane={lane or 'promotion'}"
        if want == got:
            print(f"OK   {tag} ({len(want)} bytes)")
        else:
            failures += 1
            print(f"FAIL {tag}: shell={len(want)}B core={len(got)}B")
            diff = difflib.unified_diff(
                want.splitlines(keepends=True),
                got.splitlines(keepends=True),
                fromfile="shell", tofile="core", n=1,
            )
            shown = 0
            for d in diff:
                sys.stdout.write(d)
                shown += 1
                if shown > 80:
                    print("... (diff truncated)")
                    break
    if failures:
        print(f"\n{failures} case(s) FAILED")
        return 1
    print("\nAll cases byte-identical.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
