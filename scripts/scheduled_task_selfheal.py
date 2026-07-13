#!/usr/bin/env python3
"""Scheduled-task registry self-heal, run ONLY while Claude Desktop is DOWN
(the running app caches the registry in memory and clobbers a live edit on
its next fire).

WHY THIS IS A STANDALONE SCRIPT, NOT JUST A MENU BAR METHOD (2026-07-08):
this logic used to live only as mcp/menubar/s4l_menubar.py::_rewrite_scheduled_task_cwd,
called in-process from _mcpb_update_work and _relocate_restart_work. Both of
those run INSIDE the already-executing (old) menu bar process, BEFORE that
process quits and relaunches with the just-downloaded new code — Python does
not hot-reload an already-imported module just because newer .py files landed
on disk mid-run. So a fix shipped to that method would only ever take effect
on the update AFTER the one that shipped it: found on a real test box where a
newly-added fix (creating a missing registry) silently did nothing during the
very update that shipped it, because the self-heal call that fired still ran
the OLD in-process code.

The fix: unpack THIS script fresh from the just-downloaded bundle's embedded
pipeline.tgz and run it as a NEW subprocess (see the callers in
mcp/menubar/s4l_menubar.py) — a subprocess always imports whatever is on disk
at invocation time, so it can never be stale the way an in-process call can.
mcp/menubar/s4l_menubar.py::_rewrite_scheduled_task_cwd now delegates to
heal() here for in-process callers where staleness isn't the concern (kept for
back-compat rather than hunting down every caller).

stdlib-only on purpose, matching scripts/schedule_state.py's pattern.
Run as a script -> heals, then prints {"ok": true, ...} as JSON.
"""
from __future__ import annotations

import glob
import ctypes
import ctypes.util
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid as uuidlib

import schedule_state  # noqa: E402  (lives next to this file in scripts/)

WORKER_TASK_ID = "s4l-worker"
WORKER_TASK_IDS = ("s4l-worker", "saps-worker", "saps-phase1-query", "saps-phase2b-draft")
LEGACY_WORKER_TASK_IDS = ("saps-worker", "saps-phase1-query", "saps-phase2b-draft")
DEPRECATED_TASK_IDS = ("social-autoposter-autopilot",)
WORKER_CWD = os.path.join(os.path.expanduser("~"), ".s4l-worker")

# Kept in sync with SCHED_REGISTRY_GLOB in mcp/menubar/s4l_menubar.py,
# scripts/schedule_state.py, scripts/scheduled_tasks_snapshot.py, and
# queueWorkerCwd()/QUEUE_WORKERS in mcp/src/index.ts (same constant,
# necessarily duplicated across languages/processes -- pre-existing pattern).
SCHED_REGISTRY_GLOB = os.path.join(
    os.path.expanduser("~"), "Library", "Application Support", "Claude*",
    "claude-code-sessions", "*", "*", "scheduled-tasks.json",
)

CLAUDE_COOKIE_HOST = ".claude.ai"
CLAUDE_SAFE_STORAGE_SERVICE = "Claude Safe Storage"


def _normalized_uuid(value) -> str | None:
    """Return a canonical UUID string, or None for anything path-unsafe."""
    if not isinstance(value, str):
        return None
    try:
        return str(uuidlib.UUID(value.strip()))
    except (ValueError, AttributeError):
        return None


def _last_active_org_cookie_row(root: str):
    """Read only the lastActiveOrg row from this Claude profile's cookie DB.

    Opening through SQLite (rather than copying/parsing the file ourselves)
    also lets SQLite include a live WAL when Claude is still running. Cookie
    values never leave this helper except as the one selected row.
    """
    db = os.path.join(root, "Cookies")
    if not os.path.isfile(db):
        return None
    conn = None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1)
        return conn.execute(
            """
            SELECT host_key, value, encrypted_value
              FROM cookies
             WHERE name = 'lastActiveOrg'
               AND (host_key = '.claude.ai' OR host_key = 'claude.ai')
             ORDER BY last_access_utc DESC
             LIMIT 1
            """
        ).fetchone()
    except Exception:
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _has_last_active_org_cookie(root: str) -> bool:
    """Cheap capability probe that never reads Keychain or shows a prompt."""
    row = _last_active_org_cookie_row(root)
    if not row:
        return False
    _host, value, encrypted = row
    if _normalized_uuid(value):
        return True
    return isinstance(encrypted, bytes) and encrypted.startswith(b"v10")


