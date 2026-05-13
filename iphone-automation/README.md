# iPhone Automation (Appium + WebDriverAgent)

Programmatic control of a real iPhone over USB (or Wi-Fi after pairing).
Drives taps, swipes, text input, screenshots, and full UI-tree access via
the XCUITest framework.

## What's installed

| Component | Version | Where |
|---|---|---|
| Appium server | 3.4.2 | `node_modules/.bin/appium` |
| XCUITest driver | 11.4.0 | `node_modules/appium-xcuitest-driver` |
| WebDriverAgent | bundled | `node_modules/appium-xcuitest-driver/node_modules/appium-webdriveragent` |
| Appium-Python-Client | latest | `venv/` |
| `ios-deploy`, `libimobiledevice`, `carthage` | brew | system |

## One-time setup: sign WebDriverAgent

WDA is an iOS test app; it must be code-signed with YOUR Apple Developer
team before it can be installed on a real iPhone. This is the only manual
step in the whole stack.

### Get your Team ID

```bash
# Open Xcode > Settings > Accounts, sign in with Apple ID.
# Then list teams:
security find-identity -v -p codesigning | grep "Apple Development"
# The 10-char string in parentheses is your Team ID, e.g. (ABCDE12345)
```

Free Apple ID works but signed app expires every 7 days (must re-deploy WDA).
Paid Developer Program ($99/yr) = 1 year signing lifetime.

### Sign WDA in Xcode

```bash
open node_modules/appium-xcuitest-driver/node_modules/appium-webdriveragent/WebDriverAgent.xcodeproj
```

In Xcode:
1. Select `WebDriverAgentRunner` target -> Signing & Capabilities
2. Check "Automatically manage signing"
3. Pick your Team
4. If bundle ID conflicts (`com.facebook.WebDriverAgentRunner`), change to
   something unique like `com.m13v.WebDriverAgentRunner`
5. Plug in iPhone, select it as the destination
6. Product -> Test (Cmd+U). It will build, install, and run WDA on the device.
   First time: iPhone will show "Untrusted Developer". Go to
   Settings > General > VPN & Device Management > [Your Team] > Trust.
7. Stop the test once you see "Test Suite WebDriverAgentRunner started" in the log.

After that, Appium will reuse the installed WDA app automatically.

## Daily use

### 1. Find device UDID

```bash
idevice_id -l                  # libimobiledevice
# or
xcrun xctrace list devices     # Xcode flavor
```

### 2. Start Appium server (terminal A)

```bash
cd /Users/matthewdi/social-autoposter/iphone-automation
PATH=/opt/homebrew/Cellar/node/25.9.0_2/bin:$PATH ./node_modules/.bin/appium
```

Listens on `http://127.0.0.1:4723`.

### 3. Run the smoke test (terminal B)

```bash
cd /Users/matthewdi/social-autoposter/iphone-automation
export IOS_UDID=00008110-XXXXXXXXXXXXXXXX     # from `idevice_id -l`
export IOS_TEAM_ID=ABCDE12345                  # your Apple Dev team
export IOS_BUNDLE_ID=com.apple.Preferences     # any installed app

./venv/bin/python scripts/smoke_test.py
```

On success: Settings.app launches on the phone, a screenshot lands in
`logs/smoke.png`.

## Folder layout

```
iphone-automation/
  package.json              # appium + xcuitest driver
  node_modules/             # appium + WDA project
  venv/                     # python client
  scripts/
    smoke_test.py           # minimal session test
  wda/                      # space for any WDA build artifacts / configs
  logs/                     # screenshots + run logs
  README.md
```

## Common pitfalls

- **WDA 7-day expiry (free dev account):** re-run Cmd+U in Xcode weekly,
  or pay $99/yr for 1-year signing.
- **"Could not launch WebDriverAgent":** stop any other running session,
  unplug+replug the phone, re-trust the computer.
- **First Appium connect is slow (30-60s):** it's installing/launching WDA.
  Subsequent sessions reuse it (~3-5s).
- **iOS version mismatch:** Xcode 25 supports up to iOS 19. If your phone
  is on a newer beta, update Xcode.
- **Screen locked:** auto-lock disables WDA's touch injection. Set
  "Auto-Lock = Never" while automating, or wake the phone in script.

## What this stack can do

- Tap any UI element by accessibility id, predicate, or coordinate
- Type text into any focused input
- Swipe / scroll / drag with momentum
- Read the full accessibility tree (`driver.page_source`) and query elements
- Screenshot the screen or a single element
- Launch any installed app by bundle id
- Press hardware buttons (volume, home, side, siri)
- Background/foreground the current app
- Set device orientation, geolocation (simulator only)
- Install/uninstall .ipa files
- Capture device logs in real time

## What it CAN'T do

- Anything inside system passcode entry or Face ID prompts (Apple blocks it)
- Tap through DRM-protected video surfaces
- Run on a phone you can't physically/USB access (no remote control without
  an extra remote-mux service)
