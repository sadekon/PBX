"""
Find Me/Follow Me Call Routing
Ring multiple devices sequentially or simultaneously

Two halves, both owned here so the feature is self-contained:

*Configuration* -- which destinations an extension has, in what order, with what
ring times, and where to send the call when none of them answer. Stored in
``fmfm_configs``, edited through ``/api/fmfm/*``, and turned into a ring plan by
``get_ring_strategy()``.

*Execution* -- the interpreter for that plan, from ``plan_for()`` down. It keeps
the position in the destination list, arms one timer per destination, and moves
on for every way a destination can fail to take the call: ring timeout, INVITE
transaction timeout, 486 Busy, 603 Decline, or a destination that cannot be
dialled at all. When the list runs out the caller goes to the *original*
extension's mailbox, not the last destination tried, because every FMFM
destination is the same person.

Where the ring state lives
--------------------------
Here, keyed by Call-ID, for the duration of the ringing phase. A ringing plan is
this feature's business and nothing else needs to see it, so it is not on
``Call``. CallRouter and SIPServer hand every candidate event over and let the
lookup here decide whether there is a plan to apply. Entries are pruned when a
new plan starts, so a call torn down mid-ring (the caller hangs up) leaves
nothing behind.

Layering
--------
``CallRouter`` owns the leg mechanics this drives: ``_dial_extension_leg()`` for
an internal destination, ``_dial_trunk_leg()`` for an external one. Both dial on
a ``Call`` whose relay is already allocated, which is what lets FMFM re-target a
live call without disturbing the caller's side. ``SIPServer`` owns CANCEL.
Nothing here formats SIP.

The dialled extension's own phone rings first, for
``features.find_me_follow_me.initial_ring_time`` seconds, before the configured
destinations -- the way FreePBX Follow-Me's "Initial Ring Time" behaves. Nobody
lists their own desk in their follow-me list, they expect the call to reach them
there before it goes chasing, so a config of just "my mobile" reads and behaves
the same way. Set that to 0 to go straight to the list, or place the extension
in the list explicitly to control where and how long it rings.

Ringing the extension is an ordinary leg, not a loop: it is INVITEd straight to
the phone's registered contact and never re-enters ``route_call()``. (The one
real loop, a registration pointing back at the PBX's own address, is caught in
``_dial_extension_leg()``.)

Two guards keep a bad config from trapping a caller: a destination naming the
caller is dropped rather than ringing them back on their own call, and the list
is capped at ``MAX_DESTINATIONS``.

Sequential mode only
--------------------
A simultaneous config is executed *sequentially*, with a warning. Ringing
several destinations at once needs several legs in flight on one call, and
``Call`` holds exactly one callee leg -- the same limitation that leaves the call
queue's ``ring_all`` strategy deferred. Degrading to sequential reaches the same
destinations in the same order and still connects the call, which is a better
answer than the silence the feature produced before it was wired up at all.
"""

import json
import re
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pbx.utils.logger import get_logger

# Top Via branch of a request or response. Multiple Via headers are joined into
# one comma-separated value by SIPMessage.parse(), and the top Via comes first,
# so the first match is the branch of the transaction we care about.
_BRANCH_RE = re.compile(r"branch=(z9hG4bK[^;,\s]+)", re.IGNORECASE)

# Rewrites the user part of a To header, so each leg's To names the destination
# it is actually being sent to.
_TO_USER_RE = re.compile(r"sip:(\*?[^@]+)@")

# A config must not be able to keep a caller ringing indefinitely.
MAX_DESTINATIONS = 10

# Bounds on a per-destination ring time, matching the admin UI's min/max. A
# stored value outside this range is clamped rather than rejected -- the call
# still needs somewhere to go.
MIN_RING_TIME = 5
MAX_RING_TIME = 120
DEFAULT_RING_TIME = 20


def _top_via_branch(via: str | None) -> str | None:
    """Branch parameter of the topmost Via header, or None if there isn't one."""
    if not via:
        return None
    match = _BRANCH_RE.search(via)
    return match.group(1) if match else None


