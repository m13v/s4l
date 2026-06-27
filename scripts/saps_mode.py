#!/usr/bin/env python3
"""Single source of truth for the engagement MODE (2026-06-26).

The S4L pipeline drafts in one of two modes, flipped by a menu-bar toggle:

  - "promotion"      (default): the normal project/product-marketing pipeline.
                     The weighted pick (pick_project.py) chooses among enabled
                     projects; replies carry the project's link per the A/B gate.
  - "personal_brand": pure organic engagement to grow the user's personal brand.
                     The cycle is forced onto the persona project (the config
                     entry with `"persona": true`, normally `enabled:false` so the
                     promotion pick never touches it) and replies are link-free.

State lives in ONE small file, `$SAPS_STATE_DIR/mode.json`:
    {"mode": "personal_brand"}        # or "promotion"

This module is the only reader/writer of that file plus the persona resolver, so
the menu bar (writer), the cycle wrapper (env), and any CLI all agree.

The toggle takes effect WITHOUT touching any locked pipeline file: the unlocked
wrapper `skill/run-draft-and-publish.sh` evals `saps_mode.py env` right before it
invokes the locked `run-twitter-cycle.sh`, exporting the two env vars the locked
pipeline already honors:
    SAPS_FORCE_PROJECT   -> pick_project.py forces this exact project (--project
                            bypasses the enabled gate), so a disabled persona is
                            still selectable in personal_brand mode.
    TWITTER_TAIL_LINK_RATE=0 -> twitter_post_plan.py ships every reply bare.

Usage:
    saps_mode.py get                 # print current mode
    saps_mode.py set personal_brand  # set mode (personal_brand | promotion)
    saps_mode.py toggle              # flip to the other mode, print the new one
    saps_mode.py env                 # print shell `export` lines for the cycle
    saps_mode.py persona-name        # print the persona project name (or empty)
"""

import json
import os
import shlex
import sys
from pathlib import Path

PROMOTION = "promotion"
PERSONAL_BRAND = "personal_brand"
VALID_MODES = (PROMOTION, PERSONAL_BRAND)
DEFAULT_MODE = PROMOTION


def state_dir() -> Path:
    # Mirrors mcp/src/index.ts sapsStateDir() and menubar/s4l_state.py state_dir().
    return Path(
        os.environ.get("SAPS_STATE_DIR")
        or (Path.home() / ".social-autoposter-mcp")
    )


def mode_file() -> Path:
    return state_dir() / "mode.json"


def config_path() -> Path:
    # Match the locked pipeline's resolution: SAPS_REPO_DIR/config.json when set,
    # else the canonical ~/social-autoposter/config.json (what pick_project.py /
    # project_topics.py read directly).
    repo = os.environ.get("SAPS_REPO_DIR")
    if repo:
        p = Path(repo) / "config.json"
        if p.exists():
            return p
    return Path.home() / "social-autoposter" / "config.json"


def get_mode() -> str:
    """Current mode, defaulting to promotion (preserves prior behavior)."""
    try:
        data = json.loads(mode_file().read_text())
        m = str(data.get("mode") or "").strip()
        return m if m in VALID_MODES else DEFAULT_MODE
    except Exception:
        return DEFAULT_MODE


def set_mode(mode: str) -> str:
    mode = (mode or "").strip()
    if mode not in VALID_MODES:
        raise ValueError(
            f"invalid mode {mode!r}; expected one of {VALID_MODES}"
        )
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = mode_file().with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"mode": mode}))
    tmp.replace(mode_file())
    return mode


def _load_projects() -> list:
    try:
        cfg = json.loads(config_path().read_text())
        return cfg.get("projects") or []
    except Exception:
        return []


def persona_name() -> str:
    """Name of the persona project (the entry with `persona: true`), or ''.

    First match wins. Returns '' when no persona is configured yet (the cycle
    then falls back to the normal weighted pick — a safe no-op for the toggle).
    """
    for p in _load_projects():
        if p.get("persona") is True:
            return str(p.get("name") or "")
    return ""


def env_exports() -> str:
    """Shell `export` lines for the current mode, safe to `eval`.

    promotion       -> nothing (normal weighted pick; persona is enabled:false).
    personal_brand  -> force the persona project + link-free replies. If no
                       persona project exists, emit nothing and warn on stderr so
                       the cycle proceeds normally instead of crashing.
    """
    if get_mode() != PERSONAL_BRAND:
        return ""
    name = persona_name()
    if not name:
        print(
            "[saps_mode] personal_brand mode is on but no persona project "
            "(persona:true) is configured; running the normal pick instead.",
            file=sys.stderr,
        )
        return ""
    lines = [
        f"export SAPS_FORCE_PROJECT={shlex.quote(name)}",
        "export TWITTER_TAIL_LINK_RATE=0",
    ]
    return "\n".join(lines)


def main(argv) -> int:
    if not argv:
        print(get_mode())
        return 0
    cmd = argv[0]
    if cmd == "get":
        print(get_mode())
        return 0
    if cmd == "set":
        if len(argv) < 2:
            print("usage: saps_mode.py set <personal_brand|promotion>",
                  file=sys.stderr)
            return 2
        try:
            print(set_mode(argv[1]))
            return 0
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
    if cmd == "toggle":
        new = PROMOTION if get_mode() == PERSONAL_BRAND else PERSONAL_BRAND
        print(set_mode(new))
        return 0
    if cmd == "env":
        out = env_exports()
        if out:
            print(out)
        return 0
    if cmd == "persona-name":
        print(persona_name())
        return 0
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
