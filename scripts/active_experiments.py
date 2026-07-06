#!/usr/bin/env python3
"""THE generic structure for experiment/scenario arms on drafts.

How arms flow (2026-07-07, stamp-at-source design):
  1. An experiment assigns its arm wherever it naturally lives and exports it
     as an env var in the cycle process (see convention below).
  2. run-twitter-cycle.sh's plan writer calls collect() IN the cycle process
     and stamps an `experiments` {name: variant} dict onto every plan
     candidate as the plan is written. That per-candidate record is the ONLY
     source downstream — there are no env reads and no fallbacks later:
       - merge_review_queue.py carries it into review-queue.json,
       - the menu bar card's details popover renders every entry as-is,
       - twitter_post_plan.py stamps posts.draft_prompt_variant from it in
         BOTH posting homes (inside the cycle on autopilot, and spawned by
         the MCP server for approved cards, where cycle env never existed).

Convention (REQUIRED for every new experiment that affects drafting):
  export S4L_EXP_<NAME>=<variant> in the process chain that runs the cycle
  (the name is lowercased for display). Nothing else to wire: the plan writer
  stamps it, the card shows it, and any post-time consumer can read it off
  the candidate. Do NOT add per-experiment code to s4l_card.py or new env
  reads to twitter_post_plan.py.

Post-time experiments (e.g. tail_link_variant, coin-flipped inside
twitter_post_plan.py AFTER approval) have no arm at draft time and are out of
scope here by design.
"""

import json
import os
import sys

ENV_PREFIX = "S4L_EXP_"

# Experiments/scenarios that predate the S4L_EXP_ convention, keyed by env
# var. Order matters where two vars map to one name: later entries win, so
# S4L_CYCLE_LANE (the wrapper's authoritative per-cycle lane tag) overrides
# S4L_ACTIVE_LANE (set only on persona cycles; also present on direct cycle
# runs that bypass the wrapper). `lane` is the dual-lane scenario
# (personal_brand | promotion) — not an A/B arm strictly, but it decides
# which draft directive a card came from, so review wants it.
LEGACY_ENV = {
    "S4L_ACTIVE_LANE": "lane",
    "S4L_CYCLE_LANE": "lane",
    "S4L_DRAFT_PROMPT_VARIANT": "draft_prompt",
}


def collect(env=None):
    """{experiment_name: variant} from the calling process's environment.
    Must run in the process where arms are assigned (the cycle); downstream
    processes read the stamped candidate dict instead of calling this."""
    env = os.environ if env is None else env
    out = {}
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


if __name__ == "__main__":
    print(json.dumps(collect(), indent=2, sort_keys=True))
    sys.exit(0)
