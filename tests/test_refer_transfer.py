"""Tests for REFER-based call transfer (RFC 3515) with Replaces (RFC 3891).

Covers the flow observed from real Zultys ZIP phones:

- Refer-To parsing, including URL-encoded embedded Replaces headers
- Attended transfer: REFER with Replaces bridging the original call's
  remaining party with the consultation call's destination
- Semi-attended/deferred: REFER before the destination answers
- Blind transfer: REFER without Replaces, PBX-originated destination leg
- Post-bridge hangup propagation across the two bridged leg records
- Absorbing the transferor's own BYEs during and after the transfer
- Aborting a pending transfer when the destination declines or times out
- The relay replace_endpoint primitive (learned-state reset)
"""

from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

from pbx.core.call import Call, CallManager, CallState
from pbx.core.call_router import CallRouter
from pbx.core.transfer_handler import TransferHandler
from pbx.sip.server import SIPServer

A_ADDR = ("192.168.10.139", 5060)  # Transferor (referrer)
B_ADDR = ("192.168.10.155", 5060)  # Transferee (stays on the call)
C_ADDR = ("192.168.10.140", 5061)  # Transfer destination

B_RTP = {"address": "192.168.10.155", "port": 3008}
C_RTP = {"address": "192.168.10.140", "port": 3002}

# Refer-To exactly as captured from a Zultys ZIP 33G attended transfer
CAPTURE_REFER_TO = (
    "<sip:1517@192.168.1.14:5060;user=phone?Replaces=0_3666015387%40192.168.10.139"
    "%3Bto-tag%3D92aec109%3Bfrom-tag%3D3228926622>"
)


def _make_pbx(call_manager: CallManager) -> MagicMock:
    """Build a MagicMock PBXCore stand-in with a real CallManager, suitable
    for exercising CallRouter/TransferHandler logic bound to it directly."""
    pbx = MagicMock()
    pbx.call_manager = call_manager
    pbx.rtp_relay = MagicMock()
    pbx.moh_system = MagicMock()
    pbx.cdr_system = MagicMock()
    pbx.webhook_system = MagicMock()
    pbx.logger = MagicMock()
    pbx.sip_server = MagicMock()
    pbx._get_server_ip.return_value = "192.168.1.14"
    pbx.config.get.side_effect = lambda key, default=None: {
        "server.sip_port": 5060,
        "voicemail.no_answer_timeout": 30,
    }.get(key, default)
    pbx._get_compatible_codecs.return_value = ["0", "8"]
    pbx._get_dtmf_payload_type.return_value = 101
    pbx._get_ilbc_mode.return_value = 30
    # handle_callee_answer now lives on CallRouter; wire the mock's method to
    # a real CallRouter instance so tests that call it directly (below)
    # exercise the real answer-flow logic instead of a bare mock no-op. The
    # rest of pbx.call_router stays a plain MagicMock for tests asserting
    # other delegate calls on it. Likewise pbx.transfer_handler stays a bare
    # MagicMock -- tests needing real TransferHandler behavior construct
    # their own local TransferHandler(pbx) instance instead (see
    # TestBridgeAttendedTransfer et al.).
    pbx.call_router.handle_callee_answer.side_effect = CallRouter(pbx).handle_callee_answer
    return pbx


def _make_server(pbx: MagicMock) -> SIPServer:
    """Build a SIPServer wired to a mock PBXCore with send methods mocked."""
    server = SIPServer.__new__(SIPServer)
    server.pbx_core = pbx
    server.logger = MagicMock()
    server._send_message = MagicMock()  # type: ignore[method-assign]
    server._send_response = MagicMock()  # type: ignore[method-assign]
    return server


def _make_call(
    manager: CallManager,
    call_id: str,
    from_ext: str,
    to_ext: str,
    *,
    state: CallState = CallState.CONNECTED,
    caller_addr: tuple[str, int] | None = None,
    callee_addr: tuple[str, int] | None = None,
    caller_rtp: dict[str, Any] | None = None,
    callee_rtp: dict[str, Any] | None = None,
) -> Call:
    call = manager.create_call(call_id, from_ext, to_ext)
    call.state = state
    call.caller_addr = caller_addr
    call.callee_addr = callee_addr
    call.callee_rtp = callee_rtp
    call.caller_rtp = caller_rtp or {"address": "1.2.3.4", "port": 30000}
    call.rtp_ports = (20000, 20001)
    return call


