"""
Deleting recordings, voicemail and transcripts once they are old enough.

This is the *executor*; :mod:`pbx.features.retention_policies` is the policy store it acts on.
Until those two were joined there were two retention systems that never met: one owned
policies and had no caller, the other deleted on a timer and knew no policies. The admin UI
edited the first while the second did the deleting, so a saved policy governed nothing.

Every file is now resolved against the policy table, falling back to the periods below when no
policy matches. Anything under an unreleased legal hold is skipped whatever its age.

Two clocks, deliberately, not one:

* **Audio is short-lived.** It is where the bytes are -- roughly 170 KB for a 21-second G.711
  recording -- and it is the part with real privacy weight. Someone's voice.
* **Transcripts live longer.** A couple of KB, about 2% of the audio, carrying most of what
  makes a recording worth keeping. Keeping text for a year costs almost nothing.

That split is the whole design. Deleting a voicemail already keeps its transcript
(``VoicemailBox.delete_message`` tombstones the row rather than removing it); this expires the
two independently on their own schedules.

**Deletion is off until you turn it on.** ``dry_run`` defaults to true, so the first runs only
report what they *would* remove. On a system where retention has never executed, the first real
sweep deletes everything already past its period -- which may be everything. Read a dry-run log
before flipping it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pbx.features.recording_store import RecordingStore
from pbx.features.retention_policies import (
    MEDIA_RECORDING,
    MEDIA_VOICEMAIL,
    HoldStore,
    PolicyStore,
    RecordingFacts,
)
from pbx.speech.store import TranscriptStore
from pbx.utils.logger import get_logger
from pbx.utils.periodic import PeriodicTask

__all__ = ["RetentionSettings", "RetentionSweeper", "SweepResult"]

CONFIG_SECTION = "retention"

DEFAULT_AUDIO_DAYS = 90
DEFAULT_TRANSCRIPT_DAYS = 365
DEFAULT_INTERVAL_HOURS = 24.0

#: Transcript rows deleted per statement. Bounds the IN list on a first sweep, which on a
#: long-neglected install can expire everything at once.
TRANSCRIPT_DELETE_CHUNK = 500

#: Only these are swept. A retention sweep walking a directory it does not understand is how
#: you lose a greeting, a prompt or somebody's music-on-hold.
AUDIO_SUFFIX = "*.wav"

#: The manifest a recording writes beside itself, naming which channel holds which participant.
#: Swept with the audio it describes -- it is the only other file that identifies the people on
#: a call, and it must not outlive the recording.
SIDECAR_SUFFIX = ".json"

#: Never expired: a greeting is configuration a user recorded, not a message that arrived.
KEEP_FOREVER = frozenset({"greeting.wav"})

#: Directories skipped whole, whatever is inside them.
#:
#: Transcription cuts a finished recording into per-participant regions and works on them
#: under ``recordings/.transcribe/``, on the same filesystem as the recording so the copies
#: are not cross-device. Those region files are ``.wav`` and would otherwise be swept on the
#: audio clock as though they were recordings -- deleting the input of a job in progress, and
#: counting bytes twice in the process. They are owned by
#: :class:`~pbx.speech.recording.RecordingTranscriber`, which removes each workspace when it
#: finishes and clears any survivors at startup.
#:
#: Must match ``pbx.speech.recording.SCRATCH_DIRNAME``; a test asserts they do.
SKIP_DIR_NAMES = frozenset({".transcribe"})


def _as_utc(value: Any) -> datetime | None:
    """
    Normalise a timestamp column to an aware UTC datetime, or None if it cannot be read.

    Strings are parsed as well as datetimes. psycopg2 hands back datetimes, but the codebase
    has already been bitten by string timestamps elsewhere (``VoicemailBox._load_messages``
    parses them defensively), and here a value that failed to convert would silently mean
    "skip this row" -- a row that never expires rather than a visible error.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


