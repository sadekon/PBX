"""
Where recorded media is registered.

One row per captured file, whatever produced it -- a call recording, a voicemail, and in time
anything else that writes audio. A voicemail *is* a recording with a mailbox attached, and
modelling them separately is what made every retention bug need fixing twice.

This owns the answer to "what else goes with a recording". Transcripts reference it, summaries
reference those, and the voicemail row references it, all with ``ON DELETE CASCADE`` -- so
deleting a recording deletes everything derived from it because the database says so, not
because a caller remembered. Every retention hole this design replaces was a missed second
place.

**The row outlives the file.** Audio and text run on separate clocks: retention unlinks the
``.wav`` and stamps ``audio_deleted_at``, leaving the row and its transcript. A row with
``path`` set and no file on disk is the bug that used to produce phantom voicemails, so the
two are updated together here rather than by whoever happens to delete something.

Nothing here raises. Losing the registration of a recording is survivable; taking down a call
or a sweep is not.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Final

from pbx.features.retention_policies import MEDIA_RECORDING, MEDIA_VOICEMAIL
from pbx.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

__all__ = ["KIND_CALL", "KIND_VOICEMAIL", "RecordingStore"]

#: What produced the media, and *the same vocabulary retention policies match on*. Imported
#: rather than redeclared: they were briefly two lists -- "call"/"voicemail" here against
#: "recording"/"voicemail" there -- and a media policy then matched nothing at all, silently,
#: because the value it compared against never occurred.
KIND_CALL: Final[str] = MEDIA_RECORDING
KIND_VOICEMAIL: Final[str] = MEDIA_VOICEMAIL

#: PostgreSQL only -- DatabaseBackend hardcodes db_type and there is no SQLite path.
_PH: Final[str] = "%s"

#: Ids per statement. Bounds the IN list on a first sweep, which on a long-neglected install
#: can expire everything at once.
_CHUNK: Final[int] = 500

_COLUMNS: Final[str] = (
    "id, session_id, call_id, kind, path, bytes, duration_seconds, sample_rate, "
    "channels, participants, started_at, ended_at, audio_deleted_at, created_at"
)


class RecordingStore:
    """Reads and writes the ``recordings`` table. Safe to construct without a database."""

    def __init__(self, database: Any | None = None, logger: Any | None = None) -> None:
        self.database = database
        self.logger = logger or get_logger()

    @property
    def enabled(self) -> bool:
        return bool(self.database and getattr(self.database, "enabled", False))

    # ---------------------------------------------------------------- writing

    def register(
        self,
        *,
        path: Path | str | None,
        kind: str = KIND_CALL,
        session_id: str | None = None,
        call_id: str | None = None,
        duration_seconds: float | None = None,
        sample_rate: int | None = None,
        channels: Sequence[dict[str, Any]] | None = None,
        participants: Sequence[str] | None = None,
        started_at: Any = None,
        ended_at: Any = None,
        bytes_written: int | None = None,
    ) -> int | None:
        """
        Record that a file exists, returning its id.

        `channels` is the map the recorder used to write as a ``.json`` sidecar: which track
        holds whom. It lives here now because a sidecar had to be swept in lockstep with its
        audio and could orphan when it was not -- and an orphaned manifest names every
        participant on a call whose recording is already gone.

        `participants` is derivable from `channels` and stored anyway, so neither retention
        matching nor "was I on this call?" has to parse JSON per row.
        """
        database = self.database
        if database is None or not self.enabled:
            return None

        if participants is None and channels:
            participants = self._labels(channels)

        try:
            row = database.fetch_one(
                f"""
                INSERT INTO recordings (
                    session_id, call_id, kind, path, bytes, duration_seconds, sample_rate,
                    channels, participants, started_at, ended_at
                ) VALUES ({", ".join([_PH] * 11)})
                RETURNING id
                """,
                (
                    session_id,
                    call_id,
                    kind,
                    str(path) if path is not None else None,
                    bytes_written,
                    duration_seconds,
                    sample_rate,
                    json.dumps(list(channels)) if channels else None,
                    json.dumps(list(participants)) if participants else None,
                    started_at,
                    ended_at,
                ),
            )
        except Exception as e:
            self.logger.error(f"Could not register recording {path}: {e}")
            return None

        # Defensive: a driver that does not honour RETURNING, or a mock, can hand back
        # something unusable here, and this runs at the end of a call.
        try:
            return int(row["id"]) if row else None
        except (KeyError, TypeError, ValueError):
            self.logger.warning("Recording stored but its id could not be read")
            return None

    def mark_audio_deleted(self, ids: Sequence[Any]) -> int:
        """
        Note that the media is gone while keeping the row.

        Clearing `path` at the same time is the point: a row naming a file that is not there
        is what made expired voicemails keep appearing in mailboxes, pointing at nothing.
        """
        return self._update_by_id(
            ids,
            "SET audio_deleted_at = CURRENT_TIMESTAMP, path = NULL "
            "WHERE audio_deleted_at IS NULL AND id IN",
        )

    def delete(self, ids: Sequence[Any]) -> int:
        """
        Remove recordings, and by cascade their transcripts, summaries and voicemail rows.

        The cascade is declared in migration 1019 rather than performed here on purpose. It
        used to be this caller's job to remember every derived table, and the tables it forgot
        -- call_summaries, the duplicate voicemail transcript -- each outlived their source
        for months.
        """
        return self._update_by_id(ids, "", delete=True)

    def _update_by_id(self, ids: Sequence[Any], clause: str, delete: bool = False) -> int:
        database = self.database
        if database is None or not self.enabled or not ids:
            return 0

        affected = 0
        for start in range(0, len(ids), _CHUNK):
            chunk = list(ids[start : start + _CHUNK])
            marks = ", ".join([_PH] * len(chunk))
            sql = (
                f"DELETE FROM recordings WHERE id IN ({marks})"
                if delete
                else f"UPDATE recordings {clause} ({marks})"
            )
            try:
                if database.execute(sql, tuple(chunk)):
                    affected += len(chunk)
            except Exception as e:
                self.logger.error(f"Could not update recordings: {e}")

        return affected

    # ---------------------------------------------------------------- reading

    def get(self, recording_id: Any) -> dict[str, Any] | None:
        rows = self._select(f"WHERE id = {_PH}", (recording_id,), limit=1)
        return rows[0] if rows else None

    def for_session(self, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return self._select(f"WHERE session_id = {_PH}", (session_id,), limit)

    def recent(self, limit: int = 50, kind: str | None = None) -> list[dict[str, Any]]:
        """The latest recordings, for the admin view that has never had one."""
        if kind:
            return self._select(f"WHERE kind = {_PH}", (kind,), limit)
        return self._select("", (), limit)

    def expiring_before(self, cutoff: Any, kind: str | None = None) -> list[dict[str, Any]]:
        """
        Candidates for an audio sweep: rows whose media still exists and predates `cutoff`.

        Deliberately wider than the final answer -- each row's real period depends on the
        policy that matches it, which is resolved per row by the sweeper.
        """
        where = f"WHERE audio_deleted_at IS NULL AND path IS NOT NULL AND created_at < {_PH}"
        params: tuple[Any, ...] = (cutoff,)
        if kind:
            where += f" AND kind = {_PH}"
            params = (cutoff, kind)
        return self._select(where, params, limit=100000)

    def _select(self, where: str, params: tuple, limit: int) -> list[dict[str, Any]]:
        database = self.database
        if database is None or not self.enabled:
            return []

        try:
            # fetch_all, not execute: execute() returns a bool no matter the statement, so a
            # SELECT run through it comes back as True and then fails on subscripting.
            rows = database.fetch_all(
                f"SELECT {_COLUMNS} FROM recordings {where} "
                f"ORDER BY created_at DESC LIMIT {int(limit)}",
                params,
            )
        except Exception as e:
            self.logger.error(f"Could not read recordings: {e}")
            return []

        return [self._decode(row) for row in rows or []]

    @staticmethod
    def _decode(row: Any) -> dict[str, Any]:
        """Shape a row, turning the JSON columns back into structures."""
        data = dict(row)
        for key in ("channels", "participants"):
            raw = data.get(key)
            if isinstance(raw, str):
                try:
                    data[key] = json.loads(raw)
                except (TypeError, ValueError):
                    data[key] = None
        return data

    @staticmethod
    def _labels(channels: Sequence[dict[str, Any]]) -> list[str]:
        """Distinct participant labels in channel order."""
        seen: list[str] = []
        for channel in channels:
            label = str(channel.get("label", "")).strip()
            if label and label not in seen:
                seen.append(label)
        return seen
