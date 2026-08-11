"""
Result types shared by every transcription backend.

One shape, whatever produced it. Consumers -- the voicemail notification today, call logging
and the analytics features next -- read these rather than a per-engine dictionary, so adding
a backend cannot change what a caller has to handle.

Failure is represented once, by :attr:`Transcript.error`. Everything else derives from it.
Note in particular that empty text with no error is a *success*: it means the recogniser ran
and heard nothing, which is the ordinary outcome for a caller who hung up on the beep.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Word:
    """A single recognised word, with its position in the audio."""

    text: str
    start: float
    end: float
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class Segment:
    """
    A contiguous run of speech.

    Vosk emits one per utterance boundary; Whisper emits one per decoded window. Neither
    meaning is load-bearing for callers -- segments exist so that timing survives, for the
    call-logging and analytics consumers that will want to seek into a recording.
    """

    text: str
    start: float = 0.0
    end: float = 0.0
    confidence: float | None = None
    words: tuple[Word, ...] = ()
    #: Who said it. Empty when unknown, which is every single-source transcript -- voicemail
    #: has one speaker by definition, and a mixed recording cannot tell them apart. Filled in
    #: when a call is transcribed per channel, where the recording already separated the
    #: participants and attribution costs nothing more than carrying the label through.
    speaker: str = ""


@dataclass(frozen=True, slots=True)
class Transcript:
    """
    What a backend produces for one piece of audio.

    ``confidence`` is deliberately optional. Vosk reports a real per-word confidence that can
    be averaged; Whisper exposes only ``avg_logprob``, which is not a confidence and must not
    be presented as one. A backend that cannot measure it leaves this ``None``, and callers
    that render an accuracy figure omit it rather than inventing a number.
    """

    text: str = ""
    segments: tuple[Segment, ...] = field(default=())
    language: str = ""
    provider: str = ""
    model: str = ""
    confidence: float | None = None
    #: Length of the source audio, in seconds.
    audio_duration: float = 0.0
    #: Wall-clock time the backend took. Divided by audio_duration this is the real-time
    #: factor, which is the number that decides whether a model is affordable on this box.
    processing_duration: float = 0.0
    error: str | None = None

    @property
    def success(self) -> bool:
        """True when transcription ran, whether or not it heard anything."""
        return self.error is None

    @property
    def real_time_factor(self) -> float | None:
        """Processing seconds per audio second. Below 1.0 is faster than real time."""
        if self.audio_duration <= 0:
            return None
        return self.processing_duration / self.audio_duration

    @classmethod
    def failure(
        cls, error: str, *, provider: str = "", language: str = "", model: str = ""
    ) -> Transcript:
        """Build a failed result. The only way an error Transcript should be constructed."""
        return cls(error=error, provider=provider, language=language, model=model)
