"""
Transfer session state machine.

One :class:`TransferSession` owns one transfer attempt from start to finish.
Before this module existed, transfer state was spread across ad-hoc fields on
two separate ``Call`` records and every consumer (BYE handling, answer
handling, the no-answer timer, the voicemail fallback) independently re-derived
"is a transfer pending here, and what do I do about it". Nothing guaranteed
that every leg reached a terminal state, and nothing guaranteed the transferor
was ever told the transfer had ended -- so legs leaked and phones sat on a
"transferring" indicator forever.

The session fixes that structurally. Every participating ``Call`` carries only
``transfer_session_id``; the transfer's own state lives here. There are exactly
two exit paths -- :meth:`TransferSession.complete` and
:meth:`TransferSession.abort` -- and both run the same
:meth:`TransferSession._sweep` over a roster of every leg, so no leg can be
missed by forgetting a call site. Both also send exactly one final NOTIFY
before closing, so the transferor's phone always learns the outcome.

Semantics follow Asterisk's ``features.conf`` (see the ``transfer.*`` config
keys): a transferor who hangs up while the target is still ringing yields a
semi-attended transfer that proceeds, and a target that fails recalls the
transferor rather than silently dropping the parked party.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from pbx.core.call import Call


class TransferMode(Enum):
    """How the transfer target leg is established."""

    BLIND = "blind"  # PBX originates the target leg; transferor never consults
    ATTENDED = "attended"  # transferor holds a consultation leg to the target


class TransferState(Enum):
    """Lifecycle of a transfer attempt."""

    INITIATING = "initiating"  # target leg originated/adopted, not yet answered
    CONSULTING = "consulting"  # target answered, transferor talking to it
    COMPLETING = "completing"  # commit issued, waiting on/performing the bridge
    BRIDGED = "bridged"  # media re-pointed, transferor's legs being dropped
    RECALLING = "recalling"  # target failed, ringing the transferor back
    ABORTING = "aborting"  # tearing every leg down
    CLOSED = "closed"  # terminal


class LegRole(Enum):
    """Which party a tracked leg belongs to."""

    TRANSFEROR = "transferor"  # the party that initiated the transfer
    TRANSFEREE = "transferee"  # the party being handed off
    TARGET = "target"  # the party being transferred to


class LegStatus(Enum):
    """How far a tracked leg has progressed."""

    PENDING = "pending"  # INVITE sent, no provisional response yet
    RINGING = "ringing"
    ANSWERED = "answered"
    TERMINATED = "terminated"


class LegEvent(Enum):
    """Something that happened to one leg of a transfer."""

    RINGING = "ringing"
    ANSWERED = "answered"
    BYE = "bye"
    REJECTED = "rejected"
    TIMEOUT = "timeout"


class LegEventResult(Enum):
    """What the SIP layer should do with the message that caused the event."""

    ABSORB = "absorb"  # answer 200 OK only: do not forward, do not end the call
    FORWARD = "forward"  # not transfer-related; handle with the normal path
    HANDLED = "handled"  # the session already tore the relevant legs down


# sipfrag bodies for the REFER subscription's NOTIFYs (RFC 3515 SS2.4).
NOTIFY_TRYING = "SIP/2.0 100 Trying"
NOTIFY_RINGING = "SIP/2.0 180 Ringing"
NOTIFY_OK = "SIP/2.0 200 OK"
NOTIFY_NOT_FOUND = "SIP/2.0 404 Not Found"
NOTIFY_UNAVAILABLE = "SIP/2.0 480 Temporarily Unavailable"
NOTIFY_TIMEOUT = "SIP/2.0 408 Request Timeout"
NOTIFY_BUSY = "SIP/2.0 486 Busy Here"
NOTIFY_ERROR = "SIP/2.0 500 Server Internal Error"
NOTIFY_DECLINE = "SIP/2.0 603 Decline"


@dataclass
class LegRef:
    """
    One party's side of one ``Call`` record.

    A transfer's parties do not map one-to-one onto ``Call`` records: the
    transferor and transferee share the original record, and the transferor and
    target share the consultation record. So a leg is identified by
    ``(call_id, side)`` -- which is exactly what ``SIPServer._send_leg_bye``
    accepts.
    """

    call_id: str
    side: str  # "caller" | "callee"
    status: LegStatus = LegStatus.PENDING


@dataclass
class ReferDialog:
    """
    Identity of the implicit subscription created by a REFER (RFC 3515 SS2.4).

    Held for the life of the transfer so every NOTIFY goes out on the same
    dialog with a strictly increasing CSeq. Reusing a CSeq makes the receiving
    phone treat the NOTIFY as a retransmission and ignore it, which is one way
    a "transferring" indicator gets stuck forever.
    """

    call_id: str
    from_header: str
    to_header: str
    addr: tuple[str, int]
    notify_cseq: int = 1
    final_sent: bool = False
    #: The Call record whose dialog this subscription rides on. NOTIFYs and
    #: any PBX-originated BYE toward the transferor share that one dialog, so
    #: they must draw CSeq from the record's single pbx_leg_cseq counter --
    #: two independent counters collide (both produce "2" first), and the
    #: phone rejects the second request as a CSeq violation. A direct
    #: reference (not a registry lookup) so the final NOTIFY, sent after
    #: end_call unregisters the record, still continues the same sequence.
    call: Any = None


@dataclass
class TransferSession:
    """
    The single owner of one transfer attempt.

    All state transitions happen in :meth:`on_event`; all teardown happens in
    :meth:`_sweep`, reached only via :meth:`complete` or :meth:`abort`. Both
    exits are guarded by ``_lock`` and the ``_terminal`` latch so the watchdog
    thread and the SIP receive thread cannot run them concurrently or twice.
    """

    pbx: Any
    mode: TransferMode
    original_call_id: str
    transferor_side: str
    transferor_extension: str
    transferee_extension: str
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    state: TransferState = TransferState.INITIATING
    destination: str | None = None
    target_call_id: str | None = None
    refer_dialog: ReferDialog | None = None
    on_failure: Callable[[], None] | None = None
    on_complete: Callable[[], None] | None = None
    #: Why abort() ran, for initiators whose on_failure needs to distinguish
    #: causes (e.g. a call queue: "transferee_hangup" means the caller
    #: abandoned and must not be offered to another agent).
    abort_reason: str | None = None
    legs: dict[LegRole, list[LegRef]] = field(default_factory=dict)
    deadline_timer: threading.Timer | None = None
    recall_attempts: int = 0
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _terminal: bool = False

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        pbx_core: Any,
        original: Call,
        *,
        mode: TransferMode,
        transferor_side: str,
        destination: str | None = None,
        refer_dialog: ReferDialog | None = None,
        on_failure: Callable[[], None] | None = None,
        on_complete: Callable[[], None] | None = None,
    ) -> TransferSession:
        """
        Build a session for a transfer of `original`'s far party.

        Args:
            pbx_core: The PBXCore instance.
            original: The call whose remaining party is being transferred.
            mode: Blind (PBX originates the target leg) or attended.
            transferor_side: Which side of `original` the transferor occupies
                ("caller" or "callee").
            destination: Target extension, when known up front.
            refer_dialog: Subscription to NOTIFY, for REFER-driven transfers.
                None for REST- or IVR-driven transfers, where nobody subscribed.
            on_failure: Invoked instead of tearing the transferee's leg down
                when the transfer fails -- used by a transfer initiator with no
                real phone to recall (e.g. an auto attendant, which wants to
                replay its menu rather than be hung up on).
            on_complete: Invoked once after a successful bridge -- for
                initiators tracking the outcome (e.g. a call queue recording
                that the agent answered).

        Returns:
            The session, already registered on `original`.
        """
        transferee_side = "callee" if transferor_side == "caller" else "caller"
        session = cls(
            pbx=pbx_core,
            mode=mode,
            original_call_id=original.call_id,
            transferor_side=transferor_side,
            transferor_extension=(
                original.from_extension if transferor_side == "caller" else original.to_extension
            ),
            transferee_extension=(
                original.to_extension if transferor_side == "caller" else original.from_extension
            ),
            destination=destination,
            refer_dialog=refer_dialog,
            on_failure=on_failure,
            on_complete=on_complete,
        )
        session.legs = {
            LegRole.TRANSFEROR: [
                LegRef(original.call_id, transferor_side, LegStatus.ANSWERED),
            ],
            LegRole.TRANSFEREE: [
                LegRef(original.call_id, transferee_side, LegStatus.ANSWERED),
            ],
            LegRole.TARGET: [],
        }
        original.transfer_session_id = session.session_id
        return session

    def add_transferor_leg(self, call: Call, side: str) -> None:
        """
        Track an additional leg belonging to the transferor.

        Used when the transferor's SIP dialog lives on a different record than
        the media -- a second transfer chained off an already-bridged call,
        where the REFER arrives on the peer leg while the relay (and the
        transferee) belong to the record it is cross-linked to.

        Args:
            call: Record holding the transferor's dialog.
            side: Which side of it the transferor occupies.
        """
        with self._lock:
            call.transfer_session_id = self.session_id
            self.legs[LegRole.TRANSFEROR].append(LegRef(call.call_id, side, LegStatus.ANSWERED))

    def attach_target(self, consult: Call, *, transferor_present: bool, answered: bool) -> None:
        """
        Register the leg toward the transfer target.

        Args:
            consult: The call carrying the target leg. For a blind transfer the
                PBX originated it, so it has no transferor side; for an
                attended transfer the transferor's phone established it and
                occupies its caller side.
            transferor_present: Whether the transferor occupies `consult`'s
                caller side (true for attended, false for PBX-originated blind).
            answered: Whether the target has already answered.
        """
        with self._lock:
            self.target_call_id = consult.call_id
            consult.transfer_session_id = self.session_id
            self.legs[LegRole.TARGET] = [
                LegRef(
                    consult.call_id,
                    "callee",
                    LegStatus.ANSWERED if answered else LegStatus.RINGING,
                )
            ]
            if transferor_present:
                self.legs[LegRole.TRANSFEROR].append(
                    LegRef(consult.call_id, "caller", LegStatus.ANSWERED)
                )

    # ------------------------------------------------------------------
    # Event dispatch -- the only place transitions happen
    # ------------------------------------------------------------------

    def on_event(self, role: LegRole, event: LegEvent) -> LegEventResult:
        """
        Advance the state machine in response to something happening to a leg.

        Args:
            role: Which party's leg the event concerns.
            event: What happened.

        Returns:
            What the caller should do with the triggering SIP message.
        """
        with self._lock:
            if self._terminal:
                # The transfer is already resolved; anything still arriving is
                # a straggler from a leg we have moved past. Acknowledge it
                # without disturbing whatever replaced this transfer.
                return LegEventResult.ABSORB

            if event is LegEvent.RINGING:
                return self._on_ringing(role)
            if event is LegEvent.ANSWERED:
                return self._on_answered(role)
            if event is LegEvent.BYE:
                return self._on_bye(role)
            if event in (LegEvent.REJECTED, LegEvent.TIMEOUT):
                return self._on_target_failure(role, event)
            return LegEventResult.FORWARD

    def _on_ringing(self, role: LegRole) -> LegEventResult:
        """Target started ringing: report progress on the REFER subscription."""
        if role is not LegRole.TARGET:
            return LegEventResult.FORWARD
        self._set_status(role, LegStatus.RINGING)
        self._notify(NOTIFY_RINGING)
        return LegEventResult.HANDLED

    def _on_answered(self, role: LegRole) -> LegEventResult:
        """
        Target answered.

        Blind transfers bridge immediately. Attended transfers park in
        CONSULTING so the transferor can talk -- unless the transferor already
        committed or hung up (COMPLETING), which is the semi-attended case and
        bridges as soon as the answer lands.
        """
        if role is not LegRole.TARGET:
            return LegEventResult.FORWARD

        self._set_status(role, LegStatus.ANSWERED)

        if self.mode is TransferMode.BLIND or self.state is TransferState.COMPLETING:
            self.complete()
            return LegEventResult.HANDLED

        self.state = TransferState.CONSULTING
        self._cancel_timer()
        return LegEventResult.HANDLED

    def _on_bye(self, role: LegRole) -> LegEventResult:
        """
        A party hung up. Which leg it was decides whether the transfer can
        still complete, must roll back, or must tear everything down.
        """
        if role is LegRole.TRANSFEROR:
            return self._on_transferor_bye()

        if role is LegRole.TRANSFEREE:
            # Nobody left to hand off. Drop the target leg too rather than
            # leaving it ringing at a destination with no one to connect to.
            self._set_status(role, LegStatus.TERMINATED)
            self.abort("transferee_hangup", sipfrag=NOTIFY_DECLINE)
            return LegEventResult.HANDLED

        # Target hung up (during consultation, or having answered and changed
        # its mind before the bridge).
        self._set_status(role, LegStatus.TERMINATED)
        self.abort("target_hangup", sipfrag=NOTIFY_DECLINE)
        return LegEventResult.HANDLED

    def _on_transferor_bye(self) -> LegEventResult:
        """
        The transferor dropped out.

        With the target already answered this is a completed attended transfer
        (Asterisk completes on hangup). With the target still ringing it is a
        semi-attended transfer, which proceeds: the transferee stays parked and
        is bridged when the target answers -- guarded by a watchdog, because
        otherwise a target that never answers leaves the transferee parked
        forever.
        """
        self._set_status(LegRole.TRANSFEROR, LegStatus.TERMINATED)
        # A phone that hangs up mid-transfer drops every dialog it holds, so
        # clear the transferor's addresses: a later sweep must not aim a BYE at
        # a party that is already gone, and nulling them is also what lets
        # _send_leg_bye auto-select the surviving party on these records.
        for ref in self.legs.get(LegRole.TRANSFEROR, []):
            self._null_leg_address(ref)

        target = self._target_ref()
        if target is not None and target.status is LegStatus.ANSWERED:
            self.complete()
            return LegEventResult.ABSORB

        self.state = TransferState.COMPLETING
        self.arm_watchdog(self._no_answer_timeout())
        self.pbx.logger.info(
            f"Transfer {self.session_id}: transferor left before the target answered; "
            "proceeding as semi-attended"
        )
        # Absorbed, not forwarded: the transferee must stay up for the bridge.
        return LegEventResult.ABSORB

    def _on_target_failure(self, role: LegRole, event: LegEvent) -> LegEventResult:
        """Target declined, was busy, or never answered."""
        if role is not LegRole.TARGET:
            return LegEventResult.FORWARD
        self._set_status(role, LegStatus.TERMINATED)
        sipfrag = NOTIFY_TIMEOUT if event is LegEvent.TIMEOUT else NOTIFY_BUSY
        self.abort(f"target_{event.value}", sipfrag=sipfrag)
        return LegEventResult.HANDLED

    # ------------------------------------------------------------------
    # Exit path 1: completion
    # ------------------------------------------------------------------

    def complete(self) -> bool:
        """
        Bridge the transferee to the target and drop the transferor's legs.

        Returns:
            True if the bridge was established.
        """
        with self._lock:
            if self._terminal:
                return False

            target = self._target_ref()
            if target is None or target.status is not LegStatus.ANSWERED:
                self.pbx.logger.warning(
                    f"Transfer {self.session_id}: complete() with no answered target"
                )
                return False

            self.state = TransferState.COMPLETING
            self._cancel_timer()

            original = self.pbx.call_manager.get_call(self.original_call_id)
            consult = (
                self.pbx.call_manager.get_call(self.target_call_id) if self.target_call_id else None
            )
            if original is None or consult is None:
                self.abort("missing_leg", sipfrag=NOTIFY_ERROR)
                return False

            # Hang the transferor up first: bridging rewrites their side of the
            # original record (nulling the very address a BYE would be aimed
            # at), so sweeping afterwards would silently reach nobody and leave
            # their phone up, apparently still connected.
            self._sweep(except_roles={LegRole.TRANSFEREE, LegRole.TARGET})

            try:
                bridged = self.pbx.transfer_handler.bridge(self, original, consult)
            except Exception as exc:
                self.pbx.logger.error(
                    f"Transfer {self.session_id}: bridge raised: {exc}", exc_info=True
                )
                bridged = False

            if not bridged:
                self.abort("bridge_failed", sipfrag=NOTIFY_ERROR)
                return False

            # The transferee and target are talking to each other now, and the
            # transferor's legs were already closed above.
            self.state = TransferState.BRIDGED
            try:
                self._notify(NOTIFY_OK)
                self._close()
            except Exception as exc:
                # Must not strand on_complete below: it is a one-shot promise
                # callers depend on for their own cleanup (e.g. the queue
                # handler releasing the agent's offering exclusion).
                self.pbx.logger.error(
                    f"Transfer {self.session_id}: post-bridge teardown raised: {exc}",
                    exc_info=True,
                )

            # One-shot success callback, after the session is fully closed so
            # the callback sees a clean call record (transfer_session_id
            # already cleared).
            if self.on_complete is not None:
                callback = self.on_complete
                self.on_complete = None
                try:
                    callback()
                except Exception as exc:
                    self.pbx.logger.error(f"Transfer {self.session_id}: on_complete raised: {exc}")
            return True

    # ------------------------------------------------------------------
    # Exit path 2: abort
    # ------------------------------------------------------------------

    def abort(self, reason: str, *, sipfrag: str = NOTIFY_ERROR) -> None:
        """
        Resolve a transfer that cannot complete, leaving no leg behind.

        Safe to call from any state and from any thread; the second call is a
        no-op. Which parties survive depends on who is left:

        - Transferor still connected: roll back. Only the target leg is
          dropped and the original call is un-parked, so the transferor and
          transferee simply carry on.
        - Transferor gone, recall eligible: ring the transferor back and bridge
          them to the still-parked transferee (Asterisk ``atxferdropcall=no``).
        - Otherwise: sweep every leg.

        Args:
            reason: Short cause, for logs and CDR.
            sipfrag: Status line reported on the REFER subscription.
        """
        with self._lock:
            if self._terminal or self.state is TransferState.RECALLING:
                return

            self._cancel_timer()
            self.state = TransferState.ABORTING
            self.abort_reason = reason
            self.pbx.logger.warning(f"Transfer {self.session_id} aborting: {reason}")

            if self._transferor_live():
                # Roll back to the pre-transfer call.
                self._sweep(except_roles={LegRole.TRANSFEROR, LegRole.TRANSFEREE})
                self._restore_original()
                self._notify(sipfrag)
                self._close()
                return

            if self.on_failure is not None:
                # The initiator owns the transferee's leg and handles failure
                # itself (e.g. an auto attendant replaying its menu). The
                # callback must fire even if teardown below raises -- it's a
                # one-shot promise (already cleared from self) callers rely
                # on for their own cleanup, e.g. the queue handler releasing
                # the agent's offering exclusion.
                callback = self.on_failure
                self.on_failure = None
                try:
                    self._sweep(except_roles={LegRole.TRANSFEREE})
                    self._notify(sipfrag)
                    self._close()
                except Exception as exc:
                    self.pbx.logger.error(
                        f"Transfer {self.session_id}: pre-callback teardown raised: {exc}",
                        exc_info=True,
                    )
                try:
                    callback()
                except Exception as exc:
                    self.pbx.logger.error(f"Transfer {self.session_id}: on_failure raised: {exc}")
                return

            if self._recall_eligible():
                self.state = TransferState.RECALLING
                self._sweep(except_roles={LegRole.TRANSFEREE})
                self._notify(sipfrag)
                self._start_recall()
                return

            self._give_up_on_transferee()
            self._notify(sipfrag)
            self._close()

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def _sweep(self, except_roles: set[LegRole] | None = None) -> None:
        """
        Drive every tracked leg to a terminal state.

        Unanswered legs get CANCEL, answered legs get BYE, and any call whose
        legs are all terminated is ended (which also releases its relay and
        closes its CDR). Iterating the roster is what makes "every leg is
        closed" structural: a leg cannot be missed by a call site forgetting
        about it.

        Args:
            except_roles: Roles to leave untouched -- the parties that are
                meant to survive this exit.
        """
        skip = except_roles or set()

        for role, refs in self.legs.items():
            if role in skip:
                continue
            for ref in refs:
                if ref.status is LegStatus.TERMINATED:
                    continue
                self._terminate_leg(ref)

        self._end_finished_calls(skip)

    def _terminate_leg(self, ref: LegRef) -> None:
        """Send the right teardown request for one leg and mark it terminated."""
        call = self.pbx.call_manager.get_call(ref.call_id)
        if call is None:
            ref.status = LegStatus.TERMINATED
            return

        try:
            if ref.status in (LegStatus.PENDING, LegStatus.RINGING):
                self.pbx.call_router._send_cancel_to_callee(call, ref.call_id)
            else:
                self.pbx.sip_server._send_leg_bye(call, side=ref.side)
        except Exception as exc:
            # A failed teardown request must not strand the remaining legs.
            self.pbx.logger.error(
                f"Transfer {self.session_id}: failed to terminate {ref.call_id}/{ref.side}: {exc}"
            )
        finally:
            ref.status = LegStatus.TERMINATED

    def _end_finished_calls(self, skip: set[LegRole]) -> None:
        """End every involved call that no longer has a live leg."""
        live: dict[str, bool] = {}
        for role, refs in self.legs.items():
            for ref in refs:
                still_live = ref.status is not LegStatus.TERMINATED or role in skip
                live[ref.call_id] = live.get(ref.call_id, False) or still_live

        for call_id, still_live in live.items():
            if still_live:
                continue
            if self.pbx.call_manager.get_call(call_id) is None:
                continue
            self.pbx.end_call(call_id)

    def _restore_original(self) -> None:
        """
        Un-park the transferee after a rolled-back transfer.

        Stopping MOH also un-pauses the relay. The transferor's phone sends its
        own re-INVITE to come off hold, so the PBX does not force a state
        change on a dialog the phone still owns.
        """
        self.pbx.moh_system.stop_moh(self.original_call_id)
        original = self.pbx.call_manager.get_call(self.original_call_id)
        if original is not None:
            original.transferred = False
            original.transfer_destination = None

    def _close(self) -> None:
        """Latch the session terminal and detach it from its calls."""
        self._terminal = True
        self.state = TransferState.CLOSED
        self._cancel_timer()
        for call_id in self._involved_call_ids():
            call = self.pbx.call_manager.get_call(call_id)
            if call is not None and call.transfer_session_id == self.session_id:
                call.transfer_session_id = None
        self.pbx.transfer_handler.retire(self)

    # ------------------------------------------------------------------
    # No-answer resolution (transfer.atxfer_no_answer_action)
    # ------------------------------------------------------------------

    def _no_answer_action(self) -> str:
        """What to do with the transferee once nobody can take them back."""
        action = str(self.pbx.config.get("transfer.atxfer_no_answer_action", "drop")).lower()
        return action if action in ("drop", "voicemail") else "drop"

    def _give_up_on_transferee(self) -> None:
        """
        Final resolution once nobody (transferor or recall) can take the
        transferee back: drop them (default) or record a message into the
        transfer destination's mailbox, per ``_no_answer_action()``.

        Leaves ``_notify``/``_close`` to the caller, matching each call
        site's existing sequencing.
        """
        destination = self.destination
        if self._no_answer_action() == "voicemail" and destination:
            self._sweep(except_roles={LegRole.TRANSFEREE})
            if not self._route_transferee_to_voicemail(destination):
                # Nothing usable to record from (call gone, no media) --
                # fall back to dropping so the transferee isn't stranded on
                # a relay nobody is driving.
                self._sweep()
            return
        self._sweep()

    def _route_transferee_to_voicemail(self, destination: str) -> bool:
        """Record a message from the transferee into the destination's mailbox."""
        original = self.pbx.call_manager.get_call(self.original_call_id)
        if original is None:
            return False
        transferee_side = "callee" if self.transferor_side == "caller" else "caller"
        party_rtp = original.caller_rtp if transferee_side == "caller" else original.callee_rtp
        return bool(
            self.pbx.voicemail_handler.record_into_mailbox(
                original,
                self.original_call_id,
                destination,
                party_rtp,
                original.rtp_ports,
                hangup_cause="transfer_no_answer",
            )
        )

    # ------------------------------------------------------------------
    # Recall (Asterisk atxferdropcall=no)
    # ------------------------------------------------------------------

    def _recall_eligible(self) -> bool:
        """Whether a failed transfer should ring the transferor back."""
        if bool(self.pbx.config.get("transfer.atxferdropcall", False)):
            return False
        retries = int(self.pbx.config.get("transfer.atxfercallbackretries", 2))
        if self.recall_attempts >= retries:
            return False
        if self.pbx.call_manager.get_call(self.original_call_id) is None:
            return False
        return bool(self.transferor_extension)

    def _start_recall(self) -> None:
        """Ring the transferor back so they can take the transferee back."""
        self.recall_attempts += 1
        original = self.pbx.call_manager.get_call(self.original_call_id)
        if original is None:
            self._sweep()
            self._close()
            return

        self.pbx.logger.info(
            f"Transfer {self.session_id}: recalling transferor "
            f"{self.transferor_extension} (attempt {self.recall_attempts})"
        )

        recall = self.pbx.call_originator.originate_call(
            self.transferee_extension,
            self.transferor_extension,
            answer_timeout=self._no_answer_timeout(),
            on_answer=self._on_recall_answer,
            on_failure=self._on_recall_failure,
            rtp_ports_override=original.rtp_ports,
        )
        if recall is None:
            self._on_recall_failure(None, "unreachable")
            return

        self.attach_target(recall, transferor_present=False, answered=False)

    def _on_recall_answer(self, recall: Call) -> None:
        """Transferor picked the recall up: bridge them back to the transferee."""
        with self._lock:
            if self._terminal:
                return
            self.state = TransferState.COMPLETING
            self._set_status(LegRole.TARGET, LegStatus.ANSWERED)
            self.complete()

    def _on_recall_failure(self, recall: Call | None, reason: str) -> None:
        """Recall attempt failed: try again, or give up on the transferee."""
        with self._lock:
            if self._terminal:
                return
            self.state = TransferState.ABORTING
            if recall is not None:
                self._set_status(LegRole.TARGET, LegStatus.TERMINATED)

            if self._recall_eligible():
                delay = float(self.pbx.config.get("transfer.atxferloopdelay", 10))
                self.state = TransferState.RECALLING
                timer = threading.Timer(delay, self._start_recall)
                timer.daemon = True
                self.deadline_timer = timer
                timer.start()
                return

            self.pbx.logger.warning(f"Transfer {self.session_id}: recall exhausted ({reason})")
            self._give_up_on_transferee()
            self._close()

    # ------------------------------------------------------------------
    # NOTIFY
    # ------------------------------------------------------------------

    def _notify(self, sipfrag: str) -> None:
        """
        Report transfer progress on the REFER subscription.

        Every exit path calls this, so the transferor's phone is always told
        the transfer ended -- the guarantee whose absence leaves phones stuck
        showing "transferring". At most one final NOTIFY is sent.

        Args:
            sipfrag: Status line for the ``message/sipfrag`` body.
        """
        dialog = self.refer_dialog
        if dialog is None:
            # REST- or IVR-driven transfer: no REFER, so nobody subscribed.
            return

        # send_transfer_notify owns the CSeq counter and the one-final-NOTIFY
        # latch, so that direct callers (a REFER that failed before any session
        # existed) and sessions cannot disagree about the subscription's state.
        is_final = sipfrag not in (NOTIFY_TRYING, NOTIFY_RINGING)
        try:
            self.pbx.sip_server.send_transfer_notify(dialog, sipfrag, terminated=is_final)
        except Exception as exc:
            self.pbx.logger.error(f"Transfer {self.session_id}: NOTIFY failed: {exc}")

    def notify_accepted(self) -> None:
        """Send the initial ``100 Trying`` for a freshly accepted REFER."""
        self._notify(NOTIFY_TRYING)

    def notify_failure(self, sipfrag: str = NOTIFY_NOT_FOUND) -> None:
        """Send a final failure NOTIFY for a transfer that never started."""
        self._notify(sipfrag)

    # ------------------------------------------------------------------
    # Watchdog
    # ------------------------------------------------------------------

    def arm_watchdog(self, seconds: float) -> None:
        """
        Bound how long the transfer may wait on the target.

        Without this a target that never answers leaves the transferee parked
        on hold and the target leg ringing indefinitely.

        Args:
            seconds: How long to wait before aborting.
        """
        with self._lock:
            self._cancel_timer()
            if self._terminal:
                return
            timer = threading.Timer(seconds, self._on_watchdog)
            timer.daemon = True
            self.deadline_timer = timer
            timer.start()

    def _on_watchdog(self) -> None:
        """Watchdog fired: the target never answered."""
        self.abort("no_answer", sipfrag=NOTIFY_TIMEOUT)

    def _cancel_timer(self) -> None:
        """Cancel any armed watchdog."""
        if self.deadline_timer is not None:
            self.deadline_timer.cancel()
            self.deadline_timer = None

    def _no_answer_timeout(self) -> int:
        """Seconds to wait for the transfer target (Asterisk atxfernoanswertimeout)."""
        return int(self.pbx.config.get("transfer.atxfernoanswertimeout", 15))

    # ------------------------------------------------------------------
    # Roster helpers
    # ------------------------------------------------------------------

    def role_for(self, call_id: str, side: str) -> LegRole | None:
        """
        Identify which party a ``(call_id, side)`` leg belongs to.

        Args:
            call_id: The call the leg belongs to.
            side: "caller" or "callee".

        Returns:
            The role, or None if this session does not track that leg.
        """
        for role, refs in self.legs.items():
            for ref in refs:
                if ref.call_id == call_id and ref.side == side:
                    return role
        return None

    def role_for_address(self, call: Call, addr: tuple[str, int]) -> LegRole | None:
        """
        Identify a party by the SIP source address a request arrived from.

        Args:
            call: The call the request's Call-ID resolved to.
            addr: Source address of the request.

        Returns:
            The role, or None if the address matches neither side.
        """
        if call.caller_addr == addr:
            return self.role_for(call.call_id, "caller")
        if call.callee_addr == addr:
            return self.role_for(call.call_id, "callee")
        return None

    def _target_ref(self) -> LegRef | None:
        """The target's leg, if one has been attached."""
        refs = self.legs.get(LegRole.TARGET) or []
        return refs[0] if refs else None

    def _set_status(self, role: LegRole, status: LegStatus) -> None:
        """Mark every leg of a role with a status."""
        for ref in self.legs.get(role, []):
            ref.status = status

    def _null_leg_address(self, ref: LegRef) -> None:
        """Forget a departed party's SIP address on its record."""
        call = self.pbx.call_manager.get_call(ref.call_id)
        if call is None:
            return
        if ref.side == "caller":
            call.caller_addr = None
        else:
            call.callee_addr = None

    def _transferor_live(self) -> bool:
        """Whether the transferor still has a leg up that we can fall back to."""
        for ref in self.legs.get(LegRole.TRANSFEROR, []):
            if ref.status is LegStatus.TERMINATED:
                continue
            call = self.pbx.call_manager.get_call(ref.call_id)
            if call is None:
                continue
            addr = call.caller_addr if ref.side == "caller" else call.callee_addr
            if addr is not None:
                return True
        return False

    def _involved_call_ids(self) -> set[str]:
        """Every call this session touches."""
        return {ref.call_id for refs in self.legs.values() for ref in refs}

    @property
    def is_terminal(self) -> bool:
        """Whether the session has finished and can be retired."""
        return self._terminal
