#!/bin/bash
# Launch the Appium server on port 4723. Run in its own terminal.
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="/opt/homebrew/Cellar/node/25.9.0_2/bin:$PATH"
exec ./node_modules/.bin/appium "$@"
