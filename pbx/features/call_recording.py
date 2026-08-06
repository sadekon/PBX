"""
Recording calls to one WAV channel per participant.

``features.call_recording`` has been a switch that does nothing. ``CallRecordingSystem`` was
constructed at startup, read its config flag into ``auto_record``, and then never recorded
anything, because ``add_audio`` had no feeder anywhere in the codebase. ``recordings/`` has
always been empty. The RTP tap (``pbx/rtp/tap.py``) is the missing feeder; this is what it
feeds.

**One channel per participant, never a mix.** The tap says which contributor each packet came
from, so speaker separation costs nothing here -- and it is most of what makes a transcript
worth reading, since "who said that" is not recoverable from mixed audio without a diarisation
model. Channels open the first time a source is heard, so two is only the common case: a
transfer adds one part-way through, and a conference has as many as it has participants. No
mixing happens on the media path either, which keeps the relay loop as cheap as it was.

Because a multi-channel WAV carries no channel names, every recording writes a ``.json``
sidecar saying which channel holds whom. Without it the extra channels are unattributable and
the separation is wasted.

**Participants, not sides.** A transfer replaces one side of the relay with a different human.
Keying channels on "a"/"b" put both of them on one track with nothing marking the handover --
output that looked correctly attributed and was not. The handler now stamps a generation onto
each source (``b0`` becomes ``b1``), so the replacement is simply somebody new.

**Everything is decoded to PCM16 at 8 kHz.** Legs negotiate independently, so a call can be
µ-law one way and G.722 the other; there is no single wire format to preserve. One decode path
means mismatched codecs stop being a special case, the file plays in anything, and the
transcriber gets what it wants without a second conversion. It costs about twice the size of
µ-law -- roughly 19 MB per ten minutes per participant -- which retention sweeps at 90 days.

**Alignment is by RTP timestamp, not arrival order.** Silence suppression and packet loss both
leave holes, so a channel built by concatenating whatever arrived drifts further from real time
the longer the call runs, and by the end the speakers no longer line up. Each packet is placed
where its timestamp says it belongs and gaps are filled with silence, so every channel shares
one timeline and a transcript's segment times mean something.

Timestamps from different sources are *not* comparable -- separate origins, separate random
offsets -- so channels are positioned relative to each other by the arrival time of each
participant's first packet, and within a channel by its own timestamps. That is what makes a
callee who answered five seconds late start five seconds into the file rather than at zero,
and a transfer target start where they actually joined.

**A recording belongs to a session, not a leg.** ``call_id`` names one SIP dialog, and a
transfer or bridge replaces the dialog while the people keep talking. Recordings are keyed on
``Call.session_id``, which is carried across legs, so one conversation stays one file.
"""

from __future__ import annotations

import contextlib
import json
import time
import wave
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from pbx.utils.audio import alaw_to_pcm16, resample_pcm16, ulaw_to_pcm16
from pbx.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from pbx.rtp.tap import RtpFrame

__all__ = ["CallRecording", "CallRecordingSystem"]

#: Everything is written at this rate. Telephony is 8 kHz; G.722's 16 kHz output is
#: downsampled to match rather than upsampling every other call to meet it.
SAMPLE_RATE = 8000

#: RTP clock for every codec we can decode. G.722 is the odd one -- it samples at 16 kHz but
#: RFC 3551 specifies an 8 kHz RTP clock for it, a known historical wart. That works in our
#: favour: one timestamp unit is one output sample for all of them.
RTP_CLOCK_HZ = 8000

#: Payload types we can turn into samples. G.729 (18) and G.726 (2) can be negotiated but
#: have no decoder in this codebase, so a call using them records silence on that leg.
PAYLOAD_ULAW = 0
PAYLOAD_ALAW = 8
PAYLOAD_G722 = 9

#: A jump larger than this is treated as a discontinuity to resync on, not a gap to fill.
#: Hold-and-resume re-INVITEs restart the timestamp series, and filling that "gap" literally
#: would write hours of zeros from one bad packet.
RESYNC_GAP_SECONDS = 60.0

#: Samples handled at a time when interleaving the spools into the final file. Keeps memory
#: flat regardless of call length instead of loading every channel at once.
_INTERLEAVE_CHUNK = 8000

