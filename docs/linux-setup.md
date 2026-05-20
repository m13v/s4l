# Running social-autoposter on Linux

This guide covers bringing up the posting pipeline on a fresh Linux VM.
The macOS launchd path is the reference implementation; on Linux the
installer translates every shipped plist into a systemd user unit pair
(`.service` + `.timer`).

**Out of scope for this guide:** cookie/credential bootstrap for the
browser agents. This document only covers the *shell* and *MCP-server*
side of the pipeline. Seeding `~/.claude/browser-profiles/{twitter,
reddit,linkedin}/` with a logged-in session is a separate concern.

---

## 1. OS prerequisites

Tested on Ubuntu 24.04 LTS (should work on any systemd-based distro).

```bash
sudo apt update
sudo apt install -y \
  build-essential \
  nodejs npm \
  python3 python3-pip python3-venv \
  postgresql-client \
  coreutils util-linux \
  git curl \
  xvfb \
  chromium-browser
```

Notes:
- `coreutils` provides `timeout`; `lib/platform.sh` aliases it to `gtimeout`.
- `postgresql-client` provides `psql` for the Neon DB writes in several scripts.
- `chromium-browser` satisfies Playwright's browser dependency for the MCP
  browser agents. On some distros the package is named `chromium`.
- `xvfb` is only needed on headless VMs where the browser agents must run
  without a display. See the MCP section below.

Verify:

```bash
node -v       # >= 18
python3 -V    # >= 3.10
psql --version
timeout --version
```

---

## 2. Enable lingering so timers fire without login

systemd user units stop when the user's last session ends. To keep the
pipeline running on a VM that nobody stays SSH'd into:

```bash
sudo loginctl enable-linger $USER
```

Without this, timers silently stop between SSH sessions.

---

## 3. Install social-autoposter

From the target user's `$HOME`:

```bash
cd ~
npx social-autoposter init
```

The installer will:
1. Copy the skill tree to `~/social-autoposter/`.
2. Generate `~/social-autoposter/launchd/*.plist` (kept on Linux as the source
   of truth, since their schedules and env are easier to edit there).
3. Translate every plist into a matching `.service` + `.timer` pair at
   `~/social-autoposter/systemd/`, normalizing absolute paths against your
   `$HOME` (no publisher hardcodes leak through).
4. Symlink both units into `~/.config/systemd/user/` and run
   `systemctl --user daemon-reload`.
5. Install browser-agent MCP configs to `~/.claude/browser-agent-configs/`
   and create empty profile directories at `~/.claude/browser-profiles/{twitter,reddit,linkedin}/`.
6. Symlink the skill into `~/.claude/skills/social-autoposter`.

After init, the units are **linked but not enabled**. Nothing runs until
you explicitly enable the timers you want (step 5).

---

## 4. Environment + database

```bash
cd ~/social-autoposter
cp config.example.json config.json   # if it does not already exist
$EDITOR config.json                  # fill in accounts, projects
$EDITOR .env                         # DATABASE_URL, API keys
```

The schema lives at `~/social-autoposter/schema-postgres.sql`. Apply it
against your Neon database once:

```bash
psql "$DATABASE_URL" -f schema-postgres.sql
```

---

## 5. Enable timers selectively

Do not enable all 36 timers blindly. Pick what you actually want running:

```bash
# list every installed timer
systemctl --user list-unit-files 'com.m13v.social-*.timer'

# enable + start a specific job (example: the main Reddit reply scanner)
systemctl --user enable --now com.m13v.social-scan-reddit-replies.timer

# watch what fires
systemctl --user list-timers 'com.m13v.social-*'

# view logs for a specific job
journalctl --user -u com.m13v.social-scan-reddit-replies.service -n 200 -f
```

To stop a job:

```bash
systemctl --user disable --now com.m13v.social-scan-reddit-replies.timer
```

To pause the whole pipeline without removing units, disable every
`com.m13v.social-*.timer` in one shot:

```bash
systemctl --user list-unit-files 'com.m13v.social-*.timer' --no-legend --state=enabled \
  | awk '{print $1}' \
  | xargs -r systemctl --user disable --now
```

---

## 6. Bringing up the MCP browser servers

The pipeline delegates every social-platform browser action to a
per-platform Playwright MCP server. These are spawned on demand by
Claude via the configs installed at step 3.5. There is nothing to
`systemctl enable` for them; they are stdio servers, launched as
child processes each time Claude needs them.

### 6.1 Configs installed by `init`

```
~/.claude/browser-agent-configs/
  reddit-agent.json
  reddit-agent-mcp.json
  linkedin-agent.json
  linkedin-agent-mcp.json
```

Each `*-mcp.json` launches `npx @playwright/mcp@latest --config
~/.claude/browser-agent-configs/<agent>.json`. The non-MCP config sets
`userDataDir: ~/.claude/browser-profiles/<platform>` so cookies and
logged-in state persist across invocations.

Twitter is intentionally absent: as of 2026-05-19 the Twitter pipeline is
driven exclusively by the browser-harness MCP (`twitter-harness-mcp.json`,
profile `~/.claude/browser-profiles/browser-harness`), which is installed
out-of-band as part of the browser-harness setup rather than by this
installer.

### 6.2 Register the servers with Claude

