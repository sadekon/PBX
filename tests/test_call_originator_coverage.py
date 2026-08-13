"""Tests for pbx/core/call_originator.py - PBX-initiated call origination."""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from pbx.core.call import Call, CallState
from pbx.core.call_originator import CallOriginator
from pbx.core.call_router import CallRouter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pbx_core(*, config_overrides: dict[str, Any] | None = None) -> MagicMock:
    """Build a mock PBXCore wired for CallOriginator, tracking real Call objects
    per call_id (unlike test_call_router_coverage's single-mock-call helper --
    originate_and_bridge() creates two distinct Call objects per invocation)."""
    pbx = MagicMock()

    config_data: dict[str, Any] = {
        "server.sip_port": 5060,
        "voicemail.no_answer_timeout": 30,
    }
    if config_overrides:
        config_data.update(config_overrides)
    pbx.config = MagicMock()
    pbx.config.get.side_effect = lambda key, default=None: config_data.get(key, default)

    pbx.logger = MagicMock()

    calls: dict[str, Call] = {}

    def create_call(call_id: str, from_ext: str, to_ext: str) -> Call:
        call = Call(call_id, from_ext, to_ext)
        calls[call_id] = call
        return call

    def get_call(call_id: str) -> Call | None:
        return calls.get(call_id)

    def end_call(call_id: str) -> None:
        calls.pop(call_id, None)

    pbx.call_manager = MagicMock()
    pbx.call_manager.create_call.side_effect = create_call
    pbx.call_manager.get_call.side_effect = get_call
    pbx._test_calls = calls  # test-only introspection of all created Call objects

    def pbx_end_call(call_id: str) -> None:
        call = calls.get(call_id)
        if call and call.invite_transaction:
            call.invite_transaction.cancel()
        end_call(call_id)

    pbx.end_call.side_effect = pbx_end_call

    dest_ext = MagicMock()
    dest_ext.address = ("10.0.0.2", 5060)
    dest_ext.registered = True
    dest_ext.is_expired.return_value = False
    pbx.extension_registry = MagicMock()
    pbx.extension_registry.get.return_value = dest_ext
    pbx.registered_phones_db = None

    pbx.cdr_system = MagicMock()
    pbx.webhook_system = MagicMock()

    pbx.rtp_relay = MagicMock()
    pbx.rtp_relay.allocate_relay.return_value = (20000, 20001)

    pbx.sip_server = MagicMock()
    pbx._get_server_ip.return_value = "10.0.0.1"

    pbx.trunk_system = MagicMock()

    pbx.call_router = CallRouter(pbx)

    return pbx


def _make_trunk(*, allocate_ok: bool = True) -> MagicMock:
    trunk = MagicMock()
    trunk.host = "trunk.example.com"
    trunk.port = 5060
    trunk.name = "Test Trunk"
    trunk.codec_preferences = ["0", "8", "18"]
    trunk.allocate_channel.return_value = allocate_ok
    return trunk


# ---------------------------------------------------------------------------
# originate_call - internal extension destination
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOriginateCallExtension:
    def test_sends_invite_and_arms_no_answer_timer(self) -> None:
        pbx = _make_pbx_core()
        originator = CallOriginator(pbx)

        # InviteClientTransaction legitimately starts its own Timer A/B (SIP
        # retransmission, RFC 3261) using the same threading.Timer, so filter
        # for calls actually targeting our no-answer handler.
        with patch("threading.Timer") as mock_timer_cls:
            mock_timer_cls.return_value = MagicMock()

            call = originator.originate_call("c2d:web", "1002")

            no_answer_timer_calls = [
                c
                for c in mock_timer_cls.call_args_list
                if c.args[1] == originator._handle_originate_no_answer
            ]
            assert len(no_answer_timer_calls) == 1

        assert call is not None
        assert call.from_extension == "c2d:web"
        assert call.to_extension == "1002"
        assert call.rtp_ports == (20000, 20001)
        pbx.sip_server._send_message.assert_called_once()

    def test_no_original_invite(self) -> None:
        pbx = _make_pbx_core()
        originator = CallOriginator(pbx)

        call = originator.originate_call("c2d:web", "1002")

        assert call is not None
        assert call.original_invite is None
        assert call.caller_addr is None

    def test_unresolvable_extension_fails_and_calls_on_failure(self) -> None:
        pbx = _make_pbx_core()
        pbx.extension_registry.get.return_value = None
        originator = CallOriginator(pbx)

        on_failure = MagicMock()
        call = originator.originate_call("c2d:web", "9999", on_failure=on_failure)

        assert call is None
        on_failure.assert_called_once()
        args = on_failure.call_args[0]
        assert args[1] == "no_route"

    def test_relay_allocation_failure_fails_and_calls_on_failure(self) -> None:
        pbx = _make_pbx_core()
        pbx.rtp_relay.allocate_relay.return_value = None
        originator = CallOriginator(pbx)

        on_failure = MagicMock()
        call = originator.originate_call("c2d:web", "1002", on_failure=on_failure)

        assert call is None
        on_failure.assert_called_once()
        pbx.sip_server._send_message.assert_not_called()

    def test_webrtc_registration_is_not_dialed_over_sip(self) -> None:
        # A WebRTC registration's address is the ("webrtc", session) marker,
        # not a host: dialling it would hand "webrtc" to the socket and fail
        # in DNS instead of failing as a route.
        pbx = _make_pbx_core()
        pbx.extension_registry.get.return_value.address = ("webrtc", "session-abc")
        originator = CallOriginator(pbx)

        on_failure = MagicMock()
        call = originator.originate_call("c2d:web", "1002", on_failure=on_failure)

        assert call is None
        assert on_failure.call_args[0][1] == "no_route"
        pbx.sip_server._send_message.assert_not_called()

    def test_rtp_ports_override_reuses_relay(self) -> None:
        pbx = _make_pbx_core()
        originator = CallOriginator(pbx)

        call = originator.originate_call("1001", "1002", rtp_ports_override=(30000, 30001))

        assert call is not None
        assert call.rtp_ports == (30000, 30001)
        assert call.uses_peer_relay is True
        pbx.rtp_relay.allocate_relay.assert_not_called()


