"""End-to-end sequences for ordinary two-party calls.

Everything here drives real components through a whole exchange -- INVITE in,
responses out, teardown -- rather than asserting that one routing function
returned True. These are the sequences a PBX has to get right before anything
else works, and they were the gap that let a hangup leave the far end ringing.

The scenarios mirror what a phone actually does:

    A dials B, B answers, one of them hangs up
    A dials B, A gives up while B is ringing        (CANCEL)
    A dials B, B is busy / declines                 (486 / 603)
    A dials B, B never answers                      (no-answer -> voicemail)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from pbx.core.call import CallManager, CallState
from tests.call_harness import (
    CALL_ID,
    CALLEE_ADDR,
    CALLEE_EXT,
    CALLEE_SDP_PORT,
    CALLER_ADDR,
    assert_no_leaks,
    body_of,
    callee_response,
    invite,
    make_pbx,
    request,
    requests,
    responses,
    sdp,
    sent,
)


@pytest.fixture(autouse=True)
def _no_retransmit_timers():
    """Keep INVITE retransmission timers from outliving the test."""
    with patch("pbx.sip.transaction.InviteClientTransaction._schedule_timer_a"):
        yield


@pytest.fixture
def cm() -> CallManager:
    return CallManager()


@pytest.fixture
def pbx(cm: CallManager) -> MagicMock:
    return make_pbx(cm)


def _dial(pbx: MagicMock) -> None:
    """A dials B: the INVITE arrives and the PBX places the callee leg."""
    with patch("threading.Timer"):
        pbx.sip_server._handle_invite(invite(), CALLER_ADDR)


def _answer(pbx: MagicMock) -> None:
    """B's phone answers with SDP."""
    pbx.sip_server._handle_response(
        callee_response("200", "OK", body=sdp(CALLEE_ADDR[0], CALLEE_SDP_PORT)),
        CALLEE_ADDR,
    )


@pytest.mark.unit
class TestBasicCall:
    def test_invite_is_acknowledged_then_offered_to_the_callee(self, pbx: MagicMock) -> None:
        _dial(pbx)

        # 100 Trying goes back before any routing work, per RFC 3261 8.2.6.1.
        assert responses(pbx, to=CALLER_ADDR)[0] == 100

        invites = requests(pbx, "INVITE")
        assert len(invites) == 1, "exactly one leg should be offered"
        raw, dest = invites[0]
        assert dest == CALLEE_ADDR
        assert f"m=audio {pbx.relay_ports[0]}" in body_of(raw), "callee is offered the relay"

    def test_ringing_reaches_the_caller(self, pbx: MagicMock) -> None:
        _dial(pbx)
        pbx.sip_server._handle_response(callee_response("180", "Ringing"), CALLEE_ADDR)

        assert 180 in responses(pbx, to=CALLER_ADDR)

    def test_answer_bridges_media_and_answers_the_caller(
        self, pbx: MagicMock, cm: CallManager
    ) -> None:
        _dial(pbx)
        _answer(pbx)

        # The caller is answered with the PBX relay as its far end.
        ok = [
            raw for raw, dest in sent(pbx) if raw.startswith("SIP/2.0 200") and dest == CALLER_ADDR
        ]
        assert ok, "caller never got a 200 OK"
        assert f"m=audio {pbx.relay_ports[0]}" in body_of(ok[-1])
        # Without a Contact the caller has nowhere to send its ACK or BYE.
        assert "Contact:" in ok[-1], "200 OK to the caller has no Contact header"
        assert "To:" in ok[-1] and ";tag=" in ok[-1].split("To:")[1].split("\r\n")[0], (
            "the caller's dialog needs a To tag"
        )

        # Both endpoints are on the relay, so audio can actually flow.
        pbx.rtp_relay.set_endpoints.assert_called_once()
        _cid, caller_ep, callee_ep = pbx.rtp_relay.set_endpoints.call_args[0]
        assert caller_ep == (CALLER_ADDR[0], 40000)
        assert callee_ep == (CALLEE_ADDR[0], CALLEE_SDP_PORT)

        assert cm.get_call(CALL_ID).state == CallState.CONNECTED

    def test_caller_hangup_forwards_bye_and_releases_everything(
        self, pbx: MagicMock, cm: CallManager
    ) -> None:
        _dial(pbx)
        _answer(pbx)
        pbx.sip_server._handle_bye(request("BYE"), CALLER_ADDR)

        byes = requests(pbx, "BYE")
        assert [dest for _raw, dest in byes] == [CALLEE_ADDR], "callee must be told to hang up"
        assert 200 in responses(pbx, to=CALLER_ADDR)
        assert_no_leaks(pbx)

    def test_callee_hangup_forwards_bye_to_the_caller(self, pbx: MagicMock) -> None:
        _dial(pbx)
        _answer(pbx)
        pbx.sip_server._handle_bye(request("BYE"), CALLEE_ADDR)

        assert [dest for _raw, dest in requests(pbx, "BYE")] == [CALLER_ADDR]
        assert_no_leaks(pbx)


