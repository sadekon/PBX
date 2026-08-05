"""
Deleting recordings, voicemail and transcripts once they are old enough.

Nothing in this PBX has ever actually expired. ``RecordingRetentionManager`` has policies, a
scanner and a working ``cleanup_old_recordings`` -- and no caller, anywhere. Voicemail never
had retention at all. So both ``recordings/`` and ``voicemail/`` grow until the disk does not.

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

from pbx.utils.logger import get_logger
from pbx.utils.periodic import PeriodicTask

__all__ = ["RetentionSettings", "RetentionSweeper", "SweepResult"]

CONFIG_SECTION = "retention"

DEFAULT_AUDIO_DAYS = 90
DEFAULT_TRANSCRIPT_DAYS = 365
DEFAULT_INTERVAL_HOURS = 24.0

#: Only these are swept. A retention sweep walking a directory it does not understand is how
#: you lose a greeting, a prompt or somebody's music-on-hold.
AUDIO_SUFFIX = "*.wav"

#: Never expired: a greeting is configuration a user recorded, not a message that arrived.
KEEP_FOREVER = frozenset({"greeting.wav"})


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
    transcripts: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def audio_megabytes(self) -> float:
        return self.audio_bytes / 1_048_576

    def summary(self) -> str:
        verb = "would delete" if self.dry_run else "deleted"
        return (
            f"{verb} {self.audio_files} audio file(s) ({self.audio_megabytes:,.1f} MB) "
            f"and {self.transcripts} transcript(s)"
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
    ) -> None:
        self.settings = settings
        self.database = database
        self.logger = logger or get_logger()
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

        mode = "DRY RUN - nothing will be deleted" if self.settings.dry_run else "deleting"
        self.logger.info(
            f"Retention: audio {self.settings.audio_days}d, "
            f"transcripts {self.settings.transcript_days}d ({mode})"
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

        cutoff = datetime.now(UTC) - timedelta(days=self.settings.audio_days)
        for root in (self.settings.voicemail_path, self.settings.recording_path):
            self._sweep_audio(Path(root), cutoff, result)

        self._sweep_transcripts(result)

        self.logger.info(
            f"Retention sweep: {result.summary()} in {time.monotonic() - started:.1f}s"
        )
        for error in result.errors:
            self.logger.warning(f"Retention: {error}")
        return result

    def _sweep_audio(self, root: Path, cutoff: datetime, result: SweepResult) -> None:
        """Delete recordings under `root` last modified before `cutoff`."""
        if not root.is_dir():
            return

        for path in root.rglob(AUDIO_SUFFIX):
            if path.name in KEEP_FOREVER:
                continue
            try:
                stat = path.stat()
                modified = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
                if modified >= cutoff:
                    continue

                result.audio_files += 1
                result.audio_bytes += stat.st_size
                if self.settings.dry_run:
                    self.logger.debug(f"  would delete {path} ({modified:%Y-%m-%d})")
                else:
                    path.unlink()
                    self.logger.debug(f"  deleted {path}")
            except OSError as e:
                # One unreadable file must not end the sweep; the rest still expire.
                result.errors.append(f"{path}: {e}")

    def _sweep_transcripts(self, result: SweepResult) -> None:
        """Delete transcripts past their own, longer period."""
        if not (self.database and getattr(self.database, "enabled", False)):
            return

        cutoff = datetime.now(UTC) - timedelta(days=self.settings.transcript_days)
        try:
            rows = self.database.execute(
                "SELECT COUNT(*) FROM call_transcripts WHERE created_at < %s", (cutoff,)
            )
            count = int(rows[0][0]) if rows and rows[0] else 0
            result.transcripts = count

            if count and not self.settings.dry_run:
                self.database.execute(
                    "DELETE FROM call_transcripts WHERE created_at < %s", (cutoff,)
                )
        except Exception as e:
            # Broad: every driver raises its own type, and a failed sweep must not take the
            # background thread -- or the PBX -- down with it.
            result.errors.append(f"transcript sweep failed: {e}")
