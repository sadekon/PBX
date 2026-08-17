"""
Find Me/Follow Me ring execution: the part that actually rings destinations.

The existing FMFM tests (test_fmfm_persistence, test_fmfm_save_failure) cover the
other half of this feature -- the config store -- and need PostgreSQL, so they
skip on a machine without a database. These drive the execution half against a
mocked CallRouter instead, so the ring logic (advancing, cancelling, falling
through to voicemail) is covered everywhere.

Ring timers are armed by CallRouter, which is mocked here, so a timeout is
simulated by invoking the `on_no_answer` callback the feature passed down rather
than by waiting for a real timer.
"""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from pbx.core.call_router import CallRouter
from pbx.features.find_me_follow_me import MAX_DESTINATIONS, FindMeFollowMe, FMFMState

CALL_ID = "fmfm-call-1"
EXTENSION = "1001"
CALLER = "1002"


class _FakeInvite:
    """Minimal stand-in for the INVITE stored on a leg, carrying a Via branch."""

    def __init__(self, branch: str) -> None:
        self.branch = branch

    def get_header(self, name: str) -> str | None:
        if name == "Via":
            return f"SIP/2.0/UDP 10.0.0.1:5060;branch={self.branch}"
        return None


def _make_response(status: int, branch: str) -> MagicMock:
    """An error response whose top Via carries `branch`."""
    msg = MagicMock()
    msg.status_code = status
    msg.get_header.side_effect = lambda name: (
        f"SIP/2.0/UDP 10.0.0.1:5060;branch={branch}" if name == "Via" else None
    )
    return msg


class _Harness:
    """A handler wired to a mocked PBX, with the dialled legs recorded."""

    def __init__(self, strategy: dict[str, Any], *, initial_ring_time: int | None = 0) -> None:
        """
        Args:
            strategy: The ring strategy `get_ring_strategy` should return.
            initial_ring_time: Value for
                `features.find_me_follow_me.initial_ring_time`. Defaults to 0 so
                the ring-mechanics tests see exactly the destinations they
                configure; pass None to omit the key and exercise the shipped
                default instead.
        """
        pbx = MagicMock()
        pbx.logger = MagicMock()
        pbx.config.get.side_effect = lambda key, default=None: (
            30 if key == "voicemail.no_answer_timeout" else default
        )

        # The real pattern, so internal/external classification matches production.
        pbx.call_router = MagicMock()
        pbx.call_router.EXTERNAL_NUMBER_PATTERN = CallRouter.EXTERNAL_NUMBER_PATTERN

        call = MagicMock()
        call.call_id = CALL_ID
        call.from_extension = CALLER
        call.to_extension = EXTENSION
        call.no_answer_timer = None
        call.invite_transaction = None
        call.callee_invite = None
        call.trunk = None
        pbx.call_manager.active_calls = {CALL_ID: call}
        pbx.call_manager.get_call.side_effect = lambda cid: call if cid == CALL_ID else None

        # Every dial succeeds and, like send_leg_invite, leaves its INVITE (and
        # so its Via branch) on the call. The branch is unique per leg.
        self.legs: list[dict[str, Any]] = []
        self.dial_result = True

        def dial(
            c: Any,
            _cid: str,
            number: str,
            *,
            ring_timeout: int | None = None,
            on_no_answer: Any = None,
            **_kw: Any,
        ) -> bool:
            """Stand in for CallRouter.dial_destination, leaving a leg on the call."""
            if not self.dial_result:
                return False
            branch = f"z9hG4bKleg{len(self.legs)}"
            # Whatever a real dial leaves behind, which _capture_leg lifts off.
            c.callee_invite = _FakeInvite(branch)
            c.callee_addr = ("10.0.0.9", 5060 + len(self.legs))
            c.invite_transaction = MagicMock()
            c.no_answer_timer = MagicMock()
            c.trunk = None
            self.legs.append(
                {
                    "kind": (
                        "trunk" if CallRouter.EXTERNAL_NUMBER_PATTERN.match(number) else "extension"
                    ),
                    "number": number,
                    "ring_timeout": ring_timeout,
                    "on_no_answer": on_no_answer,
                    "branch": branch,
                    "addr": c.callee_addr,
                }
            )
            return True

        pbx.call_router.dial_destination.side_effect = dial

        # The real feature object, with only the config lookup stubbed so each
        # test states its ring plan directly instead of building stored configs.
        feature_config: dict[str, Any] = {"enabled": True}
        if initial_ring_time is not None:
            feature_config["initial_ring_time"] = initial_ring_time
        fmfm = FindMeFollowMe(
            config={"features": {"find_me_follow_me": feature_config}},
            pbx_core=pbx,
        )
        fmfm.get_ring_strategy = MagicMock(return_value=strategy)  # type: ignore[method-assign]
        fmfm.logger = MagicMock()
        pbx.find_me_follow_me = fmfm

        self.pbx = pbx
        self.call = call
        self.handler = fmfm

    def start(self) -> bool:
        """Plan and begin, as CallRouter._dial_to_internal_extension does."""
        plan = self.handler.plan_for(EXTENSION, CALLER, CALL_ID)
        assert plan is not None, "expected a plan"
        return bool(
            self.handler.begin(self.call, plan, f"<sip:{CALLER}@pbx>", f"<sip:{EXTENSION}@pbx>")
        )

    def ring_out(self, leg_index: int = -1) -> None:
        """Fire the ring timer of a dialled leg."""
        self.legs[leg_index]["on_no_answer"]()


