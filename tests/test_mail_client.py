"""Tests for pbx.mail.client -- protocol order, TLS modes, and failure classification."""

import smtplib
import socket
import ssl

import pytest

from conftest import FakeSmtp
from pbx.mail import SmtpClient, SmtpSettings, build_message
from pbx.mail.client import build_ssl_context
from pbx.mail.errors import (
    EmailConfigError,
    EmailPermanentError,
    EmailTransientError,
    classify,
)


def make_client(settings, transport):
    return SmtpClient(settings, transport_factory=lambda _s: transport)


def make_message(settings, to="user@corp.local"):
    return build_message(settings, to, "Subject", "body")


@pytest.mark.unit
class TestSecurityModes:
    """All three transport modes, including the one use_tls could not express."""

    def test_starttls_secures_then_authenticates_in_order(self, smtp_settings):
        settings = SmtpSettings(
            host=smtp_settings.host,
            from_address=smtp_settings.from_address,
            security="starttls",
            auth="login",
            username="pbx",
            password="secret",
        )
        transport = FakeSmtp()

        result = make_client(settings, transport).send(make_message(settings))

        assert result.ok is True
        ordered = [step for step in transport.conversation if step != "has_extn:starttls"]
        assert ordered.index("starttls") < ordered.index("login:pbx")
        assert ordered.index("ehlo") < ordered.index("starttls")

    def test_starttls_is_re_greeted_so_auth_capabilities_are_re_read(self, smtp_settings):
        """AUTH is commonly advertised only after the channel is encrypted."""
        transport = FakeSmtp()

        make_client(smtp_settings, transport).send(make_message(smtp_settings))

        assert transport.conversation.count("ehlo") == 2

    def test_smtps_does_not_call_starttls(self, smtp_settings):
        """Implicit TLS is established by the constructor, not by a STARTTLS command."""
        settings = SmtpSettings(
            host=smtp_settings.host,
            from_address=smtp_settings.from_address,
            security="smtps",
            port=465,
        )
        transport = FakeSmtp()

        result = make_client(settings, transport).send(make_message(settings))

        assert result.ok is True
        assert "starttls" not in transport.conversation

    def test_auth_none_never_logs_in(self, smtp_settings):
        transport = FakeSmtp()

        make_client(smtp_settings, transport).send(make_message(smtp_settings))

        assert not any(step.startswith("login") for step in transport.conversation)


@pytest.mark.unit
class TestSslContext:
    def test_default_context_verifies_hostname_and_certificate(self):
        context = build_ssl_context(SmtpSettings(host="h", from_address="a@b.com"))

        assert context.check_hostname is True
        assert context.verify_mode == ssl.CERT_REQUIRED

    def test_verify_cert_false_disables_both_checks(self):
        context = build_ssl_context(
            SmtpSettings(host="h", from_address="a@b.com", verify_cert=False)
        )

        assert context.check_hostname is False
        assert context.verify_mode == ssl.CERT_NONE


@pytest.mark.unit
class TestSendOutcomes:
    def test_successful_send_reports_the_message_id(self, smtp_settings):
        transport = FakeSmtp()
        message = make_message(smtp_settings)

        result = make_client(smtp_settings, transport).send(message)

        assert result.ok is True
        assert result.message_id == message["Message-ID"]
        assert len(transport.sent) == 1

    def test_partial_recipient_refusal_is_reported_as_failure(self, smtp_settings):
        """send_message returns normally here; treating that as success hides lost mail."""
        transport = FakeSmtp(refuse={"bad@corp.local": (550, b"No such user")})

        result = make_client(smtp_settings, transport).send(make_message(smtp_settings))

        assert result.ok is False
        assert isinstance(result.error, EmailPermanentError)
        assert "bad@corp.local" in result.error.detail

    def test_unconfigured_settings_fail_without_connecting(self):
        transport = FakeSmtp()

        result = make_client(SmtpSettings(), transport).send(
            build_message(SmtpSettings(host="h", from_address="a@b.com"), "u@corp.local", "s", "b")
        )

        assert result.ok is False
        assert isinstance(result.error, EmailConfigError)
        assert transport.conversation == []

    def test_envelope_sender_override_is_passed_through(self, smtp_settings):
        settings = SmtpSettings(
            host=smtp_settings.host,
            from_address=smtp_settings.from_address,
            envelope_from="bounces@corp.local",
        )
        transport = FakeSmtp()

        make_client(settings, transport).send(make_message(settings))

        assert "send_message:from=bounces@corp.local" in transport.conversation

    def test_without_override_smtplib_derives_the_envelope_sender(self, smtp_settings):
        transport = FakeSmtp()

        make_client(smtp_settings, transport).send(make_message(smtp_settings))

        assert "send_message:from=None" in transport.conversation


