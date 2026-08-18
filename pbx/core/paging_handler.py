"""
Paging handler for PBX Core.

Dial a zone's number and this answers, opens a one-way SIP leg to every destination in that
zone, and copies your audio to all of them until you hang up.

WHAT CHANGED, AND WHY
    The previous implementation hand-built an INVITE to a "DAC device", sent it, and never
    read the response -- so no ACK was ever sent and the dialog never completed. It then sent
    RTP to the PBX's *own advertised port* on the ATA's IP, rather than the port the ATA
    named in its answer SDP, which is why no audio ever arrived. And it paged `zones[0]`, one
    device, discarding the list of resolved devices the layer below had already built.

    Legs are now placed through CallOriginator, which completes the dialog properly, and the
    RTP destination comes from `call.callee_rtp` -- the endpoint the ATA actually asked for.
    Every destination in the zone gets its own leg, and PagingMediaSession copies one inbound
    stream to all of them.

CODEC
    Answered PCMU-only. The PBX does not transcode (see codec_negotiator), and a page is one
    stream fanned out to several endpoints that never negotiated with each other, so they all
    have to already agree. PCMU is what every ATA and amplifier supports.
"""

import threading
from dataclasses import dataclass, field
from typing import Any

#: G.711 u-law. The only codec a page is answered with -- see the module docstring.
PCMU_PAYLOAD_TYPE = "0"

#: Confirmation tone, played to the pager once a destination is live. The cue that means
#: "you are being heard"; it must never play before a leg has actually answered.
READY_TONE_HZ = 1000
READY_TONE_MS = 250

#: Failure tone. Lower and repeated, so it cannot be mistaken for the ready beep by someone
#: who is already talking.
FAILURE_TONE_HZ = 440
FAILURE_TONE_MS = 200
FAILURE_TONE_REPEATS = 3


@dataclass
class _PageSession:
    """Everything one live page owns, so teardown can find all of it from the call id."""

    page: Any
    media: Any
    rtp_port: int
    call_id: str
    pager_endpoint: tuple[str, int] | None = None
    #: destination_id -> the leg's own call id, for hanging it up
    leg_call_ids: dict[int, str] = field(default_factory=dict)
    answered: set[int] = field(default_factory=set)
    failed: set[int] = field(default_factory=set)
    ready_tone_played: bool = False
    torn_down: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