def _sequential(*destinations: tuple[str, int], no_answer: str | None = None) -> dict[str, Any]:
    strategy: dict[str, Any] = {
        "strategy": "sequential",
        "destinations": [{"destination": n, "ring_time": t} for n, t in destinations],
        "call_id": CALL_ID,
    }
    if no_answer:
        strategy["no_answer_destination"] = no_answer
    return strategy


def _simultaneous(*destinations: tuple[str, int]) -> dict[str, Any]:
    return {
        "strategy": "simultaneous",
        "destinations": [{"destination": n, "ring_time": t} for n, t in destinations],
        "max_ring_time": max((t for _, t in destinations), default=0),
        "call_id": CALL_ID,
    }


# ===========================================================================
# plan_for
# ===========================================================================


@pytest.mark.unit
class TestPlanFor:
    """Which calls FMFM takes, and what it makes of a questionable config."""

    def test_disabled_feature_returns_no_plan(self) -> None:
        h = _Harness(_sequential(("1003", 20)))
        h.handler.enabled = False
        assert h.handler.plan_for(EXTENSION, CALLER, CALL_ID) is None

    def test_config_only_instance_returns_no_plan(self) -> None:
        """Built without a PBX (the API/config-store usage), it cannot ring anything."""
        h = _Harness(_sequential(("1003", 20)))
        h.handler.pbx_core = None
        assert h.handler.plan_for(EXTENSION, CALLER, CALL_ID) is None

    def test_normal_strategy_returns_no_plan(self) -> None:
        """No config, or a disabled one, so the call routes normally."""
        h = _Harness({"strategy": "normal", "destinations": [EXTENSION]})
        assert h.handler.plan_for(EXTENSION, CALLER, CALL_ID) is None

    def test_extension_rings_first_without_being_configured(self) -> None:
        """
        The expectation the feature is named for: reach me at my desk, then
        chase me. A config of just "my mobile" must still ring the desk.
        """
        h = _Harness(_sequential(("1003", 25)), initial_ring_time=None)
        plan = h.handler.plan_for(EXTENSION, CALLER, CALL_ID)
        assert plan is not None
        assert [d["destination"] for d in plan.destinations] == [EXTENSION, "1003"]
        assert plan.destinations[0]["ring_time"] == 20  # initial_ring_time default

    def test_implicit_first_stop_actually_rings_before_the_next_hop(self) -> None:
        h = _Harness(_sequential(("1003", 25)), initial_ring_time=None)
        h.start()
        assert h.legs[0]["number"] == EXTENSION
        assert h.legs[0]["ring_timeout"] == 20
        h.ring_out()
        assert [leg["number"] for leg in h.legs] == [EXTENSION, "1003"]

    def test_explicit_placement_of_the_extension_wins(self) -> None:
        """Listing it yourself controls where and how long it rings."""
        h = _Harness(_sequential(("1003", 25), (EXTENSION, 45)), initial_ring_time=None)
        plan = h.handler.plan_for(EXTENSION, CALLER, CALL_ID)
        assert plan is not None
        assert [d["destination"] for d in plan.destinations] == ["1003", EXTENSION]
        assert plan.destinations[1]["ring_time"] == 45

    def test_initial_ring_time_zero_skips_the_desk(self) -> None:
        h = _Harness(_sequential(("1003", 25)), initial_ring_time=0)
        plan = h.handler.plan_for(EXTENSION, CALLER, CALL_ID)
        assert plan is not None
        assert [d["destination"] for d in plan.destinations] == ["1003"]

    def test_implicit_stop_does_not_push_past_the_cap(self) -> None:
        h = _Harness(
            _sequential(*[(f"20{i:02d}", 20) for i in range(MAX_DESTINATIONS)]),
            initial_ring_time=None,
        )
        plan = h.handler.plan_for(EXTENSION, CALLER, CALL_ID)
        assert plan is not None
        assert len(plan.destinations) == MAX_DESTINATIONS
        assert plan.destinations[0]["destination"] == EXTENSION

    def test_destination_naming_the_caller_is_dropped(self) -> None:
        h = _Harness(_sequential((CALLER, 20), ("1003", 20)))
        plan = h.handler.plan_for(EXTENSION, CALLER, CALL_ID)
        assert plan is not None
        assert [d["destination"] for d in plan.destinations] == ["1003"]

    def test_config_of_only_bad_destinations_routes_normally(self) -> None:
        """Degrade to ordinary routing rather than trap the caller."""
        h = _Harness(_sequential((CALLER, 20), ("", 20)))
        assert h.handler.plan_for(EXTENSION, CALLER, CALL_ID) is None

    def test_duplicate_destinations_are_collapsed(self) -> None:
        h = _Harness(_sequential(("1003", 20), ("1003", 25), ("1004", 20)))
        plan = h.handler.plan_for(EXTENSION, CALLER, CALL_ID)
        assert plan is not None
        assert [d["destination"] for d in plan.destinations] == ["1003", "1004"]

    def test_ring_times_are_clamped(self) -> None:
        h = _Harness(_sequential(("1003", 1), ("1004", 9999)))
        plan = h.handler.plan_for(EXTENSION, CALLER, CALL_ID)
        assert plan is not None
        assert [d["ring_time"] for d in plan.destinations] == [5, 120]

    def test_destination_list_is_capped(self) -> None:
        h = _Harness(_sequential(*[(f"20{i:02d}", 20) for i in range(MAX_DESTINATIONS + 5)]))
        plan = h.handler.plan_for(EXTENSION, CALLER, CALL_ID)
        assert plan is not None
        assert len(plan.destinations) == MAX_DESTINATIONS

    def test_legacy_no_answer_destination_is_ignored(self) -> None:
        """
        Dropped as a concept: an exhausted list always reaches the dialled
        extension's own mailbox, so a stored row from before that decision must
        not add a surprise extra leg.
        """
        h = _Harness(_sequential(("1003", 20), no_answer="1009"))
        plan = h.handler.plan_for(EXTENSION, CALLER, CALL_ID)
        assert plan is not None
        assert [d["destination"] for d in plan.destinations] == ["1003"]

    def test_simultaneous_config_is_marked_on_the_plan(self) -> None:
        h = _Harness(_simultaneous(("1003", 30), ("1004", 30)))
        plan = h.handler.plan_for(EXTENSION, CALLER, CALL_ID)
        assert plan is not None
        assert plan.simultaneous is True

    def test_desk_joins_a_simultaneous_burst(self) -> None:
        """Not a separate first stage -- everything rings at once, desk included."""
        h = _Harness(_simultaneous(("1003", 30)), initial_ring_time=None)
        plan = h.handler.plan_for(EXTENSION, CALLER, CALL_ID)
        assert plan is not None
        assert [d["destination"] for d in plan.destinations] == [EXTENSION, "1003"]
        assert plan.destinations[0]["ring_time"] == 20