def _claude_safe_storage_password() -> bytes | None:
    """Read Electron's per-user cookie key from macOS Keychain.

    There is deliberately no password field in S4L. macOS owns authorization:
    the read commonly succeeds silently, but it may show its standard Keychain
    dialog for the logged-in user. A denial, locked keychain, missing item, or
    unanswered dialog simply makes this best-effort resolver return None.
    """
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-w",
                "-s",
                CLAUDE_SAFE_STORAGE_SERVICE,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
        )
        if result.returncode != 0 or not result.stdout:
            return None
        password = result.stdout
        if password.endswith(b"\r\n"):
            password = password[:-2]
        elif password.endswith(b"\n"):
            password = password[:-1]
        return password or None
    except Exception:
        return None


def _aes_128_cbc_decrypt(ciphertext: bytes, key: bytes, iv: bytes) -> bytes | None:
    """AES-CBC/PKCS7 via macOS CommonCrypto, available through libSystem.

    Keeping this in-process avoids passing the derived cookie key on an
    `openssl -K ...` command line, where another local process could see it.
    """
    if sys.platform != "darwin" or not ciphertext:
        return None
    try:
        system_lib = ctypes.util.find_library("System") or "/usr/lib/libSystem.dylib"
        common_crypto = ctypes.CDLL(system_lib)
        cc_crypt = common_crypto.CCCrypt
        cc_crypt.argtypes = [
            ctypes.c_uint32,  # operation
            ctypes.c_uint32,  # algorithm
            ctypes.c_uint32,  # options
            ctypes.c_void_p, ctypes.c_size_t,  # key, key length
            ctypes.c_void_p,  # IV
            ctypes.c_void_p, ctypes.c_size_t,  # input, input length
            ctypes.c_void_p, ctypes.c_size_t,  # output, output capacity
            ctypes.POINTER(ctypes.c_size_t),  # bytes written
        ]
        cc_crypt.restype = ctypes.c_int32

        key_buf = ctypes.create_string_buffer(key, len(key))
        iv_buf = ctypes.create_string_buffer(iv, len(iv))
        input_buf = ctypes.create_string_buffer(ciphertext, len(ciphertext))
        output_buf = ctypes.create_string_buffer(len(ciphertext) + 16)
        output_len = ctypes.c_size_t()
        status = cc_crypt(
            1,  # kCCDecrypt
            0,  # kCCAlgorithmAES
            1,  # kCCOptionPKCS7Padding
            key_buf, len(key),
            iv_buf,
            input_buf, len(ciphertext),
            output_buf, len(output_buf),
            ctypes.byref(output_len),
        )
        if status != 0:
            return None
        return output_buf.raw[:output_len.value]
    except Exception:
        return None


def _active_org_uuid_from_cookie(root: str) -> str | None:
    """Resolve the active org without any claude-code-sessions directory.

    Chromium's macOS v10 cookie format uses a PBKDF2-derived AES key from the
    app's Safe Storage item. Newer databases bind plaintext to the cookie host
    by prepending SHA-256(host_key); older ones omit that prefix, so accept
    both formats but only return a syntactically valid UUID.
    """
    row = _last_active_org_cookie_row(root)
    if not row:
        return None
    host, value, encrypted = row
    plain_uuid = _normalized_uuid(value)
    if plain_uuid:
        return plain_uuid
    if not isinstance(encrypted, bytes) or not encrypted.startswith(b"v10"):
        return None

    password = _claude_safe_storage_password()
    if not password:
        return None
    key = hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1003, 16)
    clear = _aes_128_cbc_decrypt(encrypted[3:], key, b" " * 16)
    if clear is None:
        return None
    host_digest = hashlib.sha256(str(host).encode("utf-8")).digest()
    if clear.startswith(host_digest):
        clear = clear[len(host_digest):]
    try:
        return _normalized_uuid(clear.decode("utf-8"))
    except UnicodeDecodeError:
        return None


def _ensure_worker_skill_md() -> bool:
    """Make sure ~/.claude/scheduled-tasks/s4l-worker/SKILL.md exists before we
    register a task that points at it. The MCP writes it on every boot
    (create-if-missing), so normally this is a no-op; as a belt-and-suspenders
    fallback we clone a legacy worker's file and fix the frontmatter name."""
    base = os.path.join(os.path.expanduser("~"), ".claude", "scheduled-tasks")
    dst = os.path.join(base, WORKER_TASK_ID, "SKILL.md")
    if os.path.exists(dst):
        return True
    for tid in LEGACY_WORKER_TASK_IDS:
        src = os.path.join(base, tid, "SKILL.md")
        try:
            with open(src) as fh:
                body = fh.read()
        except Exception:
            continue
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "w") as fh:
                fh.write(body.replace(f"name: {tid}", f"name: {WORKER_TASK_ID}", 1))
            return True
        except Exception:
            continue
    return False


