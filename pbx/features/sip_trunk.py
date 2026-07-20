"""
SIP Trunk Support
Allows external calls through SIP providers
Includes health monitoring and automatic failover
"""

import threading
import time
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from pbx.utils.e911_protection import E911Protection
from pbx.utils.logger import get_logger


class TrunkStatus(Enum):
    """SIP trunk status"""

    REGISTERED = "registered"
    UNREGISTERED = "unregistered"
    FAILED = "failed"
    DISABLED = "disabled"
    DEGRADED = "degraded"  # Partial failure


class TrunkHealthStatus(Enum):
    """Health status of trunk"""

    HEALTHY = "healthy"
    WARNING = "warning"
    CRITICAL = "critical"
    DOWN = "down"


class SIPTrunk:
    """Represents a SIP trunk connection"""

    # Re-REGISTER once this fraction of the granted Expires interval has
    # elapsed, so the refresh lands with margin before the provider's lease
    # actually lapses (standard SIP UA convention).
    REGISTRATION_REFRESH_FRACTION = 0.5

    def __init__(
        self,
        trunk_id: str,
        name: str,
        host: str,
        username: str,
        password: str,
        port: int = 5060,
        codec_preferences: list | None = None,
        priority: int = 100,
        max_channels: int = 10,
        health_check_interval: int = 60,
    ) -> None:
        """
        Initialize SIP trunk

        Args:
            trunk_id: Trunk identifier
            name: Trunk name
            host: SIP provider host
            username: SIP username
            password: SIP password
            port: SIP port (default 5060)
            codec_preferences: list of preferred codecs, as numeric RTP static
                payload-type strings (e.g. ``["0", "8", "18"]`` for PCMU/PCMA/G729)
                -- the same format used for internal extensions/phone models
                throughout ``pbx.core.pbx`` and ``pbx.sip.sdp``, so this list can
                be intersected directly against a caller's offered codecs and
                passed straight into ``SDPBuilder.build_audio_sdp``.
            priority: Trunk priority (lower is better, for failover)
            max_channels: Maximum concurrent channels
            health_check_interval: Seconds between health checks
        """
        self.trunk_id = trunk_id
        self.name = name
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        # Default: PCMU, PCMA, G722, G729, G726-32 -- the same "unknown model"
        # fallback set used for internal phones (see
        # PBXCore._get_codecs_for_phone_model's ultimate fallback).
        self.codec_preferences = codec_preferences or ["0", "8", "9", "18", "2"]
        self.status = TrunkStatus.UNREGISTERED
        self.priority = priority
        self.max_channels = max_channels
        self.channels_available = max_channels
        self.channels_in_use = 0
        self.health_check_interval = health_check_interval
        self.logger = get_logger()

        # Health monitoring
        self.health_status = TrunkHealthStatus.DOWN
        self.last_health_check = None
        self.last_successful_call = None
        self.last_failed_call = None
        self.consecutive_failures = 0
        self.total_calls = 0
        self.successful_calls = 0
        self.failed_calls = 0
        self.last_registration_attempt = None
        self.registration_failures = 0
        # When the current registration lease expires, and when it should be
        # refreshed (REGISTRATION_REFRESH_FRACTION of the way through the
        # granted Expires) -- both None until a REGISTER actually succeeds.
        self.registration_expires_at: datetime | None = None
        self.registration_refresh_at: datetime | None = None

        # Performance metrics
        self.average_call_setup_time = 0.0
        self.call_setup_times = []

        # Failover tracking
        self.failover_count = 0
        self.last_failover_time = None

    @staticmethod
    def create_from_db(db_trunk: dict) -> "SIPTrunk":
        """
        Create a SIPTrunk object from a database row (as returned by ``TrunkDB``).

        Args:
            db_trunk: Trunk row dict (``trunk_id``, ``name``, ``host``, ``username``,
                ``password``, ``port``, ``codec_preferences``, ``priority``,
                ``max_channels``, ``health_check_interval``)

        Returns:
            SIPTrunk instance
        """
        return SIPTrunk(
            trunk_id=db_trunk["trunk_id"],
            name=db_trunk["name"],
            host=db_trunk["host"],
            username=db_trunk["username"],
            password=db_trunk["password"],
            port=db_trunk.get("port", 5060),
            codec_preferences=db_trunk.get("codec_preferences"),
            priority=db_trunk.get("priority", 100),
            max_channels=db_trunk.get("max_channels", 10),
            health_check_interval=db_trunk.get("health_check_interval", 60),
        )

    @staticmethod
    def create_from_config(trunk_config: dict) -> "SIPTrunk":
        """
        Create a SIPTrunk object from a config.yml ``sip_trunks`` entry.

        Matches the field names used in the existing trunk example templates
        (``config_att_sip.yml``, ``config_comcast_sip.yml``), which key the
        trunk identifier as ``id`` rather than ``trunk_id``.

        Args:
            trunk_config: Trunk dict from config (``id``/``trunk_id``, ``name``,
                ``host``, ``username``, ``password``, ``port``,
                ``codec_preferences``, ``priority``, ``max_channels``)

        Returns:
            SIPTrunk instance
        """
        trunk_id = trunk_config.get("trunk_id") or trunk_config["id"]
        return SIPTrunk(
            trunk_id=trunk_id,
            name=trunk_config.get("name", trunk_id),
            host=trunk_config["host"],
            username=trunk_config["username"],
            password=trunk_config["password"],
            port=trunk_config.get("port", 5060),
            codec_preferences=trunk_config.get("codec_preferences"),
            priority=trunk_config.get("priority", 100),
            max_channels=trunk_config.get("max_channels", 10),
            health_check_interval=trunk_config.get("health_check_interval", 60),
        )

    def register(self, sip_server: Any | None = None) -> bool:
        """
        Register trunk with provider.

        If ``sip_server`` is given, sends a real SIP REGISTER (with digest
        auth handled by ``sip_server.register_trunk``); the outcome
        (REGISTERED vs FAILED) is determined asynchronously once the
        provider responds, so ``status`` may still be UNREGISTERED right
        after this call returns -- ``sip_server`` calls ``mark_registered()``
        on success. Also used for periodic re-REGISTER: ``SIPTrunkSystem``
        calls this again via ``needs_reregistration()``/
        ``_perform_reregistration_checks()`` before the granted Expires
        lapses; a fresh REGISTER transaction is indistinguishable from the
        initial one, so no special-casing is needed here.

        If ``sip_server`` is omitted (e.g. tests, or no live network),
        falls back to optimistically marking the trunk REGISTERED/HEALTHY
        without sending anything.

        Args:
            sip_server: SIPServer instance to send the REGISTER through.

        Returns:
            True if registration was initiated (sip_server given) or
            simulated successfully (sip_server omitted)
        """
        self.logger.info(f"Registering SIP trunk {self.name} with {self.host}")
        self.last_registration_attempt = datetime.now(UTC)

        if sip_server is not None:
            sip_server.register_trunk(self)
            return True

        self.mark_registered()
        return True

    def mark_registered(self, expires_seconds: int = 3600) -> None:
        """
        Record a successful registration: mark REGISTERED/HEALTHY, reset
        failure counters, and schedule the next refresh.

        Called by ``SIPServer._handle_trunk_register_response()`` on a 200 OK
        (with the provider's actual granted Expires, which may differ from
        what was requested), and by ``register()``'s no-``sip_server``
        fallback (with the default, since there's no wire response to read).

        Args:
            expires_seconds: Registration lease length granted by the
                provider, in seconds.
        """
        self.status = TrunkStatus.REGISTERED
        self.health_status = TrunkHealthStatus.HEALTHY
        self.registration_failures = 0
        self.consecutive_failures = 0

        now = datetime.now(UTC)
        self.registration_expires_at = now + timedelta(seconds=expires_seconds)
        self.registration_refresh_at = now + timedelta(
            seconds=expires_seconds * self.REGISTRATION_REFRESH_FRACTION
        )

    def needs_reregistration(self) -> bool:
        """
        Whether this trunk's registration should be refreshed now.

        True only for a currently-REGISTERED trunk whose refresh threshold
        (``registration_refresh_at``) has passed. A trunk that's UNREGISTERED,
        FAILED, or DISABLED is not auto-refreshed here -- that's the
        failover/health system's concern, not a plain lease renewal.

        Returns:
            True if a refresh REGISTER should be sent now.
        """
        return (
            self.status == TrunkStatus.REGISTERED
            and self.registration_refresh_at is not None
            and datetime.now(UTC) >= self.registration_refresh_at
        )

    def unregister(self) -> None:
        """Tear down the trunk's SIP registration and mark it DOWN so it stops receiving routed calls."""
        self.logger.info(f"Unregistering SIP trunk {self.name}")
        self.status = TrunkStatus.UNREGISTERED
        self.health_status = TrunkHealthStatus.DOWN

    def can_make_call(self) -> bool:
        """Check whether this trunk is eligible to take a new call right now.

        True only if the trunk is REGISTERED, its health is HEALTHY or WARNING
        (not CRITICAL/DOWN), and it has at least one free channel below
        ``max_channels``. Used by routing/failover logic to decide whether to
        send a call here or fall back to another trunk.
        """
        return (
            self.status == TrunkStatus.REGISTERED
            and self.health_status in [TrunkHealthStatus.HEALTHY, TrunkHealthStatus.WARNING]
            and self.channels_in_use < self.channels_available
        )

    def allocate_channel(self) -> bool:
        """Reserve one concurrent-call channel on this trunk for an outbound call.

        Re-checks ``can_make_call()`` first (status/health/capacity) and only
        increments ``channels_in_use``/``total_calls`` if it still passes.

        Returns:
            True if a channel was reserved, False if the trunk is unavailable
            or already at ``max_channels``.
        """
        if self.can_make_call():
            self.channels_in_use += 1
            self.total_calls += 1
            return True
        return False

    def release_channel(self) -> None:
        """Free one previously allocated channel after a call ends, so it can be reused."""
        if self.channels_in_use > 0:
            self.channels_in_use -= 1

    def record_successful_call(self, setup_time: float | None = None) -> None:
        """Record a completed call as successful and update health/performance stats.

        Resets ``consecutive_failures`` to 0, optionally rolls ``setup_time``
        into a trailing window of the last 100 call setup times to recompute
        ``average_call_setup_time``, then recalculates ``health_status`` via
        ``_update_health_status()``.
        """
        self.successful_calls += 1
        self.last_successful_call = datetime.now(UTC)
        self.consecutive_failures = 0

        if setup_time is not None:
            self.call_setup_times.append(setup_time)
            # Keep only last 100 call setup times
            if len(self.call_setup_times) > 100:
                self.call_setup_times.pop(0)
            self.average_call_setup_time = sum(self.call_setup_times) / len(self.call_setup_times)

        # Update health status based on success rate
        self._update_health_status()

    def record_failed_call(self, reason: str | None = None) -> None:
        """Record a failed call, update health, and trip the trunk to FAILED after repeated failures.

        Increments ``consecutive_failures`` and re-evaluates health via
        ``_update_health_status()``. If 5 or more failures have happened in a
        row, forces ``status`` to FAILED and ``health_status`` to DOWN,
        regardless of the success-rate-based calculation, so a trunk that is
        currently erroring is pulled out of routing immediately.
        """
        self.failed_calls += 1
        self.last_failed_call = datetime.now(UTC)
        self.consecutive_failures += 1

        self.logger.warning(f"Call failed on trunk {self.name}: {reason or 'Unknown'}")

        # Update health status
        self._update_health_status()

        # Mark trunk as failed if too many consecutive failures
        if self.consecutive_failures >= 5:
            self.logger.error(
                f"Trunk {self.name} marked as FAILED after {self.consecutive_failures} consecutive failures"
            )
            self.status = TrunkStatus.FAILED
            self.health_status = TrunkHealthStatus.DOWN

    def _update_health_status(self) -> None:
        """Recompute ``health_status`` from the trunk's lifetime call success rate.

        Skips the update until at least 10 calls have been made (too little
        data to be meaningful). Thresholds: >=95% success -> HEALTHY,
        >=80% -> WARNING, >=50% -> CRITICAL, otherwise DOWN.
        """
        if self.total_calls < 10:
            # Not enough data yet
            return

        success_rate = self.successful_calls / self.total_calls

        if success_rate >= 0.95:
            self.health_status = TrunkHealthStatus.HEALTHY
        elif success_rate >= 0.80:
            self.health_status = TrunkHealthStatus.WARNING
        elif success_rate >= 0.50:
            self.health_status = TrunkHealthStatus.CRITICAL
        else:
            self.health_status = TrunkHealthStatus.DOWN

    def check_health(self) -> TrunkHealthStatus:
        """
        Perform health check on trunk

        Returns:
            Current health status
        """
        self.last_health_check = datetime.now(UTC)

        # Check if trunk is registered
        if self.status != TrunkStatus.REGISTERED:
            self.health_status = TrunkHealthStatus.DOWN
            return self.health_status

        # Check consecutive failures
        if self.consecutive_failures >= 5:
            self.health_status = TrunkHealthStatus.DOWN
        elif self.consecutive_failures >= 3:
            self.health_status = TrunkHealthStatus.CRITICAL
        elif self.consecutive_failures >= 1:
            self.health_status = TrunkHealthStatus.WARNING

        # Check last successful call time
        if self.last_successful_call:
            time_since_success = (datetime.now(UTC) - self.last_successful_call).total_seconds()
            if time_since_success > 3600:  # 1 hour
                self.health_status = TrunkHealthStatus.CRITICAL

        # In a real implementation, would:
        # 1. Send OPTIONS ping to trunk
        # 2. Measure response time
        # 3. Check registration status
        # 4. Verify DNS resolution

        self.logger.debug(f"Health check for {self.name}: {self.health_status.value}")
        return self.health_status

    def get_success_rate(self) -> float:
        """Return the fraction of this trunk's calls that succeeded (0.0-1.0), or 0.0 if none made yet."""
        if self.total_calls == 0:
            return 0.0
        return self.successful_calls / self.total_calls

    def get_health_metrics(self) -> dict:
        """Build a dict snapshot of this trunk's health/performance counters for status APIs and dashboards."""
        return {
            "health_status": self.health_status.value,
            "last_health_check": (
                self.last_health_check.isoformat() if self.last_health_check else None
            ),
            "total_calls": self.total_calls,
            "successful_calls": self.successful_calls,
            "failed_calls": self.failed_calls,
            "success_rate": self.get_success_rate(),
            "consecutive_failures": self.consecutive_failures,
            "average_setup_time": self.average_call_setup_time,
            "last_successful_call": (
                self.last_successful_call.isoformat() if self.last_successful_call else None
            ),
            "last_failed_call": (
                self.last_failed_call.isoformat() if self.last_failed_call else None
            ),
            "failover_count": self.failover_count,
            "registration_expires_at": (
                self.registration_expires_at.isoformat() if self.registration_expires_at else None
            ),
            "registration_refresh_at": (
                self.registration_refresh_at.isoformat() if self.registration_refresh_at else None
            ),
        }

    def to_dict(self) -> dict:
        """Serialize this trunk's configuration and current stats for the trunk-status API/admin UI."""
        return {
            "trunk_id": self.trunk_id,
            "name": self.name,
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "status": self.status.value,
            "health_status": self.health_status.value,
            "priority": self.priority,
            "max_channels": self.max_channels,
            "channels_available": self.channels_available,
            "channels_in_use": self.channels_in_use,
            "codec_preferences": self.codec_preferences,
            "success_rate": self.get_success_rate(),
            "consecutive_failures": self.consecutive_failures,
            "total_calls": self.total_calls,
            "successful_calls": self.successful_calls,
            "failed_calls": self.failed_calls,
        }