def _attended_pair(cm: CallManager, *, consult_answered: bool = True) -> tuple[Call, Call]:
    """Original A->B call on hold plus A->C consultation call."""
    original = _make_call(
        cm,
        "call1",
        "1513",
        "1512",
        state=CallState.HOLD,
        caller_addr=A_ADDR,
        callee_addr=B_ADDR,
        callee_rtp=B_RTP,
    )
    original.original_invite = MagicMock()
    original.original_invite.get_header.side_effect = {
        "From": '"Test" <sip:1513@192.168.1.14:5060>;tag=1687173424',
        "To": "<sip:1512@192.168.1.14:5060;user=phone>",
    }.get
    original.callee_dialog_to = "<sip:1512@192.168.1.14:5060;user=phone>;tag=518364649"

    consult = _make_call(
        cm,
        "call2",
        "1513",
        "1517",
        state=CallState.CONNECTED if consult_answered else CallState.RINGING,
        caller_addr=A_ADDR,
        callee_addr=C_ADDR if consult_answered else None,
        callee_rtp=C_RTP if consult_answered else None,
    )
    consult.original_invite = MagicMock()
    consult.original_invite.get_header.side_effect = {
        "From": '"Test" <sip:1513@192.168.1.14:5060>;tag=3228926622',
        "To": "<sip:1517@192.168.1.14:5060;user=phone>",
    }.get
    consult.callee_invite = consult.original_invite
    if consult_answered:
        consult.callee_dialog_to = "<sip:1517@192.168.1.14:5060;user=phone>;tag=3972643251"
    return original, consult


# ===========================================================================
# Refer-To parsing
# ===========================================================================


@pytest.mark.unit
class TestParseReferTo:
    """Tests for SIPServer._parse_refer_to()."""

    def test_capture_string_with_replaces(self) -> None:
        destination, headers = SIPServer._parse_refer_to(CAPTURE_REFER_TO)

        assert destination == "1517"
        assert headers["replaces"] == (
            "0_3666015387@192.168.10.139;to-tag=92aec109;from-tag=3228926622"
        )

    def test_plain_refer_to_without_replaces(self) -> None:
        destination, headers = SIPServer._parse_refer_to("<sip:1003@pbx.local>")

        assert destination == "1003"
        assert headers == {}

    def test_refer_to_without_brackets(self) -> None:
        destination, headers = SIPServer._parse_refer_to("sip:1003@pbx.local;user=phone")

        assert destination == "1003"
        assert headers == {}

    def test_malformed_refer_to(self) -> None:
        destination, headers = SIPServer._parse_refer_to("not-a-uri")

        assert destination is None
        assert headers == {}

    def test_multiple_embedded_headers(self) -> None:
        refer_to = "<sip:1003@pbx?Replaces=abc%3Bto-tag%3Dx&Require=replaces>"
        destination, headers = SIPServer._parse_refer_to(refer_to)

        assert destination == "1003"
        assert headers["replaces"] == "abc;to-tag=x"
        assert headers["require"] == "replaces"


# ===========================================================================
# TransferHandler.bridge_attended_transfer
# ===========================================================================


