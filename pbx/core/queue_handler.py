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

Concurrency model: **one owner thread per queued caller** (_caller_loop),
started at admission. That loop is the only thing that decides anything about
its caller -- announcements, agent selection, offers, overflow -- and it does
so strictly sequentially, so an announcement can never overlap an agent offer.
Offers block on the TransferSession's own callbacks (_offer_agent), which are
reduced to pure signals rather than driving recovery themselves. The only
other thread is a stats ticker, which is purely observational.

That single-owner rule is what keeps the caller's media coherent: MOH pause /
prompt / resume and the bridge's MOH stop are all sequenced by one thread
instead of racing. A previous design drove offers from a sweep tick, from the
offer worker's own retry loop, and recursively from the failure callback --
with announcements racing all three -- which produced clipped announcements
and, when a bridge landed mid-announcement, a re-paused relay and dead air.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

#: Agent star codes (internal-origin only; see CallRouter.route_call).
STAR_CODE_LOGIN = "*61"
STAR_CODE_LOGOUT = "*62"

#: Pacing for a caller loop that has nobody to offer to right now, and the
#: period of the stats ticker.
SWEEP_INTERVAL_SECONDS = 2.0

#: Slack added to a queue's ring_timeout when waiting for an offer to resolve.
#: The TransferSession watchdog always fires a callback, so this bound is only
#: a backstop against a wedged session -- never the normal path.
OFFER_RESOLVE_GRACE_SECONDS = 10.0

#: _offer_agent outcome for an agent who answered (any other value is a
#: TransferSession abort reason, e.g. "no_answer" / "target_rejected").
OFFER_ANSWERED = "answered"

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

#: Default hold-announcement text when a queue enables announcements without
#: a custom message or pre-recorded file (see _resolve_announcement_audio).
DEFAULT_ANNOUNCEMENT_TEXT = (
    "Thank you for holding. Your call is important to us and will be answered as soon as possible."
)

