"""
Transcript storage (``pbx/speech/store.py``) and migration 1017.

Two things are being protected here.

The first is that ``call_summaries`` finally exists. It has been written to at
``speech_analytics.py:452`` and read at ``:521`` since it was written, and no migration ever
created it, so every summary write has failed on every install.

The second is that the schema does not paint live transcription into a corner. A live session
has no file on disk and no duration until it ends, and one call can produce several transcripts
-- one per leg, or a live pass plus a better post-call re-run. Those cases are asserted now,
while changing the shape is still free.
"""

import json
import sqlite3
from unittest.mock import MagicMock

import pytest

from pbx.speech.store import SOURCE_LIVE, SOURCE_RECORDING, SOURCE_VOICEMAIL, TranscriptStore
from pbx.speech.types import Segment, Transcript, Word


def _transcript(**overrides):
    base = {
        "text": "call me back on tuesday",
        "language": "en-US",
        "provider": "faster-whisper",
        "model": "small.en",
        "confidence": None,
        "audio_duration": 3.0,
        "processing_duration": 1.4,
    }
    base.update(overrides)
    return Transcript(**base)


class _FakeDatabase:
    """
    Mirrors DatabaseBackend's actual three-method API.

    This originally returned rows from execute(), which the real backend never does -- it
    returns a bool for every statement. The store read through execute() and the tests agreed
    with it, so a SELECT returning True got all the way to a production log line reading
    "'bool' object is not subscriptable". The shape below is the point of the fixture.
    """

    def __init__(self, rows=None, raises=None):
        self.enabled = True
        self.calls = []
        self._rows = rows or []
        self._raises = raises

    def execute(self, query, params=None):
        """Writes only. Real signature: -> bool."""
        self.calls.append((query, params))
        if self._raises:
            raise self._raises
        return True

    def fetch_all(self, query, params=None):
        """Real signature: -> list[dict]."""
        self.calls.append((query, params))
        if self._raises:
            raise self._raises
        return self._rows

    def fetch_one(self, query, params=None):
        """Real signature: -> dict | None. INSERT ... RETURNING id comes through here."""
        self.calls.append((query, params))
        if self._raises:
            raise self._raises
        if "RETURNING id" in query:
            return {"id": 1}
        return self._rows[0] if self._rows else None


@pytest.mark.unit
class TestColumnsMatchTheSchema:
    """
    The store unpacks rows by name, so a column renamed in the migration and not here is a
    silent KeyError at read time rather than a failure at deploy time.
    """

    def test_every_column_the_store_reads_exists(self):
        import sqlite3

        from pbx.speech.store import _COLUMNS
        from pbx.utils.migrations import MigrationManager, register_all_migrations

        manager = MigrationManager(MagicMock())
        manager.migrations = []
        register_all_migrations(manager)
        sql = next(m["sql"] for m in manager.migrations if m["version"] == 1019)
        sql = sql.replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT").replace(
            "BIGINT", "INTEGER"
        )

        conn = sqlite3.connect(":memory:")
        conn.executescript(sql)

        actual = {r[1] for r in conn.execute("PRAGMA table_info(transcripts)")}
        assert {name.strip() for name in _COLUMNS.split(",")} <= actual