def heal() -> dict:
    """Five fixes in one pass, across every scheduled-tasks.json:
      1. Point worker tasks' cwd at ~/.s4l-worker.
      2. REMOVE the deprecated single autopilot task.
      3. CONSOLIDATE every legacy worker entry into ONE s4l-worker entry (the
         universal type-blind worker): drop the legacy entries and, if no
         s4l-worker is registered there yet, add one inheriting the legacy
         cron/enabled state. Migration path for pre-universal-queue installs.
      4. ENSURE an enabled s4l-worker entry exists in EVERY account registry
         that already has a scheduled-tasks.json (the account-switch orphan
         heal, 2026-07-06): switching Claude accounts leaves the task under
         the old account's registry and the new account never fires it.
         Writing the record into every EXISTING registry while Claude is down
         means whichever account the user logs into has the task; copies
         under logged-out accounts are inert. Guarded by user intent: if ANY
         registry holds an explicitly DISABLED worker copy, the user turned
         it off -- add nothing anywhere. (The Quit flow deletes the SKILL.md
         dirs, so worker_skill_ok also gates this from resurrecting a quit
         install.) This restores the June 27 direct-write re-arm (45f1c45d)
         with the targeting problem dissolved by writing everywhere instead
         of guessing the live account.
      5. CREATE a fresh registry for the active account when it has NONE at
         all (2026-07-08): fix 4 only edits files the glob finds -- it can
         never create one where none exists. An account that's never had a
         scheduled-tasks.json (just switched into, never scheduled on this
         box) got nothing written despite fix 4 running: found on a real test
         box where the active account's entire claude-code-sessions/<uuid>/
         tree held zero registry files. Resolves the active account via
         schedule_state.py's config.json lookup (lastKnownAccountUuid,
         verified correct against real installs 2026-07-08) and writes a
         fresh worker entry into its most-recently-touched existing session
         directory. If the account has no directory yet, decrypts Claude
         Desktop's .claude.ai/lastActiveOrg cookie with the current user's
         macOS `Claude Safe Storage` Keychain item and creates the exact
         <account>/<organization> pair. Keychain denial/missing-cookie cases
         fail closed and retain the live-host-tool fallback. Same user-intent
         and worker_skill_ok guards as fix 4.
    Best-effort: never raises. Returns a small summary dict for logging."""
    summary = {"ok": True, "edited": [], "created": [], "error": None}
    try:
        os.makedirs(WORKER_CWD, exist_ok=True)
    except Exception:
        pass
    worker_skill_ok = _ensure_worker_skill_md()

    # Pre-scan for user intent + a template: an explicitly disabled worker
    # copy anywhere means the user opted out -- never re-add. Otherwise clone
    # cron from an existing record so the shape matches what the host wrote.
    any_disabled = False
    tmpl_cron = "* * * * *"
    try:
        for f in glob.glob(SCHED_REGISTRY_GLOB):
            try:
                with open(f) as fh:
                    d = json.load(fh)
            except Exception:
                continue
            for t in (d.get("scheduledTasks") or []):
                if t.get("id") in WORKER_TASK_IDS:
                    if not t.get("enabled", True):
                        any_disabled = True
                    if t.get("cronExpression"):
                        tmpl_cron = t.get("cronExpression")
    except Exception:
        pass

    # Fixes 1-4: edit every EXISTING registry file the glob finds.
    try:
        for f in glob.glob(SCHED_REGISTRY_GLOB):
            try:
                with open(f) as fh:
                    d = json.load(fh)
            except Exception:
                continue
            tasks = d.get("scheduledTasks") or []
            legacy = [t for t in tasks if t.get("id") in LEGACY_WORKER_TASK_IDS]
            has_worker = any(t.get("id") == WORKER_TASK_ID for t in tasks)
            new_tasks = []
            dirty = False
            for t in tasks:
                tid = t.get("id")
                if tid in DEPRECATED_TASK_IDS:
                    dirty = True          # drop it
                    continue
                if tid in LEGACY_WORKER_TASK_IDS and worker_skill_ok:
                    dirty = True          # consolidated into s4l-worker below
                    continue
                if tid in WORKER_TASK_IDS and t.get("cwd") != WORKER_CWD:
                    t["cwd"] = WORKER_CWD
                    dirty = True
                new_tasks.append(t)
            add_worker = worker_skill_ok and not has_worker and (
                legacy                      # fix 3: legacy consolidation
                or not any_disabled         # fix 4: orphan heal (user intent guard)
            )
            if add_worker:
                tmpl = legacy[0] if legacy else {}
                new_tasks.append({
                    "id": WORKER_TASK_ID,
                    "cronExpression": tmpl.get("cronExpression") or tmpl_cron,
                    "enabled": bool(tmpl.get("enabled", True)),
                    "filePath": os.path.join(
                        os.path.expanduser("~"), ".claude",
                        "scheduled-tasks", WORKER_TASK_ID, "SKILL.md",
                    ),
                    # Fresh createdAt keeps schedule_state's CREATED_GRACE
                    # treating the never-yet-fired task as "ok" until its
                    # first fire lands (no ⚠ flap during the restart).
                    "createdAt": int(time.time() * 1000),
                    "cwd": WORKER_CWD,
                })
                dirty = True
            if not dirty:
                continue
            d["scheduledTasks"] = new_tasks
            try:
                fd, tmp = tempfile.mkstemp(dir=os.path.dirname(f))
                with os.fdopen(fd, "w") as fh:
                    json.dump(d, fh, indent=2)
                os.replace(tmp, f)
                summary["edited"].append(f)
            except Exception:
                pass
    except Exception as e:
        summary["error"] = str(e)

    # Fix 5: create a fresh registry for the active account when it has none.
    try:
        if worker_skill_ok and not any_disabled:
            for cfg in schedule_state._config_json_paths():
                root = os.path.dirname(cfg)
                uuid = schedule_state._active_account_uuid(cfg)
                if not uuid:
                    continue
                account_dir = os.path.join(root, "claude-code-sessions", uuid)
                existing = glob.glob(os.path.join(account_dir, "*", "scheduled-tasks.json"))
                if existing:
                    continue  # fixes 1-4 above already cover this account
                session_dirs = [
                    p for p in glob.glob(os.path.join(account_dir, "*"))
                    if os.path.isdir(p)
                ]
                if session_dirs:
                    # Most-recently-touched session dir is the best available
                    # choice when Desktop has already materialized one.
                    target_dir = max(session_dirs, key=lambda p: os.path.getmtime(p))
                else:
                    # A login creates lastActiveOrg before agent mode creates
                    # claude-code-sessions. It is therefore the missing
                    # server-assigned half of the exact account/org path.
                    org_uuid = _active_org_uuid_from_cookie(root)
                    if not org_uuid:
                        continue
                    target_dir = os.path.join(account_dir, org_uuid)
                    try:
                        # Private identity-scoped state. Do not chmod a path
                        # Desktop already made; mode applies only when new.
                        os.makedirs(account_dir, mode=0o700, exist_ok=True)
                        os.makedirs(target_dir, mode=0o700, exist_ok=True)
                    except Exception:
                        continue
                target_file = os.path.join(target_dir, "scheduled-tasks.json")
                new_entry = {
                    "id": WORKER_TASK_ID,
                    "cronExpression": tmpl_cron,
                    "enabled": True,
                    "filePath": os.path.join(
                        os.path.expanduser("~"), ".claude",
                        "scheduled-tasks", WORKER_TASK_ID, "SKILL.md",
                    ),
                    "createdAt": int(time.time() * 1000),
                    "cwd": WORKER_CWD,
                }
                try:
                    fd, tmp = tempfile.mkstemp(dir=target_dir)
                    with os.fdopen(fd, "w") as fh:
                        json.dump(
                            {"scheduledTasks": [new_entry], "recordedSkips": []},
                            fh, indent=2,
                        )
                    os.replace(tmp, target_file)
                    summary["created"].append(target_file)
                except Exception:
                    pass
    except Exception as e:
        summary["error"] = summary["error"] or str(e)

    # Remove retired tasks' on-disk SKILL.md dirs too, so they can't be
    # re-registered from a stale prompt file (and the MCP's boot refresh
    # stops resurrecting the legacy prompts).
    try:
        retired = list(DEPRECATED_TASK_IDS)
        if worker_skill_ok:
            retired += list(LEGACY_WORKER_TASK_IDS)
        for tid in retired:
            shutil.rmtree(os.path.join(os.path.expanduser("~"), ".claude",
                                       "scheduled-tasks", tid), ignore_errors=True)
    except Exception:
        pass

    return summary


def can_create_for_active_account() -> bool:
    """Read-only: would fix 5 (see heal()) actually be able to create a fresh
    registration right now? True if the active account has either an existing
    session directory or a plausible lastActiveOrg cookie. This passive menu/
    status probe deliberately does NOT read Keychain (which could show an
    authorization dialog); heal() performs and validates decryption only after
    the user chooses the restart action. Any denial then fails closed and the
    post-restart verification points the user to the manual re-arm fallback."""
    try:
        for cfg in schedule_state._config_json_paths():
            root = os.path.dirname(cfg)
            uuid = schedule_state._active_account_uuid(cfg)
            if not uuid:
                continue
            account_dir = os.path.join(root, "claude-code-sessions", uuid)
            session_dirs = [
                p for p in glob.glob(os.path.join(account_dir, "*"))
                if os.path.isdir(p)
            ]
            if session_dirs or _has_last_active_org_cookie(root):
                return True
    except Exception:
        pass
    return False


def main() -> int:
    out = heal()
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
