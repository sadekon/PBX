"""
Call transfer handler for PBX Core.

Extracts the full transfer machinery from PBXCore into a dedicated class.
Two families of entry point converge here:

- **SIP REFER / IVR transfers** (`bridge_attended_transfer`,
  `start_blind_refer_transfer`, `abort_pending_transfer`): driven by a phone
  sending REFER (RFC 3515/3891, handled in ``pbx/sip/server.py``) or by the
  auto attendant handing a caller off. These share the relay-bridge model,
  keeping both surviving legs alive and cross-linked.
- **REST-API transfers** (`blind_transfer`, `attended_transfer`,
  `consultation_transfer_start`): driven by ``POST /api/calls/<id>/transfer``
  where there is no SIP REFER to carry the transfer intent, so a client
  supplies the call ids directly.
"""

import threading
import uuid
from datetime import UTC, datetime
from typing import Any

from pbx.features.webhooks import WebhookEvent


class TransferHandler:
    """Handles blind, attended, and consultative call transfers."""

    def __init__(self, pbx_core: Any) -> None:
        """
        Initialize TransferHandler with a reference to PBXCore.

        Args:
            pbx_core: The PBXCore instance.
        """
        self.pbx_core: Any = pbx_core

    def blind_transfer(self, call_id: str, destination: str) -> bool:
        """
        Perform blind (unattended) transfer, driven by the REST API.

        Entry point for ``POST /api/calls/<id>/transfer`` (type ``blind``) and
        the operator console's transfer action -- i.e. a programmatic caller
        that hands the PBX a call id and a destination rather than signaling a
        transfer over SIP. A phone that signals its own blind transfer via
        REFER is handled instead by :meth:`start_blind_refer_transfer`.

        The transferring party's call leg is immediately terminated and the
        remaining party is connected to the new destination via a new INVITE.

        Args:
            call_id: Call identifier
            destination: Destination extension to transfer to

        Returns:
            True if transfer initiated successfully
        """
        from pbx.sip.message import SIPMessageBuilder
        from pbx.sip.sdp import SDPBuilder

        pbx = self.pbx_core

        call = pbx.call_manager.get_call(call_id)
        if not call:
            pbx.logger.error(f"Call {call_id} not found for blind transfer")
            return False

        if not pbx.extension_registry.is_registered(destination):
            pbx.logger.error(f"Blind transfer destination {destination} not registered")
            return False

        dest_addr = pbx.extension_registry.get_address(destination)
        if not dest_addr:
            pbx.logger.error(f"No address for blind transfer destination {destination}")
            return False

        pbx.logger.info(f"Blind transfer: call {call_id} to {destination}")

        # Set call state to transferring
        from pbx.core.call import CallState

        call.state = CallState.TRANSFERRING
        call.transferred = True
        call.transfer_destination = destination

        # Build new INVITE to the transfer destination
        server_ip = pbx._get_server_ip()
        sip_port = pbx.config.get("server.sip_port", 5060)
        new_call_id = str(uuid.uuid4())

        invite_msg = SIPMessageBuilder.build_request(
            method="INVITE",
            uri=f"sip:{destination}@{dest_addr[0]}:{dest_addr[1]}",
            from_addr=f"<sip:{call.from_extension}@{server_ip}>",
            to_addr=f"<sip:{destination}@{server_ip}>",
            call_id=new_call_id,
            cseq=1,
        )
        invite_msg.set_header("Contact", f"<sip:{call.from_extension}@{server_ip}:{sip_port}>")

        # Include SDP with the existing RTP relay ports
        if call.rtp_ports:
            transfer_protocol = "RTP/AVP"
            transfer_crypto: list[str] | None = None
            if call.caller_rtp:
                transfer_protocol = call.caller_rtp.get("protocol", "RTP/AVP")
                transfer_crypto = call.caller_rtp.get("crypto") or None
            transfer_sdp = SDPBuilder.build_audio_sdp(
                server_ip,
                call.rtp_ports[0],
                session_id=new_call_id,
                protocol=transfer_protocol,
                crypto=transfer_crypto,
            )
            invite_msg.body = transfer_sdp
            invite_msg.set_header("Content-type", "application/sdp")
            invite_msg.set_header("Content-Length", str(len(transfer_sdp.encode("utf-8"))))

        # Send INVITE to new destination
        pbx.sip_server._send_message(invite_msg.build(), dest_addr)

        # Create new call record for the transferred leg
        new_call = pbx.call_manager.create_call(new_call_id, call.from_extension, destination)
        new_call.start()
        new_call.caller_rtp = call.caller_rtp
        new_call.caller_addr = call.caller_addr
        new_call.rtp_ports = call.rtp_ports

        # Transfer RTP relay ownership from old call to new call before ending
        # the old call (end_call releases the relay, which would kill audio)
        relay_info = pbx.rtp_relay.active_relays.pop(call_id, None)
        if relay_info:
            pbx.rtp_relay.active_relays[new_call_id] = relay_info

        # Send BYE to the transferring party (the party that initiated transfer)
        if call.callee_addr:
            bye_msg = SIPMessageBuilder.build_request(
                method="BYE",
                uri=f"sip:{call.to_extension}@{call.callee_addr[0]}:{call.callee_addr[1]}",
                from_addr=f"<sip:{call.from_extension}@{server_ip}>",
                to_addr=f"<sip:{call.to_extension}@{server_ip}>",
                call_id=call_id,
                cseq=2,
            )
            pbx.sip_server._send_message(bye_msg.build(), call.callee_addr)

        # End the original call (relay already transferred, so release_relay is a no-op)
        pbx.call_manager.end_call(call_id)
        pbx.cdr_system.end_record(call_id, hangup_cause="blind_transfer")

        pbx.logger.info(f"Blind transfer complete: {call_id} -> {destination}")

        # Trigger webhook
        pbx.webhook_system.trigger_event(
            WebhookEvent.CALL_TRANSFERRED,
            {
                "call_id": call_id,
                "new_call_id": new_call_id,
                "from_extension": call.from_extension,
                "to_extension": call.to_extension,
                "transfer_destination": destination,
                "transfer_type": "blind",
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )

        return True

    def attended_transfer(self, call_id: str, consultation_call_id: str) -> bool:
        """
        Complete an attended (consultative) transfer, driven by the REST API.

        Entry point for ``POST /api/calls/<id>/transfer`` (type ``attended``),
        the completion step for a consultation started via
        :meth:`consultation_transfer_start`. This is the counterpart of a
        phone-signaled attended transfer, where the REFER's ``Replaces`` header
        already identifies the consultation dialog and the bridge is performed
        by :meth:`bridge_attended_transfer` instead; here a programmatic caller
        supplies the two call ids directly because there is no REFER.

        Note: no in-repo client currently calls this endpoint (no admin UI or
        softphone drives it); it exists as a REST surface for external/
        programmatic transfer clients.

        The transferring party has already established a consultation call with
        the transfer destination. This method bridges the original caller with
        the consultation call's far end.

        Args:
            call_id: Original call identifier (caller on hold)
            consultation_call_id: Consultation call identifier

        Returns:
            True if transfer completed successfully
        """
        from pbx.sip.message import SIPMessageBuilder

        pbx = self.pbx_core

        original_call = pbx.call_manager.get_call(call_id)
        consult_call = pbx.call_manager.get_call(consultation_call_id)

        if not original_call:
            pbx.logger.error(f"Original call {call_id} not found for attended transfer")
            return False

        if not consult_call:
            pbx.logger.error(
                f"Consultation call {consultation_call_id} not found for attended transfer"
            )
            return False

        pbx.logger.info(
            f"Attended transfer: bridging {original_call.from_extension} "
            f"with {consult_call.to_extension}"
        )

        from pbx.core.call import CallState

        original_call.state = CallState.TRANSFERRING
        original_call.transferred = True
        original_call.transfer_destination = consult_call.to_extension

        # Re-point the RTP relay: connect original caller with consultation
        # call's far end
        server_ip = pbx._get_server_ip()

        if original_call.caller_rtp and consult_call.callee_rtp:
            caller_endpoint = (
                original_call.caller_rtp["address"],
                original_call.caller_rtp["port"],
            )
            new_dest_endpoint = (
                consult_call.callee_rtp["address"],
                consult_call.callee_rtp["port"],
            )

            # Update the RTP relay for the original call to point to new
            # destination
            if original_call.rtp_ports:
                pbx.rtp_relay.set_endpoints(call_id, caller_endpoint, new_dest_endpoint)
                pbx.logger.info(f"RTP relay re-bridged: {caller_endpoint} <-> {new_dest_endpoint}")

        # Resume the original call from hold
        original_call.state = CallState.CONNECTED
        original_call.on_hold = False
        original_call.held_by = None
        original_call.to_extension = consult_call.to_extension
        original_call.callee_addr = consult_call.callee_addr
        original_call.callee_rtp = consult_call.callee_rtp

        # Stop MOH in case the original call was held via a phone-initiated
        # re-INVITE (a no-op if MOH was never started for this call).
        pbx.moh_system.stop_moh(call_id)

        # Send BYE to the transferring party (consultation call's A-leg)
        if consult_call.caller_addr:
            bye_msg = SIPMessageBuilder.build_request(
                method="BYE",
                uri=f"sip:{consult_call.from_extension}@{consult_call.caller_addr[0]}:{consult_call.caller_addr[1]}",
                from_addr=f"<sip:{consult_call.to_extension}@{server_ip}>",
                to_addr=f"<sip:{consult_call.from_extension}@{server_ip}>",
                call_id=consultation_call_id,
                cseq=2,
            )
            pbx.sip_server._send_message(bye_msg.build(), consult_call.caller_addr)

        # Release consultation call's RTP relay and end it
        pbx.rtp_relay.release_relay(consultation_call_id)
        pbx.call_manager.end_call(consultation_call_id)
        pbx.cdr_system.end_record(consultation_call_id, hangup_cause="attended_transfer")

        pbx.logger.info(
            f"Attended transfer complete: {original_call.from_extension} "
            f"now connected to {consult_call.to_extension}"
        )

        # Trigger webhook
        pbx.webhook_system.trigger_event(
            WebhookEvent.CALL_TRANSFERRED,
            {
                "call_id": call_id,
                "consultation_call_id": consultation_call_id,
                "from_extension": original_call.from_extension,
                "transfer_destination": consult_call.to_extension,
                "transfer_type": "attended",
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )

        return True

    def bridge_attended_transfer(self, original: Any, consult: Any) -> bool:
        """
        Complete a REFER-based attended transfer by bridging the original
        call's remaining party with the consultation call's destination.

        The transferor (referrer) drops out of both calls. Both Call records
        stay alive -- the original as the transferee's leg, the consultation
        as the destination's leg -- cross-linked via bridged_peer_call_id so
        each party's own SIP dialog keeps resolving to a live record. Media
        flows through the original call's relay only: the transferor's side
        of that relay is replaced with the destination's endpoint, and the
        destination is re-INVITEd onto the original relay's port (unless the
        consultation leg was PBX-originated and already advertises it).

        Args:
            original: The original call (transferee still parked on hold).
            consult: The consultation call to the transfer destination
                (destination must have answered -- callee_rtp set).

        Returns:
            True if the bridge completed.
        """
        from pbx.core.call import CallState

        pbx = self.pbx_core

        dest_rtp = consult.callee_rtp
        if not dest_rtp:
            pbx.logger.error(
                f"Cannot bridge transfer: consultation call {consult.call_id} has no "
                "destination media"
            )
            return False

        # If `original` is itself a peer leg from an earlier bridge, it has
        # no relay of its own (already released when that bridge completed)
        # -- its party's real media lives on the relay-owning record it's
        # cross-linked to. Redirect there before doing anything else, using
        # the same bridged_peer_call_id/bridge_peer_side indirection
        # handle_callee_answer already follows for a re-INVITE arriving on
        # a peer leg's own dialog.
        stale_peer: Any | None = None
        if (
            pbx.rtp_relay.get_handler(original.call_id) is None
            and original.bridged_peer_call_id
            and original.bridge_peer_side
        ):
            relay_owner = pbx.call_manager.get_call(original.bridged_peer_call_id)
            if relay_owner and pbx.rtp_relay.get_handler(relay_owner.call_id) is not None:
                pbx.logger.info(
                    f"Transfer REFER arrived on peer leg {original.call_id}; "
                    f"redirecting to relay owner {relay_owner.call_id}"
                )
                stale_peer, original = original, relay_owner

        # Which side of the original call (and its relay) the transferor
        # occupies. Recorded at REFER time; fall back to the shared-extension
        # heuristic for phone-originated consultation calls.
        if stale_peer is not None:
            transferor_is_caller = stale_peer.bridge_peer_side == "a"
        else:
            transferor_is_caller = original.transfer_referrer_is_caller
            if transferor_is_caller is None:
                transferor_is_caller = original.from_extension in (
                    consult.from_extension,
                    consult.to_extension,
                )
        transferor_side = "a" if transferor_is_caller else "b"

        pbx.logger.info(
            f"Bridging transfer: call {original.call_id} "
            f"({'callee' if transferor_is_caller else 'caller'} leg kept) -> "
            f"{consult.to_extension} (consult {consult.call_id})"
        )

        # Deterministically end both of the transferor's legs: the
        # consultation call, and their original leg (the dialog the REFER
        # itself arrived on, or -- after a peer-leg redirect -- the stale
        # peer record, whichever still holds the transferor's own address).
        # Relying on the transferor's phone to end these on its own does
        # not hold universally: some phones leave one leg's dialog state
        # untouched, appearing permanently connected/on-hold with no way to
        # hang up or resume, even though the PBX has already moved on.
        if consult.caller_addr:
            pbx.sip_server._send_leg_bye(consult, side="caller")

        if stale_peer is not None:
            # Only one side of the stale peer's own record still holds a
            # real address (the other was nulled by the earlier bridge) --
            # auto-detect picks it correctly.
            pbx.sip_server._send_leg_bye(stale_peer)
        else:
            pbx.sip_server._send_leg_bye(
                original, side="caller" if transferor_is_caller else "callee"
            )

        # Un-park the transferee: stop MOH (also un-pauses the relay).
        pbx.moh_system.stop_moh(original.call_id)

        # Retarget the original relay's transferor side to the destination.
        dest_endpoint = (dest_rtp["address"], dest_rtp["port"])
        pbx.rtp_relay.replace_endpoint(original.call_id, transferor_side, dest_endpoint)

        # Rewrite the transferor's side of the original record.
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
        original.pending_transfer_consult_id = None

        consult.state = CallState.CONNECTED
        consult.transferred = True
        consult.is_transfer_consult = False
        consult.caller_addr = None
        consult.caller_rtp = None
        if consult.no_answer_timer:
            consult.no_answer_timer.cancel()
            consult.no_answer_timer = None

        original.bridged_peer_call_id = consult.call_id
        consult.bridged_peer_call_id = original.call_id
        consult.bridge_peer_side = transferor_side

        # Move the destination's media onto the original relay's port. A
        # PBX-originated blind leg already advertised that port in its
        # INVITE, so no re-INVITE is needed there.
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
                "transfer_type": "refer_attended",
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )

        # The redirected-from peer leg no longer represents anyone's live
        # identity -- its party's dialog now resolves through the (possibly
        # new) relay-owning record above. No SIP signaling needed: it has
        # no relay to release, and a stale BYE its former party's phone may
        # still send lands on _handle_bye's existing "call not found" path.
        if stale_peer is not None:
            pbx.call_manager.end_call(stale_peer.call_id)

        return True

    def _send_bridge_reinvite(self, original: Any, consult: Any) -> None:
        """
        Re-INVITE the transfer destination onto the original call's relay.

        The destination negotiated media toward the consultation call's
        relay port; after the bridge, media flows through the original
        call's relay, so the destination must be re-INVITEd (in its own
        dialog on the consultation Call-ID) with SDP advertising the
        surviving relay port. The 200 OK lands in handle_callee_answer's
        already-connected branch, which refreshes the bridged peer relay's
        endpoint.

        Args:
            original: Surviving call whose relay carries the bridged media.
            consult: Consultation call record (destination's leg).
        """
        from pbx.sip.message import SIPMessageBuilder
        from pbx.sip.sdp import SDPBuilder

        pbx = self.pbx_core

        dest_addr = consult.callee_addr
        dest_rtp = consult.callee_rtp
        if not dest_addr or not original.rtp_ports:
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

    def start_blind_refer_transfer(
        self,
        original: Any,
        referrer_is_caller: bool,
        destination: str,
        referrer_addr: tuple[str, int] | None,
        referred_by: str | None = None,
    ) -> bool:
        """
        Start a blind (unattended) REFER transfer: the PBX originates the
        leg to the destination itself, advertising the original call's
        relay port, and defers the bridge until the destination answers.

        Args:
            original: The call whose remaining party is being transferred.
            referrer_is_caller: Whether the referrer occupies the original
                call's caller leg. There need not be a real REFER sender --
                an IVR session (e.g. auto attendant) transferring its own
                single-leg call passes False here, since the caller is
                always the remaining party in that case.
            destination: Destination extension.
            referrer_addr: SIP source address of the REFER sender, or None
                if the transfer was not initiated by a real SIP party (e.g.
                an IVR session transferring its own call). None disables the
                "absorb the referrer's own BYE" handling in
                _handle_bye -- there is no referrer leg whose BYE should be
                silently swallowed, so a real hangup on the remaining leg
                correctly ends the call instead of being mistaken for it.
            referred_by: Referred-By header value to pass along, if any.

        Returns:
            True if the destination leg was originated.
        """
        from pbx.sip.message import SIPMessageBuilder
        from pbx.sip.sdp import SDPBuilder

        pbx = self.pbx_core

        if not pbx.extension_registry.is_registered(destination):
            pbx.logger.error(f"Blind transfer destination {destination} not registered")
            return False

        dest_addr = pbx.extension_registry.get_address(destination)
        if not dest_addr or not original.rtp_ports:
            pbx.logger.error(f"No address or relay for blind transfer to {destination}")
            return False

        if referrer_is_caller:
            transferee_ext = original.to_extension
            transferee_rtp = original.callee_rtp
        else:
            transferee_ext = original.from_extension
            transferee_rtp = original.caller_rtp

        consult_call_id = str(uuid.uuid4())
        consult = pbx.call_manager.create_call(consult_call_id, transferee_ext, destination)
        consult.start()
        consult.rtp_ports = original.rtp_ports
        consult.uses_peer_relay = True
        consult.is_transfer_consult = True
        consult.transfer_referrer_addr = referrer_addr

        original.pending_transfer_consult_id = consult_call_id
        original.transfer_referrer_addr = referrer_addr
        original.transfer_referrer_is_caller = referrer_is_caller

        server_ip = pbx._get_server_ip()
        sip_port = pbx.config.get("server.sip_port", 5060)

        protocol = "RTP/AVP"
        crypto: list[str] | None = None
        if transferee_rtp:
            protocol = transferee_rtp.get("protocol", "RTP/AVP")
            crypto = transferee_rtp.get("crypto") or None

        invite_sdp = SDPBuilder.build_audio_sdp(
            server_ip,
            original.rtp_ports[0],
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

        pbx.sip_server._send_message(invite_msg.build(), dest_addr)
        pbx.cdr_system.start_record(consult_call_id, transferee_ext, destination)

        # Abort the whole transfer if the destination never answers.
        no_answer_timeout = pbx.config.get("voicemail.no_answer_timeout", 30)
        timer = threading.Timer(
            float(no_answer_timeout),
            self.abort_pending_transfer,
            args=(consult,),
            kwargs={"cancel_destination": True},
        )
        timer.daemon = True
        consult.no_answer_timer = timer
        timer.start()

        pbx.logger.info(
            f"Blind transfer leg originated: {consult_call_id} ({transferee_ext} -> {destination})"
        )
        return True

    def abort_pending_transfer(self, consult: Any, cancel_destination: bool = False) -> None:
        """
        Abort a pending (deferred) transfer whose destination declined,
        failed, or never answered.

        Default behavior assumes a REFER-based transfer: the transferor is
        already gone, so both the consultation leg and the parked transferee
        leg are torn down. If `original.transfer_failure_callback` is set,
        it is invoked instead of that default -- for a transfer with no real
        transferor phone to fall back to (e.g. an IVR session transferring
        its own call), which needs to keep its own call alive and handle the
        failure itself (e.g. replay a menu) rather than being hung up on.

        Args:
            consult: The consultation call record.
            cancel_destination: Send CANCEL to the (still ringing)
                destination -- used by the no-answer timer path.
        """
        pbx = self.pbx_core

        original = next(
            (
                c
                for c in pbx.call_manager.get_active_calls()
                if c.pending_transfer_consult_id == consult.call_id
            ),
            None,
        )

        pbx.logger.warning(
            f"Aborting pending transfer: consult {consult.call_id}"
            + (f", original {original.call_id}" if original else "")
        )

        if cancel_destination and consult.callee_addr is None and consult.callee_invite:
            pbx.call_router._send_cancel_to_callee(consult, consult.call_id)

        pbx.end_call(consult.call_id)

        if original:
            original.pending_transfer_consult_id = None

            if original.transfer_failure_callback is not None:
                callback = original.transfer_failure_callback
                original.transfer_failure_callback = None
                callback()
                return

            # Null the departed transferor's side so the leg BYE reaches the
            # parked transferee, then tear the original call down.
            if original.transfer_referrer_addr is not None:
                if original.caller_addr == original.transfer_referrer_addr:
                    original.caller_addr = None
                elif original.callee_addr == original.transfer_referrer_addr:
                    original.callee_addr = None
            pbx.sip_server._send_leg_bye(original)
            pbx.end_call(original.call_id)

    def consultation_transfer_start(self, call_id: str, destination: str) -> str | None:
        """
        Start a consultative transfer, driven by the REST API.

        Entry point for ``POST /api/calls/<id>/transfer`` (type
        ``consultative``): places the original call on hold and originates a
        new consultation call to `destination`, returning its call id so a
        client can later complete the transfer via :meth:`attended_transfer`.
        This is the programmatic equivalent of a phone establishing its own
        consultation call before sending a REFER with Replaces.

        Note: no in-repo client currently calls this endpoint; it exists as a
        REST surface for external/programmatic transfer clients.

        Args:
            call_id: Original call identifier
            destination: Destination extension to consult with

        Returns:
            New consultation call ID, or None if failed
        """
        from pbx.sip.message import SIPMessageBuilder
        from pbx.sip.sdp import SDPBuilder

        pbx = self.pbx_core

        call = pbx.call_manager.get_call(call_id)
        if not call:
            pbx.logger.error(f"Call {call_id} not found for consultation transfer")
            return None

        if not pbx.extension_registry.is_registered(destination):
            pbx.logger.error(f"Consultation destination {destination} not registered")
            return None

        dest_addr = pbx.extension_registry.get_address(destination)
        if not dest_addr:
            pbx.logger.error(f"No address for consultation destination {destination}")
            return None

        # Place original call on hold
        pbx.hold_call(call_id)

        # Allocate new RTP ports for the consultation call
        consult_call_id = str(uuid.uuid4())
        rtp_ports = pbx.rtp_relay.allocate_relay(consult_call_id)
        if not rtp_ports:
            pbx.logger.error("Failed to allocate RTP ports for consultation call")
            resumed = pbx.resume_call(call_id)
            if not resumed:
                pbx.logger.critical(
                    f"Failed to resume original call {call_id} after RTP allocation failure — "
                    "call is stuck on hold. Tearing down call."
                )
                self._teardown_stuck_call(call_id, call)
            else:
                pbx.logger.info(
                    f"Original call {call_id} resumed after consultation transfer failure"
                )
            return None

        # Create consultation call
        consult_call = pbx.call_manager.create_call(consult_call_id, call.to_extension, destination)
        consult_call.start()
        consult_call.rtp_ports = rtp_ports

        # Build and send INVITE for consultation
        server_ip = pbx._get_server_ip()
        sip_port = pbx.config.get("server.sip_port", 5060)

        # Preserve SRTP protocol from the original call
        consult_protocol = "RTP/AVP"
        consult_crypto: list[str] | None = None
        if call.caller_rtp:
            consult_protocol = call.caller_rtp.get("protocol", "RTP/AVP")
            consult_crypto = call.caller_rtp.get("crypto") or None
        consult_sdp = SDPBuilder.build_audio_sdp(
            server_ip,
            rtp_ports[0],
            session_id=consult_call_id,
            protocol=consult_protocol,
            crypto=consult_crypto,
        )

        invite_msg = SIPMessageBuilder.build_request(
            method="INVITE",
            uri=f"sip:{destination}@{dest_addr[0]}:{dest_addr[1]}",
            from_addr=f"<sip:{call.to_extension}@{server_ip}>",
            to_addr=f"<sip:{destination}@{server_ip}>",
            call_id=consult_call_id,
            cseq=1,
            body=consult_sdp,
        )
        invite_msg.set_header("Content-type", "application/sdp")
        invite_msg.set_header("Contact", f"<sip:{call.to_extension}@{server_ip}:{sip_port}>")

        pbx.sip_server._send_message(invite_msg.build(), dest_addr)
        pbx.logger.info(f"Consultation call initiated: {consult_call_id} to {destination}")

        # Start CDR for consultation call
        pbx.cdr_system.start_record(consult_call_id, call.to_extension, destination)

        return consult_call_id

    def _teardown_stuck_call(self, call_id: str, call: Any) -> None:
        """
        Tear down a call that is stuck in an unrecoverable state (e.g. hold
        with no way to resume). Sends BYE to both parties and releases RTP.
        """
        from pbx.sip.message import SIPMessageBuilder

        pbx = self.pbx_core

        server_ip = pbx._get_server_ip()

        for party, ext_field in [("caller", "from_extension"), ("callee", "to_extension")]:
            ext = getattr(call, ext_field, None)
            if not ext:
                continue
            addr = (
                pbx.extension_registry.get_address(ext)
                if pbx.extension_registry is not None
                else None
            )
            if not addr:
                continue
            try:
                bye = SIPMessageBuilder.build_request(
                    method="BYE",
                    uri=f"sip:{ext}@{addr[0]}:{addr[1]}",
                    from_addr=f"<sip:pbx@{server_ip}>",
                    to_addr=f"<sip:{ext}@{server_ip}>",
                    call_id=call_id,
                    cseq=99,
                )
                bye.set_header("Reason", 'Q.850;cause=47;text="Resource unavailable"')
                pbx.sip_server._send_message(bye.build(), addr)
                pbx.logger.info(f"Sent BYE to {party} ({ext}) for stuck call {call_id}")
            except Exception as e:
                pbx.logger.error(f"Failed to send BYE to {party} ({ext}): {e}")

        pbx.rtp_relay.release_relay(call_id)
        pbx.call_manager.end_call(call_id)
        pbx.logger.info(f"Stuck call {call_id} torn down")
