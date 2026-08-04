#!/usr/bin/env python3
"""
Send one emergency (Kari's Law) alert email, to prove the path works before it has to.

This is the only PBX email with a legal mandate behind it and the only one nobody can afford
to discover is broken at the moment it fires. It has plenty of unit tests -- and unit tests are
exactly what did not catch it the last time it silently logged instead of sending. A mock
agrees with whatever the code does; an SMTP server does not.

It drives the real path: the real ``EmergencyNotificationSystem._send_email_notification``, the
same subject and body, the same synchronous ``Mailer.send`` with its ``result.ok`` check, and
the same audit-log entry Kari's Law wants as evidence the attempt was made. Only the recipient
is overridden.

Usage::

    python scripts/send_test_emergency_alert.py --to you@corp.com
    python scripts/send_test_emergency_alert.py --to you@corp.com --dry-run
    python scripts/send_test_emergency_alert.py --list-contacts

**The subject line begins with "EMERGENCY ALERT".** Tell whoever might see it first, and
prefer --dry-run or an address only you read.

Runs without a PBXCore. Reads config.yml directly, so it works on a box where the PBX will
not start.

Exit codes: 0 sent, 1 failed or not delivered, 2 bad usage or missing dependency.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

try:
    from pbx.features.emergency_notification import EmergencyContact, EmergencyNotificationSystem
    from pbx.mail import Mailer, SmtpSettings, enable_smtp_debug
    from pbx.utils.config import Config
except ModuleNotFoundError as exc:
    _venv = _REPO_ROOT / ".venv" / "bin" / "python"
    _script = sys.argv[0] or str(__file__)
    _hint = f"{_venv} {_script}" if _venv.exists() else f"make install, then rerun {_script}"
    print(
        f"error: missing dependency {exc.name!r}.\n"
        f"This script needs the project's virtualenv. Try:\n"
        f"  {_hint} --to you@example.com\n",
        file=sys.stderr,
    )
    raise SystemExit(2) from exc


class _CoreStub:
    """
    The single attribute ``_send_email_notification`` reaches for on PBXCore.

    Deliberately not a PBXCore: starting one binds SIP and RTP ports, and this must run on a
    box where the PBX is already running -- or refusing to.
    """

    def __init__(self, mailer: Mailer) -> None:
        self.mailer = mailer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Send one emergency alert email through the real notification path.",
    )
    parser.add_argument("--to", help="Where to send the alert (required unless --list-contacts)")
    parser.add_argument(
        "--trigger", default="911 Emergency Call", help="Trigger type shown in the subject"
    )
    parser.add_argument(
        "--extension", default="1001", help="Extension shown as the caller (default: 1001)"
    )
    parser.add_argument(
        "--list-contacts",
        action="store_true",
        help="Show the configured emergency contacts and exit, sending nothing",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print the alert that would be sent, without sending"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Dump the SMTP conversation, with credentials redacted",
    )
    parser.add_argument("--config", default="config.yml", help="Path to config.yml")
    return parser


def _show_contacts(system: EmergencyNotificationSystem) -> int:
    """List who would really be notified. Config errors here are silent in production."""
    if not system.emergency_contacts:
        print("No emergency contacts are configured.")
        print("Set features.emergency_notification.contacts in config.yml, or add rows to")
        print("the emergency_contacts table. Without one, a 911 call notifies nobody.")
        return 1

    print(f"{len(system.emergency_contacts)} emergency contact(s):")
    for contact in sorted(system.emergency_contacts, key=lambda c: c.priority):
        methods = ", ".join(contact.notification_methods)
        email = contact.email or "(no email -- will not be emailed)"
        print(f"  priority {contact.priority}  {contact.name}")
        print(f"      email:   {email}")
        print(f"      methods: {methods}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = Config(args.config)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    settings = SmtpSettings.from_dict(config.get("smtp", {}) or {})
    mailer = Mailer(settings)
    system = EmergencyNotificationSystem(_CoreStub(mailer), config=config)

    if args.list_contacts:
        return _show_contacts(system)

    if not args.to:
        print("error: --to is required (or use --list-contacts)", file=sys.stderr)
        return 2

    details: dict[str, Any] = {
        "timestamp": datetime.now(tz=UTC),
        "caller_extension": args.extension,
        "dialed_number": "911",
        "location": config.get("emergency.location", "not configured"),
        "note": "TEST ALERT sent by scripts/send_test_emergency_alert.py",
    }
    contact = EmergencyContact(
        name="Test Recipient", email=args.to, priority=1, notification_methods=["email"]
    )

    for problem in settings.validate():
        print(f"  smtp: {problem}")

    if args.dry_run:
        print("\n" + "=" * 70)
        print(f"To:      {args.to}")
        print(f"Subject: 🚨 EMERGENCY ALERT: {args.trigger}")
        print("=" * 70)
        for key, value in details.items():
            print(f"  {key}: {value}")
        print("=" * 70)
        print("\nDry run: nothing was sent.")
        return 0

    if not mailer.enabled:
        print("error: SMTP is not configured; set smtp.host and smtp.from_address", file=sys.stderr)
        return 1

    if args.verbose:
        enable_smtp_debug(settings)

    print(f"Sending a {args.trigger!r} alert to {args.to}...")
    # The real method: synchronous send, result.ok check, and the Kari's Law audit entry.
    # Its own logging reports success or the SMTP error, so nothing is re-implemented here.
    system._send_email_notification(contact, args.trigger, details)

    print("\nDone. The log line above says whether the send actually succeeded --")
    print("a failure is logged, not raised, exactly as it would be during a real 911 call.")
    if settings.redirect_to:
        print(f"Note: smtp.redirect_to is set, so it went to {settings.redirect_to} instead.")
    print("\nAlso worth checking: --list-contacts, to confirm a real 911 call would")
    print("reach somebody. An empty contact list notifies nobody and logs nothing alarming.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
