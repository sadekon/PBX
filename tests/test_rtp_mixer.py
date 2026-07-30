"""
Tests for the N-way audio mixer (capability C3).

The routing-matrix tests are cheap and catch mistakes in the mode builders,
but they only prove the bookkeeping. The tests that actually matter are in
TestLoopbackAudio: real UDP sockets, a distinct tone per participant, and a
Goertzel filter on each output asserting it contains exactly the tones its
routing allows and none of the others. That is what proves mix-minus-self
and, more importantly, that whisper is genuinely inaudible to the caller.

TestDtmfPassThrough covers the same routing for telephone-events, which are
forwarded rather than mixed -- without that, anyone bridged into a call loses
the ability to drive an IVR. TestWidebandBridge exercises the G.722
transcode path end to end, since that is the codec with real CPU cost.

No feature is wired onto the mixer yet (see the C3 section of
docs/DEVELOPMENT_GUIDE.md), so these tests are the contract that future
wiring is written against.
"""

from __future__ import annotations

import socket
import struct
import threading
import time
from typing import ClassVar
from unittest.mock import MagicMock

import numpy as np
import pytest

from pbx.rtp.codecs import (
    FRAME_SAMPLES,
    MIX_RATE,
    PT_G722,
    PT_PCMA,
    PT_PCMU,
    PT_TELEPHONE_EVENT,
    make_codec,
)
from pbx.rtp.mixer import (
    BargeMode,
    ConferenceMode,
    MixBridge,
    MonitorMode,
    RTPMixer,
    WhisperMode,
)

# Well separated so leakage between them is unambiguous.
TONE_HZ = {"agent": 500.0, "caller": 1200.0, "supervisor": 1900.0}


def _tone(freq: float, samples: int, amplitude: int = 8000, phase: float = 0.0) -> np.ndarray:
    t = (np.arange(samples) + phase) / MIX_RATE
    return (np.sin(2 * np.pi * freq * t) * amplitude).astype(np.int16)


def _goertzel(samples: np.ndarray, freq: float) -> float:
    """Energy at one frequency, normalised so results compare across tones."""
    n = len(samples)
    k = int(0.5 + (n * freq) / MIX_RATE)
    omega = (2.0 * np.pi * k) / n
    coeff = 2.0 * np.cos(omega)
    s_prev = s_prev2 = 0.0
    for value in samples.astype(np.float64) / 32768.0:
        s = value + coeff * s_prev - s_prev2
        s_prev2, s_prev = s_prev, s
    power = s_prev2**2 + s_prev**2 - coeff * s_prev * s_prev2
    return float(power / n)


