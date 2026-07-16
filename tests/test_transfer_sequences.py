"""Integration-level sequence tests for REFER-based call transfer.

tests/test_refer_transfer.py exercises each transfer method in isolation,
with the other TransferHandler/CallRouter methods left as bare MagicMocks.
That style cannot catch bugs that only appear from the *interaction* between
two real methods across a multi-step sequence (e.g. hold, transfer, transfer
again, hang up in a different order than the happy path).

This file wires real TransferHandler and CallRouter instances onto a
MagicMock `pbx` (the attributes SIPServer/CallRouter actually call, now that
there is no PBXCore facade for this logic) plus a real CallManager and a real
RTPRelay (so a silently-missing relay entry -- a released relay being
operated on -- shows up as an actual no-op, not just a mock call that
"succeeded"). Each test drives a realistic (or deliberately adversarial)
sequence of SIP events through the real SIPServer handlers and asserts on the
resulting call/relay state.
"""

from typing import Any
from unittest.mock import MagicMock

import pytest

from pbx.core.call import CallManager, CallState
from pbx.core.call_router import CallRouter
from pbx.core.transfer_handler import TransferHandler
from pbx.rtp.handler import RTPRelay
from pbx.sip.server import SIPServer

A_ADDR = ("192.168.10.139", 5060)  # First transferor
B_ADDR = ("192.168.10.155", 5060)  # Stays on the call throughout
C_ADDR = ("192.168.10.140", 5061)  # First transfer destination / second transferor
D_ADDR = ("192.168.10.141", 5062)  # Second transfer destination

B_RTP = {"address": "192.168.10.155", "port": 40000}
C_RTP = {"address": "192.168.10.140", "port": 40010}
D_RTP = {"address": "192.168.10.141", "port": 40020}


def _wire_pbx(cm: CallManager, relay: RTPRelay) -> MagicMock:
    """Build a MagicMock PBXCore whose transfer/routing handlers are real
    TransferHandler/CallRouter instances, with a real CallManager/RTPRelay so
    relay-level bugs (a silent no-op on a released relay) are observable."""
    pbx = MagicMock()
    pbx.call_manager = cm
    pbx.rtp_relay = relay
    pbx.moh_system = MagicMock()
    pbx.cdr_system = MagicMock()
    pbx.webhook_system = MagicMock()
    pbx.logger = MagicMock()
    pbx._get_server_ip.return_value = "192.168.1.14"
    pbx.config.get.side_effect = lambda key, default=None: {
        "server.sip_port": 5060,
        "voicemail.no_answer_timeout": 30,
    }.get(key, default)
    pbx._get_compatible_codecs.return_value = ["0", "8"]
    pbx._get_dtmf_payload_type.return_value = 101
    pbx._get_ilbc_mode.return_value = 30
    pbx.extension_registry.is_registered.return_value = True

    # There is no PBXCore facade for transfer/routing logic anymore -- callers
    # (SIPServer, CallRouter) reach pbx.transfer_handler / pbx.call_router
    # directly. This file wants full real behavior across a whole sequence,
    # so both are wired as complete real instances bound to this mock pbx
    # (unlike test_refer_transfer.py, which wires only individual methods to
    # keep other assertions mock-based).
    pbx.transfer_handler = TransferHandler(pbx)
    pbx.call_router = CallRouter(pbx)

    def real_end_call(call_id: str) -> None:
        call = cm.get_call(call_id)
        if call:
            relay.release_relay(call_id)
            cm.end_call(call_id)

    pbx.end_call.side_effect = real_end_call
    return pbx


def _wire_server(pbx: MagicMock) -> SIPServer:
    server = SIPServer.__new__(SIPServer)
    server.pbx_core = pbx
    server.logger = MagicMock()
    server._send_message = MagicMock()  # type: ignore[method-assign]
    server._send_response = MagicMock()  # type: ignore[method-assign]
    pbx.sip_server = server
    return server


def _seed_relay(relay: RTPRelay, call_id: str) -> tuple[int, int]:
    ports = relay.allocate_relay(call_id)
    assert ports is not None, "test setup: relay allocation must succeed"
    return ports