@pytest.mark.unit
class TestBridgeAttendedTransfer:
    """Tests for bridging the original call with the consultation call."""

    def test_bridge_when_transferor_is_caller(self) -> None:
        cm = CallManager()
        original, consult = _attended_pair(cm)
        original.transfer_referrer_is_caller = True

        pbx = _make_pbx(cm)
        handler = TransferHandler(pbx)
        handler._send_bridge_reinvite = MagicMock()

        result = handler.bridge_attended_transfer(original, consult)

        assert result is True
        # Transferor (caller side "a") replaced with C on the original relay
        pbx.rtp_relay.replace_endpoint.assert_called_once_with(
            "call1", "a", (C_RTP["address"], C_RTP["port"])
        )
        pbx.moh_system.stop_moh.assert_called_with("call1")
        # Original record: A's side gone, C takes it; B untouched
        assert original.from_extension == "1517"
        assert original.caller_addr is None
        assert original.caller_rtp == C_RTP
        assert original.to_extension == "1512"
        assert original.callee_addr == B_ADDR
        assert original.state == CallState.CONNECTED
        assert original.on_hold is False
        assert original.transferred is True
        # Cross-linked leg records, both still alive
        assert original.bridged_peer_call_id == "call2"
        assert consult.bridged_peer_call_id == "call1"
        assert consult.bridge_peer_side == "a"
        assert consult.caller_addr is None
        assert cm.get_call("call1") is not None
        assert cm.get_call("call2") is not None
        # C is re-INVITEd onto the surviving relay; consult relay released
        handler._send_bridge_reinvite.assert_called_once_with(original, consult)
        pbx.rtp_relay.release_relay.assert_called_once_with("call2")
        # BYE sent to the transferor's old consult leg AND their original
        # leg -- relying on the transferor's phone to end either on its own
        # doesn't hold universally (some leave one dialog stuck, appearing
        # permanently connected/on-hold with no way to hang up or resume).
        assert pbx.sip_server._send_leg_bye.call_args_list == [
            call(consult, side="caller"),
            call(original, side="caller"),
        ]

    def test_bridge_when_transferor_is_callee(self) -> None:
        """B called A originally; A (callee of call1) transfers B to C."""
        cm = CallManager()
        original = _make_call(
            cm,
            "call1",
            "1512",
            "1513",
            state=CallState.HOLD,
            caller_addr=B_ADDR,
            callee_addr=A_ADDR,
            caller_rtp=B_RTP,
        )
        consult = _make_call(
            cm,
            "call2",
            "1513",
            "1517",
            state=CallState.CONNECTED,
            caller_addr=A_ADDR,
            callee_addr=C_ADDR,
            callee_rtp=C_RTP,
        )
        original.transfer_referrer_is_caller = False

        pbx = _make_pbx(cm)
        handler = TransferHandler(pbx)
        handler._send_bridge_reinvite = MagicMock()

        result = handler.bridge_attended_transfer(original, consult)

        assert result is True
        pbx.rtp_relay.replace_endpoint.assert_called_once_with(
            "call1", "b", (C_RTP["address"], C_RTP["port"])
        )
        # B (caller) preserved; A (callee side) replaced by C
        assert original.from_extension == "1512"
        assert original.caller_addr == B_ADDR
        assert original.to_extension == "1517"
        assert original.callee_addr is None
        assert original.callee_rtp == C_RTP
        assert consult.bridge_peer_side == "b"
        assert pbx.sip_server._send_leg_bye.call_args_list == [
            call(consult, side="caller"),
            call(original, side="callee"),
        ]

    def test_bridge_falls_back_to_shared_extension_heuristic(self) -> None:
        cm = CallManager()
        original, consult = _attended_pair(cm)
        original.transfer_referrer_is_caller = None  # not recorded

        pbx = _make_pbx(cm)
        handler = TransferHandler(pbx)
        handler._send_bridge_reinvite = MagicMock()

        result = handler.bridge_attended_transfer(original, consult)

        assert result is True
        # 1513 (A) is from_extension of both calls -> caller side replaced
        pbx.rtp_relay.replace_endpoint.assert_called_once_with(
            "call1", "a", (C_RTP["address"], C_RTP["port"])
        )

    def test_bridge_refuses_unanswered_consult(self) -> None:
        cm = CallManager()
        original, consult = _attended_pair(cm, consult_answered=False)

        pbx = _make_pbx(cm)

        result = TransferHandler(pbx).bridge_attended_transfer(original, consult)

        assert result is False
        pbx.rtp_relay.replace_endpoint.assert_not_called()

    def test_blind_leg_skips_reinvite_and_relay_release(self) -> None:
        cm = CallManager()
        original, consult = _attended_pair(cm)
        original.transfer_referrer_is_caller = True
        consult.uses_peer_relay = True  # PBX-originated blind leg

        pbx = _make_pbx(cm)
        handler = TransferHandler(pbx)
        handler._send_bridge_reinvite = MagicMock()

        result = handler.bridge_attended_transfer(original, consult)

        assert result is True
        handler._send_bridge_reinvite.assert_not_called()
        pbx.rtp_relay.release_relay.assert_not_called()


# ===========================================================================
# TransferHandler._send_bridge_reinvite
# ===========================================================================


@pytest.mark.unit
class TestSendBridgeReinvite:
    """Tests for the re-INVITE that moves C onto the surviving relay."""

    def test_reinvite_dialog_and_sdp(self) -> None:
        cm = CallManager()
        original, consult = _attended_pair(cm)

        pbx = _make_pbx(cm)

        TransferHandler(pbx)._send_bridge_reinvite(original, consult)

        assert pbx.sip_server._send_message.call_count == 1
        raw, dest = pbx.sip_server._send_message.call_args[0]
        assert dest == C_ADDR
        assert raw.startswith(f"INVITE sip:1517@{C_ADDR[0]}:{C_ADDR[1]} SIP/2.0")
        # Dialog identity of the PBX->C leg on the consultation Call-ID
        assert "Call-ID: call2" in raw or "Call-Id: call2" in raw.replace("Call-ID", "Call-Id")
        assert "tag=3228926622" in raw  # From tag as sent in the INVITE to C
        assert "tag=3972643251" in raw  # To tag from C's 200 OK
        assert "CSeq: 2 INVITE" in raw
        # SDP advertises the surviving (original) relay port
        assert "m=audio 20000 " in raw
        assert consult.pbx_leg_cseq == 2

    def test_reinvite_skipped_without_destination_address(self) -> None:
        cm = CallManager()
        original, consult = _attended_pair(cm)
        consult.callee_addr = None

        pbx = _make_pbx(cm)

        TransferHandler(pbx)._send_bridge_reinvite(original, consult)

        pbx.sip_server._send_message.assert_not_called()


# ===========================================================================
# SIPServer._handle_refer
# ===========================================================================


