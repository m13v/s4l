import json, time, sys
import websocket
import urllib.request

CDP = "http://127.0.0.1:57909"

def pages():
    return json.load(urllib.request.urlopen(CDP + "/json"))

def page_ws():
    for p in pages():
        if p.get("type") == "page":
            return p["webSocketDebuggerUrl"]
    raise RuntimeError("no page target")

class CdpClient:
    def __init__(self, url):
        self.ws = websocket.create_connection(url, timeout=30, suppress_origin=True)
        self.i = 0
    def send(self, method, params=None):
        self.i += 1
        self.ws.send(json.dumps({"id": self.i, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.i:
                return msg
    def evaluate(self, expr):
        r = self.send("Runtime.evaluate", {
            "expression": expr, "returnByValue": True, "awaitPromise": True})
        return r.get("result", {}).get("result", {}).get("value")

c = CdpClient(page_ws())
c.send("Page.enable")
c.send("Runtime.enable")

# navigate to a real site
c.send("Page.navigate", {"url": "https://www.reddit.com"})
time.sleep(8)

fp = c.evaluate("""(() => ({
  webdriver: navigator.webdriver,
  platform: navigator.platform,
  ua: navigator.userAgent,
  cores: navigator.hardwareConcurrency,
  deviceMemory: navigator.deviceMemory,
  lang: navigator.language,
  langs: navigator.languages,
  screen: screen.width + 'x' + screen.height,
  tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
  vendor: navigator.vendor,
  webglVendor: (() => { try { const gl=document.createElement('canvas').getContext('webgl');
     const e=gl.getExtension('WEBGL_debug_renderer_info');
     return gl.getParameter(e.UNMASKED_VENDOR_WEBGL)+' | '+gl.getParameter(e.UNMASKED_RENDERER_WEBGL);
   } catch(e){return 'n/a';} })(),
  title: document.title,
  url: location.href
}))()""")

print(json.dumps(fp, indent=2))
c.ws.close()
