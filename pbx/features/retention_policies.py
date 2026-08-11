"""
What expires, when, and what must not expire at all.

This is the *policy* half of retention; :mod:`pbx.features.retention` is the executor that
acts on it. The split is deliberate and is the one thing the previous arrangement got wrong:
there were two retention systems, one that owned policies and never deleted anything
(``recording_retention.RecordingRetentionManager``) and one that deleted on a timer and knew
no policies (``retention.RetentionSweeper``). An operator could add a policy in the admin UI,
watch it save, and have it govern nothing -- while real deletion followed a config file that
same UI never showed. This module is the policy store both halves now share.

Three concepts, kept apart on purpose:

**Policies** are standing rules about a *class* of calls: "voicemail for the support queue
keeps 30 days". They are configuration, set in advance, and they set a period.

**Holds** apply to *one specific call*, are placed by a person after something happened, and
suspend expiry entirely until released. They replace the old tag vocabulary, which mapped
``legal`` to a fixed 2555 days -- wrong in both directions, since a dispute lasting longer
still lost the audio and one settled in a month held the recording for another seven years
with no way to let it go.

**Facts** are what we know about a recording at sweep time, read straight off its row. They
used to be parsed from a ``.json`` sidecar and from the voicemail path layout, which meant a
manifest that expired with its audio took a policy's ability to match with it.

Two clocks throughout. Audio is where the bytes and the privacy weight are; transcripts are
~2% of the size and carry most of the value, so they outlive the audio they came from.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

from pbx.utils.logger import get_logger

__all__ = [
    "MATCH_KEYS",
    "HoldStore",
    "PolicyStore",
    "RecordingFacts",
    "RetentionPolicy",
]

#: The complete match vocabulary. Closed on purpose: an open expression language would mean
#: evaluating operator-supplied strings on a live PBX, and could not be rendered as a form.
MATCH_KEYS = frozenset({"media", "extensions", "min_duration_seconds", "max_duration_seconds"})

#: Where a file was found. Not a guess -- it is which tree the sweeper walked.
MEDIA_RECORDING = "recording"
MEDIA_VOICEMAIL = "voicemail"

#: The seeded catch-all sits far down the order so anything an operator adds later outranks it
#: without them having to reason about priority at all.
SEED_PRIORITY = 1000
SEED_POLICY_ID = "default"


# --------------------------------------------------------------------------- facts


@dataclass(frozen=True, slots=True)
class RecordingFacts:
    """
    What is known about one file when the sweeper reaches it.

    Everything here is read from disk -- the sidecar manifest for a recording, the path layout
    for a voicemail. Nothing requires a database lookup, because a sweep walks thousands of
    files and must not issue a query per file.
    """

    path: Path
    media: str
    session_id: str = ""
    participants: tuple[str, ...] = ()
    duration_seconds: float | None = None


# --------------------------------------------------------------------------- policy


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """
    One standing rule. Periods of ``None`` inherit the configured fallback.

    Inheriting rather than defaulting to zero matters: a policy that only lengthens audio
    retention leaves ``transcript_days`` unset, and that must not be read as "delete the
    transcript immediately".
    """

    policy_id: str
    name: str
    description: str = ""
    audio_days: int | None = None
    transcript_days: int | None = None
    priority: int = 100
    match_rules: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    origin: str = "api"

    @property
    def is_catch_all(self) -> bool:
        return not self.match_rules

    def matches(self, facts: RecordingFacts) -> bool:
        """True when every stated condition holds. No conditions means every file."""
        rules = self.match_rules

        media = rules.get("media")
        if media is not None:
            wanted = {media} if isinstance(media, str) else set(media)
            if facts.media not in wanted:
                return False

        extensions = rules.get("extensions")
        if extensions is not None:
            wanted = {str(e) for e in extensions}
            if not wanted.intersection(facts.participants):
                return False

        # A duration rule cannot match a file whose duration is unknown. Treating an absent
        # duration as passing would let a rule meant for short calls swallow every voicemail,
        # none of which carry a duration.
        minimum = rules.get("min_duration_seconds")
        if minimum is not None and (
            facts.duration_seconds is None or facts.duration_seconds < float(minimum)
        ):
            return False

        maximum = rules.get("max_duration_seconds")
        if maximum is None:
            return True
        return facts.duration_seconds is not None and facts.duration_seconds <= float(maximum)


def validate_match_rules(rules: Any) -> list[str]:
    """
    Every problem with a rule set. Empty means usable.

    Unknown keys are an **error, not a warning to skip past**. Dropping a condition makes a
    policy match *more* files, and this subsystem deletes them -- so a typo'd key that got
    silently ignored would quietly widen a narrow policy into a catch-all.
    """
    problems: list[str] = []
    if not isinstance(rules, dict):
        return ["match_rules must be a JSON object"]

    unknown = set(rules) - MATCH_KEYS
    if unknown:
        problems.append(f"unknown match key(s) {sorted(unknown)}; supported: {sorted(MATCH_KEYS)}")

    media = rules.get("media")
    if media is not None:
        values = {media} if isinstance(media, str) else set(media or ())
        bad = values - {MEDIA_RECORDING, MEDIA_VOICEMAIL}
        if bad:
            problems.append(f"media must be 'recording' or 'voicemail', got {sorted(bad)}")

    extensions = rules.get("extensions")
    if extensions is not None and (
        isinstance(extensions, str) or not isinstance(extensions, list | tuple)
    ):
        problems.append("extensions must be a list")

    for key in ("min_duration_seconds", "max_duration_seconds"):
        value = rules.get(key)
        if value is None:
            continue
        try:
            if float(value) < 0:
                problems.append(f"{key} must not be negative")
        except (TypeError, ValueError):
            problems.append(f"{key} must be a number")

    low = rules.get("min_duration_seconds")
    high = rules.get("max_duration_seconds")
    if low is not None and high is not None:
        try:
            if float(low) > float(high):
                problems.append("min_duration_seconds is greater than max_duration_seconds")
        except (TypeError, ValueError):
            pass

    return problems


# --------------------------------------------------------------------------- stores


class PolicyStore:
    """
    Persisted retention policies, cached in memory for the sweep.

    Backed by the ``retention_policies`` table. Policies were previously a plain dict, so the
    admin UI wrote them to memory and lost them on restart; persisting them is most of why
    this module exists.

    Works without a database -- the cache is authoritative in that case -- so tests and
    database-less installs get a real object rather than something to guard with ``hasattr``.
    """

    def __init__(self, database: Any | None = None, logger: Any | None = None) -> None:
        self.database = database
        self.logger = logger or get_logger()
        self._policies: dict[str, RetentionPolicy] = {}

    # -- lifecycle

    @property
    def persistent(self) -> bool:
        return bool(self.database and getattr(self.database, "enabled", False))

    def load(self) -> None:
        """Refresh the cache from the table. A read failure keeps whatever we already had."""
        if not self.persistent:
            return

        try:
            rows = self.database.fetch_all(
                "SELECT * FROM retention_policies ORDER BY priority ASC, policy_id ASC"
            )
        except Exception as e:
            self.logger.error(f"Could not load retention policies: {e}")
            return

        loaded: dict[str, RetentionPolicy] = {}
        for row in rows or []:
            policy = self._from_row(row)
            if policy is not None:
                loaded[policy.policy_id] = policy
        self._policies = loaded

    def seed(self, audio_days: int, transcript_days: int) -> bool:
        """
        Write the starter catch-all, but only into an empty table.

        Seeding once and then leaving the table alone is what makes the admin UI authoritative:
        an operator who edits the seeded policy keeps that edit across restarts. Re-syncing from
        config on every boot would silently overwrite their work, which is the same
        "set it and it does nothing" trap in a new costume.
        """
        if not self.persistent or self._policies:
            return False

        policy = RetentionPolicy(
            policy_id=SEED_POLICY_ID,
            name="Default retention",
            description=(
                "Applies to everything no other policy matches. Seeded from the retention: "
                "block in config.yml on first start; edit it here rather than in the file."
            ),
            audio_days=audio_days,
            transcript_days=transcript_days,
            priority=SEED_PRIORITY,
            match_rules={},
            origin="config",
        )
        if not self.save(policy):
            return False

        self.logger.info(
            f"Seeded retention policy '{policy.policy_id}' "
            f"(audio {audio_days}d, transcripts {transcript_days}d) from config"
        )
        return True

    # -- reads

    def all(self) -> list[RetentionPolicy]:
        """Every policy, in resolution order."""
        return sorted(self._policies.values(), key=lambda p: (p.priority, p.policy_id))

    def get(self, policy_id: str) -> RetentionPolicy | None:
        return self._policies.get(policy_id)

    def resolve(self, facts: RecordingFacts) -> RetentionPolicy | None:
        """
        The policy governing `facts`, or None to fall back to the configured periods.

        First match in priority order wins; ties break on policy_id so the outcome is stable
        rather than dependent on dict ordering. Disabled policies are skipped entirely.
        """
        for policy in self.all():
            if policy.enabled and policy.matches(facts):
                return policy
        return None

    # -- writes

    def save(self, policy: RetentionPolicy) -> bool:
        """Insert or update. Updates the cache only when the write actually landed."""
        problems = validate_match_rules(policy.match_rules)
        if problems:
            for problem in problems:
                self.logger.error(f"Retention policy '{policy.policy_id}': {problem}")
            return False

        if self.persistent:
            try:
                ok = self.database.execute(
                    """
                    INSERT INTO retention_policies (
                        policy_id, name, description, audio_days, transcript_days,
                        priority, match_rules, enabled, origin, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (policy_id) DO UPDATE SET
                        name = EXCLUDED.name,
                        description = EXCLUDED.description,
                        audio_days = EXCLUDED.audio_days,
                        transcript_days = EXCLUDED.transcript_days,
                        priority = EXCLUDED.priority,
                        match_rules = EXCLUDED.match_rules,
                        enabled = EXCLUDED.enabled,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        policy.policy_id,
                        policy.name,
                        policy.description,
                        policy.audio_days,
                        policy.transcript_days,
                        policy.priority,
                        json.dumps(policy.match_rules),
                        policy.enabled,
                        policy.origin,
                    ),
                )
            except Exception as e:
                self.logger.error(f"Could not save retention policy '{policy.policy_id}': {e}")
                return False
            if not ok:
                return False

        self._policies[policy.policy_id] = policy
        return True

    def delete(self, policy_id: str) -> bool:
        """Remove a policy. Returns False when it was not there to begin with."""
        if policy_id not in self._policies:
            return False

        if self.persistent:
            try:
                if not self.database.execute(
                    "DELETE FROM retention_policies WHERE policy_id = %s", (policy_id,)
                ):
                    return False
            except Exception as e:
                self.logger.error(f"Could not delete retention policy '{policy_id}': {e}")
                return False

        del self._policies[policy_id]
        return True

    # -- internals

    def _from_row(self, row: dict) -> RetentionPolicy | None:
        """
        Build a policy from a database row, or None if the row is unusable.

        A row whose match_rules will not parse is dropped rather than treated as ``{}``:
        an empty rule set is a catch-all, so falling back to it would turn a corrupted narrow
        policy into one that governs every recording on the system.
        """
        policy_id = str(row.get("policy_id") or "")
        if not policy_id:
            return None

        raw = row.get("match_rules") or "{}"
        try:
            rules = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (TypeError, ValueError) as e:
            self.logger.error(
                f"Retention policy '{policy_id}' has unreadable match_rules ({e}); ignoring it"
            )
            return None

        problems = validate_match_rules(rules)
        if problems:
            for problem in problems:
                self.logger.error(f"Retention policy '{policy_id}': {problem}; ignoring it")
            return None

        def as_days(key: str) -> int | None:
            value = row.get(key)
            if value is None:
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        return RetentionPolicy(
            policy_id=policy_id,
            name=str(row.get("name") or policy_id),
            description=str(row.get("description") or ""),
            audio_days=as_days("audio_days"),
            transcript_days=as_days("transcript_days"),
            priority=int(row.get("priority") or 100),
            match_rules=rules,
            enabled=bool(row.get("enabled", True)),
            origin=str(row.get("origin") or "api"),
        )


