"""
Per-extension subscription to voicemail notification email.

The `voicemail_email_enabled` column gates whether new voicemail is emailed. The behaviour
that needs pinning most is the *default*: before the column existed, every extension with an
email address was notified unconditionally, so an absent value has to mean "subscribed".
Getting that wrong would silently unsubscribe every existing user the moment the migration
ran, with no error anywhere.
"""

import tempfile
from unittest.mock import MagicMock

import pytest

from pbx.features.voicemail import (
    VoicemailBox,
    VoicemailSystem,
    is_subscribed_to_voicemail_email,
)


class RecordingMailer:
    """Captures what the feature asked to send."""

    def __init__(self) -> None:
        self.enabled = True
        self.calls: list[dict] = []

    def send_async(self, to, subject, body, **kwargs) -> None:
        self.calls.append({"to": to, "subject": subject, "body": body, **kwargs})

    def send(self, to, subject, body, **kwargs):
        self.send_async(to, subject, body, **kwargs)
        return MagicMock(ok=True, message_id="<test@corp.local>", error=None)


@pytest.fixture
def storage():
    path = tempfile.mkdtemp()
    yield path
    import shutil

    shutil.rmtree(path, ignore_errors=True)


def _config(extension: dict | None, *, reminders: bool = True) -> MagicMock:
    values = {
        "voicemail.email.subject_template": "New Voicemail from {caller_id}",
        "voicemail.email.include_attachment": False,
        "voicemail.reminders.enabled": reminders,
    }
    stub = MagicMock()
    stub.get.side_effect = lambda key, default=None: values.get(key, default)
    stub.get_extension.return_value = extension
    return stub


@pytest.mark.unit
class TestSubscriptionHelper:
    def test_absent_key_means_subscribed(self):
        """The upgrade path: rows predating the column were already being emailed."""
        assert is_subscribed_to_voicemail_email({"number": "1001"}) is True

    def test_explicit_true(self):
        assert is_subscribed_to_voicemail_email({"voicemail_email_enabled": True}) is True

    def test_explicit_false(self):
        assert is_subscribed_to_voicemail_email({"voicemail_email_enabled": False}) is False

    def test_none_config_is_not_subscribed(self):
        assert is_subscribed_to_voicemail_email(None) is False

    def test_empty_config_is_not_subscribed(self):
        assert is_subscribed_to_voicemail_email({}) is False

    @pytest.mark.parametrize(("raw", "expected"), [(0, False), (1, True), ("", False)])
    def test_database_truthiness_is_coerced(self, raw, expected):
        """SQLite stores booleans as 0/1, so the helper must not compare identity."""
        assert is_subscribed_to_voicemail_email({"voicemail_email_enabled": raw}) is expected


@pytest.mark.unit
class TestNotificationGating:
    def test_subscribed_extension_is_notified(self, storage):
        mailer = RecordingMailer()
        config = _config(
            {"number": "1001", "email": "u@corp.local", "voicemail_email_enabled": True}
        )
        system = VoicemailSystem(storage_path=storage, config=config, mailer=mailer)

        system.save_message("1001", "5551234567", b"RIFF" + b"\x00" * 64, 5)

        assert len(mailer.calls) == 1

    def test_unsubscribed_extension_is_not_notified(self, storage):
        mailer = RecordingMailer()
        config = _config(
            {"number": "1001", "email": "u@corp.local", "voicemail_email_enabled": False}
        )
        system = VoicemailSystem(storage_path=storage, config=config, mailer=mailer)

        system.save_message("1001", "5551234567", b"RIFF" + b"\x00" * 64, 5)

        assert mailer.calls == []

    def test_extension_without_the_key_is_still_notified(self, storage):
        mailer = RecordingMailer()
        config = _config({"number": "1001", "email": "u@corp.local"})
        system = VoicemailSystem(storage_path=storage, config=config, mailer=mailer)

        system.save_message("1001", "5551234567", b"RIFF" + b"\x00" * 64, 5)

        assert len(mailer.calls) == 1

    def test_unsubscribing_does_not_stop_the_message_being_stored(self, storage):
        """Only the email is suppressed -- the voicemail itself must still be recorded."""
        mailer = RecordingMailer()
        config = _config(
            {"number": "1001", "email": "u@corp.local", "voicemail_email_enabled": False}
        )
        system = VoicemailSystem(storage_path=storage, config=config, mailer=mailer)

        message_id = system.save_message("1001", "555", b"RIFF" + b"\x00" * 64, 5)

        assert message_id is not None
        assert len(system.get_mailbox("1001").messages) == 1


@pytest.mark.unit
class TestReminderGating:
    def test_subscribed_extension_gets_reminders(self, storage):
        mailer = RecordingMailer()
        config = _config(
            {"number": "1001", "email": "u@corp.local", "voicemail_email_enabled": True}
        )
        system = VoicemailSystem(storage_path=storage, config=config, mailer=mailer)
        system.save_message("1001", "555", b"RIFF" + b"\x00" * 64, 5)
        mailer.calls.clear()

        assert system.send_daily_reminders() == 1

    def test_unsubscribed_extension_gets_no_reminders(self, storage):
        mailer = RecordingMailer()
        config = _config(
            {"number": "1001", "email": "u@corp.local", "voicemail_email_enabled": False}
        )
        system = VoicemailSystem(storage_path=storage, config=config, mailer=mailer)
        system.save_message("1001", "555", b"RIFF" + b"\x00" * 64, 5)
        mailer.calls.clear()

        assert system.send_daily_reminders() == 0
        assert mailer.calls == []


@pytest.mark.unit
class TestVoicemailBoxDirect:
    def test_box_skips_send_when_unsubscribed(self, storage):
        """Gating happens before _send_notification_email, so it is never called."""
        mailer = RecordingMailer()
        config = _config(
            {"number": "1001", "email": "u@corp.local", "voicemail_email_enabled": False}
        )
        box = VoicemailBox("1001", storage, config=config, mailer=mailer)

        box.save_message("5551234567", b"RIFF" + b"\x00" * 64, 5)

        assert mailer.calls == []


@pytest.mark.unit
class TestMigration:
    def test_revision_chain_is_correct(self):
        """004 must follow 003, or alembic will refuse the duplicate revision."""
        import importlib.util
        from pathlib import Path

        path = (
            Path(__file__).resolve().parent.parent
            / "alembic"
            / "versions"
            / "004_add_voicemail_email_enabled.py"
        )
        spec = importlib.util.spec_from_file_location("migration_004", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        assert module.revision == "004"
        assert module.down_revision == "003"
        assert callable(module.upgrade)
        assert callable(module.downgrade)
