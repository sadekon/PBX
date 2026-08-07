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
        """Real signature: -> dict | None."""
        self.calls.append((query, params))
        if self._raises:
            raise self._raises
        return self._rows[0] if self._rows else None


@pytest.mark.unit
class TestMigrationSql:
    """
    The migration SQL is PostgreSQL, which is not reachable from the test suite.

    SQLite is close enough to prove the statements parse, the columns are spelled the way the
    store reads them, and nullability is what live transcription needs. It cannot prove
    PostgreSQL accepts them, so this is a syntax and shape check, not a substitute for
    running it against a real database.
    """

    @staticmethod
    def _sqlite_ddl():
        from pbx.utils.migrations import MigrationManager

        manager = MigrationManager(MagicMock())
        manager.migrations = []
        from pbx.utils.migrations import register_all_migrations

        register_all_migrations(manager)
        sql = next(m["sql"] for m in manager.migrations if m["version"] == 1017)
        # SERIAL is PostgreSQL's; SQLite spells the same idea differently.
        return sql.replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")

    def test_1017_is_registered(self):
        from pbx.utils.migrations import MigrationManager, register_all_migrations

        manager = MigrationManager(MagicMock())
        manager.migrations = []
        register_all_migrations(manager)

        versions = [m["version"] for m in manager.migrations]
        assert 1017 in versions
        assert len(versions) == len(set(versions)), "duplicate migration version"

    def test_both_tables_are_created(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(self._sqlite_ddl())

        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"call_transcripts", "call_summaries"} <= names

    def test_columns_match_what_the_store_reads(self):
        """The store unpacks rows positionally, so a rename here is silent corruption."""
        from pbx.speech.store import _COLUMNS

        conn = sqlite3.connect(":memory:")
        conn.executescript(self._sqlite_ddl())

        actual = {r[1] for r in conn.execute("PRAGMA table_info(call_transcripts)")}
        assert {name.strip() for name in _COLUMNS.split(",")} <= actual

    def test_a_live_shaped_row_is_accepted(self):
        """No file, no durations, no call_id: what a live session has before it ends."""
        conn = sqlite3.connect(":memory:")
        conn.executescript(self._sqlite_ddl())

        conn.execute(
            "INSERT INTO call_transcripts (source, transcript_text) VALUES (?, ?)",
            (SOURCE_LIVE, "half a sentence so f"),
        )

        assert conn.execute("SELECT COUNT(*) FROM call_transcripts").fetchone()[0] == 1

    def test_one_call_may_hold_several_transcripts(self):
        """Per-leg, or a live pass plus a post-call re-run. A UNIQUE here would block both."""
        conn = sqlite3.connect(":memory:")
        conn.executescript(self._sqlite_ddl())

        conn.execute("INSERT INTO call_transcripts (call_id, source) VALUES (?, ?)", ("c1", "live"))
        conn.execute(
            "INSERT INTO call_transcripts (call_id, source) VALUES (?, ?)", ("c1", "recording")
        )

        assert conn.execute("SELECT COUNT(*) FROM call_transcripts").fetchone()[0] == 2

    def test_confidence_stays_null_rather_than_zero(self):
        """Whisper cannot report one, and 0.0 would render to a user as 0% accuracy."""
        conn = sqlite3.connect(":memory:")
        conn.executescript(self._sqlite_ddl())

        conn.execute("INSERT INTO call_transcripts (source) VALUES ('recording')")

        assert conn.execute("SELECT confidence FROM call_transcripts").fetchone()[0] is None


