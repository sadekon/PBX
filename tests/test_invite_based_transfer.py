"""Tests for INVITE-based call transfer detection (phones without REFER support).

Some SIP phones don't send REFER to transfer a call. Instead they hold the
current call, dial the transfer destination as a brand-new INVITE (new
Call-ID), then hang up one of the two legs. These tests cover:

- CallRouter.route_call linking a new call to an existing held call
- PBXCore.handle_invite_transfer_hangup completing or deferring the bridge
- PBXCore.handle_callee_answer completing a deferred bridge once the
  destination answers
- Non-transferor hangups and cancelled consultations leaving things unlinked
"""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from pbx.core.call import Call, CallManager, CallState
from pbx.core.call_router import CallRouter
from pbx.core.pbx import PBXCore

from tests.test_call_router_coverage import CALLER_ADDR, _make_invite_message, _make_pbx_core

A_ADDR = ("192.168.1.101", 5060)
B_ADDR = ("192.168.1.102", 5060)
C_ADDR = ("192.168.1.103", 5060)


def _make_pbx(call_manager: CallManager) -> MagicMock:
    """Build a MagicMock PBXCore stand-in with a real CallManager, suitable
    for invoking PBXCore's unbound instance methods directly."""
    pbx = MagicMock()
    pbx.call_manager = call_manager
    pbx.rtp_relay = MagicMock()
    pbx.moh_system = MagicMock()
    pbx.cdr_system = MagicMock()
    pbx.webhook_system = MagicMock()
    pbx.logger = MagicMock()
    pbx.sip_server = MagicMock()
    pbx._get_server_ip.return_value = "10.0.0.100"
    # `pbx` is a MagicMock standing in for `self`; methods under test call
    # `self._complete_invite_transfer(...)` internally, which would otherwise
    # resolve to an inert auto-generated Mock attribute instead of the real
    # implementation. Wire it to actually dispatch to PBXCore's real method.
    pbx._complete_invite_transfer = MagicMock(
        side_effect=lambda *a, **kw: PBXCore._complete_invite_transfer(pbx, *a, **kw)
    )
    return pbx


def _make_call(
    manager: CallManager,
    call_id: str,
    from_ext: str,
    to_ext: str,
    *,
    state: CallState = CallState.CONNECTED,
    caller_addr: tuple[str, int] | None = None,
    callee_addr: tuple[str, int] | None = None,
    callee_rtp: dict[str, Any] | None = None,
) -> Call:
    call = manager.create_call(call_id, from_ext, to_ext)
    call.state = state
    call.caller_addr = caller_addr
    call.callee_addr = callee_addr
    call.callee_rtp = callee_rtp
    call.caller_rtp = {"address": "1.2.3.4", "port": 30000}
    call.rtp_ports = (20000, 20001)
    return call


# ===========================================================================
# CallRouter.route_call - transfer link detection
# ===========================================================================


