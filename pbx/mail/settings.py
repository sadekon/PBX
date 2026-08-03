"""
SMTP transport settings.

The single most important thing this module does is keep ``security`` (how the connection is
protected) separate from ``auth`` (how, or whether, we identify ourselves). The setting it
replaces was one boolean, ``use_tls``, which made implicit TLS on port 465 impossible to
express and left anonymous relay -- the normal answer for an appliance talking to an
on-premises Exchange server -- indistinguishable from "no encryption".

**TLS is mandatory.** There is no plaintext mode: ``security`` is ``starttls`` or ``smtps``.
A configuration asking for anything else is corrected to ``starttls`` and the substitution is
reported through :meth:`SmtpSettings.validate`. If the server does not advertise STARTTLS the
send fails rather than falling back to the clear -- see :meth:`SmtpClient._prepare`.

``auth: none`` is *not* the insecure setting. It means anonymous, IP-restricted relay, which
is the documented pattern for an appliance talking to on-premises Exchange, and it keeps a
stored credential out of the deployment entirely. The transport is encrypted either way.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Final, Literal, get_args

from pbx.utils.config import Config

__all__ = ["AuthMode", "SecurityMode", "SmtpSettings"]

SecurityMode = Literal["starttls", "smtps"]
AuthMode = Literal["none", "login"]

SECURITY_MODES: Final[tuple[str, ...]] = get_args(SecurityMode)
AUTH_MODES: Final[tuple[str, ...]] = get_args(AuthMode)

#: Environment variable holding the SMTP password. Deliberately the only source -- see
#: :meth:`SmtpSettings.from_dict`.
PASSWORD_ENV_VAR: Final[str] = "SMTP_PASSWORD"

_MAX_PORT: Final[int] = 65535


def _is_unresolved(value: str) -> bool:
    """True for a value that still looks like an unsubstituted ``${VAR}`` placeholder.

    ``EnvironmentLoader.resolve_value`` leaves the original text in place when a variable has
    neither a value nor a default, so an unset variable can surface as the literal string
    ``"${SMTP_HOST}"``. Treating that as configured would produce a DNS lookup for a host name
    made of punctuation.
    """
    return "${" in value or value.startswith("$")


def _as_str(raw: Any, default: str = "") -> str:
    if raw is None:
        return default
    text = str(raw).strip()
    if not text or _is_unresolved(text):
        return default
    return text


def _as_int(raw: Any, default: int) -> int:
    text = _as_str(raw)
    if not text:
        return default
    try:
        return int(float(text))
    except ValueError:
        return default


def _as_float(raw: Any, default: float) -> float:
    text = _as_str(raw)
    if not text:
        return default
    try:
        return float(text)
    except ValueError:
        return default


def _as_bool(raw: Any, default: bool) -> bool:
    if isinstance(raw, bool):
        return raw
    text = _as_str(raw).lower()
    if text in ("true", "yes", "on", "1"):
        return True
    if text in ("false", "no", "off", "0"):
        return False
    return default


@dataclass(frozen=True, slots=True)
class SmtpSettings:
    """Everything the transport needs, and nothing about what the mail says."""

    host: str = ""
    port: int = 587
    security: SecurityMode = "starttls"
    timeout: float = 10.0
    helo_hostname: str = ""
    verify_cert: bool = True
    ca_file: str = ""
    auth: AuthMode = "none"
    username: str = ""
    password: str = field(default="", repr=False)
    from_address: str = ""
    from_name: str = "Warden VoIP"
    envelope_from: str = ""
    max_retries: int = 3
    retry_backoff: float = 2.0
    max_attachment_bytes: int = 8 * 1024 * 1024

    #: Divert every message to this address instead of its real recipients. A testing aid for
    #: running against live extensions without mailing their owners -- see build_message().
    #: Applied at the transport layer so it also covers emergency notification, which must
    #: not reach real emergency contacts during a test. Never set this in production.
    redirect_to: str = ""

    #: Problems found while coercing the raw config, surfaced through validate(). Populated by
    #: from_dict() so a typo in `security:` is reported rather than silently defaulted.
    config_warnings: tuple[str, ...] = field(default=(), repr=False)

    @classmethod
    def from_dict(cls, smtp: dict[str, Any]) -> SmtpSettings:
        """
        Build settings from the ``smtp:`` section of config.yml.

        Takes a plain dict rather than a Config object so this package never depends on the
        configuration machinery. The password is read from the environment and never from the
        supplied mapping, so a credential accidentally committed to config.yml is inert.
        """
        warnings: list[str] = []

        security = _as_str(smtp.get("security"), "starttls").lower()
        if security not in SECURITY_MODES:
            detail = (
                "plaintext SMTP is no longer supported"
                if security == "none"
                else f"expected one of {', '.join(SECURITY_MODES)}"
            )
            warnings.append(f"smtp.security {security!r} rejected ({detail}); using 'starttls'")
            security = "starttls"

        # 'basic' was a synonym for 'login' -- both simply call smtplib's login(), which
        # negotiates the mechanism from the server's AUTH advertisement. Accepted quietly so
        # an existing config keeps working.
        auth = _as_str(smtp.get("auth"), "none").lower()
        if auth == "basic":
            auth = "login"
        elif auth not in AUTH_MODES:
            warnings.append(
                f"smtp.auth {auth!r} is not one of {', '.join(AUTH_MODES)}; using 'none'"
            )
            auth = "none"

        default_port = 465 if security == "smtps" else 587

        return cls(
            host=_as_str(smtp.get("host")),
            port=_as_int(smtp.get("port"), default_port),
            security=security,  # type: ignore[arg-type]  # membership checked above
            timeout=_as_float(smtp.get("timeout"), 10.0),
            helo_hostname=_as_str(smtp.get("helo_hostname")),
            verify_cert=_as_bool(smtp.get("verify_cert"), True),
            ca_file=_as_str(smtp.get("ca_file")),
            auth=auth,  # type: ignore[arg-type]  # membership checked above
            username=_as_str(smtp.get("username")),
            password=os.environ.get(PASSWORD_ENV_VAR, ""),
            from_address=_as_str(smtp.get("from_address")),
            from_name=_as_str(smtp.get("from_name"), "Warden VoIP"),
            envelope_from=_as_str(smtp.get("envelope_from")),
            max_retries=_as_int(smtp.get("max_retries"), 3),
            retry_backoff=_as_float(smtp.get("retry_backoff"), 2.0),
            max_attachment_bytes=_as_int(smtp.get("max_attachment_bytes"), 8 * 1024 * 1024),
            redirect_to=_as_str(smtp.get("redirect_to")),
            config_warnings=tuple(warnings),
        )

    @property
    def is_configured(self) -> bool:
        """True when there is somewhere to connect and something to put in ``From:``."""
        return bool(self.host) and bool(self.from_address)

    @property
    def sender(self) -> str:
        """Envelope sender: the explicit override if set, otherwise the header address."""
        return self.envelope_from or self.from_address

    def validate(self) -> list[str]:
        """
        Return every problem found, most-blocking first. Empty means the settings are usable.

        Reported rather than raised: an unconfigured PBX must still start and still record
        voicemail, so the caller decides whether a problem is fatal.
        """
        problems: list[str] = list(self.config_warnings)

        if not self.host:
            problems.append("smtp.host is not set")
        if not self.from_address:
            problems.append("smtp.from_address is not set")
        elif not Config.validate_email(self.from_address):
            problems.append(f"smtp.from_address {self.from_address!r} is not a valid address")

        if self.envelope_from and not Config.validate_email(self.envelope_from):
            problems.append(f"smtp.envelope_from {self.envelope_from!r} is not a valid address")

        if not 0 < self.port <= _MAX_PORT:
            problems.append(f"smtp.port {self.port} is outside 1-{_MAX_PORT}")

        if self.auth != "none":
            if not self.username:
                problems.append(f"smtp.auth is {self.auth!r} but smtp.username is empty")
            if not self.password:
                problems.append(
                    f"smtp.auth is {self.auth!r} but the {PASSWORD_ENV_VAR} "
                    "environment variable is empty"
                )

        if self.ca_file and not Path(self.ca_file).is_file():
            problems.append(f"smtp.ca_file {self.ca_file!r} does not exist")

        if not self.verify_cert:
            problems.append(
                "smtp.verify_cert is false -- the server certificate is not checked, which "
                "defeats TLS against an active attacker. If the certificate is issued by an "
                "internal CA, set smtp.ca_file instead; if the name does not match, correct "
                "smtp.host to the name on the certificate"
            )

        if self.redirect_to:
            if not Config.validate_email(self.redirect_to):
                problems.append(
                    f"smtp.redirect_to {self.redirect_to!r} is not a valid address; "
                    "mail cannot be delivered while it is set"
                )
            else:
                problems.append(
                    f"smtp.redirect_to is set -- ALL mail is being diverted to "
                    f"{self.redirect_to} and no real recipient will receive anything. "
                    "This is a testing aid; unset it before production use"
                )

        if self.timeout <= 0:
            problems.append(f"smtp.timeout {self.timeout} must be greater than zero")
        if self.max_retries < 0:
            problems.append(f"smtp.max_retries {self.max_retries} cannot be negative")
        if self.max_attachment_bytes <= 0:
            problems.append(
                f"smtp.max_attachment_bytes {self.max_attachment_bytes} must be greater than zero"
            )

        return problems

    def redacted(self) -> dict[str, Any]:
        """All settings as a dict, safe to log or return from an API. Never the password."""
        result: dict[str, Any] = {}
        for spec in fields(self):
            if spec.name == "password":
                result[spec.name] = "***" if self.password else ""
            elif spec.name == "config_warnings":
                continue
            else:
                result[spec.name] = getattr(self, spec.name)
        result["is_configured"] = self.is_configured
        return result
