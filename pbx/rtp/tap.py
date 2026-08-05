"""
Capturing relayed audio without touching the relay.

This is the primitive every recording and transcription feature has been missing.
``RTPRelayHandler._relay_loop`` receives a packet, works out which leg it came from, forwards
it, and then forgets it. Nothing in this PBX has ever kept a copy, which is why
``CallRecordingSystem.add_audio`` has no feeder and ``recordings/`` has always been empty.

The tap sits in the slot that already exists for this: *after* ``sock.sendto``, alongside the
QoS accounting. Forwarding is finished by the time we are called, so nothing here can delay a
packet on its way to the far end.

Three rules, and they are the whole design:

1. **No work on the relay thread.** :meth:`AudioTap.feed` appends the packet to a queue and
   returns. It does not parse, decode, or write. There is one relay thread per active call and
   it has a 20 ms cadence to keep; a disk write in that loop is audible jitter on a live call.
2. **Drop, never block.** A full queue discards the packet and counts it. Losing recording
   bytes is survivable. Stalling the relay is not, and an unbounded queue just moves the
   failure to memory.
3. **Every contributor kept apart.** The relay has already worked out where the packet came
   from. That classification is free speaker separation and is passed straight through -- it
   is what lets a recording hold one channel per participant and a transcript say who spoke.

**The source is an opaque string, deliberately.** It is not "which side of the bridge"; it is
"which contributor", and the two stop being the same thing the moment a call is transferred.
``RTPRelayHandler`` replaces one side's endpoint with an entirely different person on a
transfer (``replace_endpoint``), so a source that meant "side b" would put two people on one
channel with nothing marking the handover -- output that looks correctly attributed and is
not. The handler therefore emits ``a0``, ``b0``, ``b1`` and so on, and anything downstream
treats a new string as a new participant. A mixer with N ports can feed the same tap by
passing its own port ids.

**Paused relays produce a gap.** ``_relay_loop`` drops packets while ``self.paused`` is set,
which is how music-on-hold takes over the socket. The tap sits after that check, so a recording
has silence exactly where the two parties were not hearing each other. That is correct -- there
was no conversation to record -- but it looks like lost audio if you do not know why.
"""

from __future__ import annotations

import queue
import struct
import threading
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pbx.utils.logger import get_logger

__all__ = ["AudioTap", "RtpFrame", "TapSink", "parse_rtp_packet"]

#: Packets buffered per tap before we start dropping. At 50 packets/second/direction this is
#: roughly five seconds of both legs -- long enough to ride out a slow disk, short enough that
#: a wedged sink cannot eat memory. The bound *is* the backpressure; there is no other.
DEFAULT_QUEUE_SIZE = 500

#: How long the drain thread blocks on the queue before re-checking whether it should exit.
_POLL_INTERVAL = 0.5

#: Minimum RTP header, per RFC 3550 section 5.1.
_RTP_HEADER_BYTES = 12

_EXTENSION_BIT = 0x10
_PADDING_BIT = 0x20
_CSRC_COUNT_MASK = 0x0F
_PAYLOAD_TYPE_MASK = 0x7F


@dataclass(frozen=True, slots=True)
class RtpFrame:
    """One packet's worth of audio, with the header already taken off."""

    #: Which contributor it came *from* -- the speaker, not the destination. Opaque: "a0",
    #: "b1", a mixer port id. A value not seen before is a new participant.
    source: str
    #: RTP timestamp, in samples at the codec's clock rate. This is what keeps the two
    #: channels aligned: it says *when* in the call these samples belong, which arrival order
    #: alone cannot, because silence suppression and packet loss leave holes.
    timestamp: int
    #: RTP payload type. 0 is G.711 µ-law, 8 is A-law.
    payload_type: int
    #: Sequence number, for detecting loss within a leg.
    sequence: int
    #: The encoded audio itself.
    payload: bytes


@runtime_checkable
class TapSink(Protocol):
    """Something that consumes tapped audio. Called only from the drain thread."""

    def write(self, frame: RtpFrame) -> None:
        """Accept one frame. Runs off the media path, so this may do real work."""
        ...

    def close(self) -> None:
        """Release whatever this sink holds. Called once, when the tap stops."""
        ...


def parse_rtp_packet(data: bytes, source: str = "") -> RtpFrame | None:
    """
    Pull the audio out of one RTP packet, or return None if it is not usable.

    Handles the parts of the header that are variable-length -- CSRC list, extension, padding
    -- rather than assuming 12 bytes. Most phones send exactly 12, but a packet that has been
    through a conference bridge or an SRTP gateway may not, and slicing at a fixed offset would
    put a few bytes of header into the audio as a click.
    """
    if len(data) < _RTP_HEADER_BYTES:
        return None

    first, second, sequence, timestamp, _ssrc = struct.unpack("!BBHII", data[:_RTP_HEADER_BYTES])

    header_length = _RTP_HEADER_BYTES + 4 * (first & _CSRC_COUNT_MASK)

    if first & _EXTENSION_BIT:
        # Extension header is 4 bytes of (profile, length-in-32-bit-words) then the body.
        if len(data) < header_length + 4:
            return None
        (extension_words,) = struct.unpack("!H", data[header_length + 2 : header_length + 4])
        header_length += 4 + 4 * extension_words

    payload = data[header_length:]

    if first & _PADDING_BIT and payload:
        # The final byte counts the padding bytes, itself included.
        padding = payload[-1]
        if 0 < padding <= len(payload):
            payload = payload[:-padding]

    if not payload:
        return None

    return RtpFrame(
        source=source,
        timestamp=timestamp,
        payload_type=second & _PAYLOAD_TYPE_MASK,
        sequence=sequence,
        payload=payload,
    )


