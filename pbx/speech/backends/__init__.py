"""Transcription backends. One module per engine; each implements what it can do well."""

from pbx.speech.backends.vosk import VoskBackend

__all__ = ["VoskBackend"]
