"""
Paging handler for PBX Core

Extracts paging system logic from PBXCore into a dedicated class,
including page initiation, zone management, and audio routing to DAC devices.
"""

import time
from typing import Any


class PagingHandler:
    """Handles paging system calls and audio routing"""

    def __init__(self, pbx_core: Any) -> None:
        """
        Initialize PagingHandler with reference to PBXCore.

        Args:
            pbx_core: The PBXCore instance
        """
        self.pbx_core: Any = pbx_core

    def handle_paging(
        self,
        from_ext: str,
        to_ext: str,
        call_id: str,
        message: Any,
        from_addr: tuple[str, int],
    ) -> bool:
        """
        Handle paging system calls (7xx pattern or all-call)

        Args:
            from_ext: Calling extension
            to_ext: Paging extension (e.g., 700, 701, 702)
            call_id: Call ID
            message: SIP INVITE message
            from_addr: Caller address

        Returns:
            True if call was handled
        """
        import threading

        from pbx.sip.message import SIPMessageBuilder
        from pbx.sip.sdp import SDPBuilder, SDPSession

        pbx = self.pbx_core

        pbx.logger.info(f"Paging call: {from_ext} -> {to_ext}")

        # Initiate the page through the paging system
        page_id: str | None = pbx.paging_system.initiate_page(from_ext, to_ext)
        if not page_id:
            pbx.logger.error(f"Failed to initiate page from {from_ext} to {to_ext}")
            return False

        # Get zone information
        page_info: dict[str, Any] | None = pbx.paging_system.get_page_info(page_id)
        if not page_info:
            pbx.logger.error(f"Failed to get page info for {page_id}")
            return False

        # Parse SDP from caller's INVITE
        caller_sdp: dict[str, Any] | None = None
        caller_codecs: list[str] | None = None
        if message.body:
            caller_sdp_obj = SDPSession()
            caller_sdp_obj.parse(message.body)
            caller_sdp = caller_sdp_obj.get_audio_info()

            if caller_sdp:
                pbx.logger.info(f"Paging caller RTP: {caller_sdp['address']}:{caller_sdp['port']}")
                # Extract caller's codec list for negotiation
                caller_codecs = caller_sdp.get("formats", None)

        # Create call for paging
        call = pbx.call_manager.create_call(call_id, from_ext, to_ext)
        call.start()
        call.original_invite = message
        call.caller_addr = from_addr
        call.caller_rtp = caller_sdp
        call.paging_active = True
        call.page_id = page_id
        call.paging_zones = page_info.get("zone_names", "Unknown")

        # Start CDR record for analytics
        pbx.cdr_system.start_record(call_id, from_ext, to_ext)

        # Allocate RTP ports
        rtp_ports = pbx.rtp_relay.allocate_relay(call_id)
        if rtp_ports:
            call.rtp_ports = rtp_ports
        else:
            pbx.logger.error(f"Failed to allocate RTP ports for paging {call_id}")
            pbx.end_call(call_id)
            return False

        # Get configured paging gateway device
        if not page_info.get("zones"):
            pbx.logger.error(f"No zones configured for paging extension {to_ext}")
            pbx.end_call(call_id)
            return False

        dac_device: dict[str, Any] | None = self._resolve_dac_device(page_info)

        # Answer the call immediately (auto-answer for paging)
        server_ip: str = pbx._get_server_ip()

        # Determine which codecs to offer based on caller's phone model
        # Get caller's User-Agent to detect phone model
        caller_user_agent = pbx._get_phone_user_agent(from_ext)
        caller_phone_model = pbx._detect_phone_model(caller_user_agent)

        # Select appropriate codecs for the caller's phone
        codecs_for_caller = pbx._get_codecs_for_phone_model(
            caller_phone_model, default_codecs=caller_codecs
        )

        if caller_phone_model:
            pbx.logger.info(
                f"Paging: Detected caller phone model: {caller_phone_model}, "
                f"offering codecs: {codecs_for_caller}"
            )

        # Build SDP for answering, using phone-model-specific codecs
        # Get DTMF payload type from config
        dtmf_payload_type = pbx._get_dtmf_payload_type()
        ilbc_mode = pbx._get_ilbc_mode()

        # Omit rtpmap for static PTs on Zultys phones to avoid codec name
        # mismatch errors in their RTP engine.
        skip_rtpmap = pbx._should_skip_static_rtpmap(caller_phone_model)

        paging_sdp = SDPBuilder.build_audio_sdp(
            server_ip,
            call.rtp_ports[0],
            session_id=call_id,
            codecs=codecs_for_caller,
            dtmf_payload_type=dtmf_payload_type,
            ilbc_mode=ilbc_mode,
            skip_static_rtpmap=skip_rtpmap,
        )

        # Send 200 OK to answer the call
        ok_response = SIPMessageBuilder.build_response(
            200, "OK", call.original_invite, body=paging_sdp
        )
        ok_response.set_header("Content-type", "application/sdp")

        # Build Contact header
        sip_port: int = pbx.config.get("server.sip_port", 5060)
        contact_uri: str = f"<sip:{to_ext}@{server_ip}:{sip_port}>"
        ok_response.set_header("Contact", contact_uri)

        # Send to caller
        pbx.sip_server._send_message(ok_response.build(), call.caller_addr)
        pbx.logger.info(f"Answered paging call {call_id} - Paging {page_info.get('zone_names')}")

        # Mark call as connected
        call.connect()

        # If we have a DAC device configured, route audio to it
        if dac_device:
            # Start paging session thread to handle audio routing
            paging_thread = threading.Thread(
                target=self._paging_session, args=(call_id, call, dac_device, page_info)
            )
            paging_thread.daemon = True
            paging_thread.start()
        else:
            # No hardware - just maintain the call for testing
            pbx.logger.warning(f"Paging call {call_id} connected but no DAC device available")
            pbx.logger.info(
                f"Audio from {from_ext} would be routed to {page_info.get('zone_names')}"
            )

        return True

    def _resolve_dac_device(self, page_info: dict[str, Any]) -> dict[str, Any] | None:
        """
        Resolve which DAC device carries a page's audio.

        ``initiate_page()`` already matched each target zone's ``dac_device``
        id against the configured device list, so this just picks from that
        result rather than re-scanning.

        Only the first resolved device is returned: a multi-zone or all-call
        page currently reaches one zone, not all of them. Fanning audio out to
        every device requires one SIP leg and relay branch per device, which
        the single-relay-per-call model here does not yet support.

        Args:
            page_info: Paging information dictionary from ``get_page_info()``

        Returns:
            The DAC device configuration, or None if the target zone(s) have
            no device assigned -- in which case the page is still answered so
            the announcer gets confirmation, but no audio reaches a speaker.
        """
        resolved: list[dict[str, Any]] = page_info.get("resolved_dac_devices") or []
        if not resolved:
            self.pbx_core.logger.warning(
                f"No DAC device assigned for zone(s) {page_info.get('zone_names')} -- "
                f"page will be answered but no audio will reach the speakers"
            )
            return None

        if len(resolved) > 1:
            self.pbx_core.logger.warning(
                f"Page {page_info.get('page_id')} targets {len(resolved)} DAC devices "
                f"but only {resolved[0].get('device_id')} will receive audio "
                f"(multi-device fan-out is not implemented)"
            )

        return resolved[0]

    def start_test_page(self, from_ext: str, zone_ext: str) -> dict[str, Any]:
        """
        Place a test page from the admin UI: ring `from_ext`, and once it
        answers, open a page from that phone to `zone_ext`.

        This is the inverse of ``handle_paging()``. There, the announcer dials
        in and the PBX answers; here the PBX calls the announcer, so the page
        cannot be opened until the origination is answered. The INVITE is sent
        before this returns -- the page itself begins later, in the answer
        callback.

        Args:
            from_ext: Extension to ring; whoever answers makes the announcement
            zone_ext: Paging zone extension (or the all-call extension)

        Returns:
            dict with ``call_id`` of the ringing leg, plus the resolved zone
            name. Raises ValueError if the request cannot be started at all.
        """
        pbx = self.pbx_core

        if not pbx.paging_system or not pbx.paging_system.enabled:
            raise ValueError("Paging system is not enabled")

        if not pbx.paging_system.is_paging_extension(zone_ext):
            raise ValueError(f"{zone_ext} is not a paging extension")

        # Fail before ringing anyone if the zone is unroutable -- a phone that
        # rings and then connects to nothing is a worse failure than an error.
        if zone_ext != pbx.paging_system.all_call_extension and not (
            pbx.paging_system.get_zone_for_extension(zone_ext)
        ):
            raise ValueError(f"No paging zone configured for extension {zone_ext}")

        call = pbx.call_originator.originate_call(
            "paging-test",
            from_ext,
            on_answer=lambda answered: self._begin_originated_page(answered, zone_ext),
            on_failure=lambda _call, reason: pbx.logger.warning(
                f"Test page to {zone_ext} aborted: {from_ext} did not answer ({reason})"
            ),
        )

        if not call:
            raise ValueError(f"Could not place a call to extension {from_ext}")

        pbx.logger.info(f"Test page originated: ringing {from_ext} to page {zone_ext}")
        return {"call_id": call.call_id, "from_extension": from_ext, "zone": zone_ext}

    def _begin_originated_page(self, call: Any, zone_ext: str) -> None:
        """
        Open the page once a test-page origination has been answered.

        Mirrors the tail of ``handle_paging()`` -- initiate the page, attach it
        to the call, and start the DAC session thread -- minus the SIP answer,
        which for an originated leg the far end has already sent.

        Args:
            call: The answered, PBX-originated `Call` to the announcer
            zone_ext: Paging zone extension being paged
        """
        import threading

        pbx = self.pbx_core

        page_id: str | None = pbx.paging_system.initiate_page(call.to_extension, zone_ext)
        if not page_id:
            pbx.logger.error(f"Test page failed: could not initiate page to {zone_ext}")
            pbx.end_call(call.call_id)
            return

        page_info: dict[str, Any] | None = pbx.paging_system.get_page_info(page_id)
        if not page_info:
            pbx.logger.error(f"Test page failed: no page info for {page_id}")
            pbx.end_call(call.call_id)
            return

        call.paging_active = True
        call.page_id = page_id
        call.paging_zones = page_info.get("zone_names", "Unknown")

        dac_device = self._resolve_dac_device(page_info)
        if not dac_device:
            pbx.logger.warning(
                f"Test page {page_id} connected to {call.to_extension} but no DAC "
                f"device is available -- audio will not reach the speakers"
            )
            return

        paging_thread = threading.Thread(
            target=self._paging_session,
            args=(call.call_id, call, dac_device, page_info),
            kwargs={"source_rtp": call.callee_rtp},
        )
        paging_thread.daemon = True
        paging_thread.start()

    def _paging_session(
        self,
        call_id: str,
        call: Any,
        dac_device: dict[str, Any],
        page_info: dict[str, Any],
        source_rtp: dict[str, Any] | None = None,
    ) -> None:
        """
        Handle paging session with audio routing to DAC device

        Args:
            call_id: Call identifier
            call: Call object
            dac_device: DAC device configuration
            page_info: Paging information dictionary
            source_rtp: RTP endpoint of the announcing party, whose audio is
                relayed to the DAC. Defaults to ``call.caller_rtp`` (the
                inbound-INVITE case, where the announcer dialed in). An
                originated page passes ``call.callee_rtp`` instead, since
                there the PBX placed the call and the announcer is the callee.
        """
        pbx = self.pbx_core

        if source_rtp is None:
            source_rtp = call.caller_rtp

        try:
            pbx.logger.info(f"Paging session started for {call_id}")
            pbx.logger.info(
                f"DAC device: {dac_device.get('device_id')} ({dac_device.get('device_type')})"
            )
            pbx.logger.info(f"Paging zones: {page_info.get('zone_names')}")

            # Get DAC device SIP information
            dac_sip_uri: str | None = dac_device.get("sip_uri")
            dac_ip: str | None = dac_device.get("ip_address")
            dac_port: int = dac_device.get("port", 5060)

            if not dac_sip_uri or not dac_ip:
                pbx.logger.error(
                    f"DAC device {dac_device.get('device_id')} missing SIP configuration"
                )
                return

            # Establish SIP connection to the DAC gateway device
            import uuid

            from pbx.sip.message import SIPMessageBuilder
            from pbx.sip.sdp import SDPBuilder

            server_ip: str = pbx._get_server_ip()
            sip_port: int = pbx.config.get("server.sip_port", 5060)
            dac_call_id = str(uuid.uuid4())

            # Build SDP for DAC connection (sendonly - paging is one-way audio)
            dac_rtp_port: int = call.rtp_ports[0] if call.rtp_ports else 10000
            dac_sdp = SDPBuilder.build_audio_sdp(server_ip, dac_rtp_port, session_id=dac_call_id)

            # Build INVITE to DAC device
            invite_msg = SIPMessageBuilder.build_request(
                method="INVITE",
                uri=dac_sip_uri,
                from_addr=f"<sip:paging@{server_ip}>",
                to_addr=f"<sip:paging@{dac_ip}:{dac_port}>",
                call_id=dac_call_id,
                cseq=1,
                body=dac_sdp,
            )
            invite_msg.set_header("Content-type", "application/sdp")
            invite_msg.set_header("Contact", f"<sip:paging@{server_ip}:{sip_port}>")

            # Add zone selection header if multi-zone DAC
            zones: list[dict[str, Any]] = page_info.get("zones", [])
            if zones:
                zone_id: str | None = zones[0].get("zone_id")
                if zone_id:
                    invite_msg.set_header("X-Paging-Zone", zone_id)

            # Send INVITE to DAC device
            dac_addr: tuple[str, int] = (dac_ip, dac_port)
            try:
                pbx.sip_server._send_message(invite_msg.build(), dac_addr)
                pbx.logger.info(f"Sent INVITE to DAC device at {dac_ip}:{dac_port}")
            except Exception as invite_err:
                pbx.logger.error(f"Failed to send INVITE to DAC: {invite_err}")
                return

            # Set up RTP relay to forward audio from the announcer to the DAC
            if source_rtp:
                caller_endpoint: tuple[str, int] = (
                    source_rtp["address"],
                    source_rtp["port"],
                )
                # Use the RTP port from our SDP offer (dac_rtp_port) as the
                # destination — the DAC device will send/receive RTP on
                # the port we advertised, not the SIP signaling port + 1.
                dac_endpoint: tuple[str, int] = (dac_ip, dac_rtp_port)

                # Configure RTP relay for forwarding
                pbx.rtp_relay.set_endpoints(call.call_id, caller_endpoint, dac_endpoint)
                pbx.logger.info(f"RTP relay configured: {caller_endpoint} -> {dac_endpoint}")
                pbx.logger.info(
                    f"Audio relay active: Caller -> PBX:{call.rtp_ports[0]} -> DAC:{dac_ip}"
                )

            # Monitor the call until it ends
            while call.state.value != "ended":
                time.sleep(1)

            # Send BYE to DAC device to end the paging session
            try:
                bye_msg = SIPMessageBuilder.build_request(
                    method="BYE",
                    uri=dac_sip_uri,
                    from_addr=f"<sip:paging@{server_ip}>",
                    to_addr=f"<sip:paging@{dac_ip}:{dac_port}>",
                    call_id=dac_call_id,
                    cseq=2,
                )
                pbx.sip_server._send_message(bye_msg.build(), dac_addr)
                pbx.logger.info("Sent BYE to DAC device")
            except Exception as bye_err:
                pbx.logger.error(f"Failed to send BYE to DAC: {bye_err}")

            # The page itself is released by PBXCore.end_call() as part of
            # normal call teardown, so it is not ended here -- this thread
            # only owns the DAC-side SIP dialog.
            pbx.logger.info(f"Paging session ended for {call_id}")

        except (KeyError, TypeError, ValueError) as e:
            pbx.logger.error(f"Error in paging session {call_id}: {e}")
            pbx.logger.debug("Paging session error details", exc_info=True)