#: Granularity of the cross-participant start offset, in samples (20 ms at 8 kHz).
#:
#: Where a participant starts is derived from the arrival time of their first packet, because
#: RTP timestamps from different sources share no origin. Arrival time is only good to a
#: packet interval -- network jitter alone is milliseconds -- so rounding it to the nearest
#: sample claims 125 µs of precision that does not exist, and makes the result depend on how
#: long the recorder happened to take opening a file. Quantising to whole packets is both
#: honest about the accuracy and deterministic.
LEAD_QUANTUM_SAMPLES = 160

#: Ceiling on participants in one recording. WAV itself allows far more, but every channel
#: costs a file handle and a full-length track in the output, and a source id that changed
#: unexpectedly would otherwise open one per packet. Comfortably above `mixer.max_ports_per_
#: bridge` (8) plus room for transfers.
MAX_CHANNELS = 32

#: Config key that must *also* be true before anything is recorded.
#:
#: ``features.call_recording`` has been true in config.yml for as long as it has existed, and
#: was harmless the whole time because nothing fed the recorder. Wiring the RTP tap turned that
#: dormant flag into "record every call from the next restart", which is not a change anybody
#: opted into by leaving a config file alone.
#:
#: Recording a call without telling the participants is unlawful in two-party-consent
#: jurisdictions. This PBX plays no announcement yet, so the second key is the thing that says
#: an operator decided, rather than inherited, that recording is appropriate here. Remove it
#: once a consent announcement exists and the decision has somewhere better to live.
CONSENT_KEY = "recording.consent_acknowledged"


class _Channel:
    """
    One leg's audio, spooled to its own file while the call runs.

    Two mono spools rather than one interleaved file because the legs arrive independently:
    you cannot write an interleaved stereo frame until you have both sides of that instant,
    and waiting for the other side is what a jitter buffer is for. Interleaving happens once,
    at the end, streaming.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file = path.open("wb")
        #: Samples written so far. This *is* the channel's write position on the timeline.
        self.written = 0
        #: RTP timestamp treated as this channel's origin, and the sample offset it maps to.
        self._reference_timestamp: int | None = None
        self._reference_sample = 0
        #: Kept across packets: G.722 decoding is stateful, so one codec per leg.
        self._g722: Any | None = None
        self.late_packets = 0
        self.undecodable_packets = 0

    @property
    def started(self) -> bool:
        """Whether this channel has an origin yet, i.e. has seen a decodable packet."""
        return self._reference_timestamp is not None

    def begin(self, timestamp: int, lead_samples: int) -> None:
        """Set the channel's origin, and pad the silence before its first packet."""
        self._reference_timestamp = timestamp
        self._reference_sample = lead_samples
        self._pad(lead_samples - self.written)

    def add(self, timestamp: int, samples: np.ndarray) -> None:
        """Place `samples` where `timestamp` says they belong."""
        if self._reference_timestamp is None:
            self.begin(timestamp, 0)

        assert self._reference_timestamp is not None
        # Signed 32-bit difference, so a wrapped timestamp reads as a small delta rather
        # than a four-billion-sample gap.
        elapsed = (timestamp - self._reference_timestamp) & 0xFFFFFFFF
        if elapsed > 0x7FFFFFFF:
            elapsed -= 0x100000000

        target = self._reference_sample + elapsed
        gap = target - self.written

        if gap < 0:
            # Arrived after its slot was already written past. Re-recording it would mean
            # seeking backwards over silence we have committed to; the packet is worth less
            # than the complexity.
            self.late_packets += 1
            return

        if gap > RESYNC_GAP_SECONDS * SAMPLE_RATE:
            # Not a real gap: the far end restarted its timestamp series. Treat this packet
            # as the new origin and carry on from where we are.
            self.begin(timestamp, self.written)
            gap = 0

        self._pad(gap)
        self._file.write(samples.astype("<i2").tobytes())
        self.written += len(samples)

    def _pad(self, count: int) -> None:
        """Write `count` samples of silence."""
        if count <= 0:
            return
        self._file.write(np.zeros(count, dtype="<i2").tobytes())
        self.written += count

    def decode(self, payload: bytes, payload_type: int) -> np.ndarray | None:
        """Turn one payload into int16 samples at SAMPLE_RATE, or None if we cannot."""
        if payload_type == PAYLOAD_ULAW:
            return ulaw_to_pcm16(payload)
        if payload_type == PAYLOAD_ALAW:
            return alaw_to_pcm16(payload)
        if payload_type == PAYLOAD_G722:
            return self._decode_g722(payload)

        self.undecodable_packets += 1
        return None

    def _decode_g722(self, payload: bytes) -> np.ndarray | None:
        """G.722 decodes to 16 kHz, so it is downsampled to match everything else."""
        if self._g722 is None:
            from pbx.features.g722_codec import G722Codec

            self._g722 = G722Codec(bitrate=64000)

        decoded = self._g722.decode(payload)
        if not decoded:
            self.undecodable_packets += 1
            return None

        return np.frombuffer(resample_pcm16(decoded, 16000, SAMPLE_RATE), dtype="<i2")

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._file.close()

    def discard(self) -> None:
        """Close and delete the spool, for the paths where no file is produced."""
        self.close()
        with contextlib.suppress(OSError):
            self.path.unlink()


