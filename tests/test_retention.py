"""
Retention sweeping and the shared periodic-task loop.

Retention is the only thing in this PBX that deletes user data on a timer, and it has never
run: ``cleanup_old_recordings`` was written with no caller. So the tests that matter most here
are the ones proving it does *not* delete -- dry_run being on, greetings being skipped, a bad
file not aborting the pass -- because the first real sweep on a system that has never expired
anything removes everything already past its period.
"""

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pbx.features.retention import SKIP_DIR_NAMES, RetentionSettings, RetentionSweeper
from pbx.features.retention_policies import (
    MEDIA_RECORDING,
    MEDIA_VOICEMAIL,
    HoldStore,
    PolicyStore,
    RecordingFacts,
    RetentionPolicy,
    validate_match_rules,
)
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


def _facts(**overrides) -> RecordingFacts:
    """A recording row's worth of match facts."""
    base = {
        "path": Path("recordings/a.wav"),
        "media": MEDIA_RECORDING,
        "session_id": "s-1",
        "participants": ("1512", "1513"),
        "duration_seconds": 42.0,
    }
    base.update(overrides)
    return RecordingFacts(**base)


class _Rows:
    """
    A recordings table, in memory, behind the real store's API.

    The sweep is driven by rows now rather than by walking directories, so the fixture that
    matters is a table rather than a tmp_path tree.
    """

    def __init__(self, rows=None, policies=(), holds=()):
        self.enabled = True
        self.rows = {r["id"]: dict(r) for r in (rows or [])}
        self.policies = list(policies)
        self.holds = list(holds)
        self.statements = []

    # -- DatabaseBackend shape

    def fetch_all(self, query, params=None):
        q = " ".join(query.split())
        if "FROM retention_policies" in q:
            return [self._policy_row(p) for p in self.policies]
        if "FROM retention_holds" in q:
            return [{"session_id": s} for s in self.holds]
        if "FROM transcripts" in q:
            return []
        if "FROM recordings" in q:
            return self._recordings(q, params)
        return []

    def fetch_one(self, query, params=None):
        rows = self.fetch_all(query, params)
        return rows[0] if rows else None

    def execute(self, query, params=None):
        q = " ".join(query.split())
        self.statements.append((q, params))
        if q.startswith("DELETE FROM recordings"):
            for row_id in params or ():
                self.rows.pop(row_id, None)
        elif q.startswith("UPDATE recordings"):
            for row_id in params or ():
                if row_id in self.rows:
                    self.rows[row_id]["audio_deleted_at"] = datetime.now(UTC)
                    self.rows[row_id]["path"] = None
        return True

    # -- helpers

    def _recordings(self, q, params):
        rows = list(self.rows.values())
        if "SELECT path FROM recordings" in q:
            return [{"path": r["path"]} for r in rows if r.get("path")]
        if "audio_deleted_at IS NOT NULL OR path IS NULL" in q:
            return [r for r in rows if r.get("audio_deleted_at") or not r.get("path")]
        if "WHERE id =" in q:
            return [r for r in rows if r["id"] == params[0]]
        # expiring_before: still has media
        return [r for r in rows if r.get("path") and not r.get("audio_deleted_at")]

    @staticmethod
    def _policy_row(policy):
        return {
            "policy_id": policy.policy_id,
            "name": policy.name,
            "description": policy.description,
            "audio_days": policy.audio_days,
            "transcript_days": policy.transcript_days,
            "priority": policy.priority,
            "match_rules": json.dumps(policy.match_rules),
            "enabled": policy.enabled,
            "origin": policy.origin,
        }


def _row(row_id=1, days_old=200, **overrides):
    row = {
        "id": row_id,
        "session_id": f"s-{row_id}",
        "call_id": f"c-{row_id}",
        "kind": MEDIA_RECORDING,
        "path": f"recordings/{row_id}.wav",
        "bytes": 1024,
        "duration_seconds": 42.0,
        "sample_rate": 8000,
        "channels": None,
        "participants": json.dumps(["1512", "1513"]),
        "started_at": None,
        "ended_at": None,
        "audio_deleted_at": None,
        "created_at": datetime.now(UTC) - timedelta(days=days_old),
    }
    row.update(overrides)
    return row


