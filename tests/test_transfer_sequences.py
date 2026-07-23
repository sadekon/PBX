"""Integration-level sequence tests for REFER-based call transfer.

tests/test_refer_transfer.py exercises each transfer mechanism in isolation.
That style cannot catch bugs that only appear from the *interaction* between
real components across a multi-step sequence (hold, transfer, transfer again,
hang up in a different order than the happy path).

This file wires real TransferHandler, TransferSession, CallRouter and SIPServer
instances onto a MagicMock `pbx`, plus a real CallManager and a real RTPRelay --
so a silently-missing relay entry (a released relay being operated on) shows up
as an actual no-op rather than a mock call that "succeeded". Each test drives a
realistic, or deliberately adversarial, sequence of SIP events through the real
handlers and asserts on the resulting call and relay state.

:class:`TestHangupMatrix` is the centrepiece: every party hanging up at every
stage of a transfer, each case asserting the invariants the state machine exists
to guarantee -- every leg terminal, exactly one final NOTIFY, and no leaked Call
record or RTP relay.
"""

from __future__ import annotations

import threading
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _no_retransmit_timers():
    """Keep INVITE retransmission timer chains from leaking past each test.

    Target-leg origination starts a real InviteClientTransaction whose
    timer-A chain keeps re-scheduling after the test ends; if it fires while
    another test has threading.Thread/Timer patched globally, the leaked
    thread raises and pytest fails that unrelated test.
    """
    with patch("pbx.sip.transaction.InviteClientTransaction._schedule_timer_a"):
        yield

from pbx.core.call import CallManager, CallState
from pbx.core.call_router import CallRouter
from pbx.core.transfer_handler import TransferHandler
from pbx.core.transfer_session import (
    LegEvent,
    LegEventResult,
    LegRole,
    LegStatus,
    TransferState,
)
from pbx.rtp.handler import RTPRelay
from pbx.sip.server import SIPServer

A_ADDR = ("192.168.10.139", 5060)  # First transferor
B_ADDR = ("192.168.10.155", 5060)  # Stays on the call throughout
C_ADDR = ("192.168.10.140", 5061)  # First transfer destination / second transferor
D_ADDR = ("192.168.10.141", 5062)  # Second transfer destination

B_RTP = {"address": "192.168.10.155", "port": 40000}
C_RTP = {"address": "192.168.10.140", "port": 40010}
D_RTP = {"address": "192.168.10.141", "port": 40020}


def _wire_pbx(
    cm: CallManager,
    relay: RTPRelay,
    *,
    drop_on_failure: bool = True,
    callback_retries: int = 2,
) -> MagicMock:
    """Build a MagicMock PBXCore whose transfer/routing handlers are real
    instances, with a real CallManager/RTPRelay so relay-level bugs (a silent
    no-op on a released relay) are observable."""
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
        # Asterisk features.conf equivalents. Failure defaults to dropping so
        # teardown is deterministic; TestRecallPolicy flips it.
        "transfer.atxfernoanswertimeout": 15,
        "transfer.atxferdropcall": drop_on_failure,
        "transfer.atxfercallbackretries": callback_retries,
        "transfer.atxferloopdelay": 10,
    }.get(key, default)
    pbx._get_compatible_codecs.return_value = ["0", "8"]
    pbx._get_dtmf_payload_type.return_value = 101
    pbx._get_ilbc_mode.return_value = 30
    pbx.extension_registry.is_registered.return_value = True

    # No call queues configured: transfers never divert to queue adoption
    pbx.queue_handler.is_queue_destination.return_value = False

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
    server._pending_trunk_registrations = {}
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
        "Via": "SIP/2.0/UDP 192.168.1.14:5060;branch=z9hG4bKseed",
        "CSeq": "1 INVITE",
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


def _answer_response(rtp: dict[str, Any], tag: str = "answered") -> MagicMock:
    """A 200 OK carrying the answering party's SDP."""
    response = MagicMock()
    response.body = (
        f"v=0\r\no=- 0 0 IN IP4 {rtp['address']}\r\ns=-\r\n"
        f"c=IN IP4 {rtp['address']}\r\nt=0 0\r\n"
        f"m=audio {rtp['port']} RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\n"
    )
    response.get_header.side_effect = {"To": f"<sip:x@192.168.1.14>;tag={tag}"}.get
    return response


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


