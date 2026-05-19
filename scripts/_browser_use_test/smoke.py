"""Open Twitter via browser-use using a Playwright storage_state.json
exported from the twitter-agent MCP (decrypted cookies, plug-and-play).

Pipeline:
  twitter-agent (Playwright) -> context.storageState() -> storage_state.json
  storage_state.json -> browser-use Browser(storage_state=...) -> Chrome -> x.com/home

Run:
    .venv/bin/python smoke.py
"""

import asyncio
import os
import sys

from browser_use import Browser
from browser_use.browser.profile import BrowserProfile


# Skip browser-use's automatic profile copy (we use a fresh profile dir).
BrowserProfile._copy_profile = lambda self: None  # type: ignore[method-assign]


CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
STORAGE_STATE = "/Users/matthewdi/social-autoposter/scripts/_browser_use_test/storage_state.json"
USER_DATA_DIR = os.path.expanduser("~/.claude/browser-profiles/browser-use-twitter")
TARGET_URL = "https://x.com/home"


async def main() -> int:
    os.makedirs(USER_DATA_DIR, exist_ok=True)

    browser = Browser(
        executable_path=CHROME_PATH,
        user_data_dir=USER_DATA_DIR,
        storage_state=STORAGE_STATE,
        headless=False,
        is_local=True,
        window_size={"width": 1280, "height": 800},
        window_position={"width": 100, "height": 100},
        no_viewport=True,
        args=[
            "--window-size=1280,800",
            "--window-position=100,100",
        ],
        ignore_default_args=["--start-maximized"],
    )

    print(f"Launching Chrome with storage_state: {STORAGE_STATE}")
    print(f"User data dir (fresh): {USER_DATA_DIR}")
    await browser.start()

    print(f"Navigating to {TARGET_URL}")
    await browser.navigate_to(TARGET_URL)
    await asyncio.sleep(4)

    cookies = await browser.cookies()
    tw = [c for c in cookies if "x.com" in c.get("domain", "") or "twitter" in c.get("domain", "")]
    auth = next((c for c in tw if c.get("name") == "auth_token"), None)
    print(f"\nx.com/twitter cookies in session: {len(tw)}")
    print(f"auth_token present: {bool(auth)}")
    if auth:
        v = auth.get("value", "")
        print(f"auth_token value (masked): {v[:6]}...{v[-4:]} (len={len(v)})")

    title = await browser.get_current_page_title()
    url = await browser.get_current_page_url()
    print(f"\nFinal URL:   {url}")
    print(f"Page title:  {title!r}")

    logged_in = "/home" in url or "Home" in title
    print(f"Logged in:   {logged_in}")

    hold_seconds = int(os.environ.get("HOLD_SECONDS", "25"))
    print(f"\nHolding window for {hold_seconds}s...")
    for remaining in range(hold_seconds, 0, -5):
        print(f"  {remaining}s remaining")
        await asyncio.sleep(5)

    await browser.kill()
    print("Browser closed.")
    return 0 if logged_in else 3


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
