"""Shared pytest fixtures for PBX test suite."""

import smtplib
from collections.abc import Callable, Generator
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from flask.testing import FlaskClient

if TYPE_CHECKING:
    from pbx.mail import SmtpSettings


@pytest.fixture
def mock_config() -> MagicMock:
    """Provide a mock Config object for testing."""
    config = MagicMock()
    config.get.return_value = None
    config.config = {
        "server": {
            "host": "0.0.0.0",
            "external_ip": "127.0.0.1",
        },
        "sip": {
            "port": 5060,
            "domain": "pbx.local",
        },
        "api": {
            "port": 9000,
            "ssl": {"enabled": False},
        },
        "security": {
            "fips_mode": False,
        },
    }

    def config_get_side_effect(key: str, default: Any = None) -> Any:
        keys = key.split(".")
        value = config.config
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
        return value

    config.get.side_effect = config_get_side_effect
    return config


@pytest.fixture
def mock_database() -> MagicMock:
    """Provide a mock database for testing."""
    db = MagicMock()
    db.get_all.return_value = []
    db.get.return_value = None
    return db


@pytest.fixture
def mock_extension() -> MagicMock:
    """Provide a mock extension for testing."""
    ext = MagicMock()
    ext.number = "1001"
    ext.name = "Test Extension"
    ext.registered = True
    ext.config = {
        "email": "test@example.com",
        "allow_external": True,
        "is_admin": False,
        "voicemail_pin_hash": "hashed_pin",
    }
    return ext


@pytest.fixture
def mock_pbx_core(mock_config: MagicMock, mock_database: MagicMock) -> MagicMock:
    """Provide a mock PBXCore for testing."""
    pbx_core = MagicMock()
    pbx_core.config = mock_config
    pbx_core.extension_db = mock_database
    pbx_core.call_manager = MagicMock()
    pbx_core.call_manager.get_active_calls.return_value = []
    pbx_core.extension_registry = MagicMock()
    pbx_core.extension_registry.get_all.return_value = []
    pbx_core.metrics_exporter = None
    return pbx_core


@pytest.fixture
def api_client(mock_pbx_core: MagicMock) -> Generator[FlaskClient]:
    """Provide a Flask test client for API testing."""
    from pbx.api.app import create_app

    app = create_app(mock_pbx_core)
    app.config["TESTING"] = True

    with app.test_client() as client:
        yield client


@pytest.fixture
def sip_message_factory() -> Callable[..., str]:
    """Factory fixture to create SIP messages for testing."""

    def make_sip_message(
        method: str = "INVITE",
        uri: str = "sip:1002@pbx.local",
        from_ext: str = "1001",
        to_ext: str = "1002",
    ) -> str:
        return (
            f"{method} {uri} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP 192.168.1.100:5060;branch=z9hG4bK776asdhds\r\n"
            f"From: <sip:{from_ext}@pbx.local>;tag=1928301774\r\n"
            f"To: <sip:{to_ext}@pbx.local>\r\n"
            f"Call-ID: a84b4c76e66710@192.168.1.100\r\n"
            f"CSeq: 314159 {method}\r\n"
            f"Content-Length: 0\r\n\r\n"
        )

    return make_sip_message


class FakeSmtp:
    """
    Scriptable stand-in for :class:`smtplib.SMTP`, satisfying ``pbx.mail.SmtpTransport``.

    Records the conversation so tests can assert on protocol *order* (EHLO before STARTTLS
    before AUTH), and can be scripted to fail a set number of times or to refuse specific
    recipients. Using a real object rather than ``mock.patch("smtplib.SMTP")`` means the
    client is exercised through the same seam production uses.
    """

    def __init__(
        self,
        *,
        fail_times: int = 0,
        failure: Exception | None = None,
        refuse: dict[str, tuple[int, bytes]] | None = None,
        quit_raises: bool = False,
    ) -> None:
        self.conversation: list[str] = []
        self.sent: list[Any] = []
        self.fail_times = fail_times
        self.failure = failure or smtplib.SMTPServerDisconnected("connection lost")
        self.refuse = refuse or {}
        self.quit_raises = quit_raises
        self.closed = False
        self.tls_context = None

    def ehlo_or_helo_if_needed(self) -> None:
        self.conversation.append("ehlo")

    def has_extn(self, name: str) -> bool:
        self.conversation.append(f"has_extn:{name}")
        return True

    def starttls(self, *, context: Any = None) -> tuple[int, bytes]:
        self.conversation.append("starttls")
        self.tls_context = context
        return (220, b"ready to start TLS")

    def login(self, user: str, password: str) -> tuple[int, bytes]:
        self.conversation.append(f"login:{user}")
        return (235, b"authenticated")

    def send_message(
        self, msg: Any, from_addr: str | None = None, to_addrs: Any = None
    ) -> dict[str, tuple[int, bytes]]:
        self.conversation.append(f"send_message:from={from_addr}")
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.failure
        self.sent.append(msg)
        return dict(self.refuse)

    def quit(self) -> tuple[int, bytes]:
        self.conversation.append("quit")
        if self.quit_raises:
            raise smtplib.SMTPServerDisconnected("already gone")
        return (221, b"bye")

    def close(self) -> None:
        self.conversation.append("close")
        self.closed = True


@pytest.fixture
def fake_smtp() -> FakeSmtp:
    """A FakeSmtp that accepts everything."""
    return FakeSmtp()


@pytest.fixture
def smtp_settings() -> "SmtpSettings":
    """Minimal working SMTP settings with retry delays short enough for tests."""
    from pbx.mail import SmtpSettings

    return SmtpSettings(
        host="mail.corp.local",
        port=587,
        security="starttls",
        auth="none",
        from_address="pbx@corp.local",
        from_name="Warden VoIP",
        max_retries=2,
        retry_backoff=0.01,
    )