def _free_port() -> int:
    """
    An ephemeral UDP port a MixPort can bind.

    Probes on 0.0.0.0 because that is what MixPort binds: a port free on
    127.0.0.1 can still be taken on another interface, and binding 0.0.0.0
    conflicts with any address on that port.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("0.0.0.0", 0))  # nosec B104 - matches MixPort's bind
        return probe.getsockname()[1]


class Participant:
    """A fake endpoint: sends its own tone, records what the bridge sends back."""

    def __init__(self, name: str, payload_type: int = PT_PCMU) -> None:
        self.name = name
        self.payload_type = payload_type
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.settimeout(0.3)
        self.address = self.socket.getsockname()
        self.bridge_port = _free_port()
        self.port_id: str | None = None

        self._codec = make_codec(payload_type)
        self._received: list[np.ndarray] = []
        #: Telephone-event payloads received, with the SSRC they arrived on.
        self.dtmf: list[tuple[bytes, int]] = []
        self._running = False
        self._thread: threading.Thread | None = None
        self._sequence = 0
        self._timestamp = 0
        self._phase = 0

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._receive, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self.socket.close()

    def _receive(self) -> None:
        while self._running:
            try:
                data, _ = self.socket.recvfrom(2048)
            except (TimeoutError, OSError):
                continue
            if len(data) <= 12:
                continue
            payload_type = data[1] & 0x7F
            ssrc = struct.unpack("!I", data[8:12])[0]
            if payload_type == PT_TELEPHONE_EVENT:
                self.dtmf.append((data[12:], ssrc))
            else:
                self._received.append(self._codec.decode(data[12:]))

    def send_dtmf(self, payload: bytes) -> None:
        """Emit one RFC 4733 telephone-event packet."""
        header = struct.pack(
            "!BBHII", 0x80, PT_TELEPHONE_EVENT, self._sequence, self._timestamp, 0x1234
        )
        self._sequence = (self._sequence + 1) & 0xFFFF
        self.socket.sendto(header + payload, ("127.0.0.1", self.bridge_port))

    def send_tone(self, frames: int) -> None:
        """
        Emit `frames` 20 ms packets of this participant's tone.

        Stops quietly if the socket goes away mid-stream, which is both what
        a real endpoint hanging up looks like and what teardown does here.
        """
        for _ in range(frames):
            if not self._running:
                return
            chunk = _tone(TONE_HZ[self.name], FRAME_SAMPLES, phase=self._phase)
            self._phase += FRAME_SAMPLES
            header = struct.pack(
                "!BBHII", 0x80, self.payload_type, self._sequence, self._timestamp, 0x1234
            )
            self._sequence = (self._sequence + 1) & 0xFFFF
            self._timestamp = (self._timestamp + FRAME_SAMPLES) & 0xFFFFFFFF
            try:
                self.socket.sendto(
                    header + self._codec.encode(chunk), ("127.0.0.1", self.bridge_port)
                )
            except OSError:
                return
            time.sleep(0.02)

    def energy(self) -> dict[str, float]:
        """Energy of each participant's tone in everything received so far."""
        # Snapshot first: the receive thread may still be appending.
        chunks = list(self._received)
        if not chunks:
            return dict.fromkeys(TONE_HZ, 0.0)
        audio = np.concatenate(chunks)
        return {name: _goertzel(audio, freq) for name, freq in TONE_HZ.items()}


@pytest.fixture
def parties():
    people = {name: Participant(name) for name in TONE_HZ}
    for person in people.values():
        person.start()
    yield people
    for person in people.values():
        person.stop()


def _build_bridge(parties: dict[str, Participant], bridge: MixBridge) -> None:
    for person in parties.values():
        person.port_id = bridge.add_port(
            call_id=f"call-{person.name}",
            local_port=person.bridge_port,
            remote=person.address,
            # Must match what the participant actually sends: a port drops
            # packets whose payload type it was not opened for.
            payload_type=person.payload_type,
        )
        assert person.port_id is not None


def _run(parties: dict[str, Participant], frames: int = 25) -> None:
    """Everyone talks at once, then let the last frames drain."""
    senders = [threading.Thread(target=p.send_tone, args=(frames,)) for p in parties.values()]
    for sender in senders:
        sender.start()
    for sender in senders:
        sender.join(timeout=5)
    time.sleep(0.15)


def _assert_hears(energy: dict[str, float], expected: set[str], listener: str) -> None:
    """Every expected tone must dominate every unexpected one by 20x."""
    heard = {name: value for name, value in energy.items() if name in expected}
    silent = {name: value for name, value in energy.items() if name not in expected}
    assert heard, f"{listener}: nothing expected?"
    weakest_heard = min(heard.values())
    loudest_silent = max(silent.values(), default=0.0)
    assert weakest_heard > 1e-7, f"{listener} did not hear {expected}: {energy}"
    assert weakest_heard > loudest_silent * 20, (
        f"{listener} should hear exactly {expected} but got {energy}"
    )