@dataclass
class FMFMState:
    """
    Where a call stands in its extension's Find Me/Follow Me destination list.

    Held by :class:`FindMeFollowMe`, keyed by Call-ID, for the life of the
    ringing phase.
    """

    # The dialled extension, i.e. whose FMFM config this is and whose mailbox
    # the call falls back to. Deliberately distinct from the destination
    # currently ringing.
    extension: str
    destinations: list[dict[str, Any]] = field(default_factory=list)
    # From/To headers of the caller's original INVITE, reused to build each leg.
    from_header: str = ""
    to_header: str = ""
    index: int = 0
    # Bumped every time something claims the right to move the call on. A timer
    # or transaction callback captures the value it was armed with and is ignored
    # once it no longer matches, so a ring timeout and an INVITE timeout racing
    # on the same leg advance exactly once between them.
    generation: int = 0
    # Via branch of the leg currently in flight, used to tell a response for
    # this leg from a late 487 belonging to one already abandoned.
    leg_branch: str | None = None
    # True once ringing has resolved -- answered, or handed to voicemail. No
    # further advancing, and late responses from abandoned legs are swallowed.
    finished: bool = False
    # Set once, so a simultaneous config only warns on the first dial.
    warned_simultaneous: bool = False

    def current(self) -> str:
        """The destination currently being rung."""
        return str(self.destinations[self.index]["destination"])


