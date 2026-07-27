"""
Queue call handler for PBX Core

Owns every live queued call: answering direct-dial/DID callers into the
queue, adopting transferred callers, parking them on hold music, serially
offering them to agents through the blind-transfer pipeline, and diverting
them to the queue's voicemail box on overflow.

The pbx.features.call_queue engine holds configuration, membership, and agent
state; this handler is the only component that touches Call objects, SIP, or
RTP for queued calls. The media model is the auto attendant's proven
park-and-bridge loop (see AutoAttendantHandler._begin_transfer): the caller
sits as one side of an adopted RTP relay with MOH, and each agent attempt is
a blind TransferSession whose bridge swaps the far side of that relay.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

#: Agent star codes (internal-origin only; see CallRouter.route_call).
STAR_CODE_LOGIN = "*61"
STAR_CODE_LOGOUT = "*62"

#: Seconds between sweep ticks (max-wait expiry, retry pacing, stats push).
SWEEP_INTERVAL_SECONDS = 2.0

#: How long a parked caller may wait with no selectable agent (all members
#: logged out, or paused -- including auto-paused after missing offers) before
#: overflowing to voicemail instead of holding for the full max_wait_time.
#: Entry already overflows immediately in this situation (see _admit); this is
#: the same rule applied mid-wait, with a short grace so a transient state --
#: an agent re-registering, or mid *62/*61 -- does not dump a caller. Override
#: with the top-level `queue_no_agent_timeout` in config.yml (`queues:` itself
#: is a list, so it cannot carry nested settings); 0 disables the check.
NO_AGENT_TIMEOUT_SECONDS = 15.0

#: TransferSession.abort_reason when the agent's phone answered the INVITE with
#: an explicit failure (486 Busy Here / 603 Decline / 480 Unavailable -- what a
#: phone in DND sends), as opposed to "no_answer"/"target_timeout" for a leg
#: that simply rang out. The distinction matters: a phone that actively refuses
#: is telling us it will not take calls, so re-offering to it in a tight loop
#: is pointless. See _record_reject.
REJECT_ABORT_REASON = "target_rejected"


class QueueEntryShape(Enum):
    """How the caller's leg reached the queue"""

    #: The queue (or the AA before it) answered the caller's INVITE itself:
    #: the caller occupies the record's caller side and the PBX holds the UAS
    #: dialog state (original_invite / caller_addr / voicemail_dialog_to).
    ANSWERED_BY_QUEUE = "answered_by_queue"

    #: The caller was transferred in via REFER on an established call: they
    #: may occupy either side of the record, and the departing transferor's
    #: BYE (same Call-ID) must be absorbed.
    ADOPTED_FROM_TRANSFER = "adopted_from_transfer"


class QueueCallState(Enum):
    """Lifecycle of one queued caller"""

    WAITING = "waiting"  # Parked on MOH, no agent leg in flight
    OFFERING = "offering"  # An agent leg is ringing
    OVERFLOW_PENDING = "overflow_pending"  # Max-wait hit mid-offer; VM on failure
    OVERFLOW_VM = "overflow_vm"  # Recording into the queue mailbox
    BRIDGED = "bridged"  # Terminal: talking to an agent
    ABANDONED = "abandoned"  # Terminal: caller hung up while waiting
    DONE = "done"  # Terminal: overflow VM finished / cleanup


TERMINAL_STATES = (QueueCallState.BRIDGED, QueueCallState.ABANDONED, QueueCallState.DONE)


@dataclass
class QueueCallContext:
    """Everything the handler tracks about one queued caller"""

    call_id: str
    queue_number: str
    caller_ext: str
    shape: QueueEntryShape
    #: Which side of the Call record the caller (transferee) occupies.
    transferee_side: str
    #: Departed transferor's address whose BYE must be absorbed (adopted shape).
    absorbed_addr: tuple[int | str, ...] | None
    state: QueueCallState = QueueCallState.WAITING
    enqueue_time: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    attempts: int = 0
    tried_agents: set[str] = field(default_factory=set)
    current_agent: str | None = None
    #: Monotonic timestamp before which no new offer should start.
    retry_at: float = 0.0
    #: Monotonic timestamp when the queue last went empty of selectable
    #: agents, or None while at least one is available (see _sweep_once).
    no_agents_since: float | None = None

    def wait_seconds(self) -> float:
        """Seconds this caller has been in the queue"""
        return (datetime.now(tz=UTC) - self.enqueue_time).total_seconds()