@pytest.mark.unit
class TestHandleReferAttended:
    """Tests for REFER with an embedded Replaces header."""

    def _refer_message(self, refer_to: str = CAPTURE_REFER_TO) -> MagicMock:
        msg = MagicMock()
        msg.get_header.side_effect = {
            "Refer-To": refer_to,
            "Call-ID": "call1",
            "Referred-By": '"Test" <sip:1513@192.168.1.14:5060>',
            "From": '"Test" <sip:1513@192.168.1.14:5060>;tag=1687173424',
            "To": "<sip:1512@192.168.1.14:5060;user=phone>;tag=306a7b6a",
        }.get
        return msg

    def test_attended_refer_bridges_answered_consult(self) -> None:
        cm = CallManager()
        original, consult = _attended_pair(cm)
        # Register the consult under the Call-ID named in the Replaces header
        cm.active_calls["0_3666015387@192.168.10.139"] = cm.active_calls.pop("call2")
        consult.call_id = "0_3666015387@192.168.10.139"

        pbx = _make_pbx(cm)
        pbx.transfer_handler.bridge_attended_transfer.return_value = True
        server = _make_server(pbx)
        server._send_transfer_notify = MagicMock()  # type: ignore[method-assign]

        server._handle_refer(self._refer_message(), A_ADDR)

        assert server._send_response.call_count == 1
        assert server._send_response.call_args[0][:2] == (202, "Accepted")
        pbx.transfer_handler.bridge_attended_transfer.assert_called_once_with(original, consult)
        assert original.transfer_referrer_addr == A_ADDR
        assert original.transfer_referrer_is_caller is True
        assert consult.transfer_referrer_addr == A_ADDR
        # Success NOTIFY sent
        sipfrags = [c.args[2] for c in server._send_transfer_notify.call_args_list]
        assert "SIP/2.0 200 OK" in sipfrags

    def test_attended_refer_defers_unanswered_consult(self) -> None:
        cm = CallManager()
        original, consult = _attended_pair(cm, consult_answered=False)
        cm.active_calls["0_3666015387@192.168.10.139"] = cm.active_calls.pop("call2")
        consult.call_id = "0_3666015387@192.168.10.139"

        pbx = _make_pbx(cm)
        server = _make_server(pbx)
        server._send_transfer_notify = MagicMock()  # type: ignore[method-assign]

        server._handle_refer(self._refer_message(), A_ADDR)

        pbx.transfer_handler.bridge_attended_transfer.assert_not_called()
        assert original.pending_transfer_consult_id == "0_3666015387@192.168.10.139"
        assert consult.is_transfer_consult is True

    def test_attended_refer_unknown_replaces_dialog(self) -> None:
        cm = CallManager()
        _attended_pair(cm)  # consult stays registered as "call2", not the Replaces id

        pbx = _make_pbx(cm)
        server = _make_server(pbx)
        server._send_transfer_notify = MagicMock()  # type: ignore[method-assign]

        server._handle_refer(self._refer_message(), A_ADDR)

        pbx.transfer_handler.bridge_attended_transfer.assert_not_called()
        sipfrags = [c.args[2] for c in server._send_transfer_notify.call_args_list]
        assert "SIP/2.0 481 Call/Transaction Does Not Exist" in sipfrags

    def test_blind_refer_originates_destination_leg(self) -> None:
        cm = CallManager()
        original, _consult = _attended_pair(cm)

        pbx = _make_pbx(cm)
        pbx.transfer_handler.start_blind_refer_transfer.return_value = True
        server = _make_server(pbx)
        server._send_transfer_notify = MagicMock()  # type: ignore[method-assign]

        server._handle_refer(self._refer_message("<sip:1517@192.168.1.14:5060>"), A_ADDR)

        pbx.transfer_handler.start_blind_refer_transfer.assert_called_once_with(
            original, True, "1517", A_ADDR, '"Test" <sip:1513@192.168.1.14:5060>'
        )
        sipfrags = [c.args[2] for c in server._send_transfer_notify.call_args_list]
        assert "SIP/2.0 200 OK" in sipfrags

    def test_refer_for_unknown_dialog(self) -> None:
        pbx = _make_pbx(CallManager())
        server = _make_server(pbx)
        server._send_transfer_notify = MagicMock()  # type: ignore[method-assign]

        server._handle_refer(self._refer_message(), A_ADDR)

        sipfrags = [c.args[2] for c in server._send_transfer_notify.call_args_list]
        assert "SIP/2.0 481 Call/Transaction Does Not Exist" in sipfrags


# ===========================================================================
# TransferHandler.start_blind_refer_transfer
# ===========================================================================


