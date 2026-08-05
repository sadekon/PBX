#!/usr/bin/env python3
"""
Send the daily voicemail summary for one extension to an address of your choosing.

The summary that VoicemailSystem.send_daily_reminders() would produce, for a single
mailbox, delivered wherever you point it. Nothing schedules that method yet, so this is
the only way to see the summary a user would receive.

It drives the real code -- the same mailbox loading, the same subject and body builders --
so what arrives is what production would send. Only the recipient is overridden, and the
per-extension subscription check is bypassed, since the point is to test the template.

Usage::

    python scripts/send_test_voicemail_summary.py --extension 1001 --to you@corp.com
    python scripts/send_test_voicemail_summary.py --extension 1001 --to you@corp.com --dry-run

Runs without a PBXCore. Reads config.yml directly, so it works on a box where the PBX
will not start.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

try:
    from pbx.features.voicemail import VoicemailSystem
    from pbx.mail import Mailer, SmtpSettings, enable_smtp_debug
    from pbx.utils.config import Config
    from pbx.utils.database import DatabaseBackend
    from pbx.utils.timezone import display_timezone
except ModuleNotFoundError as exc:
    # Almost always "run with the system interpreter instead of the project venv". The bare
    # traceback names a transitive dependency, which points nowhere useful.
    _venv = _REPO_ROOT / ".venv" / "bin" / "python"
    _script = sys.argv[0] or str(Path(__file__))
    _hint = f"{_venv} {_script}" if _venv.exists() else f"make install, then rerun {_script}"
    print(
        f"error: missing dependency {exc.name!r}.\n"
        f"This script needs the project's virtualenv. Try:\n"
        f"  {_hint} --extension 1001 --to you@example.com\n",
        file=sys.stderr,
    )
    raise SystemExit(2) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Send a voicemail summary for one extension to a chosen address."
    )
    parser.add_argument("--extension", required=True, help="Extension whose mailbox to summarize")
    parser.add_argument("--to", required=True, help="Where to send the summary")
    parser.add_argument(
        "--include-read",
        action="store_true",
        help="Summarize every message, not just unread ones (useful when nothing is unread)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the summary that would be sent, without sending it",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Dump the SMTP conversation, with credentials redacted",
    )
    parser.add_argument(
        "--storage",
        help="Voicemail storage path (defaults to voicemail.storage_path from config.yml)",
    )
    parser.add_argument("--config", default="config.yml", help="Path to config.yml")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = Config(args.config)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # The mailbox may live in the database; without it only on-disk messages are visible.
    database = DatabaseBackend(config)
    if database.connect():
        print(f"Database: connected ({database.db_type})")
    else:
        database = None
        print("Database: unavailable - reading voicemail from the file system only")

    storage_path: str = args.storage or config.get("voicemail.storage_path", "voicemail")
    system = VoicemailSystem(storage_path=storage_path, config=config, database=database)

    mailbox = system.get_mailbox(args.extension)
    messages = mailbox.get_messages(unread_only=not args.include_read)
    total_count = len(mailbox.get_messages(unread_only=False))
    scope = "message(s)" if args.include_read else "unread message(s)"
    print(
        f"Mailbox {args.extension}: {len(messages)} {scope} "
        f"of {total_count} total, in {storage_path}"
    )

    if not messages:
        print()
        print("Nothing to summarize. Leave a voicemail on this extension, or pass")
        print("--include-read to summarize messages that have already been heard.")
        return 1

    subject = VoicemailSystem._reminder_subject(len(messages))
    body = VoicemailSystem._reminder_body(
        args.extension, messages, total_count=total_count, tz=display_timezone(config)
    )

    print()
    print("=" * 70)
    print(f"To:      {args.to}")
    print(f"Subject: {subject}")
    print("-" * 70)
    print(body)
    print("=" * 70)
    print()

    if args.dry_run:
        print("Dry run - nothing sent.")
        return 0

    settings = SmtpSettings.from_dict(config.get("smtp", {}) or {})
    problems = settings.validate()
    if problems:
        print("Configuration warnings:")
        for problem in problems:
            print(f"  - {problem}")
        print()

    if not settings.is_configured:
        print("SMTP is not configured; set smtp.host and smtp.from_address in config.yml")
        return 1

    if args.verbose:
        enable_smtp_debug(settings)

    # Sent synchronously so the outcome is reported here rather than swallowed by the queue.
    mailer = Mailer(settings)
    print(f"Sending to {args.to} via {settings.host}:{settings.port} ...")
    result = mailer.send(args.to, subject, body)

    print()
    if result.ok:
        print(f"SUCCESS in {result.duration_ms:.0f} ms")
        if result.message_id:
            print(f"  Message-ID: {result.message_id}")
        return 0

    error = result.error
    print(f"FAILED after {result.duration_ms:.0f} ms")
    print(f"  {type(error).__name__}: {error}")
    print(f"  Retryable: {error.retryable if error else False}")
    print()
    print("See docs/EMAIL_SETUP.md for the troubleshooting table.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
