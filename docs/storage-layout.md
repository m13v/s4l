# S4L plugin storage layout

What the S4L Claude Desktop plugin stores, and where. Source of truth for the
path constants is `mcp/src/runtime.ts` and `mcp/src/index.ts`; the reset flow in
`scripts/reset-test-machine.sh` must stay in sync with every path here.

## Three storage zones, three lifetimes

| Zone | Location | Lifetime | Holds |
|------|----------|----------|-------|
| Extension dir | `~/Library/Application Support/Claude/Claude Extensions/local.mcpb.s4l.ai.social-autoposter/` | Wiped + rewritten on every `.mcpb` update | Code only (`dist/index.js`, `dist/pipeline.tgz`, `manifest.json`) |
| State dir | `~/.social-autoposter-mcp/` (override: `SAPS_STATE_DIR`) | Durable, survives updates | Everything load-bearing |
| System wiring | `~/Library/LaunchAgents/`, `~/.claude.json`, `~/.s4l-worker/`, `~/.claude/scheduled-tasks/` | OS-level, persistent | Supervisors, Cowork registration, scheduler |

Rule of thumb: **never anchor anything durable in the extension dir** — it is
volatile. The menu bar, runtime, and review queue all point at the state dir for
exactly this reason.

## Extension dir (Chat tab) — volatile

```
~/Library/Application Support/Claude/Claude Extensions/
  local.mcpb.s4l.ai.social-autoposter/
    dist/index.js        # the compiled MCP server Claude Desktop launches
    dist/pipeline.tgz    # embedded Python pipeline (exact `npm pack` output)
    manifest.json
```

The Chat tab loads this via Claude Desktop's `LocalMcpServerManager`. Every
update replaces the whole directory, so the absolute path can change (and the
dir name can shift if the manifest author field changes — the box updater
glob-detects it).

## State dir (`~/.social-autoposter-mcp/`) — durable

```
~/.social-autoposter-mcp/
  runtime/
    .venv/bin/python3        # owned CPython 3.12 interpreter (absolute, no activation)
    python/                  # uv-installed standalone CPython
    (Chromium resolved + recorded in runtime.json)
  repo/package/              # pipeline source materialized from pipeline.tgz
                             # (npm tarballs unpack under package/, so repo root = repo/package)
  runtime.json               # durable record: python path, repo_dir, chromium path, last-materialized version
  install-progress.json      # step-by-step provisioning state (panel polls this)
  mode.json                  # engagement lanes {personal_brand, promotion, mode}
  review-request.json        # the draft cards the menu bar presents for approval
  menubar/
    s4l_menubar.py           # STABLE menu-bar copy (NOT the extension dir, so updates don't kill it)
    menubar.out.log
    menubar.err.log
```

### Pipeline resolution (dynamic, per-call)

`repoDir()` in `mcp/src/runtime.ts` resolves in this order so a first-run
materialize is picked up without a server restart:

1. `SAPS_REPO_DIR` when it is a real clone (npm/git installs keep their working tree).
2. `runtime.json`'s `repo_dir` (the materialized repo from a `.mcpb` install).
3. The materialized `repo/package` path on disk even if `runtime.json` is missing.
4. `SAPS_REPO_DIR` as-is, then the two-levels-up dev fallback.

A directory only counts as a repo if it has both `requirements.txt` and
`scripts/`. `resolvePython()` mirrors this: owned venv → `SAPS_PYTHON` → bare
`python3`.

## System wiring

### LaunchAgents (`~/Library/LaunchAgents/`)

| Label | Job |
|-------|-----|
| `com.m13v.social-twitter-cycle` | Autopilot kicker |
| `com.m13v.social-claude-reaper` | Kills leaked `claude` agent-mode sessions (~200MB each) |
| `com.m13v.social-memory-snapshot` | 60s host-resource sampler |
| `com.m13v.social-autopilot-stall-watch` | 120s Sentry backstop for stalled autopilot |
| `com.m13v.social-overlay-watch` | KeepAlive foreground overlay watcher |
| `com.m13v.social-autoposter-update` | Daily self-updater |
| `com.m13v.social-autoposter.menubar` | The menu-bar app |

The reset script historically missed `stall-watch` / `claude-reaper` /
`memory-snapshot`; if they are left loaded they resurrect a half-wiped state
dir. Boot them out before deleting the state dir.

### Cowork / Code tab registration (`~/.claude.json`)

The Code tab is a separate embedded `claude-code` launched with
`--setting-sources=user`. It reads `mcpServers` from `~/.claude.json`, NOT the
`.mcpb`. `ensureCoworkMcpRegistered()` (in `mcp/src/index.ts`, called from
`main()`) writes:

```json
"mcpServers": {
  "social-autoposter": {
    "command": "node",
    "args": [".../Claude Extensions/local.mcpb.s4l.ai.social-autoposter/dist/index.js"],
    "env": { "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" }
  }
}
```

It rewrites on every boot (idempotent, atomic, preserves other keys) so the path
self-corrects after an update relocates the extension dir. Kill switch:
`SAPS_COWORK_MCP=0`. Caveat: the Cowork host's `--allowedTools` allowlist may
prompt for permission on the first tool call; the tools still load and appear.

### Queue worker

```
~/.s4l-worker/                                # dedicated cwd for the two draft routines
                                              # (keeps scheduled-task sessions out of the resume picker)
~/.claude/scheduled-tasks/<taskId>/SKILL.md   # per-routine instructions, pre-written at boot
```

## Logs and misc

```
<repo>/skill/logs/memory-snapshots.jsonl      # rotated host snapshots (read when SSHing a box)
~/.social-autoposter/identity.json            # anonymous install fingerprint (X-Installation header)
```