@pytest.mark.unit
class TestCallRouterTransferLinking:
    """Tests for INVITE-based transfer link detection in route_call."""

    def test_new_invite_links_to_single_held_call(self) -> None:
        pbx = _make_pbx_core()
        pbx.call_manager = CallManager()
        held = _make_call(pbx.call_manager, "call1", "1001", "1002", state=CallState.HOLD)

        router = CallRouter(pbx)
        msg = _make_invite_message(from_ext="1001", to_ext="1003", call_id="call2", body="")

        result = router.route_call(
            "<sip:1001@pbx.local>",
            "<sip:1003@pbx.local>",
            "call2",
            msg,
            CALLER_ADDR,
        )

        assert result is True
        call2 = pbx.call_manager.get_call("call2")
        assert call2 is not None
        assert call2.linked_call_id == "call1"
        assert call2.is_transfer_consult is True
        assert held.linked_call_id == "call2"

    def test_new_invite_from_different_extension_not_linked(self) -> None:
        pbx = _make_pbx_core()
        pbx.call_manager = CallManager()
        _make_call(pbx.call_manager, "call1", "1001", "1002", state=CallState.HOLD)

        router = CallRouter(pbx)
        msg = _make_invite_message(from_ext="1005", to_ext="1003", call_id="call2", body="")

        router.route_call(
            "<sip:1005@pbx.local>",
            "<sip:1003@pbx.local>",
            "call2",
            msg,
            CALLER_ADDR,
        )

        call2 = pbx.call_manager.get_call("call2")
        assert call2 is not None
        assert call2.linked_call_id is None
        assert call2.is_transfer_consult is False

    def test_new_invite_with_no_held_call_not_linked(self) -> None:
        pbx = _make_pbx_core()
        pbx.call_manager = CallManager()
        # Existing call for 1001 exists but is CONNECTED, not on hold.
        _make_call(pbx.call_manager, "call1", "1001", "1002", state=CallState.CONNECTED)

        router = CallRouter(pbx)
        msg = _make_invite_message(from_ext="1001", to_ext="1003", call_id="call2", body="")

        router.route_call(
            "<sip:1001@pbx.local>",
            "<sip:1003@pbx.local>",
            "call2",
            msg,
            CALLER_ADDR,
        )

        call2 = pbx.call_manager.get_call("call2")
        assert call2 is not None
        assert call2.linked_call_id is None

    def test_ambiguous_multiple_held_calls_not_linked(self) -> None:
        pbx = _make_pbx_core()
        pbx.call_manager = CallManager()
        _make_call(pbx.call_manager, "call1", "1001", "1002", state=CallState.HOLD)
        _make_call(pbx.call_manager, "call1b", "1004", "1001", state=CallState.HOLD)

        router = CallRouter(pbx)
        msg = _make_invite_message(from_ext="1001", to_ext="1003", call_id="call2", body="")

        router.route_call(
            "<sip:1001@pbx.local>",
            "<sip:1003@pbx.local>",
            "call2",
            msg,
            CALLER_ADDR,
        )

        call2 = pbx.call_manager.get_call("call2")
        assert call2 is not None
        assert call2.linked_call_id is None


# ===========================================================================
# PBXCore.handle_invite_transfer_hangup
# ===========================================================================


