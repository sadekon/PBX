"""
Comprehensive tests for PagingHandler (pbx.core.paging_handler).

Covers:
  - __init__
  - handle_paging (happy path with DAC, happy path without DAC, page initiation failure,
                   page info failure, no SDP body, SDP with audio info, SDP without audio info,
                   RTP allocation failure, no zones, no DAC device configured,
                   codec negotiation, phone model detection)
  - _resolve_dac_device (resolved device, unresolvable device, multi-zone warning)
  - start_test_page / _begin_originated_page (validation, origination, page opening)
  - _paging_session (happy path, missing SIP config, caller RTP present,
                     no caller RTP, source_rtp override, call ended monitoring,
                     error handling)
"""

import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

from pbx.core.paging_handler import PagingHandler


def _make_pbx_core() -> MagicMock:
    """Create a fully-wired MagicMock acting as PBXCore."""
    pbx = MagicMock()
    pbx.logger = MagicMock()
    pbx.config.get.return_value = 5060
    pbx._get_server_ip.return_value = "10.0.0.1"
    pbx._get_phone_user_agent.return_value = "TestAgent/1.0"
    pbx._detect_phone_model.return_value = "GenericPhone"
    pbx._get_codecs_for_phone_model.return_value = ["0", "8"]
    pbx._get_dtmf_payload_type.return_value = 101
    pbx._get_ilbc_mode.return_value = 30
    pbx.rtp_relay.allocate_relay.return_value = (20000, 20001)
    return pbx


def _make_message(with_body: bool = True) -> MagicMock:
    """Create a mock SIP message."""
    message = MagicMock()
    if with_body:
        message.body = "v=0\r\no=- 0 0 IN IP4 192.168.1.10\r\n"
    else:
        message.body = None
    return message


def _make_call(state_value: str = "connected") -> MagicMock:
    """Create a mock Call object."""
    call_obj = MagicMock()
    call_obj.rtp_ports = (20000, 20001)
    call_obj.caller_rtp = {"address": "192.168.1.10", "port": 30000}
    call_obj.caller_addr = ("192.168.1.10", 5060)
    call_obj.paging_active = True
    call_obj.page_id = "page-1"
    state = MagicMock()
    state.value = state_value
    call_obj.state = state
    return call_obj


def _make_page_info(with_dac: bool = True, zone_count: int = 1) -> dict[str, Any]:
    """
    Create page info dictionary, shaped as PagingSystem.initiate_page() returns
    it -- including the ``resolved_dac_devices`` list, which is where the
    handler reads device configuration from.
    """
    zones = []
    resolved = []
    for i in range(zone_count):
        zone = {
            "name": f"Zone {i + 1}",
            "dac_device": f"dac-{i}" if with_dac else None,
        }
        zones.append(zone)
        if with_dac:
            resolved.append(
                {
                    "device_id": f"dac-{i}",
                    "device_type": "CyberData",
                    "sip_uri": f"sip:dac@10.0.0.5{i}",
                    "ip_address": f"10.0.0.5{i}",
                    "port": 5060,
                }
            )
    return {
        "page_id": "page-1",
        "zone_names": "Zone 1" if zone_count == 1 else f"Zones 1-{zone_count}",
        "zones": zones,
        "resolved_dac_devices": resolved,
    }


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPagingHandlerInit:
    """Tests for PagingHandler initialisation."""

    def test_init_stores_pbx_core(self) -> None:
        """Handler should store the pbx_core reference."""
        pbx = MagicMock()
        handler = PagingHandler(pbx)
        assert handler.pbx_core is pbx