# ---------------------------------------------------------------------------
# originate_call - external (trunk) destination
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOriginateCallTrunk:
    def test_routes_via_trunk_and_allocates_channel(self) -> None:
        pbx = _make_pbx_core()
        trunk = _make_trunk()
        pbx.trunk_system.route_outbound_with_failover.return_value = (
            trunk,
            "12125551234",
        )
        originator = CallOriginator(pbx)

        call = originator.originate_call("1001", "12125551234")

        assert call is not None
        assert call.trunk is trunk
        trunk.allocate_channel.assert_called_once()
        pbx.sip_server._send_message.assert_called_once()

    def test_no_trunk_route_fails(self) -> None:
        pbx = _make_pbx_core()
        pbx.trunk_system.route_outbound_with_failover.return_value = (None, None)
        originator = CallOriginator(pbx)

        on_failure = MagicMock()
        call = originator.originate_call("1001", "12125551234", on_failure=on_failure)

        assert call is None
        on_failure.assert_called_once()
        assert on_failure.call_args[0][1] == "no_route"

    def test_trunk_at_capacity_fails(self) -> None:
        pbx = _make_pbx_core()
        trunk = _make_trunk(allocate_ok=False)
        pbx.trunk_system.route_outbound_with_failover.return_value = (
            trunk,
            "12125551234",
        )
        originator = CallOriginator(pbx)

        on_failure = MagicMock()
        call = originator.originate_call("1001", "12125551234", on_failure=on_failure)

        assert call is None
        on_failure.assert_called_once()
        assert on_failure.call_args[0][1] == "unreachable"

    def test_no_trunk_system_fails(self) -> None:
        pbx = _make_pbx_core()
        pbx.trunk_system = None
        originator = CallOriginator(pbx)

        call = originator.originate_call("1001", "12125551234")

        assert call is None


# ---------------------------------------------------------------------------
# CallRouter.handle_callee_answer - originate_callbacks dispatch
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHandleCalleeAnswerOriginateDispatch:
    def test_on_answer_fires_for_originate_only_call(self) -> None:
        pbx = _make_pbx_core()
        originator = CallOriginator(pbx)

        on_answer = MagicMock()
        with patch("threading.Timer"):
            call = originator.originate_call("1001", "1002", on_answer=on_answer)
        assert call is not None

        response = MagicMock()
        response.body = ""
        response.get_header.return_value = "<sip:1002@10.0.0.2>;tag=xyz"

        pbx.call_router.handle_callee_answer(call.call_id, response, ("10.0.0.2", 5060))

        on_answer.assert_called_once_with(call)
        assert call.state == CallState.CONNECTED

    def test_no_crash_when_no_original_invite_and_no_caller_addr(self) -> None:
        # Regression guard for the exact case CallOriginator relies on:
        # handle_callee_answer's "send 200 OK to caller" block must be
        # skippable without error when there is no real caller leg.
        pbx = _make_pbx_core()
        originator = CallOriginator(pbx)

        with patch("threading.Timer"):
            call = originator.originate_call("1001", "1002")
        assert call is not None
        assert call.caller_addr is None
        assert call.original_invite is None

        response = MagicMock()
        response.body = ""
        response.get_header.return_value = "<sip:1002@10.0.0.2>;tag=xyz"

        # Must not raise, and must not attempt a second send (a "200 OK to
        # caller") beyond the original INVITE already sent by originate_call.
        pbx.call_router.handle_callee_answer(call.call_id, response, ("10.0.0.2", 5060))
        pbx.sip_server._send_message.assert_called_once()


