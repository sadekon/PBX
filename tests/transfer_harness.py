"""Shared harness for the call-transfer tests.

Builds a PBXCore stand-in that is mock enough to be cheap but real enough to
prove the properties the transfer state machine exists to guarantee:

- ``call_manager`` is a real CallManager and ``pbx.end_call`` really removes
  calls and releases relays, so tests can assert that no Call record or RTP
  relay leaks.
- ``sip_server`` is a real SIPServer with only the socket mocked, so the actual
  BYE/CANCEL/NOTIFY builders run and their headers can be inspected.
- ``call_router._send_cancel_to_callee`` is the real implementation, so a
  cancelled leg is cancelled the way production cancels it.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from pbx.core.call import Call, CallManager, CallState
from pbx.core.call_router import CallRouter
from pbx.core.transfer_handler import TransferHandler
from pbx.sip.server import SIPServer

A_ADDR = ("192.168.10.139", 5060)  # Transferor (referrer)
B_ADDR = ("192.168.10.155", 5060)  # Transferee (stays on the call)
C_ADDR = ("192.168.10.140", 5061)  # Transfer destination

A_RTP = {"address": "192.168.10.139", "port": 3000}
B_RTP = {"address": "192.168.10.155", "port": 3008}
C_RTP = {"address": "192.168.10.140", "port": 3002}

SERVER_IP = "192.168.1.14"

# Refer-To exactly as captured from a Zultys ZIP 33G attended transfer
CAPTURE_REFER_TO = (
    "<sip:1517@192.168.1.14:5060;user=phone?Replaces=0_3666015387%40192.168.10.139"
    "%3Bto-tag%3D92aec109%3Bfrom-tag%3D3228926622>"
)


def make_pbx(
    call_manager: CallManager,
    *,
    drop_on_failure: bool = True,
    no_answer_timeout: int = 15,
    callback_retries: int = 2,
    loop_delay: int = 10,
) -> MagicMock:
    """
    Build a PBXCore stand-in wired to a real CallManager and TransferHandler.

    Args:
        call_manager: The real CallManager to bind.
        drop_on_failure: Asterisk ``atxferdropcall``. Defaults to True so
            failure paths tear down deterministically; the recall tests flip it
            to exercise the production default.
        no_answer_timeout: Asterisk ``atxfernoanswertimeout``.
        callback_retries: Asterisk ``atxfercallbackretries``.
        loop_delay: Asterisk ``atxferloopdelay``.

    Returns:
        The mock PBXCore.
    """
    pbx = MagicMock()
    pbx.call_manager = call_manager
    pbx.moh_system = MagicMock()
    pbx.cdr_system = MagicMock()
    pbx.webhook_system = MagicMock()
    pbx.logger = MagicMock()
    pbx._get_server_ip.return_value = SERVER_IP
    pbx._get_compatible_codecs.return_value = ["0", "8"]
    pbx._get_dtmf_payload_type.return_value = 101
    pbx._get_ilbc_mode.return_value = 30

    pbx.config.get.side_effect = lambda key, default=None: {
        "server.sip_port": 5060,
        "voicemail.no_answer_timeout": 30,
        "transfer.atxfernoanswertimeout": no_answer_timeout,
        "transfer.atxferdropcall": drop_on_failure,
        "transfer.atxfercallbackretries": callback_retries,
        "transfer.atxferloopdelay": loop_delay,
    }.get(key, default)

    # A relay registry real enough to detect leaks.
    relays: dict[str, Any] = {}
    pbx.rtp_relay.active_relays = relays
    pbx.rtp_relay.get_handler.side_effect = relays.get
    pbx.rtp_relay.release_relay.side_effect = lambda call_id: relays.pop(call_id, None)
    pbx.rtp_relay.allocate_relay.side_effect = lambda call_id: (
        relays.setdefault(call_id, MagicMock()) and (21000, 21001)
    )

    # end_call must really end the call, or leak assertions prove nothing.
    def _end_call(call_id: str) -> None:
        relays.pop(call_id, None)
        call_manager.end_call(call_id)

    pbx.end_call.side_effect = _end_call

    pbx.sip_server = make_server(pbx)
    pbx.transfer_handler = TransferHandler(pbx)

    router = CallRouter(pbx)
    pbx.call_router.handle_callee_answer.side_effect = router.handle_callee_answer
    pbx.call_router._send_cancel_to_callee.side_effect = router._send_cancel_to_callee

    return pbx


def make_server(pbx: MagicMock) -> SIPServer:
    """Build a real SIPServer with only its socket writes mocked."""
    server = SIPServer.__new__(SIPServer)
    server.pbx_core = pbx
    server.logger = MagicMock()
    server._send_message = MagicMock()  # type: ignore[method-assign]
    server._send_response = MagicMock()  # type: ignore[method-assign]
    server._pending_trunk_registrations = {}
    return server


def make_call(
    manager: CallManager,
    call_id: str,
    from_ext: str,
    to_ext: str,
    *,
    state: CallState = CallState.CONNECTED,
    caller_addr: tuple[str, int] | None = None,
    callee_addr: tuple[str, int] | None = None,
    caller_rtp: dict[str, Any] | None = None,
    callee_rtp: dict[str, Any] | None = None,
    relays: dict[str, Any] | None = None,
) -> Call:
    """Create a Call with the dialog identity the BYE builders need."""
    call = manager.create_call(call_id, from_ext, to_ext)
    call.state = state
    call.caller_addr = caller_addr
    call.callee_addr = callee_addr
    call.callee_rtp = callee_rtp
    call.caller_rtp = caller_rtp or {"address": "1.2.3.4", "port": 30000}
    call.rtp_ports = (20000, 20001)

    call.original_invite = MagicMock()
    call.original_invite.get_header.side_effect = {
        "From": f'"Test" <sip:{from_ext}@{SERVER_IP}:5060>;tag=1687173424',
        "To": f"<sip:{to_ext}@{SERVER_IP}:5060;user=phone>",
        "Via": f"SIP/2.0/UDP {SERVER_IP}:5060;branch=z9hG4bKorig",
        "CSeq": "1 INVITE",
    }.get
    call.callee_dialog_to = f"<sip:{to_ext}@{SERVER_IP}:5060;user=phone>;tag=518364649"
    call.caller_dialog_to = f"<sip:{to_ext}@{SERVER_IP}:5060>;tag=pbxtag"

    if relays is not None:
        relays[call_id] = MagicMock()
    return call


def attended_pair(pbx: MagicMock, *, consult_answered: bool = True) -> tuple[Call, Call]:
    """
    Original A->B call parked, plus the transferor's A->C consultation leg.

    A (1513) is the transferor on the caller side; B (1512) is the transferee;
    C (1517) is the target.
    """
    cm = pbx.call_manager
    relays = pbx.rtp_relay.active_relays

    original = make_call(
        cm,
        "call1",
        "1513",
        "1512",
        state=CallState.HOLD,
        caller_addr=A_ADDR,
        callee_addr=B_ADDR,
        caller_rtp=A_RTP,
        callee_rtp=B_RTP,
        relays=relays,
    )
    consult = make_call(
        cm,
        "call2",
        "1513",
        "1517",
        state=CallState.CONNECTED if consult_answered else CallState.RINGING,
        caller_addr=A_ADDR,
        callee_addr=C_ADDR if consult_answered else None,
        callee_rtp=C_RTP if consult_answered else None,
        relays=relays if consult_answered else None,
    )
    consult.callee_invite = MagicMock()
    consult.callee_invite.uri = f"sip:1517@{C_ADDR[0]}:{C_ADDR[1]}"
    consult.callee_invite.get_header.side_effect = {
        "From": f'"Test" <sip:1513@{SERVER_IP}:5060>;tag=3228926622',
        "To": f"<sip:1517@{SERVER_IP}:5060>",
        "Via": f"SIP/2.0/UDP {SERVER_IP}:5060;branch=z9hG4bKconsult",
        "CSeq": "1 INVITE",
    }.get
    return original, consult


# ----------------------------------------------------------------------
# Assertions on what actually went out on the wire
# ----------------------------------------------------------------------


def sent_messages(pbx: MagicMock) -> list[tuple[str, tuple[str, int]]]:
    """Every (raw message, destination) the SIP server wrote."""
    return [
        (args[0] if isinstance(args[0], str) else args[0].decode(), args[1])
        for args, _ in pbx.sip_server._send_message.call_args_list
    ]


def messages_of(pbx: MagicMock, method: str) -> list[tuple[str, tuple[str, int]]]:
    """Every sent request whose start line uses `method`."""
    return [(raw, dest) for raw, dest in sent_messages(pbx) if raw.split(" ", 1)[0] == method]


def header_of(raw: str, name: str) -> str | None:
    """First value of a header in a raw SIP message."""
    for line in raw.split("\r\n"):
        if not line or line == raw.split("\r\n", 1)[0]:
            continue
        if line.lower().startswith(f"{name.lower()}:"):
            return line.split(":", 1)[1].strip()
    return None


def notifies(pbx: MagicMock) -> list[dict[str, Any]]:
    """
    Every REFER-subscription NOTIFY sent, decomposed for assertions.

    Returns:
        One dict per NOTIFY with its cseq, subscription state, sipfrag body,
        and whether the mandatory routing headers were present.
    """
    out: list[dict[str, Any]] = []
    for raw, dest in messages_of(pbx, "NOTIFY"):
        cseq_raw = header_of(raw, "CSeq") or "0 NOTIFY"
        body = raw.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in raw else ""
        state = header_of(raw, "Subscription-State") or ""
        out.append(
            {
                "cseq": int(cseq_raw.split()[0]),
                "state": state,
                "terminated": state.startswith("terminated"),
                "body": body.strip(),
                "via": header_of(raw, "Via"),
                "max_forwards": header_of(raw, "Max-Forwards"),
                "dest": dest,
            }
        )
    return out


def assert_notify_contract(pbx: MagicMock, *, expect_final: bool = True) -> None:
    """
    Assert the properties whose absence leaves phones stuck "transferring".

    Every NOTIFY must be routable (Via + Max-Forwards) and carry a CSeq
    strictly greater than the one before it, and the subscription must be
    closed by exactly one terminating NOTIFY.

    Args:
        pbx: The harness PBX whose sent messages to inspect.
        expect_final: Whether a terminating NOTIFY is required.
    """
    sent = notifies(pbx)
    assert sent, "no NOTIFY was sent at all"

    for n in sent:
        assert n["via"], f"NOTIFY {n['body']!r} has no Via; strict UAs drop it"
        assert n["max_forwards"], f"NOTIFY {n['body']!r} has no Max-Forwards"

    cseqs = [n["cseq"] for n in sent]
    assert cseqs == sorted(set(cseqs)), (
        f"NOTIFY CSeqs must strictly increase, got {cseqs} -- a repeat reads as "
        "a retransmission and the phone ignores it"
    )

    finals = [n for n in sent if n["terminated"]]
    if expect_final:
        assert len(finals) == 1, f"expected exactly one final NOTIFY, got {len(finals)}"
    else:
        assert not finals, "did not expect the subscription to be closed yet"


def assert_no_leaks(pbx: MagicMock, *, allow: set[str] | None = None) -> None:
    """
    Assert no Call record and no RTP relay outlived the transfer.

    Args:
        pbx: The harness PBX to inspect.
        allow: Call ids that are legitimately still active (the surviving
            bridged legs, after a successful transfer).
    """
    allowed = allow or set()
    active = set(pbx.call_manager.active_calls)
    assert active <= allowed, f"leaked call records: {sorted(active - allowed)}"

    relays = set(pbx.rtp_relay.active_relays)
    assert relays <= allowed, f"leaked RTP relays: {sorted(relays - allowed)}"