# ----------------------------------------------------------------------
# Assertions on the REFER subscription
# ----------------------------------------------------------------------


def _notifies(server: SIPServer) -> list[dict[str, Any]]:
    """Every NOTIFY the server sent, decomposed for assertions."""
    out: list[dict[str, Any]] = []
    for c in server._send_message.call_args_list:
        raw, dest = c.args[0], c.args[1]
        if not raw.startswith("NOTIFY "):
            continue
        headers = {}
        for line in raw.split("\r\n")[1:]:
            if not line:
                break
            name, _, value = line.partition(":")
            headers.setdefault(name.strip().lower(), value.strip())
        state = headers.get("subscription-state", "")
        out.append(
            {
                "cseq": int(headers.get("cseq", "0 NOTIFY").split()[0]),
                "state": state,
                "terminated": state.startswith("terminated"),
                "body": raw.split("\r\n\r\n", 1)[1].strip() if "\r\n\r\n" in raw else "",
                "via": headers.get("via"),
                "max_forwards": headers.get("max-forwards"),
                "dest": dest,
            }
        )
    return out


def _assert_notify_contract(server: SIPServer, *, expect_final: bool = True) -> None:
    """
    Assert the properties whose absence leaves a phone stuck "transferring".

    Args:
        server: The SIP server whose sent messages to inspect.
        expect_final: Whether the subscription must have been closed.
    """
    sent = _notifies(server)
    assert sent, "no NOTIFY was sent at all"

    for n in sent:
        assert n["via"], f"NOTIFY {n['body']!r} has no Via; strict UAs drop it"
        assert n["max_forwards"], f"NOTIFY {n['body']!r} has no Max-Forwards"

    cseqs = [n["cseq"] for n in sent]
    assert cseqs == sorted(set(cseqs)), (
        f"NOTIFY CSeqs must strictly increase, got {cseqs} -- a repeat reads as a "
        "retransmission and the phone ignores it"
    )

    finals = [n for n in sent if n["terminated"]]
    if expect_final:
        assert len(finals) == 1, f"expected exactly one final NOTIFY, got {len(finals)}"
    else:
        assert not finals, "did not expect the subscription closed yet"


def _assert_no_leaks(cm: CallManager, relay: RTPRelay, *, allow: set[str] | None = None) -> None:
    """Assert no Call record and no RTP relay outlived the transfer."""
    allowed = allow or set()
    active = set(cm.active_calls)
    assert active <= allowed, f"leaked call records: {sorted(active - allowed)}"
    relays = set(relay.active_relays)
    assert relays <= allowed, f"leaked RTP relays: {sorted(relays - allowed)}"


def _all_legs_terminal(session: Any) -> bool:
    return all(ref.status is LegStatus.TERMINATED for refs in session.legs.values() for ref in refs)


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

    def test_success_is_reported_on_the_refer_subscription(
        self, cm: CallManager, relay: RTPRelay
    ) -> None:
        _pbx, server = self._setup(cm, relay)

        server._handle_refer(
            _refer_message("call1", _replaces_refer_to("1517", "call2"), "1513", A_ADDR),
            A_ADDR,
        )

        _assert_notify_contract(server)
        sent = _notifies(server)
        assert sent[0]["body"] == "SIP/2.0 100 Trying"
        assert sent[-1]["body"] == "SIP/2.0 200 OK"
        assert sent[-1]["dest"] == A_ADDR

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
        assert relay.get_handler("call2") is None
        assert cm.get_call("call2").bridge_peer_side == "a"

        pbx.moh_system.start_moh.reset_mock()
        server._handle_reinvite(
            _hold_reinvite_message("call2", C_RTP), C_ADDR, cm.get_call("call2")
        )

        pbx.moh_system.start_moh.assert_called_once()
        moh_call_id, moh_handler, moh_side = pbx.moh_system.start_moh.call_args[0]
        assert moh_call_id == "call1"
        assert moh_handler is relay.get_handler("call1")
        assert moh_side == "b"

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
        server._send_message.reset_mock()

        server._handle_bye(_bye_message("call1"), B_ADDR)

        raw, dest = server._send_message.call_args[0]
        assert dest == C_ADDR
        assert raw.startswith("BYE ")
        _assert_no_leaks(cm, relay)

    def test_destination_hangup_after_bridge_sends_real_bye_to_transferee(
        self, cm: CallManager, relay: RTPRelay
    ) -> None:
        _pbx, server = self._setup(cm, relay)
        server._handle_refer(
            _refer_message("call1", _replaces_refer_to("1517", "call2"), "1513", A_ADDR),
            A_ADDR,
        )
        server._send_message.reset_mock()

        server._handle_bye(_bye_message("call2"), C_ADDR)

        raw, dest = server._send_message.call_args[0]
        assert dest == B_ADDR
        assert raw.startswith("BYE ")
        _assert_no_leaks(cm, relay)