# ===========================================================================
# Ringing
# ===========================================================================


@pytest.mark.unit
class TestSequentialRinging:
    """Destinations are rung in order, each for its own ring time."""

    def test_first_destination_is_dialled_with_its_ring_time(self) -> None:
        h = _Harness(_sequential(("1003", 15), ("1004", 25)))
        assert h.start() is True
        assert len(h.legs) == 1
        assert h.legs[0]["kind"] == "extension"
        assert h.legs[0]["number"] == "1003"
        assert h.legs[0]["ring_timeout"] == 15

    def test_ring_timeout_cancels_the_leg_and_dials_the_next(self) -> None:
        h = _Harness(_sequential(("1003", 15), ("1004", 25)))
        h.start()
        h.ring_out()

        h.pbx.sip_server.cancel_leg.assert_called_once_with(h.call)
        assert [leg["number"] for leg in h.legs] == ["1003", "1004"]
        assert h.legs[1]["ring_timeout"] == 25

    def test_external_destination_goes_out_a_trunk(self) -> None:
        h = _Harness(_sequential(("1003", 15), ("5551234567", 25)))
        h.start()
        h.ring_out()

        assert h.legs[1]["kind"] == "trunk"
        assert h.legs[1]["number"] == "5551234567"

    def test_undialable_destination_is_skipped(self) -> None:
        """A destination that will not dial must not stall the plan."""
        h = _Harness(_sequential(("1003", 15), ("1004", 20), ("1005", 20)))
        h.start()

        # 1004 refuses, 1005 takes it.
        calls: list[str] = []

        def _dial(c: Any, _cid: str, number: str, *a: Any, **kw: Any) -> bool:
            calls.append(number)
            if number == "1004":
                return False
            c.callee_invite = _FakeInvite("z9hG4bKlast")
            h.legs.append({"number": number, "on_no_answer": kw.get("on_no_answer")})
            return True

        h.pbx.call_router.dial_destination.side_effect = _dial
        h.ring_out(0)

        assert calls == ["1004", "1005"]

    def test_answer_stops_the_plan(self) -> None:
        h = _Harness(_sequential(("1003", 15), ("1004", 25)))
        h.start()
        h.handler.on_answered(h.call)

        # A ring timer that fires late must not move a live call on.
        h.ring_out(0)
        assert [leg["number"] for leg in h.legs] == ["1003"]

    def test_a_stale_ring_timer_does_not_double_advance(self) -> None:
        """The leg-1 timer firing after leg 1 already gave up is a no-op."""
        h = _Harness(_sequential(("1003", 15), ("1004", 20), ("1005", 20)))
        h.start()
        h.ring_out(0)  # 1003 -> 1004
        h.ring_out(0)  # the same (now stale) callback again

        assert [leg["number"] for leg in h.legs] == ["1003", "1004"]


