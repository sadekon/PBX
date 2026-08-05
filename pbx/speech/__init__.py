"""
Speech transcription for the PBX.

This package owns *how* audio becomes text: engine selection, model loading, audio conversion
and the background worker that keeps it off the call path. It owns none of what the text is
*for*. Whether a transcript goes in an email, into a call log or to an analytics feature
belongs to the feature asking for it, which is why nothing here knows what a voicemail is.

Configuration lives under ``features.voicemail_transcription`` -- historical, since voicemail
was the first consumer.

Typical use, from a feature holding ``pbx_core.transcription_service``::

    accepted = worker.submit(
        Path(wav_path),
        on_complete=lambda transcript: ...,
        label=message_id,
        audio_seconds=duration,
    )
    if not accepted:
        ...  # proceed without a transcript; nothing was queued
"""

from pbx.speech.protocols import FileTranscriber, StreamSession, StreamTranscriber
from pbx.speech.settings import TranscriptionSettings
from pbx.speech.types import Segment, Transcript, Word
from pbx.speech.worker import TranscriptionJob, TranscriptionWorker

__all__ = [
    "FileTranscriber",
    "Segment",
    "StreamSession",
    "StreamTranscriber",
    "Transcript",
    "TranscriptionJob",
    "TranscriptionSettings",
    "TranscriptionWorker",
    "Word",
    "build_backend",
]


def build_backend(settings: TranscriptionSettings, logger: object | None = None) -> object | None:
    """
    Construct the backend named by ``settings.provider``.

    Returns None for an unknown or unsupported provider rather than raising -- a typo in
    config.yml costs transcription, not the PBX.
    """
    if settings.provider == "vosk":
        from pbx.speech.backends.vosk import VoskBackend

        return VoskBackend(settings, logger=logger)
    if settings.provider == "faster-whisper":
        from pbx.speech.backends.whisper import WhisperBackend

        return WhisperBackend(settings, logger=logger)
    return None
