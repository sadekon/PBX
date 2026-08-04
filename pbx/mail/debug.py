"""
Credential-safe SMTP debug output.

``smtplib``'s ``debuglevel`` transcript is the most useful thing there is when mail will not
send -- and it prints the AUTH exchange verbatim. SASL base64-encodes credentials before they
reach the wire, so a naive "search for the password" filter never matches and the password is
printed in full. This module is the filter that actually works.

It lives here rather than in a script because three separate scripts need it, and because
redaction is the one piece of that tooling where being wrong leaks a production credential
into someone's terminal scrollback.
"""

from __future__ import annotations

import base64
import re
import smtplib
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pbx.mail.settings import SmtpSettings

__all__ = ["Redactor", "enable_smtp_debug"]


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


class Redactor:
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


def enable_smtp_debug(settings: SmtpSettings) -> None:
    """
    Turn on smtplib's transcript, with credentials filtered out.

    Patches the class rather than an instance because the scripts do not own the connection --
    it is created several layers down inside SmtpClient. Diagnostics go to stderr so piping
    the script's real output stays clean.
    """
    redactor = Redactor(settings)

    def patched(self: object, *bits: object) -> None:
        print(redactor(" ".join(str(bit) for bit in bits)), file=sys.stderr)

    smtplib.SMTP._print_debug = patched  # type: ignore[method-assign]
    smtplib.SMTP.debuglevel = 1
