#!/usr/bin/env python3
"""Passive installation identity for the open-source social-autoposter client.

Generates and stores a stable install_id on first run plus a snapshot of
machine fingerprint fields. Every API call to social-autoposter-website
carries this as an X-Installation header (base64 JSON) so the server can
attribute writes per install, rate-limit, and surface usage without
requiring any user signup.

NO data is sent until the pipeline actually calls the API. NO secrets are
collected. Every field captured is documented in PRIVACY.md at the repo
root.

CLI:
    python3 scripts/identity.py show     # print identity JSON
    python3 scripts/identity.py header   # print base64 X-Installation value
    python3 scripts/identity.py reset    # delete identity.json
    python3 scripts/identity.py path     # print path to identity.json

Library:
    from scripts.identity import get_identity, get_identity_header
    headers = {"X-Installation": get_identity_header()}
"""

from __future__ import annotations

import base64
import json
import os
import platform
import subprocess
import sys
import time
import uuid
from pathlib import Path

IDENTITY_DIR = Path.home() / ".social-autoposter"
IDENTITY_FILE = IDENTITY_DIR / "identity.json"


def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:
        return None


def _hardware_uuid_macos():
    out = _safe(
        subprocess.check_output,
        ["ioreg", "-d2", "-c", "IOPlatformExpertDevice"],
        stderr=subprocess.DEVNULL, timeout=5,
    )
    if not out:
        return None
    for line in out.decode("utf8", errors="ignore").splitlines():
        if "IOPlatformUUID" in line:
            parts = line.split('"')
            if len(parts) >= 4:
                return parts[3].strip()
    return None


def _hardware_uuid_linux():
    for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(p) as f:
                v = f.read().strip()
                if v:
                    return v
        except Exception:
            continue
    return None


def _hardware_uuid_windows():
    out = _safe(
        subprocess.check_output,
        ["reg", "query",
         r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Cryptography",
         "/v", "MachineGuid"],
        stderr=subprocess.DEVNULL, timeout=5,
    )
    if not out:
        return None
    for line in out.decode("utf8", errors="ignore").splitlines():
        if "MachineGuid" in line:
            tokens = line.split()
            if tokens:
                return tokens[-1].strip()
    return None


def _hardware_uuid():
    sys_name = platform.system().lower()
    if sys_name == "darwin":
        return _hardware_uuid_macos()
    if sys_name == "linux":
        return _hardware_uuid_linux()
    if sys_name == "windows":
        return _hardware_uuid_windows()
    return None


def _hostname():
    sys_name = platform.system().lower()
    if sys_name == "darwin":
        out = _safe(
            subprocess.check_output,
            ["scutil", "--get", "ComputerName"],
            stderr=subprocess.DEVNULL, timeout=5,
        )
        if out:
            v = out.decode("utf8", errors="ignore").strip()
            if v:
                return v
    try:
        import socket
        return socket.gethostname() or None
    except Exception:
        return None


def _git_email():
    out = _safe(
        subprocess.check_output,
        ["git", "config", "--global", "user.email"],
        stderr=subprocess.DEVNULL, timeout=3,
    )
    if not out:
        return None
    v = out.decode("utf8", errors="ignore").strip()
    return v or None


def _node_version():
    out = _safe(
        subprocess.check_output,
        ["node", "--version"],
        stderr=subprocess.DEVNULL, timeout=3,
    )
    if not out:
        return None
    v = out.decode("utf8", errors="ignore").strip()
    return v.lstrip("v") or None


def _app_version():
    """Version of the installed S4L plugin.

    On a .mcpb box the extension dir has manifest.json + package.json at its
    root (one level above scripts/); read whichever resolves first. Honors
    S4L_REPO_DIR / REPO_DIR when the pipeline sets it (launchd plists do).
    """
    root = Path(
        os.environ.get("S4L_REPO_DIR")
        or os.environ.get("REPO_DIR")
        or Path(__file__).resolve().parents[1]
    )
    for name in ("manifest.json", "package.json"):
        try:
            data = json.loads((root / name).read_text())
        except Exception:
            continue
        v = data.get("version")
        if v:
            return str(v).strip() or None
    return None


def _claude_desktop_version():
    """CFBundleShortVersionString of the Claude Desktop app (macOS), or None.

    Stamped into the install identity (and thus the X-Installation header on every
    heartbeat) so the install-lane digest can correlate leaks/regressions with the
    Desktop version. This is the variable we could not answer for Karol's box. Reads
    Info.plist directly via plistlib; best-effort, never raises."""
    if (platform.system() or "").lower() != "darwin":
        return None
    candidates = [
        Path("/Applications/Claude.app/Contents/Info.plist"),
        Path.home() / "Applications" / "Claude.app" / "Contents" / "Info.plist",
    ]
    for plist in candidates:
        try:
            if not plist.exists():
                continue
            import plistlib

            with plist.open("rb") as f:
                data = plistlib.load(f)
            v = data.get("CFBundleShortVersionString") or data.get("CFBundleVersion")
            if v:
                return str(v).strip() or None
        except Exception:
            continue
    return None


def _tz():
    try:
        from datetime import datetime
        tz = datetime.now().astimezone().tzinfo
        if tz is not None:
            name = tz.tzname(datetime.now())
            if name:
                return name
    except Exception:
        pass
    return os.environ.get("TZ") or None


def _build_fresh_identity():
    return {
        "install_id": str(uuid.uuid4()),
        "hardware_uuid": _hardware_uuid(),
        "hostname": _hostname(),
        "os": (platform.system() or "").lower() or None,
        "os_version": platform.release() or None,
        "cpu_arch": platform.machine() or None,
        "python_version": platform.python_version() or None,
        "node_version": _node_version(),
        "app_version": _app_version(),
        "claude_desktop_version": _claude_desktop_version(),
        "git_email": _git_email(),
        "tz": _tz(),
        "first_seen_at": int(time.time()),
    }


