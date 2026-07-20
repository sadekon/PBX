"""Comprehensive tests for pbx/features/sip_trunk.py module."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from pbx.features.sip_trunk import (
    OutboundRule,
    SIPTrunk,
    SIPTrunkSystem,
    TrunkHealthStatus,
    TrunkStatus,
    get_trunk_manager,
)

# ---------------------------------------------------------------------------
# TrunkStatus / TrunkHealthStatus enums
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTrunkStatusEnum:
    """Tests for the TrunkStatus enum."""

    def test_registered_value(self) -> None:
        assert TrunkStatus.REGISTERED.value == "registered"

    def test_unregistered_value(self) -> None:
        assert TrunkStatus.UNREGISTERED.value == "unregistered"

    def test_failed_value(self) -> None:
        assert TrunkStatus.FAILED.value == "failed"

    def test_disabled_value(self) -> None:
        assert TrunkStatus.DISABLED.value == "disabled"

    def test_degraded_value(self) -> None:
        assert TrunkStatus.DEGRADED.value == "degraded"


@pytest.mark.unit
class TestTrunkHealthStatusEnum:
    """Tests for the TrunkHealthStatus enum."""

    def test_healthy_value(self) -> None:
        assert TrunkHealthStatus.HEALTHY.value == "healthy"

    def test_warning_value(self) -> None:
        assert TrunkHealthStatus.WARNING.value == "warning"

    def test_critical_value(self) -> None:
        assert TrunkHealthStatus.CRITICAL.value == "critical"

    def test_down_value(self) -> None:
        assert TrunkHealthStatus.DOWN.value == "down"


# ---------------------------------------------------------------------------
# SIPTrunk
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSIPTrunkInit:
    """Tests for SIPTrunk initialization."""

    @patch("pbx.features.sip_trunk.get_logger")
    def test_init_defaults(self, mock_logger: MagicMock) -> None:
        trunk = SIPTrunk(
            trunk_id="t1",
            name="Primary",
            host="sip.example.com",
            username="user",
            password="pass",
        )
        assert trunk.trunk_id == "t1"
        assert trunk.name == "Primary"
        assert trunk.host == "sip.example.com"
        assert trunk.port == 5060
        assert trunk.username == "user"
        assert trunk.password == "pass"
        assert trunk.codec_preferences == ["0", "8", "9", "18", "2"]
        assert trunk.status == TrunkStatus.UNREGISTERED
        assert trunk.priority == 100
        assert trunk.max_channels == 10
        assert trunk.channels_available == 10
        assert trunk.channels_in_use == 0
        assert trunk.health_check_interval == 60
        assert trunk.health_status == TrunkHealthStatus.DOWN
        assert trunk.last_health_check is None
        assert trunk.last_successful_call is None
        assert trunk.last_failed_call is None
        assert trunk.consecutive_failures == 0
        assert trunk.total_calls == 0
        assert trunk.successful_calls == 0
        assert trunk.failed_calls == 0
        assert trunk.last_registration_attempt is None
        assert trunk.registration_failures == 0
        assert trunk.average_call_setup_time == 0.0
        assert trunk.call_setup_times == []
        assert trunk.failover_count == 0
        assert trunk.last_failover_time is None

    @patch("pbx.features.sip_trunk.get_logger")
    def test_init_custom_params(self, mock_logger: MagicMock) -> None:
        trunk = SIPTrunk(
            trunk_id="t2",
            name="Secondary",
            host="sip2.example.com",
            username="user2",
            password="pass2",
            port=5061,
            codec_preferences=["G.722", "OPUS"],
            priority=50,
            max_channels=20,
            health_check_interval=30,
        )
        assert trunk.port == 5061
        assert trunk.codec_preferences == ["G.722", "OPUS"]
        assert trunk.priority == 50
        assert trunk.max_channels == 20
        assert trunk.channels_available == 20
        assert trunk.health_check_interval == 30


@pytest.mark.unit
class TestSIPTrunkRegistration:
    """Tests for SIPTrunk register/unregister."""

    @patch("pbx.features.sip_trunk.get_logger")
    def test_register_success(self, mock_logger: MagicMock) -> None:
        trunk = SIPTrunk("t1", "Primary", "sip.example.com", "user", "pass")
        result = trunk.register()
        assert result is True
        assert trunk.status == TrunkStatus.REGISTERED
        assert trunk.health_status == TrunkHealthStatus.HEALTHY
        assert trunk.registration_failures == 0
        assert trunk.consecutive_failures == 0
        assert trunk.last_registration_attempt is not None

    @patch("pbx.features.sip_trunk.get_logger")
    def test_unregister(self, mock_logger: MagicMock) -> None:
        trunk = SIPTrunk("t1", "Primary", "sip.example.com", "user", "pass")
        trunk.register()
        trunk.unregister()
        assert trunk.status == TrunkStatus.UNREGISTERED
        assert trunk.health_status == TrunkHealthStatus.DOWN


@pytest.mark.unit
class TestSIPTrunkMarkRegistered:
    """Tests for SIPTrunk.mark_registered."""

    @patch("pbx.features.sip_trunk.get_logger")
    def test_sets_status_and_health(self, mock_logger: MagicMock) -> None:
        trunk = SIPTrunk("t1", "Primary", "sip.example.com", "user", "pass")
        trunk.consecutive_failures = 3
        trunk.registration_failures = 2

        trunk.mark_registered(expires_seconds=3600)

        assert trunk.status == TrunkStatus.REGISTERED
        assert trunk.health_status == TrunkHealthStatus.HEALTHY
        assert trunk.registration_failures == 0
        assert trunk.consecutive_failures == 0

    @patch("pbx.features.sip_trunk.get_logger")
    def test_schedules_expiry_and_refresh_at_half_the_interval(
        self, mock_logger: MagicMock
    ) -> None:
        trunk = SIPTrunk("t1", "Primary", "sip.example.com", "user", "pass")
        before = datetime.now(UTC)

        trunk.mark_registered(expires_seconds=3600)

        after = datetime.now(UTC)
        assert trunk.registration_expires_at is not None
        assert trunk.registration_refresh_at is not None
        # Expires ~3600s out
        assert (
            before + timedelta(seconds=3600)
            <= trunk.registration_expires_at
            <= after + timedelta(seconds=3600)
        )
        # Refresh at 50% of that (REGISTRATION_REFRESH_FRACTION)
        assert (
            before + timedelta(seconds=1800)
            <= trunk.registration_refresh_at
            <= after + timedelta(seconds=1800)
        )
        assert trunk.registration_refresh_at < trunk.registration_expires_at

    @patch("pbx.features.sip_trunk.get_logger")
    def test_honors_shorter_provider_granted_expiry(self, mock_logger: MagicMock) -> None:
        """A provider may grant less than the 3600s requested."""
        trunk = SIPTrunk("t1", "Primary", "sip.example.com", "user", "pass")
        before = datetime.now(UTC)

        trunk.mark_registered(expires_seconds=120)

        after = datetime.now(UTC)
        assert (
            before + timedelta(seconds=60)
            <= trunk.registration_refresh_at
            <= after + timedelta(seconds=60)
        )


@pytest.mark.unit
class TestSIPTrunkNeedsReregistration:
    """Tests for SIPTrunk.needs_reregistration."""

    @patch("pbx.features.sip_trunk.get_logger")
    def test_false_when_never_registered(self, mock_logger: MagicMock) -> None:
        trunk = SIPTrunk("t1", "Primary", "sip.example.com", "user", "pass")
        assert trunk.needs_reregistration() is False

    @patch("pbx.features.sip_trunk.get_logger")
    def test_false_when_recently_registered(self, mock_logger: MagicMock) -> None:
        trunk = SIPTrunk("t1", "Primary", "sip.example.com", "user", "pass")
        trunk.mark_registered(expires_seconds=3600)
        assert trunk.needs_reregistration() is False

    @patch("pbx.features.sip_trunk.get_logger")
    def test_true_once_refresh_threshold_passed(self, mock_logger: MagicMock) -> None:
        trunk = SIPTrunk("t1", "Primary", "sip.example.com", "user", "pass")
        trunk.mark_registered(expires_seconds=3600)
        # Simulate time passing well beyond the 50% refresh threshold.
        trunk.registration_refresh_at = datetime.now(UTC) - timedelta(seconds=1)
        assert trunk.needs_reregistration() is True

    @patch("pbx.features.sip_trunk.get_logger")
    def test_false_when_not_registered_even_if_refresh_time_passed(
        self, mock_logger: MagicMock
    ) -> None:
        """A FAILED/UNREGISTERED trunk isn't auto-refreshed by this check --
        that's the failover/health system's concern."""
        trunk = SIPTrunk("t1", "Primary", "sip.example.com", "user", "pass")
        trunk.mark_registered(expires_seconds=3600)
        trunk.registration_refresh_at = datetime.now(UTC) - timedelta(seconds=1)
        trunk.status = TrunkStatus.FAILED
        assert trunk.needs_reregistration() is False


@pytest.mark.unit
class TestSIPTrunkCanMakeCall:
    """Tests for SIPTrunk.can_make_call."""

    @patch("pbx.features.sip_trunk.get_logger")
    def test_can_make_call_when_registered_healthy(self, mock_logger: MagicMock) -> None:
        trunk = SIPTrunk("t1", "Primary", "sip.example.com", "user", "pass")
        trunk.register()
        assert trunk.can_make_call() is True

    @patch("pbx.features.sip_trunk.get_logger")
    def test_cannot_make_call_when_unregistered(self, mock_logger: MagicMock) -> None:
        trunk = SIPTrunk("t1", "Primary", "sip.example.com", "user", "pass")
        assert trunk.can_make_call() is False

    @patch("pbx.features.sip_trunk.get_logger")
    def test_cannot_make_call_when_channels_full(self, mock_logger: MagicMock) -> None:
        trunk = SIPTrunk("t1", "Primary", "sip.example.com", "user", "pass", max_channels=1)
        trunk.register()
        trunk.channels_in_use = 1
        assert trunk.can_make_call() is False

    @patch("pbx.features.sip_trunk.get_logger")
    def test_can_make_call_when_warning_status(self, mock_logger: MagicMock) -> None:
        trunk = SIPTrunk("t1", "Primary", "sip.example.com", "user", "pass")
        trunk.register()
        trunk.health_status = TrunkHealthStatus.WARNING
        assert trunk.can_make_call() is True

    @patch("pbx.features.sip_trunk.get_logger")
    def test_cannot_make_call_when_critical(self, mock_logger: MagicMock) -> None:
        trunk = SIPTrunk("t1", "Primary", "sip.example.com", "user", "pass")
        trunk.register()
        trunk.health_status = TrunkHealthStatus.CRITICAL
        assert trunk.can_make_call() is False

    @patch("pbx.features.sip_trunk.get_logger")
    def test_cannot_make_call_when_down(self, mock_logger: MagicMock) -> None:
        trunk = SIPTrunk("t1", "Primary", "sip.example.com", "user", "pass")
        trunk.register()
        trunk.health_status = TrunkHealthStatus.DOWN
        assert trunk.can_make_call() is False


@pytest.mark.unit
class TestSIPTrunkChannels:
    """Tests for channel allocation and release."""

    @patch("pbx.features.sip_trunk.get_logger")
    def test_allocate_channel_success(self, mock_logger: MagicMock) -> None:
        trunk = SIPTrunk("t1", "Primary", "sip.example.com", "user", "pass")
        trunk.register()
        result = trunk.allocate_channel()
        assert result is True
        assert trunk.channels_in_use == 1
        assert trunk.total_calls == 1

    @patch("pbx.features.sip_trunk.get_logger")
    def test_allocate_channel_failure_no_capacity(self, mock_logger: MagicMock) -> None:
        trunk = SIPTrunk("t1", "Primary", "sip.example.com", "user", "pass", max_channels=1)
        trunk.register()
        trunk.allocate_channel()
        result = trunk.allocate_channel()
        assert result is False
        assert trunk.channels_in_use == 1

    @patch("pbx.features.sip_trunk.get_logger")
    def test_release_channel(self, mock_logger: MagicMock) -> None:
        trunk = SIPTrunk("t1", "Primary", "sip.example.com", "user", "pass")
        trunk.register()
        trunk.allocate_channel()
        assert trunk.channels_in_use == 1
        trunk.release_channel()
        assert trunk.channels_in_use == 0

    @patch("pbx.features.sip_trunk.get_logger")
    def test_release_channel_does_not_go_negative(self, mock_logger: MagicMock) -> None:
        trunk = SIPTrunk("t1", "Primary", "sip.example.com", "user", "pass")
        trunk.release_channel()
        assert trunk.channels_in_use == 0


@pytest.mark.unit
class TestSIPTrunkCallRecording:
    """Tests for recording successful/failed calls."""

    @patch("pbx.features.sip_trunk.get_logger")
    def test_record_successful_call_no_setup_time(self, mock_logger: MagicMock) -> None:
        trunk = SIPTrunk("t1", "Primary", "sip.example.com", "user", "pass")
        trunk.consecutive_failures = 3
        trunk.record_successful_call()
        assert trunk.successful_calls == 1
        assert trunk.consecutive_failures == 0
        assert trunk.last_successful_call is not None

    @patch("pbx.features.sip_trunk.get_logger")
    def test_record_successful_call_with_setup_time(self, mock_logger: MagicMock) -> None:
        trunk = SIPTrunk("t1", "Primary", "sip.example.com", "user", "pass")
        trunk.record_successful_call(setup_time=1.5)
        assert trunk.call_setup_times == [1.5]
        assert trunk.average_call_setup_time == 1.5

    @patch("pbx.features.sip_trunk.get_logger")
    def test_record_successful_call_trims_setup_times_to_100(self, mock_logger: MagicMock) -> None:
        trunk = SIPTrunk("t1", "Primary", "sip.example.com", "user", "pass")
        # Fill with 100 entries
        trunk.call_setup_times = [1.0] * 100
        trunk.record_successful_call(setup_time=2.0)
        assert len(trunk.call_setup_times) == 100
        assert trunk.call_setup_times[-1] == 2.0
        assert trunk.call_setup_times[0] == 1.0  # first old one was popped, second stays

    @patch("pbx.features.sip_trunk.get_logger")
    def test_record_failed_call(self, mock_logger: MagicMock) -> None:
        trunk = SIPTrunk("t1", "Primary", "sip.example.com", "user", "pass")
        trunk.record_failed_call(reason="Timeout")
        assert trunk.failed_calls == 1
        assert trunk.consecutive_failures == 1
        assert trunk.last_failed_call is not None

    @patch("pbx.features.sip_trunk.get_logger")
    def test_record_failed_call_no_reason(self, mock_logger: MagicMock) -> None:
        trunk = SIPTrunk("t1", "Primary", "sip.example.com", "user", "pass")
        trunk.record_failed_call()
        assert trunk.failed_calls == 1

    @patch("pbx.features.sip_trunk.get_logger")
    def test_trunk_marked_failed_after_5_consecutive_failures(self, mock_logger: MagicMock) -> None:
        trunk = SIPTrunk("t1", "Primary", "sip.example.com", "user", "pass")
        trunk.register()
        for _ in range(5):
            trunk.record_failed_call(reason="Error")
        assert trunk.status == TrunkStatus.FAILED
        assert trunk.health_status == TrunkHealthStatus.DOWN


@pytest.mark.unit
class TestSIPTrunkHealthStatus:
    """Tests for _update_health_status."""

    @patch("pbx.features.sip_trunk.get_logger")
    def test_update_health_not_enough_data(self, mock_logger: MagicMock) -> None:
        trunk = SIPTrunk("t1", "Primary", "sip.example.com", "user", "pass")
        trunk.total_calls = 5
        trunk.successful_calls = 5
        trunk._update_health_status()
        # Should not change, not enough data (< 10)
        assert trunk.health_status == TrunkHealthStatus.DOWN

    @patch("pbx.features.sip_trunk.get_logger")
    def test_update_health_healthy(self, mock_logger: MagicMock) -> None:
        trunk = SIPTrunk("t1", "Primary", "sip.example.com", "user", "pass")
        trunk.total_calls = 100
        trunk.successful_calls = 96
        trunk._update_health_status()
        assert trunk.health_status == TrunkHealthStatus.HEALTHY

    @patch("pbx.features.sip_trunk.get_logger")
    def test_update_health_warning(self, mock_logger: MagicMock) -> None:
        trunk = SIPTrunk("t1", "Primary", "sip.example.com", "user", "pass")
        trunk.total_calls = 100
        trunk.successful_calls = 85
        trunk._update_health_status()
        assert trunk.health_status == TrunkHealthStatus.WARNING

    @patch("pbx.features.sip_trunk.get_logger")
    def test_update_health_critical(self, mock_logger: MagicMock) -> None:
        trunk = SIPTrunk("t1", "Primary", "sip.example.com", "user", "pass")
        trunk.total_calls = 100
        trunk.successful_calls = 60
        trunk._update_health_status()
        assert trunk.health_status == TrunkHealthStatus.CRITICAL

    @patch("pbx.features.sip_trunk.get_logger")
    def test_update_health_down(self, mock_logger: MagicMock) -> None:
        trunk = SIPTrunk("t1", "Primary", "sip.example.com", "user", "pass")
        trunk.total_calls = 100
        trunk.successful_calls = 40
        trunk._update_health_status()
        assert trunk.health_status == TrunkHealthStatus.DOWN


@pytest.mark.unit
class TestSIPTrunkCheckHealth:
    """Tests for SIPTrunk.check_health."""

    @patch("pbx.features.sip_trunk.get_logger")
    def test_check_health_unregistered(self, mock_logger: MagicMock) -> None:
        trunk = SIPTrunk("t1", "Primary", "sip.example.com", "user", "pass")
        result = trunk.check_health()
        assert result == TrunkHealthStatus.DOWN
        assert trunk.last_health_check is not None

    @patch("pbx.features.sip_trunk.get_logger")
    def test_check_health_registered_no_failures(self, mock_logger: MagicMock) -> None:
        trunk = SIPTrunk("t1", "Primary", "sip.example.com", "user", "pass")
        trunk.register()
        result = trunk.check_health()
        assert result == TrunkHealthStatus.HEALTHY

    @patch("pbx.features.sip_trunk.get_logger")
    def test_check_health_5_consecutive_failures(self, mock_logger: MagicMock) -> None:
        trunk = SIPTrunk("t1", "Primary", "sip.example.com", "user", "pass")
        trunk.status = TrunkStatus.REGISTERED
        trunk.health_status = TrunkHealthStatus.HEALTHY
        trunk.consecutive_failures = 5
        result = trunk.check_health()
        assert result == TrunkHealthStatus.DOWN

    @patch("pbx.features.sip_trunk.get_logger")
    def test_check_health_3_consecutive_failures(self, mock_logger: MagicMock) -> None:
        trunk = SIPTrunk("t1", "Primary", "sip.example.com", "user", "pass")
        trunk.status = TrunkStatus.REGISTERED
        trunk.health_status = TrunkHealthStatus.HEALTHY
        trunk.consecutive_failures = 3
        result = trunk.check_health()
        assert result == TrunkHealthStatus.CRITICAL

    @patch("pbx.features.sip_trunk.get_logger")
    def test_check_health_1_consecutive_failure(self, mock_logger: MagicMock) -> None:
        trunk = SIPTrunk("t1", "Primary", "sip.example.com", "user", "pass")
        trunk.status = TrunkStatus.REGISTERED
        trunk.health_status = TrunkHealthStatus.HEALTHY
        trunk.consecutive_failures = 1
        result = trunk.check_health()
        assert result == TrunkHealthStatus.WARNING

    @patch("pbx.features.sip_trunk.get_logger")
    def test_check_health_stale_last_success(self, mock_logger: MagicMock) -> None:
        trunk = SIPTrunk("t1", "Primary", "sip.example.com", "user", "pass")
        trunk.status = TrunkStatus.REGISTERED
        trunk.health_status = TrunkHealthStatus.HEALTHY
        trunk.last_successful_call = datetime.now(UTC) - timedelta(hours=2)
        result = trunk.check_health()
        assert result == TrunkHealthStatus.CRITICAL


@pytest.mark.unit
class TestSIPTrunkMetrics:
    """Tests for success rate and health metrics."""

    @patch("pbx.features.sip_trunk.get_logger")
    def test_get_success_rate_no_calls(self, mock_logger: MagicMock) -> None:
        trunk = SIPTrunk("t1", "Primary", "sip.example.com", "user", "pass")
        assert trunk.get_success_rate() == 0.0

    @patch("pbx.features.sip_trunk.get_logger")
    def test_get_success_rate_with_calls(self, mock_logger: MagicMock) -> None:
        trunk = SIPTrunk("t1", "Primary", "sip.example.com", "user", "pass")
        trunk.total_calls = 10
        trunk.successful_calls = 8
        assert trunk.get_success_rate() == 0.8

    @patch("pbx.features.sip_trunk.get_logger")
    def test_get_health_metrics(self, mock_logger: MagicMock) -> None:
        trunk = SIPTrunk("t1", "Primary", "sip.example.com", "user", "pass")
        trunk.total_calls = 5
        trunk.successful_calls = 4
        trunk.failed_calls = 1
        trunk.consecutive_failures = 1
        trunk.average_call_setup_time = 0.5
        metrics = trunk.get_health_metrics()
        assert metrics["health_status"] == "down"
        assert metrics["total_calls"] == 5
        assert metrics["successful_calls"] == 4
        assert metrics["failed_calls"] == 1
        assert metrics["success_rate"] == 0.8
        assert metrics["consecutive_failures"] == 1
        assert metrics["average_setup_time"] == 0.5
        assert metrics["last_health_check"] is None
        assert metrics["last_successful_call"] is None
        assert metrics["last_failed_call"] is None
        assert metrics["failover_count"] == 0

    @patch("pbx.features.sip_trunk.get_logger")
    def test_get_health_metrics_with_timestamps(self, mock_logger: MagicMock) -> None:
        trunk = SIPTrunk("t1", "Primary", "sip.example.com", "user", "pass")
        now = datetime.now(UTC)
        trunk.last_health_check = now
        trunk.last_successful_call = now
        trunk.last_failed_call = now
        metrics = trunk.get_health_metrics()
        assert metrics["last_health_check"] == now.isoformat()
        assert metrics["last_successful_call"] == now.isoformat()
        assert metrics["last_failed_call"] == now.isoformat()


@pytest.mark.unit
class TestSIPTrunkToDict:
    """Tests for SIPTrunk.to_dict."""

    @patch("pbx.features.sip_trunk.get_logger")
    def test_to_dict(self, mock_logger: MagicMock) -> None:
        trunk = SIPTrunk("t1", "Primary", "sip.example.com", "user", "pass", port=5061)
        d = trunk.to_dict()
        assert d["trunk_id"] == "t1"
        assert d["name"] == "Primary"
        assert d["host"] == "sip.example.com"
        assert d["port"] == 5061
        assert d["username"] == "user"
        assert d["status"] == "unregistered"
        assert d["health_status"] == "down"
        assert d["priority"] == 100
        assert d["max_channels"] == 10
        assert d["channels_available"] == 10
        assert d["channels_in_use"] == 0
        assert d["codec_preferences"] == ["0", "8", "9", "18", "2"]
        assert d["success_rate"] == 0.0
        assert d["consecutive_failures"] == 0
        assert d["total_calls"] == 0
        assert d["successful_calls"] == 0
        assert d["failed_calls"] == 0


# ---------------------------------------------------------------------------
# OutboundRule
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOutboundRule:
    """Tests for the OutboundRule class."""

    def test_init(self) -> None:
        rule = OutboundRule("r1", r"^1\d{10}$", "t1", prepend="9", strip=1)
        assert rule.rule_id == "r1"
        assert rule.pattern == r"^1\d{10}$"
        assert rule.trunk_id == "t1"
        assert rule.prepend == "9"
        assert rule.strip == 1

    def test_init_defaults(self) -> None:
        rule = OutboundRule("r1", r"^1\d{10}$", "t1")
        assert rule.prepend == ""
        assert rule.strip == 0

    def test_matches_positive(self) -> None:
        rule = OutboundRule("r1", r"^1\d{10}$", "t1")
        assert rule.matches("12125551234") is True

    def test_matches_negative(self) -> None:
        rule = OutboundRule("r1", r"^1\d{10}$", "t1")
        assert rule.matches("5551234") is False

    def test_matches_local(self) -> None:
        rule = OutboundRule("r1", r"^\d{7}$", "t1")
        assert rule.matches("5551234") is True

    def test_transform_number_strip_only(self) -> None:
        rule = OutboundRule("r1", r"^9\d+$", "t1", strip=1)
        result = rule.transform_number("912125551234")
        assert result == "12125551234"

    def test_transform_number_prepend_only(self) -> None:
        rule = OutboundRule("r1", r"^\d+$", "t1", prepend="1")
        result = rule.transform_number("2125551234")
        assert result == "12125551234"

    def test_transform_number_strip_and_prepend(self) -> None:
        rule = OutboundRule("r1", r"^9\d+$", "t1", prepend="1", strip=1)
        result = rule.transform_number("92125551234")
        assert result == "12125551234"

    def test_transform_number_no_transform(self) -> None:
        rule = OutboundRule("r1", r"^\d+$", "t1")
        result = rule.transform_number("12125551234")
        assert result == "12125551234"


# ---------------------------------------------------------------------------
# SIPTrunkSystem
# ---------------------------------------------------------------------------


def _make_trunk(trunk_id: str = "t1", name: str = "Primary", priority: int = 100) -> SIPTrunk:
    """Helper to create a SIPTrunk with mocked logger."""
    with patch("pbx.features.sip_trunk.get_logger"):
        return SIPTrunk(trunk_id, name, "sip.example.com", "user", "pass", priority=priority)


@pytest.mark.unit
class TestSIPTrunkSystemInit:
    """Tests for SIPTrunkSystem initialization."""

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_init_defaults(self, mock_logger: MagicMock, mock_e911: MagicMock) -> None:
        system = SIPTrunkSystem()
        assert system.trunks == {}
        assert system.outbound_rules == []
        assert system.health_check_enabled is True
        assert system.health_check_thread is None
        assert system.health_check_interval == 60
        assert system.monitoring_active is False
        assert system.failover_enabled is True
        assert system.auto_recovery_enabled is True
        assert system.failover_threshold == 3

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_init_with_config(self, mock_logger: MagicMock, mock_e911: MagicMock) -> None:
        config = {"some_key": "some_value"}
        _system = SIPTrunkSystem(config=config)
        mock_e911.assert_called_once_with(config)


@pytest.mark.unit
class TestSIPTrunkSystemTrunkManagement:
    """Tests for add/remove/get trunk."""

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_add_trunk(self, mock_logger: MagicMock, mock_e911: MagicMock) -> None:
        system = SIPTrunkSystem()
        trunk = _make_trunk("t1", "Primary")
        system.add_trunk(trunk)
        assert "t1" in system.trunks
        assert system.trunks["t1"] is trunk

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_get_trunk_exists(self, mock_logger: MagicMock, mock_e911: MagicMock) -> None:
        system = SIPTrunkSystem()
        trunk = _make_trunk("t1", "Primary")
        system.add_trunk(trunk)
        assert system.get_trunk("t1") is trunk

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_get_trunk_not_found(self, mock_logger: MagicMock, mock_e911: MagicMock) -> None:
        system = SIPTrunkSystem()
        assert system.get_trunk("nonexistent") is None

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_remove_trunk(self, mock_logger: MagicMock, mock_e911: MagicMock) -> None:
        system = SIPTrunkSystem()
        trunk = _make_trunk("t1", "Primary")
        trunk.register()
        system.add_trunk(trunk)
        system.remove_trunk("t1")
        assert "t1" not in system.trunks
        assert trunk.status == TrunkStatus.UNREGISTERED

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_remove_trunk_nonexistent(self, mock_logger: MagicMock, mock_e911: MagicMock) -> None:
        system = SIPTrunkSystem()
        # Should not raise
        system.remove_trunk("nonexistent")

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_register_all(self, mock_logger: MagicMock, mock_e911: MagicMock) -> None:
        system = SIPTrunkSystem()
        t1 = _make_trunk("t1", "Primary")
        t2 = _make_trunk("t2", "Secondary")
        system.add_trunk(t1)
        system.add_trunk(t2)
        system.register_all()
        assert t1.status == TrunkStatus.REGISTERED
        assert t2.status == TrunkStatus.REGISTERED


@pytest.mark.unit
class TestSIPTrunkSystemOutboundRules:
    """Tests for outbound rule management."""

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_add_outbound_rule(self, mock_logger: MagicMock, mock_e911: MagicMock) -> None:
        system = SIPTrunkSystem()
        rule = OutboundRule("r1", r"^\d+$", "t1")
        system.add_outbound_rule(rule)
        assert len(system.outbound_rules) == 1
        assert system.outbound_rules[0] is rule


@pytest.mark.unit
class TestSIPTrunkSystemRouting:
    """Tests for route_outbound."""

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_route_outbound_success(self, mock_logger: MagicMock, mock_e911: MagicMock) -> None:
        mock_e911.return_value.block_if_e911.return_value = False
        system = SIPTrunkSystem()
        trunk = _make_trunk("t1", "Primary")
        trunk.register()
        system.add_trunk(trunk)
        rule = OutboundRule("r1", r"^1\d{10}$", "t1", prepend="", strip=0)
        system.add_outbound_rule(rule)
        result_trunk, transformed = system.route_outbound("12125551234")
        assert result_trunk is trunk
        assert transformed == "12125551234"

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_route_outbound_no_match(self, mock_logger: MagicMock, mock_e911: MagicMock) -> None:
        mock_e911.return_value.block_if_e911.return_value = False
        system = SIPTrunkSystem()
        trunk = _make_trunk("t1", "Primary")
        trunk.register()
        system.add_trunk(trunk)
        rule = OutboundRule("r1", r"^1\d{10}$", "t1")
        system.add_outbound_rule(rule)
        result_trunk, transformed = system.route_outbound("555")
        assert result_trunk is None
        assert transformed is None

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_route_outbound_trunk_unavailable(
        self, mock_logger: MagicMock, mock_e911: MagicMock
    ) -> None:
        mock_e911.return_value.block_if_e911.return_value = False
        system = SIPTrunkSystem()
        trunk = _make_trunk("t1", "Primary")
        # trunk not registered, so can't make call
        system.add_trunk(trunk)
        rule = OutboundRule("r1", r"^\d+$", "t1")
        system.add_outbound_rule(rule)
        result_trunk, transformed = system.route_outbound("12125551234")
        assert result_trunk is None
        assert transformed is None

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_route_outbound_e911_blocked(
        self, mock_logger: MagicMock, mock_e911: MagicMock
    ) -> None:
        mock_e911.return_value.block_if_e911.return_value = True
        system = SIPTrunkSystem()
        result_trunk, transformed = system.route_outbound("911")
        assert result_trunk is None
        assert transformed is None

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_route_outbound_rule_trunk_missing(
        self, mock_logger: MagicMock, mock_e911: MagicMock
    ) -> None:
        """Rule references a trunk_id that doesn't exist in system."""
        mock_e911.return_value.block_if_e911.return_value = False
        system = SIPTrunkSystem()
        rule = OutboundRule("r1", r"^\d+$", "missing_trunk")
        system.add_outbound_rule(rule)
        result_trunk, transformed = system.route_outbound("12125551234")
        assert result_trunk is None
        assert transformed is None


