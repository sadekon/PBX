"""
Tests for QueueCallHandler: entry, parking, abandon, dial-out loop,
overflow routing, adoption, and agent star codes.

SIP/RTP plumbing is stubbed; the queue engine (QueueSystem) is real, as is
the context/state orchestration under test.
"""

import threading
import time
from collections import Counter
from contextlib import nullcontext
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

FAKE_SDP = (
    "v=0\r\n"
    "o=- 0 0 IN IP4 10.0.0.5\r\n"
    "s=-\r\n"
    "c=IN IP4 10.0.0.5\r\n"
    "t=0 0\r\n"
    "m=audio 4000 RTP/AVP 0 101\r\n"
)

CALLER_ADDR = ("10.0.0.5", 5060)


class _FakeMessage:
    body = FAKE_SDP


class _FakeCall:
    """Minimal Call stand-in (attribute bag, like the real Call object)"""

    def __init__(self, call_id, from_ext, to_ext):
        self.call_id = call_id
        self.from_extension = from_ext
        self.to_extension = to_ext
        self.caller_addr = None
        self.callee_addr = None
        self.caller_rtp = None
        self.callee_rtp = None
        self.rtp_ports = None
        self.original_invite = None
        self.routed_to_voicemail = False
        self.no_answer_timer = None
        self.transfer_session_id = None

    def start(self):
        pass

    def connect(self):
        pass


class _FakeCallManager:
    def __init__(self):
        self.calls = {}

    def create_call(self, call_id, from_ext, to_ext):
        call = _FakeCall(call_id, from_ext, to_ext)
        self.calls[call_id] = call
        return call

    def get_call(self, call_id):
        return self.calls.get(call_id)

    def end_call(self, call_id):
        self.calls.pop(call_id, None)

    def get_extension_calls(self, extension):
        return [c for c in self.calls.values() if extension in (c.from_extension, c.to_extension)]


def _make_pbx():
    """Mock PBXCore with a real QueueSystem and fake call manager"""
    from pbx.features.call_queue import QueueSystem

    pbx = MagicMock()
    with patch("pbx.features.call_queue.get_logger", return_value=MagicMock()):
        pbx.queue_system = QueueSystem()
    pbx.call_manager = _FakeCallManager()

    def config_get(key, default=None):
        return default

    pbx.config.get.side_effect = config_get

    pbx.rtp_relay._pool_lock = threading.Lock()
    pbx.rtp_relay.port_pool = [10000, 10002, 10004]
    pbx.rtp_relay.adopt_existing_port.return_value = True
    pbx.rtp_relay.active_relays = {}

    pbx.extension_registry.is_registered.return_value = True
    return pbx


@pytest.fixture
def pbx():
    return _make_pbx()


@pytest.fixture
def handler(pbx):
    from pbx.core.queue_handler import QueueCallHandler

    h = QueueCallHandler(pbx)
    pbx.queue_handler = h
    yield h
    h.shutdown()


def _seed_queue(pbx, number="8001", agents=("1001",), login=True, **attrs):
    queue = pbx.queue_system.create_queue(number, f"Queue {number}")
    for ext in agents:
        pbx.queue_system.add_member(number, ext)
        if login:
            pbx.queue_system.set_agent_login(ext, True)
    for key, value in attrs.items():
        setattr(queue, key, value)
    return queue


def _enter(pbx, handler, call_id="c1", from_ext="2000", *, owned=False):
    """
    Answer a caller into the queue.

    The caller's owner thread is stubbed out by default so a test can drive
    one step at a time; pass owned=True to let the real loop run.
    """
    owner = nullcontext() if owned else patch.object(handler, "_start_owner")
    with patch.object(handler, "_answer_caller", return_value=True), owner:
        return handler.handle_queue_entry(from_ext, "8001", call_id, _FakeMessage(), CALLER_ADDR)


def _ctx(pbx, call_id="c1"):
    """The queue context attached to an admitted caller."""
    return pbx.call_manager.get_call(call_id).queue_ctx


def _use_moh_dir(pbx, tmp_path):
    """Point music_on_hold.directory at tmp_path, leaving other config defaults."""
    pbx.config.get.side_effect = lambda key, default=None: (
        str(tmp_path) if key == "music_on_hold.directory" else default
    )


def _resolve_transfer_with(pbx, callback: str, abort_reason=None):
    """
    Make start_transfer hand back a session that immediately resolves.

    The callback fires from arm_watchdog so the session is already in
    _offer_agent's holder, matching the real watchdog/SIP-thread ordering
    where the abort reason is readable.
    """
    session = MagicMock()
    session.abort_reason = abort_reason

    def _start(_call, _agent_ext, **kwargs):
        session.arm_watchdog.side_effect = lambda _timeout: kwargs[callback]()
        return session

    pbx.transfer_handler.start_transfer.side_effect = _start
    return session


@pytest.mark.unit
class TestQueueDestination:
    def test_enabled_queue(self, pbx, handler):
        _seed_queue(pbx)
        assert handler.is_queue_destination("8001") is True

    def test_disabled_queue(self, pbx, handler):
        _seed_queue(pbx, enabled=False)
        assert handler.is_queue_destination("8001") is False

    def test_unknown(self, pbx, handler):
        assert handler.is_queue_destination("8009") is False