def _basic_call(
    cm: CallManager,
    call_id: str,
    from_ext: str,
    to_ext: str,
    *,
    state: CallState,
    caller_addr: tuple[str, int] | None,
    callee_addr: tuple[str, int] | None,
    caller_rtp: dict[str, Any] | None = None,
    callee_rtp: dict[str, Any] | None = None,
    rtp_ports: tuple[int, int] | None = None,
) -> Any:
    call = cm.create_call(call_id, from_ext, to_ext)
    call.state = state
    call.caller_addr = caller_addr
    call.callee_addr = callee_addr
    call.caller_rtp = caller_rtp
    call.callee_rtp = callee_rtp
    call.rtp_ports = rtp_ports
    call.original_invite = MagicMock()
    call.original_invite.get_header.side_effect = {
        "From": f'"F" <sip:{from_ext}@192.168.1.14:5060>;tag=fromtag-{call_id}',
        "To": f"<sip:{to_ext}@192.168.1.14:5060>",
    }.get
    call.callee_invite = call.original_invite
    return call


def _refer_message(call_id: str, refer_to: str, from_ext: str, from_addr: tuple) -> MagicMock:
    msg = MagicMock()
    msg.get_header.side_effect = {
        "Refer-To": refer_to,
        "Call-ID": call_id,
        "Referred-By": f"<sip:{from_ext}@{from_addr[0]}>",
        "From": f"<sip:{from_ext}@{from_addr[0]}>;tag=refer-{call_id}",
        "To": "<sip:x@192.168.1.14>;tag=x",
    }.get
    return msg


def _bye_message(call_id: str) -> MagicMock:
    msg = MagicMock()
    msg.get_header.side_effect = {"Call-ID": call_id}.get
    return msg


def _replaces_refer_to(dest_ext: str, replaces_call_id: str) -> str:
    from urllib.parse import quote

    replaces = quote(f"{replaces_call_id};to-tag=x;from-tag=y", safe="")
    return f"<sip:{dest_ext}@192.168.1.14:5060?Replaces={replaces}>"


def _hold_reinvite_message(call_id: str, rtp: dict[str, Any]) -> MagicMock:
    """A re-INVITE placing the call on hold (a=sendonly)."""
    msg = MagicMock()
    msg.body = (
        f"v=0\r\no=- 0 1 IN IP4 {rtp['address']}\r\ns=-\r\n"
        f"c=IN IP4 {rtp['address']}\r\nt=0 0\r\n"
        f"m=audio {rtp['port']} RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\na=sendonly\r\n"
    )
    msg.get_header.side_effect = {
        "Call-ID": call_id,
        "From": f"<sip:x@192.168.1.14>;tag=t-{call_id}",
        "To": "<sip:y@192.168.1.14>;tag=u",
        "CSeq": "2 INVITE",
    }.get
    return msg


@pytest.fixture
def relay():  # type: ignore[no-untyped-def]
    r = RTPRelay(port_range_start=30000, port_range_end=30100)
    yield r
    # Force-close any sockets left bound by a test that failed mid-sequence
    # (an assertion error skips the rest of the test body, including any
    # release_relay calls it would otherwise have made).
    for call_id in list(r.active_relays):
        r.release_relay(call_id)


@pytest.fixture
def cm() -> CallManager:
    return CallManager()


# ===========================================================================
# 1. Full end-to-end attended transfer, then hangup from either side
# ===========================================================================