# ---------------------------------------------------------------------------
# handle_paging
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHandlePaging:
    """Tests for handle_paging."""

    @patch("threading.Thread")
    @patch("pbx.sip.sdp.SDPSession")
    @patch("pbx.sip.sdp.SDPBuilder")
    @patch("pbx.sip.message.SIPMessageBuilder")
    def test_happy_path_with_dac_device(
        self, mock_sip, mock_sdp_builder, mock_sdp_session, mock_thread_cls
    ) -> None:
        """Successful paging with a DAC device should return True and start thread."""
        pbx = _make_pbx_core()
        handler = PagingHandler(pbx)
        message = _make_message()

        pbx.paging_system.initiate_page.return_value = "page-1"
        pbx.paging_system.get_page_info.return_value = _make_page_info(with_dac=True)

        result = handler.handle_paging("1001", "700", "call-1", message, ("192.168.1.10", 5060))
        assert result is True
        pbx.call_manager.create_call.assert_called_once_with("call-1", "1001", "700")
        pbx.cdr_system.start_record.assert_called_once_with("call-1", "1001", "700")
        mock_thread_cls.assert_called_once()

    @patch("pbx.sip.sdp.SDPSession")
    @patch("pbx.sip.sdp.SDPBuilder")
    @patch("pbx.sip.message.SIPMessageBuilder")
    def test_happy_path_without_dac_device(
        self, mock_sip, mock_sdp_builder, mock_sdp_session
    ) -> None:
        """Paging without a DAC device should return True without starting thread."""
        pbx = _make_pbx_core()
        handler = PagingHandler(pbx)
        message = _make_message()

        pbx.paging_system.initiate_page.return_value = "page-1"
        pbx.paging_system.get_page_info.return_value = _make_page_info(with_dac=False)

        result = handler.handle_paging("1001", "700", "call-1", message, ("192.168.1.10", 5060))
        assert result is True
        pbx.logger.warning.assert_called()

    def test_page_initiation_failure(self) -> None:
        """If paging_system.initiate_page fails, should return False."""
        pbx = _make_pbx_core()
        handler = PagingHandler(pbx)
        message = _make_message()

        pbx.paging_system.initiate_page.return_value = None

        result = handler.handle_paging("1001", "700", "call-1", message, ("192.168.1.10", 5060))
        assert result is False
        pbx.logger.error.assert_called()

    def test_page_info_failure(self) -> None:
        """If get_page_info returns None, should return False."""
        pbx = _make_pbx_core()
        handler = PagingHandler(pbx)
        message = _make_message()

        pbx.paging_system.initiate_page.return_value = "page-1"
        pbx.paging_system.get_page_info.return_value = None

        result = handler.handle_paging("1001", "700", "call-1", message, ("192.168.1.10", 5060))
        assert result is False

    @patch("pbx.sip.sdp.SDPSession")
    @patch("pbx.sip.sdp.SDPBuilder")
    @patch("pbx.sip.message.SIPMessageBuilder")
    def test_no_sdp_body(self, mock_sip, mock_sdp_builder, mock_sdp_session) -> None:
        """When INVITE has no SDP body, should still proceed."""
        pbx = _make_pbx_core()
        handler = PagingHandler(pbx)
        message = _make_message(with_body=False)

        pbx.paging_system.initiate_page.return_value = "page-1"
        pbx.paging_system.get_page_info.return_value = _make_page_info(with_dac=False)

        result = handler.handle_paging("1001", "700", "call-1", message, ("192.168.1.10", 5060))
        assert result is True

    @patch("pbx.sip.sdp.SDPSession")
    @patch("pbx.sip.sdp.SDPBuilder")
    @patch("pbx.sip.message.SIPMessageBuilder")
    def test_rtp_allocation_failure(self, mock_sip, mock_sdp_builder, mock_sdp_session) -> None:
        """If RTP relay allocation fails, should tear down the call and return False."""
        pbx = _make_pbx_core()
        pbx.rtp_relay.allocate_relay.return_value = None
        handler = PagingHandler(pbx)
        message = _make_message()

        pbx.paging_system.initiate_page.return_value = "page-1"
        pbx.paging_system.get_page_info.return_value = _make_page_info(with_dac=True)

        result = handler.handle_paging("1001", "700", "call-1", message, ("192.168.1.10", 5060))
        assert result is False
        # end_call() releases the relay, CDR and page together -- ending only
        # the page would leak the half-built call.
        pbx.end_call.assert_called_with("call-1")

    @patch("pbx.sip.sdp.SDPSession")
    @patch("pbx.sip.sdp.SDPBuilder")
    @patch("pbx.sip.message.SIPMessageBuilder")
    def test_no_zones_returns_false(self, mock_sip, mock_sdp_builder, mock_sdp_session) -> None:
        """If no zones configured, should tear down the call and return False."""
        pbx = _make_pbx_core()
        handler = PagingHandler(pbx)
        message = _make_message()

        pbx.paging_system.initiate_page.return_value = "page-1"
        pbx.paging_system.get_page_info.return_value = {"zone_names": "", "zones": []}

        result = handler.handle_paging("1001", "700", "call-1", message, ("192.168.1.10", 5060))
        assert result is False
        pbx.end_call.assert_called_with("call-1")

    @patch("pbx.sip.sdp.SDPSession")
    @patch("pbx.sip.sdp.SDPBuilder")
    @patch("pbx.sip.message.SIPMessageBuilder")
    def test_dac_device_id_none_continues(
        self, mock_sip, mock_sdp_builder, mock_sdp_session
    ) -> None:
        """If zone has no dac_device, should continue with warning."""
        pbx = _make_pbx_core()
        handler = PagingHandler(pbx)
        message = _make_message()

        page_info = {
            "zone_names": "Zone 1",
            "zones": [{"name": "Zone 1", "dac_device": None}],
            "resolved_dac_devices": [],
        }
        pbx.paging_system.initiate_page.return_value = "page-1"
        pbx.paging_system.get_page_info.return_value = page_info

        result = handler.handle_paging("1001", "700", "call-1", message, ("192.168.1.10", 5060))
        assert result is True
        pbx.logger.warning.assert_called()

    @patch("threading.Thread")
    @patch("pbx.sip.sdp.SDPSession")
    @patch("pbx.sip.sdp.SDPBuilder")
    @patch("pbx.sip.message.SIPMessageBuilder")
    def test_phone_model_detected_logged(
        self, mock_sip, mock_sdp_builder, mock_sdp_session, mock_thread_cls
    ) -> None:
        """When phone model is detected, codec info should be logged."""
        pbx = _make_pbx_core()
        pbx._detect_phone_model.return_value = "Yealink T54W"
        handler = PagingHandler(pbx)
        message = _make_message()

        pbx.paging_system.initiate_page.return_value = "page-1"
        pbx.paging_system.get_page_info.return_value = _make_page_info(with_dac=False)

        result = handler.handle_paging("1001", "700", "call-1", message, ("192.168.1.10", 5060))
        assert result is True
        pbx._get_codecs_for_phone_model.assert_called()

    @patch("pbx.sip.sdp.SDPSession")
    @patch("pbx.sip.sdp.SDPBuilder")
    @patch("pbx.sip.message.SIPMessageBuilder")
    def test_no_phone_model_still_succeeds(
        self, mock_sip, mock_sdp_builder, mock_sdp_session
    ) -> None:
        """When no phone model is detected, should still succeed."""
        pbx = _make_pbx_core()
        pbx._detect_phone_model.return_value = None
        handler = PagingHandler(pbx)
        message = _make_message()

        pbx.paging_system.initiate_page.return_value = "page-1"
        pbx.paging_system.get_page_info.return_value = _make_page_info(with_dac=False)

        result = handler.handle_paging("1001", "700", "call-1", message, ("192.168.1.10", 5060))
        assert result is True

    @patch("threading.Thread")
    @patch("pbx.sip.sdp.SDPSession")
    @patch("pbx.sip.sdp.SDPBuilder")
    @patch("pbx.sip.message.SIPMessageBuilder")
    def test_resolved_dac_device_used_for_session(
        self, mock_sip, mock_sdp_builder, mock_sdp_session, mock_thread_cls
    ) -> None:
        """The device resolved at page initiation should be passed to the session."""
        pbx = _make_pbx_core()
        handler = PagingHandler(pbx)
        message = _make_message()

        page_info = _make_page_info(with_dac=True)
        pbx.paging_system.initiate_page.return_value = "page-1"
        pbx.paging_system.get_page_info.return_value = page_info

        result = handler.handle_paging("1001", "700", "call-1", message, ("192.168.1.10", 5060))
        assert result is True
        mock_thread_cls.assert_called_once()
        # args=(call_id, call, dac_device, page_info)
        passed_device = mock_thread_cls.call_args.kwargs["args"][2]
        assert passed_device["device_id"] == "dac-0"

    @patch("threading.Thread")
    @patch("pbx.sip.sdp.SDPSession")
    @patch("pbx.sip.sdp.SDPBuilder")
    @patch("pbx.sip.message.SIPMessageBuilder")
    def test_unresolvable_dac_device_treated_as_no_hardware(
        self, mock_sip, mock_sdp_builder, mock_sdp_session, mock_thread_cls
    ) -> None:
        """A zone whose dac_device id resolved to nothing pages without audio."""
        pbx = _make_pbx_core()
        handler = PagingHandler(pbx)
        message = _make_message()

        # Zone names a device, but initiate_page could not match it to config.
        page_info = _make_page_info(with_dac=True)
        page_info["resolved_dac_devices"] = []
        pbx.paging_system.initiate_page.return_value = "page-1"
        pbx.paging_system.get_page_info.return_value = page_info

        result = handler.handle_paging("1001", "700", "call-1", message, ("192.168.1.10", 5060))
        # The call is still answered so the announcer gets confirmation.
        assert result is True
        mock_thread_cls.assert_not_called()
        pbx.logger.warning.assert_called()

    @patch("threading.Thread")
    @patch("pbx.sip.sdp.SDPSession")
    @patch("pbx.sip.sdp.SDPBuilder")
    @patch("pbx.sip.message.SIPMessageBuilder")
    def test_multi_zone_page_warns_and_uses_first_device(
        self, mock_sip, mock_sdp_builder, mock_sdp_session, mock_thread_cls
    ) -> None:
        """An all-call page reaches one device today, and says so."""
        pbx = _make_pbx_core()
        handler = PagingHandler(pbx)
        message = _make_message()

        pbx.paging_system.initiate_page.return_value = "page-1"
        pbx.paging_system.get_page_info.return_value = _make_page_info(with_dac=True, zone_count=3)

        result = handler.handle_paging("1001", "700", "call-1", message, ("192.168.1.10", 5060))
        assert result is True
        passed_device = mock_thread_cls.call_args.kwargs["args"][2]
        assert passed_device["device_id"] == "dac-0"
        assert any(
            "fan-out is not implemented" in str(c) for c in pbx.logger.warning.call_args_list
        )

    @patch("threading.Thread")
    @patch("pbx.sip.sdp.SDPSession")
    @patch("pbx.sip.sdp.SDPBuilder")
    @patch("pbx.sip.message.SIPMessageBuilder")
    def test_call_connects_after_200ok(
        self, mock_sip, mock_sdp_builder, mock_sdp_session, mock_thread_cls
    ) -> None:
        """Call.connect() should be called after sending 200 OK."""
        pbx = _make_pbx_core()
        handler = PagingHandler(pbx)
        message = _make_message()

        call_obj = MagicMock()
        pbx.call_manager.create_call.return_value = call_obj

        pbx.paging_system.initiate_page.return_value = "page-1"
        pbx.paging_system.get_page_info.return_value = _make_page_info(with_dac=False)

        handler.handle_paging("1001", "700", "call-1", message, ("192.168.1.10", 5060))
        call_obj.connect.assert_called_once()


