"""Tests for music-on-hold streaming and the generic relay pause/resume primitives."""

from __future__ import annotations

import socket
import struct
import time
from typing import TYPE_CHECKING, ClassVar

import pytest

from pbx.features.music_on_hold import MusicOnHold
from pbx.rtp.handler import RTPPlayer, RTPRelay, RTPRelayHandler
from pbx.sip.sdp import SDPBuilder, SDPSession
from pbx.utils.audio import build_wav_header

if TYPE_CHECKING:
    from pathlib import Path


def _recv_socket() -> socket.socket:
    """A bound loopback UDP socket with a short recv timeout."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(1.0)
    return sock


def _write_wav(path: Path, samples: int = 800) -> None:
    """Write a small mono 16-bit PCM WAV (a simple square wave)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = struct.pack(f"<{samples}h", *([4000, -4000] * (samples // 2)))
    path.write_bytes(build_wav_header(len(pcm), sample_rate=8000) + pcm)


def _rtp_packet() -> bytes:
    header = struct.pack("!BBHII", 0x80, 0, 1, 0, 0x11223344)
    return header + b"\xff" * 160


# ---------------------------------------------------------------------------
# RTPRelayHandler generic pause/resume/get_endpoint
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestRelayPausePrimitives:
    def test_pause_resume_toggles_flag(self) -> None:
        handler = RTPRelayHandler(local_port=0, call_id="c1")
        assert handler.paused is False
        handler.pause_relay()
        assert handler.paused is True
        handler.resume_relay()
        assert handler.paused is False

    def test_get_endpoint_prefers_learned(self) -> None:
        handler = RTPRelayHandler(local_port=0, call_id="c1")
        handler.endpoint_a = ("10.0.0.1", 5000)
        handler.endpoint_b = ("10.0.0.2", 6000)
        assert handler.get_endpoint("a") == ("10.0.0.1", 5000)
        handler.learned_a = ("192.168.1.1", 40000)
        assert handler.get_endpoint("a") == ("192.168.1.1", 40000)
        assert handler.get_endpoint("b") == ("10.0.0.2", 6000)
        assert handler.get_endpoint("x") is None

    def test_paused_relay_drops_packets(self) -> None:
        relay = RTPRelay(port_range_start=41200, port_range_end=41300)
        call_id = "paused_relay"
        assert relay.allocate_relay(call_id) is not None
        handler = relay.get_handler(call_id)
        assert handler is not None
        relay_port = relay.active_relays[call_id]["rtp_port"]

        sock_a, sock_b = _recv_socket(), _recv_socket()
        try:
            relay.set_endpoints(call_id, sock_a.getsockname(), sock_b.getsockname())
            time.sleep(0.05)

            # Learn both sides, then confirm A->B relays normally.
            sock_a.sendto(_rtp_packet(), ("127.0.0.1", relay_port))
            sock_b.sendto(_rtp_packet(), ("127.0.0.1", relay_port))
            time.sleep(0.05)
            sock_a.sendto(_rtp_packet(), ("127.0.0.1", relay_port))
            assert sock_b.recvfrom(2048)[0]

            # Paused: A's packet must not reach B.
            handler.pause_relay()
            sock_a.sendto(_rtp_packet(), ("127.0.0.1", relay_port))
            with pytest.raises(TimeoutError):
                sock_b.recvfrom(2048)

            # Resumed: relaying works again.
            handler.resume_relay()
            sock_a.sendto(_rtp_packet(), ("127.0.0.1", relay_port))
            assert sock_b.recvfrom(2048)[0]
        finally:
            sock_a.close()
            sock_b.close()
            relay.release_relay(call_id)


# ---------------------------------------------------------------------------
# RTPPlayer external socket + stop_event
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestRTPPlayerReuse:
    def test_external_socket_not_closed_on_stop(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        try:
            player = RTPPlayer(0, "127.0.0.1", 9999, call_id="c1", external_socket=sock)
            assert player.start() is True
            assert player._owns_socket is False
            player.stop()
            # The borrowed socket stays open for its real owner.
            assert sock.fileno() != -1
        finally:
            sock.close()

    def test_interrupt_check_aborts_before_send(self) -> None:
        receiver = _recv_socket()
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            player = RTPPlayer(0, *receiver.getsockname(), call_id="c1", external_socket=sender)
            player.start()
            # Enough data for many packets; a firing predicate must abort
            # before any send.
            assert player.send_audio(b"\xff" * 1600, interrupt_check=lambda: True) is True
            with pytest.raises(TimeoutError):
                receiver.recvfrom(2048)
        finally:
            receiver.close()
            sender.close()


# ---------------------------------------------------------------------------
# MusicOnHold streaming
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestMusicOnHold:
    def test_start_streams_and_stop_resumes(self, tmp_path: Path) -> None:
        _write_wav(tmp_path / "default" / "hold.wav")
        moh = MusicOnHold(moh_directory=str(tmp_path))
        assert "default" in moh.get_classes()

        relay = RTPRelay(port_range_start=41300, port_range_end=41400)
        call_id = "moh_stream"
        assert relay.allocate_relay(call_id) is not None
        handler = relay.get_handler(call_id)
        assert handler is not None

        held = _recv_socket()
        try:
            # Held party is side "a"; point it at our receiver.
            handler.learned_a = held.getsockname()

            audio_file = moh.start_moh(call_id, handler, "a")
            assert audio_file is not None
            assert handler.paused is True
            assert call_id in moh.active_sessions
            # Hold music should arrive at the held party.
            assert held.recvfrom(2048)[0]

            moh.stop_moh(call_id)
            assert handler.paused is False
            assert call_id not in moh.active_sessions
        finally:
            held.close()
            relay.release_relay(call_id)

    def test_start_without_files_holds_in_silence(self, tmp_path: Path) -> None:
        moh = MusicOnHold(moh_directory=str(tmp_path))  # default dir exists but empty
        handler = RTPRelayHandler(local_port=0, call_id="silent")

        assert moh.start_moh("silent", handler, "a") is None
        assert handler.paused is True  # still paused: no live audio leaks
        assert moh.active_sessions["silent"]["thread"] is None

        moh.stop_moh("silent")
        assert handler.paused is False
        assert "silent" not in moh.active_sessions

    def test_stop_moh_is_idempotent(self, tmp_path: Path) -> None:
        moh = MusicOnHold(moh_directory=str(tmp_path))
        moh.stop_moh("never_held")  # must not raise

    def test_repeated_start_stops_previous_playback(self, tmp_path: Path, monkeypatch) -> None:
        _write_wav(tmp_path / "default" / "hold.wav")
        moh = MusicOnHold(moh_directory=str(tmp_path))
        relay = RTPRelayHandler(local_port=0, call_id="duplicate_hold")

        class FakeThread:
            instances: ClassVar[list[FakeThread]] = []

            def __init__(self, **_kwargs) -> None:
                self.alive = False
                self.join_timeout: float | None = None
                self.__class__.instances.append(self)

            def start(self) -> None:
                self.alive = True

            def is_alive(self) -> bool:
                return self.alive

            def join(self, timeout: float | None = None) -> None:
                self.join_timeout = timeout
                self.alive = False

        monkeypatch.setattr("pbx.features.music_on_hold.threading.Thread", FakeThread)

        moh.start_moh("duplicate_hold", relay, "a")
        previous = moh.active_sessions["duplicate_hold"]
        previous_thread = previous["thread"]

        moh.start_moh("duplicate_hold", relay, "a")

        assert previous["stop_event"].is_set()
        assert previous_thread.join_timeout == 1.0
        assert moh.active_sessions["duplicate_hold"] is not previous
        assert len(FakeThread.instances) == 2
        assert relay.paused is True


# ---------------------------------------------------------------------------
# MusicOnHold.interject (sequential hold-announcement interruption)
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestMusicOnHoldInterject:
    def test_interject_plays_prompt_then_resumes_moh(self, tmp_path: Path) -> None:
        _write_wav(tmp_path / "default" / "hold.wav")
        prompt_path = tmp_path / "prompt.wav"
        _write_wav(prompt_path, samples=400)
        moh = MusicOnHold(moh_directory=str(tmp_path))

        relay = RTPRelay(port_range_start=41400, port_range_end=41500)
        call_id = "interject_test"
        assert relay.allocate_relay(call_id) is not None
        handler = relay.get_handler(call_id)
        assert handler is not None

        held = _recv_socket()
        try:
            handler.learned_a = held.getsockname()
            assert moh.start_moh(call_id, handler, "a") is not None
            assert held.recvfrom(2048)[0]  # MOH is flowing

            played = moh.interject(call_id, "a", prompt_path)

            assert played is True
            # MOH resumed afterward: a fresh session exists, relay stays paused.
            assert call_id in moh.active_sessions
            assert handler.paused is True
            assert held.recvfrom(2048)[0]  # MOH resumed after the prompt

            moh.stop_moh(call_id)
            assert handler.paused is False
        finally:
            held.close()
            relay.release_relay(call_id)

    def test_interject_no_session_is_noop(self, tmp_path: Path) -> None:
        moh = MusicOnHold(moh_directory=str(tmp_path))
        prompt_path = tmp_path / "prompt.wav"
        _write_wav(prompt_path)

        assert moh.interject("never_held", "a", prompt_path) is False
        assert "never_held" not in moh.active_sessions


# ---------------------------------------------------------------------------
# SDP media-direction parse/build (hold signalling)
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestSDPDirection:
    @staticmethod
    def _sdp(direction_attr: str) -> str:
        lines = [
            "v=0",
            "o=- 1 1 IN IP4 1.2.3.4",
            "s=-",
            "c=IN IP4 1.2.3.4",
            "t=0 0",
            "m=audio 5000 RTP/AVP 0",
        ]
        if direction_attr:
            lines.append(f"a={direction_attr}")
        return "\r\n".join(lines) + "\r\n"

    @pytest.mark.parametrize(
        ("attr", "expected"),
        [
            ("sendonly", "sendonly"),
            ("inactive", "inactive"),
            ("sendrecv", "sendrecv"),
            ("", "sendrecv"),  # absent defaults to sendrecv
        ],
    )
    def test_parse_direction(self, attr: str, expected: str) -> None:
        session = SDPSession()
        session.parse(self._sdp(attr))
        info = session.get_audio_info()
        assert info is not None
        assert info["direction"] == expected

    def test_build_emits_requested_direction(self) -> None:
        sdp = SDPBuilder.build_audio_sdp("1.2.3.4", 10000, codecs=["0"], direction="recvonly")
        assert "a=recvonly" in sdp
        assert "a=sendrecv" not in sdp
