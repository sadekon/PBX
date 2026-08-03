"""
Tests for voicemail transcription functionality
"""

import struct
import sys
import tempfile
import wave
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, Mock, patch

from pbx.features.voicemail_transcription import VoicemailTranscriptionService

# Mock the optional dependencies before importing the module
sys.modules["vosk"] = MagicMock()
sys.modules["google"] = MagicMock()
sys.modules["google.cloud"] = MagicMock()
sys.modules["google.cloud.speech"] = MagicMock()


def _config(**settings: Any) -> Mock:
    """
    Build a Config stub answering the dotted lookups the service actually performs.

    Keys are passed without the ``features.voicemail_transcription.`` prefix, since that is the
    only namespace this service reads. Anything not supplied falls through to the caller's
    default, exactly as the real Config does -- which is what makes these tests notice if a
    default changes.
    """
    values = {f"features.voicemail_transcription.{key}": v for key, v in settings.items()}
    config = Mock()
    config.get = Mock(side_effect=lambda key, default=None: values.get(key, default))
    return config


class TestVoicemailTranscription:
    """Test voicemail transcription service"""

    def setup_method(self) -> None:
        """Set up test fixtures"""
        self.temp_dir = tempfile.mkdtemp()

        # Create test audio file (simple WAV)
        self.test_audio_path = Path(self.temp_dir) / "test_voicemail.wav"
        self._create_test_wav(self.test_audio_path)

    def teardown_method(self) -> None:
        """Clean up test fixtures"""
        # Remove test files
        if Path(self.test_audio_path).exists():
            Path(self.test_audio_path).unlink(missing_ok=True)
        Path(self.temp_dir).rmdir()

    def _create_test_wav(
        self, filepath: str, duration: float = 1.0, sample_rate: int = 8000
    ) -> None:
        """
        Create a test WAV file with a simple varying amplitude wave

        Args:
            filepath: Path to save WAV file
            duration: Duration in seconds
            sample_rate: Sample rate in Hz
        """
        num_samples = int(duration * sample_rate)

        # Generate samples with varying amplitude
        samples = []
        for i in range(num_samples):
            # Simple varying amplitude
            value = int(32767.0 * 0.5 * (1 + (i % 100) / 100))
            samples.append(struct.pack("<h", value))

        # Write WAV file (wave.open needs a str path, not a Path object)
        with wave.open(str(filepath), "w") as wav_file:
            wav_file.setnchannels(1)  # Mono
            wav_file.setsampwidth(2)  # 16-bit
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(b"".join(samples))

    def test_transcription_service_disabled(self) -> None:
        """Test transcription service when disabled"""
        config = _config(enabled=False)

        service = VoicemailTranscriptionService(config)

        assert not service.enabled
        assert not service.ready
        result = service.transcribe(self.test_audio_path)

        assert not result["success"]
        assert result["text"] is None
        assert result["error"] == "Transcription service is disabled"

    def test_transcription_file_not_found(self) -> None:
        """Test transcription with non-existent file"""
        config = _config(enabled=True, provider="vosk", vosk_model_path="models/test")

        service = VoicemailTranscriptionService(config)

        result = service.transcribe("/nonexistent/file.wav")

        assert not result["success"]
        assert "Audio file not found" in result["error"]

    def test_transcription_unsupported_provider(self) -> None:
        """Test transcription with unsupported provider"""
        config = _config(enabled=True, provider="unsupported_provider")

        service = VoicemailTranscriptionService(config)
        result = service.transcribe(self.test_audio_path)

        assert not result["success"]
        assert "Unsupported transcription provider" in result["error"]
        # An unrecognised provider has no backend, so the service is never ready even though
        # the operator asked for it.
        assert service.enabled
        assert not service.ready

    @patch("pbx.features.voicemail_transcription.GOOGLE_SPEECH_AVAILABLE", True)
    @patch("pbx.features.voicemail_transcription.speech")
    def test_transcription_google_success(self, mock_speech: MagicMock) -> None:
        """Test successful Google Cloud Speech-to-Text transcription"""
        # Google auth comes from GOOGLE_APPLICATION_CREDENTIALS, never from config.
        config = _config(enabled=True, provider="google")

        # Mock Google Speech client
        mock_client = MagicMock()
        mock_speech.SpeechClient.return_value = mock_client

        # Mock configuration classes
        mock_speech.RecognitionAudio = MagicMock()
        mock_speech.RecognitionConfig = MagicMock()
        mock_speech.RecognitionConfig.AudioEncoding = MagicMock()

        # Mock recognition response
        mock_alternative = MagicMock()
        mock_alternative.transcript = "This is a Google transcription test."
        mock_alternative.confidence = 0.95

        mock_result = MagicMock()
        mock_result.alternatives = [mock_alternative]

        mock_response = MagicMock()
        mock_response.results = [mock_result]

        mock_client.recognize.return_value = mock_response

        service = VoicemailTranscriptionService(config)
        result = service.transcribe(self.test_audio_path)

        assert result["success"]
        assert result["text"] == "This is a Google transcription test."
        assert result["confidence"] == 0.95
        assert result["provider"] == "google"
        assert result["error"] is None

    @patch("pbx.features.voicemail_transcription.GOOGLE_SPEECH_AVAILABLE", True)
    @patch("pbx.features.voicemail_transcription.speech")
    def test_transcription_google_no_results(self, mock_speech: MagicMock) -> None:
        """Test Google transcription with no results"""
        config = _config(enabled=True, provider="google")

        # Mock Google Speech client
        mock_client = MagicMock()
        mock_speech.SpeechClient.return_value = mock_client

        # Mock configuration classes
        mock_speech.RecognitionAudio = MagicMock()
        mock_speech.RecognitionConfig = MagicMock()
        mock_speech.RecognitionConfig.AudioEncoding = MagicMock()

        # Mock empty response
        mock_response = MagicMock()
        mock_response.results = []

        mock_client.recognize.return_value = mock_response

        service = VoicemailTranscriptionService(config)
        result = service.transcribe(self.test_audio_path)

        assert not result["success"]
        assert result["error"] == "Transcription returned no results"

    def test_transcription_result_structure(self) -> None:
        """Test that transcription result has correct structure"""
        config = _config(enabled=False)

        service = VoicemailTranscriptionService(config)
        result = service.transcribe(self.test_audio_path)

        # Verify all required keys are present
        required_keys = [
            "success",
            "text",
            "confidence",
            "language",
            "provider",
            "timestamp",
            "error",
        ]
        for key in required_keys:
            assert key in result
        # Verify types
        assert isinstance(result["success"], bool)
        assert isinstance(result["confidence"], float)
        assert isinstance(result["timestamp"], datetime)


