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
    """Records what would have been executed, and replays rows for reads."""

    def __init__(self, rows=None, raises=None):
        self.enabled = True
        self.calls = []
        self._rows = rows or []
        self._raises = raises

    def execute(self, query, params=None):
        self.calls.append((query, params))
        if self._raises:
            raise self._raises
        return self._rows


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
        row = (
            1,
            "c1",
            "live",
            None,
            "vosk",
            "m",
            "en",
            "hi",
            '[{"text": "hi"}]',
            None,
            1.0,
            0.3,
            "now",
        )
        db = _FakeDatabase(rows=[row])

        record = TranscriptStore(db).for_call("c1")[0]

        assert record["transcript_text"] == "hi"
        assert record["segments"] == [{"text": "hi"}]

    def test_malformed_segments_do_not_lose_the_text(self):
        row = (1, "c1", "live", None, "vosk", "m", "en", "hi", "{not json", None, 1.0, 0.3, "now")
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

    def test_a_read_error_returns_nothing_rather_than_raising(self):
        db = _FakeDatabase(raises=RuntimeError("gone"))

        assert TranscriptStore(db).recent() == []