# ---------------------------------------------------------------------------
# _paging_session
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPagingSession:
    """Tests for _paging_session."""

    @patch("pbx.core.paging_handler.time")
    def test_happy_path_monitors_until_ended(self, mock_time) -> None:
        """Paging session should monitor until call state is 'ended', then BYE the DAC."""
        pbx = _make_pbx_core()
        handler = PagingHandler(pbx)
        call_obj = _make_call()

        # Simulate call ending after one loop
        call_obj.state.value = "ended"

        dac_device = {
            "device_id": "dac-0",
            "device_type": "CyberData",
            "sip_uri": "sip:dac@10.0.0.50",
            "ip_address": "10.0.0.50",
            "port": 5060,
        }
        page_info = _make_page_info()

        handler._paging_session("call-1", call_obj, dac_device, page_info)
        # INVITE on the way in, BYE on the way out.
        assert pbx.sip_server._send_message.call_count == 2
        # The page itself belongs to PBXCore.end_call(), not to this thread --
        # ending it here too would double-release it.
        pbx.paging_system.end_page.assert_not_called()

    @patch("pbx.core.paging_handler.time")
    def test_source_rtp_overrides_caller_rtp(self, mock_time) -> None:
        """An originated page relays the callee's RTP, not the caller's."""
        pbx = _make_pbx_core()
        handler = PagingHandler(pbx)
        call_obj = _make_call()
        call_obj.state.value = "ended"
        call_obj.call_id = "call-1"

        dac_device = {
            "device_id": "dac-0",
            "device_type": "CyberData",
            "sip_uri": "sip:dac@10.0.0.50",
            "ip_address": "10.0.0.50",
            "port": 5060,
        }

        handler._paging_session(
            "call-1",
            call_obj,
            dac_device,
            _make_page_info(),
            source_rtp={"address": "192.168.1.99", "port": 40000},
        )

        pbx.rtp_relay.set_endpoints.assert_called_once()
        source_endpoint = pbx.rtp_relay.set_endpoints.call_args[0][1]
        assert source_endpoint == ("192.168.1.99", 40000)

    @patch("pbx.core.paging_handler.time")
    def test_missing_sip_config_returns_early(self, mock_time) -> None:
        """If DAC device is missing SIP URI or IP, should return early."""
        pbx = _make_pbx_core()
        handler = PagingHandler(pbx)
        call_obj = _make_call()

        dac_device = {
            "device_id": "dac-0",
            "device_type": "CyberData",
            "sip_uri": None,
            "ip_address": None,
        }
        page_info = _make_page_info()

        handler._paging_session("call-1", call_obj, dac_device, page_info)
        pbx.logger.error.assert_called()
        pbx.paging_system.end_page.assert_not_called()

    @patch("pbx.core.paging_handler.time")
    def test_missing_sip_uri_only(self, mock_time) -> None:
        """If only sip_uri is missing, should still return early."""
        pbx = _make_pbx_core()
        handler = PagingHandler(pbx)
        call_obj = _make_call()

        dac_device = {
            "device_id": "dac-0",
            "device_type": "CyberData",
            "sip_uri": None,
            "ip_address": "10.0.0.50",
            "port": 5060,
        }
        page_info = _make_page_info()

        handler._paging_session("call-1", call_obj, dac_device, page_info)
        pbx.logger.error.assert_called()

    @patch("pbx.core.paging_handler.time")
    def test_missing_ip_address_only(self, mock_time) -> None:
        """If only ip_address is missing, should still return early."""
        pbx = _make_pbx_core()
        handler = PagingHandler(pbx)
        call_obj = _make_call()

        dac_device = {
            "device_id": "dac-0",
            "device_type": "CyberData",
            "sip_uri": "sip:dac@10.0.0.50",
            "ip_address": None,
            "port": 5060,
        }
        page_info = _make_page_info()

        handler._paging_session("call-1", call_obj, dac_device, page_info)
        pbx.logger.error.assert_called()

    @patch("pbx.core.paging_handler.time")
    def test_caller_rtp_present_logs_routing(self, mock_time) -> None:
        """When caller RTP info exists, should log audio routing details."""
        pbx = _make_pbx_core()
        handler = PagingHandler(pbx)
        call_obj = _make_call()
        call_obj.state.value = "ended"  # End immediately

        dac_device = {
            "device_id": "dac-0",
            "device_type": "CyberData",
            "sip_uri": "sip:dac@10.0.0.50",
            "ip_address": "10.0.0.50",
            "port": 5060,
        }
        page_info = _make_page_info()

        handler._paging_session("call-1", call_obj, dac_device, page_info)
        # Should log caller RTP info
        pbx.logger.info.assert_called()

    @patch("pbx.core.paging_handler.time")
    def test_no_caller_rtp_skips_routing_log(self, mock_time) -> None:
        """When caller RTP info is absent, routing log should be skipped."""
        pbx = _make_pbx_core()
        handler = PagingHandler(pbx)
        call_obj = _make_call()
        call_obj.caller_rtp = None
        call_obj.state.value = "ended"

        dac_device = {
            "device_id": "dac-0",
            "device_type": "CyberData",
            "sip_uri": "sip:dac@10.0.0.50",
            "ip_address": "10.0.0.50",
            "port": 5060,
        }
        page_info = _make_page_info()

        handler._paging_session("call-1", call_obj, dac_device, page_info)
        # No source RTP to relay, so no relay is configured -- but the DAC
        # dialog is still opened and closed.
        pbx.rtp_relay.set_endpoints.assert_not_called()
        assert pbx.sip_server._send_message.call_count == 2

    @patch("pbx.core.paging_handler.time")
    def test_default_port_when_not_configured(self, mock_time) -> None:
        """If port is not in DAC config, should default to 5060."""
        pbx = _make_pbx_core()
        handler = PagingHandler(pbx)
        call_obj = _make_call()
        call_obj.state.value = "ended"

        dac_device = {
            "device_id": "dac-0",
            "device_type": "CyberData",
            "sip_uri": "sip:dac@10.0.0.50",
            "ip_address": "10.0.0.50",
            # No "port" key
        }
        page_info = _make_page_info()

        handler._paging_session("call-1", call_obj, dac_device, page_info)
        # Should not crash; dac_port defaults to 5060
        dac_addr = pbx.sip_server._send_message.call_args[0][1]
        assert dac_addr == ("10.0.0.50", 5060)

    @patch("pbx.core.paging_handler.time")
    def test_error_in_session_logged(self, mock_time) -> None:
        """On error in paging session, error should be logged."""
        pbx = _make_pbx_core()
        handler = PagingHandler(pbx)
        call_obj = _make_call()

        # Cause a TypeError
        dac_device = {
            "device_id": "dac-0",
            "device_type": "CyberData",
            "sip_uri": "sip:dac@10.0.0.50",
            "ip_address": "10.0.0.50",
            "port": 5060,
        }
        page_info = _make_page_info()

        # Make state.value raise a TypeError to trigger the except block
        type(call_obj.state).value = property(lambda self: (_ for _ in ()).throw(TypeError("bad")))

        handler._paging_session("call-1", call_obj, dac_device, page_info)
        pbx.logger.error.assert_called()

    @patch("pbx.core.paging_handler.time")
    def test_key_error_in_session_logged(self, mock_time) -> None:
        """On KeyError in paging session, error should be logged."""
        pbx = _make_pbx_core()
        handler = PagingHandler(pbx)
        call_obj = _make_call()
        call_obj.state.value = "ended"

        # Missing required keys to trigger KeyError
        dac_device = {
            "device_id": "dac-0",
            "device_type": "CyberData",
            "sip_uri": "sip:dac@10.0.0.50",
            "ip_address": "10.0.0.50",
            "port": 5060,
        }
        # Cause KeyError via caller_rtp access
        call_obj.caller_rtp = {"bad_key": "value"}

        page_info = _make_page_info()

        handler._paging_session("call-1", call_obj, dac_device, page_info)
        pbx.logger.error.assert_called()


