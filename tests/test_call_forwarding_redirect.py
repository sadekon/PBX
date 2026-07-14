"""Tests for pbx/core/call_router.py - SIP 3xx redirect (call forwarding) handling."""

from typing import Any
from unittest.mock import MagicMock

import pytest

from pbx.core.call import Call
from pbx.core.call_router import CallRouter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pbx_core() -> MagicMock:
    """Build a minimal mock PBXCore sufficient to dial an extension leg."""
    pbx = MagicMock()

    config_data: dict[str, Any] = {
        "server.sip_port": 5060,
        "voicemail.no_answer_timeout": 30,
        "sip.caller_id.send_p_asserted_identity": True,
        "sip.caller_id.send_remote_party_id": True,
        "sip.device.send_mac_address": True,
        "sip.device.accept_mac_in_invite": True,
    }
    pbx.config = MagicMock()
    pbx.config.get.side_effect = lambda key, default=None: config_data.get(key, default)

    pbx.logger = MagicMock()
    pbx.extension_registry = MagicMock()
    pbx.rtp_relay = MagicMock()
    pbx.rtp_relay.active_relays = {}
    pbx.sip_server = MagicMock()
    pbx._get_server_ip.return_value = "10.0.0.1"
    pbx._get_phone_user_agent.return_value = "Generic/1.0"
    pbx._detect_phone_model.return_value = None
    pbx._get_dtmf_payload_type.return_value = 101
    pbx._get_ilbc_mode.return_value = 30
    pbx.registered_phones_db = MagicMock()
    pbx.registered_phones_db.get_by_extension.return_value = []
    pbx.webrtc_gateway = None
    pbx.voicemail_system = MagicMock()

    return pbx


def _make_original_invite(from_ext: str = "1001", to_ext: str = "1002") -> MagicMock:
    msg = MagicMock()
    msg.body = ""
    headers = {
        "From": f"<sip:{from_ext}@pbx.local>;tag=abc123",
        "To": f"<sip:{to_ext}@pbx.local>",
        "Call-ID": "call-1",
        "CSeq": "1 INVITE",
    }
    msg.get_header.side_effect = headers.get
    return msg


def _make_call(from_ext: str = "1001", to_ext: str = "1002") -> Call:
    call = Call("call-1", from_ext, to_ext)
    call.start()
    call.original_invite = _make_original_invite(from_ext, to_ext)
    call.caller_addr = ("192.168.1.100", 5060)
    call.rtp_ports = (20000, 20001)
    return call


# ===========================================================================
# CallRouter.handle_redirect
# ===========================================================================


@pytest.mark.unit
class TestHandleRedirect:
    """Tests for CallRouter.handle_redirect() - SIP 3xx call forwarding."""

    def test_redirect_to_registered_extension_updates_to_extension(self) -> None:
        pbx = _make_pbx_core()
        call = _make_call()
        pbx.call_manager.get_call.return_value = call

        target_ext_obj = MagicMock()
        target_ext_obj.address = ("10.0.0.9", 5060)
        target_ext_obj.name = "Forward Target"
        pbx.extension_registry.get.return_value = target_ext_obj

        router = CallRouter(pbx)
        router.handle_redirect("call-1", "<sip:1003@10.0.0.9:5060>")

        assert call.to_extension == "1003"
        assert call.callee_addr == ("10.0.0.9", 5060)
        assert call.callee_invite is not None
        assert call.invite_transaction is not None
        assert call.no_answer_timer is not None
        assert call.redirect_count == 1
        pbx.sip_server._send_message.assert_called()

    def test_redirect_no_contact_falls_back_to_voicemail(self) -> None:
        pbx = _make_pbx_core()
        call = _make_call()
        pbx.call_manager.get_call.return_value = call

        router = CallRouter(pbx)
        router._handle_no_answer = MagicMock()
        router.handle_redirect("call-1", None)

        router._handle_no_answer.assert_called_once_with("call-1")
        assert call.to_extension == "1002"

    def test_redirect_pointing_at_current_callee_is_treated_as_loop(self) -> None:
        pbx = _make_pbx_core()
        call = _make_call()
        pbx.call_manager.get_call.return_value = call

        router = CallRouter(pbx)
        router._handle_no_answer = MagicMock()
        router.handle_redirect("call-1", "<sip:1002@10.0.0.9:5060>")

        router._handle_no_answer.assert_called_once_with("call-1")

    def test_redirect_back_to_caller_is_treated_as_loop(self) -> None:
        pbx = _make_pbx_core()
        call = _make_call()
        pbx.call_manager.get_call.return_value = call

        router = CallRouter(pbx)
        router._handle_no_answer = MagicMock()
        router.handle_redirect("call-1", "<sip:1001@10.0.0.9:5060>")

        router._handle_no_answer.assert_called_once_with("call-1")

    def test_redirect_exceeds_max_redirects_falls_back(self) -> None:
        pbx = _make_pbx_core()
        call = _make_call()
        call.redirect_count = CallRouter.MAX_REDIRECTS
        pbx.call_manager.get_call.return_value = call

        router = CallRouter(pbx)
        router._handle_no_answer = MagicMock()
        router.handle_redirect("call-1", "<sip:1099@10.0.0.9:5060>")

        router._handle_no_answer.assert_called_once_with("call-1")

    def test_redirect_ignored_when_already_connected(self) -> None:
        pbx = _make_pbx_core()
        call = _make_call()
        call.connect()
        pbx.call_manager.get_call.return_value = call

        router = CallRouter(pbx)
        router._handle_no_answer = MagicMock()
        router.handle_redirect("call-1", "<sip:1003@10.0.0.9:5060>")

        router._handle_no_answer.assert_not_called()
        assert call.to_extension == "1002"

    def test_redirect_ignored_when_already_routed_to_voicemail(self) -> None:
        pbx = _make_pbx_core()
        call = _make_call()
        call.routed_to_voicemail = True
        pbx.call_manager.get_call.return_value = call

        router = CallRouter(pbx)
        router._handle_no_answer = MagicMock()
        router.handle_redirect("call-1", "<sip:1003@10.0.0.9:5060>")

        router._handle_no_answer.assert_not_called()

    def test_redirect_unresolvable_extension_falls_back_to_voicemail(self) -> None:
        pbx = _make_pbx_core()
        call = _make_call()
        pbx.call_manager.get_call.return_value = call
        pbx.extension_registry.get.return_value = None  # target not registered

        router = CallRouter(pbx)
        router._handle_no_answer = MagicMock()
        router.handle_redirect("call-1", "<sip:1003@10.0.0.9:5060>")

        router._handle_no_answer.assert_called_once_with("call-1")
        assert call.to_extension == "1002"

    def test_redirect_call_not_found_is_noop(self) -> None:
        pbx = _make_pbx_core()
        pbx.call_manager.get_call.return_value = None

        router = CallRouter(pbx)
        # Should not raise
        router.handle_redirect("missing-call", "<sip:1003@10.0.0.9:5060>")

    def test_redirect_cancels_prior_no_answer_timer(self) -> None:
        pbx = _make_pbx_core()
        call = _make_call()
        pbx.call_manager.get_call.return_value = call
        stale_timer = MagicMock()
        call.no_answer_timer = stale_timer

        target_ext_obj = MagicMock()
        target_ext_obj.address = ("10.0.0.9", 5060)
        pbx.extension_registry.get.return_value = target_ext_obj

        router = CallRouter(pbx)
        router.handle_redirect("call-1", "<sip:1003@10.0.0.9:5060>")

        stale_timer.cancel.assert_called_once()
