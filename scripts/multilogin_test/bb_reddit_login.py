"""Log the s4l Reddit account into a Browserbase session (Mediar context,
CA-pinned proxy). Native username/password login, no Google OAuth.
"""
import json, subprocess, time, urllib.request
from playwright.sync_api import sync_playwright

API_KEY = subprocess.run(
    ["security", "find-generic-password", "-s", "Browserbase API Key", "-w"],
    capture_output=True, text=True).stdout.strip()
USERNAME = "Deep_Ad1959"
PASSWORD = subprocess.run(
    ["security", "find-generic-password", "-s", "Reddit-Autoposter", "-w"],
    capture_output=True, text=True).stdout.strip()
PROJECT_ID = "a95115ba-3653-4376-bff7-c6744d4ea18c"
CONTEXT_ID = "28c07684-206c-4151-b17d-891a6d76d96b"
BASE = "https://api.browserbase.com/v1"
HDR = {"X-BB-API-Key": API_KEY, "Content-Type": "application/json"}

def api(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method, headers=HDR)
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(req, data, timeout=40) as r:
        return json.load(r)

# release the old session with the dangling Google popup
try:
    api("POST", "/sessions/f7309a6a-865e-44cf-81a3-792439e9eeda",
        {"projectId": PROJECT_ID, "status": "REQUEST_RELEASE"})
except Exception:
    pass

s = api("POST", "/sessions", {
    "projectId": PROJECT_ID,
    "browserSettings": {"context": {"id": CONTEXT_ID, "persist": True}},
    "proxies": [{"type": "browserbase",
                 "geolocation": {"country": "US", "state": "CA", "city": "San Francisco"}}],
    "keepAlive": True,
    "timeout": 900,
})
print("session:", s["id"])
dbg = api("GET", f"/sessions/{s['id']}/debug")
live = dbg.get("debuggerFullscreenUrl")

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(s["connectUrl"])
    c = browser.contexts[0]
    page = c.pages[0] if c.pages else c.new_page()
    page.goto("https://www.reddit.com/login/", wait_until="domcontentloaded", timeout=45000)
    time.sleep(4)

    filled = False
    for usel, psel in [('input[name="username"]', 'input[name="password"]'),
                        ('#login-username', '#login-password')]:
        try:
            if page.locator(usel).count() and page.locator(psel).count():
                page.fill(usel, USERNAME)
                page.fill(psel, PASSWORD)
                filled = True
                print(f"filled via {usel}")
                break
        except Exception as e:
            print(f"selector {usel} failed: {e}")
    if not filled:
        inputs = page.evaluate("Array.from(document.querySelectorAll('input')).map(i=>({name:i.name,id:i.id,type:i.type}))")
        print("could not find login inputs. inputs on page:", inputs)
    else:
        try:
            page.get_by_role("button", name="Log In").click(timeout=8000)
        except Exception:
            page.keyboard.press("Enter")
        time.sleep(8)
        print("post-submit url:", page.url)
        print("page title:", page.title())
        body = page.inner_text("body")[:300]
        print("body snippet:", body.replace("\n", " "))
    page.screenshot(path="bb_reddit_login.png")
    browser.close()

open("bb_reddit.json", "w").write(json.dumps(
    {"sessionId": s["id"], "liveViewUrl": live}, indent=2))
print("LIVE VIEW:", live)
