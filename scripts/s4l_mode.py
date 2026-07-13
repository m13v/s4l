#!/usr/bin/env python3
"""Single source of truth for the engagement MODE (2026-06-26, dual-flag 2026-06-29).

The S4L pipeline drafts for TWO independently toggleable lanes:

  - "personal_brand" (default ON): pure organic engagement to grow the user's
                     personal brand. The cycle is forced onto the persona project
                     (the config entry with `"persona": true`, normally
                     `enabled:false` so the promotion pick never touches it) and
                     replies are link-free.
  - "promotion"      (default OFF): the normal project/product-marketing pipeline.
                     The weighted pick (pick_project.py) chooses among enabled
                     projects; replies carry the project's link per the A/B gate.

Both can be ON at once. When both are ON, each cycle invocation flips a
WEIGHTED coin (`personal_brand_share` in mode.json, default 0.5) and runs that
one cycle as either a persona (link-free) cycle or a normal promotion cycle.
The locked pipeline never changes — it just reads the env vars env_exports()
prints. The share only matters in the both-on state; single-lane states ignore
it. Set it via `s4l_mode.py split <value>` (the dashboard slider and the menu
bar's "Lane split" presets both call that).

State lives in ONE small file, `$S4L_STATE_DIR/mode.json`:
    {"personal_brand": true, "promotion": false, "mode": "personal_brand",
     "personal_brand_share": 0.5}

The `"mode"` field is a DERIVED legacy mirror (personal_brand if that lane is on,
else promotion) kept only so any old reader that still does `data["mode"]` keeps
working. s4l_mode.py is the only writer; it always writes all three keys.

Backward-compat read: a legacy file `{"mode": "promotion"}` (no flags) maps to
promotion-only; `{"mode": "personal_brand"}` maps to personal-only. A missing
file defaults to personal_brand ON / promotion OFF (the 2026-06-29 default flip).

The toggle takes effect WITHOUT touching any locked pipeline file: the unlocked
wrapper `skill/run-draft-and-publish.sh` evals `s4l_mode.py env` right before it
invokes the locked `run-twitter-cycle.sh`, exporting the env vars the locked
pipeline already honors:
    S4L_FORCE_PROJECT       -> pick_project.py forces this exact project
                                (--project bypasses the enabled gate), so a
                                disabled persona is still selectable.
    TWITTER_TAIL_LINK_RATE=0 -> twitter_post_plan.py ships every reply bare.

Usage:
    s4l_mode.py get                 # print derived legacy mode (compat)
    s4l_mode.py flags               # print JSON {personal_brand, promotion}
    s4l_mode.py set personal_brand  # legacy: personal-only (compat)
    s4l_mode.py set promotion       # legacy: promotion-only (compat)
    s4l_mode.py set-flags <pb> <pr> # set both lanes, e.g. `set-flags 1 1`
    s4l_mode.py enable personal_brand|promotion
    s4l_mode.py disable personal_brand|promotion
    s4l_mode.py toggle personal_brand|promotion   # flip ONE lane
    s4l_mode.py toggle              # legacy whole-mode flip (compat)
    s4l_mode.py env                 # print shell `export` lines for this cycle
    s4l_mode.py persona-name        # print the persona project name (or empty)
    s4l_mode.py draft-only          # print 1|0 (1 = cycles stop at review cards)
    s4l_mode.py draft-only on|off   # set the draft-only flag (operator-only)
    s4l_mode.py split               # print the personal-brand share (0.0-1.0)
    s4l_mode.py split <value>       # set it; accepts 70, 70%, or 0.7

Draft-only (2026-07-06, single global switch as of 2026-07-08): a third,
independent flag in mode.json, DEFAULT ON. While ON (the normal state), every
cycle stops before posting and its drafts become review cards. When an
OPERATOR turns it off, run-draft-and-publish.sh runs EVERY lane (promotion AND
personal_brand) with DRAFT_ONLY=0 so they POST autonomously (promotion
additionally runs behind the rolling virality bar). This applies uniformly
across lanes; persona-lane cycles do NOT stay draft-only when the flag is off.
Deliberately NOT exposed on any user surface (no MCP tool param, no menubar
toggle): this CLI and mode.json are the only way to flip it.
`env` additionally exports S4L_CYCLE_LANE=<lane> for both lanes so the wrapper
knows which lane this cycle is without re-deriving it.

The pre-2026-07-06 helper filename is retired. Current code must import or
invoke this module directly.
"""

