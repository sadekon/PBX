"""Comprehensive tests for pbx/features/voicemail.py"""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

import pytest


@pytest.fixture(autouse=True)
def _patch_loggers():
    """Patch loggers used by voicemail module."""
    with (
        patch("pbx.features.voicemail.get_logger") as mock_logger_fn,
        patch("pbx.features.voicemail.get_vm_ivr_logger") as mock_vm_logger_fn,
    ):
        mock_logger_fn.return_value = MagicMock()
        mock_vm_logger_fn.return_value = MagicMock()
        yield


@pytest.fixture
def tmp_storage(tmp_path):
    """Provide a temporary storage directory."""
    return str(tmp_path / "voicemail")


@pytest.fixture
def mock_config():
    """Provide a mock config object."""
    config = MagicMock()
    config.get_extension.return_value = {
        "voicemail_pin": "1234",
        "email": "test@example.com",
    }
    return config


@pytest.fixture
def mock_database():
    """Provide a mock database backend."""
    db = MagicMock()
    db.enabled = True
    db.db_type = "sqlite"
    db.execute.return_value = True
    db.fetch_all.return_value = []
    return db


@pytest.fixture
def mock_mailer():
    """Provide a mock email notifier."""
    notifier = MagicMock()
    return notifier


@pytest.fixture
def voicemail_box(tmp_storage, mock_config):
    """Provide a VoicemailBox instance with config-based PIN."""
    from pbx.features.voicemail import VoicemailBox

    return VoicemailBox(
        extension_number="1001",
        storage_path=tmp_storage,
        config=mock_config,
    )


@pytest.fixture
def voicemail_system(tmp_storage, mock_config):
    """Provide a VoicemailSystem instance."""
    from pbx.features.voicemail import VoicemailSystem

    return VoicemailSystem(
        storage_path=tmp_storage,
        config=mock_config,
    )


# =============================================================================
# VoicemailBox Tests
# =============================================================================


@pytest.mark.unit
class TestVoicemailBoxInit:
    """Tests for VoicemailBox initialization."""

    def test_init_basic(self, tmp_storage) -> None:
        from pbx.features.voicemail import VoicemailBox

        box = VoicemailBox(extension_number="1001", storage_path=tmp_storage)
        assert box.extension_number == "1001"
        assert box.messages == []
        assert box.pin is None
        assert box.pin_hash is None
        assert box.pin_salt is None

    def test_init_with_config_pin(self, tmp_storage, mock_config) -> None:
        from pbx.features.voicemail import VoicemailBox

        box = VoicemailBox(
            extension_number="1001",
            storage_path=tmp_storage,
            config=mock_config,
        )
        assert box.pin == "1234"

    def test_init_with_database_pin(self, tmp_storage, mock_database) -> None:
        from pbx.features.voicemail import VoicemailBox

        with (
            patch("pbx.features.voicemail.EXTENSIONDB_AVAILABLE", True),
            patch("pbx.features.voicemail.ExtensionDB") as mock_ext_db_cls,
        ):
            mock_ext_db = MagicMock()
            mock_ext_db.get.return_value = {
                "voicemail_pin_hash": "somehash",
                "voicemail_pin_salt": "somesalt",
            }
            mock_ext_db_cls.return_value = mock_ext_db

            box = VoicemailBox(
                extension_number="1001",
                storage_path=tmp_storage,
                database=mock_database,
            )
            assert box.pin_hash == "somehash"
            assert box.pin_salt == "somesalt"

    def test_init_db_pin_load_error(self, tmp_storage, mock_database) -> None:
        from pbx.features.voicemail import VoicemailBox

        with (
            patch("pbx.features.voicemail.EXTENSIONDB_AVAILABLE", True),
            patch("pbx.features.voicemail.ExtensionDB") as mock_ext_db_cls,
        ):
            mock_ext_db_cls.side_effect = KeyError("test error")

            box = VoicemailBox(
                extension_number="1001",
                storage_path=tmp_storage,
                database=mock_database,
            )
            assert box.pin_hash is None

    def test_init_db_extension_not_found(self, tmp_storage, mock_database) -> None:
        from pbx.features.voicemail import VoicemailBox

        with (
            patch("pbx.features.voicemail.EXTENSIONDB_AVAILABLE", True),
            patch("pbx.features.voicemail.ExtensionDB") as mock_ext_db_cls,
        ):
            mock_ext_db = MagicMock()
            mock_ext_db.get.return_value = None
            mock_ext_db_cls.return_value = mock_ext_db

            box = VoicemailBox(
                extension_number="1001",
                storage_path=tmp_storage,
                database=mock_database,
            )
            assert box.pin_hash is None

    def test_init_config_no_extension(self, tmp_storage) -> None:
        from pbx.features.voicemail import VoicemailBox

        config = MagicMock()
        config.get_extension.return_value = None

        box = VoicemailBox(
            extension_number="1001",
            storage_path=tmp_storage,
            config=config,
        )
        assert box.pin is None

    def test_init_creates_storage_directory(self, tmp_storage) -> None:
        from pbx.features.voicemail import VoicemailBox

        box = VoicemailBox(extension_number="1001", storage_path=tmp_storage)
        assert Path(box.storage_path).exists()

    def test_greeting_path_set_on_init(self, tmp_storage) -> None:
        from pbx.features.voicemail import VoicemailBox

        box = VoicemailBox(extension_number="1001", storage_path=tmp_storage)
        assert box.greeting_path == Path(tmp_storage) / "1001" / "greeting.wav"

    def test_init_loads_existing_messages_from_disk(self, tmp_storage) -> None:
        from pbx.features.voicemail import VoicemailBox

        # Create directory structure manually
        ext_dir = Path(tmp_storage) / "1001"
        ext_dir.mkdir(parents=True, exist_ok=True)

        # Create a valid wav file
        wav_file = ext_dir / "5551234_20250101_120000.wav"
        wav_file.write_bytes(b"fake audio data")

        box = VoicemailBox(extension_number="1001", storage_path=tmp_storage)
        assert len(box.messages) == 1
        assert box.messages[0]["caller_id"] == "5551234"

    def test_init_loads_messages_from_database(self, tmp_storage, mock_database) -> None:
        from pbx.features.voicemail import VoicemailBox

        mock_database.fetch_all.return_value = [
            {
                "message_id": "caller1_20250101_120000",
                "caller_id": "caller1",
                "file_path": "/some/path.wav",
                "duration": 30.0,
                "listened": False,
                "created_at": "2025-01-01T12:00:00",
                "transcription_text": None,
            }
        ]

        box = VoicemailBox(
            extension_number="1001",
            storage_path=tmp_storage,
            database=mock_database,
        )
        assert len(box.messages) == 1
        assert box.messages[0]["id"] == "caller1_20250101_120000"

    def test_init_loads_messages_from_db_with_transcription(
        self, tmp_storage, mock_database
    ) -> None:
        from pbx.features.voicemail import VoicemailBox

        mock_database.fetch_all.return_value = [
            {
                "message_id": "caller1_20250101_120000",
                "caller_id": "caller1",
                "file_path": "/some/path.wav",
                "duration": 30.0,
                "listened": True,
                "created_at": datetime.now(UTC),
                "text": "Hello world",
                "confidence": 0.95,
                "language": "en",
                "provider": "whisper",
                "transcribed_at": "2025-01-01T12:00:05",
            }
        ]

        box = VoicemailBox(
            extension_number="1001",
            storage_path=tmp_storage,
            database=mock_database,
        )
        assert len(box.messages) == 1
        assert box.messages[0]["transcription"] == "Hello world"
        assert box.messages[0]["listened"] is True

    def test_init_db_load_error_falls_back_to_disk(self, tmp_storage, mock_database) -> None:
        from pbx.features.voicemail import VoicemailBox

        mock_database.fetch_all.side_effect = Exception("db error")

        ext_dir = Path(tmp_storage) / "1001"
        ext_dir.mkdir(parents=True, exist_ok=True)
        wav_file = ext_dir / "5551234_20250101_120000.wav"
        wav_file.write_bytes(b"fake audio data")

        box = VoicemailBox(
            extension_number="1001",
            storage_path=tmp_storage,
            database=mock_database,
        )
        assert len(box.messages) == 1

    def test_load_messages_invalid_timestamp_in_filename(self, tmp_storage) -> None:
        from pbx.features.voicemail import VoicemailBox

        ext_dir = Path(tmp_storage) / "1001"
        ext_dir.mkdir(parents=True, exist_ok=True)
        wav_file = ext_dir / "caller_BADDATE_BADTIME.wav"
        wav_file.write_bytes(b"fake audio data")

        box = VoicemailBox(extension_number="1001", storage_path=tmp_storage)
        assert len(box.messages) == 0

    def test_load_messages_short_filename_ignored(self, tmp_storage) -> None:
        from pbx.features.voicemail import VoicemailBox

        ext_dir = Path(tmp_storage) / "1001"
        ext_dir.mkdir(parents=True, exist_ok=True)
        wav_file = ext_dir / "short_name.wav"
        wav_file.write_bytes(b"fake audio data")

        box = VoicemailBox(extension_number="1001", storage_path=tmp_storage)
        # only 2 parts (short, name) which is less than 3
        assert len(box.messages) == 0

    def test_load_messages_db_string_timestamp_parse_error(
        self, tmp_storage, mock_database
    ) -> None:
        from pbx.features.voicemail import VoicemailBox

        mock_database.fetch_all.return_value = [
            {
                "message_id": "m1",
                "caller_id": "c1",
                "file_path": "/p.wav",
                "duration": 10.0,
                "listened": False,
                "created_at": "INVALID_TIMESTAMP",
                "transcription_text": None,
            }
        ]

        box = VoicemailBox(
            extension_number="1001",
            storage_path=tmp_storage,
            database=mock_database,
        )
        # Should still load the message, but with a fallback timestamp
        assert len(box.messages) == 1


