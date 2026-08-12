"""Comprehensive tests for pbx/core/call_router.py - Call routing logic."""

import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest


@pytest.fixture(autouse=True)
def _no_retransmit_timers():
    """Keep INVITE retransmission timer chains from leaking past each test.

    Routing tests start real InviteClientTransactions whose timer-A chain
    keeps re-scheduling after the test ends; if it fires while another test
    has threading.Thread/Timer patched globally, the leaked thread raises
    and pytest fails that unrelated test.
    """
    with patch("pbx.sip.transaction.InviteClientTransaction._schedule_timer_a"):
        yield


from pbx.core.call_router import CallRouter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pbx_core(
    *,
    config_overrides: dict[str, Any] | None = None,
    extension_registered: bool = True,
    dialplan: dict[str, str] | None = None,
) -> MagicMock:
    """Build a comprehensive mock PBXCore."""
    pbx = MagicMock()

    # Config mock with side_effect-based get
    config_data: dict[str, Any] = {
        "server.sip_port": 5060,
        "voicemail.no_answer_timeout": 30,
        "voicemail.max_message_duration": 180,
        "sip.caller_id.send_p_asserted_identity": True,
        "sip.caller_id.send_remote_party_id": True,
        "sip.device.send_mac_address": True,
        "sip.device.accept_mac_in_invite": True,
    }
    if dialplan is not None:
        config_data["dialplan"] = dialplan
    if config_overrides:
        config_data.update(config_overrides)

    def config_get(key: str, default: Any = None) -> Any:
        return config_data.get(key, default)

    pbx.config = MagicMock()
    pbx.config.get.side_effect = config_get

    # Logger
    pbx.logger = MagicMock()

    # Extension registry
    pbx.extension_registry = MagicMock()
    pbx.extension_registry.is_registered.return_value = extension_registered

    dest_ext = MagicMock()
    dest_ext.address = ("10.0.0.2", 5060)
    dest_ext.name = "Test User"
    dest_ext.registered = extension_registered
    dest_ext.is_expired.return_value = False
    pbx.extension_registry.get.return_value = dest_ext

    # Kari's law
    pbx.karis_law = MagicMock()
    pbx.karis_law.is_emergency_number.return_value = False

    # Auto attendant
    pbx.auto_attendant = MagicMock()
    pbx.auto_attendant.get_extension.return_value = "0"

    # Paging system
    pbx.paging_system = MagicMock()
    pbx.paging_system.is_paging_extension.return_value = False

    # Trunk system -- default to "this call didn't arrive from a trunk", so
    # existing tests keep exercising the internal-dispatch path. Tests for
    # inbound-DID routing override get_trunk_by_addr's return value.
    pbx.trunk_system = MagicMock()
    pbx.trunk_system.get_trunk_by_addr.return_value = None

    # Call manager
    mock_call = MagicMock()
    mock_call.start_time = MagicMock()
    mock_call.start_time.isoformat.return_value = "2026-01-01T00:00:00+00:00"
    mock_call.rtp_ports = None
    mock_call.caller_rtp = None
    mock_call.caller_addr = None
    mock_call.callee_addr = None
    mock_call.original_invite = None
    mock_call.no_answer_timer = None
    mock_call.routed_to_voicemail = False
    mock_call.transfer_session_id = None
    mock_call.from_extension = "1001"
    mock_call.to_extension = "1002"
    pbx.call_manager = MagicMock()
    pbx.call_manager.create_call.return_value = mock_call
    pbx.call_manager.get_call.return_value = mock_call
    pbx.call_manager.get_extension_calls.return_value = []

    # CDR
    pbx.cdr_system = MagicMock()

    # Webhook
    pbx.webhook_system = MagicMock()

    # RTP relay
    pbx.rtp_relay = MagicMock()
    pbx.rtp_relay.allocate_relay.return_value = (20000, 20001)
    pbx.rtp_relay.active_relays = {}

    # SIP server
    pbx.sip_server = MagicMock()

    # Server IP
    pbx._get_server_ip.return_value = "10.0.0.1"

    # Phone model detection
    pbx._get_phone_user_agent.return_value = "Generic/1.0"
    pbx._detect_phone_model.return_value = None
    pbx._get_codecs_for_phone_model.return_value = ["PCMU", "PCMA"]

    # DTMF / iLBC config
    pbx._get_dtmf_payload_type.return_value = 101
    pbx._get_ilbc_mode.return_value = 30

    # Registered phones DB
    pbx.registered_phones_db = MagicMock()
    pbx.registered_phones_db.get_by_extension.return_value = []

    # WebRTC
    pbx.webrtc_gateway = None

    # Voicemail handler and system
    pbx.voicemail_handler = MagicMock()
    pbx.emergency_handler = MagicMock()
    pbx.auto_attendant_handler = MagicMock()
    pbx.paging_handler = MagicMock()
    pbx.voicemail_system = MagicMock()

    # Queue handler: no queues configured, never divert
    pbx.queue_handler.is_queue_destination.return_value = False

    return pbx


def _make_invite_message(
    *,
    from_ext: str = "1001",
    to_ext: str = "1002",
    call_id: str = "test-call-id-123",
    body: str = "",
    via: str = "SIP/2.0/UDP 10.0.0.1:5060;branch=z9hG4bK776",
    cseq: str = "1 INVITE",
    extra_headers: dict[str, str] | None = None,
) -> MagicMock:
    """Build a mock SIP INVITE message."""
    msg = MagicMock()
    msg.body = body

    headers: dict[str, str] = {
        "From": f"<sip:{from_ext}@pbx.local>;tag=abc123",
        "To": f"<sip:{to_ext}@pbx.local>",
        "Call-ID": call_id,
        "CSeq": cseq,
        "Via": via,
    }
    if extra_headers:
        headers.update(extra_headers)

    msg.get_header.side_effect = headers.get
    msg.uri = f"sip:{to_ext}@pbx.local"
    return msg


CALLER_ADDR = ("192.168.1.100", 5060)


# ===========================================================================
# CallRouter.__init__
# ===========================================================================


@pytest.mark.unit
class TestCallRouterInit:
    """Tests for CallRouter initialization."""

    def test_init_stores_pbx_core(self) -> None:
        pbx = MagicMock()
        router = CallRouter(pbx)
        assert router.pbx_core is pbx


# ===========================================================================
# CallRouter._check_dialplan
# ===========================================================================


@pytest.mark.unit
class TestCheckDialplan:
    """Tests for _check_dialplan() method."""

    def test_emergency_pattern_911(self) -> None:
        pbx = _make_pbx_core(dialplan={})
        router = CallRouter(pbx)
        assert router._check_dialplan("911") is True

    def test_emergency_pattern_9911(self) -> None:
        pbx = _make_pbx_core(dialplan={})
        router = CallRouter(pbx)
        assert router._check_dialplan("9911") is True

    def test_internal_extension_1001(self) -> None:
        pbx = _make_pbx_core(dialplan={})
        router = CallRouter(pbx)
        assert router._check_dialplan("1001") is True

    def test_internal_extension_1999(self) -> None:
        pbx = _make_pbx_core(dialplan={})
        router = CallRouter(pbx)
        assert router._check_dialplan("1999") is True

    def test_conference_extension_2001(self) -> None:
        pbx = _make_pbx_core(dialplan={})
        router = CallRouter(pbx)
        assert router._check_dialplan("2001") is True

    def test_voicemail_pattern_star_1001(self) -> None:
        pbx = _make_pbx_core(dialplan={})
        router = CallRouter(pbx)
        assert router._check_dialplan("*1001") is True

    def test_voicemail_pattern_star_3digits(self) -> None:
        pbx = _make_pbx_core(dialplan={})
        router = CallRouter(pbx)
        assert router._check_dialplan("*100") is True

    def test_auto_attendant_pattern_0(self) -> None:
        pbx = _make_pbx_core(dialplan={})
        router = CallRouter(pbx)
        assert router._check_dialplan("0") is True

    def test_parking_pattern_70(self) -> None:
        pbx = _make_pbx_core(dialplan={})
        router = CallRouter(pbx)
        assert router._check_dialplan("70") is True

    def test_parking_pattern_79(self) -> None:
        pbx = _make_pbx_core(dialplan={})
        router = CallRouter(pbx)
        assert router._check_dialplan("79") is True

    def test_queue_pattern_8001(self) -> None:
        pbx = _make_pbx_core(dialplan={})
        router = CallRouter(pbx)
        assert router._check_dialplan("8001") is True

    def test_invalid_extension_rejected(self) -> None:
        pbx = _make_pbx_core(dialplan={})
        router = CallRouter(pbx)
        assert router._check_dialplan("5555") is False

    def test_invalid_short_extension_rejected(self) -> None:
        pbx = _make_pbx_core(dialplan={})
        router = CallRouter(pbx)
        assert router._check_dialplan("99") is False

    def test_custom_internal_pattern(self) -> None:
        pbx = _make_pbx_core(dialplan={"internal_pattern": "^3[0-9]{3}$"})
        router = CallRouter(pbx)
        assert router._check_dialplan("3001") is True
        assert router._check_dialplan("1001") is False

    def test_empty_dialplan_uses_defaults(self) -> None:
        pbx = _make_pbx_core(dialplan={})
        router = CallRouter(pbx)
        assert router._check_dialplan("1001") is True

    def test_no_dialplan_config(self) -> None:
        pbx = _make_pbx_core()
        router = CallRouter(pbx)
        assert router._check_dialplan("1001") is True


# ===========================================================================
# CallRouter.route_call - header parsing
# ===========================================================================


@pytest.mark.unit
class TestRouteCallHeaderParsing:
    """Tests for route_call() SIP header parsing."""

    def test_invalid_from_header_returns_false(self) -> None:
        pbx = _make_pbx_core()
        router = CallRouter(pbx)

        result = router.route_call(
            "invalid-no-sip-uri",
            "<sip:1002@pbx.local>",
            "call-1",
            _make_invite_message(),
            CALLER_ADDR,
        )
        assert result is False

    def test_invalid_to_header_returns_false(self) -> None:
        pbx = _make_pbx_core()
        router = CallRouter(pbx)

        result = router.route_call(
            "<sip:1001@pbx.local>",
            "invalid-no-sip-uri",
            "call-1",
            _make_invite_message(),
            CALLER_ADDR,
        )
        assert result is False


# ===========================================================================
# CallRouter.route_call - emergency calls
# ===========================================================================


