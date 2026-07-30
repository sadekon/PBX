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
import base64
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

try:
    from pbx.mail import SmtpClient, SmtpSettings, build_message
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


#: A bare base64 token. Only meaningful inside the AUTH exchange: SMTP verbs such as
#: STARTTLS, QUIT, DATA and RSET are also pure alphanumerics and must stay visible.
_BASE64_BLOB = re.compile(r"^[A-Za-z0-9+/]{4,}={0,2}$")

#: Commands that look like base64 but are protocol, not payload.
_SMTP_VERBS = frozenset(
    {"starttls", "quit", "data", "rset", "noop", "helo", "ehlo", "auth", "vrfy", "bdat"}
)

#: The AUTH *command* the client issues, whose argument may carry an inline credential.
#: Deliberately anchored to 'send:' so it cannot match a '250-AUTH' capability line, which
#: is not secret and is genuinely useful when diagnosing a connector.
_AUTH_COMMAND = re.compile(r"^send:\s*b?['\"]?AUTH\s", re.IGNORECASE)


class _Redactor:
    """
    Strip credentials from smtplib's debug transcript.

    Fails safe: rather than searching for the password (which is base64-encoded by SASL and
    so never appears literally), it suppresses anything that *could* be a credential -- the
    client's reply to a 334 challenge, any bare base64 payload, and the AUTH command's
    argument. Server capability lines are left intact.
    """

    def __init__(self, settings: SmtpSettings) -> None:
        self._secrets = {value for value in (settings.password, settings.username) if value}
        # Cover the encodings SASL actually puts on the wire, so a literal match still works
        # as a backstop: LOGIN sends each field separately, PLAIN sends \0user\0pass.
        for value in list(self._secrets):
            self._secrets.add(base64.b64encode(value.encode()).decode())
        if settings.username and settings.password:
            plain = f"\0{settings.username}\0{settings.password}".encode()
            self._secrets.add(base64.b64encode(plain).decode())
        self._challenged = False
        self._in_auth = False

    def __call__(self, line: str) -> str:
        stripped = line.lstrip()
        is_send = stripped.startswith("send:")
        payload = line.partition(":")[2].strip().strip("'\"").removesuffix("\\r\\n").strip()

        # The line straight after a 334 challenge is the credential itself.
        if self._challenged and is_send:
            self._challenged = False
            return "send: <credential redacted>"
        self._challenged = stripped.startswith("reply:") and payload.startswith(("334", "b'334"))

        if _AUTH_COMMAND.match(stripped):
            self._in_auth = True
            return "send: AUTH <mechanism and credential redacted>"

        # A bare base64 blob is only a credential mid-AUTH. Outside it, an all-alphanumeric
        # payload is an ordinary SMTP verb and hiding it would defeat the point of --verbose.
        if (
            is_send
            and self._in_auth
            and payload.lower() not in _SMTP_VERBS
            and _BASE64_BLOB.match(payload)
        ):
            return "send: <base64 payload redacted>"

        # The AUTH exchange ends at the first non-334 server reply.
        if stripped.startswith("reply:") and not self._challenged:
            self._in_auth = False

        for secret in self._secrets:
            if secret and secret in line:
                line = line.replace(secret, "***")
        return line


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
        # smtplib's debug output goes to stderr; the redaction above covers the AUTH lines.
        import smtplib

        redactor = _Redactor(settings)

        def patched(self, *bits):  # type: ignore[no-untyped-def]
            print(redactor(" ".join(str(bit) for bit in bits)), file=sys.stderr)

        smtplib.SMTP._print_debug = patched  # type: ignore[method-assign]
        smtplib.SMTP.debuglevel = 1

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
