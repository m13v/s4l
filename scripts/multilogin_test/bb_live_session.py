"""Start a Browserbase session with a persistent context + proxy, return the
interactive Live View URL so the user can watch and take control.
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
        print(f"HTTP {e.code} on {method} {path}: {e.read().decode()[:300]}")
        raise

# context = persistent identity for this client account
ctx = api("POST", "/contexts", {"projectId": PROJECT_ID})
print("context:", ctx["id"])

body = {
    "projectId": PROJECT_ID,
    "browserSettings": {"context": {"id": ctx["id"], "persist": True}},
    "proxies": True,
    "timeout": 900,
}
try:
    s = api("POST", "/sessions", body)
    proxied = True
except Exception:
    print("retrying without proxy (free-tier proxy may be unavailable)")
    body.pop("proxies")
    s = api("POST", "/sessions", body)
    proxied = False
print("session:", s["id"], "region:", s.get("region"), "proxied:", proxied)

dbg = api("GET", f"/sessions/{s['id']}/debug")
live = dbg.get("debuggerFullscreenUrl") or dbg.get("debuggerUrl")
print("debug keys:", list(dbg.keys()))

# park the cloud browser on a neutral page + report the egress IP/geo
ip_info = "?"
with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(s["connectUrl"])
    bctx = browser.contexts[0]
    page = bctx.pages[0] if bctx.pages else bctx.new_page()
    try:
        page.goto("https://ipapi.co/json/", wait_until="domcontentloaded", timeout=30000)
        ip_info = page.inner_text("body").strip()[:400]
    except Exception as e:
        ip_info = f"(ip lookup failed: {e})"
    page.goto("https://www.reddit.com/login/", wait_until="domcontentloaded", timeout=45000)
    print("parked on:", page.url)
    browser.close()

out = {
    "sessionId": s["id"],
    "contextId": ctx["id"],
    "proxied": proxied,
    "liveViewUrl": live,
    "connectUrl": s["connectUrl"],
    "egress": ip_info,
}
open("bb_live.json", "w").write(json.dumps(out, indent=2))
print("=== EGRESS ===")
print(ip_info)
print("=== LIVE VIEW URL ===")
print(live)