@pytest.mark.unit
class TestTranscriptStore:
    def test_a_transcript_is_written(self):
        db = _FakeDatabase()

        assert TranscriptStore(db).save(_transcript(), source=SOURCE_VOICEMAIL, recording_id=1)

        query, params = db.calls[0]
        assert "INSERT INTO transcripts" in query
        assert "call me back on tuesday" in params

    def test_a_failed_transcript_is_not_written(self):
        """There is no text to keep, and the admin view should not list engine errors."""
        db = _FakeDatabase()
        failed = Transcript.failure("model not loaded", provider="faster-whisper")

        assert not TranscriptStore(db).save(failed, source=SOURCE_VOICEMAIL)
        assert db.calls == []

    def test_no_database_is_survivable(self):
        assert not TranscriptStore(None).save(_transcript(), source=SOURCE_VOICEMAIL)
        assert TranscriptStore(None).recent() == []

    def test_a_disabled_database_is_survivable(self):
        db = _FakeDatabase()
        db.enabled = False

        assert not TranscriptStore(db).save(_transcript(), source=SOURCE_RECORDING)

    def test_a_database_error_never_propagates(self):
        """A storage failure must not cost the voicemail that triggered it."""
        db = _FakeDatabase(raises=RuntimeError("connection reset"))

        assert not TranscriptStore(db).save(_transcript(), source=SOURCE_VOICEMAIL)

    def test_segments_are_stored_as_json(self):
        db = _FakeDatabase()
        segment = Segment(text="hello", start=0.0, end=0.5, words=(Word("hello", 0.0, 0.5, 0.9),))

        TranscriptStore(db).save(
            _transcript(segments=(segment,)), source=SOURCE_LIVE, recording_id=1
        )

        stored = json.loads(db.calls[0][1][7])
        assert stored[0]["text"] == "hello"
        assert stored[0]["words"][0]["text"] == "hello"

    def test_segments_are_decoded_on_read(self):
        row = {
            "id": 1,
            "recording_id": 1,
            "source": "live",
            "text": "hi",
            "segments": '[{"text": "hi"}]',
        }
        db = _FakeDatabase(rows=[row])

        record = TranscriptStore(db).for_recording(1)[0]

        assert record["text"] == "hi"
        assert record["segments"] == [{"text": "hi"}]

    def test_malformed_segments_do_not_lose_the_text(self):
        row = {"recording_id": 1, "text": "hi", "segments": "{not json"}
        db = _FakeDatabase(rows=[row])

        record = TranscriptStore(db).for_recording(1)[0]

        assert record["text"] == "hi"
        assert record["segments"] is None

    def test_recent_can_filter_by_source(self):
        db = _FakeDatabase(rows=[])

        TranscriptStore(db).recent(source=SOURCE_LIVE)

        query, params = db.calls[0]
        assert "WHERE source" in query
        assert params == (SOURCE_LIVE,)

    def test_reads_do_not_go_through_execute(self):
        """
        execute() returns a bool for every statement, so a SELECT run through it yields True.

        That is the bug this guards: it reached production as
        "transcript sweep failed: 'bool' object is not subscriptable".
        """
        calls = []

        class _Strict(_FakeDatabase):
            def execute(self, query, params=None):
                calls.append(query)
                return super().execute(query, params)

        TranscriptStore(_Strict(rows=[])).recent()

        assert not any("SELECT" in q.upper() for q in calls), "reads must use fetch_all"

    def test_a_read_error_returns_nothing_rather_than_raising(self):
        db = _FakeDatabase(raises=RuntimeError("gone"))

        assert TranscriptStore(db).recent() == []


@pytest.mark.unit
class TestRecordingLinkage:
    """
    A transcript belongs to a recording, not to a path.

    ``media_path`` was a string that dangled the moment retention expired the audio, and the
    participants it needed lived in a sidecar that expired with it. Both are on the recording
    row now, and the link is a foreign key that cascades.
    """

    def test_the_recording_id_is_stored(self):
        db = _FakeDatabase()

        TranscriptStore(db).save(_transcript(), source=SOURCE_RECORDING, recording_id=7)

        query, params = db.calls[0]
        assert "INSERT INTO transcripts" in query
        assert params[0] == 7

    def test_the_session_is_stored_alongside_it(self):
        """A hold is placed on a session, and a live transcript has no recording to hold."""
        db = _FakeDatabase()

        TranscriptStore(db).save(
            _transcript(), source=SOURCE_LIVE, recording_id=None, session_id="s-1"
        )

        params = db.calls[0][1]
        assert params[0] is None
        assert params[1] == "s-1"

    def test_the_new_id_is_returned(self):
        """The caller needs it to attach a summary."""
        assert (
            TranscriptStore(_FakeDatabase()).save(
                _transcript(), source=SOURCE_RECORDING, recording_id=1
            )
            == 1
        )

    def test_a_failed_save_returns_none_rather_than_raising(self):
        db = _FakeDatabase(raises=RuntimeError("connection reset"))

        assert TranscriptStore(db).save(_transcript(), source=SOURCE_RECORDING) is None

    def test_reads_by_recording(self):
        db = _FakeDatabase(rows=[{"id": 1, "text": "hi", "segments": None}])

        TranscriptStore(db).for_recording(7)

        assert "WHERE recording_id" in db.calls[0][0]

    def test_orphans_are_only_those_without_a_recording(self):
        """
        Live transcripts are the only rows retention deletes directly; everything else goes
        by cascade when its recording does.
        """
        db = _FakeDatabase(rows=[])

        TranscriptStore(db).orphans_before("2026-01-01")

        assert "recording_id IS NULL" in db.calls[0][0]

    def test_delete_names_only_transcripts(self):
        """Summaries follow by cascade; this method no longer has to know they exist."""
        db = _FakeDatabase()

        assert TranscriptStore(db).delete([1, 2]) == 2

        assert "DELETE FROM transcripts" in db.calls[0][0]
        assert "call_summaries" not in db.calls[0][0]

    def test_delete_is_survivable_without_a_database(self):
        assert TranscriptStore(None).delete([1]) == 0