@dataclass(frozen=True, slots=True)
class RetentionSettings:
    """How long things live. Read once at construction, like every other settings object."""

    enabled: bool = False
    #: Report rather than delete. On by default, and the single most important key here.
    dry_run: bool = True
    interval_hours: float = DEFAULT_INTERVAL_HOURS
    audio_days: int = DEFAULT_AUDIO_DAYS
    transcript_days: int = DEFAULT_TRANSCRIPT_DAYS
    voicemail_path: str = "voicemail"
    recording_path: str = "recordings"

    @classmethod
    def from_dict(cls, section: dict[str, Any]) -> RetentionSettings:
        """Build from the raw config section. Takes a dict so this never imports Config."""

        def as_int(key: str, default: int) -> int:
            try:
                return int(section.get(key, default))
            except (TypeError, ValueError):
                return default

        return cls(
            enabled=bool(section.get("enabled", False)),
            dry_run=bool(section.get("dry_run", True)),
            interval_hours=float(section.get("interval_hours", DEFAULT_INTERVAL_HOURS) or 24),
            audio_days=as_int("audio_days", DEFAULT_AUDIO_DAYS),
            transcript_days=as_int("transcript_days", DEFAULT_TRANSCRIPT_DAYS),
            voicemail_path=str(section.get("voicemail_path", "voicemail")),
            recording_path=str(section.get("recording_path", "recordings")),
        )

    def validate(self) -> list[str]:
        """Every problem found. Empty means usable."""
        problems: list[str] = []
        if not self.enabled:
            return problems

        if self.audio_days <= 0:
            problems.append(f"{CONFIG_SECTION}.audio_days must be positive")
        if self.transcript_days <= 0:
            problems.append(f"{CONFIG_SECTION}.transcript_days must be positive")
        if self.transcript_days < self.audio_days:
            # Not fatal, but almost certainly a mistake: it throws away the cheap, useful half
            # while keeping the expensive half.
            problems.append(
                f"{CONFIG_SECTION}.transcript_days ({self.transcript_days}) is shorter than "
                f"audio_days ({self.audio_days}); transcripts are ~2% of the size and usually "
                "the part worth keeping longer"
            )
        if self.interval_hours <= 0:
            problems.append(f"{CONFIG_SECTION}.interval_hours must be positive")
        return problems


@dataclass(slots=True)
class SweepResult:
    """What one sweep did, or would have done."""

    dry_run: bool = True
    audio_files: int = 0
    audio_bytes: int = 0
    #: Channel manifests removed alongside their recording, or orphaned ones expired on their
    #: own. Counted separately so the audio figure stays a count of recordings.
    sidecars: int = 0
    transcripts: int = 0
    #: Voicemail rows removed once their last content expired. Counted apart from
    #: transcripts because one is a row and the other is text on it.
    voicemail_messages: int = 0
    #: Items left alone because their session is under an unreleased legal hold. Reported
    #: because "nothing expired this week" and "nothing expired because 400 calls are frozen
    #: for a lawsuit" need to look different in the log.
    held: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def audio_megabytes(self) -> float:
        return self.audio_bytes / 1_048_576

    def summary(self) -> str:
        verb = "would delete" if self.dry_run else "deleted"
        sidecars = f", {self.sidecars} manifest(s)" if self.sidecars else ""
        held = f", {self.held} held" if self.held else ""
        mailbox = f", {self.voicemail_messages} voicemail row(s)" if self.voicemail_messages else ""
        return (
            f"{verb} {self.audio_files} audio file(s) ({self.audio_megabytes:,.1f} MB)"
            f"{sidecars} and {self.transcripts} transcript(s){mailbox}{held}"
        )


