#!/usr/bin/env python3
"""draft_provider.py: which engine answers the pipeline's pure text->JSON turns.

One file in the state dir (<state_dir>/draft-provider.json) records the choice,
stamped once at setup time and read at the run_claude.sh seam (via
claude_job.py). No env-var fallback layering: the file is the single source of
truth, matching the experiments stamp-at-source rule.

Providers:
  claude-desktop-queue  (default) enqueue for the Claude Desktop s4l-worker
                        scheduled task; the only option on boxes whose sole
                        authenticated engine is Claude Desktop itself.
  claude-p              skip the queue entirely; claude_job.py `eligible`
                        returns 1 so run_claude.sh falls through to the real
                        `claude -p` it already knows how to run. Requires the
                        Claude Code CLI, logged in.
  codex-exec            answer inline via the ChatGPT-app-bundled `codex exec`
                        (headless, subscription-authenticated). Requires the
                        ChatGPT app installed and logged in. No queue, no
                        worker, no app-open requirement.

CLI:
  python3 scripts/draft_provider.py get            -> prints the active provider
  python3 scripts/draft_provider.py set <name>     -> records the choice
  python3 scripts/draft_provider.py codex-bin      -> prints resolved codex path
"""

from __future__ import annotations

import json
import os
import sys
import time

VALID_PROVIDERS = ("claude-desktop-queue", "claude-p", "codex-exec")
DEFAULT_PROVIDER = "claude-desktop-queue"

# The npm @openai/codex install is unreliable on macOS (XProtect false-positives
# trash its native binary, openai/codex #31377), so the ChatGPT-app-bundled
# binary is the only path we trust. S4L_CODEX_BIN exists for tests and for
# machines that keep the binary elsewhere.
CODEX_APP_BIN = "/Applications/ChatGPT.app/Contents/Resources/codex"


def state_dir() -> str:
    return os.environ.get("S4L_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".social-autoposter-mcp"
    )


def provider_path() -> str:
    return os.path.join(state_dir(), "draft-provider.json")


def get() -> str:
    try:
        with open(provider_path()) as f:
            v = json.load(f).get("provider", "")
    except Exception:
        return DEFAULT_PROVIDER
    return v if v in VALID_PROVIDERS else DEFAULT_PROVIDER


def set_provider(name: str, set_by: str = "cli") -> None:
    if name not in VALID_PROVIDERS:
        raise ValueError(f"unknown provider {name!r}; valid: {VALID_PROVIDERS}")
    os.makedirs(state_dir(), exist_ok=True)
    tmp = provider_path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump(
            {
                "provider": name,
                "set_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "set_by": set_by,
            },
            f,
            indent=2,
        )
    os.replace(tmp, provider_path())


def codex_bin() -> str | None:
    """Resolved codex binary path, or None when nothing usable exists."""
    override = os.environ.get("S4L_CODEX_BIN", "").strip()
    if override and os.access(override, os.X_OK):
        return override
    if os.access(CODEX_APP_BIN, os.X_OK):
        return CODEX_APP_BIN
    return None


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in ("get", "set", "codex-bin"):
        print(__doc__, file=sys.stderr)
        return 2
    cmd = sys.argv[1]
    if cmd == "get":
        print(get())
        return 0
    if cmd == "codex-bin":
        b = codex_bin()
        if not b:
            print("codex binary not found (is the ChatGPT app installed?)", file=sys.stderr)
            return 1
        print(b)
        return 0
    if len(sys.argv) < 3:
        print("usage: draft_provider.py set <name>", file=sys.stderr)
        return 2
    try:
        set_provider(sys.argv[2])
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(f"draft provider set to {sys.argv[2]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