@pytest.mark.unit
class TestSIPTrunkSystemGetTrunkStatus:
    """Tests for get_trunk_status."""

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_get_trunk_status_empty(self, mock_logger: MagicMock, mock_e911: MagicMock) -> None:
        system = SIPTrunkSystem()
        assert system.get_trunk_status() == []

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_get_trunk_status_with_trunks(
        self, mock_logger: MagicMock, mock_e911: MagicMock
    ) -> None:
        system = SIPTrunkSystem()
        system.add_trunk(_make_trunk("t1", "Primary"))
        system.add_trunk(_make_trunk("t2", "Secondary"))
        status = system.get_trunk_status()
        assert len(status) == 2
        ids = {s["trunk_id"] for s in status}
        assert ids == {"t1", "t2"}


@pytest.mark.unit
class TestSIPTrunkSystemHealthMonitoring:
    """Tests for health monitoring start/stop and health checks."""

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_start_health_monitoring(self, mock_logger: MagicMock, mock_e911: MagicMock) -> None:
        system = SIPTrunkSystem()
        system.health_check_interval = 0.01  # Short interval for test
        system.start_health_monitoring()
        assert system.monitoring_active is True
        assert system.health_check_thread is not None
        system.stop_health_monitoring()

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_start_health_monitoring_already_active(
        self, mock_logger: MagicMock, mock_e911: MagicMock
    ) -> None:
        system = SIPTrunkSystem()
        system.monitoring_active = True
        system.start_health_monitoring()
        # Should not create a second thread
        assert system.health_check_thread is None

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_stop_health_monitoring(self, mock_logger: MagicMock, mock_e911: MagicMock) -> None:
        system = SIPTrunkSystem()
        system.monitoring_active = True
        mock_thread = MagicMock()
        system.health_check_thread = mock_thread
        system.stop_health_monitoring()
        assert system.monitoring_active is False
        mock_thread.join.assert_called_once_with(timeout=5)

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_stop_health_monitoring_no_thread(
        self, mock_logger: MagicMock, mock_e911: MagicMock
    ) -> None:
        system = SIPTrunkSystem()
        system.stop_health_monitoring()
        assert system.monitoring_active is False

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_perform_health_checks_no_status_change(
        self, mock_logger: MagicMock, mock_e911: MagicMock
    ) -> None:
        system = SIPTrunkSystem()
        trunk = _make_trunk("t1", "Primary")
        trunk.register()
        system.add_trunk(trunk)
        system._perform_health_checks()
        # Trunk was healthy, still healthy
        assert trunk.health_status == TrunkHealthStatus.HEALTHY

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_perform_health_checks_triggers_failover(
        self, mock_logger: MagicMock, mock_e911: MagicMock
    ) -> None:
        system = SIPTrunkSystem()
        trunk = _make_trunk("t1", "Primary")
        trunk.status = TrunkStatus.REGISTERED
        trunk.health_status = TrunkHealthStatus.HEALTHY
        trunk.consecutive_failures = 6  # Will cause DOWN status
        system.add_trunk(trunk)
        system._perform_health_checks()
        assert trunk.health_status == TrunkHealthStatus.DOWN

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_perform_health_checks_triggers_recovery(
        self, mock_logger: MagicMock, mock_e911: MagicMock
    ) -> None:
        system = SIPTrunkSystem()
        trunk = MagicMock()
        trunk.name = "Primary"
        trunk.trunk_id = "t1"
        trunk.health_status = TrunkHealthStatus.DOWN
        trunk.check_health.return_value = TrunkHealthStatus.HEALTHY
        system.trunks["t1"] = trunk
        system.outbound_rules.append(OutboundRule("r1", r"^\d+$", "t2"))
        system.original_rule_trunk_ids["r1"] = "t1"

        system._perform_health_checks()

        assert system.outbound_rules[0].trunk_id == "t1"
        assert system.original_rule_trunk_ids == {}

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_perform_health_checks_handles_exception(
        self, mock_logger: MagicMock, mock_e911: MagicMock
    ) -> None:
        system = SIPTrunkSystem()
        trunk = MagicMock()
        trunk.name = "Broken"
        trunk.health_status = TrunkHealthStatus.HEALTHY
        trunk.check_health.side_effect = RuntimeError("check failed")
        system.trunks["t1"] = trunk
        # Should not raise
        system._perform_health_checks()

    @patch("pbx.features.sip_trunk.time.sleep", side_effect=[None, StopIteration])
    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_health_monitoring_loop(
        self, mock_logger: MagicMock, mock_e911: MagicMock, mock_sleep: MagicMock
    ) -> None:
        system = SIPTrunkSystem()
        system.monitoring_active = True

        # Make it stop after 1 iteration
        def stop_after_one(*args, **kwargs):
            system.monitoring_active = False

        with patch.object(system, "_perform_health_checks", side_effect=stop_after_one):
            system._health_monitoring_loop()

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_health_monitoring_loop_handles_exception(
        self, mock_logger: MagicMock, mock_e911: MagicMock
    ) -> None:
        system = SIPTrunkSystem()
        system.monitoring_active = True
        call_count = 0

        def perform_checks_then_stop():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("health check error")
            system.monitoring_active = False

        with (
            patch.object(system, "_perform_health_checks", side_effect=perform_checks_then_stop),
            patch("pbx.features.sip_trunk.time.sleep"),
        ):
            system._health_monitoring_loop()
        assert call_count == 2

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_health_monitoring_loop_also_runs_reregistration_checks(
        self, mock_logger: MagicMock, mock_e911: MagicMock
    ) -> None:
        system = SIPTrunkSystem()
        system.monitoring_active = True

        def stop_after_one(*args, **kwargs):
            system.monitoring_active = False

        with (
            patch.object(system, "_perform_health_checks"),
            patch.object(
                system, "_perform_reregistration_checks", side_effect=stop_after_one
            ) as mock_rereg,
            patch("pbx.features.sip_trunk.time.sleep"),
        ):
            system._health_monitoring_loop()

        mock_rereg.assert_called_once()