# ---------------------------------------------------------------------------
# _handle_originate_no_answer
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHandleOriginateNoAnswer:
    def test_no_answer_sends_cancel_and_fires_on_failure(self) -> None:
        pbx = _make_pbx_core()
        originator = CallOriginator(pbx)

        on_failure = MagicMock()
        with patch("threading.Timer"):
            call = originator.originate_call("1001", "1002", on_failure=on_failure)
        assert call is not None
        call_id = call.call_id

        originator._handle_originate_no_answer(call_id)

        on_failure.assert_called_once_with(call, "no_answer")
        assert pbx.call_manager.get_call(call_id) is None

    def test_already_connected_call_is_untouched(self) -> None:
        pbx = _make_pbx_core()
        originator = CallOriginator(pbx)

        on_failure = MagicMock()
        with patch("threading.Timer"):
            call = originator.originate_call("1001", "1002", on_failure=on_failure)
        assert call is not None
        call.state = CallState.CONNECTED

        originator._handle_originate_no_answer(call.call_id)

        on_failure.assert_not_called()
        assert pbx.call_manager.get_call(call.call_id) is call

    def test_delegates_teardown_to_pbx_end_call(self) -> None:
        # Trunk-channel release, RTP relay release, and CDR end-record are
        # all handled by PBXCore.end_call() -- same as every other
        # call-teardown path in the codebase. Verify _handle_originate_no_answer
        # delegates to it rather than re-implementing that cleanup.
        pbx = _make_pbx_core()
        trunk = _make_trunk()
        pbx.trunk_system.route_outbound_with_failover.return_value = (
            trunk,
            "12125551234",
        )
        originator = CallOriginator(pbx)

        with patch("threading.Timer"):
            call = originator.originate_call("1001", "12125551234")
        assert call is not None

        originator._handle_originate_no_answer(call.call_id)

        pbx.end_call.assert_called_once_with(call.call_id)


# ---------------------------------------------------------------------------
# originate_and_bridge
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOriginateAndBridge:
    def test_leg_a_originated_immediately(self) -> None:
        pbx = _make_pbx_core()
        originator = CallOriginator(pbx)

        with patch("threading.Timer"):
            leg_a_call, leg_b_call = originator.originate_and_bridge("1001", "1002")

        assert leg_a_call is not None
        assert leg_a_call.to_extension == "1001"
        assert leg_b_call is None

    def test_leg_b_originated_after_leg_a_answers_and_bridge_completes(self) -> None:
        pbx = _make_pbx_core()
        originator = CallOriginator(pbx)

        on_leg_b_answer = MagicMock()
        with patch("threading.Timer"):
            leg_a_call, _ = originator.originate_and_bridge(
                "1001", "1002", on_leg_b_answer=on_leg_b_answer
            )
        assert leg_a_call is not None

        # Simulate leg_a answering (normally driven by handle_callee_answer).
        response = MagicMock()
        response.body = ""
        response.get_header.return_value = "<sip:1001@10.0.0.2>;tag=a"
        pbx.call_router.handle_callee_answer(leg_a_call.call_id, response, ("10.0.0.2", 5060))

        # leg_b should now have been originated, sharing leg_a's relay.
        leg_b_call = next(c for c in pbx._test_calls.values() if c.call_id != leg_a_call.call_id)
        assert leg_b_call.rtp_ports == leg_a_call.rtp_ports
        assert leg_b_call.uses_peer_relay is True

        # Simulate leg_b answering -> bridge completes.
        response_b = MagicMock()
        response_b.body = ""
        response_b.get_header.return_value = "<sip:1002@10.0.0.3>;tag=b"
        pbx.call_router.handle_callee_answer(leg_b_call.call_id, response_b, ("10.0.0.3", 5060))

        assert leg_a_call.bridged_peer_call_id == leg_b_call.call_id
        assert leg_b_call.bridged_peer_call_id == leg_a_call.call_id
        assert leg_a_call.state == CallState.CONNECTED
        assert leg_b_call.state == CallState.CONNECTED
        on_leg_b_answer.assert_called_once_with(leg_b_call)