import json
import os
import random
import shlex
import sys
from pathlib import Path

PROMOTION = "promotion"
PERSONAL_BRAND = "personal_brand"
VALID_MODES = (PROMOTION, PERSONAL_BRAND)

# 2026-06-29 default flip: personal brand is the out-of-the-box lane; promotion
# is opt-in (asked for during setup).
DEFAULT_PERSONAL_BRAND = True
DEFAULT_PROMOTION = False

# Retained so old imports of `DEFAULT_MODE` don't break.
DEFAULT_MODE = PERSONAL_BRAND

# Share of both-lanes-on cycles that run as personal_brand (the rest run as
# promotion). Only consulted when BOTH lanes are on; a missing/invalid
# mode.json key falls back here, so pre-split files keep the historic 50/50.
DEFAULT_PERSONAL_BRAND_SHARE = 0.5


def state_dir() -> Path:
    # Mirrors mcp/src/index.ts s4lStateDir() and menubar/s4l_state.py state_dir().
    return Path(
        os.environ.get("S4L_STATE_DIR")
        or (Path.home() / ".social-autoposter-mcp")
    )


def mode_file() -> Path:
    return state_dir() / "mode.json"


def config_path() -> Path:
    # Match the locked pipeline's resolution: S4L_REPO_DIR/config.json when set,
    # else the canonical ~/social-autoposter/config.json (what pick_project.py /
    # project_topics.py read directly).
    repo = os.environ.get("S4L_REPO_DIR")
    if repo:
        p = Path(repo) / "config.json"
        if p.exists():
            return p
    return Path.home() / "social-autoposter" / "config.json"


def _coerce_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return False


def get_flags() -> dict:
    """Current lane flags as {"personal_brand": bool, "promotion": bool}.

    Read precedence: explicit flag keys win; else map a legacy {"mode": ...}
    string; else the (new) default of personal-brand ON / promotion OFF.
    """
    try:
        data = json.loads(mode_file().read_text())
    except Exception:
        data = None
    if not isinstance(data, dict):
        return {"personal_brand": DEFAULT_PERSONAL_BRAND, "promotion": DEFAULT_PROMOTION}

    if "personal_brand" in data or "promotion" in data:
        return {
            "personal_brand": _coerce_bool(data.get("personal_brand", False)),
            "promotion": _coerce_bool(data.get("promotion", False)),
        }

    # Legacy single-mode file.
    legacy = str(data.get("mode") or "").strip()
    if legacy == PERSONAL_BRAND:
        return {"personal_brand": True, "promotion": False}
    if legacy == PROMOTION:
        return {"personal_brand": False, "promotion": True}
    return {"personal_brand": DEFAULT_PERSONAL_BRAND, "promotion": DEFAULT_PROMOTION}


def _legacy_mode(flags: dict) -> str:
    """Derived single-mode mirror: personal_brand wins when on (it's the default
    lane), else promotion. Only used for the back-compat `mode` field/readers."""
    return PERSONAL_BRAND if flags.get("personal_brand") else PROMOTION


def get_mode() -> str:
    """Derived legacy mode string (compat shim for old callers)."""
    return _legacy_mode(get_flags())


def write_flags(personal_brand: bool, promotion: bool) -> dict:
    """Persist both lane flags atomically (plus the derived legacy `mode`).

    Preserves any OTHER keys already in mode.json (e.g. `draft_only`) so a lane
    flip can never silently reset an unrelated setting.
    """
    flags = {"personal_brand": bool(personal_brand), "promotion": bool(promotion)}
    try:
        payload = json.loads(mode_file().read_text())
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}
    payload.update(flags)
    payload["mode"] = _legacy_mode(flags)
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = mode_file().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(mode_file())
    return flags


def parse_share(value) -> float:
    """Normalize a user-supplied share: accepts 70, '70%', or 0.7.

    Bare numbers above 1 are read as percentages. Clamps to [0, 1]; raises
    ValueError on non-numeric input so callers can report it.
    """
    if isinstance(value, str):
        value = value.strip().rstrip("%")
    try:
        share = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"invalid share {value!r}; expected a number like 70, 70%, or 0.7")
    if share > 1.0:
        share = share / 100.0
    return min(1.0, max(0.0, share))


