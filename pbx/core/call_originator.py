"""
Call-origination primitives for PBX Core.

Handles calls the PBX places itself -- click-to-dial, and future
originate-driven features (predictive dialing, callback completion,
emergency-notification calls) -- as opposed to CallRouter, which reacts to
an inbound INVITE that already has a live caller leg to relay responses
back to. A call from here has no ``original_invite``: the PBX itself is
the "caller," so there is nothing to relay to.

Layering
--------
``SIPServer`` owns the wire: it builds and sends every SIP message,
including the in-dialog teardown requests (``cancel_leg()`` for a leg still
ringing, ``_send_leg_bye()`` for one that answered) and the transactions
that retransmit them. Nothing here formats SIP by hand except the initial
INVITE, which it hands to ``CallRouter.send_leg_invite()`` to transmit.

``CallRouter`` owns two things this module shares rather than duplicates,
so both entry points resolve and dial identically:
``EXTERNAL_NUMBER_PATTERN`` + ``resolve_extension()`` (where does this
destination live) and ``send_leg_invite()`` (put a leg on the wire). Its
routing *decisions* -- dialplan, queues, paging, DID, voicemail fallback --
belong to inbound calls and are deliberately not reused here.

``CallOriginator`` owns only the outbound call lifecycle: place a leg,
track its answer/failure, and bridge two legs into one conversation.

Provenance
----------
The primitive set is modeled on the *documented behavior* of Asterisk's
``Originate`` -- its dial-then-connect shape and its CallerID, Timeout and
early-media parameters -- so that operators get the semantics they already
expect (the ``transfer.*`` options in ``features.conf`` are mirrored the
same way in ``transfer_session.py``).

That is a behavioral reference, not a derivation. No Asterisk source is
incorporated here: this is an independent implementation over this
package's own SIP, relay and Call layers, and behavior, parameter names and
protocol semantics are not copyrightable expression. Asterisk's GPLv2
therefore places no obligation on this MIT-licensed package, and there is
no attribution owed under it. "Asterisk" is a registered trademark of
Sangoma Technologies Corporation, used here descriptively to name the
behavior being matched; no affiliation with or endorsement by Sangoma is
implied.
"""

import threading
import uuid
from collections.abc import Callable
from typing import Any

from pbx.core.call import CallState