class RetentionSweeper:
    """
    Expires audio and transcripts on their own schedules.

    Owns the timer and the deletion; owns no policy beyond the two periods. Constructed
    unconditionally like the mailer and the transcriber, so callers get a real object that
    reports its own state rather than something to guard with hasattr.
    """

    def __init__(
        self,
        settings: RetentionSettings,
        database: Any | None = None,
        logger: Any | None = None,
        policies: PolicyStore | None = None,
        holds: HoldStore | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.logger = logger or get_logger()
        # Constructed rather than required, so a caller that only wants the flat periods still
        # gets a working sweeper -- an empty policy store resolves to the fallback every time.
        self.policies = policies if policies is not None else PolicyStore(database, self.logger)
        self.holds = holds if holds is not None else HoldStore(database, self.logger)
        #: The sweep reads recordings and deletes them; the cascade in migration 1019
        #: takes transcripts and summaries. Live transcripts have no recording, so the
        #: transcript store is still needed for those.
        self.recordings = RecordingStore(database, self.logger)
        self._transcripts = TranscriptStore(database, self.logger)

        # Reported to the admin UI. Lifetime counters advance only on real deletions, never on
        # dry runs -- a "Deleted (All Time)" figure that counts things still sitting on disk is
        # worse than no figure, and the previous UI showed exactly that kind of number.
        self.last_sweep: datetime | None = None
        self.last_result: SweepResult | None = None
        self.lifetime_audio_deleted = 0
        self.lifetime_transcripts_deleted = 0
        self._task = PeriodicTask(
            "RetentionSweeper",
            settings.interval_hours * 3600,
            self.sweep,
            logger=self.logger,
        )

    @property
    def running(self) -> bool:
        return self._task.running

    def start(self) -> None:
        """Begin sweeping, if enabled. Says why when it does not."""
        if not self.settings.enabled:
            self.logger.info("Retention sweeper is disabled")
            return

        for problem in self.settings.validate():
            self.logger.warning(f"Retention config: {problem}")

        # Load before seeding: seed() only writes into an empty table, so it has to know
        # whether one is already there. After the first start this is a no-op.
        self.policies.load()
        self.policies.seed(self.settings.audio_days, self.settings.transcript_days)

        mode = "DRY RUN - nothing will be deleted" if self.settings.dry_run else "deleting"
        self.logger.info(
            f"Retention: {len(self.policies.all())} policy/policies, fallback audio "
            f"{self.settings.audio_days}d, transcripts {self.settings.transcript_days}d ({mode})"
        )
        self._task.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._task.stop(timeout)

    def sweep(self) -> SweepResult:
        """
        Run one full pass. Safe to call directly, which is what makes it testable.

        Driven by the ``recordings`` table rather than by walking directories. A row knows its
        participants, duration and session, so a policy can be resolved without opening a
        sidecar; and deleting a row takes its transcripts and their summaries by cascade. The
        filesystem walk that remains is only a safety net for files no row points at.

        Audio first, rows second. The order matters only for the log: seeing the files go
        before the rows makes a dry-run read the way deletion actually happens.
        """
        result = SweepResult(dry_run=self.settings.dry_run)
        started = time.monotonic()

        # Re-read both on every sweep, so an operator's policy edit or a hold placed this
        # morning takes effect tonight rather than at the next restart.
        self.policies.load()
        self.holds.refresh()

        now = datetime.now(UTC)
        self._sweep_audio(now, result)
        self._sweep_rows(now, result)
        self._sweep_live_transcripts(now, result)
        self._sweep_unregistered_files(now, result)

        self.last_sweep = now
        self.last_result = result
        if not result.dry_run:
            self.lifetime_audio_deleted += result.audio_files
            self.lifetime_transcripts_deleted += result.transcripts

        self.logger.info(
            f"Retention sweep: {result.summary()} in {time.monotonic() - started:.1f}s"
        )
        for error in result.errors:
            self.logger.warning(f"Retention: {error}")
        return result

    # ------------------------------------------------------------------ audio

    def _sweep_audio(self, now: datetime, result: SweepResult) -> None:
        """
        Expire media whose recording row is past its audio period.

        The row outlives the file: the path is cleared and ``audio_deleted_at`` stamped, so a
        mailbox stops listing a message whose audio is gone. Skipping that step is what used
        to leave phantom voicemails pointing at nothing.
        """
        horizon = now - self._shortest_audio_period()
        rows = self.recordings.expiring_before(horizon)
        expired: list[Any] = []

        for row in rows:
            facts = self._facts(row)
            if self.holds.held(facts.session_id):
                result.held += 1
                continue

            created = _as_utc(row.get("created_at"))
            if created is None:
                continue

            policy = self.policies.resolve(facts)
            period = timedelta(days=self.settings.audio_days)
            if policy is not None and policy.audio_days is not None:
                period = timedelta(days=policy.audio_days)
            if created >= now - period:
                continue

            result.audio_files += 1
            result.audio_bytes += int(row.get("bytes") or 0)
            expired.append(row["id"])

            if not self.settings.dry_run:
                self._unlink(row.get("path"), result)

        if expired and not self.settings.dry_run:
            self.recordings.mark_audio_deleted(expired)

    def _unlink(self, path: Any, result: SweepResult) -> None:
        """Remove a recording and the sidecar beside it. Neither is fatal."""
        if not path:
            return
        media = Path(str(path))
        for target in (media, media.with_suffix(SIDECAR_SUFFIX)):
            try:
                target.unlink()
            except FileNotFoundError:
                continue
            except OSError as e:
                result.errors.append(f"{target}: {e}")
            else:
                if target is not media:
                    result.sidecars += 1

    # ------------------------------------------------------------------ rows

    def _sweep_rows(self, now: datetime, result: SweepResult) -> None:
        """
        Delete recording rows past their transcript period.

        This is the whole retention story in one statement: the cascade declared in migration
        1020 takes each row's transcripts, their summaries, and the voicemail row that points
        at it. Every hole this design replaced -- the duplicate voicemail transcript, the
        summary nobody swept, the row nothing deleted -- was a second place somebody forgot.
        """
        horizon = now - self._shortest_transcript_period()
        rows = self.recordings.expiring_before(horizon) + self._rows_without_media(horizon)
        seen: set[Any] = set()
        expired: list[Any] = []

        for row in rows:
            if row["id"] in seen:
                continue
            seen.add(row["id"])

            facts = self._facts(row)
            if self.holds.held(facts.session_id):
                result.held += 1
                continue

            created = _as_utc(row.get("created_at"))
            if created is None:
                continue

            policy = self.policies.resolve(facts)
            period = timedelta(days=self.settings.transcript_days)
            if policy is not None and policy.transcript_days is not None:
                period = timedelta(days=policy.transcript_days)
            if created >= now - period:
                continue

            expired.append(row["id"])
            if row.get("kind") == MEDIA_VOICEMAIL:
                result.voicemail_messages += 1

        result.transcripts += len(expired)
        if expired and not self.settings.dry_run:
            for row_id in expired:
                self._unlink_by_id(row_id, result)
            self.recordings.delete(expired)

    def _rows_without_media(self, horizon: Any) -> list[dict[str, Any]]:
        """
        Rows the audio sweep will not return: media already expired, or never present.

        ``expiring_before`` only yields rows that still have a file, since that is what an
        audio sweep acts on. Everything else has to be caught here or it never expires at all
        -- both the row whose audio went months ago and still holds its transcript, and the
        row that never had a file (a registration whose media was never written).
        """
        try:
            rows = self.recordings.database.fetch_all(  # type: ignore[union-attr]
                "SELECT id, session_id, kind, participants, duration_seconds, created_at, "
                "path, bytes FROM recordings "
                "WHERE (audio_deleted_at IS NOT NULL OR path IS NULL) AND created_at < %s",
                (horizon,),
            )
        except Exception as e:
            self.logger.error(f"Could not read expired-audio recordings: {e}")
            return []
        return [self.recordings._decode(r) for r in rows or []]

    def _unlink_by_id(self, row_id: Any, result: SweepResult) -> None:
        """Remove any media still on disk for a row that is about to be deleted."""
        row = self.recordings.get(row_id)
        if row:
            self._unlink(row.get("path"), result)

    # ------------------------------------------------------------------ transcripts

    def _sweep_live_transcripts(self, now: datetime, result: SweepResult) -> None:
        """
        Expire transcripts that have no recording to cascade from.

        Only live transcription produces these: there is no file, so nothing owns the row.
        Everything else is expired by deleting its recording.
        """
        cutoff = now - timedelta(days=self.settings.transcript_days)
        rows = self._transcripts.orphans_before(cutoff)
        if not rows:
            return

        expired = []
        for row in rows:
            if self.holds.held(str(row.get("session_id") or "")):
                result.held += 1
                continue
            expired.append(row["id"])

        result.transcripts += len(expired)
        if expired and not self.settings.dry_run:
            self._transcripts.delete(expired)

    # ------------------------------------------------------------------ orphans

    def _sweep_unregistered_files(self, now: datetime, result: SweepResult) -> None:
        """
        Delete audio on disk that no recording row points at.

        A safety net, not the main path. Registration can fail -- a database blip at the end
        of a call -- and a file nothing knows about would otherwise sit there forever, outside
        every policy and every clock. Judged on mtime against the fallback period, because
        there is no row to resolve a policy from.
        """
        cutoff = now - timedelta(days=self.settings.audio_days)
        known = self._known_paths()

        for root_name in (self.settings.voicemail_path, self.settings.recording_path):
            root = Path(root_name)
            if not root.is_dir():
                continue

            for path in root.rglob(AUDIO_SUFFIX):
                if path.name in KEEP_FOREVER:
                    continue
                if SKIP_DIR_NAMES.intersection(path.relative_to(root).parts[:-1]):
                    continue
                if str(path) in known:
                    continue
                try:
                    stat = path.stat()
                    if datetime.fromtimestamp(stat.st_mtime, tz=UTC) >= cutoff:
                        continue

                    result.audio_files += 1
                    result.audio_bytes += stat.st_size
                    if self.settings.dry_run:
                        self.logger.debug(f"  would delete unregistered {path}")
                    else:
                        self._unlink(path, result)
                except OSError as e:
                    result.errors.append(f"{path}: {e}")

    def _known_paths(self) -> set[str]:
        """Every path a recording row still claims."""
        try:
            rows = self.recordings.database.fetch_all(  # type: ignore[union-attr]
                "SELECT path FROM recordings WHERE path IS NOT NULL"
            )
        except Exception as e:
            # Failing open here would delete every registered recording as though it were an
            # orphan, so an unreadable table means the safety net simply does not fire.
            self.logger.error(f"Could not list known recording paths ({e}); skipping orphans")
            raise
        return {str(r["path"]) for r in rows or [] if r.get("path")}

    # ------------------------------------------------------------------ helpers

    def _facts(self, row: dict[str, Any]) -> RecordingFacts:
        """Match facts straight off a recording row -- no sidecar, no path parsing."""
        participants = row.get("participants") or []
        if not isinstance(participants, list):
            participants = []

        duration = row.get("duration_seconds")
        return RecordingFacts(
            path=Path(str(row.get("path") or "")),
            media=str(row.get("kind") or MEDIA_RECORDING),
            session_id=str(row.get("session_id") or ""),
            participants=tuple(str(p) for p in participants),
            duration_seconds=float(duration) if isinstance(duration, int | float) else None,
        )

    def _shortest_audio_period(self) -> timedelta:
        """
        The shortest audio period any policy could impose.

        A pre-filter only, so it must never be *longer* than a real period or rows would be
        skipped before their policy was ever consulted.
        """
        days = [self.settings.audio_days]
        days.extend(
            p.audio_days for p in self.policies.all() if p.enabled and p.audio_days is not None
        )
        return timedelta(days=max(1, min(days)))

    def _shortest_transcript_period(self) -> timedelta:
        """Shortest transcript period any policy could impose. See `_shortest_audio_period`."""
        days = [self.settings.transcript_days]
        days.extend(
            p.transcript_days
            for p in self.policies.all()
            if p.enabled and p.transcript_days is not None
        )
        return timedelta(days=max(1, min(days)))

    def statistics(self) -> dict[str, Any]:
        """
        What the admin UI shows. Deliberately reports `dry_run`.

        Without it the page cannot distinguish "retention ran and had nothing to remove" from
        "retention is in report-only mode and has never removed anything" -- and those look
        identical in every counter here.
        """
        managed = 0
        for root in (self.settings.voicemail_path, self.settings.recording_path):
            path = Path(root)
            if not path.is_dir():
                continue
            managed += sum(
                1
                for f in path.rglob(AUDIO_SUFFIX)
                if f.name not in KEEP_FOREVER
                and not SKIP_DIR_NAMES.intersection(f.relative_to(path).parts[:-1])
            )

        return {
            "enabled": self.settings.enabled,
            "running": self.running,
            "dry_run": self.settings.dry_run,
            "policies": len(self.policies.all()),
            "active_holds": len(self.holds.active),
            "managed_recordings": managed,
            "fallback_audio_days": self.settings.audio_days,
            "fallback_transcript_days": self.settings.transcript_days,
            "last_sweep": self.last_sweep.isoformat() if self.last_sweep else None,
            "lifetime_audio_deleted": self.lifetime_audio_deleted,
            "lifetime_transcripts_deleted": self.lifetime_transcripts_deleted,
            "last_sweep_summary": self.last_result.summary() if self.last_result else None,
        }