@pytest.mark.unit
class TestTranscriptStore:
    def test_a_transcript_is_written(self):
        db = _FakeDatabase()

        assert TranscriptStore(db).save(_transcript(), source=SOURCE_VOICEMAIL, call_id="vm1")

        query, params = db.calls[0]
        assert "INSERT INTO call_transcripts" in query
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

        TranscriptStore(db).save(_transcript(segments=(segment,)), source=SOURCE_LIVE, call_id="c1")

        stored = json.loads(db.calls[0][1][7])
        assert stored[0]["text"] == "hello"
        assert stored[0]["words"][0]["text"] == "hello"

    def test_segments_are_decoded_on_read(self):
        row = {
            "id": 1,
            "call_id": "c1",
            "source": "live",
            "transcript_text": "hi",
            "segments": '[{"text": "hi"}]',
        }
        db = _FakeDatabase(rows=[row])

        record = TranscriptStore(db).for_call("c1")[0]

        assert record["transcript_text"] == "hi"
        assert record["segments"] == [{"text": "hi"}]

    def test_malformed_segments_do_not_lose_the_text(self):
        row = {"call_id": "c1", "transcript_text": "hi", "segments": "{not json"}
        db = _FakeDatabase(rows=[row])

        record = TranscriptStore(db).for_call("c1")[0]

        assert record["transcript_text"] == "hi"
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
class TestParticipantsAndSession:
    """
    Who was on the call, denormalised onto the transcript row.

    Both are derivable from the recording's sidecar manifest -- but audio expires on a far
    shorter clock than text, so by the time a transcript comes up for retention the manifest
    is gone. These columns are the only surviving record, and both retention matching and the
    review surface's "was I on this call?" read them here.
    """

    def test_participants_are_stored_as_json(self):
        db = _FakeDatabase()

        TranscriptStore(db).save(
            _transcript(),
            source=SOURCE_RECORDING,
            call_id="c1",
            session_id="sess-1",
            participants=["1512", "1513"],
        )

        query, params = db.calls[0]
        assert "participants" in query
        assert json.loads(params[-1]) == ["1512", "1513"]

    def test_session_id_is_stored(self):
        db = _FakeDatabase()

        TranscriptStore(db).save(_transcript(), source=SOURCE_RECORDING, session_id="sess-1")

        assert db.calls[0][1][-2] == "sess-1"

    def test_speakers_are_inferred_when_participants_are_not_given(self):
        """A caller that knows nothing about the recording still stores something matchable."""
        db = _FakeDatabase()
        segments = (
            Segment(text="hello", start=0.0, end=0.5, speaker="1512"),
            Segment(text="hi", start=1.0, end=1.5, speaker="1513"),
        )

        TranscriptStore(db).save(_transcript(segments=segments), source=SOURCE_RECORDING)

        assert json.loads(db.calls[0][1][-1]) == ["1512", "1513"]

    def test_inferred_speakers_keep_first_heard_order_without_duplicates(self):
        db = _FakeDatabase()
        segments = (
            Segment(text="a", start=0.0, end=0.5, speaker="1513"),
            Segment(text="b", start=1.0, end=1.5, speaker="1512"),
            Segment(text="c", start=2.0, end=2.5, speaker="1513"),
        )

        TranscriptStore(db).save(_transcript(segments=segments), source=SOURCE_RECORDING)

        assert json.loads(db.calls[0][1][-1]) == ["1513", "1512"]

    def test_no_participants_stores_null_not_an_empty_array(self):
        """Null is 'unknown'; [] would claim the call provably had nobody on it."""
        db = _FakeDatabase()

        TranscriptStore(db).save(_transcript(), source=SOURCE_VOICEMAIL)

        assert db.calls[0][1][-1] is None

    def test_explicit_participants_win_over_inferred_speakers(self):
        db = _FakeDatabase()
        segments = (Segment(text="hello", start=0.0, end=0.5, speaker="a0"),)

        TranscriptStore(db).save(
            _transcript(segments=segments),
            source=SOURCE_RECORDING,
            participants=["1512"],
        )

        assert json.loads(db.calls[0][1][-1]) == ["1512"]

    def test_1018_adds_the_columns_the_store_writes(self):
        from pbx.utils.migrations import MigrationManager, register_all_migrations

        manager = MigrationManager(MagicMock())
        manager.migrations = []
        register_all_migrations(manager)

        sql = next(m["sql"] for m in manager.migrations if m["version"] == 1018)
        assert "participants" in sql
        assert "session_id" in sql
