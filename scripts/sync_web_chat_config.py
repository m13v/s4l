#!/usr/bin/env python3
"""Sync web_chat config from social-autoposter/config.json into the
social-autoposter-website repo's projects.ts allowlist.

Reads:
  ~/social-autoposter/config.json  -> projects[].web_chat blocks

Writes:
  ~/social-autoposter-website/src/lib/web-chat/projects.ts

The website allowlist is what the public /api/web-chat/* routes validate
against. Whenever you add or change a `web_chat` block in config.json, run
this script then commit + push the website (Vercel auto-deploys):

  python3 ~/social-autoposter/scripts/sync_web_chat_config.py
  cd ~/social-autoposter-website && git add -A && git commit -m "sync web-chat config" && git push

Idempotent. Safe to re-run.
"""
import json
import os
import sys
from datetime import datetime, timezone

CONFIG_PATH = os.path.expanduser("~/social-autoposter/config.json")
TARGET_PATH = os.path.expanduser("~/social-autoposter-website/src/lib/web-chat/projects.ts")

DEFAULTS = {
    "founder_name": "matt",
    "reply_eta": "usually replies within a couple hours",
    "from_email": "Matt <matt@mail.omi.me>",
    "notify_email": "i@m13v.com",
}


def main():
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)

    rows = []
    for p in cfg.get("projects", []):
        wc = p.get("web_chat") or {}
        if not isinstance(wc, dict):
            continue
        # Only emit rows for projects where someone has at least added a
        # web_chat block. enabled may be true or false; both are written
        # so the API route can return a sensible "project_not_enabled" 404
        # instead of "unknown_project".
        rows.append({
            "name": p.get("name") or "",
            "website": (p.get("website") or "").rstrip("/"),
            "enabled": bool(wc.get("enabled")),
            "notify_email": wc.get("notify_email") or DEFAULTS["notify_email"],
            "founder_name": wc.get("founder_name") or DEFAULTS["founder_name"],
            "reply_eta": wc.get("reply_eta") or DEFAULTS["reply_eta"],
            "from_email": wc.get("from_email") or DEFAULTS["from_email"],
        })

    rows.sort(key=lambda r: r["name"].lower())

    def js_str(s: str) -> str:
        return json.dumps(s, ensure_ascii=False)

    body_entries = []
    for r in rows:
        if not r["name"]:
            continue
        key = r["name"]
        body_entries.append(
            "  " + json.dumps(key) + ": {\n"
            + f"    name: {js_str(r['name'])},\n"
            + f"    website: {js_str(r['website'])},\n"
            + f"    enabled: {'true' if r['enabled'] else 'false'},\n"
            + f"    notifyEmail: {js_str(r['notify_email'])},\n"
            + f"    founderName: {js_str(r['founder_name'])},\n"
            + f"    replyEta: {js_str(r['reply_eta'])},\n"
            + f"    fromEmail: {js_str(r['from_email'])},\n"
            + "  }"
        )

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out = (
        "// AUTO-GENERATED. Do not edit by hand.\n"
        "// Source: ~/social-autoposter/config.json projects[].web_chat\n"
        f"// Synced:  {stamp}\n"
        "// Re-run:  python3 ~/social-autoposter/scripts/sync_web_chat_config.py\n"
        "//\n"
        "// The public /api/web-chat/* routes validate `project` against this\n"
        "// allowlist. Adding a new site is a 3-step ritual:\n"
        "//   1. add `web_chat` block to config.json\n"
        "//   2. run this script\n"
        "//   3. commit + push (Vercel auto-deploys)\n"
        "\n"
        "export interface WebChatProjectConfig {\n"
        "  name: string;\n"
        "  website: string;\n"
        "  enabled: boolean;\n"
        "  notifyEmail: string;\n"
        "  founderName: string;\n"
        "  replyEta: string;\n"
        "  fromEmail: string;\n"
        "}\n"
        "\n"
        "export const WEB_CHAT_PROJECTS: Record<string, WebChatProjectConfig> = {\n"
        + ",\n".join(body_entries)
        + ("\n" if body_entries else "")
        + "};\n"
        "\n"
        "export function getEnabledProject(name: string): WebChatProjectConfig | null {\n"
        "  const p = WEB_CHAT_PROJECTS[name];\n"
        "  if (!p || !p.enabled) return null;\n"
        "  return p;\n"
        "}\n"
        "\n"
        "export function corsAllowList(): string[] {\n"
        "  const out = new Set<string>();\n"
        "  for (const p of Object.values(WEB_CHAT_PROJECTS)) {\n"
        "    if (!p.enabled) continue;\n"
        "    const u = p.website.replace(/\\/+$/, \"\");\n"
        "    if (u) {\n"
        "      out.add(u);\n"
        "      out.add(u.includes(\"://www.\") ? u.replace(\"://www.\", \"://\") : u.replace(\"://\", \"://www.\"));\n"
        "    }\n"
        "  }\n"
        "  out.add(\"http://localhost:3000\");\n"
        "  out.add(\"http://localhost:3001\");\n"
        "  return Array.from(out);\n"
        "}\n"
    )

    os.makedirs(os.path.dirname(TARGET_PATH), exist_ok=True)
    with open(TARGET_PATH, "w") as f:
        f.write(out)

    enabled = sum(1 for r in rows if r["enabled"])
    total = len(rows)
    print(f"wrote {TARGET_PATH}  ({enabled} enabled / {total} total)")
    for r in rows:
        flag = "[on]" if r["enabled"] else "[off]"
        print(f"  {flag} {r['name']:<24} -> {r['notify_email']}")


if __name__ == "__main__":
    main()
