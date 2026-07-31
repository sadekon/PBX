"""
Error taxonomy for the mail subsystem.

The old notifier collapsed every failure into a single log line, which made a wrong password
indistinguishable from a rebooting server. Three classes replace that, and the only question
the dispatcher ever has to ask is ``error.retryable``:

* :class:`EmailConfigError` -- the configuration or the message is wrong. Never retried; an
  administrator has to change something.
* :class:`EmailTransientError` -- the server was momentarily unavailable. Retried with backoff.
* :class:`EmailPermanentError` -- the server understood and refused. Not retried, surfaced loudly.
"""

from __future__ import annotations

import smtplib
import socket
import ssl
from typing import ClassVar

__all__ = [
    "EmailConfigError",
    "EmailError",
    "EmailPermanentError",
    "EmailTransientError",
    "classify",
]

# SMTP reply codes in the 4xx range are "try again later"; 5xx are refusals.
_TRANSIENT_CODE_MIN = 400
_TRANSIENT_CODE_MAX = 500


class EmailError(Exception):
    """Base class for every mail failure.

    Args:
        message: Short human-readable summary, safe for an INFO/ERROR log line.
        detail: Server-supplied text or other diagnostic context. May be verbose.
        code: SMTP reply code when the failure came from the server.
    """

    retryable: ClassVar[bool] = False

    def __init__(self, message: str, *, detail: str = "", code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.code = code

    def __str__(self) -> str:
        parts = [self.message]
        if self.code is not None:
            parts.append(f"(code {self.code})")
        if self.detail:
            parts.append(f"- {self.detail}")
        return " ".join(parts)


class EmailConfigError(EmailError):
    """Misconfiguration or an unsendable message. Retrying cannot help."""


class EmailTransientError(EmailError):
    """A temporary failure. Worth retrying with backoff."""

    retryable: ClassVar[bool] = True


class EmailPermanentError(EmailError):
    """The server refused the message. Retrying would repeat the refusal."""


def _response_detail(exc: smtplib.SMTPResponseException) -> str:
    """Decode the server's reply text, which smtplib hands back as bytes."""
    error = exc.smtp_error
    if isinstance(error, bytes):
        return error.decode("utf-8", errors="replace")
    return str(error)


def classify(exc: BaseException) -> EmailError:
    """
    Map an arbitrary exception onto the taxonomy.

    Ordering matters: ``smtplib``'s hierarchy is deep (``SMTPAuthenticationError`` and
    ``SMTPConnectError`` are both ``SMTPResponseException``, and ``SMTPException`` itself
    derives from ``OSError``), so the most specific cases are tested first and the broad
    ``OSError`` catch is last.
    """
    if isinstance(exc, EmailError):
        return exc

    # A certificate that fails verification is an administrative problem -- the wrong CA
    # bundle, or a host name that does not match the certificate. Never retry it.
    if isinstance(exc, ssl.SSLCertVerificationError):
        return EmailConfigError(
            "TLS certificate verification failed",
            detail=str(exc),
        )

    # Raised for a server without SMTPUTF8 when an address needs it, and for STARTTLS on a
    # server that does not offer it. Both are configuration facts, not weather.
    if isinstance(exc, smtplib.SMTPNotSupportedError):
        return EmailConfigError(
            "SMTP server does not support a required extension", detail=str(exc)
        )

    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return EmailPermanentError(
            "SMTP authentication failed",
            detail=_response_detail(exc),
            code=exc.smtp_code,
        )

    # Connecting failed. Even with a 5xx greeting this is worth another attempt, because the
    # usual cause is a server that is starting up or briefly out of connections.
    if isinstance(exc, smtplib.SMTPConnectError):
        return EmailTransientError(
            "Could not connect to SMTP server",
            detail=_response_detail(exc),
            code=exc.smtp_code,
        )

    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        refused = ", ".join(sorted(exc.recipients)) if exc.recipients else "all recipients"
        return EmailPermanentError("SMTP server refused every recipient", detail=refused)

    # Covers SMTPSenderRefused, SMTPDataError and SMTPHeloError. The reply code decides:
    # 4xx means "come back later", 5xx is a refusal.
    if isinstance(exc, smtplib.SMTPResponseException):
        detail = _response_detail(exc)
        if _TRANSIENT_CODE_MIN <= exc.smtp_code < _TRANSIENT_CODE_MAX:
            return EmailTransientError(
                "SMTP server returned a temporary failure", detail=detail, code=exc.smtp_code
            )
        return EmailPermanentError(
            "SMTP server rejected the message", detail=detail, code=exc.smtp_code
        )

    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return EmailTransientError("SMTP server closed the connection", detail=str(exc))

    if isinstance(exc, socket.gaierror):
        return EmailTransientError("Could not resolve SMTP server host name", detail=str(exc))

    if isinstance(exc, TimeoutError):
        return EmailTransientError("Timed out talking to the SMTP server", detail=str(exc))

    if isinstance(exc, ssl.SSLError):
        return EmailTransientError("TLS negotiation failed", detail=str(exc))

    # SMTPException derives from OSError, so this also catches the smtplib errors that have
    # no more specific branch above (SMTPException itself, for one).
    if isinstance(exc, OSError):
        return EmailTransientError("Network error talking to the SMTP server", detail=str(exc))

    return EmailPermanentError(f"Unexpected mail failure: {type(exc).__name__}", detail=str(exc))