class OutboundRule:
    """A single dial-plan entry: which dialed numbers go to which trunk, and how
    the number should be rewritten (strip/prepend digits) before sending it out.
    ``SIPTrunkSystem`` evaluates a list of these in order and uses the first match.
    """

    def __init__(
        self, rule_id: str, pattern: str, trunk_id: str, prepend: str = "", strip: int = 0
    ) -> None:
        """
        Initialize outbound rule

        Args:
            rule_id: Rule identifier
            pattern: Dial pattern (regex)
            trunk_id: Trunk to use
            prepend: Digits to prepend
            strip: Number of digits to strip from beginning
        """
        self.rule_id = rule_id
        self.pattern = pattern
        self.trunk_id = trunk_id
        self.prepend = prepend
        self.strip = strip

    def matches(self, number: str) -> bool:
        """
        Test whether ``number`` matches this rule's dial pattern (regex, anchored
        at the start via ``re.match`` — a partial/prefix match is enough).

        Args:
            number: Dialed number

        Returns:
            True if matches
        """
        import re

        return bool(re.match(self.pattern, number))

    def transform_number(self, number: str) -> str:
        """
        Rewrite ``number`` into the form actually sent to the trunk: first strip
        ``self.strip`` leading digits (e.g. drop an outside-line "9"), then
        prepend ``self.prepend`` (e.g. add a "1" country/trunk-access code).

        Args:
            number: Original number

        Returns:
            Transformed number
        """
        # Strip digits
        if self.strip > 0:
            number = number[self.strip :]

        # Prepend digits
        if self.prepend:
            number = self.prepend + number

        return number


