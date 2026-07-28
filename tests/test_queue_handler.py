"""
Tests for QueueCallHandler: entry, parking, abandon, dial-out loop,
overflow routing, adoption, and agent star codes.

SIP/RTP plumbing is stubbed; the queue engine (QueueSystem) is real, as is
the context/state orchestration under test.
"""

import threading
import time
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
    def _enter(self, pbx, handler, call_id="c1", from_ext="2000"):
        with patch.object(handler, "_answer_caller", return_value=True):
            return handler.handle_queue_entry(
                from_ext, "8001", call_id, _FakeMessage(), CALLER_ADDR
            )

    def test_answers_parks_and_registers(self, pbx, handler):
        _seed_queue(pbx)
        assert self._enter(pbx, handler) is True

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
        assert self._enter(pbx, handler) is False
        assert pbx.call_manager.get_call("c1") is None

    def test_no_sdp_falls_through(self, pbx, handler):
        _seed_queue(pbx)
        message = _FakeMessage()
        message.body = ""
        assert handler.handle_queue_entry("2000", "8001", "c1", message, CALLER_ADDR) is False

    def test_zero_agents_overflows(self, pbx, handler):
        _seed_queue(pbx, login=False)
        with patch.object(handler, "_overflow_to_voicemail") as overflow:
            assert self._enter(pbx, handler) is True
            deadline = time.monotonic() + 2
            while not overflow.called and time.monotonic() < deadline:
                time.sleep(0.02)
            assert overflow.called
        # Never queued
        assert pbx.queue_system.get_queue_status("8001")["calls_waiting"] == 0

    def test_full_queue_overflows(self, pbx, handler):
        _seed_queue(pbx, max_queue_size=1)
        assert self._enter(pbx, handler, call_id="c1") is True
        with patch.object(handler, "_overflow_to_voicemail") as overflow:
            assert self._enter(pbx, handler, call_id="c2", from_ext="2001") is True
            deadline = time.monotonic() + 2
            while not overflow.called and time.monotonic() < deadline:
                time.sleep(0.02)
            assert overflow.called


@pytest.mark.unit
class TestOnBye:
    def _waiting_ctx(self, pbx, handler, call_id="c1"):
        _seed_queue(pbx)
        with patch.object(handler, "_answer_caller", return_value=True):
            handler.handle_queue_entry("2000", "8001", call_id, _FakeMessage(), CALLER_ADDR)
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