@pytest.mark.unit
class TestLegFailures:
    """Busy, DND and declines move the call on instead of reaching the caller."""

    def test_busy_advances_to_the_next_destination(self) -> None:
        h = _Harness(_sequential(("1003", 15), ("1004", 25)))
        h.start()

        handled = h.handler.on_leg_failure(h.call, _make_response(486, h.legs[0]["branch"]))

        assert handled is True
        assert [leg["number"] for leg in h.legs] == ["1003", "1004"]

    def test_decline_advances_to_the_next_destination(self) -> None:
        h = _Harness(_sequential(("1003", 15), ("1004", 25)))
        h.start()

        assert h.handler.on_leg_failure(h.call, _make_response(603, h.legs[0]["branch"])) is True
        assert len(h.legs) == 2

    def test_redirect_advances_the_plan_with_the_configured_ring_time(self) -> None:
        """
        A destination whose phone forwards itself (3xx) is just a destination
        not taking the call. Following the redirect instead would re-target
        with the router's default timeout and abandon the rest of the list.
        """
        h = _Harness(_sequential(("1003", 15), ("1004", 10)))
        h.start()

        handled = h.handler.on_leg_failure(h.call, _make_response(302, h.legs[0]["branch"]))

        assert handled is True
        assert [leg["number"] for leg in h.legs] == ["1003", "1004"]
        assert h.legs[1]["ring_timeout"] == 10, "must use the configured ring time, not a default"

    def test_late_487_from_an_abandoned_leg_is_swallowed(self) -> None:
        """The CANCEL we sent to move on comes back as a 487; it must not skip a destination."""
        h = _Harness(_sequential(("1003", 15), ("1004", 20), ("1005", 20)))
        h.start()
        stale_branch = h.legs[0]["branch"]
        h.ring_out(0)  # 1003 -> 1004
        assert len(h.legs) == 2

        handled = h.handler.on_leg_failure(h.call, _make_response(487, stale_branch))

        assert handled is True, "a stale 487 must not reach the caller"
        assert len(h.legs) == 2, "and must not advance past 1004"

    def test_call_without_a_plan_is_not_claimed(self) -> None:
        """Ordinary calls must fall through to the normal error handling."""
        h = _Harness(_sequential(("1003", 15)))
        other = MagicMock()
        other.call_id = "some-other-call"
        assert h.handler.on_leg_failure(other, _make_response(486, "z9hG4bKx")) is False

    def test_error_after_the_plan_finished_is_swallowed(self) -> None:
        h = _Harness(_sequential(("1003", 15)))
        h.start()
        branch = h.legs[0]["branch"]
        h.handler.on_answered(h.call)

        assert h.handler.on_leg_failure(h.call, _make_response(487, branch)) is True


