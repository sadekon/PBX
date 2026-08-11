"""Tests for the recording and transcript read API.

The subject here is access control, not retrieval. A recording is the most sensitive thing
this system stores, so the cases that matter are the refusals: a non-admin with the feature
off, a non-admin who was not on the call, and a path that points outside the storage tree.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from flask.testing import FlaskClient

from pbx.features.recording_store import KIND_CALL, KIND_VOICEMAIL
from pbx.utils.audio import WAV_FORMAT_PCM, build_wav_header
from pbx.utils.session_token import get_session_token_manager


def _wav(path: Path, audio_format: int = WAV_FORMAT_PCM, payload: bytes = b"\x00\x01" * 64) -> Path:
    """Write a minimal but genuinely parseable WAV. The endpoint rejects anything else."""
    bits = 16 if audio_format == WAV_FORMAT_PCM else 8
    header = build_wav_header(
        len(payload),
        sample_rate=8000,
        channels=1,
        bits_per_sample=bits,
        audio_format=audio_format,
    )
    path.write_bytes(header + payload)
    return path


def _token(extension: str, is_admin: bool) -> dict[str, str]:
    """An Authorization header for a session token with the given privileges."""
    token = get_session_token_manager().generate_token(
        extension=extension, is_admin=is_admin, name="Test User", email=""
    )
    return {"Authorization": f"Bearer {token}"}


def _recording(**overrides: object) -> dict:
    """A recordings row, as RecordingStore hands one back."""
    row = {
        "id": 7,
        "session_id": "sess-1",
        "call_id": "call-1",
        "kind": KIND_CALL,
        "path": None,
        "bytes": 1024,
        "duration_seconds": 12.5,
        "sample_rate": 8000,
        "channels": None,
        "participants": ["1001", "1002"],
        "started_at": None,
        "ended_at": None,
        "audio_deleted_at": None,
        "created_at": None,
    }
    row.update(overrides)
    return row


@pytest.fixture
def recording_core(mock_pbx_core: MagicMock) -> MagicMock:
    """A PBX core whose recording and transcript stores are live and return one row."""
    store = MagicMock()
    store.enabled = True
    store.get.return_value = _recording()
    store.recent.return_value = [_recording()]
    mock_pbx_core.recording_system = MagicMock()
    mock_pbx_core.recording_system.store = store

    transcripts = MagicMock()
    transcripts.enabled = True
    transcripts.for_recording.return_value = [
        {
            "id": 3,
            "recording_id": 7,
            "source": "recording",
            "provider": "whisper",
            "model": "base.en",
            "language": "en",
            "text": "hello there",
            "segments": None,
            "confidence": None,
            "processing_duration": 1.2,
            "created_at": None,
        }
    ]
    mock_pbx_core.transcript_store = transcripts

    # Off unless a test turns it on -- that is the shipped default and most cases assert it.
    mock_pbx_core.config.get.side_effect = lambda key, default=None: (
        False if key == "recording.participant_access" else default
    )
    return mock_pbx_core


@pytest.mark.unit
class TestRecordingAccessControl:
    """Who may read a recording."""

    def test_unauthenticated_is_rejected(self, api_client: FlaskClient) -> None:
        assert api_client.get("/api/recordings").status_code == 401

    def test_admin_may_list(self, api_client: FlaskClient, recording_core: MagicMock) -> None:
        resp = api_client.get("/api/recordings", headers=_token("9000", is_admin=True))
        assert resp.status_code == 200
        assert len(resp.get_json()["recordings"]) == 1

    def test_non_admin_refused_when_participant_access_off(
        self, api_client: FlaskClient, recording_core: MagicMock
    ) -> None:
        """The shipped default: a recording is admin-only even for someone who was on it."""
        resp = api_client.get("/api/recordings", headers=_token("1001", is_admin=False))
        assert resp.status_code == 403

    def test_participant_may_read_when_flag_on(
        self, api_client: FlaskClient, recording_core: MagicMock
    ) -> None:
        recording_core.config.get.side_effect = lambda key, default=None: (
            True if key == "recording.participant_access" else default
        )
        resp = api_client.get("/api/recordings/7", headers=_token("1001", is_admin=False))
        assert resp.status_code == 200

    def test_non_participant_refused_even_when_flag_on(
        self, api_client: FlaskClient, recording_core: MagicMock
    ) -> None:
        """1009 was not on this call, so the flag does not help them."""
        recording_core.config.get.side_effect = lambda key, default=None: (
            True if key == "recording.participant_access" else default
        )
        resp = api_client.get("/api/recordings/7", headers=_token("1009", is_admin=False))
        assert resp.status_code == 403

    def test_list_filters_to_the_callers_own_recordings(
        self, api_client: FlaskClient, recording_core: MagicMock
    ) -> None:
        recording_core.config.get.side_effect = lambda key, default=None: (
            True if key == "recording.participant_access" else default
        )
        recording_core.recording_system.store.recent.return_value = [
            _recording(id=7, participants=["1001", "1002"]),
            _recording(id=8, participants=["1003", "1004"]),
        ]
        resp = api_client.get("/api/recordings", headers=_token("1001", is_admin=False))
        assert resp.status_code == 200
        assert [r["id"] for r in resp.get_json()["recordings"]] == [7]

    def test_denial_does_not_distinguish_missing_from_forbidden(
        self, api_client: FlaskClient, recording_core: MagicMock
    ) -> None:
        """A 403 body must not reveal that the recording exists, or who was on it."""
        recording_core.config.get.side_effect = lambda key, default=None: (
            True if key == "recording.participant_access" else default
        )
        resp = api_client.get("/api/recordings/7", headers=_token("1009", is_admin=False))
        body = resp.get_json()
        assert "1001" not in str(body)
        assert "sess-1" not in str(body)


@pytest.mark.unit
class TestListResponseShape:
    """What a listing is allowed to carry."""

    def test_listing_never_includes_transcript_text(
        self, api_client: FlaskClient, recording_core: MagicMock
    ) -> None:
        """Text comes only from the per-recording fetch, which is audited."""
        resp = api_client.get("/api/recordings", headers=_token("9000", is_admin=True))
        assert "hello there" not in resp.get_data(as_text=True)

    def test_listing_never_includes_the_storage_path(
        self, api_client: FlaskClient, recording_core: MagicMock
    ) -> None:
        recording_core.recording_system.store.recent.return_value = [
            _recording(path="/srv/recordings/secret-layout/7.wav")
        ]
        resp = api_client.get("/api/recordings", headers=_token("9000", is_admin=True))
        assert "secret-layout" not in resp.get_data(as_text=True)

    def test_voicemail_is_excluded_unless_asked_for(
        self, api_client: FlaskClient, recording_core: MagicMock
    ) -> None:
        """Voicemail has its own page; this one is for calls."""
        api_client.get("/api/recordings", headers=_token("9000", is_admin=True))
        kwargs = recording_core.recording_system.store.recent.call_args.kwargs
        assert kwargs["kinds"] == [KIND_CALL]

    def test_include_voicemail_widens_the_query(
        self, api_client: FlaskClient, recording_core: MagicMock
    ) -> None:
        api_client.get("/api/recordings?include_voicemail=1", headers=_token("9000", is_admin=True))
        kwargs = recording_core.recording_system.store.recent.call_args.kwargs
        assert kwargs["kinds"] == [KIND_CALL, KIND_VOICEMAIL]

    def test_participant_filter_is_passed_to_the_store(
        self, api_client: FlaskClient, recording_core: MagicMock
    ) -> None:
        api_client.get("/api/recordings?participant=1001", headers=_token("9000", is_admin=True))
        kwargs = recording_core.recording_system.store.recent.call_args.kwargs
        assert kwargs["participant"] == "1001"

    def test_overlong_participant_filter_is_rejected(
        self, api_client: FlaskClient, recording_core: MagicMock
    ) -> None:
        resp = api_client.get(
            f"/api/recordings?participant={'9' * 80}", headers=_token("9000", is_admin=True)
        )
        assert resp.status_code == 400


@pytest.mark.unit
class TestAudioRetrieval:
    """Serving the media, and refusing to."""

    def test_expired_audio_returns_410_not_404(
        self, api_client: FlaskClient, recording_core: MagicMock
    ) -> None:
        """The row outlives the file by design; 404 would read as "no such recording"."""
        recording_core.recording_system.store.get.return_value = _recording(
            path="/srv/recordings/7.wav", audio_deleted_at="2026-01-01T00:00:00Z"
        )
        resp = api_client.get("/api/recordings/7/audio", headers=_token("9000", is_admin=True))
        assert resp.status_code == 410
        assert resp.get_json()["transcript_may_remain"] is True

    def test_path_outside_storage_root_is_refused(
        self, api_client: FlaskClient, recording_core: MagicMock, tmp_path: Path
    ) -> None:
        """A stored path that escapes the tree must not become an arbitrary file read."""
        outside = tmp_path / "passwd"
        outside.write_text("root:x:0:0:")

        recording_core.config.get.side_effect = lambda key, default=None: {
            "recording.storage_path": str(tmp_path / "recordings"),
            "voicemail.storage_path": str(tmp_path / "voicemail"),
        }.get(key, default)
        recording_core.recording_system.store.get.return_value = _recording(path=str(outside))

        resp = api_client.get("/api/recordings/7/audio", headers=_token("9000", is_admin=True))
        assert resp.status_code == 404
        assert "root:x" not in resp.get_data(as_text=True)

    def test_audio_is_served_from_inside_the_root(
        self, api_client: FlaskClient, recording_core: MagicMock, tmp_path: Path
    ) -> None:
        root = tmp_path / "recordings"
        root.mkdir()
        media = _wav(root / "7.wav")

        recording_core.config.get.side_effect = lambda key, default=None: {
            "recording.storage_path": str(root),
            "voicemail.storage_path": str(tmp_path / "voicemail"),
        }.get(key, default)
        recording_core.recording_system.store.get.return_value = _recording(path=str(media))

        resp = api_client.get("/api/recordings/7/audio", headers=_token("9000", is_admin=True))
        assert resp.status_code == 200
        assert resp.get_data() == media.read_bytes()

    def test_media_response_is_not_cacheable(
        self, api_client: FlaskClient, recording_core: MagicMock, tmp_path: Path
    ) -> None:
        """Audio must not settle into a browser cache that outlives the retention policy."""
        root = tmp_path / "recordings"
        root.mkdir()
        media = _wav(root / "7.wav")

        recording_core.config.get.side_effect = lambda key, default=None: {
            "recording.storage_path": str(root),
        }.get(key, default)
        recording_core.recording_system.store.get.return_value = _recording(path=str(media))

        resp = api_client.get("/api/recordings/7/audio", headers=_token("9000", is_admin=True))
        assert "no-store" in resp.headers.get("Cache-Control", "")


@pytest.mark.unit
class TestTranscriptRetrieval:
    """Reading the text."""

    def test_admin_gets_transcript_text(
        self, api_client: FlaskClient, recording_core: MagicMock
    ) -> None:
        resp = api_client.get("/api/recordings/7/transcript", headers=_token("9000", is_admin=True))
        assert resp.status_code == 200
        assert resp.get_json()["transcripts"][0]["text"] == "hello there"

    def test_missing_transcript_is_404(
        self, api_client: FlaskClient, recording_core: MagicMock
    ) -> None:
        recording_core.transcript_store.for_recording.return_value = []
        resp = api_client.get("/api/recordings/7/transcript", headers=_token("9000", is_admin=True))
        assert resp.status_code == 404

    def test_non_admin_refused_by_default(
        self, api_client: FlaskClient, recording_core: MagicMock
    ) -> None:
        resp = api_client.get(
            "/api/recordings/7/transcript", headers=_token("1001", is_admin=False)
        )
        assert resp.status_code == 403
        assert "hello there" not in resp.get_data(as_text=True)


@pytest.mark.unit
class TestRetentionRoutesAreAdminOnly:
    """Retention policy and legal holds are administrative, and destructive to get wrong."""

    @pytest.mark.parametrize(
        "path",
        [
            "/api/recording-retention/policies",
            "/api/recording-retention/statistics",
            "/api/recording-retention/holds",
        ],
    )
    def test_non_admin_is_refused(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock, path: str
    ) -> None:
        resp = api_client.get(path, headers=_token("1001", is_admin=False))
        assert resp.status_code == 403


@pytest.mark.unit
class TestAudioFormatConversion:
    """G.711 is the format a browser silently refuses, and the one telephony stores."""

    @staticmethod
    def _write_wav(path: Path, audio_format: int, payload: bytes) -> None:
        """A minimal single-chunk WAV with the given format code."""
        from pbx.utils.audio import build_wav_header

        bits = 16 if audio_format == 1 else 8
        header = build_wav_header(
            len(payload),
            sample_rate=8000,
            channels=1,
            bits_per_sample=bits,
            audio_format=audio_format,
        )
        path.write_bytes(header + payload)

    def test_ulaw_is_decoded_to_pcm(self, tmp_path: Path) -> None:
        """Served as-is, this is exactly the 'no supported source was found' failure."""
        from pbx.utils.audio import (
            WAV_FORMAT_PCM,
            WAV_FORMAT_ULAW,
            read_wav_format,
            wav_as_pcm16_wav,
        )

        src = tmp_path / "vm.wav"
        self._write_wav(src, WAV_FORMAT_ULAW, bytes(range(256)))

        assert read_wav_format(src)[0] == WAV_FORMAT_ULAW

        converted, detected = wav_as_pcm16_wav(src)
        assert detected == WAV_FORMAT_ULAW
        assert converted is not None

        out = tmp_path / "out.wav"
        out.write_bytes(converted)
        assert read_wav_format(out)[0] == WAV_FORMAT_PCM
        # 8-bit in, 16-bit out.
        assert len(converted) > len(src.read_bytes())

    def test_pcm_is_left_alone(self, tmp_path: Path) -> None:
        """Untouched so send_file keeps serving range requests."""
        from pbx.utils.audio import WAV_FORMAT_PCM, wav_as_pcm16_wav

        src = tmp_path / "call.wav"
        self._write_wav(src, WAV_FORMAT_PCM, b"\x00\x01" * 64)

        converted, detected = wav_as_pcm16_wav(src)
        assert detected == WAV_FORMAT_PCM
        assert converted is None

    def test_unreadable_file_reports_no_format(self, tmp_path: Path) -> None:
        junk = tmp_path / "not.wav"
        junk.write_bytes(b"this is not a RIFF file")

        from pbx.utils.audio import wav_as_pcm16_wav

        assert wav_as_pcm16_wav(junk) == (None, None)

    def test_undecodable_format_is_refused_not_served(
        self, api_client: FlaskClient, recording_core: MagicMock, tmp_path: Path
    ) -> None:
        """G.722 has no decoder here; serving it would reproduce the silent failure."""
        from pbx.utils.audio import WAV_FORMAT_G722

        root = tmp_path / "recordings"
        root.mkdir()
        media = root / "7.wav"
        self._write_wav(media, WAV_FORMAT_G722, b"\x00" * 32)

        recording_core.config.get.side_effect = lambda key, default=None: {
            "recording.storage_path": str(root),
        }.get(key, default)
        recording_core.recording_system.store.get.return_value = _recording(path=str(media))

        resp = api_client.get("/api/recordings/7/audio", headers=_token("9000", is_admin=True))
        assert resp.status_code == 415
        assert resp.get_json()["wav_format"] == WAV_FORMAT_G722


@pytest.mark.unit
class TestMalformedWavRepair:
    """A WAV whose header lies about its size still holds playable audio."""

    @staticmethod
    def _unfinalised(path: Path, payload: bytes) -> Path:
        """A PCM WAV as `wave.open(..., 'wb')` leaves it when close() never runs.

        The sizes are patched in on close, so a writer that was killed mid-call leaves both
        at zero. The file parses as a valid WAV and decodes to silence.
        """
        import struct

        from pbx.utils.audio import build_wav_header

        header = bytearray(build_wav_header(len(payload)))
        struct.pack_into("<I", header, 40, 0)  # data chunk size
        struct.pack_into("<I", header, 4, 36)  # RIFF size
        path.write_bytes(bytes(header) + payload)
        return path

    def test_zero_size_header_is_repaired(self, tmp_path: Path) -> None:
        import struct

        from pbx.utils.audio import wav_as_pcm16_wav

        payload = b"\x01\x02" * 100
        src = self._unfinalised(tmp_path / "cut-short.wav", payload)

        converted, fmt = wav_as_pcm16_wav(src)
        assert fmt == WAV_FORMAT_PCM
        assert converted is not None, "an unfinalised WAV must be repaired, not passed through"
        assert struct.unpack_from("<I", converted, 40)[0] == len(payload)
        assert converted[44:] == payload

    def test_truncated_file_keeps_what_is_there(self, tmp_path: Path) -> None:
        """Declared size larger than the file: serve the audio that survived."""
        from pbx.utils.audio import build_wav_header, wav_as_pcm16_wav

        payload = b"\x01\x02" * 100
        src = tmp_path / "truncated.wav"
        # Header promises twice the payload that follows.
        src.write_bytes(build_wav_header(len(payload) * 2) + payload)

        converted, fmt = wav_as_pcm16_wav(src)
        assert fmt == WAV_FORMAT_PCM
        assert converted is not None
        assert converted[44:] == payload

    def test_well_formed_pcm_is_still_passed_through(self, tmp_path: Path) -> None:
        """Untouched, so send_file keeps serving range requests for long calls."""
        from pbx.utils.audio import wav_as_pcm16_wav

        converted, fmt = wav_as_pcm16_wav(_wav(tmp_path / "fine.wav"))
        assert fmt == WAV_FORMAT_PCM
        assert converted is None


@pytest.mark.unit
class TestPagination:
    """Cursor paging, so a call ending mid-browse cannot shift a page boundary."""

    def test_first_page_sends_no_cursor(
        self, api_client: FlaskClient, recording_core: MagicMock
    ) -> None:
        api_client.get("/api/recordings", headers=_token("9000", is_admin=True))
        assert recording_core.recording_system.store.recent.call_args.kwargs["before_id"] is None

    def test_cursor_is_passed_through(
        self, api_client: FlaskClient, recording_core: MagicMock
    ) -> None:
        api_client.get("/api/recordings?before=42", headers=_token("9000", is_admin=True))
        assert recording_core.recording_system.store.recent.call_args.kwargs["before_id"] == 42

    def test_non_numeric_cursor_is_rejected(
        self, api_client: FlaskClient, recording_core: MagicMock
    ) -> None:
        resp = api_client.get("/api/recordings?before=abc", headers=_token("9000", is_admin=True))
        assert resp.status_code == 400

    def test_asks_for_one_more_than_the_limit(
        self, api_client: FlaskClient, recording_core: MagicMock
    ) -> None:
        """The extra row answers 'is there another page?' without a second COUNT query."""
        api_client.get("/api/recordings?limit=10", headers=_token("9000", is_admin=True))
        assert recording_core.recording_system.store.recent.call_args.kwargs["limit"] == 11

    def test_extra_row_is_trimmed_and_reported_as_more(
        self, api_client: FlaskClient, recording_core: MagicMock
    ) -> None:
        recording_core.recording_system.store.recent.return_value = [
            _recording(id=i) for i in range(1, 5)
        ]
        resp = api_client.get("/api/recordings?limit=3", headers=_token("9000", is_admin=True))
        body = resp.get_json()

        assert len(body["recordings"]) == 3, "the probe row must not be sent to the client"
        assert body["has_more"] is True
        assert body["next_before"] == 3, "the cursor is the last row actually returned"

    def test_last_page_reports_no_more(
        self, api_client: FlaskClient, recording_core: MagicMock
    ) -> None:
        recording_core.recording_system.store.recent.return_value = [_recording(id=1)]
        body = api_client.get(
            "/api/recordings?limit=3", headers=_token("9000", is_admin=True)
        ).get_json()

        assert body["has_more"] is False
        assert body["next_before"] is None