class QueueCallHandler:
    """Orchestrates live queued calls on top of the QueueSystem engine"""

    def __init__(self, pbx_core: Any) -> None:
        """
        Initialize QueueCallHandler with reference to PBXCore.

        Args:
            pbx_core: The PBXCore instance
        """
        self.pbx_core: Any = pbx_core
        # One lock for contexts, waiting lists, and the offering set. Never
        # held across SIP sends, start_transfer, DB writes, or audio playback.
        self._lock = threading.RLock()
        self._contexts: dict[str, QueueCallContext] = {}
        # FIFO of call_ids per queue (position = index while non-terminal).
        self._waiting: dict[str, list[str]] = {}
        # Agents with an offer leg currently ringing (never double-ring).
        self._offering: set[str] = set()
        self._sweep_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._kick_event = threading.Event()

    # ------------------------------------------------------------------
    # Public queries
    # ------------------------------------------------------------------

    def is_queue_destination(self, ext: str) -> bool:
        """Whether `ext` is an enabled call queue's number"""
        queue_system = getattr(self.pbx_core, "queue_system", None)
        if queue_system is None:
            return False
        if not self.pbx_core.config.get("features.call_queues", True):
            return False
        queue = queue_system.get_queue(ext)
        return queue is not None and queue.enabled

    # ------------------------------------------------------------------
    # Entry point 1: direct dial / DID (unanswered INVITE)
    # ------------------------------------------------------------------

    def handle_queue_entry(
        self,
        from_ext: str,
        to_ext: str,
        call_id: str,
        message: Any,
        from_addr: tuple[str, int],
    ) -> bool:
        """
        Answer an inbound INVITE into the queue: 180 -> 200 OK w/SDP, promote
        the answering port to a relay, start MOH, and register the caller.

        Reached from CallRouter._dial_to_internal_extension for both directly
        dialed queue numbers and DIDs mapped to one.

        Args:
            from_ext: Calling extension (or trunk caller ID)
            to_ext: Queue number
            call_id: Call ID
            message: SIP INVITE message
            from_addr: Caller address

        Returns:
            True if the call was taken by the queue
        """
        from pbx.features.webhooks import WebhookEvent
        from pbx.sip.sdp import SDPSession

        pbx = self.pbx_core
        queue = pbx.queue_system.get_queue(to_ext)
        if queue is None or not queue.enabled:
            return False

        pbx.logger.info(f"Queue entry: {from_ext} -> queue {to_ext} ({queue.name})")

        # Parse the caller's SDP -- without it there is no audio path to
        # park, so let normal routing produce the error response.
        caller_sdp: dict[str, Any] | None = None
        if message.body:
            sdp_obj = SDPSession()
            sdp_obj.parse(message.body)
            caller_sdp = sdp_obj.get_audio_info()
        if not caller_sdp:
            pbx.logger.warning(f"Queue {to_ext}: INVITE from {from_ext} has no usable SDP")
            return False

        call = pbx.call_manager.create_call(call_id, from_ext, to_ext)
        call.start()
        call.original_invite = message
        call.caller_addr = from_addr
        call.caller_rtp = caller_sdp
        pbx.cdr_system.start_record(call_id, from_ext, to_ext)

        # Take a media port straight from the pool (AA pattern): the queue
        # answers the caller itself, so no two-party relay exists yet.
        with pbx.rtp_relay._pool_lock:
            try:
                rtp_port: int = pbx.rtp_relay.port_pool.pop(0)
            except IndexError:
                pbx.logger.error(f"No available RTP ports for queue call {call_id}")
                pbx.cdr_system.end_record(call_id, hangup_cause="resource_unavailable")
                pbx.call_manager.end_call(call_id)
                return False
        call.rtp_ports = (rtp_port, rtp_port + 1)

        if not self._answer_caller(call, call_id, to_ext):
            self._return_port_to_pool(rtp_port)
            pbx.cdr_system.end_record(call_id, hangup_cause="resource_unavailable")
            pbx.call_manager.end_call(call_id)
            return False

        # Promote the raw port to a relay: caller = side A, agent fills side
        # B on answer. From here on end_call -> release_relay returns the
        # port to the pool.
        if not pbx.rtp_relay.adopt_existing_port(call_id, call.rtp_ports[0], call.rtp_ports[1]):
            pbx.logger.error(f"Failed to promote queue port to relay for {call_id}")
            self._return_port_to_pool(rtp_port)
            pbx.cdr_system.end_record(call_id, hangup_cause="resource_unavailable")
            pbx.call_manager.end_call(call_id)
            return False
        caller_ep = (caller_sdp["address"], caller_sdp["port"])
        pbx.rtp_relay.set_endpoints(call_id, caller_ep, None)

        ctx = QueueCallContext(
            call_id=call_id,
            queue_number=to_ext,
            caller_ext=from_ext,
            shape=QueueEntryShape.ANSWERED_BY_QUEUE,
            transferee_side="caller",
            absorbed_addr=None,
        )
        call.queue_ctx = ctx

        pbx.webhook_system.trigger_event(
            WebhookEvent.QUEUE_CALL_ADDED,
            {"call_id": call_id, "queue": to_ext, "caller": from_ext},
        )

        self._admit(ctx, call)
        return True

    # ------------------------------------------------------------------
    # Entry point 2: adopted callers (AA menu / REFER transfer)
    # ------------------------------------------------------------------

    def adopt_parked_caller(self, call_id: str, call: Any, queue_number: str) -> None:
        """
        Take over a caller the auto attendant already parked: answered by the
        PBX, promoted to a relay as side A, MOH running.

        The AA must treat the call as handed off after this returns.

        Args:
            call_id: Call identifier
            call: Call object (caller occupies the caller side)
            queue_number: Destination queue
        """
        from pbx.features.webhooks import WebhookEvent

        pbx = self.pbx_core
        pbx.logger.info(f"Queue {queue_number}: adopting AA caller {call.from_extension}")

        ctx = QueueCallContext(
            call_id=call_id,
            queue_number=queue_number,
            caller_ext=call.from_extension,
            shape=QueueEntryShape.ANSWERED_BY_QUEUE,
            transferee_side="caller",
            absorbed_addr=None,
        )
        call.queue_ctx = ctx

        pbx.webhook_system.trigger_event(
            WebhookEvent.QUEUE_CALL_ADDED,
            {"call_id": call_id, "queue": queue_number, "caller": call.from_extension},
        )

        # MOH is already running (the AA starts it before handing off);
        # _admit only starts it when absent.
        self._admit(ctx, call, moh_running=True)

    def adopt_transfer(
        self,
        original: Any,
        queue_number: str,
        *,
        transferor_side: str,
        refer_dialog: Any | None,
    ) -> None:
        """
        Adopt a caller REFER-transferred into the queue on an established
        call. The blind transfer "succeeds" the moment the queue accepts:
        a final NOTIFY 200 goes to the transferor, whose leg is detached
        from the record and whose subsequent BYE is absorbed.

        Args:
            original: Call record owning the relay (transferee on one side)
            queue_number: Destination queue
            transferor_side: Which side the departing transferor occupies
            refer_dialog: REFER subscription to report success on, if any
        """
        from pbx.core.transfer_session import NOTIFY_OK
        from pbx.features.webhooks import WebhookEvent

        pbx = self.pbx_core
        call_id = original.call_id
        transferee_side = "callee" if transferor_side == "caller" else "caller"
        caller_ext = (
            original.to_extension if transferee_side == "callee" else original.from_extension
        )

        pbx.logger.info(
            f"Queue {queue_number}: adopting transferred caller {caller_ext} "
            f"(transferee side: {transferee_side})"
        )

        # Report success on the REFER subscription -- no TransferSession is
        # ever created for a queue destination.
        if refer_dialog is not None:
            try:
                pbx.sip_server.send_transfer_notify(refer_dialog, NOTIFY_OK, terminated=True)
            except Exception as exc:
                pbx.logger.error(f"Queue adoption NOTIFY failed: {exc}")

        # Detach the transferor from the record: remember their address so
        # the BYE their phone sends on this Call-ID is absorbed, then null it
        # so nothing routes toward them anymore.
        if transferor_side == "caller":
            absorbed = original.caller_addr
            original.caller_addr = None
        else:
            absorbed = original.callee_addr
            original.callee_addr = None

        ctx = QueueCallContext(
            call_id=call_id,
            queue_number=queue_number,
            caller_ext=caller_ext,
            shape=QueueEntryShape.ADOPTED_FROM_TRANSFER,
            transferee_side=transferee_side,
            absorbed_addr=tuple(absorbed) if absorbed else None,
        )
        original.queue_ctx = ctx

        # A real phone's hold re-INVITE may already have MOH running toward
        # the transferee; restart it deterministically toward their side.
        pbx.moh_system.stop_moh(call_id)

        pbx.webhook_system.trigger_event(
            WebhookEvent.QUEUE_CALL_ADDED,
            {"call_id": call_id, "queue": queue_number, "caller": caller_ext},
        )

        self._admit(ctx, original)

    # ------------------------------------------------------------------
    # Admission: overflow-at-entry check, MOH, registration
    # ------------------------------------------------------------------

    def _admit(self, ctx: QueueCallContext, call: Any, moh_running: bool = False) -> None:
        """
        Register an answered caller with the queue: overflow immediately if
        the queue is full or has no logged-in agents, otherwise park on MOH
        and kick the pump.
        """
        pbx = self.pbx_core
        queue = pbx.queue_system.get_queue(ctx.queue_number)

        with self._lock:
            waiting = self._waiting.setdefault(ctx.queue_number, [])
            overflow = (
                queue is None
                or len(waiting) >= queue.max_queue_size
                or not self._any_agent_logged_in(ctx.queue_number)
            )
            self._contexts[ctx.call_id] = ctx
            if not overflow:
                waiting.append(ctx.call_id)

        if overflow:
            if moh_running:
                pbx.moh_system.stop_moh(ctx.call_id)
            reason = "full" if queue and len(waiting) >= queue.max_queue_size else "no_agents"
            pbx.logger.info(
                f"Queue {ctx.queue_number}: overflow at entry for {ctx.call_id} ({reason})"
            )
            self._spawn(self._overflow_to_voicemail, ctx)
            return

        if not moh_running:
            relay_handler = pbx.rtp_relay.get_handler(ctx.call_id)
            if relay_handler is not None:
                held_side = "a" if ctx.transferee_side == "caller" else "b"
                pbx.moh_system.start_moh(ctx.call_id, relay_handler, held_side)

        self._push_stats()
        self._ensure_sweeper()
        self._kick()

    def _no_agent_timeout(self) -> float:
        """Grace period before a caller with no selectable agents overflows"""
        try:
            return float(
                self.pbx_core.config.get("queue_no_agent_timeout", NO_AGENT_TIMEOUT_SECONDS)
            )
        except (TypeError, ValueError):
            return NO_AGENT_TIMEOUT_SECONDS

    def _any_agent_logged_in(self, queue_number: str) -> bool:
        """Whether any member of the queue is logged in and not paused"""
        queue_system = self.pbx_core.queue_system
        queue = queue_system.get_queue(queue_number)
        if queue is None:
            return False
        return any(
            ext in queue_system.agents and queue_system.agents[ext].is_selectable()
            for ext in queue.members
        )

    # ------------------------------------------------------------------
    # Answering (AA/voicemail 200 OK pattern)
    # ------------------------------------------------------------------

    def _answer_caller(self, call: Any, call_id: str, to_ext: str) -> bool:
        """
        Send 180 then 200 OK w/SDP for the queue's own port, storing the
        dialog To header so the PBX can later BYE the caller in-dialog
        (voicemail completion path).
        """
        from pbx.sip.message import SIPMessageBuilder
        from pbx.sip.sdp import SDPBuilder

        pbx = self.pbx_core

        try:
            server_ip: str = pbx._get_server_ip()
            sip_port: int = pbx.config.get("server.sip_port", 5060)
            contact_uri = f"<sip:{to_ext}@{server_ip}:{sip_port}>"

            ringing = SIPMessageBuilder.build_response(180, "Ringing", call.original_invite)
            ringing.set_header("Contact", contact_uri)
            pbx.sip_server._send_message(ringing.build(), call.caller_addr)

            # Brief pause so the caller's phone registers ring-back before
            # the answer (matches the auto attendant's behavior).
            time.sleep(0.5)

            caller_user_agent = pbx._get_phone_user_agent(call.from_extension)
            caller_phone_model = pbx._detect_phone_model(caller_user_agent)
            caller_codecs = call.caller_rtp.get("formats") if call.caller_rtp else None
            codecs = pbx._get_compatible_codecs(caller_phone_model, caller_codecs)

            protocol = "RTP/AVP"
            crypto: list[str] | None = None
            if call.caller_rtp:
                protocol = call.caller_rtp.get("protocol", "RTP/AVP")
                crypto = call.caller_rtp.get("crypto") or None

            sdp = SDPBuilder.build_audio_sdp(
                server_ip,
                call.rtp_ports[0],
                session_id=call_id,
                codecs=codecs,
                dtmf_payload_type=pbx._get_dtmf_payload_type(),
                ilbc_mode=pbx._get_ilbc_mode(),
                protocol=protocol,
                crypto=crypto,
                skip_static_rtpmap=pbx._should_skip_static_rtpmap(caller_phone_model),
            )

            ok = SIPMessageBuilder.build_response(200, "OK", call.original_invite, body=sdp)
            ok.set_header("Content-type", "application/sdp")
            ok.set_header("Contact", contact_uri)
            # The dialog To header (with our to-tag) makes the standard
            # voicemail-completion BYE work for queue calls too, and lets
            # _send_leg_bye() reach the caller correctly once bridged to an
            # agent (it reads caller_dialog_to, not voicemail_dialog_to).
            call.voicemail_dialog_to = ok.get_header("To")
            call.caller_dialog_to = ok.get_header("To")
            pbx.sip_server._send_message(ok.build(), call.caller_addr)
            call.connect()
            pbx.logger.info(f"Answered queue call {call_id} for queue {to_ext}")
            return True
        except (KeyError, OSError, TypeError, ValueError) as exc:
            pbx.logger.error(f"Failed to answer queue call {call_id}: {exc}")
            return False

    # ------------------------------------------------------------------
    # BYE handling (abandon + transferor absorption)
    # ------------------------------------------------------------------

    def on_bye(self, call: Any, addr: tuple[str, int]) -> bool:
        """
        Handle a BYE on a queued call's Call-ID.

        Returns:
            True if the BYE was fully handled (the server just sends 200);
            False to let the normal BYE fall-through run (used during
            overflow VM so end_call auto-saves the recording).
        """
        ctx: QueueCallContext | None = getattr(call, "queue_ctx", None)
        if ctx is None:
            return False

        pbx = self.pbx_core

        # Departed transferor's BYE (adopted shape): absorb, don't touch the
        # queued caller.
        if ctx.absorbed_addr is not None and tuple(addr) == ctx.absorbed_addr:
            pbx.logger.info(f"Queue {ctx.queue_number}: absorbed transferor BYE on {ctx.call_id}")
            ctx.absorbed_addr = None
            return True

        # Only the transferee's own BYE remains interesting.
        transferee_addr = call.caller_addr if ctx.transferee_side == "caller" else call.callee_addr
        if transferee_addr is not None and tuple(addr) != tuple(transferee_addr):
            return False

        if ctx.state == QueueCallState.OVERFLOW_VM:
            # Let the normal fall-through end_call run: routed_to_voicemail +
            # voicemail_recorder make it save the partial message.
            return False

        if ctx.state in TERMINAL_STATES:
            call.queue_ctx = None
            return False

        # Caller abandoned while waiting (or between agent attempts -- an
        # in-flight attempt's BYE is consumed by the transfer session before
        # this hook is reached).
        self._abandon(ctx, call)
        return True

    def _abandon(self, ctx: QueueCallContext, call: Any) -> None:
        """Terminal cleanup for a caller who hung up while queued"""
        from pbx.features.webhooks import WebhookEvent

        pbx = self.pbx_core
        with self._lock:
            if ctx.state in TERMINAL_STATES:
                return
            ctx.state = QueueCallState.ABANDONED
            self._forget_locked(ctx)

        pbx.logger.info(
            f"Queue {ctx.queue_number}: caller {ctx.caller_ext} abandoned after "
            f"{ctx.wait_seconds():.0f}s"
        )
        call.queue_ctx = None
        # First end_record wins: the accurate cause beats end_call's
        # normal_clearing.
        pbx.cdr_system.end_record(ctx.call_id, hangup_cause="queue_abandoned")
        pbx.webhook_system.trigger_event(
            WebhookEvent.QUEUE_CALL_ABANDONED,
            {
                "call_id": ctx.call_id,
                "queue": ctx.queue_number,
                "caller": ctx.caller_ext,
                "wait_seconds": round(ctx.wait_seconds(), 1),
            },
        )
        pbx.end_call(ctx.call_id)
        self._push_stats()

    # ------------------------------------------------------------------
    # The pump: sweep thread + kicks
    # ------------------------------------------------------------------

    def kick(self) -> None:
        """Public nudge (agent login/unpause) to re-evaluate waiting callers"""
        self._ensure_sweeper()
        self._kick()

    def _kick(self) -> None:
        self._kick_event.set()

    def _ensure_sweeper(self) -> None:
        """Start the sweep thread if it is not already running"""
        with self._lock:
            if self._sweep_thread is not None and self._sweep_thread.is_alive():
                return
            self._stop.clear()
            thread = threading.Thread(target=self._sweep_loop, daemon=True)
            self._sweep_thread = thread
            thread.start()

    def shutdown(self) -> None:
        """Stop the sweep thread (from PBXCore.stop())"""
        self._stop.set()
        self._kick_event.set()
        thread = self._sweep_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)

    def _sweep_loop(self) -> None:
        """
        Periodic driver: dead-call cleanup, max-wait expiry, retry pacing,
        offering waiting callers, stats push. Exits when idle (reaper
        pattern); restarted lazily by the next admission or kick.
        """
        pbx = self.pbx_core
        while not self._stop.is_set():
            self._kick_event.wait(SWEEP_INTERVAL_SECONDS)
            self._kick_event.clear()
            if self._stop.is_set():
                return

            try:
                self._sweep_once()
            except Exception as exc:
                pbx.logger.error(f"Queue sweep error: {exc}", exc_info=True)

            with self._lock:
                if not self._contexts:
                    self._sweep_thread = None
                    return

    def _sweep_once(self) -> None:
        """One sweep tick over every tracked context"""
        pbx = self.pbx_core
        now = time.monotonic()

        with self._lock:
            snapshot = list(self._contexts.values())

        for ctx in snapshot:
            call = pbx.call_manager.get_call(ctx.call_id)
            if call is None:
                # Ended elsewhere (e.g. end_call raced us): drop tracking.
                with self._lock:
                    if ctx.state not in TERMINAL_STATES:
                        ctx.state = QueueCallState.DONE
                    self._forget_locked(ctx)
                continue

            queue = pbx.queue_system.get_queue(ctx.queue_number)
            max_wait = queue.max_wait_time if queue else 300

            if ctx.state == QueueCallState.WAITING:
                # Nobody can take this call: every member is logged out or
                # paused (auto-pause after missed offers lands here too, which
                # is what a DND phone produces). Holding for the full max_wait
                # just plays MOH at a caller no one will ever answer.
                if self._any_agent_logged_in(ctx.queue_number):
                    ctx.no_agents_since = None
                else:
                    if ctx.no_agents_since is None:
                        ctx.no_agents_since = now
                    grace = self._no_agent_timeout()
                    if grace > 0 and now - ctx.no_agents_since >= grace:
                        with self._lock:
                            if ctx.state != QueueCallState.WAITING:
                                continue
                            ctx.state = QueueCallState.OVERFLOW_PENDING
                        pbx.logger.info(
                            f"Queue {ctx.queue_number}: no agents available for "
                            f"{ctx.call_id}, overflowing to voicemail"
                        )
                        self._spawn(self._overflow_to_voicemail, ctx)
                        continue

                if ctx.wait_seconds() >= max_wait:
                    # Latch under the lock before spawning so consecutive
                    # sweep ticks cannot spawn duplicate overflow workers.
                    with self._lock:
                        if ctx.state != QueueCallState.WAITING:
                            continue
                        ctx.state = QueueCallState.OVERFLOW_PENDING
                    pbx.logger.info(f"Queue {ctx.queue_number}: max wait reached for {ctx.call_id}")
                    self._spawn(self._overflow_to_voicemail, ctx)
                elif now >= ctx.retry_at:
                    self._spawn(self._offer_worker, ctx)
            elif ctx.state == QueueCallState.OFFERING and ctx.wait_seconds() >= max_wait:
                # Never interrupt a ringing agent: latch so the attempt's
                # failure path goes to voicemail instead of retrying. An
                # answer still wins.
                with self._lock:
                    if ctx.state == QueueCallState.OFFERING:
                        ctx.state = QueueCallState.OVERFLOW_PENDING

        self._push_stats()

    def _spawn(self, target: Any, ctx: QueueCallContext) -> None:
        """Run queue work on a fresh daemon thread (never on SIP/timer threads)"""
        thread = threading.Thread(target=target, args=(ctx,), daemon=True)
        thread.start()

    # ------------------------------------------------------------------
    # Dial-out: one blind TransferSession per agent attempt
    # ------------------------------------------------------------------

    def _agent_dialable(self, ext: str) -> bool:
        """Registration + busy ground truth for agent availability"""
        pbx = self.pbx_core
        if not pbx.extension_registry.is_registered(ext):
            return False
        return not pbx.call_manager.get_extension_calls(ext)

    def _offer_worker(self, ctx: QueueCallContext) -> None:
        """
        Pick an agent and originate the offer leg; on synchronous failure
        (agent unregistered race) count the miss and try the next agent in
        the same thread.
        """
        from pbx.core.transfer_session import TransferMode

        pbx = self.pbx_core

        while True:
            with self._lock:
                if ctx.state != QueueCallState.WAITING:
                    return
                queue = pbx.queue_system.get_queue(ctx.queue_number)
                if queue is None:
                    return
                agent = queue.get_next_agent(
                    pbx.queue_system.agents,
                    exclude=ctx.tried_agents | self._offering,
                    is_dialable=self._agent_dialable,
                )
                if agent is None:
                    # Nobody offerable right now: clear the exclusion cycle
                    # once everyone has been tried, and let the sweep pace
                    # the next attempt.
                    if ctx.tried_agents:
                        ctx.tried_agents.clear()
                    ctx.retry_at = time.monotonic() + SWEEP_INTERVAL_SECONDS
                    return
                agent_ext = agent.extension
                ctx.state = QueueCallState.OFFERING
                ctx.current_agent = agent_ext
                ctx.attempts += 1
                self._offering.add(agent_ext)

            call = pbx.call_manager.get_call(ctx.call_id)
            if call is None:
                with self._lock:
                    self._offering.discard(agent_ext)
                    if ctx.state not in TERMINAL_STATES:
                        ctx.state = QueueCallState.DONE
                    self._forget_locked(ctx)
                return

            pbx.logger.info(
                f"Queue {ctx.queue_number}: offering {ctx.call_id} to agent {agent_ext} "
                f"(attempt {ctx.attempts})"
            )

            transferor_side = "callee" if ctx.transferee_side == "caller" else "caller"
            # The session outlives this scope; the failure closure reads the
            # holder to learn the abort reason. Loop variables are bound as
            # defaults so each iteration's callbacks see their own agent.
            holder: list[Any] = []
            session = pbx.transfer_handler.start_transfer(
                call,
                agent_ext,
                mode=TransferMode.BLIND,
                transferor_side=transferor_side,
                on_failure=lambda a=agent_ext, h=holder: self._on_offer_failure(ctx, a, h),
                on_complete=lambda a=agent_ext: self._on_offer_complete(ctx, a),
            )

            if session is None:
                # Synchronous failure (agent unregistered between the
                # dialability check and the INVITE): a miss, try the next.
                pbx.logger.info(
                    f"Queue {ctx.queue_number}: agent {agent_ext} unreachable, trying next"
                )
                self._record_miss(ctx, agent_ext)
                with self._lock:
                    self._offering.discard(agent_ext)
                    ctx.current_agent = None
                    if ctx.state == QueueCallState.OFFERING:
                        ctx.state = QueueCallState.WAITING
                    if ctx.state == QueueCallState.OVERFLOW_PENDING:
                        break
                continue

            holder.append(session)
            # Per-queue ring timeout overrides the default armed in
            # _originate_target (safe: arm_watchdog cancels the prior timer).
            session.arm_watchdog(float(queue.ring_timeout))
            return

        # OVERFLOW_PENDING latched while cycling agents.
        self._overflow_to_voicemail(ctx)

    def _on_offer_failure(self, ctx: QueueCallContext, agent_ext: str, holder: list[Any]) -> None:
        """
        TransferSession failure hook. Fires on SIP-response/watchdog threads:
        hop to a fresh daemon thread before doing recovery work.
        """
        reason = holder[0].abort_reason if holder else None

        def _recover() -> None:
            pbx = self.pbx_core
            with self._lock:
                self._offering.discard(agent_ext)
                ctx.current_agent = None

            if reason == "transferee_hangup":
                # The caller hung up while the agent was ringing: the session
                # swept the agent leg and left the caller's record for us.
                call = pbx.call_manager.get_call(ctx.call_id)
                if call is not None:
                    self._abandon(ctx, call)
                return

            if reason == REJECT_ABORT_REASON and self._pause_on_reject():
                self._record_reject(ctx, agent_ext)
            else:
                self._record_miss(ctx, agent_ext)

            with self._lock:
                if ctx.state == QueueCallState.OFFERING:
                    ctx.state = QueueCallState.WAITING
                overflow_now = ctx.state == QueueCallState.OVERFLOW_PENDING

            if overflow_now:
                self._overflow_to_voicemail(ctx)
            else:
                # Caller kept their position (never dequeued); ring the next
                # agent immediately.
                self._offer_worker(ctx)

        thread = threading.Thread(target=_recover, daemon=True)
        thread.start()

    def _on_offer_complete(self, ctx: QueueCallContext, agent_ext: str) -> None:
        """TransferSession success hook: the agent answered and is bridged"""
        from pbx.features.webhooks import WebhookEvent

        pbx = self.pbx_core
        pbx.queue_system.record_answered(agent_ext)

        with self._lock:
            ctx.state = QueueCallState.BRIDGED
            ctx.current_agent = agent_ext
            self._offering.discard(agent_ext)
            self._forget_locked(ctx)

        call = pbx.call_manager.get_call(ctx.call_id)
        if call is not None:
            # Post-bridge BYEs use the normal bridged-call handling.
            call.queue_ctx = None

        pbx.logger.info(
            f"Queue {ctx.queue_number}: agent {agent_ext} answered {ctx.call_id} "
            f"after {ctx.wait_seconds():.0f}s"
        )
        pbx.webhook_system.trigger_event(
            WebhookEvent.QUEUE_CALL_ANSWERED,
            {
                "call_id": ctx.call_id,
                "queue": ctx.queue_number,
                "caller": ctx.caller_ext,
                "agent": agent_ext,
                "wait_seconds": round(ctx.wait_seconds(), 1),
            },
        )
        self._push_stats()

    def _pause_on_reject(self) -> bool:
        """Whether an explicitly rejected offer pauses the agent"""
        return bool(self.pbx_core.config.get("queue_pause_on_reject", True))

    def _record_reject(self, ctx: QueueCallContext, agent_ext: str) -> None:
        """
        The agent's phone actively refused the offer -- the signature of DND.

        Pause the agent rather than counting a miss, so the rotation stops
        redialing a phone that has told us it will not take calls. This is
        deliberately distinct from the no-answer path: a rang-out offer might
        just be someone away from their desk, but a decline is an answer.
        The pause is ordinary agent state -- visible in the admin UI, cleared
        by *61 or an admin unpause -- so nothing is stuck permanently.
        """
        from pbx.features.webhooks import WebhookEvent

        pbx = self.pbx_core
        ctx.tried_agents.add(agent_ext)
        if not pbx.queue_system.set_agent_pause(agent_ext, True, "auto_rejected"):
            return
        pbx.logger.info(
            f"Queue {ctx.queue_number}: agent {agent_ext} rejected the offer "
            "(DND/decline); pausing them until *61"
        )
        pbx.webhook_system.trigger_event(
            WebhookEvent.QUEUE_AGENT_PAUSED,
            {"agent": agent_ext, "reason": "auto_rejected"},
        )

    def _record_miss(self, ctx: QueueCallContext, agent_ext: str) -> None:
        """Count a missed offer; fire the auto-pause webhook when triggered"""
        from pbx.features.webhooks import WebhookEvent

        pbx = self.pbx_core
        ctx.tried_agents.add(agent_ext)
        if pbx.queue_system.record_miss(agent_ext):
            pbx.webhook_system.trigger_event(
                WebhookEvent.QUEUE_AGENT_PAUSED,
                {"agent": agent_ext, "reason": "auto_missed"},
            )

    # ------------------------------------------------------------------
    # Overflow: voicemail into the queue's mailbox
    # ------------------------------------------------------------------

    def _overflow_to_voicemail(self, ctx: QueueCallContext) -> None:
        """
        Divert a queued caller to the queue's voicemail box: stop MOH, take
        the relay's port back for a player/recorder, play greeting + beep,
        and record until max duration, '#', or hangup.

        Runs on a worker thread (audio playback blocks).
        """
        from pbx.features.webhooks import WebhookEvent
        from pbx.rtp.dtmf_monitor import build_ivr_dtmf_channel
        from pbx.rtp.handler import RTPPlayer

        pbx = self.pbx_core

        with self._lock:
            if ctx.state in TERMINAL_STATES or ctx.state == QueueCallState.OVERFLOW_VM:
                return
            ctx.state = QueueCallState.OVERFLOW_VM
            waiting = self._waiting.get(ctx.queue_number)
            if waiting and ctx.call_id in waiting:
                waiting.remove(ctx.call_id)

        call = pbx.call_manager.get_call(ctx.call_id)
        queue = pbx.queue_system.get_queue(ctx.queue_number)
        if call is None or queue is None:
            with self._lock:
                ctx.state = QueueCallState.DONE
                self._forget_locked(ctx)
            return

        mailbox_key: str = queue.overflow_mailbox()
        pbx.logger.info(
            f"Queue {ctx.queue_number}: overflowing {ctx.call_id} to mailbox {mailbox_key}"
        )
        pbx.webhook_system.trigger_event(
            WebhookEvent.QUEUE_CALL_OVERFLOW,
            {
                "call_id": ctx.call_id,
                "queue": ctx.queue_number,
                "caller": ctx.caller_ext,
                "mailbox": mailbox_key,
            },
        )

        pbx.moh_system.stop_moh(ctx.call_id)

        # Stop the relay handler in place (record kept so the final
        # end_call -> release_relay port bookkeeping stays consistent) and
        # reuse its port for the greeting player and recorder.
        relay_info = pbx.rtp_relay.active_relays.get(ctx.call_id)
        if relay_info:
            relay_info["handler"].stop()

        # Rewrite the record so every downstream voicemail path (completion,
        # hangup auto-save) keys off the queue mailbox.
        call.to_extension = mailbox_key
        call.routed_to_voicemail = True
        # First end_record wins: record the overflow cause now.
        pbx.cdr_system.end_record(ctx.call_id, hangup_cause="queue_overflow")

        transferee_rtp = call.caller_rtp if ctx.transferee_side == "caller" else call.callee_rtp
        if not transferee_rtp or not call.rtp_ports:
            pbx.logger.error(f"Queue overflow: no media info for {ctx.call_id}, ending call")
            with self._lock:
                ctx.state = QueueCallState.DONE
                self._forget_locked(ctx)
            call.queue_ctx = None
            pbx.end_call(ctx.call_id)
            return

        # Greeting + beep (queue mailbox greeting, else the default prompt).
        try:
            player = RTPPlayer(
                local_port=call.rtp_ports[0],
                remote_host=transferee_rtp["address"],
                remote_port=transferee_rtp["port"],
                call_id=ctx.call_id,
            )
            if player.start():
                self._play_greeting(player, mailbox_key)
                player.play_beep(frequency=1000, duration_ms=500)
                player.stop()
        except OSError as exc:
            pbx.logger.error(f"Queue overflow greeting failed for {ctx.call_id}: {exc}")

        recorder, _monitor = build_ivr_dtmf_channel(pbx, call, ctx.call_id, call.rtp_ports[0])
        if not recorder.start():
            pbx.logger.error(f"Queue overflow recorder failed for {ctx.call_id}")
            with self._lock:
                ctx.state = QueueCallState.DONE
                self._forget_locked(ctx)
            call.queue_ctx = None
            pbx.end_call(ctx.call_id)
            return

        call.voicemail_recorder = recorder
        max_duration: int = pbx.config.get("voicemail.max_message_duration", 180)
        timer = threading.Timer(
            max_duration,
            pbx.voicemail_handler.complete_voicemail_recording,
            args=(ctx.call_id,),
        )
        timer.daemon = True
        timer.start()
        call.voicemail_timer = timer

        # '#' finishes the recording via the standard monitor.
        monitor_thread = threading.Thread(
            target=pbx.voicemail_handler.monitor_voicemail_dtmf,
            args=(ctx.call_id, call, recorder),
            daemon=True,
        )
        monitor_thread.start()

        with self._lock:
            self._forget_locked(ctx, keep_context=True)
        self._push_stats()

    def _play_greeting(self, player: Any, mailbox_key: str) -> None:
        """Play the mailbox's custom greeting, or the default prompt"""
        import contextlib
        import tempfile
        from pathlib import Path

        from pbx.utils.audio import get_prompt_audio

        pbx = self.pbx_core

        greeting_path: str | None = None
        with contextlib.suppress(Exception):
            mailbox = pbx.voicemail_system.get_mailbox(mailbox_key)
            candidate = mailbox.get_greeting_path()
            if candidate and Path(candidate).exists():
                greeting_path = candidate

        if greeting_path:
            player.play_file(greeting_path)
            time.sleep(0.3)
            return

        prompt = get_prompt_audio("leave_message")
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
            temp_file.write(prompt)
            temp_path = temp_file.name
        try:
            player.play_file(temp_path)
            time.sleep(0.3)
        finally:
            with contextlib.suppress(OSError):
                Path(temp_path).unlink()

    # ------------------------------------------------------------------
    # Star codes: *61 login / *62 logout
    # ------------------------------------------------------------------

    def handle_agent_star_code(
        self,
        from_ext: str,
        to_ext: str,
        call_id: str,
        message: Any,
        from_addr: tuple[str, int],
    ) -> bool:
        """
        Handle an agent dialing *61 (login) or *62 (logout).

        State is persisted before the call is answered, so the toggle
        survives even if the confirmation audio fails. Confirmation is one
        beep for login, two for logout; non-members get 404.

        Args:
            from_ext: The agent's extension
            to_ext: "*61" or "*62"
            call_id: Call ID
            message: SIP INVITE message
            from_addr: Caller address

        Returns:
            True (the star code was handled, successfully or not)
        """
        from pbx.sip.message import SIPMessageBuilder
        from pbx.sip.sdp import SDPSession

        pbx = self.pbx_core
        login = to_ext == STAR_CODE_LOGIN

        queues = pbx.queue_system.set_agent_login(from_ext, login)
        if not queues:
            pbx.logger.info(f"Star code {to_ext} from non-agent {from_ext}")
            response = SIPMessageBuilder.build_response(404, "Not Found", message)
            pbx.sip_server._send_message(response.build(), from_addr)
            return True

        pbx.logger.info(
            f"Agent {from_ext} {'logged into' if login else 'logged out of'} "
            f"queue(s) {', '.join(queues)} via {to_ext}"
        )
        if login:
            self.kick()

        # Confirmation beeps on a short answered call.
        caller_sdp: dict[str, Any] | None = None
        if message.body:
            sdp_obj = SDPSession()
            sdp_obj.parse(message.body)
            caller_sdp = sdp_obj.get_audio_info()
        if not caller_sdp:
            # No media offered: just decline politely; the state already
            # changed.
            response = SIPMessageBuilder.build_response(200, "OK", message)
            pbx.sip_server._send_message(response.build(), from_addr)
            return True

        call = pbx.call_manager.create_call(call_id, from_ext, to_ext)
        call.start()
        call.original_invite = message
        call.caller_addr = from_addr
        call.caller_rtp = caller_sdp

        with pbx.rtp_relay._pool_lock:
            try:
                rtp_port: int = pbx.rtp_relay.port_pool.pop(0)
            except IndexError:
                pbx.logger.error(f"No RTP port for star-code confirmation {call_id}")
                pbx.call_manager.end_call(call_id)
                return True
        call.rtp_ports = (rtp_port, rtp_port + 1)

        if not self._answer_caller(call, call_id, to_ext):
            self._return_port_to_pool(rtp_port)
            pbx.call_manager.end_call(call_id)
            return True

        thread = threading.Thread(
            target=self._star_code_confirm,
            args=(call, call_id, rtp_port, 1 if login else 2),
            daemon=True,
        )
        thread.start()
        return True

    def _star_code_confirm(self, call: Any, call_id: str, rtp_port: int, beeps: int) -> None:
        """Play N confirmation beeps, BYE the agent, release resources"""
        from pbx.rtp.handler import RTPPlayer

        pbx = self.pbx_core
        try:
            time.sleep(0.3)
            player = RTPPlayer(
                local_port=rtp_port,
                remote_host=call.caller_rtp["address"],
                remote_port=call.caller_rtp["port"],
                call_id=call_id,
            )
            if player.start():
                for _ in range(beeps):
                    player.play_beep(frequency=1000, duration_ms=200)
                    time.sleep(0.15)
                player.stop()
        except (KeyError, OSError) as exc:
            pbx.logger.error(f"Star-code beep failed for {call_id}: {exc}")
        finally:
            pbx.voicemail_handler._send_bye_to_caller(call, call_id)
            self._return_port_to_pool(rtp_port)
            pbx.call_manager.end_call(call_id)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _forget_locked(self, ctx: QueueCallContext, keep_context: bool = False) -> None:
        """
        Remove a context from the waiting list (and, unless keep_context,
        from tracking entirely). Caller must hold the lock.
        """
        waiting = self._waiting.get(ctx.queue_number)
        if waiting and ctx.call_id in waiting:
            waiting.remove(ctx.call_id)
        if not keep_context:
            self._contexts.pop(ctx.call_id, None)

    def _return_port_to_pool(self, rtp_port: int) -> None:
        """Return a directly popped (never relay-registered) port to the pool"""
        pbx = self.pbx_core
        with pbx.rtp_relay._pool_lock:
            pbx.rtp_relay.port_pool.append(rtp_port)
            pbx.rtp_relay.port_pool.sort()

    def _push_stats(self) -> None:
        """Publish live waiting counts into the engine for status/dashboards"""
        pbx = self.pbx_core
        with self._lock:
            for queue_number in pbx.queue_system.queues:
                waiting_ids = self._waiting.get(queue_number, [])
                longest = 0.0
                for call_id in waiting_ids:
                    ctx = self._contexts.get(call_id)
                    if ctx is not None:
                        longest = max(longest, ctx.wait_seconds())
                pbx.queue_system.set_runtime_stats(queue_number, len(waiting_ids), longest)
