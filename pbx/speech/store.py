"""
Where transcripts are kept.

One module owns reading and writing ``call_transcripts`` so that nothing else has to know the
column names. Voicemail already denormalises its transcript onto ``voicemail_messages`` and
keeps doing so -- the email path reads it there -- but every transcript also lands here, which
gives retention one table to sweep and the admin UI one place to query.

Takes and returns the same :class:`~pbx.speech.types.Transcript` the backends produce, so a
caller never assembles a row by hand. Nothing here raises: losing a stored transcript is
survivable, and it must never cost a voicemail or a call.

**This is shaped for live transcription as much as for files.** A live session has no file on
disk and no duration until it ends, so ``media_path`` and both durations are optional and
``call_id`` is not unique -- one call can hold several transcripts, whether that is one per leg
or a live pass plus a better post-call re-run. ``StreamSession.close()`` already returns a
``Transcript``, so a finished live session stores through this same method with
``source="live"`` and no schema change.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, Final

from pbx.utils.logger import get_logger

if TYPE_CHECKING:
    from pbx.speech.types import Transcript

__all__ = ["SOURCE_LIVE", "SOURCE_RECORDING", "SOURCE_VOICEMAIL", "TranscriptStore"]

#: Where a transcript came from. Plain strings, and the column is a plain VARCHAR, so adding
#: one later is not a migration.
SOURCE_VOICEMAIL: Final[str] = "voicemail"
SOURCE_RECORDING: Final[str] = "recording"
SOURCE_LIVE: Final[str] = "live"

#: Parameter placeholder. PostgreSQL only -- DatabaseBackend hardcodes db_type to
#: "postgresql" and there is no SQLite path in the codebase, whatever CLAUDE.md says. Matches
#: VoicemailBox._get_db_placeholder, which also returns this unconditionally.
_PH: Final[str] = "%s"

#: Columns read back, in order. Named rather than SELECT * because the row is unpacked
#: positionally and a column added in the middle would otherwise shift every field silently.
_COLUMNS: Final[str] = (
    "id, call_id, source, media_path, provider, model, language, transcript_text, "
    "segments, confidence, audio_duration, processing_duration, created_at"
)


class TranscriptStore:
    """Reads and writes transcripts. Safe to construct without a database."""

    def __init__(self, database: Any | None = None, logger: Any | None = None) -> None:
        self.database = database
        self.logger = logger or get_logger()

    @property
    def enabled(self) -> bool:
        """Whether there is somewhere to store anything."""
        return bool(self.database and getattr(self.database, "enabled", False))

    def save(
        self,
        transcript: Transcript,
        *,
        source: str,
        call_id: str | None = None,
        media_path: str | None = None,
    ) -> bool:
        """
        Store one transcript. Returns whether it was written.

        A failed transcript is not stored: there is no text to keep, and a row recording that
        an engine errored belongs in the log, not in the table the admin UI reads.
        """
        database = self.database
        if database is None or not self.enabled or not transcript.success:
            return False

        segments = None
        if transcript.segments:
            try:
                segments = json.dumps([asdict(segment) for segment in transcript.segments])
            except (TypeError, ValueError) as e:
                # Timing is a nice-to-have; the text is not. Keep the row, drop the segments.
                self.logger.warning(f"Could not serialise transcript segments: {e}")

        try:
            database.execute(
                f"INSERT INTO call_transcripts (call_id, source, media_path, provider, model, "
                f"language, transcript_text, segments, confidence, audio_duration, "
                f"processing_duration) VALUES ({', '.join([_PH] * 11)})",
                (
                    call_id,
                    source,
                    media_path,
                    transcript.provider,
                    transcript.model,
                    transcript.language,
                    transcript.text,
                    segments,
                    transcript.confidence,
                    transcript.audio_duration,
                    transcript.processing_duration,
                ),
            )
        except Exception as e:
            # Broad on purpose: every driver raises its own type, and none of them are worth
            # losing a voicemail or dropping a call over.
            self.logger.error(f"Could not store transcript for {call_id or media_path}: {e}")
            return False

        return True

    def for_call(self, call_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Every transcript for one call, newest first."""
        return self._select(f"WHERE call_id = {_PH}", (call_id,), limit)

    def recent(self, limit: int = 50, source: str | None = None) -> list[dict[str, Any]]:
        """The latest transcripts, for the admin monitoring view."""
        if source:
            return self._select(f"WHERE source = {_PH}", (source,), limit)
        return self._select("", (), limit)

    def _select(self, where: str, params: tuple, limit: int) -> list[dict[str, Any]]:
        """Run a read and shape the rows. Returns empty on any failure."""
        database = self.database
        if database is None or not self.enabled:
            return []

        try:
            # fetch_all, not execute: execute() returns a bool no matter the statement, so a
            # SELECT run through it yields True rather than rows.
            rows = database.fetch_all(
                f"SELECT {_COLUMNS} FROM call_transcripts {where} "
                f"ORDER BY created_at DESC LIMIT {int(limit)}",
                params,
            )
        except Exception as e:
            self.logger.error(f"Could not read transcripts: {e}")
            return []

        return [self._row_to_dict(row) for row in rows or []]

    @staticmethod
    def _row_to_dict(row: Any) -> dict[str, Any]:
        """One row as a dict, with segments decoded back from JSON."""
        record = dict(row)

        raw = record.get("segments")
        if raw:
            try:
                record["segments"] = json.loads(raw)
            except (TypeError, ValueError):
                # A malformed blob should not cost the caller the text next to it.
                record["segments"] = None
        return record