class CallRecording:
    """
    Records one call. Implements the ``TapSink`` protocol, so an ``AudioTap`` drives it.

    Every method except :meth:`start` and :meth:`stop` runs on the tap's drain thread, off
    the media path, which is why decoding and disk writes are allowed to happen here at all.
    """

    def __init__(
        self,
        call_id: str,
        recording_path: str = "recordings",
        logger: Any | None = None,
        on_finished: Callable[[CallRecording, Path | None], None] | None = None,
        session_id: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> None:
        self.call_id = call_id
        #: The conversation this belongs to, which outlives any one leg. Transfers and
        #: bridges hand a call off between legs with different call_ids; the session is what
        #: makes them one recording and one transcript.
        self.session_id = session_id or call_id
        self.recording_path = recording_path
        self.logger = logger or get_logger()
        #: Human names for sources, e.g. {"a0": "1001", "b1": "1003"}. Optional -- an
        #: unlabelled source still gets its own channel, which is the part that matters.
        self.labels: dict[str, str] = dict(labels or {})
        #: Called once when the file is finished, however it finished. The tap normally ends
        #: a recording -- the relay stopping detaches it -- so the owning system learns about
        #: it here rather than by being told separately.
        self.on_finished = on_finished

        self.recording = False
        self.file_path: Path | None = None
        self.start_time: datetime | None = None
        self.end_time: datetime | None = None

        #: Insertion-ordered, so a channel's index in the WAV is the order it was first
        #: heard. That ordering is what the manifest records.
        self._channels: dict[str, _Channel] = {}
        self._first_arrival: float | None = None
        self._channel_limit_warned = False

        Path(recording_path).mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- lifecycle

    def start(self, from_ext: str, to_ext: str) -> Path | None:
        """Choose the output file and open the spools. Returns None if already recording."""
        if self.recording:
            return None

        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        safe_session = "".join(c for c in self.session_id if c.isalnum() or c in "-_")[:40]
        filename = f"{from_ext}_to_{to_ext}_{timestamp}_{safe_session}.wav"
        self.file_path = Path(self.recording_path) / filename

        # Channels are opened on first hearing a source rather than up front. Two is only
        # the common case: a conference has as many as it has participants, and a transfer
        # adds one part-way through.
        self.recording = True
        self.start_time = datetime.now(UTC)
        self.logger.info(f"Recording call {self.call_id} to {self.file_path}")
        return self.file_path

    # ---------------------------------------------------------------- TapSink

    def write(self, frame: RtpFrame) -> None:
        """Accept one tapped packet. Called on the tap's drain thread."""
        if not self.recording:
            return

        channel = self._channel_for(frame.source)
        if channel is None:
            return

        samples = channel.decode(frame.payload, frame.payload_type)
        if samples is None or not len(samples):
            return

        arrival = time.monotonic()
        if self._first_arrival is None:
            self._first_arrival = arrival

        if not channel.started:
            # How far into the call this participant started speaking. A callee who answered
            # five seconds in belongs five seconds into the file, not at zero. Quantised to
            # whole packets -- see LEAD_QUANTUM_SAMPLES.
            elapsed = max(0.0, arrival - self._first_arrival)
            packets = round(elapsed * SAMPLE_RATE / LEAD_QUANTUM_SAMPLES)
            channel.begin(frame.timestamp, packets * LEAD_QUANTUM_SAMPLES)

        channel.add(frame.timestamp, samples)

    def _channel_for(self, source: str) -> _Channel | None:
        """
        The channel for `source`, opening one the first time it is heard.

        A source that has not been seen before is a new participant -- someone joining a
        conference, or the party a transfer just put on the line. They get their own channel
        rather than being appended to somebody else's.
        """
        channel = self._channels.get(source)
        if channel is not None:
            return channel

        if self.file_path is None:
            return None

        if len(self._channels) >= MAX_CHANNELS:
            # A runaway source id would otherwise open a file handle per packet.
            if not self._channel_limit_warned:
                self._channel_limit_warned = True
                self.logger.warning(
                    f"Recording {self.call_id} hit the {MAX_CHANNELS}-channel limit; "
                    f"audio from {source} onwards is not being recorded"
                )
            return None

        try:
            channel = _Channel(self.file_path.with_suffix(f".{source}.pcm"))
        except OSError as e:
            self.logger.error(f"Could not open recording spool for {source}: {e}")
            return None

        self._channels[source] = channel
        who = self.labels.get(source, source)
        self.logger.info(f"Recording {self.call_id}: channel {len(self._channels) - 1} is {who}")
        return channel

    def label(self, source: str, name: str) -> None:
        """Give a source a human name. Safe at any time; affects only the manifest."""
        self.labels[source] = name

    def close(self) -> None:
        """Finish the file. Called once by the tap when it stops."""
        self.stop()

    def stop(self) -> Path | None:
        """Close the spools, interleave them into the WAV, and return its path."""
        if not self.recording:
            return None

        self.recording = False
        self.end_time = datetime.now(UTC)

        for channel in self._channels.values():
            channel.close()

        path = self.file_path
        if path is None:
            return None

        try:
            frames = self._interleave(path)
        except (OSError, ValueError, wave.Error) as e:
            self.logger.error(f"Could not write recording {path}: {e}")
            self._discard()
            self._finished(None)
            return None

        self._report()
        self._remove_spools()

        if frames == 0:
            # Nothing was ever tapped -- a call that never carried audio. An empty WAV is
            # worse than no WAV: retention would keep it and a transcriber would open it.
            with contextlib.suppress(OSError):
                path.unlink()
            self.logger.info(f"Recording for {self.call_id} had no audio; nothing written")
            self._finished(None)
            return None

        self._write_manifest(path, frames)
        self.logger.info(
            f"Saved recording {path} "
            f"({frames / SAMPLE_RATE:.1f}s, {len(self._channels)} channel(s))"
        )
        self._finished(path)
        return path

    def _finished(self, path: Path | None) -> None:
        """Tell the owner this recording is done. Never raises into the tap's drain thread."""
        if self.on_finished is None:
            return
        callback, self.on_finished = self.on_finished, None
        try:
            callback(self, path)
        except Exception as e:
            self.logger.error(f"Recording completion callback for {self.call_id} raised: {e}")

    # ---------------------------------------------------------------- writing

    def _interleave(self, path: Path) -> int:
        """
        Stream every spool into one multi-channel WAV. Returns the frame count written.

        One channel per participant, in the order they were first heard. Chunked rather than
        loaded whole: a long call would otherwise be held in memory once per participant, at
        exactly the moment several calls tend to end together.
        """
        channels = list(self._channels.values())
        if not channels:
            return 0

        total = max((c.written for c in channels), default=0)
        if total == 0:
            return 0

        with contextlib.ExitStack() as stack:
            out = stack.enter_context(wave.open(str(path), "wb"))
            spools = [stack.enter_context(c.path.open("rb")) for c in channels]

            out.setnchannels(len(channels))
            out.setsampwidth(2)
            out.setframerate(SAMPLE_RATE)

            written = 0
            while written < total:
                count = min(_INTERLEAVE_CHUNK, total - written)
                # A participant who joined late or left early has a shorter spool; read()
                # simply returns less and _padded tops it up, so every channel is the same
                # length and the timeline stays shared.
                block = np.empty((count, len(spools)), dtype="<i2")
                for index, spool in enumerate(spools):
                    block[:, index] = _padded(spool.read(count * 2), count)
                out.writeframes(block.tobytes())
                written += count

        return total

    def channel_map(self) -> list[dict[str, Any]]:
        """
        Which channel holds whom, in channel order.

        A multi-channel WAV carries no channel names, so this is the only thing that makes
        per-participant channels usable for attribution. Written beside the audio as a
        sidecar, and what a transcriber reads to know whose words it just produced.
        """
        return [
            {
                "channel": index,
                "source": source,
                "label": self.labels.get(source, source),
                "samples": channel.written,
                "seconds": round(channel.written / SAMPLE_RATE, 3),
            }
            for index, (source, channel) in enumerate(self._channels.items())
        ]

    def _write_manifest(self, path: Path, frames: int) -> None:
        """Write the sidecar describing the channels. Never fatal to the recording."""
        manifest = {
            "session_id": self.session_id,
            "call_id": self.call_id,
            "started_at": self.start_time.isoformat() if self.start_time else None,
            "ended_at": self.end_time.isoformat() if self.end_time else None,
            "sample_rate": SAMPLE_RATE,
            "duration_seconds": round(frames / SAMPLE_RATE, 3),
            "audio": path.name,
            "channels": self.channel_map(),
        }
        try:
            path.with_suffix(".json").write_text(json.dumps(manifest, indent=2))
        except (OSError, TypeError, ValueError) as e:
            self.logger.error(f"Could not write recording manifest for {self.call_id}: {e}")

    def _report(self) -> None:
        """Log anything that made the recording less than complete."""
        for source, channel in self._channels.items():
            who = self.labels.get(source, source)
            if channel.late_packets:
                self.logger.debug(
                    f"Recording {self.call_id} source {who}: "
                    f"{channel.late_packets} packet(s) arrived too late to place"
                )
            if channel.undecodable_packets:
                self.logger.warning(
                    f"Recording {self.call_id} source {who}: "
                    f"{channel.undecodable_packets} packet(s) used a codec with no decoder; "
                    "that participant is silent for those stretches"
                )

    def _remove_spools(self) -> None:
        for channel in self._channels.values():
            with contextlib.suppress(OSError):
                channel.path.unlink()

    def _discard(self) -> None:
        """Give up on this recording and leave nothing behind."""
        self.recording = False
        for channel in self._channels.values():
            channel.discard()
        self._channels.clear()

    # ---------------------------------------------------------------- reporting

    def get_duration(self) -> float:
        """Recorded length in seconds, from the audio itself rather than the wall clock."""
        if self._channels:
            samples = max((c.written for c in self._channels.values()), default=0)
            if samples:
                return samples / SAMPLE_RATE

        if self.start_time:
            return ((self.end_time or datetime.now(UTC)) - self.start_time).total_seconds()
        return 0.0


def _padded(raw: bytes, count: int) -> np.ndarray:
    """`count` int16 samples from `raw`, padded with silence if it is short."""
    samples = np.frombuffer(raw, dtype="<i2")
    if len(samples) == count:
        return samples
    if len(samples) > count:
        return samples[:count]
    out = np.zeros(count, dtype="<i2")
    out[: len(samples)] = samples
    return out


class CallRecordingSystem:
    """
    Owns the recordings in progress.

    Held by ``PBXCore`` as ``recording_system``. ``start_recording`` builds the tap that
    feeds one; the caller attaches it to that call's relay handler.
    """

    def __init__(
        self,
        recording_path: str = "recordings",
        requested: bool = False,
        consent_acknowledged: bool = False,
    ) -> None:
        self.recording_path = recording_path
        #: Requested by ``features.call_recording``.
        #:
        #: Named ``requested`` rather than ``auto_record`` because config.yml had a dead
        #: ``recording.auto_record`` key that nothing read, and the two being spelled the same
        #: made an inert setting look like the switch that works.
        self.requested = requested
        #: Set by ``recording.consent_acknowledged``. See :data:`CONSENT_KEY`.
        self.consent_acknowledged = consent_acknowledged
        self.active_recordings: dict[str, CallRecording] = {}
        self.recording_metadata: list[dict[str, Any]] = []
        self.logger = get_logger()
        #: Called with the finished file whenever a recording completes. How transcription is
        #: started, without this module having to know that transcription exists.
        self.on_recording_finished: Callable[[Path], None] | None = None

        if self.requested and not self.consent_acknowledged:
            self.logger.warning(
                "features.call_recording is enabled but %s is false, so no calls will be "
                "recorded. Calls cannot be recorded without notifying the participants in "
                "two-party-consent jurisdictions, and this PBX plays no announcement yet. "
                "Set %s to true once you have decided that recording is appropriate here.",
                CONSENT_KEY,
                CONSENT_KEY,
            )
        elif self.requested:
            self.logger.info("Call recording is enabled; every answered call will be recorded")

        Path(recording_path).mkdir(parents=True, exist_ok=True)

    @property
    def auto_record(self) -> bool:
        """Whether calls actually get recorded. Both switches must be on."""
        return self.requested and self.consent_acknowledged

    def start_recording(
        self,
        call_id: str,
        from_ext: str,
        to_ext: str,
        session_id: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> Any | None:
        """
        Begin recording a call.

        Returns an :class:`~pbx.rtp.tap.AudioTap` to attach to the call's relay handler, or
        None if recording did not start. The tap is *not* attached here -- this class does
        not know about relays, and the caller already has the handler.

        Recordings are keyed by `session_id` where one is given, so a conversation that moves
        between legs stays one recording rather than becoming several.
        """
        key = session_id or call_id
        if key in self.active_recordings:
            return None

        from pbx.rtp.tap import AudioTap

        recording = CallRecording(
            call_id,
            self.recording_path,
            logger=self.logger,
            on_finished=self._reap,
            session_id=key,
            labels=labels,
        )
        if recording.start(from_ext, to_ext) is None:
            return None

        self.active_recordings[key] = recording
        return AudioTap(key, recording, logger=self.logger)

    def get(self, session_id: str) -> CallRecording | None:
        """The recording in progress for a session, if there is one."""
        return self.active_recordings.get(session_id)

    def label(self, session_id: str, source: str, name: str) -> None:
        """
        Name a participant on a recording in progress.

        Called when a transfer or a conference join puts somebody new on the line, so the
        manifest says who the new channel is rather than just which source produced it.
        """
        recording = self.active_recordings.get(session_id)
        if recording is not None:
            recording.label(source, name)

    def _reap(self, recording: CallRecording, file_path: Path | None) -> None:
        """
        Record that a recording finished, whoever finished it.

        Ending a call stops its relay, which detaches the tap, which closes the recording --
        so this is the usual path, not the exceptional one. Doing the bookkeeping here rather
        than in stop_recording() is what stops active_recordings leaking an entry for every
        call that ended normally.
        """
        self.active_recordings.pop(recording.session_id, None)

        if file_path and self.on_recording_finished is not None:
            # Runs on the tap's drain thread, at the end of a call. Guarded because a
            # transcription problem must not lose the recording that just succeeded.
            try:
                self.on_recording_finished(file_path)
            except Exception as e:
                self.logger.error(f"Recording follow-up for {recording.session_id} failed: {e}")

        if file_path:
            self.recording_metadata.append(
                {
                    "session_id": recording.session_id,
                    "call_id": recording.call_id,
                    "file_path": file_path,
                    "duration": recording.get_duration(),
                    "timestamp": recording.start_time,
                    "channels": recording.channel_map(),
                }
            )

    def stop_recording(self, call_id: str) -> Path | None:
        """
        Finish a recording early, before the call ends.

        Not needed on the normal path: the tap closes the recording when the relay stops.
        This is for stopping one deliberately, and for shutdown.
        """
        recording = self.active_recordings.get(call_id)
        if recording is None:
            return None

        # stop() fires _reap, which removes it from active_recordings.
        return recording.stop()

    def stop_all(self) -> int:
        """Finish every recording in progress. Returns how many were closed."""
        in_progress = list(self.active_recordings.values())
        for recording in in_progress:
            try:
                recording.stop()
            except Exception as e:
                self.logger.error(f"Could not finish recording {recording.call_id}: {e}")

        # _reap normally empties this; clearing covers a recording that failed to stop and
        # would otherwise be held forever.
        self.active_recordings.clear()
        return len(in_progress)

    def is_recording(self, call_id: str) -> bool:
        return call_id in self.active_recordings

    def get_recordings(self, limit: int = 100) -> list[dict[str, Any]]:
        """Metadata for the most recent recordings."""
        return self.recording_metadata[-limit:]
