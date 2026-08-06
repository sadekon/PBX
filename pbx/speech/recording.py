"""
Turning a finished call recording into one attributed transcript.

A recording holds one channel per participant (``pbx/features/call_recording.py``), so each
channel contains exactly one voice. That is worth a great deal: transcribing each channel
separately means every line comes out already labelled with who said it, and two people
talking over each other transcribe cleanly because they never shared any audio. Mixing to mono
first would throw both properties away, and recovering them afterwards needs a diarisation
model that this project does not have and would not want on a PBX.

The cost is one model pass per participant rather than one per call. Two things keep that
in hand:

* **Channels where nobody spoke are never submitted.** Each one is checked against the noise
  floor first (``active_speech_seconds``). A participant who only listened, or a leg that sat
  on hold, costs nothing instead of a full pass -- and cannot produce a hallucination, which
  is what a speech model reliably does when handed silence.
* **Silence inside a channel is skipped by the recogniser.** Each channel is quiet for the
  whole time the other party is talking, so roughly half of every channel is nothing.
  faster-whisper's ``vad_filter`` (on by default) segments speech with Silero VAD and runs the
  model only over those regions, mapping timestamps back itself. Vosk has no equivalent, so
  the per-channel cost there is proportional to the whole call.

Transcription runs on the shared :class:`~pbx.speech.worker.TranscriptionWorker`, so N channels
are N queued jobs competing with everything else for one worker rather than N threads competing
with live calls. The merge happens when the last of them reports back.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import wave
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from pbx.speech.dialogue import format_dialogue
from pbx.speech.store import SOURCE_RECORDING, TranscriptStore
from pbx.speech.types import Segment, Transcript
from pbx.utils.audio import SILENCE_RMS_FLOOR, active_speech_seconds
from pbx.utils.logger import get_logger

if TYPE_CHECKING:
    from pbx.speech.worker import TranscriptionWorker

__all__ = ["MIN_SPEECH_SECONDS", "RecordingTranscriber"]

#: A channel with less speech than this is not transcribed at all.
#:
#: Low on purpose. This is not trying to judge whether a channel is *interesting*, only whether
#: there is anything there -- a single word is worth keeping, and the recogniser decides what
#: it was. The threshold exists to skip channels that are genuinely empty.
MIN_SPEECH_SECONDS = 0.3

#: Samples read at a time when splitting channels, so a long call is never held whole.
_SPLIT_CHUNK = 16000


class _PendingTranscription:
    """
    One recording's worth of per-channel jobs, and what came back.

    Jobs complete on worker threads in any order, so this collects them under a lock and the
    last one in triggers the merge. Counting submissions rather than channels matters: a
    channel that was skipped as silent, or that the worker refused, never reports back, and
    waiting for it would strand the whole transcript.
    """

    def __init__(self, session_id: str, media_path: Path, workspace: Path) -> None:
        self.session_id = session_id
        self.media_path = media_path
        self.workspace = workspace
        self.expected = 0
        self.results: dict[str, Transcript] = {}
        self.lock = threading.Lock()
        self.finished = False

    def record(self, speaker: str, transcript: Transcript | None) -> bool:
        """Store one result. Returns True when this was the last one outstanding."""
        with self.lock:
            if transcript is not None and transcript.success:
                self.results[speaker] = transcript
            self.expected -= 1
            if self.expected > 0 or self.finished:
                return False
            self.finished = True
            return True


class RecordingTranscriber:
    """
    Transcribes finished recordings, one pass per participant.

    Constructed unconditionally like the mailer and the store, so callers get a real object
    that reports its own state rather than something to guard with ``hasattr``.
    """

    def __init__(
        self,
        worker: TranscriptionWorker | None = None,
        store: TranscriptStore | None = None,
        logger: Any | None = None,
        min_speech_seconds: float = MIN_SPEECH_SECONDS,
        silence_floor: float = SILENCE_RMS_FLOOR,
    ) -> None:
        self.worker = worker
        self.store = store or TranscriptStore()
        self.logger = logger or get_logger()
        self.min_speech_seconds = min_speech_seconds
        #: RMS floor for the whole-channel gate. A genuine power threshold, unlike Silero's,
        #: and the only one that decides whether a model runs at all.
        self.silence_floor = silence_floor

    @property
    def enabled(self) -> bool:
        """Whether a submission could be accepted right now."""
        return bool(self.worker is not None and getattr(self.worker, "available", False))

    # ---------------------------------------------------------------- submission

    def submit(self, media_path: Path) -> bool:
        """
        Queue a finished recording for transcription. Returns whether anything was accepted.

        Never raises: this is called from the recording's completion callback, which runs on
        the tap's drain thread at the end of a call. A transcription that cannot start is
        worth a log line, not a lost recording.
        """
        if not self.enabled:
            return False

        try:
            return self._submit(media_path)
        except Exception as e:
            self.logger.error(f"Could not transcribe recording {media_path}: {e}")
            return False

    def _submit(self, media_path: Path) -> bool:
        channels = self._channel_labels(media_path)
        if not channels:
            self.logger.debug(f"No channel manifest for {media_path}; not transcribing")
            return False

        workspace = Path(tempfile.mkdtemp(prefix="transcribe-", dir=str(media_path.parent)))
        pending = _PendingTranscription(
            session_id=self._session_id(media_path), media_path=media_path, workspace=workspace
        )

        jobs = self._split(media_path, channels, workspace)
        if not jobs:
            shutil.rmtree(workspace, ignore_errors=True)
            self.logger.info(
                f"Recording {media_path.name}: every channel was silent; nothing transcribed"
            )
            return False

        # Counted up front, before anything is submitted. Incrementing as each job is accepted
        # would let an early completion see expected==0 and merge a partial transcript.
        pending.expected = len(jobs)
        accepted = 0

        for speaker, path, seconds in jobs:
            if self.worker.submit(
                path,
                lambda transcript, speaker=speaker: self._on_channel(pending, speaker, transcript),
                label=f"{media_path.stem}:{speaker}",
                audio_seconds=seconds,
            ):
                accepted += 1
            else:
                # Refused -- queue full, over the length cap, service stopping. Its callback
                # will never fire, so release the slot here or the merge never happens.
                self._on_channel(pending, speaker, None)

        if accepted == 0:
            self.logger.warning(f"Recording {media_path.name}: no channel was accepted")
            return False

        self.logger.info(
            f"Recording {media_path.name}: transcribing {accepted} channel(s) of {len(channels)}"
        )
        return True

    # ---------------------------------------------------------------- splitting

    def _channel_labels(self, media_path: Path) -> list[str]:
        """Speaker labels in channel order, from the sidecar the recorder wrote."""
        manifest_path = media_path.with_suffix(".json")
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, TypeError, ValueError):
            return []

        return [
            str(entry.get("label") or entry.get("source") or f"channel{index}")
            for index, entry in enumerate(manifest.get("channels", []))
        ]

    def _session_id(self, media_path: Path) -> str:
        manifest_path = media_path.with_suffix(".json")
        try:
            manifest = json.loads(manifest_path.read_text())
            return str(manifest.get("session_id") or media_path.stem)
        except (OSError, TypeError, ValueError):
            return media_path.stem

    def _split(
        self, media_path: Path, labels: list[str], workspace: Path
    ) -> list[tuple[str, Path, float]]:
        """
        Write each channel that contains speech to its own mono WAV.

        Returns (speaker, path, seconds) per channel worth transcribing. Silent channels are
        dropped here, which is the whole point: they never reach a model.
        """
        with wave.open(str(media_path), "rb") as source:
            count = source.getnchannels()
            rate = source.getframerate()
            width = source.getsampwidth()
            if width != 2:
                self.logger.warning(f"{media_path.name}: expected PCM16, got {width * 8}-bit")
                return []

            columns: list[bytearray] = [bytearray() for _ in range(count)]
            while True:
                raw = source.readframes(_SPLIT_CHUNK)
                if not raw:
                    break
                block = np.frombuffer(raw, dtype="<i2").reshape(-1, count)
                for index in range(count):
                    columns[index].extend(block[:, index].tobytes())

        jobs: list[tuple[str, Path, float]] = []
        for index, column in enumerate(columns):
            speaker = labels[index] if index < len(labels) else f"channel{index}"
            speech = active_speech_seconds(bytes(column), rate, self.silence_floor)
            if speech < self.min_speech_seconds:
                self.logger.debug(
                    f"{media_path.name}: {speaker} has {speech:.1f}s of speech; skipping"
                )
                continue

            path = workspace / f"{speaker}.wav"
            try:
                with wave.open(str(path), "wb") as out:
                    out.setnchannels(1)
                    out.setsampwidth(2)
                    out.setframerate(rate)
                    out.writeframes(bytes(column))
            except (OSError, wave.Error) as e:
                self.logger.error(f"Could not write channel {speaker}: {e}")
                continue

            jobs.append((speaker, path, len(column) / 2 / rate))

        return jobs

    # ---------------------------------------------------------------- completion

    def _on_channel(
        self, pending: _PendingTranscription, speaker: str, transcript: Transcript | None
    ) -> None:
        """One channel finished. Merges and stores when it was the last one."""
        try:
            if not pending.record(speaker, transcript):
                return
            self._finish(pending)
        except Exception as e:
            self.logger.error(f"Transcription merge for {pending.session_id} failed: {e}")
        finally:
            if pending.finished:
                shutil.rmtree(pending.workspace, ignore_errors=True)

    def _finish(self, pending: _PendingTranscription) -> None:
        """Merge every channel into one transcript and store it."""
        if not pending.results:
            self.logger.info(f"Recording {pending.media_path.name}: no speech recognised")
            return

        merged = merge_channels(pending.results)
        self.store.save(
            merged,
            source=SOURCE_RECORDING,
            call_id=pending.session_id,
            media_path=str(pending.media_path),
        )
        self.logger.info(
            f"Transcribed {pending.media_path.name}: "
            f"{len(merged.segments)} segment(s) across {len(pending.results)} speaker(s)"
        )


def merge_channels(results: dict[str, Transcript]) -> Transcript:
    """
    Fold per-speaker transcripts into one, ordered by time.

    Every channel shares the recording's timeline -- that is what the recorder's alignment
    buys -- so segments from different speakers interleave correctly on their start times
    without any further reconciliation.
    """
    segments: list[Segment] = []
    for speaker, transcript in results.items():
        segments.extend(replace(segment, speaker=speaker) for segment in transcript.segments)

    segments.sort(key=lambda segment: (segment.start, segment.speaker))

    # format_dialogue regroups these into turns: a recogniser emits a segment every few
    # seconds, so one sentence arrives as several and printing them line by line is unreadable.
    text = format_dialogue(segments)

    if not text:
        # A backend that reports no segments still produced text worth keeping. Falls back to
        # speaker order, since without timings there is nothing better to sort on.
        text = "\n".join(
            f"{speaker}: {t.text.strip()}" for speaker, t in results.items() if t.text.strip()
        )

    first = next(iter(results.values()))
    return Transcript(
        text=text,
        segments=tuple(segments),
        language=first.language,
        provider=first.provider,
        model=first.model,
        # Deliberately None: averaging per-channel confidences produces a number that
        # describes nothing in particular, and the disclaimer omits the claim when it is None.
        confidence=None,
        audio_duration=max((t.audio_duration for t in results.values()), default=0.0),
        processing_duration=sum(t.processing_duration for t in results.values()),
    )