@pytest.mark.unit
class TestOfferLoop:
    def _ctx(self, pbx, handler, **queue_attrs):
        _seed_queue(pbx, agents=("1001", "1002"), **queue_attrs)
        with patch.object(handler, "_answer_caller", return_value=True):
            handler.handle_queue_entry("2000", "8001", "c1", _FakeMessage(), CALLER_ADDR)
        return pbx.call_manager.get_call("c1").queue_ctx

    def test_offer_starts_blind_transfer_with_ring_timeout(self, pbx, handler):
        from pbx.core.transfer_session import TransferMode

        ctx = self._ctx(pbx, handler, ring_timeout=7)
        session = MagicMock()
        pbx.transfer_handler.start_transfer.return_value = session

        handler._offer_worker(ctx)

        assert ctx.state.value == "offering"
        args, kwargs = pbx.transfer_handler.start_transfer.call_args
        assert args[1] in ("1001", "1002")
        assert kwargs["mode"] == TransferMode.BLIND
        assert kwargs["transferor_side"] == "callee"
        session.arm_watchdog.assert_called_once_with(7.0)

    def test_synchronous_failure_tries_next_agent(self, pbx, handler):
        ctx = self._ctx(pbx, handler)
        session = MagicMock()
        pbx.transfer_handler.start_transfer.side_effect = [None, session]

        handler._offer_worker(ctx)

        assert pbx.transfer_handler.start_transfer.call_count == 2
        first = pbx.transfer_handler.start_transfer.call_args_list[0][0][1]
        second = pbx.transfer_handler.start_transfer.call_args_list[1][0][1]
        assert first != second
        # First agent's miss was recorded
        assert pbx.queue_system.get_agent(first).consecutive_misses == 1

    def test_explicit_reject_pauses_agent(self, pbx, handler):
        """A DND phone refuses the INVITE: pause it instead of redialing."""
        ctx = self._ctx(pbx, handler)
        session = MagicMock()
        session.abort_reason = "target_rejected"

        with patch.object(handler, "_offer_worker"):
            handler._on_offer_failure(ctx, "1001", [session])
            deadline = time.monotonic() + 2
            while not pbx.queue_system.get_agent("1001").paused:
                if time.monotonic() > deadline:
                    break
                time.sleep(0.02)

        agent = pbx.queue_system.get_agent("1001")
        assert agent.paused is True
        assert agent.pause_reason == "auto_rejected"
        # Paused, not merely missed: the miss counter is not what removed them.
        assert "1001" in ctx.tried_agents

    def test_ring_out_counts_miss_not_pause(self, pbx, handler):
        """No-answer is not a refusal: count a miss, leave the agent active."""
        ctx = self._ctx(pbx, handler)
        session = MagicMock()
        session.abort_reason = "no_answer"

        with patch.object(handler, "_offer_worker"):
            handler._on_offer_failure(ctx, "1001", [session])
            deadline = time.monotonic() + 2
            while pbx.queue_system.get_agent("1001").consecutive_misses == 0:
                if time.monotonic() > deadline:
                    break
                time.sleep(0.02)

        agent = pbx.queue_system.get_agent("1001")
        assert agent.consecutive_misses == 1
        assert agent.paused is False

    def test_no_dialable_agents_stays_waiting(self, pbx, handler):
        ctx = self._ctx(pbx, handler)
        pbx.extension_registry.is_registered.return_value = False

        handler._offer_worker(ctx)

        assert ctx.state.value == "waiting"
        pbx.transfer_handler.start_transfer.assert_not_called()
        assert ctx.retry_at > 0

    def test_offer_failure_miss_then_retry(self, pbx, handler):
        ctx = self._ctx(pbx, handler)
        session = MagicMock()
        session.abort_reason = "target_timeout"

        with patch.object(handler, "_offer_worker") as next_offer:
            from pbx.core.queue_handler import QueueCallState

            ctx.state = QueueCallState.OFFERING
            handler._on_offer_failure(ctx, "1001", [session])
            deadline = time.monotonic() + 2
            while not next_offer.called and time.monotonic() < deadline:
                time.sleep(0.02)
            assert next_offer.called

        assert ctx.state.value == "waiting"
        assert "1001" in ctx.tried_agents
        assert pbx.queue_system.get_agent("1001").consecutive_misses == 1

    def test_offer_failure_caller_hangup_abandons(self, pbx, handler):
        ctx = self._ctx(pbx, handler)
        session = MagicMock()
        session.abort_reason = "transferee_hangup"

        handler._on_offer_failure(ctx, "1001", [session])
        deadline = time.monotonic() + 2
        while not pbx.end_call.called and time.monotonic() < deadline:
            time.sleep(0.02)

        pbx.end_call.assert_called_once_with("c1")
        pbx.cdr_system.end_record.assert_called_with("c1", hangup_cause="queue_abandoned")
        assert ctx.state.value == "abandoned"

    def test_offer_failure_overflow_pending_goes_to_vm(self, pbx, handler):
        from pbx.core.queue_handler import QueueCallState

        ctx = self._ctx(pbx, handler)
        ctx.state = QueueCallState.OVERFLOW_PENDING
        session = MagicMock()
        session.abort_reason = "no_answer"

        with patch.object(handler, "_overflow_to_voicemail") as overflow:
            handler._on_offer_failure(ctx, "1001", [session])
            deadline = time.monotonic() + 2
            while not overflow.called and time.monotonic() < deadline:
                time.sleep(0.02)
            overflow.assert_called_once_with(ctx)

    def test_offer_complete_records_answer(self, pbx, handler):
        ctx = self._ctx(pbx, handler)
        call = pbx.call_manager.get_call("c1")

        handler._on_offer_complete(ctx, "1002")

        assert ctx.state.value == "bridged"
        assert call.queue_ctx is None
        agent = pbx.queue_system.get_agent("1002")
        assert agent.calls_taken == 1
        assert pbx.queue_system.get_queue_status("8001")["calls_waiting"] == 0

    def test_auto_pause_webhook_on_missed_threshold(self, pbx, handler):
        from pbx.features.webhooks import WebhookEvent

        ctx = self._ctx(pbx, handler, auto_pause_misses=1)
        handler._record_miss(ctx, "1001")

        assert pbx.queue_system.get_agent("1001").paused is True
        events = [c[0][0] for c in pbx.webhook_system.trigger_event.call_args_list]
        assert WebhookEvent.QUEUE_AGENT_PAUSED in events


