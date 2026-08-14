"""Shared harness for ordinary (non-transfer) call-sequence tests.

The transfer state machine has had a real-component harness for a while
(``transfer_harness``); ordinary calls never did, and every bug this file
exists to catch -- a CANCEL that never reached the callee, a decline that
never reached the caller, a hangup that left the far end ringing -- lives in
the gap between "route_call() returned True" and "the right bytes went out on
the wire in the right order".

So this harness drives whole sequences through real components:

- ``call_manager`` is a real CallManager and ``pbx.end_call`` really removes
  calls and releases relays, so leaks are detectable.
- ``sip_server`` is a real SIPServer with only the socket write mocked, and
  ``call_router`` is a real CallRouter. Requests go in as raw SIP text and are
  parsed by the real parser; every response the PBX emits is captured as raw
  text and can be asserted on, including its status line and headers.
- Nothing stubs ``_send_response``: responses funnel through ``_send_message``
  like they do in production, so a test cannot pass because a builder was
  skipped.
"""

from __future__ import annotations

import itertools
from typing import Any
from unittest.mock import MagicMock

from pbx.core.call import CallManager  # noqa: TC001  (constructed, not just annotated)
from pbx.core.call_router import CallRouter
from pbx.sip.message import SIPMessage
from pbx.sip.server import SIPServer

CALLER_ADDR = ("192.168.10.20", 5060)  # the phone that dials
CALLEE_ADDR = ("192.168.10.30", 5060)  # the phone being dialled
SERVER_IP = "192.168.1.14"

CALLER_EXT = "1001"
CALLEE_EXT = "1002"
CALL_ID = "seq-call-1"

CALLER_SDP_PORT = 40000
CALLEE_SDP_PORT = 41000


def sdp(host: str, port: int) -> str:
    """A minimal but real audio SDP body the SDP parser accepts."""
    return (
        "v=0\r\n"
        f"o=- 1 1 IN IP4 {host}\r\n"
        "s=call\r\n"
        f"c=IN IP4 {host}\r\n"
        "t=0 0\r\n"
        f"m=audio {port} RTP/AVP 0 8 101\r\n"
        "a=rtpmap:0 PCMU/8000\r\n"
        "a=rtpmap:8 PCMA/8000\r\n"
        "a=rtpmap:101 telephone-event/8000\r\n"
    )


def invite(
    *,
    from_ext: str = CALLER_EXT,
    to_ext: str = CALLEE_EXT,
    call_id: str = CALL_ID,
    body: str | None = None,
) -> SIPMessage:
    """An INVITE as a phone would send it, parsed by the real parser."""
    body = sdp(CALLER_ADDR[0], CALLER_SDP_PORT) if body is None else body
    raw = (
        f"INVITE sip:{to_ext}@{SERVER_IP}:5060 SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP {CALLER_ADDR[0]}:{CALLER_ADDR[1]};branch=z9hG4bKcaller1;rport\r\n"
        f"From: <sip:{from_ext}@{SERVER_IP}>;tag=callertag\r\n"
        f"To: <sip:{to_ext}@{SERVER_IP}>\r\n"
        f"Call-ID: {call_id}\r\n"
        "CSeq: 1 INVITE\r\n"
        f"Contact: <sip:{from_ext}@{CALLER_ADDR[0]}:{CALLER_ADDR[1]}>\r\n"
        "User-Agent: HarnessPhone/1.0\r\n"
        "Max-Forwards: 70\r\n"
        "Content-Type: application/sdp\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
        f"{body}"
    )
    return SIPMessage(raw)


def request(method: str, *, call_id: str = CALL_ID, cseq: int = 2) -> SIPMessage:
    """An in-dialog request from the caller (BYE, CANCEL, ACK)."""
    raw = (
        f"{method} sip:{CALLEE_EXT}@{SERVER_IP}:5060 SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP {CALLER_ADDR[0]}:{CALLER_ADDR[1]};branch=z9hG4bKcaller1\r\n"
        f"From: <sip:{CALLER_EXT}@{SERVER_IP}>;tag=callertag\r\n"
        f"To: <sip:{CALLEE_EXT}@{SERVER_IP}>;tag=calleetag\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: {cseq} {method}\r\n"
        "Max-Forwards: 70\r\n"
        "Content-Length: 0\r\n\r\n"
    )
    return SIPMessage(raw)


def callee_response(
    status: int,
    reason: str,
    *,
    call_id: str = CALL_ID,
    body: str | None = None,
    cseq_method: str = "INVITE",
) -> SIPMessage:
    """
    A response as the *callee's phone* would send it to the PBX.

    The Via is the PBX's own, echoed back, which is what the response path
    matches against.
    """
    body = body or ""
    raw = (
        f"SIP/2.0 {status} {reason}\r\n"
        f"Via: SIP/2.0/UDP {SERVER_IP}:5060;branch=z9hG4bKleg\r\n"
        f"From: <sip:{CALLER_EXT}@{SERVER_IP}>;tag=pbxtag\r\n"
        f"To: <sip:{CALLEE_EXT}@{SERVER_IP}>;tag=calleetag\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: 1 {cseq_method}\r\n"
        + ("Content-Type: application/sdp\r\n" if body else "")
        + f"Content-Length: {len(body)}\r\n\r\n"
        f"{body}"
    )
    return SIPMessage(raw)


