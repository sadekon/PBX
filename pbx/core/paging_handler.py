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
import time
from dataclasses import dataclass, field
from typing import Any

#: G.711 u-law. The only codec a page is answered with -- see the module docstring.
PCMU_PAYLOAD_TYPE = "0"

#: The only tone the PBX plays, and only when a page reached nothing at all. Low and
#: repeated so it reads as a fault rather than as one of the amplifier's own prompts, which
#: are what the pager otherwise hears. Nothing else can report this: a page with no
#: destination up produces exactly the same silence as a working one.
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
    torn_down: bool = False
    #: True when the PBX placed the pager's leg rather than answering it -- a test page
    #: fired from the admin view. It changes which side of the dialog a BYE is addressed to.
    pager_is_originated: bool = False
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

    # ------------------------------------------------------------------ test page

    def start_test_page(self, from_extension: str, zone_id: int) -> tuple[str | None, str | None]:
        """
        Page a zone without anybody dialling it, by ringing an extension first.

        The same page as a dialled one from the amplifier's side. What differs is only how
        the pager's leg comes to exist, and that changes two things:

          * the pager's media is in the 200 OK it sends back, not in an offer it made, so
            the media session has to be listening before the INVITE goes out rather than
            after -- the phone may answer instantly and start sending immediately;
          * the PBX is the caller, so a BYE toward the pager goes to the callee side of the
            dialog. `pager_is_originated` is what tells `hang_up_pager` that.

        The zone is reserved before the phone is rung, not after it answers. Ringing someone
        for a zone that turns out to be busy wastes their time, and two people testing the
        same amplifier at once is exactly what the reservation exists to prevent.

        Args:
            from_extension: The extension to ring, whose handset becomes the pager
            zone_id: The zone to open once it answers

        Returns:
            (page_id, error). Only one is ever set.
        """
        pbx = self.pbx_core
        paging_system = getattr(pbx, "paging_system", None)
        if not paging_system or not paging_system.enabled:
            return None, "Paging is not enabled"

        zone = paging_system.get_zone(zone_id)
        if not zone:
            return None, "That zone no longer exists"

        page, error = paging_system.begin_page(from_extension, zone["extension"])
        if not page:
            return None, error or "Could not start the page"

        rtp_ports = pbx.rtp_relay.allocate_port()
        if not rtp_ports:
            pbx.logger.error(f"No RTP port available for test page to {zone['extension']}")
            paging_system.end_page(page.page_id)
            return None, "No RTP port was free"

        rtp_port = rtp_ports[0]

        from pbx.rtp.paging_media import PagingMediaSession

        media = PagingMediaSession(rtp_port, page.page_id, logger=pbx.logger)
        if not media.start():
            pbx.logger.error(f"Could not open paging media for test page {page.page_id}")
            pbx.rtp_relay.release_port(rtp_port)
            paging_system.end_page(page.page_id)
            return None, "Could not open the media session"

        session = _PageSession(
            page=page,
            media=media,
            rtp_port=rtp_port,
            call_id="",
            pager_is_originated=True,
        )

        # Registered before the INVITE goes out. A phone set to auto-answer can respond
        # before originate_call has even returned, and the callback that runs then expects
        # to find its own session.
        with self._sessions_lock:
            self._sessions[page.page_id] = session

        leg = pbx.call_originator.originate_call(
            "paging-test",
            from_extension,
            answer_timeout=pbx.config.get("features.paging.answer_timeout", 30),
            # Shown as the zone, because from the handset's side the call is with the zone
            # rather than with whatever the PBX calls its own originating context.
            caller_id=(zone["extension"], zone.get("name") or "Paging test"),
            # The page's own socket, so the phone is told to send where the fan-out is
            # already listening. Passing the ports also skips relay allocation, which would
            # otherwise bind a second socket to a port this session already owns.
            rtp_ports_override=rtp_ports,
            codecs=[PCMU_PAYLOAD_TYPE],
            sdp_direction="sendrecv",
            on_answer=lambda call: self._on_test_pager_answered(session, call),
            on_failure=lambda _call, reason: self._on_test_pager_failed(session, reason),
        )

        if leg is None:
            pbx.logger.error(f"Could not place a test page leg to {from_extension}")
            self._close_session(session)
            return None, f"Could not place a call to {from_extension}"

        # Both this and the answer callback assign the same value, so whichever runs first
        # wins harmlessly. It is set here as well because a leg that rings and is never
        # answered still has to be findable by call id when its BYE or CANCEL arrives.
        session.call_id = leg.call_id
        page.call_id = leg.call_id
        leg.paging_active = True
        leg.page_id = page.page_id
        leg.paging_zones = page.zone_name

        pbx.logger.info(
            f"Test page {page.page_id}: ringing {from_extension} to open "
            f"{page.zone_name} ({len(page.destinations)} destination(s))"
        )
        return page.page_id, None

    def _on_test_pager_answered(self, session: _PageSession, call: Any) -> None:
        """
        The extension picked up: it is the pager now, so open the zone to it.

        Args:
            session: The page waiting on this answer
            call: The answered leg
        """
        pbx = self.pbx_core

        session.call_id = call.call_id
        session.page.call_id = call.call_id

        rtp = getattr(call, "callee_rtp", None)
        if not rtp:
            # Answered with no media to send to. Nothing can be relayed, and the page would
            # otherwise sit open holding the zone reserved.
            pbx.logger.error(
                f"Test page {session.page.page_id}: {session.page.from_extension} answered "
                f"without usable media; abandoning the page"
            )
            self.hang_up_pager(call.call_id)
            return

        endpoint = (rtp["address"], rtp["port"])
        session.pager_endpoint = endpoint
        session.media.expect_source(endpoint)

        pbx.cdr_system.mark_answered(call.call_id)
        pbx.logger.info(
            f"Test page {session.page.page_id}: {session.page.from_extension} answered; "
            f"opening {session.page.zone_name}"
        )

        self._open_destination_legs(session)

    def _on_test_pager_failed(self, session: _PageSession, reason: str) -> None:
        """
        The extension never picked up, so there is no page to run.

        Args:
            session: The page that was waiting on it
            reason: Why, from CallOriginator
        """
        self.pbx_core.logger.warning(
            f"Test page {session.page.page_id} to {session.page.zone_name} abandoned: "
            f"{session.page.from_extension} did not answer ({reason})"
        )
        self._close_session(session)

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
            # build_response mints a fresh to-tag, and that tag is this dialog's identity.
            # `caller_dialog_to` is where the rest of the PBX looks for it -- SIPServer's
            # _send_leg_bye reads it to build an in-dialog BYE, exactly as the auto attendant
            # and queue handler record it after their own 200 OK. Set before the response
            # goes out, so a teardown racing the answer still finds it.
            call.caller_dialog_to = response.get_header("To")
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

        # How long a destination gets to answer before the leg is CANCELled.
        #
        # An endpoint that auto-answers takes a second -- a fax machine sends 180 then 200 OK
        # almost immediately. An amplifier that seizes the line on ring voltage instead needs
        # whole ring cycles (2s on, 4s off), and a timeout that expires mid-ring CANCELs a
        # destination that was about to answer. Configurable because it depends entirely on
        # what is wired to the FXS port, which the PBX cannot know.
        answer_timeout = pbx.config.get("features.paging.answer_timeout", 30)

        for destination in sip_destinations:
            if not self._destination_is_reachable(destination, server_ip, sip_port):
                self._on_leg_failed(session, destination, "unreachable")
                continue

            # Normally empty. An amplifier is not a SIP device: it watches the FXS port for
            # ring voltage, which the ATA only raises while it is ringing, so the leg is left
            # to ring and the amplifier seizes the line itself. A header is attached only
            # where a destination was explicitly given one -- a desk phone, which has no ring
            # voltage to offer and would otherwise ring at somebody until answered by hand.
            extra_headers: dict[str, str] = {}
            header = destination.auto_answer_header(server_ip)
            if header:
                extra_headers[header[0]] = header[1]
                pbx.logger.debug(
                    f"Page {page.page_id}: telling {destination.display_name} to auto-answer "
                    f"with {header[0]}"
                )

            pbx.paging_system.set_destination_state(
                page.page_id, destination.destination_id, "ringing"
            )

            leg = pbx.call_originator.originate_call(
                from_context=page.from_extension,
                destination=destination.endpoint_extension,
                caller_id=(page.from_extension, f"Page {page.zone_name}"),
                answer_timeout=answer_timeout,
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

    def _destination_is_reachable(self, destination: Any, server_ip: str, sip_port: int) -> bool:
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

        pbx.paging_system.set_destination_state(
            session.page.page_id, destination.destination_id, "answered"
        )

        if destination.dtmf_sequence:
            # Direct-dial: pick the circuit before anyone is broadcast into the building.
            # The tones go to this amplifier alone and the destination joins the fan-out
            # only once they are done, so the pager's voice cannot reach a circuit that has
            # not been chosen yet. Threaded because the tones play in real time and this
            # runs on a SIP callback thread that must not block.
            threading.Thread(
                target=self._select_circuit_then_stream,
                args=(session, destination, endpoint),
                name=f"paging-select-{session.page.page_id[:12]}",
                daemon=True,
            ).start()
            return

        # Manual: the person paging picks the circuit on their own keypad, so the amplifier
        # has to be receiving before they press anything.
        session.media.add_target(destination.destination_id, endpoint)

        # No tone from the PBX here. The amplifier announces itself when it answers, and
        # again when a zone digit lands, and the media session now carries both back to the
        # pager. A synthetic beep on top would talk over the one cue that actually says
        # which circuit opened -- and the PBX cannot know that, since everything past the
        # FXS port is analog. The failure tone stays, because nothing else can report a
        # page that reached nothing.

    def _select_circuit_then_stream(
        self, session: _PageSession, destination: Any, endpoint: tuple[str, int]
    ) -> None:
        """
        Play a destination's circuit-selection digits, then start relaying the pager to it.

        Inband, as audio, because that is what the amplifiers listen for -- the same tones a
        person would produce on a keypad. Not RFC 2833: the leg negotiates PCMU alone, so
        there is no telephone-event payload to carry named events, and the amplifier is
        listening to the audio anyway.

        Sent to this one destination, never the whole fan-out. Two amplifiers in the same
        zone can want different circuits, and in any case a digit meant for one has no
        business arriving at another.

        Runs on its own thread: the tones play in real time.

        Args:
            session: The live page
            destination: The destination whose circuit is being selected
            endpoint: Where that destination receives RTP
        """
        pbx = self.pbx_core
        digits = destination.dtmf_sequence

        try:
            from pbx.rtp.handler import RTPPlayer
            from pbx.utils.audio import float_samples_to_pcm16, pcm16_to_ulaw
            from pbx.utils.dtmf import DTMFGenerator

            settle_ms = pbx.config.get("features.paging.dtmf_settle_ms", 500)
            tone_ms = pbx.config.get("features.paging.dtmf_tone_ms", 120)
            gap_ms = pbx.config.get("features.paging.dtmf_gap_ms", 80)
            confirm_ms = pbx.config.get("features.paging.dtmf_confirm_ms", 600)

            # An amplifier that has just gone off-hook is not always listening yet.
            if settle_ms > 0:
                time.sleep(settle_ms / 1000.0)

            with session.lock:
                if session.torn_down:
                    return

            samples = (
                DTMFGenerator().generate_sequence(digits, tone_ms=tone_ms, gap_ms=gap_ms)
                if session.media.socket is not None
                else []
            )
            if not samples:
                pbx.logger.error(
                    f"Page {session.page.page_id}: could not build tones for "
                    f"{destination.display_name} from '{digits}'"
                )
            else:
                player = RTPPlayer(
                    session.rtp_port,
                    endpoint[0],
                    endpoint[1],
                    call_id=session.call_id,
                    external_socket=session.media.socket,
                )
                if player.start():
                    player.send_audio(
                        pcm16_to_ulaw(float_samples_to_pcm16(samples)), payload_type=0
                    )
                    pbx.logger.info(
                        f"Page {session.page.page_id}: sent '{digits}' to "
                        f"{destination.display_name} to select its circuit"
                    )

            # Let the amplifier act on the selection -- and let its confirmation tone reach
            # the pager, which the media session returns on its own -- before the page starts
            # flowing into it.
            if confirm_ms > 0:
                time.sleep(confirm_ms / 1000.0)
        except Exception as e:
            # A destination that could not be told which circuit to use is still better
            # joined than dropped: the amplifier will page whatever it defaults to, which is
            # more useful than silence, and the log says what happened.
            pbx.logger.error(
                f"Page {session.page.page_id}: circuit selection failed for "
                f"{destination.display_name}: {e}"
            )

        with session.lock:
            if session.torn_down:
                return

        session.media.add_target(destination.destination_id, endpoint)

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
                    # Through hang_up_pager, not end_call: the pager is still in the dialog
                    # and needs a BYE, or their phone keeps counting against a page that
                    # already failed.
                    self.hang_up_pager(session.call_id)

        threading.Thread(
            target=_run, name=f"paging-tone-{session.page.page_id[:16]}", daemon=True
        ).start()

    # ------------------------------------------------------------------ legs ending

    def _find_leg(self, call_id: str) -> tuple[_PageSession | None, int]:
        """
        Find the page a destination leg belongs to.

        Args:
            call_id: A leg's call id

        Returns:
            (session, destination_id), or (None, 0) if this is not a paging leg
        """
        with self._sessions_lock:
            for session in self._sessions.values():
                with session.lock:
                    for destination_id, leg_call_id in session.leg_call_ids.items():
                        if leg_call_id == call_id:
                            return session, destination_id
        return None, 0

    def _on_leg_ended(self, session: _PageSession, destination_id: int) -> None:
        """
        A destination that had answered has hung up.

        Stops sending to it, and ends the whole page once the last one goes: a page with no
        destination left reaches nothing, so keeping it alive would only hold the zone busy
        and leave the pager talking to themselves.

        Args:
            session: The live page
            destination_id: The destination whose leg ended
        """
        pbx = self.pbx_core

        with session.lock:
            if session.torn_down:
                return
            session.leg_call_ids.pop(destination_id, None)
            session.answered.discard(destination_id)
            any_left = bool(session.answered)

        session.media.remove_target(destination_id)
        pbx.paging_system.set_destination_state(session.page.page_id, destination_id, "ended")

        if any_left:
            pbx.logger.info(
                f"Page {session.page.page_id}: destination {destination_id} hung up, "
                f"{len(session.answered)} still receiving"
            )
            return

        pbx.logger.info(f"Page {session.page.page_id}: last destination hung up, ending the page")
        self.hang_up_pager(session.call_id)

    # ------------------------------------------------------------------ hanging up

    def hang_up_pager(self, call_id: str) -> None:
        """
        End a page the PBX decided to stop, and tell the pager's phone about it.

        `PBXCore.end_call` only tears internal state down; it sends nothing. That is right
        when the pager hung up first -- their BYE is what brought us here -- and wrong for
        every hangup the PBX initiates. Without the BYE the phone sits in a session the PBX
        has already forgotten, still counting, until someone puts the handset down.

        Reached when a zone's destinations all refuse the call, and when a page runs past its
        duration cap.

        Args:
            call_id: The pager's call id
        """
        pbx = self.pbx_core

        with self._sessions_lock:
            session = next((s for s in self._sessions.values() if s.call_id == call_id), None)
        originated = bool(session and session.pager_is_originated)

        call = pbx.call_manager.get_call(call_id)
        if call:
            try:
                # SIPServer owns this: it reads the dialog's to-tag, draws CSeq from the
                # call's own pbx_leg_cseq counter so successive PBX requests do not reuse a
                # number, and adds the Via, Max-Forwards and Contact a strict UA needs
                # before it will act on a BYE at all.
                #
                # Which side depends on who placed the leg. On a dialled page the pager is
                # the caller and the tag we need is the one our own 200 OK minted, so the
                # side is forced. On a test page the PBX sent the INVITE, making the pager
                # the callee; the default picks that side, exactly as originate_and_bridge
                # does when it ends a leg it placed.
                pbx.sip_server._send_leg_bye(call, side=None if originated else "caller")
            except Exception as e:
                pbx.logger.error(f"Could not send BYE to paging caller {call_id}: {e}")

        try:
            pbx.end_call(call_id)
        except Exception as e:
            pbx.logger.error(f"Could not end paging call {call_id}: {e}")

    # ------------------------------------------------------------------ teardown

    def teardown_page(self, call_id: str) -> bool:
        """
        End whatever part of a page this call was carrying.

        Called from `PBXCore.end_call` for every call, so it has to recognise both ends of a
        page: the pager's call, whose ending ends the page, and a destination's leg, whose
        ending only removes that one amplifier.

        Reachable from the pager hanging up, a destination hanging up, the zone's duration
        timer, and an operator killing the page, so it is idempotent -- whichever arrives
        second does nothing.

        Args:
            call_id: A call id -- the pager's, or one of the destination legs'

        Returns:
            bool: True if this call was part of a page
        """
        with self._sessions_lock:
            session = next((s for s in self._sessions.values() if s.call_id == call_id), None)

        # Deliberately outside the lock above, and not folded into it: _find_leg takes the
        # same lock, which is not reentrant, and _on_leg_ended can end the pager's call --
        # re-entering this method. Holding the lock across either deadlocks the SIP thread
        # that delivered the BYE, and with it every call the PBX is handling.
        if session is None:
            # Not a pager. A destination that answered and later sent BYE arrives here under
            # its leg's call id, and without this the page would keep running with nothing on
            # the other end -- relaying audio to an amplifier that had hung up, and holding
            # the zone busy against the next page.
            leg_session, destination_id = self._find_leg(call_id)
            if leg_session is None:
                return False
            self._on_leg_ended(leg_session, destination_id)
            return True

        return self._close_session(session)

    def _close_session(self, session: _PageSession) -> bool:
        """
        Release everything one page owns: its legs, its socket, its port, its reservation.

        Split out from `teardown_page` because a test page can fail before it has a call id
        to be found by -- the pager's phone never answered -- and that path still has to
        give back the port and free the zone, or the amplifier stays reserved against a page
        that never happened.

        Args:
            session: The page to close

        Returns:
            bool: True if this call closed it, False if it was already closed
        """
        pbx = self.pbx_core

        with self._sessions_lock:
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
            pbx.logger.error(f"Could not stop paging media for page {session.page.page_id}: {e}")

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

        Through the server's own _send_response rather than building and sending the message
        here: that is what applies _add_via_nat_params, which writes received= and rport=
        onto the Via (RFC 3261 SS18.2.2, RFC 3581). A phone whose Via sent-by differs from
        where its packet actually came from needs those to match the response to its
        transaction, and a hand-built response silently omits them.

        Args:
            message: The request being responded to
            from_addr: Where to send it
            code: SIP status code
            reason: SIP reason phrase
        """
        pbx = self.pbx_core
        try:
            pbx.sip_server._send_response(code, reason, message, from_addr)
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
        stats: dict[str, Any] = session.media.get_stats()
        return stats
