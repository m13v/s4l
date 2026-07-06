#!/usr/bin/env python3
"""LEGACY-NAME SHIM (brand rename SAPS -> S4L, completed 2026-07-06).

The real module is scripts/s4l_activity.py. This shim exists ONLY for callers
that cannot be edited (chflags-locked pipeline scripts) and for older installed
runtimes. Do NOT add new references to this filename; import or invoke
s4l_activity.py directly.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from s4l_activity import *  # noqa: F401,F403,E402
from s4l_activity import _main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