@pytest.mark.unit
class TestFullAttendedTransferSequence:
    def _setup(self, cm: CallManager, relay: RTPRelay) -> tuple[MagicMock, SIPServer]:
        pbx = _wire_pbx(cm, relay)
        server = _wire_server(pbx)

        original_ports = _seed_relay(relay, "call1")
        relay.set_endpoints("call1", (A_ADDR[0], 100), (B_RTP["address"], B_RTP["port"]))
        _basic_call(
            cm,
            "call1",
            "1513",
            "1512",
            state=CallState.HOLD,
            caller_addr=A_ADDR,
            callee_addr=B_ADDR,
            caller_rtp={"address": A_ADDR[0], "port": 100},
            callee_rtp=B_RTP,
            rtp_ports=original_ports,
        )
        consult_ports = _seed_relay(relay, "call2")
        relay.set_endpoints("call2", (A_ADDR[0], 100), (C_RTP["address"], C_RTP["port"]))
        _basic_call(
            cm,
            "call2",
            "1513",
            "1517",
            state=CallState.CONNECTED,
            caller_addr=A_ADDR,
            callee_addr=C_ADDR,
            caller_rtp={"address": A_ADDR[0], "port": 100},
            callee_rtp=C_RTP,
            rtp_ports=consult_ports,
        )
        return pbx, server

    def test_bridge_completes_and_relay_carries_correct_endpoints(
        self, cm: CallManager, relay: RTPRelay
    ) -> None:
        _pbx, server = self._setup(cm, relay)

        server._handle_refer(
            _refer_message("call1", _replaces_refer_to("1517", "call2"), "1513", A_ADDR),
            A_ADDR,
        )

        original = cm.get_call("call1")
        consult = cm.get_call("call2")
        assert original.state == CallState.CONNECTED
        assert original.bridged_peer_call_id == "call2"
        assert consult.bridged_peer_call_id == "call1"
        # Consult's relay was actually released (real RTPRelay, not a mock)
        assert relay.get_handler("call2") is None
        # Original relay retargeted to C's real endpoint; B's side untouched
        handler = relay.get_handler("call1")
        assert handler is not None
        assert handler.endpoint_a == (C_RTP["address"], C_RTP["port"])
        assert handler.endpoint_b == (B_RTP["address"], B_RTP["port"])

    def test_transferor_gets_bye_for_both_its_own_legs(
        self, cm: CallManager, relay: RTPRelay
    ) -> None:
        """Relying on the transferor's phone to end its own legs on hangup
        does not hold universally -- some phones leave the original (held)
        leg's dialog state untouched, appearing permanently connected/
        on-hold with no way to hang up or resume even though the PBX has
        already dropped them from the call entirely."""
        _pbx, server = self._setup(cm, relay)

        server._handle_refer(
            _refer_message("call1", _replaces_refer_to("1517", "call2"), "1513", A_ADDR),
            A_ADDR,
        )

        byes_to_a = [
            c.args
            for c in server._send_message.call_args_list
            if c.args[1] == A_ADDR and c.args[0].startswith("BYE ")
        ]
        assert len(byes_to_a) == 2
        call_ids_byed = set()
        for raw, _dest in byes_to_a:
            assert raw.startswith("BYE ")
            call_ids_byed.add(next(ln for ln in raw.split("\r\n") if ln.startswith("Call-ID:")))
        # One BYE for the consultation leg (call2), one for the original
        # leg (call1) -- A's own dialog identity on both.
        assert any("call2" in cid for cid in call_ids_byed)
        assert any("call1" in cid for cid in call_ids_byed)

    def test_hold_on_peer_leg_starts_moh_on_the_shared_relay(
        self, cm: CallManager, relay: RTPRelay
    ) -> None:
        """After a bridge, the destination's dialog is a peer leg that owns
        no relay of its own. When that party holds (to transfer onward, as
        happens on the third-and-later transfer of a chain), MOH must start
        on the shared relay owner -- keying it off the peer leg's own
        (released) Call-ID silently plays no music."""
        pbx, server = self._setup(cm, relay)
        server._handle_refer(
            _refer_message("call1", _replaces_refer_to("1517", "call2"), "1513", A_ADDR),
            A_ADDR,
        )
        # call2 is now C's peer-leg dialog with no relay; C occupies side "a"
        # of call1's relay (transferor A was call1's caller).
        assert relay.get_handler("call2") is None
        assert cm.get_call("call2").bridge_peer_side == "a"

        # C holds (re-INVITE with a=sendonly on call2).
        server._handle_reinvite(
            _hold_reinvite_message("call2", C_RTP), C_ADDR, cm.get_call("call2")
        )

        # MOH started on the shared relay (call1), not the relay-less peer
        # leg (call2), and plays to side "b" -- the transferee B.
        pbx.moh_system.start_moh.assert_called_once()
        moh_call_id, moh_handler, moh_side = pbx.moh_system.start_moh.call_args[0]
        assert moh_call_id == "call1"
        assert moh_handler is relay.get_handler("call1")
        assert moh_side == "b"

        # The 200 OK advertises call1's live relay port, so C resumes to the
        # real relay rather than call2's released one.
        answer = next(
            c.args[0]
            for c in server._send_message.call_args_list
            if c.args[1] == C_ADDR and c.args[0].startswith("SIP/2.0 200")
        )
        assert f"m=audio {relay.get_handler('call1').local_port} " in answer

    def test_transferee_hangup_after_bridge_sends_real_bye_to_destination(
        self, cm: CallManager, relay: RTPRelay
    ) -> None:
        _pbx, server = self._setup(cm, relay)
        server._handle_refer(
            _refer_message("call1", _replaces_refer_to("1517", "call2"), "1513", A_ADDR),
            A_ADDR,
        )

        # B hangs up
        server._handle_bye(_bye_message("call1"), B_ADDR)

        assert server._send_message.called
        raw, dest = server._send_message.call_args[0]
        assert dest == C_ADDR
        assert raw.startswith("BYE ")
        assert cm.get_call("call1") is None
        assert cm.get_call("call2") is None
        assert relay.get_handler("call1") is None

    def test_destination_hangup_after_bridge_sends_real_bye_to_transferee(
        self, cm: CallManager, relay: RTPRelay
    ) -> None:
        _pbx, server = self._setup(cm, relay)
        server._handle_refer(
            _refer_message("call1", _replaces_refer_to("1517", "call2"), "1513", A_ADDR),
            A_ADDR,
        )

        server._handle_bye(_bye_message("call2"), C_ADDR)

        raw, dest = server._send_message.call_args[0]
        assert dest == B_ADDR
        assert raw.startswith("BYE ")
        assert cm.get_call("call1") is None
        assert cm.get_call("call2") is None


