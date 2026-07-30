"""
N-way audio mixer (capability C3).

The RTP relay carries a call by forwarding packets untouched, which is why it
works with any negotiated codec but can only ever join two parties. Anything
with three or more participants -- conference, three-way calling, and the
supervisor modes -- needs the audio actually summed, which means decoding
every leg, adding the streams, and re-encoding per listener.

The unit of work is a **bridge**: N ports, where each port declares the set of
other ports it hears. That routing matrix is the whole abstraction, and it is
deliberately not hard-coded to "everyone hears everyone minus themselves",
because the supervisor modes are asymmetric:

    silent monitor   supervisor hears both; neither party hears the supervisor
    whisper          the agent also hears the supervisor; the caller does not
    barge            everyone hears everyone -- a three-party conference
    conference       every port hears every other port

:class:`BridgeMode` builders turn each of those into a matrix so features
never hand-roll routing.

Audio is normalised to mono int16 at 8 kHz in 20 ms frames (see
``pbx.rtp.codecs``). One thread per bridge does the mixing on a
deadline-corrected clock; one thread per port receives. Ports whose codec
cannot be transcoded are rejected at ``add_port`` so the caller can
renegotiate the leg to G.711 instead of silently producing noise.
"""

from __future__ import annotations

import contextlib
import random
import socket
import struct
import threading
import time
import uuid
from collections import deque
from typing import TYPE_CHECKING, Any

import numpy as np

from pbx.rtp.codecs import (
    FRAME_MS,
    FRAME_SAMPLES,
    PT_TELEPHONE_EVENT,
    is_supported,
    make_codec,
)
from pbx.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

AddrTuple = tuple[str, int]

#: Seconds per mix tick.
FRAME_SECONDS = FRAME_MS / 1000.0

#: Frames of elasticity per port. One tick of slack absorbs ordinary jitter
#: without the latency of a real jitter buffer; the oldest frame is dropped
#: when a sender runs ahead.
PORT_QUEUE_DEPTH = 3

#: Silence in the mixer's domain, shared because it is never mutated.
_SILENCE = np.zeros(FRAME_SAMPLES, dtype=np.int16)
_SILENCE.setflags(write=False)


