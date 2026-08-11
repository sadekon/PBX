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
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from pbx.features.recording_consent import SYSTEM_LABEL
from pbx.speech.dialogue import format_dialogue
from pbx.speech.store import SOURCE_RECORDING, TranscriptStore
from pbx.speech.types import Segment, Transcript
from pbx.utils.audio import (
    DEFAULT_SPLIT_GAP_SECONDS,
    SILENCE_RMS_FLOOR,
    frame_rms,
    frame_seconds,
    regions_from_rms,
)
from pbx.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from pbx.speech.worker import TranscriptionWorker

__all__ = ["MIN_SPEECH_SECONDS", "RecordingTranscriber", "combine_regions", "merge_channels"]


def _safe(name: str) -> str:
    """A filename-safe form of a speaker label, which may be a phone number or anything."""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:40] or "channel"


#: A channel with less speech than this is not transcribed at all.
#:
#: Low on purpose. This is not trying to judge whether a channel is *interesting*, only whether
#: there is anything there -- a single word is worth keeping, and the recogniser decides what
#: it was. The threshold exists to skip channels that are genuinely empty.
MIN_SPEECH_SECONDS = 0.3

#: Samples read at a time when splitting channels, so a long call is never held whole.
_SPLIT_CHUNK = 16000

#: Directory the per-region working files live in, created beneath the recording's own
#: directory so they land on the same filesystem -- a scratch dir on another mount turns every
#: region into a cross-device copy.
#:
#: Named, rather than a bare mkdtemp, for two reasons. Retention skips it by name, so the
#: half-finished region files inside are never mistaken for recordings and swept on the audio
#: clock (:data:`pbx.features.retention.SKIP_DIR_NAMES` must match this). And a crash between
#: creating one and removing it leaves an identifiable directory, which
#: :meth:`RecordingTranscriber.clear_scratch` removes at the next startup.
SCRATCH_DIRNAME = ".transcribe"


#: Longest a merged region may become, in seconds.
#:
#: Two reasons for a ceiling. The worker refuses anything over
#: ``transcription.max_audio_seconds`` (300 by default), and a refused region is audio that
#: never gets transcribed at all. And a single enormous job blocks the one worker thread for
#: its whole duration while every other recording waits behind it.
DEFAULT_MAX_REGION_SECONDS = 240.0


def merge_uninterrupted(
    spans: dict[int, list[tuple[float, float]]],
    max_seconds: float = DEFAULT_MAX_REGION_SECONDS,
) -> dict[int, list[tuple[float, float]]]:
    """
    Rejoin a speaker's regions across gaps where nobody else was talking.

    Splitting exists for exactly one reason: whisper merges utterances either side of silence
    it never receives, and on a per-participant recording that silence is usually the other
    person's turn. A gap where *nobody* spoke is not that case -- it is one person pausing,
    and merging across it is not merely harmless but better, because the model keeps the
    context it uses for punctuation and capitalisation.

    It is also much cheaper. Whisper's encoder always processes a 30-second window and pads
    anything shorter, so a two-second region costs about what a thirty-second one costs.
    Cutting a channel at every pause turns one window pass into ten. Cutting it only where
    somebody actually interjected keeps the regions few and long, which is where the model is
    both fastest per second of audio and most accurate.

    Args:
        spans: Regions per channel index, each sorted and non-overlapping.
        max_seconds: Never merge past this length -- see :data:`DEFAULT_MAX_REGION_SECONDS`.

    Returns:
        The same shape, with adjacent regions joined where the gap was quiet.
    """
    merged: dict[int, list[tuple[float, float]]] = {}

    for index, regions in spans.items():
        others = [span for other, rest in spans.items() if other != index for span in rest]
        joined: list[tuple[float, float]] = []

        for start, end in regions:
            if (
                joined
                and end - joined[-1][0] <= max_seconds
                and not _anyone_speaking(others, joined[-1][1], start)
            ):
                joined[-1] = (joined[-1][0], end)
            else:
                joined.append((start, end))

        merged[index] = joined

    return merged


def _anyone_speaking(spans: list[tuple[float, float]], start: float, end: float) -> bool:
    """Whether any of `spans` overlaps the open interval between `start` and `end`."""
    return any(other_start < end and start < other_end for other_start, other_end in spans)


