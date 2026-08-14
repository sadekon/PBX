"""
How the PBX signals the party on the *caller* side of a call.

The two sides of a call are not symmetric. The callee is always a party the
PBX INVITEd, so it is signalled with requests. The caller is whoever the call
exists for, and how you talk to them depends on how they got there: a phone
that dialled in has an INVITE outstanding, so "ringing" and "busy" are
responses to it; a leg the PBX placed (click-to-dial ringing you first) has
already answered, so the same events have to be audio and requests instead.

Every site that tells the caller something used to build a response inline
against ``original_invite``, which quietly did nothing for a PBX-placed leg.
That one gap is why click-to-dial heard no ringback, never reached voicemail,
and never learned the far end was busy.

Modelled on Asterisk's ``ast_channel_tech`` indicate/answer callbacks: call
sites say *what happened*, the channel decides how to say it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pbx.core.call import Call


class SipCaller:
    """
    A caller that dialled in and has an INVITE outstanding.

    Each signal is a response built against that INVITE, so the Via matches
    the caller's transaction -- forwarding the callee's own response would
    carry the PBX's Via and be dropped.
    """

    def __init__(self, pbx_core: Any, call: Call) -> None:
        self.pbx = pbx_core
        self.call = call

    def _send(self, status: int, reason: str, body: str = "", *, contact: bool = False) -> None:
        from pbx.sip.message import SIPMessageBuilder

        call = self.call
        response = SIPMessageBuilder.build_response(status, reason, call.original_invite, body=body)
        if body:
            response.set_header("Content-type", "application/sdp")
        if contact:
            # Where the caller sends ACK, BYE and re-INVITEs from here on.
            server_ip = self.pbx._get_server_ip()
            sip_port = self.pbx.config.get("server.sip_port", 5060)
            response.set_header("Contact", f"<sip:{call.to_extension}@{server_ip}:{sip_port}>")
        # One To tag identifies this dialog for its whole life; build_response
        # mints a fresh one each time, so the first to carry it wins and the
        # rest reuse it. Two tags read as two dialogs.
        if call.caller_dialog_to:
            response.set_header("To", call.caller_dialog_to)
        else:
            call.caller_dialog_to = response.get_header("To")
        self.pbx.sip_server._send_message(response.build(), call.caller_addr)

    def progress(self, status: int, reason: str, *, body: str | None = None) -> None:
        self._send(status, reason, body or "")

    def answer(self, body: str) -> None:
        self._send(200, "OK", body, contact=True)

    def reject(self, status: int, reason: str) -> None:
        self._send(status, reason)


class OriginatedCaller:
    """
    A leg the PBX placed, which answered before the far end was even dialled.

    Nothing is sent here, and that is the point rather than an omission:
    there is no transaction to respond to, and the party is already connected
    and hearing whatever the bridge plays them.

    In particular this must not hang the leg up on failure. A placed leg is
    half of a bridged pair, so the party to tell is the one on the *other*
    leg -- and every path that rejects a call already calls
    ``SIPServer.end_bridged_peer()`` immediately afterwards, which tells
    exactly that party. Hanging up here as well would send a BYE to the
    phone that just declined, which answers 481.
    """

    def __init__(self, pbx_core: Any, call: Call) -> None:
        self.pbx = pbx_core
        self.call = call

    def progress(self, status: int, reason: str, *, body: str | None = None) -> None:
        """Nothing to send: this party answered before the far end rang."""

    def answer(self, body: str) -> None:
        """Nothing to send: this party is already answered."""

    def reject(self, status: int, reason: str) -> None:
        self.pbx.logger.info(f"Placed leg {self.call.call_id} failed: {status} {reason}")


def caller_of(pbx_core: Any, call: Call) -> SipCaller | OriginatedCaller:
    """
    Which kind of caller this call has -- derived, never stored.

    The answer is already implied by the call: a party who dialled in left an
    INVITE to respond to, a leg the PBX placed did not. Recording it again on
    the record would be a second copy of something already known, free to
    drift out of step with the first.
    """
    if call.original_invite and call.caller_addr:
        return SipCaller(pbx_core, call)
    return OriginatedCaller(pbx_core, call)