class AudioTap:
    """
    Copies relayed RTP off the media path and onto a background thread.

    Constructed by whatever wants the audio, attached to a relay handler, and detached when it
    is done. The handler holds at most one, and knows nothing about it beyond calling
    :meth:`feed`.
    """

    def __init__(
        self,
        call_id: str,
        sink: TapSink,
        *,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        logger: Any | None = None,
    ) -> None:
        self.call_id = call_id
        self.sink = sink
        self.logger = logger or get_logger()

        self._queue: queue.Queue[tuple[str, bytes]] = queue.Queue(maxsize=max(1, queue_size))
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

        self._fed = 0
        self._dropped = 0
        self._written = 0
        self._malformed = 0

    # ---------------------------------------------------------------- media path

    def feed(self, source: str, data: bytes) -> None:
        """
        Hand one packet to the tap. **Called from the relay thread.**

        Does the least possible: one queue append. Nothing here parses, decodes or writes, and
        nothing here raises -- an exception on the relay thread would be logged once per packet
        and take the call's audio down with it.

        `source` identifies the contributor, not the direction. See the module docstring.
        """
        try:
            self._queue.put_nowait((source, data))
        except queue.Full:
            self._dropped += 1
        except Exception:
            # Genuinely unreachable, and that is exactly why it is caught: the cost of being
            # wrong is a broken call, and the cost of the guard is nothing.
            self._dropped += 1
        else:
            self._fed += 1

    # ---------------------------------------------------------------- lifecycle

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stop.is_set()

    def start(self) -> None:
        """Begin draining. Calling this twice is a no-op rather than a second thread."""
        if self.running:
            return

        self._stop.clear()
        self._thread = threading.Thread(
            target=self._drain_loop, name=f"tap-{self.call_id}", daemon=True
        )
        self._thread.start()
        self.logger.debug(f"Audio tap started for call {self.call_id}")

    def stop(self, timeout: float = 5.0) -> None:
        """
        Stop draining, flush what is still queued, and close the sink.

        The flush matters: the last second or so of a call is sitting in the queue when the
        call ends, and it is usually the part where somebody says goodbye.
        """
        thread = self._thread
        self._stop.set()

        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                self.logger.warning(
                    f"Audio tap for {self.call_id} did not stop within {timeout:.0f}s"
                )
        self._thread = None

        self._flush()

        try:
            self.sink.close()
        except Exception as e:
            self.logger.error(f"Audio tap sink for {self.call_id} failed to close: {e}")

        if self._dropped:
            self.logger.warning(
                f"Audio tap for {self.call_id} dropped {self._dropped} packet(s): "
                "the sink could not keep up with the relay"
            )
        self.logger.debug(
            f"Audio tap stopped for call {self.call_id} "
            f"({self._written} frame(s) written, {self._dropped} dropped)"
        )

    # ---------------------------------------------------------------- drain thread

    def _drain_loop(self) -> None:
        """Pull packets and hand them to the sink until asked to stop."""
        while True:
            try:
                source, data = self._queue.get(timeout=_POLL_INTERVAL)
            except queue.Empty:
                if self._stop.is_set():
                    return
                continue

            self._deliver(source, data)

    def _flush(self) -> None:
        """Deliver whatever is left in the queue. Called after the thread has stopped."""
        while True:
            try:
                source, data = self._queue.get_nowait()
            except queue.Empty:
                return
            self._deliver(source, data)

    def _deliver(self, source: str, data: bytes) -> None:
        """Parse one packet and give it to the sink. Never raises."""
        frame = parse_rtp_packet(data, source)
        if frame is None:
            self._malformed += 1
            return

        try:
            self.sink.write(frame)
        except Exception as e:
            # A failing sink must not kill the drain thread; the call is still up and the
            # next packet may well succeed.
            self.logger.error(f"Audio tap sink for {self.call_id} raised: {e}")
        else:
            self._written += 1

    # ---------------------------------------------------------------- reporting

    def stats(self) -> dict[str, Any]:
        """Counters, for logging and the status API."""
        return {
            "call_id": self.call_id,
            "running": self.running,
            "fed": self._fed,
            "written": self._written,
            "dropped": self._dropped,
            "malformed": self._malformed,
            "queue_depth": self._queue.qsize(),
            "queue_capacity": self._queue.maxsize,
        }
