"""Tests for REFER-based call transfer (RFC 3515) with Replaces (RFC 3891).

Covers the flow observed from real Zultys ZIP phones, exercised through the
transfer state machine in ``pbx/core/transfer_session.py``:

- Refer-To parsing, including URL-encoded embedded Replaces headers
- The NOTIFY contract on the implicit REFER subscription -- routable headers,
  strictly increasing CSeq, and exactly one terminating NOTIFY. Getting any of
  these wrong is what leaves a transferor's phone stuck showing "transferring"
- Attended transfer: REFER with Replaces bridging the transferee with the
  target, with the transferor's two legs deterministically hung up
- Semi-attended: REFER before the target answers, including the watchdog that
  keeps a never-answered target from parking the transferee forever
- Blind transfer: REFER without Replaces, PBX-originated target leg
- Rejection of overlapping and self-referencing REFERs
- Abort paths: decline, timeout, and hangup, each leaving no leg behind
"""

from __future__ import annotations

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
from pbx.core.transfer_session import (
    NOTIFY_OK,
    NOTIFY_TRYING,
    LegEvent,
    LegRole,
    LegStatus,
    ReferDialog,
    TransferMode,
    TransferState,
)
from pbx.sip.server import SIPServer
from tests.transfer_harness import (
    A_ADDR,
    B_ADDR,
    C_ADDR,
    C_RTP,
    CAPTURE_REFER_TO,
    assert_no_leaks,
    assert_notify_contract,
    attended_pair,
    make_call,
    make_pbx,
    messages_of,
    notifies,
)


def _refer_message(refer_to: str = CAPTURE_REFER_TO, call_id: str = "call1") -> MagicMock:
    """A REFER as a Zultys ZIP sends it."""
    msg = MagicMock()
    msg.get_header.side_effect = {
        "Refer-To": refer_to,
        "Call-ID": call_id,
        "Referred-By": '"Test" <sip:1513@192.168.1.14:5060>',
        "From": '"Test" <sip:1513@192.168.1.14:5060>;tag=1687173424',
        "To": "<sip:1512@192.168.1.14:5060;user=phone>;tag=306a7b6a",
    }.get
    return msg


def _dialog(call_id: str = "call1") -> ReferDialog:
    """A REFER subscription to report progress on."""
    return ReferDialog(
        call_id=call_id,
        from_header="<sip:1512@192.168.1.14:5060;user=phone>;tag=306a7b6a",
        to_header='"Test" <sip:1513@192.168.1.14:5060>;tag=1687173424',
        addr=A_ADDR,
    )


# ----------------------------------------------------------------------


@pytest.mark.unit
class TestParseReferTo:
    """Refer-To parsing (RFC 3515 embedded headers, RFC 3891 Replaces)."""

    def test_plain_uri(self) -> None:
        dest, headers = SIPServer._parse_refer_to("<sip:1517@192.168.1.14>")
        assert dest == "1517"
        assert headers == {}

    def test_uri_without_brackets(self) -> None:
        dest, headers = SIPServer._parse_refer_to("sip:1517@192.168.1.14")
        assert dest == "1517"
        assert headers == {}

    def test_captured_attended_refer_to(self) -> None:
        dest, headers = SIPServer._parse_refer_to(CAPTURE_REFER_TO)
        assert dest == "1517"
        assert headers["replaces"] == (
            "0_3666015387@192.168.10.139;to-tag=92aec109;from-tag=3228926622"
        )

    def test_multiple_embedded_headers(self) -> None:
        dest, headers = SIPServer._parse_refer_to(
            "<sip:1517@pbx?Replaces=abc%3Bto-tag%3Dx&Require=replaces>"
        )
        assert dest == "1517"
        assert headers["replaces"] == "abc;to-tag=x"
        assert headers["require"] == "replaces"

    def test_uri_parameters_are_not_part_of_the_user(self) -> None:
        dest, _ = SIPServer._parse_refer_to("<sip:1517@pbx;user=phone>")
        assert dest == "1517"

    def test_malformed_returns_none(self) -> None:
        dest, headers = SIPServer._parse_refer_to("not-a-uri")
        assert dest is None
        assert headers == {}