# ===========================================================================
# 2. Transferring a call that is ITSELF the result of a previous transfer
# ===========================================================================


@pytest.mark.unit
class TestSecondTransferAfterFirstBridge:
    """After A transfers B<->C (round 1), can C now transfer B to D (round
    2), sent as a REFER on the *peer* leg (call2, C's real dialog)?

    C's SIP dialog was always call2 (never renegotiated) -- the bridge only
    moved C's *media* onto call1's relay and re-pointed call1's record
    fields; call2 (the peer leg) is still alive and still holds C's real
    address, per the two-live-records design. If C wants to transfer again,
    its phone can only send a REFER on the dialog it actually has: call2.
    """

    def _bridged_pair(
        self, cm: CallManager, relay: RTPRelay
    ) -> tuple[Any, Any, MagicMock, SIPServer]:
        pbx = _wire_pbx(cm, relay)
        server = _wire_server(pbx)

        original_ports = _seed_relay(relay, "call1")
        _basic_call(
            cm,
            "call1",
            "1513",
            "1512",
            state=CallState.HOLD,
            caller_addr=A_ADDR,
            callee_addr=B_ADDR,
            caller_rtp={"address": A_ADDR[0], "port": 100},
            callee_rtp=B_RTP,
            rtp_ports=original_ports,
        )
        consult_ports = _seed_relay(relay, "call2")
        _basic_call(
            cm,
            "call2",
            "1513",
            "1517",
            state=CallState.CONNECTED,
            caller_addr=A_ADDR,
            callee_addr=C_ADDR,
            caller_rtp={"address": A_ADDR[0], "port": 100},
            callee_rtp=C_RTP,
            rtp_ports=consult_ports,
        )
        server._handle_refer(
            _refer_message("call1", _replaces_refer_to("1517", "call2"), "1513", A_ADDR),
            A_ADDR,
        )
        server._send_message.reset_mock()
        return cm.get_call("call1"), cm.get_call("call2"), pbx, server

    def test_round_two_referred_on_peer_leg_retargets_the_surviving_relay(
        self, cm: CallManager, relay: RTPRelay
    ) -> None:
        _original, _consult, _pbx, server = self._bridged_pair(cm, relay)

        # C (now on call2's dialog) holds, calls D, D answers -> new call3
        d_ports = _seed_relay(relay, "call3")
        _basic_call(
            cm,
            "call3",
            "1517",
            "1519",
            state=CallState.CONNECTED,
            caller_addr=C_ADDR,
            callee_addr=D_ADDR,
            caller_rtp=C_RTP,
            callee_rtp=D_RTP,
            rtp_ports=d_ports,
        )

        # C sends REFER on ITS OWN dialog (call2), replacing call3
        server._handle_refer(
            _refer_message("call2", _replaces_refer_to("1519", "call3"), "1517", C_ADDR),
            C_ADDR,
        )

        # bridge_attended_transfer redirects: call2 has no relay of its own
        # (released in round 1), but is cross-linked to call1 (which does)
        # via bridged_peer_call_id/bridge_peer_side, so the retarget lands
        # on call1's actual live relay -- B ends up talking to D.
        call1_handler = relay.get_handler("call1")
        assert call1_handler is not None
        assert call1_handler.endpoint_a == (D_RTP["address"], D_RTP["port"])

        original = cm.get_call("call1")
        assert original.bridged_peer_call_id == "call3"
        assert original.from_extension == "1519"
        assert original.to_extension == "1512"  # B untouched throughout
        assert cm.get_call("call2") is None, "superseded peer leg was cleaned up"
        assert cm.get_call("call3").bridged_peer_call_id == "call1"