@pytest.mark.unit
class TestRouteCallEmergency:
    """Tests for emergency call routing."""

    def test_emergency_call_routed_to_handler(self) -> None:
        pbx = _make_pbx_core()
        pbx.karis_law.is_emergency_number.return_value = True
        pbx.emergency_handler.handle_emergency_call.return_value = True

        router = CallRouter(pbx)
        result = router.route_call(
            "<sip:1001@pbx.local>",
            "<sip:911@pbx.local>",
            "call-911",
            _make_invite_message(to_ext="911"),
            CALLER_ADDR,
        )

        assert result is True
        pbx.emergency_handler.handle_emergency_call.assert_called_once()


# ===========================================================================
# CallRouter.route_call - auto attendant
# ===========================================================================


@pytest.mark.unit
class TestRouteCallAutoAttendant:
    """Tests for auto attendant routing."""

    def test_auto_attendant_call_routed(self) -> None:
        pbx = _make_pbx_core()
        pbx.auto_attendant.get_extension.return_value = "0"
        pbx.auto_attendant_handler.handle_auto_attendant.return_value = True

        router = CallRouter(pbx)
        result = router.route_call(
            "<sip:1001@pbx.local>",
            "<sip:0@pbx.local>",
            "call-aa",
            _make_invite_message(to_ext="0"),
            CALLER_ADDR,
        )

        assert result is True
        pbx.auto_attendant_handler.handle_auto_attendant.assert_called_once()


# ===========================================================================
# CallRouter.route_call - voicemail access
# ===========================================================================


@pytest.mark.unit
class TestRouteCallVoicemailAccess:
    """Tests for voicemail access (*xxxx) routing."""

    def test_voicemail_access_star_4digits(self) -> None:
        pbx = _make_pbx_core()
        pbx.voicemail_handler.handle_voicemail_access.return_value = True

        router = CallRouter(pbx)
        result = router.route_call(
            "<sip:1001@pbx.local>",
            "<sip:*1002@pbx.local>",
            "call-vm",
            _make_invite_message(to_ext="*1002"),
            CALLER_ADDR,
        )

        assert result is True
        pbx.voicemail_handler.handle_voicemail_access.assert_called_once()

    def test_voicemail_access_star_3digits(self) -> None:
        pbx = _make_pbx_core()
        pbx.voicemail_handler.handle_voicemail_access.return_value = True

        router = CallRouter(pbx)
        result = router.route_call(
            "<sip:1001@pbx.local>",
            "<sip:*100@pbx.local>",
            "call-vm",
            _make_invite_message(to_ext="*100"),
            CALLER_ADDR,
        )

        assert result is True
        pbx.voicemail_handler.handle_voicemail_access.assert_called_once()

    def test_voicemail_access_too_short_not_routed(self) -> None:
        """*12 is too short (len < 4), not voicemail access."""
        pbx = _make_pbx_core()
        router = CallRouter(pbx)

        # *12 won't match voicemail pattern, will go through normal routing
        # which may fail at dialplan or extension check
        router.route_call(
            "<sip:1001@pbx.local>",
            "<sip:*12@pbx.local>",
            "call-vm",
            _make_invite_message(to_ext="*12"),
            CALLER_ADDR,
        )

        pbx.voicemail_handler.handle_voicemail_access.assert_not_called()

    def test_voicemail_access_too_long_not_routed(self) -> None:
        """*12345 is too long (len > 5), not voicemail access."""
        pbx = _make_pbx_core()
        router = CallRouter(pbx)

        router.route_call(
            "<sip:1001@pbx.local>",
            "<sip:*12345@pbx.local>",
            "call-vm",
            _make_invite_message(to_ext="*12345"),
            CALLER_ADDR,
        )

        pbx.voicemail_handler.handle_voicemail_access.assert_not_called()


# ===========================================================================
# CallRouter.route_call - paging
# ===========================================================================


@pytest.mark.unit
class TestRouteCallPaging:
    """Tests for paging system routing."""

    def test_paging_call_routed(self) -> None:
        pbx = _make_pbx_core()
        pbx.paging_system.is_paging_extension.return_value = True
        pbx.paging_handler.handle_paging.return_value = True

        router = CallRouter(pbx)
        result = router.route_call(
            "<sip:1001@pbx.local>",
            "<sip:700@pbx.local>",
            "call-page",
            _make_invite_message(to_ext="700"),
            CALLER_ADDR,
        )

        assert result is True
        pbx.paging_handler.handle_paging.assert_called_once()


# ===========================================================================
# CallRouter.route_call - extension not registered
# ===========================================================================


@pytest.mark.unit
class TestRouteCallUnregistered:
    """Tests for unregistered extension handling."""

    def test_unregistered_extension_returns_false(self) -> None:
        pbx = _make_pbx_core(extension_registered=False)
        # Ensure get() returns an unregistered extension mock
        unregistered_ext = MagicMock()
        unregistered_ext.registered = False
        unregistered_ext.address = None
        unregistered_ext.is_expired.return_value = True
        pbx.extension_registry.get.return_value = unregistered_ext

        router = CallRouter(pbx)

        result = router.route_call(
            "<sip:1001@pbx.local>",
            "<sip:1002@pbx.local>",
            "call-1",
            _make_invite_message(),
            CALLER_ADDR,
        )

        assert result is False
        pbx.logger.warning.assert_called()


# ===========================================================================
# CallRouter.route_call - dialplan check
# ===========================================================================


@pytest.mark.unit
class TestRouteCallDialplanCheck:
    """Tests for dialplan check in route_call."""

    def test_dialplan_rejected_returns_false(self) -> None:
        pbx = _make_pbx_core(
            dialplan={"internal_pattern": "^3[0-9]{3}$"},
        )
        # Extension 1002 does not match 3xxx pattern
        router = CallRouter(pbx)

        result = router.route_call(
            "<sip:1001@pbx.local>",
            "<sip:1002@pbx.local>",
            "call-1",
            _make_invite_message(),
            CALLER_ADDR,
        )

        assert result is False


# ===========================================================================
# CallRouter.route_call - SDP parsing
# ===========================================================================


@pytest.mark.unit
class TestRouteCallSDP:
    """Tests for SDP parsing in route_call."""

    @patch("pbx.sip.sdp.SDPBuilder.build_audio_sdp", return_value="v=0\r\n")
    @patch("pbx.sip.sdp.SDPSession")
    def test_caller_sdp_parsed_with_body(
        self, mock_sdp_cls: MagicMock, mock_build_sdp: MagicMock
    ) -> None:
        pbx = _make_pbx_core()

        sdp_obj = MagicMock()
        sdp_obj.get_audio_info.return_value = {
            "address": "192.168.1.100",
            "port": 30000,
            "formats": ["PCMU", "PCMA"],
        }
        mock_sdp_cls.return_value = sdp_obj

        router = CallRouter(pbx)
        msg = _make_invite_message(body="v=0\r\no=- 0 0 IN IP4 192.168.1.100")

        result = router.route_call(
            "<sip:1001@pbx.local>",
            "<sip:1002@pbx.local>",
            "call-1",
            msg,
            CALLER_ADDR,
        )

        assert result is True

    def test_no_sdp_body_still_routes(self) -> None:
        pbx = _make_pbx_core()
        router = CallRouter(pbx)
        msg = _make_invite_message(body="")

        result = router.route_call(
            "<sip:1001@pbx.local>",
            "<sip:1002@pbx.local>",
            "call-1",
            msg,
            CALLER_ADDR,
        )

        assert result is True

    @patch("pbx.sip.sdp.SDPBuilder.build_audio_sdp", return_value="v=0\r\n")
    @patch("pbx.sip.sdp.SDPSession")
    def test_sdp_with_no_audio_info(
        self, mock_sdp_cls: MagicMock, mock_build_sdp: MagicMock
    ) -> None:
        pbx = _make_pbx_core()

        sdp_obj = MagicMock()
        sdp_obj.get_audio_info.return_value = None
        mock_sdp_cls.return_value = sdp_obj

        router = CallRouter(pbx)
        msg = _make_invite_message(body="v=0\r\n")

        result = router.route_call(
            "<sip:1001@pbx.local>",
            "<sip:1002@pbx.local>",
            "call-1",
            msg,
            CALLER_ADDR,
        )

        assert result is True


# ===========================================================================
# CallRouter.route_call - RTP relay setup
# ===========================================================================


@pytest.mark.unit
class TestRouteCallRTPRelay:
    """Tests for RTP relay allocation and endpoint setup."""

    @patch("pbx.sip.sdp.SDPBuilder.build_audio_sdp", return_value="v=0\r\n")
    @patch("pbx.sip.sdp.SDPSession")
    def test_rtp_relay_allocated_and_endpoint_set(
        self, mock_sdp_cls: MagicMock, mock_build_sdp: MagicMock
    ) -> None:
        pbx = _make_pbx_core()

        sdp_obj = MagicMock()
        sdp_obj.get_audio_info.return_value = {
            "address": "192.168.1.100",
            "port": 30000,
            "formats": ["PCMU"],
        }
        mock_sdp_cls.return_value = sdp_obj

        # Setup active relay info
        mock_handler = MagicMock()
        pbx.rtp_relay.active_relays = {"call-1": {"handler": mock_handler}}

        router = CallRouter(pbx)
        msg = _make_invite_message(body="v=0\r\n")

        result = router.route_call(
            "<sip:1001@pbx.local>",
            "<sip:1002@pbx.local>",
            "call-1",
            msg,
            CALLER_ADDR,
        )

        assert result is True
        pbx.rtp_relay.allocate_relay.assert_called_once_with("call-1")
        mock_handler.set_endpoints.assert_called_once_with(("192.168.1.100", 30000), None)

    def test_rtp_relay_allocation_returns_none(self) -> None:
        pbx = _make_pbx_core()
        pbx.rtp_relay.allocate_relay.return_value = None

        router = CallRouter(pbx)
        msg = _make_invite_message(body="")

        result = router.route_call(
            "<sip:1001@pbx.local>",
            "<sip:1002@pbx.local>",
            "call-1",
            msg,
            CALLER_ADDR,
        )

        # RTP allocation failure now correctly returns False and cleans up
        assert result is False
        pbx.call_manager.end_call.assert_called_once_with("call-1")


# ===========================================================================
# CallRouter.route_call - destination extension handling
# ===========================================================================