@pytest.mark.unit
class TestNotifyContract:
    """
    The REFER subscription's NOTIFYs.

    These are the only way a transferor's phone learns a transfer ended, so
    each property here maps directly to a stuck "transferring" indicator.
    """

    def test_notify_carries_mandatory_routing_headers(self) -> None:
        pbx = make_pbx(CallManager())
        dialog = _dialog()

        pbx.sip_server.send_transfer_notify(dialog, NOTIFY_TRYING, terminated=False)

        sent = notifies(pbx)
        assert len(sent) == 1
        # RFC 3261 SS8.1.1.6-7: strict UAs silently drop requests without these,
        # so the phone would never see the transfer progress at all.
        assert sent[0]["via"], "NOTIFY must carry Via"
        assert sent[0]["max_forwards"], "NOTIFY must carry Max-Forwards"

    def test_each_notify_uses_a_new_cseq(self) -> None:
        pbx = make_pbx(CallManager())
        dialog = _dialog()

        pbx.sip_server.send_transfer_notify(dialog, NOTIFY_TRYING, terminated=False)
        pbx.sip_server.send_transfer_notify(dialog, NOTIFY_OK, terminated=True)

        cseqs = [n["cseq"] for n in notifies(pbx)]
        # A repeated CSeq reads as a retransmission of the first NOTIFY, so the
        # phone ignores the final one and never clears its transfer state.
        assert len(cseqs) == len(set(cseqs)), f"CSeq reused: {cseqs}"
        assert cseqs == sorted(cseqs)

    def test_only_one_terminating_notify_is_sent(self) -> None:
        pbx = make_pbx(CallManager())
        dialog = _dialog()

        pbx.sip_server.send_transfer_notify(dialog, NOTIFY_OK, terminated=True)
        pbx.sip_server.send_transfer_notify(dialog, "SIP/2.0 500 Oops", terminated=True)

        sent = notifies(pbx)
        assert len(sent) == 1
        assert sent[0]["terminated"]

    def test_non_final_notify_keeps_subscription_active(self) -> None:
        pbx = make_pbx(CallManager())

        pbx.sip_server.send_transfer_notify(_dialog(), NOTIFY_TRYING, terminated=False)

        assert notifies(pbx)[0]["state"].startswith("active")


@pytest.mark.unit
class TestAttendedTransfer:
    """REFER with Replaces, target already answered: bridge immediately."""

    def test_bridges_and_reports_success(self) -> None:
        pbx = make_pbx(CallManager())
        original, consult = attended_pair(pbx)

        session = pbx.transfer_handler.start_transfer(
            original,
            mode=TransferMode.ATTENDED,
            transferor_side="caller",
            refer_dialog=_dialog(),
            existing_consult=consult,
        )

        assert session is not None
        assert session.state is TransferState.CLOSED
        # The transferee's record survives, retargeted at the transfer target.
        assert original.bridged_peer_call_id == consult.call_id
        assert consult.bridged_peer_call_id == original.call_id
        assert original.from_extension == "1517"
        assert original.caller_rtp == C_RTP
        assert_notify_contract(pbx)
        assert notifies(pbx)[-1]["body"] == NOTIFY_OK

    def test_transferor_gets_a_bye_for_both_of_its_legs(self) -> None:
        pbx = make_pbx(CallManager())
        original, consult = attended_pair(pbx)

        pbx.transfer_handler.start_transfer(
            original,
            mode=TransferMode.ATTENDED,
            transferor_side="caller",
            refer_dialog=_dialog(),
            existing_consult=consult,
        )

        # Relying on the transferor's phone to drop its own legs does not hold
        # universally: some leave a dialog up and appear stuck connected.
        bye_destinations = [dest for _, dest in messages_of(pbx, "BYE")]
        assert bye_destinations.count(A_ADDR) == 2

    def test_transferee_and_target_are_not_hung_up(self) -> None:
        pbx = make_pbx(CallManager())
        original, consult = attended_pair(pbx)

        pbx.transfer_handler.start_transfer(
            original,
            mode=TransferMode.ATTENDED,
            transferor_side="caller",
            refer_dialog=_dialog(),
            existing_consult=consult,
        )

        bye_destinations = [dest for _, dest in messages_of(pbx, "BYE")]
        assert B_ADDR not in bye_destinations
        assert C_ADDR not in bye_destinations
        assert_no_leaks(pbx, allow={"call1", "call2"})

    def test_transferor_on_the_callee_side(self) -> None:
        """The transferor may be the callee -- B transferring A onward."""
        pbx = make_pbx(CallManager())
        cm = pbx.call_manager
        relays = pbx.rtp_relay.active_relays

        original = make_call(
            cm,
            "call1",
            "1512",
            "1513",
            state=CallState.HOLD,
            caller_addr=B_ADDR,
            callee_addr=A_ADDR,
            relays=relays,
        )
        consult = make_call(
            cm,
            "call2",
            "1513",
            "1517",
            caller_addr=A_ADDR,
            callee_addr=C_ADDR,
            callee_rtp=C_RTP,
            relays=relays,
        )

        pbx.transfer_handler.start_transfer(
            original,
            mode=TransferMode.ATTENDED,
            transferor_side="callee",
            refer_dialog=_dialog(),
            existing_consult=consult,
        )

        # The callee side of the original is what gets retargeted.
        assert original.to_extension == "1517"
        assert original.callee_rtp == C_RTP
        assert original.callee_addr is None

    def test_refuses_to_bridge_an_unanswered_target(self) -> None:
        pbx = make_pbx(CallManager())
        original, consult = attended_pair(pbx, consult_answered=False)

        session = pbx.transfer_handler.start_transfer(
            original,
            mode=TransferMode.ATTENDED,
            transferor_side="caller",
            refer_dialog=_dialog(),
            existing_consult=consult,
        )

        assert session is not None
        # Committed, but waiting on the answer -- nothing bridged yet.
        assert session.state is TransferState.COMPLETING
        assert original.bridged_peer_call_id is None
        assert_notify_contract(pbx, expect_final=False)