def get_split() -> float:
    """Personal-brand share of both-lanes-on cycles (0.0-1.0, default 0.5)."""
    try:
        data = json.loads(mode_file().read_text())
        if isinstance(data, dict) and "personal_brand_share" in data:
            return parse_share(data.get("personal_brand_share"))
    except Exception:
        pass
    return DEFAULT_PERSONAL_BRAND_SHARE


def set_split(value) -> float:
    """Persist the personal-brand share, preserving every other mode.json key."""
    share = parse_share(value)
    try:
        payload = json.loads(mode_file().read_text())
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}
    payload["personal_brand_share"] = share
    # Keep the lane keys + legacy mirror intact on first-ever write.
    flags = get_flags()
    payload.setdefault("personal_brand", flags["personal_brand"])
    payload.setdefault("promotion", flags["promotion"])
    payload["mode"] = _legacy_mode(flags)
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = mode_file().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(mode_file())
    return share


def get_draft_only() -> bool:
    """Whether every cycle stops at review cards (DEFAULT: True).

    False (operator opt-out via `draft-only off`) means EVERY lane (promotion
    AND personal_brand) POSTs autonomously; promotion additionally runs behind
    the rolling virality bar. As of 2026-07-08 this is a single global switch,
    not promotion-only: see skill/run-draft-and-publish.sh's DRAFT_ONLY_FLAG
    gate, which applies this value uniformly across lanes. Reads the retired
    `autopilot` key (inverted) from mode.json files written before the
    2026-07-06 rename.
    """
    try:
        data = json.loads(mode_file().read_text())
        if not isinstance(data, dict):
            return True
        if "draft_only" in data:
            return _coerce_bool(data.get("draft_only"))
        if "autopilot" in data:  # pre-rename file: autopilot=true meant NOT draft-only
            return not _coerce_bool(data.get("autopilot"))
        return True
    except Exception:
        return True


def set_draft_only(on: bool) -> bool:
    try:
        payload = json.loads(mode_file().read_text())
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}
    payload["draft_only"] = bool(on)
    payload.pop("autopilot", None)  # retire the pre-rename key
    # Keep the lane keys + legacy mirror intact on first-ever write.
    flags = get_flags()
    payload.setdefault("personal_brand", flags["personal_brand"])
    payload.setdefault("promotion", flags["promotion"])
    payload["mode"] = _legacy_mode(flags)
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = mode_file().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(mode_file())
    return bool(on)


def set_mode(mode: str) -> str:
    """Legacy single-mode setter: turns the named lane ON and the other OFF."""
    mode = (mode or "").strip()
    if mode not in VALID_MODES:
        raise ValueError(f"invalid mode {mode!r}; expected one of {VALID_MODES}")
    write_flags(personal_brand=(mode == PERSONAL_BRAND), promotion=(mode == PROMOTION))
    return mode


def set_lane(lane: str, on: bool) -> dict:
    lane = (lane or "").strip()
    if lane not in VALID_MODES:
        raise ValueError(f"invalid lane {lane!r}; expected one of {VALID_MODES}")
    flags = get_flags()
    flags[lane] = bool(on)
    return write_flags(flags["personal_brand"], flags["promotion"])


def toggle_lane(lane: str) -> dict:
    lane = (lane or "").strip()
    if lane not in VALID_MODES:
        raise ValueError(f"invalid lane {lane!r}; expected one of {VALID_MODES}")
    flags = get_flags()
    flags[lane] = not flags.get(lane)
    return write_flags(flags["personal_brand"], flags["promotion"])


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


