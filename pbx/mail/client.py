"""
SMTP wire protocol. Connects, secures, authenticates, sends -- and nothing else.

Message construction lives in :mod:`pbx.mail.message`; queueing and retry live in
:mod:`pbx.mail.dispatcher`. Keeping those apart is what lets the whole protocol surface be
tested against a fake transport without patching ``smtplib`` by string name.
"""

from __future__ import annotations

import contextlib
import smtplib
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from pbx.mail.errors import EmailConfigError, EmailError, EmailPermanentError, classify
from pbx.mail.message import message_summary
from pbx.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence
    from email.message import EmailMessage

    from pbx.mail.settings import SmtpSettings

__all__ = ["SendResult", "SmtpClient", "SmtpTransport", "TransportFactory"]


@dataclass(frozen=True, slots=True)
class SendResult:
    """Outcome of one delivery attempt."""

    ok: bool
    message_id: str | None = None
    error: EmailError | None = None
    duration_ms: float = 0.0

    @property
    def retryable(self) -> bool:
        """True when another attempt could plausibly succeed."""
        return self.error is not None and self.error.retryable


class SmtpTransport(Protocol):
    """The slice of :class:`smtplib.SMTP` this client actually uses.

    Declaring it as a Protocol is what makes the test fake a peer of the real thing rather
    than a monkeypatch.
    """

    def ehlo_or_helo_if_needed(self) -> None: ...

    def starttls(self, *, context: ssl.SSLContext | None = ...) -> tuple[int, bytes]: ...

    def login(self, user: str, password: str) -> tuple[int, bytes]: ...

    def send_message(
        self,
        msg: EmailMessage,
        from_addr: str | None = ...,
        to_addrs: str | Sequence[str] | None = ...,
    ) -> dict[str, tuple[int, bytes]]: ...

    def quit(self) -> tuple[int, bytes]: ...

    def close(self) -> None: ...


TransportFactory = Callable[["SmtpSettings"], SmtpTransport]


def build_ssl_context(settings: SmtpSettings) -> ssl.SSLContext:
    """
    Build the TLS context for both STARTTLS and implicit TLS.

    ``create_default_context`` returns a context with ``check_hostname`` on and
    ``verify_mode`` at ``CERT_REQUIRED``. For on-premises Exchange that matters twice: the
    internal CA has to be supplied via ``ca_file``, *and* ``smtp.host`` has to match a name on
    the certificate. Loading the right CA while connecting to the wrong name still fails.
    """
    context = ssl.create_default_context(cafile=settings.ca_file or None)
    if not settings.verify_cert:
        # Order matters: CPython rejects verify_mode=CERT_NONE while check_hostname is True.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def _default_transport_factory(settings: SmtpSettings) -> SmtpTransport:
    """Open a real connection. Implicit TLS takes the context in the constructor."""
    local_hostname = settings.helo_hostname or None
    if settings.security == "smtps":
        return smtplib.SMTP_SSL(
            settings.host,
            settings.port,
            local_hostname,
            timeout=settings.timeout,
            context=build_ssl_context(settings),
        )
    return smtplib.SMTP(
        settings.host,
        settings.port,
        local_hostname,
        timeout=settings.timeout,
    )


