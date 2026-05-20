"""Twitter-handle helper. Thin shim over `account_resolver` so the dozens of
existing `from twitter_account import resolve_handle` callers keep working.

New code should call `account_resolver.resolve('twitter')` directly. See
`account_resolver.py` for the canonical resolution order and normalization
rules.
"""
from __future__ import annotations

from typing import Optional

from account_resolver import (
    resolve as _resolve,
    require as _require,
    normalize as _normalize,
)


def resolve_handle() -> Optional[str]:
    """Return the normalized Twitter handle for this machine, or None."""
    return _resolve("twitter")


def require_handle() -> str:
    """Raise if no Twitter handle is configured."""
    return _require("twitter")


# Some callers import the raw normalizer; keep the symbol stable.
def _normalize_legacy(handle: Optional[str]) -> Optional[str]:  # pragma: no cover
    return _normalize(handle)


if __name__ == "__main__":
    import sys
    h = resolve_handle()
    if h:
        sys.stdout.write(h + "\n")
        sys.exit(0)
    sys.stderr.write("no twitter handle configured\n")
    sys.exit(1)
