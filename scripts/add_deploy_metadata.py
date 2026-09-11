#!/usr/bin/env python3
"""Add a `deploy` object to each project in config.json so automated agents
know how to trigger a production deploy after pushing code changes.

Shape:
  "deploy": {
    "target": "vercel" | "cloudrun" | "manual",
    "production_trigger": "push:main" | "tag:v*" | "manual",
    "staging_url": "https://..."    # optional, only when a staging env exists
  }

Preserves all existing keys and ordering within the landing_pages block.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config_io import save_config

CONFIG = Path.home() / "social-autoposter" / "config.json"

DEPLOY_BY_NAME = {
    # Vercel, push-to-main auto-deploys production. No staging.
    "fazm":                  {"target": "vercel",   "production_trigger": "push:main"},
    "Terminator":            {"target": "vercel",   "production_trigger": "push:main"},
    "macOS MCP":             {"target": "vercel",   "production_trigger": "push:main"},
    "Vipassana":             {"target": "vercel",   "production_trigger": "push:main"},
    "S4L":                   {"target": "vercel",   "production_trigger": "push:main"},
    "Cyrano":                {"target": "vercel",   "production_trigger": "push:main"},
    "Assrt":                 {"target": "vercel",   "production_trigger": "push:main"},
    "PieLine":               {"target": "vercel",   "production_trigger": "push:main"},
    "Clone":                 {"target": "vercel",   "production_trigger": "push:main"},
    # Cloud Run, GitHub Actions deploys production on push-to-main. No staging.
    "fde10x":                {"target": "cloudrun", "production_trigger": "push:main"},
    "claude-meter":          {"target": "cloudrun", "production_trigger": "push:main"},
    "c0nsl":                 {"target": "cloudrun", "production_trigger": "push:main"},
    "tenxats":               {"target": "cloudrun", "production_trigger": "push:main"},
    "paperback-expert":      {"target": "cloudrun", "production_trigger": "push:main"},
    # Cloud Run with staging/production split. Push-to-main goes to staging,
    # a `v*` git tag promotes to production. Agents MUST cut and push a tag
    # (e.g. `git tag v0.3.27 && git push origin v0.3.27`) after merging to
    # main for changes to reach the live site.
    "mk0r":                  {
        "target": "cloudrun",
        "production_trigger": "tag:v*",
        "staging_url": "https://staging.mk0r.com",
    },
    # No deployment pipeline wired.
    "WhatsApp MCP":          {"target": "manual", "production_trigger": "manual"},
    "macOS Session Replay":  {"target": "manual", "production_trigger": "manual"},
}


def main():
    cfg = json.loads(CONFIG.read_text())
    changed = 0
    for proj in cfg.get("projects", []):
        name = proj.get("name")
        if name not in DEPLOY_BY_NAME:
            continue
        lp = proj.get("landing_pages")
        if not lp:
            continue
        if lp.get("deploy") == DEPLOY_BY_NAME[name]:
            continue
        lp["deploy"] = DEPLOY_BY_NAME[name]
        changed += 1
    # Atomic write (flock + tmp + os.replace); a plain write_text truncates
    # config.json for concurrent readers (2026-09-09 root cause).
    save_config(cfg, cfg_path=str(CONFIG))
    print(f"updated {changed} project(s) in {CONFIG}")


if __name__ == "__main__":
    main()