@pytest.mark.unit
class TestAudioSweep:
    """
    Audio expires on its own clock; the row survives to carry the transcript.

    The row outliving its file is the whole two-clock design. It is also where the phantom
    voicemails came from: the sweep deleted files and left rows claiming they existed.
    """

    def _sweeper(self, tmp_path, db, **overrides):
        return RetentionSweeper(_settings(tmp_path, **overrides), database=db)

    def test_an_expired_recording_is_tombstoned(self, tmp_path):
        db = _Rows([_row(days_old=200)])

        result = self._sweeper(tmp_path, db, dry_run=False, audio_days=90).sweep()

        assert result.audio_files == 1
        assert db.rows[1]["audio_deleted_at"] is not None
        assert db.rows[1]["path"] is None, "a row naming a missing file is the phantom bug"

    def test_the_row_and_its_transcript_survive(self, tmp_path):
        db = _Rows([_row(days_old=200)])

        self._sweeper(tmp_path, db, dry_run=False, audio_days=90).sweep()

        assert 1 in db.rows
        assert not [q for q, _ in db.statements if q.startswith("DELETE FROM recordings")]

    def test_a_recent_recording_is_untouched(self, tmp_path):
        db = _Rows([_row(days_old=5)])

        result = self._sweeper(tmp_path, db, dry_run=False, audio_days=90).sweep()

        assert result.audio_files == 0
        assert db.rows[1]["path"] is not None

    def test_dry_run_counts_but_changes_nothing(self, tmp_path):
        db = _Rows([_row(days_old=200)])

        result = self._sweeper(tmp_path, db, dry_run=True, audio_days=90).sweep()

        assert result.audio_files == 1
        assert db.rows[1]["path"] is not None
        assert "would delete" in result.summary()

    def test_a_policy_can_extend_audio_retention(self, tmp_path):
        vip = RetentionPolicy(
            "vip", "VIP", audio_days=3650, priority=5, match_rules={"extensions": ["1513"]}
        )
        db = _Rows([_row(days_old=200)], policies=[vip])

        result = self._sweeper(tmp_path, db, dry_run=False, audio_days=90).sweep()

        assert result.audio_files == 0

    def test_a_media_policy_matches_the_row_kind(self, tmp_path):
        """
        kind and the policy vocabulary are one list, imported rather than redeclared. They
        were briefly two, and a media policy then matched nothing at all, silently.
        """
        vm = RetentionPolicy(
            "vm", "Voicemail", audio_days=1, priority=5, match_rules={"media": MEDIA_VOICEMAIL}
        )
        db = _Rows([_row(days_old=30, kind=MEDIA_VOICEMAIL)], policies=[vm])

        result = self._sweeper(tmp_path, db, dry_run=False, audio_days=90).sweep()

        assert result.audio_files == 1


@pytest.mark.unit
class TestRowSweep:
    """Deleting a recording is the single deletion path; the cascade does the rest."""

    def test_a_row_past_the_transcript_clock_is_deleted(self, tmp_path):
        db = _Rows([_row(days_old=900)])

        result = RetentionSweeper(
            _settings(tmp_path, dry_run=False, transcript_days=365), database=db
        ).sweep()

        assert 1 not in db.rows
        assert result.transcripts == 1

    def test_a_row_whose_audio_already_went_is_still_expired(self, tmp_path):
        """
        Rows with no media are invisible to the audio query, so they have to be caught
        separately -- otherwise a recording expired months ago keeps its transcript forever.
        """
        db = _Rows([_row(days_old=900, path=None, audio_deleted_at=datetime.now(UTC))])

        RetentionSweeper(
            _settings(tmp_path, dry_run=False, transcript_days=365), database=db
        ).sweep()

        assert 1 not in db.rows

    def test_a_voicemail_row_is_counted_as_one(self, tmp_path):
        db = _Rows([_row(days_old=900, kind=MEDIA_VOICEMAIL)])

        result = RetentionSweeper(
            _settings(tmp_path, dry_run=False, transcript_days=365), database=db
        ).sweep()

        assert result.voicemail_messages == 1

    def test_dry_run_deletes_no_rows(self, tmp_path):
        db = _Rows([_row(days_old=900)])

        result = RetentionSweeper(
            _settings(tmp_path, dry_run=True, transcript_days=365), database=db
        ).sweep()

        assert 1 in db.rows
        assert result.transcripts == 1

    def test_a_policy_can_extend_transcript_retention(self, tmp_path):
        vip = RetentionPolicy(
            "vip", "VIP", transcript_days=3650, priority=5, match_rules={"extensions": ["1513"]}
        )
        db = _Rows([_row(days_old=900)], policies=[vip])

        RetentionSweeper(
            _settings(tmp_path, dry_run=False, transcript_days=365), database=db
        ).sweep()

        assert 1 in db.rows


