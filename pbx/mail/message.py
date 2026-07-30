"""
MIME assembly. Pure construction -- nothing in this module opens a socket.

Built on :class:`email.message.EmailMessage` rather than the legacy ``MIMEMultipart`` API.
That choice is what makes attachments, RFC 2047 encoding and ``Bcc`` handling ordinary
instead of fiddly, and it is why every header value can be funnelled through one sanitiser.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, make_msgid
from typing import TYPE_CHECKING, Any, Final, Literal

from pbx.mail.errors import EmailConfigError
from pbx.utils.config import Config

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from pathlib import Path

    from pbx.mail.settings import SmtpSettings

__all__ = ["Attachment", "build_message", "sanitize_header"]

_DEFAULT_MIME_TYPE: Final[str] = "application/octet-stream"


def sanitize_header(value: str) -> str:
    """
    Strip everything that could start a new header line.

    This is the whole defence against header injection, and it is applied to every value
    interpolated into a header -- subjects built from a template, and display names taken
    from a SIP ``From`` header, which is attacker-controlled text straight off the wire.
    Carriage returns, newlines and NULs go; surrounding whitespace goes with them.
    """
    return value.replace("\r", "").replace("\n", "").replace("\x00", "").strip()


def _split_mime_type(mime_type: str) -> tuple[str, str]:
    """Split ``"audio/wav"`` into ``("audio", "wav")``, falling back to a safe generic type."""
    maintype, _, subtype = mime_type.partition("/")
    if not maintype or not subtype:
        return ("application", "octet-stream")
    return (maintype, subtype)


@dataclass(frozen=True, slots=True)
class Attachment:
    """A file to hang off the message. Held in memory: these are voicemail clips, not archives."""

    filename: str
    data: bytes
    mime_type: str = _DEFAULT_MIME_TYPE

    @classmethod
    def from_path(cls, path: Path, mime_type: str | None = None) -> Attachment:
        """
        Read a file from disk into an attachment.

        Args:
            path: File to read.
            mime_type: Override for the guessed content type.

        Raises:
            EmailConfigError: If the file is missing or cannot be read. Retrying will not
                make the file appear, so this is a config-class failure rather than transient.
        """
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise EmailConfigError(
                f"Could not read attachment {path.name}", detail=str(exc)
            ) from exc

        guessed = mime_type or mimetypes.guess_type(path.name)[0] or _DEFAULT_MIME_TYPE
        return cls(filename=path.name, data=data, mime_type=guessed)


def _normalize_recipients(value: str | Iterable[str] | None) -> list[str]:
    """Accept a single address or any iterable of them; drop blanks."""
    if value is None:
        return []
    if isinstance(value, str):
        candidates = value.split(",")
    else:
        candidates = list(value)
    return [cleaned for cleaned in (sanitize_header(item) for item in candidates) if cleaned]


def _validate_addresses(addresses: Sequence[str], field_name: str) -> None:
    invalid = [address for address in addresses if not Config.validate_email(address)]
    if invalid:
        raise EmailConfigError(
            f"Invalid {field_name} address: {', '.join(invalid)}",
            detail="Addresses must be a plain user@domain form",
        )


def build_message(
    settings: SmtpSettings,
    to: str | Iterable[str],
    subject: str,
    body: str,
    *,
    html: str | None = None,
    attachments: Iterable[Attachment] = (),
    cc: str | Iterable[str] | None = None,
    bcc: str | Iterable[str] | None = None,
    reply_to: str | None = None,
    headers: Mapping[str, str] | None = None,
    from_address: str | None = None,
    from_name: str | None = None,
    importance: Literal["normal", "high"] = "normal",
) -> EmailMessage:
    """
    Assemble a message ready for :meth:`smtplib.SMTP.send_message`.

    Args:
        settings: Supplies the default sender identity and the attachment size ceiling.
        to: One address or an iterable of them.
        subject: Subject line. Sanitised, then RFC 2047 encoded by the stdlib if needed.
        body: Plain-text body.
        html: Optional HTML alternative, producing a multipart/alternative message.
        attachments: Files to attach.
        cc: Carbon-copy recipients.
        bcc: Blind carbon-copy recipients. Set as a normal header; ``send_message`` reads it
            for the envelope and strips it before transmission, so it never reaches the wire.
        reply_to: Optional Reply-To address.
        headers: Extra headers. Names and values are sanitised like everything else.
        from_address: Overrides ``settings.from_address``.
        from_name: Overrides ``settings.from_name``.
        importance: ``"high"`` adds the X-Priority/Importance pair Outlook reacts to.

    Returns:
        A fully populated message, including a ``Message-ID`` and a timezone-aware ``Date``.

    Raises:
        EmailConfigError: No recipients, no sender, a malformed address, or attachments over
            ``settings.max_attachment_bytes``.
    """
    sender_address = sanitize_header(from_address or settings.from_address)
    if not sender_address:
        raise EmailConfigError("No sender address configured (smtp.from_address)")
    _validate_addresses([sender_address], "From")

    to_addresses = _normalize_recipients(to)
    cc_addresses = _normalize_recipients(cc)
    bcc_addresses = _normalize_recipients(bcc)
    if not (to_addresses or cc_addresses or bcc_addresses):
        raise EmailConfigError("No recipients supplied")

    _validate_addresses(to_addresses, "To")
    _validate_addresses(cc_addresses, "Cc")
    _validate_addresses(bcc_addresses, "Bcc")

    attachment_list = list(attachments)
    total_bytes = sum(len(item.data) for item in attachment_list)
    if total_bytes > settings.max_attachment_bytes:
        raise EmailConfigError(
            "Attachments exceed the configured size limit",
            detail=f"{total_bytes} bytes > {settings.max_attachment_bytes} bytes",
        )

    message = EmailMessage()

    display_name = sanitize_header(from_name if from_name is not None else settings.from_name)
    # formataddr handles RFC 2047 encoding of a non-ASCII display name for us.
    message["From"] = formataddr((display_name, sender_address))

    if to_addresses:
        message["To"] = ", ".join(to_addresses)
    if cc_addresses:
        message["Cc"] = ", ".join(cc_addresses)
    if bcc_addresses:
        message["Bcc"] = ", ".join(bcc_addresses)
    if reply_to:
        reply_address = sanitize_header(reply_to)
        _validate_addresses([reply_address], "Reply-To")
        message["Reply-To"] = reply_address

    message["Subject"] = sanitize_header(subject)
    message["Date"] = format_datetime(datetime.now(tz=UTC))

    # A Message-ID in the sender's own domain costs nothing and is the handle you search on in
    # Exchange message tracking when someone reports a voicemail that never arrived.
    _, _, sender_domain = sender_address.partition("@")
    message["Message-ID"] = make_msgid(domain=sender_domain or None)

    if importance == "high":
        message["X-Priority"] = "1"
        message["Importance"] = "high"

    if headers:
        for name, value in headers.items():
            clean_name = sanitize_header(name)
            if clean_name and clean_name not in message:
                message[clean_name] = sanitize_header(value)

    message.set_content(body)
    if html is not None:
        message.add_alternative(html, subtype="html")

    for item in attachment_list:
        maintype, subtype = _split_mime_type(item.mime_type)
        message.add_attachment(
            item.data,
            maintype=maintype,
            subtype=subtype,
            filename=sanitize_header(item.filename),
        )

    return message


def message_summary(message: EmailMessage) -> dict[str, Any]:
    """
    Loggable facts about a message, with no recipient addresses in it.

    Recipient *domains* are enough to tell "the mail is going nowhere" from "the mail is going
    to the wrong place" while keeping mailbox names out of logs that ship off the box.
    """
    domains: set[str] = set()
    count = 0
    for header in ("To", "Cc", "Bcc"):
        raw = message.get(header)
        if not raw:
            continue
        for address in str(raw).split(","):
            cleaned = address.strip()
            if cleaned:
                count += 1
                domains.add(cleaned.rpartition("@")[2].lower())

    return {
        "message_id": message.get("Message-ID", ""),
        "recipient_count": count,
        "recipient_domains": sorted(domains),
        "subject_length": len(str(message.get("Subject", ""))),
    }