@pytest.mark.unit
class TestHandleInviteTransferHangup:
    """Tests for detecting and completing/deferring transfer on BYE."""

    def test_immediate_bridge_when_transferor_is_caller_of_held_call(self) -> None:
        """A originally called B (A is from_extension/caller of the held
        call), then A calls C. B (callee) must be preserved; A is replaced
        by C on the caller side."""
        cm = CallManager()
        original = _make_call(
            cm,
            "call1",
            "1001",
            "1002",
            state=CallState.HOLD,
            caller_addr=A_ADDR,
            callee_addr=B_ADDR,
            callee_rtp={"address": "9.9.9.9", "port": 35000},
        )
        consult = _make_call(
            cm,
            "call2",
            "1001",
            "1003",
            state=CallState.CONNECTED,
            caller_addr=A_ADDR,
            callee_addr=C_ADDR,
            callee_rtp={"address": "10.0.0.3", "port": 40000},
        )
        consult.is_transfer_consult = True
        original.linked_call_id = "call2"
        consult.linked_call_id = "call1"

        pbx = _make_pbx(cm)

        handled = PBXCore.handle_invite_transfer_hangup(pbx, "call1", A_ADDR)

        assert handled is True
        # Consultation call is gone; B stays put, A is replaced by C.
        assert cm.get_call("call2") is None
        assert original.from_extension == "1003"
        assert original.caller_addr == C_ADDR
        assert original.caller_rtp == {"address": "10.0.0.3", "port": 40000}
        assert original.to_extension == "1002"
        assert original.callee_addr == B_ADDR
        assert original.state == CallState.CONNECTED
        assert original.on_hold is False
        pbx.rtp_relay.set_endpoints.assert_called_once()
        pbx.moh_system.stop_moh.assert_called_with("call1")
        pbx.sip_server._send_message.assert_called_once()

    def test_immediate_bridge_when_transferor_is_callee_of_held_call(self) -> None:
        """B originally called A (A is to_extension/callee of the held
        call), then A calls C. B (caller) must be preserved; A is replaced
        by C on the callee side."""
        cm = CallManager()
        original = _make_call(
            cm,
            "call1",
            "1002",
            "1001",
            state=CallState.HOLD,
            caller_addr=B_ADDR,
            callee_addr=A_ADDR,
        )
        consult = _make_call(
            cm,
            "call2",
            "1001",
            "1003",
            state=CallState.CONNECTED,
            caller_addr=A_ADDR,
            callee_addr=C_ADDR,
            callee_rtp={"address": "10.0.0.3", "port": 40000},
        )
        consult.is_transfer_consult = True
        original.linked_call_id = "call2"
        consult.linked_call_id = "call1"

        pbx = _make_pbx(cm)

        handled = PBXCore.handle_invite_transfer_hangup(pbx, "call2", A_ADDR)

        assert handled is True
        assert cm.get_call("call2") is None
        # B (caller) must be preserved; A (callee) replaced by C.
        assert original.from_extension == "1002"
        assert original.caller_addr == B_ADDR
        assert original.to_extension == "1003"
        assert original.callee_addr == C_ADDR
        assert original.callee_rtp == {"address": "10.0.0.3", "port": 40000}
        pbx.rtp_relay.set_endpoints.assert_called_once()

    def test_deferred_when_destination_not_yet_answered(self) -> None:
        cm = CallManager()
        original = _make_call(
            cm, "call1", "1001", "1002", state=CallState.HOLD, caller_addr=A_ADDR, callee_addr=B_ADDR
        )
        consult = _make_call(
            cm,
            "call2",
            "1001",
            "1003",
            state=CallState.RINGING,
            caller_addr=A_ADDR,
            callee_addr=None,
            callee_rtp=None,
        )
        consult.is_transfer_consult = True
        original.linked_call_id = "call2"
        consult.linked_call_id = "call1"

        pbx = _make_pbx(cm)

        handled = PBXCore.handle_invite_transfer_hangup(pbx, "call1", A_ADDR)

        assert handled is True
        assert original.pending_transfer_consult_id == "call2"
        # Neither call is torn down yet.
        assert cm.get_call("call1") is not None
        assert cm.get_call("call2") is not None
        pbx.rtp_relay.set_endpoints.assert_not_called()

    def test_non_transferor_hangup_unlinks_without_bridging(self) -> None:
        cm = CallManager()
        original = _make_call(
            cm, "call1", "1001", "1002", state=CallState.HOLD, caller_addr=A_ADDR, callee_addr=B_ADDR
        )
        consult = _make_call(
            cm,
            "call2",
            "1001",
            "1003",
            state=CallState.CONNECTED,
            caller_addr=A_ADDR,
            callee_addr=C_ADDR,
            callee_rtp={"address": "10.0.0.3", "port": 40000},
        )
        consult.is_transfer_consult = True
        original.linked_call_id = "call2"
        consult.linked_call_id = "call1"

        pbx = _make_pbx(cm)

        # B (not the transferor) hangs up call1.
        handled = PBXCore.handle_invite_transfer_hangup(pbx, "call1", B_ADDR)

        assert handled is False
        assert original.linked_call_id is None
        assert consult.linked_call_id is None
        # Both calls remain untouched otherwise.
        assert cm.get_call("call1") is not None
        assert cm.get_call("call2") is not None

    def test_stale_link_returns_false_and_clears(self) -> None:
        cm = CallManager()
        original = _make_call(
            cm, "call1", "1001", "1002", state=CallState.HOLD, caller_addr=A_ADDR, callee_addr=B_ADDR
        )
        original.linked_call_id = "does-not-exist"

        pbx = _make_pbx(cm)

        handled = PBXCore.handle_invite_transfer_hangup(pbx, "call1", A_ADDR)

        assert handled is False
        assert original.linked_call_id is None

    def test_unlinked_call_returns_false(self) -> None:
        cm = CallManager()
        _make_call(cm, "call1", "1001", "1002", state=CallState.CONNECTED)

        pbx = _make_pbx(cm)

        handled = PBXCore.handle_invite_transfer_hangup(pbx, "call1", A_ADDR)

        assert handled is False


# ===========================================================================
# PBXCore.handle_callee_answer - deferred bridge completion
# ===========================================================================


