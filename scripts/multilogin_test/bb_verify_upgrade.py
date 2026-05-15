"""Verify the Developer-plan upgrade (proxies should now work) and test
whether Browserbase's proxy gives a consistent IP across sessions.
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
    with urllib.request.urlopen(req, data, timeout=40) as r:
        return json.load(r)

def egress(connect_url):
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(connect_url)
        c = b.contexts[0]
        pg = c.pages[0] if c.pages else c.new_page()
        info = {}
        try:
            pg.goto("https://api.ipify.org?format=json", wait_until="domcontentloaded", timeout=30000)
            info["ip"] = json.loads(pg.inner_text("body")).get("ip")
        except Exception as e:
            info["ip"] = f"(ip lookup failed: {e})"
        try:
            pg.goto("http://ip-api.com/json/", wait_until="domcontentloaded", timeout=30000)
            geo = json.loads(pg.inner_text("body"))
            info.update({"city": geo.get("city"), "region": geo.get("regionName"),
                         "country_name": geo.get("country"), "org": geo.get("isp")})
        except Exception:
            pass
        b.close()
        return info

def release(sid):
    try:
        api("POST", f"/sessions/{sid}", {"projectId": PROJECT_ID, "status": "REQUEST_RELEASE"})
    except Exception:
        pass

results = []
for i in (1, 2):
    try:
        s = api("POST", "/sessions", {"projectId": PROJECT_ID, "proxies": True})
    except urllib.error.HTTPError as e:
        print(f"session {i}: HTTP {e.code} {e.read().decode()[:200]}")
        print(">>> UPGRADE NOT ACTIVE (proxies still blocked)" if e.code == 402 else "")
        raise SystemExit(1)
    info = egress(s["connectUrl"])
    ipline = f"{info.get('ip')}  {info.get('city')}, {info.get('region')}, {info.get('country_name')}  ({info.get('org')})"
    print(f"session {i}: {s['id']}  ->  {ipline}")
    results.append(info.get("ip"))
    release(s["id"])

print()
print("UPGRADE: active (proxied sessions created OK)")
if results[0] == results[1]:
    print(f"PROXY IP: SAME across sessions ({results[0]}) -> consistent")
else:
    print(f"PROXY IP: DIFFERENT across sessions ({results[0]} vs {results[1]}) -> rotating pool")