@pytest.mark.unit
class TestStartBlindReferTransfer:
    """Tests for the PBX-originated blind transfer destination leg."""

    def _registered_pbx(self, cm: CallManager) -> MagicMock:
        pbx = _make_pbx(cm)
        pbx.extension_registry.is_registered.return_value = True
        pbx.extension_registry.get_address.return_value = C_ADDR
        return pbx

    def test_blind_leg_from_referrer_as_caller(self) -> None:
        cm = CallManager()
        original, _ = _attended_pair(cm)

        pbx = self._registered_pbx(cm)

        result = TransferHandler(pbx).start_blind_refer_transfer(
            original, True, "1517", A_ADDR, referred_by="<sip:1513@pbx>"
        )

        assert result is True
        consult_id = original.pending_transfer_consult_id
        assert consult_id is not None
        consult = cm.get_call(consult_id)
        assert consult is not None
        try:
            # Transferee is B (callee of original since referrer is caller)
            assert consult.from_extension == "1512"
            assert consult.to_extension == "1517"
            assert consult.uses_peer_relay is True
            assert consult.is_transfer_consult is True
            assert consult.rtp_ports == original.rtp_ports
            assert original.transfer_referrer_is_caller is True
            assert original.transfer_referrer_addr == A_ADDR

            raw, dest = pbx.sip_server._send_message.call_args[0]
            assert dest == C_ADDR
            assert raw.startswith(f"INVITE sip:1517@{C_ADDR[0]}:{C_ADDR[1]} SIP/2.0")
            assert "Referred-By: <sip:1513@pbx>" in raw
            # SDP advertises the original call's relay port directly
            assert "m=audio 20000 " in raw
            assert "Via: SIP/2.0/UDP" in raw
            assert "Max-Forwards: 70" in raw
        finally:
            if consult.no_answer_timer:
                consult.no_answer_timer.cancel()

    def test_blind_leg_from_referrer_as_callee(self) -> None:
        cm = CallManager()
        original = _make_call(
            cm,
            "call1",
            "1512",
            "1513",
            state=CallState.HOLD,
            caller_addr=B_ADDR,
            callee_addr=A_ADDR,
            caller_rtp=B_RTP,
        )

        pbx = self._registered_pbx(cm)

        result = TransferHandler(pbx).start_blind_refer_transfer(original, False, "1517", A_ADDR)

        assert result is True
        consult = cm.get_call(original.pending_transfer_consult_id)
        assert consult is not None
        try:
            # Transferee is B (caller of original since referrer is callee)
            assert consult.from_extension == "1512"
        finally:
            if consult.no_answer_timer:
                consult.no_answer_timer.cancel()

    def test_blind_leg_unregistered_destination(self) -> None:
        cm = CallManager()
        original, _ = _attended_pair(cm)

        pbx = _make_pbx(cm)
        pbx.extension_registry.is_registered.return_value = False

        result = TransferHandler(pbx).start_blind_refer_transfer(original, True, "1517", A_ADDR)

        assert result is False
        assert original.pending_transfer_consult_id is None


# ===========================================================================
# Deferred bridge completion on answer
# ===========================================================================


@pytest.mark.unit
class TestDeferredBridgeOnAnswer:
    """Tests for handle_callee_answer completing a deferred bridge."""

    @patch("pbx.sip.sdp.SDPSession")
    def test_pending_bridge_completes_on_answer(self, mock_sdp_cls: MagicMock) -> None:
        cm = CallManager()
        original, consult = _attended_pair(cm, consult_answered=False)
        original.pending_transfer_consult_id = "call2"
        consult.is_transfer_consult = True

        pbx = _make_pbx(cm)

        sdp_obj = MagicMock()
        sdp_obj.get_audio_info.return_value = C_RTP
        mock_sdp_cls.return_value = sdp_obj

        response = MagicMock()
        response.body = "v=0\r\n"
        response.get_header.side_effect = {"To": "<sip:1517@192.168.1.14>;tag=3972643251"}.get

        pbx.call_router.handle_callee_answer("call2", response, C_ADDR)

        pbx.transfer_handler.bridge_attended_transfer.assert_called_once_with(original, consult)
        # C's answer data captured before bridging
        assert consult.callee_rtp == C_RTP
        assert consult.callee_addr == C_ADDR
        assert consult.callee_dialog_to == "<sip:1517@192.168.1.14>;tag=3972643251"

    @patch("pbx.sip.sdp.SDPSession")
    def test_bridged_reinvite_answer_refreshes_peer_relay(self, mock_sdp_cls: MagicMock) -> None:
        cm = CallManager()
        _original, consult = _attended_pair(cm)
        consult.state = CallState.CONNECTED
        consult.bridged_peer_call_id = "call1"
        consult.bridge_peer_side = "a"

        pbx = _make_pbx(cm)

        new_rtp = {"address": "192.168.10.140", "port": 3010}
        sdp_obj = MagicMock()
        sdp_obj.get_audio_info.return_value = new_rtp
        mock_sdp_cls.return_value = sdp_obj

        response = MagicMock()
        response.body = "v=0\r\n"
        response.get_header.side_effect = {"To": "<sip:1517@192.168.1.14>;tag=3972643251"}.get

        pbx.call_router.handle_callee_answer("call2", response, C_ADDR)

        pbx.rtp_relay.replace_endpoint.assert_called_once_with(
            "call1", "a", ("192.168.10.140", 3010)
        )
        # Own (released) relay must not be touched
        pbx.rtp_relay.set_endpoints.assert_not_called()


# ===========================================================================
# BYE handling: bridged teardown, absorb guards
# ===========================================================================


