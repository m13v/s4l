"""Minimal Appium + WDA smoke test.

Prerequisites:
  1. Appium server running:   ../node_modules/.bin/appium  (port 4723)
  2. iPhone connected via USB, unlocked, trusted this Mac.
  3. WebDriverAgent signed with your team (see README.md, Signing section).
  4. Set env vars below or edit inline:
        export IOS_UDID=<your device udid from `idevice_id -l`>
        export IOS_TEAM_ID=<10-char Apple Dev Team ID, e.g. ABCDE12345>
        export IOS_BUNDLE_ID=com.apple.Preferences      # app to launch
"""
import os
import time

from appium import webdriver
from appium.options.ios import XCUITestOptions

UDID = os.environ.get("IOS_UDID", "")
TEAM_ID = os.environ.get("IOS_TEAM_ID", "")
BUNDLE_ID = os.environ.get("IOS_BUNDLE_ID", "com.apple.Preferences")
APPIUM_URL = os.environ.get("APPIUM_URL", "http://127.0.0.1:4723")

if not UDID or not TEAM_ID:
    raise SystemExit("Set IOS_UDID and IOS_TEAM_ID environment variables first.")

opts = XCUITestOptions()
opts.platform_name = "iOS"
opts.device_name = "iPhone"
opts.udid = UDID
opts.xcode_org_id = TEAM_ID
opts.xcode_signing_id = "iPhone Developer"
opts.bundle_id = BUNDLE_ID
opts.set_capability("usePrebuiltWDA", False)
opts.set_capability("showXcodeLog", True)

print(f"connecting to {APPIUM_URL} udid={UDID} bundle={BUNDLE_ID}")
driver = webdriver.Remote(APPIUM_URL, options=opts)

try:
    print("session started:", driver.session_id)
    print("orientation:", driver.orientation)
    print("window size:", driver.get_window_size())
    time.sleep(2)
    screenshot_path = os.path.join(os.path.dirname(__file__), "..", "logs", "smoke.png")
    driver.get_screenshot_as_file(screenshot_path)
    print("screenshot saved:", screenshot_path)
finally:
    driver.quit()
