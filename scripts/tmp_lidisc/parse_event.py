import sys, json
try:
    e = json.load(sys.stdin)
except Exception as ex:
    print("PARSE ERR", ex); sys.exit()
tags = {t["key"]: t["value"] for t in e.get("tags", [])}
print("title:", e.get("title"))
print("culprit:", e.get("culprit"))
print("dateCreated:", e.get("dateCreated"))
for k in ("release", "environment", "server_name", "component", "handled",
          "install_id", "level", "logType", "context", "transaction",
          "exit_code", "attempted", "failure_reasons", "runtime"):
    if k in tags:
        print("tag %s: %s" % (k, tags[k]))
msg = e.get("message") or (e.get("logentry") or {}).get("formatted")
if msg:
    print("message:", str(msg)[:500])
for ent in e.get("entries", []):
    if ent.get("type") == "exception":
        for v in ent["data"].get("values", []):
            print("EXC:", v.get("type"), "-", str(v.get("value"))[:300])
            frames = (v.get("stacktrace") or {}).get("frames") or []
            for f in frames[-7:]:
                fn = f.get("filename") or f.get("absPath") or ""
                print("   %s:%s in %s" % (fn, f.get("lineNo"), f.get("function")))
    if ent.get("type") == "message":
        print("MSG-ENTRY:", str(ent["data"].get("formatted"))[:500])
