"""
Call-origination primitive for PBX Core.

Handles calls the PBX places itself -- click-to-dial, and future
originate-driven features (predictive dialing, callback completion,
emergency-notification calls) -- as opposed to CallRouter, which reacts to
an inbound INVITE that already has a live caller leg to relay responses
back to. A call from here has no ``original_invite``: the PBX itself is
the "caller," so there is nothing to relay to.

Destination classification ("is this an extension or a PSTN number") and
leg construction are shared with CallRouter (``EXTERNAL_NUMBER_PATTERN``,
``_resolve_extension()``, ``_build_and_send_leg_invite()``) so both entry
points resolve calls identically -- but the two orchestration paths stay
separate because their call lifecycles genuinely differ (no caller leg to
answer back to, no PBX-owned voicemail fallback on no-answer).
"""

import threading
import uuid
from collections.abc import Callable
from typing import Any


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
        on_answer: Callable[[Any], None] | None = None,
        on_failure: Callable[[Any, str], None] | None = None,
        rtp_ports_override: tuple[int, int] | None = None,
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
            on_answer: Called with the `Call` once the destination answers.
            on_failure: Called with the `Call` and a reason string
                ("no_answer" | "busy" | "unreachable" | "no_route") if the
                leg never connects.
            rtp_ports_override: Reuse an already-allocated relay (e.g. the
                other leg's, when bridging) instead of allocating a new one.

        Returns:
            The originated `Call`, or None if it could not be started at
            all (no route, no free trunk channel, no relay available).
        """
        from pbx.features.webhooks import WebhookEvent
        from pbx.sip.message import SIPMessageBuilder
        from pbx.sip.sdp import SDPBuilder

        pbx = self.pbx_core
        call_router = pbx.call_router

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
        )

        trunk = None
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
            invite_request = SIPMessageBuilder.build_request(
                method="INVITE",
                uri=f"sip:{transformed_number}@{trunk.host}:{trunk.port}",
                # From tag required on a new dialog (RFC 3261 SS8.1.1.3);
                # in-dialog follow-ups copy From from this message.
                from_addr=f"<sip:{from_context}@{server_ip}>;tag={uuid.uuid4().hex[:8]}",
                to_addr=f"<sip:{transformed_number}@{trunk.host}>",
                call_id=call_id,
                cseq=1,
                body=invite_sdp,
            )
            sip_port = pbx.config.get("server.sip_port", 5060)
            invite_request.set_header(
                "Contact", f"<sip:{transformed_number}@{server_ip}:{sip_port}>"
            )
            SIPMessageBuilder.add_caller_id_headers(
                invite_request, from_context, from_context, server_ip
            )
        else:
            dest_ext_obj = call_router._resolve_extension(destination)
            if not dest_ext_obj or not dest_ext_obj.address:
                pbx.logger.error(f"Cannot get address for extension {destination}")
                self._fail_and_end(call, call_id, "no_route")
                return None

            dest_addr = dest_ext_obj.address
            invite_request = SIPMessageBuilder.build_request(
                method="INVITE",
                uri=f"sip:{destination}@{dest_addr[0]}:{dest_addr[1]}",
                # From tag required on a new dialog (RFC 3261 SS8.1.1.3);
                # in-dialog follow-ups copy From from this message.
                from_addr=f"<sip:{from_context}@{server_ip}>;tag={uuid.uuid4().hex[:8]}",
                to_addr=f"<sip:{destination}@{server_ip}>",
                call_id=call_id,
                cseq=1,
                body=invite_sdp,
            )
            sip_port = pbx.config.get("server.sip_port", 5060)
            invite_request.set_header(
                "Contact", f"<sip:{from_context}@{server_ip}:{sip_port}>"
            )
            SIPMessageBuilder.add_caller_id_headers(
                invite_request, from_context, from_context, server_ip
            )

        call_router._build_and_send_leg_invite(
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
        **kwargs: Any,
    ) -> tuple[Any | None, Any | None]:
        """
        Originate a call to `leg_a`, and once it answers, originate a
        second call to `leg_b` sharing `leg_a`'s relay -- bridging the two
        once `leg_b` also answers. Used for click-to-dial (ring the
        extension, then bridge to the dialed destination).

        Args:
            leg_a: First destination (extension or external number).
            leg_b: Second destination, bridged to leg_a once both answer.
            from_context: CDR/display identity for the leg_a origination
                (leg_b's origination uses leg_a's own number once it
                answers, since it's then the "caller" for the bridge).
            **kwargs: Forwarded to both `originate_call()` calls (e.g.
                `answer_timeout`); `on_answer`/`on_failure` apply to leg_a
                only -- pass `on_leg_b_answer`/`on_leg_b_failure` for leg_b.

        Returns:
            (leg_a_call, leg_b_call_or_None) -- leg_b is None until leg_a
            answers; callers should not assume both are non-None on return.
        """
        pbx = self.pbx_core
        on_leg_b_answer = kwargs.pop("on_leg_b_answer", None)
        on_leg_b_failure = kwargs.pop("on_leg_b_failure", None)
        user_on_answer = kwargs.pop("on_answer", None)
        user_on_failure = kwargs.pop("on_failure", None)

        def _leg_a_answered(leg_a_call: Any) -> None:
            if user_on_answer:
                user_on_answer(leg_a_call)
            # leg_a has answered -- it's now the "caller" context for leg_b.
            self.originate_call(
                leg_a_call.to_extension,
                leg_b,
                on_answer=lambda leg_b_call: self._complete_bridge(
                    leg_a_call, leg_b_call, on_leg_b_answer
                ),
                on_failure=on_leg_b_failure,
                rtp_ports_override=leg_a_call.rtp_ports,
                **kwargs,
            )

        leg_a_call = self.originate_call(
            from_context,
            leg_a,
            on_answer=_leg_a_answered,
            on_failure=user_on_failure,
            **kwargs,
        )
        pbx.logger.info(
            f"originate_and_bridge started: {leg_a} -> {leg_b} (leg_a call {leg_a_call.call_id if leg_a_call else None})"
        )
        return leg_a_call, None

    def _complete_bridge(self, leg_a_call: Any, leg_b_call: Any, on_leg_b_answer: Callable[[Any], None] | None) -> None:
        """
        Cross-link two answered, PBX-originated legs and hand media on
        leg_a's relay over to leg_b, mirroring TransferHandler.bridge()'s
        bridge-completion step.
        """
        from pbx.core.call import CallState

        pbx = self.pbx_core

        leg_a_call.bridged_peer_call_id = leg_b_call.call_id
        leg_b_call.bridged_peer_call_id = leg_a_call.call_id
        leg_b_call.bridge_peer_side = "b"

        if leg_b_call.callee_rtp:
            dest_endpoint = (leg_b_call.callee_rtp["address"], leg_b_call.callee_rtp["port"])
            pbx.rtp_relay.replace_endpoint(leg_a_call.call_id, "b", dest_endpoint)

        leg_a_call.state = CallState.CONNECTED
        leg_b_call.state = CallState.CONNECTED

        pbx.logger.info(
            f"Bridge complete: {leg_a_call.call_id} <-> {leg_b_call.call_id} "
            f"on relay of call {leg_a_call.call_id}"
        )
        if on_leg_b_answer:
            on_leg_b_answer(leg_b_call)

    def _fail_and_end(self, call: Any, call_id: str, reason: str) -> None:
        """
        Invoke the pending on_failure callback (if any) and end the call.

        Uses PBXCore.end_call() rather than CallManager.end_call() directly
        so the RTP relay, trunk channel (if any), and CDR record are all
        released the same way every other call-teardown path in the
        codebase releases them -- each of those steps already guards for
        "was this actually allocated" internally, so it's safe to call even
        when origination failed before a relay/trunk was ever acquired.
        """
        pbx = self.pbx_core
        callbacks = getattr(call, "originate_callbacks", None)
        if callbacks and callbacks.get("on_failure"):
            callbacks["on_failure"](call, reason)
        pbx.end_call(call_id)

    def _handle_originate_no_answer(self, call_id: str) -> None:
        """
        No-answer/timeout handler for a PBX-originated leg. Separate from
        CallRouter._handle_no_answer(), which assumes voicemail/trunk-480
        semantics tied to an inbound caller leg that doesn't exist here.
        """
        pbx = self.pbx_core
        call_router = pbx.call_router

        call = pbx.call_manager.get_call(call_id)
        if not call or call.state.value == "connected":
            return

        call_router._send_cancel_to_callee(call, call_id)
        self._fail_and_end(call, call_id, "no_answer")