# ===========================================================================
# 3. REFER arriving while a transfer is already pending on the same call
# ===========================================================================


@pytest.mark.unit
class TestOverlappingTransferAttempts:
    def test_second_refer_while_first_still_pending_is_rejected(
        self, cm: CallManager, relay: RTPRelay
    ) -> None:
        pbx = _wire_pbx(cm, relay)
        server = _wire_server(pbx)

        original_ports = _seed_relay(relay, "call1")
        _basic_call(
            cm,
            "call1",
            "1513",
            "1512",
            state=CallState.HOLD,
            caller_addr=A_ADDR,
            callee_addr=B_ADDR,
            callee_rtp=B_RTP,
            rtp_ports=original_ports,
        )
        # First consultation call to C, not yet answered
        _basic_call(
            cm,
            "call2",
            "1513",
            "1517",
            state=CallState.RINGING,
            caller_addr=A_ADDR,
            callee_addr=None,
            callee_rtp=None,
        )
        server._handle_refer(
            _refer_message("call1", _replaces_refer_to("1517", "call2"), "1513", A_ADDR),
            A_ADDR,
        )
        original = cm.get_call("call1")
        assert original.pending_transfer_consult_id == "call2"

        # A second, unrelated consultation call to D, also unanswered
        _basic_call(
            cm,
            "call3",
            "1513",
            "1519",
            state=CallState.RINGING,
            caller_addr=A_ADDR,
            callee_addr=None,
            callee_rtp=None,
        )
        server._send_message.reset_mock()
        server._handle_refer(
            _refer_message("call1", _replaces_refer_to("1519", "call3"), "1513", A_ADDR),
            A_ADDR,
        )

        # Rejected: the first pending transfer is untouched, so the first
        # consultation call (still ringing C) remains resolvable when it
        # answers instead of being silently orphaned.
        raw, dest = server._send_message.call_args[0]
        assert dest == A_ADDR
        assert "480" in raw
        assert original.pending_transfer_consult_id == "call2"
        assert cm.get_call("call3") is not None  # second consult untouched, not cleaned up either

        # Confirm the first consult call still completes normally when C
        # answers -- the deferred-bridge hook finds it via
        # pending_transfer_consult_id, unchanged by the rejected second REFER.
        response = MagicMock()
        response.body = (
            "v=0\r\no=- 0 0 IN IP4 192.168.10.140\r\ns=-\r\n"
            f"c=IN IP4 {C_RTP['address']}\r\nt=0 0\r\n"
            f"m=audio {C_RTP['port']} RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\n"
        )
        response.get_header.side_effect = {"To": "<sip:1517@192.168.1.14>;tag=z"}.get

        pbx.call_router.handle_callee_answer("call2", response, C_ADDR)

        assert original.bridged_peer_call_id == "call2"