@pytest.mark.unit
class TestVoicemailBoxGetDbPlaceholder:
    """Tests for _get_db_placeholder."""

    def test_postgresql_placeholder(self, voicemail_box) -> None:
        db = MagicMock()
        db.db_type = "postgresql"
        voicemail_box.database = db
        assert voicemail_box._get_db_placeholder() == "%s"

    def test_sqlite_placeholder(self, voicemail_box) -> None:
        db = MagicMock()
        db.db_type = "sqlite"
        voicemail_box.database = db
        assert voicemail_box._get_db_placeholder() == "%s"

    def test_no_database_placeholder(self, voicemail_box) -> None:
        voicemail_box.database = None
        assert voicemail_box._get_db_placeholder() == "%s"


@pytest.mark.unit
class TestVoicemailBoxSaveMessage:
    """Tests for VoicemailBox.save_message."""

    def test_save_message_basic(self, voicemail_box) -> None:
        msg_id = voicemail_box.save_message("5551234", b"audio data", duration=10.0)
        assert msg_id.startswith("5551234_")
        assert len(voicemail_box.messages) == 1
        assert voicemail_box.messages[0]["caller_id"] == "5551234"
        assert voicemail_box.messages[0]["duration"] == 10.0
        assert voicemail_box.messages[0]["listened"] is False

    def test_save_message_creates_wav_file(self, voicemail_box) -> None:
        _msg_id = voicemail_box.save_message("5551234", b"audio data")
        file_path = voicemail_box.messages[0]["file_path"]
        assert Path(file_path).exists()
        assert Path(file_path).read_bytes() == b"audio data"

    def test_save_message_with_database(self, voicemail_box, mock_database) -> None:
        voicemail_box.database = mock_database
        _msg_id = voicemail_box.save_message("5551234", b"audio data", duration=5.0)
        mock_database.execute.assert_called()

    def test_save_message_database_execute_fails(self, voicemail_box, mock_database) -> None:
        voicemail_box.database = mock_database
        mock_database.execute.return_value = False
        msg_id = voicemail_box.save_message("5551234", b"audio data")
        assert msg_id is not None  # Still returns an ID even if DB fails

    def test_save_message_database_exception(self, voicemail_box, mock_database) -> None:
        voicemail_box.database = mock_database
        mock_database.execute.side_effect = Exception("db error")
        msg_id = voicemail_box.save_message("5551234", b"audio data")
        assert msg_id is not None

    def test_save_message_no_database(self, voicemail_box) -> None:
        voicemail_box.database = None
        msg_id = voicemail_box.save_message("5551234", b"audio data")
        assert msg_id is not None

    @staticmethod
    def _inline_worker(text: str = "Hello this is a test message", confidence: float = 0.95):
        """
        A TranscriptionWorker stand-in that runs the job on the calling thread.

        Mirrors the real contract: submit() returns True and then calls the callback exactly
        once. Transcription is no longer awaited inside save_message, so a stub that only
        offers transcribe() would never be reached.
        """
        from pbx.speech import Transcript

        worker = MagicMock()
        worker.settings.deadline_seconds = 30.0

        def submit(path, on_complete, **kwargs) -> bool:
            on_complete(
                Transcript(text=text, confidence=confidence, language="en", provider="vosk")
            )
            return True

        worker.submit.side_effect = submit
        return worker

    def test_save_message_with_transcription_success(self, voicemail_box, mock_mailer) -> None:
        # A mailer is required: transcription is submitted for the notification's benefit, so
        # a box with nowhere to send does not spend CPU transcribing.
        voicemail_box.mailer = mock_mailer
        voicemail_box.transcription_service = self._inline_worker()
        _msg_id = voicemail_box.save_message("5551234", b"audio data")
        msg = voicemail_box.messages[0]
        assert msg["transcription"] == "Hello this is a test message"
        assert msg["transcription_confidence"] == 0.95

    def test_save_message_with_transcription_db_save(
        self, voicemail_box, mock_database, mock_mailer
    ) -> None:
        voicemail_box.database = mock_database
        voicemail_box.mailer = mock_mailer
        voicemail_box.transcription_service = self._inline_worker(text="Hello", confidence=0.9)
        voicemail_box.save_message("5551234", b"audio data")

        # The mailbox row goes through execute(); the recording and the transcript use
        # fetch_one, because both need the id RETURNING gives back. What matters is that the
        # transcript is written exactly once -- it used to be written twice, and only one of
        # the two copies was ever swept.
        statements = " ".join(
            str(call[0][0])
            for call in (
                *mock_database.execute.call_args_list,
                *mock_database.fetch_one.call_args_list,
            )
        )
        assert "INSERT INTO voicemail_messages" in statements
        assert "INSERT INTO recordings" in statements
        assert statements.count("INSERT INTO transcripts") == 1

    def test_save_message_without_a_mailer_skips_transcription(self, voicemail_box) -> None:
        """No mailer means no notification, so there is nothing a transcript would serve."""
        worker = self._inline_worker()
        voicemail_box.mailer = None
        voicemail_box.transcription_service = worker

        voicemail_box.save_message("5551234", b"audio data")

        worker.submit.assert_not_called()
        assert "transcription" not in voicemail_box.messages[0]

    def test_save_message_transcription_db_error(self, voicemail_box, mock_database) -> None:
        voicemail_box.database = mock_database
        mock_database.execute.side_effect = [True, Exception("db error")]
        transcription_svc = MagicMock()
        transcription_svc.transcribe.return_value = {
            "success": True,
            "text": "Hello",
            "confidence": 0.9,
            "language": "en",
            "provider": "whisper",
            "timestamp": "2025-01-01T12:00:00",
        }
        voicemail_box.transcription_service = transcription_svc
        msg_id = voicemail_box.save_message("5551234", b"audio data")
        assert msg_id is not None

    def test_save_message_with_transcription_failure(self, voicemail_box) -> None:
        transcription_svc = MagicMock()
        transcription_svc.transcribe.return_value = {
            "success": False,
            "error": "Service unavailable",
        }
        voicemail_box.transcription_service = transcription_svc
        _msg_id = voicemail_box.save_message("5551234", b"audio data")
        assert "transcription" not in voicemail_box.messages[0]

    def test_save_message_with_email_notification(
        self, voicemail_box, mock_config, mock_mailer
    ) -> None:
        """Notification is queued, not sent inline, so call teardown never waits on SMTP."""
        voicemail_box.config = mock_config
        voicemail_box.mailer = mock_mailer

        voicemail_box.save_message("5551234", b"audio data", duration=10.0)

        mock_mailer.send_async.assert_called_once()
        mock_mailer.send.assert_not_called()

    def test_save_message_notification_carries_subject_and_body(
        self, voicemail_box, mock_config, mock_mailer
    ) -> None:
        voicemail_box.config = mock_config
        voicemail_box.mailer = mock_mailer

        voicemail_box.save_message("5551234", b"audio data", duration=10.0)

        args, kwargs = mock_mailer.send_async.call_args
        assert "5551234" in args[2]  # body names the caller
        assert "1001" in args[2]  # ...and the extension
        assert "attachments" in kwargs

    def test_save_message_email_from_database(
        self, voicemail_box, mock_config, mock_database, mock_mailer
    ) -> None:
        voicemail_box.config = mock_config
        voicemail_box.mailer = mock_mailer
        voicemail_box.database = mock_database

        with patch("pbx.utils.database.ExtensionDB") as mock_ext_db_cls:
            mock_ext_db = MagicMock()
            mock_ext_db.get.return_value = {"email": "db@example.com"}
            mock_ext_db_cls.return_value = mock_ext_db

            voicemail_box.save_message("5551234", b"audio data")

            mock_mailer.send_async.assert_called_once()
            assert mock_mailer.send_async.call_args[0][0] == "db@example.com"

    def test_save_message_email_db_lookup_error_falls_back(
        self, voicemail_box, mock_config, mock_database, mock_mailer
    ) -> None:
        voicemail_box.config = mock_config
        voicemail_box.mailer = mock_mailer
        voicemail_box.database = mock_database

        with patch("pbx.utils.database.ExtensionDB") as mock_ext_db_cls:
            mock_ext_db_cls.side_effect = KeyError("db error")

            voicemail_box.save_message("5551234", b"audio data")

            # Falls back to config, which has email "test@example.com"
            mock_mailer.send_async.assert_called_once()

    def test_save_message_no_email_address(self, voicemail_box, mock_mailer) -> None:
        config = MagicMock()
        config.get_extension.return_value = {"voicemail_pin": "1234"}
        voicemail_box.config = config
        voicemail_box.mailer = mock_mailer
        voicemail_box.database = None

        voicemail_box.save_message("5551234", b"audio data")
        # No email address in config, so notification should NOT be sent
        mock_mailer.send_voicemail_notification.assert_not_called()


