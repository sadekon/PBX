"""
Where transcripts are kept.

One module owns reading and writing ``transcripts`` so nothing else has to know the column
names. A transcript belongs to a *recording* -- ``recording_id`` with ``ON DELETE CASCADE`` --
so expiring the audio's row takes its text with it, and anything derived from that text
(``call_summaries``) goes too. That relationship used to be a ``media_path`` string, which
dangled the moment retention deleted the file, while duplicated copies of the same text sat on
``voicemail_messages`` and ``call_summaries`` and each had to be swept by hand.

Takes and returns the same :class:`~pbx.speech.types.Transcript` the backends produce, so a
caller never assembles a row by hand. Nothing here raises: losing a stored transcript is
survivable, and it must never cost a voicemail or a call.

**Shaped for live transcription as much as for files.** A live session has no recording, so
``recording_id`` is nullable and those rows expire on their own ``created_at`` rather than by
cascade. ``StreamSession.close()`` already returns a ``Transcript``, so a finished live session
stores through this same method with ``source="live"`` and no schema change.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, Final

from pbx.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pbx.speech.types import Transcript

__all__ = ["SOURCE_LIVE", "SOURCE_RECORDING", "SOURCE_VOICEMAIL", "TranscriptStore"]

#: Where a transcript came from. Plain strings, and the column is a plain VARCHAR, so adding
#: one later is not a migration.
SOURCE_VOICEMAIL: Final[str] = "voicemail"
SOURCE_RECORDING: Final[str] = "recording"
SOURCE_LIVE: Final[str] = "live"

#: PostgreSQL only -- DatabaseBackend hardcodes db_type and there is no SQLite path.
_PH: Final[str] = "%s"

#: Ids per DELETE. Bounds the IN list on a first sweep, which on a long-neglected install can
#: expire everything at once.
_DELETE_CHUNK: Final[int] = 500

#: Columns read back. Named rather than SELECT * because a column added in the middle would
#: otherwise shift every field silently.
_COLUMNS: Final[str] = (
    "id, recording_id, session_id, source, provider, model, language, text, "
    "segments, confidence, processing_duration, created_at"
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
        recording_id: Any = None,
        session_id: str | None = None,
    ) -> int | None:
        """
        Store one transcript, returning its id.

        A failed transcript is not stored: there is no text to keep, and a row recording that
        an engine errored belongs in the log, not in the table the admin UI reads.

        `session_id` is kept alongside `recording_id` because a live transcript has no
        recording, and a legal hold is placed on a session rather than on a file.
        """
        database = self.database
        if database is None or not self.enabled or not transcript.success:
            return None

        segments = None
        if transcript.segments:
            try:
                segments = json.dumps([asdict(segment) for segment in transcript.segments])
            except (TypeError, ValueError) as e:
                # Timing is a nice-to-have; the text is not. Keep the row, drop the segments.
                self.logger.warning(f"Could not serialise transcript segments: {e}")

        try:
            row = database.fetch_one(
                f"""
                INSERT INTO transcripts (
                    recording_id, session_id, source, provider, model, language,
                    text, segments, confidence, processing_duration
                ) VALUES ({", ".join([_PH] * 10)})
                RETURNING id
                """,
                (
                    recording_id,
                    session_id,
                    source,
                    transcript.provider,
                    transcript.model,
                    transcript.language,
                    transcript.text,
                    segments,
                    transcript.confidence,
                    transcript.processing_duration,
                ),
            )
        except Exception as e:
            # Broad on purpose: every driver raises its own type, and none of them are worth
            # losing a voicemail or dropping a call over.
            self.logger.error(f"Could not store transcript for recording {recording_id}: {e}")
            return None

        # Defensive: a driver that does not honour RETURNING, or a mock, can hand back
        # something unusable here, and this runs at the end of a call.
        try:
            return int(row["id"]) if row else None
        except (KeyError, TypeError, ValueError):
            self.logger.warning("Transcript stored but its id could not be read")
            return None

    def delete(self, ids: Sequence[Any]) -> int:
        """
        Delete transcripts by id. Summaries follow by cascade, not by this method remembering.

        Only needed for transcripts with no recording -- a live pass. Anything attached to a
        recording is expired by deleting the recording, which is the single deletion path the
        schema was reshaped to give us.
        """
        database = self.database
        if database is None or not self.enabled or not ids:
            return 0

        removed = 0
        for start in range(0, len(ids), _DELETE_CHUNK):
            chunk = list(ids[start : start + _DELETE_CHUNK])
            marks = ", ".join([_PH] * len(chunk))
            try:
                if database.execute(f"DELETE FROM transcripts WHERE id IN ({marks})", tuple(chunk)):
                    removed += len(chunk)
            except Exception as e:
                self.logger.error(f"Could not delete transcripts: {e}")

        return removed

    def for_recording(self, recording_id: Any, limit: int = 50) -> list[dict[str, Any]]:
        """Every transcript of one recording, newest first."""
        return self._select(f"WHERE recording_id = {_PH}", (recording_id,), limit)

    def for_session(self, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Every transcript of one conversation, across its legs."""
        return self._select(f"WHERE session_id = {_PH}", (session_id,), limit)

    def recent(self, limit: int = 50, source: str | None = None) -> list[dict[str, Any]]:
        """The latest transcripts, for the admin monitoring view."""
        if source:
            return self._select(f"WHERE source = {_PH}", (source,), limit)
        return self._select("", (), limit)

    def orphans_before(self, cutoff: Any) -> list[dict[str, Any]]:
        """
        Transcripts with no recording, older than `cutoff`.

        Live transcripts have nothing to cascade from, so they are the only ones retention has
        to expire directly.
        """
        return self._select(
            f"WHERE recording_id IS NULL AND created_at < {_PH}", (cutoff,), limit=100000
        )

    def _select(self, where: str, params: tuple, limit: int) -> list[dict[str, Any]]:
        """Run a read and shape the rows. Returns empty on any failure."""
        database = self.database
        if database is None or not self.enabled:
            return []

        try:
            # fetch_all, not execute: execute() returns a bool no matter the statement, so a
            # SELECT run through it yields True rather than rows.
            rows = database.fetch_all(
                f"SELECT {_COLUMNS} FROM transcripts {where} "
                f"ORDER BY created_at DESC LIMIT {int(limit)}",
                params,
            )
        except Exception as e:
            self.logger.error(f"Could not read transcripts: {e}")
            return []

        return [self._row_to_dict(row) for row in rows or []]

    @staticmethod
    def _row_to_dict(row: Any) -> dict[str, Any]:
        """Shape a row, decoding the segments JSON."""
        data = dict(row)
        raw = data.get("segments")
        if isinstance(raw, str):
            try:
                data["segments"] = json.loads(raw)
            except (TypeError, ValueError):
                # Malformed timing must not cost the text, which is the part anyone reads.
                data["segments"] = None
        return data