class HoldStore:
    """
    Legal holds: calls that must not expire until somebody says so.

    Keyed on ``session_id`` rather than ``call_id`` because a session spans transfers and
    re-INVITEs -- holding a conversation has to hold every leg of it, and recordings are named
    by session.

    A hold is checked on every sweep for every file, so the active set is cached in memory and
    refreshed once per sweep rather than queried per file.
    """

    def __init__(self, database: Any | None = None, logger: Any | None = None) -> None:
        self.database = database
        self.logger = logger or get_logger()
        self._active: set[str] = set()

    @property
    def persistent(self) -> bool:
        return bool(self.database and getattr(self.database, "enabled", False))

    def refresh(self) -> None:
        """
        Reload the active hold set.

        On a read failure the previous set is kept rather than cleared. Clearing would mean a
        transient database error silently lifts every legal hold on the system moments before
        the sweep deletes the files they were protecting.
        """
        if not self.persistent:
            return

        try:
            rows = self.database.fetch_all(
                "SELECT session_id FROM retention_holds WHERE released_at IS NULL"
            )
        except Exception as e:
            self.logger.error(f"Could not load retention holds ({e}); keeping the previous set")
            return

        self._active = {str(r["session_id"]) for r in rows or [] if r.get("session_id")}

    def held(self, session_id: str) -> bool:
        return bool(session_id) and session_id in self._active

    @property
    def active(self) -> set[str]:
        return set(self._active)

    def place(self, session_id: str, reason: str, placed_by: str) -> bool:
        """Put a session beyond the reach of the sweeper until it is released."""
        if not (session_id and reason and placed_by):
            return False

        if self.persistent:
            try:
                if not self.database.execute(
                    """
                    INSERT INTO retention_holds (session_id, reason, placed_by)
                    VALUES (%s, %s, %s)
                    """,
                    (session_id, reason, placed_by),
                ):
                    return False
            except Exception as e:
                self.logger.error(f"Could not place hold on session {session_id}: {e}")
                return False

        self._active.add(session_id)
        self.logger.info(f"Retention hold placed on session {session_id} by {placed_by}: {reason}")
        return True

    def release(self, session_id: str, released_by: str) -> bool:
        """
        Lift every active hold on a session.

        The rows are stamped, never deleted: the audit value of a hold is the record that it
        existed and who lifted it.
        """
        if not session_id:
            return False

        if self.persistent:
            try:
                if not self.database.execute(
                    """
                    UPDATE retention_holds
                       SET released_at = CURRENT_TIMESTAMP, released_by = %s
                     WHERE session_id = %s AND released_at IS NULL
                    """,
                    (released_by, session_id),
                ):
                    return False
            except Exception as e:
                self.logger.error(f"Could not release hold on session {session_id}: {e}")
                return False

        self._active.discard(session_id)
        self.logger.info(f"Retention hold on session {session_id} released by {released_by}")
        return True

    def list_holds(self, include_released: bool = False) -> list[dict]:
        """Every hold, newest first. Reads through to the database rather than the cache."""
        if not self.persistent:
            return [{"session_id": s, "released_at": None} for s in sorted(self._active)]

        where = "" if include_released else " WHERE released_at IS NULL"
        try:
            return (
                self.database.fetch_all(
                    f"SELECT * FROM retention_holds{where} ORDER BY placed_at DESC"
                )
                or []
            )
        except Exception as e:
            self.logger.error(f"Could not list retention holds: {e}")
            return []