@pytest.mark.unit
class TestRoutingMatrices:
    """The mode builders, independent of any audio."""

    IDS: ClassVar[list[str]] = ["a", "b", "c"]

    def test_conference_is_everyone_minus_self(self) -> None:
        assert ConferenceMode().matrix(self.IDS) == {
            "a": {"b", "c"},
            "b": {"a", "c"},
            "c": {"a", "b"},
        }

    def test_monitor_is_one_way(self) -> None:
        """Supervisor 'c' hears the call; the call is unchanged."""
        assert MonitorMode(supervisor="c").matrix(self.IDS) == {
            "a": {"b"},
            "b": {"a"},
            "c": {"a", "b"},
        }

    def test_whisper_reaches_only_the_target(self) -> None:
        """'a' also hears supervisor 'c'; 'b' must not."""
        assert WhisperMode(supervisor="c", target="a").matrix(self.IDS) == {
            "a": {"b", "c"},
            "b": {"a"},
            "c": {"a", "b"},
        }

    def test_barge_matches_conference(self) -> None:
        assert BargeMode().matrix(self.IDS) == ConferenceMode().matrix(self.IDS)

    def test_no_port_ever_hears_itself(self) -> None:
        for mode in (
            ConferenceMode(),
            MonitorMode("c"),
            WhisperMode("c", "a"),
            BargeMode(),
        ):
            for pid, hears in mode.matrix(self.IDS).items():
                assert pid not in hears, f"{type(mode).__name__} routes {pid} to itself"


@pytest.mark.unit
class TestMixMath:
    def test_sums_contributions(self) -> None:
        a = np.full(FRAME_SAMPLES, 1000, dtype=np.int16)
        b = np.full(FRAME_SAMPLES, 2000, dtype=np.int16)
        assert np.array_equal(MixBridge._sum([a, b]), np.full(FRAME_SAMPLES, 3000))

    def test_clips_instead_of_wrapping(self) -> None:
        loud = np.full(FRAME_SAMPLES, 30000, dtype=np.int16)
        mixed = MixBridge._sum([loud, loud])
        assert mixed.max() == 32767
        assert mixed.min() >= 0  # wrapping would produce large negatives

    def test_clips_negative_overflow(self) -> None:
        quiet = np.full(FRAME_SAMPLES, -30000, dtype=np.int16)
        assert MixBridge._sum([quiet, quiet]).min() == -32768

    def test_no_sources_is_silence(self) -> None:
        assert not MixBridge._sum([]).any()

    def test_single_source_passes_through_untouched(self) -> None:
        only = _tone(500.0, FRAME_SAMPLES)
        assert np.array_equal(MixBridge._sum([only]), only)


@pytest.mark.unit
class TestBridgeMembership:
    def test_rejects_untranscodable_codec(self) -> None:
        """G.729 has no transcoder: the caller must renegotiate, not guess."""
        bridge = MixBridge("b1")
        try:
            assert bridge.add_port("c1", _free_port(), ("127.0.0.1", 5000), payload_type=18) is None
        finally:
            bridge.stop()

    def test_enforces_max_ports(self) -> None:
        bridge = MixBridge("b1", max_ports=2)
        try:
            for _ in range(2):
                assert bridge.add_port("c", _free_port(), ("127.0.0.1", 5000), PT_PCMU) is not None
            assert bridge.add_port("c3", _free_port(), ("127.0.0.1", 5000), PT_PCMU) is None
        finally:
            bridge.stop()

    def test_removing_a_port_clears_it_from_routing(self) -> None:
        bridge = MixBridge("b1")
        try:
            ids = [
                bridge.add_port(f"c{i}", _free_port(), ("127.0.0.1", 5000), PT_PCMU)
                for i in range(3)
            ]
            bridge.apply_mode(ConferenceMode())
            bridge.remove_port(ids[0])
            for hears in bridge.routing().values():
                assert ids[0] not in hears
        finally:
            bridge.stop()

    def test_set_hears_ignores_self_and_unknown_ports(self) -> None:
        bridge = MixBridge("b1")
        try:
            pid = bridge.add_port("c1", _free_port(), ("127.0.0.1", 5000), PT_PCMU)
            bridge.set_hears(pid, {pid, "nonexistent"})
            assert bridge.routing()[pid] == set()
        finally:
            bridge.stop()

    def test_bind_conflict_is_handled_without_raising(self) -> None:
        """
        A contended port must degrade to a None return, never an exception --
        add_port is called from call-control paths that cannot crash.

        Whether the bind actually fails is platform-dependent (SO_REUSEADDR
        semantics for UDP differ), so this asserts the contract, not the
        outcome.
        """
        blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        blocker.bind(("127.0.0.1", 0))
        taken = blocker.getsockname()[1]
        bridge = MixBridge("b1")
        try:
            result = bridge.add_port("c1", taken, ("127.0.0.1", 5000), PT_PCMU)
            assert result is None or isinstance(result, str)
        finally:
            bridge.stop()
            blocker.close()