@pytest.mark.unit
class TestQueueEntry:
    def test_answers_parks_and_registers(self, pbx, handler):
        _seed_queue(pbx)
        assert _enter(pbx, handler) is True

        call = pbx.call_manager.get_call("c1")
        assert call.queue_ctx is not None
        assert call.queue_ctx.state.value == "waiting"
        assert call.rtp_ports == (10000, 10001)
        pbx.rtp_relay.adopt_existing_port.assert_called_once_with("c1", 10000, 10001)
        pbx.moh_system.start_moh.assert_called_once()
        assert pbx.moh_system.start_moh.call_args[0][2] == "a"
        pbx.cdr_system.start_record.assert_called_once_with("c1", "2000", "8001")
        assert pbx.queue_system.get_queue_status("8001")["calls_waiting"] == 1

    def test_disabled_queue_falls_through(self, pbx, handler):
        _seed_queue(pbx, enabled=False)
        assert _enter(pbx, handler) is False
        assert pbx.call_manager.get_call("c1") is None

    def test_no_sdp_falls_through(self, pbx, handler):
        _seed_queue(pbx)
        message = _FakeMessage()
        message.body = ""
        assert handler.handle_queue_entry("2000", "8001", "c1", message, CALLER_ADDR) is False

    def test_zero_agents_overflows(self, pbx, handler):
        _seed_queue(pbx, login=False)
        with patch.object(handler, "_overflow_to_voicemail") as overflow:
            assert _enter(pbx, handler) is True
            deadline = time.monotonic() + 2
            while not overflow.called and time.monotonic() < deadline:
                time.sleep(0.02)
            assert overflow.called
        # Never queued
        assert pbx.queue_system.get_queue_status("8001")["calls_waiting"] == 0

    def test_full_queue_overflows(self, pbx, handler):
        _seed_queue(pbx, max_queue_size=1)
        assert _enter(pbx, handler, call_id="c1") is True
        with patch.object(handler, "_overflow_to_voicemail") as overflow:
            assert _enter(pbx, handler, call_id="c2", from_ext="2001") is True
            deadline = time.monotonic() + 2
            while not overflow.called and time.monotonic() < deadline:
                time.sleep(0.02)
            assert overflow.called


@pytest.mark.unit
class TestOnBye:
    def _waiting_ctx(self, pbx, handler, call_id="c1"):
        _seed_queue(pbx)
        _enter(pbx, handler, call_id=call_id)
        return pbx.call_manager.get_call(call_id)

    def test_abandon_while_waiting(self, pbx, handler):
        call = self._waiting_ctx(pbx, handler)
        call.caller_addr = CALLER_ADDR

        assert handler.on_bye(call, CALLER_ADDR) is True
        assert call.queue_ctx is None
        pbx.cdr_system.end_record.assert_called_with("c1", hangup_cause="queue_abandoned")
        pbx.end_call.assert_called_once_with("c1")
        assert pbx.queue_system.get_queue_status("8001")["calls_waiting"] == 0

    def test_absorbs_transferor_bye(self, pbx, handler):
        call = self._waiting_ctx(pbx, handler)
        transferor_addr = ("10.0.0.9", 5060)
        call.queue_ctx.absorbed_addr = tuple(transferor_addr)

        assert handler.on_bye(call, transferor_addr) is True
        # Caller still queued
        assert call.queue_ctx is not None
        assert call.queue_ctx.absorbed_addr is None
        pbx.end_call.assert_not_called()

    def test_falls_through_during_overflow_vm(self, pbx, handler):
        from pbx.core.queue_handler import QueueCallState

        call = self._waiting_ctx(pbx, handler)
        call.caller_addr = CALLER_ADDR
        call.queue_ctx.state = QueueCallState.OVERFLOW_VM

        assert handler.on_bye(call, CALLER_ADDR) is False

    def test_unknown_addr_falls_through(self, pbx, handler):
        call = self._waiting_ctx(pbx, handler)
        call.caller_addr = CALLER_ADDR
        assert handler.on_bye(call, ("172.16.0.1", 5062)) is False


def _two_agent_ctx(pbx, handler, **queue_attrs):
    """A caller waiting in a two-agent queue, with no owner loop running."""
    _seed_queue(pbx, agents=("1001", "1002"), **queue_attrs)
    _enter(pbx, handler)
    return _ctx(pbx)