def _persona_env_lines() -> str:
    name = persona_name()
    if not name:
        print(
            "[s4l_mode] personal_brand lane is on but no persona project "
            "(persona:true) is configured; running the normal pick instead.",
            file=sys.stderr,
        )
        return ""
    return "\n".join(
        [
            f"export S4L_FORCE_PROJECT={shlex.quote(name)}",
            "export TWITTER_TAIL_LINK_RATE=0",
            # Explicit lane signal so the (locked) cycle can branch the draft
            # directive + inject the persona corpus without re-deriving the lane
            # from S4L_FORCE_PROJECT (which is also set by manual single-project
            # MCP draft_cycle runs). Only the personal_brand lane sets this.
            "export S4L_ACTIVE_LANE=personal_brand",
            # Wrapper-facing lane tag (2026-07-06). Unlike S4L_ACTIVE_LANE (which
            # the locked cycle branches on and must stay persona-only), this is
            # exported for BOTH lanes so run-draft-and-publish.sh can decide the
            # per-cycle DRAFT_ONLY value (draft-only off posts promotion cycles).
            "export S4L_CYCLE_LANE=personal_brand",
        ]
    )


_PROMOTION_ENV = "export S4L_CYCLE_LANE=promotion"


def env_exports() -> str:
    """Shell `export` lines for THIS cycle, safe to `eval`.

    personal_brand only -> force the persona project + link-free replies.
    promotion only      -> nothing (normal weighted pick; persona is enabled:false).
    both on             -> weighted coin flip per cycle: personal_brand with
                           probability `personal_brand_share` (default 0.5),
                           else the normal promotion pick.
    neither (shouldn't happen; default keeps personal on) -> behave like personal
                           so the cycle is never a silent no-op.
    """
    flags = get_flags()
    pb = flags.get("personal_brand")
    pr = flags.get("promotion")

    if pb and pr:
        # Both lanes active: this single cycle is one or the other, weighted.
        share = get_split()
        pct = f"{round(share * 100)}/{round((1 - share) * 100)}"
        if random.random() < share:
            print(f"[s4l_mode] both lanes on; this cycle -> personal_brand ({pct})",
                  file=sys.stderr)
            return _persona_env_lines()
        print(f"[s4l_mode] both lanes on; this cycle -> promotion ({pct})",
              file=sys.stderr)
        return _PROMOTION_ENV
    if pb:
        return _persona_env_lines()
    if pr:
        return _PROMOTION_ENV
    # Neither on (degenerate) -> don't leave the cycle dead; run personal.
    print("[s4l_mode] no lane enabled; defaulting this cycle to personal_brand.",
          file=sys.stderr)
    return _persona_env_lines()


def main(argv) -> int:
    if not argv:
        print(get_mode())
        return 0
    cmd = argv[0]
    if cmd == "get":
        print(get_mode())
        return 0
    if cmd == "flags":
        print(json.dumps(get_flags()))
        return 0
    if cmd == "set":
        if len(argv) < 2:
            print("usage: s4l_mode.py set <personal_brand|promotion>", file=sys.stderr)
            return 2
        try:
            print(set_mode(argv[1]))
            return 0
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
    if cmd == "set-flags":
        if len(argv) < 3:
            print("usage: s4l_mode.py set-flags <personal_brand 0|1> <promotion 0|1>",
                  file=sys.stderr)
            return 2
        flags = write_flags(_coerce_bool(argv[1]), _coerce_bool(argv[2]))
        print(json.dumps(flags))
        return 0
    if cmd in ("enable", "disable"):
        if len(argv) < 2:
            print(f"usage: s4l_mode.py {cmd} <personal_brand|promotion>", file=sys.stderr)
            return 2
        try:
            flags = set_lane(argv[1], on=(cmd == "enable"))
            print(json.dumps(flags))
            return 0
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
    if cmd == "toggle":
        if len(argv) >= 2:
            try:
                flags = toggle_lane(argv[1])
                print(json.dumps(flags))
                return 0
            except ValueError as e:
                print(str(e), file=sys.stderr)
                return 2
        # Legacy whole-mode flip: personal<->promotion (mutually exclusive).
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
    if cmd == "split":
        # `split` -> print current share; `split <value>` -> set and print.
        if len(argv) >= 2:
            try:
                print(json.dumps({"personal_brand_share": set_split(argv[1])}))
                return 0
            except ValueError as e:
                print(str(e), file=sys.stderr)
                return 2
        print(json.dumps({"personal_brand_share": get_split()}))
        return 0
    if cmd == "draft-only":
        # `draft-only` -> print 1|0; `draft-only on|off|1|0` -> set and print.
        if len(argv) >= 2:
            print("1" if set_draft_only(_coerce_bool(argv[1])) else "0")
            return 0
        print("1" if get_draft_only() else "0")
        return 0
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