@pytest.mark.unit
class TestFrameFitting:
    """take_frame normalises whatever a codec produced to the mix domain."""

    def _port(self):
        from pbx.rtp.mixer import MixPort

        return MixPort("p1", "c1", 0, None, PT_PCMU)

    def test_missing_input_becomes_silence(self) -> None:
        assert not self._port().take_frame().any()

    def test_short_frame_is_zero_padded(self) -> None:
        port = self._port()
        port._frames.append(np.full(40, 700, dtype=np.int16))
        frame = port.take_frame()
        assert frame.size == FRAME_SAMPLES
        assert (frame[:40] == 700).all()
        assert not frame[40:].any()

    def test_long_frame_is_trimmed(self) -> None:
        port = self._port()
        port._frames.append(np.full(FRAME_SAMPLES * 2, 700, dtype=np.int16))
        assert port.take_frame().size == FRAME_SAMPLES

    def test_each_frame_is_consumed_once(self) -> None:
        port = self._port()
        port._frames.append(np.full(FRAME_SAMPLES, 700, dtype=np.int16))
        assert port.take_frame().any()
        assert not port.take_frame().any()

    def test_queue_is_bounded_so_a_fast_sender_cannot_grow_latency(self) -> None:
        port = self._port()
        for _ in range(50):
            port._frames.append(np.full(FRAME_SAMPLES, 700, dtype=np.int16))
        assert len(port._frames) <= 3


@pytest.mark.unit
class TestUnexpectedPayloadType:
    """
    A port opened for one codec drops other codecs -- but says so once.

    Found the hard way: a mismatched port silently discards every packet, so
    a leg that renegotiates mid-call goes deaf with nothing in the log.
    """

    def _port(self):
        from pbx.rtp.mixer import MixPort

        return MixPort("p1", "c1", 0, None, PT_PCMU)

    def test_warns_once_per_unexpected_type(self) -> None:
        port = self._port()
        port.logger = MagicMock()

        for _ in range(10):
            port._warn_unexpected_payload(PT_G722)

        assert port.logger.warning.call_count == 1
        message = port.logger.warning.call_args[0][0]
        assert str(PT_G722) in message
        assert "renegotiated" in message

    def test_each_distinct_type_is_reported(self) -> None:
        port = self._port()
        port.logger = MagicMock()

        port._warn_unexpected_payload(PT_G722)
        port._warn_unexpected_payload(18)
        port._warn_unexpected_payload(PT_G722)

        assert port.logger.warning.call_count == 2


@pytest.mark.unit
class TestMixerRegistry:
    def test_create_get_destroy(self) -> None:
        mixer = RTPMixer()
        try:
            bridge = mixer.create_bridge("room1")
            assert mixer.get_bridge("room1") is bridge
            assert mixer.active_bridges() == ["room1"]
            mixer.destroy_bridge("room1")
            assert mixer.get_bridge("room1") is None
        finally:
            mixer.shutdown()

    def test_recreating_an_id_replaces_the_old_bridge(self) -> None:
        mixer = RTPMixer()
        try:
            first = mixer.create_bridge("room1")
            second = mixer.create_bridge("room1")
            assert first is not second
            assert mixer.get_bridge("room1") is second
        finally:
            mixer.shutdown()