@pytest.mark.unit
class TestPickAgent:
    def test_claims_agent_and_marks_offering(self, pbx, handler):
        ctx = _two_agent_ctx(pbx, handler)
        queue = pbx.queue_system.get_queue("8001")
        offers = Counter()

        agent_ext = handler._pick_agent(ctx, queue, offers)

        assert agent_ext in ("1001", "1002")
        assert ctx.state.value == "offering"
        assert ctx.current_agent == agent_ext
        assert ctx.attempts == 1
        assert offers[agent_ext] == 1
        # Claimed, so a second caller cannot be offered the same agent.
        with handler._lock:
            assert agent_ext in handler._offering

    def test_none_when_nothing_dialable(self, pbx, handler):
        ctx = _two_agent_ctx(pbx, handler)
        pbx.extension_registry.is_registered.return_value = False

        assert handler._pick_agent(ctx, pbx.queue_system.get_queue("8001"), Counter()) is None
        assert ctx.state.value == "waiting"

    def test_skips_agent_that_spent_its_budget(self, pbx, handler):
        ctx = _two_agent_ctx(pbx, handler)  # max_redials defaults to 0 -> budget 1
        queue = pbx.queue_system.get_queue("8001")

        assert handler._pick_agent(ctx, queue, Counter({"1001": 1})) == "1002"

    def test_redials_same_agent_while_budget_remains(self, pbx, handler):
        ctx = _two_agent_ctx(pbx, handler, max_redials=1)  # budget 2
        queue = pbx.queue_system.get_queue("8001")

        assert handler._pick_agent(ctx, queue, Counter({"1001": 1})) == "1001"

    def test_none_when_caller_no_longer_waiting(self, pbx, handler):
        from pbx.core.queue_handler import QueueCallState

        ctx = _two_agent_ctx(pbx, handler)
        ctx.state = QueueCallState.ABANDONED

        assert handler._pick_agent(ctx, pbx.queue_system.get_queue("8001"), Counter()) is None

    def test_expired_offering_claim_is_released(self, pbx, handler):
        """
        Regression: _offering was a plain set only ever cleared by the path
        that added it, so one missed release excluded that extension from
        every queue until the process restarted -- the reported symptom of
        only one of two active agents ever getting calls.
        """
        ctx = _two_agent_ctx(pbx, handler)
        queue = pbx.queue_system.get_queue("8001")
        with handler._lock:
            handler._offering["1001"] = time.monotonic() - 1  # already expired
            handler._offering["1002"] = time.monotonic() + 300  # still ringing

        assert handler._pick_agent(ctx, queue, Counter()) == "1001"
        with handler._lock:
            assert "1002" in handler._offering  # live claim untouched


@pytest.mark.unit
class TestRetryBudget:
    def test_not_exhausted_while_budget_remains(self, pbx, handler):
        _two_agent_ctx(pbx, handler)
        queue = pbx.queue_system.get_queue("8001")

        assert handler._retries_exhausted(queue, Counter({"1001": 1})) is False

    def test_exhausted_once_every_selectable_agent_is_spent(self, pbx, handler):
        _two_agent_ctx(pbx, handler)
        queue = pbx.queue_system.get_queue("8001")

        assert handler._retries_exhausted(queue, Counter({"1001": 1, "1002": 1})) is True

    def test_all_logged_out_before_any_attempt_defers_to_grace(self, pbx, handler):
        """A caller admitted as the last agent leaves gets the grace window."""
        _two_agent_ctx(pbx, handler)
        queue = pbx.queue_system.get_queue("8001")
        pbx.queue_system.set_agent_login("1001", False)
        pbx.queue_system.set_agent_login("1002", False)

        assert handler._retries_exhausted(queue, Counter()) is False

    def test_agent_going_unselectable_mid_loop_still_counts_as_spent(self, pbx, handler):
        """
        Regression: an agent auto-paused by the very attempt that rang out
        (or logging out mid-loop) left no selectable members, which used to
        report 'not exhausted' and fall through to the no-agent grace --
        adding its whole timeout, or holding to max_wait_time when that
        grace is disabled.
        """
        _two_agent_ctx(pbx, handler)
        queue = pbx.queue_system.get_queue("8001")
        offers = Counter({"1001": 1, "1002": 1})
        pbx.queue_system.set_agent_pause("1001", True, "auto_missed")
        pbx.queue_system.set_agent_login("1002", False)

        assert handler._retries_exhausted(queue, offers) is True

    def test_agent_returning_with_budget_left_is_not_exhausted(self, pbx, handler):
        """One agent spent, the other logs back in unspent: keep trying."""
        _two_agent_ctx(pbx, handler)
        queue = pbx.queue_system.get_queue("8001")

        assert handler._retries_exhausted(queue, Counter({"1001": 1})) is False

    def test_budget_follows_max_redials(self, pbx, handler):
        _two_agent_ctx(pbx, handler, max_redials=2)
        queue = pbx.queue_system.get_queue("8001")

        assert handler._offer_budget(queue) == 3
        assert handler._retries_exhausted(queue, Counter({"1001": 3, "1002": 2})) is False
        assert handler._retries_exhausted(queue, Counter({"1001": 3, "1002": 3})) is True


@pytest.mark.unit
class TestSelectionReport:
    def _report(self, pbx, handler, offers=None):
        return handler._selection_report(
            pbx.queue_system.get_queue("8001"), offers if offers is not None else Counter()
        )

    def test_names_spent_budget(self, pbx, handler):
        _two_agent_ctx(pbx, handler)
        assert "1001: offered 1/1" in self._report(pbx, handler, Counter({"1001": 1}))

    def test_names_logged_out_and_paused(self, pbx, handler):
        _two_agent_ctx(pbx, handler)
        pbx.queue_system.set_agent_login("1001", False)
        pbx.queue_system.set_agent_pause("1002", True, "auto_missed")

        report = self._report(pbx, handler)
        assert "1001: logged out" in report
        assert "1002: paused (auto_missed)" in report

    def test_names_unregistered(self, pbx, handler):
        _two_agent_ctx(pbx, handler)
        pbx.extension_registry.is_registered.return_value = False

        assert "1001: not registered" in self._report(pbx, handler)

    def test_names_busy_call(self, pbx, handler):
        """The gate the queue layer cannot fix: a leaked call record."""
        _two_agent_ctx(pbx, handler)
        pbx.call_manager.create_call("stuck", "2000", "1001")

        assert "1001: busy on stuck" in self._report(pbx, handler)