# ---------------------------------------------------------------------------
# start_test_page / _begin_originated_page
# ---------------------------------------------------------------------------


def _make_test_page_pbx() -> MagicMock:
    """PBXCore mock wired for an admin-initiated test page."""
    pbx = _make_pbx_core()
    pbx.paging_system.enabled = True
    pbx.paging_system.is_paging_extension.return_value = True
    pbx.paging_system.all_call_extension = "700"
    pbx.paging_system.get_zone_for_extension.return_value = {"extension": "701", "name": "Zone 1"}
    return pbx


@pytest.mark.unit
class TestStartTestPage:
    """Tests for start_test_page."""

    def test_originates_call_to_announcer(self) -> None:
        """A test page rings the announcing extension, not the zone."""
        pbx = _make_test_page_pbx()
        originated = MagicMock()
        originated.call_id = "call-1"
        pbx.call_originator.originate_call.return_value = originated
        handler = PagingHandler(pbx)

        result = handler.start_test_page("1001", "701")

        assert result["call_id"] == "call-1"
        assert result["zone"] == "701"
        # The destination is the phone that will make the announcement.
        assert pbx.call_originator.originate_call.call_args[0][1] == "1001"
        # The page itself has not started -- that waits for the answer.
        pbx.paging_system.initiate_page.assert_not_called()

    def test_rejects_when_disabled(self) -> None:
        """A disabled paging system should not ring anyone."""
        pbx = _make_test_page_pbx()
        pbx.paging_system.enabled = False
        handler = PagingHandler(pbx)

        with pytest.raises(ValueError, match="not enabled"):
            handler.start_test_page("1001", "701")
        pbx.call_originator.originate_call.assert_not_called()

    def test_rejects_non_paging_extension(self) -> None:
        """A destination outside the paging range is rejected before ringing."""
        pbx = _make_test_page_pbx()
        pbx.paging_system.is_paging_extension.return_value = False
        handler = PagingHandler(pbx)

        with pytest.raises(ValueError, match="not a paging extension"):
            handler.start_test_page("1001", "1002")
        pbx.call_originator.originate_call.assert_not_called()

    def test_rejects_unconfigured_zone_before_ringing(self) -> None:
        """Ringing a phone that would then connect to nothing is the worse failure."""
        pbx = _make_test_page_pbx()
        pbx.paging_system.get_zone_for_extension.return_value = None
        handler = PagingHandler(pbx)

        with pytest.raises(ValueError, match="No paging zone configured"):
            handler.start_test_page("1001", "705")
        pbx.call_originator.originate_call.assert_not_called()

    def test_all_call_extension_needs_no_zone_lookup(self) -> None:
        """The all-call extension is valid even though it is not itself a zone."""
        pbx = _make_test_page_pbx()
        pbx.paging_system.get_zone_for_extension.return_value = None
        pbx.call_originator.originate_call.return_value = MagicMock(call_id="call-1")
        handler = PagingHandler(pbx)

        result = handler.start_test_page("1001", "700")
        assert result["zone"] == "700"

    def test_failed_origination_raises(self) -> None:
        """If the call could not be placed at all, report it rather than lying."""
        pbx = _make_test_page_pbx()
        pbx.call_originator.originate_call.return_value = None
        handler = PagingHandler(pbx)

        with pytest.raises(ValueError, match="Could not place a call"):
            handler.start_test_page("1001", "701")