@pytest.mark.unit
class TestVoicemailBoxGetMessages:
    """Tests for VoicemailBox.get_messages."""

    def test_get_all_messages(self, voicemail_box) -> None:
        voicemail_box.save_message("caller1", b"data1")
        voicemail_box.save_message("caller2", b"data2")
        msgs = voicemail_box.get_messages()
        assert len(msgs) == 2

    def test_get_unread_only(self, voicemail_box) -> None:
        voicemail_box.save_message("caller1", b"data1")
        voicemail_box.save_message("caller2", b"data2")
        voicemail_box.messages[0]["listened"] = True
        msgs = voicemail_box.get_messages(unread_only=True)
        assert len(msgs) == 1
        assert msgs[0]["caller_id"] == "caller2"

    def test_get_messages_empty(self, voicemail_box) -> None:
        msgs = voicemail_box.get_messages()
        assert msgs == []


@pytest.mark.unit
class TestVoicemailBoxMarkListened:
    """Tests for VoicemailBox.mark_listened."""

    def test_mark_listened(self, voicemail_box) -> None:
        msg_id = voicemail_box.save_message("caller1", b"data1")
        voicemail_box.mark_listened(msg_id)
        assert voicemail_box.messages[0]["listened"] is True

    def test_mark_listened_with_database(self, voicemail_box, mock_database) -> None:
        voicemail_box.database = mock_database
        msg_id = voicemail_box.save_message("caller1", b"data1")
        mock_database.execute.reset_mock()  # reset from save_message calls
        voicemail_box.mark_listened(msg_id)
        mock_database.execute.assert_called_once()

    def test_mark_listened_db_error(self, voicemail_box, mock_database) -> None:
        voicemail_box.database = mock_database
        msg_id = voicemail_box.save_message("caller1", b"data1")
        mock_database.execute.side_effect = Exception("db error")
        voicemail_box.mark_listened(msg_id)
        assert voicemail_box.messages[0]["listened"] is True  # still marked in memory

    def test_mark_listened_nonexistent_message(self, voicemail_box) -> None:
        voicemail_box.mark_listened("nonexistent_id")
        # Should not raise

    def test_mark_listened_no_database(self, voicemail_box) -> None:
        voicemail_box.database = None
        msg_id = voicemail_box.save_message("caller1", b"data1")
        voicemail_box.mark_listened(msg_id)
        assert voicemail_box.messages[0]["listened"] is True


