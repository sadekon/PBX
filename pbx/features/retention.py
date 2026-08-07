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

import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pbx.features.retention_policies import (
    MEDIA_RECORDING,
    MEDIA_VOICEMAIL,
    HoldStore,
    PolicyStore,
    RecordingFacts,
    facts_for,
)
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
        return (
            f"{verb} {self.audio_files} audio file(s) ({self.audio_megabytes:,.1f} MB)"
            f"{sidecars} and {self.transcripts} transcript(s){held}"
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

        Audio first, transcripts second. The order matters only for the log: seeing the files
        go before the rows makes a dry-run log read the way the deletion actually happens.
        """
        result = SweepResult(dry_run=self.settings.dry_run)
        started = time.monotonic()

        # Re-read both on every sweep, so an operator's policy edit or a hold placed this
        # morning takes effect tonight rather than at the next restart.
        self.policies.load()
        self.holds.refresh()

        now = datetime.now(UTC)
        for root, media in (
            (self.settings.voicemail_path, MEDIA_VOICEMAIL),
            (self.settings.recording_path, MEDIA_RECORDING),
        ):
            self._sweep_audio(Path(root), media, now, result)

        self._sweep_transcripts(now, result)

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

    def _sweep_audio(self, root: Path, media: str, now: datetime, result: SweepResult) -> None:
        """
        Delete recordings under `root` that are past whatever period governs each of them.

        The cutoff is per file, not per sweep: two recordings in the same directory can be
        governed by different policies, so each one's age is compared against its own period.
        """
        if not root.is_dir():
            return

        fallback = timedelta(days=self.settings.audio_days)

        for path in root.rglob(AUDIO_SUFFIX):
            if path.name in KEEP_FOREVER:
                continue
            # relative_to(root) so a skip name appearing above the sweep root -- someone's
            # recordings living under a path that happens to contain it -- does not exempt
            # everything below it.
            if SKIP_DIR_NAMES.intersection(path.relative_to(root).parts[:-1]):
                continue
            try:
                stat = path.stat()
                modified = datetime.fromtimestamp(stat.st_mtime, tz=UTC)

                # Cheapest test first: nothing younger than the shortest possible period can
                # expire under any policy, and that skips almost every file on a daily sweep
                # without reading a single sidecar.
                if modified >= now - self._shortest_audio_period():
                    continue

                facts = facts_for(path, media)
                if self.holds.held(facts.session_id):
                    result.held += 1
                    self.logger.debug(f"  holding {path} (session {facts.session_id})")
                    continue

                policy = self.policies.resolve(facts)
                period = fallback
                if policy is not None and policy.audio_days is not None:
                    period = timedelta(days=policy.audio_days)
                if modified >= now - period:
                    continue

                result.audio_files += 1
                result.audio_bytes += stat.st_size
                if self.settings.dry_run:
                    self.logger.debug(
                        f"  would delete {path} ({modified:%Y-%m-%d}, "
                        f"policy {policy.policy_id if policy else 'fallback'})"
                    )
                else:
                    path.unlink()
                    self.logger.debug(f"  deleted {path}")

                self._sweep_sidecar(path, result)
            except OSError as e:
                # One unreadable file must not end the sweep; the rest still expire.
                result.errors.append(f"{path}: {e}")

        self._sweep_orphan_sidecars(root, now - fallback, result)

    def _shortest_audio_period(self) -> timedelta:
        """
        The shortest audio period any policy could impose.

        Used only as a pre-filter, so it must never be *longer* than a real period or files
        would be skipped before their policy was ever consulted.
        """
        days = [self.settings.audio_days]
        days.extend(
            p.audio_days for p in self.policies.all() if p.enabled and p.audio_days is not None
        )
        return timedelta(days=max(1, min(days)))

    def _sweep_sidecar(self, audio: Path, result: SweepResult) -> None:
        """
        Remove the manifest written beside a recording, when the recording goes.

        The sidecar names every participant on every channel. Leaving it once the audio is
        gone keeps a record of who spoke to whom, indefinitely, on a system whose whole
        retention design is that things provably expire. It is a few hundred bytes, so this
        was easy to miss and is not about disk.
        """
        sidecar = audio.with_suffix(SIDECAR_SUFFIX)
        if not sidecar.is_file():
            return

        try:
            result.sidecars += 1
            result.audio_bytes += sidecar.stat().st_size
            if not self.settings.dry_run:
                sidecar.unlink()
                self.logger.debug(f"  deleted {sidecar}")
        except OSError as e:
            result.errors.append(f"{sidecar}: {e}")

    def _sweep_orphan_sidecars(self, root: Path, cutoff: datetime, result: SweepResult) -> None:
        """
        Expire manifests whose recording is already gone.

        Catches the ones deleted before this existed, and any whose audio was removed by hand.
        Only manifests that sit beside a recording are considered -- an unrelated .json under
        the same root is somebody else's file.
        """
        for sidecar in root.rglob(f"*{SIDECAR_SUFFIX}"):
            if SKIP_DIR_NAMES.intersection(sidecar.relative_to(root).parts[:-1]):
                continue
            if sidecar.with_suffix(AUDIO_SUFFIX.removeprefix("*")).exists():
                continue
            try:
                stat = sidecar.stat()
                if datetime.fromtimestamp(stat.st_mtime, tz=UTC) >= cutoff:
                    continue

                result.sidecars += 1
                result.audio_bytes += stat.st_size
                if self.settings.dry_run:
                    self.logger.debug(f"  would delete orphaned {sidecar}")
                else:
                    sidecar.unlink()
                    self.logger.debug(f"  deleted orphaned {sidecar}")
            except OSError as e:
                result.errors.append(f"{sidecar}: {e}")

    def _sweep_transcripts(self, now: datetime, result: SweepResult) -> None:
        """
        Delete transcripts past their own, longer period.

        Policy is resolved per row from columns rather than from the sidecar, because by the
        time a transcript expires its recording is long gone -- audio runs on the short clock
        and the manifest goes with it. That is why migration 1018 puts ``participants`` and
        ``session_id`` on the row: they are the only surviving record of who was on the call.
        """
        if not (self.database and getattr(self.database, "enabled", False)):
            return

        # Widest net first, then decide per row. Fetching by the shortest period keeps the scan
        # bounded without letting a short policy's rows escape the query.
        horizon = now - self._shortest_transcript_period()
        try:
            rows = self.database.fetch_all(
                """
                SELECT id, session_id, source, participants, audio_duration, created_at
                  FROM call_transcripts
                 WHERE created_at < %s
                """,
                (horizon,),
            )
        except Exception as e:
            # Broad: every driver raises its own type, and a failed sweep must not take the
            # background thread -- or the PBX -- down with it.
            result.errors.append(f"transcript sweep failed: {e}")
            return

        fallback = timedelta(days=self.settings.transcript_days)
        expired: list[Any] = []

        for row in rows or []:
            created = row.get("created_at")
            if not isinstance(created, datetime):
                continue
            if created.tzinfo is None:
                # The column is a naive TIMESTAMP; every writer stores UTC.
                created = created.replace(tzinfo=UTC)

            session_id = str(row.get("session_id") or "")
            if self.holds.held(session_id):
                result.held += 1
                continue

            policy = self.policies.resolve(self._transcript_facts(row))
            period = fallback
            if policy is not None and policy.transcript_days is not None:
                period = timedelta(days=policy.transcript_days)

            if created < now - period:
                expired.append(row["id"])

        result.transcripts = len(expired)
        if not expired or self.settings.dry_run:
            return

        try:
            # Chunked: a first sweep on a long-neglected install can expire tens of thousands
            # of rows, and one IN list that size is a statement no driver enjoys.
            for start in range(0, len(expired), TRANSCRIPT_DELETE_CHUNK):
                chunk = expired[start : start + TRANSCRIPT_DELETE_CHUNK]
                placeholders = ", ".join(["%s"] * len(chunk))
                self.database.execute(
                    f"DELETE FROM call_transcripts WHERE id IN ({placeholders})",
                    tuple(chunk),
                )
        except Exception as e:
            result.errors.append(f"transcript delete failed: {e}")

    def _shortest_transcript_period(self) -> timedelta:
        """Shortest transcript period any policy could impose. See `_shortest_audio_period`."""
        days = [self.settings.transcript_days]
        days.extend(
            p.transcript_days
            for p in self.policies.all()
            if p.enabled and p.transcript_days is not None
        )
        return timedelta(days=max(1, min(days)))

    def _transcript_facts(self, row: dict) -> RecordingFacts:
        """
        Rebuild match facts from a transcript row.

        ``source`` is 'recording', 'voicemail' or 'live'; the first two line up with the media
        vocabulary, and a live transcript simply matches no media rule.
        """
        raw = row.get("participants")
        participants: tuple[str, ...] = ()
        if raw:
            try:
                parsed = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(parsed, list):
                    participants = tuple(str(p) for p in parsed)
            except (TypeError, ValueError):
                participants = ()

        duration = row.get("audio_duration")
        return RecordingFacts(
            path=Path(),
            media=str(row.get("source") or ""),
            session_id=str(row.get("session_id") or ""),
            participants=participants,
            duration_seconds=float(duration) if isinstance(duration, int | float) else None,
        )
