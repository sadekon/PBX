"""
Vosk file-transcription backend.

Offline, free, and small enough to sit on a PBX. This is the logic that previously lived in
``pbx/features/voicemail_transcription.py``, moved here behind :class:`FileTranscriber` so
voicemail, call logging and the analytics features can share one engine and one model instead
of the three separate Vosk loaders that had accumulated.

Two things here are specific to telephony and easy to get wrong:

* Voicemail is stored as G.711, which :mod:`wave` refuses outright (``unknown format: 7``).
  Decoding goes through :func:`pbx.utils.audio.read_wav_as_pcm16`.
* Vosk models are trained at 16 kHz while calls are 8 kHz, and Kaldi treats the mismatch as
  fatal rather than resampling. The audio is resampled to whatever the model declares.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from pbx.speech.types import Segment, Transcript, Word
from pbx.utils.audio import read_wav_as_pcm16, resample_pcm16
from pbx.utils.logger import get_logger

if TYPE_CHECKING:
    from pbx.speech.settings import TranscriptionSettings

#: Samples handed to the recogniser per call.
VOSK_FRAME_SIZE: Final[int] = 4000
PCM16_BYTES_PER_SAMPLE: Final[int] = 2
SUPPORTED_SAMPLE_RATES: Final[tuple[int, ...]] = (8000, 16000, 32000, 44100, 48000)
#: Assumed when a model does not declare one. Effectively every Vosk model is 16 kHz.
DEFAULT_MODEL_SAMPLE_RATE: Final[int] = 16000

try:
    from vosk import KaldiRecognizer, Model

    VOSK_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on the install, not on logic
    VOSK_AVAILABLE = False


class VoskBackend:
    """Implements :class:`~pbx.speech.protocols.FileTranscriber` using Vosk."""

    provider = "vosk"

    def __init__(self, settings: TranscriptionSettings, logger: Any | None = None) -> None:
        """
        Load the model, or leave the backend not-:attr:`ready`.

        Loading happens here because construction is done once at startup by
        ``FeatureInitializer``, which is the right place to pay for it. Nothing raises: a
        missing library or a corrupt model costs transcription and nothing else, and the PBX
        must still boot and still record voicemail.
        """
        self.settings = settings
        self.logger = logger or get_logger()
        self.model: Any | None = None
        self.model_path = settings.vosk_model_path
        #: Recorded against a transcript: the directory name identifies the model
        #: (vosk-model-small-en-us-0.15) while the full path is specific to one host.
        self.model_name = Path(self.model_path).name or self.model_path

        if settings.enabled:
            self._load_model()

    def _load_model(self) -> None:
        """Load the model, reporting and swallowing every failure."""
        if not VOSK_AVAILABLE:
            self.logger.warning("Vosk library not installed. Install with: pip install vosk")
            return

        if not Path(self.model_path).exists():
            self.logger.warning(f"Vosk model not found at {self.model_path}")
            self.logger.info("Download models from: https://alphacephei.com/vosk/models")
            return

        try:
            self.model = Model(self.model_path)
        except Exception as e:
            # Catches broadly on purpose: Vosk surfaces whatever its native layer raises for a
            # malformed or half-extracted model directory, and that is not reliably an OSError.
            self.logger.error(f"Failed to load Vosk model from {self.model_path}: {e}")
            return

        self.logger.info(f"Vosk model loaded from {self.model_path}")

    @property
    def ready(self) -> bool:
        """Whether a transcription request can actually be served right now."""
        return self.model is not None

    def _model_sample_rate(self) -> int:
        """
        The rate the model was trained at, from its ``conf/mfcc.conf``.

        Kaldi aborts with "Sampling frequency mismatch" rather than resampling, so the audio
        is brought to this rate instead of editing a downloaded model's config.
        """
        conf = Path(self.model_path) / "conf" / "mfcc.conf"
        try:
            for line in conf.read_text().splitlines():
                stripped = line.strip()
                if stripped.startswith("--sample-frequency"):
                    return int(float(stripped.split("=", 1)[1].strip()))
        except (OSError, ValueError, IndexError) as e:
            self.logger.debug(f"Could not read model sample rate from {conf}: {e}")
        return DEFAULT_MODEL_SAMPLE_RATE

    def transcribe_file(
        self, path: Path, *, language: str | None = None, want_words: bool = False
    ) -> Transcript:
        """Transcribe a WAV file. Never raises; failures come back as an error Transcript."""
        language = language or self.settings.language
        started = time.monotonic()

        def failed(message: str) -> Transcript:
            self.logger.error(f"Vosk transcription failed: {message}")
            return Transcript.failure(
                message, provider=self.provider, language=language, model=self.model_name
            )

        if not self.ready:
            return failed(f"Vosk model not loaded (path: {self.model_path})")

        try:
            pcm16, sample_rate = read_wav_as_pcm16(path)

            if sample_rate not in SUPPORTED_SAMPLE_RATES:
                return failed(
                    f"Unsupported sample rate: {sample_rate}. Use "
                    f"{', '.join(str(r) for r in SUPPORTED_SAMPLE_RATES)} Hz"
                )

            audio_duration = len(pcm16) / (PCM16_BYTES_PER_SAMPLE * float(sample_rate))
            if audio_duration > self.settings.max_audio_seconds:
                return failed(
                    f"Audio is {audio_duration:.0f}s, longer than the "
                    f"{self.settings.max_audio_seconds}s limit"
                )

            model_rate = self._model_sample_rate()
            if sample_rate != model_rate:
                self.logger.debug(f"Resampling {sample_rate} Hz -> {model_rate} Hz for the model")
                pcm16 = resample_pcm16(pcm16, sample_rate, model_rate)
                sample_rate = model_rate

            segments = self._recognise(pcm16, sample_rate, want_words=want_words)

        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as e:
            return failed(str(e))

        text = " ".join(s.text for s in segments).strip()
        confidences = [s.confidence for s in segments if s.confidence is not None]

        # Empty text is a success, not a failure: it means the recogniser ran and heard
        # nothing, which is exactly what a caller hanging up on the beep produces.
        return Transcript(
            text=text,
            segments=tuple(segments),
            language=language,
            provider=self.provider,
            model=self.model_name,
            confidence=(sum(confidences) / len(confidences)) if confidences else None,
            audio_duration=audio_duration,
            processing_duration=time.monotonic() - started,
        )

    def _recognise(self, pcm16: bytes, sample_rate: int, *, want_words: bool) -> list[Segment]:
        """Feed the audio through Kaldi and collect one Segment per finalised utterance."""
        recognizer = KaldiRecognizer(self.model, sample_rate)
        recognizer.SetWords(True)  # Per-word timing and confidence.

        segments: list[Segment] = []

        def collect(payload: dict) -> None:
            text = payload.get("text")
            if not text:
                return
            words = tuple(
                Word(
                    text=w["word"],
                    start=float(w.get("start", 0.0)),
                    end=float(w.get("end", 0.0)),
                    confidence=w.get("conf"),
                )
                for w in payload.get("result", [])
                if "word" in w
            )
            confidences = [w.confidence for w in words if w.confidence is not None]
            segments.append(
                Segment(
                    text=text,
                    start=words[0].start if words else 0.0,
                    end=words[-1].end if words else 0.0,
                    confidence=(sum(confidences) / len(confidences)) if confidences else None,
                    words=words if want_words else (),
                )
            )

        chunk_bytes = VOSK_FRAME_SIZE * PCM16_BYTES_PER_SAMPLE
        for offset in range(0, len(pcm16), chunk_bytes):
            if recognizer.AcceptWaveform(pcm16[offset : offset + chunk_bytes]):
                collect(json.loads(recognizer.Result()))
        collect(json.loads(recognizer.FinalResult()))

        return segments