@pytest.mark.unit
class TestSemiAttendedTransfer:
    """REFER before the target answers: the bridge is deferred to the answer."""

    def test_answer_completes_the_deferred_bridge(self) -> None:
        pbx = make_pbx(CallManager())
        original, consult = attended_pair(pbx, consult_answered=False)

        session = pbx.transfer_handler.start_transfer(
            original,
            mode=TransferMode.ATTENDED,
            transferor_side="caller",
            refer_dialog=_dialog(),
            existing_consult=consult,
        )
        assert session is not None

        # Target picks up.
        consult.callee_rtp = C_RTP
        consult.callee_addr = C_ADDR
        pbx.transfer_handler.on_leg_event(consult, LegEvent.ANSWERED, side="callee")

        assert session.state is TransferState.CLOSED
        assert original.bridged_peer_call_id == consult.call_id
        assert_notify_contract(pbx)

    def test_transferor_hangup_before_answer_keeps_the_transfer_alive(self) -> None:
        """
        Asterisk semantics: hanging up while the target rings is a
        semi-attended transfer, and it proceeds rather than aborting.
        """
        pbx = make_pbx(CallManager())
        original, consult = attended_pair(pbx, consult_answered=False)

        session = pbx.transfer_handler.start_transfer(
            original,
            mode=TransferMode.ATTENDED,
            transferor_side="caller",
            refer_dialog=_dialog(),
            existing_consult=consult,
        )
        assert session is not None

        result = pbx.transfer_handler.on_leg_event(original, LegEvent.BYE, addr=A_ADDR)

        # Absorbed, not forwarded: the parked transferee must survive.
        assert result.value == "absorb"
        assert not session.is_terminal
        assert "call1" in pbx.call_manager.active_calls

    def test_watchdog_is_armed_so_a_silent_target_cannot_park_forever(self) -> None:
        pbx = make_pbx(CallManager())
        original, consult = attended_pair(pbx, consult_answered=False)

        session = pbx.transfer_handler.start_transfer(
            original,
            mode=TransferMode.ATTENDED,
            transferor_side="caller",
            refer_dialog=_dialog(),
            existing_consult=consult,
        )

        assert session is not None
        # Without this the transferee parks on hold indefinitely and the
        # target leg rings forever.
        assert session.deadline_timer is not None
        assert session.deadline_timer.is_alive()
        session._cancel_timer()

    def test_watchdog_expiry_aborts_and_reports_timeout(self) -> None:
        pbx = make_pbx(CallManager())
        original, consult = attended_pair(pbx, consult_answered=False)

        session = pbx.transfer_handler.start_transfer(
            original,
            mode=TransferMode.ATTENDED,
            transferor_side="caller",
            refer_dialog=_dialog(),
            existing_consult=consult,
        )
        assert session is not None
        session._cancel_timer()

        # Transferor already gone, so there is nobody to roll back to.
        pbx.transfer_handler.on_leg_event(original, LegEvent.BYE, addr=A_ADDR)
        session._on_watchdog()

        assert session.state is TransferState.CLOSED
        assert_notify_contract(pbx)
        assert notifies(pbx)[-1]["body"] == "SIP/2.0 408 Request Timeout"
        assert_no_leaks(pbx)