@pytest.mark.unit
class TestVoicemailBoxDeleteMessage:
    """Tests for VoicemailBox.delete_message."""

    def test_delete_message(self, voicemail_box) -> None:
        msg_id = voicemail_box.save_message("caller1", b"data1")
        result = voicemail_box.delete_message(msg_id)
        assert result is True
        assert len(voicemail_box.messages) == 0

    def test_delete_message_removes_file(self, voicemail_box) -> None:
        msg_id = voicemail_box.save_message("caller1", b"data1")
        file_path = voicemail_box.messages[0]["file_path"]
        assert Path(file_path).exists()
        voicemail_box.delete_message(msg_id)
        assert not Path(file_path).exists()

    def test_delete_message_with_database(self, voicemail_box, mock_database) -> None:
        voicemail_box.database = mock_database
        msg_id = voicemail_box.save_message("caller1", b"data1")
        mock_database.execute.reset_mock()
        voicemail_box.delete_message(msg_id)
        mock_database.execute.assert_called_once()

    def test_delete_message_tombstones_rather_than_deleting_the_row(
        self, voicemail_box, mock_database
    ) -> None:
        """
        The audio goes, the record stays.

        Deleting the row destroyed the transcript along with the recording, so clearing a
        mailbox erased the only remaining evidence of what was said. The row now survives with
        audio_deleted_at set, and _load_messages filters it out so the mailbox still looks empty.
        """
        voicemail_box.database = mock_database
        msg_id = voicemail_box.save_message("caller1", b"data1")
        mock_database.execute.reset_mock()

        voicemail_box.delete_message(msg_id)

        query = str(mock_database.execute.call_args[0][0])
        # Tombstoned on the recording, which is where the audio lives now. The mailbox row
        # and the transcript stay until retention expires them.
        assert "UPDATE recordings" in query
        assert "audio_deleted_at" in query
        assert "path = NULL" in query, "a row naming a missing file is the phantom bug"
        assert "audio_deleted_at" in query
        # "DELETE FROM", not "DELETE": audio_deleted_at contains the word.
        assert "DELETE FROM" not in query.upper()

    def test_delete_message_db_error(self, voicemail_box, mock_database) -> None:
        voicemail_box.database = mock_database
        msg_id = voicemail_box.save_message("caller1", b"data1")
        mock_database.execute.side_effect = [True, Exception("db error")]
        result = voicemail_box.delete_message(msg_id)
        assert result is True  # still succeeds in memory

    def test_delete_nonexistent_message(self, voicemail_box) -> None:
        result = voicemail_box.delete_message("nonexistent_id")
        assert result is False

    def test_delete_message_no_database(self, voicemail_box) -> None:
        voicemail_box.database = None
        msg_id = voicemail_box.save_message("caller1", b"data1")
        result = voicemail_box.delete_message(msg_id)
        assert result is True

    def test_delete_message_file_already_gone(self, voicemail_box) -> None:
        msg_id = voicemail_box.save_message("caller1", b"data1")
        # Delete file manually first
        Path(voicemail_box.messages[0]["file_path"]).unlink()
        result = voicemail_box.delete_message(msg_id)
        assert result is True


@pytest.mark.unit
class TestVoicemailBoxSetPin:
    """Tests for VoicemailBox.set_pin."""

    def test_set_valid_pin(self, voicemail_box) -> None:
        assert voicemail_box.set_pin("5678") is True
        assert voicemail_box.pin == "5678"

    def test_set_pin_as_int(self, voicemail_box) -> None:
        # pin can be passed as int
        assert voicemail_box.set_pin("9999") is True
        assert voicemail_box.pin == "9999"

    def test_set_invalid_pin_too_short(self, voicemail_box) -> None:
        assert voicemail_box.set_pin("12") is False

    def test_set_invalid_pin_too_long(self, voicemail_box) -> None:
        assert voicemail_box.set_pin("12345") is False

    def test_set_invalid_pin_non_digit(self, voicemail_box) -> None:
        assert voicemail_box.set_pin("abcd") is False

    def test_set_empty_pin(self, voicemail_box) -> None:
        assert voicemail_box.set_pin("") is False

    def test_set_none_pin(self, voicemail_box) -> None:
        assert voicemail_box.set_pin(None) is False