@pytest.mark.unit
class TestOfferAgent:
    def _offer(self, pbx, handler, ctx, agent_ext="1001"):
        return handler._offer_agent(
            ctx, pbx.queue_system.get_queue("8001"), pbx.call_manager.get_call("c1"), agent_ext
        )

    def test_blind_transfer_with_per_queue_ring_timeout(self, pbx, handler):
        from pbx.core.transfer_session import TransferMode

        ctx = _two_agent_ctx(pbx, handler, ring_timeout=7)
        session = _resolve_transfer_with(pbx, "on_failure", "no_answer")

        assert self._offer(pbx, handler, ctx) == "no_answer"

        _args, kwargs = pbx.transfer_handler.start_transfer.call_args
        assert kwargs["mode"] == TransferMode.BLIND
        assert kwargs["transferor_side"] == "callee"
        session.arm_watchdog.assert_called_once_with(7.0)

    def test_answered_outcome(self, pbx, handler):
        from pbx.core.queue_handler import OFFER_ANSWERED

        ctx = _two_agent_ctx(pbx, handler)
        _resolve_transfer_with(pbx, "on_complete")

        assert self._offer(pbx, handler, ctx) == OFFER_ANSWERED

    def test_unreachable_when_no_session(self, pbx, handler):
        """Agent unregistered between the dialability check and the INVITE."""
        ctx = _two_agent_ctx(pbx, handler)
        pbx.transfer_handler.start_transfer.side_effect = None
        pbx.transfer_handler.start_transfer.return_value = None

        assert self._offer(pbx, handler, ctx) == "unreachable"


@pytest.mark.unit
class TestResolveOffer:
    def _resolve(self, pbx, handler, ctx, agent_ext="1001"):
        return handler._resolve_offer(
            ctx,
            pbx.queue_system.get_queue("8001"),
            pbx.call_manager.get_call("c1"),
            agent_ext,
        )

    def test_answered_records_and_stops(self, pbx, handler):
        ctx = _two_agent_ctx(pbx, handler)
        call = pbx.call_manager.get_call("c1")
        _resolve_transfer_with(pbx, "on_complete")

        assert self._resolve(pbx, handler, ctx, "1002") is True

        assert ctx.state.value == "bridged"
        assert call.queue_ctx is None
        assert pbx.queue_system.get_agent("1002").calls_taken == 1
        assert pbx.queue_system.get_queue_status("8001")["calls_waiting"] == 0

    def test_explicit_reject_pauses_agent(self, pbx, handler):
        """A DND phone refuses the INVITE: pause it instead of redialing."""
        ctx = _two_agent_ctx(pbx, handler)
        _resolve_transfer_with(pbx, "on_failure", "target_rejected")

        assert self._resolve(pbx, handler, ctx, "1001") is False

        agent = pbx.queue_system.get_agent("1001")
        assert agent.paused is True
        assert agent.pause_reason == "auto_rejected"

    def test_ring_out_counts_miss_not_pause(self, pbx, handler):
        """No-answer is not a refusal: count a miss, leave the agent active."""
        ctx = _two_agent_ctx(pbx, handler)
        _resolve_transfer_with(pbx, "on_failure", "no_answer")

        assert self._resolve(pbx, handler, ctx, "1001") is False

        agent = pbx.queue_system.get_agent("1001")
        assert agent.consecutive_misses == 1
        assert agent.paused is False
        assert ctx.state.value == "waiting"

    def test_caller_hangup_abandons(self, pbx, handler):
        ctx = _two_agent_ctx(pbx, handler)
        _resolve_transfer_with(pbx, "on_failure", "transferee_hangup")

        assert self._resolve(pbx, handler, ctx, "1001") is True

        pbx.end_call.assert_called_once_with("c1")
        pbx.cdr_system.end_record.assert_called_with("c1", hangup_cause="queue_abandoned")
        assert ctx.state.value == "abandoned"

    def test_offering_exclusion_released(self, pbx, handler):
        """However an attempt ends, the agent must not stay blacklisted."""
        ctx = _two_agent_ctx(pbx, handler)
        queue = pbx.queue_system.get_queue("8001")
        _resolve_transfer_with(pbx, "on_failure", "no_answer")
        agent_ext = handler._pick_agent(ctx, queue, Counter())

        self._resolve(pbx, handler, ctx, agent_ext)

        with handler._lock:
            assert agent_ext not in handler._offering
        assert ctx.current_agent is None

    def test_auto_pause_webhook_on_missed_threshold(self, pbx, handler):
        from pbx.features.webhooks import WebhookEvent

        ctx = _two_agent_ctx(pbx, handler, auto_pause_misses=1)
        handler._record_miss(ctx, "1001")

        assert pbx.queue_system.get_agent("1001").paused is True
        events = [c[0][0] for c in pbx.webhook_system.trigger_event.call_args_list]
        assert WebhookEvent.QUEUE_AGENT_PAUSED in events