def _bridge_to_ringing_leg_b(pbx: MagicMock, originator: CallOriginator, **kwargs: Any) -> Any:
    """Drive originate_and_bridge to the state where leg_a has answered and
    leg_b's INVITE is out but unanswered -- the window a mid-setup hangup
    falls in. Returns (leg_a_call, leg_b_call)."""
    with patch("threading.Timer"):
        leg_a_call, _ = originator.originate_and_bridge("1001", "1002", **kwargs)
    assert leg_a_call is not None

    response = MagicMock()
    response.body = ""
    response.get_header.return_value = "<sip:1001@10.0.0.2>;tag=a"
    with patch("threading.Timer"):
        pbx.call_router.handle_callee_answer(leg_a_call.call_id, response, ("10.0.0.2", 5060))

    leg_b_call = next(c for c in pbx._test_calls.values() if c.call_id != leg_a_call.call_id)
    return leg_a_call, leg_b_call


@pytest.mark.unit
class TestMidBridgeTeardown:
    def test_legs_are_cross_linked_while_leg_b_is_still_ringing(self) -> None:
        # The link is what lets a hangup on either leg find the other; waiting
        # for both to answer is what left the destination ringing.
        pbx = _make_pbx_core()
        originator = CallOriginator(pbx)

        leg_a_call, leg_b_call = _bridge_to_ringing_leg_b(pbx, originator)

        assert leg_a_call.bridged_peer_call_id == leg_b_call.call_id
        assert leg_b_call.bridged_peer_call_id == leg_a_call.call_id
        assert leg_b_call.state != CallState.CONNECTED

    def test_leg_a_is_rung_showing_the_destination_as_caller_id(self) -> None:
        # From that party's side the call is with leg_b, not with the PBX,
        # so "originator" on the display would be meaningless.
        pbx = _make_pbx_core()
        originator = CallOriginator(pbx)

        with patch("threading.Timer"):
            originator.originate_and_bridge("1001", "1002")

        invite = pbx.sip_server._send_message.call_args[0][0]
        assert "sip:1002@" in invite.split("From:")[1].split("\n")[0]

    def test_early_media_plays_until_the_bridge_completes(self) -> None:
        pbx = _make_pbx_core()
        originator = CallOriginator(pbx)

        leg_a_call, leg_b_call = _bridge_to_ringing_leg_b(pbx, originator)

        # leg_a is answered and leg_b is ringing: without this the party on
        # leg_a hears dead air until leg_b picks up.
        pbx.moh_system.start_moh.assert_called_once()
        assert pbx.moh_system.start_moh.call_args[0][0] == leg_a_call.call_id
        assert pbx.moh_system.start_moh.call_args[0][2] == "a"
        pbx.moh_system.stop_moh.assert_not_called()

        response = MagicMock()
        response.body = ""
        response.get_header.return_value = "<sip:1002@10.0.0.3>;tag=b"
        pbx.call_router.handle_callee_answer(leg_b_call.call_id, response, ("10.0.0.3", 5060))

        pbx.moh_system.stop_moh.assert_called_once_with(leg_a_call.call_id)

    def test_ringing_leg_failure_ends_the_answered_leg(self) -> None:
        # Nobody picks up the destination, so the party who answered first
        # must not be left connected to a dead relay.
        pbx = _make_pbx_core()
        originator = CallOriginator(pbx)

        leg_a_call, leg_b_call = _bridge_to_ringing_leg_b(pbx, originator)
        leg_a_call.state = CallState.CONNECTED

        originator._handle_originate_no_answer(leg_b_call.call_id)

        pbx.sip_server.end_bridged_peer.assert_called_once_with(leg_b_call)
        pbx.end_call.assert_any_call(leg_b_call.call_id)

    def test_leg_a_is_hung_up_when_leg_b_cannot_be_originated(self) -> None:
        pbx = _make_pbx_core()
        originator = CallOriginator(pbx)

        with patch("threading.Timer"):
            leg_a_call, _ = originator.originate_and_bridge("1001", "1002")
        assert leg_a_call is not None

        # leg_b's destination stops resolving before it is dialled.
        pbx.extension_registry.get.return_value = None
        response = MagicMock()
        response.body = ""
        response.get_header.return_value = "<sip:1001@10.0.0.2>;tag=a"
        with patch("threading.Timer"):
            pbx.call_router.handle_callee_answer(leg_a_call.call_id, response, ("10.0.0.2", 5060))

        pbx.sip_server._send_leg_bye.assert_called_once_with(leg_a_call)
        pbx.end_call.assert_any_call(leg_a_call.call_id)
