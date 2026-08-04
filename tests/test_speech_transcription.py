"""
Tests for the shared transcription subsystem (``pbx/speech``).

Replaces tests/test_voicemail_transcription.py, which tested the same logic when it lived in
pbx/features/voicemail_transcription.py. The audio-format coverage is the important part and
is carried over intact: voicemail is stored as G.711, which Python's `wave` module cannot
open at all, and that broke every real transcription until it was fixed.
"""

import json
import struct
from itertools import pairwise
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, Mock, patch

import pytest

from pbx.speech import Transcript, TranscriptionSettings, TranscriptionWorker, build_backend
from pbx.speech.backends.vosk import VoskBackend


def _settings(**overrides: Any) -> TranscriptionSettings:
    """Settings with transcription on, overridable per test."""
    base: dict[str, Any] = {"enabled": True, "provider": "vosk", "workers": 0}
    base.update(overrides)
    return TranscriptionSettings.from_dict(base)


def _tone_pcm16(seconds: float = 1.0, rate: int = 8000, freq: int = 440) -> bytes:
    """A PCM16 sine tone, used as the reference signal for codec round trips."""
    import math

    return b"".join(
        struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * t / rate)))
        for t in range(int(rate * seconds))
    )


def _write_wav(path: Path, payload: bytes, audio_format: int, bits: int, rate: int = 8000) -> Path:
    """Write a WAV with an explicit format code, the way PBXCore._build_wav_file does."""
    from pbx.utils.audio import build_wav_header

    path.write_bytes(build_wav_header(len(payload), rate, 1, bits, audio_format) + payload)
    return path


class _FakeRecognizer:
    """
    Stands in for Kaldi so the audio plumbing can be tested without a real model.

    Rejects a sample-rate mismatch exactly as Kaldi does -- it aborts rather than resampling.
    An earlier version of this stub accepted any rate, which is precisely why the 8 kHz
    telephony / 16 kHz model mismatch got through the suite and only surfaced on a server.
    """

    transcript = "hello from voicemail"
    expected_rate = 16000

    def __init__(self, model: Any, sample_rate: int) -> None:
        if sample_rate != self.expected_rate:
            raise ValueError(
                f"Sampling frequency mismatch, expected {self.expected_rate}, got {sample_rate}"
            )
        self.sample_rate = sample_rate

    def SetWords(self, flag: bool) -> None:  # noqa: N802 - mirrors the Vosk API
        pass

    def AcceptWaveform(self, data: bytes) -> bool:  # noqa: N802 - mirrors the Vosk API
        return False

    def Result(self) -> str:  # noqa: N802 - mirrors the Vosk API
        return json.dumps({"text": ""})

    def FinalResult(self) -> str:  # noqa: N802 - mirrors the Vosk API
        return json.dumps(
            {
                "text": self.transcript,
                "result": [
                    {"word": "hello", "start": 0.0, "end": 0.4, "conf": 0.9},
                    {"word": "from", "start": 0.4, "end": 0.7, "conf": 0.8},
                ],
            }
        )


def _ready_backend(settings: TranscriptionSettings | None = None) -> VoskBackend:
    """A backend with the model-loading step short-circuited."""
    backend = VoskBackend(settings or _settings())
    backend.model = Mock()
    return backend


@pytest.mark.unit
class TestSettings:
    def test_defaults_when_nothing_configured(self) -> None:
        settings = TranscriptionSettings.from_dict({})

        # Off by default: the model is a separate ~40 MB download, so defaulting to on would
        # mean a fresh install silently fails to transcribe.
        assert not settings.enabled
        assert settings.provider == "vosk"
        assert settings.language == "en-US"
        assert settings.max_audio_seconds == 300
        assert settings.workers == 1

    def test_workers_zero_runs_inline(self) -> None:
        assert TranscriptionSettings.from_dict({"workers": 0}).runs_inline

    def test_workers_are_capped_to_leave_cores_for_media(self) -> None:
        """A transcription thread per core would starve the RTP relay threads."""
        settings = TranscriptionSettings.from_dict({"workers": 999})

        assert settings.workers < 999
        assert any("exceeds the safe ceiling" in w for w in settings.config_warnings)

    def test_unknown_provider_is_reported(self) -> None:
        settings = TranscriptionSettings.from_dict({"enabled": True, "provider": "whisper"})

        assert any("is not one of" in w for w in settings.config_warnings)
        assert any("not supported" in p for p in settings.validate())

    def test_disabled_config_reports_nothing(self) -> None:
        assert TranscriptionSettings.from_dict({"enabled": False}).validate() == []

    def test_missing_model_is_reported(self) -> None:
        problems = _settings(vosk_model_path="/nonexistent/model").validate()

        assert any("does not exist" in p for p in problems)

    def test_relative_model_path_is_flagged(self, tmp_path: Any) -> None:
        """A relative path resolves against the service's working directory, not the repo."""
        (tmp_path / "m").mkdir()
        import os

        cwd = Path.cwd()
        os.chdir(tmp_path)
        try:
            problems = _settings(vosk_model_path="m").validate()
        finally:
            os.chdir(cwd)

        assert any("is relative" in p for p in problems)

    def test_redacted_is_serialisable(self) -> None:
        redacted = _settings().redacted()

        assert redacted["provider"] == "vosk"
        assert "config_warnings" not in redacted