def _age(ctx, seconds):
    """Backdate a caller's enqueue time so it looks like it has waited."""
    from datetime import UTC, datetime, timedelta

    ctx.enqueue_time = datetime.now(tz=UTC) - timedelta(seconds=seconds)


@pytest.mark.unit
class TestCallerLoop:
    """
    The owner loop is the only driver now, so these run it directly and let
    it return -- no sweep ticks, no spawned workers, no polling.
    """

    def test_max_wait_overflows(self, pbx, handler):
        _seed_queue(pbx, max_wait_time=10)
        _enter(pbx, handler)
        ctx = _ctx(pbx)
        _age(ctx, 60)

        with patch.object(handler, "_handle_overflow") as overflow:
            handler._caller_loop(ctx)

        overflow.assert_called_once_with(ctx)

    def test_max_wait_never_interrupts_a_ringing_agent(self, pbx, handler):
        """
        Max wait passing mid-offer must not cut the agent off: the loop is
        blocked in the offer and only overflows once it resolves. (Replaces
        the old OVERFLOW_PENDING latch, which existed to get this ordering
        when several threads could act on the caller at once.)
        """
        _seed_queue(pbx, max_wait_time=10)
        _enter(pbx, handler)
        ctx = _ctx(pbx)
        order = []

        def _offer(*_args, **_kwargs):
            _age(ctx, 60)  # max wait passes while the agent is ringing
            order.append("offer-resolved")
            return "no_answer"

        with (
            patch.object(handler, "_offer_agent", side_effect=_offer),
            patch.object(
                handler, "_handle_overflow", side_effect=lambda _c: order.append("overflow")
            ),
        ):
            handler._caller_loop(ctx)

        assert order == ["offer-resolved", "overflow"]

    def test_no_agents_overflows_after_grace(self, pbx, handler):
        """All agents gone mid-wait (e.g. DND auto-pause + logged-out peer):
        overflow to voicemail instead of holding for the full max_wait."""
        _seed_queue(pbx, max_wait_time=300)
        _enter(pbx, handler)
        ctx = _ctx(pbx)
        # Every member becomes unselectable while the caller waits.
        pbx.queue_system.set_agent_login("1001", False)
        pbx.config.get.side_effect = lambda key, default=None: (
            0.01 if key == "queue_no_agent_timeout" else default
        )

        with patch.object(handler, "_handle_overflow") as overflow:
            handler._caller_loop(ctx)

        overflow.assert_called_once_with(ctx)

    def test_agent_available_never_overflows_for_no_agents(self, pbx, handler):
        """A live agent keeps the no-agent grace from ever starting."""
        _seed_queue(pbx, max_wait_time=300)
        _enter(pbx, handler)
        ctx = _ctx(pbx)

        queue = pbx.queue_system.get_queue("8001")
        assert handler._overflow_reason(ctx, queue, None) is None

    def test_overflows_once_every_agent_has_had_its_attempts(self, pbx, handler):
        """
        Each agent is offered 1 + max_redials times for one caller, then the
        caller overflows immediately rather than holding to max_wait_time.
        """
        _seed_queue(pbx, agents=("1001", "1002"), max_wait_time=300, max_redials=1)
        _enter(pbx, handler)
        ctx = _ctx(pbx)
        offered = []

        def _offer(_ctx, _queue, _call, agent_ext):
            offered.append(agent_ext)
            return "no_answer"

        with (
            patch.object(handler, "_offer_agent", side_effect=_offer),
            patch.object(handler, "_handle_overflow") as overflow,
        ):
            handler._caller_loop(ctx)

        # budget = 1 + max_redials = 2 attempts each, across two agents
        assert sorted(offered) == ["1001", "1001", "1002", "1002"]
        overflow.assert_called_once_with(ctx)

    def test_overflows_when_final_attempt_auto_pauses_the_last_agent(self, pbx, handler):
        """
        Regression: the last redial rings out, _record_miss trips auto-pause,
        and the queue is left with nobody selectable. The loop must still see
        the attempts as spent and overflow, rather than fall through to the
        no-agent grace -- which is disabled here, so the old behaviour held
        the caller until max_wait_time.
        """
        _seed_queue(pbx, agents=("1001",), max_wait_time=300, max_redials=1, auto_pause_misses=2)
        pbx.config.get.side_effect = lambda key, default=None: (
            0 if key == "queue_no_agent_timeout" else default
        )
        _enter(pbx, handler)
        ctx = _ctx(pbx)
        offered = []

        def _offer(_ctx, _queue, _call, agent_ext):
            offered.append(agent_ext)
            return "no_answer"

        with (
            patch.object(handler, "_offer_agent", side_effect=_offer),
            patch.object(handler, "_handle_overflow") as overflow,
        ):
            handler._caller_loop(ctx)

        assert offered == ["1001", "1001"]  # budget spent
        assert pbx.queue_system.get_agent("1001").paused is True  # auto-paused by the 2nd miss
        overflow.assert_called_once_with(ctx)

    def test_each_agent_offered_once_by_default(self, pbx, handler):
        _seed_queue(pbx, agents=("1001", "1002"), max_wait_time=300)
        _enter(pbx, handler)
        ctx = _ctx(pbx)
        offered = []

        def _offer(_ctx, _queue, _call, agent_ext):
            offered.append(agent_ext)
            return "no_answer"

        with (
            patch.object(handler, "_offer_agent", side_effect=_offer),
            patch.object(handler, "_handle_overflow"),
        ):
            handler._caller_loop(ctx)

        assert sorted(offered) == ["1001", "1002"]

    def test_dead_call_is_released(self, pbx, handler):
        _seed_queue(pbx)
        _enter(pbx, handler)
        ctx = _ctx(pbx)
        pbx.call_manager.end_call("c1")

        handler._caller_loop(ctx)

        assert pbx.queue_system.get_queue_status("8001")["calls_waiting"] == 0
        with handler._lock:
            assert "c1" not in handler._contexts

    def test_stops_once_caller_is_terminal(self, pbx, handler):
        from pbx.core.queue_handler import QueueCallState

        _seed_queue(pbx)
        _enter(pbx, handler)
        ctx = _ctx(pbx)
        ctx.state = QueueCallState.ABANDONED

        with patch.object(handler, "_offer_agent") as offer:
            handler._caller_loop(ctx)

        assert not offer.called

    def test_announcement_completes_before_any_offer(self, pbx, handler):
        """
        Regression: announcements used to run on their own thread, spawned
        from the same sweep tick as an offer. The offer flipped state to
        OFFERING mid-TTS and the prompt's barge-out check clipped it after
        about one packet. One owner thread makes the ordering structural.
        """
        from pbx.core.queue_handler import OFFER_ANSWERED

        _seed_queue(pbx, announcement_enabled=True, announcement_interval=0)
        _enter(pbx, handler)
        ctx = _ctx(pbx)
        order = []

        def _announce(_ctx, queue):
            order.append("announce-start")
            time.sleep(0.05)
            order.append("announce-end")
            queue.announcement_enabled = False  # exactly one, then move on

        def _offer(*_args, **_kwargs):
            order.append("offer")
            return OFFER_ANSWERED  # terminal, so the loop stops here

        with (
            patch.object(handler, "_play_announcement", side_effect=_announce),
            patch.object(handler, "_offer_agent", side_effect=_offer),
        ):
            handler._caller_loop(ctx)

        assert order == ["announce-start", "announce-end", "offer"]

    def test_announcement_skipped_when_disabled(self, pbx, handler):
        from pbx.core.queue_handler import OFFER_ANSWERED

        _seed_queue(pbx)  # announcement_enabled defaults to False
        _enter(pbx, handler)
        ctx = _ctx(pbx)

        with (
            patch.object(handler, "_play_announcement") as announce,
            patch.object(handler, "_offer_agent", return_value=OFFER_ANSWERED),
        ):
            handler._caller_loop(ctx)

        assert not announce.called

    def test_owner_thread_registered_per_caller(self, pbx, handler):
        _seed_queue(pbx)
        release = threading.Event()

        # _owners is populated before the thread starts, so this is not racy.
        with patch.object(handler, "_caller_loop", side_effect=lambda _ctx: release.wait(2)):
            _enter(pbx, handler, owned=True)
            with handler._lock:
                assert list(handler._owners) == ["c1"]
            release.set()


