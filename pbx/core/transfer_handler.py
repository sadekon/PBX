"""
Call transfer handler for PBX Core.

Owns the registry of in-flight :class:`~pbx.core.transfer_session.TransferSession`
objects and the media work a transfer performs. The state machine itself lives
in ``pbx/core/transfer_session.py``; this module is the entry surface every
transfer initiator funnels through, whichever way the transfer was requested:

- **SIP REFER** (RFC 3515/3891) from a phone, dispatched by ``pbx/sip/server.py``
- **REST API**, via ``POST /api/calls/<id>/transfer``
- **Internal callers** such as the auto attendant and the operator console

All of them use the same three methods -- :meth:`TransferHandler.start_transfer`,
:meth:`TransferHandler.complete_transfer` and :meth:`TransferHandler.on_leg_event`
-- so there is exactly one implementation of "what happens when a transfer
starts, finishes, or falls apart".
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pbx.core.transfer_session import (
    NOTIFY_NOT_FOUND,
    NOTIFY_UNAVAILABLE,
    LegEvent,
    LegEventResult,
    LegRole,
    LegStatus,
    ReferDialog,
    TransferMode,
    TransferSession,
    TransferState,
)
from pbx.features.webhooks import WebhookEvent

if TYPE_CHECKING:
    from collections.abc import Callable

    from pbx.core.call import Call


class TransferHandler:
    """Registry and media plumbing for blind, attended, and consultative transfers."""

    #: How often the reaper checks for sessions that outlived their state.
    REAP_INTERVAL_SECONDS = 5.0

    def __init__(self, pbx_core: Any) -> None:
        """
        Initialize TransferHandler with a reference to PBXCore.

        Args:
            pbx_core: The PBXCore instance.
        """
        self.pbx_core: Any = pbx_core
        self.sessions: dict[str, TransferSession] = {}
        self._lock = threading.RLock()
        self._reaper: threading.Thread | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------
    # Entry point 1: start a transfer
    # ------------------------------------------------------------------

    def start_transfer(
        self,
        original: Call,
        destination: str | None = None,
        *,
        mode: TransferMode,
        transferor_side: str,
        refer_dialog: ReferDialog | None = None,
        existing_consult: Call | None = None,
        referred_by: str | None = None,
        on_failure: Callable[[], None] | None = None,
    ) -> TransferSession | None:
        """
        Begin transferring `original`'s far party to `destination`.

        Covers every way a transfer can start. With `existing_consult` the
        transferor's phone already established the leg to the target (an
        attended transfer, REFER with Replaces) and the PBX adopts it; without
        one the PBX originates the target leg itself.

        Args:
            original: Call whose remaining party is being transferred.
            destination: Target extension. Required unless `existing_consult`
                is supplied.
            mode: Blind or attended.
            transferor_side: Which side of `original` the transferor occupies
                ("caller" or "callee").
            refer_dialog: REFER subscription to report progress on, if the
                transfer was signaled over SIP.
            existing_consult: An already-established leg to the target.
            referred_by: Referred-By header to pass to the target, if any.
            on_failure: Invoked instead of dropping the transferee if the
                transfer fails -- for initiators with no phone to recall.

        Returns:
            The session, or None if the transfer could not be started (in
            which case `original` is left untouched).
        """
        pbx = self.pbx_core

        existing = self.session_for(original)
        if existing is not None and not existing.is_terminal:
            # A genuine double attempt racing the first. Overwriting would
            # orphan the first target leg, left ringing with nothing to bridge.
            pbx.logger.warning(
                f"Transfer already in progress on call {original.call_id}; rejecting"
            )
            self.fail_before_start(refer_dialog, NOTIFY_UNAVAILABLE)
            return None

        # A REFER can arrive on a leg that was itself bridged by an earlier
        # transfer. Such a record holds the transferor's dialog but no relay --
        # the media, and the real transferee, live on the record it is
        # cross-linked to. Resolve that here so the session is built around the
        # record that actually owns the call.
        stale_peer: Call | None = None
        if (
            pbx.rtp_relay.get_handler(original.call_id) is None
            and original.bridged_peer_call_id
            and original.bridge_peer_side
        ):
            relay_owner = pbx.call_manager.get_call(original.bridged_peer_call_id)
            if relay_owner is not None and pbx.rtp_relay.get_handler(relay_owner.call_id):
                pbx.logger.info(
                    f"Transfer REFER arrived on peer leg {original.call_id}; "
                    f"using relay owner {relay_owner.call_id}"
                )
                stale_peer = original
                transferor_side = "caller" if stale_peer.bridge_peer_side == "a" else "callee"
                original = relay_owner

        session = TransferSession.create(
            pbx,
            original,
            mode=mode,
            transferor_side=transferor_side,
            destination=destination,
            refer_dialog=refer_dialog,
            on_failure=on_failure,
        )

        if stale_peer is not None:
            # The transferor's real dialog is on the stale record: only one of
            # its sides still holds an address (the other was nulled by the
            # earlier bridge), and that is the one to hang up.
            side = "callee" if stale_peer.callee_addr else "caller"
            session.add_transferor_leg(stale_peer, side)

        started = (
            self._adopt_consult(session, original, existing_consult)
            if existing_consult is not None
            else self._originate_target(session, original, destination, referred_by)
        )
        if not started:
            # Nothing was torn down, so the call is exactly as it was and the
            # transferor stays connected -- but their phone still has to be
            # told, or it sits on a "transferring" indicator forever.
            self._detach(session)
            self.fail_before_start(refer_dialog, NOTIFY_NOT_FOUND)
            return None

        self._register(session)
        return session

    def _adopt_consult(self, session: TransferSession, original: Call, consult: Call) -> bool:
        """
        Adopt a consultation leg the transferor's phone already established.

        The REFER is itself the commit, so an already-answered target bridges
        immediately and a still-ringing one bridges as soon as it answers.

        Args:
            session: The session being started.
            original: The call being transferred.
            consult: The transferor's existing leg to the target.

        Returns:
            True if the transfer was adopted.
        """
        if consult.call_id == original.call_id:
            self.pbx_core.logger.error(
                f"Transfer consult {consult.call_id} is the call being transferred"
            )
            return False

        answered = bool(consult.callee_rtp)
        session.attach_target(consult, transferor_present=True, answered=answered)
        session.destination = consult.to_extension

        if answered:
            session.complete()
        else:
            session.state = TransferState.COMPLETING
            session.arm_watchdog(session._no_answer_timeout())
            # Report progress rather than going silent: the transferor is
            # committed and may hang up now, and their phone should show the
            # transfer still working rather than nothing at all.
            session.on_event(LegRole.TARGET, LegEvent.RINGING)
            self.pbx_core.logger.info(
                f"Transfer bridge deferred until {consult.call_id} is answered"
            )
        return True

    def _originate_target(
        self,
        session: TransferSession,
        original: Call,
        destination: str | None,
        referred_by: str | None,
    ) -> bool:
        """
        Originate the leg to the transfer target from the PBX.

        For a blind transfer the INVITE advertises the original call's relay
        port directly, so once the target answers its media is already on the
        surviving relay and no re-INVITE is needed to move it.

        An attended transfer started this way (rather than adopted from a
        phone's own consultation leg) instead parks the original call and gives
        the target its own relay, so the transfer can be completed later by
        :meth:`complete_transfer`.

        .. note::
           This attended variant carries over a long-standing limitation of the
           REST consultation endpoint it replaces: it establishes the target
           leg and its relay, but nothing joins the transferor's audio to that
           relay, so there is no consultation audio path. It is signaling-
           complete and media-incomplete, and no in-repo client drives it.
           Phone-signaled attended transfers are unaffected -- they arrive with
           `existing_consult` and never take this path.

        Args:
            session: The session being started.
            original: The call being transferred.
            destination: Target extension.
            referred_by: Referred-By header to pass along, if any.

        Returns:
            True if the target leg was originated.
        """
        from pbx.sip.message import SIPMessageBuilder
        from pbx.sip.sdp import SDPBuilder

        pbx = self.pbx_core

        if not destination:
            pbx.logger.error("Transfer requested with no destination")
            return False

        if not pbx.extension_registry.is_registered(destination):
            pbx.logger.error(f"Transfer destination {destination} not registered")
            return False

        dest_addr = pbx.extension_registry.get_address(destination)
        if not dest_addr or not original.rtp_ports:
            pbx.logger.error(f"No address or relay for transfer to {destination}")
            return False

        transferee_ext = session.transferee_extension
        transferee_rtp = (
            original.callee_rtp if session.transferor_side == "caller" else original.caller_rtp
        )

        consult_call_id = str(uuid.uuid4())
        consult = pbx.call_manager.create_call(consult_call_id, transferee_ext, destination)
        consult.start()

        if session.mode is TransferMode.ATTENDED:
            # Park the transferee and give the consultation its own media.
            pbx.hold_call(original.call_id)
            consult_ports = pbx.rtp_relay.allocate_relay(consult_call_id)
            if not consult_ports:
                pbx.logger.error("Failed to allocate RTP relay for consultation call")
                pbx.call_manager.end_call(consult_call_id)
                pbx.resume_call(original.call_id)
                return False
            consult.rtp_ports = consult_ports
        else:
            consult.rtp_ports = original.rtp_ports
            consult.uses_peer_relay = True

        server_ip = pbx._get_server_ip()
        sip_port = pbx.config.get("server.sip_port", 5060)

        protocol = "RTP/AVP"
        crypto: list[str] | None = None
        if transferee_rtp:
            protocol = transferee_rtp.get("protocol", "RTP/AVP")
            crypto = transferee_rtp.get("crypto") or None

        invite_sdp = SDPBuilder.build_audio_sdp(
            server_ip,
            consult.rtp_ports[0],
            session_id=consult_call_id,
            protocol=protocol,
            crypto=crypto,
        )

        invite_msg = SIPMessageBuilder.build_request(
            method="INVITE",
            uri=f"sip:{destination}@{dest_addr[0]}:{dest_addr[1]}",
            from_addr=f"<sip:{transferee_ext}@{server_ip}>",
            to_addr=f"<sip:{destination}@{server_ip}>",
            call_id=consult_call_id,
            cseq=1,
            body=invite_sdp,
        )
        if referred_by:
            invite_msg.set_header("Referred-By", referred_by)
        invite_msg.set_header("Contact", f"<sip:{transferee_ext}@{server_ip}:{sip_port}>")
        invite_msg.set_header("Content-type", "application/sdp")
        # Via and Max-Forwards are mandatory (RFC 3261 SS8.1.1.6-7); some
        # strict SIP stacks silently drop requests missing them.
        branch_id = str(uuid.uuid4()).replace("-", "")
        invite_msg.set_header(
            "Via", f"SIP/2.0/UDP {server_ip}:{sip_port};branch=z9hG4bK{branch_id}"
        )
        invite_msg.set_header("Max-Forwards", "70")
        consult.callee_invite = invite_msg
        # Record where the INVITE went, exactly as CallRouter does for a normal
        # outbound leg. Without it a CANCEL has no destination, so aborting the
        # transfer leaves the target's phone ringing forever with no call
        # behind it. Answered-ness is tracked by callee_rtp, not this.
        consult.callee_addr = dest_addr

        pbx.sip_server._send_message(invite_msg.build(), dest_addr)
        pbx.cdr_system.start_record(consult_call_id, transferee_ext, destination)

        session.attach_target(consult, transferor_present=False, answered=False)
        session.destination = destination
        # A blind transfer is committed the moment it starts, so the answer
        # bridges straight through. An attended one waits in INITIATING for an
        # explicit complete_transfer() after the consultation.
        if session.mode is TransferMode.BLIND:
            session.state = TransferState.COMPLETING
        session.arm_watchdog(session._no_answer_timeout())

        pbx.logger.info(
            f"Transfer leg originated: {consult_call_id} ({transferee_ext} -> {destination})"
        )
        return True

    # ------------------------------------------------------------------
    # Entry point 2: complete a transfer
    # ------------------------------------------------------------------

    def complete_transfer(self, session: TransferSession) -> bool:
        """
        Commit a transfer whose target has answered.

        Args:
            session: The session to complete.

        Returns:
            True if the bridge was established.
        """
        return session.complete()

    # ------------------------------------------------------------------
    # Entry point 3: leg events
    # ------------------------------------------------------------------

    def on_leg_event(
        self,
        call: Call,
        event: LegEvent,
        *,
        addr: tuple[str, int] | None = None,
        side: str | None = None,
    ) -> LegEventResult:
        """
        Report something that happened to one leg of a call.

        This is the single place the SIP and routing layers hand transfer
        decisions off to, replacing the per-call-site checks that used to
        re-derive transfer state independently.

        Args:
            call: The call the event concerns.
            event: What happened.
            addr: SIP source address of the triggering request, when the party
                must be identified by where the message came from (e.g. BYE).
            side: Which side of `call`, when it is already known (e.g. an
                answer on the callee leg).

        Returns:
            What the caller should do with the triggering message. FORWARD if
            no transfer is in progress, so normal handling applies.
        """
        session = self.session_for(call)
        if session is None:
            return LegEventResult.FORWARD

        role: LegRole | None = None
        if side is not None:
            role = session.role_for(call.call_id, side)
        if role is None and addr is not None:
            role = session.role_for_address(call, addr)

        if role is None:
            # The request came from an address matching neither side: a stale
            # leg from a party the transfer has already moved past.
            self.pbx_core.logger.info(
                f"Transfer {session.session_id}: ignoring {event.value} from "
                f"an untracked leg of {call.call_id}"
            )
            return LegEventResult.ABSORB

        return session.on_event(role, event)

    # ------------------------------------------------------------------
    # Media: the bridge
    # ------------------------------------------------------------------

    def bridge(self, session: TransferSession, original: Call, consult: Call) -> bool:
        """
        Re-point media so the transferee and the target are talking.

        Both surviving legs keep their own Call record -- each party's own SIP
        dialog must keep resolving to a live record -- cross-linked via
        bridged_peer_call_id. Media flows through the original call's relay
        only: the transferor's side of that relay is replaced with the target's
        endpoint, and the target is re-INVITEd onto the original relay's port
        unless its leg was PBX-originated and already advertises it.

        Hanging up the transferor's legs is deliberately *not* done here -- the
        session's sweep owns all teardown, so that every exit path closes legs
        the same way.

        Args:
            session: The transfer being completed.
            original: The call whose relay carries the bridged media.
            consult: The target's leg (must have answered).

        Returns:
            True if the bridge completed.
        """
        from pbx.core.call import CallState

        pbx = self.pbx_core

        dest_rtp = consult.callee_rtp
        if not dest_rtp:
            pbx.logger.error(
                f"Cannot bridge transfer: call {consult.call_id} has no destination media"
            )
            return False

        transferor_is_caller = session.transferor_side == "caller"
        transferor_relay_side = "a" if transferor_is_caller else "b"

        pbx.logger.info(
            f"Bridging transfer: call {original.call_id} "
            f"({'callee' if transferor_is_caller else 'caller'} leg kept) -> "
            f"{consult.to_extension} (target leg {consult.call_id})"
        )

        # Un-park the transferee: stopping MOH also un-pauses the relay.
        pbx.moh_system.stop_moh(original.call_id)

        # Retarget the original relay's transferor side to the destination.
        dest_endpoint = (dest_rtp["address"], dest_rtp["port"])
        pbx.rtp_relay.replace_endpoint(original.call_id, transferor_relay_side, dest_endpoint)

        # Rewrite the transferor's side of the original record to the target.
        if transferor_is_caller:
            original.from_extension = consult.to_extension
            original.caller_addr = None
            original.caller_rtp = dest_rtp
        else:
            original.to_extension = consult.to_extension
            original.callee_addr = None
            original.callee_rtp = dest_rtp

        original.state = CallState.CONNECTED
        original.on_hold = False
        original.held_by = None
        original.transferred = True
        original.transfer_destination = consult.to_extension

        consult.state = CallState.CONNECTED
        consult.transferred = True
        consult.caller_addr = None
        consult.caller_rtp = None

        original.bridged_peer_call_id = consult.call_id
        consult.bridged_peer_call_id = original.call_id
        consult.bridge_peer_side = transferor_relay_side

        # Move the destination's media onto the original relay's port. A
        # PBX-originated leg already advertised that port in its INVITE, so no
        # re-INVITE is needed there.
        if not consult.uses_peer_relay:
            self._send_bridge_reinvite(original, consult)
            pbx.rtp_relay.release_relay(consult.call_id)

        pbx.logger.info(
            f"Transfer bridge complete: {original.from_extension} <-> "
            f"{original.to_extension} on relay of call {original.call_id}"
        )

        pbx.webhook_system.trigger_event(
            WebhookEvent.CALL_TRANSFERRED,
            {
                "call_id": original.call_id,
                "consultation_call_id": consult.call_id,
                "from_extension": original.from_extension,
                "transfer_destination": consult.to_extension,
                "transfer_type": session.mode.value,
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )
        return True

    def _send_bridge_reinvite(self, original: Call, consult: Call) -> None:
        """
        Re-INVITE the transfer destination onto the original call's relay.

        The destination negotiated media toward its own leg's relay port; after
        the bridge, media flows through the original call's relay, so the
        destination must be re-INVITEd (in its own dialog, on its own Call-ID)
        with SDP advertising the surviving relay port. The 200 OK lands in
        handle_callee_answer's already-connected branch, which refreshes the
        bridged peer relay's endpoint.

        Args:
            original: Surviving call whose relay carries the bridged media.
            consult: The destination's leg.
        """
        from pbx.sip.message import SIPMessageBuilder
        from pbx.sip.sdp import SDPBuilder

        pbx = self.pbx_core

        dest_addr = consult.callee_addr
        dest_rtp = consult.callee_rtp
        if not dest_addr or not original.rtp_ports or not dest_rtp:
            return

        server_ip = pbx._get_server_ip()
        sip_port = pbx.config.get("server.sip_port", 5060)

        # Dialog identity for the PBX->destination leg: From as sent in the
        # INVITE (correct tag), To as returned in the destination's 200 OK.
        source_invite = getattr(consult, "callee_invite", None) or consult.original_invite
        from_header = source_invite.get_header("From") if source_invite else None
        to_header = consult.callee_dialog_to or (
            source_invite.get_header("To") if source_invite else None
        )

        # Mirror the codecs/protocol the destination already negotiated.
        user_agent = pbx._get_phone_user_agent(consult.to_extension)
        phone_model = pbx._detect_phone_model(user_agent)
        codecs = pbx._get_compatible_codecs(phone_model, dest_rtp.get("formats"))

        reinvite_sdp = SDPBuilder.build_audio_sdp(
            server_ip,
            original.rtp_ports[0],
            session_id=consult.call_id,
            codecs=codecs,
            dtmf_payload_type=pbx._get_dtmf_payload_type(),
            ilbc_mode=pbx._get_ilbc_mode(),
            protocol=dest_rtp.get("protocol", "RTP/AVP"),
            crypto=dest_rtp.get("crypto") or None,
            rtpmap_overrides=dest_rtp.get("rtpmap_names") or None,
        )

        consult.pbx_leg_cseq += 1
        reinvite = SIPMessageBuilder.build_request(
            method="INVITE",
            uri=f"sip:{consult.to_extension}@{dest_addr[0]}:{dest_addr[1]}",
            from_addr=from_header or f"<sip:{consult.from_extension}@{server_ip}>",
            to_addr=to_header or f"<sip:{consult.to_extension}@{server_ip}>",
            call_id=consult.call_id,
            cseq=consult.pbx_leg_cseq,
            body=reinvite_sdp,
        )
        branch_id = str(uuid.uuid4()).replace("-", "")
        reinvite.set_header("Via", f"SIP/2.0/UDP {server_ip}:{sip_port};branch=z9hG4bK{branch_id}")
        reinvite.set_header("Contact", f"<sip:{consult.from_extension}@{server_ip}:{sip_port}>")
        reinvite.set_header("Content-type", "application/sdp")
        reinvite.set_header("Max-Forwards", "70")

        pbx.sip_server._send_message(reinvite.build(), dest_addr)
        pbx.logger.info(
            f"Sent bridge re-INVITE to {consult.to_extension} at {dest_addr} "
            f"(relay port {original.rtp_ports[0]})"
        )

    # ------------------------------------------------------------------
    # Session registry
    # ------------------------------------------------------------------

    def session_for(self, call: Call) -> TransferSession | None:
        """
        Look up the transfer a call is participating in.

        Args:
            call: The call to look up.

        Returns:
            The session, or None if this call is not in a transfer.
        """
        session_id = getattr(call, "transfer_session_id", None)
        if not session_id:
            return None
        with self._lock:
            return self.sessions.get(session_id)

    def _register(self, session: TransferSession) -> None:
        """Track a session and make sure the reaper is running."""
        with self._lock:
            if not session.is_terminal:
                self.sessions[session.session_id] = session
        self._ensure_reaper()

    def retire(self, session: TransferSession) -> None:
        """
        Drop a finished session from the registry.

        Args:
            session: The session that has reached a terminal state.
        """
        with self._lock:
            self.sessions.pop(session.session_id, None)

    def _detach(self, session: TransferSession) -> None:
        """Unlink a session that never started from the calls it touched."""
        for call_id in session._involved_call_ids():
            call = self.pbx_core.call_manager.get_call(call_id)
            if call is not None and call.transfer_session_id == session.session_id:
                call.transfer_session_id = None
        self.retire(session)

    def fail_before_start(
        self, refer_dialog: ReferDialog | None, sipfrag: str = NOTIFY_NOT_FOUND
    ) -> None:
        """
        Report a transfer that could not be started at all.

        The call being transferred is left exactly as it was, so the transferor
        stays connected -- but their phone still has to be told, or it sits on
        a "transferring" indicator forever.

        Args:
            refer_dialog: The subscription to notify, if the transfer was
                signaled over SIP.
            sipfrag: Status line to report.
        """
        if refer_dialog is None:
            return
        try:
            self.pbx_core.sip_server.send_transfer_notify(refer_dialog, sipfrag, terminated=True)
        except Exception as exc:
            self.pbx_core.logger.error(f"Failed to send transfer failure NOTIFY: {exc}")

    # ------------------------------------------------------------------
    # Reaper
    # ------------------------------------------------------------------

    def _ensure_reaper(self) -> None:
        """Start the reaper thread if it is not already running."""
        with self._lock:
            if self._reaper is not None and self._reaper.is_alive():
                return
            self._stop.clear()
            self._reaper = threading.Thread(
                target=self._reap_loop, name="transfer-reaper", daemon=True
            )
            self._reaper.start()

    def _reap_loop(self) -> None:
        """
        Force-resolve transfers that outlived their state.

        Nothing should reach the reaper: every failure mode has a watchdog and
        every exit path closes its own session. It exists so that a bug in one
        of those paths degrades into a late teardown rather than a call leg
        that survives forever and a phone stuck showing "transferring".
        """
        while not self._stop.wait(self.REAP_INTERVAL_SECONDS):
            with self._lock:
                if not self.sessions:
                    self._reaper = None
                    return
                sessions = list(self.sessions.values())

            for session in sessions:
                try:
                    self._reap(session)
                except Exception as exc:
                    self.pbx_core.logger.error(
                        f"Transfer reaper failed on {session.session_id}: {exc}"
                    )

    def _reap(self, session: TransferSession) -> None:
        """Retire or force-abort one session if it can no longer make progress."""
        if session.is_terminal:
            self.retire(session)
            return

        # Waiting on something with a live watchdog is normal.
        timer = session.deadline_timer
        if timer is not None and timer.is_alive():
            return
        if session.state is TransferState.CONSULTING:
            # Bounded by the transferor's own call, not by us.
            return

        # Every remaining party gone means nothing can complete this transfer.
        if all(
            self.pbx_core.call_manager.get_call(call_id) is None
            for call_id in session._involved_call_ids()
        ):
            self.pbx_core.logger.warning(
                f"Reaping transfer {session.session_id}: all legs already gone"
            )
            session.abort("all_legs_gone")
            return

        target = session.legs.get(LegRole.TARGET) or []
        if session.state is TransferState.COMPLETING and not any(
            ref.status is LegStatus.ANSWERED for ref in target
        ):
            self.pbx_core.logger.warning(
                f"Reaping transfer {session.session_id}: stuck in {session.state.value} "
                "with no armed watchdog"
            )
            session.abort("stalled")

    def shutdown(self) -> None:
        """Stop the reaper thread."""
        self._stop.set()
        reaper = self._reaper
        if reaper is not None and reaper.is_alive():
            reaper.join(timeout=self.REAP_INTERVAL_SECONDS + 1)
        self._reaper = None
