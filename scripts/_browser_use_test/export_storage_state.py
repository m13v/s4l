"""Decrypt Chrome cookies from the twitter-agent profile and emit a Playwright
storage_state.json file. Sidesteps browser-use's broken cookie loading by
handing it pre-decrypted cookies.

macOS Chrome v10 cookies:
- Key = PBKDF2-HMAC-SHA1(keychain "Chrome Safe Storage" password,
        salt="saltysalt", iterations=1003, dklen=16)
- AES-128-CBC, IV = b' ' * 16, PKCS7-padded
- encrypted_value = b"v10" + ciphertext
- After decryption: first 32 bytes are a SHA256 of host_key + value (Chrome 116+ binding),
  remainder is the cookie value bytes.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from hashlib import pbkdf2_hmac
from pathlib import Path

from Crypto.Cipher import AES


PROFILE = Path.home() / ".claude/browser-profiles/twitter"
COOKIES_DB = PROFILE / "Default/Cookies"
OUT_PATH = Path("/Users/matthewdi/social-autoposter/scripts/_browser_use_test/storage_state.json")


def get_chrome_safe_storage_password() -> bytes:
    out = subprocess.run(
        ["security", "find-generic-password", "-s", "Chrome Safe Storage", "-w"],
        capture_output=True, check=True, text=True,
    )
    return out.stdout.strip().encode("utf-8")


def derive_key(password: bytes) -> bytes:
    return pbkdf2_hmac("sha1", password, b"saltysalt", 1003, dklen=16)


def decrypt_v10(encrypted: bytes, key: bytes) -> bytes:
    if not encrypted.startswith(b"v10"):
        raise ValueError(f"Not v10 cookie: prefix={encrypted[:3]!r}")
    cipher = AES.new(key, AES.MODE_CBC, b" " * 16)
    decrypted = cipher.decrypt(encrypted[3:])
    pad = decrypted[-1]
    decrypted = decrypted[:-pad]
    # Chrome 116+ binds value with a 32-byte sha256 prefix.
    if len(decrypted) > 32:
        try:
            return decrypted[32:]
        except Exception:
            return decrypted
    return decrypted


def main() -> int:
    if not COOKIES_DB.exists():
        print(f"Cookies DB not found: {COOKIES_DB}", file=sys.stderr)
        return 1

    password = get_chrome_safe_storage_password()
    key = derive_key(password)

    with tempfile.TemporaryDirectory() as td:
        db_copy = Path(td) / "Cookies"
        shutil.copy(COOKIES_DB, db_copy)
        conn = sqlite3.connect(db_copy)
        rows = conn.execute(
            "SELECT host_key, name, value, encrypted_value, path, expires_utc, "
            "is_secure, is_httponly, samesite "
            "FROM cookies WHERE host_key LIKE '%x.com' OR host_key LIKE '%twitter%'"
        ).fetchall()
        conn.close()

    samesite_map = {-1: "Lax", 0: "None", 1: "Lax", 2: "Strict"}
    cookies = []
    failed = 0
    for host_key, name, value, encrypted, path, expires_utc, is_secure, is_httponly, samesite in rows:
        if encrypted:
            try:
                raw = decrypt_v10(encrypted, key)
                cookie_value = raw.decode("utf-8", errors="replace")
                # Chrome v10 with sha256 prefix: try without offset if utf-8 looks garbage
                if "\x00" in cookie_value[:5] or not cookie_value.isprintable():
                    cipher = AES.new(key, AES.MODE_CBC, b" " * 16)
                    dec = cipher.decrypt(encrypted[3:])
                    dec = dec[:-dec[-1]]
                    cookie_value = dec.decode("utf-8", errors="replace")
            except Exception as e:
                failed += 1
                print(f"  decrypt fail {host_key} {name}: {e}", file=sys.stderr)
                continue
        else:
            cookie_value = value

        # Convert Chrome time (microseconds since 1601-01-01) -> Unix seconds.
        # Session cookies expire_utc == 0.
        if expires_utc == 0:
            expires = -1
        else:
            expires = (expires_utc / 1_000_000) - 11644473600

        cookies.append({
            "name": name,
            "value": cookie_value,
            "domain": host_key,
            "path": path,
            "expires": expires,
            "httpOnly": bool(is_httponly),
            "secure": bool(is_secure),
            "sameSite": samesite_map.get(samesite, "Lax"),
        })

    storage_state = {"cookies": cookies, "origins": []}
    OUT_PATH.write_text(json.dumps(storage_state, indent=2))
    print(f"Wrote {len(cookies)} cookies to {OUT_PATH}  (decrypt failures: {failed})")

    has_auth = any(c["name"] == "auth_token" for c in cookies)
    print(f"auth_token in storage_state: {has_auth}")
    if has_auth:
        c = next(c for c in cookies if c["name"] == "auth_token")
        v = c["value"]
        print(f"auth_token value (masked): {v[:6]}...{v[-4:]} (len={len(v)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