@pytest.mark.unit
class TestBlindTransfer:
    """REFER without Replaces: the PBX originates the target leg itself."""

    def test_originates_target_leg_on_the_existing_relay(self) -> None:
        pbx = make_pbx(CallManager())
        cm = pbx.call_manager
        original = make_call(
            cm,
            "call1",
            "1513",
            "1512",
            caller_addr=A_ADDR,
            callee_addr=B_ADDR,
            relays=pbx.rtp_relay.active_relays,
        )
        pbx.extension_registry.is_registered.return_value = True
        pbx.extension_registry.get_address.return_value = C_ADDR

        session = pbx.transfer_handler.start_transfer(
            original,
            "1517",
            mode=TransferMode.BLIND,
            transferor_side="caller",
            refer_dialog=_dialog(),
        )

        assert session is not None
        invites = messages_of(pbx, "INVITE")
        assert len(invites) == 1
        raw, dest = invites[0]
        assert dest == C_ADDR
        # Advertising the surviving relay's port means no re-INVITE is needed
        # once the target answers.
        assert "20000" in raw
        target = pbx.call_manager.get_call(session.target_call_id)
        assert target.uses_peer_relay is True

    def test_unregistered_destination_leaves_the_call_untouched(self) -> None:
        pbx = make_pbx(CallManager())
        cm = pbx.call_manager
        original = make_call(
            cm,
            "call1",
            "1513",
            "1512",
            caller_addr=A_ADDR,
            callee_addr=B_ADDR,
            relays=pbx.rtp_relay.active_relays,
        )
        pbx.extension_registry.is_registered.return_value = False

        session = pbx.transfer_handler.start_transfer(
            original,
            "9999",
            mode=TransferMode.BLIND,
            transferor_side="caller",
            refer_dialog=_dialog(),
        )

        assert session is None
        # The transferor stays connected and nothing was torn down...
        assert "call1" in cm.active_calls
        assert original.transfer_session_id is None
        assert not messages_of(pbx, "BYE")
        # ...but their phone is still told, or it hangs on "transferring".
        assert_notify_contract(pbx)
        assert notifies(pbx)[-1]["body"] == "SIP/2.0 404 Not Found"

    def test_answered_target_bridges_without_a_reinvite(self) -> None:
        pbx = make_pbx(CallManager())
        cm = pbx.call_manager
        original = make_call(
            cm,
            "call1",
            "1513",
            "1512",
            caller_addr=A_ADDR,
            callee_addr=B_ADDR,
            relays=pbx.rtp_relay.active_relays,
        )
        pbx.extension_registry.is_registered.return_value = True
        pbx.extension_registry.get_address.return_value = C_ADDR

        session = pbx.transfer_handler.start_transfer(
            original,
            "1517",
            mode=TransferMode.BLIND,
            transferor_side="caller",
            refer_dialog=_dialog(),
        )
        assert session is not None
        target = pbx.call_manager.get_call(session.target_call_id)
        target.callee_rtp = C_RTP
        target.callee_addr = C_ADDR

        pbx.transfer_handler.on_leg_event(target, LegEvent.ANSWERED, side="callee")

        assert session.state is TransferState.CLOSED
        # Exactly one INVITE: the original origination, no bridge re-INVITE.
        assert len(messages_of(pbx, "INVITE")) == 1
        assert original.bridged_peer_call_id == target.call_id