@pytest.mark.unit
class TestVoicemailBoxVerifyPin:
    """Tests for VoicemailBox.verify_pin."""

    def test_verify_correct_plaintext_pin(self, voicemail_box) -> None:
        voicemail_box.pin = "1234"
        assert voicemail_box.verify_pin("1234") is True

    def test_verify_wrong_plaintext_pin(self, voicemail_box) -> None:
        voicemail_box.pin = "1234"
        assert voicemail_box.verify_pin("9999") is False

    def test_verify_hashed_pin(self, voicemail_box) -> None:
        voicemail_box.pin_hash = "some_hash"
        voicemail_box.pin_salt = "some_salt"

        with (
            patch("pbx.features.voicemail.ENCRYPTION_AVAILABLE", True),
            patch("pbx.features.voicemail.get_encryption") as mock_enc_fn,
        ):
            mock_enc = MagicMock()
            mock_enc.verify_password.return_value = True
            mock_enc_fn.return_value = mock_enc

            assert voicemail_box.verify_pin("1234") is True
            mock_enc.verify_password.assert_called_once_with("1234", "some_hash", "some_salt")

    def test_verify_hashed_pin_failure(self, voicemail_box) -> None:
        voicemail_box.pin_hash = "some_hash"
        voicemail_box.pin_salt = "some_salt"

        with (
            patch("pbx.features.voicemail.ENCRYPTION_AVAILABLE", True),
            patch("pbx.features.voicemail.get_encryption") as mock_enc_fn,
        ):
            mock_enc = MagicMock()
            mock_enc.verify_password.return_value = False
            mock_enc_fn.return_value = mock_enc

            assert voicemail_box.verify_pin("9999") is False

    def test_verify_hashed_pin_error(self, voicemail_box) -> None:
        voicemail_box.pin_hash = "some_hash"
        voicemail_box.pin_salt = "some_salt"

        with (
            patch("pbx.features.voicemail.ENCRYPTION_AVAILABLE", True),
            patch("pbx.features.voicemail.get_encryption") as mock_enc_fn,
        ):
            mock_enc_fn.side_effect = KeyError("crypto error")

            assert voicemail_box.verify_pin("1234") is False

    def test_verify_no_pin_configured(self, tmp_storage) -> None:
        from pbx.features.voicemail import VoicemailBox

        box = VoicemailBox(extension_number="1001", storage_path=tmp_storage)
        assert box.verify_pin("1234") is False


@pytest.mark.unit
class TestVoicemailBoxGreeting:
    """Tests for greeting management."""

    def test_has_custom_greeting_false(self, voicemail_box) -> None:
        assert voicemail_box.has_custom_greeting() is False

    def test_has_custom_greeting_true(self, voicemail_box) -> None:
        voicemail_box.greeting_path.parent.mkdir(parents=True, exist_ok=True)
        voicemail_box.greeting_path.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfake")
        assert voicemail_box.has_custom_greeting() is True

    def test_save_greeting(self, voicemail_box) -> None:
        audio_data = b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 100
        result = voicemail_box.save_greeting(audio_data)
        assert result is True
        assert voicemail_box.greeting_path.exists()

    def test_save_greeting_empty_data(self, voicemail_box) -> None:
        result = voicemail_box.save_greeting(b"")
        assert result is False

    def test_save_greeting_invalid_wav_header(self, voicemail_box) -> None:
        # non-wav data larger than MIN_WAV_HEADER_SIZE
        result = voicemail_box.save_greeting(b"NOT_RIFF_DATA_PADDING_TO_MAKE_LONG_ENOUGH")
        # Should still save (just warns)
        assert result is True

    def test_save_greeting_small_data(self, voicemail_box) -> None:
        # Data smaller than MIN_WAV_HEADER_SIZE - no header check
        result = voicemail_box.save_greeting(b"tiny")
        assert result is True

    def test_save_greeting_os_error(self, voicemail_box) -> None:
        with patch.object(Path, "open", side_effect=OSError("disk full")):
            result = voicemail_box.save_greeting(b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 100)
            assert result is False

    def test_get_greeting_path_exists(self, voicemail_box) -> None:
        voicemail_box.greeting_path.parent.mkdir(parents=True, exist_ok=True)
        voicemail_box.greeting_path.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfake")
        path = voicemail_box.get_greeting_path()
        assert path == voicemail_box.greeting_path

    def test_get_greeting_path_not_exists(self, voicemail_box) -> None:
        path = voicemail_box.get_greeting_path()
        assert path is None

    def test_delete_greeting(self, voicemail_box) -> None:
        voicemail_box.greeting_path.parent.mkdir(parents=True, exist_ok=True)
        voicemail_box.greeting_path.write_bytes(b"greeting data")
        result = voicemail_box.delete_greeting()
        assert result is True
        assert not voicemail_box.greeting_path.exists()

    def test_delete_greeting_not_exists(self, voicemail_box) -> None:
        result = voicemail_box.delete_greeting()
        assert result is False

    def test_delete_greeting_os_error(self, voicemail_box) -> None:
        voicemail_box.greeting_path.parent.mkdir(parents=True, exist_ok=True)
        voicemail_box.greeting_path.write_bytes(b"greeting data")
        with patch.object(Path, "unlink", side_effect=OSError("permission denied")):
            result = voicemail_box.delete_greeting()
            assert result is False


# =============================================================================
# VoicemailSystem Tests
# =============================================================================


@pytest.mark.unit
class TestVoicemailSystemInit:
    """Tests for VoicemailSystem initialization."""

    def test_init_basic(self, tmp_storage) -> None:
        from pbx.features.voicemail import VoicemailSystem

        system = VoicemailSystem(storage_path=tmp_storage)
        assert system.mailboxes == {}
        assert system.mailer is None

    def test_init_with_mailer(self, tmp_storage, mock_config) -> None:
        """The mailer is injected by PBXCore now; voicemail never constructs a transport."""
        from pbx.features.voicemail import VoicemailSystem

        mailer = MagicMock()
        system = VoicemailSystem(storage_path=tmp_storage, config=mock_config, mailer=mailer)
        assert system.mailer is mailer

    def test_mailer_is_passed_down_to_each_mailbox(self, tmp_storage, mock_config) -> None:
        from pbx.features.voicemail import VoicemailSystem

        mailer = MagicMock()
        system = VoicemailSystem(storage_path=tmp_storage, config=mock_config, mailer=mailer)
        assert system.get_mailbox("1001").mailer is mailer

    def test_init_creates_storage_directory(self, tmp_storage) -> None:
        from pbx.features.voicemail import VoicemailSystem

        _system = VoicemailSystem(storage_path=tmp_storage)
        assert Path(tmp_storage).exists()


@pytest.mark.unit
class TestVoicemailSystemGetMailbox:
    """Tests for VoicemailSystem.get_mailbox."""

    def test_get_mailbox_creates_new(self, voicemail_system) -> None:
        mailbox = voicemail_system.get_mailbox("1001")
        assert mailbox.extension_number == "1001"
        assert "1001" in voicemail_system.mailboxes

    def test_get_mailbox_returns_existing(self, voicemail_system) -> None:
        mb1 = voicemail_system.get_mailbox("1001")
        mb2 = voicemail_system.get_mailbox("1001")
        assert mb1 is mb2

    def test_get_different_mailboxes(self, voicemail_system) -> None:
        mb1 = voicemail_system.get_mailbox("1001")
        mb2 = voicemail_system.get_mailbox("1002")
        assert mb1 is not mb2


