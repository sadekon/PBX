"""
Tests for pbx.rtp.dtmf_monitor.DTMFMonitor and build_ivr_dtmf_channel.

In-band cases use real DTMF tone audio (DTMFGenerator) encoded to G.711
µ-law and fed as 20 ms RTP-sized payloads, exercising the same decode +
Goertzel path used on live calls.
"""

import struct
import time
from unittest.mock import MagicMock

import pytest

from pbx.rtp.dtmf_monitor import DTMFMonitor, build_ivr_dtmf_channel
from pbx.utils.audio import pcm16_to_ulaw
from pbx.utils.dtmf import DTMFGenerator


class FakeCall:
    """Minimal call object: just the out-of-band digit queue."""

    def __init__(self) -> None:
        self.dtmf_info_queue: list[str] = []


def _tone_packets(digit: str, duration_ms: int = 400, amplitude: int = 12000) -> list[bytes]:
    """Generate a real DTMF tone as µ-law G.711 20 ms packet payloads."""
    gen = DTMFGenerator(sample_rate=8000)
    samples = gen.generate_tone(digit, duration_ms=duration_ms)
    pcm = b"".join(struct.pack("<h", int(s * amplitude)) for s in samples)
    ulaw = pcm16_to_ulaw(pcm)
    return [ulaw[i : i + 160] for i in range(0, len(ulaw), 160)]


@pytest.mark.unit
class TestDTMFMonitorInband:
    def test_detects_real_tone(self) -> None:
        monitor = DTMFMonitor(FakeCall())
        for packet in _tone_packets("5"):
            monitor.on_audio_packet(0, packet)
        assert monitor.get_digit(timeout=1.0) == "5"

    def test_ignores_silence(self) -> None:
        monitor = DTMFMonitor(FakeCall())
        for _ in range(50):
            monitor.on_audio_packet(0, b"\xff" * 160)  # µ-law silence
        assert monitor.get_digit(timeout=0.3) is None

    def test_ignores_non_g711_payload(self) -> None:
        monitor = DTMFMonitor(FakeCall())
        monitor.on_audio_packet(9, b"\x55" * 160)  # G.722: no in-band detection
        assert not monitor.has_digit()

    def test_debounces_same_tone(self) -> None:
        """One long keypress must yield one digit, not one per scan."""
        monitor = DTMFMonitor(FakeCall())
        for packet in _tone_packets("8", duration_ms=600):
            monitor.on_audio_packet(0, packet)
        assert monitor.get_digit(timeout=1.0) == "8"
        assert monitor.get_digit(timeout=0.3) is None


@pytest.mark.unit
class TestDTMFMonitorOutOfBand:
    def test_drains_queue(self) -> None:
        call = FakeCall()
        monitor = DTMFMonitor(call)
        call.dtmf_info_queue.append("3")
        assert monitor.get_digit(timeout=0.5) == "3"
        assert call.dtmf_info_queue == []

    def test_oob_never_debounced(self) -> None:
        """Deliberate double-press via RFC 2833 must yield two digits."""
        call = FakeCall()
        monitor = DTMFMonitor(call)
        call.dtmf_info_queue.extend(["4", "4"])
        assert monitor.get_digit(timeout=0.2) == "4"
        assert monitor.get_digit(timeout=0.2) == "4"

    def test_inband_echo_of_oob_digit_suppressed(self) -> None:
        """A keypress arriving out-of-band AND as an audible tone = 1 digit."""
        call = FakeCall()
        monitor = DTMFMonitor(call)
        call.dtmf_info_queue.append("7")
        assert monitor.get_digit(timeout=0.2) == "7"
        for packet in _tone_packets("7", duration_ms=300):
            monitor.on_audio_packet(0, packet)
        time.sleep(0.15)  # allow a scan interval to elapse
        assert monitor.get_digit(timeout=0.2) is None


@pytest.mark.unit
class TestDTMFMonitorApi:
    def test_peek_does_not_consume_and_preserves_order(self) -> None:
        call = FakeCall()
        monitor = DTMFMonitor(call)
        call.dtmf_info_queue.extend(["1", "#"])
        assert monitor.peek_digits() == ("1", "#")
        assert monitor.peek_digits() == ("1", "#")
        assert monitor.get_digit(timeout=0.1) == "1"
        assert monitor.get_digit(timeout=0.1) == "#"

    def test_has_digit_peeks(self) -> None:
        call = FakeCall()
        monitor = DTMFMonitor(call)
        call.dtmf_info_queue.append("2")
        assert monitor.has_digit()
        assert monitor.get_digit(timeout=0.1) == "2"

    def test_clear(self) -> None:
        call = FakeCall()
        monitor = DTMFMonitor(call)
        call.dtmf_info_queue.append("9")
        assert monitor.has_digit()
        monitor.clear()
        assert monitor.get_digit(timeout=0.1) is None

    def test_get_digit_times_out(self) -> None:
        monitor = DTMFMonitor(FakeCall())
        start = time.time()
        assert monitor.get_digit(timeout=0.2) is None
        assert time.time() - start < 2.0


@pytest.mark.unit
class TestBuildIvrDtmfChannel:
    def test_wires_recorder_and_monitor(self) -> None:
        pbx = MagicMock()
        pbx._get_dtmf_payload_type.return_value = 101
        call = FakeCall()
        call.caller_rtp = {"rtpmap_names": {"96": "telephone-event/8000"}}

        recorder, monitor = build_ivr_dtmf_channel(pbx, call, "call-1", 30000)

        assert recorder.dtmf_monitor is monitor
        assert recorder.rfc2833_handler is not None
        # Negotiated PT plus the caller's offered telephone-event PT
        assert recorder.dtmf_payload_types == {101, 96}
        assert recorder.rfc2833_handler.payload_types == {101, 96}

    def test_no_caller_rtp(self) -> None:
        pbx = MagicMock()
        pbx._get_dtmf_payload_type.return_value = 101
        call = FakeCall()
        call.caller_rtp = None

        recorder, monitor = build_ivr_dtmf_channel(pbx, call, "call-1", 30000)
        assert recorder.dtmf_payload_types == {101}
        assert recorder.dtmf_monitor is monitor