@pytest.mark.unit
class TestHandleCalleeAnswerDeferredTransfer:
    """Tests for completing a deferred INVITE-based transfer on answer."""

    @patch("pbx.sip.sdp.SDPSession")
    def test_deferred_transfer_completes_on_answer(self, mock_sdp_cls: MagicMock) -> None:
        cm = CallManager()
        original = _make_call(
            cm,
            "call1",
            "1001",
            "1002",
            state=CallState.HOLD,
            caller_addr=A_ADDR,
            callee_addr=B_ADDR,
            callee_rtp={"address": "9.9.9.9", "port": 35000},
        )
        original.pending_transfer_consult_id = "call2"
        consult = _make_call(
            cm,
            "call2",
            "1001",
            "1003",
            state=CallState.RINGING,
            caller_addr=A_ADDR,
            callee_addr=None,
            callee_rtp=None,
        )
        consult.is_transfer_consult = True
        consult.linked_call_id = "call1"
        original.linked_call_id = "call2"

        pbx = _make_pbx(cm)

        sdp_obj = MagicMock()
        sdp_obj.get_audio_info.return_value = {"address": "10.0.0.3", "port": 40000}
        mock_sdp_cls.return_value = sdp_obj

        response = MagicMock()
        response.body = "v=0\r\n"

        PBXCore.handle_callee_answer(pbx, "call2", response, C_ADDR)

        # Consultation call ended; original call now bridged to C (A was the
        # from_extension/caller of the held call, so that side is replaced).
        assert cm.get_call("call2") is None
        assert original.from_extension == "1003"
        assert original.caller_addr == C_ADDR
        assert original.to_extension == "1002"
        assert original.callee_addr == B_ADDR
        assert original.state == CallState.CONNECTED
        pbx.rtp_relay.set_endpoints.assert_called_once()

    @patch("pbx.sip.sdp.SDPSession")
    def test_normal_answer_without_pending_transfer_unaffected(
        self, mock_sdp_cls: MagicMock
    ) -> None:
        cm = CallManager()
        call = _make_call(
            cm, "call1", "1001", "1002", state=CallState.CALLING, caller_addr=A_ADDR, callee_addr=None
        )

        pbx = _make_pbx(cm)
        pbx.webrtc_signaling = None

        sdp_obj = MagicMock()
        sdp_obj.get_audio_info.return_value = {"address": "10.0.0.2", "port": 40001}
        mock_sdp_cls.return_value = sdp_obj

        response = MagicMock()
        response.body = "v=0\r\n"

        PBXCore.handle_callee_answer(pbx, "call1", response, B_ADDR)

        # Normal answer path proceeds; call still present.
        assert cm.get_call("call1") is not None
        assert call.callee_addr == B_ADDR


# ===========================================================================
# CANCEL of an unanswered consultation call
# ===========================================================================


@pytest.mark.unit
class TestCancelPendingConsultation:
    """Tests for _handle_cancel resuming the original call when the
    transferor abandons the consultation call before it's answered."""

    def test_cancel_resumes_linked_held_call(self) -> None:
        from pbx.sip.server import SIPServer

        cm = CallManager()
        original = _make_call(
            cm, "call1", "1001", "1002", state=CallState.HOLD, caller_addr=A_ADDR, callee_addr=B_ADDR
        )
        consult = _make_call(
            cm, "call2", "1001", "1003", state=CallState.RINGING, caller_addr=A_ADDR
        )
        consult.linked_call_id = "call1"
        original.linked_call_id = "call2"
        consult.original_invite = MagicMock()
        consult.invite_transaction = None
        consult.callee_addr = None
        consult.callee_invite = None

        pbx = _make_pbx(cm)
        pbx.end_call = MagicMock(side_effect=lambda cid: cm.end_call(cid))

        server = SIPServer.__new__(SIPServer)
        server.pbx_core = pbx
        server.logger = MagicMock()
        server._send_message = MagicMock()

        message = MagicMock()
        message.get_header.side_effect = {"Call-ID": "call2"}.get

        server._handle_cancel(message, A_ADDR)

        assert original.linked_call_id is None
        assert original.state == CallState.CONNECTED
        assert original.on_hold is False
        pbx.moh_system.stop_moh.assert_called_with("call1")
        assert cm.get_call("call2") is None