@pytest.mark.unit
class TestHandleByeBridged:
    """Tests for BYE handling on bridged and pending-transfer calls."""

    def _bye(self, call_id: str = "call1") -> MagicMock:
        msg = MagicMock()
        msg.get_header.side_effect = {"Call-ID": call_id}.get
        return msg

    def _bridged_pair(self, cm: CallManager) -> tuple[Call, Call]:
        original, consult = _attended_pair(cm)
        original.caller_addr = None  # transferor side dropped
        original.bridged_peer_call_id = "call2"
        original.transferred = True
        consult.caller_addr = None
        consult.bridged_peer_call_id = "call1"
        consult.transferred = True
        return original, consult

    def test_transferee_bye_tears_down_both_legs(self) -> None:
        cm = CallManager()
        _original, consult = self._bridged_pair(cm)

        pbx = _make_pbx(cm)
        server = _make_server(pbx)
        server._send_leg_bye = MagicMock()  # type: ignore[method-assign]

        server._handle_bye(self._bye("call1"), B_ADDR)

        server._send_leg_bye.assert_called_once_with(consult)
        assert pbx.end_call.call_count == 2
        ended = {c.args[0] for c in pbx.end_call.call_args_list}
        assert ended == {"call1", "call2"}
        server._send_response.assert_called_once()

    def test_destination_bye_tears_down_both_legs(self) -> None:
        cm = CallManager()
        original, _consult = self._bridged_pair(cm)

        pbx = _make_pbx(cm)
        server = _make_server(pbx)
        server._send_leg_bye = MagicMock()  # type: ignore[method-assign]

        server._handle_bye(self._bye("call2"), C_ADDR)

        server._send_leg_bye.assert_called_once_with(original)
        ended = {c.args[0] for c in pbx.end_call.call_args_list}
        assert ended == {"call1", "call2"}

    def test_stale_transferor_bye_absorbed(self) -> None:
        cm = CallManager()
        self._bridged_pair(cm)

        pbx = _make_pbx(cm)
        server = _make_server(pbx)
        server._send_leg_bye = MagicMock()  # type: ignore[method-assign]

        # A's own post-transfer BYE on call1 -- matches neither party now
        server._handle_bye(self._bye("call1"), A_ADDR)

        server._send_leg_bye.assert_not_called()
        pbx.end_call.assert_not_called()
        server._send_response.assert_called_once()
        assert cm.get_call("call1") is not None
        assert cm.get_call("call2") is not None

    def test_transferor_bye_absorbed_while_pending(self) -> None:
        cm = CallManager()
        original, consult = _attended_pair(cm, consult_answered=False)
        original.pending_transfer_consult_id = "call2"
        original.transfer_referrer_addr = A_ADDR
        consult.is_transfer_consult = True
        consult.transfer_referrer_addr = A_ADDR

        pbx = _make_pbx(cm)
        server = _make_server(pbx)

        # A drops both its legs after the success NOTIFY
        server._handle_bye(self._bye("call1"), A_ADDR)
        server._handle_bye(self._bye("call2"), A_ADDR)

        pbx.end_call.assert_not_called()
        server._send_message.assert_not_called()  # nothing forwarded to B or C
        assert original.caller_addr is None
        assert consult.caller_addr is None
        assert cm.get_call("call1") is not None
        assert cm.get_call("call2") is not None

    def test_normal_bye_unaffected(self) -> None:
        """Call-waiting regression: a plain second call is a plain call."""
        cm = CallManager()
        _make_call(
            cm,
            "call1",
            "1513",
            "1512",
            state=CallState.HOLD,
            caller_addr=A_ADDR,
            callee_addr=B_ADDR,
        )
        second = _make_call(
            cm,
            "call2",
            "1513",
            "1517",
            state=CallState.CONNECTED,
            caller_addr=A_ADDR,
            callee_addr=C_ADDR,
        )
        second.routed_to_voicemail = False

        pbx = _make_pbx(cm)
        server = _make_server(pbx)

        # A hangs up the second call: normal teardown, no bridging of any kind
        server._handle_bye(self._bye("call2"), A_ADDR)

        pbx.end_call.assert_called_once_with("call2")
        # BYE forwarded to the second call's other party (C), nobody else
        forwarded = [c.args[1] for c in server._send_message.call_args_list]
        assert forwarded == [C_ADDR]
        assert cm.get_call("call1") is not None

    def test_transferee_bye_while_pending_aborts_consult_leg(self) -> None:
        """B hangs up while the transfer bridge is still pending: the
        ringing consultation leg is cancelled rather than orphaned."""
        cm = CallManager()
        original, consult = _attended_pair(cm, consult_answered=False)
        original.pending_transfer_consult_id = "call2"
        original.transfer_referrer_addr = A_ADDR
        original.caller_addr = None  # A already dropped its leg
        consult.is_transfer_consult = True

        pbx = _make_pbx(cm)
        server = _make_server(pbx)

        server._handle_bye(self._bye("call1"), B_ADDR)

        pbx.transfer_handler.abort_pending_transfer.assert_called_once_with(
            consult, cancel_destination=True
        )
        pbx.end_call.assert_called_once_with("call1")
        assert original.pending_transfer_consult_id is None