@pytest.mark.unit
class TestSimultaneousRinging:
    """Every destination rings at once and the first to answer takes the call."""

    def test_all_destinations_ring_at_once_each_on_its_own_time(self) -> None:
        h = _Harness(_simultaneous(("1003", 15), ("1004", 25), ("5551234567", 30)))
        assert h.start() is True

        assert [leg["number"] for leg in h.legs] == ["1003", "1004", "5551234567"]
        assert [leg["ring_timeout"] for leg in h.legs] == [15, 25, 30]
        # Internal and external go out through the one common interface.
        assert [leg["kind"] for leg in h.legs] == ["extension", "extension", "trunk"]

    def test_legs_are_lifted_off_the_call_so_they_do_not_overwrite_each_other(self) -> None:
        """Call has room for one callee; a burst cannot live there."""
        h = _Harness(_simultaneous(("1003", 15), ("1004", 25)))
        h.start()

        state = h.handler.state_for(CALL_ID)
        assert state is not None
        assert sorted(state.legs) == ["1003", "1004"]
        assert len({leg.branch for leg in state.legs.values()}) == 2, "legs must be distinguishable"
        # The call's single slot is left free for whichever leg answers.
        assert h.call.callee_invite is None
        assert h.call.callee_addr is None

    def test_first_answer_wins_and_the_others_are_cancelled(self) -> None:
        h = _Harness(_simultaneous(("1003", 15), ("1004", 25), ("1005", 25)))
        h.start()
        winner = h.legs[1]  # 1004 picks up first

        claimed = h.handler.on_leg_answered(h.call, _make_response(200, winner["branch"]))

        assert claimed is True
        # The winner becomes the call's callee, so everything downstream works.
        assert h.call.callee_addr == winner["addr"]
        assert h.call.callee_invite.branch == winner["branch"]
        # Both losers get a CANCEL, addressed individually.
        cancelled = {
            c.kwargs["addr"] for c in h.pbx.sip_server.cancel_leg.call_args_list if c.kwargs
        }
        assert cancelled == {h.legs[0]["addr"], h.legs[2]["addr"]}

    def test_losing_destinations_are_not_logged_as_missed_calls(self) -> None:
        """
        They lost the race, they did not miss the call. Without this the phones
        that were merely beaten to it all show a missed call.
        """
        h = _Harness(_simultaneous(("1003", 15), ("1004", 25)))
        h.start()

        h.handler.on_leg_answered(h.call, _make_response(200, h.legs[1]["branch"]))

        losers = [c for c in h.pbx.sip_server.cancel_leg.call_args_list if c.kwargs]
        assert losers, "the losing destination was never cancelled"
        assert all(c.kwargs.get("answered_elsewhere") for c in losers)

    def test_a_destination_that_rang_out_is_still_a_missed_call(self) -> None:
        """Nobody took that call, so the phone should log it as normal."""
        h = _Harness(_simultaneous(("1003", 15), ("1004", 25)))
        h.start()

        h.ring_out(0)

        cancelled = [c for c in h.pbx.sip_server.cancel_leg.call_args_list if c.kwargs]
        assert cancelled
        assert not any(c.kwargs.get("answered_elsewhere") for c in cancelled)

    def test_a_leg_ringing_out_leaves_the_others_ringing(self) -> None:
        h = _Harness(_simultaneous(("1003", 15), ("1004", 25)))
        h.start()

        with patch("pbx.features.find_me_follow_me.threading.Timer") as timer_cls:
            h.ring_out(0)  # 1003 gives up

        state = h.handler.state_for(CALL_ID)
        assert state is not None
        assert sorted(state.legs) == ["1004"], "1004 must still be ringing"
        timer_cls.assert_not_called(), "voicemail must wait for the last leg"

    def test_voicemail_only_once_the_last_leg_gives_up(self) -> None:
        h = _Harness(_simultaneous(("1003", 15), ("1004", 25)))
        h.start()
        h.ring_out(0)

        with patch("pbx.features.find_me_follow_me.threading.Timer") as timer_cls:
            h.ring_out(1)

        timer_cls.assert_called_once()
        assert timer_cls.call_args.args[1] is h.pbx.call_router._handle_no_answer

    def test_a_declining_leg_does_not_end_the_call(self) -> None:
        h = _Harness(_simultaneous(("1003", 15), ("1004", 25)))
        h.start()

        handled = h.handler.on_leg_failure(h.call, _make_response(486, h.legs[0]["branch"]))

        assert handled is True, "the caller must not see one destination's busy"
        state = h.handler.state_for(CALL_ID)
        assert state is not None
        assert sorted(state.legs) == ["1004"]

    def test_every_leg_declining_reaches_voicemail(self) -> None:
        h = _Harness(_simultaneous(("1003", 15), ("1004", 25)))
        h.start()
        h.handler.on_leg_failure(h.call, _make_response(486, h.legs[0]["branch"]))

        with patch("pbx.features.find_me_follow_me.threading.Timer") as timer_cls:
            h.handler.on_leg_failure(h.call, _make_response(603, h.legs[1]["branch"]))

        timer_cls.assert_called_once()

    def test_caller_hanging_up_stops_every_ringing_destination(self) -> None:
        """Burst legs are not on the Call, so ordinary teardown cannot see them."""
        h = _Harness(_simultaneous(("1003", 15), ("1004", 25), ("1005", 25)))
        h.start()

        h.handler.on_call_ended(h.call)

        cancelled = {
            c.kwargs["addr"] for c in h.pbx.sip_server.cancel_leg.call_args_list if c.kwargs
        }
        assert cancelled == {leg["addr"] for leg in h.legs}
        assert h.handler.state_for(CALL_ID) is None

    def test_an_undialable_destination_does_not_stop_the_burst(self) -> None:
        h = _Harness(_simultaneous(("1003", 15), ("1004", 25)))

        calls: list[str] = []

        def _dial(c: Any, _cid: str, number: str, **kw: Any) -> bool:
            calls.append(number)
            if number == "1003":
                return False
            c.callee_invite = _FakeInvite("z9hG4bKok")
            c.callee_addr = ("10.0.0.9", 6000)
            c.invite_transaction = MagicMock()
            c.no_answer_timer = MagicMock()
            c.trunk = None
            return True

        h.pbx.call_router.dial_destination.side_effect = _dial
        assert h.start() is True

        assert calls == ["1003", "1004"]
        state = h.handler.state_for(CALL_ID)
        assert state is not None
        assert sorted(state.legs) == ["1004"]

    def test_answer_after_the_burst_resolved_is_not_claimed(self) -> None:
        """A second destination answering the instant we cancelled it."""
        h = _Harness(_simultaneous(("1003", 15), ("1004", 25)))
        h.start()
        h.handler.on_leg_answered(h.call, _make_response(200, h.legs[0]["branch"]))

        late = h.handler.on_leg_answered(h.call, _make_response(200, h.legs[1]["branch"]))

        assert late is False, "the call is already bridged to the first answerer"