@pytest.mark.unit
class TestRouteCallDestination:
    """Tests for destination extension address resolution."""

    def test_dest_ext_no_address_returns_false(self) -> None:
        pbx = _make_pbx_core()
        dest_ext = MagicMock()
        dest_ext.address = None
        dest_ext.registered = True
        dest_ext.is_expired.return_value = False
        pbx.extension_registry.get.return_value = dest_ext

        router = CallRouter(pbx)
        msg = _make_invite_message(body="")

        result = router.route_call(
            "<sip:1001@pbx.local>",
            "<sip:1002@pbx.local>",
            "call-1",
            msg,
            CALLER_ADDR,
        )

        assert result is False

    def test_dest_ext_not_found_returns_false(self) -> None:
        pbx = _make_pbx_core()
        pbx.extension_registry.get.return_value = None

        router = CallRouter(pbx)
        msg = _make_invite_message(body="")

        result = router.route_call(
            "<sip:1001@pbx.local>",
            "<sip:1002@pbx.local>",
            "call-1",
            msg,
            CALLER_ADDR,
        )

        assert result is False


# ===========================================================================
# CallRouter.route_call - WebRTC routing
# ===========================================================================


@pytest.mark.unit
class TestRouteCallWebRTC:
    """Tests for WebRTC destination routing."""

    def test_webrtc_destination_success(self) -> None:
        pbx = _make_pbx_core()
        dest_ext = MagicMock()
        dest_ext.address = ("webrtc", "session-abc")
        dest_ext.registered = True
        dest_ext.is_expired.return_value = False
        pbx.extension_registry.get.return_value = dest_ext
        pbx.webrtc_gateway = MagicMock()
        pbx.webrtc_gateway.receive_call.return_value = True

        router = CallRouter(pbx)
        msg = _make_invite_message(body="v=0\r\n")

        result = router.route_call(
            "<sip:1001@pbx.local>",
            "<sip:1002@pbx.local>",
            "call-1",
            msg,
            CALLER_ADDR,
        )

        assert result is True
        pbx.webrtc_gateway.receive_call.assert_called_once()

    def test_webrtc_destination_receive_call_fails(self) -> None:
        pbx = _make_pbx_core()
        dest_ext = MagicMock()
        dest_ext.address = ("webrtc", "session-abc")
        dest_ext.registered = True
        dest_ext.is_expired.return_value = False
        pbx.extension_registry.get.return_value = dest_ext
        pbx.webrtc_gateway = MagicMock()
        pbx.webrtc_gateway.receive_call.return_value = False

        router = CallRouter(pbx)
        msg = _make_invite_message(body="")

        result = router.route_call(
            "<sip:1001@pbx.local>",
            "<sip:1002@pbx.local>",
            "call-1",
            msg,
            CALLER_ADDR,
        )

        assert result is False

    def test_webrtc_destination_no_gateway(self) -> None:
        pbx = _make_pbx_core()
        dest_ext = MagicMock()
        dest_ext.address = ("webrtc", "session-abc")
        dest_ext.registered = True
        dest_ext.is_expired.return_value = False
        pbx.extension_registry.get.return_value = dest_ext
        pbx.webrtc_gateway = None

        router = CallRouter(pbx)
        msg = _make_invite_message(body="")

        result = router.route_call(
            "<sip:1001@pbx.local>",
            "<sip:1002@pbx.local>",
            "call-1",
            msg,
            CALLER_ADDR,
        )

        assert result is False


# ===========================================================================
# CallRouter.route_call - SIP INVITE forwarding
# ===========================================================================


@pytest.mark.unit
class TestRouteCallInviteForwarding:
    """Tests for INVITE forwarding to callee."""

    def test_invite_forwarded_to_callee(self) -> None:
        pbx = _make_pbx_core()
        router = CallRouter(pbx)
        msg = _make_invite_message(body="")

        result = router.route_call(
            "<sip:1001@pbx.local>",
            "<sip:1002@pbx.local>",
            "call-1",
            msg,
            CALLER_ADDR,
        )

        assert result is True
        # INVITE is sent via InviteClientTransaction (initial send)
        pbx.sip_server._send_message.assert_called()

    def test_caller_id_headers_added(self) -> None:
        pbx = _make_pbx_core()

        caller_ext = MagicMock()
        caller_ext.name = "John Doe"

        def get_ext(ext_num: str) -> MagicMock:
            if ext_num == "1001":
                return caller_ext
            dest = MagicMock()
            dest.address = ("10.0.0.2", 5060)
            dest.registered = True
            dest.is_expired.return_value = False
            return dest

        pbx.extension_registry.get.side_effect = get_ext

        router = CallRouter(pbx)
        msg = _make_invite_message(body="")

        router.route_call(
            "<sip:1001@pbx.local>",
            "<sip:1002@pbx.local>",
            "call-1",
            msg,
            CALLER_ADDR,
        )

        # The INVITE built by SIPMessageBuilder should have caller ID headers set
        # We verify via the sip_server._send_message being called (means forwarding happened)
        pbx.sip_server._send_message.assert_called()

    def test_caller_id_headers_skipped_when_disabled(self) -> None:
        pbx = _make_pbx_core(
            config_overrides={
                "sip.caller_id.send_p_asserted_identity": False,
                "sip.caller_id.send_remote_party_id": False,
            }
        )

        router = CallRouter(pbx)
        msg = _make_invite_message(body="")

        result = router.route_call(
            "<sip:1001@pbx.local>",
            "<sip:1002@pbx.local>",
            "call-1",
            msg,
            CALLER_ADDR,
        )

        assert result is True

    def test_caller_ext_with_empty_name_uses_extension_number(self) -> None:
        pbx = _make_pbx_core()

        caller_ext = MagicMock()
        caller_ext.name = ""

        def get_ext(ext_num: str) -> MagicMock:
            if ext_num == "1001":
                return caller_ext
            dest = MagicMock()
            dest.address = ("10.0.0.2", 5060)
            dest.registered = True
            dest.is_expired.return_value = False
            return dest

        pbx.extension_registry.get.side_effect = get_ext

        router = CallRouter(pbx)
        msg = _make_invite_message(body="")

        # Should not raise
        result = router.route_call(
            "<sip:1001@pbx.local>",
            "<sip:1002@pbx.local>",
            "call-1",
            msg,
            CALLER_ADDR,
        )

        assert result is True

    def test_caller_ext_not_found_uses_ext_number_as_name(self) -> None:
        pbx = _make_pbx_core()

        def get_ext(ext_num: str) -> MagicMock | None:
            if ext_num == "1001":
                return None
            dest = MagicMock()
            dest.address = ("10.0.0.2", 5060)
            dest.registered = True
            dest.is_expired.return_value = False
            return dest

        pbx.extension_registry.get.side_effect = get_ext

        router = CallRouter(pbx)
        msg = _make_invite_message(body="")

        result = router.route_call(
            "<sip:1001@pbx.local>",
            "<sip:1002@pbx.local>",
            "call-1",
            msg,
            CALLER_ADDR,
        )

        assert result is True


# ===========================================================================
# CallRouter.route_call - MAC address handling
# ===========================================================================


@pytest.mark.unit
class TestRouteCallMACAddress:
    """Tests for MAC address header handling."""

    def test_mac_from_registered_phones_db(self) -> None:
        pbx = _make_pbx_core()
        pbx.registered_phones_db.get_by_extension.return_value = [
            {"mac_address": "AA:BB:CC:DD:EE:FF"}
        ]

        router = CallRouter(pbx)
        msg = _make_invite_message(body="")

        router.route_call(
            "<sip:1001@pbx.local>",
            "<sip:1002@pbx.local>",
            "call-1",
            msg,
            CALLER_ADDR,
        )

        pbx.sip_server._send_message.assert_called_once()

    def test_mac_from_invite_x_mac_header(self) -> None:
        pbx = _make_pbx_core()
        pbx.registered_phones_db.get_by_extension.return_value = []

        router = CallRouter(pbx)
        msg = _make_invite_message(
            body="",
            extra_headers={"X-MAC-Address": "11:22:33:44:55:66"},
        )

        result = router.route_call(
            "<sip:1001@pbx.local>",
            "<sip:1002@pbx.local>",
            "call-1",
            msg,
            CALLER_ADDR,
        )

        assert result is True

    def test_mac_disabled_in_config(self) -> None:
        pbx = _make_pbx_core(
            config_overrides={
                "sip.device.send_mac_address": False,
            }
        )

        router = CallRouter(pbx)
        msg = _make_invite_message(body="")

        result = router.route_call(
            "<sip:1001@pbx.local>",
            "<sip:1002@pbx.local>",
            "call-1",
            msg,
            CALLER_ADDR,
        )

        assert result is True

    def test_mac_db_lookup_exception(self) -> None:
        pbx = _make_pbx_core()
        pbx.registered_phones_db.get_by_extension.side_effect = KeyError("db error")

        router = CallRouter(pbx)
        msg = _make_invite_message(
            body="",
            extra_headers={"X-MAC-Address": "AA:BB:CC:DD:EE:FF"},
        )

        # Should not raise
        result = router.route_call(
            "<sip:1001@pbx.local>",
            "<sip:1002@pbx.local>",
            "call-1",
            msg,
            CALLER_ADDR,
        )

        assert result is True

    def test_no_mac_available(self) -> None:
        pbx = _make_pbx_core()
        pbx.registered_phones_db.get_by_extension.return_value = []

        router = CallRouter(pbx)
        msg = _make_invite_message(body="")

        result = router.route_call(
            "<sip:1001@pbx.local>",
            "<sip:1002@pbx.local>",
            "call-1",
            msg,
            CALLER_ADDR,
        )

        assert result is True

    def test_mac_db_returns_empty_mac(self) -> None:
        pbx = _make_pbx_core()
        pbx.registered_phones_db.get_by_extension.return_value = [{"mac_address": None}]

        router = CallRouter(pbx)
        msg = _make_invite_message(body="")

        result = router.route_call(
            "<sip:1001@pbx.local>",
            "<sip:1002@pbx.local>",
            "call-1",
            msg,
            CALLER_ADDR,
        )

        assert result is True

    def test_mac_accept_from_invite_disabled(self) -> None:
        pbx = _make_pbx_core(
            config_overrides={
                "sip.device.accept_mac_in_invite": False,
            }
        )
        pbx.registered_phones_db.get_by_extension.return_value = []

        router = CallRouter(pbx)
        msg = _make_invite_message(
            body="",
            extra_headers={"X-MAC-Address": "11:22:33:44:55:66"},
        )

        result = router.route_call(
            "<sip:1001@pbx.local>",
            "<sip:1002@pbx.local>",
            "call-1",
            msg,
            CALLER_ADDR,
        )

        assert result is True