@pytest.mark.unit
class TestVoicemailSystemSaveMessage:
    """Tests for VoicemailSystem.save_message."""

    def test_save_message(self, voicemail_system) -> None:
        msg_id = voicemail_system.save_message("1001", "caller1", b"audio", duration=5.0)
        assert msg_id is not None
        mailbox = voicemail_system.get_mailbox("1001")
        assert len(mailbox.messages) == 1


@pytest.mark.unit
class TestVoicemailSystemGetMessageCount:
    """Tests for VoicemailSystem.get_message_count."""

    def test_get_message_count_unread(self, voicemail_system) -> None:
        voicemail_system.save_message("1001", "caller1", b"audio1")
        voicemail_system.save_message("1001", "caller2", b"audio2")
        count = voicemail_system.get_message_count("1001", unread_only=True)
        assert count == 2

    def test_get_message_count_all(self, voicemail_system) -> None:
        voicemail_system.save_message("1001", "caller1", b"audio1")
        voicemail_system.save_message("1001", "caller2", b"audio2")
        mailbox = voicemail_system.get_mailbox("1001")
        mailbox.messages[0]["listened"] = True
        count = voicemail_system.get_message_count("1001", unread_only=False)
        assert count == 2

    def test_get_message_count_empty(self, voicemail_system) -> None:
        count = voicemail_system.get_message_count("1001")
        assert count == 0


@pytest.mark.unit
class TestVoicemailSystemDailyReminders:
    """Tests for VoicemailSystem.send_daily_reminders."""

    def test_send_daily_reminders_no_notifier(self, voicemail_system) -> None:
        voicemail_system.mailer = None
        count = voicemail_system.send_daily_reminders()
        assert count == 0

    def test_send_daily_reminders_no_config(self, voicemail_system) -> None:
        voicemail_system.config = None
        count = voicemail_system.send_daily_reminders()
        assert count == 0

    def test_send_daily_reminders_with_unread(self, voicemail_system, mock_config) -> None:
        voicemail_system.mailer = MagicMock()
        voicemail_system.config = mock_config

        voicemail_system.save_message("1001", "caller1", b"audio1")
        voicemail_system.mailer.send_async.reset_mock()

        count = voicemail_system.send_daily_reminders()
        assert count == 1
        voicemail_system.mailer.send_async.assert_called_once()

    def test_send_daily_reminders_no_unread(self, voicemail_system, mock_config) -> None:
        voicemail_system.mailer = MagicMock()
        voicemail_system.config = mock_config

        voicemail_system.save_message("1001", "caller1", b"audio1")
        mb = voicemail_system.get_mailbox("1001")
        mb.messages[0]["listened"] = True

        count = voicemail_system.send_daily_reminders()
        assert count == 0

    def test_send_daily_reminders_no_email_in_config(self, voicemail_system) -> None:
        voicemail_system.mailer = MagicMock()
        config = MagicMock()
        config.get_extension.return_value = {}  # no email key
        voicemail_system.config = config

        voicemail_system.save_message("1001", "caller1", b"audio1")
        count = voicemail_system.send_daily_reminders()
        assert count == 0


# =============================================================================
# VoicemailIVR Tests
# =============================================================================


@pytest.mark.unit
class TestVoicemailIVRInit:
    """Tests for VoicemailIVR initialization."""

    def test_init(self, voicemail_system) -> None:
        from pbx.features.voicemail import VoicemailIVR

        ivr = VoicemailIVR(voicemail_system, "1001")
        assert ivr.extension_number == "1001"
        assert ivr.state == "welcome"
        assert ivr.pin_attempts == 0
        assert ivr.entered_pin == ""
        assert ivr.current_message_index == 0
        assert ivr.recorded_greeting_data is None


@pytest.mark.unit
class TestVoicemailIVRWelcome:
    """Tests for IVR welcome state."""

    def test_welcome_digit_transitions_to_pin_entry(self, voicemail_system) -> None:
        from pbx.features.voicemail import VoicemailIVR

        ivr = VoicemailIVR(voicemail_system, "1001")
        result = ivr.handle_dtmf("1")
        assert ivr.state == "pin_entry"
        assert result["action"] == "collect_digit"

    def test_welcome_star_transitions_to_pin_prompt(self, voicemail_system) -> None:
        from pbx.features.voicemail import VoicemailIVR

        ivr = VoicemailIVR(voicemail_system, "1001")
        result = ivr.handle_dtmf("*")
        assert ivr.state == "pin_entry"
        assert result["prompt"] == "enter_pin"


@pytest.mark.unit
class TestVoicemailIVRPinEntry:
    """Tests for IVR PIN entry state."""

    def test_collect_pin_digits(self, voicemail_system) -> None:
        from pbx.features.voicemail import VoicemailIVR

        ivr = VoicemailIVR(voicemail_system, "1001")
        ivr.state = "pin_entry"
        result = ivr.handle_dtmf("1")
        assert result["action"] == "collect_digit"
        result = ivr.handle_dtmf("2")
        result = ivr.handle_dtmf("3")
        result = ivr.handle_dtmf("4")
        assert ivr.entered_pin == "1234"

    def test_pin_correct(self, voicemail_system) -> None:
        from pbx.features.voicemail import VoicemailIVR

        mb = voicemail_system.get_mailbox("1001")
        mb.pin = "1234"

        ivr = VoicemailIVR(voicemail_system, "1001")
        ivr.state = "pin_entry"
        for d in "1234":
            ivr.handle_dtmf(d)
        result = ivr.handle_dtmf("#")
        assert ivr.state == "main_menu"
        assert result["prompt"] == "main_menu"

    def test_pin_incorrect(self, voicemail_system) -> None:
        from pbx.features.voicemail import VoicemailIVR

        mb = voicemail_system.get_mailbox("1001")
        mb.pin = "1234"

        ivr = VoicemailIVR(voicemail_system, "1001")
        ivr.state = "pin_entry"
        for d in "9999":
            ivr.handle_dtmf(d)
        result = ivr.handle_dtmf("#")
        assert result["prompt"] == "invalid_pin"
        assert ivr.pin_attempts == 1

    def test_pin_max_attempts_hangup(self, voicemail_system) -> None:
        from pbx.features.voicemail import VoicemailIVR

        mb = voicemail_system.get_mailbox("1001")
        mb.pin = "1234"

        ivr = VoicemailIVR(voicemail_system, "1001")
        ivr.state = "pin_entry"

        for _ in range(3):
            for d in "9999":
                ivr.handle_dtmf(d)
            result = ivr.handle_dtmf("#")

        assert result["action"] == "hangup"
        assert ivr.state == "goodbye"

    def test_pin_entry_max_digits(self, voicemail_system) -> None:
        from pbx.features.voicemail import VoicemailIVR

        ivr = VoicemailIVR(voicemail_system, "1001")
        ivr.state = "pin_entry"
        # Enter more than 10 digits
        for _ in range(12):
            ivr.handle_dtmf("1")
        assert len(ivr.entered_pin) == 10  # capped at 10

    def test_pin_entry_invalid_digit_ignored(self, voicemail_system) -> None:
        from pbx.features.voicemail import VoicemailIVR

        ivr = VoicemailIVR(voicemail_system, "1001")
        ivr.state = "pin_entry"
        result = ivr.handle_dtmf("*")
        assert result["action"] == "collect_digit"
        assert ivr.entered_pin == ""


