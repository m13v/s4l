#!/bin/bash
# Build and install WebDriverAgent on the connected iPhone.
# Run once after first plugging in the phone, and any time WDA needs a refresh.
set -euo pipefail

TEAM_ID="${IOS_TEAM_ID:-S6DP5HF77G}"
WDA_DIR="$(cd "$(dirname "$0")/.." && pwd)/node_modules/appium-xcuitest-driver/node_modules/appium-webdriveragent"

if ! command -v /opt/homebrew/bin/idevice_id >/dev/null; then
  echo "idevice_id not found. brew install libimobiledevice"
  exit 1
fi

UDID="$(/opt/homebrew/bin/idevice_id -l | head -n1 || true)"
if [ -z "$UDID" ]; then
  echo "No iPhone connected via USB. Plug it in, unlock, tap 'Trust this computer'."
  exit 1
fi

echo "device UDID: $UDID"
echo "team ID:     $TEAM_ID"
echo "WDA path:    $WDA_DIR"
echo

cd "$WDA_DIR"

xcodebuild \
  -project WebDriverAgent.xcodeproj \
  -scheme WebDriverAgentRunner \
  -destination "id=$UDID" \
  -allowProvisioningUpdates \
  DEVELOPMENT_TEAM="$TEAM_ID" \
  CODE_SIGN_STYLE=Automatic \
  PRODUCT_BUNDLE_IDENTIFIER=com.m13v.WebDriverAgentRunner \
  build-for-testing

echo
echo "Build succeeded. Now installing test bundle on device..."

xcodebuild \
  -project WebDriverAgent.xcodeproj \
  -scheme WebDriverAgentRunner \
  -destination "id=$UDID" \
  -allowProvisioningUpdates \
  DEVELOPMENT_TEAM="$TEAM_ID" \
  CODE_SIGN_STYLE=Automatic \
  PRODUCT_BUNDLE_IDENTIFIER=com.m13v.WebDriverAgentRunner \
  test-without-building &
XCBUILD_PID=$!

# WDA launches and stays running; once HTTP responds we know it's up.
echo "waiting for WDA to come up on device..."
for i in $(seq 1 60); do
  sleep 2
  # WDA listens on a USB-forwarded port; xcodebuild test starts it.
  echo "  (still booting, ${i}s)"
done

kill "$XCBUILD_PID" 2>/dev/null || true
echo "WDA installed. On the iPhone: Settings -> General -> VPN & Device Management -> trust 'Apple Development: Matthew Diakonov'."