@pytest.mark.unit
class TestSIPTrunkSystemReregistrationChecks:
    """Tests for _perform_reregistration_checks()."""

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_reregisters_trunk_when_due(self, mock_logger: MagicMock, mock_e911: MagicMock) -> None:
        system = SIPTrunkSystem()
        trunk = MagicMock()
        trunk.name = "Due Trunk"
        trunk.needs_reregistration.return_value = True
        system.trunks["t1"] = trunk

        system._perform_reregistration_checks()

        trunk.register.assert_called_once_with(system.sip_server)

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_does_not_reregister_trunk_when_not_due(
        self, mock_logger: MagicMock, mock_e911: MagicMock
    ) -> None:
        system = SIPTrunkSystem()
        trunk = MagicMock()
        trunk.needs_reregistration.return_value = False
        system.trunks["t1"] = trunk

        system._perform_reregistration_checks()

        trunk.register.assert_not_called()

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_handles_exception_gracefully(
        self, mock_logger: MagicMock, mock_e911: MagicMock
    ) -> None:
        system = SIPTrunkSystem()
        trunk = MagicMock()
        trunk.name = "Broken"
        trunk.needs_reregistration.side_effect = RuntimeError("boom")
        system.trunks["t1"] = trunk

        # Should not raise
        system._perform_reregistration_checks()

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_passes_sip_server_through(self, mock_logger: MagicMock, mock_e911: MagicMock) -> None:
        mock_sip_server = MagicMock()
        system = SIPTrunkSystem(sip_server=mock_sip_server)
        trunk = MagicMock()
        trunk.needs_reregistration.return_value = True
        system.trunks["t1"] = trunk

        system._perform_reregistration_checks()

        trunk.register.assert_called_once_with(mock_sip_server)


