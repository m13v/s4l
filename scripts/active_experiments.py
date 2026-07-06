#!/usr/bin/env python3
"""THE generic structure for surfacing experiment/scenario arms on draft cards.

Problem this solves (2026-07-06): experiments used to be invisible ad-hoc
plumbing. Each one was an env var assigned inside the locked cycle script
(S4L_DRAFT_PROMPT_VARIANT, historically LENGTH_ARM) or a per-post coin flip at
post time (tail_link_variant), stamped straight onto a dedicated posts column
at post time and NEVER onto the plan candidate. So the review card could not
show which arms shaped a draft, and every new experiment needed bespoke wiring.

Convention (REQUIRED for every new experiment that affects drafting):
  1. Assign the arm wherever it naturally lives.
  2. Make it discoverable by AT LEAST ONE of:
       a. export S4L_EXP_<NAME>=<variant> in the process chain that runs the
          cycle/merge (name is lowercased for display), or
       b. print ONE machine-readable marker line to the cycle's stdout:
              S4L_EXPERIMENT name=<name> variant=<variant>
  3. Done. merge_review_queue.py calls collect() and stamps every incoming
     plan candidate with an `experiments` {name: variant} dict, the menu bar
     card's details popover renders every entry automatically, and the dict
     rides the candidate into review-queue.json for any later reader. Zero
     per-experiment wiring in the card.

Post-time experiments (e.g. tail_link_variant, decided by a coin flip inside
twitter_post_plan.py AFTER approval) have no arm yet at review time and are out
of scope here by design.

LEGACY_TEXT below recovers the draft-prompt arm from the LOCKED cycle script's
existing human log line. That file is chflags-uchg locked, which freezes the
line's format, so parsing it is stable by construction. If the cycle is ever
unlocked and edited, prefer switching it to the S4L_EXPERIMENT marker.
"""

import json
import os
import re
import sys

ENV_PREFIX = "S4L_EXP_"

# Experiments that predate the S4L_EXP_ convention, keyed by their env var.
# S4L_CYCLE_LANE is the dual-lane scenario (personal_brand | promotion) from
# s4l_mode.py env; not an A/B arm strictly, but it is THE scenario that decides
# which draft directive a card came from, so review wants it.
LEGACY_ENV = {
    "S4L_DRAFT_PROMPT_VARIANT": "draft_prompt",
    "S4L_CYCLE_LANE": "lane",
}

MARKER_RE = re.compile(
    r"^S4L_EXPERIMENT\s+name=([\w.-]+)\s+variant=(\S+)\s*$", re.MULTILINE
)

# Arms recoverable only from the locked cycle's stdout (see module docstring).
LEGACY_TEXT = (
    # run-twitter-cycle.sh: log "Draft-prompt A/B arm: <arm> (rate=<r>)"
    (re.compile(r"Draft-prompt A/B arm:\s+(\S+)"), "draft_prompt"),
)


def collect(text=None, env=None):
    """{experiment_name: variant} for the current cycle. `text` is the cycle's
    captured stdout (optional); env entries win over text on conflict because
    env is same-process truth while text is recovered from a child process."""
    env = os.environ if env is None else env
    out = {}
    if text:
        for rex, name in LEGACY_TEXT:
            m = rex.search(text)
            if m:
                out[name] = m.group(1)
        for m in MARKER_RE.finditer(text):
            out[m.group(1)] = m.group(2)
    for var, name in LEGACY_ENV.items():
        v = (env.get(var) or "").strip()
        if v:
            out[name] = v
    for var, v in env.items():
        if var.startswith(ENV_PREFIX) and (v or "").strip():
            out[var[len(ENV_PREFIX):].lower()] = v.strip()
    # The personal_brand lane REPLACES the draft directive wholesale (lane
    # override in run-twitter-cycle.sh runs AFTER the A/B arm is picked), so
    # the assigned draft_prompt arm never touched these drafts; stamping it
    # would mislead the reviewer and pollute any per-arm readout built on it.
    if out.get("lane") == "personal_brand":
        out.pop("draft_prompt", None)
    return out


def main(argv):
    import argparse

    ap = argparse.ArgumentParser(
        description="Print the active experiment arms as JSON"
    )
    ap.add_argument(
        "--text-file",
        help="path to captured cycle stdout to also scan for arm markers",
    )
    ns = ap.parse_args(argv)
    text = None
    if ns.text_file:
        try:
            with open(ns.text_file, "r", errors="replace") as f:
                text = f.read()
        except OSError as e:
            print(f"[active_experiments] cannot read {ns.text_file}: {e}",
                  file=sys.stderr)
    print(json.dumps(collect(text=text), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
