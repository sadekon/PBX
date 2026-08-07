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

from pbx.features.retention import SKIP_DIR_NAMES, RetentionSettings, RetentionSweeper
from pbx.features.retention_policies import (
    MEDIA_RECORDING,
    MEDIA_VOICEMAIL,
    HoldStore,
    PolicyStore,
    RetentionPolicy,
    facts_for,
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

    def test_transcription_scratch_is_skipped(self, tmp_path):
        """
        Transcription cuts a recording into per-participant regions under
        recordings/.transcribe/. Those are .wav and live under a swept root, so without an
        exclusion the sweep would delete the input of a job in progress and count its bytes
        as if they were recordings.
        """
        working = _aged(tmp_path / "recordings" / ".transcribe" / "job-1" / "1001-0.wav", days=200)

        result = RetentionSweeper(_settings(tmp_path, dry_run=False)).sweep()

        assert working.exists()
        assert result.audio_files == 0

    def test_a_real_recording_beside_the_scratch_is_still_swept(self, tmp_path):
        _aged(tmp_path / "recordings" / ".transcribe" / "job-1" / "region.wav", days=200)
        real = _aged(tmp_path / "recordings" / "call.wav", days=200)

        result = RetentionSweeper(_settings(tmp_path, dry_run=False)).sweep()

        assert not real.exists()
        assert result.audio_files == 1

    def test_a_manifest_goes_with_its_recording(self, tmp_path):
        """
        The sidecar names every participant on every channel. Leaving it once the audio is
        gone keeps a record of who spoke to whom indefinitely, on a system whose whole
        retention design is that things provably expire.
        """
        audio = _aged(tmp_path / "recordings" / "call.wav", days=200)
        sidecar = _aged(tmp_path / "recordings" / "call.json", days=200)

        result = RetentionSweeper(_settings(tmp_path, dry_run=False)).sweep()

        assert not audio.exists()
        assert not sidecar.exists()
        assert result.audio_files == 1, "the manifest must not inflate the recording count"
        assert result.sidecars == 1

    def test_a_manifest_survives_while_its_recording_does(self, tmp_path):
        audio = _aged(tmp_path / "recordings" / "call.wav", days=3)
        sidecar = _aged(tmp_path / "recordings" / "call.json", days=3)

        RetentionSweeper(_settings(tmp_path, dry_run=False)).sweep()

        assert audio.exists()
        assert sidecar.exists()

    def test_an_orphaned_manifest_expires_on_its_own(self, tmp_path):
        """Catches the ones whose audio was removed before this existed, or by hand."""
        sidecar = _aged(tmp_path / "recordings" / "gone.json", days=200)

        result = RetentionSweeper(_settings(tmp_path, dry_run=False)).sweep()

        assert not sidecar.exists()
        assert result.sidecars == 1

    def test_a_recent_orphan_is_left_alone(self, tmp_path):
        sidecar = _aged(tmp_path / "recordings" / "fresh.json", days=3)

        RetentionSweeper(_settings(tmp_path, dry_run=False)).sweep()

        assert sidecar.exists()

    def test_dry_run_does_not_delete_manifests(self, tmp_path):
        audio = _aged(tmp_path / "recordings" / "call.wav", days=200)
        sidecar = _aged(tmp_path / "recordings" / "call.json", days=200)

        result = RetentionSweeper(_settings(tmp_path)).sweep()

        assert audio.exists()
        assert sidecar.exists()
        assert result.sidecars == 1
        assert "manifest" in result.summary()

    def test_manifests_in_the_scratch_directory_are_skipped(self, tmp_path):
        working = _aged(tmp_path / "recordings" / ".transcribe" / "job-1" / "x.json", days=200)

        RetentionSweeper(_settings(tmp_path, dry_run=False)).sweep()

        assert working.exists()

    def test_the_skip_name_matches_the_transcriber(self):
        """
        Two modules agreeing on a string by convention. If either changes, the sweep starts
        deleting working files -- or stops skipping a directory that no longer exists.
        """
        from pbx.speech.recording import SCRATCH_DIRNAME

        assert SCRATCH_DIRNAME in SKIP_DIR_NAMES

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

        execute() returns a bool for *every* statement, so a SELECT through it yields True and
        then fails on subscripting. That reached the server as
        "transcript sweep failed: 'bool' object is not subscriptable"; a MagicMock returning
        rows from execute() had happily agreed with the wrong code.

        The sweep now fetches candidate rows rather than a COUNT, because each row's period
        depends on the policy that matches it -- a single cutoff cannot express that.

        A sweep now issues three different reads -- policies, holds, transcripts -- so the
        fake has to dispatch on the statement. A single return_value would hand transcript
        rows back to the hold query, which reads every row's session_id as an active legal
        hold and quietly protects everything from deletion.
        """
        rows = [
            {
                "id": i,
                "session_id": f"s-{i}",
                "source": "recording",
                "participants": None,
                "audio_duration": 30.0,
                "created_at": datetime.now(UTC) - timedelta(days=900),
            }
            for i in range(expired)
        ]

        def fetch_all(query, params=None):
            if "call_transcripts" in query:
                return rows
            return []

        db = MagicMock()
        db.enabled = True
        db.execute.return_value = True
        db.fetch_all.side_effect = fetch_all
        db.fetch_one.return_value = None
        return db

    def test_transcripts_are_counted_but_not_deleted_in_dry_run(self, tmp_path):
        db = self._db(expired=7)

        result = RetentionSweeper(_settings(tmp_path), database=db).sweep()

        assert result.transcripts == 7
        db.execute.assert_not_called()

    def test_reading_uses_fetch_not_execute(self, tmp_path):
        """The regression itself: a SELECT through execute() comes back as True."""
        db = self._db(expired=2)

        RetentionSweeper(_settings(tmp_path), database=db).sweep()

        queries = [str(c[0][0]) for c in db.fetch_all.call_args_list]
        assert any("FROM call_transcripts" in q for q in queries)
        db.execute.assert_not_called()

    def test_transcripts_are_deleted_when_not_dry_run(self, tmp_path):
        db = self._db(expired=3)

        RetentionSweeper(_settings(tmp_path, dry_run=False), database=db).sweep()

        statements = " ".join(str(c[0][0]) for c in db.execute.call_args_list)
        assert "DELETE FROM call_transcripts" in statements

    def test_transcripts_are_deleted_by_id(self, tmp_path):
        """
        By id, not by a shared cutoff.

        Two rows of the same age can be governed by different policies, so the set that
        expires is decided per row and the delete has to name exactly that set.
        """
        db = self._db(expired=3)

        RetentionSweeper(_settings(tmp_path, dry_run=False), database=db).sweep()

        delete = next(
            c for c in db.execute.call_args_list if "DELETE FROM call_transcripts" in str(c[0][0])
        )
        assert sorted(delete[0][1]) == [0, 1, 2]

    def test_nothing_is_deleted_when_nothing_expired(self, tmp_path):
        db = self._db(expired=0)

        RetentionSweeper(_settings(tmp_path, dry_run=False), database=db).sweep()

        db.execute.assert_not_called()

    def test_a_database_failure_does_not_raise(self, tmp_path):
        """This runs on a background thread; an exception here would kill the sweep for good."""
        db = self._db()
        db.fetch_all.side_effect = RuntimeError("connection reset")

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
class TestFacts:
    def test_recording_facts_come_from_the_sidecar(self, tmp_path):
        wav = _recording(
            tmp_path, "call", session="s-1", labels=["1512", "1513"], seconds=42.0, days=1
        )

        facts = facts_for(wav, MEDIA_RECORDING)

        assert facts.session_id == "s-1"
        assert set(facts.participants) == {"1512", "1513"}
        assert facts.duration_seconds == 42.0

    def test_missing_sidecar_yields_empty_facts(self, tmp_path):
        """An unreadable manifest must not abort the sweep; it just cannot match rules."""
        wav = _aged(tmp_path / "orphan.wav", days=1)

        facts = facts_for(wav, MEDIA_RECORDING)

        assert facts.participants == ()
        assert facts.session_id == ""

    def test_corrupt_sidecar_yields_empty_facts(self, tmp_path):
        wav = _aged(tmp_path / "bad.wav", days=1)
        wav.with_suffix(".json").write_text("{not json")

        assert facts_for(wav, MEDIA_RECORDING).participants == ()

    def test_voicemail_facts_come_from_the_path(self, tmp_path):
        """voicemail/<mailbox>/<caller>_<timestamp>.wav -- no manifest exists here."""
        vm = _aged(tmp_path / "voicemail" / "1500" / "5551234_20260101_101010.wav", days=1)

        facts = facts_for(vm, MEDIA_VOICEMAIL)

        assert set(facts.participants) == {"1500", "5551234"}


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
        wav = _recording(tmp_path, "c", session="s", labels=["1512", "1513"], seconds=10.0, days=1)

        assert store.resolve(facts_for(wav, MEDIA_RECORDING)).policy_id == "vip"

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
        wav = _recording(tmp_path, "c", session="s", labels=["1400"], seconds=10.0, days=1)

        assert store.resolve(facts_for(wav, MEDIA_RECORDING)).policy_id == "catch"

    def test_no_policy_at_all_resolves_to_none(self, tmp_path):
        """None means 'use the configured fallback', which is what an empty table must do."""
        wav = _recording(tmp_path, "c", session="s", labels=["1400"], seconds=10.0, days=1)

        assert PolicyStore().resolve(facts_for(wav, MEDIA_RECORDING)) is None

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
        wav = _recording(tmp_path, "c", session="s", labels=["1513"], seconds=10.0, days=1)

        assert store.resolve(facts_for(wav, MEDIA_RECORDING)) is None

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
        brief = _recording(tmp_path, "brief", session="s", labels=["1400"], seconds=3.0, days=1)
        long = _recording(tmp_path, "long", session="s", labels=["1400"], seconds=300.0, days=1)

        assert store.resolve(facts_for(brief, MEDIA_RECORDING)).policy_id == "short"
        assert store.resolve(facts_for(long, MEDIA_RECORDING)) is None

    def test_invalid_rules_are_refused_at_save(self):
        store = PolicyStore()

        assert store.save(RetentionPolicy("bad", "Bad", match_rules={"nope": 1})) is False
        assert store.get("bad") is None


@pytest.mark.unit
class TestPolicyAwareSweep:
    def test_a_policy_can_extend_retention_past_the_fallback(self, tmp_path):
        """The whole point of policies: the same directory, two different periods."""
        rec_root = tmp_path / "recordings"
        normal = _recording(
            rec_root, "normal", session="s1", labels=["1400"], seconds=30.0, days=120
        )
        vip = _recording(rec_root, "vip", session="s2", labels=["1513"], seconds=30.0, days=120)

        sweeper = RetentionSweeper(_settings(tmp_path, dry_run=False, audio_days=90))
        sweeper.policies.save(
            RetentionPolicy(
                "vip",
                "VIP",
                audio_days=3650,
                priority=5,
                match_rules={"extensions": ["1513"]},
            )
        )
        result = sweeper.sweep()

        assert not normal.exists()
        assert vip.exists()
        assert result.audio_files == 1

    def test_a_policy_can_shorten_retention(self, tmp_path):
        rec_root = tmp_path / "recordings"
        brief = _recording(rec_root, "brief", session="s1", labels=["1400"], seconds=2.0, days=30)

        sweeper = RetentionSweeper(_settings(tmp_path, dry_run=False, audio_days=90))
        sweeper.policies.save(
            RetentionPolicy(
                "misdials",
                "Misdials",
                audio_days=7,
                priority=5,
                match_rules={"max_duration_seconds": 5},
            )
        )
        sweeper.sweep()

        assert not brief.exists()

    def test_media_rule_separates_voicemail_from_recordings(self, tmp_path):
        vm = _aged(tmp_path / "voicemail" / "1500" / "msg_20260101_101010.wav", days=45)
        rec = _recording(
            tmp_path / "recordings", "call", session="s", labels=["1400"], seconds=30.0, days=45
        )

        sweeper = RetentionSweeper(_settings(tmp_path, dry_run=False, audio_days=90))
        sweeper.policies.save(
            RetentionPolicy(
                "vm",
                "Voicemail",
                audio_days=30,
                priority=5,
                match_rules={"media": "voicemail"},
            )
        )
        sweeper.sweep()

        assert not vm.exists(), "voicemail policy at 30 days should have expired it"
        assert rec.exists(), "the recording is still inside the 90-day fallback"

    def test_lifetime_counters_ignore_dry_runs(self, tmp_path):
        """
        A "Deleted (All Time)" figure that counts files still sitting on disk is worse than
        no figure, and the previous admin page showed exactly that kind of number.
        """
        _recording(
            tmp_path / "recordings", "old", session="s", labels=["1400"], seconds=30.0, days=120
        )

        sweeper = RetentionSweeper(_settings(tmp_path, dry_run=True, audio_days=90))
        sweeper.sweep()

        assert sweeper.last_result.audio_files == 1
        assert sweeper.lifetime_audio_deleted == 0

    def test_statistics_report_dry_run_and_enabled(self, tmp_path):
        sweeper = RetentionSweeper(_settings(tmp_path, dry_run=True))

        stats = sweeper.statistics()

        assert stats["dry_run"] is True
        assert stats["enabled"] is True
        assert stats["last_sweep"] is None


@pytest.mark.unit
class TestLegalHolds:
    def test_a_held_session_is_never_swept(self, tmp_path):
        old = _recording(
            tmp_path / "recordings",
            "old",
            session="case-1",
            labels=["1400"],
            seconds=30.0,
            days=9999,
        )

        sweeper = RetentionSweeper(_settings(tmp_path, dry_run=False, audio_days=90))
        sweeper.holds.place("case-1", "Case 2026-14", "admin")
        result = sweeper.sweep()

        assert old.exists()
        assert result.held == 1
        assert result.audio_files == 0

    def test_releasing_a_hold_lets_the_file_expire(self, tmp_path):
        old = _recording(
            tmp_path / "recordings",
            "old",
            session="case-1",
            labels=["1400"],
            seconds=30.0,
            days=9999,
        )
        sweeper = RetentionSweeper(_settings(tmp_path, dry_run=False, audio_days=90))
        sweeper.holds.place("case-1", "Case 2026-14", "admin")
        sweeper.sweep()

        sweeper.holds.release("case-1", "admin")
        sweeper.sweep()

        assert not old.exists()

    def test_a_hold_beats_a_short_policy(self, tmp_path):
        """A hold suspends expiry entirely; it is not merely a longer period."""
        old = _recording(
            tmp_path / "recordings",
            "brief",
            session="case-1",
            labels=["1400"],
            seconds=2.0,
            days=9999,
        )
        sweeper = RetentionSweeper(_settings(tmp_path, dry_run=False, audio_days=90))
        sweeper.policies.save(
            RetentionPolicy(
                "misdials",
                "Misdials",
                audio_days=1,
                priority=5,
                match_rules={"max_duration_seconds": 5},
            )
        )
        sweeper.holds.place("case-1", "Case 2026-14", "admin")
        sweeper.sweep()

        assert old.exists()

    def test_a_hold_needs_a_reason_and_an_actor(self):
        holds = HoldStore()

        assert holds.place("s-1", "", "admin") is False
        assert holds.place("s-1", "reason", "") is False
        assert holds.held("s-1") is False

    def test_a_failed_refresh_keeps_the_previous_holds(self):
        """
        Clearing the set on a read error would mean a transient database fault silently lifts
        every legal hold moments before the sweep deletes what they were protecting.
        """
        db = MagicMock()
        db.enabled = True
        db.fetch_all.side_effect = RuntimeError("connection lost")

        holds = HoldStore(db)
        holds._active = {"case-1"}
        holds.refresh()

        assert holds.held("case-1")
