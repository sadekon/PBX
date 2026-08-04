"""
The interfaces a transcription backend may implement.

Deliberately two protocols rather than one. Batch and streaming are different problems and
different engines are good at them: Whisper is a batch model with no streaming story, while
Vosk is built around an incremental recogniser. Forcing either to satisfy both would mean
faking half an implementation, so a backend implements only what it can actually do and the
engine picks per use case.

:class:`StreamTranscriber` is declared but **not implemented** anywhere yet. Live call
monitoring additionally needs an audio tap on the RTP relay, which does not exist -- see
``pbx/rtp/handler.py``. The protocol is here so that adding it later is a new backend rather
than a reshaping of the facade.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pathlib import Path

    from pbx.speech.types import Segment, Transcript


@runtime_checkable
class FileTranscriber(Protocol):
    """Transcribes a complete audio file. Implemented by every backend."""

    def transcribe_file(
        self, path: Path, *, language: str | None = None, want_words: bool = False
    ) -> Transcript:
        """
        Transcribe `path` and return the result.

        Never raises: a failure comes back as a Transcript carrying ``error``, so a caller has
        exactly one thing to check. ``want_words`` is opt-in because word-level timing costs
        real time on some engines and only the analytics consumers need it.
        """
        ...


@runtime_checkable
class StreamSession(Protocol):
    """One live recognition session, owning the decoder state for a single audio stream."""

    def feed(self, pcm16: bytes) -> Segment | None:
        """Accept mono PCM16 audio, returning a Segment once one is finalised."""
        ...

    def close(self) -> Transcript:
        """Flush and return everything recognised in this session."""
        ...


@runtime_checkable
class StreamTranscriber(Protocol):
    """Transcribes incrementally. Not implemented yet -- see the module docstring."""

    def open_stream(self, sample_rate: int) -> StreamSession:
        """Start a session. Each caller gets its own decoder state."""
        ...