# ===========================================================================
# CallRouter.route_call - no-answer timer
# ===========================================================================


@pytest.mark.unit
class TestRouteCallNoAnswerTimer:
    """Tests for no-answer timer setup in route_call."""

    def test_no_answer_timer_started(self) -> None:
        pbx = _make_pbx_core()
        mock_call = pbx.call_manager.create_call.return_value

        router = CallRouter(pbx)
        msg = _make_invite_message(body="")

        result = router.route_call(
            "<sip:1001@pbx.local>",
            "<sip:1002@pbx.local>",
            "call-1",
            msg,
            CALLER_ADDR,
        )

        assert result is True
        # Verify the no_answer_timer was assigned on the call object
        assert mock_call.no_answer_timer is not None


@pytest.mark.unit
class TestRouteToTrunkNoAnswerTimer:
    """Tests that _route_to_trunk does NOT start a no-answer timer.

    Unlike route_call (internal calls), outbound trunk calls should ring
    until the caller hangs up or the trunk responds -- see the comment in
    _route_to_trunk just before its `return True`.
    """

    def test_no_timer_started_for_trunk_call(self) -> None:
        pbx = _make_pbx_core()
        mock_call = pbx.call_manager.create_call.return_value
        mock_call.no_answer_timer = None

        mock_trunk = MagicMock()
        mock_trunk.host = "trunk.example.com"
        mock_trunk.port = 5060
        mock_trunk.name = "Test Trunk"
        mock_trunk.codec_preferences = ["0", "8", "18"]
        mock_trunk.allocate_channel.return_value = True
        pbx.trunk_system = MagicMock()
        pbx.trunk_system.route_outbound_with_failover.return_value = (
            mock_trunk,
            "12125551234",
        )

        router = CallRouter(pbx)
        msg = _make_invite_message(to_ext="12125551234", body="")

        with patch("threading.Timer") as mock_timer_cls:
            result = router._route_to_trunk("1001", "12125551234", "call-1", msg, CALLER_ADDR)

            # InviteClientTransaction legitimately starts its own Timer A/B
            # (SIP retransmission, RFC 3261), so we can't assert Timer was
            # never called at all -- only that none of those calls target
            # _handle_no_answer (the removed PBX-side no-answer timer).
            no_answer_timer_calls = [
                c for c in mock_timer_cls.call_args_list if c.args[1] == router._handle_no_answer
            ]
            assert no_answer_timer_calls == []

        assert result is True
        assert mock_call.no_answer_timer is None


# ===========================================================================
# CallRouter._route_to_trunk - outbound caller ID
# ===========================================================================


def _trunk_pbx_with_extension(did_number: str | None) -> MagicMock:
    """Build a mock PBXCore for trunk-routing tests with a configurable caller extension."""
    pbx = _make_pbx_core()

    mock_trunk = MagicMock()
    mock_trunk.host = "trunk.example.com"
    mock_trunk.port = 5060
    mock_trunk.name = "Test Trunk"
    mock_trunk.codec_preferences = ["0", "8", "18"]
    mock_trunk.allocate_channel.return_value = True
    pbx.trunk_system = MagicMock()
    pbx.trunk_system.route_outbound_with_failover.return_value = (mock_trunk, "12125551234")

    caller_ext = MagicMock()
    caller_ext.name = "Jane Caller"
    caller_ext.config = {"did_number": did_number}
    pbx.extension_registry.get.return_value = caller_ext

    return pbx


@pytest.mark.unit
class TestRouteToTrunkCallerID:
    """Tests for outbound caller ID resolution in _route_to_trunk."""

    def test_uses_extension_did_when_set(self) -> None:
        pbx = _trunk_pbx_with_extension(did_number="19725550100")
        router = CallRouter(pbx)
        msg = _make_invite_message(to_ext="12125551234", body="")

        with patch("pbx.sip.message.SIPMessageBuilder.add_caller_id_headers") as mock_add_caller_id:
            result = router._route_to_trunk("1001", "12125551234", "call-1", msg, CALLER_ADDR)

        assert result is True
        _, number_arg, name_arg, _ = mock_add_caller_id.call_args[0]
        assert number_arg == "19725550100"
        assert name_arg == "Jane Caller"

    def test_falls_back_to_extension_number_without_did(self) -> None:
        pbx = _trunk_pbx_with_extension(did_number=None)
        router = CallRouter(pbx)
        msg = _make_invite_message(to_ext="12125551234", body="")

        with patch("pbx.sip.message.SIPMessageBuilder.add_caller_id_headers") as mock_add_caller_id:
            result = router._route_to_trunk("1001", "12125551234", "call-1", msg, CALLER_ADDR)

        assert result is True
        _, number_arg, name_arg, _ = mock_add_caller_id.call_args[0]
        assert number_arg == "1001"
        assert name_arg == "Jane Caller"

    def test_falls_back_to_extension_number_when_extension_unknown(self) -> None:
        pbx = _trunk_pbx_with_extension(did_number="19725550100")
        pbx.extension_registry.get.return_value = None
        router = CallRouter(pbx)
        msg = _make_invite_message(to_ext="12125551234", body="")

        with patch("pbx.sip.message.SIPMessageBuilder.add_caller_id_headers") as mock_add_caller_id:
            result = router._route_to_trunk("1001", "12125551234", "call-1", msg, CALLER_ADDR)

        assert result is True
        _, number_arg, name_arg, _ = mock_add_caller_id.call_args[0]
        assert number_arg == "1001"
        assert name_arg == "1001"

    def test_from_header_uses_extension_did(self) -> None:
        pbx = _trunk_pbx_with_extension(did_number="19725550100")
        router = CallRouter(pbx)
        msg = _make_invite_message(to_ext="12125551234", body="")

        captured: dict[str, Any] = {}

        def _capture(
            call: Any, call_id: Any, invite_request: Any, *args: Any, **kwargs: Any
        ) -> MagicMock:
            captured["from_header"] = invite_request.get_header("From")
            return MagicMock()

        with patch.object(router, "_build_and_send_leg_invite", side_effect=_capture):
            router._route_to_trunk("1001", "12125551234", "call-1", msg, CALLER_ADDR)

        assert "19725550100" in captured["from_header"]
        assert "1001" not in captured["from_header"]


# ===========================================================================
# CallRouter - inbound DID routing
# ===========================================================================


def _trunk_pbx_with_route(route: dict | None) -> MagicMock:
    """Build a mock PBXCore for a trunk-sourced call whose DID lookup returns `route`."""
    pbx = _make_pbx_core()

    mock_trunk = MagicMock()
    mock_trunk.trunk_id = "t1"
    pbx.trunk_system.get_trunk_by_addr.return_value = mock_trunk

    pbx.inbound_routing = MagicMock()
    pbx.inbound_routing.lookup.return_value = route

    return pbx


@pytest.mark.unit
class TestRouteCallInboundTrunkDetection:
    """Tests that route_call() recognizes a trunk-sourced INVITE and hands off to _route_inbound_did()."""

    def test_trunk_sourced_call_dispatched_to_inbound_did_routing(self) -> None:
        pbx = _trunk_pbx_with_route({"destination_type": "voicemail", "destination_value": "1001"})
        router = CallRouter(pbx)
        msg = _make_invite_message(body="")

        result = router.route_call(
            "<sip:19725550100@carrier.example.com>",
            "<sip:12125551234@pbx.local>",
            "call-1",
            msg,
            ("203.0.113.10", 5060),
        )

        assert result is True
        pbx.inbound_routing.lookup.assert_called_once_with("12125551234", "t1")
        pbx.voicemail_handler.handle_voicemail_access.assert_called_once_with(
            "19725550100", "*1001", "call-1", msg, ("203.0.113.10", 5060)
        )

    def test_trunk_sourced_call_bypasses_karis_law_check(self) -> None:
        """A trunk-sourced INVITE must not go through the internal emergency-number check."""
        pbx = _trunk_pbx_with_route({"destination_type": "voicemail", "destination_value": "1001"})
        router = CallRouter(pbx)
        msg = _make_invite_message(body="")

        router.route_call(
            "<sip:19725550100@carrier.example.com>",
            "<sip:12125551234@pbx.local>",
            "call-1",
            msg,
            ("203.0.113.10", 5060),
        )

        pbx.karis_law.is_emergency_number.assert_not_called()

    def test_non_trunk_call_unaffected(self) -> None:
        """A call with no matching trunk still goes through normal internal dispatch."""
        pbx = _make_pbx_core()
        router = CallRouter(pbx)
        msg = _make_invite_message(body="")

        result = router.route_call(
            "<sip:1001@pbx.local>", "<sip:1002@pbx.local>", "call-1", msg, CALLER_ADDR
        )

        assert result is True
        pbx.trunk_system.get_trunk_by_addr.assert_called_once_with(CALLER_ADDR)


