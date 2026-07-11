#!/usr/bin/env python3
"""scripts/invent_styles.py — standalone daily engagement-style invention.

Architectural split (2026-07-10, mirrors the 2026-05-28 invent_topics.py
split): in-cycle style invention (the picker's 5% INVENT_RATE roll) is
retired. It ran INSIDE the drafting prep session, with the top-performers
leaderboard and winner exemplars in context, so every "new" style was a
renamed variant of the currently-winning move (agree-then-relocate /
concede-then-reverse). Combined with name-only dedup in register_style,
the registry accumulated hundreds of semantic clones.

This job runs OUTSIDE any drafting context, on the operator Mac only:

  - The registry is GLOBAL across installs (every install reads every
    style), so invention must NOT fan out per install the way topic
    invention does. One central daily run is the correct scope; that is
    the deliberate difference from invent_topics.py's per-install kicker.
  - VERBALIZED SAMPLING (2026-07-10, arXiv 2510.01171): a single "invent
    one style" ask reliably returns the modal answer (typicality bias from
    preference training), which is why 653 of ~950 registered styles
    cluster at 53-98 target_chars and one rhetorical family. Instead the
    prompt asks for N_CANDIDATES candidate styles WITH a verbalized
    typicality probability each, and selection takes the LOWEST-probability
    candidate that survives dedup: we sample the tail of the distribution,
    not the mode. This is the principled fix; the family blocklist below is
    only a backstop.
  - The existing registry is framed as OCCUPIED NICHES to stay out of
    (quality-diversity framing, arXiv 2310.13032), NOT as reference
    inspiration; showing top performers as exemplars is what anchored the
    retired inline path to the champion family.
  - target_chars DIVERSITY: the prompt shows live length-band occupancy
    and requires candidates to spread across bands, biased to the
    least-occupied ones, so invention stops minting 80-char styles.
  - Post-hoc semantic dedup (token-Jaccard on description+example, plus a
    reframe-family heuristic) rejects near-clones; when every candidate is
    rejected we re-prompt with a grown avoid-list, at most DUPE_RETRIES
    times.
  - Accepted styles are registered via engagement_styles.register_style
    (kind='model_invented'), same as the retired inline path, so pickers
    see them on their next tick with zero other wiring.

Scheduling: launchd com.m13v.s4l-invent-styles (operator Mac, daily).
Uses run_claude.sh with tag 'invent-styles'; the tag is NOT in
claude_job.py TAG_TO_TYPE, so it runs the local `claude -p` lane and has
no dependency on the Desktop queue worker.

CLI:
  python3 scripts/invent_styles.py                 # invent + register 1
  python3 scripts/invent_styles.py --max-new 2
  python3 scripts/invent_styles.py --dry-run       # propose, don't register
"""

import argparse
import json
import os
import re
import subprocess
import sys

_REPO_DIR = os.path.expanduser("~/social-autoposter")
sys.path.insert(0, os.path.join(_REPO_DIR, "scripts"))
from engagement_styles import get_all_styles, register_style  # noqa: E402

_RUN_CLAUDE_SH = os.path.join(_REPO_DIR, "scripts", "run_claude.sh")
SCRIPT_TAG = "invent-styles"
CALL_TIMEOUT_SEC = 420
DUPE_RETRIES = 3
SIMILARITY_THRESHOLD = 0.5  # Jaccard on description+example tokens
N_CANDIDATES = 5            # verbalized-sampling fan-out per call

# Length bands for target_chars diversity. Occupancy is computed live from
# the registry and shown in the prompt; candidates must spread across bands
# and bias toward the least-occupied ones.
LENGTH_BANDS = [(30, 60), (60, 100), (100, 150), (150, 200), (200, 250)]

# Heuristic markers of the saturated agree-then-relocate/reframe family.
# A proposal whose description/example leans on these is a clone of the
# dominant move no matter how novel its name is; reject like a dupe.
_REFRAME_MARKERS = re.compile(
    r"\b(reframe|relocat\w+|the real (work|question|problem|cost|part|win)"
    r"|hidden (cost|part|work|meter)|easy (part|half)|hard(er)? (part|half)"
    r"|was never (the|about)|nobody (mentions|talks about|tracks|measures)"
    r"|isn.t the .{0,30}it.s|concede)\b",
    re.I,
)


def _tokens(text):
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _jaccard(a, b):
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _style_text(entry):
    return f"{entry.get('description') or ''} {entry.get('example') or ''}"


