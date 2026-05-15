"""Start a Browserbase session for the Mediar Reddit account:
persistent context + proxy geo-pinned to the SF Bay Area + keepAlive,
then return the interactive Live View URL.
"""
import json, subprocess, urllib.request
from playwright.sync_api import sync_playwright

API_KEY = subprocess.run(
    ["security", "find-generic-password", "-s", "Browserbase API Key", "-w"],
    capture_output=True, text=True).stdout.strip()
PROJECT_ID = "a95115ba-3653-4376-bff7-c6744d4ea18c"
BASE = "https://api.browserbase.com/v1"
HDR = {"X-BB-API-Key": API_KEY, "Content-Type": "application/json"}

def api(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method, headers=HDR)
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(req, data, timeout=40) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code} {method} {path}: {e.read().decode()[:300]}")
        raise

# persistent context = the Mediar client's Reddit identity
ctx = api("POST", "/contexts", {"projectId": PROJECT_ID})
print("context (Mediar Reddit):", ctx["id"])

body = {
    "projectId": PROJECT_ID,
    "browserSettings": {"context": {"id": ctx["id"], "persist": True}},
    "proxies": [{
        "type": "browserbase",
        "geolocation": {"country": "US", "state": "CA", "city": "San Francisco"},
    }],
    "keepAlive": True,
    "timeout": 900,
}
s = api("POST", "/sessions", body)
print("session:", s["id"], "region:", s.get("region"))

dbg = api("GET", f"/sessions/{s['id']}/debug")
live = dbg.get("debuggerFullscreenUrl")

geo = "?"
with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(s["connectUrl"])
    c = browser.contexts[0]
    page = c.pages[0] if c.pages else c.new_page()
    try:
        page.goto("http://ip-api.com/json/", wait_until="domcontentloaded", timeout=30000)
        g = json.loads(page.inner_text("body"))
        geo = f"{g.get('query')}  {g.get('city')}, {g.get('regionName')}, {g.get('country')}  ({g.get('isp')})"
    except Exception as e:
        geo = f"(geo lookup failed: {e})"
    for url in ("https://www.reddit.com/login/", "https://www.reddit.com/"):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            print("parked on:", page.url)
            break
        except Exception as e:
            print(f"nav {url} failed: {e}")
    browser.close()  # keepAlive=true keeps the session running after this

out = {"sessionId": s["id"], "contextId": ctx["id"], "liveViewUrl": live, "egress": geo}
open("bb_account.json", "w").write(json.dumps(out, indent=2))
print("=== EGRESS ===")
print(geo)
print("=== LIVE VIEW URL ===")
print(live)
