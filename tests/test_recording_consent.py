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
        try:
            samples = channel.decode(b"\x00\x01" * 160, 11)

            assert samples is not None
            assert len(samples) == 160
            assert channel.undecodable_packets == 0
        finally:
            # Holds an open spool; leaking it surfaces as an unraisable exception at GC.
            channel.discard()


@pytest.mark.unit
class TestOneStreamOnTheWire:
    """
    Regressions from the first version, all of which showed up only on real calls.

    Injecting a second RTP stream into a live session puts two SSRCs on one port, and phones
    lock onto whichever they saw first -- so the notice reached one end, the other, or neither,
    differing per call. And playing to the legs in turn meant the second party heard it only
    once the first had finished.
    """

    def _handler(self, notice: Path) -> MagicMock:
        handler = MagicMock()
        handler.call_id = "call-1"
        handler.running = True
        handler.local_port = 10000
        handler.tap = None
        handler.get_endpoint.side_effect = lambda side: {
            "a": ("10.0.0.1", 4000),
            "b": ("10.0.0.2", 5000),
        }[side]
        handler.sent = []
        handler.socket.sendto.side_effect = lambda data, target: handler.sent.append((target, data))
        return handler

    def _notice(self, path: Path, seconds: float = 0.2) -> Path:
        import wave

        with wave.open(str(path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(8000)
            out.writeframes(b"\x10\x02" * int(8000 * seconds))
        return path

    def _play(self, tmp_path: Path):
        notice = self._notice(tmp_path / "notice.wav")
        handler = self._handler(notice)
        announcer = _announcer(audio_file=str(notice))
        assert announcer._play_to_sides(handler, ["a", "b"]) is True
        return handler

    def test_the_relay_is_muted_while_it_plays(self, tmp_path):
        """Otherwise two SSRCs share the port, and both parties can talk over the notice."""
        handler = self._play(tmp_path)

        handler.pause_relay.assert_called_once()
        handler.resume_relay.assert_called_once()

    def test_the_relay_resumes_even_if_sending_fails(self, tmp_path):
        """A paused relay left paused is a call with no audio for the rest of its life."""
        notice = self._notice(tmp_path / "notice.wav")
        handler = self._handler(notice)
        handler.socket.sendto.side_effect = RuntimeError("network gone")

        with pytest.raises(RuntimeError):
            _announcer(audio_file=str(notice))._play_to_sides(handler, ["a", "b"])

        handler.resume_relay.assert_called_once()

    def test_one_ssrc_for_the_whole_notice(self, tmp_path):
        handler = self._play(tmp_path)

        assert len({data[8:12] for _, data in handler.sent}) == 1

    def test_both_legs_receive_the_same_packets_in_lockstep(self, tmp_path):
        """Sequential playback meant the second party heard it after the first had finished."""
        handler = self._play(tmp_path)

        by_leg = {}
        for target, data in handler.sent:
            by_leg.setdefault(target[0], []).append(data)

        assert set(by_leg) == {"10.0.0.1", "10.0.0.2"}
        assert by_leg["10.0.0.1"] == by_leg["10.0.0.2"]

    def test_it_goes_out_as_ulaw(self, tmp_path):
        """
        PT 0 is the one codec every endpoint here negotiates. RTPPlayer.play_file re-encodes
        16-bit PCM to G.722, which a phone that agreed on µ-law cannot decode -- that alone
        made the notice inaudible.
        """
        handler = self._play(tmp_path)

        payload_types = {data[1] for _, data in handler.sent}
        assert payload_types == {0}
        assert all(len(data) - 12 == 160 for _, data in handler.sent)

    def test_timestamps_advance_one_frame_per_packet(self, tmp_path):
        import struct

        handler = self._play(tmp_path)
        stamps = [
            struct.unpack("!I", data[4:8])[0] for t, data in handler.sent if t[0] == "10.0.0.1"
        ]

        assert stamps == [i * 160 for i in range(len(stamps))]

    def test_no_endpoints_means_no_playback_and_no_mute(self, tmp_path):
        """Pausing a relay we are not about to play into would just be dead air."""
        notice = self._notice(tmp_path / "notice.wav")
        handler = self._handler(notice)
        handler.get_endpoint.side_effect = lambda side: None

        assert _announcer(audio_file=str(notice))._play_to_sides(handler, ["a", "b"]) is False
        handler.pause_relay.assert_not_called()


@pytest.mark.unit
class TestCodecConversion:
    def test_sixteen_bit_pcm_is_converted(self, tmp_path):
        import wave

        path = tmp_path / "pcm.wav"
        with wave.open(str(path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(8000)
            out.writeframes(b"\x10\x02" * 800)

        payload = _announcer(audio_file=str(path))._ulaw_payload(path)

        assert payload is not None
        assert len(payload) == 800, "one byte per sample once µ-law encoded"

    def test_eight_bit_audio_is_passed_through(self, tmp_path):
        """A telephony prompt that is already 8-bit is already µ-law."""
        import wave

        path = tmp_path / "ulaw.wav"
        with wave.open(str(path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(1)
            out.setframerate(8000)
            out.writeframes(b"\xff" * 800)

        assert _announcer(audio_file=str(path))._ulaw_payload(path) == b"\xff" * 800

    def test_wideband_audio_is_resampled(self, tmp_path):
        import wave

        path = tmp_path / "wide.wav"
        with wave.open(str(path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(16000)
            out.writeframes(b"\x10\x02" * 1600)

        payload = _announcer(audio_file=str(path))._ulaw_payload(path)

        assert payload is not None
        assert len(payload) == 800, "16 kHz halves to 8 kHz"

    def test_stereo_is_refused(self, tmp_path):
        import wave

        path = tmp_path / "stereo.wav"
        with wave.open(str(path), "wb") as out:
            out.setnchannels(2)
            out.setsampwidth(2)
            out.setframerate(8000)
            out.writeframes(b"\x10\x02" * 1600)

        assert _announcer(audio_file=str(path))._ulaw_payload(path) is None


@pytest.mark.unit
class TestInternalLookupContract:
    """
    The lookup that decides who is external.

    This got it wrong once in the worst possible way: the code called
    ``extension_db.get_extension()``, which does not exist, a bare ``except Exception``
    swallowed the AttributeError, and every party resolved as external -- so the notice played
    on internal calls while looking like a deliberate policy. These tests pin the method name
    and prove a failed lookup is loud.
    """

    def test_extension_db_exposes_the_method_we_call(self):
        """A rename here silently changes who gets announced to."""
        from pbx.utils.database import ExtensionDB

        assert hasattr(ExtensionDB, "get")
        assert not hasattr(ExtensionDB, "get_extension"), (
            "if this appears, confirm which one feature_initializer.is_internal calls"
        )

    def test_config_exposes_the_fallback_we_call(self):
        from pbx.utils.config import Config

        assert hasattr(Config, "get_extension")

    def _is_internal(self, pbx_core, config):
        """Build the real closure the initializer installs."""
        from pbx.core.feature_initializer import FeatureInitializer

        FeatureInitializer._build_consent_announcer(pbx_core, config, MagicMock())
        return pbx_core.consent_announcer._is_internal

    def _pbx(self, extension_db=None):
        pbx_core = MagicMock()
        pbx_core.extension_db = extension_db
        return pbx_core

    def _config(self, extensions=()):
        config = MagicMock()
        config.get.return_value = {"announce_for": "external"}
        config.get_extension.side_effect = lambda n: (
            {"number": str(n)} if str(n) in extensions else None
        )
        return config

    def test_a_database_extension_is_internal(self):
        db = MagicMock()
        db.get.side_effect = lambda n: {"number": n} if n == "1512" else None

        is_internal = self._is_internal(self._pbx(db), self._config())

        assert is_internal("1512")
        assert not is_internal("+15551234567")

    def test_a_config_only_extension_is_internal(self):
        """Installs without a database still have extensions in config.yml."""
        is_internal = self._is_internal(self._pbx(None), self._config(extensions={"1513"}))

        assert is_internal("1513")

    def test_a_broken_database_falls_through_to_config(self):
        """The original bug: an exception here must not decide that everyone is external."""
        db = MagicMock()
        db.get.side_effect = AttributeError("no such method")

        is_internal = self._is_internal(self._pbx(db), self._config(extensions={"1512"}))

        assert is_internal("1512")

    def test_unknown_is_external(self):
        is_internal = self._is_internal(self._pbx(None), self._config())

        assert not is_internal("unknown")
        assert not is_internal("")


@pytest.mark.unit
class TestNoticeLog:
    """
    The record that a caller was told.

    Counters live in memory and the audio expires, so without this there is no durable proof
    that notice was given -- for the one feature whose entire purpose is being able to show it.
    Deliberately not swept by retention: a call expiring at 90 days must not take the evidence
    justifying it along too.
    """

    def _db(self):
        db = MagicMock()
        db.enabled = True
        db.execute.return_value = True
        db.fetch_all.return_value = []
        return db

    def _handler(self):
        handler = MagicMock()
        handler.call_id = "call-1"
        return handler

    def test_a_played_notice_is_recorded(self, tmp_path):
        db = self._db()
        announcer = _announcer(audio_file=str(tmp_path / "x.wav"))
        announcer.database = db

        announcer._record(self._handler(), played=True)

        query, params = db.execute.call_args[0]
        assert "INSERT INTO recording_notices" in query
        assert params[2] is True

    def test_the_wording_is_stored_verbatim(self, tmp_path):
        """The configured text changes; what this caller heard does not."""
        db = self._db()
        announcer = _announcer(audio_file=str(tmp_path / "x.wav"), text="Specific wording")
        announcer.database = db

        announcer._record(self._handler(), played=True)

        assert "Specific wording" in db.execute.call_args[0][1]

    def test_a_failure_is_recorded_with_its_reason(self, tmp_path):
        """The rows that matter most: each one is a recording that was discarded."""
        db = self._db()
        announcer = _announcer(audio_file=str(tmp_path / "x.wav"))
        announcer.database = db

        announcer._record(self._handler(), played=False)

        params = db.execute.call_args[0][1]
        assert params[2] is False
        assert "discarded" in params[5]

    def test_no_database_is_survivable(self, tmp_path):
        _announcer(audio_file=str(tmp_path / "x.wav"))._record(self._handler(), played=True)

    def test_a_logging_failure_never_costs_the_call(self, tmp_path):
        """Failing to log a notice is not a reason to fail the call it protected."""
        db = self._db()
        db.execute.side_effect = RuntimeError("connection reset")
        announcer = _announcer(audio_file=str(tmp_path / "x.wav"))
        announcer.database = db

        announcer._record(self._handler(), played=True)

    def test_playback_records_its_outcome(self, tmp_path):
        """_play must log whatever happened, including the failure path."""
        db = self._db()
        announcer = _announcer(audio_file=str(tmp_path / "absent.wav"))
        announcer.database = db
        handler = MagicMock()
        handler.call_id = "call-1"
        handler.running = True
        handler.tap = None

        announcer._play(handler, lambda _ok: None)

        assert db.execute.called
        assert db.execute.call_args[0][1][2] is False

    def test_recent_reads_newest_first(self, tmp_path):
        db = self._db()
        announcer = _announcer(audio_file=str(tmp_path / "x.wav"))
        announcer.database = db

        announcer.recent(10)

        assert "ORDER BY played_at DESC" in db.fetch_all.call_args[0][0]