class PagingHandler:
    """Answers paging calls and fans their audio out to a zone's destinations."""

    def __init__(self, pbx_core: Any) -> None:
        """
        Initialize PagingHandler with reference to PBXCore.

        Args:
            pbx_core: The PBXCore instance
        """
        self.pbx_core: Any = pbx_core
        #: page_id -> session
        self._sessions: dict[str, _PageSession] = {}
        self._sessions_lock = threading.Lock()

    # ------------------------------------------------------------------ entry point

    def handle_paging(
        self,
        from_ext: str,
        to_ext: str,
        call_id: str,
        message: Any,
        from_addr: tuple[str, int],
    ) -> bool:
        """
        Answer a page and open it to its zone.

        Args:
            from_ext: Calling extension
            to_ext: The zone's number
            call_id: Call ID
            message: SIP INVITE message
            from_addr: Caller address

        Returns:
            True if the call was handled -- including when it was deliberately refused, since
            a refusal is still a response and the router must not fall through to another
            destination afterwards.
        """
        from pbx.sip.sdp import SDPSession

        pbx = self.pbx_core
        pbx.logger.info(f"Paging call: {from_ext} -> {to_ext}")

        # The pager's own media, before anything is committed: a call we cannot carry should
        # be refused with a reason, not answered into silence.
        caller_rtp: dict[str, Any] | None = None
        if message.body:
            sdp = SDPSession()
            sdp.parse(message.body)
            caller_rtp = sdp.get_audio_info()

        if not caller_rtp:
            pbx.logger.warning(f"Paging call {call_id} from {from_ext} carried no audio SDP")
            self._respond(message, from_addr, 488, "Not Acceptable Here")
            return True

        formats = caller_rtp.get("formats") or []
        if formats and PCMU_PAYLOAD_TYPE not in [str(f) for f in formats]:
            # Answering anyway would open a stream the amplifiers cannot decode, which sounds
            # like a working page right up until nobody can understand it.
            pbx.logger.warning(
                f"Paging call {call_id} from {from_ext} offers no PCMU (offered: "
                f"{', '.join(str(f) for f in formats)}); paging requires it because the "
                f"PBX does not transcode"
            )
            self._respond(message, from_addr, 488, "Not Acceptable Here")
            return True

        page, error = pbx.paging_system.begin_page(from_ext, to_ext, call_id=call_id)
        if not page:
            self._refuse(message, from_addr, error, from_ext, to_ext)
            return True

        rtp_ports = pbx.rtp_relay.allocate_port()
        if not rtp_ports:
            pbx.logger.error(f"No RTP port available for paging call {call_id}")
            pbx.paging_system.end_page(page.page_id)
            self._respond(message, from_addr, 503, "Service Unavailable")
            return True

        rtp_port = rtp_ports[0]
        pager_endpoint = (caller_rtp["address"], caller_rtp["port"])

        session = _PageSession(
            page=page,
            media=None,
            rtp_port=rtp_port,
            call_id=call_id,
            pager_endpoint=pager_endpoint,
        )

        # One socket carries the whole page: it receives from the pager, sends the tones back
        # to them, and sends the page out to every amplifier. Every leg advertises this same
        # port, so our packets always leave from the port we told each endpoint to expect --
        # which is what keeps symmetric-RTP validation on the far side happy.
        from pbx.rtp.paging_media import PagingMediaSession

        media = PagingMediaSession(rtp_port, page.page_id, logger=pbx.logger)
        if not media.start(expected_source=pager_endpoint):
            pbx.logger.error(f"Could not open paging media for call {call_id}")
            pbx.rtp_relay.release_port(rtp_port)
            pbx.paging_system.end_page(page.page_id)
            self._respond(message, from_addr, 503, "Service Unavailable")
            return True
        session.media = media

        with self._sessions_lock:
            self._sessions[page.page_id] = session

        call = pbx.call_manager.create_call(call_id, from_ext, to_ext)
        call.start()
        call.original_invite = message
        call.caller_addr = from_addr
        call.caller_rtp = caller_rtp
        call.rtp_ports = rtp_ports
        call.paging_active = True
        call.page_id = page.page_id
        call.paging_zones = page.zone_name

        pbx.cdr_system.start_record(call_id, from_ext, to_ext)

        if not self._answer_pager(call, rtp_port):
            self.teardown_page(call_id)
            return True

        call.connect()
        pbx.cdr_system.mark_answered(call_id)
        pbx.logger.info(
            f"Answered page {page.page_id}: {from_ext} -> {page.zone_name} "
            f"({len(page.destinations)} destination(s))"
        )

        self._open_destination_legs(session)
        return True

    # ------------------------------------------------------------------ pager leg

    def _answer_pager(self, call: Any, rtp_port: int) -> bool:
        """
        Send 200 OK to the pager, PCMU only.

        Args:
            call: The pager's Call
            rtp_port: Port the page's media session is listening on

        Returns:
            bool: True if the answer was sent
        """
        from pbx.sip.message import SIPMessageBuilder
        from pbx.sip.sdp import SDPBuilder

        pbx = self.pbx_core
        server_ip = pbx._get_server_ip()

        # Deliberately not run through _get_codecs_for_phone_model: that picks the best codec
        # this handset supports, and the whole point here is that every endpoint on the page
        # must already agree without negotiating.
        sdp = SDPBuilder.build_audio_sdp(
            server_ip,
            rtp_port,
            session_id=call.call_id,
            codecs=[PCMU_PAYLOAD_TYPE],
            dtmf_payload_type=pbx._get_dtmf_payload_type(),
        )

        try:
            response = SIPMessageBuilder.build_response(200, "OK", call.original_invite, body=sdp)
            response.set_header("Content-type", "application/sdp")
            sip_port = pbx.config.get("server.sip_port", 5060)
            response.set_header("Contact", f"<sip:{call.to_extension}@{server_ip}:{sip_port}>")
            pbx.sip_server._send_message(response.build(), call.caller_addr)
        except Exception as e:
            pbx.logger.error(f"Could not answer paging call {call.call_id}: {e}")
            return False

        return True

    # ------------------------------------------------------------------ destination legs

    def _open_destination_legs(self, session: _PageSession) -> None:
        """
        Place one auto-answer leg per destination, in parallel.

        Args:
            session: The live page
        """
        pbx = self.pbx_core
        server_ip = pbx._get_server_ip()
        page = session.page

        sip_destinations = [d for d in page.destinations if d.kind == "sip_endpoint"]
        if not sip_destinations:
            # Only multicast destinations, which nothing streams to yet.
            pbx.logger.warning(
                f"Page {page.page_id} has no SIP destinations; multicast paging is not "
                f"implemented, so this page reaches nothing"
            )
            self._on_all_destinations_failed(session)
            return

        sip_port = pbx.config.get("server.sip_port", 5060)

        for destination in sip_destinations:
            if not self._destination_is_reachable(destination, server_ip, sip_port):
                self._on_leg_failed(session, destination, "unreachable")
                continue

            extra_headers: dict[str, str] = {}
            header = destination.auto_answer_header(server_ip)
            if header:
                extra_headers[header[0]] = header[1]
            else:
                pbx.logger.info(
                    f"Page {page.page_id}: no auto-answer header for "
                    f"{destination.display_name}; relying on the amplifier to seize the line"
                )

            pbx.paging_system.set_destination_state(
                page.page_id, destination.destination_id, "ringing"
            )

            leg = pbx.call_originator.originate_call(
                from_context=page.from_extension,
                destination=destination.endpoint_extension,
                caller_id=(page.from_extension, f"Page {page.zone_name}"),
                answer_timeout=15,
                # Every leg advertises the page's own media port, so audio leaves from the
                # port each endpoint was told to expect and no per-leg relay is built.
                rtp_ports_override=(session.rtp_port, session.rtp_port + 1),
                extra_headers=extra_headers,
                codecs=[PCMU_PAYLOAD_TYPE],
                # sendrecv, not sendonly, even though a page is strictly one-way.
                #
                # A Cisco ATA 191 answers `a=sendonly` on an FXS port with 488 Not
                # Acceptable Here: there is a handset on that port, and the firmware will
                # not set up a call it is forbidden to talk on. Other paging adapters are
                # likely to be as strict, since one-way media is unusual for an endpoint
                # that thinks it is a telephone.
                #
                # Nothing is lost by negotiating normally. One-way is enforced in the media
                # path, not in the SDP: PagingMediaSession drops every packet that does not
                # come from the pager, so an amplifier that sends audio back is ignored
                # whatever the direction attribute said.
                sdp_direction="sendrecv",
                on_answer=lambda leg_call, d=destination: self._on_leg_answered(
                    session, d, leg_call
                ),
                on_failure=lambda _leg_call, reason, d=destination: self._on_leg_failed(
                    session, d, reason
                ),
            )

            if leg is None:
                self._on_leg_failed(session, destination, "no_route")
            else:
                with session.lock:
                    session.leg_call_ids[destination.destination_id] = leg.call_id

    def _destination_is_reachable(
        self, destination: Any, server_ip: str, sip_port: int
    ) -> bool:
        """
        Refuse a destination whose registration points back at the PBX.

        A stale or bogus `registered_phones` row -- typically 127.0.0.1 -- makes
        `resolve_extension()` hand back the PBX's own SIP address, and the leg then INVITEs
        this server. The PBX receives its own INVITE as a fresh inbound call, routes it to
        the same extension, and resolves the same address again. Nothing on the far side is
        an amplifier, so the page reaches no speaker either way; refusing it here keeps a
        misconfigured destination from turning into a signalling loop.

        Checked per destination rather than once, since only some of a zone's endpoints may
        be affected and the rest of the page should still go through.

        Args:
            destination: The destination about to be INVITEd
            server_ip: This PBX's advertised address
            sip_port: This PBX's SIP port

        Returns:
            bool: True if the leg is worth placing
        """
        pbx = self.pbx_core

        try:
            resolved = pbx.call_router.resolve_extension(destination.endpoint_extension)
        except Exception as e:
            pbx.logger.error(
                f"Could not resolve paging destination {destination.display_name}: {e}"
            )
            return False

        if not resolved or not getattr(resolved, "address", None):
            pbx.logger.error(
                f"Paging destination {destination.display_name} "
                f"(ext {destination.endpoint_extension}) is not registered"
            )
            return False

        host, port = resolved.address[0], resolved.address[1]
        loops_back = host in ("127.0.0.1", "localhost", "::1", server_ip) and port == sip_port
        if loops_back:
            pbx.logger.error(
                f"Paging destination {destination.display_name} "
                f"(ext {destination.endpoint_extension}) is registered at {host}:{port}, "
                f"which is this PBX. Its registration is wrong -- the INVITE would come "
                f"straight back here instead of reaching an amplifier. Check "
                f"registered_phones for that extension."
            )
            return False

        return True

    def _on_leg_answered(self, session: _PageSession, destination: Any, leg_call: Any) -> None:
        """
        A destination picked up: start sending it audio.

        Args:
            session: The live page
            destination: The destination that answered
            leg_call: The answered leg
        """
        pbx = self.pbx_core

        endpoint_info = getattr(leg_call, "callee_rtp", None)
        if not endpoint_info:
            pbx.logger.error(
                f"Page {session.page.page_id}: {destination.display_name} answered without "
                f"usable media; dropping it from the page"
            )
            self._on_leg_failed(session, destination, "no_media")
            return

        # From the endpoint's answer SDP. The old code guessed this and guessed wrong.
        endpoint = (endpoint_info["address"], endpoint_info["port"])

        with session.lock:
            if session.torn_down:
                return
            session.answered.add(destination.destination_id)
            play_tone = not session.ready_tone_played
            session.ready_tone_played = True

        session.media.add_target(destination.destination_id, endpoint)
        pbx.paging_system.set_destination_state(
            session.page.page_id, destination.destination_id, "answered"
        )

        if play_tone:
            # Only on the first answer: the pager needs one cue that they are live, not one
            # per amplifier.
            self._play_tone(session, READY_TONE_HZ, READY_TONE_MS, repeats=1)

    def _on_leg_failed(self, session: _PageSession, destination: Any, reason: str) -> None:
        """
        A destination did not come up.

        Args:
            session: The live page
            destination: The destination that failed
            reason: Why, from CallOriginator
        """
        pbx = self.pbx_core
        pbx.logger.warning(
            f"Page {session.page.page_id}: {destination.display_name} did not answer ({reason})"
        )

        with session.lock:
            if session.torn_down:
                return
            session.failed.add(destination.destination_id)
            session.leg_call_ids.pop(destination.destination_id, None)
            total = len([d for d in session.page.destinations if d.kind == "sip_endpoint"])
            all_failed = len(session.failed) >= total and not session.answered

        pbx.paging_system.set_destination_state(
            session.page.page_id, destination.destination_id, "failed"
        )

        if all_failed:
            self._on_all_destinations_failed(session)

    def _on_all_destinations_failed(self, session: _PageSession) -> None:
        """
        Nothing answered: tell the pager rather than let them talk into silence.

        Args:
            session: The live page
        """
        pbx = self.pbx_core
        pbx.logger.error(
            f"Page {session.page.page_id} to {session.page.zone_name} reached no destination"
        )
        self._play_tone(
            session,
            FAILURE_TONE_HZ,
            FAILURE_TONE_MS,
            repeats=FAILURE_TONE_REPEATS,
            then_hang_up=True,
        )

    # ------------------------------------------------------------------ tones

    def _play_tone(
        self,
        session: _PageSession,
        frequency: int,
        duration_ms: int,
        repeats: int = 1,
        then_hang_up: bool = False,
    ) -> None:
        """
        Play a tone to the pager on the page's own socket.

        Runs on its own thread: the tone is emitted in real time and this is reached from a
        SIP callback thread that must not block.

        Args:
            session: The live page
            frequency: Tone frequency in Hz
            duration_ms: Length of each tone
            repeats: How many times to play it
            then_hang_up: End the page once the tone finishes
        """
        pbx = self.pbx_core

        def _run() -> None:
            try:
                endpoint = session.media._source or session.pager_endpoint
                if not endpoint or session.media.socket is None:
                    return

                from pbx.rtp.handler import RTPPlayer

                player = RTPPlayer(
                    session.rtp_port,
                    endpoint[0],
                    endpoint[1],
                    call_id=session.call_id,
                    external_socket=session.media.socket,
                )
                if not player.start():
                    return
                for _ in range(repeats):
                    player.play_beep(frequency, duration_ms)
            except Exception as e:
                pbx.logger.debug(f"Could not play paging tone: {e}")
            finally:
                if then_hang_up:
                    try:
                        pbx.end_call(session.call_id)
                    except Exception as e:
                        pbx.logger.error(f"Could not end failed page {session.call_id}: {e}")

        threading.Thread(
            target=_run, name=f"paging-tone-{session.page.page_id[:16]}", daemon=True
        ).start()

    # ------------------------------------------------------------------ teardown

    def teardown_page(self, call_id: str) -> bool:
        """
        Tear down the page a call is carrying.

        Reachable from the pager hanging up, the zone's duration timer, and an operator
        killing the page, so it is idempotent -- whichever arrives second does nothing.

        Args:
            call_id: The pager's call id

        Returns:
            bool: True if this call tore a page down
        """
        pbx = self.pbx_core

        with self._sessions_lock:
            session = next((s for s in self._sessions.values() if s.call_id == call_id), None)
            if session is None:
                return False
            with session.lock:
                if session.torn_down:
                    return False
                session.torn_down = True
                leg_call_ids = list(session.leg_call_ids.values())
            self._sessions.pop(session.page.page_id, None)

        for leg_call_id in leg_call_ids:
            try:
                pbx.end_call(leg_call_id)
            except Exception as e:
                pbx.logger.error(f"Could not end paging leg {leg_call_id}: {e}")

        try:
            session.media.stop()
        except Exception as e:
            pbx.logger.error(f"Could not stop paging media for {call_id}: {e}")

        pbx.rtp_relay.release_port(session.rtp_port)
        pbx.paging_system.end_page(session.page.page_id)
        return True

    # ------------------------------------------------------------------ responses

    def _refuse(
        self,
        message: Any,
        from_addr: tuple[str, int],
        error: str | None,
        from_ext: str,
        to_ext: str,
    ) -> None:
        """
        Turn a begin_page refusal into the right SIP response.

        Args:
            message: The INVITE being refused
            from_addr: Where to send the response
            error: The reason begin_page gave
            from_ext: Who dialled
            to_ext: What they dialled
        """
        pbx = self.pbx_core

        if error == "busy":
            # Two live pages through one amplifier would garble each other, and the PBX has
            # no mixer to combine them.
            pbx.logger.info(f"Page {from_ext} -> {to_ext} refused: zone already paging")
            self._respond(message, from_addr, 486, "Busy Here")
            return

        if error == "No such zone":
            self._respond(message, from_addr, 404, "Not Found")
            return

        pbx.logger.warning(f"Page {from_ext} -> {to_ext} could not start: {error}")
        self._respond(message, from_addr, 480, "Temporarily Unavailable")

    def _respond(self, message: Any, from_addr: tuple[str, int], code: int, reason: str) -> None:
        """
        Send a bare SIP response to the pager.

        Args:
            message: The request being responded to
            from_addr: Where to send it
            code: SIP status code
            reason: SIP reason phrase
        """
        from pbx.sip.message import SIPMessageBuilder

        pbx = self.pbx_core
        try:
            response = SIPMessageBuilder.build_response(code, reason, message)
            pbx.sip_server._send_message(response.build(), from_addr)
        except Exception as e:
            pbx.logger.error(f"Could not send {code} {reason} to paging caller: {e}")

    # ------------------------------------------------------------------ introspection

    def get_session_stats(self, page_id: str) -> dict[str, Any] | None:
        """
        Media counters for a live page.

        Args:
            page_id: Page id

        Returns:
            dict: Stats, or None if the page is not live
        """
        with self._sessions_lock:
            session = self._sessions.get(page_id)
        if not session:
            return None
        return session.media.get_stats()