@pytest.mark.unit
class TestOverflowAction:
    def test_handle_overflow_defaults_to_voicemail(self, pbx, handler):
        _seed_queue(pbx)  # overflow_action defaults to "voicemail"
        with patch.object(handler, "_answer_caller", return_value=True):
            handler.handle_queue_entry("2000", "8001", "c1", _FakeMessage(), CALLER_ADDR)
        ctx = pbx.call_manager.get_call("c1").queue_ctx

        with (
            patch.object(handler, "_overflow_to_voicemail") as voicemail,
            patch.object(handler, "_overflow_drop") as drop,
        ):
            handler._handle_overflow(ctx)

        voicemail.assert_called_once_with(ctx)
        assert not drop.called

    def test_handle_overflow_dispatches_to_drop(self, pbx, handler):
        _seed_queue(pbx, overflow_action="drop")
        with patch.object(handler, "_answer_caller", return_value=True):
            handler.handle_queue_entry("2000", "8001", "c1", _FakeMessage(), CALLER_ADDR)
        ctx = pbx.call_manager.get_call("c1").queue_ctx

        with (
            patch.object(handler, "_overflow_to_voicemail") as voicemail,
            patch.object(handler, "_overflow_drop") as drop,
        ):
            handler._handle_overflow(ctx)

        drop.assert_called_once_with(ctx)
        assert not voicemail.called

    def test_overflow_drop_ends_call_without_recording(self, pbx, handler):
        from pbx.core.queue_handler import QueueCallState

        _seed_queue(pbx, overflow_action="drop")
        with patch.object(handler, "_answer_caller", return_value=True):
            handler.handle_queue_entry("2000", "8001", "c1", _FakeMessage(), CALLER_ADDR)
        ctx = pbx.call_manager.get_call("c1").queue_ctx
        call = pbx.call_manager.get_call("c1")

        handler._overflow_drop(ctx)

        assert ctx.state == QueueCallState.DONE
        pbx.moh_system.stop_moh.assert_called_once_with("c1")
        pbx.cdr_system.end_record.assert_called_once_with("c1", hangup_cause="queue_overflow_drop")
        pbx.voicemail_handler._send_bye_to_caller.assert_called_once_with(call, "c1")
        assert not pbx.voicemail_handler.record_into_mailbox.called
        assert call.queue_ctx is None
        with handler._lock:
            assert "c1" not in handler._contexts
        assert pbx.queue_system.get_queue_status("8001")["calls_waiting"] == 0


