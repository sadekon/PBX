"""
Voicemail transcription service using speech-to-text
"""

import json
import wave
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pbx.utils.logger import get_logger

# Constants for Vosk transcription
VOSK_FRAME_SIZE = 4000  # Number of frames to read per chunk
VOSK_DEFAULT_CONFIDENCE = 0.95  # Default confidence when Vosk doesn't provide one

# Configuration defaults, all under features.voicemail_transcription in config.yml
DEFAULT_LANGUAGE = "en-US"
DEFAULT_MAX_AUDIO_SECONDS = 300  # A stuck recording must not pin a CPU indefinitely
DEFAULT_VOSK_MODEL_PATH = "models/vosk-model-small-en-us-0.15"

# Import Vosk (free, offline speech recognition)
try:
    from vosk import KaldiRecognizer, Model

    VOSK_AVAILABLE = True
except ImportError:
    VOSK_AVAILABLE = False

# Optional: Google Cloud Speech (kept for backward compatibility)
speech = None  # Initialize to None for testing
try:
    from google.cloud import speech

    GOOGLE_SPEECH_AVAILABLE = True
except ImportError:
    GOOGLE_SPEECH_AVAILABLE = False


class VoicemailTranscriptionService:
    """Service for transcribing voicemail messages to text"""

    def __init__(self, config: Any | None = None) -> None:
        """
        Initialize transcription service.

        The Vosk model is loaded here rather than from a separate ``start()``. The service is
        constructed by :class:`~pbx.core.feature_initializer.FeatureInitializer` during PBX
        startup, which is already the right place to pay for a ~40 MB model load, so a second
        lifecycle method would only relocate work that is where it belongs.

        Nothing raises. A missing library, a missing model or a corrupt one leaves the service
        constructed but not :attr:`ready`, which costs transcription and nothing else -- the
        PBX must still boot and still record voicemail.

        Args:
            config: Config object with transcription settings.
        """
        self.logger = get_logger()
        self.config = config
        self.enabled = False
        self.provider = "vosk"  # Default to vosk (free, offline)
        self.language = DEFAULT_LANGUAGE
        self.max_audio_seconds = DEFAULT_MAX_AUDIO_SECONDS
        self.vosk_model = None
        self.vosk_model_path = DEFAULT_VOSK_MODEL_PATH

        if not config:
            return

        # Dotted lookups, matching how every other feature reads its configuration.
        self.enabled = config.get("features.voicemail_transcription.enabled", False)
        self.provider = config.get("features.voicemail_transcription.provider", "vosk")
        self.language = config.get("features.voicemail_transcription.language", DEFAULT_LANGUAGE)
        self.max_audio_seconds = config.get(
            "features.voicemail_transcription.max_audio_seconds", DEFAULT_MAX_AUDIO_SECONDS
        )
        self.vosk_model_path = config.get(
            "features.voicemail_transcription.vosk_model_path", DEFAULT_VOSK_MODEL_PATH
        )

        if not self.enabled:
            self.logger.debug("Voicemail transcription service disabled in configuration")
            return

        self.logger.info("Voicemail transcription service initialized")
        self.logger.info(f"  Provider: {self.provider}")
        if self.provider == "vosk":
            self.logger.info(f"  Model path: {self.vosk_model_path}")
            self._load_vosk_model()

    def _load_vosk_model(self) -> None:
        """
        Load the Vosk model, leaving the service not-ready if it cannot be loaded.

        Catches broadly on purpose. Vosk surfaces whatever its native layer raises for a
        malformed or half-extracted model directory -- the classic being a double-nested
        unzip -- and that is not reliably an OSError. Narrowing this would turn a recoverable
        misconfiguration into a failure to start.
        """
        if not VOSK_AVAILABLE:
            self.logger.warning("  Vosk library not installed. Install with: pip install vosk")
            return

        if not Path(self.vosk_model_path).exists():
            self.logger.warning(f"  Vosk model not found at {self.vosk_model_path}")
            self.logger.info("  Download model from: https://alphacephei.com/vosk/models")
            return

        try:
            self.vosk_model = Model(self.vosk_model_path)
        except Exception as e:
            self.logger.error(f"  Failed to load Vosk model from {self.vosk_model_path}: {e}")
            return

        self.logger.info("  Vosk model loaded successfully (offline transcription ready)")

    @property
    def ready(self) -> bool:
        """
        Whether a transcription request can actually be served right now.

        Deliberately distinct from :attr:`enabled`, which records only what the operator asked
        for. The gap between the two is the state worth naming: transcription is switched on
        but the model is missing or failed to load. That is the most likely failure after a
        deployment, and consulting ``enabled`` alone makes it indistinguishable from working.
        Callers gate on this so the case is skipped cleanly rather than producing a failed
        transcription for every message.
        """
        if not self.enabled:
            return False
        if self.provider == "vosk":
            return self.vosk_model is not None
        if self.provider == "google":
            return GOOGLE_SPEECH_AVAILABLE
        return False

    def transcribe(self, audio_file_path: str, language: str | None = None) -> dict:
        """
        Transcribe voicemail audio file to text

        Args:
            audio_file_path: Path to audio file (WAV format)
            language: Language code. Defaults to the configured
                ``features.voicemail_transcription.language``.

        Returns:
            Dictionary with transcription results:
            {
                'success': bool,
                'text': str,
                'confidence': float,
                'language': str,
                'provider': str,
                'timestamp': datetime,
                'error': str (if success is False)
            }
        """
        language = language or self.language

        if not self.enabled:
            return {
                "success": False,
                "text": None,
                "confidence": 0.0,
                "language": language,
                "provider": None,
                "timestamp": datetime.now(UTC),
                "error": "Transcription service is disabled",
            }

        if not Path(audio_file_path).exists():
            self.logger.error(f"Audio file not found: {audio_file_path}")
            return {
                "success": False,
                "text": None,
                "confidence": 0.0,
                "language": language,
                "provider": self.provider,
                "timestamp": datetime.now(UTC),
                "error": f"Audio file not found: {audio_file_path}",
            }

        self.logger.info(f"Transcribing voicemail: {audio_file_path}")
        self.logger.info(f"  Provider: {self.provider}")
        self.logger.info(f"  Language: {language}")

        try:
            if self.provider == "vosk":
                return self._transcribe_vosk(audio_file_path, language)
            if self.provider == "google":
                return self._transcribe_google(audio_file_path, language)
            error_msg = f"Unsupported transcription provider: {self.provider}. Use 'vosk' (recommended, free) or 'google'"
            self.logger.error(error_msg)
            return {
                "success": False,
                "text": None,
                "confidence": 0.0,
                "language": language,
                "provider": self.provider,
                "timestamp": datetime.now(UTC),
                "error": error_msg,
            }
        except Exception as e:
            self.logger.error(f"Transcription failed: {e}")
            return {
                "success": False,
                "text": None,
                "confidence": 0.0,
                "language": language,
                "provider": self.provider,
                "timestamp": datetime.now(UTC),
                "error": str(e),
            }

    def _create_error_response(
        self, error_msg: str, language: str, provider: str | None = None
    ) -> dict:
        """
        Helper method to create error response structure

        Args:
            error_msg: Error message string
            language: Language code
            provider: Provider name (optional)

        Returns:
            Dictionary with error response structure
        """
        return {
            "success": False,
            "text": None,
            "confidence": 0.0,
            "language": language,
            "provider": provider or self.provider,
            "timestamp": datetime.now(UTC),
            "error": error_msg,
        }

    def _transcribe_vosk(self, audio_file_path: str, language: str = "en-US") -> dict:
        """
        Transcribe using Vosk (offline, free speech recognition)

        Args:
            audio_file_path: Path to audio file
            language: Language code

        Returns:
            Transcription result dictionary
        """
        if not VOSK_AVAILABLE:
            error_msg = "Vosk library not installed. Install with: pip install vosk"
            self.logger.error(error_msg)
            return self._create_error_response(error_msg, language, "vosk")

        if not self.vosk_model:
            error_msg = f"Vosk model not loaded. Check model path: {self.vosk_model_path}"
            self.logger.error(error_msg)
            self.logger.info("Download models from: https://alphacephei.com/vosk/models")
            return self._create_error_response(error_msg, language, "vosk")

        try:
            # Open WAV file
            with wave.open(audio_file_path, "rb") as wf:
                # Validate audio format
                if wf.getnchannels() != 1:
                    error_msg = "Audio must be mono channel"
                    self.logger.error(error_msg)
                    return self._create_error_response(error_msg, language, "vosk")

                # Check sample rate - Vosk works best with 8kHz or 16kHz
                sample_rate = wf.getframerate()
                if sample_rate not in [8000, 16000, 32000, 44100, 48000]:
                    error_msg = f"Unsupported sample rate: {sample_rate}. Use 8000, 16000, 32000, 44100, or 48000 Hz"
                    self.logger.error(error_msg)
                    return self._create_error_response(error_msg, language, "vosk")

                # Decoding is CPU-bound and runs to completion, so a recording that never
                # stopped would occupy a core for as long as it takes to decode. Refuse it.
                duration_seconds = wf.getnframes() / float(sample_rate)
                if duration_seconds > self.max_audio_seconds:
                    error_msg = (
                        f"Audio is {duration_seconds:.0f}s, longer than the "
                        f"{self.max_audio_seconds}s limit"
                    )
                    self.logger.error(error_msg)
                    return self._create_error_response(error_msg, language, "vosk")

                # Create recognizer
                rec = KaldiRecognizer(self.vosk_model, sample_rate)
                rec.SetWords(True)  # Enable word-level timestamps

                # Process audio in chunks
                self.logger.info("Processing audio with Vosk (offline)...")
                results = []

                while True:
                    data = wf.readframes(VOSK_FRAME_SIZE)
                    if len(data) == 0:
                        break
                    if rec.AcceptWaveform(data):
                        result = json.loads(rec.Result())
                        if result.get("text"):
                            results.append(result["text"])

                # Get final result
                final_result = json.loads(rec.FinalResult())
                if final_result.get("text"):
                    results.append(final_result["text"])

            # Combine all text
            text = " ".join(results).strip()

            if text:
                self.logger.info("✓ Transcription successful (offline)")
                self.logger.info(f"  Text length: {len(text)} characters")
                self.logger.debug(f"  Text: {text[:100]}...")
                return {
                    "success": True,
                    "text": text,
                    "confidence": VOSK_DEFAULT_CONFIDENCE,  # Vosk doesn't provide confidence
                    "language": language,
                    "provider": "vosk",
                    "timestamp": datetime.now(UTC),
                    "error": None,
                }
            self.logger.warning("Transcription returned empty text")
            return self._create_error_response(
                "Transcription returned empty text", language, "vosk"
            )

        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as e:
            self.logger.error(f"Vosk transcription error: {e}")
            return self._create_error_response(str(e), language, "vosk")

    def _transcribe_google(self, audio_file_path: str, language: str = "en-US") -> dict:
        """
        Transcribe using Google Cloud Speech-to-Text API

        Args:
            audio_file_path: Path to audio file
            language: Language code

        Returns:
            Transcription result dictionary
        """
        if not GOOGLE_SPEECH_AVAILABLE:
            error_msg = "Google Cloud Speech library not installed. Install with: pip install google-cloud-speech"
            self.logger.error(error_msg)
            return {
                "success": False,
                "text": None,
                "confidence": 0.0,
                "language": language,
                "provider": "google",
                "timestamp": datetime.now(UTC),
                "error": error_msg,
            }

        try:
            # Initialize Google Speech client
            client = speech.SpeechClient()

            # Read audio file
            with Path(audio_file_path).open("rb") as audio_file:
                content = audio_file.read()

            # Configure audio and recognition settings
            audio = speech.RecognitionAudio(content=content)
            config = speech.RecognitionConfig(
                encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
                sample_rate_hertz=8000,  # Standard phone audio sample rate
                language_code=language,
                enable_automatic_punctuation=True,
                model="phone_call",  # Optimized for phone call audio
            )

            # Perform transcription
            self.logger.info("Calling Google Cloud Speech-to-Text API...")
            response = client.recognize(config=config, audio=audio)

            # Extract best result
            if response.results:
                result = response.results[0]
                if result.alternatives:
                    alternative = result.alternatives[0]
                    text = alternative.transcript.strip()
                    confidence = alternative.confidence

                    if text:
                        self.logger.info("✓ Transcription successful")
                        self.logger.info(f"  Text length: {len(text)} characters")
                        self.logger.info(f"  Confidence: {confidence:.2%}")
                        self.logger.debug(f"  Text: {text[:100]}...")
                        return {
                            "success": True,
                            "text": text,
                            "confidence": confidence,
                            "language": language,
                            "provider": "google",
                            "timestamp": datetime.now(UTC),
                            "error": None,
                        }

            # No results found
            self.logger.warning("Transcription returned no results")
            return {
                "success": False,
                "text": None,
                "confidence": 0.0,
                "language": language,
                "provider": "google",
                "timestamp": datetime.now(UTC),
                "error": "Transcription returned no results",
            }

        except OSError as e:
            self.logger.error(f"Google transcription error: {e}")
            return {
                "success": False,
                "text": None,
                "confidence": 0.0,
                "language": language,
                "provider": "google",
                "timestamp": datetime.now(UTC),
                "error": str(e),
            }