# ===========================================================================
# 4. Self-referencing Replaces (malformed but observed-possible input)
# ===========================================================================


@pytest.mark.unit
class TestSelfReferencingReplaces:
    def test_replaces_names_its_own_dialog(self, cm: CallManager, relay: RTPRelay) -> None:
        pbx = _wire_pbx(cm, relay)
        server = _wire_server(pbx)

        ports = _seed_relay(relay, "call1")
        _basic_call(
            cm,
            "call1",
            "1513",
            "1512",
            state=CallState.CONNECTED,
            caller_addr=A_ADDR,
            callee_addr=B_ADDR,
            callee_rtp=B_RTP,
            rtp_ports=ports,
        )

        # Malformed/adversarial: Replaces points at the REFER's own Call-ID
        server._handle_refer(
            _refer_message("call1", _replaces_refer_to("1512", "call1"), "1513", A_ADDR),
            A_ADDR,
        )

        # Rejected outright -- must not crash, and must not corrupt the call
        # into a self-referential bridge (bridged_peer_call_id == its own
        # call_id) or touch any of its real state.
        raw, dest = server._send_message.call_args[0]
        assert dest == A_ADDR
        assert "400 Bad Request" in raw

        original = cm.get_call("call1")
        assert original is not None
        assert original.bridged_peer_call_id is None
        assert original.callee_addr == B_ADDR
        assert original.callee_rtp == B_RTP


# ===========================================================================
# 5. Blind transfer full sequence including destination decline
# ===========================================================================