@pytest.mark.unit
class TestSweep:
    def test_max_wait_triggers_overflow(self, pbx, handler):
        from datetime import UTC, datetime, timedelta

        _seed_queue(pbx, max_wait_time=10)
        with patch.object(handler, "_answer_caller", return_value=True):
            handler.handle_queue_entry("2000", "8001", "c1", _FakeMessage(), CALLER_ADDR)
        ctx = pbx.call_manager.get_call("c1").queue_ctx
        ctx.enqueue_time = datetime.now(tz=UTC) - timedelta(seconds=60)

        with patch.object(handler, "_overflow_to_voicemail") as overflow:
            handler._sweep_once()
            deadline = time.monotonic() + 2
            while not overflow.called and time.monotonic() < deadline:
                time.sleep(0.02)
            overflow.assert_called_once_with(ctx)

    def test_max_wait_mid_offer_latches_pending(self, pbx, handler):
        from datetime import UTC, datetime, timedelta

        from pbx.core.queue_handler import QueueCallState

        _seed_queue(pbx, max_wait_time=10)
        with patch.object(handler, "_answer_caller", return_value=True):
            handler.handle_queue_entry("2000", "8001", "c1", _FakeMessage(), CALLER_ADDR)
        ctx = pbx.call_manager.get_call("c1").queue_ctx
        ctx.state = QueueCallState.OFFERING
        ctx.enqueue_time = datetime.now(tz=UTC) - timedelta(seconds=60)

        handler._sweep_once()

        assert ctx.state == QueueCallState.OVERFLOW_PENDING

    def test_no_agents_mid_wait_overflows_after_grace(self, pbx, handler):
        """All agents gone mid-wait (e.g. DND auto-pause + logged-out peer):
        overflow to voicemail instead of holding for the full max_wait."""
        _seed_queue(pbx, max_wait_time=300)
        with patch.object(handler, "_answer_caller", return_value=True):
            handler.handle_queue_entry("2000", "8001", "c1", _FakeMessage(), CALLER_ADDR)
        ctx = pbx.call_manager.get_call("c1").queue_ctx

        # Every member becomes unselectable while the caller waits.
        pbx.queue_system.set_agent_login("1001", False)

        with patch.object(handler, "_overflow_to_voicemail") as overflow:
            handler._sweep_once()
            assert not overflow.called  # grace period not yet elapsed

            ctx.no_agents_since -= handler._no_agent_timeout() + 1
            handler._sweep_once()
            deadline = time.monotonic() + 2
            while not overflow.called and time.monotonic() < deadline:
                time.sleep(0.02)
            overflow.assert_called_once_with(ctx)

    def test_agent_available_never_overflows_for_no_agents(self, pbx, handler):
        """A live agent resets the no-agent timer, so waiting continues."""
        _seed_queue(pbx, max_wait_time=300)
        with patch.object(handler, "_answer_caller", return_value=True):
            handler.handle_queue_entry("2000", "8001", "c1", _FakeMessage(), CALLER_ADDR)
        ctx = pbx.call_manager.get_call("c1").queue_ctx

        with patch.object(handler, "_overflow_to_voicemail") as overflow:
            handler._sweep_once()
            handler._sweep_once()
            assert ctx.no_agents_since is None
            assert not overflow.called

    def test_dead_call_cleanup(self, pbx, handler):
        _seed_queue(pbx)
        with patch.object(handler, "_answer_caller", return_value=True):
            handler.handle_queue_entry("2000", "8001", "c1", _FakeMessage(), CALLER_ADDR)
        pbx.call_manager.end_call("c1")

        handler._sweep_once()

        assert pbx.queue_system.get_queue_status("8001")["calls_waiting"] == 0
        with handler._lock:
            assert "c1" not in handler._contexts

    def test_announcement_enabled_spawns_announce_worker(self, pbx, handler):
        _seed_queue(pbx, announcement_enabled=True, announcement_interval=5)
        with patch.object(handler, "_answer_caller", return_value=True):
            handler.handle_queue_entry("2000", "8001", "c1", _FakeMessage(), CALLER_ADDR)
        ctx = pbx.call_manager.get_call("c1").queue_ctx
        ctx.last_announcement_at -= 10  # interval already elapsed

        # Isolate from the real offer loop (also spawned by this sweep tick):
        # it would race to flip ctx.state to OFFERING before our check runs.
        with (
            patch.object(handler, "_offer_worker"),
            patch.object(handler, "_announce_worker") as announce,
        ):
            handler._sweep_once()
            deadline = time.monotonic() + 2
            while not announce.called and time.monotonic() < deadline:
                time.sleep(0.02)
            announce.assert_called_once_with(ctx)

    def test_announcement_disabled_by_default(self, pbx, handler):
        _seed_queue(pbx)  # announcement_enabled defaults to False
        with patch.object(handler, "_answer_caller", return_value=True):
            handler.handle_queue_entry("2000", "8001", "c1", _FakeMessage(), CALLER_ADDR)
        ctx = pbx.call_manager.get_call("c1").queue_ctx
        ctx.last_announcement_at -= 3600

        with patch.object(handler, "_announce_worker") as announce:
            handler._sweep_once()
            time.sleep(0.1)
            assert not announce.called