`init` and `update` auto-register the two servers (`reddit-agent`,
`linkedin-agent`) by shelling out to `claude mcp add-json ...` if the
`claude` CLI is on `PATH`. Registration is idempotent: entries already
present in `~/.claude.json` are left untouched. The `twitter-harness`
MCP must be registered separately as part of the browser-harness setup.

Verify:

```bash
claude mcp list | grep -E '(reddit|linkedin)-agent|twitter-harness'
```

If the `claude` CLI was not on `PATH` when you ran `init`, the
installer prints fallback commands. To register manually later:

```bash
for agent in reddit linkedin; do
  claude mcp add-json "${agent}-agent" \
    "$(jq -c '.mcpServers["'${agent}'-agent"]' \
         ~/.claude/browser-agent-configs/${agent}-agent-mcp.json)"
done
```

Re-running `npx social-autoposter update` also catches up on any
missing registrations.

The pipeline shell scripts reference these servers by their exact names
(`mcp__reddit-agent__*`, `mcp__linkedin-agent__*`, and
`mcp__twitter-harness__bh_run` for Twitter, registered separately as part
of the browser-harness setup). If you rename an MCP server here, the
prompts inside `skill/run-*.sh` will not find it.

### 6.3 Headless VMs: wrap Chromium under Xvfb

Playwright in headful mode requires a display. On a VM with no monitor,
wrap each MCP invocation under Xvfb by editing the `command` in each
`*-agent-mcp.json`:

```json
{
  "mcpServers": {
    "reddit-agent": {
      "type": "stdio",
      "command": "xvfb-run",
      "args": [
        "-a",
        "--server-args=-screen 0 1920x1080x24",
        "npx",
        "@playwright/mcp@latest",
        "--config",
        "/home/YOURUSER/.claude/browser-agent-configs/reddit-agent.json"
      ]
    }
  }
}
```

Alternatively, start Xvfb once as a user service and export `DISPLAY`
before `claude` is invoked. A minimal service at
`~/.config/systemd/user/xvfb.service`:

```ini
[Unit]
Description=Virtual framebuffer for headless browser agents

[Service]
ExecStart=/usr/bin/Xvfb :99 -screen 0 1920x1080x24
Restart=always

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now xvfb.service
```

Then set `DISPLAY=:99` in the environment of every pipeline script (for
example via `Environment=DISPLAY=:99` in each `.service`, or by
exporting it in the shell scripts themselves). Easiest path: append to
`~/social-autoposter/.env`.

### 6.4 Profile seeding (deferred)

Cookie/credential bootstrap for the three browser profiles is
intentionally out of scope for this guide. Future runbook will cover:
exporting cookies from a trusted Chrome session, writing them into
`~/.claude/browser-profiles/<platform>/`, and confirming the agent opens
the feed logged in.

For now, assume the profiles are either pre-seeded out of band or that
every run will fail at the first navigation with `SESSION_INVALID`.

---

## 7. Smoke test

After `init`, linger, at least one enabled timer, and the MCP servers
registered:

```bash
# trigger one job immediately (does not wait for the timer)
systemctl --user start com.m13v.social-scan-reddit-replies.service

# tail the log
journalctl --user -u com.m13v.social-scan-reddit-replies.service -f
```

If the run finishes without `SESSION_INVALID` and you see a row land in
the `posts`/`replies` table, the pipeline is wired correctly.

---

## 8. Upgrading

Re-run:

```bash
cd ~
npx social-autoposter update
```

`update` regenerates plists, re-derives systemd units against the
current `$HOME`, relinks them under `~/.config/systemd/user/`, and runs
`daemon-reload`. It does not re-enable timers, so any timer you
disabled stays disabled.

---

## 9. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `systemctl --user` says "Failed to connect to bus" over SSH | Run `loginctl enable-linger $USER`, then reconnect. |
| Timer is enabled but `list-timers` shows no next firing | Check `journalctl --user -u <service>` for a failing preconditon; most commonly a missing env var in `.env`. |
| Browser agent fails to start with `missing X server or $DISPLAY` | You are on a headless VM without Xvfb. Wire up section 6.3. |
| Agent opens browser but every nav returns `SESSION_INVALID` | Profile under `~/.claude/browser-profiles/<platform>/` is empty or stale. Seed cookies (deferred, see 6.4). |
| `gtimeout: command not found` from a shell script | The script did not source `skill/lib/platform.sh`. Add `source "$(dirname "${BASH_SOURCE[0]}")/lib/platform.sh"` after `set -uo pipefail`. |
| `psql: command not found` inside a service | `postgresql-client` not installed, or `PATH` in the unit does not include `/usr/bin`. Add `Environment=PATH=/usr/bin:/usr/local/bin` to the `.service`. |

---

## 10. What is NOT yet Linux-ready

- macOS-specific notifications (`osascript`, `terminal-notifier`) in
  `lib/platform.sh::platform_notify` are no-ops on Linux.
- Any script that shells out to `open`, `pbcopy`, or `pbpaste` will
  fail. These are infrequent but not audited exhaustively.
- The dashboard at `bin/server.js` has only been smoke-tested on
  macOS; the server side is plain Node and should work, but the
  launchd-specific labels in the UI may read oddly.

File bugs against the repo when one of these bites.
