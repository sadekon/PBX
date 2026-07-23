"""
Auto-attendant handler for PBX Core

Extracts auto-attendant logic from PBXCore into a dedicated class,
including session management, DTMF input handling, and menu navigation.
"""

import contextlib
import time
from pathlib import Path
from typing import Any


class AutoAttendantHandler:
    """Handles auto-attendant call sessions with menu and DTMF input"""

    def __init__(self, pbx_core: Any) -> None:
        """
        Initialize AutoAttendantHandler with reference to PBXCore.

        Args:
            pbx_core: The PBXCore instance
        """
        self.pbx_core: Any = pbx_core

    def handle_auto_attendant(
        self,
        from_ext: str,
        to_ext: str,
        call_id: str,
        message: Any,
        from_addr: tuple[str, int],
    ) -> bool:
        """
        Handle auto attendant calls (extension 0)

        Args:
            from_ext: Calling extension
            to_ext: Destination (auto attendant extension, typically '0')
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

        pbx.logger.info(f"Auto attendant call: {from_ext} -> {to_ext}")

        # Parse SDP from caller's INVITE
        caller_sdp: dict[str, Any] | None = None
        caller_codecs: list[str] | None = None
        if message.body:
            caller_sdp_obj = SDPSession()
            caller_sdp_obj.parse(message.body)
            caller_sdp = caller_sdp_obj.get_audio_info()
            if caller_sdp:
                # Extract caller's codec list for negotiation
                caller_codecs = caller_sdp.get("formats", None)
                if caller_codecs:
                    pbx.logger.info(f"Auto attendant: Caller codecs: {caller_codecs}")

        # Create call for auto attendant
        call = pbx.call_manager.create_call(call_id, from_ext, to_ext)
        call.start()
        call.original_invite = message
        call.caller_addr = from_addr
        call.caller_rtp = caller_sdp
        call.auto_attendant_active = True

        # Start CDR record for analytics
        pbx.cdr_system.start_record(call_id, from_ext, to_ext)

        # Allocate RTP port for audio communication
        # For auto attendant, we don't need a relay (which forwards between two endpoints).
        # Instead, we directly play audio to the caller and listen for DTMF.
        # Find an available port from the RTP port pool.
        with pbx.rtp_relay._pool_lock:
            try:
                rtp_port: int = pbx.rtp_relay.port_pool.pop(0)
            except IndexError:
                pbx.logger.error(f"No available RTP ports for auto attendant {call_id}")
                return False

        rtcp_port: int = rtp_port + 1
        call.rtp_ports = (rtp_port, rtcp_port)
        pbx.logger.info(
            f"Allocated RTP port {rtp_port} for auto attendant {call_id} (no relay needed)"
        )

        # Store port allocation for cleanup
        call.aa_rtp_port = rtp_port

        try:
            # Send 180 Ringing first to provide ring-back tone to caller
            server_ip: str = pbx._get_server_ip()
            ringing_response = SIPMessageBuilder.build_response(
                180, "Ringing", call.original_invite
            )

            # Build Contact header for ringing response
            sip_port: int = pbx.config.get("server.sip_port", 5060)
            contact_uri: str = f"<sip:{to_ext}@{server_ip}:{sip_port}>"
            ringing_response.set_header("Contact", contact_uri)

            # Send ringing response to caller
            pbx.sip_server._send_message(ringing_response.build(), call.caller_addr)
            pbx.logger.info(f"Sent 180 Ringing for auto attendant call {call_id}")

            # Brief delay to allow ring-back tone to be established
            time.sleep(0.5)

            # Answer the call

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
                    f"Auto attendant: Detected caller phone model: {caller_phone_model}, "
                    f"offering codecs: {codecs_for_caller}"
                )

            # Build SDP for answering, using phone-model-specific codecs
            # Get DTMF payload type from config
            dtmf_payload_type = pbx._get_dtmf_payload_type()
            ilbc_mode = pbx._get_ilbc_mode()

            # Preserve the caller's media protocol and SRTP crypto attributes.
            caller_protocol = "RTP/AVP"
            caller_crypto: list[str] | None = None
            if caller_sdp:
                caller_protocol = caller_sdp.get("protocol", "RTP/AVP")
                caller_crypto = caller_sdp.get("crypto") or None

            # Omit rtpmap for static PTs on Zultys phones to avoid codec name
            # mismatch errors in their RTP engine.
            skip_rtpmap = pbx._should_skip_static_rtpmap(caller_phone_model)

            aa_sdp = SDPBuilder.build_audio_sdp(
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

            # Send 200 OK to answer the call
            ok_response = SIPMessageBuilder.build_response(
                200, "OK", call.original_invite, body=aa_sdp
            )
            ok_response.set_header("Content-type", "application/sdp")

            # Build Contact header
            sip_port = pbx.config.get("server.sip_port", 5060)
            contact_uri = f"<sip:{to_ext}@{server_ip}:{sip_port}>"
            ok_response.set_header("Contact", contact_uri)

            # Remember the dialog To header (with our to-tag) so downstream
            # features that adopt this call (queue overflow voicemail) can
            # send a proper in-dialog BYE to the caller, and so a bridge
            # teardown (_send_leg_bye reads caller_dialog_to, not this) can
            # reach the caller once this call is transferred/queued and the
            # far end hangs up first.
            call.voicemail_dialog_to = ok_response.get_header("To")
            call.caller_dialog_to = ok_response.get_header("To")

            # Send to caller
            pbx.sip_server._send_message(ok_response.build(), call.caller_addr)
            pbx.logger.info(f"Answered auto attendant call {call_id}")

            # Mark call as connected
            call.connect()

            # Start auto attendant session
            session = pbx.auto_attendant.start_session(call_id, from_ext)
            call.aa_session = session

            # Start auto attendant interaction thread
            aa_thread = threading.Thread(
                target=self._auto_attendant_session, args=(call_id, call, session)
            )
            aa_thread.daemon = True
            aa_thread.start()

            return True

        except Exception as e:
            pbx.logger.error(f"Auto attendant setup failed for {call_id}: {e}")
            # Return port to pool on setup failure
            with pbx.rtp_relay._pool_lock:
                pbx.rtp_relay.port_pool.append(rtp_port)
                pbx.rtp_relay.port_pool.sort()
            return False

    def _auto_attendant_session(
        self, call_id: str, call: Any, session: dict[str, Any], reentry: bool = False
    ) -> None:
        """
        Handle auto attendant session with menu and DTMF input

        Args:
            call_id: Call identifier
            call: Call object
            session: Auto attendant session
            reentry: True when this session was restarted after a failed
                transfer (see _return_to_menu). Skips the welcome greeting --
                the caller already heard it -- and goes straight to the menu.
        """
        import tempfile

        from pbx.rtp.dtmf_monitor import build_ivr_dtmf_channel
        from pbx.rtp.handler import RTPPlayer
        from pbx.utils.audio import get_prompt_audio

        pbx = self.pbx_core

        # Set once a transfer takes over the caller's port and call record
        # (see _begin_transfer): this session relinquishes ownership, so it
        # must not stop the player/recorder, return the port, or end the call
        # in cleanup -- the relay (on success) or the restarted menu session
        # (on failure) now owns them.
        handed_off = False
        try:
            # Wait for RTP to stabilize
            time.sleep(0.5)

            if not call.caller_rtp:
                pbx.logger.warning(f"No caller RTP info for auto attendant {call_id}")
                return

            # ============================================================
            # RTP SETUP FOR AUTO ATTENDANT - BIDIRECTIONAL AUDIO
            # ============================================================
            # 1. RTPPlayer: Sends audio prompts/menus to the caller.
            # 2. build_ivr_dtmf_channel: RTPRecorder + DTMFMonitor merging all
            #    DTMF sources (RFC 2833 telephone-event, SIP INFO, in-band
            #    G.711 tones) into one deduped digit queue.
            # All share the local port (call.rtp_ports[0]) from the RTP pool.
            # ============================================================

            # Create RTP player for sending audio prompts to the caller
            player = RTPPlayer(
                local_port=call.rtp_ports[0],
                remote_host=call.caller_rtp["address"],
                remote_port=call.caller_rtp["port"],
                call_id=call_id,
            )

            if not player.start():
                pbx.logger.error(f"Failed to start RTP player for auto attendant {call_id}")
                return

            recorder, dtmf_monitor = build_ivr_dtmf_channel(pbx, call, call_id, call.rtp_ports[0])
            if not recorder.start():
                pbx.logger.error(f"Failed to start RTP recorder for auto attendant {call_id}")
                player.stop()
                return
            pbx.logger.info(
                "Auto attendant RTP setup complete - bidirectional audio channel established"
            )

            # Barge-in predicate for menu prompts: True as soon as a digit is
            # pending from any source. has_digit() only *peeks* -- the digit
            # stays queued so the loop below still pops it via get_digit() and
            # feeds auto_attendant.handle_dtmf(), advancing the state machine.
            # Lets callers dial their choice over the greeting/menu without
            # waiting for it to finish.
            _dtmf_pending = dtmf_monitor.has_digit

            # Play welcome greeting
            action = session.get("session")
            audio_file: Path | None = session.get("file")

            pbx.logger.info(f"[Auto Attendant] Starting audio playback for call {call_id}")
            audio_played: bool = False

            # Welcome greeting -- skipped on re-entry after a failed transfer
            # (the caller already heard it; go straight back to the menu).
            if not reentry:
                if audio_file and Path(audio_file).exists():
                    pbx.logger.info(f"[Auto Attendant] Playing welcome file: {audio_file}")
                    audio_played = player.play_file(audio_file, interrupt_check=_dtmf_pending)
                    if audio_played:
                        pbx.logger.info("[Auto Attendant] ✓ Welcome audio played successfully")
                    else:
                        pbx.logger.error("[Auto Attendant] ✗ Failed to play welcome audio")
                else:
                    # Try to load from auto_attendant/welcome.wav, fallback to tone
                    # generation
                    pbx.logger.info("[Auto Attendant] Generating welcome prompt audio")
                    prompt_data = get_prompt_audio("welcome", prompt_dir="auto_attendant")
                    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
                        temp_file.write(prompt_data)
                        temp_file_path = temp_file.name
                    try:
                        audio_played = player.play_file(
                            temp_file_path, interrupt_check=_dtmf_pending
                        )
                        if audio_played:
                            pbx.logger.info(
                                "[Auto Attendant] ✓ Generated welcome audio played successfully"
                            )
                        else:
                            pbx.logger.error(
                                "[Auto Attendant] ✗ Failed to play generated welcome audio"
                            )
                    finally:
                        with contextlib.suppress(OSError):
                            Path(temp_file_path).unlink()

                time.sleep(0.5)

            # Play main menu
            pbx.logger.info(f"[Auto Attendant] Playing main menu for call {call_id}")
            menu_audio: Path | None = pbx.auto_attendant._get_audio_file("main_menu")
            if menu_audio and Path(menu_audio).exists():
                pbx.logger.info(f"[Auto Attendant] Playing menu file: {menu_audio}")
                audio_played = player.play_file(menu_audio, interrupt_check=_dtmf_pending)
                if audio_played:
                    pbx.logger.info("[Auto Attendant] ✓ Menu audio played successfully")
                else:
                    pbx.logger.error("[Auto Attendant] ✗ Failed to play menu audio")
            else:
                # Try to load from auto_attendant/main_menu.wav, fallback to
                # tone generation
                pbx.logger.info("[Auto Attendant] Generating menu prompt audio")
                prompt_data = get_prompt_audio("main_menu", prompt_dir="auto_attendant")
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
                    temp_file.write(prompt_data)
                    temp_file_path = temp_file.name
                try:
                    audio_played = player.play_file(temp_file_path, interrupt_check=_dtmf_pending)
                    if audio_played:
                        pbx.logger.info(
                            "[Auto Attendant] ✓ Generated menu audio played successfully"
                        )
                    else:
                        pbx.logger.error("[Auto Attendant] ✗ Failed to play generated menu audio")
                finally:
                    with contextlib.suppress(OSError):
                        Path(temp_file_path).unlink()

            # Main loop - wait for DTMF input
            session_active: bool = True
            timeout: int = pbx.auto_attendant.timeout
            start_time: float = time.time()

            while session_active and (time.time() - start_time) < timeout:
                # One call covers every DTMF source (RFC 2833 telephone-event,
                # SIP INFO, in-band G.711 tones), deduped by the monitor.
                digit: str | None = dtmf_monitor.get_digit(timeout=1.0)

                if digit:
                    pbx.logger.info(f"Auto attendant received DTMF: {digit}")

                    # Handle the input
                    result: dict[str, Any] = pbx.auto_attendant.handle_dtmf(
                        session["session"], digit
                    )
                    action = result.get("action")

                    if action == "transfer":
                        destination: str | None = result.get("destination")
                        pbx.logger.info(f"Auto attendant transferring to {destination}")
                        if call_id and destination:
                            self._begin_transfer(call_id, call, destination, player, recorder)
                            handed_off = True
                        else:
                            pbx.logger.warning("Cannot transfer call: no destination available")
                        session_active = False

                    elif action == "play":
                        # Play the requested audio
                        audio_file = result.get("file")
                        if audio_file and Path(audio_file).exists():
                            player.play_file(audio_file, interrupt_check=_dtmf_pending)

                        # Reset timeout
                        start_time = time.time()

                    # Update session
                    if "session" in result:
                        session["session"] = result["session"]

                    # The AA never saves the recording; drop accumulated audio
                    # so a long menu session doesn't grow memory. (In-band DTMF
                    # detection buffers independently inside the DTMFMonitor.)
                    if hasattr(recorder, "recorded_data"):
                        recorder.recorded_data = []

            # Timeout - handle it (but not if a transfer already handed off)
            if not handed_off and time.time() - start_time >= timeout:
                result = pbx.auto_attendant.handle_timeout(session["session"])
                action = result.get("action")

                if action == "transfer":
                    destination = result.get("destination")
                    pbx.logger.info(f"Auto attendant timeout, transferring to {destination}")
                    if call_id and destination:
                        self._begin_transfer(call_id, call, destination, player, recorder)
                        handed_off = True

            # Clean up -- skipped once a transfer handed off ownership of the
            # port and call record to the relay or the restarted menu session.
            if not handed_off:
                player.stop()
                recorder.stop()

                # Return port to pool (thread-safe)
                if hasattr(call, "aa_rtp_port"):
                    with pbx.rtp_relay._pool_lock:
                        pbx.rtp_relay.port_pool.append(call.aa_rtp_port)
                        pbx.rtp_relay.port_pool.sort()
                    pbx.logger.info(f"Returned RTP port {call.aa_rtp_port} to pool")

        except (KeyError, OSError, TypeError, ValueError) as e:
            pbx.logger.error(f"Error in auto attendant session: {e}")
            import traceback

            pbx.logger.error(traceback.format_exc())

            # Ensure port is returned even on error (thread-safe), unless a
            # transfer already handed the port off to another owner.
            if not handed_off and hasattr(call, "aa_rtp_port"):
                try:
                    with pbx.rtp_relay._pool_lock:
                        pbx.rtp_relay.port_pool.append(call.aa_rtp_port)
                        pbx.rtp_relay.port_pool.sort()
                except Exception as exc:
                    pbx.logger.error(f"Failed to return RTP port {call.aa_rtp_port}: {exc}")
        finally:
            # End the call only if this session still owns it -- a transfer
            # handoff leaves the call alive (bridged, or being re-served by a
            # restarted menu session).
            if not handed_off:
                time.sleep(1)
                pbx.end_call(call_id)

    def _begin_transfer(
        self, call_id: str, call: Any, destination: str, player: Any, recorder: Any
    ) -> None:
        """
        Hand the caller off to `destination` via the shared blind-transfer
        pipeline, keeping the PBX in the RTP path (the Asterisk-standard way
        to transfer a trunk/inbound call: the caller's leg is never
        re-signaled, only the far side of the relay changes).

        Promotes the single-leg IVR port to a two-party relay -- the raw
        RTPPlayer/RTPRecorder are stopped so the relay can rebind the same
        port, the caller becomes side A -- then originates the destination
        leg through TransferHandler.start_transfer as a blind transfer. The
        bridge completes when the destination answers (handle_callee_answer
        hands the event to the transfer session, which sets side B). The AA has
        no real transferor phone to hang up or recall, so it passes an
        on_failure hook: any failure returns the caller to the menu instead of
        applying the session's default failure policy (see _return_to_menu).

        The caller must treat the call/port as handed off after this returns.
        """
        import tempfile

        from pbx.utils.audio import get_prompt_audio

        pbx = self.pbx_core

        # Announce the transfer while the raw IVR audio path is still up.
        transfer_audio: Path | None = pbx.auto_attendant._get_audio_file("transferring")
        if transfer_audio and Path(transfer_audio).exists():
            player.play_file(transfer_audio)
        else:
            prompt_data = get_prompt_audio("transferring", prompt_dir="auto_attendant")
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
                temp_file.write(prompt_data)
                temp_file_path = temp_file.name
            try:
                player.play_file(temp_file_path)
            finally:
                with contextlib.suppress(OSError):
                    Path(temp_file_path).unlink()
        time.sleep(0.5)

        # Release the raw IVR sockets so the relay can rebind the same port.
        player.stop()
        recorder.stop()

        if not call.rtp_ports:
            pbx.logger.error(f"No RTP port for auto attendant transfer of {call_id}")
            self._return_to_menu(call_id, call)
            return

        rtp_port, rtcp_port = call.rtp_ports
        if not pbx.rtp_relay.adopt_existing_port(call_id, rtp_port, rtcp_port):
            pbx.logger.error(f"Failed to promote auto attendant port to relay for {call_id}")
            self._return_to_menu(call_id, call)
            return

        # Caller occupies side A; the destination will fill side B on answer.
        caller_ep = (call.caller_rtp["address"], call.caller_rtp["port"])
        pbx.rtp_relay.set_endpoints(call_id, caller_ep, None)

        # A real phone signals hold (a=sendonly re-INVITE) before REFERring,
        # which is what normally starts MOH for the party being transferred
        # (see SIPServer._handle_reinvite). The caller's leg here is never
        # re-signaled, so nothing would otherwise trigger it -- start it
        # explicitly so the caller hears hold music instead of silence while
        # the destination rings. The transfer bridge stops it on success;
        # _return_to_menu stops it on failure.
        relay_handler = pbx.rtp_relay.get_handler(call_id)
        if relay_handler:
            pbx.moh_system.start_moh(call_id, relay_handler, "a")

        # Queue destination: the queue adopts the parked caller outright.
        # Checked BEFORE start_transfer -- its queue divert returns None,
        # which the code below would misread as failure and steal the caller
        # back into the menu.
        if pbx.queue_handler.is_queue_destination(destination):
            pbx.queue_handler.adopt_parked_caller(call_id, call, destination)
            return

        from pbx.core.transfer_session import TransferMode

        # There is no transferor phone here to hang up or recall, so on
        # no-answer / busy / reject the session hands the caller back to us
        # instead of applying its default failure policy.
        session = pbx.transfer_handler.start_transfer(
            call,
            destination,
            mode=TransferMode.BLIND,
            transferor_side="callee",
            on_failure=lambda: self._return_to_menu(call_id, call),
        )

        if session is None:
            # Synchronous failure (e.g. destination offline): no session was
            # created, so on_failure never fires -- return to the menu here.
            pbx.logger.warning(
                f"Auto attendant transfer to {destination} failed to start; returning to menu"
            )
            self._return_to_menu(call_id, call)

    def _return_to_menu(self, call_id: str, call: Any) -> None:
        """
        Recover a caller whose transfer attempt failed by restarting the menu
        on the same port, with no re-signaling toward the caller.

        Reclaims the caller's port from the transfer relay WITHOUT returning
        it to the pool (it was never in the normal allocate/release cycle --
        the AA popped it directly), so a fresh IVR session can rebind the same
        port. Runs on a new daemon thread because it is also invoked from the
        SIP-response / no-answer-timer threads (via the failure callback),
        which must not block on a full menu interaction.

        TODO(transfer-fallback): interim behavior -- replays the main menu.
        Revisit the desired UX (destination voicemail? apology + hangup?)
        pending a product decision.
        """
        import threading

        pbx = self.pbx_core

        # Idempotent: stop_moh no-ops if _begin_transfer never started it
        # (e.g. adopt_existing_port failed before reaching that point).
        pbx.moh_system.stop_moh(call_id)

        # Idempotent if no relay is present (e.g. adopt_existing_port failed):
        # release_relay_keep_port returns None and the port is already free.
        pbx.rtp_relay.release_relay_keep_port(call_id)

        fresh_session = pbx.auto_attendant.start_session(call_id, call.from_extension)
        call.aa_session = fresh_session

        thread = threading.Thread(
            target=self._auto_attendant_session,
            args=(call_id, call, fresh_session),
            kwargs={"reentry": True},
        )
        thread.daemon = True
        thread.start()
