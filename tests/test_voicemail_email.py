#!/usr/bin/env python3
"""
Tests for voicemail email notification.

Rewritten for the pbx/mail cutover. The previous version asserted on production config.yml
values (a specific from_address, port 587) through EmailNotifier internals -- a config
snapshot rather than a test of behaviour, and it broke whenever the deployment changed. What
matters here is that saving a message produces the right notification, so these tests drive a
VoicemailBox against a recording stand-in for the Mailer.
"""

import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pbx.features.voicemail import VoicemailBox, VoicemailSystem


class RecordingMailer:
    """Stands in for pbx.mail.Mailer, capturing what the feature asked to send."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.calls: list[dict] = []

    def send_async(self, to, subject, body, **kwargs) -> None:
        self.calls.append({"to": to, "subject": subject, "body": body, **kwargs})

    def send(self, to, subject, body, **kwargs):
        self.send_async(to, subject, body, **kwargs)
        return MagicMock(ok=True, message_id="<test@corp.local>", error=None)


@pytest.fixture
def config():
    """Config stub returning the voicemail content settings the feature reads."""
    values = {
        "voicemail.email.subject_template": "New Voicemail from {caller_id} - {timestamp}",
        "voicemail.email.include_attachment": True,
        "voicemail.reminders.enabled": True,
    }
    stub = MagicMock()
    stub.get.side_effect = lambda key, default=None: values.get(key, default)
    stub.get_extension.return_value = {"number": "1001", "email": "user@corp.local"}
    return stub


@pytest.fixture
def storage():
    path = tempfile.mkdtemp()
    yield path
    import shutil

    shutil.rmtree(path, ignore_errors=True)


@pytest.mark.unit
class TestNotificationContent:
    """Subject and body are content, so they belong to the feature, not to pbx/mail."""

    def test_subject_uses_the_configured_template(self, storage, config):
        box = VoicemailBox("1001", storage, config=config, mailer=RecordingMailer())

        subject = box._notification_subject("5551234567", datetime(2026, 7, 30, 9, 15, tzinfo=UTC))

        assert "5551234567" in subject
        assert "2026-07-30 09:15:00" in subject

    def test_bad_template_falls_back_instead_of_losing_the_notification(self, storage):
        stub = MagicMock()
        stub.get.side_effect = lambda key, default=None: (
            "Voicemail {nonexistent_field}"
            if key == "voicemail.email.subject_template"
            else default
        )
        box = VoicemailBox("1001", storage, config=stub, mailer=RecordingMailer())

        assert box._notification_subject("555", "now") == "New Voicemail from 555"

    def test_body_contains_the_call_details(self, storage, config):
        box = VoicemailBox("1001", storage, config=config, mailer=RecordingMailer())

        body = box._notification_body("5551234567", datetime(2026, 7, 30, 9, 15, tzinfo=UTC), 95)

        assert "1001" in body
        assert "5551234567" in body
        assert "1:35" in body  # 95 seconds

    def test_body_omits_duration_when_unknown(self, storage, config):
        box = VoicemailBox("1001", storage, config=config, mailer=RecordingMailer())

        assert "Duration" not in box._notification_body("555", "now", None)


@pytest.mark.unit
class TestNotificationDispatch:
    def test_saving_a_message_queues_a_notification(self, storage, config):
        mailer = RecordingMailer()
        system = VoicemailSystem(storage_path=storage, config=config, mailer=mailer)

        message_id = system.save_message("1001", "5551234567", b"RIFF" + b"\x00" * 100, 30)

        assert message_id is not None
        assert len(mailer.calls) == 1
        assert mailer.calls[0]["to"] == "user@corp.local"

    def test_notification_is_asynchronous(self, storage, config):
        """Sending inline blocked call teardown for the full SMTP connect timeout."""
        mailer = RecordingMailer()
        mailer.send = MagicMock(side_effect=AssertionError("must not send synchronously"))
        system = VoicemailSystem(storage_path=storage, config=config, mailer=mailer)

        system.save_message("1001", "555", b"RIFF" + b"\x00" * 100, 5)

        assert len(mailer.calls) == 1

    def test_recording_is_attached_when_configured(self, storage, config):
        mailer = RecordingMailer()
        system = VoicemailSystem(storage_path=storage, config=config, mailer=mailer)

        system.save_message("1001", "555", b"RIFF" + b"\x00" * 100, 5)

        attachments = mailer.calls[0]["attachments"]
        assert len(attachments) == 1
        assert attachments[0].filename.endswith(".wav")
        assert attachments[0].mime_type == "audio/wav"

    def test_attachment_is_omitted_when_disabled(self, storage):
        stub = MagicMock()
        stub.get.side_effect = lambda key, default=None: (
            False if key == "voicemail.email.include_attachment" else default
        )
        stub.get_extension.return_value = {"number": "1001", "email": "user@corp.local"}
        mailer = RecordingMailer()
        system = VoicemailSystem(storage_path=storage, config=stub, mailer=mailer)

        system.save_message("1001", "555", b"RIFF" + b"\x00" * 100, 5)

        assert mailer.calls[0]["attachments"] == []

    def test_no_mailer_means_no_notification_and_no_crash(self, storage, config):
        system = VoicemailSystem(storage_path=storage, config=config, mailer=None)

        assert system.save_message("1001", "555", b"RIFF" + b"\x00" * 100, 5) is not None

    def test_extension_without_an_email_address_is_skipped(self, storage, config):
        config.get_extension.return_value = {"number": "1001"}
        mailer = RecordingMailer()
        system = VoicemailSystem(storage_path=storage, config=config, mailer=mailer)

        system.save_message("1001", "555", b"RIFF" + b"\x00" * 100, 5)

        assert mailer.calls == []


@pytest.mark.unit
class TestDailyReminders:
    def test_reminders_are_sent_for_unread_messages(self, storage, config):
        mailer = RecordingMailer()
        system = VoicemailSystem(storage_path=storage, config=config, mailer=mailer)
        system.save_message("1001", "555", b"RIFF" + b"\x00" * 100, 5)
        mailer.calls.clear()

        count = system.send_daily_reminders()

        assert count == 1
        assert "Unread Message" in mailer.calls[0]["subject"]

    def test_no_reminders_without_a_mailer(self, storage, config):
        system = VoicemailSystem(storage_path=storage, config=config, mailer=None)

        assert system.send_daily_reminders() == 0

    def test_reminder_body_lists_each_message(self):
        messages = [
            {"caller_id": "5551111111", "timestamp": datetime(2026, 7, 30, 9, 0, tzinfo=UTC)},
            {"caller_id": "5552222222", "timestamp": datetime(2026, 7, 30, 10, 0, tzinfo=UTC)},
        ]

        body = VoicemailSystem._reminder_body("1001", messages)

        assert "5551111111" in body
        assert "5552222222" in body
        assert "2 unread voicemail messages" in body

    def test_reminder_subject_is_singular_for_one_message(self):
        assert VoicemailSystem._reminder_subject(1).endswith("1 Unread Message")

    def test_reminder_subject_is_plural_for_several(self):
        assert VoicemailSystem._reminder_subject(3).endswith("3 Unread Messages")


@pytest.mark.unit
class TestStorage:
    def test_message_is_written_to_disk(self, storage, config):
        system = VoicemailSystem(storage_path=storage, config=config, mailer=RecordingMailer())

        system.save_message("1001", "555", b"RIFF" + b"\x00" * 100, 5)

        assert list((Path(storage) / "1001").glob("*.wav"))
        assert len(system.get_mailbox("1001").messages) == 1