# ===========================================================================
# 2. Mid-transfer hangup matrix -- party x stage
# ===========================================================================


@pytest.mark.unit
class TestHangupMatrix:
    """
    Every party hanging up at every stage of a transfer.

    This is the failure class that used to leave legs alive and phones stuck:
    the interesting cases are not the happy path but the ones where somebody
    disappears while the transfer is still in flight.
    """

    def _blind_in_flight(
        self, cm: CallManager, relay: RTPRelay, **kwargs: Any
    ) -> tuple[MagicMock, SIPServer, Any]:
        """B<->A connected; A blind-transfers B to C. Target still ringing."""
        pbx = _wire_pbx(cm, relay, **kwargs)
        server = _wire_server(pbx)
        pbx.extension_registry.get_address.return_value = C_ADDR

        ports = _seed_relay(relay, "call1")
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
            rtp_ports=ports,
        )
        server._handle_refer(
            _refer_message("call1", "<sip:1517@192.168.1.14:5060>", "1513", A_ADDR),
            A_ADDR,
        )
        session = pbx.transfer_handler.session_for(cm.get_call("call1"))
        assert session is not None
        return pbx, server, session

    def _attended_in_flight(
        self, cm: CallManager, relay: RTPRelay, *, target_answered: bool
    ) -> tuple[MagicMock, SIPServer, Any]:
        """A consulting C while B is parked; REFER already sent."""
        pbx = _wire_pbx(cm, relay)
        server = _wire_server(pbx)

        ports = _seed_relay(relay, "call1")
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
            rtp_ports=ports,
        )
        consult_ports = _seed_relay(relay, "call2") if target_answered else None
        _basic_call(
            cm,
            "call2",
            "1513",
            "1517",
            state=CallState.CONNECTED if target_answered else CallState.RINGING,
            # CallRouter records the callee address when it sends the INVITE,
            # well before the answer; answered-ness is callee_rtp.
            caller_addr=A_ADDR,
            callee_addr=C_ADDR,
            caller_rtp={"address": A_ADDR[0], "port": 100},
            callee_rtp=C_RTP if target_answered else None,
            rtp_ports=consult_ports,
        )
        server._handle_refer(
            _refer_message("call1", _replaces_refer_to("1517", "call2"), "1513", A_ADDR),
            A_ADDR,
        )
        session = pbx.transfer_handler.session_for(cm.get_call("call1"))
        return pbx, server, session

    # --- Transferor hangs up ---

    def test_transferor_hangup_while_target_rings_proceeds_semi_attended(
        self, cm: CallManager, relay: RTPRelay
    ) -> None:
        """Asterisk semantics: this is a semi-attended transfer, not an abort."""
        _pbx, server, session = self._attended_in_flight(cm, relay, target_answered=False)
        assert session.state is TransferState.COMPLETING

        server._handle_bye(_bye_message("call1"), A_ADDR)

        # Absorbed, not forwarded: B must stay parked for the bridge.
        assert not session.is_terminal
        assert cm.get_call("call1") is not None
        assert cm.get_call("call1").callee_addr == B_ADDR
        _assert_notify_contract(server, expect_final=False)
        session._cancel_timer()

    def test_semi_attended_completes_when_the_target_finally_answers(
        self, cm: CallManager, relay: RTPRelay
    ) -> None:
        pbx, server, session = self._attended_in_flight(cm, relay, target_answered=False)
        server._handle_bye(_bye_message("call1"), A_ADDR)
        _seed_relay(relay, "call2")

        pbx.call_router.handle_callee_answer("call2", _answer_response(C_RTP), C_ADDR)

        assert session.state is TransferState.CLOSED
        assert cm.get_call("call1").bridged_peer_call_id == "call2"
        _assert_notify_contract(server)
        assert _notifies(server)[-1]["body"] == "SIP/2.0 200 OK"

    def test_transferor_hangup_after_consulting_completes_the_transfer(
        self, cm: CallManager, relay: RTPRelay
    ) -> None:
        _pbx, server, session = self._attended_in_flight(cm, relay, target_answered=True)

        # A REFER naming an already-answered target bridges immediately, and
        # the session retires -- so the transferor's own BYE that follows is a
        # straggler on a dialog the PBX has already finished with.
        assert session is None, "a completed transfer must not leave a session behind"
        assert cm.get_call("call1").bridged_peer_call_id == "call2"
        _assert_notify_contract(server)

        before = set(cm.active_calls)
        server._handle_bye(_bye_message("call1"), A_ADDR)
        assert set(cm.active_calls) == before
        _assert_no_leaks(cm, relay, allow={"call1", "call2"})

    def test_transferor_never_hangs_up_and_target_never_answers(
        self, cm: CallManager, relay: RTPRelay
    ) -> None:
        """The watchdog is the only thing standing between this and a
        permanently parked transferee."""
        _pbx, server, session = self._blind_in_flight(cm, relay)
        assert session.deadline_timer is not None
        session._cancel_timer()

        session._on_watchdog()

        assert session.state is TransferState.CLOSED
        # The transferor was still up, so the pre-transfer call is restored
        # rather than everyone being dropped.
        assert cm.get_call("call1") is not None
        _assert_notify_contract(server)
        assert _notifies(server)[-1]["body"] == "SIP/2.0 408 Request Timeout"

    # --- Transferee hangs up ---

    def test_transferee_hangup_while_target_rings_cancels_the_target(
        self, cm: CallManager, relay: RTPRelay
    ) -> None:
        _pbx, server, session = self._blind_in_flight(cm, relay)
        server._handle_bye(_bye_message("call1"), A_ADDR)  # transferor leaves
        server._send_message.reset_mock()

        server._handle_bye(_bye_message("call1"), B_ADDR)

        # Nobody left to hand off: the ringing target is CANCELled, not left
        # ringing at a destination with no one to connect to.
        cancels = [
            c.args for c in server._send_message.call_args_list if c.args[0].startswith("CANCEL ")
        ]
        assert cancels, "ringing target was never cancelled"
        assert session.state is TransferState.CLOSED
        assert _all_legs_terminal(session)
        _assert_no_leaks(cm, relay)

    def test_transferee_hangup_during_consultation_tears_everything_down(
        self, cm: CallManager, relay: RTPRelay
    ) -> None:
        _pbx, server, session = self._attended_in_flight(cm, relay, target_answered=False)
        server._handle_bye(_bye_message("call1"), A_ADDR)

        server._handle_bye(_bye_message("call1"), B_ADDR)

        assert session.state is TransferState.CLOSED
        assert _all_legs_terminal(session)
        _assert_notify_contract(server)
        _assert_no_leaks(cm, relay)

    # --- Target hangs up / fails ---

    def test_target_declines_while_transferor_still_present_rolls_back(
        self, cm: CallManager, relay: RTPRelay
    ) -> None:
        pbx, server, session = self._blind_in_flight(cm, relay)
        target_id = session.target_call_id

        pbx.transfer_handler.on_leg_event(cm.get_call(target_id), LegEvent.REJECTED, side="callee")

        # A is still on the line, so the pre-transfer call simply resumes.
        assert session.state is TransferState.CLOSED
        original = cm.get_call("call1")
        assert original is not None
        assert original.caller_addr == A_ADDR
        assert original.callee_addr == B_ADDR
        _assert_notify_contract(server)
        _assert_no_leaks(cm, relay, allow={"call1"})

    def test_target_declines_after_transferor_left_drops_everything(
        self, cm: CallManager, relay: RTPRelay
    ) -> None:
        pbx, server, session = self._blind_in_flight(cm, relay)
        server._handle_bye(_bye_message("call1"), A_ADDR)
        target_id = session.target_call_id

        pbx.transfer_handler.on_leg_event(cm.get_call(target_id), LegEvent.REJECTED, side="callee")

        assert session.state is TransferState.CLOSED
        assert _all_legs_terminal(session)
        _assert_notify_contract(server)
        _assert_no_leaks(cm, relay)

    def test_target_hangs_up_after_answering_but_before_the_bridge(
        self, cm: CallManager, relay: RTPRelay
    ) -> None:
        pbx, server, session = self._attended_in_flight(cm, relay, target_answered=False)
        target = cm.get_call("call2")
        target.callee_addr = C_ADDR
        session.legs[LegRole.TARGET][0].status = LegStatus.ANSWERED
        server._handle_bye(_bye_message("call1"), A_ADDR)
        session._cancel_timer()

        pbx.transfer_handler.on_leg_event(target, LegEvent.BYE, addr=C_ADDR)

        assert session.state is TransferState.CLOSED
        assert _all_legs_terminal(session)
        _assert_no_leaks(cm, relay)

    # --- After the bridge ---

    def test_stale_bye_from_the_departed_transferor_is_absorbed(
        self, cm: CallManager, relay: RTPRelay
    ) -> None:
        _pbx, server, _session = self._attended_in_flight(cm, relay, target_answered=True)
        before = set(cm.active_calls)

        server._handle_bye(_bye_message("call1"), A_ADDR)

        # The transferor is long gone; their straggling BYE must not collapse
        # a call it no longer belongs to.
        assert set(cm.active_calls) == before


