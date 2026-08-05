"""
Retention sweeping and the shared periodic-task loop.

Retention is the only thing in this PBX that deletes user data on a timer, and it has never
run: ``cleanup_old_recordings`` was written with no caller. So the tests that matter most here
are the ones proving it does *not* delete -- dry_run being on, greetings being skipped, a bad
file not aborting the pass -- because the first real sweep on a system that has never expired
anything removes everything already past its period.
"""

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pbx.features.retention import RetentionSettings, RetentionSweeper
from pbx.utils.periodic import PeriodicTask


def _aged(path: Path, days: float) -> Path:
    """Write a file and backdate its mtime."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 1024)
    old = (datetime.now(UTC) - timedelta(days=days)).timestamp()
    import os

    os.utime(path, (old, old))
    return path


def _settings(tmp_path: Path, **overrides) -> RetentionSettings:
    base = {
        "enabled": True,
        "dry_run": True,
        "voicemail_path": str(tmp_path / "voicemail"),
        "recording_path": str(tmp_path / "recordings"),
    }
    base.update(overrides)
    return RetentionSettings.from_dict(base)


@pytest.mark.unit
class TestRetentionSettings:
    def test_defaults_are_safe(self):
        """Off, and not deleting even when on. Both matter."""
        settings = RetentionSettings.from_dict({})

        assert not settings.enabled
        assert settings.dry_run
        assert settings.audio_days == 90
        assert settings.transcript_days == 365

    def test_disabled_reports_nothing(self):
        assert RetentionSettings.from_dict({"enabled": False, "audio_days": -5}).validate() == []

    def test_transcripts_shorter_than_audio_is_flagged(self):
        """Almost always a mistake: it discards the cheap useful half and keeps the costly one."""
        problems = RetentionSettings.from_dict(
            {"enabled": True, "audio_days": 90, "transcript_days": 30}
        ).validate()

        assert any("shorter than" in p for p in problems)

    def test_nonsense_values_fall_back_rather_than_raising(self):
        settings = RetentionSettings.from_dict({"audio_days": "ninety"})

        assert settings.audio_days == 90


@pytest.mark.unit
class TestSweep:
    def test_dry_run_counts_but_deletes_nothing(self, tmp_path):
        old = _aged(tmp_path / "voicemail" / "1001" / "old.wav", days=120)

        result = RetentionSweeper(_settings(tmp_path)).sweep()

        assert result.audio_files == 1
        assert old.exists(), "dry run must not delete"
        assert "would delete" in result.summary()

    def test_deleting_removes_the_file(self, tmp_path):
        old = _aged(tmp_path / "voicemail" / "1001" / "old.wav", days=120)

        result = RetentionSweeper(_settings(tmp_path, dry_run=False)).sweep()

        assert result.audio_files == 1
        assert not old.exists()

    def test_recent_audio_is_untouched(self, tmp_path):
        recent = _aged(tmp_path / "voicemail" / "1001" / "new.wav", days=3)

        result = RetentionSweeper(_settings(tmp_path, dry_run=False)).sweep()

        assert result.audio_files == 0
        assert recent.exists()

    def test_greetings_are_never_expired(self, tmp_path):
        """A greeting is configuration a user recorded, not a message that arrived."""
        greeting = _aged(tmp_path / "voicemail" / "1001" / "greeting.wav", days=9999)

        result = RetentionSweeper(_settings(tmp_path, dry_run=False)).sweep()

        assert greeting.exists()
        assert result.audio_files == 0

    def test_non_wav_files_are_left_alone(self, tmp_path):
        """The sweep walks a directory it does not own; it must touch only what it understands."""
        note = _aged(tmp_path / "voicemail" / "1001" / "notes.txt", days=9999)

        RetentionSweeper(_settings(tmp_path, dry_run=False)).sweep()

        assert note.exists()

    def test_both_roots_are_swept(self, tmp_path):
        _aged(tmp_path / "voicemail" / "a.wav", days=200)
        _aged(tmp_path / "recordings" / "b.wav", days=200)

        result = RetentionSweeper(_settings(tmp_path, dry_run=False)).sweep()

        assert result.audio_files == 2

    def test_a_missing_directory_is_not_an_error(self, tmp_path):
        result = RetentionSweeper(_settings(tmp_path)).sweep()

        assert result.audio_files == 0
        assert result.errors == []

    def test_one_bad_file_does_not_abort_the_pass(self, tmp_path):
        """The rest must still expire; a sweep that stops on the first problem never finishes."""
        _aged(tmp_path / "voicemail" / "good.wav", days=200)
        doomed = _aged(tmp_path / "voicemail" / "doomed.wav", days=200)
        settings = _settings(tmp_path, dry_run=False)
        sweeper = RetentionSweeper(settings)

        real_unlink = Path.unlink

        def explode(self, *args, **kwargs):
            if self.name == "doomed.wav":
                raise OSError("permission denied")
            return real_unlink(self, *args, **kwargs)

        import unittest.mock

        with unittest.mock.patch.object(Path, "unlink", explode):
            result = sweeper.sweep()

        assert any("doomed.wav" in e for e in result.errors)
        assert not (tmp_path / "voicemail" / "good.wav").exists()
        assert doomed.exists()

    def _db(self, expired=0):
        """
        Shaped like the real DatabaseBackend, which is three methods, not one.

        execute() returns a bool for *every* statement, so counting through it yields True and
        then fails on subscripting. That reached the server as
        "transcript sweep failed: 'bool' object is not subscriptable"; a MagicMock returning
        rows from execute() had happily agreed with the wrong code.
        """
        db = MagicMock()
        db.enabled = True
        db.execute.return_value = True
        db.fetch_one.return_value = {"expired": expired}
        return db

    def test_transcripts_are_counted_but_not_deleted_in_dry_run(self, tmp_path):
        db = self._db(expired=7)

        result = RetentionSweeper(_settings(tmp_path), database=db).sweep()

        assert result.transcripts == 7
        db.execute.assert_not_called()

    def test_counting_uses_fetch_not_execute(self, tmp_path):
        """The regression itself: a SELECT through execute() comes back as True."""
        db = self._db(expired=2)

        RetentionSweeper(_settings(tmp_path), database=db).sweep()

        db.fetch_one.assert_called_once()
        assert "SELECT COUNT" in str(db.fetch_one.call_args[0][0])

    def test_transcripts_are_deleted_when_not_dry_run(self, tmp_path):
        db = self._db(expired=3)

        RetentionSweeper(_settings(tmp_path, dry_run=False), database=db).sweep()

        statements = " ".join(str(c[0][0]) for c in db.execute.call_args_list)
        assert "DELETE FROM call_transcripts" in statements

    def test_nothing_is_deleted_when_nothing_expired(self, tmp_path):
        db = self._db(expired=0)

        RetentionSweeper(_settings(tmp_path, dry_run=False), database=db).sweep()

        db.execute.assert_not_called()

    def test_a_database_failure_does_not_raise(self, tmp_path):
        """This runs on a background thread; an exception here would kill the sweep for good."""
        db = self._db()
        db.fetch_one.side_effect = RuntimeError("connection reset")

        result = RetentionSweeper(_settings(tmp_path), database=db).sweep()

        assert any("transcript sweep failed" in e for e in result.errors)

    def test_disabled_sweeper_never_starts(self, tmp_path):
        sweeper = RetentionSweeper(_settings(tmp_path, enabled=False))

        sweeper.start()

        assert not sweeper.running


@pytest.mark.unit
class TestPeriodicTask:
    def test_it_runs_and_stops(self):
        calls = []
        task = PeriodicTask("t", 0.01, lambda: calls.append(1), run_on_start=True)

        task.start()
        time.sleep(0.08)
        task.stop(timeout=1)

        assert calls, "task never ran"
        assert not task.running

    def test_stop_returns_promptly_despite_a_long_interval(self):
        """
        The bug this replaced: SecurityMonitor slept the full 300s, so stop() blocked for up to
        five minutes -- longer than the entire 30s shutdown budget. Waiting on an Event instead
        of sleeping means the interval no longer bounds shutdown.
        """
        task = PeriodicTask("slow", 3600, lambda: None, run_on_start=True)
        task.start()
        time.sleep(0.05)

        started = time.monotonic()
        task.stop(timeout=2)

        assert time.monotonic() - started < 1.0

    def test_a_raising_task_keeps_running(self):
        calls = []

        def boom():
            calls.append(1)
            raise ValueError("bad")

        task = PeriodicTask("boom", 0.01, boom, run_on_start=True, error_interval=0.01)
        task.start()
        time.sleep(0.08)
        task.stop(timeout=1)

        assert len(calls) > 1, "loop died on the first exception"

    def test_starting_twice_does_not_make_a_second_thread(self):
        task = PeriodicTask("once", 0.01, lambda: None)

        task.start()
        first = task._thread
        task.start()

        assert task._thread is first
        task.stop(timeout=1)

    def test_stop_without_start_is_harmless(self):
        PeriodicTask("never", 1, lambda: None).stop()
