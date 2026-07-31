"""
Mail transport for the PBX.

This package owns *how* mail is sent: connection, TLS, authentication, retry, queueing and
MIME assembly. It owns none of what mail *says*. Subject templates, body wording and the
decision to attach a recording belong to the feature raising the notification, which is why
nothing here knows what a voicemail or Kari's Law is.

Configuration lives under the top-level ``smtp:`` key. The password is read from the
``SMTP_PASSWORD`` environment variable and never from the config file.

Typical use, from a feature holding a reference to ``pbx_core.mailer``::

    mailer.send_async(
        "user@corp.com",
        "New voicemail from 5551234567",
        body_text,
        attachments=[Attachment.from_path(Path(wav_path))],
    )
"""

from pbx.mail.client import SendResult, SmtpClient, SmtpTransport, TransportFactory
from pbx.mail.dispatcher import Mailer
from pbx.mail.errors import (
    EmailConfigError,
    EmailError,
    EmailPermanentError,
    EmailTransientError,
    classify,
)
from pbx.mail.message import Attachment, build_message, sanitize_header
from pbx.mail.settings import SmtpSettings

__all__ = [
    "Attachment",
    "EmailConfigError",
    "EmailError",
    "EmailPermanentError",
    "EmailTransientError",
    "Mailer",
    "SendResult",
    "SmtpClient",
    "SmtpSettings",
    "SmtpTransport",
    "TransportFactory",
    "build_message",
    "classify",
    "sanitize_header",
]
