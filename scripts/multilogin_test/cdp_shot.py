import json, time, base64
import websocket, urllib.request

CDP = "http://127.0.0.1:57909"

def page_ws():
    for p in json.load(urllib.request.urlopen(CDP + "/json")):
        if p.get("type") == "page":
            return p["webSocketDebuggerUrl"]
    raise RuntimeError("no page")

class C:
    def __init__(self, url):
        self.ws = websocket.create_connection(url, timeout=40, suppress_origin=True)
        self.i = 0
    def send(self, m, p=None):
        self.i += 1
        self.ws.send(json.dumps({"id": self.i, "method": m, "params": p or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.i:
                return msg

c = C(page_ws())
time.sleep(2)
shot = c.send("Page.captureScreenshot", {"format": "png"})
data = shot.get("result", {}).get("data")
if data:
    open("reddit_shot.png", "wb").write(base64.b64decode(data))
    print("screenshot saved: reddit_shot.png")
else:
    print("no screenshot:", json.dumps(shot)[:300])

# also report final url + a chunk of visible text
r = c.send("Runtime.evaluate", {"expression":
    "JSON.stringify({url:location.href,title:document.title,bodyText:document.body.innerText.slice(0,400)})",
    "returnByValue": True})
print(r.get("result", {}).get("result", {}).get("value"))
c.ws.close()