@pytest.mark.unit
class TestSIPTrunkSystemFailover:
    """Tests for failover handling."""

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_handle_trunk_failure_with_alternative(
        self, mock_logger: MagicMock, mock_e911: MagicMock
    ) -> None:
        system = SIPTrunkSystem()
        failed = _make_trunk("t1", "Primary", priority=100)
        failed.register()
        failed.status = TrunkStatus.FAILED
        failed.health_status = TrunkHealthStatus.DOWN

        alt = _make_trunk("t2", "Secondary", priority=50)
        alt.register()

        system.add_trunk(failed)
        system.add_trunk(alt)

        rule = OutboundRule("r1", r"^\d+$", "t1")
        system.add_outbound_rule(rule)

        system._handle_trunk_failure(failed)
        assert failed.failover_count == 1
        assert failed.last_failover_time is not None
        # Rule re-pointed to the failover trunk, original remembered
        assert rule.trunk_id == "t2"
        assert system.original_rule_trunk_ids == {"r1": "t1"}

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_handle_trunk_failure_no_affected_rules(
        self, mock_logger: MagicMock, mock_e911: MagicMock
    ) -> None:
        system = SIPTrunkSystem()
        failed = _make_trunk("t1", "Primary")
        system.add_trunk(failed)
        # No rules point to t1 (outbound_rules is empty) -> re-pointing is a no-op
        system._handle_trunk_failure(failed)
        assert failed.failover_count == 1
        assert system.original_rule_trunk_ids == {}

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_handle_trunk_recovery_restores_original_rule(
        self, mock_logger: MagicMock, mock_e911: MagicMock
    ) -> None:
        system = SIPTrunkSystem()
        failed = _make_trunk("t1", "Primary", priority=100)
        failed.register()
        failed.status = TrunkStatus.FAILED
        failed.health_status = TrunkHealthStatus.DOWN

        alt = _make_trunk("t2", "Secondary", priority=50)
        alt.register()

        system.add_trunk(failed)
        system.add_trunk(alt)

        rule = OutboundRule("r1", r"^\d+$", "t1")
        system.add_outbound_rule(rule)

        system._handle_trunk_failure(failed)
        assert rule.trunk_id == "t2"

        # t1 comes back healthy
        failed.status = TrunkStatus.REGISTERED
        failed.health_status = TrunkHealthStatus.HEALTHY
        system._handle_trunk_recovery(failed)

        assert rule.trunk_id == "t1"
        assert system.original_rule_trunk_ids == {}

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_handle_trunk_recovery_no_repointed_rules(
        self, mock_logger: MagicMock, mock_e911: MagicMock
    ) -> None:
        system = SIPTrunkSystem()
        recovered = _make_trunk("t1", "Primary")
        recovered.register()
        # Nothing in original_rule_trunk_ids -> no-op, no crash
        system._handle_trunk_recovery(recovered)
        assert system.original_rule_trunk_ids == {}

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_handle_trunk_failure_no_alternatives(
        self, mock_logger: MagicMock, mock_e911: MagicMock
    ) -> None:
        system = SIPTrunkSystem()
        failed = _make_trunk("t1", "Primary")
        failed.status = TrunkStatus.FAILED
        failed.health_status = TrunkHealthStatus.DOWN
        system.add_trunk(failed)
        rule = OutboundRule("r1", r"^\d+$", "t1")
        system.add_outbound_rule(rule)
        system._handle_trunk_failure(failed)
        # No crash, failover count is incremented
        assert failed.failover_count == 1

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_get_available_trunks_by_priority(
        self, mock_logger: MagicMock, mock_e911: MagicMock
    ) -> None:
        system = SIPTrunkSystem()
        t1 = _make_trunk("t1", "Low Priority", priority=200)
        t1.register()
        t2 = _make_trunk("t2", "High Priority", priority=50)
        t2.register()
        t3 = _make_trunk("t3", "Not Registered", priority=10)
        # t3 not registered
        system.add_trunk(t1)
        system.add_trunk(t2)
        system.add_trunk(t3)
        available = system._get_available_trunks_by_priority()
        assert len(available) == 2
        assert available[0].trunk_id == "t2"
        assert available[1].trunk_id == "t1"