@pytest.mark.unit
class TestCallerGivesUp:
    """CANCEL: the caller hangs up while the callee is still ringing."""

    def test_cancel_stops_the_callee_ringing_and_ends_the_call(
        self, pbx: MagicMock, cm: CallManager
    ) -> None:
        _dial(pbx)
        pbx.sip_server._handle_response(callee_response("180", "Ringing"), CALLEE_ADDR)

        pbx.sip_server._handle_cancel(request("CANCEL"), CALLER_ADDR)

        cancels = requests(pbx, "CANCEL")
        assert cancels, "the ringing callee was never cancelled -- their phone rings on"
        assert cancels[0][1] == CALLEE_ADDR

        # The caller's INVITE transaction is terminated per RFC 3261 9.2.
        assert 487 in responses(pbx, to=CALLER_ADDR), "caller needs 487 Request Terminated"
        assert 200 in responses(pbx, to=CALLER_ADDR), "the CANCEL itself needs a 200 OK"
        assert_no_leaks(pbx)


@pytest.mark.unit
class TestCalleeRejects:
    """The dialled party is busy or declines."""

    @pytest.mark.parametrize(("status", "reason"), [("486", "Busy Here"), ("603", "Decline")])
    def test_rejection_reaches_the_caller(
        self, pbx: MagicMock, cm: CallManager, status: str, reason: str
    ) -> None:
        _dial(pbx)
        pbx.sip_server._handle_response(callee_response(status, reason), CALLEE_ADDR)

        assert int(status) in responses(pbx, to=CALLER_ADDR), (
            f"{status} never reached the caller, so their phone plays no busy tone"
        )
        assert_no_leaks(pbx)

    def test_rejection_is_acked_so_the_callee_stops_retransmitting(self, pbx: MagicMock) -> None:
        _dial(pbx)
        pbx.sip_server._handle_response(callee_response("486", "Busy Here"), CALLEE_ADDR)

        acks = requests(pbx, "ACK")
        assert acks, "a non-2xx final response must be ACKed (RFC 3261 17.1.1.3)"
        assert acks[0][1] == CALLEE_ADDR


@pytest.mark.unit
class TestNoAnswer:
    def test_no_answer_cancels_the_callee(self, pbx: MagicMock, cm: CallManager) -> None:
        _dial(pbx)
        pbx.sip_server._handle_response(callee_response("180", "Ringing"), CALLEE_ADDR)

        pbx.call_router._handle_no_answer(CALL_ID)

        cancels = requests(pbx, "CANCEL")
        assert cancels and cancels[0][1] == CALLEE_ADDR, "an unanswered phone must be cancelled"

    def test_no_answer_routes_the_caller_to_voicemail(
        self, pbx: MagicMock, cm: CallManager
    ) -> None:
        _dial(pbx)
        pbx.sip_server._handle_response(callee_response("180", "Ringing"), CALLEE_ADDR)

        # Playing the greeting is real RTP and takes seconds of wall clock;
        # stub just that. The recorder stays real so the mailbox is genuinely
        # opened.
        with patch("pbx.rtp.handler.RTPPlayer"), patch("threading.Timer"):
            pbx.call_router._handle_no_answer(CALL_ID)

        call = cm.get_call(CALL_ID)
        assert call.routed_to_voicemail is True
        # The caller is answered so they can hear the greeting and leave a
        # message -- the callee's mailbox, not the caller's.
        assert 200 in responses(pbx, to=CALLER_ADDR)
        pbx.voicemail_system.get_mailbox.assert_called_once_with(CALLEE_EXT)