@pytest.mark.unit
class TestHoldAnnouncements:
    def test_resolve_prefers_announcement_file(self, pbx, handler, tmp_path):
        (tmp_path / "announcements").mkdir()
        prompt = tmp_path / "announcements" / "sales.wav"
        prompt.write_bytes(b"RIFF....WAVEfmt ")
        _use_moh_dir(pbx, tmp_path)
        queue = _seed_queue(pbx, announcement_file="sales.wav")

        path, is_temp = handler._resolve_announcement_audio(queue, position=1)

        assert path == prompt
        assert is_temp is False

    def test_resolve_missing_file_falls_through(self, pbx, handler, tmp_path):
        _use_moh_dir(pbx, tmp_path)
        queue = _seed_queue(pbx, announcement_file="missing.wav")

        path, is_temp = handler._resolve_announcement_audio(queue, position=1)

        assert path is None
        assert is_temp is False
        assert pbx.logger.warning.called

    def test_resolve_uses_tts_with_position(self, pbx, handler, tmp_path):
        queue = _seed_queue(pbx, announcement_text="Please hold", announcement_position=True)

        with patch("pbx.utils.audio.generate_tts_audio", return_value=b"WAVDATA") as tts:
            path, is_temp = handler._resolve_announcement_audio(queue, position=3)

        assert path is not None
        assert is_temp is True
        assert path.read_bytes() == b"WAVDATA"
        spoken_text = tts.call_args[0][0]
        assert "Please hold" in spoken_text
        assert "caller number 3" in spoken_text
        path.unlink()

    def test_resolve_returns_none_when_tts_unavailable(self, pbx, handler):
        queue = _seed_queue(pbx)

        with patch("pbx.utils.audio.generate_tts_audio", return_value=None):
            path, is_temp = handler._resolve_announcement_audio(queue, position=1)

        assert path is None
        assert is_temp is False

    def test_play_announcement_interjects_with_prompt(self, pbx, handler, tmp_path):
        (tmp_path / "announcements").mkdir()
        prompt = tmp_path / "announcements" / "sales.wav"
        prompt.write_bytes(b"RIFF....WAVEfmt ")
        _use_moh_dir(pbx, tmp_path)
        queue = _seed_queue(pbx, announcement_enabled=True, announcement_file="sales.wav")
        _enter(pbx, handler)
        ctx = _ctx(pbx)

        handler._play_announcement(ctx, queue)

        pbx.moh_system.interject.assert_called_once()
        args, kwargs = pbx.moh_system.interject.call_args
        assert args[0] == "c1"
        assert args[1] == ctx.held_side
        assert args[2] == prompt
        assert "interrupt_check" in kwargs

    def test_play_announcement_skips_when_not_waiting(self, pbx, handler):
        from pbx.core.queue_handler import QueueCallState

        queue = _seed_queue(pbx, announcement_enabled=True, announcement_file="sales.wav")
        _enter(pbx, handler)
        ctx = _ctx(pbx)
        ctx.state = QueueCallState.OFFERING

        handler._play_announcement(ctx, queue)

        assert not pbx.moh_system.interject.called


@pytest.mark.unit
class TestAdoption:
    def test_adopt_parked_caller(self, pbx, handler):
        _seed_queue(pbx)
        call = pbx.call_manager.create_call("aa1", "2000", "0")
        call.caller_addr = CALLER_ADDR

        handler.adopt_parked_caller("aa1", call, "8001")

        assert call.queue_ctx is not None
        assert call.queue_ctx.shape.value == "answered_by_queue"
        assert call.queue_ctx.transferee_side == "caller"
        # AA already runs MOH; the handler must not restart it
        pbx.moh_system.start_moh.assert_not_called()
        assert pbx.queue_system.get_queue_status("8001")["calls_waiting"] == 1

    def test_adopt_transfer_detaches_transferor(self, pbx, handler):
        _seed_queue(pbx)
        call = pbx.call_manager.create_call("t1", "2000", "1005")
        transferor_addr = ("10.0.0.9", 5060)
        call.caller_addr = CALLER_ADDR  # transferee (caller side)
        call.callee_addr = transferor_addr  # transferor REFERs and departs

        refer_dialog = MagicMock()
        handler.adopt_transfer(call, "8001", transferor_side="callee", refer_dialog=refer_dialog)

        ctx = call.queue_ctx
        assert ctx.shape.value == "adopted_from_transfer"
        assert ctx.transferee_side == "caller"
        assert ctx.absorbed_addr == tuple(transferor_addr)
        assert call.callee_addr is None
        pbx.sip_server.send_transfer_notify.assert_called_once()
        assert "200 OK" in pbx.sip_server.send_transfer_notify.call_args[0][1]
        # Deterministic MOH restart toward the transferee
        pbx.moh_system.stop_moh.assert_called_with("t1")