IDENTITY_BACKUP = IDENTITY_DIR / "identity.json.bak"


def _atomic_write(path: Path, data: dict) -> None:
    """Write via tmp + os.replace so concurrent writers can never leave a
    torn/half-written file. The 2026-07-03 install_id loss was exactly this:
    several pipelines rewriting identity.json with plain write_text() after a
    hostname change, one reader hit the torn file, and the corrupt-rebuild
    path silently minted a new install_id, orphaning all server-side data."""
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2))
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    os.replace(tmp, path)


def _read_valid(path: Path):
    """Return the parsed identity dict if path holds one with an install_id."""
    try:
        d = json.loads(path.read_text())
        if isinstance(d, dict) and d.get("install_id"):
            return d
    except Exception:
        pass
    return None


def get_identity(refresh: bool = False) -> dict:
    """Read identity.json, creating it on first call.

    refresh=True re-snapshots the volatile fields (versions, hostname,
    git_email, tz) while preserving install_id and first_seen_at.

    install_id is PERSISTENT: a corrupt or missing primary file recovers from
    identity.json.bak before ever minting a new id, and every successful
    write refreshes the backup. Minting a brand-new id is a loud last resort.
    """
    IDENTITY_DIR.mkdir(parents=True, exist_ok=True)

    ident = _read_valid(IDENTITY_FILE)

    if ident is None:
        # Primary missing or corrupt: recover the stable id from the backup
        # rather than orphaning the install's server-side history.
        backup = _read_valid(IDENTITY_BACKUP)
        if backup is not None:
            ident = _build_fresh_identity()
            ident["install_id"] = backup["install_id"]
            ident["first_seen_at"] = backup.get("first_seen_at") or ident["first_seen_at"]
            print(f"[identity] primary missing/corrupt; recovered install_id from {IDENTITY_BACKUP}",
                  file=sys.stderr)
        else:
            ident = _build_fresh_identity()
            print(f"[identity] minted NEW install_id {ident['install_id']} "
                  "(no valid identity.json or backup found); server-side data "
                  "keyed to a previous id will not be visible to this client",
                  file=sys.stderr)
        try:
            _atomic_write(IDENTITY_FILE, ident)
            _atomic_write(IDENTITY_BACKUP, ident)
        except Exception:
            pass
        return ident

    if refresh:
        snap = _build_fresh_identity()
        # preserve stable identifiers across refresh
        snap["install_id"] = ident.get("install_id") or snap["install_id"]
        snap["first_seen_at"] = ident.get("first_seen_at") or snap["first_seen_at"]
        if snap != ident:
            try:
                _atomic_write(IDENTITY_FILE, snap)
            except Exception:
                pass
        ident = snap

    # Keep the backup carrying the current install_id (cheap: only rewrite
    # when the id it holds differs or it is missing/corrupt).
    backup = _read_valid(IDENTITY_BACKUP)
    if backup is None or backup.get("install_id") != ident.get("install_id"):
        try:
            _atomic_write(IDENTITY_BACKUP, ident)
        except Exception:
            pass
    return ident


def get_identity_header(refresh: bool = False) -> str:
    """Return the base64 value to put in the X-Installation HTTP header."""
    ident = get_identity(refresh=refresh)
    payload = {
        k: v for k, v in ident.items()
        if k != "first_seen_at" and v is not None
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf8")
    return base64.b64encode(raw).decode("ascii")


# Our own infra: operator Mac, mk0r E2B sandbox fleet, MacStadium remote QA boxes.
# Single source of truth for "is this install actually a customer" — consumed by
# active_users.py (customer-roster dedupe) and autopilot_stall_watch.py (stall-page
# severity) so the two lists can't drift apart. "71522" was the MacStadium box
# retired by the 2026-07-06 hardware swap (ticket #10904); "71732" is its
# replacement — both listed since the old box's hostname could still appear in
# historical data.
INTERNAL_EMAILS = {"i@m13v.com", "agent@mk0r.com", "matt@mediar.ai"}
INTERNAL_HOSTNAME_SUBSTR = ("e2b.local", "71522", "71732")
INTERNAL_HARDWARE_UUIDS = {"07CB793D-6E32-5EF8-82E2-7CDEABD47FBC"}


def is_internal_install(ident: dict | None = None) -> bool:
    """True when this install is our own infra (staging/QA/dev), not a real
    customer. Local-only (no DB), so it works on shipped .mcpb installs too."""
    ident = ident if ident is not None else get_identity()
    if (ident.get("git_email") or "").strip().lower() in INTERNAL_EMAILS:
        return True
    hostname = ident.get("hostname") or ""
    if any(sub in hostname for sub in INTERNAL_HOSTNAME_SUBSTR):
        return True
    if (ident.get("hardware_uuid") or "") in INTERNAL_HARDWARE_UUIDS:
        return True
    return False


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    if cmd == "show":
        print(json.dumps(get_identity(refresh=True), indent=2))
    elif cmd == "header":
        print(get_identity_header(refresh=True))
    elif cmd == "reset":
        # Explicit reset removes the backup too; otherwise the next call
        # would just recover the old install_id from it.
        deleted = []
        for p in (IDENTITY_FILE, IDENTITY_BACKUP):
            if p.exists():
                p.unlink()
                deleted.append(str(p))
        print("deleted " + ", ".join(deleted) if deleted else f"no identity at {IDENTITY_FILE}")
    elif cmd == "path":
        print(str(IDENTITY_FILE))
    else:
        print(f"unknown cmd: {cmd}", file=sys.stderr)
        print("usage: identity.py [show|header|reset|path]", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
