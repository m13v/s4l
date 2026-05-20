#!/usr/bin/env python3
"""Log a successful, verified DM send.

Usage:
  dm_send_log.py --dm-id DM_ID --message TEXT --verified \
                 [--session-id UUID]

REQUIRES --verified. Without it the script refuses to flip status='sent'.
This is the gate against the prompt-driven "always mark sent" bug that
produced ~700 phantom rows in April 2026. The browser tool's send_dm /
compose_dm now returns ok=False when DOM verification fails; the LLM
running the outreach pipeline must only call this script when the tool
actually returned verified=true.
"""
import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def load_env():
    env_path = "/Users/matthewdi/social-autoposter/.env"
    if not os.path.exists(env_path):
        return
    for line in open(env_path):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main():
    parser = argparse.ArgumentParser(
        description="Log a verified DM send (gates status='sent' on --verified)."
    )
    parser.add_argument("--dm-id", required=True, help="dms.id")
    parser.add_argument("--message", required=True, help="DM body that was sent")
    parser.add_argument(
        "--verified",
        action="store_true",
        help="REQUIRED. Confirms the browser tool returned verified=true.",
    )
    parser.add_argument(
        "--session-id",
        default=os.environ.get("CLAUDE_SESSION_ID"),
        help="claude_session_id UUID (defaults to $CLAUDE_SESSION_ID)",
    )

    # Back-compat: old call sites used positional dm_id + message.
    # Detect that shape so we can refuse cleanly instead of crashing.
    if len(sys.argv) >= 3 and not sys.argv[1].startswith("--"):
        print(
            "ERROR: dm_send_log.py now requires named flags. Call as:\n"
            "  dm_send_log.py --dm-id ID --message TEXT --verified",
            file=sys.stderr,
        )
        sys.exit(2)

    args = parser.parse_args()

    if not args.verified:
        print(
            "ERROR: refusing to mark dm_id={} as sent without --verified.\n"
            "The browser send_dm/compose_dm tool must return verified=true "
            "first. If verification failed, mark the row as 'error' instead.".format(
                args.dm_id
            ),
            file=sys.stderr,
        )
        sys.exit(3)

    load_env()
    import http_api
    from version import read_version as read_autoposter_version
    patch_body: dict = {"status": "sent", "our_dm_content": args.message}
    if args.session_id:
        patch_body["claude_session_id"] = args.session_id
    # autoposter_version: stamp on the 'sent' transition so DM engagement
    # (replies / bookings) can be attributed to the release of the autoposter
    # code that drafted the message.
    autoposter_version = read_autoposter_version()
    if autoposter_version:
        patch_body["autoposter_version"] = autoposter_version
    http_api.api_patch(f"/api/v1/dms/{args.dm_id}", patch_body)

    subprocess.run(
        [
            "python3",
            "/Users/matthewdi/social-autoposter/scripts/dm_conversation.py",
            "log-outbound",
            "--dm-id",
            str(args.dm_id),
            "--content",
            args.message,
            "--verified",
        ],
        check=True,
    )


if __name__ == "__main__":
    main()