@pytest.mark.unit
class TestWavDecoding:
    """
    The formats the PBX actually writes must be readable.

    PBXCore._build_wav_file stores voicemail as G.711 u-law (format 7) or A-law (6). Python's
    `wave` module supports only linear PCM and raises "unknown format: 7" on those files, so
    every real voicemail failed to transcribe. These fixtures are the regression guard.
    """

    def test_ulaw_wav_decodes_to_pcm16(self, tmp_path: Any) -> None:
        from pbx.utils.audio import WAV_FORMAT_ULAW, pcm16_to_ulaw, read_wav_as_pcm16

        reference = _tone_pcm16()
        path = _write_wav(tmp_path / "u.wav", pcm16_to_ulaw(reference), WAV_FORMAT_ULAW, 8)

        pcm, rate = read_wav_as_pcm16(path)

        assert rate == 8000
        assert len(pcm) == len(reference)
        # G.711 is 8-bit logarithmic, so expect quantisation error but the same waveform.
        count = len(pcm) // 2
        ref = struct.unpack(f"<{count}h", reference)
        got = struct.unpack(f"<{count}h", pcm)
        assert max(abs(a - b) for a, b in zip(ref, got, strict=True)) < 500

    def test_alaw_wav_decodes_to_pcm16(self, tmp_path: Any) -> None:
        from pbx.utils.audio import WAV_FORMAT_ALAW, pcm16_to_alaw, read_wav_as_pcm16

        path = _write_wav(tmp_path / "a.wav", pcm16_to_alaw(_tone_pcm16()), WAV_FORMAT_ALAW, 8)

        pcm, rate = read_wav_as_pcm16(path)

        assert rate == 8000
        assert len(pcm) == len(_tone_pcm16())

    def test_pcm16_wav_is_returned_unchanged(self, tmp_path: Any) -> None:
        from pbx.utils.audio import WAV_FORMAT_PCM, read_wav_as_pcm16

        reference = _tone_pcm16()
        path = _write_wav(tmp_path / "p.wav", reference, WAV_FORMAT_PCM, 16)

        assert read_wav_as_pcm16(path) == (reference, 8000)

    def test_eight_bit_pcm_is_recentred(self, tmp_path: Any) -> None:
        """8-bit PCM in a WAV is unsigned around 128, unlike every other width."""
        from pbx.utils.audio import WAV_FORMAT_PCM, read_wav_as_pcm16

        path = _write_wav(tmp_path / "p8.wav", bytes([128, 255, 0, 128]), WAV_FORMAT_PCM, 8)

        pcm, _ = read_wav_as_pcm16(path)

        assert struct.unpack("<4h", pcm) == (0, 32512, -32768, 0)

    def test_stereo_is_rejected(self, tmp_path: Any) -> None:
        from pbx.utils.audio import WAV_FORMAT_PCM, build_wav_header, read_wav_as_pcm16

        payload = b"\x00\x00" * 100
        path = tmp_path / "stereo.wav"
        path.write_bytes(build_wav_header(len(payload), 8000, 2, 16, WAV_FORMAT_PCM) + payload)

        with pytest.raises(ValueError, match="mono"):
            read_wav_as_pcm16(path)

    def test_unconvertible_format_is_rejected(self, tmp_path: Any) -> None:
        """G.722 has no linear decode here, and must fail with a clear message."""
        from pbx.utils.audio import WAV_FORMAT_G722, read_wav_as_pcm16

        path = _write_wav(tmp_path / "g722.wav", b"\x00" * 100, WAV_FORMAT_G722, 8)

        with pytest.raises(ValueError, match="Cannot convert WAV format"):
            read_wav_as_pcm16(path)

    def test_not_a_wav_is_rejected(self, tmp_path: Any) -> None:
        from pbx.utils.audio import read_wav_as_pcm16

        path = tmp_path / "nope.wav"
        path.write_bytes(b"this is not a wav file at all")

        with pytest.raises(ValueError, match="RIFF"):
            read_wav_as_pcm16(path)