@pytest.mark.unit
class TestHoldsOverrideEverything:
    def test_a_held_session_keeps_its_audio(self, tmp_path):
        db = _Rows([_row(days_old=9999)], holds=["s-1"])

        result = RetentionSweeper(
            _settings(tmp_path, dry_run=False, audio_days=90), database=db
        ).sweep()

        assert db.rows[1]["path"] is not None
        assert result.held >= 1

    def test_a_held_session_keeps_its_row(self, tmp_path):
        db = _Rows([_row(days_old=9999)], holds=["s-1"])

        RetentionSweeper(
            _settings(tmp_path, dry_run=False, transcript_days=365), database=db
        ).sweep()

        assert 1 in db.rows

    def test_a_hold_beats_a_short_policy(self, tmp_path):
        """A hold suspends expiry entirely; it is not merely a longer period."""
        brief = RetentionPolicy("brief", "Brief", audio_days=1, transcript_days=1, priority=1)
        db = _Rows([_row(days_old=9999)], policies=[brief], holds=["s-1"])

        RetentionSweeper(_settings(tmp_path, dry_run=False), database=db).sweep()

        assert 1 in db.rows


@pytest.mark.unit
class TestUnregisteredFiles:
    """
    A file no row points at would otherwise sit outside every policy and every clock.

    A safety net rather than the main path: registration can fail on a database blip at the
    end of a call, and the file that results is invisible to everything else here.
    """

    def test_an_unregistered_file_is_expired(self, tmp_path):
        orphan = _aged(tmp_path / "recordings" / "stray.wav", days=200)
        db = _Rows([])

        result = RetentionSweeper(
            _settings(tmp_path, dry_run=False, audio_days=90), database=db
        ).sweep()

        assert not orphan.exists()
        assert result.audio_files == 1

    def test_a_registered_file_is_not_treated_as_an_orphan(self, tmp_path):
        known = _aged(tmp_path / "recordings" / "known.wav", days=5)
        db = _Rows([_row(days_old=5, path=str(known))])

        RetentionSweeper(_settings(tmp_path, dry_run=False, audio_days=90), database=db).sweep()

        assert known.exists()

    def test_greetings_are_never_expired(self, tmp_path):
        """A greeting is configuration a user recorded, not a message that arrived."""
        greeting = _aged(tmp_path / "voicemail" / "1001" / "greeting.wav", days=9999)

        RetentionSweeper(_settings(tmp_path, dry_run=False), database=_Rows([])).sweep()

        assert greeting.exists()

    def test_transcription_scratch_is_skipped(self, tmp_path):
        """Deleting these would remove the input of a job in progress."""
        working = _aged(tmp_path / "recordings" / ".transcribe" / "job-1" / "1001-0.wav", days=200)

        RetentionSweeper(_settings(tmp_path, dry_run=False), database=_Rows([])).sweep()

        assert working.exists()

    def test_non_wav_files_are_left_alone(self, tmp_path):
        note = _aged(tmp_path / "recordings" / "notes.txt", days=9999)

        RetentionSweeper(_settings(tmp_path, dry_run=False), database=_Rows([])).sweep()

        assert note.exists()


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