@pytest.mark.unit
class TestSIPTrunkSystemRouteWithFailover:
    """Tests for route_outbound_with_failover."""

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_route_with_failover_primary_available(
        self, mock_logger: MagicMock, mock_e911: MagicMock
    ) -> None:
        mock_e911.return_value.block_if_e911.return_value = False
        system = SIPTrunkSystem()
        trunk = _make_trunk("t1", "Primary")
        trunk.register()
        system.add_trunk(trunk)
        rule = OutboundRule("r1", r"^\d+$", "t1")
        system.add_outbound_rule(rule)
        result_trunk, transformed = system.route_outbound_with_failover("12125551234")
        assert result_trunk is trunk
        assert transformed == "12125551234"

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_route_with_failover_to_alt(self, mock_logger: MagicMock, mock_e911: MagicMock) -> None:
        mock_e911.return_value.block_if_e911.return_value = False
        system = SIPTrunkSystem()
        primary = _make_trunk("t1", "Primary", priority=100)
        primary.status = TrunkStatus.REGISTERED
        primary.health_status = TrunkHealthStatus.DOWN  # Can't make call

        alt = _make_trunk("t2", "Secondary", priority=50)
        alt.register()

        system.add_trunk(primary)
        system.add_trunk(alt)
        rule = OutboundRule("r1", r"^\d+$", "t1")
        system.add_outbound_rule(rule)

        result_trunk, transformed = system.route_outbound_with_failover("12125551234")
        assert result_trunk is alt
        assert transformed == "12125551234"

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_route_with_failover_disabled(
        self, mock_logger: MagicMock, mock_e911: MagicMock
    ) -> None:
        mock_e911.return_value.block_if_e911.return_value = False
        system = SIPTrunkSystem()
        system.failover_enabled = False
        primary = _make_trunk("t1", "Primary")
        primary.status = TrunkStatus.REGISTERED
        primary.health_status = TrunkHealthStatus.DOWN

        system.add_trunk(primary)
        rule = OutboundRule("r1", r"^\d+$", "t1")
        system.add_outbound_rule(rule)

        result_trunk, transformed = system.route_outbound_with_failover("12125551234")
        assert result_trunk is None
        assert transformed is None

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_route_with_failover_no_alt_available(
        self, mock_logger: MagicMock, mock_e911: MagicMock
    ) -> None:
        mock_e911.return_value.block_if_e911.return_value = False
        system = SIPTrunkSystem()
        primary = _make_trunk("t1", "Primary")
        primary.status = TrunkStatus.REGISTERED
        primary.health_status = TrunkHealthStatus.DOWN

        system.add_trunk(primary)
        rule = OutboundRule("r1", r"^\d+$", "t1")
        system.add_outbound_rule(rule)

        result_trunk, transformed = system.route_outbound_with_failover("12125551234")
        assert result_trunk is None
        assert transformed is None

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_route_with_failover_e911_blocked(
        self, mock_logger: MagicMock, mock_e911: MagicMock
    ) -> None:
        mock_e911.return_value.block_if_e911.return_value = True
        system = SIPTrunkSystem()
        result_trunk, transformed = system.route_outbound_with_failover("911")
        assert result_trunk is None
        assert transformed is None

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_route_with_failover_no_matching_rule(
        self, mock_logger: MagicMock, mock_e911: MagicMock
    ) -> None:
        mock_e911.return_value.block_if_e911.return_value = False
        system = SIPTrunkSystem()
        rule = OutboundRule("r1", r"^1\d{10}$", "t1")
        system.add_outbound_rule(rule)
        result_trunk, transformed = system.route_outbound_with_failover("555")
        assert result_trunk is None
        assert transformed is None

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_route_with_failover_trunk_id_missing(
        self, mock_logger: MagicMock, mock_e911: MagicMock
    ) -> None:
        """Rule references nonexistent trunk_id -- trunk is None, no failover possible."""
        mock_e911.return_value.block_if_e911.return_value = False
        system = SIPTrunkSystem()
        rule = OutboundRule("r1", r"^\d+$", "missing")
        system.add_outbound_rule(rule)
        result_trunk, transformed = system.route_outbound_with_failover("12125551234")
        assert result_trunk is None
        assert transformed is None