@pytest.mark.unit
class TestResampling:
    """
    Telephony is 8 kHz; Vosk models are 16 kHz. Kaldi will not bridge that itself.

    On a real server this failed with "Sampling frequency mismatch, expected 16000, got 8000"
    after the audio had decoded perfectly -- the recording was fine, the recogniser refused
    the rate.
    """

    def test_upsampling_doubles_the_sample_count(self) -> None:
        from pbx.utils.audio import resample_pcm16

        out = resample_pcm16(_tone_pcm16(seconds=1.0, rate=8000), 8000, 16000)

        assert len(out) // 2 == pytest.approx(16000, abs=2)

    def test_matching_rates_are_a_no_op(self) -> None:
        from pbx.utils.audio import resample_pcm16

        pcm = _tone_pcm16(seconds=0.1)

        assert resample_pcm16(pcm, 8000, 8000) is pcm

    def test_empty_audio_is_safe(self) -> None:
        from pbx.utils.audio import resample_pcm16

        assert resample_pcm16(b"", 8000, 16000) == b""

    def test_resampling_preserves_the_waveform(self) -> None:
        """A 440 Hz tone must still be a 440 Hz tone, not noise."""
        from pbx.utils.audio import resample_pcm16

        out = resample_pcm16(_tone_pcm16(seconds=0.5, rate=8000, freq=440), 8000, 16000)
        samples = struct.unpack(f"<{len(out) // 2}h", out)

        # Zero crossings scale with frequency, not sample rate: ~440 per second either way.
        crossings = sum(1 for a, b in pairwise(samples) if (a < 0) != (b < 0))
        assert 400 <= crossings / 0.5 / 2 <= 480

    def test_model_sample_rate_is_read_from_mfcc_conf(self, tmp_path: Any) -> None:
        conf = tmp_path / "conf"
        conf.mkdir()
        (conf / "mfcc.conf").write_text("--sample-frequency=16000\n--use-energy=false\n")

        assert (
            _ready_backend(_settings(vosk_model_path=str(tmp_path)))._model_sample_rate() == 16000
        )

    def test_model_sample_rate_falls_back_when_unreadable(self, tmp_path: Any) -> None:
        """A model without a readable mfcc.conf is assumed 16 kHz, like every Vosk model."""
        assert (
            _ready_backend(_settings(vosk_model_path=str(tmp_path)))._model_sample_rate() == 16000
        )