_relay_port_seq = itertools.count(21000, 2)


def _next_relay_ports() -> tuple[int, int]:
    """A relay port pair no other harness instance in this process will use."""
    base = next(_relay_port_seq)
    return base, base + 1


def make_pbx(call_manager: CallManager, **config: Any) -> MagicMock:
    """
    Build a PBXCore stand-in around a real CallManager, CallRouter and
    SIPServer, with one registered destination extension.

    Args:
        call_manager: The real CallManager to bind.
        **config: Config overrides merged over the defaults (e.g.
            ``voicemail_enabled=False``).

    Returns:
        The mock PBXCore.
    """
    pbx = MagicMock()
    pbx.call_manager = call_manager
    pbx.logger = MagicMock()
    pbx._get_server_ip.return_value = SERVER_IP
    pbx._get_compatible_codecs.return_value = ["0", "8"]
    pbx._get_codecs_for_phone_model.return_value = ["0", "8"]
    pbx._get_phone_user_agent.return_value = "HarnessPhone/1.0"
    pbx._detect_phone_model.return_value = None
    pbx._get_dtmf_payload_type.return_value = 101
    pbx._get_ilbc_mode.return_value = 30

    settings = {
        "server.sip_port": 5060,
        "voicemail.no_answer_timeout": 30,
        "dialplan.allow_all": True,
    }
    settings.update(config)
    pbx.config.get.side_effect = lambda key, default=None: settings.get(key, default)

    # The dialled extension is registered at a real address.
    dest = MagicMock()
    dest.address = CALLEE_ADDR
    dest.registered = True
    dest.is_expired.return_value = False
    dest.config = {}
    pbx.extension_registry.get.return_value = dest
    pbx.extension_registry.get_extension.return_value = dest
    pbx.registered_phones_db = None

    # An ordinary extension-to-extension call: none of route_call's earlier
    # dispatch branches apply. These are set explicitly because a bare
    # MagicMock predicate is truthy, which silently diverts the call --
    # karis_law in particular would swallow every INVITE as an emergency.
    pbx.karis_law = None
    pbx.auto_attendant = None
    pbx.trunk_system = None
    pbx.webrtc_gateway = None
    pbx.queue_handler.is_queue_destination.return_value = False
    pbx.paging_system.is_paging_extension.return_value = False

    relays: dict[str, Any] = {}
    pbx.rtp_relay.active_relays = relays
    pbx.rtp_relay.get_handler.side_effect = relays.get
    pbx.rtp_relay.release_relay.side_effect = lambda call_id: relays.pop(call_id, None)

    # Distinct ports per harness instance. The relay itself is a mock, but the
    # voicemail path opens a real recorder socket on the allocated port, and
    # reusing one port across tests in a process makes the second one fail to
    # bind -- which looks like a voicemail bug rather than a test collision.
    ports = _next_relay_ports()
    pbx.rtp_relay.allocate_relay.side_effect = lambda call_id: (
        relays.setdefault(call_id, MagicMock()) and ports
    )
    pbx.relay_ports = ports

    def _end_call(call_id: str) -> None:
        relays.pop(call_id, None)
        call_manager.end_call(call_id)

    pbx.end_call.side_effect = _end_call

    pbx.sip_server = make_server(pbx)
    pbx.call_router = CallRouter(pbx)
    return pbx


def make_server(pbx: MagicMock) -> SIPServer:
    """A real SIPServer with only the socket write mocked."""
    server = SIPServer.__new__(SIPServer)
    server.pbx_core = pbx
    server.logger = MagicMock()
    server.socket = MagicMock()
    server._send_message = MagicMock()  # type: ignore[method-assign]
    server._pending_trunk_registrations = {}
    return server


# ----------------------------------------------------------------------
# What actually went out on the wire
# ----------------------------------------------------------------------


def sent(pbx: MagicMock) -> list[tuple[str, tuple[str, int]]]:
    """Every (raw message, destination) the SIP server wrote, in order."""
    return [
        (args[0] if isinstance(args[0], str) else args[0].decode(), args[1])
        for args, _ in pbx.sip_server._send_message.call_args_list
    ]


def requests(pbx: MagicMock, method: str) -> list[tuple[str, tuple[str, int]]]:
    """Every request sent with the given method."""
    return [(raw, dest) for raw, dest in sent(pbx) if raw.split(" ", 1)[0] == method]


def responses(pbx: MagicMock, *, to: tuple[str, int] | None = None) -> list[int]:
    """Status codes of every response sent, optionally filtered by destination."""
    out = []
    for raw, dest in sent(pbx):
        if not raw.startswith("SIP/2.0 "):
            continue
        if to is not None and dest != to:
            continue
        out.append(int(raw.split(" ")[1]))
    return out


def body_of(raw: str) -> str:
    """The body of a raw SIP message."""
    return raw.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in raw else ""


def assert_no_leaks(pbx: MagicMock, *, allow: set[str] | None = None) -> None:
    """Assert no Call record and no RTP relay outlived the sequence."""
    allowed = allow or set()
    active = set(pbx.call_manager.active_calls)
    assert active <= allowed, f"leaked call records: {sorted(active - allowed)}"
    relays = set(pbx.rtp_relay.active_relays)
    assert relays <= allowed, f"leaked RTP relays: {sorted(relays - allowed)}"
