"""SAPS_ -> S4L_ environment mirror (brand rename 2026-07-03).

The internal env-var prefix moved from SAPS_ to S4L_, but launchd plists,
Claude Desktop scheduled-task prompts, and cron entries on customer machines
were written before the rename and still export SAPS_* names. They cannot all
be regenerated instantly (plists are only rewritten on re-registration), so
every python entrypoint that launchd / scheduled tasks invoke directly calls
mirror() right after imports. It copies each SAPS_FOO to S4L_FOO when the new
name is unset, and (defensively, for old code launched by NEW plists that emit
only S4L_*) the reverse direction too. Existing values always win: mirror()
never overwrites a name that is already set.

Stdlib-only, safe under /usr/bin/python3. Usage:

    import s4l_env
    s4l_env.mirror()
"""

from __future__ import annotations

import os

_OLD = "SAPS_"
_NEW = "S4L_"


def mirror(environ=None) -> int:
    """Copy SAPS_* -> S4L_* (and S4L_* -> SAPS_*) for any name whose twin is
    unset. Returns the number of variables copied. Never overwrites."""
    env = environ if environ is not None else os.environ
    copied = 0
    # Snapshot keys first: we mutate env while iterating.
    for key in list(env.keys()):
        if key.startswith(_OLD):
            twin = _NEW + key[len(_OLD):]
        elif key.startswith(_NEW):
            twin = _OLD + key[len(_NEW):]
        else:
            continue
        if twin not in env:
            env[twin] = env[key]
            copied += 1
    return copied


if __name__ == "__main__":
    print(f"mirrored {mirror()} env var(s)")