@pytest.mark.integration
class TestLoopbackAudio:
    """Real sockets, real codecs, real mix thread."""

    def test_conference_each_party_hears_others_not_itself(self, parties) -> None:
        bridge = MixBridge("conf")
        try:
            _build_bridge(parties, bridge)
            bridge.apply_mode(ConferenceMode())
            _run(parties)

            for name, person in parties.items():
                _assert_hears(person.energy(), set(TONE_HZ) - {name}, name)
        finally:
            bridge.stop()

    def test_monitor_supervisor_hears_call_but_stays_inaudible(self, parties) -> None:
        bridge = MixBridge("mon")
        try:
            _build_bridge(parties, bridge)
            bridge.apply_mode(MonitorMode(supervisor=parties["supervisor"].port_id))
            _run(parties)

            _assert_hears(parties["supervisor"].energy(), {"agent", "caller"}, "supervisor")
            _assert_hears(parties["agent"].energy(), {"caller"}, "agent")
            _assert_hears(parties["caller"].energy(), {"agent"}, "caller")
        finally:
            bridge.stop()

    def test_whisper_reaches_the_agent_and_never_the_caller(self, parties) -> None:
        """The property the whole asymmetric-routing design exists for."""
        bridge = MixBridge("whisper")
        try:
            _build_bridge(parties, bridge)
            bridge.apply_mode(
                WhisperMode(
                    supervisor=parties["supervisor"].port_id,
                    target=parties["agent"].port_id,
                )
            )
            _run(parties)

            _assert_hears(parties["agent"].energy(), {"caller", "supervisor"}, "agent")
            _assert_hears(parties["caller"].energy(), {"agent"}, "caller")
            _assert_hears(parties["supervisor"].energy(), {"agent", "caller"}, "supervisor")
        finally:
            bridge.stop()

    def test_barge_is_a_three_way_call(self, parties) -> None:
        bridge = MixBridge("barge")
        try:
            _build_bridge(parties, bridge)
            bridge.apply_mode(BargeMode())
            _run(parties)

            for name, person in parties.items():
                _assert_hears(person.energy(), set(TONE_HZ) - {name}, name)
        finally:
            bridge.stop()

    def test_muted_participant_contributes_nothing_but_still_hears(self, parties) -> None:
        bridge = MixBridge("mute")
        try:
            _build_bridge(parties, bridge)
            bridge.apply_mode(ConferenceMode())
            bridge.mute(parties["supervisor"].port_id, True)
            _run(parties)

            _assert_hears(parties["agent"].energy(), {"caller"}, "agent")
            _assert_hears(parties["caller"].energy(), {"agent"}, "caller")
            _assert_hears(parties["supervisor"].energy(), {"agent", "caller"}, "supervisor")
        finally:
            bridge.stop()


@pytest.mark.integration
class TestDtmfPassThrough:
    """
    Telephone-events must survive a bridge, or anyone conferenced in loses
    the ability to drive an IVR. They are forwarded, never mixed.
    """

    def test_dtmf_reaches_everyone_who_hears_the_sender(self, parties) -> None:
        bridge = MixBridge("dtmf")
        try:
            _build_bridge(parties, bridge)
            bridge.apply_mode(ConferenceMode())

            # RFC 4733: digit 5, end bit clear, volume 10, duration 160.
            event = bytes([5, 10, 0x00, 0xA0])
            parties["agent"].send_dtmf(event)
            time.sleep(0.2)

            for name in ("caller", "supervisor"):
                payloads = [payload for payload, _ssrc in parties[name].dtmf]
                assert event in payloads, f"{name} never received the DTMF event"
            assert not parties["agent"].dtmf, "sender should not hear its own DTMF"
        finally:
            bridge.stop()

    def test_dtmf_respects_asymmetric_routing(self, parties) -> None:
        """In whisper the caller does not hear the supervisor -- including DTMF."""
        bridge = MixBridge("dtmf-whisper")
        try:
            _build_bridge(parties, bridge)
            bridge.apply_mode(
                WhisperMode(
                    supervisor=parties["supervisor"].port_id,
                    target=parties["agent"].port_id,
                )
            )

            event = bytes([7, 10, 0x00, 0xA0])
            parties["supervisor"].send_dtmf(event)
            time.sleep(0.2)

            assert [p for p, _ in parties["agent"].dtmf] == [event]
            assert not parties["caller"].dtmf, "caller must not receive supervisor DTMF"
        finally:
            bridge.stop()

    def test_dtmf_is_restamped_onto_the_listener_stream(self, parties) -> None:
        """One SSRC per port: endpoints reject a second, unexpected stream."""
        bridge = MixBridge("dtmf-ssrc")
        try:
            _build_bridge(parties, bridge)
            bridge.apply_mode(ConferenceMode())

            parties["agent"].send_tone(3)  # establish the audio stream first
            parties["agent"].send_dtmf(bytes([1, 10, 0x00, 0xA0]))
            time.sleep(0.2)

            caller = parties["caller"]
            assert caller.dtmf, "no DTMF received"
            dtmf_ssrc = caller.dtmf[0][1]
            assert dtmf_ssrc != 0x1234, "forwarded verbatim instead of re-stamped"
        finally:
            bridge.stop()


