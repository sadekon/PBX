#!/usr/bin/env python3
"""
Send a test email, or just exercise the SMTP handshake.

Separates the two failures that get confused with each other. ``--verify-only`` connects,
does EHLO, negotiates TLS and authenticates without sending anything: if that succeeds but a
real send fails, the problem is the message or the receive connector's rules, not the
credentials or the certificate.

Usage::

    python scripts/send_test_email.py --to admin@corp.com
    python scripts/send_test_email.py --to admin@corp.com --verify-only --verbose

Runs without a PBXCore -- it reads config.yml directly, so it works on a box where the PBX
will not start.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

try:
    from pbx.mail import SmtpClient, SmtpSettings, build_message, enable_smtp_debug
    from pbx.utils.config import Config
except ModuleNotFoundError as exc:
    # Almost always "run with the system interpreter instead of the project venv". The bare
    # traceback names a transitive dependency (yaml), which points nowhere useful.
    _venv = _REPO_ROOT / ".venv" / "bin" / "python"
    _script = sys.argv[0] or str(Path(__file__))
    _hint = f"{_venv} {_script}" if _venv.exists() else f"make install, then rerun {_script}"
    print(
        f"error: missing dependency {exc.name!r}.\n"
        f"This script needs the project's virtualenv. Try:\n"
        f"  {_hint} --to you@example.com\n",
        file=sys.stderr,
    )
    raise SystemExit(2) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Send a test email through the configured SMTP transport."
    )
    parser.add_argument("--to", help="Recipient address (omit with --verify-only)")
    parser.add_argument(
        "--from",
        dest="from_address",
        help=(
            "Override smtp.from_address for this run. Useful when testing from a workstation "
            "and the server will not let you send as the configured sender."
        ),
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Connect, secure and authenticate, but send nothing",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Dump the SMTP conversation, with credentials redacted",
    )
    parser.add_argument("--config", default="config.yml", help="Path to config.yml")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.to and not args.verify_only:
        print("error: --to is required unless --verify-only is given", file=sys.stderr)
        return 2

    try:
        config = Config(args.config)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    smtp_config = config.get("smtp", {}) or {}
    if args.from_address:
        smtp_config = {**smtp_config, "from_address": args.from_address}
    settings = SmtpSettings.from_dict(smtp_config)

    print("SMTP configuration")
    for key, value in settings.redacted().items():
        print(f"  {key}: {value}")
    print()

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

    client = SmtpClient(settings)

    if args.verify_only:
        print(f"Verifying {settings.host}:{settings.port} (security={settings.security}, ")
        print(f"           auth={settings.auth}) ...")
        result = client.verify()
    else:
        now = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M:%S %Z")
        message = build_message(
            settings,
            args.to,
            f"Warden VoIP SMTP test - {now}",
            (
                "This is a test message from the Warden VoIP PBX.\n\n"
                f"Sent: {now}\n"
                f"Server: {settings.host}:{settings.port}\n"
                f"Security: {settings.security}\n"
                f"Auth: {settings.auth}\n\n"
                "If you received this, voicemail and emergency notification email will work.\n"
            ),
        )
        print(f"Sending to {args.to} via {settings.host}:{settings.port} ...")
        result = client.send(message)

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
