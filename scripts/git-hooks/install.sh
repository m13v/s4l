#!/bin/bash
# Activate the shared git hooks for this clone. Run once after cloning:
#   bash scripts/git-hooks/install.sh
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"
chmod +x "$ROOT/scripts/git-hooks/pre-commit"
git -C "$ROOT" config core.hooksPath scripts/git-hooks
echo "core.hooksPath -> scripts/git-hooks (shared pre-commit active)."
echo "Tip: create pii_denylist.local.txt (gitignored) with real client names/emails/handles"
echo "so the scanner can catch them. One term per line."