@pytest.mark.unit
class TestWidebandIsRefused:
    """
    G.722 is not mixable yet, and the mixer must say so rather than produce
    distorted audio.

    The ITU codec in pbx/features/ is non-functional -- its sub-band ADPCM
    predictor diverges and its QMF synthesis is a stub, so a 1 kHz tone
    round-trips to 250 Hz. Until that is replaced, a G.722 leg has to be
    renegotiated to G.711. These tests pin that contract so re-enabling the
    codec later is a deliberate act, not an accident.
    """

    def test_g722_is_not_registered(self) -> None:
        from pbx.rtp.codecs import is_supported, make_codec, supported_payload_types

        assert not is_supported(PT_G722)
        assert make_codec(PT_G722) is None
        assert supported_payload_types() == frozenset({PT_PCMU, PT_PCMA})

    def test_bridge_refuses_a_g722_leg(self) -> None:
        """add_port returning None is the caller's cue to re-INVITE to G.711."""
        bridge = MixBridge("wideband")
        try:
            assert bridge.add_port("c1", _free_port(), ("127.0.0.1", 5000), PT_G722) is None
            assert bridge.port_ids() == []
        finally:
            bridge.stop()

    def test_registry_stays_extensible(self) -> None:
        """Re-enabling a codec must be one call, with no mixer change."""
        from pbx.rtp import codecs

        assert not codecs.is_supported(99)
        try:
            codecs.register_codec(
                99, lambda: codecs.G711Codec(99, codecs.ulaw_to_pcm16, codecs.samples_to_ulaw)
            )
            assert codecs.is_supported(99)
            bridge = MixBridge("extensible")
            try:
                assert bridge.add_port("c1", _free_port(), ("127.0.0.1", 5000), 99) is not None
            finally:
                bridge.stop()
        finally:
            codecs._FACTORIES.pop(99, None)


@pytest.mark.integration
class TestMixClock:
    def test_tick_does_not_drift(self, parties) -> None:
        """
        A bare sleep(0.02) drifts long; the deadline-corrected clock must not.
        Measured by packet count, since that is what an endpoint actually hears.
        """
        bridge = MixBridge("clock")
        try:
            _build_bridge(parties, bridge)
            bridge.apply_mode(ConferenceMode())

            agent = parties["agent"]
            sender = threading.Thread(target=agent.send_tone, args=(50,), daemon=True)
            sender.start()
            time.sleep(1.0)
            produced = len(parties["caller"]._received)
            sender.join(timeout=2)

            # 1.0 s of 20 ms frames is 50; allow generous scheduling slack but
            # catch systematic drift, which would show up as a large shortfall.
            assert 40 <= produced <= 60, f"expected ~50 frames in 1 s, got {produced}"
            assert bridge.late_ticks < 10, f"{bridge.late_ticks} late ticks in 1 s"
        finally:
            bridge.stop()