@pytest.mark.unit
class TestAddPbxRequestHeaders:
    """Tests for the mandatory-header helper shared by transfer teardown.

    SIPMessageBuilder.build_request() only sets From/To/Call-ID/CSeq -- Via
    and Max-Forwards are mandatory per RFC 3261 SS8.1.1.6-7, and _send_message
    is a raw UDP send with no header injection of its own. Omitting them was
    the actual root cause of a real-world bug: a bridged party's phone kept
    its call timer running after hangup because the BYE built without a Via
    header was silently dropped by its (strict) SIP stack.
    """

    def test_adds_via_max_forwards_and_contact(self) -> None:
        cm = CallManager()
        pbx = _make_pbx(cm)
        server = _make_server(pbx)
        msg = MagicMock()

        server._add_pbx_request_headers(msg, C_ADDR)

        headers = dict(c.args for c in msg.set_header.call_args_list)
        assert headers["Via"].startswith("SIP/2.0/UDP 192.168.1.14:5060;branch=z9hG4bK")
        assert headers["Max-Forwards"] == "70"
        assert headers["Contact"] == "<sip:192.168.1.14:5060>"

    def test_branch_id_is_unique_per_call(self) -> None:
        cm = CallManager()
        pbx = _make_pbx(cm)
        server = _make_server(pbx)
        msg1, msg2 = MagicMock(), MagicMock()

        server._add_pbx_request_headers(msg1, C_ADDR)
        server._add_pbx_request_headers(msg2, C_ADDR)

        via1 = dict(c.args for c in msg1.set_header.call_args_list)["Via"]
        via2 = dict(c.args for c in msg2.set_header.call_args_list)["Via"]
        assert via1 != via2


@pytest.mark.unit
class TestSendLegBye:
    """Tests for building in-dialog BYEs toward a leg's remaining party."""

    def test_bye_to_callee_leg_uses_stored_dialog(self) -> None:
        cm = CallManager()
        _original, consult = _attended_pair(cm)
        consult.caller_addr = None

        pbx = _make_pbx(cm)
        server = _make_server(pbx)

        server._send_leg_bye(consult)

        raw, dest = server._send_message.call_args[0]
        assert dest == C_ADDR
        assert raw.startswith(f"BYE sip:1517@{C_ADDR[0]}:{C_ADDR[1]} SIP/2.0")
        assert "tag=3228926622" in raw  # From tag of the PBX->C dialog
        assert "tag=3972643251" in raw  # C's own tag from its 200 OK
        assert "CSeq: 2 BYE" in raw
        assert consult.pbx_leg_cseq == 2
        # Mandatory headers a strict UA requires to accept the request
        # (RFC 3261 SS8.1.1.6-7) -- without these a real phone (e.g. the
        # Zultys ZIP) silently drops the BYE and its call timer never ends.
        assert "Via: SIP/2.0/UDP" in raw
        assert "Max-Forwards: 70" in raw
        assert "Contact:" in raw

    def test_bye_to_caller_leg_swaps_dialog_headers(self) -> None:
        cm = CallManager()
        original = _make_call(
            cm,
            "call1",
            "1512",
            "1513",
            state=CallState.CONNECTED,
            caller_addr=B_ADDR,
            callee_addr=None,
        )
        original.original_invite = MagicMock()
        original.original_invite.get_header.side_effect = {
            "From": "<sip:1512@192.168.1.14:5060>;tag=518364649",
            "To": "<sip:1513@192.168.1.14:5060>",
        }.get
        # The tagged To header the PBX actually sent the caller in its 200
        # OK (captured by handle_callee_answer) -- NOT the same as
        # original_invite's own To header, which predates any tag.
        original.caller_dialog_to = "<sip:1513@192.168.1.14:5060>;tag=pbxgenerated99"

        pbx = _make_pbx(cm)
        server = _make_server(pbx)

        server._send_leg_bye(original)

        raw, dest = server._send_message.call_args[0]
        assert dest == B_ADDR
        assert raw.startswith(f"BYE sip:1512@{B_ADDR[0]}:{B_ADDR[1]} SIP/2.0")
        # Direction swapped: request goes To the caller's identity
        assert "To: <sip:1512@192.168.1.14:5060>;tag=518364649" in raw
        # From must carry the caller's real dialog tag (caller_dialog_to),
        # not original_invite's untagged To header -- a mismatched/missing
        # From-tag means the caller's phone can't match this BYE to its
        # active dialog and never ends the call.
        assert "From: <sip:1513@192.168.1.14:5060>;tag=pbxgenerated99" in raw
        assert "Via: SIP/2.0/UDP" in raw
        assert "Max-Forwards: 70" in raw

    def test_bye_to_caller_leg_falls_back_when_caller_dialog_to_unset(self) -> None:
        """Defensive fallback for a call whose caller_dialog_to was never
        captured (shouldn't happen in practice, but must not crash)."""
        cm = CallManager()
        original = _make_call(
            cm,
            "call1",
            "1512",
            "1513",
            state=CallState.CONNECTED,
            caller_addr=B_ADDR,
            callee_addr=None,
        )
        original.original_invite = MagicMock()
        original.original_invite.get_header.side_effect = {
            "From": "<sip:1512@192.168.1.14:5060>;tag=518364649",
            "To": "<sip:1513@192.168.1.14:5060>",
        }.get
        assert original.caller_dialog_to is None

        pbx = _make_pbx(cm)
        server = _make_server(pbx)

        server._send_leg_bye(original)

        raw, _dest = server._send_message.call_args[0]
        assert "From: <sip:1513@192.168.1.14:5060>" in raw


