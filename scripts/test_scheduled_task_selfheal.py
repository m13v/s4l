#!/usr/bin/env python3
"""Focused tests for fresh-account scheduled-task registry creation."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import scheduled_task_selfheal as selfheal  # noqa: E402


ACCOUNT_UUID = "f598e7b5-55be-46e8-815e-e8ddb394fc73"
ORG_UUID = "d03d2d69-12d0-45bf-9f67-ae085d6848e5"


def _write_cookie_db(root: str, *, value: str = "", encrypted: bytes = b"v10fixture"):
    db = os.path.join(root, "Cookies")
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            """
            CREATE TABLE cookies (
                host_key TEXT,
                name TEXT,
                value TEXT,
                encrypted_value BLOB,
                last_access_utc INTEGER
            )
            """
        )
        conn.execute(
            "INSERT INTO cookies VALUES (?, 'lastActiveOrg', ?, ?, 1)",
            (selfheal.CLAUDE_COOKIE_HOST, value, encrypted),
        )
        conn.commit()
    finally:
        conn.close()


class ScheduledTaskSelfhealTest(unittest.TestCase):
    def test_decrypted_cookie_strips_chromium_host_digest(self):
        with tempfile.TemporaryDirectory() as root:
            _write_cookie_db(root)
            clear = (
                hashlib.sha256(selfheal.CLAUDE_COOKIE_HOST.encode()).digest()
                + ORG_UUID.encode()
            )
            with (
                mock.patch.object(
                    selfheal, "_claude_safe_storage_password", return_value=b"fixture"
                ),
                mock.patch.object(selfheal, "_aes_128_cbc_decrypt", return_value=clear),
            ):
                self.assertEqual(selfheal._active_org_uuid_from_cookie(root), ORG_UUID)

    def test_capability_probe_does_not_read_keychain(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(root, exist_ok=True)
            config = os.path.join(root, "config.json")
            with open(config, "w") as fh:
                json.dump({"lastKnownAccountUuid": ACCOUNT_UUID}, fh)
            _write_cookie_db(root)
            with (
                mock.patch.object(
                    selfheal.schedule_state, "_config_json_paths", return_value=[config]
                ),
                mock.patch.object(
                    selfheal,
                    "_claude_safe_storage_password",
                    side_effect=AssertionError("passive probe touched Keychain"),
                ),
            ):
                self.assertTrue(selfheal.can_create_for_active_account())

    def test_heal_creates_exact_account_org_registry_without_session_dir(self):
        with tempfile.TemporaryDirectory() as root:
            config = os.path.join(root, "config.json")
            with open(config, "w") as fh:
                json.dump({"lastKnownAccountUuid": ACCOUNT_UUID}, fh)
            worker_cwd = os.path.join(root, "worker")
            registry_glob = os.path.join(
                root, "claude-code-sessions", "*", "*", "scheduled-tasks.json"
            )
            expected = os.path.join(
                root,
                "claude-code-sessions",
                ACCOUNT_UUID,
                ORG_UUID,
                "scheduled-tasks.json",
            )
            with (
                mock.patch.object(
                    selfheal.schedule_state, "_config_json_paths", return_value=[config]
                ),
                mock.patch.object(
                    selfheal, "_active_org_uuid_from_cookie", return_value=ORG_UUID
                ),
                mock.patch.object(selfheal, "_ensure_worker_skill_md", return_value=True),
                mock.patch.object(selfheal, "WORKER_CWD", worker_cwd),
                mock.patch.object(selfheal, "SCHED_REGISTRY_GLOB", registry_glob),
                mock.patch.object(selfheal, "DEPRECATED_TASK_IDS", ()),
                mock.patch.object(selfheal, "LEGACY_WORKER_TASK_IDS", ()),
            ):
                summary = selfheal.heal()

            self.assertEqual(summary["created"], [expected])
            with open(expected) as fh:
                registry = json.load(fh)
            self.assertEqual(registry["recordedSkips"], [])
            self.assertEqual(registry["scheduledTasks"][0]["id"], selfheal.WORKER_TASK_ID)
            self.assertEqual(registry["scheduledTasks"][0]["cwd"], worker_cwd)


if __name__ == "__main__":
    unittest.main()