@dataclass(frozen=True, slots=True)
class _Region:
    """One stretch of one participant's speech, written out for transcription."""

    speaker: str
    path: Path
    #: Where this region begins in the recording. Every timestamp the model returns is
    #: relative to the region, so this is what puts it back on the call's timeline.
    offset: float
    seconds: float


class _PendingTranscription:
    """
    One recording's worth of jobs, and what came back.

    Jobs complete on worker threads in any order, so this collects them under a lock and the
    last one in triggers the merge. Counting submissions rather than regions matters: a region
    the worker refused never reports back, and waiting for it would strand the transcript.
    """

    def __init__(
        self,
        session_id: str,
        media_path: Path,
        workspace: Path,
        recording_id: Any = None,
    ) -> None:
        self.session_id = session_id
        self.media_path = media_path
        #: The row this recording was registered as. What ties the transcript to its audio
        #: so retention can expire both by deleting one.
        self.recording_id = recording_id
        self.workspace = workspace
        self.expected = 0
        #: Per speaker, one entry per region: (offset, transcript).
        self.results: dict[str, list[tuple[float, Transcript]]] = {}
        self.lock = threading.Lock()
        self.finished = False

    def record(self, region: _Region, transcript: Transcript | None) -> bool:
        """Store one result. Returns True when this was the last one outstanding."""
        with self.lock:
            if transcript is not None and transcript.success:
                self.results.setdefault(region.speaker, []).append((region.offset, transcript))
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
        recordings: Any = None,
        logger: Any | None = None,
        min_speech_seconds: float = MIN_SPEECH_SECONDS,
        silence_floor: float = SILENCE_RMS_FLOOR,
        split_gap_seconds: float = DEFAULT_SPLIT_GAP_SECONDS,
        max_region_seconds: float = DEFAULT_MAX_REGION_SECONDS,
        notice_text: str = "",
    ) -> None:
        self.worker = worker
        self.store = store or TranscriptStore()
        #: Reads the channel map back from the recording row. Optional: without it the
        #: sidecar is used, which is what a database-less install has anyway.
        self.recordings = recordings
        self.logger = logger or get_logger()
        self.min_speech_seconds = min_speech_seconds
        #: What the recording notice said, injected verbatim rather than recognised. Empty
        #: when consent announcements are off, in which case no notice channel exists.
        self.notice_text = notice_text
        #: Called with (recording_id, transcript_id) when a transcript is stored. How
        #: analysis is triggered without this module having to know analysis exists.
        self.on_transcribed: Callable[[Any, Any], None] | None = None
        #: Silence that must elapse before a channel is cut into separate regions. Raising it
        #: cuts less and protects punctuation; lowering it risks splitting sentences.
        self.split_gap_seconds = split_gap_seconds
        #: RMS floor for the whole-channel gate. A genuine power threshold, unlike Silero's,
        #: and the only one that decides whether a model runs at all.
        self.silence_floor = silence_floor
        #: Ceiling on a merged region. Must stay under the worker's max_audio_seconds, which
        #: refuses anything longer -- and a refused region is audio nobody ever transcribes.
        self.max_region_seconds = max_region_seconds

    @property
    def enabled(self) -> bool:
        """Whether a submission could be accepted right now."""
        return bool(self.worker is not None and getattr(self.worker, "available", False))

    def clear_scratch(self, recording_path: str | Path) -> int:
        """
        Remove working directories left behind by a previous run. Returns how many.

        Called at startup, where any surviving workspace is by definition orphaned -- nothing
        can be transcribing in a process that has only just begun. Without it, a crash or a
        kill between creating a workspace and finishing with it leaks the region files forever,
        and they are the same size as the recording they came from.
        """
        scratch = Path(recording_path) / SCRATCH_DIRNAME
        if not scratch.is_dir():
            return 0

        removed = 0
        for entry in scratch.iterdir():
            try:
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink()
                removed += 1
            except OSError as e:
                self.logger.warning(f"Could not remove stale transcription scratch {entry}: {e}")

        if removed:
            self.logger.info(f"Removed {removed} transcription workspace(s) left by a previous run")
        return removed

    # ---------------------------------------------------------------- submission

    def submit(self, media_path: Path, recording_id: Any = None) -> bool:
        """
        Queue a finished recording for transcription. Returns whether anything was accepted.

        Never raises: this is called from the recording's completion callback, which runs on
        the tap's drain thread at the end of a call. A transcription that cannot start is
        worth a log line, not a lost recording.
        """
        if not self.enabled:
            return False

        try:
            return self._submit(media_path, recording_id)
        except Exception as e:
            self.logger.error(f"Could not transcribe recording {media_path}: {e}")
            return False

    def _submit(self, media_path: Path, recording_id: Any = None) -> bool:
        channels = self._channel_labels(media_path, recording_id)
        if not channels:
            self.logger.debug(f"No channel manifest for {media_path}; not transcribing")
            return False

        scratch = media_path.parent / SCRATCH_DIRNAME
        scratch.mkdir(parents=True, exist_ok=True)
        workspace = Path(tempfile.mkdtemp(prefix="job-", dir=str(scratch)))
        pending = _PendingTranscription(
            session_id=self._session_id(media_path),
            media_path=media_path,
            workspace=workspace,
            recording_id=recording_id,
        )

        regions = self._split(media_path, channels, workspace)
        if not regions:
            shutil.rmtree(workspace, ignore_errors=True)
            self.logger.info(
                f"Recording {media_path.name}: every channel was silent; nothing transcribed"
            )
            return False

        # Counted up front, before anything is submitted. Incrementing as each job is accepted
        # would let an early completion see expected==0 and merge a partial transcript.
        pending.expected = len(regions)
        accepted = 0

        worker = self.worker
        if worker is None:  # pragma: no cover - enabled already checked this
            return False

        for region in regions:
            if worker.submit(
                region.path,
                # partial rather than a lambda with a default argument: the callback takes
                # only the transcript, and this is the one shape mypy can infer.
                partial(self._on_region, pending, region),
                label=f"{media_path.stem}:{region.speaker}@{region.offset:.0f}s",
                audio_seconds=region.seconds,
            ):
                accepted += 1
            else:
                # Refused -- queue full, over the length cap, service stopping. Its callback
                # will never fire, so release the slot here or the merge never happens.
                self._on_region(pending, region, None)

        if accepted == 0:
            self.logger.warning(f"Recording {media_path.name}: no region was accepted")
            return False

        speakers = {region.speaker for region in regions}
        self.logger.info(
            f"Recording {media_path.name}: transcribing {accepted} region(s) "
            f"across {len(speakers)} speaker(s)"
        )
        return True

    # ---------------------------------------------------------------- splitting

    def _prepend_notice(self, pending: _PendingTranscription, merged: Transcript) -> Transcript:
        """
        Put the recording notice at the front of the transcript, verbatim.

        The notice was deliberately not transcribed -- see the skip in ``_measure`` -- so its
        text comes from the manifest, which recorded what was actually played rather than what
        a model thought it heard. A recording whose channels show no notice gets nothing added.
        """
        if not self.notice_text:
            return merged
        if SYSTEM_LABEL not in self._channel_labels(pending.media_path, pending.recording_id):
            return merged

        notice = Segment(text=self.notice_text, start=0.0, end=0.0, speaker=SYSTEM_LABEL)
        segments = (notice, *merged.segments)
        return replace(merged, segments=segments, text=format_dialogue(segments))

    def _channel_labels(self, media_path: Path, recording_id: Any = None) -> list[str]:
        """
        Speaker labels in channel order.

        The recording row is authoritative -- the sidecar is a disposable convenience artifact
        now, kept so a ``.wav`` moved to another machine is still attributable. Falling back to
        it means a database hiccup costs accuracy of attribution, not the whole transcript.
        """
        if recording_id is not None and self.recordings is not None:
            row = self.recordings.get(recording_id)
            channels = (row or {}).get("channels") or []
            labels = [str(c.get("label", "")) for c in channels if isinstance(c, dict)]
            if labels:
                return labels

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

    def _split(self, media_path: Path, labels: list[str], workspace: Path) -> list[_Region]:
        """
        Write each stretch of each participant's speech to its own mono WAV.

        Two cuts happen here, for two different reasons. A channel with nothing above the
        noise floor is dropped entirely, so no model runs on silence. A channel that does
        contain speech is then cut at gaps long enough to be turn boundaries, because a model
        will not split a segment across silence it never receives -- and on a per-participant
        recording that silence is the other person's whole turn.

        Cutting is kept rare on purpose: every cut costs the model context it uses for
        punctuation and capitalisation, so only unambiguous gaps qualify and continuous
        speech is never broken however long it runs.

        Two passes over the file, neither of which holds it. The first reduces each channel
        to one energy figure per 20 ms -- about 400 KB per channel per hour -- and decides
        where the cuts go. The second seeks to each region and copies only that span out.
        Holding whole channels instead would be ~19 MB per ten-minute call, and this runs on
        the teardown thread, so every call ending at the same moment would pay it at once.
        """
        with wave.open(str(media_path), "rb") as source:
            channels = source.getnchannels()
            rate = source.getframerate()
            width = source.getsampwidth()
            total_frames = source.getnframes()
            if width != 2:
                self.logger.warning(f"{media_path.name}: expected PCM16, got {width * 8}-bit")
                return []

            spans = self._measure(source, channels, rate, media_path, labels)
            # Only somebody else speaking justifies a cut. Rejoining the rest keeps context
            # for the model and, because whisper pays a full 30-second window per region,
            # keeps the cost proportional to the audio rather than to the number of pauses.
            before = sum(len(found) for found in spans.values())
            spans = merge_uninterrupted(spans, self.max_region_seconds)
            after = sum(len(found) for found in spans.values())
            if after < before:
                self.logger.debug(
                    f"{media_path.name}: rejoined {before} region(s) into {after} "
                    "across gaps nobody spoke in"
                )
            return self._extract(source, spans, labels, channels, rate, total_frames, workspace)

    def _measure(
        self,
        source: wave.Wave_read,
        channels: int,
        rate: int,
        media_path: Path,
        labels: list[str],
    ) -> dict[int, list[tuple[float, float]]]:
        """Stream the file once, reducing each channel to frame energies, and pick regions."""
        per_channel: list[list[np.ndarray]] = [[] for _ in range(channels)]

        source.rewind()
        while True:
            raw = source.readframes(_SPLIT_CHUNK)
            if not raw:
                break
            block = np.frombuffer(raw, dtype="<i2").reshape(-1, channels)
            for index in range(channels):
                per_channel[index].append(frame_rms(block[:, index].tobytes()))

        seconds = frame_seconds(rate)
        spans: dict[int, list[tuple[float, float]]] = {}

        for index, pieces in enumerate(per_channel):
            speaker = labels[index] if index < len(labels) else f"channel{index}"

            # The recording notice is never recognised. We already have its exact wording --
            # it is the configured string that generated the audio -- so running a model over
            # it would spend a whole encoder window to recover a sentence we hold, and could
            # return it paraphrased. That is the last sentence worth approximating if the
            # recording is ever evidence. Its text is injected in _finish() instead.
            if speaker == SYSTEM_LABEL:
                continue

            rms = np.concatenate(pieces) if pieces else np.zeros(0)

            # Derived from the same frames rather than re-reading: a channel nobody spoke on
            # must never reach a model.
            speech = float(np.count_nonzero(rms > self.silence_floor) * seconds)
            if speech < self.min_speech_seconds:
                self.logger.debug(
                    f"{media_path.name}: {speaker} has {speech:.1f}s of speech; skipping"
                )
                continue

            found = regions_from_rms(
                rms,
                seconds_per_frame=seconds,
                duration=rms.size * seconds,
                floor=self.silence_floor,
                min_gap_seconds=self.split_gap_seconds,
                min_region_seconds=self.min_speech_seconds,
            )
            if found:
                spans[index] = found
                self.logger.debug(f"{media_path.name}: {speaker} split into {len(found)} region(s)")

        return spans

    def _extract(
        self,
        source: wave.Wave_read,
        spans: dict[int, list[tuple[float, float]]],
        labels: list[str],
        channels: int,
        rate: int,
        total_frames: int,
        workspace: Path,
    ) -> list[_Region]:
        """Copy each region out by seeking to it, so only one region is in memory at a time."""
        regions: list[_Region] = []

        for index, found in spans.items():
            speaker = labels[index] if index < len(labels) else f"channel{index}"
            for number, (start, end) in enumerate(found):
                first = max(0, int(start * rate))
                last = min(total_frames, int(end * rate))
                if last <= first:
                    continue

                path = workspace / f"{_safe(speaker)}-{number}.wav"
                try:
                    written = self._copy_span(source, index, channels, first, last, rate, path)
                except (OSError, wave.Error) as e:
                    self.logger.error(f"Could not write region {number} for {speaker}: {e}")
                    continue

                if written:
                    regions.append(
                        _Region(speaker=speaker, path=path, offset=start, seconds=written / rate)
                    )

        return regions

    @staticmethod
    def _copy_span(
        source: wave.Wave_read,
        channel: int,
        channels: int,
        first: int,
        last: int,
        rate: int,
        path: Path,
    ) -> int:
        """Write one channel's samples between two frame positions. Returns frames written."""
        source.setpos(first)
        remaining = last - first
        written = 0

        with wave.open(str(path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(rate)
            while remaining > 0:
                raw = source.readframes(min(_SPLIT_CHUNK, remaining))
                if not raw:
                    break
                block = np.frombuffer(raw, dtype="<i2").reshape(-1, channels)
                out.writeframes(block[:, channel].tobytes())
                written += block.shape[0]
                remaining -= block.shape[0]

        return written

    # ---------------------------------------------------------------- completion

    def _on_region(
        self, pending: _PendingTranscription, region: _Region, transcript: Transcript | None
    ) -> None:
        """One region finished. Merges and stores when it was the last one."""
        try:
            if not pending.record(region, transcript):
                return
            self._finish(pending)
        except Exception as e:
            self.logger.error(f"Transcription merge for {pending.session_id} failed: {e}")
        finally:
            if pending.finished:
                shutil.rmtree(pending.workspace, ignore_errors=True)

    def _finish(self, pending: _PendingTranscription) -> None:
        """Merge every region of every speaker into one transcript and store it."""
        if not pending.results:
            self.logger.info(f"Recording {pending.media_path.name}: no speech recognised")
            return

        per_speaker = {
            speaker: combine_regions(entries) for speaker, entries in pending.results.items()
        }
        merged = merge_channels(per_speaker)
        merged = self._prepend_notice(pending, merged)
        # The speakers are the participants: each region was cut from one participant's
        # channel, so the keys here are exactly the labels the recording manifest named.
        # Stored on the row because the manifest expires with the audio, long before the
        # transcript does, and retention still has to know whose call this was.
        transcript_id = self.store.save(
            merged,
            source=SOURCE_RECORDING,
            recording_id=pending.recording_id,
            session_id=pending.session_id,
        )

        if transcript_id is not None and self.on_transcribed is not None:
            # Guarded: an analysis failure must not lose the transcript that just succeeded.
            try:
                self.on_transcribed(pending.recording_id, transcript_id)
            except Exception as e:
                self.logger.error(f"Analysis after transcription of {pending.session_id}: {e}")
        self.logger.info(
            f"Transcribed {pending.media_path.name}: "
            f"{len(merged.segments)} segment(s) across {len(pending.results)} speaker(s)"
        )


def combine_regions(entries: list[tuple[float, Transcript]]) -> Transcript:
    """
    Put one speaker's regions back onto the call's timeline as a single transcript.

    Each region was transcribed as its own file, so every timestamp it returned is relative
    to that region's start. Adding the offset is what makes the times comparable again --
    both against this speaker's other regions and against everybody else's.
    """
    entries = sorted(entries, key=lambda entry: entry[0])

    segments: list[Segment] = []
    for offset, transcript in entries:
        segments.extend(
            replace(segment, start=segment.start + offset, end=segment.end + offset)
            for segment in transcript.segments
        )

    first = entries[0][1]
    return Transcript(
        text=" ".join(t.text.strip() for _, t in entries if t.text.strip()),
        segments=tuple(segments),
        language=first.language,
        provider=first.provider,
        model=first.model,
        confidence=None,
        # The end of the last region, not the sum of their lengths: the silence between them
        # is part of the call even though none of it was transcribed.
        audio_duration=max((offset + t.audio_duration for offset, t in entries), default=0.0),
        processing_duration=sum(t.processing_duration for _, t in entries),
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
