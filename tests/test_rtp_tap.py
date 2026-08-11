"""
The RTP audio tap.

This is the only code in this effort that runs on the media path, so the tests that matter
most are the ones proving what it does *not* do: never raise on the relay thread, never block
when the sink falls behind, never lose the tail of a call. A tap that drops audio is a
degraded recording. A tap that blocks is a broken phone call.
"""

import struct
import threading
import time

import pytest

from pbx.rtp.tap import AudioTap, RtpFrame, parse_rtp_packet


def rtp(
    payload: bytes = b"\xff" * 160,
    *,
    sequence: int = 1,
    timestamp: int = 0,
    payload_type: int = 0,
    marker: bool = False,
    csrc_count: int = 0,
    padding: int = 0,
    extension_words: int | None = None,
) -> bytes:
    """Build one RTP packet. Defaults to a plain 20 ms G.711 µ-law frame."""
    first = 0x80 | (csrc_count & 0x0F)
    if padding:
        first |= 0x20
    if extension_words is not None:
        first |= 0x10

    second = (0x80 if marker else 0) | payload_type
    header = struct.pack("!BBHII", first, second, sequence, timestamp, 0xDEADBEEF)
    header += b"\x00\x00\x00\x00" * csrc_count

    if extension_words is not None:
        header += struct.pack("!HH", 0xBEDE, extension_words)
        header += b"\x00\x00\x00\x00" * extension_words

    if padding:
        payload = payload + b"\x00" * (padding - 1) + bytes([padding])

    return header + payload


class CollectingSink:
    """Records everything it is given, so a test can assert on the audio that survived."""

    def __init__(self) -> None:
        self.frames: list[RtpFrame] = []
        self.closed = False

    def write(self, frame: RtpFrame) -> None:
        self.frames.append(frame)

    def close(self) -> None:
        self.closed = True