@pytest.mark.unit
class TestVoskBackend:
    """End-to-end over the audio plumbing: a real stored voicemail must transcribe."""

    @patch("pbx.speech.backends.vosk.KaldiRecognizer", _FakeRecognizer, create=True)
    def test_ulaw_voicemail_transcribes(self, tmp_path: Any) -> None:
        """The exact regression: a u-law voicemail used to fail with "unknown format: 7"."""
        from pbx.utils.audio import WAV_FORMAT_ULAW, pcm16_to_ulaw

        path = _write_wav(tmp_path / "vm.wav", pcm16_to_ulaw(_tone_pcm16()), WAV_FORMAT_ULAW, 8)

        result = _ready_backend().transcribe_file(path)

        assert result.success, result.error
        assert result.text == _FakeRecognizer.transcript
        assert result.provider == "vosk"
        assert result.audio_duration == pytest.approx(1.0, abs=0.01)

    @patch("pbx.speech.backends.vosk.KaldiRecognizer", _FakeRecognizer, create=True)
    def test_alaw_voicemail_transcribes(self, tmp_path: Any) -> None:
        from pbx.utils.audio import WAV_FORMAT_ALAW, pcm16_to_alaw

        path = _write_wav(tmp_path / "vm.wav", pcm16_to_alaw(_tone_pcm16()), WAV_FORMAT_ALAW, 8)

        assert _ready_backend().transcribe_file(path).success

    @patch("pbx.speech.backends.vosk.KaldiRecognizer", _FakeRecognizer, create=True)
    def test_confidence_is_averaged_from_word_scores(self, tmp_path: Any) -> None:
        """Vosk reports real per-word confidence; it must not be a hardcoded constant."""
        from pbx.utils.audio import WAV_FORMAT_ULAW, pcm16_to_ulaw

        path = _write_wav(tmp_path / "vm.wav", pcm16_to_ulaw(_tone_pcm16()), WAV_FORMAT_ULAW, 8)

        result = _ready_backend().transcribe_file(path)

        assert result.confidence == pytest.approx(0.85)  # mean of 0.9 and 0.8

    @patch("pbx.speech.backends.vosk.KaldiRecognizer", _FakeRecognizer, create=True)
    def test_words_are_opt_in(self, tmp_path: Any) -> None:
        from pbx.utils.audio import WAV_FORMAT_ULAW, pcm16_to_ulaw

        path = _write_wav(tmp_path / "vm.wav", pcm16_to_ulaw(_tone_pcm16()), WAV_FORMAT_ULAW, 8)
        backend = _ready_backend()

        assert backend.transcribe_file(path).segments[0].words == ()
        assert len(backend.transcribe_file(path, want_words=True).segments[0].words) == 2

    @patch("pbx.speech.backends.vosk.KaldiRecognizer", _FakeRecognizer, create=True)
    def test_over_long_audio_is_refused(self, tmp_path: Any) -> None:
        from pbx.utils.audio import WAV_FORMAT_ULAW, pcm16_to_ulaw

        path = _write_wav(
            tmp_path / "long.wav", pcm16_to_ulaw(_tone_pcm16(seconds=5)), WAV_FORMAT_ULAW, 8
        )

        result = _ready_backend(_settings(max_audio_seconds=2)).transcribe_file(path)

        assert not result.success
        assert "longer than the 2s limit" in result.error

    @patch("pbx.speech.backends.vosk.KaldiRecognizer", _FakeRecognizer, create=True)
    def test_unsupported_sample_rate_is_refused(self, tmp_path: Any) -> None:
        from pbx.utils.audio import WAV_FORMAT_ULAW, pcm16_to_ulaw

        payload = pcm16_to_ulaw(_tone_pcm16(seconds=0.5, rate=11025))
        path = _write_wav(tmp_path / "odd.wav", payload, WAV_FORMAT_ULAW, 8, rate=11025)

        result = _ready_backend().transcribe_file(path)

        assert not result.success
        assert "Unsupported sample rate" in result.error

    def test_backend_without_a_model_is_not_ready(self) -> None:
        backend = VoskBackend(_settings(vosk_model_path="/nonexistent/model"))

        assert not backend.ready
        assert not backend.transcribe_file(Path("/tmp/whatever.wav")).success

    def test_unknown_provider_yields_no_backend(self) -> None:
        assert build_backend(_settings(provider="whisper")) is None