class SmtpClient:
    """
    One connection per send.

    A PBX sends tens of messages an hour, so pooling would buy nothing and cost a class of
    stale-socket bugs. What it does buy is a lifecycle short enough to guarantee in a
    ``finally``, which the implementation it replaces did not do -- a failed ``starttls()`` or
    ``login()`` there leaked the socket every time.
    """

    def __init__(
        self,
        settings: SmtpSettings,
        *,
        transport_factory: TransportFactory | None = None,
    ) -> None:
        self.settings = settings
        self.logger = get_logger()
        self._transport_factory = transport_factory or _default_transport_factory

    def __enter__(self) -> SmtpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def send(self, msg: EmailMessage) -> SendResult:
        """Deliver one message. Never raises -- every failure comes back in the result."""
        return self._run(message=msg)

    def verify(self) -> SendResult:
        """
        Exercise connect, EHLO, TLS and AUTH without sending anything.

        This is the difference between "email is broken" and "your receive connector rejects
        our certificate", and it is the first thing to run against a new Exchange connector.
        """
        return self._run(message=None)

    def _run(self, *, message: EmailMessage | None) -> SendResult:
        started = time.monotonic()

        if not self.settings.is_configured:
            return self._failure(
                EmailConfigError(
                    "SMTP is not configured",
                    detail="smtp.host and smtp.from_address must both be set",
                ),
                started,
            )

        transport: SmtpTransport | None = None
        try:
            transport = self._transport_factory(self.settings)
            self._prepare(transport)

            if message is None:
                return SendResult(ok=True, duration_ms=self._elapsed_ms(started))

            return self._transmit(transport, message, started)
        except Exception as exc:
            return self._failure(classify(exc), started)
        finally:
            self._disconnect(transport)

    def _prepare(self, transport: SmtpTransport) -> None:
        """
        EHLO, then secure the channel, then authenticate -- in that order.

        There is no plaintext path. With ``smtps`` the socket was already wrapped by the
        constructor; otherwise STARTTLS is mandatory, and smtplib raises
        ``SMTPNotSupportedError`` if the server does not advertise it. classify() maps that to
        a config error, so the send fails loudly instead of continuing in the clear.
        """
        transport.ehlo_or_helo_if_needed()

        if self.settings.security == "starttls":
            transport.starttls(context=build_ssl_context(self.settings))
            # Re-issue EHLO: the server's capability list can change after the upgrade, and
            # AUTH is commonly advertised only once the channel is encrypted.
            transport.ehlo_or_helo_if_needed()

        if self.settings.auth != "none":
            # smtplib negotiates the mechanism from the server's AUTH advertisement.
            transport.login(self.settings.username, self.settings.password)

    def _transmit(
        self, transport: SmtpTransport, message: EmailMessage, started: float
    ) -> SendResult:
        # from_addr is only overridden when an envelope sender is explicitly configured;
        # otherwise smtplib derives it from the From header, which is the usual case.
        envelope_from = self.settings.envelope_from or None
        refused = transport.send_message(message, from_addr=envelope_from)

        message_id = str(message.get("Message-ID", "")) or None
        summary = message_summary(message)

        if refused:
            # send_message returns normally when at least one recipient was accepted, handing
            # back a dict of the ones that were not. Reporting that as success is how a
            # half-delivered message becomes invisible, so any refusal fails the send.
            detail = "; ".join(
                f"{recipient}: {code} {text.decode('utf-8', errors='replace')}"
                for recipient, (code, text) in sorted(refused.items())
            )
            return self._failure(
                EmailPermanentError("SMTP server refused some recipients", detail=detail),
                started,
                message_id=message_id,
            )

        duration_ms = self._elapsed_ms(started)
        self.logger.info(
            "Sent mail %s to %d recipient(s) in domain(s) %s in %.0f ms",
            summary["message_id"],
            summary["recipient_count"],
            ", ".join(summary["recipient_domains"]) or "-",
            duration_ms,
        )
        return SendResult(ok=True, message_id=message_id, duration_ms=duration_ms)

    def _disconnect(self, transport: SmtpTransport | None) -> None:
        """Close the connection whatever happened. quit() can itself fail; close() must not."""
        if transport is None:
            return
        try:
            transport.quit()
        except Exception:
            # quit() writes to the socket, so it fails exactly when the connection is already
            # broken -- which is when closing matters most. Nothing useful is left to do if
            # close() also fails, and raising here would mask the real send failure.
            with contextlib.suppress(Exception):
                transport.close()

    def _failure(
        self, error: EmailError, started: float, *, message_id: str | None = None
    ) -> SendResult:
        duration_ms = self._elapsed_ms(started)
        self.logger.error("Mail send failed: %s", error)
        return SendResult(ok=False, message_id=message_id, error=error, duration_ms=duration_ms)

    @staticmethod
    def _elapsed_ms(started: float) -> float:
        return (time.monotonic() - started) * 1000.0