@pytest.mark.unit
class TestRouteInboundDID:
    """Tests for CallRouter._route_inbound_did()."""

    def test_no_matching_route_returns_false(self) -> None:
        pbx = _trunk_pbx_with_route(None)
        router = CallRouter(pbx)
        mock_trunk = MagicMock(trunk_id="t1")
        msg = _make_invite_message(body="")

        result = router._route_inbound_did(
            mock_trunk,
            "19725550100",
            "12125551234",
            "<sip:19725550100@carrier>",
            "<sip:12125551234@pbx.local>",
            "call-1",
            msg,
            ("203.0.113.10", 5060),
        )

        assert result is False

    def test_extension_destination_rewrites_to_header_and_dials(self) -> None:
        pbx = _trunk_pbx_with_route({"destination_type": "extension", "destination_value": "1001"})
        router = CallRouter(pbx)
        mock_trunk = MagicMock(trunk_id="t1")
        msg = _make_invite_message(body="")

        with patch.object(router, "_dial_to_internal_extension", return_value=True) as mock_dial:
            result = router._route_inbound_did(
                mock_trunk,
                "19725550100",
                "12125551234",
                "<sip:19725550100@carrier>",
                "<sip:12125551234@pbx.local>",
                "call-1",
                msg,
                ("203.0.113.10", 5060),
            )

        assert result is True
        mock_dial.assert_called_once()
        args = mock_dial.call_args[0]
        assert args[0] == "19725550100"  # from_ext (carrier caller ID)
        assert args[1] == "1001"  # rewritten to the extension, not the dialed DID
        assert "sip:1001@" in args[3]  # rewritten to_header

    def test_auto_attendant_destination(self) -> None:
        pbx = _trunk_pbx_with_route(
            {"destination_type": "auto_attendant", "destination_value": "ignored"}
        )
        pbx.auto_attendant.get_extension.return_value = "0"
        router = CallRouter(pbx)
        mock_trunk = MagicMock(trunk_id="t1")
        msg = _make_invite_message(body="")

        result = router._route_inbound_did(
            mock_trunk,
            "19725550100",
            "12125551234",
            "<sip:19725550100@carrier>",
            "<sip:12125551234@pbx.local>",
            "call-1",
            msg,
            ("203.0.113.10", 5060),
        )

        assert result is True
        pbx.auto_attendant_handler.handle_auto_attendant.assert_called_once_with(
            "19725550100", "0", "call-1", msg, ("203.0.113.10", 5060)
        )

    def test_auto_attendant_destination_when_feature_disabled(self) -> None:
        pbx = _trunk_pbx_with_route(
            {"destination_type": "auto_attendant", "destination_value": "ignored"}
        )
        pbx.auto_attendant = None
        router = CallRouter(pbx)
        mock_trunk = MagicMock(trunk_id="t1")
        msg = _make_invite_message(body="")

        result = router._route_inbound_did(
            mock_trunk,
            "19725550100",
            "12125551234",
            "<sip:19725550100@carrier>",
            "<sip:12125551234@pbx.local>",
            "call-1",
            msg,
            ("203.0.113.10", 5060),
        )

        assert result is False
        pbx.auto_attendant_handler.handle_auto_attendant.assert_not_called()

    def test_voicemail_destination(self) -> None:
        pbx = _trunk_pbx_with_route({"destination_type": "voicemail", "destination_value": "1001"})
        router = CallRouter(pbx)
        mock_trunk = MagicMock(trunk_id="t1")
        msg = _make_invite_message(body="")

        result = router._route_inbound_did(
            mock_trunk,
            "19725550100",
            "12125551234",
            "<sip:19725550100@carrier>",
            "<sip:12125551234@pbx.local>",
            "call-1",
            msg,
            ("203.0.113.10", 5060),
        )

        assert result is True
        pbx.voicemail_handler.handle_voicemail_access.assert_called_once_with(
            "19725550100", "*1001", "call-1", msg, ("203.0.113.10", 5060)
        )

    def test_unknown_destination_type_returns_false(self) -> None:
        pbx = _trunk_pbx_with_route({"destination_type": "queue", "destination_value": "800"})
        router = CallRouter(pbx)
        mock_trunk = MagicMock(trunk_id="t1")
        msg = _make_invite_message(body="")

        result = router._route_inbound_did(
            mock_trunk,
            "19725550100",
            "12125551234",
            "<sip:19725550100@carrier>",
            "<sip:12125551234@pbx.local>",
            "call-1",
            msg,
            ("203.0.113.10", 5060),
        )

        assert result is False


# ===========================================================================
# CallRouter.route_call - CDR and webhooks
# ===========================================================================


@pytest.mark.unit
class TestRouteCallCDRWebhooks:
    """Tests for CDR and webhook triggering."""

    def test_cdr_record_started(self) -> None:
        pbx = _make_pbx_core()
        router = CallRouter(pbx)
        msg = _make_invite_message(body="")

        router.route_call(
            "<sip:1001@pbx.local>",
            "<sip:1002@pbx.local>",
            "call-1",
            msg,
            CALLER_ADDR,
        )

        pbx.cdr_system.start_record.assert_called_once_with("call-1", "1001", "1002")

    def test_webhook_triggered(self) -> None:
        pbx = _make_pbx_core()
        router = CallRouter(pbx)
        msg = _make_invite_message(body="")

        router.route_call(
            "<sip:1001@pbx.local>",
            "<sip:1002@pbx.local>",
            "call-1",
            msg,
            CALLER_ADDR,
        )

        pbx.webhook_system.trigger_event.assert_called_once()
        webhook_args = pbx.webhook_system.trigger_event.call_args
        from pbx.features.webhooks import WebhookEvent

        assert webhook_args[0][0] == WebhookEvent.CALL_STARTED


# ===========================================================================
# CallRouter._send_cancel_to_callee
# ===========================================================================


@pytest.mark.unit
class TestSendCancelToCallee:
    """Tests for _send_cancel_to_callee()."""

    def test_sends_cancel_to_callee(self) -> None:
        pbx = _make_pbx_core()

        mock_call = MagicMock()
        mock_call.callee_addr = ("10.0.0.2", 5060)
        mock_call.callee_invite = MagicMock()
        mock_call.callee_invite.uri = "sip:1002@10.0.0.1"
        _callee_headers = {
            "From": "<sip:1001@pbx.local>",
            "To": "<sip:1002@pbx.local>",
            "CSeq": "1 INVITE",
            "Via": "SIP/2.0/UDP 10.0.0.1:5060",
        }
        mock_call.callee_invite.get_header.side_effect = _callee_headers.get
        mock_call.to_extension = "1002"

        router = CallRouter(pbx)
        router._send_cancel_to_callee(mock_call, "call-1")

        pbx.sip_server._send_message.assert_called_once()

    def test_no_callee_addr_returns_early(self) -> None:
        pbx = _make_pbx_core()
        mock_call = MagicMock()
        mock_call.callee_addr = None
        mock_call.callee_invite = MagicMock()

        router = CallRouter(pbx)
        router._send_cancel_to_callee(mock_call, "call-1")

        pbx.sip_server._send_message.assert_not_called()

    def test_no_callee_invite_returns_early(self) -> None:
        pbx = _make_pbx_core()
        mock_call = MagicMock(spec=[])  # Empty spec so hasattr returns False
        mock_call.callee_addr = ("10.0.0.2", 5060)

        router = CallRouter(pbx)
        router._send_cancel_to_callee(mock_call, "call-1")

        pbx.sip_server._send_message.assert_not_called()


# ===========================================================================
# CallRouter._answer_call_for_voicemail
# ===========================================================================


@pytest.mark.unit
class TestAnswerCallForVoicemail:
    """Tests for _answer_call_for_voicemail()."""

    def test_answer_success(self) -> None:
        pbx = _make_pbx_core()

        mock_call = MagicMock()
        mock_call.original_invite = MagicMock()
        mock_call.caller_addr = CALLER_ADDR
        mock_call.caller_rtp = {"address": "192.168.1.100", "port": 30000, "formats": ["PCMU"]}
        mock_call.rtp_ports = (20000, 20001)
        mock_call.from_extension = "1001"
        mock_call.to_extension = "1002"

        router = CallRouter(pbx)
        result = router._answer_call_for_voicemail(mock_call, "call-1")

        assert result is True
        pbx.sip_server._send_message.assert_called_once()
        mock_call.connect.assert_called_once()

    def test_answer_missing_original_invite(self) -> None:
        pbx = _make_pbx_core()
        mock_call = MagicMock()
        mock_call.original_invite = None
        mock_call.caller_addr = CALLER_ADDR
        mock_call.caller_rtp = {"address": "192.168.1.100", "port": 30000}
        mock_call.rtp_ports = (20000, 20001)

        router = CallRouter(pbx)
        result = router._answer_call_for_voicemail(mock_call, "call-1")

        assert result is False

    def test_answer_missing_caller_addr(self) -> None:
        pbx = _make_pbx_core()
        mock_call = MagicMock()
        mock_call.original_invite = MagicMock()
        mock_call.caller_addr = None
        mock_call.caller_rtp = {"address": "192.168.1.100", "port": 30000}
        mock_call.rtp_ports = (20000, 20001)

        router = CallRouter(pbx)
        result = router._answer_call_for_voicemail(mock_call, "call-1")

        assert result is False

    def test_answer_missing_caller_rtp(self) -> None:
        pbx = _make_pbx_core()
        mock_call = MagicMock()
        mock_call.original_invite = MagicMock()
        mock_call.caller_addr = CALLER_ADDR
        mock_call.caller_rtp = None
        mock_call.rtp_ports = (20000, 20001)

        router = CallRouter(pbx)
        result = router._answer_call_for_voicemail(mock_call, "call-1")

        assert result is False

    def test_answer_missing_rtp_ports(self) -> None:
        pbx = _make_pbx_core()
        mock_call = MagicMock()
        mock_call.original_invite = MagicMock()
        mock_call.caller_addr = CALLER_ADDR
        mock_call.caller_rtp = {"address": "192.168.1.100", "port": 30000}
        mock_call.rtp_ports = None

        router = CallRouter(pbx)
        result = router._answer_call_for_voicemail(mock_call, "call-1")

        assert result is False


# ===========================================================================
# CallRouter._end_unanswered_trunk_call
# ===========================================================================


@pytest.mark.unit
class TestEndUnansweredTrunkCall:
    """Tests for _end_unanswered_trunk_call()."""

    def test_sends_480_and_releases_trunk_channel(self) -> None:
        pbx = _make_pbx_core()

        mock_trunk = MagicMock()
        mock_call = MagicMock()
        mock_call.original_invite = MagicMock()
        mock_call.caller_addr = CALLER_ADDR
        mock_call.trunk = mock_trunk

        router = CallRouter(pbx)
        router._end_unanswered_trunk_call(mock_call, "call-1")

        pbx.sip_server._send_message.assert_called_once()
        sent_message = pbx.sip_server._send_message.call_args[0][0]
        assert "480 Temporarily Unavailable" in sent_message

        mock_trunk.release_channel.assert_called_once()
        mock_trunk.record_failed_call.assert_called_once_with(reason="no answer")

        pbx.rtp_relay.release_relay.assert_called_once_with("call-1")
        pbx.cdr_system.end_record.assert_called_once_with("call-1", hangup_cause="no_answer")
        pbx.call_manager.end_call.assert_called_once_with("call-1")

    def test_no_original_invite_skips_response_but_still_cleans_up(self) -> None:
        pbx = _make_pbx_core()

        mock_trunk = MagicMock()
        mock_call = MagicMock()
        mock_call.original_invite = None
        mock_call.caller_addr = CALLER_ADDR
        mock_call.trunk = mock_trunk

        router = CallRouter(pbx)
        router._end_unanswered_trunk_call(mock_call, "call-1")

        pbx.sip_server._send_message.assert_not_called()
        mock_trunk.release_channel.assert_called_once()
        pbx.call_manager.end_call.assert_called_once_with("call-1")

    def test_no_caller_addr_skips_response_but_still_cleans_up(self) -> None:
        pbx = _make_pbx_core()

        mock_trunk = MagicMock()
        mock_call = MagicMock()
        mock_call.original_invite = MagicMock()
        mock_call.caller_addr = None
        mock_call.trunk = mock_trunk

        router = CallRouter(pbx)
        router._end_unanswered_trunk_call(mock_call, "call-1")

        pbx.sip_server._send_message.assert_not_called()
        mock_trunk.release_channel.assert_called_once()
        pbx.call_manager.end_call.assert_called_once_with("call-1")

    def test_no_trunk_still_cleans_up_without_error(self) -> None:
        """Defensive: if called on a call with no trunk, don't blow up trying
        to release one -- just clean up the call/RTP/CDR as usual."""
        pbx = _make_pbx_core()

        mock_call = MagicMock()
        mock_call.original_invite = MagicMock()
        mock_call.caller_addr = CALLER_ADDR
        mock_call.trunk = None

        router = CallRouter(pbx)
        router._end_unanswered_trunk_call(mock_call, "call-1")

        pbx.sip_server._send_message.assert_called_once()
        pbx.rtp_relay.release_relay.assert_called_once_with("call-1")
        pbx.cdr_system.end_record.assert_called_once_with("call-1", hangup_cause="no_answer")
        pbx.call_manager.end_call.assert_called_once_with("call-1")