def _recording(
    root: Path, name: str, *, session: str, labels: list[str], seconds: float, days: float
) -> Path:
    """A recording plus the sidecar manifest the recorder writes beside it."""
    import json
    import os

    wav = _aged(root / f"{name}.wav", days=days)
    sidecar = wav.with_suffix(".json")
    sidecar.write_text(
        json.dumps(
            {
                "session_id": session,
                "duration_seconds": seconds,
                "channels": [{"channel": i, "label": name} for i, name in enumerate(labels)],
            }
        )
    )
    old = (datetime.now(UTC) - timedelta(days=days)).timestamp()
    os.utime(sidecar, (old, old))
    return wav


@pytest.mark.unit
class TestMatchRuleValidation:
    """
    Unknown keys are rejected rather than ignored.

    This is the one validation rule here that is a safety property rather than a nicety:
    dropping an unrecognised condition makes a policy match *more* files, and the thing on the
    other end of a match is deletion. A typo would silently widen a narrow policy into a
    catch-all governing every recording on the system.
    """

    def test_known_keys_pass(self):
        assert validate_match_rules({"media": "recording", "extensions": ["1500"]}) == []

    def test_empty_rules_pass(self):
        """No conditions is legitimate -- that is how a catch-all is expressed."""
        assert validate_match_rules({}) == []

    def test_misspelled_key_is_an_error(self):
        problems = validate_match_rules({"medai": "recording"})
        assert len(problems) == 1
        assert "medai" in problems[0]

    def test_unknown_media_value_rejected(self):
        assert validate_match_rules({"media": "video"})

    def test_inverted_duration_bounds_rejected(self):
        assert validate_match_rules({"min_duration_seconds": 60, "max_duration_seconds": 10})

    def test_non_object_rejected(self):
        assert validate_match_rules(["media"])


@pytest.mark.unit
class TestPolicyResolution:
    def _store(self, *policies) -> PolicyStore:
        store = PolicyStore()
        for policy in policies:
            store.save(policy)
        return store

    def test_lowest_priority_number_wins(self, tmp_path):
        store = self._store(
            RetentionPolicy("catch", "Catch all", audio_days=90, priority=1000),
            RetentionPolicy(
                "vip",
                "VIP",
                audio_days=400,
                priority=5,
                match_rules={"extensions": ["1513"]},
            ),
        )
        facts = _facts(
            participants=(
                "1512",
                "1513",
            ),
            duration_seconds=10.0,
        )

        assert store.resolve(facts).policy_id == "vip"

    def test_unmatched_file_falls_through_to_catch_all(self, tmp_path):
        store = self._store(
            RetentionPolicy("catch", "Catch all", audio_days=90, priority=1000),
            RetentionPolicy(
                "vip",
                "VIP",
                audio_days=400,
                priority=5,
                match_rules={"extensions": ["1513"]},
            ),
        )
        facts = _facts(participants=("1400",), duration_seconds=10.0)

        assert store.resolve(facts).policy_id == "catch"

    def test_no_policy_at_all_resolves_to_none(self, tmp_path):
        """None means 'use the configured fallback', which is what an empty table must do."""
        facts = _facts(participants=("1400",), duration_seconds=10.0)

        assert PolicyStore().resolve(facts) is None

    def test_disabled_policies_are_skipped(self, tmp_path):
        store = self._store(
            RetentionPolicy(
                "off",
                "Disabled",
                audio_days=400,
                priority=1,
                enabled=False,
                match_rules={"extensions": ["1513"]},
            ),
        )
        facts = _facts(participants=("1513",), duration_seconds=10.0)

        assert store.resolve(facts) is None

    def test_duration_rules_match(self, tmp_path):
        store = self._store(
            RetentionPolicy(
                "short",
                "Short calls",
                audio_days=7,
                priority=1,
                match_rules={"max_duration_seconds": 5},
            ),
        )
        brief = _facts(duration_seconds=3.0)
        long = _facts(duration_seconds=300.0)

        assert store.resolve(brief).policy_id == "short"
        assert store.resolve(long) is None

    def test_invalid_rules_are_refused_at_save(self):
        store = PolicyStore()

        assert store.save(RetentionPolicy("bad", "Bad", match_rules={"nope": 1})) is False
        assert store.get("bad") is None