# ===========================================================================
# Aborting pending transfers
# ===========================================================================


@pytest.mark.unit
class TestAbortPendingTransfer:
    """Tests for tearing down both legs when the destination never joins."""

    def test_abort_ends_consult_and_original(self) -> None:
        cm = CallManager()
        original, consult = _attended_pair(cm, consult_answered=False)
        original.pending_transfer_consult_id = "call2"
        original.transfer_referrer_addr = A_ADDR
        consult.is_transfer_consult = True

        pbx = _make_pbx(cm)
        pbx.sip_server._send_leg_bye = MagicMock()

        TransferHandler(pbx).abort_pending_transfer(consult)

        ended = {c.args[0] for c in pbx.end_call.call_args_list}
        assert ended == {"call1", "call2"}
        pbx.sip_server._send_leg_bye.assert_called_once_with(original)
        # Transferor's side nulled so the leg BYE targets the transferee
        assert original.caller_addr is None
        assert original.pending_transfer_consult_id is None

    def test_abort_cancels_still_ringing_destination(self) -> None:
        cm = CallManager()
        original, consult = _attended_pair(cm, consult_answered=False)
        original.pending_transfer_consult_id = "call2"
        consult.callee_invite = MagicMock()

        pbx = _make_pbx(cm)

        TransferHandler(pbx).abort_pending_transfer(consult, cancel_destination=True)

        pbx.call_router._send_cancel_to_callee.assert_called_once_with(consult, "call2")

    def test_error_response_aborts_pending_transfer(self) -> None:
        cm = CallManager()
        _original, consult = _attended_pair(cm, consult_answered=False)
        consult.is_transfer_consult = True
        consult.invite_transaction = None

        pbx = _make_pbx(cm)
        server = _make_server(pbx)
        server._send_ack_to_callee = MagicMock()  # type: ignore[method-assign]

        response = MagicMock()
        response.status_code = 486
        response.status_text = "Busy Here"
        response.get_header.side_effect = {
            "Call-ID": "call2",
            "CSeq": "1 INVITE",
        }.get

        server._handle_response(response, C_ADDR)

        server._send_ack_to_callee.assert_called_once()
        pbx.transfer_handler.abort_pending_transfer.assert_called_once_with(consult)
        pbx.end_call.assert_not_called()


# ===========================================================================
# RTP relay endpoint replacement
# ===========================================================================


@pytest.mark.unit
class TestReplaceEndpoint:
    """Tests for RTPRelayHandler.replace_endpoint()."""

    def test_replace_clears_learned_state_and_reopens_learning(self) -> None:
        from pbx.rtp.handler import RTPRelayHandler

        handler = RTPRelayHandler(local_port=20000, call_id="call1")
        handler.set_endpoints(("1.1.1.1", 1111), ("2.2.2.2", 2222))
        handler.learned_a = ("1.1.1.1", 1111)
        handler.learned_b = ("2.2.2.2", 2222)
        handler._start_time = 0.0  # learning window long expired

        handler.replace_endpoint("a", ("3.3.3.3", 3333))

        assert handler.endpoint_a == ("3.3.3.3", 3333)
        assert handler.learned_a is None
        assert handler.learned_b == ("2.2.2.2", 2222)  # other side untouched
        assert handler.endpoint_b == ("2.2.2.2", 2222)
        assert handler._start_time is not None and handler._start_time > 0.0

    def test_replace_side_b(self) -> None:
        from pbx.rtp.handler import RTPRelayHandler

        handler = RTPRelayHandler(local_port=20000, call_id="call1")
        handler.set_endpoints(("1.1.1.1", 1111), ("2.2.2.2", 2222))
        handler.learned_b = ("2.2.2.2", 2222)

        handler.replace_endpoint("b", ("3.3.3.3", 3333))

        assert handler.endpoint_b == ("3.3.3.3", 3333)
        assert handler.learned_b is None
        assert handler.endpoint_a == ("1.1.1.1", 1111)

    def test_manager_wrapper_routes_to_handler(self) -> None:
        from pbx.rtp.handler import RTPRelay

        manager = RTPRelay.__new__(RTPRelay)
        manager.logger = MagicMock()
        handler = MagicMock()
        manager.active_relays = {"call1": {"handler": handler}}

        manager.replace_endpoint("call1", "a", ("3.3.3.3", 3333))

        handler.replace_endpoint.assert_called_once_with("a", ("3.3.3.3", 3333))

    def test_manager_wrapper_ignores_unknown_call(self) -> None:
        from pbx.rtp.handler import RTPRelay

        manager = RTPRelay.__new__(RTPRelay)
        manager.logger = MagicMock()
        manager.active_relays = {}

        manager.replace_endpoint("nope", "a", ("3.3.3.3", 3333))  # no raise