@pytest.mark.unit
class TestStarCodes:
    def test_login_star_code(self, pbx, handler):
        _seed_queue(pbx, login=False)
        with patch.object(handler, "_answer_caller", return_value=True):
            handled = handler.handle_agent_star_code(
                "1001", "*61", "s1", _FakeMessage(), CALLER_ADDR
            )
        assert handled is True
        agent = pbx.queue_system.get_agent("1001")
        assert agent.logged_in is True

    def test_logout_star_code(self, pbx, handler):
        _seed_queue(pbx)
        with patch.object(handler, "_answer_caller", return_value=True):
            handler.handle_agent_star_code("1001", "*62", "s1", _FakeMessage(), CALLER_ADDR)
        assert pbx.queue_system.get_agent("1001").logged_in is False

    def test_non_member_gets_404(self, pbx, handler):
        _seed_queue(pbx)
        with patch("pbx.sip.message.SIPMessageBuilder") as builder:
            builder.build_response.return_value = MagicMock()
            handled = handler.handle_agent_star_code(
                "3000", "*61", "s1", _FakeMessage(), CALLER_ADDR
            )
        assert handled is True
        assert builder.build_response.call_args[0][0] == 404
        assert pbx.queue_system.get_agent("3000") is None

    def test_state_persists_even_without_media(self, pbx, handler):
        _seed_queue(pbx, login=False)
        message = _FakeMessage()
        message.body = ""
        with patch("pbx.sip.message.SIPMessageBuilder") as builder:
            builder.build_response.return_value = MagicMock()
            handler.handle_agent_star_code("1001", "*61", "s1", message, CALLER_ADDR)
        assert pbx.queue_system.get_agent("1001").logged_in is True

    def test_star_code_confirm_speaks_tts_message(self, pbx, handler):
        call = _FakeCall("s1", "1001", "*61")
        call.caller_rtp = {"address": "10.0.0.5", "port": 4000}

        class FakePlayer:
            instances: ClassVar[list] = []

            def __init__(self, **_kwargs):
                self.played_files = []
                self.beeps = 0
                self.stopped = False
                self.__class__.instances.append(self)

            def start(self):
                return True

            def play_file(self, path):
                self.played_files.append(path)
                return True

            def play_beep(self, **_kwargs):
                self.beeps += 1

            def stop(self):
                self.stopped = True

        with (
            patch("pbx.rtp.handler.RTPPlayer", FakePlayer),
            patch("pbx.utils.audio.generate_tts_audio", return_value=b"WAVDATA"),
            patch.object(pbx.call_manager, "end_call") as end_call,
        ):
            handler._star_code_confirm(call, "s1", 10000, True)

        assert len(FakePlayer.instances) == 1
        player = FakePlayer.instances[0]
        assert len(player.played_files) == 1
        assert player.beeps == 0
        assert player.stopped is True
        pbx.voicemail_handler._send_bye_to_caller.assert_called_once()
        end_call.assert_called_once_with("s1")

    def test_star_code_confirm_falls_back_to_beeps_without_tts(self, pbx, handler):
        call = _FakeCall("s1", "1001", "*62")
        call.caller_rtp = {"address": "10.0.0.5", "port": 4000}

        class FakePlayer:
            instances: ClassVar[list] = []

            def __init__(self, **_kwargs):
                self.played_files = []
                self.beeps = 0
                self.stopped = False
                self.__class__.instances.append(self)

            def start(self):
                return True

            def play_file(self, path):
                self.played_files.append(path)
                return True

            def play_beep(self, **_kwargs):
                self.beeps += 1

            def stop(self):
                self.stopped = True

        with (
            patch("pbx.rtp.handler.RTPPlayer", FakePlayer),
            patch("pbx.utils.audio.generate_tts_audio", return_value=None),
        ):
            handler._star_code_confirm(call, "s1", 10000, False)

        player = FakePlayer.instances[0]
        assert player.played_files == []
        assert player.beeps == 2  # logout = 2 beeps


@pytest.mark.unit
class TestRouterDivert:
    """The routing hooks divert queue destinations before extension logic."""

    def test_dial_to_internal_extension_diverts(self):
        from pbx.core.call_router import CallRouter

        pbx = MagicMock()
        pbx.queue_handler.is_queue_destination.return_value = True
        pbx.queue_handler.handle_queue_entry.return_value = True
        router = CallRouter(pbx)

        result = router._dial_to_internal_extension(
            "2000",
            "8001",
            "<sip:2000@pbx>",
            "<sip:8001@pbx>",
            "c1",
            MagicMock(),
            CALLER_ADDR,
        )

        assert result is True
        pbx.queue_handler.handle_queue_entry.assert_called_once()
        # Never reached extension resolution
        pbx.extension_registry.get.assert_not_called()