class CallOriginator:
    """Places calls on the PBX's own behalf, to an extension or external number."""

    def __init__(self, pbx_core: Any) -> None:
        """
        Initialize CallOriginator with a reference to PBXCore.

        Args:
            pbx_core: The PBXCore instance.
        """
        self.pbx_core: Any = pbx_core

    def originate_call(
        self,
        from_context: str,
        destination: str,
        *,
        answer_timeout: int = 30,
        caller_id: tuple[str, str] | None = None,
        on_answer: Callable[[Any], None] | None = None,
        on_failure: Callable[[Any, str], None] | None = None,
        rtp_ports_override: tuple[int, int] | None = None,
        extra_headers: dict[str, str] | None = None,
        codecs: list[str] | None = None,
        sdp_direction: str = "sendrecv",
    ) -> Any | None:
        """
        Place a call from the PBX to `destination` (extension or external
        number) and track its outcome via callbacks -- fire-and-track, not
        blocking. The INVITE is sent before this returns; answer/failure
        are reported asynchronously through `on_answer`/`on_failure` once
        the destination responds.

        A bare `originate_call()` establishes only one leg -- it does not
        by itself create two-way audio. Pair it with a second
        `originate_call()` (via `originate_and_bridge()`) to actually
        connect two parties.

        Args:
            from_context: Identifies the originator for CDR/display -- an
                extension number, or a synthetic id like "c2d:web" for a
                non-extension-driven origination.
            destination: Extension number or external (PSTN) number, using
                the same classification `route_call()` uses
                (`CallRouter.EXTERNAL_NUMBER_PATTERN`).
            answer_timeout: Seconds to wait before treating the leg as
                unanswered.
            caller_id: (number, name) to present to the destination, the
                equivalent of Asterisk Originate's CallerID. Defaults to
                `from_context` for both, which is right when the originator
                is a real extension and wrong-looking when it is a synthetic
                id -- so callers placing a leg on someone's behalf (e.g.
                click-to-dial ringing you first) should pass who the call is
                really with.
            on_answer: Called with the `Call` once the destination answers.
            on_failure: Called with the `Call` and a reason string
                ("no_answer" | "busy" | "unreachable" | "no_route") if the
                leg never connects.
            rtp_ports_override: Reuse an already-allocated relay (e.g. the
                other leg's, when bridging) instead of allocating a new one.
            extra_headers: Headers to set on the INVITE, beyond the ones every
                leg carries. Paging uses this for the vendor-specific
                auto-answer header (`Call-Info: <sip:host>;answer-after=0` and
                friends) that makes an ATA go off-hook without a human.
            codecs: Payload types to offer, as strings ("0" is PCMU). Defaults
                to the SDP builder's full set. Pass a single codec when the leg
                must agree with something the PBX cannot renegotiate -- paging
                fans one stream out to several endpoints and the PBX does not
                transcode, so every leg has to be PCMU.
            sdp_direction: Media direction to advertise. "sendonly" for a
                one-way leg such as an overhead page, where the amplifier has
                nothing to send back.

        Returns:
            The originated `Call`, or None if it could not be started at
            all (no route, no free trunk channel, no relay available).
        """
        from pbx.features.webhooks import WebhookEvent
        from pbx.sip.message import SIPMessageBuilder
        from pbx.sip.sdp import SDPBuilder

        pbx = self.pbx_core
        call_router = pbx.call_router
        cid_number, cid_name = caller_id or (from_context, from_context)

        call_id = str(uuid.uuid4())
        call = pbx.call_manager.create_call(call_id, from_context, destination)
        call.start()
        call.originate_callbacks = {"on_answer": on_answer, "on_failure": on_failure}

        pbx.cdr_system.start_record(call_id, from_context, destination)
        pbx.webhook_system.trigger_event(
            WebhookEvent.CALL_STARTED,
            {
                "call_id": call_id,
                "from_extension": from_context,
                "to_extension": destination,
                "timestamp": call.start_time.isoformat() if call.start_time else None,
            },
        )

        if rtp_ports_override:
            call.rtp_ports = rtp_ports_override
            call.uses_peer_relay = True
        else:
            rtp_ports = pbx.rtp_relay.allocate_relay(call_id)
            if not rtp_ports:
                pbx.logger.error(f"Failed to allocate RTP relay for originated call {call_id}")
                self._fail_and_end(call, call_id, "unreachable")
                return None
            call.rtp_ports = rtp_ports

        server_ip = pbx._get_server_ip()

        # No caller SDP to intersect against -- offer the SDP builder's
        # default codec set, same as the PBX-originated transfer target leg
        # (TransferHandler._originate_target).
        invite_sdp = SDPBuilder.build_audio_sdp(
            server_ip,
            call.rtp_ports[0],
            session_id=call_id,
            codecs=codecs,
            direction=sdp_direction,
        )

        # Where the leg goes, and how the request line, To and Contact name
        # it: a trunk leg is addressed to the carrier and identified by the
        # dialed number, an extension leg to the phone's registered contact.
        # Everything after this block is identical for both.
        if call_router.EXTERNAL_NUMBER_PATTERN.match(destination):
            if not pbx.trunk_system:
                pbx.logger.error(f"No trunk system available to originate call to {destination}")
                self._fail_and_end(call, call_id, "no_route")
                return None
            trunk, transformed_number = pbx.trunk_system.route_outbound_with_failover(destination)
            if not trunk:
                pbx.logger.warning(f"No outbound trunk route found for {destination}")
                self._fail_and_end(call, call_id, "no_route")
                return None
            if not trunk.allocate_channel():
                pbx.logger.warning(f"Trunk {trunk.name} has no free channels for {destination}")
                self._fail_and_end(call, call_id, "unreachable")
                return None
            call.trunk = trunk

            dest_addr = (trunk.host, trunk.port)
            request_uri = f"sip:{transformed_number}@{trunk.host}:{trunk.port}"
            to_uri = f"sip:{transformed_number}@{trunk.host}"
            contact_user = transformed_number
        else:
            dest_ext_obj = call_router.resolve_extension(destination)
            if not dest_ext_obj or not dest_ext_obj.address:
                pbx.logger.error(f"Cannot get address for extension {destination}")
                self._fail_and_end(call, call_id, "no_route")
                return None
            # A WebRTC registration's address is the ("webrtc", session_id)
            # marker, not a routable contact. route_call() hands those to the
            # gateway; origination has no caller INVITE to hand it, so it
            # fails as a route here rather than putting the literal host
            # "webrtc" into sendto() and failing in DNS.
            if call_router.is_webrtc_address(dest_ext_obj.address):
                pbx.logger.warning(
                    f"Cannot originate to extension {destination}: registered as a "
                    f"WebRTC client, which the PBX cannot ring without a caller leg"
                )
                self._fail_and_end(call, call_id, "no_route")
                return None

            dest_addr = dest_ext_obj.address
            request_uri = f"sip:{destination}@{dest_addr[0]}:{dest_addr[1]}"
            to_uri = f"sip:{destination}@{server_ip}"
            contact_user = from_context

        sip_port = pbx.config.get("server.sip_port", 5060)
        invite_request = SIPMessageBuilder.build_request(
            method="INVITE",
            uri=request_uri,
            # From tag required on a new dialog (RFC 3261 SS8.1.1.3);
            # in-dialog follow-ups copy From from this message.
            from_addr=f"<sip:{cid_number}@{server_ip}>;tag={uuid.uuid4().hex[:8]}",
            to_addr=f"<{to_uri}>",
            call_id=call_id,
            cseq=1,
            body=invite_sdp,
        )
        invite_request.set_header("Contact", f"<sip:{contact_user}@{server_ip}:{sip_port}>")
        SIPMessageBuilder.add_caller_id_headers(invite_request, cid_number, cid_name, server_ip)

        # Set last so a caller can override a header this method chose -- and so an
        # auto-answer header cannot be clobbered by the caller-ID block above.
        for header_name, header_value in (extra_headers or {}).items():
            invite_request.set_header(header_name, header_value)

        call_router.send_leg_invite(
            call,
            call_id,
            invite_request,
            dest_addr,
            server_ip,
            on_timeout=lambda: self._handle_originate_no_answer(call_id),
        )

        call.no_answer_timer = threading.Timer(
            answer_timeout, self._handle_originate_no_answer, args=(call_id,)
        )
        call.no_answer_timer.daemon = True
        call.no_answer_timer.start()

        pbx.logger.info(f"Originated call {call_id}: {from_context} -> {destination}")
        return call

    def originate_and_bridge(
        self,
        leg_a: str,
        leg_b: str,
        *,
        from_context: str = "originator",
        answer_timeout: int = 30,
        on_answer: Callable[[Any], None] | None = None,
        on_failure: Callable[[Any, str], None] | None = None,
        on_leg_b_answer: Callable[[Any], None] | None = None,
        on_leg_b_failure: Callable[[Any, str], None] | None = None,
    ) -> tuple[Any | None, Any | None]:
        """
        Originate a call to `leg_a`, and once it answers, originate a
        second call to `leg_b` sharing `leg_a`'s relay -- bridging the two
        once `leg_b` also answers. Used for click-to-dial (ring the
        extension, then bridge to the dialed destination).

        `leg_a` is rung showing `leg_b` as the caller ID, since from that
        party's side the call is with `leg_b`, not with the PBX; and it hears
        hold music while `leg_b` rings, standing in for the ringback an
        inbound caller would get.

        Args:
            leg_a: First destination (extension or external number).
            leg_b: Second destination, bridged to leg_a once both answer.
            from_context: CDR/display identity for the leg_a origination
                (leg_b's origination uses leg_a's own number once it
                answers, since it's then the "caller" for the bridge).
            answer_timeout: Seconds each leg may ring unanswered.
            on_answer/on_failure: Outcome callbacks for leg_a.
            on_leg_b_answer/on_leg_b_failure: Outcome callbacks for leg_b;
                on_leg_b_answer fires once the two legs are bridged.

        Returns:
            (leg_a_call, leg_b_call_or_None) -- leg_b is None until leg_a
            answers; callers should not assume both are non-None on return.
        """
        pbx = self.pbx_core

        def _leg_a_answered(leg_a_call: Any) -> None:
            if on_answer:
                on_answer(leg_a_call)

            def _bridge(leg_b_call: Any) -> None:
                # Both legs are up: stop the ringback, point the far end of
                # leg_a's relay at leg_b, and make the pair one conversation
                # so recordings and transcripts stay together.
                pbx.moh_system.stop_moh(leg_a_call.call_id)
                leg_b_call.bridge_peer_side = "b"
                leg_b_call.join_session(leg_a_call)
                if leg_b_call.callee_rtp:
                    pbx.rtp_relay.replace_endpoint(
                        leg_a_call.call_id,
                        "b",
                        (leg_b_call.callee_rtp["address"], leg_b_call.callee_rtp["port"]),
                    )
                leg_a_call.state = CallState.CONNECTED
                leg_b_call.state = CallState.CONNECTED
                pbx.logger.info(
                    f"Bridge complete: {leg_a_call.call_id} <-> {leg_b_call.call_id} "
                    f"on relay of call {leg_a_call.call_id}"
                )
                if on_leg_b_answer:
                    on_leg_b_answer(leg_b_call)

            # leg_a has answered -- it's now the "caller" context for leg_b.
            leg_b_call = self.originate_call(
                leg_a_call.to_extension,
                leg_b,
                answer_timeout=answer_timeout,
                on_answer=_bridge,
                on_failure=on_leg_b_failure,
                rtp_ports_override=leg_a_call.rtp_ports,
            )
            if leg_b_call is None:
                # leg_b never got off the ground (no route, no channel). The
                # party on leg_a is holding an answered call with nothing on
                # the far end, so end it here.
                pbx.sip_server._send_leg_bye(leg_a_call)
                pbx.end_call(leg_a_call.call_id)
                return

            # One conversation from here on: ending either leg ends the
            # other, so a hangup while leg_b is still ringing stops the
            # ringing instead of leaving it to answer into a dead call.
            leg_a_call.bridged_peer_call_id = leg_b_call.call_id
            leg_b_call.bridged_peer_call_id = leg_a_call.call_id

            # Early media. leg_a is answered and its party would otherwise
            # hear dead air until leg_b picks up; they sit on side "a" of the
            # relay, which is the side that gets the music.
            relay_handler = pbx.rtp_relay.get_handler(leg_a_call.call_id)
            if relay_handler:
                pbx.moh_system.start_moh(leg_a_call.call_id, relay_handler, "a")

        leg_a_call = self.originate_call(
            from_context,
            leg_a,
            answer_timeout=answer_timeout,
            caller_id=(leg_b, leg_b),
            on_answer=_leg_a_answered,
            on_failure=on_failure,
        )
        pbx.logger.info(
            f"originate_and_bridge started: {leg_a} -> {leg_b} "
            f"(leg_a call {leg_a_call.call_id if leg_a_call else None})"
        )
        return leg_a_call, None

    def _fail_and_end(self, call: Any, call_id: str, reason: str) -> None:
        """
        Invoke the pending on_failure callback (if any) and end the call.

        Uses PBXCore.end_call() rather than CallManager.end_call() directly
        so the RTP relay, trunk channel (if any), and CDR record are all
        released the same way every other call-teardown path in the
        codebase releases them -- each of those steps already guards for
        "was this actually allocated" internally, so it's safe to call even
        when origination failed before a relay/trunk was ever acquired.

        A leg bridged to another takes that leg down with it -- a click-to-dial
        whose second leg never answers must not leave the first party holding
        a connected call with no one on it.
        """
        pbx = self.pbx_core
        callbacks = call.originate_callbacks
        if callbacks and callbacks.get("on_failure"):
            callbacks["on_failure"](call, reason)
        pbx.sip_server.end_bridged_peer(call)
        pbx.end_call(call_id)

    def _handle_originate_no_answer(self, call_id: str) -> None:
        """
        No-answer/timeout handler for a PBX-originated leg. Separate from
        CallRouter._handle_no_answer(), which assumes voicemail/trunk-480
        semantics tied to an inbound caller leg that doesn't exist here.
        """
        pbx = self.pbx_core
        call = pbx.call_manager.get_call(call_id)
        if not call or call.state == CallState.CONNECTED:
            return

        pbx.sip_server.cancel_leg(call)
        self._fail_and_end(call, call_id, "no_answer")