# ===========================================================================
# 3. Teardown guarantees
# ===========================================================================


@pytest.mark.unit
class TestTeardownGuarantees:
    def _in_flight(self, cm: CallManager, relay: RTPRelay) -> tuple[MagicMock, SIPServer, Any]:
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
        return pbx, server, pbx.transfer_handler.session_for(cm.get_call("call1"))

    def test_abort_is_idempotent(self, cm: CallManager, relay: RTPRelay) -> None:
        _pbx, server, session = self._in_flight(cm, relay)
        server._handle_bye(_bye_message("call1"), A_ADDR)

        session.abort("first")
        byes_after_first = sum(
            1 for c in server._send_message.call_args_list if c.args[0].startswith("BYE ")
        )
        session.abort("second")

        byes_after_second = sum(
            1 for c in server._send_message.call_args_list if c.args[0].startswith("BYE ")
        )
        assert byes_after_second == byes_after_first
        _assert_notify_contract(server)

    def test_abort_and_complete_cannot_both_run(self, cm: CallManager, relay: RTPRelay) -> None:
        """The watchdog thread and the SIP receive thread genuinely race here;
        the terminal latch is what stops a half-aborted, half-bridged call."""
        _pbx, server, session = self._in_flight(cm, relay)
        target = cm.get_call(session.target_call_id)
        target.callee_rtp = C_RTP
        target.callee_addr = C_ADDR
        session.legs[LegRole.TARGET][0].status = LegStatus.ANSWERED
        session._cancel_timer()

        barrier = threading.Barrier(2)

        def _abort() -> None:
            barrier.wait()
            session.abort("racing")

        def _complete() -> None:
            barrier.wait()
            session.complete()

        threads = [threading.Thread(target=_abort), threading.Thread(target=_complete)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert session.state is TransferState.CLOSED
        # Whichever won, the subscription is closed exactly once.
        _assert_notify_contract(server)

    def test_resolved_session_detaches_from_every_call(
        self, cm: CallManager, relay: RTPRelay
    ) -> None:
        pbx, _server, session = self._in_flight(cm, relay)
        target_id = session.target_call_id
        _seed_relay(relay, target_id)

        pbx.call_router.handle_callee_answer(target_id, _answer_response(C_RTP), C_ADDR)

        # The survivors are an ordinary bridged call now and must not still
        # point at a finished session.
        assert cm.get_call("call1").transfer_session_id is None
        assert cm.get_call(target_id).transfer_session_id is None
        assert not pbx.transfer_handler.sessions


# ===========================================================================
# 4. Recall policy (Asterisk atxferdropcall=no)
# ===========================================================================


@pytest.mark.unit
class TestRecallPolicy:
    def _abandoned(
        self, cm: CallManager, relay: RTPRelay, **kwargs: Any
    ) -> tuple[MagicMock, SIPServer, Any]:
        """Blind transfer in flight with the transferor already gone."""
        pbx = _wire_pbx(cm, relay, **kwargs)
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
        session = pbx.transfer_handler.session_for(cm.get_call("call1"))
        server._handle_bye(_bye_message("call1"), A_ADDR)
        return pbx, server, session

    def test_failure_rings_the_transferor_back(self, cm: CallManager, relay: RTPRelay) -> None:
        pbx, _server, session = self._abandoned(cm, relay, drop_on_failure=False)
        recall = _basic_call(
            cm,
            "recall1",
            "1512",
            "1513",
            state=CallState.CALLING,
            caller_addr=None,
            callee_addr=None,
        )
        pbx.call_originator.originate_call.return_value = recall

        pbx.transfer_handler.on_leg_event(
            cm.get_call(session.target_call_id), LegEvent.REJECTED, side="callee"
        )

        assert session.state is TransferState.RECALLING
        args, _kwargs = pbx.call_originator.originate_call.call_args
        assert args[1] == "1513", "should ring the transferor back"
        # The transferee stays up waiting for them rather than being dropped.
        assert cm.get_call("call1") is not None

    def test_dropcall_skips_the_recall(self, cm: CallManager, relay: RTPRelay) -> None:
        pbx, _server, session = self._abandoned(cm, relay, drop_on_failure=True)

        pbx.transfer_handler.on_leg_event(
            cm.get_call(session.target_call_id), LegEvent.REJECTED, side="callee"
        )

        pbx.call_originator.originate_call.assert_not_called()
        assert session.state is TransferState.CLOSED
        _assert_no_leaks(cm, relay)

    def test_exhausted_recalls_finally_drop_the_transferee(
        self, cm: CallManager, relay: RTPRelay
    ) -> None:
        pbx, server, session = self._abandoned(cm, relay, drop_on_failure=False, callback_retries=1)
        recall = _basic_call(
            cm,
            "recall1",
            "1512",
            "1513",
            state=CallState.CALLING,
            caller_addr=None,
            callee_addr=None,
        )
        pbx.call_originator.originate_call.return_value = recall

        pbx.transfer_handler.on_leg_event(
            cm.get_call(session.target_call_id), LegEvent.REJECTED, side="callee"
        )
        session._on_recall_failure(recall, "no_answer")

        assert session.state is TransferState.CLOSED
        assert cm.get_call("call1") is None
        _assert_notify_contract(server)


# ===========================================================================
# 5. Transferring a call that is ITSELF the result of a previous transfer
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

        # call2 has no relay of its own (released in round 1) but is
        # cross-linked to call1 (which does), so the retarget lands on the
        # actual live relay -- B ends up talking to D.
        call1_handler = relay.get_handler("call1")
        assert call1_handler is not None
        assert call1_handler.endpoint_a == (D_RTP["address"], D_RTP["port"])

        original = cm.get_call("call1")
        assert original.bridged_peer_call_id == "call3"
        assert original.from_extension == "1519"
        assert original.to_extension == "1512"  # B untouched throughout
        assert cm.get_call("call2") is None, "superseded peer leg was cleaned up"
        assert cm.get_call("call3").bridged_peer_call_id == "call1"

    def test_round_two_reports_success_on_its_own_subscription(
        self, cm: CallManager, relay: RTPRelay
    ) -> None:
        _original, _consult, _pbx, server = self._bridged_pair(cm, relay)
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

        server._handle_refer(
            _refer_message("call2", _replaces_refer_to("1519", "call3"), "1517", C_ADDR),
            C_ADDR,
        )

        # C is the second transferor: its phone must be told too, or it is the
        # one left showing "transferring".
        _assert_notify_contract(server)
        assert _notifies(server)[-1]["dest"] == C_ADDR
        assert _notifies(server)[-1]["body"] == "SIP/2.0 200 OK"


# ===========================================================================
# 6. Adversarial / malformed REFERs
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
        session = pbx.transfer_handler.session_for(cm.get_call("call1"))
        assert session.target_call_id == "call2"

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

        # Rejected: accepting it would orphan the first target leg, left
        # ringing C forever with nothing to bridge to.
        assert _notifies(server)[-1]["body"] == "SIP/2.0 480 Temporarily Unavailable"
        assert session.target_call_id == "call2"
        assert cm.get_call("call3") is not None  # second consult untouched

        # The first transfer still completes normally when C answers.
        _seed_relay(relay, "call2")
        pbx.call_router.handle_callee_answer("call2", _answer_response(C_RTP), C_ADDR)
        assert cm.get_call("call1").bridged_peer_call_id == "call2"


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
        # into a self-referential bridge or touch any of its real state.
        assert _notifies(server)[-1]["body"] == "SIP/2.0 400 Bad Request"
        original = cm.get_call("call1")
        assert original is not None
        assert original.bridged_peer_call_id is None
        assert original.transfer_session_id is None
        assert original.callee_addr == B_ADDR
        assert original.callee_rtp == B_RTP


# ===========================================================================
# 7. Blind transfer full sequences
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
        session = pbx.transfer_handler.session_for(cm.get_call("call1"))
        consult_id = session.target_call_id
        assert consult_id is not None
        session._cancel_timer()

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
        _assert_no_leaks(cm, relay)
        _assert_notify_contract(server)

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
        session = pbx.transfer_handler.session_for(cm.get_call("call1"))
        consult_id = session.target_call_id
        server._handle_bye(_bye_message("call1"), A_ADDR)

        pbx.call_router.handle_callee_answer(consult_id, _answer_response(C_RTP), C_ADDR)

        original = cm.get_call("call1")
        assert original.bridged_peer_call_id == consult_id
        # The blind leg used the original's relay directly, so no bridge
        # re-INVITE was needed to move its media.
        handler = relay.get_handler("call1")
        assert handler is not None
        assert handler.endpoint_a == (C_RTP["address"], C_RTP["port"])
        _assert_notify_contract(server)

        server._send_message.reset_mock()
        server._handle_bye(_bye_message("call1"), B_ADDR)
        _raw, dest = server._send_message.call_args[0]
        assert dest == C_ADDR
        _assert_no_leaks(cm, relay)

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

        server._handle_refer(
            _refer_message("call1", "<sip:1519@192.168.1.14:5060>", "1517", B_ADDR),
            B_ADDR,
        )
        session = pbx.transfer_handler.session_for(cm.get_call("call1"))
        consult_id = session.target_call_id
        assert consult_id is not None
        server._handle_bye(_bye_message("call1"), B_ADDR)

        pbx.call_router.handle_callee_answer(consult_id, _answer_response(D_RTP), D_ADDR)

        original = cm.get_call("call1")
        assert original.bridged_peer_call_id == consult_id
        assert original.caller_addr == A_ADDR  # 1513 untouched throughout

        # D hangs up -- the BYE reaching 1513 must carry its real dialog tag.
        server._send_message.reset_mock()
        server._handle_bye(_bye_message(consult_id), D_ADDR)
        raw, dest = server._send_message.call_args[0]
        assert dest == A_ADDR
        assert "From: <sip:1513@192.168.1.14:5060>;tag=pbxminted42" in raw
        _assert_no_leaks(cm, relay)
