#!/usr/bin/env python3
"""
apply_onboarding_selections.py -- PROTOTYPE handler for the S4L bundled-question
widget. Turns the widget's sendPrompt confirmation into concrete actions:

  1. Engagement lanes -> saps_mode.py enable/disable (DRY-RUN by default so a
     prototype never flips the LIVE autopilot; pass --commit-lanes to really run).
  2. History consent   -> history_context.set_optin(...) (persisted sidecar).
  3. If consent == yes  -> history_context.pull(project) and summarize candidates.

The widget sends a line like:
  "... personal_brand lane: ON, product lane: OFF, read past Claude
   conversations: YES. Apply these ..."

so this handler also accepts that raw text via --from-prompt, or explicit flags.

Usage:
  python3 scripts/apply_onboarding_selections.py --project S4L \
      --personal-brand on --product off --read-history yes
  python3 scripts/apply_onboarding_selections.py --project S4L \
      --from-prompt "personal_brand lane: ON, product lane: OFF, read past Claude conversations: YES"
  # add --commit-lanes to actually toggle saps_mode (default is dry-run)
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import history_context as hc  # noqa: E402

S4L_MODE = Path(__file__).resolve().parent / "saps_mode.py"


def parse_from_prompt(text: str) -> dict:
    """Extract the three toggles from the widget's confirmation sentence."""
    t = text.lower()

    def flag(label: str, on_words=("on", "yes")) -> bool | None:
        m = re.search(re.escape(label) + r"[^:]*:\s*(on|off|yes|no)", t)
        return None if not m else m.group(1) in on_words

    return {
        "personal_brand": flag("personal_brand"),
        "product_mode": flag("product lane"),
        "read_history": flag("read past claude conversations"),
    }


def apply_lanes(personal_brand: bool, product: bool, commit: bool) -> list[str]:
    """Return the saps_mode commands, running them only when commit=True."""
    plan = [
        [sys.executable, str(S4L_MODE),
         "enable" if personal_brand else "disable", "personal_brand"],
        [sys.executable, str(S4L_MODE),
         "enable" if product else "disable", "promotion"],
    ]
    rendered = [" ".join(c) for c in plan]
    if commit:
        for c in plan:
            subprocess.run(c, check=False)
    return rendered


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", required=True)
    ap.add_argument("--from-prompt", help="raw widget confirmation text")
    ap.add_argument("--personal-brand", choices=["on", "off"])
    ap.add_argument("--product", choices=["on", "off"])
    ap.add_argument("--read-history", choices=["yes", "no"])
    ap.add_argument("--commit-lanes", action="store_true",
                    help="actually toggle saps_mode (default: dry-run print only)")
    args = ap.parse_args()

    sel = {"personal_brand": None, "product_mode": None, "read_history": None}
    if args.from_prompt:
        sel.update({k: v for k, v in parse_from_prompt(args.from_prompt).items()
                    if v is not None})
    if args.personal_brand:
        sel["personal_brand"] = args.personal_brand == "on"
    if args.product:
        sel["product_mode"] = args.product == "on"
    if args.read_history:
        sel["read_history"] = args.read_history == "yes"

    for k in sel:
        if sel[k] is None:
            raise SystemExit(f"missing selection for '{k}'")

    out = {"project": args.project, "selections": sel, "actions": {}}

    lane_cmds = apply_lanes(sel["personal_brand"], sel["product_mode"],
                            commit=args.commit_lanes)
    out["actions"]["lanes"] = {
        "committed": args.commit_lanes,
        "commands": lane_cmds,
    }

    optin = hc.set_optin(sel["read_history"])
    out["actions"]["history_optin"] = optin

    if sel["read_history"]:
        pull = hc.pull(args.project, terms=None, limit=40)
        if pull.get("ok"):
            summary = {
                "sessions": pull["session_count"],
                "snippets": pull["snippet_count"],
                "scope": pull["scope"],
                "sample": [
                    {"session": sid[:8],
                     "recent_previews": [s["preview"][:140] for s in snips[:2]]}
                    for sid, snips in list(pull["sessions"].items())[:5]
                ],
            }
            out["actions"]["history_pull"] = summary
        else:
            out["actions"]["history_pull"] = pull
    else:
        out["actions"]["history_pull"] = {"skipped": "consent=no"}

    print(json.dumps(out, indent=2)[:6000])


if __name__ == "__main__":
    main()
