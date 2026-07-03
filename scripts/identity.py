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


def get_identity(refresh: bool = False) -> dict:
    """Read identity.json, creating it on first call.

    refresh=True re-snapshots the volatile fields (versions, hostname,
    git_email, tz) while preserving install_id and first_seen_at.
    """
    IDENTITY_DIR.mkdir(parents=True, exist_ok=True)

    if not IDENTITY_FILE.exists():
        ident = _build_fresh_identity()
        IDENTITY_FILE.write_text(json.dumps(ident, indent=2))
        try:
            os.chmod(IDENTITY_FILE, 0o600)
        except Exception:
            pass
        return ident

    try:
        ident = json.loads(IDENTITY_FILE.read_text())
    except Exception:
        # Corrupt file; rebuild rather than crashing the pipeline.
        ident = _build_fresh_identity()
        IDENTITY_FILE.write_text(json.dumps(ident, indent=2))
        return ident

    if refresh:
        snap = _build_fresh_identity()
        # preserve stable identifiers across refresh
        snap["install_id"] = ident.get("install_id") or snap["install_id"]
        snap["first_seen_at"] = ident.get("first_seen_at") or snap["first_seen_at"]
        if snap != ident:
            try:
                IDENTITY_FILE.write_text(json.dumps(snap, indent=2))
            except Exception:
                pass
        return snap
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


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    if cmd == "show":
        print(json.dumps(get_identity(refresh=True), indent=2))
    elif cmd == "header":
        print(get_identity_header(refresh=True))
    elif cmd == "reset":
        if IDENTITY_FILE.exists():
            IDENTITY_FILE.unlink()
            print(f"deleted {IDENTITY_FILE}")
        else:
            print(f"no identity at {IDENTITY_FILE}")
    elif cmd == "path":
        print(str(IDENTITY_FILE))
    else:
        print(f"unknown cmd: {cmd}", file=sys.stderr)
        print("usage: identity.py [show|header|reset|path]", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