class TestTranscriptionConfiguration:
    """Configuration defaults and the enabled/ready distinction."""

    def test_defaults_when_nothing_configured(self) -> None:
        """An empty config yields a disabled service with usable defaults."""
        service = VoicemailTranscriptionService(_config())

        # Off by default: the model is a separate ~40 MB download, so defaulting to on would
        # mean a fresh install silently fails to transcribe.
        assert not service.enabled
        assert service.provider == "vosk"
        assert service.language == "en-US"
        assert service.max_audio_seconds == 300

    def test_no_config_object_is_safe(self) -> None:
        """Constructing without config must not raise."""
        service = VoicemailTranscriptionService(None)

        assert not service.enabled
        assert not service.ready

    def test_enabled_but_no_model_is_not_ready(self) -> None:
        """
        The state worth naming: switched on, but the model never loaded.

        This is what a deployment looks like when the Vosk model was not downloaded, and it
        must be distinguishable from a working setup rather than failing per message.
        """
        service = VoicemailTranscriptionService(
            _config(enabled=True, provider="vosk", vosk_model_path="/nonexistent/model")
        )

        assert service.enabled
        assert not service.ready
        assert service.vosk_model is None

    def test_ready_when_model_loaded(self) -> None:
        """A loaded model is what flips ready to True."""
        service = VoicemailTranscriptionService(_config(enabled=True, provider="vosk"))
        service.vosk_model = Mock()

        assert service.ready

    def test_configured_language_is_the_default(self) -> None:
        """transcribe() falls back to the configured language, not a hardcoded one."""
        service = VoicemailTranscriptionService(_config(enabled=False, language="nl-NL"))

        assert service.language == "nl-NL"
        assert service.transcribe("/nonexistent/file.wav")["language"] == "nl-NL"

    def test_explicit_language_overrides_config(self) -> None:
        """An explicit argument still wins."""
        service = VoicemailTranscriptionService(_config(enabled=False, language="nl-NL"))

        assert service.transcribe("/nonexistent/file.wav", language="de-DE")["language"] == "de-DE"

    def test_api_key_is_not_read_from_config(self) -> None:
        """
        Credentials never come from the config file.

        Google auth goes through GOOGLE_APPLICATION_CREDENTIALS. Reading a key out of
        config.yml is the mistake smtp.password was deliberately fixed to avoid.
        """
        service = VoicemailTranscriptionService(_config(enabled=True, provider="google"))

        assert not hasattr(service, "api_key")


class TestTranscriptionWiring:
    """The service must reach the mailbox that needs it."""

    def test_voicemail_system_forwards_service_to_mailboxes(self, tmp_path: Any) -> None:
        """
        VoicemailSystem holds the shared instance and hands it to every mailbox.

        Sharing is a hard requirement, not tidiness: the Vosk model is ~40 MB resident, so one
        service per mailbox would not survive a few hundred extensions.
        """
        from pbx.features.voicemail import VoicemailSystem

        transcriber = Mock()
        system = VoicemailSystem(storage_path=str(tmp_path), transcription_service=transcriber)

        first = system.get_mailbox("1001")
        second = system.get_mailbox("1002")

        assert first.transcription_service is transcriber
        assert second.transcription_service is transcriber

    def test_voicemail_system_without_service_is_safe(self, tmp_path: Any) -> None:
        """Omitting the service leaves mailboxes with None rather than failing."""
        from pbx.features.voicemail import VoicemailSystem

        system = VoicemailSystem(storage_path=str(tmp_path))

        assert system.get_mailbox("1001").transcription_service is None