@pytest.mark.unit
class TestSIPTrunkSystemRouteWithFailoverNoRulesFallback:
    """Tests for the no-outbound-rules priority fallback in route_outbound_with_failover."""

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_single_trunk_no_rules_routes_unmodified(
        self, mock_logger: MagicMock, mock_e911: MagicMock
    ) -> None:
        mock_e911.return_value.block_if_e911.return_value = False
        system = SIPTrunkSystem()
        trunk = _make_trunk("t1", "Primary")
        trunk.register()
        system.add_trunk(trunk)

        result_trunk, transformed = system.route_outbound_with_failover("12125551234")
        assert result_trunk is trunk
        assert transformed == "12125551234"

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_multiple_trunks_no_rules_picks_highest_priority(
        self, mock_logger: MagicMock, mock_e911: MagicMock
    ) -> None:
        mock_e911.return_value.block_if_e911.return_value = False
        system = SIPTrunkSystem()
        low_priority = _make_trunk("t1", "Secondary", priority=100)
        low_priority.register()
        high_priority = _make_trunk("t2", "Primary", priority=10)
        high_priority.register()
        system.add_trunk(low_priority)
        system.add_trunk(high_priority)

        result_trunk, transformed = system.route_outbound_with_failover("12125551234")
        assert result_trunk is high_priority
        assert transformed == "12125551234"

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_multiple_trunks_no_rules_skips_unusable_trunk(
        self, mock_logger: MagicMock, mock_e911: MagicMock
    ) -> None:
        """Highest-priority trunk is unhealthy/full -- falls through to the next one."""
        mock_e911.return_value.block_if_e911.return_value = False
        system = SIPTrunkSystem()
        unusable = _make_trunk("t1", "Primary", priority=10)
        unusable.status = TrunkStatus.REGISTERED
        unusable.health_status = TrunkHealthStatus.DOWN
        usable = _make_trunk("t2", "Secondary", priority=100)
        usable.register()
        system.add_trunk(unusable)
        system.add_trunk(usable)

        result_trunk, transformed = system.route_outbound_with_failover("12125551234")
        assert result_trunk is usable
        assert transformed == "12125551234"

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_no_rules_no_trunks_returns_none(
        self, mock_logger: MagicMock, mock_e911: MagicMock
    ) -> None:
        mock_e911.return_value.block_if_e911.return_value = False
        system = SIPTrunkSystem()

        result_trunk, transformed = system.route_outbound_with_failover("12125551234")
        assert result_trunk is None
        assert transformed is None

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_no_rules_e911_still_blocked(
        self, mock_logger: MagicMock, mock_e911: MagicMock
    ) -> None:
        mock_e911.return_value.block_if_e911.return_value = True
        system = SIPTrunkSystem()
        trunk = _make_trunk("t1", "Primary")
        trunk.register()
        system.add_trunk(trunk)

        result_trunk, transformed = system.route_outbound_with_failover("911")
        assert result_trunk is None
        assert transformed is None

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_rules_defined_but_unmatched_does_not_use_fallback(
        self, mock_logger: MagicMock, mock_e911: MagicMock
    ) -> None:
        """Once any rule exists, an unmatched number is 'no route', not a fallback trunk."""
        mock_e911.return_value.block_if_e911.return_value = False
        system = SIPTrunkSystem()
        trunk = _make_trunk("t1", "Primary")
        trunk.register()
        system.add_trunk(trunk)
        system.add_outbound_rule(OutboundRule("r1", r"^1\d{10}$", "t1"))

        result_trunk, transformed = system.route_outbound_with_failover("555")
        assert result_trunk is None
        assert transformed is None