def _find_near_dupe(proposal, universe):
    """Return (reason, existing_name) when the proposal is a clone; else None."""
    name = proposal["name"]
    if name in universe:
        return ("name_exists", name)
    ptext = _style_text(proposal)
    if _REFRAME_MARKERS.search(f"{proposal.get('description','')} {name}"):
        return ("reframe_family", "(dominant agree-then-relocate family)")
    best_name, best_sim = None, 0.0
    for ename, entry in universe.items():
        sim = _jaccard(ptext, _style_text(entry))
        if sim > best_sim:
            best_name, best_sim = ename, sim
    if best_sim >= SIMILARITY_THRESHOLD:
        return (f"jaccard={best_sim:.2f}", best_name)
    return None


def band_occupancy(universe):
    """[(lo, hi, count), ...] for LENGTH_BANDS over the registry's
    target_chars. The live histogram the prompt shows so candidates can
    claim underrepresented bands."""
    counts = [0] * len(LENGTH_BANDS)
    for e in universe.values():
        try:
            tc = int(e.get("target_chars") or 0)
        except (TypeError, ValueError):
            continue
        for i, (lo, hi) in enumerate(LENGTH_BANDS):
            if lo <= tc < hi:
                counts[i] += 1
                break
    return [(lo, hi, counts[i]) for i, (lo, hi) in enumerate(LENGTH_BANDS)]


def build_prompt(universe, avoid):
    names = sorted(universe.keys())
    # Detail only a bounded sample (prompt-size guard): the seeds plus the
    # most recently registered rows carry enough signal about what already
    # exists; the full NAME list covers the rest for the model's self-check.
    detailed = []
    for name in names:
        e = universe[name]
        if e.get("kind") == "seed" or len(detailed) < 40:
            desc = " ".join((e.get("description") or "").split())[:180]
            detailed.append(f"- {name}: {desc}")
        if len(detailed) >= 60:
            break
    avoid_block = ""
    if avoid:
        avoid_block = (
            "\nAlready proposed and REJECTED this run (do not resubmit or "
            "paraphrase): " + ", ".join(avoid) + "\n"
        )
    bands = band_occupancy(universe)
    band_lines = "\n".join(
        f"- {lo}-{hi} chars: {count} existing styles"
        f"{'  <- UNDERREPRESENTED, prefer this band' if count == min(c for _, _, c in bands) else ''}"
        for lo, hi, count in bands
    )
    return f"""You maintain the engagement-style registry for a social reply system. A style is a named rhetorical TEMPLATE (description + one example reply) that drafters follow when writing short replies on X/Twitter and Reddit.

The registry below is OCCUPIED TERRITORY, not inspiration. Your job is to find empty niches: structural families and length bands that no existing style covers. Do not riff on what is already there.

The registry is saturated with ONE structural family: agree-then-relocate (concede the surface point, move the spotlight to the hidden/harder/unmeasured part, often "X is easy, Y is the real work" or "X was never the point, Y is"). Do NOT propose any member of that family, however disguised.

EXISTING STYLE NAMES ({len(names)} total):
{", ".join(names)}

REPRESENTATIVE DETAILS (occupied niches, sample):
{chr(10).join(detailed)}

LENGTH-BAND OCCUPANCY (live registry histogram of target_chars):
{band_lines}
{avoid_block}
TASK (verbalized sampling): generate {N_CANDIDATES} CANDIDATE styles. For each, verbalize a `probability`: how likely this exact style would be as a language model's single first answer to "invent a new reply style" (0.0-1.0, honest, they need not sum to 1). Deliberately include tail candidates: at least 3 of the {N_CANDIDATES} must have probability under 0.10, meaning genuinely atypical moves a model would almost never produce first. We will programmatically select from the LOW-probability tail, so the obvious candidates are effectively discards; put your creativity into the tail.

Diversity requirements across the {N_CANDIDATES} candidates:
- Each from a DIFFERENT structural family. Families worth mining (or others you identify): direct answer with zero framing; first-person confession of a specific failure; pure curious question with no thesis; dry understatement one-liner; enthusiastic cosign with one concrete addition; flat disagreement stated plainly without conceding anything first; a tiny numbered checklist; a vivid analogy that does NOT end in a lesson; deadpan humor riffing on the thread's wording.
- Each in a DIFFERENT length band from the histogram above, biased toward the least-occupied bands. `target_chars` must be the actual length of your example, and the example must genuinely inhabit its band (a 200-char style is narrative, not a padded one-liner).

Per-candidate rules:
- `description` must state the style's DEFINING MOVE (the one thing every draft in this style must contain) and its OPENING (how the first words enter: e.g. lowercase noun, a number, the question itself). A future model reading only the description must know exactly what shape to write.
- `note` states when to use / when not to, and how a product mention would enter this style if ever (one clause; the link/CTA layer is downstream, so no URLs or link mechanics).
- The example must read like a real human reply (lowercase ok), NO links, NO product names.
- The style must be usable across many products and threads, not thread-specific.
- Every candidate is dedup-checked by token similarity against every existing style's description+example; if you cannot field {N_CANDIDATES} genuinely different moves, return the saturation envelope instead of forcing paraphrases.

Answer with ONLY one JSON object, no prose, in one of these two shapes:
{{"candidates": [{{"name": "snake_case_name", "description": "...", "example": "...", "note": "...", "why_existing_didnt_fit": "...", "target_chars": <int>, "probability": <float>}}, ...]}}
{{"saturated": true, "reason": "..."}}"""