@pytest.mark.unit
class TestExhaustion:
    """What happens when nobody picks up anywhere."""

    def test_last_destination_falls_through_to_voicemail(self) -> None:
        h = _Harness(_sequential(("1003", 15)))
        h.start()

        with patch("pbx.features.find_me_follow_me.threading.Timer") as timer_cls:
            h.ring_out()

        # Voicemail is reached the same way an ordinary unanswered call reaches
        # it, dispatched off the SIP thread.
        timer_cls.assert_called_once()
        assert timer_cls.call_args.args[1] is h.pbx.call_router._handle_no_answer
        assert timer_cls.call_args.kwargs["args"] == (CALL_ID,)

    def test_voicemail_uses_the_dialled_extension_not_the_last_destination(self) -> None:
        """Every destination is the same person, so the mailbox is the extension's."""
        h = _Harness(_sequential(("1003", 15), ("5551234567", 20)))
        h.start()
        with patch("pbx.features.find_me_follow_me.threading.Timer"):
            h.ring_out()
            h.ring_out()

        assert h.call.to_extension == EXTENSION

    def test_nothing_dialable_at_all_still_reaches_voicemail(self) -> None:
        """Not a 404 -- an unreachable extension is a voicemail answer."""
        h = _Harness(_sequential(("1003", 15), ("1004", 20)))
        h.dial_result = False

        with patch("pbx.features.find_me_follow_me.threading.Timer") as timer_cls:
            assert h.start() is True

        assert h.legs == []
        timer_cls.assert_called_once()
        assert timer_cls.call_args.args[1] is h.pbx.call_router._handle_no_answer

    def test_abandoning_an_external_leg_releases_its_trunk_channel(self) -> None:
        """Otherwise a trunk leaks a channel for every destination tried."""
        h = _Harness(_sequential(("5551234567", 15), ("1004", 20)))
        h.start()

        trunk = MagicMock()
        h.call.trunk = trunk
        h.ring_out()

        trunk.release_channel.assert_called_once()
        # Cleared, so the later voicemail fall-through is not mistaken for an
        # unanswered outbound trunk call.
        assert h.call.trunk is None