#: Spoken *61/*62 confirmation text (see _star_code_confirm). Falls back to
#: tone beeps if TTS is unavailable.
STAR_CODE_LOGIN_TEXT = "You are now signed in."
STAR_CODE_LOGOUT_TEXT = "You are now signed out."


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
    current_agent: str | None = None
    #: Which side ("a" or "b") of the relay hears MOH/announcements; the
    #: inverse of transferee_side, computed once in _admit.
    held_side: str = ""
    #: Wakes this caller's owner loop out of an idle pause -- an agent logging
    #: in, the caller hanging up, or shutdown. Never a decision in itself; the
    #: loop always re-reads real state after waking.
    wake: threading.Event = field(default_factory=threading.Event)

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
        # One lock for contexts, waiting lists, the offering set, and the owner
        # registry. Never held across SIP sends, start_transfer, DB writes, or
        # audio playback.
        self._lock = threading.RLock()
        self._contexts: dict[str, QueueCallContext] = {}
        # FIFO of call_ids per queue (position = index while non-terminal).
        self._waiting: dict[str, list[str]] = {}
        # Agents with an offer leg currently ringing, mapped to the monotonic
        # time their claim expires. Genuinely cross-caller: it stops two
        # callers offering the same agent at once. Expiring rather than
        # relying solely on an explicit release matters -- a claim that never
        # got released used to exclude that extension from every queue until
        # the process restarted, with nothing to show why.
        self._offering: dict[str, float] = {}
        # call_id -> the one thread that owns that caller's decisions.
        self._owners: dict[str, threading.Thread] = {}
        self._stats_thread: threading.Thread | None = None
        self._stop = threading.Event()

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
        and start the caller's owner thread.
        """
        pbx = self.pbx_core
        queue = pbx.queue_system.get_queue(ctx.queue_number)
        ctx.held_side = "a" if ctx.transferee_side == "caller" else "b"

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
            self._spawn(self._handle_overflow, ctx)
            return

        if not moh_running:
            relay_handler = pbx.rtp_relay.get_handler(ctx.call_id)
            if relay_handler is not None:
                pbx.moh_system.start_moh(ctx.call_id, relay_handler, ctx.held_side)

        self._push_stats()
        self._ensure_stats_thread()
        self._start_owner(ctx)

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
        # Let the owner loop notice immediately rather than after its pause.
        ctx.wake.set()

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
    # Owner threads + the stats ticker
    # ------------------------------------------------------------------

    def kick(self) -> None:
        """
        Public nudge (agent login/unpause): wake every waiting caller so it
        re-evaluates now instead of after its idle pause. Never a decision --
        each loop re-reads real state after waking.
        """
        with self._lock:
            contexts = list(self._contexts.values())
        for ctx in contexts:
            ctx.wake.set()

    def _start_owner(self, ctx: QueueCallContext) -> None:
        """Start the single thread that owns every decision for this caller"""
        thread = threading.Thread(
            target=self._caller_loop,
            args=(ctx,),
            daemon=True,
            name=f"queue-{ctx.queue_number}-{ctx.call_id}",
        )
        with self._lock:
            self._owners[ctx.call_id] = thread
        thread.start()

    def _ensure_stats_thread(self) -> None:
        """Start the stats ticker if it is not already running"""
        with self._lock:
            if self._stats_thread is not None and self._stats_thread.is_alive():
                return
            self._stop.clear()
            thread = threading.Thread(target=self._stats_loop, daemon=True)
            self._stats_thread = thread
            thread.start()

    def shutdown(self) -> None:
        """Stop the stats ticker and wake every caller loop (from PBXCore.stop())"""
        self._stop.set()
        with self._lock:
            contexts = list(self._contexts.values())
            owners = list(self._owners.values())
        for ctx in contexts:
            ctx.wake.set()

        thread = self._stats_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        # Best-effort: owners are daemons, and one parked in an offer can
        # outlive this by up to its ring timeout.
        for owner in owners:
            if owner.is_alive():
                owner.join(timeout=1)

    def _stats_loop(self) -> None:
        """
        Publish live waiting counts on a fixed tick.

        Purely observational -- it makes no decisions about any caller, so it
        cannot race an owner loop. Exits when idle (reaper pattern); the next
        admission restarts it.
        """
        pbx = self.pbx_core
        while not self._stop.is_set():
            self._stop.wait(SWEEP_INTERVAL_SECONDS)
            if self._stop.is_set():
                return

            try:
                self._push_stats()
            except Exception as exc:
                pbx.logger.error(f"Queue stats error: {exc}", exc_info=True)

            with self._lock:
                if not self._contexts:
                    self._stats_thread = None
                    return

    def _spawn(self, target: Any, ctx: QueueCallContext) -> None:
        """Run queue work on a fresh daemon thread (never on SIP/timer threads)"""
        thread = threading.Thread(target=target, args=(ctx,), daemon=True)
        thread.start()

    # ------------------------------------------------------------------
    # The caller loop: the one place a queued caller's fate is decided
    # ------------------------------------------------------------------

    def _caller_loop(self, ctx: QueueCallContext) -> None:
        """
        Own one queued caller from admission to a terminal outcome.

        Everything happens in this one thread, in order: overflow checks,
        then a hold announcement if one is due, then a single agent offer
        that blocks until it resolves. Nothing here can overlap anything
        else for this caller, which is what keeps its MOH/relay coherent.
        """
        pbx = self.pbx_core
        # Loop-local: only this thread reads them, so they are not shared
        # state and need no locking.
        offers: Counter[str] = Counter()
        no_agents_since: float | None = None
        last_announcement = time.monotonic()
        logged_idle = False

        try:
            while not self._stop.is_set():
                call = pbx.call_manager.get_call(ctx.call_id)
                if call is None:
                    # Ended elsewhere (e.g. end_call raced us).
                    self._release(ctx)
                    return

                with self._lock:
                    if ctx.state != QueueCallState.WAITING:
                        # Terminal, or handed to the overflow recorder.
                        return

                queue = pbx.queue_system.get_queue(ctx.queue_number)
                if queue is None:
                    self._release(ctx)
                    return

                if self._any_agent_logged_in(ctx.queue_number):
                    no_agents_since = None
                elif no_agents_since is None:
                    no_agents_since = time.monotonic()

                reason = self._overflow_reason(ctx, queue, no_agents_since)
                if reason is not None:
                    pbx.logger.info(
                        f"Queue {ctx.queue_number}: {reason} for {ctx.call_id}; overflowing"
                    )
                    self._handle_overflow(ctx)
                    return

                # An announcement runs to completion before any offer starts,
                # so it can never be clipped by one.
                if (
                    queue.announcement_enabled
                    and time.monotonic() - last_announcement >= queue.announcement_interval
                ):
                    self._play_announcement(ctx, queue)
                    last_announcement = time.monotonic()
                    continue

                agent_ext = self._pick_agent(ctx, queue, offers)
                if agent_ext is None:
                    # Everyone who could take this call has had their allowed
                    # attempts: stop waiting rather than holding to max_wait.
                    if self._retries_exhausted(queue, offers):
                        pbx.logger.info(
                            f"Queue {ctx.queue_number}: every agent has had "
                            f"{self._offer_budget(queue)} attempt(s) for "
                            f"{ctx.call_id}; overflowing"
                        )
                        self._handle_overflow(ctx)
                        return
                    # Otherwise agents still have attempts left but none are
                    # reachable this instant. Say why once, then pace.
                    if not logged_idle:
                        pbx.logger.info(
                            f"Queue {ctx.queue_number}: nobody offerable for "
                            f"{ctx.call_id} -- {self._selection_report(queue, offers)}"
                        )
                        logged_idle = True
                    ctx.wake.wait(SWEEP_INTERVAL_SECONDS)
                    ctx.wake.clear()
                    continue

                logged_idle = False
                if self._resolve_offer(ctx, queue, call, agent_ext):
                    return
        except Exception as exc:
            pbx.logger.error(
                f"Queue {ctx.queue_number}: caller loop failed for {ctx.call_id}: {exc}",
                exc_info=True,
            )
            self._release(ctx)
        finally:
            with self._lock:
                self._owners.pop(ctx.call_id, None)

    def _overflow_reason(
        self, ctx: QueueCallContext, queue: Any, no_agents_since: float | None
    ) -> str | None:
        """
        Why this caller should stop waiting now, or None to keep waiting.

        The no-agent rule mirrors the one applied at admission: when every
        member is logged out or paused -- where a DND phone ends up once
        auto-pause trips -- holding for the full max_wait just plays MOH at
        a caller nobody will ever answer. The grace period rides out a
        transient gap, e.g. an agent re-registering or mid *62/*61.
        """
        if ctx.wait_seconds() >= queue.max_wait_time:
            return "max wait reached"
        grace = self._no_agent_timeout()
        if (
            no_agents_since is not None
            and grace > 0
            and (time.monotonic() - no_agents_since >= grace)
        ):
            return "no agents available"
        return None

    def _resolve_offer(
        self,
        ctx: QueueCallContext,
        queue: Any,
        call: Any,
        agent_ext: str,
    ) -> bool:
        """
        Offer the caller to one agent and act on the result.

        Returns:
            True if the caller reached a terminal outcome (answered or
            abandoned) and the loop should stop.
        """
        outcome = self._offer_agent(ctx, queue, call, agent_ext)

        with self._lock:
            self._offering.pop(agent_ext, None)
            ctx.current_agent = None

        if outcome == OFFER_ANSWERED:
            self._on_answered(ctx, agent_ext)
            return True

        if outcome == "transferee_hangup":
            # The caller hung up while the agent was ringing: the session
            # swept the agent leg and left the caller's record for us.
            live = self.pbx_core.call_manager.get_call(ctx.call_id)
            if live is not None:
                self._abandon(ctx, live)
            return True

        # The attempt failed: back to waiting. The offer already counted
        # against this agent's budget when it was claimed in _pick_agent.
        with self._lock:
            if ctx.state == QueueCallState.OFFERING:
                ctx.state = QueueCallState.WAITING

        if outcome == REJECT_ABORT_REASON and self._pause_on_reject():
            self._record_reject(ctx, agent_ext)
        else:
            self._record_miss(ctx, agent_ext)
        return False

    def _release(self, ctx: QueueCallContext) -> None:
        """
        Drop tracking for a caller the loop is giving up on.

        Releases any offering exclusion the caller still holds: normally the
        offer's own resolution does that, but a call torn down outside the
        usual BYE/transfer path could otherwise strand an extension out of
        selection for every queue until restart.
        """
        with self._lock:
            if ctx.state not in TERMINAL_STATES:
                ctx.state = QueueCallState.DONE
            if ctx.current_agent is not None:
                self._offering.pop(ctx.current_agent, None)
                ctx.current_agent = None
            self._forget_locked(ctx)
        self._push_stats()

    # ------------------------------------------------------------------
    # Dial-out: one blind TransferSession per agent attempt
    # ------------------------------------------------------------------

    def _agent_dialable(self, ext: str) -> bool:
        """Registration + busy ground truth for agent availability"""
        pbx = self.pbx_core
        if not pbx.extension_registry.is_registered(ext):
            return False
        return not pbx.call_manager.get_extension_calls(ext)

    def _offer_budget(self, queue: Any) -> int:
        """How many times one caller may be offered to the same agent"""
        return 1 + max(0, int(getattr(queue, "max_redials", 0) or 0))

    def _claim_seconds(self, queue: Any) -> float:
        """How long an offer claim stays valid before it is assumed stale"""
        return float(queue.ring_timeout) + OFFER_RESOLVE_GRACE_SECONDS

    def _offering_now(self) -> set[str]:
        """
        Extensions with a live offer claim, dropping any that have expired.

        Caller must hold the lock. An expired claim means some path failed to
        release it; recovering here keeps that from silently excluding the
        agent from every queue for the life of the process.
        """
        now = time.monotonic()
        stale = [ext for ext, until in self._offering.items() if until <= now]
        for ext in stale:
            del self._offering[ext]
            self.pbx_core.logger.warning(
                f"Queue: stale offer claim on agent {ext} released; they were "
                "excluded from selection without an in-flight offer"
            )
        return set(self._offering)

    def _pick_agent(self, ctx: QueueCallContext, queue: Any, offers: Counter[str]) -> str | None:
        """
        Claim the next agent to offer to, or None if nobody is offerable.

        Claiming (state, current_agent, the offer count, the cross-caller
        _offering entry) happens under the lock so two callers cannot claim
        the same agent.
        """
        budget = self._offer_budget(queue)
        with self._lock:
            if ctx.state != QueueCallState.WAITING:
                return None
            spent = {ext for ext, count in offers.items() if count >= budget}
            agent = queue.get_next_agent(
                self.pbx_core.queue_system.agents,
                exclude=spent | self._offering_now(),
                is_dialable=self._agent_dialable,
            )
            if agent is None:
                return None
            agent_ext = str(agent.extension)
            ctx.state = QueueCallState.OFFERING
            ctx.current_agent = agent_ext
            ctx.attempts += 1
            offers[agent_ext] += 1
            self._offering[agent_ext] = time.monotonic() + self._claim_seconds(queue)
            return agent_ext

    def _retries_exhausted(self, queue: Any, offers: Counter[str]) -> bool:
        """
        Whether no agent can still be offered this caller.

        Keyed on whether anything has been attempted yet, not on whether
        anyone is currently selectable. An agent going unselectable *because*
        of the attempt -- ringing out into an auto-pause, or logging out
        mid-offer -- still counts as an attempt spent, so the caller must
        move on to the queue's end behaviour rather than fall through to the
        no-agent grace (which would add its whole timeout, or hold to
        max_wait_time when that grace is disabled).

        Before any offer has been made the grace period does own the
        decision: a caller admitted just as the last agent logs out should
        get that window to see if somebody comes back.
        """
        if not offers:
            return False
        agents = self.pbx_core.queue_system.agents
        budget = self._offer_budget(queue)
        return not any(
            offers[ext] < budget
            for ext in queue.members
            if ext in agents and agents[ext].is_selectable()
        )

    def _selection_report(self, queue: Any, offers: Counter[str]) -> str:
        """
        One `ext: reason` per member explaining why nobody could be offered.

        Mirrors the gates in CallQueue.get_next_agent and _agent_dialable, so
        a queue that stops distributing calls can be diagnosed from the log
        instead of by inspection.
        """
        pbx = self.pbx_core
        agents = pbx.queue_system.agents
        budget = self._offer_budget(queue)
        with self._lock:
            claimed = self._offering_now()

        parts = []
        for ext in sorted(queue.members):
            agent = agents.get(ext)
            if agent is None:
                reason = "no agent record"
            elif not agent.logged_in:
                reason = "logged out"
            elif agent.paused:
                reason = f"paused ({agent.pause_reason or 'manual'})"
            elif offers[ext] >= budget:
                reason = f"offered {offers[ext]}/{budget}"
            elif ext in claimed:
                reason = "ringing for another caller"
            elif not pbx.extension_registry.is_registered(ext):
                reason = "not registered"
            else:
                busy = pbx.call_manager.get_extension_calls(ext)
                reason = f"busy on {busy[0].call_id}" if busy else "available"
            parts.append(f"{ext}: {reason}")
        return "; ".join(parts) if parts else "queue has no members"

    def _offer_agent(self, ctx: QueueCallContext, queue: Any, call: Any, agent_ext: str) -> str:
        """
        Ring one agent and block until the attempt resolves.

        The TransferSession callbacks are pure signals here -- they record the
        outcome and wake this thread, which then owns every recovery decision.

        Returns:
            OFFER_ANSWERED, or the session's abort reason.
        """
        from pbx.core.transfer_session import TransferMode

        pbx = self.pbx_core
        pbx.logger.info(
            f"Queue {ctx.queue_number}: offering {ctx.call_id} to agent {agent_ext} "
            f"(attempt {ctx.attempts})"
        )

        done = threading.Event()
        outcome: list[str] = []
        # The session is only available after start_transfer returns, but a
        # callback can fire before then; an empty holder just means the abort
        # reason is unknown.
        holder: list[Any] = []

        def _failed() -> None:
            outcome.append((holder[0].abort_reason if holder else None) or "failed")
            done.set()

        def _answered() -> None:
            outcome.append(OFFER_ANSWERED)
            done.set()

        session = pbx.transfer_handler.start_transfer(
            call,
            agent_ext,
            mode=TransferMode.BLIND,
            transferor_side="callee" if ctx.transferee_side == "caller" else "caller",
            on_failure=_failed,
            on_complete=_answered,
        )
        if session is None:
            # Agent unregistered between the dialability check and the INVITE.
            pbx.logger.info(f"Queue {ctx.queue_number}: agent {agent_ext} unreachable")
            return "unreachable"

        holder.append(session)
        if not done.is_set():
            # Per-queue ring timeout overrides the default armed in
            # _originate_target (safe: arm_watchdog cancels the prior timer).
            session.arm_watchdog(float(queue.ring_timeout))
            timeout = float(queue.ring_timeout) + OFFER_RESOLVE_GRACE_SECONDS
            if not done.wait(timeout):
                # The watchdog should always have resolved by now.
                pbx.logger.error(f"Queue {ctx.queue_number}: offer to {agent_ext} never resolved")
                return "unresolved"
        return outcome[0] if outcome else "failed"

    def _on_answered(self, ctx: QueueCallContext, agent_ext: str) -> None:
        """The agent answered and is bridged to the caller"""
        from pbx.features.webhooks import WebhookEvent

        pbx = self.pbx_core
        pbx.queue_system.record_answered(agent_ext)

        with self._lock:
            ctx.state = QueueCallState.BRIDGED
            ctx.current_agent = agent_ext
            self._offering.pop(agent_ext, None)
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
        if pbx.queue_system.record_miss(agent_ext):
            pbx.webhook_system.trigger_event(
                WebhookEvent.QUEUE_AGENT_PAUSED,
                {"agent": agent_ext, "reason": "auto_missed"},
            )

    # ------------------------------------------------------------------
    # Hold announcements: sequential interruption of MOH
    # ------------------------------------------------------------------

    def _play_announcement(self, ctx: QueueCallContext, queue: Any) -> None:
        """
        Interrupt MOH with one hold announcement and let it resume.

        Called only from the caller's own loop, so no agent offer can be in
        flight and the prompt always plays in full. It still barges out if
        the caller hangs up mid-prompt.
        """
        pbx = self.pbx_core

        with self._lock:
            waiting = self._waiting.get(ctx.queue_number, [])
            if ctx.state != QueueCallState.WAITING or ctx.call_id not in waiting:
                return
            position = waiting.index(ctx.call_id) + 1

        audio_path, is_temp = self._resolve_announcement_audio(queue, position)
        if audio_path is None:
            return
        try:
            pbx.moh_system.interject(
                ctx.call_id,
                ctx.held_side,
                audio_path,
                interrupt_check=lambda: ctx.state != QueueCallState.WAITING,
            )
        finally:
            if is_temp:
                with contextlib.suppress(OSError):
                    audio_path.unlink()

    def _resolve_announcement_audio(self, queue: Any, position: int) -> tuple[Path | None, bool]:
        """
        Resolve the audio to play for one hold announcement.

        Returns:
            (path, is_temp) -- path is None if no announcement could be
            produced (missing file, TTS unavailable); is_temp tells the
            caller whether to delete the file afterward.
        """
        pbx = self.pbx_core

        if queue.announcement_file:
            moh_dir = pbx.config.get("music_on_hold.directory", "moh")
            candidate = Path(moh_dir) / "announcements" / queue.announcement_file
            if candidate.exists():
                return candidate, False
            pbx.logger.warning(
                f"Queue {queue.queue_number}: announcement file {candidate} not found"
            )
            return None, False

        from pbx.utils.audio import generate_tts_audio

        text = queue.announcement_text or DEFAULT_ANNOUNCEMENT_TEXT
        if queue.announcement_position:
            text = f"{text} You are caller number {position}."

        audio_bytes = generate_tts_audio(text)
        if audio_bytes is None:
            return None, False

        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
            temp_file.write(audio_bytes)
            return Path(temp_file.name), True

    # ------------------------------------------------------------------
    # Overflow: voicemail into the queue's mailbox, or drop
    # ------------------------------------------------------------------

    def _handle_overflow(self, ctx: QueueCallContext) -> None:
        """Dispatch an overflowing caller per the queue's overflow_action."""
        queue = self.pbx_core.queue_system.get_queue(ctx.queue_number)
        if queue is not None and queue.overflow_action == "drop":
            self._overflow_drop(ctx)
        else:
            self._overflow_to_voicemail(ctx)

    def _overflow_drop(self, ctx: QueueCallContext) -> None:
        """
        Overflow-drop: end the call instead of recording a message, when
        queue.overflow_action is 'drop'.

        Runs on a worker thread (matches _overflow_to_voicemail).
        """
        from pbx.features.webhooks import WebhookEvent

        pbx = self.pbx_core

        with self._lock:
            if ctx.state in TERMINAL_STATES:
                return
            ctx.state = QueueCallState.DONE
            waiting = self._waiting.get(ctx.queue_number)
            if waiting and ctx.call_id in waiting:
                waiting.remove(ctx.call_id)

        pbx.logger.info(f"Queue {ctx.queue_number}: dropping {ctx.call_id} on overflow")
        pbx.webhook_system.trigger_event(
            WebhookEvent.QUEUE_CALL_OVERFLOW,
            {
                "call_id": ctx.call_id,
                "queue": ctx.queue_number,
                "caller": ctx.caller_ext,
                "mailbox": None,
            },
        )

        pbx.moh_system.stop_moh(ctx.call_id)
        pbx.cdr_system.end_record(ctx.call_id, hangup_cause="queue_overflow_drop")

        call = pbx.call_manager.get_call(ctx.call_id)
        if call is not None:
            pbx.voicemail_handler._send_bye_to_caller(call, ctx.call_id)
            call.queue_ctx = None

        with self._lock:
            self._forget_locked(ctx)
        pbx.end_call(ctx.call_id)
        self._push_stats()

    def _overflow_to_voicemail(self, ctx: QueueCallContext) -> None:
        """
        Divert a queued caller to the queue's voicemail box: stop MOH and
        record via the shared VoicemailHandler.record_into_mailbox (greeting
        + beep, record until max duration, '#', or hangup).

        Runs on a worker thread (audio playback blocks).
        """
        from pbx.features.webhooks import WebhookEvent

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

        transferee_rtp = call.caller_rtp if ctx.transferee_side == "caller" else call.callee_rtp
        started = pbx.voicemail_handler.record_into_mailbox(
            call,
            ctx.call_id,
            mailbox_key,
            transferee_rtp,
            call.rtp_ports,
            hangup_cause="queue_overflow",
        )
        if not started:
            pbx.logger.error(f"Queue overflow: could not record {ctx.call_id}, ending call")
            with self._lock:
                ctx.state = QueueCallState.DONE
                self._forget_locked(ctx)
            call.queue_ctx = None
            pbx.end_call(ctx.call_id)
            return

        with self._lock:
            self._forget_locked(ctx, keep_context=True)
        self._push_stats()

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
        survives even if the confirmation audio fails. Confirmation is a
        short spoken message (falling back to tone beeps if TTS is
        unavailable); non-members get 404.

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
            args=(call, call_id, rtp_port, login),
            daemon=True,
        )
        thread.start()
        return True

    def _star_code_confirm(self, call: Any, call_id: str, rtp_port: int, login: bool) -> None:
        """Play a spoken sign-in/out confirmation, BYE the agent, release resources"""
        import tempfile

        from pbx.rtp.handler import RTPPlayer
        from pbx.utils.audio import generate_tts_audio

        pbx = self.pbx_core
        temp_path: Path | None = None
        try:
            time.sleep(0.3)
            player = RTPPlayer(
                local_port=rtp_port,
                remote_host=call.caller_rtp["address"],
                remote_port=call.caller_rtp["port"],
                call_id=call_id,
            )
            if player.start():
                text = STAR_CODE_LOGIN_TEXT if login else STAR_CODE_LOGOUT_TEXT
                audio_bytes = generate_tts_audio(text)
                if audio_bytes is not None:
                    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
                        temp_file.write(audio_bytes)
                        temp_path = Path(temp_file.name)
                    player.play_file(temp_path)
                else:
                    for _ in range(1 if login else 2):
                        player.play_beep(frequency=1000, duration_ms=200)
                        time.sleep(0.15)
                player.stop()
        except (KeyError, OSError) as exc:
            pbx.logger.error(f"Star-code confirmation failed for {call_id}: {exc}")
        finally:
            if temp_path is not None:
                with contextlib.suppress(OSError):
                    temp_path.unlink()
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
