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

# What each arm MEANS, for humans reviewing cards: the details popover shows
# this next to the bare variant name (s4l_card._details_lines calls
# describe()). Keyed experiment name -> variant -> short description; one
# sentence, the popover wraps at ~300px. A missing entry degrades to the bare
# variant name, so unknown or future arms need no code change to render.
# Keep draft_prompt entries in sync with bin/server.js
# DRAFT_PROMPT_VARIANT_DEFS and the arm strings in run-twitter-cycle.sh.
DESCRIPTIONS = {
    "draft_b_source": {
        "human_derived": (
            "Draft B explore slot: style distilled from real top-performing "
            "human replies (daily synthesizer), least-used first"
        ),
        "model_invented": (
            "Draft B explore slot: freshly invented style from the "
            "standalone invention job (post 2026-07-10), least-used first"
        ),
        "scored_fallback": (
            "Draft B explore pool was empty; fell back to a second scored "
            "pick from the proven-style pool"
        ),
    },
    "draft_prompt": {
        "treatment_v3": (
            "style-as-form: the assigned style is the binding FORM (defining "
            "move + per-style length + self-check), learned preferences apply "
            "inside it; keeps the v2 skeleton ban"
        ),
        "control_v3": (
            "plain draft directive, uniform length clamp, no structure ban"
        ),
        # v2 arms (skeleton-ban) retired 2026-07-10; v1 arms
        # (decoupled-product-pivot) retired 2026-07-06; kept so any
        # straggler card from an old plan still explains itself.
        "treatment_v2": (
            "v2, retired: skeleton ban, forbids the concede-then-reverse "
            '"easy X / hard Y" structure and forces varied entry points'
        ),
        "control_v2": (
            "v2, retired: draft directive (style + product pivot), no structure ban"
        ),
        "treatment": "v1, retired: product pivot decoupled from the reply",
        "control": "v1, retired: original draft directive",
    },
    "voice_fidelity": {
        "treatment": (
            "persona lane: verbatim reply examples + persona corpus are ground "
            "truth for writing mechanics (capitalization, punctuation, "
            "contractions); voice.tone loses on conflict with them"
        ),
        "control": (
            "persona lane: today's directive, voice.tone treated as equal to "
            "voice.examples with no tie-break"
        ),
    },
    "lane": {
        "personal_brand": (
            "organic persona lane: first-hand voice, no product, no link, no CTA"
        ),
        "promotion": (
            "product lane: routes to the matched project and may carry a link"
        ),
    },
}


def describe(name, variant):
    """Human-readable meaning of an arm, or None when unknown."""
    try:
        return DESCRIPTIONS.get(str(name), {}).get(str(variant))
    except Exception:
        return None

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
    # 2026-07-06: the personal_brand persona directive is now ARM-AWARE in
    # run-twitter-cycle.sh (treatment_v3 adds the skeleton ban + two-layer
    # style/preferences contract, control_v3 does not), so the assigned
    # draft_prompt arm DOES touch persona
    # drafts. Keep it stamped so the arm surfaces on persona cards and the per-arm
    # readout covers both lanes. (Previously dropped here because the persona
    # directive overrode both arms wholesale; that is no longer the case.)
    return out


if __name__ == "__main__":
    print(json.dumps(collect(), indent=2, sort_keys=True))
    sys.exit(0)