# ===========================================================================
# CallRouter._handle_no_answer
# ===========================================================================


@pytest.mark.unit
class TestHandleNoAnswer:
    """Tests for _handle_no_answer() method."""

    @patch("pbx.utils.audio.get_prompt_audio")
    @patch("pbx.rtp.dtmf_monitor.RTPRecorder")
    @patch("pbx.rtp.handler.RTPPlayer")
    @patch("pbx.core.call.CallState")
    def test_call_not_found(
        self,
        mock_call_state: MagicMock,
        mock_rtp_player: MagicMock,
        mock_rtp_recorder: MagicMock,
        mock_get_prompt: MagicMock,
    ) -> None:
        pbx = _make_pbx_core()
        pbx.call_manager.get_call.return_value = None

        router = CallRouter(pbx)
        router._handle_no_answer("call-nonexistent")

        pbx.logger.warning.assert_called()

    @patch("pbx.utils.audio.get_prompt_audio")
    @patch("pbx.rtp.dtmf_monitor.RTPRecorder")
    @patch("pbx.rtp.handler.RTPPlayer")
    @patch("pbx.core.call.CallState")
    def test_call_already_connected(
        self,
        mock_call_state: MagicMock,
        mock_rtp_player: MagicMock,
        mock_rtp_recorder: MagicMock,
        mock_get_prompt: MagicMock,
    ) -> None:
        pbx = _make_pbx_core()
        mock_call = MagicMock()
        mock_call.state = mock_call_state.CONNECTED
        mock_call.routed_to_voicemail = False
        mock_call.transfer_session_id = None
        mock_call.trunk = None
        pbx.call_manager.get_call.return_value = mock_call

        router = CallRouter(pbx)
        router._handle_no_answer("call-1")

        pbx.logger.debug.assert_called()

    @patch("pbx.utils.audio.get_prompt_audio")
    @patch("pbx.rtp.dtmf_monitor.RTPRecorder")
    @patch("pbx.rtp.handler.RTPPlayer")
    @patch("pbx.core.call.CallState")
    def test_already_routed_to_voicemail(
        self,
        mock_call_state: MagicMock,
        mock_rtp_player: MagicMock,
        mock_rtp_recorder: MagicMock,
        mock_get_prompt: MagicMock,
    ) -> None:
        pbx = _make_pbx_core()
        mock_call = MagicMock()
        mock_call.state = "RINGING"
        mock_call.routed_to_voicemail = True
        pbx.call_manager.get_call.return_value = mock_call

        router = CallRouter(pbx)
        router._handle_no_answer("call-1")

        pbx.logger.debug.assert_called()

    @patch("pbx.utils.audio.get_prompt_audio")
    @patch("pbx.rtp.dtmf_monitor.RTPRecorder")
    @patch("pbx.rtp.handler.RTPPlayer")
    @patch("pbx.core.call.CallState")
    def test_no_answer_routes_to_voicemail(
        self,
        mock_call_state: MagicMock,
        mock_rtp_player_cls: MagicMock,
        mock_rtp_recorder_cls: MagicMock,
        mock_get_prompt: MagicMock,
    ) -> None:
        pbx = _make_pbx_core()
        mock_call = MagicMock()
        mock_call.state = "RINGING"
        mock_call.routed_to_voicemail = False
        mock_call.transfer_session_id = None
        mock_call.trunk = None
        mock_call.caller_rtp = {"address": "192.168.1.100", "port": 30000}
        mock_call.rtp_ports = (20000, 20001)
        mock_call.caller_addr = CALLER_ADDR
        mock_call.original_invite = MagicMock()
        mock_call.from_extension = "1001"
        mock_call.to_extension = "1002"
        pbx.call_manager.get_call.return_value = mock_call

        mock_player = MagicMock()
        mock_player.start.return_value = True
        mock_rtp_player_cls.return_value = mock_player

        mock_recorder = MagicMock()
        mock_recorder.start.return_value = True
        mock_rtp_recorder_cls.return_value = mock_recorder

        mock_get_prompt.return_value = b"\x00\x01\x02"

        mailbox = MagicMock()
        mailbox.get_greeting_path.return_value = None
        pbx.voicemail_system.get_mailbox.return_value = mailbox

        router = CallRouter(pbx)
        router._answer_call_for_voicemail = MagicMock(return_value=True)
        router._send_cancel_to_callee = MagicMock()

        with (
            patch("threading.Timer") as mock_timer_cls,
            patch("threading.Thread") as mock_thread_cls,
            patch("time.sleep"),
        ):
            mock_timer = MagicMock()
            mock_timer_cls.return_value = mock_timer
            mock_thread = MagicMock()
            mock_thread_cls.return_value = mock_thread

            router._handle_no_answer("call-1")

        assert mock_call.routed_to_voicemail is True
        router._send_cancel_to_callee.assert_called_once()
        router._answer_call_for_voicemail.assert_called_once()

    @patch("pbx.utils.audio.get_prompt_audio")
    @patch("pbx.rtp.dtmf_monitor.RTPRecorder")
    @patch("pbx.rtp.handler.RTPPlayer")
    @patch("pbx.core.call.CallState")
    def test_no_answer_trunk_call_ends_cleanly_not_voicemail(
        self,
        mock_call_state: MagicMock,
        mock_rtp_player_cls: MagicMock,
        mock_rtp_recorder_cls: MagicMock,
        mock_get_prompt: MagicMock,
    ) -> None:
        """An unanswered outbound trunk call must not be answered into the
        internal caller's voicemail -- it has no PBX-owned mailbox to route
        to (see _end_unanswered_trunk_call)."""
        pbx = _make_pbx_core()
        mock_call = MagicMock()
        mock_call.state = "RINGING"
        mock_call.routed_to_voicemail = False
        mock_call.transfer_session_id = None
        mock_call.trunk = MagicMock()
        mock_call.caller_addr = CALLER_ADDR
        mock_call.original_invite = MagicMock()
        mock_call.from_extension = "1001"
        mock_call.to_extension = "12125551234"
        pbx.call_manager.get_call.return_value = mock_call

        router = CallRouter(pbx)
        router._send_cancel_to_callee = MagicMock()
        router._answer_call_for_voicemail = MagicMock(return_value=True)
        router._end_unanswered_trunk_call = MagicMock()

        router._handle_no_answer("call-1")

        assert mock_call.routed_to_voicemail is True
        router._send_cancel_to_callee.assert_called_once()
        router._end_unanswered_trunk_call.assert_called_once_with(mock_call, "call-1")
        router._answer_call_for_voicemail.assert_not_called()
        mock_get_prompt.assert_not_called()

    @patch("pbx.utils.audio.get_prompt_audio")
    @patch("pbx.rtp.handler.RTPRecorder")
    @patch("pbx.rtp.handler.RTPPlayer")
    @patch("pbx.core.call.CallState")
    def test_no_answer_answer_fails_returns_early(
        self,
        mock_call_state: MagicMock,
        mock_rtp_player_cls: MagicMock,
        mock_rtp_recorder_cls: MagicMock,
        mock_get_prompt: MagicMock,
    ) -> None:
        pbx = _make_pbx_core()
        mock_call = MagicMock()
        mock_call.state = "RINGING"
        mock_call.routed_to_voicemail = False
        mock_call.transfer_session_id = None
        mock_call.trunk = None
        mock_call.caller_rtp = {"address": "192.168.1.100", "port": 30000}
        pbx.call_manager.get_call.return_value = mock_call

        router = CallRouter(pbx)
        router._answer_call_for_voicemail = MagicMock(return_value=False)
        router._send_cancel_to_callee = MagicMock()

        router._handle_no_answer("call-1")

        router._answer_call_for_voicemail.assert_called_once()
        mock_rtp_player_cls.assert_not_called()

    @patch("pbx.utils.audio.get_prompt_audio")
    @patch("pbx.rtp.dtmf_monitor.RTPRecorder")
    @patch("pbx.rtp.handler.RTPPlayer")
    @patch("pbx.core.call.CallState")
    def test_no_answer_no_caller_rtp_ends_call(
        self,
        mock_call_state: MagicMock,
        mock_rtp_player_cls: MagicMock,
        mock_rtp_recorder_cls: MagicMock,
        mock_get_prompt: MagicMock,
    ) -> None:
        pbx = _make_pbx_core()
        mock_call = MagicMock()
        mock_call.state = "RINGING"
        mock_call.routed_to_voicemail = False
        mock_call.transfer_session_id = None
        mock_call.trunk = None
        mock_call.caller_rtp = None
        mock_call.caller_addr = CALLER_ADDR
        mock_call.original_invite = MagicMock()
        mock_call.rtp_ports = (20000, 20001)
        pbx.call_manager.get_call.return_value = mock_call

        router = CallRouter(pbx)
        router._answer_call_for_voicemail = MagicMock(return_value=True)
        router._send_cancel_to_callee = MagicMock()

        router._handle_no_answer("call-1")

        pbx.end_call.assert_called_once_with("call-1")

    @patch("pbx.utils.audio.get_prompt_audio")
    @patch("pbx.rtp.dtmf_monitor.RTPRecorder")
    @patch("pbx.rtp.handler.RTPPlayer")
    @patch("pbx.core.call.CallState")
    def test_no_answer_player_start_fails(
        self,
        mock_call_state: MagicMock,
        mock_rtp_player_cls: MagicMock,
        mock_rtp_recorder_cls: MagicMock,
        mock_get_prompt: MagicMock,
    ) -> None:
        pbx = _make_pbx_core()
        mock_call = MagicMock()
        mock_call.state = "RINGING"
        mock_call.routed_to_voicemail = False
        mock_call.transfer_session_id = None
        mock_call.trunk = None
        mock_call.caller_rtp = {"address": "192.168.1.100", "port": 30000}
        mock_call.rtp_ports = (20000, 20001)
        mock_call.caller_addr = CALLER_ADDR
        mock_call.original_invite = MagicMock()
        mock_call.from_extension = "1001"
        mock_call.to_extension = "1002"
        pbx.call_manager.get_call.return_value = mock_call

        mock_player = MagicMock()
        mock_player.start.return_value = False
        mock_rtp_player_cls.return_value = mock_player

        mock_recorder = MagicMock()
        mock_recorder.start.return_value = True
        mock_rtp_recorder_cls.return_value = mock_recorder

        mailbox = MagicMock()
        mailbox.get_greeting_path.return_value = None
        pbx.voicemail_system.get_mailbox.return_value = mailbox

        router = CallRouter(pbx)
        router._answer_call_for_voicemail = MagicMock(return_value=True)
        router._send_cancel_to_callee = MagicMock()

        with (
            patch("threading.Timer") as mock_timer_cls,
            patch("threading.Thread") as mock_thread_cls,
        ):
            mock_timer_cls.return_value = MagicMock()
            mock_thread_cls.return_value = MagicMock()

            router._handle_no_answer("call-1")

        pbx.logger.warning.assert_called()

    @patch("pbx.utils.audio.get_prompt_audio")
    @patch("pbx.rtp.dtmf_monitor.RTPRecorder")
    @patch("pbx.rtp.handler.RTPPlayer")
    @patch("pbx.core.call.CallState")
    def test_no_answer_recorder_start_fails(
        self,
        mock_call_state: MagicMock,
        mock_rtp_player_cls: MagicMock,
        mock_rtp_recorder_cls: MagicMock,
        mock_get_prompt: MagicMock,
    ) -> None:
        pbx = _make_pbx_core()
        mock_call = MagicMock()
        mock_call.state = "RINGING"
        mock_call.routed_to_voicemail = False
        mock_call.transfer_session_id = None
        mock_call.trunk = None
        mock_call.caller_rtp = {"address": "192.168.1.100", "port": 30000}
        mock_call.rtp_ports = (20000, 20001)
        mock_call.caller_addr = CALLER_ADDR
        mock_call.original_invite = MagicMock()
        mock_call.from_extension = "1001"
        mock_call.to_extension = "1002"
        pbx.call_manager.get_call.return_value = mock_call

        mock_player = MagicMock()
        mock_player.start.return_value = True
        mock_rtp_player_cls.return_value = mock_player

        mock_recorder = MagicMock()
        mock_recorder.start.return_value = False
        mock_rtp_recorder_cls.return_value = mock_recorder

        mock_get_prompt.return_value = b"\x00\x01\x02"

        mailbox = MagicMock()
        mailbox.get_greeting_path.return_value = None
        pbx.voicemail_system.get_mailbox.return_value = mailbox

        router = CallRouter(pbx)
        router._answer_call_for_voicemail = MagicMock(return_value=True)
        router._send_cancel_to_callee = MagicMock()

        with patch("time.sleep"):
            router._handle_no_answer("call-1")

        pbx.end_call.assert_called_once_with("call-1")

    @patch("pbx.utils.audio.get_prompt_audio")
    @patch("pbx.rtp.dtmf_monitor.RTPRecorder")
    @patch("pbx.rtp.handler.RTPPlayer")
    @patch("pbx.core.call.CallState")
    def test_no_answer_with_custom_greeting(
        self,
        mock_call_state: MagicMock,
        mock_rtp_player_cls: MagicMock,
        mock_rtp_recorder_cls: MagicMock,
        mock_get_prompt: MagicMock,
    ) -> None:
        pbx = _make_pbx_core()
        mock_call = MagicMock()
        mock_call.state = "RINGING"
        mock_call.routed_to_voicemail = False
        mock_call.transfer_session_id = None
        mock_call.trunk = None
        mock_call.caller_rtp = {"address": "192.168.1.100", "port": 30000}
        mock_call.rtp_ports = (20000, 20001)
        mock_call.caller_addr = CALLER_ADDR
        mock_call.original_invite = MagicMock()
        mock_call.from_extension = "1001"
        mock_call.to_extension = "1002"
        pbx.call_manager.get_call.return_value = mock_call

        mock_player = MagicMock()
        mock_player.start.return_value = True
        mock_rtp_player_cls.return_value = mock_player

        mock_recorder = MagicMock()
        mock_recorder.start.return_value = True
        mock_rtp_recorder_cls.return_value = mock_recorder

        mailbox = MagicMock()
        mailbox.get_greeting_path.return_value = "/tmp/custom_greeting.wav"
        pbx.voicemail_system.get_mailbox.return_value = mailbox

        router = CallRouter(pbx)
        router._answer_call_for_voicemail = MagicMock(return_value=True)
        router._send_cancel_to_callee = MagicMock()

        with (
            patch("threading.Timer") as mock_timer_cls,
            patch("threading.Thread") as mock_thread_cls,
            patch("time.sleep"),
            patch("pbx.core.call_router.Path") as mock_path,
        ):
            mock_timer_cls.return_value = MagicMock()
            mock_thread_cls.return_value = MagicMock()
            mock_path_instance = MagicMock()
            mock_path_instance.exists.return_value = True
            mock_path_instance.stat.return_value = MagicMock(st_size=1024)
            mock_path.return_value = mock_path_instance

            router._handle_no_answer("call-1")

        mock_player.play_file.assert_called_once_with("/tmp/custom_greeting.wav")
        mock_player.play_beep.assert_called_once_with(frequency=1000, duration_ms=500)

    @patch("pbx.utils.audio.get_prompt_audio")
    @patch("pbx.rtp.dtmf_monitor.RTPRecorder")
    @patch("pbx.rtp.handler.RTPPlayer")
    @patch("pbx.core.call.CallState")
    def test_no_answer_custom_greeting_file_not_found_falls_back(
        self,
        mock_call_state: MagicMock,
        mock_rtp_player_cls: MagicMock,
        mock_rtp_recorder_cls: MagicMock,
        mock_get_prompt: MagicMock,
    ) -> None:
        pbx = _make_pbx_core()
        mock_call = MagicMock()
        mock_call.state = "RINGING"
        mock_call.routed_to_voicemail = False
        mock_call.transfer_session_id = None
        mock_call.trunk = None
        mock_call.caller_rtp = {"address": "192.168.1.100", "port": 30000}
        mock_call.rtp_ports = (20000, 20001)
        mock_call.caller_addr = CALLER_ADDR
        mock_call.original_invite = MagicMock()
        mock_call.from_extension = "1001"
        mock_call.to_extension = "1002"
        pbx.call_manager.get_call.return_value = mock_call

        mock_player = MagicMock()
        mock_player.start.return_value = True
        mock_rtp_player_cls.return_value = mock_player

        mock_recorder = MagicMock()
        mock_recorder.start.return_value = True
        mock_rtp_recorder_cls.return_value = mock_recorder

        mock_get_prompt.return_value = b"\x00\x01\x02"

        mailbox = MagicMock()
        mailbox.get_greeting_path.return_value = "/tmp/nonexistent.wav"
        pbx.voicemail_system.get_mailbox.return_value = mailbox

        router = CallRouter(pbx)
        router._answer_call_for_voicemail = MagicMock(return_value=True)
        router._send_cancel_to_callee = MagicMock()

        with (
            patch("threading.Timer") as mock_timer_cls,
            patch("threading.Thread") as mock_thread_cls,
            patch("time.sleep"),
            patch("pbx.core.call_router.Path") as mock_path,
        ):
            mock_timer_cls.return_value = MagicMock()
            mock_thread_cls.return_value = MagicMock()
            mock_path_instance = MagicMock()
            mock_path_instance.exists.return_value = False
            mock_path.return_value = mock_path_instance

            router._handle_no_answer("call-1")

        mock_get_prompt.assert_called_once_with("leave_message")

    @patch("pbx.utils.audio.get_prompt_audio")
    @patch("pbx.rtp.dtmf_monitor.RTPRecorder")
    @patch("pbx.rtp.handler.RTPPlayer")
    @patch("pbx.core.call.CallState")
    def test_no_answer_greeting_oserror(
        self,
        mock_call_state: MagicMock,
        mock_rtp_player_cls: MagicMock,
        mock_rtp_recorder_cls: MagicMock,
        mock_get_prompt: MagicMock,
    ) -> None:
        pbx = _make_pbx_core()
        mock_call = MagicMock()
        mock_call.state = "RINGING"
        mock_call.routed_to_voicemail = False
        mock_call.transfer_session_id = None
        mock_call.trunk = None
        mock_call.caller_rtp = {"address": "192.168.1.100", "port": 30000}
        mock_call.rtp_ports = (20000, 20001)
        mock_call.caller_addr = CALLER_ADDR
        mock_call.original_invite = MagicMock()
        mock_call.from_extension = "1001"
        mock_call.to_extension = "1002"
        pbx.call_manager.get_call.return_value = mock_call

        mock_rtp_player_cls.side_effect = OSError("port in use")

        mock_recorder = MagicMock()
        mock_recorder.start.return_value = True
        mock_rtp_recorder_cls.return_value = mock_recorder

        mailbox = MagicMock()
        mailbox.get_greeting_path.return_value = None
        pbx.voicemail_system.get_mailbox.return_value = mailbox

        router = CallRouter(pbx)
        router._answer_call_for_voicemail = MagicMock(return_value=True)
        router._send_cancel_to_callee = MagicMock()

        with (
            patch("threading.Timer") as mock_timer_cls,
            patch("threading.Thread") as mock_thread_cls,
        ):
            mock_timer_cls.return_value = MagicMock()
            mock_thread_cls.return_value = MagicMock()

            router._handle_no_answer("call-1")

        pbx.logger.error.assert_called()