@pytest.mark.unit
class TestBlindTransferSequences:
    def test_blind_transfer_decline_leaves_no_orphans(
        self, cm: CallManager, relay: RTPRelay
    ) -> None:
        pbx = _wire_pbx(cm, relay)
        server = _wire_server(pbx)
        pbx.extension_registry.get_address.return_value = C_ADDR

        ports = _seed_relay(relay, "call1")
        _basic_call(
            cm,
            "call1",
            "1513",
            "1512",
            state=CallState.HOLD,
            caller_addr=A_ADDR,
            callee_addr=B_ADDR,
            callee_rtp=B_RTP,
            rtp_ports=ports,
        )

        server._handle_refer(
            _refer_message("call1", "<sip:1517@192.168.1.14:5060>", "1513", A_ADDR),
            A_ADDR,
        )
        consult_id = cm.get_call("call1").pending_transfer_consult_id
        assert consult_id is not None
        consult = cm.get_call(consult_id)
        try:
            # A drops both its legs (typical post-NOTIFY phone behavior)
            server._handle_bye(_bye_message("call1"), A_ADDR)

            # C declines
            response = MagicMock()
            response.status_code = 486
            response.status_text = "Busy Here"
            response.get_header.side_effect = {
                "Call-ID": consult_id,
                "CSeq": "1 INVITE",
            }.get
            server._send_ack_to_callee = MagicMock()  # type: ignore[method-assign]
            server._handle_response(response, C_ADDR)

            assert cm.get_call("call1") is None, "original call left dangling after decline"
            assert cm.get_call(consult_id) is None, "consult call left dangling after decline"
        finally:
            if consult.no_answer_timer:
                consult.no_answer_timer.cancel()

    def test_blind_transfer_answered_then_hangup_uses_peer_relay_not_reinvite(
        self, cm: CallManager, relay: RTPRelay
    ) -> None:
        pbx = _wire_pbx(cm, relay)
        server = _wire_server(pbx)
        pbx.extension_registry.get_address.return_value = C_ADDR

        ports = _seed_relay(relay, "call1")
        _basic_call(
            cm,
            "call1",
            "1513",
            "1512",
            state=CallState.HOLD,
            caller_addr=A_ADDR,
            callee_addr=B_ADDR,
            callee_rtp=B_RTP,
            rtp_ports=ports,
        )
        server._handle_refer(
            _refer_message("call1", "<sip:1517@192.168.1.14:5060>", "1513", A_ADDR),
            A_ADDR,
        )
        server._handle_bye(_bye_message("call1"), A_ADDR)
        consult_id = cm.get_call("call1").pending_transfer_consult_id

        response = MagicMock()
        response.body = (
            "v=0\r\no=- 0 0 IN IP4 192.168.10.140\r\ns=-\r\n"
            f"c=IN IP4 {C_RTP['address']}\r\nt=0 0\r\n"
            f"m=audio {C_RTP['port']} RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\n"
        )
        response.get_header.side_effect = {"To": "<sip:1517@192.168.1.14>;tag=answered"}.get

        pbx.call_router.handle_callee_answer(consult_id, response, C_ADDR)

        original = cm.get_call("call1")
        assert original.bridged_peer_call_id == consult_id
        # Blind leg used the original's relay directly -- its own relay
        # entry (if any was even allocated) is not the one carrying media.
        handler = relay.get_handler("call1")
        assert handler is not None
        assert handler.endpoint_a == (C_RTP["address"], C_RTP["port"])

        server._send_message.reset_mock()
        server._handle_bye(_bye_message("call1"), B_ADDR)
        _raw, dest = server._send_message.call_args[0]
        assert dest == C_ADDR
        assert cm.get_call("call1") is None
        assert cm.get_call(consult_id) is None

    def test_blind_transfer_by_callee_bridges_caller_with_correct_dialog_tag(
        self, cm: CallManager, relay: RTPRelay
    ) -> None:
        """The transferring party can be either side of the original call.
        Here the *callee* (1517) blind-transfers to D -- the party left
        connected is the *caller* (1513), exercising _send_leg_bye's
        caller-remains branch, which depends on caller_dialog_to (the
        tagged To header the PBX actually sent the caller in its original
        200 OK) rather than original_invite's own untagged To header."""
        pbx = _wire_pbx(cm, relay)
        server = _wire_server(pbx)
        pbx.extension_registry.get_address.return_value = D_ADDR

        ports = _seed_relay(relay, "call1")
        relay.set_endpoints("call1", (A_ADDR[0], 100), (B_RTP["address"], B_RTP["port"]))
        call1 = _basic_call(
            cm,
            "call1",
            "1513",
            "1517",
            state=CallState.CONNECTED,
            caller_addr=A_ADDR,
            callee_addr=B_ADDR,
            caller_rtp={"address": A_ADDR[0], "port": 100},
            callee_rtp=B_RTP,
            rtp_ports=ports,
        )
        # What handle_callee_answer would have captured when 1517 (callee)
        # originally answered call1 -- a tag the PBX generated, unrelated to
        # anything in original_invite.
        call1.caller_dialog_to = "<sip:1513@192.168.1.14:5060>;tag=pbxminted42"

        # 1517 (the callee) blind-transfers to D -- referrer address is B_ADDR
        server._handle_refer(
            _refer_message("call1", "<sip:1519@192.168.1.14:5060>", "1517", B_ADDR),
            B_ADDR,
        )
        consult_id = cm.get_call("call1").pending_transfer_consult_id
        assert consult_id is not None
        server._handle_bye(_bye_message("call1"), B_ADDR)

        response = MagicMock()
        response.body = (
            "v=0\r\no=- 0 0 IN IP4 192.168.10.141\r\ns=-\r\n"
            f"c=IN IP4 {D_RTP['address']}\r\nt=0 0\r\n"
            f"m=audio {D_RTP['port']} RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\n"
        )
        response.get_header.side_effect = {"To": "<sip:1519@192.168.1.14>;tag=answered"}.get
        pbx.call_router.handle_callee_answer(consult_id, response, D_ADDR)

        original = cm.get_call("call1")
        assert original.bridged_peer_call_id == consult_id
        assert original.caller_addr == A_ADDR  # 1513 untouched throughout

        # D hangs up -- the BYE reaching 1513 must carry its real dialog tag.
        server._send_message.reset_mock()
        server._handle_bye(_bye_message(consult_id), D_ADDR)
        raw, dest = server._send_message.call_args[0]
        assert dest == A_ADDR
        assert "From: <sip:1513@192.168.1.14:5060>;tag=pbxminted42" in raw
        assert cm.get_call("call1") is None
        assert cm.get_call(consult_id) is None