def call_claude(prompt):
    proc = subprocess.run(
        ["bash", _RUN_CLAUDE_SH, SCRIPT_TAG, "-p", "--output-format", "json",
         prompt],
        text=True, capture_output=True, timeout=CALL_TIMEOUT_SEC,
    )
    if proc.returncode == 79:
        raise SystemExit("[invent_styles] provider blocked (exit 79); skipping run")
    if proc.returncode != 0:
        raise SystemExit(
            f"[invent_styles] run_claude.sh exited {proc.returncode}: "
            f"{(proc.stderr or '')[:400]}")
    try:
        envelope = json.loads(proc.stdout)
        text = envelope.get("result") or ""
    except json.JSONDecodeError:
        text = proc.stdout
    return text


def _extract_json(text):
    """Pull the first parseable JSON object out of model output."""
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    start = text.find("{")
    while start != -1:
        for end in range(len(text), start, -1):
            try:
                obj = json.loads(text[start:end])
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
        start = text.find("{", start + 1)
    return None


def main():
    parser = argparse.ArgumentParser(description="Standalone style invention job")
    parser.add_argument("--max-new", type=int, default=1,
                        help="How many styles to invent this run (default 1)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Propose and dedup-check but do not register")
    args = parser.parse_args()

    registered = 0
    for slot in range(max(1, args.max_new)):
        universe = get_all_styles()
        avoid = []
        accepted = None
        for attempt in range(1 + DUPE_RETRIES):
            raw = call_claude(build_prompt(universe, avoid))
            obj = _extract_json(raw)
            if not obj:
                print(f"[invent_styles] slot={slot} attempt={attempt} "
                      f"unparseable output: {raw[:200]!r}", file=sys.stderr)
                continue
            if obj.get("saturated"):
                print(f"[invent_styles] slot={slot} model reports saturation: "
                      f"{obj.get('reason', '')[:200]}", file=sys.stderr)
                break
            candidates = obj.get("candidates")
            if not isinstance(candidates, list) or not candidates:
                print(f"[invent_styles] slot={slot} attempt={attempt} no "
                      f"candidates array in output", file=sys.stderr)
                continue
            # Verbalized-sampling selection: walk the tail first (ascending
            # verbalized probability = least typical candidate first) and
            # take the first that survives dedup. The modal candidates at
            # the top of the distribution only get a chance if every tail
            # candidate is a clone.
            def _prob(c):
                try:
                    return float(c.get("probability"))
                except (TypeError, ValueError):
                    return 1.0  # unparseable prob sorts as maximally typical
            for cand in sorted(candidates, key=_prob):
                name = str(cand.get("name") or "").strip()
                if not re.fullmatch(r"[a-z0-9_]{3,60}", name):
                    print(f"[invent_styles] slot={slot} bad name {name!r}; "
                          f"skipping candidate", file=sys.stderr)
                    avoid.append(name or "(unnamed)")
                    continue
                dupe = _find_near_dupe({**cand, "name": name}, universe)
                if dupe:
                    reason, existing = dupe
                    print(f"[invent_styles] slot={slot} rejected {name!r} "
                          f"(p={_prob(cand):.2f}): {reason} vs {existing}",
                          file=sys.stderr)
                    avoid.append(name)
                    continue
                accepted = {**cand, "name": name}
                print(f"[invent_styles] slot={slot} selected tail candidate "
                      f"{name!r} (p={_prob(cand):.2f}) from "
                      f"{len(candidates)} candidates", file=sys.stderr)
                break
            if accepted:
                break
        if not accepted:
            continue
        if args.dry_run:
            print(json.dumps({"dry_run": True, **accepted}, indent=2))
            continue
        status, entry = register_style(
            accepted["name"],
            {
                "description": accepted.get("description", ""),
                "example": accepted.get("example", ""),
                "note": accepted.get("note", ""),
                "why_existing_didnt_fit": accepted.get("why_existing_didnt_fit", ""),
                "target_chars": accepted.get("target_chars"),
            },
            source_post={"platform": "invent_styles_job", "model": "invent_styles"},
        )
        print(f"[invent_styles] slot={slot} register status={status} "
              f"name={accepted['name']}", file=sys.stderr)
        if status == "new":
            registered += 1
            print(json.dumps({"registered": accepted["name"],
                              "description": accepted.get("description", "")}))
    print(f"[invent_styles] done: registered={registered}", file=sys.stderr)


if __name__ == "__main__":
    main()