class SIPTrunkSystem:
    """Manages SIP trunks for external calls with health monitoring and failover"""

    def __init__(
        self,
        config: Any | None = None,
        sip_server: Any | None = None,
        trunk_db: Any | None = None,
    ) -> None:
        """Initialize SIP trunk system

        Args:
            config: Configuration object (optional). Also used as a fallback
                source of persisted trunks (``config.get_sip_trunks()``) when
                ``trunk_db`` is not provided.
            sip_server: SIPServer instance used to send real SIP REGISTERs to
                trunk providers. If omitted, ``register_all()``/``trunk.register()``
                fall back to simulated (optimistic) registration.
            trunk_db: TrunkDB instance used to load persisted trunks from the
                database. If omitted (e.g. database disabled), persisted
                trunks are instead loaded from ``config`` if it supports
                ``get_sip_trunks()``.
        """
        self.trunks = {}
        self.outbound_rules = []
        # rule_id -> trunk_id the rule pointed at before a failover re-pointed
        # it elsewhere. Populated by _handle_trunk_failure, drained by
        # _handle_trunk_recovery when the original trunk comes back healthy.
        self.original_rule_trunk_ids: dict[str, str] = {}
        self.logger = get_logger()
        self.e911_protection = E911Protection(config)
        self.config = config
        self.sip_server = sip_server
        self.trunk_db = trunk_db

        # Health monitoring
        self.health_check_enabled = True
        self.health_check_thread = None
        self.health_check_interval = 60  # seconds
        self.monitoring_active = False

        # Failover configuration
        self.failover_enabled = True
        self.auto_recovery_enabled = True
        self.failover_threshold = 3  # consecutive failures before failover

        self._load_trunks()

    def _load_trunks(self) -> None:
        """
        Load persisted trunk definitions into ``self.trunks``.

        Prefers the database (``self.trunk_db``) when available; otherwise
        falls back to ``self.config.get_sip_trunks()`` if ``config`` supports
        it. Trunks with ``enabled`` explicitly set to False in the database
        are skipped. Mirrors ``ExtensionRegistry._load_extensions()``.
        """
        if self.trunk_db is not None:
            try:
                db_trunks = self.trunk_db.get_all()
            except (KeyError, TypeError, ValueError) as e:
                self.logger.error(f"Failed to load SIP trunks from database: {e}")
                return

            if db_trunks:
                self.logger.info(f"Loading {len(db_trunks)} SIP trunks from database")
                for row in db_trunks:
                    if row.get("enabled") is False:
                        continue
                    self.add_trunk(SIPTrunk.create_from_db(row))
            return

        if self.config is not None and hasattr(self.config, "get_sip_trunks"):
            configured_trunks = self.config.get_sip_trunks()
            if configured_trunks:
                self.logger.info(f"Loading {len(configured_trunks)} SIP trunks from config")
                for trunk_config in configured_trunks:
                    self.add_trunk(SIPTrunk.create_from_config(trunk_config))

    def reload_trunks(self) -> None:
        """Discard all in-memory trunks and reload them from the database/config (e.g. after an admin API write)."""
        self.trunks = {}
        self._load_trunks()

    def start_health_monitoring(self) -> None:
        """Start a daemon background thread that periodically health-checks every trunk and triggers failover on outage (no-op if already running)."""
        if self.monitoring_active:
            self.logger.warning("Health monitoring already active")
            return

        self.monitoring_active = True
        self.health_check_thread = threading.Thread(
            target=self._health_monitoring_loop, daemon=True
        )
        self.health_check_thread.start()
        self.logger.info("Started SIP trunk health monitoring")

    def stop_health_monitoring(self) -> None:
        """Signal the health-monitoring thread to exit and block (up to 5s) for it to join."""
        self.monitoring_active = False
        if self.health_check_thread:
            self.health_check_thread.join(timeout=5)
        self.logger.info("Stopped SIP trunk health monitoring")

    def _health_monitoring_loop(self) -> None:
        """Loop body run by the health-monitoring thread: check all trunks every
        ``health_check_interval`` seconds, sleeping 5s and continuing on error
        instead of letting the thread die.
        """
        while self.monitoring_active:
            try:
                self._perform_health_checks()
                self._perform_reregistration_checks()
                time.sleep(self.health_check_interval)
            except Exception as e:
                self.logger.error(f"Error in health monitoring loop: {e}")
                time.sleep(5)

    def _perform_health_checks(self) -> None:
        """Run ``check_health()`` on every trunk, log any status transitions, and
        kick off failover via ``_handle_trunk_failure`` for any trunk that just
        went DOWN (if ``failover_enabled``).
        """
        for trunk in self.trunks.values():
            try:
                old_status = trunk.health_status
                new_status = trunk.check_health()

                # Log status changes
                if old_status != new_status:
                    self.logger.warning(
                        f"Trunk {trunk.name} health changed: {old_status.value} -> {new_status.value}"
                    )

                    # Trigger failover if trunk went down
                    if new_status == TrunkHealthStatus.DOWN and self.failover_enabled:
                        self._handle_trunk_failure(trunk)
                    # Restore re-pointed rules if a previously-down trunk recovered
                    elif (
                        old_status == TrunkHealthStatus.DOWN
                        and new_status in (TrunkHealthStatus.HEALTHY, TrunkHealthStatus.WARNING)
                        and self.auto_recovery_enabled
                    ):
                        self._handle_trunk_recovery(trunk)
            except Exception as e:
                self.logger.error(f"Error checking health of trunk {trunk.name}: {e}")

    def _perform_reregistration_checks(self) -> None:
        """Re-REGISTER any trunk whose lease is due for a refresh.

        Runs every health-monitoring cycle alongside ``_perform_health_checks``
        (same thread, same cadence) rather than on a per-trunk timer -- a
        trunk's ``needs_reregistration()`` stays True until its next 200 OK
        updates ``registration_refresh_at``, so a refresh that's lost or
        unanswered is naturally retried on the following cycle without any
        separate retry bookkeeping here.
        """
        for trunk in self.trunks.values():
            try:
                if trunk.needs_reregistration():
                    self.logger.info(f"Refreshing registration for trunk {trunk.name}")
                    trunk.register(self.sip_server)
            except Exception as e:
                self.logger.error(f"Error refreshing registration for trunk {trunk.name}: {e}")

    def add_trunk(self, trunk: Any) -> None:
        """
        Add SIP trunk

        Args:
            trunk: SIPTrunk object
        """
        self.trunks[trunk.trunk_id] = trunk
        self.logger.info(f"Added SIP trunk: {trunk.name}")

    def remove_trunk(self, trunk_id: str) -> None:
        """Unregister and delete the trunk with this ID, if it exists. Does not touch any ``outbound_rules`` still pointing at it."""
        if trunk_id in self.trunks:
            trunk = self.trunks[trunk_id]
            trunk.unregister()
            del self.trunks[trunk_id]
            self.logger.info(f"Removed SIP trunk: {trunk_id}")

    def get_trunk(self, trunk_id: str) -> Any | None:
        """Look up a registered ``SIPTrunk`` by its ``trunk_id``, or None if not found."""
        return self.trunks.get(trunk_id)

    def register_all(self) -> None:
        """Call ``register()`` on every trunk currently managed by this system (e.g. on startup), routed through ``self.sip_server`` if one was provided."""
        for trunk in self.trunks.values():
            trunk.register(self.sip_server)

    def add_outbound_rule(self, rule: Any) -> None:
        """
        Add outbound routing rule

        Args:
            rule: OutboundRule object
        """
        self.outbound_rules.append(rule)
        self.logger.info(f"Added outbound rule: {rule.pattern} -> trunk {rule.trunk_id}")

    def route_outbound(self, number: str) -> tuple:
        """
        Pick a trunk and rewritten number for an outbound call, with no failover.

        Blocks the call outright if it's an E911 number under test-mode
        protection. Otherwise walks ``outbound_rules`` in order and returns the
        first matching rule whose trunk is currently usable (``can_make_call()``).
        If the matching trunk for a rule is unavailable, this method does NOT
        try another trunk for that rule — see ``route_outbound_with_failover``
        for that behavior.

        Args:
            number: Dialed number

        Returns:
            tuple of (trunk, transformed_number) or (None, None)
        """
        # Block E911 calls in test mode
        if self.e911_protection.block_if_e911(number, context="route_outbound"):
            self.logger.error(f"E911 call to {number} blocked by protection system")
            return (None, None)

        for rule in self.outbound_rules:
            if rule.matches(number):
                trunk = self.get_trunk(rule.trunk_id)

                if trunk and trunk.can_make_call():
                    transformed = rule.transform_number(number)
                    self.logger.info(f"Routing {number} -> {transformed} via trunk {trunk.name}")
                    return (trunk, transformed)

        self.logger.warning(f"No route found for outbound number {number}")
        return (None, None)

    def get_trunk_status(self) -> dict:
        """Return ``to_dict()`` for every managed trunk, for use in status APIs/dashboards."""
        return [trunk.to_dict() for trunk in self.trunks.values()]

    def _handle_trunk_failure(self, failed_trunk: SIPTrunk) -> None:
        """
        React to a trunk going DOWN: bump its failover counters, find which
        outbound rules route through it, and re-point those rules at the
        next-best alternative trunk by priority. This keeps ``route_outbound()``
        (the non-failover-aware lookup) reflecting the outage too, not just
        ``route_outbound_with_failover()`` -- per-call failover already works
        correctly independent of this. Notification is log-only for v1; there's
        no ``WebhookEvent`` type for trunk failure yet.

        Args:
            failed_trunk: The trunk that failed
        """
        self.logger.error(f"Handling failure of trunk: {failed_trunk.name}")

        # Mark failover time
        failed_trunk.last_failover_time = datetime.now(UTC)
        failed_trunk.failover_count += 1

        # Find rules using this trunk
        affected_rules = [
            rule for rule in self.outbound_rules if rule.trunk_id == failed_trunk.trunk_id
        ]

        if not affected_rules:
            return

        # Find alternative trunks
        alternative_trunks = self._get_available_trunks_by_priority()

        if not alternative_trunks:
            self.logger.critical(
                f"No alternative trunks available for failover from {failed_trunk.name}"
            )
            return

        # Use highest priority alternative
        failover_trunk = alternative_trunks[0]

        self.logger.warning(
            f"Failing over from trunk {failed_trunk.name} to {failover_trunk.name} "
            f"for {len(affected_rules)} routes"
        )

        for rule in affected_rules:
            # Only remember the trunk a rule pointed at before *any* failover,
            # so a second failure (failover_trunk itself going down) doesn't
            # overwrite the true original with an intermediate failover trunk.
            self.original_rule_trunk_ids.setdefault(rule.rule_id, rule.trunk_id)
            rule.trunk_id = failover_trunk.trunk_id

    def _handle_trunk_recovery(self, recovered_trunk: SIPTrunk) -> None:
        """
        React to a previously-DOWN trunk becoming healthy again: restore any
        outbound rules that were re-pointed away from it in
        ``_handle_trunk_failure`` back to their original trunk, and drop the
        shadow entry. Called from ``_perform_health_checks`` on the same cycle
        that detects the recovery -- no separate timer/thread needed.

        Args:
            recovered_trunk: The trunk that just transitioned out of DOWN
        """
        restored_rule_ids = [
            rule_id
            for rule_id, original_trunk_id in self.original_rule_trunk_ids.items()
            if original_trunk_id == recovered_trunk.trunk_id
        ]

        if not restored_rule_ids:
            return

        rules_by_id = {rule.rule_id: rule for rule in self.outbound_rules}

        for rule_id in restored_rule_ids:
            original_trunk_id = self.original_rule_trunk_ids.pop(rule_id)
            rule = rules_by_id.get(rule_id)
            if rule is None:
                # Rule was deleted while its trunk was down; nothing to restore.
                continue
            self.logger.info(
                f"Trunk {recovered_trunk.name} recovered: restoring rule "
                f"{rule.rule_id} from {rule.trunk_id} back to {original_trunk_id}"
            )
            rule.trunk_id = original_trunk_id

    def _route_via_priority_fallback(self, number: str) -> tuple[SIPTrunk | None, str | None]:
        """
        Route ``number`` unmodified via the highest-priority trunk that can
        currently take a call, with no dial-plan rule involved.

        Used by ``route_outbound_with_failover`` only when zero
        ``outbound_rules`` are configured, so the common single-trunk (or
        multiple-trunks-ranked-only-by-priority) deployment can place calls
        without an admin having to define a rule first.

        Args:
            number: Dialed number (sent as-is; there is no rule to
                strip/prepend digits).

        Returns:
            tuple of (trunk, number) or (None, None) if no trunk is usable.
        """
        for trunk in self._get_available_trunks_by_priority():
            if trunk.can_make_call():
                self.logger.info(
                    f"No outbound rules configured; routing {number} via "
                    f"highest-priority trunk {trunk.name}"
                )
                return (trunk, number)

        self.logger.warning(f"No outbound rules configured and no trunk available for {number}")
        return (None, None)

    def _get_available_trunks_by_priority(self) -> list[SIPTrunk]:
        """
        Collect trunks that are REGISTERED and HEALTHY/WARNING (regardless of
        current channel usage), sorted ascending by ``priority`` (lower number
        = preferred). Used to pick failover candidates.

        Returns:
            list of available trunks
        """
        available = [
            trunk
            for trunk in self.trunks.values()
            if trunk.status == TrunkStatus.REGISTERED
            and trunk.health_status in [TrunkHealthStatus.HEALTHY, TrunkHealthStatus.WARNING]
        ]

        # Sort by priority (lower is better)
        available.sort(key=lambda t: t.priority)
        return available

    def route_outbound_with_failover(self, number: str) -> tuple[SIPTrunk | None, str | None]:
        """
        Like ``route_outbound``, but if the first matching rule's trunk is
        unavailable (down/unhealthy/full), automatically falls back to the
        next-best healthy trunk by priority (via ``_find_failover_trunk``)
        instead of giving up on that rule. This is the routing method that
        should be used for live calls.

        If no ``outbound_rules`` are configured at all, routes via the
        highest-priority trunk that can currently take a call (see
        ``_route_via_priority_fallback``), so a deployment with one trunk
        (or several ranked only by ``priority``) works without requiring
        dial-plan rules to be defined first. This fallback only applies
        when zero rules exist -- once any rule is added, an unmatched
        number is treated as "no route" rather than silently going out an
        arbitrary trunk, since a defined rule set expresses deliberate
        routing intent (e.g. excluding premium/international numbers).

        Args:
            number: Dialed number

        Returns:
            tuple of (trunk, transformed_number) or (None, None)
        """
        # Block E911 calls in test mode
        if self.e911_protection.block_if_e911(number, context="route_outbound_with_failover"):
            self.logger.error(f"E911 call to {number} blocked by protection system")
            return (None, None)

        if not self.outbound_rules:
            return self._route_via_priority_fallback(number)

        # Try primary trunk first
        for rule in self.outbound_rules:
            if rule.matches(number):
                trunk = self.get_trunk(rule.trunk_id)

                if trunk and trunk.can_make_call():
                    transformed = rule.transform_number(number)
                    self.logger.info(f"Routing {number} -> {transformed} via trunk {trunk.name}")
                    return (trunk, transformed)
                if trunk:
                    # Primary trunk unavailable, try failover
                    self.logger.warning(
                        f"Primary trunk {trunk.name} unavailable (status: {trunk.status.value}, "
                        f"health: {trunk.health_status.value})"
                    )

                    if self.failover_enabled:
                        failover_trunk = self._find_failover_trunk(trunk)
                        if failover_trunk:
                            transformed = rule.transform_number(number)
                            self.logger.warning(
                                f"Failover: Routing {number} -> {transformed} via trunk {failover_trunk.name}"
                            )
                            return (failover_trunk, transformed)

        self.logger.warning(f"No route found for outbound number {number}")
        return (None, None)

    def _find_failover_trunk(self, primary_trunk: SIPTrunk) -> SIPTrunk | None:
        """
        Pick the highest-priority trunk, other than ``primary_trunk``, that
        can actually take a call right now to take over the routing. Returns
        None if no other usable trunk exists.

        ``_get_available_trunks_by_priority()`` only filters on status/health,
        not free channel capacity, so it can include a trunk that is
        REGISTERED and HEALTHY but already at ``max_channels``. Checking
        ``can_make_call()`` here (same check ``allocate_channel()`` makes)
        ensures failover never hands back a trunk with no room for the call.

        Args:
            primary_trunk: The primary trunk that failed

        Returns:
            Alternative trunk or None
        """
        for trunk in self._get_available_trunks_by_priority():
            if trunk.trunk_id != primary_trunk.trunk_id and trunk.can_make_call():
                return trunk

        return None

    def get_trunk_health_summary(self) -> dict:
        """
        Aggregate per-health-status trunk counts, total/successful/failed call
        counts, and an overall success rate across all trunks, plus a detailed
        per-trunk breakdown. Used to power a trunk health dashboard.

        Returns:
            Dictionary with health statistics
        """
        summary = {
            "total_trunks": len(self.trunks),
            "healthy": 0,
            "warning": 0,
            "critical": 0,
            "down": 0,
            "total_calls": 0,
            "successful_calls": 0,
            "failed_calls": 0,
            "trunks": [],
        }

        for trunk in self.trunks.values():
            # Count by health status
            if trunk.health_status == TrunkHealthStatus.HEALTHY:
                summary["healthy"] += 1
            elif trunk.health_status == TrunkHealthStatus.WARNING:
                summary["warning"] += 1
            elif trunk.health_status == TrunkHealthStatus.CRITICAL:
                summary["critical"] += 1
            elif trunk.health_status == TrunkHealthStatus.DOWN:
                summary["down"] += 1

            # Aggregate call stats
            summary["total_calls"] += trunk.total_calls
            summary["successful_calls"] += trunk.successful_calls
            summary["failed_calls"] += trunk.failed_calls

            # Add trunk details
            summary["trunks"].append(
                {
                    "trunk_id": trunk.trunk_id,
                    "name": trunk.name,
                    "status": trunk.status.value,
                    "health": trunk.health_status.value,
                    "success_rate": trunk.get_success_rate(),
                    "metrics": trunk.get_health_metrics(),
                }
            )

        # Calculate overall success rate
        if summary["total_calls"] > 0:
            summary["overall_success_rate"] = summary["successful_calls"] / summary["total_calls"]
        else:
            summary["overall_success_rate"] = 0.0

        return summary


# Global instance
_trunk_manager = None


def get_trunk_manager(config: Any | None = None) -> SIPTrunkSystem:
    """
    Get or create SIP trunk manager instance.

    Args:
        config: Configuration dict. Required for first initialization.

    Returns:
        SIPTrunkSystem instance or None if not yet initialized.
        Callers must check for None before using.
    """
    global _trunk_manager
    if _trunk_manager is None and config is not None:
        _trunk_manager = SIPTrunkSystem(config)
    return _trunk_manager