@pytest.mark.unit
class TestTranscriptionWorker:
    """
    submit() returning True is a promise that the callback fires exactly once.

    Every voicemail notification depends on that promise, so each failure mode gets a test.
    """

    def _worker(self, backend: Any, **overrides: Any) -> TranscriptionWorker:
        return TranscriptionWorker(backend, _settings(**overrides))

    def test_inline_worker_runs_the_job_and_calls_back(self, tmp_path: Any) -> None:
        path = tmp_path / "a.wav"
        path.write_bytes(b"x")
        backend = Mock(ready=True, provider="vosk")
        backend.transcribe_file.return_value = Transcript(text="hi", provider="vosk")
        seen: list[Any] = []

        accepted = self._worker(backend).submit(path, seen.append)

        assert accepted
        assert len(seen) == 1
        assert seen[0].text == "hi"

    def test_disabled_worker_refuses(self, tmp_path: Any) -> None:
        path = tmp_path / "a.wav"
        path.write_bytes(b"x")
        seen: list[Any] = []

        accepted = self._worker(Mock(ready=True), enabled=False).submit(path, seen.append)

        assert not accepted
        assert seen == []

    def test_unready_backend_refuses(self, tmp_path: Any) -> None:
        path = tmp_path / "a.wav"
        path.write_bytes(b"x")
        seen: list[Any] = []

        accepted = self._worker(Mock(ready=False)).submit(path, seen.append)

        assert not accepted
        assert seen == []

    def test_missing_file_refuses(self, tmp_path: Any) -> None:
        seen: list[Any] = []

        accepted = self._worker(Mock(ready=True)).submit(tmp_path / "gone.wav", seen.append)

        assert not accepted
        assert seen == []

    def test_over_long_audio_refuses_at_admission(self, tmp_path: Any) -> None:
        """Refused on the caller's thread, before it can occupy a worker."""
        path = tmp_path / "a.wav"
        path.write_bytes(b"x")
        backend = Mock(ready=True)
        seen: list[Any] = []

        accepted = self._worker(backend, max_audio_seconds=10).submit(
            path, seen.append, audio_seconds=99
        )

        assert not accepted
        assert seen == []
        backend.transcribe_file.assert_not_called()

    def test_backend_raising_still_calls_back(self, tmp_path: Any) -> None:
        """A crash inside the engine must never swallow the notification."""
        path = tmp_path / "a.wav"
        path.write_bytes(b"x")
        backend = Mock(ready=True, provider="vosk")
        backend.transcribe_file.side_effect = RuntimeError("boom")
        seen: list[Any] = []

        accepted = self._worker(backend).submit(path, seen.append)

        assert accepted
        assert len(seen) == 1
        assert not seen[0].success
        assert "boom" in seen[0].error

    def test_callback_raising_is_absorbed(self, tmp_path: Any) -> None:
        path = tmp_path / "a.wav"
        path.write_bytes(b"x")
        backend = Mock(ready=True, provider="vosk")
        backend.transcribe_file.return_value = Transcript(text="hi")

        def explode(_transcript: Any) -> None:
            raise RuntimeError("callback boom")

        # Must not propagate to the caller tearing a call down.
        assert self._worker(backend).submit(path, explode)

    def test_queued_jobs_are_released_on_stop(self, tmp_path: Any) -> None:
        """
        Shutdown must fire pending callbacks rather than let daemon threads take them.

        Without this a voicemail queued when the PBX stops would never notify anyone.
        """
        path = tmp_path / "a.wav"
        path.write_bytes(b"x")
        backend = Mock(ready=True, provider="vosk")
        worker = self._worker(backend, workers=1)
        seen: list[Any] = []

        # Never started, so nothing consumes the queue; put a job straight on it.
        worker._running = True
        assert worker.submit(path, seen.append)
        worker._running = False
        worker.stop(timeout=0.1)

        assert seen == [None]
        backend.transcribe_file.assert_not_called()

    def test_full_queue_refuses(self, tmp_path: Any) -> None:
        path = tmp_path / "a.wav"
        path.write_bytes(b"x")
        worker = self._worker(Mock(ready=True), workers=1, queue_size=1)
        worker._running = True
        seen: list[Any] = []

        assert worker.submit(path, seen.append)  # fills the queue
        assert not worker.submit(path, seen.append)  # refused
        assert worker.stats()["dropped"] == 1

    def test_stats_report_the_queue(self, tmp_path: Any) -> None:
        stats = self._worker(Mock(ready=True)).stats()

        assert stats["enabled"] is True
        assert stats["queue_capacity"] == 32
        assert stats["mean_real_time_factor"] is None


@pytest.mark.unit
class TestTranscriptionWiring:
    """The worker must reach the mailbox that needs it."""

    def test_voicemail_system_forwards_service_to_mailboxes(self, tmp_path: Any) -> None:
        """
        VoicemailSystem holds the shared instance and hands it to every mailbox.

        Sharing is a hard requirement, not tidiness: the model is far too large to load once
        per mailbox on a system with a few hundred extensions.
        """
        from pbx.features.voicemail import VoicemailSystem

        worker = MagicMock()
        system = VoicemailSystem(storage_path=str(tmp_path), transcription_service=worker)

        assert system.get_mailbox("1001").transcription_service is worker
        assert system.get_mailbox("1002").transcription_service is worker

    def test_voicemail_system_without_a_service_is_safe(self, tmp_path: Any) -> None:
        from pbx.features.voicemail import VoicemailSystem

        system = VoicemailSystem(storage_path=str(tmp_path))

        assert system.get_mailbox("1001").transcription_service is None