@pytest.mark.unit
class TestVoicemailIVRMainMenu:
    """Tests for IVR main menu state."""

    def _make_ivr_at_main_menu(self, voicemail_system):
        from pbx.features.voicemail import VoicemailIVR

        ivr = VoicemailIVR(voicemail_system, "1001")
        ivr.state = "main_menu"
        return ivr

    def test_press_1_listen_to_messages(self, voicemail_system) -> None:
        voicemail_system.save_message("1001", "caller1", b"audio")
        ivr = self._make_ivr_at_main_menu(voicemail_system)
        result = ivr.handle_dtmf("1")
        assert result["action"] == "play_message"
        assert ivr.state == "playing_message"

    def test_press_1_no_messages(self, voicemail_system) -> None:
        ivr = self._make_ivr_at_main_menu(voicemail_system)
        result = ivr.handle_dtmf("1")
        assert result["prompt"] == "no_messages"

    def test_press_1_no_unread_plays_all(self, voicemail_system) -> None:
        voicemail_system.save_message("1001", "caller1", b"audio")
        mb = voicemail_system.get_mailbox("1001")
        mb.messages[0]["listened"] = True

        ivr = self._make_ivr_at_main_menu(voicemail_system)
        result = ivr.handle_dtmf("1")
        assert result["action"] == "play_message"

    def test_press_2_options(self, voicemail_system) -> None:
        ivr = self._make_ivr_at_main_menu(voicemail_system)
        result = ivr.handle_dtmf("2")
        assert ivr.state == "options_menu"
        assert result["prompt"] == "options_menu"

    def test_press_star_exit(self, voicemail_system) -> None:
        ivr = self._make_ivr_at_main_menu(voicemail_system)
        result = ivr.handle_dtmf("*")
        assert result["action"] == "hangup"
        assert ivr.state == "goodbye"

    def test_invalid_option(self, voicemail_system) -> None:
        ivr = self._make_ivr_at_main_menu(voicemail_system)
        result = ivr.handle_dtmf("9")
        assert result["prompt"] == "invalid_option"


@pytest.mark.unit
class TestVoicemailIVRPlayingMessage:
    """Tests for IVR playing message state."""

    def test_playing_transitions_to_message_menu(self, voicemail_system) -> None:
        from pbx.features.voicemail import VoicemailIVR

        ivr = VoicemailIVR(voicemail_system, "1001")
        ivr.state = "playing_message"
        result = ivr.handle_dtmf("5")
        assert ivr.state == "message_menu"
        assert result["prompt"] == "message_menu"


@pytest.mark.unit
class TestVoicemailIVRMessageMenu:
    """Tests for IVR message menu state."""

    def _setup_ivr_with_messages(self, voicemail_system):
        from pbx.features.voicemail import VoicemailIVR

        voicemail_system.save_message("1001", "caller1", b"audio1")
        voicemail_system.save_message("1001", "caller2", b"audio2")

        ivr = VoicemailIVR(voicemail_system, "1001")
        ivr.state = "message_menu"
        ivr.current_messages = voicemail_system.get_mailbox("1001").get_messages()
        ivr.current_message_index = 0
        return ivr

    def test_press_1_replay(self, voicemail_system) -> None:
        ivr = self._setup_ivr_with_messages(voicemail_system)
        result = ivr.handle_dtmf("1")
        assert result["action"] == "play_message"
        assert ivr.state == "playing_message"

    def test_press_2_next_message(self, voicemail_system) -> None:
        ivr = self._setup_ivr_with_messages(voicemail_system)
        result = ivr.handle_dtmf("2")
        assert result["action"] == "play_message"
        assert ivr.current_message_index == 1

    def test_press_2_no_more_messages(self, voicemail_system) -> None:
        ivr = self._setup_ivr_with_messages(voicemail_system)
        ivr.current_message_index = 1
        result = ivr.handle_dtmf("2")
        assert result["prompt"] == "no_more_messages"
        assert ivr.state == "main_menu"

    def test_press_3_delete_with_more_messages(self, voicemail_system) -> None:
        from pbx.features.voicemail import VoicemailIVR

        # Need 4 messages so that after deleting index 0 (list shrinks to 3),
        # index increments to 1, and 1 < 2 is True -> plays next message
        voicemail_system.save_message("1001", "c1", b"a1")
        voicemail_system.save_message("1001", "c2", b"a2")
        voicemail_system.save_message("1001", "c3", b"a3")
        voicemail_system.save_message("1001", "c4", b"a4")

        ivr = VoicemailIVR(voicemail_system, "1001")
        ivr.state = "message_menu"
        ivr.current_messages = voicemail_system.get_mailbox("1001").get_messages()
        ivr.current_message_index = 0

        result = ivr.handle_dtmf("3")
        assert result["action"] == "play_message"

    def test_press_3_delete_last_message(self, voicemail_system) -> None:
        ivr = self._setup_ivr_with_messages(voicemail_system)
        ivr.current_message_index = 1
        result = ivr.handle_dtmf("3")
        assert result["prompt"] == "message_deleted"
        assert ivr.state == "main_menu"

    def test_press_star_return_to_main(self, voicemail_system) -> None:
        ivr = self._setup_ivr_with_messages(voicemail_system)
        result = ivr.handle_dtmf("*")
        assert ivr.state == "main_menu"
        assert result["prompt"] == "main_menu"

    def test_invalid_option(self, voicemail_system) -> None:
        ivr = self._setup_ivr_with_messages(voicemail_system)
        result = ivr.handle_dtmf("9")
        assert result["prompt"] == "invalid_option"