@pytest.mark.unit
class TestHoldAnnouncements:
    def test_resolve_prefers_announcement_file(self, pbx, handler, tmp_path):
        (tmp_path / "announcements").mkdir()
        prompt = tmp_path / "announcements" / "sales.wav"
        prompt.write_bytes(b"RIFF....WAVEfmt ")
        pbx.config.get.side_effect = (
            lambda key, default=None: str(tmp_path)
            if key == "music_on_hold.directory"
            else default
        )
        queue = _seed_queue(pbx, announcement_file="sales.wav")

        path, is_temp = handler._resolve_announcement_audio(queue, position=1)

        assert path == prompt
        assert is_temp is False

    def test_resolve_missing_file_falls_through(self, pbx, handler, tmp_path):
        pbx.config.get.side_effect = (
            lambda key, default=None: str(tmp_path)
            if key == "music_on_hold.directory"
            else default
        )
        queue = _seed_queue(pbx, announcement_file="missing.wav")

        path, is_temp = handler._resolve_announcement_audio(queue, position=1)

        assert path is None
        assert is_temp is False
        assert pbx.logger.warning.called

    def test_resolve_uses_tts_with_position(self, pbx, handler, tmp_path):
        queue = _seed_queue(
            pbx, announcement_text="Please hold", announcement_position=True
        )

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

    def test_announce_worker_interjects_and_updates_timestamp(self, pbx, handler, tmp_path):
        (tmp_path / "announcements").mkdir()
        prompt = tmp_path / "announcements" / "sales.wav"
        prompt.write_bytes(b"RIFF....WAVEfmt ")
        pbx.config.get.side_effect = (
            lambda key, default=None: str(tmp_path)
            if key == "music_on_hold.directory"
            else default
        )
        _seed_queue(pbx, announcement_enabled=True, announcement_file="sales.wav")
        with patch.object(handler, "_answer_caller", return_value=True):
            handler.handle_queue_entry("2000", "8001", "c1", _FakeMessage(), CALLER_ADDR)
        ctx = pbx.call_manager.get_call("c1").queue_ctx
        before = ctx.last_announcement_at

        handler._announce_worker(ctx)

        pbx.moh_system.interject.assert_called_once()
        args, kwargs = pbx.moh_system.interject.call_args
        assert args[0] == "c1"
        assert args[1] == ctx.held_side
        assert args[2] == prompt
        assert "interrupt_check" in kwargs
        assert ctx.last_announcement_at > before

    def test_announce_worker_skips_when_not_waiting(self, pbx, handler):
        from pbx.core.queue_handler import QueueCallState

        _seed_queue(pbx, announcement_enabled=True, announcement_file="sales.wav")
        with patch.object(handler, "_answer_caller", return_value=True):
            handler.handle_queue_entry("2000", "8001", "c1", _FakeMessage(), CALLER_ADDR)
        ctx = pbx.call_manager.get_call("c1").queue_ctx
        ctx.state = QueueCallState.OFFERING

        handler._announce_worker(ctx)

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