class FindMeFollowMe:
    """Find Me/Follow Me call routing system"""

    def __init__(
        self,
        config: Any | None = None,
        database: Any | None = None,
        pbx_core: Any | None = None,
    ) -> None:
        """
        Initialize Find Me/Follow Me.

        Args:
            config: PBX configuration.
            database: Database backend for persisted configs, if enabled.
            pbx_core: The PBXCore instance, needed to actually ring anything.
                Without it this is a config store only -- every call still gets
                planned and stored, but `plan_for()` declines to take calls, so
                a partially constructed PBX routes normally instead of failing.
        """
        self.logger = get_logger()
        self.config = config or {}
        self.database = database
        self.pbx_core = pbx_core
        self.enabled = (
            self.config.get("features", {}).get("find_me_follow_me", {}).get("enabled", False)
        )

        # User configurations
        self.user_configs = {}  # extension -> FMFM config

        # Call-ID -> plan, for calls currently ringing through their FMFM list.
        self._plans: dict[str, FMFMState] = {}
        # Guards _plans and every decision to move a call on. Held only long
        # enough to decide, never across dialling -- the same discipline
        # QueueCallHandler uses for its offer claims.
        self._lock = threading.Lock()

        # Initialize database schema if database is available
        if self.database and self.database.enabled:
            self._initialize_schema()

        if self.enabled:
            self.logger.info("Find Me/Follow Me system initialized")
            self._load_configs()

    def _initialize_schema(self) -> None:
        """Initialize database schema for FMFM"""
        if not self.database or not self.database.enabled:
            return

        # FMFM configurations table
        fmfm_table = """
        CREATE TABLE IF NOT EXISTS fmfm_configs (
            extension VARCHAR(20) PRIMARY KEY,
            mode VARCHAR(20) NOT NULL CHECK (mode IN ('sequential', 'simultaneous')),
            enabled BOOLEAN DEFAULT TRUE,
            destinations TEXT NOT NULL,
            no_answer_destination VARCHAR(50),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """

        try:
            cursor = self.database.connection.cursor()
            cursor.execute(fmfm_table)
            self.database.connection.commit()
            cursor.close()
            self.logger.debug("FMFM database schema initialized")
        except Exception as e:
            self.logger.error(f"Error initializing FMFM schema: {e}")

    def _load_configs(self) -> None:
        """Load FMFM configurations from database or config file"""
        # First try to load from database
        if self.database and self.database.enabled:
            self._load_from_database()

        # Also load from config file (for backward compatibility)
        configs = self.config.get("features", {}).get("find_me_follow_me", {}).get("users", [])
        for cfg in configs:
            # Only add if not already in database
            if cfg["extension"] not in self.user_configs:
                self.user_configs[cfg["extension"]] = cfg

    def _load_from_database(self) -> None:
        """Load FMFM configurations from database"""
        if not self.database or not self.database.enabled:
            return

        try:
            cursor = self.database.connection.cursor()
            try:
                cursor.execute("""
                    SELECT extension, mode, enabled, destinations, no_answer_destination, updated_at
                    FROM fmfm_configs
                """)

                rows = cursor.fetchall()
                for row in rows:
                    extension, mode, enabled, destinations_json, no_answer, updated_at = row

                    # Parse destinations from JSON
                    try:
                        destinations = json.loads(destinations_json)
                    except json.JSONDecodeError:
                        self.logger.warning(
                            f"Invalid JSON in destinations for extension {extension}, skipping"
                        )
                        destinations = []

                    config = {
                        "extension": extension,
                        "mode": mode,
                        "enabled": bool(enabled),
                        "destinations": destinations,
                        "updated_at": updated_at,
                    }

                    if no_answer:
                        config["no_answer_destination"] = no_answer

                    self.user_configs[extension] = config

                self.logger.info(f"Loaded {len(rows)} FMFM configurations from database")
            finally:
                cursor.close()
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as e:
            self.logger.error(f"Error loading FMFM configs from database: {e}")

    def _save_to_database(self, extension: str) -> bool:
        """
        Save FMFM configuration to database

        Note: Uses SQL CURRENT_TIMESTAMP for updated_at field instead of
        datetime.now(timezone.utc) to let the database handle timestamp generation.
        """
        if not self.database or not self.database.enabled:
            return False

        if extension not in self.user_configs:
            return False

        config = self.user_configs[extension]

        try:
            cursor = self.database.connection.cursor()

            # Convert destinations to JSON
            destinations_json = json.dumps(config.get("destinations", []))

            # Upsert (insert or update)
            cursor.execute(
                """
                INSERT INTO fmfm_configs (extension, mode, enabled, destinations, no_answer_destination, updated_at)
                VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (extension) DO UPDATE SET
                    mode = EXCLUDED.mode,
                    enabled = EXCLUDED.enabled,
                    destinations = EXCLUDED.destinations,
                    no_answer_destination = EXCLUDED.no_answer_destination,
                    updated_at = CURRENT_TIMESTAMP
            """,
                (
                    extension,
                    config.get("mode", "sequential"),
                    config.get("enabled", True),
                    destinations_json,
                    config.get("no_answer_destination"),
                ),
            )

            self.database.connection.commit()
            cursor.close()
            return True
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as e:
            self.logger.error(f"Error saving FMFM config to database: {e}")
            return False

    def _delete_from_database(self, extension: str) -> bool:
        """Delete FMFM configuration from database"""
        if not self.database or not self.database.enabled:
            return False

        try:
            cursor = self.database.connection.cursor()

            cursor.execute("DELETE FROM fmfm_configs WHERE extension = %s", (extension,))

            self.database.connection.commit()
            cursor.close()
            return True
        except Exception as e:
            self.logger.error(f"Error deleting FMFM config from database: {e}")
            return False

    def set_config(self, extension: str, config: dict) -> bool:
        """
        set Find Me/Follow Me configuration for an extension

        Args:
            extension: Extension number
            config: FMFM configuration
                Required: mode ('sequential' or 'simultaneous')
                Required: destinations (list of numbers with ring_time)
                Optional: enabled, no_answer_destination

        Returns:
            True if successful
        """
        if not self.enabled:
            self.logger.error(
                f"Cannot set FMFM config for {extension}: Find Me/Follow Me feature is not enabled globally"
            )
            return False

        required_fields = ["mode", "destinations"]
        if not all(field in config for field in required_fields):
            self.logger.error(f"Missing required FMFM fields for {extension}")
            return False

        if config["mode"] not in ["sequential", "simultaneous"]:
            self.logger.error(f"Invalid FMFM mode: {config['mode']}")
            return False

        self.user_configs[extension] = {**config, "extension": extension}

        # Add timestamp only if no database (otherwise database generates it)
        if not (self.database and self.database.enabled):
            self.user_configs[extension]["updated_at"] = datetime.now(UTC)

        # Save to database if one is configured
        if self.database and self.database.enabled:
            save_result = self._save_to_database(extension)
            if not save_result:
                # Remove from memory if database save failed
                del self.user_configs[extension]
                self.logger.error(f"Failed to save FMFM config to database for {extension}")
                return False

        self.logger.info(
            f"set FMFM config for {extension}: {config['mode']} mode with {len(config['destinations'])} destinations"
        )
        return True

    def get_config(self, extension: str) -> dict | None:
        """Get FMFM configuration for an extension"""
        return self.user_configs.get(extension)

    def get_ring_strategy(self, extension: str, call_id: str) -> dict:
        """
        Get ringing strategy for a call

        Args:
            extension: Called extension
            call_id: Call identifier

        Returns:
            Ring strategy information
        """
        if not self.enabled:
            return {"strategy": "normal", "destinations": [extension]}

        config = self.get_config(extension)
        if not config or not config.get("enabled", True):
            return {"strategy": "normal", "destinations": [extension]}

        mode = config["mode"]
        destinations = config["destinations"]

        if mode == "sequential":
            # Ring destinations one at a time
            ring_plan = [
                {
                    "destination": dest["number"],
                    "ring_time": dest.get("ring_time", 20),
                    "order": "sequential",
                }
                for dest in destinations
            ]

            return {
                "strategy": "sequential",
                "destinations": ring_plan,
                "no_answer_destination": config.get("no_answer_destination"),
                "call_id": call_id,
            }

        if mode == "simultaneous":
            # Ring all destinations at once
            ring_plan = []
            max_ring_time = 0
            for dest in destinations:
                ring_time = dest.get("ring_time", 30)
                ring_plan.append({"destination": dest["number"], "ring_time": ring_time})
                max_ring_time = max(max_ring_time, ring_time)

            return {
                "strategy": "simultaneous",
                "destinations": ring_plan,
                "max_ring_time": max_ring_time,
                "no_answer_destination": config.get("no_answer_destination"),
                "call_id": call_id,
            }

        return {"strategy": "normal", "destinations": [extension]}

    def add_destination(self, extension: str, number: str, ring_time: int = 20) -> bool:
        """Add a destination to an extension's FMFM list"""
        if extension not in self.user_configs:
            # Create new config
            self.user_configs[extension] = {
                "extension": extension,
                "mode": "sequential",
                "destinations": [],
                "enabled": True,
            }

        config = self.user_configs[extension]
        original_destinations = config["destinations"].copy()
        config["destinations"].append({"number": number, "ring_time": ring_time})

        # Save to database if one is configured
        if self.database and self.database.enabled:
            save_result = self._save_to_database(extension)
            if not save_result:
                # Revert the change if database save failed
                config["destinations"] = original_destinations
                self.logger.error(f"Failed to save FMFM config to database for {extension}")
                return False

        self.logger.info(f"Added FMFM destination for {extension}: {number}")
        return True

    def remove_destination(self, extension: str, number: str) -> bool:
        """Remove a destination from an extension's FMFM list"""
        if extension not in self.user_configs:
            return False

        config = self.user_configs[extension]
        original_destinations = config["destinations"].copy()
        original_count = len(config["destinations"])
        config["destinations"] = [d for d in config["destinations"] if d["number"] != number]

        removed = original_count - len(config["destinations"])
        if removed > 0:
            # Save to database if one is configured
            if self.database and self.database.enabled:
                save_result = self._save_to_database(extension)
                if not save_result:
                    # Revert the change if database save failed
                    config["destinations"] = original_destinations
                    self.logger.error(f"Failed to save FMFM config to database for {extension}")
                    return False

            self.logger.info(f"Removed {removed} FMFM destination(s) for {extension}: {number}")
            return True

        return False

    def enable_fmfm(self, extension: str) -> bool:
        """Enable FMFM for an extension"""
        if extension in self.user_configs:
            self.user_configs[extension]["enabled"] = True

            # Save to database if one is configured
            if self.database and self.database.enabled:
                save_result = self._save_to_database(extension)
                if not save_result:
                    # Revert the change if database save failed
                    self.user_configs[extension]["enabled"] = False
                    self.logger.error(f"Failed to save FMFM config to database for {extension}")
                    return False

            self.logger.info(f"Enabled FMFM for {extension}")
            return True
        return False

    def disable_fmfm(self, extension: str) -> bool:
        """Disable FMFM for an extension"""
        if extension in self.user_configs:
            self.user_configs[extension]["enabled"] = False

            # Save to database if one is configured
            if self.database and self.database.enabled:
                save_result = self._save_to_database(extension)
                if not save_result:
                    # Revert the change if database save failed
                    self.user_configs[extension]["enabled"] = True
                    self.logger.error(f"Failed to save FMFM config to database for {extension}")
                    return False

            self.logger.info(f"Disabled FMFM for {extension}")
            return True
        return False

    def delete_config(self, extension: str) -> bool:
        """Delete FMFM configuration for an extension"""
        if extension in self.user_configs:
            # Save a backup in case we need to restore
            backup_config = self.user_configs[extension].copy()
            del self.user_configs[extension]

            # Delete from database if one is configured
            if self.database and self.database.enabled:
                delete_result = self._delete_from_database(extension)
                if not delete_result:
                    # Restore from backup if database delete failed
                    self.user_configs[extension] = backup_config
                    self.logger.error(f"Failed to delete FMFM config from database for {extension}")
                    return False

            self.logger.info(f"Deleted FMFM config for {extension}")
            return True
        return False

    def list_extensions_with_fmfm(self) -> list[str]:
        """list extensions with FMFM configured"""
        return [ext for ext, cfg in self.user_configs.items() if cfg.get("enabled", True)]

    def get_statistics(self) -> dict:
        """Get FMFM statistics"""
        sequential_count = sum(
            1
            for cfg in self.user_configs.values()
            if cfg.get("mode") == "sequential" and cfg.get("enabled", True)
        )
        simultaneous_count = sum(
            1
            for cfg in self.user_configs.values()
            if cfg.get("mode") == "simultaneous" and cfg.get("enabled", True)
        )

        return {
            "enabled": self.enabled,
            "total_configs": len(self.user_configs),
            "sequential_configs": sequential_count,
            "simultaneous_configs": simultaneous_count,
        }

    # ==================================================================
    # Execution: turning a stored config into destinations that ring
    # ==================================================================

    def plan_for(self, extension: str, from_ext: str, call_id: str) -> FMFMState | None:
        """
        Build the ring plan for a call to `extension`, or None if Find
        Me/Follow Me should not take this call.

        Returning None is the "route normally" signal -- the feature being
        disabled, no PBX to ring with, the extension having no enabled config,
        and a config whose destinations are all unusable all reach it, so a
        broken config degrades to ordinary single-extension routing rather than
        trapping the caller.

        Args:
            extension: The dialled extension, whose config is consulted.
            from_ext: The caller, excluded from the destination list so a
                config naming the caller cannot ring them back.
            call_id: SIP Call-ID.

        Returns:
            An FMFMState positioned at the first destination, or None.
        """
        if not self.enabled or self.pbx_core is None:
            return None

        strategy = self.get_ring_strategy(extension, call_id)
        mode = strategy.get("strategy")
        if mode not in ("sequential", "simultaneous"):
            # "normal" -- no config, or the config is disabled.
            return None

        destinations = self._sanitize(strategy.get("destinations"), extension, from_ext)

        if not destinations:
            self.logger.warning(
                f"FMFM config for {extension} has no usable destinations; routing the call normally"
            )
            return None

        # Ring the extension's own phone first. Nobody lists their desk in their
        # own follow-me list -- they expect the call to reach them there before
        # it goes chasing, so an implicit first stop is what makes a config of
        # just "my mobile" behave the way it reads. Skipped when the config
        # already places the extension somewhere itself (that placement wins,
        # ring time included), and when initial_ring_time is 0.
        initial_ring = self._initial_ring_time()
        if initial_ring and not any(d["destination"] == extension for d in destinations):
            destinations.insert(0, {"destination": extension, "ring_time": initial_ring})

        # The no-answer destination is simply the last place to try, so it joins
        # the list instead of needing a branch of its own. It rings for the
        # standard no-answer timeout, since the config carries no ring time for
        # it.
        no_answer = strategy.get("no_answer_destination")
        if no_answer:
            fallback_ring = self.pbx_core.config.get("voicemail.no_answer_timeout", 30)
            destinations.extend(
                self._sanitize(
                    [{"destination": no_answer, "ring_time": fallback_ring}],
                    extension,
                    from_ext,
                    exclude={d["destination"] for d in destinations},
                )
            )

        # One implicit stop plus a long list must still not ring forever.
        del destinations[MAX_DESTINATIONS:]

        state = FMFMState(
            extension=extension,
            destinations=destinations,
            warned_simultaneous=(mode != "simultaneous"),
        )
        self.logger.info(
            f"FMFM plan for {extension}: {mode} mode, "
            f"{len(destinations)} destination(s) "
            f"({', '.join(str(d['destination']) for d in destinations)})"
        )
        return state

    def _initial_ring_time(self) -> int:
        """
        How long the dialled extension's own phone rings before the configured
        destinations, from ``features.find_me_follow_me.initial_ring_time``.

        Mirrors FreePBX Follow-Me's "Initial Ring Time". Set it to 0 to send
        calls straight into the destination list without ringing the desk
        first; an out-of-range value is clamped like any other ring time.

        Returns:
            Seconds to ring the extension, or 0 to skip that stop entirely.
        """
        raw = (
            self.config.get("features", {})
            .get("find_me_follow_me", {})
            .get("initial_ring_time", DEFAULT_RING_TIME)
        )
        try:
            seconds = int(raw)
        except (TypeError, ValueError):
            self.logger.warning(
                f"FMFM initial_ring_time is not a number ({raw!r}); using {DEFAULT_RING_TIME}s"
            )
            return DEFAULT_RING_TIME
        if seconds <= 0:
            return 0
        return max(MIN_RING_TIME, min(MAX_RING_TIME, seconds))

    def _sanitize(
        self,
        raw: Any,
        extension: str,
        from_ext: str,
        exclude: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Turn a ring strategy's destination list into one that is safe to dial:
        well-formed entries only, no self-reference, no duplicates, ring times
        clamped, and no more than MAX_DESTINATIONS of them.

        Args:
            raw: The `destinations` value from a ring strategy.
            extension: The dialled extension, for logging. Deliberately *not*
                excluded -- listing it is how a config rings the desk phone
                before moving on.
            from_ext: The caller, excluded so a config cannot ring them back.
            exclude: Additional destinations already claimed.

        Returns:
            Cleaned destination dicts, each with `destination` and `ring_time`.
        """
        seen: set[str] = set(exclude or ())
        cleaned: list[dict[str, Any]] = []

        for entry in raw or []:
            if not isinstance(entry, dict):
                continue
            number = str(entry.get("destination") or "").strip()
            if not number:
                continue
            if number == from_ext:
                self.logger.warning(
                    f"FMFM destination {number} for extension {extension} points at the "
                    "caller; skipping it rather than ringing them back on their own call"
                )
                continue
            if number in seen:
                continue
            seen.add(number)

            try:
                ring_time = int(entry.get("ring_time") or DEFAULT_RING_TIME)
            except (TypeError, ValueError):
                ring_time = DEFAULT_RING_TIME
            ring_time = max(MIN_RING_TIME, min(MAX_RING_TIME, ring_time))

            cleaned.append({"destination": number, "ring_time": ring_time})
            if len(cleaned) >= MAX_DESTINATIONS:
                self.logger.warning(
                    f"FMFM config for {extension} exceeds {MAX_DESTINATIONS} "
                    "destinations; the rest are ignored"
                )
                break

        return cleaned

    # ------------------------------------------------------------------
    # Plan registry
    # ------------------------------------------------------------------

    def state_for(self, call_id: str) -> FMFMState | None:
        """The plan ringing on `call_id`, or None if it has no plan."""
        with self._lock:
            return self._plans.get(call_id)

    def _prune_locked(self) -> None:
        """
        Drop plans whose call is gone.

        Caller must hold the lock. A call torn down while still ringing -- the
        caller hung up -- never resolves its plan, so this is what keeps the
        registry from growing for the life of the process.
        """
        active = self.pbx_core.call_manager.active_calls
        for call_id in [cid for cid in self._plans if cid not in active]:
            del self._plans[call_id]

    def _claim(self, call_id: str, expected_generation: int | None = None) -> FMFMState | None:
        """
        Claim the right to move `call_id` on to its next destination.

        Bumping the generation under the lock is what makes the claim
        exclusive: whichever of a ring timeout, an INVITE timeout and an error
        response gets here first invalidates the others.

        Args:
            call_id: Call identifier.
            expected_generation: The generation the caller was armed with, or
                None for a caller that has already established it is looking at
                the current leg.

        Returns:
            The plan, with its generation advanced, or None if the call has no
            plan, has already resolved, or has already moved on.
        """
        with self._lock:
            state = self._plans.get(call_id)
            if state is None or state.finished:
                return None
            if expected_generation is not None and expected_generation != state.generation:
                return None
            state.generation += 1
            return state

    # ------------------------------------------------------------------
    # Ringing
    # ------------------------------------------------------------------

    def begin(self, call: Any, state: FMFMState, from_header: str, to_header: str) -> bool:
        """
        Start ringing `state`'s destinations on `call`, which must already have
        its RTP relay allocated and its caller side recorded.

        Args:
            call: The Call being routed, freshly set up by CallRouter.
            state: The plan from `plan_for()`.
            from_header: Raw From header of the caller's original INVITE.
            to_header: Raw To header of the caller's original INVITE.

        Returns:
            True if the call has been taken care of -- a leg is ringing, or
            nobody was reachable and the caller has been handed to voicemail.
            False only if the call could not be progressed at all, in which case
            the caller tears it down.
        """
        state.from_header = from_header
        state.to_header = to_header
        state.generation = 1

        with self._lock:
            self._prune_locked()
            self._plans[call.call_id] = state

        if self._dial_from_index(call, state, state.generation):
            return True

        # Every destination refused to dial on the first attempt (all offline,
        # no trunk route). The extension is unreachable, which is a voicemail
        # answer, not a 404 -- and voicemail is exactly what the caller would
        # have got had the desk phone simply not picked up.
        self.logger.info(
            f"FMFM for {state.extension}: no destination could be dialled; "
            "sending the caller to voicemail"
        )
        self._to_voicemail(call, state)
        return True

    def _dial_from_index(self, call: Any, state: FMFMState, generation: int) -> bool:
        """
        Dial destinations from the current index onward, skipping any that
        cannot be reached, until one leg is in flight.

        Returns:
            True if a leg is ringing. False if the list ran out.
        """
        while state.index < len(state.destinations):
            if self._dial_one(call, state, generation):
                return True
            state.index += 1
        return False

    def _dial_one(self, call: Any, state: FMFMState, generation: int) -> bool:
        """
        Dial the destination at the current index and arm its ring timer.

        Args:
            call: The Call being rung.
            state: The plan.
            generation: Claim generation this leg belongs to; its timers carry
                it so a stale expiry can be recognised and dropped.

        Returns:
            True if the INVITE went out.
        """
        router = self.pbx_core.call_router
        entry = state.destinations[state.index]
        number = str(entry["destination"])
        ring_time = int(entry["ring_time"])
        call_id = call.call_id

        if not state.warned_simultaneous:
            state.warned_simultaneous = True
            self.logger.warning(
                f"FMFM config for {state.extension} asks for simultaneous ring, which "
                "needs several legs on one call and is not supported yet; ringing the "
                "destinations sequentially instead"
            )

        def _gave_up() -> None:
            self._on_leg_gave_up(call_id, generation)

        self.logger.info(
            f"FMFM {state.extension}: ringing destination "
            f"{state.index + 1}/{len(state.destinations)} ({number}) for {ring_time}s"
        )

        if router.EXTERNAL_NUMBER_PATTERN.match(number):
            dialled = router._dial_trunk_leg(
                call, call_id, number, ring_timeout=ring_time, on_no_answer=_gave_up
            )
        else:
            leg_to_header = _TO_USER_RE.sub(f"sip:{number}@", state.to_header, count=1)
            dialled = router._dial_extension_leg(
                call,
                call_id,
                number,
                state.from_header,
                leg_to_header,
                ring_timeout=ring_time,
                on_no_answer=_gave_up,
            )

        if not dialled:
            self.logger.info(
                f"FMFM {state.extension}: destination {number} could not be dialled, skipping"
            )
            return False

        state.leg_branch = _top_via_branch(
            call.callee_invite.get_header("Via") if call.callee_invite else None
        )
        return True

    def _on_leg_gave_up(self, call_id: str, generation: int) -> None:
        """
        The destination currently ringing ran out of time -- either its ring
        timer expired or its INVITE transaction gave up without any response.

        Args:
            call_id: Call identifier.
            generation: The claim this callback was armed for. A mismatch means
                the call already moved on and this is a late duplicate.
        """
        state = self._claim(call_id, generation)
        if state is None:
            return
        call = self.pbx_core.call_manager.get_call(call_id)
        if call is None:
            return
        self._advance(call, state, "no answer")

    def on_leg_failure(self, call: Any, message: Any) -> bool:
        """
        A destination answered our INVITE with a 4xx/5xx/6xx. Move to the next
        destination instead of letting the error reach the caller.

        Called by SIPServer for every error response on an INVITE, after it has
        ACKed it. Calls with no FMFM plan fall straight back to the normal error
        handling.

        Args:
            call: The Call the response belongs to.
            message: The error response.

        Returns:
            True if this response has been dealt with and the caller should see
            nothing of it. False to let the normal error handling run.
        """
        branch = _top_via_branch(message.get_header("Via"))

        with self._lock:
            state = self._plans.get(call.call_id)
            if state is None:
                return False
            if state.finished:
                # The outcome is already decided (answered, or on its way to
                # voicemail). A dying leg's 487 must not reach the caller.
                return True
            if state.leg_branch is None or branch is None or branch != state.leg_branch:
                # Not the leg we are waiting on -- typically a 487 from one
                # already abandoned, arriving after the next destination started
                # ringing. Swallow it; the ring timer still guarantees progress.
                self.logger.debug(
                    f"FMFM {state.extension}: ignoring {message.status_code} that does "
                    "not belong to the leg currently ringing"
                )
                return True
            state.generation += 1
            failed = state.current()

        self.logger.info(
            f"FMFM {state.extension}: destination {failed} returned "
            f"{message.status_code}, trying the next one"
        )
        self._advance(call, state, f"status {message.status_code}")
        return True

    def on_answered(self, call: Any) -> None:
        """
        A destination picked up. Close the plan so no in-flight timer moves the
        call on from under a live conversation.

        Safe to call for any answered call; one with no plan is ignored.
        """
        with self._lock:
            state = self._plans.get(call.call_id)
            if state is None or state.finished:
                return
            state.finished = True
            answered = state.current()
            self._prune_locked()

        self.logger.info(f"FMFM {state.extension}: destination {answered} answered")

    def _advance(self, call: Any, state: FMFMState, reason: str) -> None:
        """
        Give up on the destination currently ringing and start the next one,
        falling through to voicemail once the list is exhausted.

        The caller must already have claimed the right to do this via
        :meth:`_claim` or the equivalent check under the lock.

        Args:
            call: The Call being rung.
            state: The claimed plan.
            reason: Why this destination was abandoned, for the log.
        """
        abandoned = state.current()
        self._abandon_leg(call, state)
        state.index += 1

        if self._dial_from_index(call, state, state.generation):
            return

        self.logger.info(
            f"FMFM {state.extension}: last destination {abandoned} gave up ({reason}); "
            "no destinations left"
        )
        self._to_voicemail(call, state)

    def _abandon_leg(self, call: Any, state: FMFMState) -> None:
        """
        Stop the leg currently in flight and clear it off the call, so the next
        destination starts from a clean callee side.

        CANCEL goes out before the leg's INVITE is dropped -- SIPServer builds
        the CANCEL from it, reusing its Via branch to match the transaction.
        """
        if call.no_answer_timer:
            call.no_answer_timer.cancel()
            call.no_answer_timer = None
        if call.invite_transaction:
            call.invite_transaction.cancel()
            call.invite_transaction = None

        self.pbx_core.sip_server.cancel_leg(call)

        # An external destination held a trunk channel for the duration of its
        # ring. Release it here or the trunk leaks a channel per destination
        # tried -- and leave call.trunk cleared, so a later fall-through to
        # voicemail is not mistaken for an unanswered outbound trunk call.
        trunk = getattr(call, "trunk", None)
        if trunk:
            trunk.release_channel()
            trunk.record_failed_call(reason="no answer")
            call.trunk = None

        call.callee_addr = None
        call.callee_invite = None
        call.callee_rtp = None
        call.callee_dialog_to = None
        state.leg_branch = None

    def _to_voicemail(self, call: Any, state: FMFMState) -> None:
        """
        Hand the caller to the dialled extension's mailbox, the same way an
        ordinary unanswered call gets there.

        Dispatched on a timer rather than called inline: CallRouter's no-answer
        path plays a greeting and starts a recorder, which is a second or so of
        blocking work that has no business running on the SIP receive thread.
        Going through a timer also matches how that path is normally reached.

        The plan stays registered, marked finished, so a late response from the
        last abandoned leg is still recognised and swallowed rather than being
        forwarded to a caller who is now listening to a greeting.
        """
        with self._lock:
            state.finished = True

        # to_extension is still the dialled extension -- FMFM never retargets
        # it, precisely so the mailbox is the right person's.
        timer = threading.Timer(
            0.1, self.pbx_core.call_router._handle_no_answer, args=(call.call_id,)
        )
        timer.daemon = True
        timer.start()
        call.no_answer_timer = timer
