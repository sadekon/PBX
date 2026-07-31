"""Tests for pbx.mail.message -- MIME structure, encoding and header injection."""

import email

import pytest

from pbx.mail import Attachment, EmailConfigError, SmtpSettings, build_message
from pbx.mail.message import message_summary, sanitize_header


@pytest.fixture
def settings():
    return SmtpSettings(
        host="mail.corp.local",
        from_address="pbx@corp.local",
        from_name="Warden VoIP",
    )


def _reparse(message):
    """Round-trip through the serialized form, which is what actually crosses the wire."""
    return email.message_from_string(message.as_string())


@pytest.mark.unit
class TestSanitizeHeader:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("plain", "plain"),
            ("with\rcarriage", "withcarriage"),
            ("with\nnewline", "withnewline"),
            ("with\r\nboth", "withboth"),
            ("with\x00null", "withnull"),
            ("  padded  ", "padded"),
        ],
    )
    def test_strips_line_breaking_characters(self, raw, expected):
        assert sanitize_header(raw) == expected


@pytest.mark.unit
class TestHeaderInjection:
    """Caller ID arrives from the SIP wire, so every interpolated header is attacker-influenced."""

    def test_crlf_in_subject_cannot_forge_a_header(self, settings):
        message = build_message(
            settings, "user@corp.local", "Voicemail\r\nBcc: attacker@evil.com", "body"
        )
        raw = message.as_string()

        assert _reparse(message)["Bcc"] is None
        assert not any(line.startswith("Bcc:") for line in raw.splitlines())

    def test_crlf_in_display_name_cannot_forge_a_header(self, settings):
        message = build_message(
            settings,
            "user@corp.local",
            "Subject",
            "body",
            from_name="Warden\r\nX-Injected: yes",
        )
        raw = message.as_string()

        assert _reparse(message)["X-Injected"] is None
        assert not any(line.startswith("X-Injected") for line in raw.splitlines())

    def test_crlf_in_extra_header_value_cannot_forge_a_header(self, settings):
        message = build_message(
            settings,
            "user@corp.local",
            "Subject",
            "body",
            headers={"X-Extension": "1001\r\nX-Sneaky: yes"},
        )

        assert _reparse(message)["X-Sneaky"] is None


@pytest.mark.unit
class TestRequiredHeaders:
    def test_message_id_is_present_and_uses_the_sender_domain(self, settings):
        message = build_message(settings, "user@corp.local", "Subject", "body")

        assert message["Message-ID"]
        assert message["Message-ID"].endswith("@corp.local>")

    def test_date_is_present_and_timezone_aware(self, settings):
        message = build_message(settings, "user@corp.local", "Subject", "body")

        assert message["Date"]
        assert email.utils.parsedate_to_datetime(message["Date"]).tzinfo is not None

    def test_from_combines_display_name_and_address(self, settings):
        message = build_message(settings, "user@corp.local", "Subject", "body")

        assert "Warden VoIP" in message["From"]
        assert "pbx@corp.local" in message["From"]

    def test_non_ascii_subject_is_rfc2047_encoded(self, settings):
        message = build_message(settings, "user@corp.local", "Grüße von Björn", "body")
        raw = message.as_string()

        assert "Grüße" not in raw
        assert str(_reparse(message)["Subject"]).startswith("=?")

    def test_high_importance_adds_both_headers(self, settings):
        message = build_message(settings, "user@corp.local", "Subject", "body", importance="high")

        assert message["X-Priority"] == "1"
        assert message["Importance"] == "high"

    def test_normal_importance_adds_neither(self, settings):
        message = build_message(settings, "user@corp.local", "Subject", "body")

        assert message["X-Priority"] is None


@pytest.mark.unit
class TestRecipients:
    def test_accepts_a_single_address(self, settings):
        assert build_message(settings, "a@corp.local", "s", "b")["To"] == "a@corp.local"

    def test_accepts_a_list(self, settings):
        message = build_message(settings, ["a@corp.local", "b@corp.local"], "s", "b")

        assert message["To"] == "a@corp.local, b@corp.local"

    def test_accepts_a_comma_separated_string(self, settings):
        message = build_message(settings, "a@corp.local,b@corp.local", "s", "b")

        assert message["To"] == "a@corp.local, b@corp.local"

    def test_bcc_is_set_as_a_header_for_send_message_to_consume(self, settings):
        """send_message reads Bcc for the envelope, then strips it before transmission."""
        message = build_message(settings, "a@corp.local", "s", "b", bcc="hidden@corp.local")

        assert message["Bcc"] == "hidden@corp.local"

    def test_no_recipients_is_a_config_error(self, settings):
        with pytest.raises(EmailConfigError, match="No recipients"):
            build_message(settings, [], "s", "b")

    def test_malformed_recipient_is_a_config_error(self, settings):
        with pytest.raises(EmailConfigError, match="Invalid To address"):
            build_message(settings, "not-an-address", "s", "b")

    def test_missing_sender_is_a_config_error(self):
        with pytest.raises(EmailConfigError, match="No sender address"):
            build_message(SmtpSettings(), "a@corp.local", "s", "b")


@pytest.mark.unit
class TestBodyAndAttachments:
    def test_plain_text_body_round_trips(self, settings):
        message = build_message(settings, "a@corp.local", "s", "Hello there")

        assert message.get_content_type() == "text/plain"
        assert "Hello there" in message.get_content()

    def test_html_alternative_produces_multipart_alternative(self, settings):
        message = build_message(
            settings, "a@corp.local", "s", "plain body", html="<p>rich body</p>"
        )
        parsed = _reparse(message)

        assert parsed.get_content_type() == "multipart/alternative"
        assert {part.get_content_type() for part in parsed.walk()} >= {
            "text/plain",
            "text/html",
        }

    def test_attachment_structure_survives_reparsing(self, settings):
        attachment = Attachment(filename="vm.wav", data=b"RIFF....", mime_type="audio/wav")
        message = build_message(settings, "a@corp.local", "s", "body", attachments=[attachment])
        parsed = _reparse(message)

        attached = [p for p in parsed.walk() if p.get_filename() == "vm.wav"]
        assert len(attached) == 1
        assert attached[0].get_content_type() == "audio/wav"
        assert attached[0].get_payload(decode=True) == b"RIFF...."

    def test_oversize_attachment_is_rejected_before_the_wire(self, settings):
        small = SmtpSettings(
            host=settings.host, from_address=settings.from_address, max_attachment_bytes=10
        )
        attachment = Attachment(filename="big.wav", data=b"x" * 11)

        with pytest.raises(EmailConfigError, match="exceed"):
            build_message(small, "a@corp.local", "s", "b", attachments=[attachment])

    def test_attachment_from_path_guesses_the_mime_type(self, tmp_path):
        path = tmp_path / "greeting.wav"
        path.write_bytes(b"RIFF")

        attachment = Attachment.from_path(path)

        assert attachment.filename == "greeting.wav"
        assert attachment.data == b"RIFF"
        assert attachment.mime_type == "audio/x-wav"

    def test_attachment_from_missing_path_is_a_config_error(self, tmp_path):
        with pytest.raises(EmailConfigError, match="Could not read attachment"):
            Attachment.from_path(tmp_path / "gone.wav")


@pytest.mark.unit
class TestMessageSummary:
    def test_summary_reports_domains_not_addresses(self, settings):
        message = build_message(settings, ["a@corp.local", "b@other.com"], "Subject", "body")

        summary = message_summary(message)

        assert summary["recipient_count"] == 2
        assert summary["recipient_domains"] == ["corp.local", "other.com"]
        assert "a@corp.local" not in str(summary)