@pytest.mark.unit
class TestReferRejection:
    """REFERs that must not be acted on."""

    def test_overlapping_refer_is_rejected(self) -> None:
        pbx = make_pbx(CallManager())
        original, consult = attended_pair(pbx, consult_answered=False)

        first = pbx.transfer_handler.start_transfer(
            original,
            mode=TransferMode.ATTENDED,
            transferor_side="caller",
            refer_dialog=_dialog(),
            existing_consult=consult,
        )
        second = pbx.transfer_handler.start_transfer(
            original,
            "1599",
            mode=TransferMode.BLIND,
            transferor_side="caller",
            refer_dialog=_dialog(),
        )

        assert first is not None
        # Accepting it would orphan the first target leg, left ringing with
        # nothing to bridge to.
        assert second is None
        assert original.transfer_session_id == first.session_id
        if first.deadline_timer:
            first.deadline_timer.cancel()

    def test_self_referencing_replaces_is_rejected(self) -> None:
        pbx = make_pbx(CallManager())
        original, _ = attended_pair(pbx)
        server = pbx.sip_server

        msg = _refer_message("<sip:1517@pbx?Replaces=call1%3Bto-tag%3Dx>")
        server._handle_refer(msg, A_ADDR)

        assert original.transfer_session_id is None
        assert notifies(pbx)[-1]["body"] == "SIP/2.0 400 Bad Request"

    def test_refer_for_an_unknown_dialog_is_rejected(self) -> None:
        pbx = make_pbx(CallManager())
        server = pbx.sip_server

        server._handle_refer(_refer_message(call_id="nope"), A_ADDR)

        assert notifies(pbx)[-1]["body"] == "SIP/2.0 481 Call/Transaction Does Not Exist"

    def test_unknown_replaces_dialog_is_rejected(self) -> None:
        pbx = make_pbx(CallManager())
        make_call(
            pbx.call_manager,
            "call1",
            "1513",
            "1512",
            caller_addr=A_ADDR,
            callee_addr=B_ADDR,
        )
        server = pbx.sip_server

        server._handle_refer(_refer_message(), A_ADDR)

        assert notifies(pbx)[-1]["body"] == "SIP/2.0 481 Call/Transaction Does Not Exist"


@pytest.mark.unit
class TestHandleReferDispatch:
    """_handle_refer's job is to validate, accept, and delegate."""

    def test_accepts_and_sends_trying_before_anything_else(self) -> None:
        pbx = make_pbx(CallManager())
        _original, consult = attended_pair(pbx)
        server = pbx.sip_server
        # Register the consult under the Call-ID named in Replaces.
        pbx.call_manager.active_calls["0_3666015387@192.168.10.139"] = (
            pbx.call_manager.active_calls.pop("call2")
        )
        consult.call_id = "0_3666015387@192.168.10.139"

        server._handle_refer(_refer_message(), A_ADDR)

        assert server._send_response.call_args_list[0][0][0] == 202
        assert notifies(pbx)[0]["body"] == NOTIFY_TRYING
        assert notifies(pbx)[0]["state"].startswith("active")
        assert_notify_contract(pbx)

    def test_missing_refer_to_is_a_bad_request(self) -> None:
        pbx = make_pbx(CallManager())
        server = pbx.sip_server
        msg = MagicMock()
        msg.get_header.side_effect = {"Call-ID": "call1"}.get

        server._handle_refer(msg, A_ADDR)

        assert not notifies(pbx)
        server._send_response.assert_called_once()


@pytest.mark.unit
class TestSessionRoster:
    """The leg roster is what makes 'every leg is closed' structural."""

    def test_roster_maps_both_parties_of_the_original_call(self) -> None:
        pbx = make_pbx(CallManager())
        original, consult = attended_pair(pbx, consult_answered=False)

        session = pbx.transfer_handler.start_transfer(
            original,
            mode=TransferMode.ATTENDED,
            transferor_side="caller",
            refer_dialog=_dialog(),
            existing_consult=consult,
        )
        assert session is not None

        assert session.role_for("call1", "caller") is LegRole.TRANSFEROR
        assert session.role_for("call1", "callee") is LegRole.TRANSFEREE
        assert session.role_for("call2", "callee") is LegRole.TARGET
        # The transferor holds a leg on the consultation call too.
        assert session.role_for("call2", "caller") is LegRole.TRANSFEROR
        session._cancel_timer()

    def test_roles_resolve_by_source_address(self) -> None:
        pbx = make_pbx(CallManager())
        original, consult = attended_pair(pbx, consult_answered=False)

        session = pbx.transfer_handler.start_transfer(
            original,
            mode=TransferMode.ATTENDED,
            transferor_side="caller",
            refer_dialog=_dialog(),
            existing_consult=consult,
        )
        assert session is not None

        assert session.role_for_address(original, A_ADDR) is LegRole.TRANSFEROR
        assert session.role_for_address(original, B_ADDR) is LegRole.TRANSFEREE
        assert session.role_for_address(original, ("9.9.9.9", 5060)) is None
        session._cancel_timer()

    def test_target_leg_starts_unanswered(self) -> None:
        pbx = make_pbx(CallManager())
        original, consult = attended_pair(pbx, consult_answered=False)

        session = pbx.transfer_handler.start_transfer(
            original,
            mode=TransferMode.ATTENDED,
            transferor_side="caller",
            refer_dialog=_dialog(),
            existing_consult=consult,
        )
        assert session is not None

        assert session.legs[LegRole.TARGET][0].status is LegStatus.RINGING
        session._cancel_timer()