@pytest.mark.unit
class TestVoicemailIVROptionsMenu:
    """Tests for IVR options menu state."""

    def test_press_1_record_greeting(self, voicemail_system) -> None:
        from pbx.features.voicemail import VoicemailIVR

        ivr = VoicemailIVR(voicemail_system, "1001")
        ivr.state = "options_menu"
        result = ivr.handle_dtmf("1")
        assert ivr.state == "recording_greeting"
        assert result["action"] == "start_recording"

    def test_press_star_return_to_main(self, voicemail_system) -> None:
        from pbx.features.voicemail import VoicemailIVR

        ivr = VoicemailIVR(voicemail_system, "1001")
        ivr.state = "options_menu"
        _result = ivr.handle_dtmf("*")
        assert ivr.state == "main_menu"

    def test_invalid_option(self, voicemail_system) -> None:
        from pbx.features.voicemail import VoicemailIVR

        ivr = VoicemailIVR(voicemail_system, "1001")
        ivr.state = "options_menu"
        result = ivr.handle_dtmf("9")
        assert result["prompt"] == "invalid_option"


@pytest.mark.unit
class TestVoicemailIVRRecordingGreeting:
    """Tests for IVR recording greeting state."""

    def test_press_hash_stops_recording(self, voicemail_system) -> None:
        from pbx.features.voicemail import VoicemailIVR

        ivr = VoicemailIVR(voicemail_system, "1001")
        ivr.state = "recording_greeting"
        result = ivr.handle_dtmf("#")
        assert ivr.state == "greeting_review"
        assert result["action"] == "stop_recording"

    def test_other_digits_continue_recording(self, voicemail_system) -> None:
        from pbx.features.voicemail import VoicemailIVR

        ivr = VoicemailIVR(voicemail_system, "1001")
        ivr.state = "recording_greeting"
        result = ivr.handle_dtmf("5")
        assert result["action"] == "continue_recording"


@pytest.mark.unit
class TestVoicemailIVRGreetingReview:
    """Tests for IVR greeting review state."""

    def test_press_1_play_back(self, voicemail_system) -> None:
        from pbx.features.voicemail import VoicemailIVR

        ivr = VoicemailIVR(voicemail_system, "1001")
        ivr.state = "greeting_review"
        result = ivr.handle_dtmf("1")
        assert result["action"] == "play_greeting"

    def test_press_2_re_record(self, voicemail_system) -> None:
        from pbx.features.voicemail import VoicemailIVR

        ivr = VoicemailIVR(voicemail_system, "1001")
        ivr.state = "greeting_review"
        ivr.recorded_greeting_data = b"some data"
        result = ivr.handle_dtmf("2")
        assert ivr.state == "recording_greeting"
        assert ivr.recorded_greeting_data is None
        assert result["action"] == "start_recording"

    def test_press_3_delete_greeting(self, voicemail_system) -> None:
        from pbx.features.voicemail import VoicemailIVR

        ivr = VoicemailIVR(voicemail_system, "1001")
        ivr.state = "greeting_review"
        result = ivr.handle_dtmf("3")
        assert ivr.state == "main_menu"
        assert "greeting_deleted" in result["prompt"]

    def test_press_3_delete_existing_custom_greeting(self, voicemail_system) -> None:
        from pbx.features.voicemail import VoicemailIVR

        mb = voicemail_system.get_mailbox("1001")
        mb.greeting_path.parent.mkdir(parents=True, exist_ok=True)
        mb.greeting_path.write_bytes(b"greeting data")

        ivr = VoicemailIVR(voicemail_system, "1001")
        ivr.state = "greeting_review"
        _result = ivr.handle_dtmf("3")
        assert not mb.greeting_path.exists()

    def test_press_star_save_greeting(self, voicemail_system) -> None:
        from pbx.features.voicemail import VoicemailIVR

        ivr = VoicemailIVR(voicemail_system, "1001")
        ivr.state = "greeting_review"
        ivr.recorded_greeting_data = b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 100
        result = ivr.handle_dtmf("*")
        assert ivr.state == "main_menu"
        assert result["prompt"] == "greeting_saved"

    def test_press_star_save_greeting_failure(self, voicemail_system) -> None:
        from pbx.features.voicemail import VoicemailIVR

        ivr = VoicemailIVR(voicemail_system, "1001")
        ivr.state = "greeting_review"
        ivr.recorded_greeting_data = b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 100

        with patch.object(ivr.mailbox, "save_greeting", return_value=False):
            result = ivr.handle_dtmf("*")
            assert result["prompt"] == "error"

    def test_press_star_save_greeting_exception(self, voicemail_system) -> None:
        from pbx.features.voicemail import VoicemailIVR

        ivr = VoicemailIVR(voicemail_system, "1001")
        ivr.state = "greeting_review"
        ivr.recorded_greeting_data = b"some data"

        with patch.object(ivr.mailbox, "save_greeting", side_effect=Exception("io error")):
            result = ivr.handle_dtmf("*")
            assert result["prompt"] == "error"

    def test_press_star_no_recorded_data(self, voicemail_system) -> None:
        from pbx.features.voicemail import VoicemailIVR

        ivr = VoicemailIVR(voicemail_system, "1001")
        ivr.state = "greeting_review"
        ivr.recorded_greeting_data = None
        _result = ivr.handle_dtmf("*")
        assert ivr.state == "main_menu"

    def test_invalid_option(self, voicemail_system) -> None:
        from pbx.features.voicemail import VoicemailIVR

        ivr = VoicemailIVR(voicemail_system, "1001")
        ivr.state = "greeting_review"
        result = ivr.handle_dtmf("9")
        assert result["prompt"] == "invalid_option"


@pytest.mark.unit
class TestVoicemailIVRHelperMethods:
    """Tests for IVR helper methods."""

    def test_save_recorded_greeting(self, voicemail_system) -> None:
        from pbx.features.voicemail import VoicemailIVR

        ivr = VoicemailIVR(voicemail_system, "1001")
        result = ivr.save_recorded_greeting(b"audio data")
        assert result is True
        assert ivr.recorded_greeting_data == b"audio data"

    def test_get_recorded_greeting(self, voicemail_system) -> None:
        from pbx.features.voicemail import VoicemailIVR

        ivr = VoicemailIVR(voicemail_system, "1001")
        assert ivr.get_recorded_greeting() is None
        ivr.recorded_greeting_data = b"audio data"
        assert ivr.get_recorded_greeting() == b"audio data"

    def test_handle_dtmf_unknown_state(self, voicemail_system) -> None:
        from pbx.features.voicemail import VoicemailIVR

        ivr = VoicemailIVR(voicemail_system, "1001")
        ivr.state = "some_unknown_state"
        result = ivr.handle_dtmf("1")
        assert result["action"] == "unknown_state"