@pytest.mark.unit
class TestSIPTrunkSystemFindFailoverTrunk:
    """Tests for _find_failover_trunk."""

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_find_failover_trunk_success(
        self, mock_logger: MagicMock, mock_e911: MagicMock
    ) -> None:
        system = SIPTrunkSystem()
        primary = _make_trunk("t1", "Primary", priority=100)
        alt = _make_trunk("t2", "Secondary", priority=50)
        alt.register()
        system.add_trunk(primary)
        system.add_trunk(alt)

        result = system._find_failover_trunk(primary)
        assert result is alt

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_find_failover_trunk_none_available(
        self, mock_logger: MagicMock, mock_e911: MagicMock
    ) -> None:
        system = SIPTrunkSystem()
        primary = _make_trunk("t1", "Primary")
        system.add_trunk(primary)
        result = system._find_failover_trunk(primary)
        assert result is None

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_find_failover_excludes_primary(
        self, mock_logger: MagicMock, mock_e911: MagicMock
    ) -> None:
        """Even if primary is registered/healthy, it should be excluded from failover."""
        system = SIPTrunkSystem()
        primary = _make_trunk("t1", "Primary")
        primary.register()
        system.add_trunk(primary)
        result = system._find_failover_trunk(primary)
        assert result is None

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_find_failover_skips_full_trunk_picks_next(
        self, mock_logger: MagicMock, mock_e911: MagicMock
    ) -> None:
        """A REGISTERED/HEALTHY trunk that's at max_channels must be skipped
        in favor of the next-best trunk that actually has room."""
        system = SIPTrunkSystem()
        primary = _make_trunk("t1", "Primary", priority=100)

        full_alt = _make_trunk("t2", "Full Secondary", priority=10)
        full_alt.register()
        full_alt.channels_in_use = full_alt.max_channels

        roomy_alt = _make_trunk("t3", "Roomy Tertiary", priority=20)
        roomy_alt.register()

        system.add_trunk(primary)
        system.add_trunk(full_alt)
        system.add_trunk(roomy_alt)

        result = system._find_failover_trunk(primary)
        assert result is roomy_alt

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_find_failover_none_if_only_alternative_is_full(
        self, mock_logger: MagicMock, mock_e911: MagicMock
    ) -> None:
        system = SIPTrunkSystem()
        primary = _make_trunk("t1", "Primary", priority=100)

        full_alt = _make_trunk("t2", "Full Secondary", priority=10)
        full_alt.register()
        full_alt.channels_in_use = full_alt.max_channels

        system.add_trunk(primary)
        system.add_trunk(full_alt)

        result = system._find_failover_trunk(primary)
        assert result is None