@pytest.mark.unit
class TestAdminListing:
    """What the admin page is allowed to see."""

    def test_disabled_configs_are_still_listed(self) -> None:
        """
        Hiding them makes them unreachable: the row stays in the database, off
        the page, with no way to switch it back on.
        """
        h = _Harness(_sequential(("1003", 20)))
        h.handler.user_configs["1001"] = {"extension": "1001", "enabled": True}
        h.handler.user_configs["1002"] = {"extension": "1002", "enabled": False}

        assert h.handler.list_extensions_with_fmfm() == ["1001", "1002"]

    def test_a_disabled_config_still_does_not_ring(self) -> None:
        """Listing it must not make it active again."""
        h = _Harness({"strategy": "normal", "destinations": [EXTENSION]})
        h.handler.user_configs[EXTENSION] = {"extension": EXTENSION, "enabled": False}

        assert h.handler.plan_for(EXTENSION, CALLER, CALL_ID) is None


@pytest.mark.unit
class TestPlanRegistry:
    """Plans live in the handler, not on the Call."""

    def test_plan_is_registered_and_looked_up_by_call_id(self) -> None:
        h = _Harness(_sequential(("1003", 15)))
        h.start()
        state = h.handler.state_for(CALL_ID)
        assert isinstance(state, FMFMState)
        assert state.extension == EXTENSION
        assert h.handler.state_for("no-such-call") is None

    def test_plan_for_an_ended_call_is_pruned(self) -> None:
        """A caller who hangs up mid-ring leaves nothing behind."""
        h = _Harness(_sequential(("1003", 15)))
        h.start()
        assert h.handler.state_for(CALL_ID) is not None

        # The call is gone, and another FMFM call starts.
        h.pbx.call_manager.active_calls.clear()
        second = MagicMock()
        second.call_id = "fmfm-call-2"
        second.from_extension = CALLER
        second.to_extension = EXTENSION
        second.no_answer_timer = None
        second.invite_transaction = None
        second.callee_invite = None
        second.trunk = None
        h.pbx.call_manager.active_calls["fmfm-call-2"] = second

        plan = h.handler.plan_for(EXTENSION, CALLER, "fmfm-call-2")
        assert plan is not None
        h.handler.begin(second, plan, f"<sip:{CALLER}@pbx>", f"<sip:{EXTENSION}@pbx>")

        assert h.handler.state_for(CALL_ID) is None
        assert h.handler.state_for("fmfm-call-2") is not None
