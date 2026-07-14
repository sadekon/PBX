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
from pathlib import Path
from typing import Any

from pbx.features.webhooks import WebhookEvent
from pbx.sip.transaction import InviteClientTransaction


class CallRouter:
    """Handles call routing, dialplan checking, and no-answer fallback"""

    # Cap on SIP 3xx redirects followed for a single call, to guard against
    # misconfigured phones that redirect back into a loop.
    MAX_REDIRECTS = 5

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
        from pbx.sip.sdp import SDPSession

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

        # Check if this is an emergency call (911) - Kari's Law compliance
        # Must be handled first for immediate routing
        if pbx.karis_law and pbx.karis_law.is_emergency_number(to_ext):
            return bool(
                pbx._emergency_handler.handle_emergency_call(
                    from_ext, to_ext, call_id, message, from_addr
                )
            )

        # Check if this is an auto attendant call (extension 0)
        if pbx.auto_attendant and to_ext == pbx.auto_attendant.get_extension():
            return bool(
                pbx._auto_attendant_handler.handle_auto_attendant(
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
                pbx._voicemail_handler.handle_voicemail_access(
                    from_ext, to_ext, call_id, message, from_addr
                )
            )

        # Check if this is a paging call (7xx pattern or all-call)
        if pbx.paging_system and pbx.paging_system.is_paging_extension(to_ext):
            return bool(
                pbx._paging_handler.handle_paging(from_ext, to_ext, call_id, message, from_addr)
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
                db_phones = pbx.registered_phones_db.get_by_extension(to_ext)
                db_phone = db_phones[0] if db_phones else None
                if db_phone and db_phone.get("ip_address"):
                    phone_ip = db_phone["ip_address"]
                    phone_port = 5060  # Default SIP port
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
        is_webrtc_destination: bool = (
            dest_ext_obj.address
            and isinstance(dest_ext_obj.address, tuple)
            and len(dest_ext_obj.address) == 2
            and dest_ext_obj.address[0] == "webrtc"
        )

        if is_webrtc_destination:
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

        # Add required headers
        # Generate PBX's own Via header so responses come back to the PBX
        branch_id = str(uuid.uuid4()).replace("-", "")
        sip_port = pbx.config.get("server.sip_port", 5060)
        invite_to_callee.set_header(
            "Via",
            f"SIP/2.0/UDP {server_ip}:{sip_port};branch=z9hG4bK{branch_id}",
        )
        invite_to_callee.set_header(
            "Contact",
            f"<sip:{from_ext}@{server_ip}:{sip_port}>",
        )
        invite_to_callee.set_header("Content-type", "application/sdp")
        # Max-Forwards is mandatory per RFC 3261 Section 8.1.1.6.
        # Some strict SIP stacks reject INVITEs without it.
        invite_to_callee.set_header("Max-Forwards", "70")

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
        invite_txn = InviteClientTransaction(
            message=invite_to_callee.build(),
            dest_addr=dest_ext_obj.address,
            send_fn=pbx.sip_server._send_message,
            on_timeout=lambda: self._handle_invite_timeout(call_id),
        )
        invite_txn.start()
        call.invite_transaction = invite_txn

        # Store callee address for later use (e.g., to send CANCEL if
        # routing to voicemail)
        call.callee_addr = dest_ext_obj.address
        call.callee_invite = invite_to_callee  # Store the INVITE for CANCEL

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

    def _handle_no_answer(self, call_id: str) -> None:
        """
        Handle no-answer timeout - route call to voicemail

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
            pbx.logger.debug(f"Call {call_id} already routed to voicemail")
            return

        # A transfer consultation leg whose bridge is pending has no live
        # caller to route into voicemail (the transferor already dropped);
        # abort the transfer and tear down both remaining legs instead.
        if call.is_transfer_consult and any(
            c.pending_transfer_consult_id == call_id for c in pbx.call_manager.get_active_calls()
        ):
            pbx.abort_pending_transfer(call, cancel_destination=True)
            return

        call.routed_to_voicemail = True
        pbx.logger.info(f"No answer for call {call_id}, routing to voicemail")

        # Cancel INVITE retransmission if still running
        if hasattr(call, "invite_transaction") and call.invite_transaction:
            call.invite_transaction.cancel()
            call.invite_transaction = None

        # Send CANCEL to the callee to stop their phone from ringing
        self._send_cancel_to_callee(call, call_id)

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
                    pbx._voicemail_handler.complete_voicemail_recording,
                    args=(call_id,),
                )
                voicemail_timer.daemon = True
                voicemail_timer.start()
                call.voicemail_timer = voicemail_timer

                # Start DTMF monitoring thread to detect # key press
                dtmf_monitor_thread = threading.Thread(
                    target=pbx._voicemail_handler.monitor_voicemail_dtmf,
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
