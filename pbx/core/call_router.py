"""
Call routing handler for PBX Core

Extracts call routing logic from PBXCore into a dedicated class,
including dialplan checking, call forwarding, no-answer handling,
and voicemail fallback routing.
"""

import re
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pbx.features.webhooks import WebhookEvent
from pbx.sip.transaction import InviteClientTransaction


class CallRouter:
    """Handles call routing, dialplan checking, and no-answer fallback"""

    # Cap on SIP 3xx redirects followed for a single call, to guard against
    # misconfigured phones that redirect back into a loop.
    MAX_REDIRECTS = 5

    # Classifies a dialed string as an external PSTN number (vs. an internal
    # extension) -- optional leading 1, then a 10-digit NANP number. Shared
    # by route_call()'s inbound-INVITE dispatch and CallOriginator's
    # PBX-initiated dial, so both resolve "internal vs. external" identically.
    EXTERNAL_NUMBER_PATTERN = re.compile(r"^1?\d{10}$")

    # What a WebRTC registration stores instead of a routable host, in both
    # the in-memory registry address and the registered_phones row
    # (WebRTCGateway.create_session).
    WEBRTC_HOST_MARKER = "webrtc"

    def __init__(self, pbx_core: Any) -> None:
        """
        Initialize CallRouter with reference to PBXCore.

        Args:
            pbx_core: The PBXCore instance
        """
        self.pbx_core: Any = pbx_core

    def route_call(
        self,
        from_header: str,
        to_header: str,
        call_id: str,
        message: Any,
        from_addr: tuple[str, int],
    ) -> bool:
        """
        Route call from one extension to another

        Args:
            from_header: From SIP header
            to_header: To SIP header
            call_id: Call ID
            message: SIP INVITE message
            from_addr: Address tuple of caller

        Returns:
            True if call was routed successfully
        """
        pbx = self.pbx_core

        # Parse extension numbers - handle both regular extensions and special
        # patterns
        from_match = re.search(r"sip:(\d+)@", from_header)
        # Allow * prefix for voicemail access (e.g., *1001), but validate
        # format
        to_match = re.search(r"sip:(\*?\d+)@", to_header)

        if not from_match or not to_match:
            pbx.logger.warning("Could not parse extensions from headers")
            return False

        from_ext = from_match.group(1)
        to_ext = to_match.group(1)

        # Calls arriving from a registered SIP trunk are inbound PSTN calls,
        # not internal ones -- the From header is carrier caller ID, not a
        # registry-validated extension, so none of the internal-origin
        # checks below (emergency, auto attendant, voicemail access,
        # dialplan) apply. Must run before all of them.
        if pbx.trunk_system:
            trunk = pbx.trunk_system.get_trunk_by_addr(from_addr)
            if trunk:
                return self._route_inbound_did(
                    trunk, from_ext, to_ext, from_header, to_header, call_id, message, from_addr
                )

        # Check if this is an emergency call (911) - Kari's Law compliance
        # Must be handled first for immediate routing
        if pbx.karis_law and pbx.karis_law.is_emergency_number(to_ext):
            return bool(
                pbx.emergency_handler.handle_emergency_call(
                    from_ext, to_ext, call_id, message, from_addr
                )
            )

        # Check if this is an auto attendant call (extension 0)
        if pbx.auto_attendant and to_ext == pbx.auto_attendant.get_extension():
            return bool(
                pbx.auto_attendant_handler.handle_auto_attendant(
                    from_ext, to_ext, call_id, message, from_addr
                )
            )

        # Check if this is a voicemail access call (*xxxx pattern)
        # Validate format: must be * followed by exactly 3 or 4 digits
        if (
            to_ext.startswith("*")
            and len(to_ext) >= 4
            and len(to_ext) <= 5
            and to_ext[1:].isdigit()
        ):
            return bool(
                pbx.voicemail_handler.handle_voicemail_access(
                    from_ext, to_ext, call_id, message, from_addr
                )
            )

        # Agent queue login/logout star codes (*61/*62). Internal-origin
        # only: trunk calls already diverted to DID routing above.
        from pbx.core.queue_handler import STAR_CODE_LOGIN, STAR_CODE_LOGOUT

        if to_ext in (STAR_CODE_LOGIN, STAR_CODE_LOGOUT):
            return bool(
                pbx.queue_handler.handle_agent_star_code(
                    from_ext, to_ext, call_id, message, from_addr
                )
            )

        # Check if this is a paging call (7xx pattern or all-call)
        if pbx.paging_system and pbx.paging_system.is_paging_extension(to_ext):
            return bool(
                pbx.paging_handler.handle_paging(from_ext, to_ext, call_id, message, from_addr)
            )

        # Check if this is an external call (10-digit)
        if pbx.trunk_system and self.EXTERNAL_NUMBER_PATTERN.match(to_ext):
            return self._route_to_trunk(from_ext, to_ext, call_id, message, from_addr)

        return self._dial_to_internal_extension(
            from_ext, to_ext, from_header, to_header, call_id, message, from_addr
        )

    def _dial_to_internal_extension(
        self,
        from_ext: str,
        to_ext: str,
        from_header: str,
        to_header: str,
        call_id: str,
        message: Any,
        from_addr: tuple[str, int],
    ) -> bool:
        """
        Resolve `to_ext` as a live internal extension and dial it: dialplan
        check, relay allocation, CDR, webhook, and INVITE construction --
        exactly what route_call() has always done for an internal
        destination. Extracted so _route_inbound_did() can give a DID that
        resolves to an extension the same treatment as a directly-dialed one.

        Returns:
            True if the call was routed successfully.
        """
        from pbx.sip.sdp import SDPSession

        pbx = self.pbx_core

        # Call queue destination? Checked here (not in route_call) so DID
        # routing -- which calls this method directly -- reaches the queue
        # with zero DID-specific code.
        if pbx.queue_handler.is_queue_destination(to_ext):
            return bool(
                pbx.queue_handler.handle_queue_entry(from_ext, to_ext, call_id, message, from_addr)
            )

        # Check if destination extension is registered and not expired,
        # recovering its registration from the database if necessary.
        if not self._resolve_extension(to_ext):
            return False

        # Check dialplan
        if not self._check_dialplan(to_ext):
            pbx.logger.warning(f"Extension {to_ext} not allowed by dialplan")
            return False

        # Parse SDP from caller's INVITE
        caller_sdp: dict[str, Any] | None = None
        caller_codecs: list[str] | None = None
        if message.body:
            caller_sdp_obj = SDPSession()
            caller_sdp_obj.parse(message.body)
            caller_sdp = caller_sdp_obj.get_audio_info()

            if caller_sdp:
                pbx.logger.info(f"Caller RTP: {caller_sdp['address']}:{caller_sdp['port']}")
                # Extract caller's codec list to maintain codec compatibility
                caller_codecs = caller_sdp.get("formats", None)
                if caller_codecs:
                    pbx.logger.info(f"Caller codecs: {caller_codecs}")
                # Log media protocol (RTP/AVP vs RTP/SAVP for SRTP)
                caller_protocol = caller_sdp.get("protocol", "RTP/AVP")
                if caller_protocol != "RTP/AVP":
                    pbx.logger.info(f"Caller media protocol: {caller_protocol}")

        # Create call
        call = pbx.call_manager.create_call(call_id, from_ext, to_ext)
        call.start()
        call.original_invite = message  # Store original INVITE for later response

        # Start CDR record for analytics
        pbx.cdr_system.start_record(call_id, from_ext, to_ext)

        # Trigger webhook event
        pbx.webhook_system.trigger_event(
            WebhookEvent.CALL_STARTED,
            {
                "call_id": call_id,
                "from_extension": from_ext,
                "to_extension": to_ext,
                "timestamp": call.start_time.isoformat() if call.start_time else None,
            },
        )

        # Always store caller address so error responses can reach the caller
        call.caller_addr = from_addr

        # Allocate RTP relay
        rtp_ports = pbx.rtp_relay.allocate_relay(call_id)
        if not rtp_ports:
            pbx.logger.error(f"Failed to allocate RTP relay for call {call_id}")
            pbx.cdr_system.end_record(call_id, hangup_cause="resource_unavailable")
            pbx.call_manager.end_call(call_id)
            return False

        call.rtp_ports = rtp_ports

        # Store caller's RTP info and set endpoint immediately to avoid
        # dropping early packets
        if caller_sdp:
            call.caller_rtp = caller_sdp

            # set caller's endpoint immediately to enable early RTP packet learning
            # This prevents dropping packets that arrive before the 200 OK
            # response
            caller_endpoint = (caller_sdp["address"], caller_sdp["port"])
            relay_info = pbx.rtp_relay.active_relays.get(call_id)
            if relay_info:
                handler = relay_info["handler"]
                # set only endpoint A for now; endpoint B will be set after
                # 200 OK
                handler.set_endpoints(caller_endpoint, None)
                pbx.logger.info(
                    f"RTP relay allocated on port {rtp_ports[0]}, "
                    f"caller endpoint set to {caller_endpoint}"
                )

        # Get destination extension's address
        dest_ext_obj = pbx.extension_registry.get(to_ext)
        if not dest_ext_obj or not dest_ext_obj.address:
            pbx.logger.error(f"Cannot get address for extension {to_ext}")
            # Release allocated RTP relay and clean up call to prevent resource leaks
            pbx.rtp_relay.release_relay(call_id)
            pbx.cdr_system.end_record(call_id, hangup_cause="resource_unavailable")
            pbx.call_manager.end_call(call_id)
            return False

        if not self._dial_extension_leg(call, call_id, to_ext, from_header, to_header):
            pbx.rtp_relay.release_relay(call_id)
            pbx.cdr_system.end_record(call_id, hangup_cause="resource_unavailable")
            pbx.call_manager.end_call(call_id)
            return False

        pbx.logger.info(
            f"Routing call {call_id}: {from_ext} -> {to_ext} via RTP relay {rtp_ports[0]}"
        )

        return True

    def _route_inbound_did(
        self,
        trunk: Any,
        from_ext: str,
        to_ext: str,
        from_header: str,
        to_header: str,
        call_id: str,
        message: Any,
        from_addr: tuple[str, int],
    ) -> bool:
        """
        Dispatch an inbound call arriving from `trunk` to its configured
        destination, looked up by the dialed DID (`to_ext`).

        Args:
            trunk: The SIPTrunk this INVITE arrived from.
            from_ext: Caller-ID digits parsed from the From header (carrier
                caller ID -- not a registry-validated internal extension).
            to_ext: Dialed DID number, parsed from the To header.
            from_header: Raw From header of the caller's INVITE.
            to_header: Raw To header of the caller's INVITE.
            call_id: SIP Call-ID.
            message: The INVITE message.
            from_addr: Address the INVITE arrived from.

        Returns:
            True if the call was dispatched. False if no inbound route
            matches this DID -- SIPServer sends a 404 in that case, same as
            any other route_call() failure.
        """
        pbx = self.pbx_core
        route = pbx.inbound_routing.lookup(to_ext, trunk.trunk_id)
        if not route:
            pbx.logger.warning(f"No inbound route for DID {to_ext} on trunk {trunk.trunk_id}")
            return False

        destination_type = route["destination_type"]
        destination_value = route["destination_value"]

        if destination_type == "extension":
            rewritten_to_header = re.sub(
                r"sip:(\*?[^@]+)@", f"sip:{destination_value}@", to_header, count=1
            )
            return self._dial_to_internal_extension(
                from_ext,
                destination_value,
                from_header,
                rewritten_to_header,
                call_id,
                message,
                from_addr,
            )

        if destination_type == "auto_attendant":
            if not pbx.auto_attendant:
                pbx.logger.warning(
                    f"Inbound route for DID {to_ext} points to the auto attendant, "
                    "but the auto attendant feature is not enabled"
                )
                return False
            return bool(
                pbx.auto_attendant_handler.handle_auto_attendant(
                    from_ext, pbx.auto_attendant.get_extension(), call_id, message, from_addr
                )
            )

        if destination_type == "voicemail":
            return bool(
                pbx.voicemail_handler.handle_voicemail_access(
                    from_ext, f"*{destination_value}", call_id, message, from_addr
                )
            )

        pbx.logger.error(
            f"Inbound route for DID {to_ext} has unknown destination_type={destination_type!r}"
        )
        return False

    def handle_callee_answer(
        self, call_id: str, response_message: Any, callee_addr: tuple[str, int]
    ) -> None:
        """
        Handle when callee answers the call.

        This method handles the initial 200 OK for a new call.  If the call
        is already connected (e.g., 200 OK to a re-INVITE forwarded by the
        PBX), only the SDP/RTP endpoints are updated — the call state and
        CDR are not re-processed.

        Args:
            call_id: Call identifier
            response_message: 200 OK response from callee
            callee_addr: Callee's address
        """
        from pbx.sip.message import SIPMessageBuilder
        from pbx.sip.sdp import SDPBuilder, SDPSession

        pbx = self.pbx_core

        call = pbx.call_manager.get_call(call_id)
        if not call:
            pbx.logger.error(f"Call {call_id} not found")
            return

        # If the call is already connected, this 200 OK is a response to a
        # re-INVITE we forwarded.  Update the SDP/RTP endpoints but do NOT
        # re-process call state, CDR, or send a duplicate 200 OK to the caller.
        if call.state.value == "connected":
            pbx.logger.info(f"200 OK for already-connected call {call_id} (re-INVITE response)")
            if not call.callee_dialog_to:
                call.callee_dialog_to = response_message.get_header("To")
            if response_message.body:
                sdp_obj = SDPSession()
                sdp_obj.parse(response_message.body)
                new_sdp = sdp_obj.get_audio_info()
                if new_sdp:
                    call.callee_rtp = new_sdp
                    callee_endpoint = (new_sdp["address"], new_sdp["port"])
                    if call.bridged_peer_call_id and call.bridge_peer_side:
                        # 200 OK to the bridge re-INVITE: this leg's media
                        # now lives on the bridged peer's relay.
                        pbx.rtp_relay.replace_endpoint(
                            call.bridged_peer_call_id,
                            call.bridge_peer_side,
                            callee_endpoint,
                        )
                        pbx.logger.info(
                            f"Refreshed bridged endpoint for call {call_id} on relay "
                            f"{call.bridged_peer_call_id}"
                        )
                        return
                    caller_endpoint = (
                        (call.caller_rtp["address"], call.caller_rtp["port"])
                        if call.caller_rtp
                        else None
                    )
                    if call.rtp_ports and caller_endpoint:
                        pbx.rtp_relay.set_endpoints(call_id, caller_endpoint, callee_endpoint)
                        pbx.logger.info(f"Updated RTP endpoints for re-INVITE on call {call_id}")
            return

        # Parse callee's SDP from 200 OK
        callee_sdp = None
        if response_message.body:
            callee_sdp_obj = SDPSession()
            callee_sdp_obj.parse(response_message.body)
            callee_sdp = callee_sdp_obj.get_audio_info()

            if callee_sdp:
                pbx.logger.info(f"Callee RTP: {callee_sdp['address']}:{callee_sdp['port']}")
                call.callee_rtp = callee_sdp
                call.callee_addr = callee_addr

        # Capture the callee's dialog identity (To header with their tag)
        # for later PBX-originated in-dialog requests (re-INVITE, BYE).
        call.callee_dialog_to = response_message.get_header("To")

        # Now we have both endpoints, complete the RTP relay setup
        # Cancel no-answer timer if it's running
        if call is not None and call.no_answer_timer:
            call.no_answer_timer.cancel()
            pbx.logger.info(f"Cancelled no-answer timer for call {call_id}")

        # If this is a transfer target leg, the transfer session decides what
        # its answer means -- bridging now if the transferor already committed
        # (semi-attended, or a PBX-originated blind leg), otherwise letting the
        # transferor consult. Either way the normal answer flow does not apply.
        if call.transfer_session_id:
            from pbx.core.transfer_session import LegEvent, LegEventResult

            result = pbx.transfer_handler.on_leg_event(call, LegEvent.ANSWERED, side="callee")
            if result is not LegEventResult.FORWARD:
                return

        # Check if this is a WebRTC-originated call
        webrtc_session_id = getattr(call, "webrtc_session_id", None)

        if webrtc_session_id and call.callee_rtp and call.rtp_ports:
            # --- WebRTC caller: start aiortc ↔ RTP media bridge ---
            callee_endpoint = (call.callee_rtp["address"], call.callee_rtp["port"])

            if hasattr(pbx, "webrtc_signaling") and pbx.webrtc_signaling:
                session = pbx.webrtc_signaling.get_session(webrtc_session_id)
                if session:
                    pbx.webrtc_signaling.start_media_bridge(
                        session, call.rtp_ports[0], callee_endpoint
                    )
                    pbx.logger.info(f"WebRTC media bridge started for call {call_id}")

            # Mark call as connected
            call.connect()
            pbx.cdr_system.mark_answered(call_id)
            pbx.logger.info(f"WebRTC call {call_id} connected")
            return

        # --- Regular SIP-to-SIP path ---
        # Note: caller endpoint (A) was already set when INVITE was received
        if call.caller_rtp and call.callee_rtp and call.rtp_ports:
            caller_endpoint = (call.caller_rtp["address"], call.caller_rtp["port"])
            callee_endpoint = (call.callee_rtp["address"], call.callee_rtp["port"])

            # set both endpoints (caller was already set, but setting again is safe)
            # This ensures callee endpoint (B) is now known for bidirectional
            # relay
            pbx.rtp_relay.set_endpoints(call_id, caller_endpoint, callee_endpoint)
            pbx.logger.info(f"RTP relay connected for call {call_id}")
            # Recording starts itself: setting both endpoints fires the relay's on_bridged
            # hook. Nothing to do here, and no other signalling path needs wiring either.

        # Mark call as connected
        call.connect()

        # Mark CDR as answered for analytics
        pbx.cdr_system.mark_answered(call_id)

        # A PBX-originated leg (CallOriginator, no live caller to relay a
        # 200 OK to) reports its answer through this callback instead.
        if call.originate_callbacks and call.originate_callbacks.get("on_answer"):
            call.originate_callbacks["on_answer"](call)

        # Send 200 OK back to caller with PBX's RTP endpoint
        server_ip = pbx._get_server_ip()

        if call.rtp_ports and call.caller_addr:
            # Extract the callee's answered codecs from their 200 OK SDP.
            # The callee's 200 OK contains the codec(s) they actually selected,
            # so we must reflect those back to the caller to ensure both sides
            # use the same codec. The RTP relay does not transcode — if we offer
            # the caller their original full codec list they may pick a different
            # codec than the callee chose, resulting in no audio.
            callee_answered_codecs: list[str] | None = None
            if callee_sdp:
                callee_answered_codecs = callee_sdp.get("formats", None)
                if callee_answered_codecs:
                    pbx.logger.info(f"Callee answered with codecs: {callee_answered_codecs}")

            # Get caller's phone model for any model-specific filtering
            caller_user_agent = pbx._get_phone_user_agent(call.from_extension)
            caller_phone_model = pbx._detect_phone_model(caller_user_agent)

            # Use the callee's answered codecs so both sides agree on the codec.
            # Fall back to the caller's original codecs only if the callee's
            # 200 OK had no SDP (shouldn't happen in practice).
            caller_codecs = call.caller_rtp.get("formats", None) if call.caller_rtp else None
            answered_codecs = callee_answered_codecs or caller_codecs

            # For phones with model-specific codec requirements, compute the
            # intersection of the callee's answered codecs and the phone model's
            # supported codecs.  This ensures the caller gets a codec it supports
            # that the callee has already committed to.
            codecs_for_caller = pbx._get_compatible_codecs(caller_phone_model, answered_codecs)

            if caller_phone_model:
                pbx.logger.info(
                    f"Detected caller phone model: {caller_phone_model}, "
                    f"offering codecs in 200 OK: {codecs_for_caller}"
                )

            # Build SDP for caller (with PBX RTP endpoint)
            # Get DTMF payload type from config
            dtmf_payload_type = pbx._get_dtmf_payload_type()
            ilbc_mode = pbx._get_ilbc_mode()

            # Preserve the caller's original media protocol and SRTP crypto
            # attributes.  The caller's INVITE specified whether it wants SRTP
            # (RTP/SAVP) or plain RTP (RTP/AVP).  The 200 OK must use the same
            # protocol or the caller will reject the SDP and produce no audio.
            caller_protocol = "RTP/AVP"
            caller_crypto: list[str] | None = None
            if call.caller_rtp:
                caller_protocol = call.caller_rtp.get("protocol", "RTP/AVP")
                caller_crypto = call.caller_rtp.get("crypto") or None

            # Omit rtpmap for static PTs on Zultys phones to avoid codec name
            # mismatch errors in their RTP engine.
            skip_rtpmap = pbx._should_skip_static_rtpmap(caller_phone_model)

            caller_response_sdp = SDPBuilder.build_audio_sdp(
                server_ip,
                call.rtp_ports[0],
                session_id=call_id,
                codecs=codecs_for_caller,
                dtmf_payload_type=dtmf_payload_type,
                ilbc_mode=ilbc_mode,
                protocol=caller_protocol,
                crypto=caller_crypto,
                skip_static_rtpmap=skip_rtpmap,
            )

            # Build 200 OK for caller using original INVITE
            if call.original_invite:
                ok_response = SIPMessageBuilder.build_response(
                    200, "OK", call.original_invite, body=caller_response_sdp
                )
                ok_response.set_header("Content-type", "application/sdp")

                # Build Contact header
                sip_port = pbx.config.get("server.sip_port", 5060)
                contact_uri = f"<sip:{call.to_extension}@{server_ip}:{sip_port}>"
                ok_response.set_header("Contact", contact_uri)

                # Capture the tagged To header actually sent to the caller --
                # build_response() mints this tag fresh; it's the caller's own
                # dialog identity for any later PBX-originated request toward
                # it (e.g. a bridge-teardown BYE), and cannot be recovered from
                # original_invite (whose To header predates this tag).
                call.caller_dialog_to = ok_response.get_header("To")

                # Send to caller
                pbx.sip_server._send_message(ok_response.build(), call.caller_addr)
                pbx.logger.info(f"Sent 200 OK to caller for call {call_id}")

    @staticmethod
    def is_webrtc_address(address: Any) -> bool:
        """
        Is `address` the ("webrtc", session_id) marker a WebRTC registration
        stands in for a SIP contact (see WebRTCGateway.create_session)?

        Shared with CallOriginator so both entry points recognise the marker
        instead of handing "webrtc" to the socket as a hostname.
        """
        return bool(
            address
            and isinstance(address, tuple)
            and len(address) == 2
            and address[0] == CallRouter.WEBRTC_HOST_MARKER
        )

    def _resolve_extension(self, to_ext: str) -> Any | None:
        """
        Look up `to_ext` in the in-memory extension registry, recovering its
        registration from the database if the PBX restarted since the phone
        last registered (in-memory state lost, database record persists).

        Used for both the initially-dialed extension and SIP 3xx redirect
        targets, so a redirect to a sibling extension on the same device
        (e.g. another line on a multi-line phone) gets the same recovery
        treatment as the original destination.

        Returns:
            The extension object if it resolves to a live registration,
            None otherwise (an expired in-memory entry is unregistered as
            a side effect).
        """
        pbx = self.pbx_core
        dest_ext = pbx.extension_registry.get(to_ext)
        if dest_ext and dest_ext.registered and not dest_ext.is_expired():
            return dest_ext

        # Try to recover from database: look up the extension's last known
        # registration and re-register it in memory.
        recovered = False
        if pbx.registered_phones_db:
            try:
                # Newest row first, but skip WebRTC rows: their "ip_address"
                # is the marker string, not a host, and the browser session
                # it stood for is gone once the in-memory registration is
                # (that is why recovery is running at all). Recovering one
                # would hand an unresolvable hostname to the socket; a real
                # SIP phone further down the list is still worth recovering.
                db_phone = next(
                    (
                        phone
                        for phone in pbx.registered_phones_db.get_by_extension(to_ext)
                        if phone.get("ip_address")
                        and phone["ip_address"] != self.WEBRTC_HOST_MARKER
                    ),
                    None,
                )
                if db_phone:
                    phone_ip = db_phone["ip_address"]
                    # Use the port the phone actually registered from. Older rows predate
                    # the column, so fall back to the default rather than skipping recovery.
                    phone_port = int(db_phone.get("sip_port") or 5060)
                    # Ensure the extension object exists in registry
                    if not dest_ext and pbx.extension_db:
                        db_ext = pbx.extension_db.get(to_ext)
                        if db_ext:
                            from pbx.features.extensions import ExtensionRegistry

                            ext_obj = ExtensionRegistry.create_extension_from_db(db_ext)
                            pbx.extension_registry.extensions[to_ext] = ext_obj
                            dest_ext = ext_obj
                    if dest_ext:
                        dest_ext.register((phone_ip, phone_port))
                        pbx.logger.info(
                            f"Recovered registration for {to_ext} from database: "
                            f"{phone_ip}:{phone_port}"
                        )
                        recovered = True
            except (KeyError, TypeError, ValueError) as e:
                pbx.logger.debug(f"DB recovery lookup failed for {to_ext}: {e}")

        if not recovered:
            reason = (
                "not in registry"
                if not dest_ext
                else ("not registered" if not dest_ext.registered else "registration expired")
            )
            pbx.logger.warning(f"Extension {to_ext} {reason}")
            if dest_ext and dest_ext.is_expired():
                pbx.extension_registry.unregister(to_ext)
            return None

        return dest_ext

    def _build_and_send_leg_invite(
        self,
        call: Any,
        call_id: str,
        invite_request: Any,
        dest_addr: tuple[str, int],
        server_ip: str,
        on_timeout: Callable[[], None],
    ) -> Any:
        """
        Attach the transport headers every outbound leg needs (Via with a
        fresh branch, Content-type, Max-Forwards), start a retransmitting
        InviteClientTransaction toward dest_addr, and record it on `call`.

        Callers build the request line, From/To/Contact/CSeq, SDP body,
        caller-ID headers, and any phone/carrier-specific quirks themselves
        first -- those differ enough between the extension and trunk paths
        (Contact identity semantics, Zultys SDP quirks, WebRTC short-circuit)
        that pulling them in here would trade real duplication for a
        parameter explosion that just moves the duplication into
        conditional flags.

        Returns:
            The started InviteClientTransaction.
        """
        pbx = self.pbx_core
        branch_id = str(uuid.uuid4()).replace("-", "")
        sip_port = pbx.config.get("server.sip_port", 5060)
        invite_request.set_header(
            "Via",
            f"SIP/2.0/UDP {server_ip}:{sip_port};branch=z9hG4bK{branch_id}",
        )
        invite_request.set_header("Content-type", "application/sdp")
        # Max-Forwards is mandatory per RFC 3261 Section 8.1.1.6.
        # Some strict SIP stacks reject INVITEs without it.
        invite_request.set_header("Max-Forwards", "70")

        invite_txn = InviteClientTransaction(
            message=invite_request.build(),
            dest_addr=dest_addr,
            send_fn=pbx.sip_server._send_message,
            on_timeout=on_timeout,
        )
        invite_txn.start()
        call.invite_transaction = invite_txn
        call.callee_addr = dest_addr
        call.callee_invite = invite_request

        return invite_txn

    def _dial_extension_leg(
        self,
        call: Any,
        call_id: str,
        to_ext: str,
        from_header: str,
        to_header: str,
    ) -> bool:
        """
        Build and send an INVITE for `to_ext` on behalf of `call`, reusing
        its already-allocated RTP relay, and arm the no-answer timer.

        Used both for the initial destination in route_call() and for
        redirect targets (SIP 3xx Contact-header forwarding).

        Args:
            call: The Call being routed.
            call_id: Call identifier.
            to_ext: Extension to dial.
            from_header: Raw From header of the caller's original INVITE.
            to_header: Raw To header to use for the leg (may name a
                different extension than the original INVITE, for redirects).

        Returns:
            True if the leg was dispatched (INVITE sent, or a WebRTC call
            was connected). False if the destination could not be dialed.
            Does not release the RTP relay or end the call on failure --
            callers decide how to handle that, since a failed initial dial
            tears the whole call down while a failed redirect should fall
            back to voicemail on the caller's still-live leg.
        """
        from pbx.sip.message import SIPMessageBuilder
        from pbx.sip.sdp import SDPBuilder, SDPSession

        pbx = self.pbx_core
        from_ext = call.from_extension
        message = call.original_invite

        caller_sdp: dict[str, Any] | None = None
        caller_codecs: list[str] | None = None
        if message and message.body:
            caller_sdp_obj = SDPSession()
            caller_sdp_obj.parse(message.body)
            caller_sdp = caller_sdp_obj.get_audio_info()
            if caller_sdp:
                caller_codecs = caller_sdp.get("formats", None)

        dest_ext_obj = self._resolve_extension(to_ext)
        if not dest_ext_obj or not dest_ext_obj.address:
            pbx.logger.error(f"Cannot get address for extension {to_ext}")
            return False

        # Check if destination is a WebRTC extension
        if self.is_webrtc_address(dest_ext_obj.address):
            # Route call to WebRTC client
            # Extract session ID from address tuple
            session_id = dest_ext_obj.address[1]
            pbx.logger.info(f"Routing call to WebRTC extension {to_ext} (session: {session_id})")

            if not pbx.webrtc_gateway:
                pbx.logger.error("WebRTC gateway not available for routing call")
                return False

            # Get caller's SDP if available
            caller_sdp_str = message.body if message else None

            # Route the call through WebRTC gateway
            success = pbx.webrtc_gateway.receive_call(
                session_id=session_id,
                call_id=call_id,
                caller_sdp=caller_sdp_str,
                webrtc_signaling=(
                    pbx.webrtc_signaling if hasattr(pbx, "webrtc_signaling") else None
                ),
            )

            if not success:
                pbx.logger.error(f"Failed to route call to WebRTC session {session_id}")
                return False

            pbx.logger.info(f"Call {call_id} routed to WebRTC session {session_id}")
            # Send 180 Ringing to the SIP caller so they hear ringback
            # tone.  Without this, the SIP phone sits in silence after
            # dialing a WebRTC extension.
            if call.caller_addr and call.original_invite:
                ringing = SIPMessageBuilder.build_response(180, "Ringing", call.original_invite)
                pbx.sip_server._send_message(ringing.build(), call.caller_addr)
                pbx.logger.info(f"Sent 180 Ringing to SIP caller for WebRTC call {call_id}")
            return True

        # Build SDP for forwarding INVITE to callee
        # Use the server's external IP address for SDP
        server_ip = pbx._get_server_ip()

        # Never route a call to the PBX's own SIP address.  A stale or bogus
        # registration (e.g. a loopback address recovered from the database)
        # would make the PBX INVITE itself, receive its own INVITE as a new
        # call with the same Call-ID, and re-route it in an infinite
        # INVITE/100 Trying loop against 127.0.0.1.
        dest_ip = str(dest_ext_obj.address[0])
        dest_port = dest_ext_obj.address[1] if len(dest_ext_obj.address) > 1 else 5060
        own_sip_port = pbx.config.get("server.sip_port", 5060)
        if dest_ip.startswith("127.") or (dest_ip == server_ip and dest_port == own_sip_port):
            pbx.logger.error(
                f"Refusing to route call {call_id} to extension {to_ext}: registered "
                f"address {dest_ip}:{dest_port} is the PBX itself (stale or bogus "
                "registration) - unregistering it"
            )
            pbx.extension_registry.unregister(to_ext)
            return False

        if not call.rtp_ports:
            pbx.logger.error(f"No RTP relay allocated for call {call_id}, cannot dial {to_ext}")
            return False

        # Determine which codecs to offer based on callee's phone model
        # Get callee's User-Agent to detect phone model
        callee_user_agent = pbx._get_phone_user_agent(to_ext)
        callee_phone_model = pbx._detect_phone_model(callee_user_agent)

        # Select codecs that are compatible with both the callee's phone
        # model and the caller's offered codecs.  This ensures the callee
        # can only choose a codec the caller also supports, preventing
        # codec mismatches when the RTP relay forwards without transcoding.
        codecs_for_callee = pbx._get_compatible_codecs(callee_phone_model, caller_codecs)

        # Warn if the computed codec list has no audio codecs (only DTMF)
        dtmf_pt_str = str(pbx._get_dtmf_payload_type())
        audio_codecs = [c for c in codecs_for_callee if c != dtmf_pt_str]
        if not audio_codecs:
            pbx.logger.error(
                f"No audio codec overlap for call {call_id}: "
                f"callee model={callee_phone_model}, caller codecs={caller_codecs}"
            )

        if callee_phone_model:
            pbx.logger.info(
                f"Detected callee phone model: {callee_phone_model}, "
                f"offering codecs: {codecs_for_callee}"
            )

        # Create new INVITE with PBX's RTP endpoint in SDP
        # Get DTMF payload type from config
        dtmf_payload_type = pbx._get_dtmf_payload_type()
        ilbc_mode = pbx._get_ilbc_mode()

        # Preserve the caller's media protocol (RTP/AVP vs RTP/SAVP) and SRTP
        # crypto attributes.  When the caller offers SRTP (RTP/SAVP), we must
        # forward the same protocol to the callee so both endpoints use matching
        # media transport.  Without this, the PBX would downgrade SRTP to plain
        # RTP and neither phone would hear audio.
        caller_protocol = "RTP/AVP"
        caller_crypto: list[str] | None = None
        if caller_sdp:
            caller_protocol = caller_sdp.get("protocol", "RTP/AVP")
            caller_crypto = caller_sdp.get("crypto") or None

        # Omit rtpmap for static PTs on Zultys phones to avoid codec name
        # mismatch errors in their RTP engine.
        skip_rtpmap = pbx._should_skip_static_rtpmap(callee_phone_model)

        callee_sdp_body = SDPBuilder.build_audio_sdp(
            server_ip,
            call.rtp_ports[0],
            session_id=call_id,
            codecs=codecs_for_callee,
            dtmf_payload_type=dtmf_payload_type,
            ilbc_mode=ilbc_mode,
            protocol=caller_protocol,
            crypto=caller_crypto,
            skip_static_rtpmap=skip_rtpmap,
        )

        # Forward INVITE to callee
        # REQUEST-URI should point to the callee's registered address
        callee_ip = dest_ext_obj.address[0]
        callee_port = dest_ext_obj.address[1] if len(dest_ext_obj.address) > 1 else 5060
        cseq_source = message.get_header("CSeq") if message else None
        invite_to_callee = SIPMessageBuilder.build_request(
            method="INVITE",
            uri=f"sip:{to_ext}@{callee_ip}:{callee_port}",
            from_addr=from_header,
            to_addr=to_header,
            call_id=call_id,
            cseq=int((cseq_source or "1 INVITE").split()[0]),
            body=callee_sdp_body,
        )

        # Add Contact header identifying the caller's extension. Via,
        # Content-type, and Max-Forwards are attached by
        # _build_and_send_leg_invite() below, along with the other
        # transport mechanics every outbound leg needs.
        sip_port = pbx.config.get("server.sip_port", 5060)
        invite_to_callee.set_header(
            "Contact",
            f"<sip:{from_ext}@{server_ip}:{sip_port}>",
        )

        # Add caller ID headers (P-Asserted-Identity and Remote-Party-ID)
        if pbx.config.get("sip.caller_id.send_p_asserted_identity", True) or pbx.config.get(
            "sip.caller_id.send_remote_party_id", True
        ):
            # Get caller's display name from extension
            caller_ext_obj = pbx.extension_registry.get(from_ext)
            display_name = from_ext  # Default to extension number
            if caller_ext_obj:
                # Try to get name from extension object
                display_name = getattr(caller_ext_obj, "name", from_ext)
                if not display_name or display_name == "":
                    display_name = from_ext

            # Add caller ID headers for line identification
            SIPMessageBuilder.add_caller_id_headers(
                invite_to_callee, from_ext, display_name, server_ip
            )
            pbx.logger.debug(f"Added caller ID headers: {display_name} <{from_ext}>")

        # Add MAC address header if configured
        if pbx.config.get("sip.device.send_mac_address", True):
            # Try to get MAC address from registered phones database
            mac_address: str | None = None
            if pbx.registered_phones_db:
                try:
                    phones = pbx.registered_phones_db.get_by_extension(from_ext)
                    if phones and len(phones) > 0 and phones[0].get("mac_address"):
                        mac_address = phones[0]["mac_address"]
                except (KeyError, TypeError, ValueError) as e:
                    pbx.logger.debug(f"Could not retrieve MAC for extension {from_ext}: {e}")

            # Also check if MAC was sent in the original INVITE
            if (
                not mac_address
                and message
                and pbx.config.get("sip.device.accept_mac_in_invite", True)
            ):
                x_mac = message.get_header("X-MAC-Address")
                if x_mac:
                    mac_address = x_mac
                    pbx.logger.debug(f"Using MAC from incoming INVITE: {mac_address}")

            # Add MAC header if we found one
            if mac_address:
                SIPMessageBuilder.add_mac_address_header(invite_to_callee, mac_address)
                pbx.logger.debug(f"Added X-MAC-Address header: {mac_address}")

        # Send INVITE to destination with retransmission (RFC 3261)
        self._build_and_send_leg_invite(
            call,
            call_id,
            invite_to_callee,
            dest_ext_obj.address,
            server_ip,
            on_timeout=lambda: self._handle_invite_timeout(call_id),
        )

        pbx.logger.info(f"Forwarded INVITE to {to_ext} at {dest_ext_obj.address}")

        # Do NOT send premature 180 Ringing here.  The callee's actual
        # 180 response is forwarded by _handle_response() in sip/server.py.
        # Fabricating a 180 before the callee responds masks delivery
        # failures and causes duplicate ringing indicators.

        # Start no-answer timer to route to voicemail if not answered
        no_answer_timeout: int = pbx.config.get("voicemail.no_answer_timeout", 30)
        call.no_answer_timer = threading.Timer(
            no_answer_timeout, self._handle_no_answer, args=(call_id,)
        )
        call.no_answer_timer.daemon = True
        call.no_answer_timer.start()
        pbx.logger.info(f"Started no-answer timer ({no_answer_timeout}s) for call {call_id}")

        return True

    def _route_to_trunk(
        self,
        from_ext: str,
        to_ext: str,
        call_id: str,
        message: Any,
        from_addr: tuple[str, int],
    ) -> bool:
        """
        Route an outbound call to an external number via a SIP trunk.

        Mirrors the internal-extension path in ``route_call``: allocates an
        RTP relay, builds an INVITE toward the trunk, and sends it with
        retransmission. The trunk's response (180/183/200/4xx) is handled
        by the existing call_id-keyed logic in ``SIPServer._handle_response``,
        the same code path used for internal calls.

        Args:
            from_ext: Calling extension.
            to_ext: Dialed (untransformed) external number.
            call_id: SIP Call-ID.
            message: Original INVITE message from the caller.
            from_addr: Caller's address tuple.

        Returns:
            True if the call was routed to a trunk successfully.
        """
        from pbx.sip.message import SIPMessageBuilder
        from pbx.sip.sdp import SDPBuilder

        pbx = self.pbx_core

        trunk, transformed_number = pbx.trunk_system.route_outbound_with_failover(to_ext)
        if not trunk:
            pbx.logger.warning(f"No outbound trunk route found for {to_ext}")
            return False

        # route_outbound_with_failover() (and its failover/priority-fallback
        # helpers) already check can_make_call() -- the same check
        # allocate_channel() makes -- before returning a trunk, and this
        # runs synchronously with no yield point in between, so this should
        # essentially never fail. No further trunk is tried if it does; the
        # call fails cleanly rather than silently misbehaving.
        if not trunk.allocate_channel():
            pbx.logger.warning(f"Trunk {trunk.name} has no free channels for {to_ext}")
            return False

        # Parse caller's SDP offer (same as the internal-call path)
        caller_sdp: dict[str, Any] | None = None
        if message.body:
            from pbx.sip.sdp import SDPSession

            caller_sdp_obj = SDPSession()
            caller_sdp_obj.parse(message.body)
            caller_sdp = caller_sdp_obj.get_audio_info()

        call = pbx.call_manager.create_call(call_id, from_ext, to_ext)
        call.start()
        call.original_invite = message
        call.trunk = trunk  # so end_call() can release the channel / record outcome

        pbx.cdr_system.start_record(call_id, from_ext, to_ext)
        pbx.webhook_system.trigger_event(
            WebhookEvent.CALL_STARTED,
            {
                "call_id": call_id,
                "from_extension": from_ext,
                "to_extension": to_ext,
                "timestamp": call.start_time.isoformat() if call.start_time else None,
            },
        )

        call.caller_addr = from_addr

        rtp_ports = pbx.rtp_relay.allocate_relay(call_id)
        if not rtp_ports:
            pbx.logger.error(f"Failed to allocate RTP relay for call {call_id}")
            trunk.release_channel()
            pbx.cdr_system.end_record(call_id, hangup_cause="resource_unavailable")
            pbx.call_manager.end_call(call_id)
            return False

        call.rtp_ports = rtp_ports

        if caller_sdp:
            call.caller_rtp = caller_sdp
            caller_endpoint = (caller_sdp["address"], caller_sdp["port"])
            relay_info = pbx.rtp_relay.active_relays.get(call_id)
            if relay_info:
                relay_info["handler"].set_endpoints(caller_endpoint, None)

        server_ip = pbx._get_server_ip()
        dtmf_payload_type = pbx._get_dtmf_payload_type()
        ilbc_mode = pbx._get_ilbc_mode()

        caller_protocol = "RTP/AVP"
        caller_crypto: list[str] | None = None
        caller_codecs: list[str] | None = None
        if caller_sdp:
            caller_protocol = caller_sdp.get("protocol", "RTP/AVP")
            caller_crypto = caller_sdp.get("crypto") or None
            caller_codecs = caller_sdp.get("formats", None)

        # Only offer the trunk codecs the caller actually offered, so we never
        # negotiate a codec on this leg that the caller-side leg can't produce
        # (the RTP relay does not transcode between legs).
        trunk_codecs = pbx._get_compatible_trunk_codecs(trunk.codec_preferences, caller_codecs)

        trunk_sdp_body = SDPBuilder.build_audio_sdp(
            server_ip,
            rtp_ports[0],
            session_id=call_id,
            codecs=trunk_codecs,
            dtmf_payload_type=dtmf_payload_type,
            ilbc_mode=ilbc_mode,
            protocol=caller_protocol,
            crypto=caller_crypto,
        )

        # Resolve the calling extension's own DID as the outbound caller ID.
        # The carrier needs a real E.164 number here, not the internal
        # extension number -- carriers commonly reject or mangle a From/PAI
        # user part that isn't dialable, and sending a number the account
        # doesn't actually own conflicts with Truth-in-Caller-ID. Falls back
        # to from_ext (previous behavior) if the extension has no DID set.
        caller_ext_obj = pbx.extension_registry.get(from_ext)
        caller_id_number = from_ext
        caller_id_name = from_ext
        if caller_ext_obj:
            did_number = caller_ext_obj.config.get("did_number")
            if did_number:
                caller_id_number = did_number
            name = getattr(caller_ext_obj, "name", None)
            if name:
                caller_id_name = name

        trunk_addr = (trunk.host, trunk.port)
        invite_to_trunk = SIPMessageBuilder.build_request(
            method="INVITE",
            uri=f"sip:{transformed_number}@{trunk.host}:{trunk.port}",
            from_addr=f"<sip:{caller_id_number}@{server_ip}>",
            to_addr=f"<sip:{transformed_number}@{trunk.host}>",
            call_id=call_id,
            cseq=int((message.get_header("CSeq") or "1 INVITE").split()[0]),
            body=trunk_sdp_body,
        )

        # Add Contact header identifying the dialed number, per carrier
        # convention. Via, Content-type, and Max-Forwards are attached by
        # _build_and_send_leg_invite() below.
        sip_port = pbx.config.get("server.sip_port", 5060)
        invite_to_trunk.set_header("Contact", f"<sip:{transformed_number}@{server_ip}:{sip_port}>")

        # If the trunk challenges this INVITE with a 401/407 (some providers
        # require Digest auth on INVITE in addition to REGISTER),
        # SIPServer._handle_response retries once with credentials via
        # _retry_trunk_invite_with_auth() -- this initial INVITE is
        # deliberately sent unauthenticated.

        SIPMessageBuilder.add_caller_id_headers(
            invite_to_trunk, caller_id_number, caller_id_name, server_ip
        )

        self._build_and_send_leg_invite(
            call,
            call_id,
            invite_to_trunk,
            trunk_addr,
            server_ip,
            on_timeout=lambda: self._handle_invite_timeout(call_id),
        )

        pbx.logger.info(
            f"Routing call {call_id}: {from_ext} -> {transformed_number} via trunk "
            f"{trunk.name} ({trunk_addr[0]}:{trunk_addr[1]})"
        )

        # Deliberately no PBX-initiated no-answer timer here, unlike the
        # internal-call path. Standard PBX behavior (e.g. Asterisk's Dial()
        # defaults to an effectively unlimited ring duration) lets an
        # outbound call keep ringing once the callee's side has started
        # ringing -- the PBX doesn't own the callee's voicemail, and
        # carrier ring-to-voicemail timing varies and is often longer than
        # a fixed internal timeout, so cutting the call off preemptively
        # can sever it moments before the callee's own voicemail would
        # have answered. The call ends when the caller hangs up, the
        # trunk sends a final response, or -- if the trunk never responds
        # to the INVITE at all -- when InviteClientTransaction's own
        # RFC 3261 retry timeout fires via _handle_invite_timeout(), which
        # still routes through _end_unanswered_trunk_call() below.

        return True

    def _check_dialplan(self, extension: str) -> bool:
        """
        Check if extension matches dialplan rules

        Args:
            extension: Extension number

        Returns:
            True if allowed by dialplan
        """
        dialplan: dict[str, str] = self.pbx_core.config.get("dialplan", {})

        # Check emergency pattern (Kari's Law - direct 911 dialing)
        # Always allow 911 and legacy formats (9911, 9-911)
        emergency_pattern: str = dialplan.get("emergency_pattern", "^9?-?911$")
        if re.match(emergency_pattern, extension):
            return True

        # Check internal pattern
        internal_pattern: str = dialplan.get("internal_pattern", "^1[0-9]{3}$")
        if re.match(internal_pattern, extension):
            return True

        # Check conference pattern
        conference_pattern: str = dialplan.get("conference_pattern", "^2[0-9]{3}$")
        if re.match(conference_pattern, extension):
            return True

        # Check voicemail pattern
        voicemail_pattern: str = dialplan.get("voicemail_pattern", "^\\*[0-9]{3,4}$")
        if re.match(voicemail_pattern, extension):
            return True

        # Check auto attendant pattern
        auto_attendant_pattern: str = dialplan.get("auto_attendant_pattern", "^0$")
        if re.match(auto_attendant_pattern, extension):
            return True

        # Check parking pattern
        parking_pattern: str = dialplan.get("parking_pattern", "^7[0-9]$")
        if re.match(parking_pattern, extension):
            return True

        # Check queue pattern
        queue_pattern: str = dialplan.get("queue_pattern", "^8[0-9]{3}$")
        return bool(re.match(queue_pattern, extension))

    def _handle_invite_timeout(self, call_id: str) -> None:
        """Handle INVITE transaction timeout (no response from callee)."""
        pbx = self.pbx_core
        call = pbx.call_manager.get_call(call_id)
        if not call:
            return
        pbx.logger.warning(
            f"INVITE timeout for call {call_id} to {call.to_extension} - callee unreachable"
        )
        # Note: Do NOT unregister the extension here.  A single missed call
        # (e.g., DND, user away, transient network blip) should not permanently
        # take the extension offline.  The registration expiry mechanism handles
        # truly unreachable devices.  Previously this unregistered the callee
        # on every no-answer, causing the extension to be offline until the
        # phone re-registered.
        pbx.logger.info(f"Extension {call.to_extension} did not answer (not unregistering)")
        # Route to voicemail or end the call
        self._handle_no_answer(call_id)

    def handle_redirect(self, call_id: str, contact_header: str | None) -> None:
        """
        Handle a SIP 3xx (e.g. 302 Moved Temporarily) redirect from the
        callee: parse the target extension out of the response's Contact
        header and re-dial it on the same call, so ringing, no-answer
        handling, and voicemail mailbox selection all follow the forwarded
        destination instead of the extension that was originally dialed.

        Args:
            call_id: Call identifier.
            contact_header: Raw Contact header from the 3xx response.
        """
        from pbx.core.call import CallState

        pbx = self.pbx_core
        call = pbx.call_manager.get_call(call_id)
        if not call:
            return

        # A response that arrives after the caller's leg is already
        # answered (connected, or already sent to voicemail) is stale --
        # acting on it now would clobber a live call.
        if call.state == CallState.CONNECTED or call.routed_to_voicemail:
            pbx.logger.debug(f"Ignoring redirect for call {call_id}: caller leg already answered")
            return

        if call.no_answer_timer:
            call.no_answer_timer.cancel()
            call.no_answer_timer = None

        contact_match = re.search(r"sip:([^@;>]+)@", contact_header or "")
        target_ext = contact_match.group(1) if contact_match else None

        if not target_ext or not target_ext.isdigit():
            pbx.logger.warning(
                f"Redirect for call {call_id} has no usable extension in Contact "
                f"({contact_header!r}); cannot forward, falling back to voicemail"
            )
            self._handle_no_answer(call_id)
            return

        call.redirect_count += 1
        if call.redirect_count > self.MAX_REDIRECTS:
            pbx.logger.warning(
                f"Call {call_id} exceeded max redirects ({self.MAX_REDIRECTS}); "
                "falling back to voicemail"
            )
            self._handle_no_answer(call_id)
            return

        if target_ext in (call.from_extension, call.to_extension):
            pbx.logger.warning(
                f"Redirect for call {call_id} points back to {target_ext} "
                "(loop); falling back to voicemail"
            )
            self._handle_no_answer(call_id)
            return

        pbx.logger.info(
            f"Call {call_id} redirected from {call.to_extension} to {target_ext} "
            f"(3xx, redirect #{call.redirect_count})"
        )

        # Clear the stale callee leg before dialing the new target.
        call.callee_addr = None
        call.callee_invite = None
        call.invite_transaction = None

        original_to_header = call.original_invite.get_header("To") if call.original_invite else ""
        from_header = (
            call.original_invite.get_header("From") if call.original_invite else ""
        ) or ""
        to_header = re.sub(
            r"sip:(\*?[^@]+)@", f"sip:{target_ext}@", original_to_header or "", count=1
        )

        if self._dial_extension_leg(call, call_id, target_ext, from_header, to_header):
            call.to_extension = target_ext
        else:
            pbx.logger.warning(
                f"Could not redirect call {call_id} to {target_ext}; falling back to voicemail"
            )
            self._handle_no_answer(call_id)

    def _send_cancel_to_callee(self, call: Any, call_id: str) -> None:
        """Send CANCEL to callee to stop their phone from ringing"""
        from pbx.sip.message import SIPMessageBuilder

        if not (
            hasattr(call, "callee_addr")
            and call.callee_addr
            and hasattr(call, "callee_invite")
            and call.callee_invite
        ):
            return

        cancel_request = SIPMessageBuilder.build_request(
            method="CANCEL",
            uri=call.callee_invite.uri,
            from_addr=call.callee_invite.get_header("From"),
            to_addr=call.callee_invite.get_header("To"),
            call_id=call_id,
            cseq=int((call.callee_invite.get_header("CSeq") or "1 CANCEL").split()[0]),
        )
        cancel_request.set_header("Via", call.callee_invite.get_header("Via"))
        self.pbx_core.sip_server._send_message(cancel_request.build(), call.callee_addr)
        self.pbx_core.logger.info(f"Sent CANCEL to callee {call.to_extension} to stop ringing")

    def _answer_call_for_voicemail(self, call: Any, call_id: str) -> bool:
        """Answer call for voicemail recording"""
        from pbx.sip.message import SIPMessageBuilder
        from pbx.sip.sdp import SDPBuilder

        pbx = self.pbx_core

        if not (call.original_invite and call.caller_addr and call.caller_rtp and call.rtp_ports):
            return False

        server_ip: str = pbx._get_server_ip()
        caller_user_agent = pbx._get_phone_user_agent(call.from_extension)
        caller_phone_model = pbx._detect_phone_model(caller_user_agent)
        caller_codecs: list[str] | None = (
            call.caller_rtp.get("formats", None) if call.caller_rtp else None
        )
        # Use _get_compatible_codecs to intersect the caller's offered codecs
        # with the phone model's supported codecs.  Using _get_codecs_for_phone_model
        # would return ALL model codecs, ignoring what the caller actually offered,
        # which can cause 488 rejections or codec mismatches.
        codecs_for_caller = pbx._get_compatible_codecs(caller_phone_model, caller_codecs)

        if caller_phone_model:
            pbx.logger.info(
                f"Voicemail: Detected caller phone model: {caller_phone_model}, "
                f"offering codecs: {codecs_for_caller}"
            )

        # Build SDP for the voicemail recording endpoint
        dtmf_payload_type = pbx._get_dtmf_payload_type()
        ilbc_mode = pbx._get_ilbc_mode()

        # Preserve the caller's media protocol and SRTP crypto.
        vm_protocol = "RTP/AVP"
        vm_crypto: list[str] | None = None
        if call.caller_rtp:
            vm_protocol = call.caller_rtp.get("protocol", "RTP/AVP")
            vm_crypto = call.caller_rtp.get("crypto") or None

        # Omit rtpmap for static PTs on Zultys phones to avoid codec name
        # mismatch errors in their RTP engine.
        skip_rtpmap = pbx._should_skip_static_rtpmap(caller_phone_model)

        voicemail_sdp = SDPBuilder.build_audio_sdp(
            server_ip,
            call.rtp_ports[0],
            session_id=call_id,
            codecs=codecs_for_caller,
            dtmf_payload_type=dtmf_payload_type,
            ilbc_mode=ilbc_mode,
            protocol=vm_protocol,
            crypto=vm_crypto,
            skip_static_rtpmap=skip_rtpmap,
        )

        # Send 200 OK to answer the call for voicemail recording
        ok_response = SIPMessageBuilder.build_response(
            200, "OK", call.original_invite, body=voicemail_sdp
        )
        ok_response.set_header("Content-type", "application/sdp")

        # Build Contact header
        sip_port: int = pbx.config.get("server.sip_port", 5060)
        contact_uri: str = f"<sip:{call.to_extension}@{server_ip}:{sip_port}>"
        ok_response.set_header("Contact", contact_uri)

        # Remember the dialog's To header (which now carries the to-tag our
        # 200 OK generated) so a proper in-dialog BYE can be sent to the
        # caller when the recording completes (timeout or # keypress).
        call.voicemail_dialog_to = ok_response.get_header("To")

        # Send to caller
        pbx.sip_server._send_message(ok_response.build(), call.caller_addr)
        pbx.logger.info(f"Answered call {call_id} for voicemail recording")
        call.connect()
        return True

    def _end_unanswered_trunk_call(self, call: Any, call_id: str) -> None:
        """
        Cleanly end an outbound trunk call that did not complete.

        Reached from ``_handle_no_answer`` only via ``_handle_invite_timeout``
        (``_route_to_trunk`` starts no separate no-answer timer, so a trunk
        call that started ringing keeps ringing indefinitely) -- i.e. this
        only fires when the trunk never sent any response to the INVITE at
        all within its own retry window.

        Unlike an internal extension, a dialed external number has no PBX-owned
        voicemail box, so falling through to ``_answer_call_for_voicemail()``
        would incorrectly answer the *internal caller's* leg and record their
        voice into a mailbox auto-created for the dialed PSTN number. Instead,
        send a final 480 Temporarily Unavailable to the caller's original
        INVITE -- the same response code this codebase already uses elsewhere
        for "reachable but not currently available" -- and release resources.

        Duplicates the trunk-channel-release/failure-recording lines from
        ``PBXCore.end_call()`` rather than calling it, so this path can record
        an accurate ``"no_answer"`` hangup_cause; ``end_call()`` hardcodes
        ``"normal_clearing"`` with no way to override it.

        Args:
            call: The Call object for the unanswered trunk call.
            call_id: Call identifier.
        """
        from pbx.sip.message import SIPMessageBuilder

        pbx = self.pbx_core

        if call.original_invite and call.caller_addr:
            response = SIPMessageBuilder.build_response(
                480, "Temporarily Unavailable", call.original_invite
            )
            pbx.sip_server._send_message(response.build(), call.caller_addr)
            pbx.logger.info(
                f"Sent 480 Temporarily Unavailable to caller for unanswered trunk call {call_id}"
            )

        if hasattr(call, "trunk") and call.trunk:
            call.trunk.release_channel()
            call.trunk.record_failed_call(reason="no answer")

        pbx.rtp_relay.release_relay(call_id)
        pbx.cdr_system.end_record(call_id, hangup_cause="no_answer")
        pbx.call_manager.end_call(call_id)

    def _handle_no_answer(self, call_id: str) -> None:
        """
        Handle no-answer timeout.

        Internal calls (the dialed extension has a PBX-owned voicemail box)
        are answered into voicemail so the caller can leave a message, via
        the ``voicemail.no_answer_timeout`` timer started in ``route_call``.

        Outbound trunk calls (dialed an external number) have no such
        destination -- see ``_end_unanswered_trunk_call``, which ends the
        call cleanly instead of answering the internal caller into a bogus
        mailbox keyed by the dialed PSTN number. Trunk calls have no
        equivalent no-answer timer (``_route_to_trunk`` does not start one,
        matching standard PBX behavior of letting an outbound call ring
        indefinitely once the callee's side starts ringing), so this method
        is only reached for a trunk call via ``_handle_invite_timeout`` --
        i.e. only when the trunk never responded to the INVITE at all.

        Args:
            call_id: Call identifier
        """
        from pbx.core.call import CallState
        from pbx.rtp.handler import RTPPlayer
        from pbx.utils.audio import get_prompt_audio

        pbx = self.pbx_core

        call = pbx.call_manager.get_call(call_id)
        if not call:
            pbx.logger.warning(f"No-answer timeout for non-existent call {call_id}")
            return

        # Check if call was already answered or routed
        if call.state == CallState.CONNECTED:
            pbx.logger.debug(f"Call {call_id} already answered, ignoring no-answer timeout")
            return

        if call.routed_to_voicemail:
            pbx.logger.debug(f"Call {call_id} no-answer timeout already handled")
            return

        # A transfer target leg that never answered has no live caller to route
        # into voicemail (the transferor may already have dropped); let the
        # transfer session resolve it -- recalling the transferor or tearing
        # every remaining leg down, per the configured failure policy.
        if call.transfer_session_id:
            from pbx.core.transfer_session import LegEvent, LegEventResult

            result = pbx.transfer_handler.on_leg_event(call, LegEvent.TIMEOUT, side="callee")
            if result is not LegEventResult.FORWARD:
                return

        # routed_to_voicemail is reused as a general "no-answer timeout
        # already handled" guard, not only a voicemail flag -- also set on
        # the trunk (no-voicemail) branch below to prevent double-handling.
        call.routed_to_voicemail = True

        # Cancel INVITE retransmission if still running
        if hasattr(call, "invite_transaction") and call.invite_transaction:
            call.invite_transaction.cancel()
            call.invite_transaction = None

        # Send CANCEL to the callee (or trunk) to stop it from ringing
        self._send_cancel_to_callee(call, call_id)

        if hasattr(call, "trunk") and call.trunk:
            pbx.logger.info(f"No answer for outbound trunk call {call_id}, ending call")
            self._end_unanswered_trunk_call(call, call_id)
            return

        pbx.logger.info(f"No answer for call {call_id}, routing to voicemail")

        # Answer the call to allow voicemail recording.
        # For WebRTC-originated calls, caller_addr is None (no SIP
        # endpoint to send a 200 OK to), so the voicemail answer
        # path will fail.  In that case, end the call so the WebRTC
        # client's status polling sees "ended" and stops ringing.
        if not self._answer_call_for_voicemail(call, call_id):
            pbx.logger.info(
                f"Cannot answer call {call_id} for voicemail "
                "(WebRTC or missing caller info), ending call"
            )
            pbx.rtp_relay.release_relay(call_id)
            pbx.cdr_system.end_record(call_id, hangup_cause="normal_clearing")
            pbx.call_manager.end_call(call_id)
            return

        # Stop the RTP relay handler so its socket is released and the
        # voicemail player/recorder can bind to the same port.  The relay
        # handler was allocated for the original call but voicemail takes
        # over the port for direct player/recorder use.
        relay_info = pbx.rtp_relay.active_relays.get(call_id)
        if relay_info:
            relay_info["handler"].stop()
            pbx.logger.info(f"Stopped RTP relay handler for voicemail on call {call_id}")

        # Play voicemail greeting and beep tone to caller
        if call.caller_rtp:
            try:
                import tempfile

                # Create RTP player to send audio to caller
                player = RTPPlayer(
                    local_port=call.rtp_ports[0],
                    remote_host=call.caller_rtp["address"],
                    remote_port=call.caller_rtp["port"],
                    call_id=call_id,
                )
                if player.start():
                    # Check for custom greeting first
                    mailbox = pbx.voicemail_system.get_mailbox(call.to_extension)
                    custom_greeting_path = mailbox.get_greeting_path()
                    greeting_file: str | None = None
                    temp_file_created: bool = False

                    if custom_greeting_path:
                        # Use custom greeting
                        greeting_file = custom_greeting_path
                        pbx.logger.info(
                            f"Using custom greeting for extension {call.to_extension}: {custom_greeting_path}"
                        )
                        # Verify file exists and is readable
                        if Path(custom_greeting_path).exists():
                            file_size = Path(custom_greeting_path).stat().st_size
                            pbx.logger.info(f"Custom greeting file exists ({file_size} bytes)")
                        else:
                            pbx.logger.warning(
                                f"Custom greeting file not found at {custom_greeting_path}, using default"
                            )
                            custom_greeting_path = None  # Fall back to default

                    if not custom_greeting_path:
                        # Use default prompt: "Please leave a message after the tone"
                        # Try to load from voicemail_prompts/leave_message.wav, fallback to tone generation
                        greeting_prompt = get_prompt_audio("leave_message")
                        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
                            temp_file.write(greeting_prompt)
                            greeting_file = temp_file.name
                            temp_file_created = True
                        pbx.logger.info(f"Using default greeting for extension {call.to_extension}")

                    if greeting_file:
                        try:
                            player.play_file(greeting_file)
                            time.sleep(0.3)  # Brief pause before beep
                        finally:
                            # Clean up temp file only if we created one
                            if temp_file_created:
                                try:
                                    Path(greeting_file).unlink()
                                except (OSError, FileNotFoundError) as e:
                                    pbx.logger.debug(f"Could not delete temp greeting file: {e}")

                    # Play beep tone (1000 Hz, 500ms)
                    player.play_beep(frequency=1000, duration_ms=500)
                    player.stop()
                    pbx.logger.info(f"Played voicemail greeting and beep for call {call_id}")
                else:
                    pbx.logger.warning(f"Failed to start RTP player for greeting on call {call_id}")
            except OSError as e:
                pbx.logger.error(f"Error playing voicemail greeting: {e}")

            # Start the receive side on the allocated port: RTPRecorder +
            # DTMFMonitor merging every DTMF source (RFC 2833 telephone-event
            # -- including the caller's own offered payload types -- SIP INFO,
            # and in-band tones), so an out-of-band or in-band # from the
            # caller stops the recording via monitor_voicemail_dtmf.
            from pbx.rtp.dtmf_monitor import build_ivr_dtmf_channel

            recorder, _dtmf_monitor = build_ivr_dtmf_channel(pbx, call, call_id, call.rtp_ports[0])
            if recorder.start():
                # Store recorder in call object for later retrieval
                call.voicemail_recorder = recorder

                # set recording timeout (max voicemail duration)
                max_duration: int = pbx.config.get("voicemail.max_message_duration", 180)

                # Schedule voicemail completion after max duration
                voicemail_timer = threading.Timer(
                    max_duration,
                    pbx.voicemail_handler.complete_voicemail_recording,
                    args=(call_id,),
                )
                voicemail_timer.daemon = True
                voicemail_timer.start()
                call.voicemail_timer = voicemail_timer

                # Start DTMF monitoring thread to detect # key press
                dtmf_monitor_thread = threading.Thread(
                    target=pbx.voicemail_handler.monitor_voicemail_dtmf,
                    args=(call_id, call, recorder),
                )
                dtmf_monitor_thread.daemon = True
                dtmf_monitor_thread.start()

                pbx.logger.info(
                    f"Started voicemail recording for call {call_id}, max duration: {max_duration}s"
                )
            else:
                pbx.logger.error(f"Failed to start voicemail recorder for call {call_id}")
                pbx.end_call(call_id)
        else:
            pbx.logger.error(
                f"Cannot route call {call_id} to voicemail - missing required information"
            )
            # Fallback to ending the call
            pbx.end_call(call_id)
