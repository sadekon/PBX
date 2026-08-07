"""
The recording notice, and the fail-closed gate behind it.

The tests that matter most here are the ones proving a recording is *destroyed* when the
notice did not play. Recording without notice is the unlawful case this feature exists to
prevent, and it is the case that fails silently -- a broken prompt file still produces
perfectly good audio, which is exactly why it must not be kept.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pbx.features.recording_consent import (
    ANNOUNCE_ALL,
    ANNOUNCE_EXTERNAL,
    ANNOUNCE_OFF,
    SYSTEM_LABEL,
    SYSTEM_SOURCE,
    ConsentAnnouncer,
    ConsentSettings,
)

INTERNAL = {"1512", "1513"}


def _announcer(mode: str = ANNOUNCE_EXTERNAL, **overrides) -> ConsentAnnouncer:
    settings = ConsentSettings.from_dict({"announce_for": mode, **overrides})
    return ConsentAnnouncer(settings, is_internal=lambda n: n in INTERNAL)


@pytest.mark.unit
class TestSettings:
    def test_defaults_to_external_only(self):
        assert ConsentSettings.from_dict({}).announce_for == ANNOUNCE_EXTERNAL

    def test_unknown_mode_is_rejected(self):
        assert ConsentSettings.from_dict({"announce_for": "sometimes"}).validate()

    def test_empty_text_is_rejected(self):
        """The text is both what callers hear and what the transcript records."""
        assert ConsentSettings.from_dict({"text": "  "}).validate()

    def test_off_does_not_require_text(self):
        assert ConsentSettings.from_dict({"announce_for": "off", "text": ""}).validate() == []

    def test_mode_is_case_insensitive(self):
        assert ConsentSettings.from_dict({"announce_for": "ALL"}).announce_for == ANNOUNCE_ALL


@pytest.mark.unit
class TestWhoGetsAnnounced:
    def test_external_call_needs_a_notice(self):
        assert _announcer().required_for("1512", "+15551234567")

    def test_internal_call_does_not(self):
        """Employees signed the phone agreement; the notice is for outside parties."""
        assert not _announcer().required_for("1512", "1513")

    def test_all_mode_announces_internal_calls_too(self):
        assert _announcer(ANNOUNCE_ALL).required_for("1512", "1513")

    def test_off_announces_nothing(self):
        assert not _announcer(ANNOUNCE_OFF).required_for("1512", "+15551234567")

    def test_unknown_numbers_count_as_external(self):
        """The cautious direction: an unresolvable party gets a notice rather than silence."""
        assert _announcer().required_for("1512", "unknown")


@pytest.mark.unit
class TestEveryLegHearsIt:
    """
    Who hears the notice is not a policy question -- both legs always do.

    Playing it only to the outside party means muting the employee so they cannot talk over
    it, which leaves them listening to dead air. The notice is itself the signal to wait.
    """

    def test_both_legs_are_played_to(self, tmp_path):
        import wave

        path = tmp_path / "notice.wav"
        with wave.open(str(path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(8000)
            out.writeframes(b"\x00\x01" * 800)

        announcer = _announcer(audio_file=str(path))
        handler = MagicMock()
        handler.call_id = "call-1"
        handler.running = True
        handler.local_port = 10000
        handler.tap = None
        handler.get_endpoint.return_value = ("10.0.0.5", 4000)

        announcer._play(handler, lambda _ok: None)

        sides = [c[0][0] for c in handler.get_endpoint.call_args_list]
        assert set(sides) == {"a", "b"}

    def test_announcer_exposes_no_per_leg_choice(self):
        """A side argument would be a way to configure the dead-air problem back in."""
        assert not hasattr(_announcer(), "external_side")


@pytest.mark.unit
class TestPlayback:
    def _handler(self, tmp_path: Path) -> MagicMock:
        handler = MagicMock()
        handler.call_id = "call-1"
        handler.running = True
        handler.local_port = 10000
        handler.get_endpoint.return_value = ("10.0.0.5", 4000)
        handler.tap = None
        return handler

    def test_missing_audio_reports_failure(self, tmp_path):
        """No prompt file means no notice, which must not be reported as success."""
        announcer = _announcer(audio_file=str(tmp_path / "absent.wav"))
        results = []

        announcer._play(self._handler(tmp_path), results.append)

        assert results == [False]
        assert announcer.failed == 1

    def test_a_stopped_relay_reports_failure(self, tmp_path):
        wav = tmp_path / "notice.wav"
        wav.write_bytes(b"RIFF")
        announcer = _announcer(audio_file=str(wav))
        handler = self._handler(tmp_path)
        handler.running = False
        results = []

        announcer._play(handler, results.append)

        assert results == [False]

    def test_a_callback_that_raises_does_not_escape(self, tmp_path):
        """This runs on its own thread; an escaping exception would be lost and unlogged."""
        announcer = _announcer(audio_file=str(tmp_path / "absent.wav"))

        def boom(_ok: bool) -> None:
            raise RuntimeError("callback failed")

        announcer._play(self._handler(tmp_path), boom)  # must not raise


@pytest.mark.unit
class TestTapFeeding:
    """The notice has to reach the recording, or the tape cannot evidence that it was given."""

    def _wav(self, path: Path, seconds: float = 1.0) -> Path:
        import wave

        with wave.open(str(path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(8000)
            out.writeframes(b"\x00\x01" * int(8000 * seconds))
        return path

    def test_notice_is_fed_as_twenty_millisecond_frames(self, tmp_path):
        """
        Frames, not one blob: a channel places audio by RTP timestamp, so a single frame
        claiming to span seconds would misreport where the notice sits.
        """
        wav = self._wav(tmp_path / "notice.wav")
        announcer = _announcer(audio_file=str(wav))
        handler = MagicMock()
        handler.tap = MagicMock()

        announcer._feed_tap(handler, wav)

        frames = [c[0][0] for c in handler.tap.feed_frame.call_args_list]
        assert len(frames) == 50, "1 second at 20 ms per frame"
        assert all(f.source == SYSTEM_SOURCE for f in frames)
        assert [f.timestamp for f in frames[:3]] == [0, 160, 320]

    def test_no_tap_is_survivable(self, tmp_path):
        wav = self._wav(tmp_path / "notice.wav")
        handler = MagicMock()
        handler.tap = None

        _announcer(audio_file=str(wav))._feed_tap(handler, wav)  # must not raise

    def test_unreadable_audio_never_stops_playback(self, tmp_path):
        """Failing to record the notice is no reason to withhold it from the person it protects."""
        bad = tmp_path / "notice.wav"
        bad.write_bytes(b"not a wav")
        handler = MagicMock()
        handler.tap = MagicMock()

        _announcer(audio_file=str(bad))._feed_tap(handler, bad)

        handler.tap.feed_frame.assert_not_called()

    def test_stereo_audio_is_refused(self, tmp_path):
        """A channel holds one voice; a stereo prompt would be written as interleaved noise."""
        import wave

        path = tmp_path / "stereo.wav"
        with wave.open(str(path), "wb") as out:
            out.setnchannels(2)
            out.setsampwidth(2)
            out.setframerate(8000)
            out.writeframes(b"\x00\x01" * 1600)
        handler = MagicMock()
        handler.tap = MagicMock()

        _announcer(audio_file=str(path))._feed_tap(handler, path)

        handler.tap.feed_frame.assert_not_called()


@pytest.mark.unit
class TestSystemLabel:
    def test_system_source_cannot_collide_with_a_participant(self):
        """
        Participants are "a0"/"b0" with the generation bumped per transfer, so the notice
        needs an id outside that space or a transfer could land a party on its channel.
        """
        assert not SYSTEM_SOURCE.startswith(("a", "b"))
        assert SYSTEM_LABEL != SYSTEM_SOURCE

    def test_retention_and_transcription_agree_on_the_label(self):
        import pbx.speech.recording as transcriber

        assert transcriber.SYSTEM_LABEL == SYSTEM_LABEL


@pytest.mark.unit
class TestFailClosed:
    """
    Audio captured before notice was established is audio we had no right to keep.

    A broken prompt still produces perfectly good recordings, so nothing about this failure
    is visible in the output -- which is exactly why the recording must be destroyed rather
    than finished.
    """

    def _system(self, tmp_path: Path):
        from pbx.features.call_recording import CallRecordingSystem

        return CallRecordingSystem(
            recording_path=str(tmp_path / "recordings"),
            requested=True,
        )

    def test_abandon_writes_nothing_to_disk(self, tmp_path):
        system = self._system(tmp_path)
        system.start_recording("call-1", "1512", "+15551234567", session_id="s-1")

        assert system.abandon("s-1", "notice failed")

        recordings = tmp_path / "recordings"
        assert not list(recordings.glob("*.wav"))
        assert not list(recordings.glob("*.json"))

    def test_abandon_removes_it_from_the_active_set(self, tmp_path):
        system = self._system(tmp_path)
        system.start_recording("call-1", "1512", "+15551234567", session_id="s-1")

        system.abandon("s-1", "notice failed")

        assert system.get("s-1") is None

    def test_abandoning_an_unknown_recording_is_survivable(self, tmp_path):
        assert not self._system(tmp_path).abandon("nope", "notice failed")

    def test_abandon_does_not_trigger_transcription(self, tmp_path):
        """A discarded recording must not reach the transcriber, which would rewrite it."""
        system = self._system(tmp_path)
        finished = []
        system.on_recording_finished = lambda *args: finished.append(args)
        system.start_recording("call-1", "1512", "+15551234567", session_id="s-1")

        system.abandon("s-1", "notice failed")

        assert finished == []


@pytest.mark.unit
class TestInjectedAudioReachesTheRecorder:
    def test_a_fed_frame_lands_on_its_own_channel(self, tmp_path):
        """
        RTPPlayer writes straight to the socket, so without feed_frame the notice is absent
        from its own recording -- which is how music-on-hold has always been missing.
        """
        from pbx.features.call_recording import CallRecordingSystem
        from pbx.rtp.tap import RtpFrame

        system = CallRecordingSystem(
            recording_path=str(tmp_path / "recordings"),
            requested=True,
        )
        tap = system.start_recording("call-1", "1512", "+15551234567", session_id="s-1")
        assert tap is not None

        recording = system.get("s-1")
        for index in range(10):
            recording.write(
                RtpFrame(
                    source=SYSTEM_SOURCE,
                    timestamp=index * 160,
                    payload_type=11,
                    sequence=index,
                    payload=b"\x00\x01" * 160,
                )
            )

        assert SYSTEM_SOURCE in {c["source"] for c in recording.channel_map()}

    def test_l16_is_decoded_rather_than_counted_undecodable(self, tmp_path):
        """PT 11 is how locally injected PCM reaches the tape without an encode round trip."""
        from pbx.features.call_recording import _Channel

        channel = _Channel(tmp_path / "spool.raw")
        samples = channel.decode(b"\x00\x01" * 160, 11)

        assert samples is not None
        assert len(samples) == 160
        assert channel.undecodable_packets == 0