def drain(tap: AudioTap, expected: int, timeout: float = 2.0) -> None:
    """Wait until the sink has seen `expected` frames, rather than sleeping a fixed time."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if tap.stats()["written"] >= expected:
            return
        time.sleep(0.005)


@pytest.mark.unit
class TestParseRtpPacket:
    def test_plain_packet(self):
        frame = parse_rtp_packet(rtp(b"audio", sequence=7, timestamp=1234), "a")

        assert frame is not None
        assert frame.payload == b"audio"
        assert frame.sequence == 7
        assert frame.timestamp == 1234
        assert frame.payload_type == 0
        assert frame.source == "a"

    def test_alaw_payload_type_is_read(self):
        frame = parse_rtp_packet(rtp(payload_type=8), "b")

        assert frame is not None
        assert frame.payload_type == 8

    def test_marker_bit_does_not_leak_into_payload_type(self):
        """The top bit of byte 2 is the marker, not part of the payload type."""
        frame = parse_rtp_packet(rtp(payload_type=8, marker=True), "a")

        assert frame is not None
        assert frame.payload_type == 8

    def test_csrc_list_is_skipped(self):
        """
        A packet from a conference bridge carries contributing sources after the header.
        Slicing at a fixed 12 bytes would put four bytes of header into the audio per CSRC,
        which is an audible click at the start of every packet.
        """
        frame = parse_rtp_packet(rtp(b"audio", csrc_count=2), "a")

        assert frame is not None
        assert frame.payload == b"audio"

    def test_extension_header_is_skipped(self):
        frame = parse_rtp_packet(rtp(b"audio", extension_words=3), "a")

        assert frame is not None
        assert frame.payload == b"audio"

    def test_padding_is_stripped(self):
        frame = parse_rtp_packet(rtp(b"audio", padding=4), "a")

        assert frame is not None
        assert frame.payload == b"audio"

    def test_short_packet_is_rejected(self):
        assert parse_rtp_packet(b"\x80\x00\x00", "a") is None

    def test_header_only_packet_is_rejected(self):
        """No payload means nothing to record; it is not an error, just not audio."""
        assert parse_rtp_packet(rtp(b""), "a") is None

    def test_truncated_extension_is_rejected_rather_than_raising(self):
        truncated = rtp(b"audio", extension_words=2)[:14]

        assert parse_rtp_packet(truncated, "a") is None

    def test_absurd_padding_is_ignored(self):
        """A padding byte longer than the payload would otherwise slice to nothing."""
        packet = rtp(b"ab", padding=0)
        # Set the padding bit by hand without adding real padding bytes.
        corrupted = bytes([packet[0] | 0x20]) + packet[1:]

        frame = parse_rtp_packet(corrupted, "a")

        assert frame is None or frame.payload


@pytest.mark.unit
class TestAudioTap:
    def test_frames_reach_the_sink_with_their_side(self):
        sink = CollectingSink()
        tap = AudioTap("call-1", sink)
        tap.start()

        tap.feed("a", rtp(b"from-a", sequence=1))
        tap.feed("b", rtp(b"from-b", sequence=2))
        drain(tap, 2)
        tap.stop()

        assert [f.source for f in sink.frames] == ["a", "b"]
        assert [f.payload for f in sink.frames] == [b"from-a", b"from-b"]

    def test_sink_is_closed_on_stop(self):
        sink = CollectingSink()
        tap = AudioTap("call-1", sink)
        tap.start()
        tap.stop()

        assert sink.closed

    def test_queued_audio_is_flushed_on_stop(self):
        """
        The last second of a call is still in the queue when the call ends, and it is usually
        where somebody says goodbye. Stopping must not discard it.
        """
        sink = CollectingSink()
        tap = AudioTap("call-1", sink)
        # Never started, so nothing drains until stop() flushes.
        for n in range(10):
            tap.feed("a", rtp(sequence=n))

        tap.stop()

        assert len(sink.frames) == 10

    def test_a_full_queue_drops_instead_of_blocking(self):
        """The bound is the backpressure. Blocking here would stall a live call."""
        tap = AudioTap("call-1", CollectingSink(), queue_size=4)
        # Not started: nothing is draining, so the queue fills and stays full.

        for n in range(50):
            tap.feed("a", rtp(sequence=n))

        stats = tap.stats()
        assert stats["dropped"] == 46
        assert stats["fed"] == 4

    def test_feed_never_blocks_measurably(self):
        """
        Feeding a full queue 1000 times must stay far inside one 20 ms packet interval.
        If this ever regresses into a blocking put, calls develop audible jitter.
        """
        tap = AudioTap("call-1", CollectingSink(), queue_size=1)
        packet = rtp()

        started = time.monotonic()
        for _ in range(1000):
            tap.feed("a", packet)
        elapsed = time.monotonic() - started

        assert elapsed < 0.02, f"feed() took {elapsed:.4f}s for 1000 packets"

    def test_a_raising_sink_does_not_kill_the_drain_thread(self):
        class Exploding:
            def __init__(self):
                self.seen = 0

            def write(self, frame):
                self.seen += 1
                raise ValueError("disk on fire")

            def close(self):
                pass

        sink = Exploding()
        tap = AudioTap("call-1", sink)
        tap.start()

        for n in range(5):
            tap.feed("a", rtp(sequence=n))

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and sink.seen < 5:
            time.sleep(0.005)
        tap.stop()

        assert sink.seen == 5, "drain thread died on the first exception"

    def test_a_raising_close_does_not_propagate(self):
        class BadClose:
            def write(self, frame):
                pass

            def close(self):
                raise OSError("no space left on device")

        tap = AudioTap("call-1", BadClose())
        tap.start()
        tap.stop()  # must not raise

    def test_malformed_packets_are_counted_not_delivered(self):
        sink = CollectingSink()
        tap = AudioTap("call-1", sink)
        tap.start()

        tap.feed("a", b"\x80\x00")
        tap.feed("a", rtp(b"good"))
        drain(tap, 1)
        tap.stop()

        assert [f.payload for f in sink.frames] == [b"good"]
        assert tap.stats()["malformed"] == 1

    def test_starting_twice_does_not_make_a_second_thread(self):
        tap = AudioTap("call-1", CollectingSink())
        tap.start()
        first = tap._thread
        tap.start()

        assert tap._thread is first
        tap.stop()

    def test_stop_without_start_is_harmless(self):
        sink = CollectingSink()
        AudioTap("call-1", sink).stop()

        assert sink.closed

    def test_feed_after_stop_does_not_raise(self):
        tap = AudioTap("call-1", CollectingSink())
        tap.start()
        tap.stop()

        tap.feed("a", rtp())  # queued, never drained; must not raise


@pytest.mark.unit
class TestRelayIntegration:
    """The tap as the relay handler actually uses it."""

    def _handler(self):
        from pbx.rtp.handler import RTPRelayHandler

        return RTPRelayHandler(local_port=0, call_id="call-1")

    def test_no_tap_by_default(self):
        assert self._handler().tap is None

    def test_attach_starts_the_tap(self):
        handler = self._handler()
        tap = AudioTap("call-1", CollectingSink())

        handler.attach_tap(tap)

        assert handler.tap is tap
        assert tap.running
        handler.detach_tap()

    def test_detach_stops_and_closes(self):
        handler = self._handler()
        sink = CollectingSink()
        tap = AudioTap("call-1", sink)

        handler.attach_tap(tap)
        handler.detach_tap()

        assert handler.tap is None
        assert not tap.running
        assert sink.closed

    def test_attaching_a_second_tap_releases_the_first(self):
        """Otherwise the first sink is abandoned with its file half written."""
        handler = self._handler()
        first_sink = CollectingSink()
        handler.attach_tap(AudioTap("call-1", first_sink))

        second = AudioTap("call-1", CollectingSink())
        handler.attach_tap(second)

        assert first_sink.closed
        assert handler.tap is second
        handler.detach_tap()

    def test_stopping_the_relay_closes_the_sink(self):
        """A call ending must finish the recording, cleanly or not."""
        handler = self._handler()
        sink = CollectingSink()
        handler.attach_tap(AudioTap("call-1", sink))

        handler.stop()

        assert sink.closed
        assert handler.tap is None

    def test_detach_twice_is_harmless(self):
        handler = self._handler()
        handler.attach_tap(AudioTap("call-1", CollectingSink()))
        handler.detach_tap()
        handler.detach_tap()

    def test_relay_loop_feeds_the_tap(self):
        """
        Drives the real _relay_loop over a loopback socket and asserts both directions land
        in the sink, labelled by the leg they came from.
        """
        import socket

        from pbx.rtp.handler import RTPRelayHandler

        handler = RTPRelayHandler(local_port=0, call_id="call-1")
        relay_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        relay_sock.bind(("127.0.0.1", 0))
        relay_sock.settimeout(0.2)
        handler.socket = relay_sock

        leg_a = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        leg_a.bind(("127.0.0.1", 0))
        leg_b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        leg_b.bind(("127.0.0.1", 0))

        handler.endpoint_a = leg_a.getsockname()
        handler.endpoint_b = leg_b.getsockname()

        sink = CollectingSink()
        tap = AudioTap("call-1", sink)
        handler.attach_tap(tap)

        handler.running = True
        loop = threading.Thread(target=handler._relay_loop, daemon=True)
        loop.start()

        try:
            relay_addr = relay_sock.getsockname()
            leg_a.sendto(rtp(b"a-speaks", sequence=1), relay_addr)
            leg_b.sendto(rtp(b"b-speaks", sequence=2), relay_addr)
            drain(tap, 2)
        finally:
            handler.running = False
            loop.join(timeout=1)
            handler.detach_tap()
            for s in (relay_sock, leg_a, leg_b):
                s.close()

        # The handler emits generation-stamped sources, so a transfer later produces "b1"
        # rather than reusing "b0" for a different person.
        by_source = {f.source: f.payload for f in sink.frames}
        assert by_source.get("a0") == b"a-speaks"
        assert by_source.get("b0") == b"b-speaks"

    def test_bridging_fires_on_bridged_once(self):
        """
        The trigger lives here rather than in the call router so that every path which
        bridges a call is covered, not just the one signalling path someone wired up.
        """
        handler = self._handler()
        fired = []
        handler.on_bridged = fired.append

        handler.set_endpoints(("1.1.1.1", 100), None)
        assert fired == [], "fired with only one endpoint known"

        handler.set_endpoints(None, ("2.2.2.2", 200))
        assert fired == [handler]

        # set_endpoints is called again on every re-INVITE.
        handler.set_endpoints(("1.1.1.1", 100), ("2.2.2.2", 200))
        assert len(fired) == 1, "fired more than once"

    def test_both_endpoints_at_once_fires(self):
        handler = self._handler()
        fired = []
        handler.on_bridged = fired.append

        handler.set_endpoints(("1.1.1.1", 100), ("2.2.2.2", 200))

        assert len(fired) == 1

    def test_an_ivr_leg_never_fires(self):
        """
        Auto attendant, queue hold and voicemail set side A only. Recording an announcement
        being played at somebody is not what call recording means.
        """
        handler = self._handler()
        fired = []
        handler.on_bridged = fired.append

        handler.set_endpoints(("1.1.1.1", 100), None)

        assert fired == []

    def test_current_source_reports_the_party_on_a_side(self):
        """
        What a transfer needs in order to label the new arrival: the source id their channel
        will be opened under.
        """
        handler = self._handler()

        assert handler.current_source("a") == "a0"
        assert handler.current_source("b") == "b0"

        handler.replace_endpoint("b", ("2.2.2.2", 200))

        assert handler.current_source("b") == "b1"
        assert handler.current_source("a") == "a0", "the other side is unaffected"

    def test_replacing_an_endpoint_can_complete_a_bridge(self):
        """
        CallOriginator never calls set_endpoints -- it fills the second side in through
        replace_endpoint. Without notifying here, a click-to-dial call would never record.
        """
        handler = self._handler()
        fired = []
        handler.on_bridged = fired.append

        handler.set_endpoints(("1.1.1.1", 100), None)
        assert fired == []

        handler.replace_endpoint("b", ("2.2.2.2", 200))

        assert len(fired) == 1

    def test_replacing_an_endpoint_on_a_bridged_call_does_not_refire(self):
        """A transfer must not start a second recording of a call already being recorded."""
        handler = self._handler()
        fired = []
        handler.on_bridged = fired.append
        handler.set_endpoints(("1.1.1.1", 100), ("2.2.2.2", 200))

        handler.replace_endpoint("b", ("3.3.3.3", 300))

        assert len(fired) == 1

    def test_a_raising_callback_does_not_break_the_call(self):
        handler = self._handler()

        def explode(_handler):
            raise RuntimeError("recording is broken")

        handler.on_bridged = explode
        handler.set_endpoints(("1.1.1.1", 100), ("2.2.2.2", 200))  # must not raise

        assert handler.endpoint_b == ("2.2.2.2", 200)

    def test_no_callback_is_fine(self):
        handler = self._handler()
        handler.set_endpoints(("1.1.1.1", 100), ("2.2.2.2", 200))

    def test_paused_relay_produces_no_tapped_audio(self):
        """
        Documented behaviour, asserted so it stays documented: hold music pauses the relay
        above the tap, so a recording has a gap rather than the injected music.
        """
        import socket

        from pbx.rtp.handler import RTPRelayHandler

        handler = RTPRelayHandler(local_port=0, call_id="call-1")
        relay_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        relay_sock.bind(("127.0.0.1", 0))
        relay_sock.settimeout(0.2)
        handler.socket = relay_sock

        leg_a = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        leg_a.bind(("127.0.0.1", 0))
        leg_b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        leg_b.bind(("127.0.0.1", 0))
        handler.endpoint_a = leg_a.getsockname()
        handler.endpoint_b = leg_b.getsockname()

        sink = CollectingSink()
        handler.attach_tap(AudioTap("call-1", sink))
        handler.pause_relay()

        handler.running = True
        loop = threading.Thread(target=handler._relay_loop, daemon=True)
        loop.start()

        try:
            leg_a.sendto(rtp(b"muted"), relay_sock.getsockname())
            time.sleep(0.15)
        finally:
            handler.running = False
            loop.join(timeout=1)
            handler.detach_tap()
            for s in (relay_sock, leg_a, leg_b):
                s.close()

        assert sink.frames == []