@pytest.mark.unit
class TestConnectionLifecycle:
    def test_connection_is_closed_after_a_successful_send(self, smtp_settings):
        transport = FakeSmtp()

        make_client(smtp_settings, transport).send(make_message(smtp_settings))

        assert transport.conversation[-1] == "quit"

    def test_connection_is_closed_after_a_failed_send(self, smtp_settings):
        """The old implementation leaked the socket on every failure."""
        transport = FakeSmtp(fail_times=1)

        make_client(smtp_settings, transport).send(make_message(smtp_settings))

        assert "quit" in transport.conversation

    def test_close_is_attempted_when_quit_itself_fails(self, smtp_settings):
        transport = FakeSmtp(quit_raises=True)

        result = make_client(smtp_settings, transport).send(make_message(smtp_settings))

        assert result.ok is True
        assert transport.closed is True


@pytest.mark.unit
class TestVerify:
    def test_verify_exercises_the_handshake_without_sending(self, smtp_settings):
        transport = FakeSmtp()

        result = make_client(smtp_settings, transport).verify()

        assert result.ok is True
        assert transport.sent == []
        assert "starttls" in transport.conversation

    def test_verify_reports_authentication_failure(self, smtp_settings):
        settings = SmtpSettings(
            host=smtp_settings.host,
            from_address=smtp_settings.from_address,
            auth="login",
            username="pbx",
            password="wrong",
        )
        transport = FakeSmtp()

        def failing_login(user, password):
            raise smtplib.SMTPAuthenticationError(535, b"5.7.3 Authentication unsuccessful")

        transport.login = failing_login

        result = make_client(settings, transport).verify()

        assert result.ok is False
        assert isinstance(result.error, EmailPermanentError)
        assert result.error.code == 535


@pytest.mark.unit
class TestClassify:
    """The taxonomy is what the retry loop reads, so each branch is pinned."""

    @pytest.mark.parametrize(
        "exc",
        [
            smtplib.SMTPServerDisconnected("gone"),
            smtplib.SMTPConnectError(421, b"try later"),
            socket.gaierror("name resolution failed"),
            TimeoutError("timed out"),
            OSError("network unreachable"),
            smtplib.SMTPResponseException(451, b"4.3.2 try again"),
        ],
    )
    def test_transient_failures_are_retryable(self, exc):
        error = classify(exc)

        assert isinstance(error, EmailTransientError)
        assert error.retryable is True

    @pytest.mark.parametrize(
        "exc",
        [
            smtplib.SMTPResponseException(550, b"5.7.1 rejected"),
            smtplib.SMTPSenderRefused(550, b"5.7.54 not allowed", "pbx@corp.local"),
            smtplib.SMTPRecipientsRefused({"a@b.com": (550, b"no such user")}),
            smtplib.SMTPAuthenticationError(535, b"5.7.3 bad credentials"),
        ],
    )
    def test_permanent_failures_are_not_retryable(self, exc):
        error = classify(exc)

        assert isinstance(error, EmailPermanentError)
        assert error.retryable is False

    @pytest.mark.parametrize(
        "exc",
        [
            smtplib.SMTPNotSupportedError("SMTPUTF8 not supported"),
            ssl.SSLCertVerificationError("hostname mismatch"),
        ],
    )
    def test_configuration_failures_are_not_retryable(self, exc):
        error = classify(exc)

        assert isinstance(error, EmailConfigError)
        assert error.retryable is False

    def test_an_existing_email_error_passes_through_unchanged(self):
        original = EmailConfigError("already classified")

        assert classify(original) is original

    def test_transient_4xx_beats_the_generic_response_branch(self):
        assert classify(smtplib.SMTPResponseException(450, b"mailbox busy")).retryable is True