@pytest.mark.unit
class TestSIPTrunkSystemHealthSummary:
    """Tests for get_trunk_health_summary."""

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_health_summary_empty(self, mock_logger: MagicMock, mock_e911: MagicMock) -> None:
        system = SIPTrunkSystem()
        summary = system.get_trunk_health_summary()
        assert summary["total_trunks"] == 0
        assert summary["healthy"] == 0
        assert summary["warning"] == 0
        assert summary["critical"] == 0
        assert summary["down"] == 0
        assert summary["total_calls"] == 0
        assert summary["overall_success_rate"] == 0.0
        assert summary["trunks"] == []

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_health_summary_with_trunks(self, mock_logger: MagicMock, mock_e911: MagicMock) -> None:
        system = SIPTrunkSystem()
        t1 = _make_trunk("t1", "Healthy Trunk")
        t1.register()
        t1.total_calls = 10
        t1.successful_calls = 9
        t1.failed_calls = 1

        t2 = _make_trunk("t2", "Warning Trunk")
        t2.register()
        t2.health_status = TrunkHealthStatus.WARNING
        t2.total_calls = 5
        t2.successful_calls = 4
        t2.failed_calls = 1

        t3 = _make_trunk("t3", "Critical Trunk")
        t3.register()
        t3.health_status = TrunkHealthStatus.CRITICAL
        t3.total_calls = 3
        t3.successful_calls = 1
        t3.failed_calls = 2

        t4 = _make_trunk("t4", "Down Trunk")
        # Not registered, health_status = DOWN by default

        system.add_trunk(t1)
        system.add_trunk(t2)
        system.add_trunk(t3)
        system.add_trunk(t4)

        summary = system.get_trunk_health_summary()
        assert summary["total_trunks"] == 4
        assert summary["healthy"] == 1
        assert summary["warning"] == 1
        assert summary["critical"] == 1
        assert summary["down"] == 1
        assert summary["total_calls"] == 18
        assert summary["successful_calls"] == 14
        assert summary["failed_calls"] == 4
        assert summary["overall_success_rate"] == 14 / 18
        assert len(summary["trunks"]) == 4


# ---------------------------------------------------------------------------
# get_trunk_manager global function
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGetTrunkManager:
    """Tests for the get_trunk_manager global function."""

    def setup_method(self) -> None:
        """Reset global _trunk_manager before each test."""
        import pbx.features.sip_trunk as module

        module._trunk_manager = None

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_create_new_manager(self, mock_logger: MagicMock, mock_e911: MagicMock) -> None:
        manager = get_trunk_manager(config={"key": "value"})
        assert manager is not None
        assert isinstance(manager, SIPTrunkSystem)

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_returns_existing_manager(self, mock_logger: MagicMock, mock_e911: MagicMock) -> None:
        m1 = get_trunk_manager(config={"key": "value"})
        m2 = get_trunk_manager()
        assert m1 is m2

    @patch("pbx.features.sip_trunk.E911Protection")
    @patch("pbx.features.sip_trunk.get_logger")
    def test_returns_none_without_config(
        self, mock_logger: MagicMock, mock_e911: MagicMock
    ) -> None:
        manager = get_trunk_manager()
        assert manager is None

    def teardown_method(self) -> None:
        """Reset global state."""
        import pbx.features.sip_trunk as module

        module._trunk_manager = None