# ===========================================================================
# CallRouter.route_call - phone model codec selection
# ===========================================================================


@pytest.mark.unit
class TestRouteCallCodecSelection:
    """Tests for phone model detection and codec selection."""

    def test_detected_phone_model_logs_codecs(self) -> None:
        pbx = _make_pbx_core()
        pbx._detect_phone_model.return_value = "ZIP37G"
        pbx._get_compatible_codecs.return_value = ["PCMU", "PCMA"]

        router = CallRouter(pbx)
        msg = _make_invite_message(body="")

        router.route_call(
            "<sip:1001@pbx.local>",
            "<sip:1002@pbx.local>",
            "call-1",
            msg,
            CALLER_ADDR,
        )

        pbx._detect_phone_model.assert_called()
        pbx._get_compatible_codecs.assert_called()
        pbx.logger.info.assert_called()


# ===========================================================================
# CallRouter.route_call - registered phones DB edge cases
# ===========================================================================


@pytest.mark.unit
class TestRouteCallRegisteredPhonesDB:
    """Tests for registered_phones_db edge cases."""

    def test_registered_phones_db_is_none(self) -> None:
        pbx = _make_pbx_core()
        pbx.registered_phones_db = None

        router = CallRouter(pbx)
        msg = _make_invite_message(body="")

        result = router.route_call(
            "<sip:1001@pbx.local>",
            "<sip:1002@pbx.local>",
            "call-1",
            msg,
            CALLER_ADDR,
        )

        assert result is True

    def test_registered_phones_db_type_error(self) -> None:
        pbx = _make_pbx_core()
        pbx.registered_phones_db.get_by_extension.side_effect = TypeError("test")

        router = CallRouter(pbx)
        msg = _make_invite_message(body="")

        result = router.route_call(
            "<sip:1001@pbx.local>",
            "<sip:1002@pbx.local>",
            "call-1",
            msg,
            CALLER_ADDR,
        )

        assert result is True

    def test_registered_phones_db_value_error(self) -> None:
        pbx = _make_pbx_core()
        pbx.registered_phones_db.get_by_extension.side_effect = ValueError("test")

        router = CallRouter(pbx)
        msg = _make_invite_message(body="")

        result = router.route_call(
            "<sip:1001@pbx.local>",
            "<sip:1002@pbx.local>",
            "call-1",
            msg,
            CALLER_ADDR,
        )

        assert result is True