@pytest.mark.unit
class TestBeginOriginatedPage:
    """Tests for the answer callback that opens the page."""

    @patch("threading.Thread")
    def test_opens_page_and_relays_callee_rtp(self, mock_thread_cls) -> None:
        """On answer, the page starts and the announcer's RTP feeds the DAC."""
        pbx = _make_test_page_pbx()
        pbx.paging_system.initiate_page.return_value = "page-1"
        pbx.paging_system.get_page_info.return_value = _make_page_info(with_dac=True)
        handler = PagingHandler(pbx)

        call_obj = MagicMock()
        call_obj.call_id = "call-1"
        call_obj.to_extension = "1001"
        call_obj.callee_rtp = {"address": "192.168.1.50", "port": 40000}

        handler._begin_originated_page(call_obj, "701")

        pbx.paging_system.initiate_page.assert_called_once_with("1001", "701")
        assert call_obj.paging_active is True
        assert call_obj.page_id == "page-1"
        mock_thread_cls.assert_called_once()
        # The originated leg's media lives on callee_rtp, not caller_rtp.
        assert mock_thread_cls.call_args.kwargs["kwargs"]["source_rtp"] == call_obj.callee_rtp

    @patch("threading.Thread")
    def test_no_dac_device_leaves_call_up_without_session(self, mock_thread_cls) -> None:
        """Without hardware the announcer stays connected, but no session runs."""
        pbx = _make_test_page_pbx()
        pbx.paging_system.initiate_page.return_value = "page-1"
        pbx.paging_system.get_page_info.return_value = _make_page_info(with_dac=False)
        handler = PagingHandler(pbx)

        call_obj = MagicMock()
        call_obj.call_id = "call-1"
        call_obj.to_extension = "1001"

        handler._begin_originated_page(call_obj, "701")

        mock_thread_cls.assert_not_called()
        pbx.end_call.assert_not_called()
        pbx.logger.warning.assert_called()

    @patch("threading.Thread")
    def test_page_initiation_failure_ends_the_call(self, mock_thread_cls) -> None:
        """A phone left connected to a page that never started is a dead call."""
        pbx = _make_test_page_pbx()
        pbx.paging_system.initiate_page.return_value = None
        handler = PagingHandler(pbx)

        call_obj = MagicMock()
        call_obj.call_id = "call-1"
        call_obj.to_extension = "1001"

        handler._begin_originated_page(call_obj, "701")

        pbx.end_call.assert_called_once_with("call-1")
        mock_thread_cls.assert_not_called()

    @patch("threading.Thread")
    def test_missing_page_info_ends_the_call(self, mock_thread_cls) -> None:
        """Same for a page that initiated but cannot be looked up."""
        pbx = _make_test_page_pbx()
        pbx.paging_system.initiate_page.return_value = "page-1"
        pbx.paging_system.get_page_info.return_value = None
        handler = PagingHandler(pbx)

        call_obj = MagicMock()
        call_obj.call_id = "call-1"
        call_obj.to_extension = "1001"

        handler._begin_originated_page(call_obj, "701")

        pbx.end_call.assert_called_once_with("call-1")
