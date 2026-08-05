"""
Two-channel call recording.

The behaviour worth pinning here is alignment. A recorder that simply concatenates arriving
packets looks correct on a short test and drifts on a real call, because silence suppression
and packet loss leave holes on one leg and not the other. Several tests below deliberately
feed gaps, late packets and timestamp discontinuities and assert on *where* the samples land,
not merely that a file appeared.
"""

import json
import struct
import wave
from pathlib import Path

import numpy as np
import pytest

from pbx.features.call_recording import SAMPLE_RATE, CallRecording, CallRecordingSystem
from pbx.rtp.tap import RtpFrame
from pbx.utils.audio import samples_to_alaw, samples_to_ulaw

#: One 20 ms G.711 frame.
FRAME_SAMPLES = 160


def tone(value: int = 8000, count: int = FRAME_SAMPLES) -> np.ndarray:
    return np.full(count, value, dtype=np.int16)


def frame(
    source: str,
    timestamp: int,
    *,
    value: int = 8000,
    payload_type: int = 0,
    samples: int = FRAME_SAMPLES,
) -> RtpFrame:
    """One tapped G.711 frame carrying a constant tone."""
    pcm = tone(value, samples)
    payload = samples_to_alaw(pcm) if payload_type == 8 else samples_to_ulaw(pcm)
    return RtpFrame(
        source=source,
        timestamp=timestamp,
        payload_type=payload_type,
        sequence=(timestamp // samples) & 0xFFFF,
        payload=payload,
    )


def rtp_packet(value: int = 8000) -> bytes:
    """A wire-format packet, for driving a tap rather than the sink directly."""
    return struct.pack("!BBHII", 0x80, 0, 1, 0, 0xDEADBEEF) + samples_to_ulaw(tone(value))


def read_channels(path) -> list[np.ndarray]:
    """
    Every channel of a WAV, as int16 arrays, in channel order.

    Returns a list rather than a pair because a recording has as many channels as the call
    had participants -- two only in the common case.
    """
    with wave.open(str(path), "rb") as wf:
        channels = wf.getnchannels()
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == SAMPLE_RATE
        raw = wf.readframes(wf.getnframes())

    interleaved = np.frombuffer(raw, dtype="<i2")
    return [interleaved[n::channels] for n in range(channels)]


def read_manifest(path) -> dict:
    """The sidecar describing which channel holds whom."""
    return json.loads(Path(path).with_suffix(".json").read_text())


@pytest.fixture
def recording(tmp_path):
    rec = CallRecording("call-1", str(tmp_path))
    rec.start("1001", "1002")
    return rec


@pytest.mark.unit
class TestOutputFormat:
    def test_writes_a_stereo_8k_wav(self, recording):
        recording.write(frame("a", 0))
        recording.write(frame("b", 0))
        path = recording.stop()

        assert path is not None
        left, right = read_channels(path)
        assert len(left) == len(right) == FRAME_SAMPLES

    def test_caller_is_left_and_callee_is_right(self, recording):
        recording.write(frame("a", 0, value=10000))
        recording.write(frame("b", 0, value=-10000))
        left, right = read_channels(recording.stop())

        assert left[0] > 5000
        assert right[0] < -5000

    def test_filename_keeps_the_existing_convention(self, recording):
        assert "1001_to_1002_" in recording.file_path.name
        assert recording.file_path.name.endswith(".wav")

    def test_spool_files_are_cleaned_up(self, recording, tmp_path):
        recording.write(frame("a", 0))
        recording.stop()

        assert list(tmp_path.glob("*.pcm")) == []

    def test_a_call_with_no_audio_writes_no_file(self, recording):
        """An empty WAV is worse than none: retention keeps it, a transcriber opens it."""
        assert recording.stop() is None
        assert not recording.file_path.exists()

    def test_alaw_is_decoded_too(self, recording):
        recording.write(frame("a", 0, value=9000, payload_type=8))
        left = read_channels(recording.stop())[0]

        assert abs(int(left[0]) - 9000) < 600


@pytest.mark.unit
class TestAlignment:
    def test_a_gap_is_filled_with_silence(self, recording):
        """
        Two packets one second apart must land one second apart, not back to back. This is
        the whole reason alignment uses timestamps rather than arrival order.
        """
        recording.write(frame("a", 0, value=10000))
        recording.write(frame("a", SAMPLE_RATE, value=10000))
        left = read_channels(recording.stop())[0]

        assert len(left) == SAMPLE_RATE + FRAME_SAMPLES
        assert left[0] > 5000
        assert left[FRAME_SAMPLES + 10] == 0
        assert left[SAMPLE_RATE] > 5000

    def test_consecutive_packets_do_not_gap(self, recording):
        for n in range(5):
            recording.write(frame("a", n * FRAME_SAMPLES, value=10000))
        left = read_channels(recording.stop())[0]

        assert len(left) == 5 * FRAME_SAMPLES
        assert np.all(left > 5000), "silence appeared between contiguous packets"

    def test_both_channels_end_the_same_length(self, recording):
        """The shorter leg is padded, or the file would be ragged."""
        recording.write(frame("a", 0))
        recording.write(frame("a", FRAME_SAMPLES))
        recording.write(frame("b", 0))
        left, right = read_channels(recording.stop())

        assert len(left) == len(right)

    def test_a_late_packet_is_dropped_not_misplaced(self, recording):
        """Placing it would mean seeking back over silence already committed."""
        recording.write(frame("a", SAMPLE_RATE, value=10000))
        recording.write(frame("a", 0, value=10000))
        left = read_channels(recording.stop())[0]

        assert len(left) == FRAME_SAMPLES

    def test_a_huge_jump_resyncs_instead_of_filling(self, recording):
        """
        A hold-and-resume restarts the timestamp series. Filling that literally would write
        hours of zeros off a single packet.
        """
        recording.write(frame("a", 0, value=10000))
        recording.write(frame("a", 10 * 3600 * SAMPLE_RATE, value=10000))
        left = read_channels(recording.stop())[0]

        assert len(left) == 2 * FRAME_SAMPLES

    def test_timestamp_wraparound_is_not_a_gap(self, recording):
        """32-bit timestamps wrap; a naive subtraction reads that as a huge hole."""
        start = 0xFFFFFFFF - FRAME_SAMPLES
        recording.write(frame("a", start, value=10000))
        recording.write(frame("a", (start + FRAME_SAMPLES) & 0xFFFFFFFF, value=10000))
        left = read_channels(recording.stop())[0]

        assert len(left) == 2 * FRAME_SAMPLES

    def test_a_leg_that_starts_late_is_offset(self, recording, monkeypatch):
        """
        A callee who answers five seconds in belongs five seconds into the file. Timestamps
        from the two legs are not comparable, so this uses arrival time.
        """
        clock = {"now": 1000.0}
        monkeypatch.setattr("pbx.features.call_recording.time.monotonic", lambda: clock["now"])

        recording.write(frame("a", 0, value=10000))
        clock["now"] += 5.0
        recording.write(frame("b", 0, value=10000))

        left, right = read_channels(recording.stop())

        assert right[0] == 0, "late leg should start with silence"
        assert abs(int(right[5 * SAMPLE_RATE])) > 5000
        assert left[0] != 0


@pytest.mark.unit
class TestRobustness:
    def test_an_undecodable_codec_is_counted_not_fatal(self, recording):
        """G.729 can be negotiated and has no decoder here; that leg records silence."""
        recording.write(
            RtpFrame(source="a", timestamp=0, payload_type=18, sequence=0, payload=b"x" * 20)
        )
        recording.write(frame("b", 0, value=9000))

        path = recording.stop()

        assert path is not None
        _, right = read_channels(path)
        assert abs(int(right[0])) > 5000

    def test_write_before_start_is_ignored(self, tmp_path):
        rec = CallRecording("call-1", str(tmp_path))

        rec.write(frame("a", 0))  # must not raise

        assert rec.stop() is None

    def test_an_unseen_source_opens_a_channel(self, recording):
        """
        There is no such thing as an invalid source any more. A string not seen before is a
        participant not seen before, which is exactly how a transfer and a conference join
        both arrive.
        """
        recording.write(frame("a0", 0))
        recording.write(frame("b0", 0))
        recording.write(frame("b1", 0))

        channels = read_channels(recording.stop())

        assert len(channels) == 3

    def test_the_channel_ceiling_is_enforced(self, tmp_path):
        """A source id that changed unexpectedly would otherwise open a spool per packet."""
        from pbx.features.call_recording import MAX_CHANNELS

        rec = CallRecording("call-1", str(tmp_path))
        rec.start("1001", "1002")

        for n in range(MAX_CHANNELS + 10):
            rec.write(frame(f"src{n}", 0))

        channels = read_channels(rec.stop())

        assert len(channels) == MAX_CHANNELS

    def test_starting_twice_returns_none(self, recording):
        assert recording.start("1001", "1002") is None

    def test_stopping_twice_returns_none(self, recording):
        recording.write(frame("a", 0))
        assert recording.stop() is not None
        assert recording.stop() is None

    def test_close_finishes_the_file(self, recording):
        """close() is what the tap calls; it must produce the file stop() would."""
        recording.write(frame("a", 0))
        recording.close()

        assert recording.file_path.exists()

    def test_duration_comes_from_the_audio(self, recording):
        for n in range(50):
            recording.write(frame("a", n * FRAME_SAMPLES))

        recording.stop()

        assert recording.get_duration() == pytest.approx(1.0, abs=0.05)

    def test_a_long_call_stays_correct_across_chunk_seams(self, recording):
        """Interleaving is chunked at 8000 samples; the seams must not drop or duplicate."""
        packets = 200  # 4 seconds, crossing several chunk boundaries
        for n in range(packets):
            recording.write(frame("a", n * FRAME_SAMPLES, value=10000))
            recording.write(frame("b", n * FRAME_SAMPLES, value=-10000))

        left, right = read_channels(recording.stop())

        assert len(left) == packets * FRAME_SAMPLES
        assert np.all(left > 5000)
        assert np.all(right < -5000)


@pytest.mark.unit
class TestCallRecordingSystem:
    def test_start_returns_a_tap(self, tmp_path):
        system = CallRecordingSystem(str(tmp_path))

        tap = system.start_recording("call-1", "1001", "1002")

        assert tap is not None
        assert system.is_recording("call-1")
        tap.stop()

    def test_the_tap_drives_the_recording(self, tmp_path):
        system = CallRecordingSystem(str(tmp_path))
        tap = system.start_recording("call-1", "1001", "1002")
        tap.start()

        tap.feed("a", rtp_packet())
        tap.stop()

        assert list(tmp_path.glob("*.wav"))

    def test_starting_the_same_call_twice_returns_none(self, tmp_path):
        system = CallRecordingSystem(str(tmp_path))
        first = system.start_recording("call-1", "1001", "1002")

        assert system.start_recording("call-1", "1001", "1002") is None
        first.stop()

    def test_stop_records_metadata(self, tmp_path):
        system = CallRecordingSystem(str(tmp_path))
        tap = system.start_recording("call-1", "1001", "1002")
        tap.start()
        tap.feed("a", rtp_packet())
        tap.stop()

        system.stop_recording("call-1")

        assert not system.is_recording("call-1")

    def test_stopping_an_unknown_call_is_harmless(self, tmp_path):
        assert CallRecordingSystem(str(tmp_path)).stop_recording("nope") is None


@pytest.mark.unit
class TestMultipleParticipants:
    """
    Channels are per participant, not per side of a bridge. The two stop being the same
    thing the moment a call is transferred or a third party joins.
    """

    def test_each_source_gets_its_own_channel(self, recording):
        recording.write(frame("a0", 0, value=10000))
        recording.write(frame("b0", 0, value=-10000))
        recording.write(frame("c0", 0, value=5000))

        channels = read_channels(recording.stop())

        assert len(channels) == 3
        assert channels[0][0] > 5000
        assert channels[1][0] < -5000
        assert 1000 < channels[2][0] < 9000

    def test_channel_order_is_order_first_heard(self, recording):
        recording.write(frame("b0", 0, value=-10000))
        recording.write(frame("a0", 0, value=10000))

        path = recording.stop()

        assert [c["source"] for c in read_manifest(path)["channels"]] == ["b0", "a0"]

    def test_every_channel_is_the_same_length(self, recording):
        """A participant who joined late or left early still shares the one timeline."""
        recording.write(frame("a0", 0))
        recording.write(frame("a0", FRAME_SAMPLES))
        recording.write(frame("a0", 2 * FRAME_SAMPLES))
        recording.write(frame("b0", 0))

        channels = read_channels(recording.stop())

        assert len({len(c) for c in channels}) == 1


@pytest.mark.unit
class TestTransfers:
    """
    A transfer replaces one party with a different human on the same relay side. Before
    source ids carried a generation, both landed on one channel with nothing marking the
    handover -- output that looked correctly attributed and was not.
    """

    def test_a_replaced_party_gets_a_new_channel(self, recording):
        recording.write(frame("a0", 0, value=10000))  # Alice
        recording.write(frame("b0", 0, value=-10000))  # Bob
        # Transfer: Bob leaves, Carol arrives on the same side.
        recording.write(frame("b1", 2 * FRAME_SAMPLES, value=6000))  # Carol

        channels = read_channels(recording.stop())

        assert len(channels) == 3, "Carol was spliced onto Bob's channel"

    def test_the_departed_party_channel_stops(self, recording, monkeypatch):
        """
        Carol's channel starts where she joined, and Bob's is silent from then on. Note the
        clock is driven rather than the timestamps: sources have independent timestamp
        origins, so Carol's position comes from when she arrived, not from her RTP clock.
        """
        clock = {"now": 500.0}
        monkeypatch.setattr("pbx.features.call_recording.time.monotonic", lambda: clock["now"])

        recording.write(frame("b0", 0, value=-10000))  # Bob
        clock["now"] += 2.0
        recording.write(frame("b1", 0, value=6000))  # Carol, two seconds later

        bob, carol = read_channels(recording.stop())

        assert abs(int(bob[0])) > 5000, "Bob should be speaking at the start"
        assert bob[2 * SAMPLE_RATE] == 0, "Bob should be silent after handing over"
        assert carol[0] == 0, "Carol should not exist before she joined"
        assert abs(int(carol[2 * SAMPLE_RATE])) > 3000

    def test_labels_name_the_participants(self, tmp_path):
        rec = CallRecording("call-1", str(tmp_path), labels={"a0": "1001", "b0": "1002"})
        rec.start("1001", "1002")
        rec.write(frame("a0", 0))
        rec.write(frame("b0", 0))
        rec.label("b1", "1003")
        rec.write(frame("b1", FRAME_SAMPLES))

        manifest = read_manifest(rec.stop())

        assert [c["label"] for c in manifest["channels"]] == ["1001", "1002", "1003"]

    def test_an_unlabelled_source_falls_back_to_its_id(self, recording):
        recording.write(frame("b7", 0))

        manifest = read_manifest(recording.stop())

        assert manifest["channels"][0]["label"] == "b7"


@pytest.mark.unit
class TestSessionIdentity:
    def test_the_manifest_carries_the_session(self, tmp_path):
        rec = CallRecording("leg-2", str(tmp_path), session_id="conversation-1")
        rec.start("1001", "1002")
        rec.write(frame("a0", 0))

        manifest = read_manifest(rec.stop())

        assert manifest["session_id"] == "conversation-1"
        assert manifest["call_id"] == "leg-2"

    def test_the_session_names_the_file(self, tmp_path):
        rec = CallRecording("leg-2", str(tmp_path), session_id="conversation-1")
        rec.start("1001", "1002")

        assert "conversation-1" in rec.file_path.name

    def test_session_defaults_to_the_call_id(self, tmp_path):
        assert CallRecording("call-1", str(tmp_path)).session_id == "call-1"

    def test_recordings_are_keyed_by_session_not_leg(self, tmp_path):
        """
        Two legs of one conversation must not each start a recording, or a transferred call
        produces two files with nothing linking them.
        """
        system = CallRecordingSystem(str(tmp_path))
        first = system.start_recording("leg-1", "1001", "1002", session_id="conv-1")

        second = system.start_recording("leg-2", "1002", "1003", session_id="conv-1")

        assert first is not None
        assert second is None, "a second leg started a competing recording"
        first.stop()

    def test_labelling_through_the_system(self, tmp_path):
        system = CallRecordingSystem(str(tmp_path))
        tap = system.start_recording("leg-1", "1001", "1002", session_id="conv-1")

        system.label("conv-1", "b1", "1003")

        assert system.get("conv-1").labels["b1"] == "1003"
        tap.stop()

    def test_labelling_an_unknown_session_is_harmless(self, tmp_path):
        CallRecordingSystem(str(tmp_path)).label("nope", "b1", "1003")


@pytest.mark.unit
class TestRelayTriggersRecording:
    """
    Recording is started by the RTP layer, not the call router.

    The router-level version only covered the one signalling path it was added to -- WebRTC
    and PBX-originated calls silently recorded nothing. Anything that bridges two endpoints
    goes through the relay, so triggering there covers every path by construction.
    """

    def _pbx(self, tmp_path, *, consent=True):
        from types import SimpleNamespace

        from pbx.core.feature_initializer import FeatureInitializer
        from pbx.rtp.handler import RTPRelay

        call = SimpleNamespace(from_extension="1001", to_extension="1002", session_id="conv-1")
        pbx = SimpleNamespace(
            rtp_relay=RTPRelay(port_range_start=30000, port_range_end=30100),
            recording_system=CallRecordingSystem(
                str(tmp_path), auto_record=True, consent_acknowledged=consent
            ),
            call_manager=SimpleNamespace(get_call=lambda _id: call),
        )
        FeatureInitializer._wire_call_recording(pbx)
        return pbx

    def _handler(self, pbx, call_id="call-1"):
        from pbx.rtp.handler import RTPRelayHandler

        handler = RTPRelayHandler(local_port=0, call_id=call_id)
        handler.on_bridged = pbx.rtp_relay.on_bridged
        return handler

    def test_bridging_starts_a_recording(self, tmp_path):
        pbx = self._pbx(tmp_path)
        handler = self._handler(pbx)

        handler.set_endpoints(("1.1.1.1", 100), ("2.2.2.2", 200))

        assert pbx.recording_system.is_recording("conv-1")
        assert handler.tap is not None
        handler.detach_tap()

    def test_it_is_keyed_by_session_and_labelled(self, tmp_path):
        pbx = self._pbx(tmp_path)
        handler = self._handler(pbx)

        handler.set_endpoints(("1.1.1.1", 100), ("2.2.2.2", 200))
        recording = pbx.recording_system.get("conv-1")

        assert recording.session_id == "conv-1"
        assert recording.labels == {"a0": "1001", "b0": "1002"}
        handler.detach_tap()

    def test_an_ivr_leg_starts_nothing(self, tmp_path):
        pbx = self._pbx(tmp_path)
        handler = self._handler(pbx)

        handler.set_endpoints(("1.1.1.1", 100), None)

        assert not pbx.recording_system.is_recording("conv-1")
        assert handler.tap is None

    def test_consent_off_records_nothing(self, tmp_path):
        pbx = self._pbx(tmp_path, consent=False)
        handler = self._handler(pbx)

        handler.set_endpoints(("1.1.1.1", 100), ("2.2.2.2", 200))

        assert handler.tap is None

    def test_audio_reaches_the_file_end_to_end(self, tmp_path):
        """Bridge, feed real packets through the tap, hang up, read the WAV back."""
        pbx = self._pbx(tmp_path)
        handler = self._handler(pbx)
        handler.set_endpoints(("1.1.1.1", 100), ("2.2.2.2", 200))

        tap = handler.tap
        for _ in range(5):
            tap.feed("a0", rtp_packet(10000))
            tap.feed("b0", rtp_packet(-10000))
        handler.stop()

        wavs = list(tmp_path.glob("*.wav"))
        assert len(wavs) == 1
        channels = read_channels(wavs[0])
        assert len(channels) == 2
        assert channels[0][0] > 5000
        assert channels[1][0] < -5000
        assert read_manifest(wavs[0])["session_id"] == "conv-1"

    def test_new_relays_inherit_the_hook(self, tmp_path):
        """A handler built by allocate_relay must carry the callback, not just a hand-made one."""
        pbx = self._pbx(tmp_path)

        ports = pbx.rtp_relay.allocate_relay("call-9")

        assert ports is not None
        handler = pbx.rtp_relay.get_handler("call-9")
        assert handler.on_bridged is not None
        pbx.rtp_relay.release_relay("call-9")


@pytest.mark.unit
class TestConsentGate:
    """
    features.call_recording has been true in config.yml the whole time this feature did
    nothing. Wiring the tap turned that dormant flag into "record every call", which nobody
    opted into by leaving a config file alone -- so recording needs a second, deliberate key.
    """

    def test_the_feature_flag_alone_does_not_record(self, tmp_path):
        system = CallRecordingSystem(str(tmp_path), auto_record=True)

        assert system.requested is True
        assert system.auto_record is False, "recording without acknowledged consent"

    def test_both_switches_on_records(self, tmp_path):
        system = CallRecordingSystem(str(tmp_path), auto_record=True, consent_acknowledged=True)

        assert system.auto_record is True

    def test_consent_alone_does_not_record(self, tmp_path):
        system = CallRecordingSystem(str(tmp_path), consent_acknowledged=True)

        assert system.auto_record is False

    def test_default_is_off(self, tmp_path):
        assert CallRecordingSystem(str(tmp_path)).auto_record is False

    def test_the_gate_is_warned_about_at_startup(self, tmp_path):
        """A silently disabled feature is worse than a noisy one; say why nothing records."""
        from unittest.mock import MagicMock, patch

        logger = MagicMock()
        with patch("pbx.features.call_recording.get_logger", return_value=logger):
            CallRecordingSystem(str(tmp_path), auto_record=True)

        warning = " ".join(str(c) for c in logger.warning.call_args_list)
        assert "consent" in warning.lower()
