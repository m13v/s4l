#!/usr/bin/env python3
"""Cross-language parity gate: constants and state machines that exist in BOTH
the Python pipeline and the Node/TS MCP server MUST agree. Each pair here has a
"keep in lockstep" comment at both definition sites; this test is what makes
that comment enforceable instead of aspirational. Runs in the release gate
(scripts/release-mcpb.sh) right after test_no_silent_fallbacks.py.

Checks:
  1. candidate_state parity — mcp/menubar/s4l_state.py::candidate_state (the
     Python authority) vs mcp/src/index.ts::candidateState (the TS mirror),
     evaluated with node on a fixture matrix covering every flag combination
     and truthiness edge case. Divergence between hand-rolled raw-flag filters
     and the state machine has produced real incidents (post_failed drain
     asymmetry 2026-07-17, get_stats lane blindness); this pins the two
     implementations to each other.
  2. Short-link fallback host parity — scripts/dm_short_links.py
     DEFAULT_FALLBACK_HOST vs bin/server.js SHORT_LINK_FALLBACK_HOST (CLAUDE.md:
     "keep them in sync").

Zero third-party deps. Needs `node` on PATH (the release script already does).
Exit 0 = all pass; exit 1 with a FAIL line = parity broken, do not release.
"""

import json
import os
import re
import shutil
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILURES = []


def report(ok, label, detail=""):
    tag = "PASS" if ok else "FAIL"
    line = f"{tag}  {label}"
    if detail:
        line += f"  -- {detail}"
    print(line)
    if not ok:
        FAILURES.append(label)


# ---------------------------------------------------------------------------
# 1. candidate_state parity
# ---------------------------------------------------------------------------

# Every flag combination that matters, plus truthiness edge cases: Python uses
# `is True` for posted/terminal/approved but bare truthiness for post_failed;
# TS uses `=== true` and bare truthiness respectively. The matrix locks in that
# BOTH implementations make the same call on non-boolean junk values too.
FIXTURES = [
    {},
    {"approved": True},
    {"approved": True, "post_failed": True},
    {"post_failed": True},
    {"post_failed": "posted_0"},
    {"post_failed": ""},
    {"post_failed": 0},
    {"terminal": True},
    {"terminal": True, "approved": True},
    {"terminal": True, "post_failed": True, "approved": True},
    {"posted": True},
    {"posted": True, "approved": True},
    {"posted": True, "terminal": True, "post_failed": True, "approved": True},
    {"posted": False, "terminal": False, "approved": False},
    {"posted": "yes"},
    {"approved": 1},
    {"terminal": 1, "approved": True},
    {"posted": 1, "terminal": True},
]


def python_states():
    sys.path.insert(0, os.path.join(REPO_ROOT, "mcp", "menubar"))
    from s4l_state import candidate_state  # noqa: E402
    return [candidate_state(dict(c)) for c in FIXTURES]


def extract_ts_function():
    """Pull the candidateState function body out of mcp/src/index.ts and strip
    the TS type annotations so plain node can evaluate it. Brittle on purpose:
    if the signature changes shape, this test fails loudly and must be updated
    alongside it -- which is exactly the review moment the lockstep needs."""
    src_path = os.path.join(REPO_ROOT, "mcp", "src", "index.ts")
    with open(src_path, encoding="utf-8") as f:
        src = f.read()
    m = re.search(r"function candidateState\(c: PlanCandidate\): CandidateState \{", src)
    if not m:
        return None
    start = m.start()
    depth = 0
    for i in range(m.end() - 1, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                body = src[start : i + 1]
                return body.replace(
                    "function candidateState(c: PlanCandidate): CandidateState {",
                    "function candidateState(c) {",
                )
    return None


def ts_states(fn_src):
    node = shutil.which("node") or "/opt/homebrew/bin/node"
    script = (
        fn_src
        + "\nconst fixtures = JSON.parse(require('fs').readFileSync(0, 'utf-8'));"
        + "\nprocess.stdout.write(JSON.stringify(fixtures.map((c) => candidateState(c))));"
    )
    out = subprocess.run(
        [node, "-e", script],
        input=json.dumps(FIXTURES),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if out.returncode != 0:
        raise RuntimeError(f"node evaluation failed: {out.stderr.strip()[:300]}")
    return json.loads(out.stdout)


def check_candidate_state():
    fn_src = extract_ts_function()
    if fn_src is None:
        report(False, "candidateState() found in mcp/src/index.ts",
               "signature changed? update extract_ts_function() alongside it")
        return
    try:
        ts = ts_states(fn_src)
    except Exception as e:
        report(False, "candidateState() evaluates under node", str(e)[:200])
        return
    py = python_states()
    for c, p, t in zip(FIXTURES, py, ts):
        report(p == t, f"candidate_state parity for {json.dumps(c)}",
               f"py={p} ts={t}")


# ---------------------------------------------------------------------------
# 2. short-link fallback host parity
# ---------------------------------------------------------------------------

def check_fallback_host():
    py_path = os.path.join(REPO_ROOT, "scripts", "dm_short_links.py")
    js_path = os.path.join(REPO_ROOT, "bin", "server.js")
    py_m = js_m = None
    try:
        with open(py_path, encoding="utf-8") as f:
            py_m = re.search(r"^DEFAULT_FALLBACK_HOST\s*=\s*['\"]([^'\"]+)['\"]",
                             f.read(), re.M)
        with open(js_path, encoding="utf-8") as f:
            js_m = re.search(r"^const SHORT_LINK_FALLBACK_HOST\s*=\s*['\"]([^'\"]+)['\"]",
                             f.read(), re.M)
    except OSError as e:
        report(False, "fallback-host files readable", str(e))
        return
    if not py_m or not js_m:
        report(False, "fallback-host constants found",
               f"py={'ok' if py_m else 'MISSING'} js={'ok' if js_m else 'MISSING'}")
        return
    report(py_m.group(1) == js_m.group(1),
           "DEFAULT_FALLBACK_HOST (py) == SHORT_LINK_FALLBACK_HOST (js)",
           f"py={py_m.group(1)} js={js_m.group(1)}")


def main():
    print("-- check 1: candidate_state TS/Python parity --")
    check_candidate_state()
    print("-- check 2: short-link fallback host parity --")
    check_fallback_host()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S)")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