class MixPort:
    """
    One participant's media: a socket, a codec pair, and who it listens to.

    Owns its RTP send state (SSRC, sequence, timestamp) because two senders
    sharing one stream produce garbled audio -- the same lesson music-on-hold
    encodes in its one-player-per-relay rule.
    """

    def __init__(
        self,
        port_id: str,
        call_id: str,
        local_port: int,
        remote: AddrTuple | None,
        payload_type: int,
        on_dtmf: Callable[[str, bytes], None] | None = None,
    ) -> None:
        self.port_id = port_id
        self.call_id = call_id
        self.local_port = local_port
        self.remote = remote
        self.payload_type = payload_type
        #: Called with (sender port id, payload) so the bridge can route
        #: telephone-events to whoever hears this port.
        self._on_dtmf = on_dtmf

        #: Port ids whose audio this port hears.
        self.hears: set[str] = set()
        #: A muted port still hears others; it just contributes nothing.
        self.muted = False

        self.logger = get_logger()
        self._decoder = make_codec(payload_type)
        self._encoder = make_codec(payload_type)
        self._frames: deque[np.ndarray] = deque(maxlen=PORT_QUEUE_DEPTH)
        self._lock = threading.Lock()
        #: Payload types already reported as unexpected, so a mismatched
        #: stream logs once rather than fifty times a second.
        self._warned_payload_types: set[int] = set()

        self.socket: socket.socket | None = None
        self.running = False
        self._thread: threading.Thread | None = None

        self._ssrc = random.randint(0, 0xFFFFFFFF)  # Random SSRC per RFC 3550
        self._sequence = 0
        self._timestamp = 0
        self._send_lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> bool:
        """Bind the port and begin receiving. False if the bind fails."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.settimeout(0.5)
            # Bind all interfaces: VoIP servers are routinely multi-homed.
            sock.bind(("0.0.0.0", self.local_port))  # nosec B104
        except OSError as exc:
            self.logger.error(f"Mixer port {self.port_id}: bind {self.local_port} failed: {exc}")
            return False

        self.socket = sock
        self.running = True
        self._thread = threading.Thread(
            target=self._receive_loop, daemon=True, name=f"mixport-{self.port_id}"
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        """Stop receiving and release the socket."""
        self.running = False
        if self.socket is not None:
            with contextlib.suppress(OSError):
                self.socket.close()
            self.socket = None

    # -- receive -----------------------------------------------------------

    def _receive_loop(self) -> None:
        """Decode inbound audio into the frame queue until stopped."""
        while self.running:
            try:
                data, addr = self.socket.recvfrom(2048)  # type: ignore[union-attr]
            except (TimeoutError, OSError):
                continue

            if len(data) < 12:
                continue

            # Symmetric RTP: trust where packets actually come from, which is
            # what the relay does and what survives NAT.
            if self.remote != addr:
                self.remote = addr

            payload_type = data[1] & 0x7F
            payload = data[12:]

            if payload_type == PT_TELEPHONE_EVENT:
                if self._on_dtmf is not None:
                    self._on_dtmf(self.port_id, payload)
                continue
            if payload_type != self.payload_type or self._decoder is None:
                self._warn_unexpected_payload(payload_type)
                continue

            try:
                frame = self._decoder.decode(payload)
            except Exception as exc:
                self.logger.debug(f"Mixer port {self.port_id}: decode failed: {exc}")
                continue

            if frame.size:
                with self._lock:
                    self._frames.append(frame)

    def _warn_unexpected_payload(self, payload_type: int) -> None:
        """
        Report a payload type this port was not opened for, once per type.

        The packet is still dropped -- a mid-call codec change belongs in SDP
        re-negotiation, not inferred from RTP. But dropping it silently means
        a leg that renegotiates goes deaf with nothing in the log to say why,
        so each unexpected type is named the first time it appears.
        """
        if payload_type in self._warned_payload_types:
            return
        self._warned_payload_types.add(payload_type)
        self.logger.warning(
            f"Mixer port {self.port_id} ({self.call_id}): dropping payload type "
            f"{payload_type}, expected {self.payload_type}. If this leg "
            "renegotiated its codec, the bridge port must be rebuilt."
        )

    def take_frame(self) -> np.ndarray:
        """
        The next inbound frame, or silence if none arrived.

        Exactly one frame is consumed per mix tick, so a sender running fast
        drains its backlog rather than building unbounded latency.
        """
        with self._lock:
            if not self._frames:
                return _SILENCE
            frame = self._frames.popleft()
        if frame.size == FRAME_SAMPLES:
            return frame
        # Short or long frame (codec edge case): pad or trim to the domain.
        fitted = np.zeros(FRAME_SAMPLES, dtype=np.int16)
        usable = min(frame.size, FRAME_SAMPLES)
        fitted[:usable] = frame[:usable]
        return fitted

    # -- send --------------------------------------------------------------

    def send_frame(self, frame: np.ndarray) -> None:
        """Encode and send one mixed frame to this participant."""
        if self._encoder is None:
            return
        try:
            payload = self._encoder.encode(frame)
        except Exception as exc:
            self.logger.debug(f"Mixer port {self.port_id}: encode failed: {exc}")
            return
        if payload:
            self._send(payload, self.payload_type, advance=FRAME_SAMPLES)

    def send_dtmf(self, payload: bytes) -> None:
        """
        Relay a telephone-event packet into this port's own RTP stream.

        Re-stamped rather than forwarded verbatim so the participant sees a
        single SSRC. The timestamp deliberately does not advance: every
        packet of one RFC 4733 event shares the event's start time.
        """
        self._send(payload, PT_TELEPHONE_EVENT, advance=0)

    def _send(self, payload: bytes, payload_type: int, advance: int) -> None:
        sock, remote = self.socket, self.remote
        if sock is None or remote is None:
            return

        with self._send_lock:
            header = struct.pack(
                "!BBHII",
                0x80,  # version 2, no padding, no extension, no CSRC
                payload_type & 0x7F,
                self._sequence,
                self._timestamp,
                self._ssrc,
            )
            self._sequence = (self._sequence + 1) & 0xFFFF
            self._timestamp = (self._timestamp + advance) & 0xFFFFFFFF

        with contextlib.suppress(OSError):
            sock.sendto(header + payload, remote)


class BridgeMode:
    """Computes a routing matrix. Subclasses express one calling feature."""

    def matrix(self, port_ids: list[str]) -> dict[str, set[str]]:
        raise NotImplementedError


class ConferenceMode(BridgeMode):
    """Everyone hears everyone else. Also what three-way calling needs."""

    def matrix(self, port_ids: list[str]) -> dict[str, set[str]]:
        return {pid: set(port_ids) - {pid} for pid in port_ids}


class MonitorMode(BridgeMode):
    """
    Supervisor hears the call; the call cannot hear the supervisor.

    The parties keep hearing each other exactly as before, so dropping a
    supervisor in and out is inaudible to them.
    """

    def __init__(self, supervisor: str) -> None:
        self.supervisor = supervisor

    def matrix(self, port_ids: list[str]) -> dict[str, set[str]]:
        parties = [pid for pid in port_ids if pid != self.supervisor]
        routing = {pid: set(parties) - {pid} for pid in parties}
        routing[self.supervisor] = set(parties)
        return routing


class WhisperMode(BridgeMode):
    """
    Supervisor coaches one party; the others never hear the supervisor.

    This asymmetry is why a bridge routes per port rather than mixing
    everyone minus self.
    """

    def __init__(self, supervisor: str, target: str) -> None:
        self.supervisor = supervisor
        self.target = target

    def matrix(self, port_ids: list[str]) -> dict[str, set[str]]:
        parties = [pid for pid in port_ids if pid != self.supervisor]
        routing = {pid: set(parties) - {pid} for pid in parties}
        routing[self.target] = (set(parties) - {self.target}) | {self.supervisor}
        routing[self.supervisor] = set(parties)
        return routing


class BargeMode(ConferenceMode):
    """Supervisor joins openly -- identical to a three-party conference."""


class MixBridge:
    """
    A set of ports mixed together according to their routing matrix.

    One thread produces every port's output on a 20 ms deadline-corrected
    clock. Naive ``sleep(0.02)`` pacing drifts long, which over a call adds
    up to audible slip, so the tick is scheduled against a monotonic
    deadline instead.
    """

    def __init__(self, bridge_id: str, max_ports: int = 8) -> None:
        self.bridge_id = bridge_id
        self.max_ports = max_ports
        self.logger = get_logger()

        self._ports: dict[str, MixPort] = {}
        self._lock = threading.RLock()
        self._running = False
        self._thread: threading.Thread | None = None
        #: Ticks whose work overran the frame budget -- the capacity signal.
        self.late_ticks = 0

    # -- membership --------------------------------------------------------

    def add_port(
        self,
        call_id: str,
        local_port: int,
        remote: AddrTuple | None,
        payload_type: int,
        port_id: str | None = None,
    ) -> str | None:
        """
        Add a participant.

        Returns the new port id, or None if the bridge is full or the codec
        cannot be transcoded -- in which case the caller should renegotiate
        that leg to G.711 and retry rather than treat it as a hard failure.
        """
        if not is_supported(payload_type):
            self.logger.warning(
                f"Bridge {self.bridge_id}: payload type {payload_type} cannot be mixed; "
                f"renegotiate {call_id} to G.711"
            )
            return None

        with self._lock:
            if len(self._ports) >= self.max_ports:
                self.logger.warning(
                    f"Bridge {self.bridge_id}: full ({self.max_ports} ports), refusing {call_id}"
                )
                return None

            pid = port_id or uuid.uuid4().hex[:8]
            port = MixPort(
                pid, call_id, local_port, remote, payload_type, on_dtmf=self._forward_dtmf
            )
            if not port.start():
                return None
            self._ports[pid] = port

        self.logger.info(
            f"Bridge {self.bridge_id}: added port {pid} for {call_id} "
            f"(pt={payload_type}, rtp={local_port})"
        )
        self._ensure_running()
        return pid

    def remove_port(self, port_id: str) -> None:
        """Remove a participant and drop it from every routing set."""
        with self._lock:
            port = self._ports.pop(port_id, None)
            for other in self._ports.values():
                other.hears.discard(port_id)
        if port is not None:
            port.stop()
            self.logger.info(f"Bridge {self.bridge_id}: removed port {port_id}")

    def port_ids(self) -> list[str]:
        """Current members, in insertion order."""
        with self._lock:
            return list(self._ports)

    def get_port(self, port_id: str) -> MixPort | None:
        with self._lock:
            return self._ports.get(port_id)

    # -- routing -----------------------------------------------------------

    def set_hears(self, port_id: str, hears: Iterable[str]) -> None:
        """Set exactly which ports this one hears. The core primitive."""
        with self._lock:
            port = self._ports.get(port_id)
            if port is None:
                return
            port.hears = {pid for pid in hears if pid in self._ports and pid != port_id}

    def apply_mode(self, mode: BridgeMode) -> None:
        """Apply a whole routing matrix at once."""
        with self._lock:
            matrix = mode.matrix(list(self._ports))
            for pid, hears in matrix.items():
                port = self._ports.get(pid)
                if port is not None:
                    port.hears = {other for other in hears if other in self._ports and other != pid}

    def mute(self, port_id: str, muted: bool = True) -> None:
        """Mute a participant's contribution. They still hear the mix."""
        with self._lock:
            port = self._ports.get(port_id)
            if port is not None:
                port.muted = muted

    def routing(self) -> dict[str, set[str]]:
        """Snapshot of who hears whom, for tests and diagnostics."""
        with self._lock:
            return {pid: set(port.hears) for pid, port in self._ports.items()}

    # -- the mix loop ------------------------------------------------------

    def _ensure_running(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._thread = threading.Thread(
                target=self._mix_loop, daemon=True, name=f"mixbridge-{self.bridge_id}"
            )
            self._thread.start()

    def stop(self) -> None:
        """Stop mixing and release every port."""
        with self._lock:
            self._running = False
            ports = list(self._ports.values())
            self._ports.clear()
        for port in ports:
            port.stop()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        self.logger.info(f"Bridge {self.bridge_id}: stopped")

    def _mix_loop(self) -> None:
        deadline = time.monotonic()
        while self._running:
            deadline += FRAME_SECONDS
            try:
                self._mix_once()
            except Exception as exc:
                self.logger.error(f"Bridge {self.bridge_id}: mix tick failed: {exc}", exc_info=True)

            slack = deadline - time.monotonic()
            if slack > 0:
                time.sleep(slack)
            else:
                self.late_ticks += 1
                if slack < -FRAME_SECONDS:
                    # Far enough behind that catching up would burst packets;
                    # resynchronise and take the gap once.
                    deadline = time.monotonic()

    def _mix_once(self) -> None:
        """Produce and send one 20 ms frame for every port."""
        with self._lock:
            ports = list(self._ports.values())
        if not ports:
            return

        # One frame consumed per port per tick, then shared by all listeners.
        frames = {port.port_id: port.take_frame() for port in ports}

        for port in ports:
            sources = [
                frames[pid]
                for pid in port.hears
                if pid in frames and not self._contribution_muted(pid)
            ]
            port.send_frame(self._sum(sources))

    def _contribution_muted(self, port_id: str) -> bool:
        with self._lock:
            port = self._ports.get(port_id)
            return port.muted if port is not None else True

    @staticmethod
    def _sum(sources: list[np.ndarray]) -> np.ndarray:
        """Sum contributions, clipping rather than wrapping on overflow."""
        if not sources:
            return _SILENCE
        if len(sources) == 1:
            return sources[0]
        total = np.zeros(FRAME_SAMPLES, dtype=np.int32)
        for frame in sources:
            total += frame
        return np.clip(total, -32768, 32767).astype(np.int16)

    def _forward_dtmf(self, sender_id: str, payload: bytes) -> None:
        """Send a telephone-event to every port that hears the sender."""
        with self._lock:
            listeners = [port for port in self._ports.values() if sender_id in port.hears]
        for port in listeners:
            port.send_dtmf(payload)


class RTPMixer:
    """
    Registry of active bridges -- the mixer counterpart to :class:`RTPRelay`.

    Attached to PBXCore as ``rtp_mixer``. Features ask for a bridge, add the
    legs they already own, and apply a mode; they never touch sockets.
    """

    def __init__(self, max_ports_per_bridge: int = 8) -> None:
        self.max_ports_per_bridge = max_ports_per_bridge
        self.logger = get_logger()
        self._bridges: dict[str, MixBridge] = {}
        self._lock = threading.Lock()

    def create_bridge(self, bridge_id: str, max_ports: int | None = None) -> MixBridge:
        """Create a bridge, replacing any existing one with the same id."""
        with self._lock:
            existing = self._bridges.pop(bridge_id, None)
        if existing is not None:
            existing.stop()

        bridge = MixBridge(bridge_id, max_ports or self.max_ports_per_bridge)
        with self._lock:
            self._bridges[bridge_id] = bridge
        self.logger.info(f"Created mix bridge {bridge_id}")
        return bridge

    def get_bridge(self, bridge_id: str) -> MixBridge | None:
        with self._lock:
            return self._bridges.get(bridge_id)

    def destroy_bridge(self, bridge_id: str) -> None:
        with self._lock:
            bridge = self._bridges.pop(bridge_id, None)
        if bridge is not None:
            bridge.stop()

    def active_bridges(self) -> list[str]:
        with self._lock:
            return list(self._bridges)

    def shutdown(self) -> None:
        """Tear every bridge down (from PBXCore.stop())."""
        with self._lock:
            bridges = list(self._bridges.values())
            self._bridges.clear()
        for bridge in bridges:
            bridge.stop()


def describe_modes() -> dict[str, Any]:
    """The routing each named mode produces, for docs and diagnostics."""
    return {
        "conference": "every port hears every other port",
        "barge": "every port hears every other port (supervisor joins openly)",
        "monitor": "supervisor hears the parties; the parties do not hear the supervisor",
        "whisper": "supervisor is heard only by the target party",
    }