@pytest.mark.unit
class TestResolveExtensionRecovery:
    """Recovering a lost in-memory registration from registered_phones."""

    @staticmethod
    def _unregistered_pbx() -> MagicMock:
        pbx = _make_pbx_core()
        stale = MagicMock()
        stale.registered = False
        stale.address = None
        stale.is_expired.return_value = False
        pbx.extension_registry.get.return_value = stale
        return pbx

    def test_recovers_a_sip_phone_row(self) -> None:
        pbx = self._unregistered_pbx()
        pbx.registered_phones_db.get_by_extension.return_value = [
            {"ip_address": "10.0.0.7", "sip_port": 5062}
        ]

        resolved = CallRouter(pbx)._resolve_extension("1002")

        assert resolved is not None
        resolved.register.assert_called_once_with(("10.0.0.7", 5062))

    def test_webrtc_row_is_not_recovered_as_a_sip_contact(self) -> None:
        # "webrtc" is a marker, not a host: recovering it puts an
        # unresolvable hostname into every INVITE sent to this extension.
        pbx = self._unregistered_pbx()
        pbx.registered_phones_db.get_by_extension.return_value = [
            {"ip_address": "webrtc", "sip_port": 5060}
        ]

        assert CallRouter(pbx)._resolve_extension("1002") is None

    def test_real_phone_is_preferred_over_a_newer_webrtc_row(self) -> None:
        pbx = self._unregistered_pbx()
        pbx.registered_phones_db.get_by_extension.return_value = [
            {"ip_address": "webrtc", "sip_port": 5060},
            {"ip_address": "10.0.0.7", "sip_port": 5060},
        ]

        resolved = CallRouter(pbx)._resolve_extension("1002")

        assert resolved is not None
        resolved.register.assert_called_once_with(("10.0.0.7", 5060))


@pytest.mark.unit
class TestIsWebRTCAddress:
    def test_marker_tuple(self) -> None:
        assert CallRouter.is_webrtc_address(("webrtc", "session-1")) is True

    def test_sip_address(self) -> None:
        assert CallRouter.is_webrtc_address(("10.0.0.7", 5060)) is False

    def test_missing_address(self) -> None:
        assert CallRouter.is_webrtc_address(None) is False


# ===========================================================================
# CallRouter.route_call - self-INVITE loop guard
# ===========================================================================


@pytest.mark.unit
class TestRouteCallSelfInviteGuard:
    """Calls must never be routed to the PBX's own SIP address."""

    def _route(self, pbx: MagicMock) -> bool:
        router = CallRouter(pbx)
        return router.route_call(
            "<sip:1001@pbx.local>",
            "<sip:1002@pbx.local>",
            "call-loop-1",
            _make_invite_message(),
            CALLER_ADDR,
        )

    def test_loopback_destination_rejected(self) -> None:
        pbx = _make_pbx_core()
        pbx.extension_registry.get.return_value.address = ("127.0.0.1", 5060)

        assert self._route(pbx) is False
        pbx.extension_registry.unregister.assert_called_once_with("1002")
        pbx.rtp_relay.release_relay.assert_called_once_with("call-loop-1")
        pbx.call_manager.end_call.assert_called_once_with("call-loop-1")

    def test_own_sip_address_rejected(self) -> None:
        pbx = _make_pbx_core()
        # _make_pbx_core sets server IP to 10.0.0.1 and sip_port to 5060
        pbx.extension_registry.get.return_value.address = ("10.0.0.1", 5060)

        assert self._route(pbx) is False
        pbx.extension_registry.unregister.assert_called_once_with("1002")
        pbx.call_manager.end_call.assert_called_once_with("call-loop-1")

    def test_own_ip_different_port_allowed_past_guard(self) -> None:
        """A softphone on the server's IP but a different port is legitimate."""
        pbx = _make_pbx_core()
        pbx.extension_registry.get.return_value.address = ("10.0.0.1", 5080)

        self._route(pbx)
        pbx.extension_registry.unregister.assert_not_called()


# ===========================================================================
# CallRouter.handle_callee_answer
# ===========================================================================


@pytest.mark.unit
class TestHandleCalleeAnswer:
    """Tests for CallRouter.handle_callee_answer -- the answer-side twin of
    route_call, handling the callee's 200 OK for a call route_call set up."""

    def test_no_call_found(self) -> None:
        """Returns early when call is not found."""
        pbx = _make_pbx_core()
        pbx.call_manager.get_call.return_value = None
        router = CallRouter(pbx)

        router.handle_callee_answer("nonexistent", MagicMock(), ("1.2.3.4", 5060))

        pbx.logger.error.assert_called_once()

    @patch("pbx.sip.message.SIPMessageBuilder.build_response")
    @patch("pbx.sip.sdp.SDPBuilder.build_audio_sdp", return_value="v=0\r\n...")
    @patch("pbx.sip.sdp.SDPSession")
    def test_callee_answer_full_flow(
        self,
        mock_sdp_cls: MagicMock,
        mock_build_sdp: MagicMock,
        mock_build_response: MagicMock,
    ) -> None:
        """Full callee answer flow with SDP, RTP relay, and 200 OK."""
        pbx = _make_pbx_core()

        mock_call = MagicMock()
        mock_call.call_id = "call-42"
        mock_call.from_extension = "1001"
        mock_call.to_extension = "1002"
        mock_call.caller_rtp = {"address": "10.0.0.1", "port": 20000, "formats": ["0", "8"]}
        mock_call.callee_rtp = None
        mock_call.rtp_ports = (30000, 30001)
        mock_call.caller_addr = ("10.0.0.1", 5060)
        mock_call.no_answer_timer = MagicMock()
        mock_call.original_invite = MagicMock()
        mock_call.transfer_session_id = None
        mock_call.webrtc_session_id = None
        pbx.call_manager.get_call.return_value = mock_call

        response_msg = MagicMock()
        response_msg.body = "v=0\r\no=- 0 0 IN IP4 10.0.0.2\r\n"

        sdp_inst = MagicMock()
        sdp_inst.get_audio_info.return_value = {"address": "10.0.0.2", "port": 20002}
        mock_sdp_cls.return_value = sdp_inst

        ok_msg = MagicMock()
        mock_build_response.return_value = ok_msg

        pbx.config.get.side_effect = lambda k, d=None: {
            "server.external_ip": "10.0.0.100",
            "server.sip_port": 5060,
        }.get(k, d)
        pbx._get_server_ip.return_value = "10.0.0.100"
        pbx._get_phone_user_agent.return_value = None
        pbx._detect_phone_model.return_value = None
        pbx._get_compatible_codecs.return_value = ["0", "8", "101"]
        pbx._get_dtmf_payload_type.return_value = 101
        pbx._get_ilbc_mode.return_value = 30

        router = CallRouter(pbx)
        router.handle_callee_answer("call-42", response_msg, ("10.0.0.2", 5060))

        # Call should be marked as connected via the regular SIP-to-SIP path
        mock_call.connect.assert_called_once()
        pbx.cdr_system.mark_answered.assert_called_once_with("call-42")
        mock_call.no_answer_timer.cancel.assert_called_once()
        pbx.rtp_relay.set_endpoints.assert_called_once_with(
            "call-42", ("10.0.0.1", 20000), ("10.0.0.2", 20002)
        )
        mock_build_sdp.assert_called_once()
        mock_build_response.assert_called_once()
        # The tagged To header actually sent to the caller must be captured
        # -- it's the caller's real dialog identity for any later
        # PBX-originated request toward it (e.g. a transfer-bridge BYE),
        # and can't be recovered from original_invite afterward.
        assert mock_call.caller_dialog_to is ok_msg.get_header.return_value
        pbx.sip_server._send_message.assert_called_once()

    def test_callee_answer_no_body(self) -> None:
        """Handle callee answer when response has no SDP body."""
        pbx = _make_pbx_core()

        mock_call = MagicMock()
        mock_call.call_id = "call-99"
        mock_call.caller_rtp = None
        mock_call.callee_rtp = None
        mock_call.rtp_ports = None
        mock_call.no_answer_timer = None
        mock_call.original_invite = None
        mock_call.caller_addr = None
        mock_call.transfer_session_id = None
        mock_call.webrtc_session_id = None
        pbx.call_manager.get_call.return_value = mock_call

        response_msg = MagicMock()
        response_msg.body = None

        router = CallRouter(pbx)
        router.handle_callee_answer("call-99", response_msg, ("1.2.3.4", 5060))

        mock_call.connect.assert_called_once()
        pbx.cdr_system.mark_answered.assert_called_once_with("call-99")
