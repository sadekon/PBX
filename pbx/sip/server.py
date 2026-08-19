"""
SIP Server implementation
"""

from __future__ import annotations

import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from pbx.sip.message import SIPMessage, SIPMessageBuilder
from pbx.utils.logger import get_logger

if TYPE_CHECKING:
    from pbx.core.pbx import PBXCore

# Type alias for network address tuples
type AddrTuple = tuple[str, int]

# Valid DTMF digits for SIP INFO validation
VALID_DTMF_DIGITS: list[str] = [
    "0",
    "1",
    "2",
    "3",
    "4",
    "5",
    "6",
    "7",
    "8",
    "9",
    "*",
    "#",
    "A",
    "B",
    "C",
    "D",
]

# RFC 2833 Event Code to DTMF digit mapping (for SIP INFO messages that send event codes)
# Some phones send "Signal=11" instead of "Signal=#"
RFC2833_EVENT_TO_DTMF: dict[str, str] = {
    "0": "0",
    "1": "1",
    "2": "2",
    "3": "3",
    "4": "4",
    "5": "5",
    "6": "6",
    "7": "7",
    "8": "8",
    "9": "9",
    "10": "*",
    "11": "#",
    "12": "A",
    "13": "B",
    "14": "C",
    "15": "D",
}

#: excludes 404/410/484 and 5xx: those mean the destination does not exist or
#: is broken, which the caller should actually hear rather than have hidden
#: behind a mailbox greeting.
VOICEMAIL_ON_REJECT_STATUSES: frozenset[int] = frozenset({408, 480, 486, 600, 603})

#: SIP status -> the reason string CallOriginator documents for on_failure.
#: Anything not listed is reported as "unreachable", which covers the media and
#: server errors (488, 5xx) as well as the genuinely unroutable ones.
_ORIGINATE_FAILURE_REASONS: dict[int, str] = {
    408: "no_answer",
    480: "no_answer",
    486: "busy",
    600: "busy",
    603: "busy",
    404: "no_route",
    410: "no_route",
    484: "no_route",
    604: "no_route",
}


class SIPServer:
    """SIP server for handling registration and calls."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 5060,
        pbx_core: PBXCore | None = None,  # nosec B104 - SIP server needs to bind all interfaces
    ) -> None:
        """
        Initialize SIP server.

        Args:
            host: Host to bind to.
            port: Port to bind to.
            pbx_core: Reference to PBX core.
        """
        self.host: str = host
        self.port: int = port
        self.pbx_core: PBXCore | None = pbx_core
        self.logger = get_logger()
        self.socket: socket.socket | None = None
        self.running: bool = False

        # Subscription tracking for SUBSCRIBE/NOTIFY (RFC 6665)
        # Key: (from_uri, event_type), Value: subscription info dict
        self.subscriptions: dict[tuple[str, str], dict] = {}

        # Published event state for PUBLISH (RFC 3903)
        # Key: (event_type, entity_tag), Value: publication info dict
        self.publications: dict[tuple[str, str], dict] = {}
        self._etag_counter: int = 0

        # Reliable provisional response tracking for PRACK (RFC 3262)
        # Key: (call_id, rseq), Value: provisional response info
        self.pending_provisional_responses: dict[tuple[str, int], dict] = {}
        self._rseq_counter: int = 1

        # Bounded thread pool to prevent thread exhaustion under load
        self._thread_pool = ThreadPoolExecutor(max_workers=200, thread_name_prefix="sip-handler")

        # Outbound trunk REGISTER transactions awaiting a response.
        # Key: Call-ID, Value: {"trunk": SIPTrunk, "cseq": int, "retried": bool}
        self._pending_trunk_registrations: dict[str, dict[str, Any]] = {}

    def start(self) -> bool:
        """
        Start SIP server.

        Returns:
            True if the server started successfully, False otherwise.
        """
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # set socket timeout to allow periodic checking of running flag
            self.socket.settimeout(1.0)
            self.socket.bind((self.host, self.port))
            self.running = True

            self.logger.info(f"SIP server started on {self.host}:{self.port}")

            # Start listening thread
            listen_thread = threading.Thread(target=self._listen)
            listen_thread.daemon = True
            listen_thread.start()

            return True
        except OSError as e:
            self.logger.error(f"Failed to start SIP server: {e}")
            return False

    def stop(self) -> None:
        """Stop SIP server."""
        self.running = False
        self._thread_pool.shutdown(wait=False)
        if self.socket:
            self.socket.close()
        self.logger.info("SIP server stopped")

    def _listen(self) -> None:
        """Listen for incoming SIP messages."""
        self.logger.info("SIP server listening for messages...")

        sock = self.socket
        if sock is None:
            return
        while self.running:
            try:
                data, addr = sock.recvfrom(65535)

                try:
                    message_text = data.decode("utf-8")
                except UnicodeDecodeError:
                    self.logger.warning(f"Malformed UTF-8 from {addr}, using lossy decode")
                    message_text = data.decode("utf-8", errors="replace")

                # Submit to bounded thread pool instead of spawning unbounded threads
                self._thread_pool.submit(self._handle_message, message_text, addr)

            except TimeoutError:
                # Timeout allows us to check running flag periodically
                continue
            except OSError as e:
                if self.running:
                    self.logger.error(f"Error receiving message: {e}")

        self.logger.info("SIP server listening thread stopped")

    def _handle_message(self, raw_message: str, addr: AddrTuple) -> None:
        """
        Handle incoming SIP message.

        Args:
            raw_message: Raw SIP message string.
            addr: Source address tuple (host, port).
        """
        try:
            message = SIPMessage(raw_message)

            self.logger.debug(f"Received {message.method or message.status_code} from {addr}")

            if message.is_request():
                self._handle_request(message, addr)
            else:
                self._handle_response(message, addr)

        except Exception as e:
            self.logger.error(f"Error handling message: {e}")

    def _handle_request(self, message: SIPMessage, addr: AddrTuple) -> None:
        """
        Handle SIP request.

        Args:
            message: SIPMessage object.
            addr: Source address.
        """
        method = message.method

        if method == "REGISTER":
            self._handle_register(message, addr)
        elif method == "INVITE":
            self._handle_invite(message, addr)
        elif method == "ACK":
            self._handle_ack(message, addr)
        elif method == "BYE":
            self._handle_bye(message, addr)
        elif method == "CANCEL":
            self._handle_cancel(message, addr)
        elif method == "OPTIONS":
            self._handle_options(message, addr)
        elif method == "SUBSCRIBE":
            self._handle_subscribe(message, addr)
        elif method == "NOTIFY":
            self._handle_notify(message, addr)
        elif method == "REFER":
            self._handle_refer(message, addr)
        elif method == "INFO":
            self._handle_info(message, addr)
        elif method == "MESSAGE":
            self._handle_sip_message_method(message, addr)
        elif method == "PRACK":
            self._handle_prack(message, addr)
        elif method == "UPDATE":
            self._handle_update(message, addr)
        elif method == "PUBLISH":
            self._handle_publish(message, addr)
        else:
            self.logger.warning(f"Unhandled SIP method: {method}")
            self._send_response(405, "Method Not Allowed", message, addr)

    def _handle_register(self, message: SIPMessage, addr: AddrTuple) -> None:
        """
        Handle REGISTER request with digest authentication (RFC 2617).

        Implements a two-step authentication flow:
        1. First REGISTER without credentials: respond with 401 + WWW-Authenticate challenge
        2. Second REGISTER with Authorization header: verify credentials and register

        Args:
            message: SIPMessage object.
            addr: Source address tuple.
        """
        self.logger.info(f"REGISTER request from {addr}")

        from_header = message.get_header("From")
        user_agent = message.get_header("User-Agent")
        contact = message.get_header("Contact")
        authorization = message.get_header("Authorization")

        # Validate mandatory headers per RFC 3261 Section 10.2
        if not from_header:
            self._send_response(400, "Bad Request", message, addr)
            return

        if not self.pbx_core:
            self._send_response(503, "Service Unavailable", message, addr)
            return

        # Check if this request contains authorization credentials
        if authorization:
            # Verify digest authentication credentials
            if self._verify_digest_auth(authorization, from_header, "REGISTER"):
                try:
                    expires_value = int(message.get_header("Expires") or "3600")
                except (ValueError, TypeError):
                    expires_value = 3600
                success = self.pbx_core.register_extension(
                    from_header, addr, user_agent, contact, expires=expires_value
                )
                if success:
                    response = SIPMessageBuilder.build_response(200, "OK", message)
                    # Set Expires header from request or default
                    response.set_header("Expires", str(expires_value))
                    if contact:
                        response.set_header("Contact", contact)
                    self._add_via_nat_params(response, addr)
                    self._send_message(response.build(), addr)
                else:
                    self._send_response(403, "Forbidden", message, addr)
            else:
                # Credentials invalid - send new challenge
                self._send_auth_challenge(message, addr)
        else:
            # No credentials provided - check if auth is required
            auth_required = True
            if self.pbx_core is not None:
                auth_required = self.pbx_core.config.get("security.sip_auth_required", True)

            if auth_required:
                self._send_auth_challenge(message, addr)
            else:
                # Auth not required - register directly
                try:
                    expires_value = int(message.get_header("Expires") or "3600")
                except (ValueError, TypeError):
                    expires_value = 3600
                success = self.pbx_core.register_extension(
                    from_header,
                    addr,
                    user_agent,
                    contact,
                    expires=expires_value,
                )
                if success:
                    self._send_response(200, "OK", message, addr)
                else:
                    self._send_response(401, "Unauthorized", message, addr)

    def _send_auth_challenge(self, message: SIPMessage, addr: AddrTuple) -> None:
        """
        Send 401 Unauthorized with WWW-Authenticate challenge.

        Args:
            message: Original REGISTER request.
            addr: Source address tuple.
        """
        import hashlib
        import time

        # Generate nonce from timestamp and server secret
        timestamp = str(time.time())
        server_secret = "pbx-sip-auth"
        if self.pbx_core:
            server_secret = self.pbx_core.config.get("security.sip_auth_secret", server_secret)
        nonce_input = f"{timestamp}:{server_secret}"
        nonce = hashlib.md5(nonce_input.encode()).hexdigest()  # nosec B324 - MD5 required by SIP digest auth RFC 2617

        realm = "warden-pbx"
        if self.pbx_core:
            realm = self.pbx_core.config.get("server.sip_realm", realm)

        response = SIPMessageBuilder.build_response(401, "Unauthorized", message)
        response.set_header(
            "WWW-Authenticate",
            f'Digest realm="{realm}", nonce="{nonce}", algorithm=MD5, qop="auth"',
        )
        self._add_via_nat_params(response, addr)
        self._send_message(response.build(), addr)

    def _verify_digest_auth(self, authorization: str, from_header: str, method: str) -> bool:
        """
        Verify SIP digest authentication credentials.

        Args:
            authorization: Authorization header value.
            from_header: From header to extract extension.
            method: SIP method (REGISTER, INVITE, etc.).

        Returns:
            True if credentials are valid.
        """
        import hashlib
        import re

        if not self.pbx_core:
            return False

        # Parse digest parameters from Authorization header
        params: dict[str, str] = {}
        for match in re.finditer(r'(\w+)="([^"]*)"', authorization):
            params[match.group(1)] = match.group(2)
        # Also handle unquoted values (like algorithm=MD5)
        for match in re.finditer(r"(\w+)=([^,\s\"]+)", authorization):
            key = match.group(1)
            if key not in params:
                params[key] = match.group(2)

        username = params.get("username", "")
        realm = params.get("realm", "")
        nonce = params.get("nonce", "")
        uri = params.get("uri", "")
        response_hash = params.get("response", "")
        qop = params.get("qop")
        nc = params.get("nc", "")
        cnonce = params.get("cnonce", "")

        if not all([username, realm, nonce, uri, response_hash]):
            self.logger.warning("Incomplete digest auth parameters")
            return False

        # Look up the password for this extension
        password = None

        # Check database first
        if self.pbx_core.extension_db:
            ext_data = self.pbx_core.extension_db.get(username)
            if ext_data:
                # For digest auth we need the plaintext password or pre-computed HA1
                # If we have a stored HA1, use it directly
                password = ext_data.get("sip_password") or ext_data.get("password")

        # Fall back to config
        if not password:
            ext_config = self.pbx_core.config.get_extension(username)
            if ext_config:
                password = ext_config.get("sip_password") or ext_config.get("password")

        # If still no password found, use default format (must match provisioning system)
        if not password:
            password = f"ext{username}"
            self.logger.debug(
                f"Using default SIP password for extension {username}. "
                f"Recommend setting explicit sip_password in database."
            )

        # Compute expected digest response (RFC 2617)
        ha1 = hashlib.md5(  # nosec B324 - MD5 required by SIP digest auth RFC 2617
            f"{username}:{realm}:{password}".encode()
        ).hexdigest()
        ha2 = hashlib.md5(  # nosec B324
            f"{method}:{uri}".encode()
        ).hexdigest()

        if qop == "auth":
            expected = hashlib.md5(  # nosec B324
                f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()
            ).hexdigest()
        else:
            expected = hashlib.md5(  # nosec B324
                f"{ha1}:{nonce}:{ha2}".encode()
            ).hexdigest()

        if response_hash == expected:
            self.logger.debug(f"Digest auth verified for {username}")
            return True

        self.logger.warning(f"Digest auth failed for {username}")
        return False

    def register_trunk(self, trunk: Any) -> None:
        """
        Send a SIP REGISTER to an upstream trunk provider (PBX acting as UAC).

        Sends an initial unauthenticated REGISTER and tracks it in
        ``_pending_trunk_registrations`` keyed by Call-ID. The provider's
        response is handled asynchronously by ``_handle_response`` ->
        ``_handle_trunk_register_response``, which retries with digest
        credentials on a 401/407 challenge and updates ``trunk.status`` once
        the exchange resolves (RFC 3261/RFC 2617 client-side flow).

        Args:
            trunk: SIPTrunk to register.
        """
        import uuid

        call_id = str(uuid.uuid4())
        cseq = 1
        message = self._build_trunk_register(trunk, call_id, cseq)

        self._pending_trunk_registrations[call_id] = {
            "trunk": trunk,
            "cseq": cseq,
            "retried": False,
        }

        self.logger.info(f"Sending REGISTER for trunk {trunk.name} to {trunk.host}:{trunk.port}")
        self._send_message(message.build(), (trunk.host, trunk.port))

    def _build_trunk_register(
        self,
        trunk: Any,
        call_id: str,
        cseq: int,
        authorization: str | None = None,
    ) -> SIPMessage:
        """Build a REGISTER request addressed to a trunk provider, optionally with an Authorization header for a digest-auth retry."""
        server_ip = self.pbx_core._get_server_ip() if self.pbx_core else self.host
        sip_port = self.pbx_core.config.get("server.sip_port", 5060) if self.pbx_core else self.port

        from_to_addr = f"<sip:{trunk.username}@{trunk.host}>"
        message = SIPMessageBuilder.build_request(
            method="REGISTER",
            uri=f"sip:{trunk.host}:{trunk.port}",
            from_addr=from_to_addr,
            to_addr=from_to_addr,
            call_id=call_id,
            cseq=cseq,
        )
        message.set_header("Contact", f"<sip:{trunk.username}@{server_ip}:{sip_port}>")
        message.set_header("Expires", "3600")
        message.set_header("Max-Forwards", "70")
        if authorization:
            message.set_header("Authorization", authorization)
        return message

    def _handle_trunk_register_response(
        self, message: SIPMessage, addr: AddrTuple, call_id: str
    ) -> None:
        """
        Process a response to an outbound trunk REGISTER.

        Handles both the initial REGISTER (sent from ``register_trunk``) and
        periodic refreshes (sent by ``SIPTrunkSystem._perform_reregistration_checks``
        via the same ``register_trunk`` path) identically -- a refresh is just
        another independent REGISTER transaction with a fresh Call-ID.

        On 401/407, builds a digest Authorization header from the trunk's
        credentials and resends with an incremented CSeq (one retry only).
        On 200, marks the trunk REGISTERED/HEALTHY and schedules the next
        refresh via ``trunk.mark_registered()``. Any other outcome (or a
        second auth failure) marks the trunk FAILED and bumps
        ``registration_failures``.
        """
        from pbx.features.sip_trunk import TrunkHealthStatus, TrunkStatus

        entry = self._pending_trunk_registrations.get(call_id)
        if not entry:
            return

        trunk = entry["trunk"]

        if message.status_code in (401, 407) and not entry["retried"]:
            challenge_header = (
                "WWW-Authenticate" if message.status_code == 401 else ("Proxy-Authenticate")
            )
            challenge = message.get_header(challenge_header)
            if not challenge:
                self.logger.error(
                    f"Trunk {trunk.name} REGISTER challenge missing {challenge_header}"
                )
                trunk.status = TrunkStatus.FAILED
                trunk.registration_failures += 1
                del self._pending_trunk_registrations[call_id]
                return

            params = self._parse_www_authenticate(challenge)
            uri = f"sip:{trunk.host}:{trunk.port}"
            response_hash, extra_params = self._compute_trunk_digest_response(trunk, params, uri)

            auth_parts = [
                f'username="{trunk.username}"',
                f'realm="{params.get("realm", "")}"',
                f'nonce="{params.get("nonce", "")}"',
                f'uri="{uri}"',
                f'response="{response_hash}"',
                "algorithm=MD5",
            ]
            if params.get("qop"):
                auth_parts.append(f"qop={extra_params['qop']}")
                auth_parts.append(f"nc={extra_params['nc']}")
                auth_parts.append(f'cnonce="{extra_params["cnonce"]}"')
            authorization = "Digest " + ", ".join(auth_parts)

            new_cseq = entry["cseq"] + 1
            retry_message = self._build_trunk_register(trunk, call_id, new_cseq, authorization)
            entry["cseq"] = new_cseq
            entry["retried"] = True

            self.logger.info(f"Retrying REGISTER for trunk {trunk.name} with credentials")
            self._send_message(retry_message.build(), addr)
            return

        if message.status_code == 200:
            # The provider may grant a shorter (or longer) lease than the
            # 3600s requested, so schedule the refresh off what was actually
            # granted rather than assuming the request was honored as-is.
            expires_value = int(message.get_header("Expires") or "3600")
            trunk.mark_registered(expires_seconds=expires_value)
            self.logger.info(
                f"Trunk {trunk.name} registered successfully (expires in {expires_value}s)"
            )
        else:
            trunk.status = TrunkStatus.FAILED
            trunk.health_status = TrunkHealthStatus.DOWN
            trunk.registration_failures += 1
            self.logger.error(
                f"Trunk {trunk.name} registration failed: {message.status_code} {message.status_text}"
            )

        del self._pending_trunk_registrations[call_id]

    def _parse_www_authenticate(self, header_value: str) -> dict[str, str]:
        """Parse a WWW-Authenticate/Proxy-Authenticate Digest challenge header into its component parameters."""
        import re

        params: dict[str, str] = {}
        for match in re.finditer(r'(\w+)="([^"]*)"', header_value):
            params[match.group(1)] = match.group(2)
        for match in re.finditer(r"(\w+)=([^,\s\"]+)", header_value):
            key = match.group(1)
            if key not in params:
                params[key] = match.group(2)
        return params

    def _compute_trunk_digest_response(
        self,
        trunk: Any,
        challenge_params: dict[str, str],
        uri: str,
        method: str = "REGISTER",
    ) -> tuple[str, dict[str, str]]:
        """
        Compute the RFC 2617 digest "response" value for a trunk request using
        the trunk's username/password against the provider's challenge.

        Args:
            trunk: SIPTrunk whose credentials to authenticate with.
            challenge_params: Parsed WWW-Authenticate/Proxy-Authenticate params.
            uri: The Request-URI being authenticated (must match the request
                this digest is for -- REGISTER's or the INVITE's).
            method: The SIP method being authenticated. HA2 = MD5(method:uri)
                per RFC 2617, so this must match the request's actual method
                (e.g. "INVITE" when re-challenging an outbound trunk INVITE,
                not "REGISTER").

        Returns:
            Tuple of (response_hash, extra_params) where extra_params contains
            qop/nc/cnonce when the challenge requested qop="auth".
        """
        import hashlib
        import secrets

        realm = challenge_params.get("realm", "")
        nonce = challenge_params.get("nonce", "")
        qop = challenge_params.get("qop")

        ha1 = hashlib.md5(  # nosec B324 - MD5 required by SIP digest auth RFC 2617
            f"{trunk.username}:{realm}:{trunk.password}".encode()
        ).hexdigest()
        ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()  # nosec B324

        extra_params: dict[str, str] = {}
        if qop:
            nc = "00000001"
            cnonce = secrets.token_hex(8)
            response_hash = hashlib.md5(  # nosec B324
                f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()
            ).hexdigest()
            extra_params = {"qop": qop, "nc": nc, "cnonce": cnonce}
        else:
            response_hash = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()  # nosec B324

        return response_hash, extra_params

    def _retry_trunk_invite_with_auth(
        self, call: Any, challenge: SIPMessage, addr: AddrTuple, call_id: str
    ) -> None:
        """
        Retry an outbound trunk INVITE with digest credentials after a 401/407.

        Mirrors ``_handle_trunk_register_response``'s REGISTER-side retry
        (one retry only, tracked via ``call.invite_auth_retried``), but reuses
        ``call.callee_invite`` -- the already-built INVITE from
        ``CallRouter._route_to_trunk`` -- rather than rebuilding a request
        from scratch, so the SDP body and other headers can't drift from the
        original offer. Per RFC 3261 Section 17.1.1.3, first ACKs the
        challenge to properly close out that transaction, then sends the
        credentialed retry as a new transaction (new branch and CSeq) on the
        same Call-ID/dialog attempt.

        Args:
            call: The Call object for this trunk call.
            challenge: The 401/407 SIPMessage received from the trunk.
            addr: The trunk's address the challenge arrived from.
            call_id: Call identifier.
        """
        import uuid as _uuid

        from pbx.sip.transaction import InviteClientTransaction

        if self.pbx_core is None:
            return

        trunk = call.trunk
        callee_invite = getattr(call, "callee_invite", None)
        if not trunk or not callee_invite:
            return

        # RFC 3261 13.2.2.4 / 17.1.1.3: a non-2xx final response must be ACKed
        # to close out the challenged transaction, same construction as the
        # 200 OK ACK (From/To-with-tag/Call-ID/CSeq echoed from the response).
        self._send_ack_to_callee(challenge, addr, call_id)

        challenge_header = (
            "WWW-Authenticate" if challenge.status_code == 401 else "Proxy-Authenticate"
        )
        auth_header = "Authorization" if challenge.status_code == 401 else "Proxy-Authorization"
        challenge_value = challenge.get_header(challenge_header)
        if not challenge_value:
            self.logger.error(
                f"Trunk {trunk.name} INVITE challenge missing {challenge_header} for call {call_id}"
            )
            return

        params = self._parse_www_authenticate(challenge_value)
        uri = callee_invite.uri
        response_hash, extra_params = self._compute_trunk_digest_response(
            trunk, params, uri, method="INVITE"
        )

        auth_parts = [
            f'username="{trunk.username}"',
            f'realm="{params.get("realm", "")}"',
            f'nonce="{params.get("nonce", "")}"',
            f'uri="{uri}"',
            f'response="{response_hash}"',
            "algorithm=MD5",
        ]
        if params.get("qop"):
            auth_parts.append(f"qop={extra_params['qop']}")
            auth_parts.append(f"nc={extra_params['nc']}")
            auth_parts.append(f'cnonce="{extra_params["cnonce"]}"')
        authorization = "Digest " + ", ".join(auth_parts)

        new_cseq = self._parse_cseq_number(callee_invite.get_header("CSeq")) + 1
        callee_invite.set_header("CSeq", f"{new_cseq} INVITE")
        callee_invite.set_header(auth_header, authorization)

        server_ip = self.pbx_core._get_server_ip()
        sip_port = self.pbx_core.config.get("server.sip_port", 5060)
        branch_id = str(_uuid.uuid4()).replace("-", "")
        callee_invite.set_header(
            "Via", f"SIP/2.0/UDP {server_ip}:{sip_port};branch=z9hG4bK{branch_id}"
        )

        call.invite_auth_retried = True

        pbx_core = self.pbx_core
        invite_txn = InviteClientTransaction(
            message=callee_invite.build(),
            dest_addr=call.callee_addr,
            send_fn=self._send_message,
            on_timeout=lambda: pbx_core.call_router._handle_invite_timeout(call_id),
        )
        invite_txn.start()
        call.invite_transaction = invite_txn

        self.logger.info(f"Retrying trunk INVITE for call {call_id} with credentials")

    def _handle_invite(self, message: SIPMessage, addr: AddrTuple) -> None:
        """
        Handle INVITE request.

        Distinguishes between initial INVITEs (new calls) and re-INVITEs
        (mid-call session modifications like codec change, hold/resume, or
        media update).  Re-INVITEs share the same Call-ID as an existing
        active call and must NOT be routed as new calls.

        Args:
            message: SIPMessage object.
            addr: Source address tuple.
        """
        self.logger.info(f"INVITE request from {addr}")

        if self.pbx_core:
            # Extract call information
            from_header = message.get_header("From")
            to_header = message.get_header("To")
            call_id = message.get_header("Call-ID")

            # Validate mandatory headers per RFC 3261 Section 8.1.1
            if not from_header or not to_header or not call_id:
                self._send_response(400, "Bad Request", message, addr)
                return

            # Send 100 Trying immediately per RFC 3261 Section 8.2.6.1
            # to suppress INVITE retransmissions before doing any routing work
            self._send_response(100, "Trying", message, addr)

            # Check if this is a re-INVITE for an existing call
            existing_call = self.pbx_core.call_manager.get_call(call_id)
            if existing_call and existing_call.state.value in ("connected", "ringing", "hold"):
                # This is a re-INVITE (mid-call session update)
                self._handle_reinvite(message, addr, existing_call)
                return

            # Route new call through PBX core
            success = self.pbx_core.call_router.route_call(
                from_header, to_header, call_id, message, addr
            )

            if not success:
                self._send_response(404, "Not Found", message, addr)
        else:
            self._send_response(503, "Service Unavailable", message, addr)

    def _handle_reinvite(self, message: SIPMessage, addr: AddrTuple, call: Any) -> None:
        """
        Handle re-INVITE for an existing call (mid-call session modification).

        Re-INVITEs are used for codec renegotiation, hold/resume, and media
        updates.  The PBX updates the RTP relay endpoints and responds with
        a 200 OK containing the current session's SDP.

        Args:
            message: SIPMessage object (re-INVITE).
            addr: Source address tuple.
            call: Existing Call object.
        """
        from pbx.sip.sdp import SDPBuilder, SDPSession

        if self.pbx_core is None:
            return

        call_id = call.call_id
        self.logger.info(f"Re-INVITE for existing call {call_id} from {addr}")

        # Parse the new SDP offer from the re-INVITE
        new_sdp = None
        offered_codecs = None
        if message.body:
            sdp_obj = SDPSession()
            sdp_obj.parse(message.body)
            new_sdp = sdp_obj.get_audio_info()
            if new_sdp:
                offered_codecs = new_sdp.get("formats", None)
                self.logger.info(
                    f"Re-INVITE SDP: {new_sdp['address']}:{new_sdp['port']}, "
                    f"codecs={offered_codecs}"
                )

        # Determine which party sent the re-INVITE and update their endpoint
        is_caller = addr == call.caller_addr

        # Resolve which relay actually carries this call's media. A peer leg
        # left over from an earlier transfer bridge owns no relay of its own
        # -- the live media flows through the bridged relay owner, where this
        # leg's remaining party sits on bridge_peer_side. Hold/resume, MOH,
        # and the answer SDP port must all act on that relay, or a hold on a
        # thrice-transferred call silently plays no music (get_handler(call_id)
        # is None) and resume advertises the released relay's dead port.
        relay_owner = call
        relay_call_id = call_id
        peer_side: str | None = None
        if (
            self.pbx_core.rtp_relay.get_handler(call_id) is None
            and call.bridged_peer_call_id
            and call.bridge_peer_side
        ):
            owner = self.pbx_core.call_manager.get_call(call.bridged_peer_call_id)
            if owner and self.pbx_core.rtp_relay.get_handler(owner.call_id) is not None:
                relay_owner = owner
                relay_call_id = owner.call_id
                peer_side = call.bridge_peer_side
                self.logger.info(f"Re-INVITE on peer leg {call_id}; media relay is {relay_call_id}")

        if new_sdp:
            new_address = new_sdp.get("address")
            new_port = new_sdp.get("port")
            # Skip retargeting on a legacy null hold address (c=0.0.0.0):
            # it isn't a usable RTP destination, and the endpoint the PBX
            # already has on file remains valid for when hold ends.
            if new_address and new_port and new_address != "0.0.0.0":
                if is_caller:
                    call.caller_rtp = new_sdp
                    self.logger.info(
                        f"Updated caller media via re-INVITE: {new_address}:{new_port}"
                    )
                else:
                    call.callee_rtp = new_sdp
                    self.logger.info(
                        f"Updated callee media via re-INVITE: {new_address}:{new_port}"
                    )

                # Update RTP relay endpoints. Skipped for a peer leg: its
                # party's endpoint on the shared relay is unchanged by a
                # hold (sendonly keeps the same address) and is owned by the
                # relay-owner record, not this one.
                if call.rtp_ports and peer_side is None:
                    caller_ep = (
                        (call.caller_rtp["address"], call.caller_rtp["port"])
                        if call.caller_rtp
                        else None
                    )
                    callee_ep = (
                        (call.callee_rtp["address"], call.callee_rtp["port"])
                        if call.callee_rtp
                        else None
                    )
                    self.pbx_core.rtp_relay.set_endpoints(call_id, caller_ep, callee_ep)

        # Detect hold/resume from the offered media direction (RFC 3264):
        # a=sendonly or a=inactive means the sender is placing the call on
        # hold; a=sendrecv (or a body-less re-INVITE) means normal media.
        # A legacy c=0.0.0.0 connection address is also treated as hold.
        sender_leg = "caller" if is_caller else "callee"
        if peer_side is not None:
            # Holder sits on peer_side of the shared relay; the other side
            # (the transferee) hears MOH.
            held_side = "a" if peer_side == "b" else "b"
        else:
            held_side = "b" if is_caller else "a"  # the *other* leg hears MOH
        direction = new_sdp.get("direction", "sendrecv") if new_sdp else "sendrecv"
        is_hold_signal = new_sdp is not None and (
            direction in ("sendonly", "inactive") or new_sdp.get("address") == "0.0.0.0"
        )
        was_on_hold = call.state.value == "hold"

        answer_direction = "sendrecv"
        skip_forward = False

        if is_hold_signal:
            call.hold(held_by=sender_leg)
            relay_handler = self.pbx_core.rtp_relay.get_handler(relay_call_id)
            if relay_handler:
                self.pbx_core.moh_system.start_moh(relay_call_id, relay_handler, held_side)
            answer_direction = "recvonly"
            skip_forward = True
            self.logger.info(f"Call {call_id} placed on hold by {sender_leg} (a={direction})")
        elif new_sdp and was_on_hold and call.held_by == sender_leg:
            call.resume()
            self.pbx_core.moh_system.stop_moh(relay_call_id)
            skip_forward = True
            self.logger.info(f"Call {call_id} resumed by {sender_leg} (a={direction})")

        # Build answer SDP with codecs compatible with the offered set.
        # The advertised port is the relay owner's (which differs from this
        # record's own released port on a peer leg), so the holder resumes
        # sending to the live relay.
        server_ip = self.pbx_core._get_server_ip()
        rtp_port = relay_owner.rtp_ports[0] if relay_owner.rtp_ports else 10000

        # Detect phone model for codec filtering
        ext_number = call.from_extension if is_caller else call.to_extension
        user_agent = self.pbx_core._get_phone_user_agent(ext_number)
        phone_model = self.pbx_core._detect_phone_model(user_agent)
        answer_codecs = self.pbx_core._get_compatible_codecs(phone_model, offered_codecs)

        dtmf_pt = self.pbx_core._get_dtmf_payload_type()
        ilbc_mode = self.pbx_core._get_ilbc_mode()

        # Preserve media protocol, SRTP crypto, and rtpmap names from the offer.
        # Mirroring the caller's rtpmap names is essential for Zultys ZIP phones.
        reinvite_protocol = new_sdp.get("protocol", "RTP/AVP") if new_sdp else "RTP/AVP"
        reinvite_crypto = (new_sdp.get("crypto") or None) if new_sdp else None
        reinvite_rtpmap = (new_sdp.get("rtpmap_names") or None) if new_sdp else None

        answer_sdp = SDPBuilder.build_audio_sdp(
            server_ip,
            rtp_port,
            session_id=call_id,
            codecs=answer_codecs,
            dtmf_payload_type=dtmf_pt,
            ilbc_mode=ilbc_mode,
            protocol=reinvite_protocol,
            crypto=reinvite_crypto,
            rtpmap_overrides=reinvite_rtpmap,
            direction=answer_direction,
        )

        response = SIPMessageBuilder.build_response(200, "OK", message, body=answer_sdp)
        response.set_header("Content-type", "application/sdp")

        # Add Contact header
        sip_port = self.pbx_core.config.get("server.sip_port", 5060)
        contact_uri = f"<sip:{call.to_extension}@{server_ip}:{sip_port}>"
        response.set_header("Contact", contact_uri)

        self._send_message(response.build(), addr)
        self.logger.info(f"Answered re-INVITE for call {call_id} with codecs {answer_codecs}")

        # Forward the re-INVITE to the other party so they can update too.
        # Skipped for hold/resume: the PBX absorbs those locally (swapping
        # in MOH or resuming the relay) without changing what the far end's
        # own session looks like.
        other_addr = call.callee_addr if is_caller else call.caller_addr
        if other_addr and new_sdp and not skip_forward:
            # Use the other party's rtpmap names (from their original SDP offer)
            other_rtp = call.callee_rtp if is_caller else call.caller_rtp
            other_rtpmap = (other_rtp.get("rtpmap_names") or None) if other_rtp else None

            # Build a re-INVITE to the other party with the PBX's RTP endpoint
            reinvite_sdp = SDPBuilder.build_audio_sdp(
                server_ip,
                rtp_port,
                session_id=call_id,
                codecs=answer_codecs,
                dtmf_payload_type=dtmf_pt,
                ilbc_mode=ilbc_mode,
                protocol=reinvite_protocol,
                crypto=reinvite_crypto,
                rtpmap_overrides=other_rtpmap,
            )
            other_ext = call.to_extension if is_caller else call.from_extension
            reinvite = SIPMessageBuilder.build_request(
                method="INVITE",
                uri=f"sip:{other_ext}@{other_addr[0]}:{other_addr[1]}",
                from_addr=message.get_header("From") or "",
                to_addr=message.get_header("To") or "",
                call_id=call_id,
                cseq=self._parse_cseq_number(message.get_header("CSeq")) + 1,
                body=reinvite_sdp,
            )
            import uuid as _uuid

            branch_id = str(_uuid.uuid4()).replace("-", "")
            reinvite.set_header(
                "Via",
                f"SIP/2.0/UDP {server_ip}:{sip_port};branch=z9hG4bK{branch_id}",
            )
            reinvite.set_header("Contact", contact_uri)
            reinvite.set_header("Content-type", "application/sdp")
            self._send_message(reinvite.build(), other_addr)
            self.logger.info(f"Forwarded re-INVITE to other party at {other_addr}")

    def _send_ack_to_callee(
        self,
        response_200: SIPMessage,
        callee_addr: AddrTuple,
        call_id: str,
        use_invite_branch: bool = False,
    ) -> None:
        """
        Generate and send ACK to callee after receiving their final response.

        Per RFC 3261 Section 13.2.2.4, the UAC MUST acknowledge a 200 OK
        to INVITE with an ACK request.  Without this, the callee will
        retransmit the 200 OK and eventually tear down the call (BYE after
        Timer H expires), causing "call drops after a few seconds".

        Args:
            response_200: The final response SIPMessage from the callee
                (200 OK, or an error response when acknowledging a non-2xx).
            callee_addr: Callee's address tuple.
            call_id: Call identifier.
            use_invite_branch: Reuse the INVITE's Via branch instead of
                generating a new one.  RFC 3261 Section 17.1.1.3: the ACK
                for a non-2xx final response belongs to the same transaction
                as the INVITE and MUST have the same branch, or the callee
                cannot match it and keeps retransmitting the response.
                A 2xx ACK is a new transaction and gets a new branch.
        """
        import uuid as _uuid

        if self.pbx_core is None:
            return

        # Deliberately tolerates a missing call record. Acknowledging a final response is a
        # transaction-layer obligation (RFC 3261 SS17.1.1.3), not an application one: a 487
        # answering our own CANCEL routinely arrives after the call it belonged to has been
        # torn down, and returning early there leaves the far end retransmitting until
        # Timer H expires 32 seconds later -- with its INVITE transaction still live, which
        # is enough to make an endpoint answer the next call 486 Busy.
        #
        # Everything an ACK needs is in the response itself. The call record only supplies
        # nicer values, so its absence degrades the ACK rather than skipping it.
        call = self.pbx_core.call_manager.get_call(call_id)

        # Build ACK for the callee's final response.
        # The Request-URI, From, To (with tag), and Call-ID must match the
        # original INVITE dialog.  CSeq uses the same number as the INVITE.
        callee_invite = getattr(call, "callee_invite", None) if call else None
        if callee_invite:
            request_uri = callee_invite.uri
        elif call:
            request_uri = f"sip:{call.to_extension}@{callee_addr[0]}:{callee_addr[1]}"
        else:
            # No call to name the user part, so address the host itself. A UAS matches the
            # ACK on branch, To-tag, Call-ID and CSeq -- never the Request-URI.
            request_uri = f"sip:{callee_addr[0]}:{callee_addr[1]}"
        from_header = response_200.get_header("From") or ""
        # To header from the 200 OK includes the remote tag
        to_header = response_200.get_header("To") or ""
        cseq_num = self._parse_cseq_number(response_200.get_header("CSeq"))

        ack = SIPMessage()
        ack.method = "ACK"
        ack.uri = request_uri
        ack.set_header("From", from_header)
        ack.set_header("To", to_header)
        ack.set_header("Call-ID", call_id)
        ack.set_header("CSeq", f"{cseq_num} ACK")
        ack.set_header("Content-Length", "0")
        ack.set_header("Max-Forwards", "70")

        # Via header for the ACK
        invite_via = (
            callee_invite.get_header("Via") if use_invite_branch and callee_invite else None
        )
        if not invite_via and use_invite_branch:
            # The response carries our own Via straight back, branch included, so a non-2xx
            # ACK can be matched to its INVITE transaction even with no stored INVITE to
            # copy it from. Without this a 487 arriving after teardown would get an ACK on a
            # fresh branch, which the far end cannot match and therefore ignores.
            invite_via = response_200.get_header("Via")
        if invite_via:
            # Non-2xx ACK: same transaction as the INVITE, same branch
            ack.set_header("Via", invite_via)
        else:
            server_ip = self.pbx_core._get_server_ip()
            sip_port = self.pbx_core.config.get("server.sip_port", 5060)
            branch_id = str(_uuid.uuid4()).replace("-", "")
            ack.set_header(
                "Via",
                f"SIP/2.0/UDP {server_ip}:{sip_port};branch=z9hG4bK{branch_id}",
            )

        self._send_message(ack.build(), callee_addr)
        self.logger.info(f"Sent ACK to callee at {callee_addr} for call {call_id}")

    def _handle_ack(self, message: SIPMessage, addr: AddrTuple) -> None:
        """
        Handle ACK request.

        Args:
            message: SIPMessage object.
            addr: Source address tuple.
        """
        self.logger.debug(f"ACK request from {addr}")

        # Forward ACK to complete the three-way handshake
        if self.pbx_core:
            call_id = message.get_header("Call-ID")
            if call_id:
                call = self.pbx_core.call_manager.get_call(call_id)
                if call and call.callee_addr:
                    # Forward ACK to callee
                    self._send_message(message.build(), call.callee_addr)
                    self.logger.debug(f"Forwarded ACK to callee for call {call_id}")

        # ACK is not responded to

    def _handle_bye(self, message: SIPMessage, addr: AddrTuple) -> None:
        """
        Handle BYE request.

        Args:
            message: SIPMessage object.
            addr: Source address tuple.
        """
        call_id = message.get_header("Call-ID")
        self.logger.info("")
        self.logger.info(">>> BYE REQUEST RECEIVED <<<")
        self.logger.info(f"  Call ID: {call_id}")
        self.logger.info(f"  From: {addr}")

        if not call_id:
            return

        if self.pbx_core:
            call = self.pbx_core.call_manager.get_call(call_id)

            # A transfer in progress owns its own legs: the session decides
            # whether this BYE ends the call, is absorbed so a parked party
            # survives until the bridge completes, or aborts the transfer.
            # Checked before the bridged branch because a second transfer
            # chained off an already-bridged call sets both.
            if call and call.transfer_session_id:
                from pbx.core.transfer_session import LegEvent, LegEventResult

                result = self.pbx_core.transfer_handler.on_leg_event(call, LegEvent.BYE, addr=addr)
                if result is not LegEventResult.FORWARD:
                    self.logger.info(
                        f"  BYE on {call_id} from {addr} handled by transfer "
                        f"session ({result.value})"
                    )
                    self._send_response(200, "OK", message, addr)
                    self.logger.info("")
                    return

            # A queued call: the queue absorbs the departed transferor's BYE
            # and handles caller abandon itself. Falls through (False) during
            # overflow voicemail so the normal end_call path auto-saves the
            # recording. Checked after the transfer-session branch so an
            # in-flight agent offer's BYE is resolved by the session first.
            if (
                call
                and getattr(call, "queue_ctx", None)
                and self.pbx_core.queue_handler.on_bye(call, addr)
            ):
                self._send_response(200, "OK", message, addr)
                self.logger.info(f"  BYE on {call_id} from {addr} handled by queue")
                self.logger.info("")
                return

            # Bridged call: each leg keeps its own record with its own dialog
            # identity, so the BYE cannot be forwarded across the bridge --
            # the peer is ended on its own dialog instead and both records go
            # away. A BYE from an address matching neither party is the
            # departed transferor's stale leg: acknowledge it and leave the
            # bridge alone.
            if call and call.bridged_peer_call_id:
                self._send_response(200, "OK", message, addr)
                if addr not in (call.caller_addr, call.callee_addr):
                    self.logger.info(f"  Absorbed stale BYE from {addr} on {call.call_id}")
                    self.logger.info("")
                    return
                peer_call_id = call.bridged_peer_call_id
                self.end_bridged_peer(call)
                self.pbx_core.end_call(call.call_id)
                self.logger.info(f"  Bridged call ended: {call.call_id} (peer {peer_call_id})")
                self.logger.info("")
                return

            # Forward BYE to the other party in the call if present
            if call:
                self.logger.info(
                    f"  Call type: {'Voicemail Access' if hasattr(call, 'voicemail_access') and call.voicemail_access else 'Regular Call'}"
                )
                self.logger.info(f"  Call State: {call.state}")
                if hasattr(call, "voicemail_extension"):
                    self.logger.info(f"  Voicemail Extension: {call.voicemail_extension}")
                # Determine which party sent BYE and end the other one.
                #
                # The BYE is re-originated on the surviving party's own dialog
                # rather than relayed as-is. The PBX is a B2BUA: each side
                # negotiated its own To/From tags, and on a call that was
                # re-targeted -- a Find Me/Follow Me destination, a 3xx
                # forward -- the two sides do not even share a To URI. A
                # relayed BYE therefore names a dialog the receiving phone
                # never had, so a strict UA answers 481 and holds its leg open
                # while the PBX has already torn the call down. Same reason
                # transfer teardown has always used _send_leg_bye().
                if call.caller_addr and call.caller_addr == addr:
                    # Caller hung up, end the callee -- unless the call was
                    # routed to voicemail, in which case the callee leg was
                    # already cancelled and has no dialog for this Call-ID.
                    if not call.routed_to_voicemail:
                        self.logger.debug("BYE from caller, ending the callee leg")
                        self._send_leg_bye(call, side="callee")
                elif call.callee_addr and call.callee_addr == addr:
                    self.logger.debug("BYE from callee, ending the caller leg")
                    self._send_leg_bye(call, side="caller")

            # End the call internally
            self.logger.info(f"  Processing BYE - ending call {call_id}")
            self.pbx_core.end_call(call_id)
            self.logger.info(f"  Call {call_id} ended")
        else:
            self.logger.info(f"  Call {call_id} not found in call manager")

        self._send_response(200, "OK", message, addr)
        self.logger.info(f"  Sent 200 OK response to {addr}")
        self.logger.info("")

    def _record_peer_into_mailbox(self, call: Any) -> bool:
        """
        Offer the mailbox to the party waiting on `call`'s bridged peer.

        A leg the PBX placed has no caller on its own record -- click-to-dial
        rings you on one Call and the person you dialled on another -- so when
        the dialled party rejects, the one who should hear the mailbox is on
        the peer record, already answered and already on the relay. That is
        exactly the shape ``record_into_mailbox`` exists for, and what the
        queue's overflow and a transfer's no-answer both use it for.

        The mailbox is keyed by the *rejecting* leg's extension, and the
        message is stamped with that leg's From identity: the peer's own
        ``from_extension`` is the synthetic origination context ("originator"),
        which would arrive as a voicemail from nobody.

        Returns:
            True if the peer is being recorded, so the caller should stop
            tearing the call down; False to fall back to hanging up.
        """
        from pbx.core.call import CallState

        if self.pbx_core is None:
            return False
        peer = self.pbx_core.call_manager.get_call(call.bridged_peer_call_id)
        if peer is None or peer.state != CallState.CONNECTED:
            return False

        # The pair is dissolved: this leg is ending, and the peer is being
        # handed to the mailbox rather than to it.
        peer.bridged_peer_call_id = None
        call.bridged_peer_call_id = None
        self.pbx_core.moh_system.stop_moh(peer.call_id)
        peer.from_extension = call.from_extension

        return bool(
            self.pbx_core.voicemail_handler.record_into_mailbox(
                peer,
                peer.call_id,
                call.to_extension,
                peer.callee_rtp,
                peer.rtp_ports,
                hangup_cause="rejected",
            )
        )

    def end_bridged_peer(self, call: Any) -> None:
        """
        End the leg bridged to `call`, if there is one, on its own dialog.

        A peer that has answered gets a BYE; one that is still ringing gets a
        CANCEL, since its INVITE has no final response yet and it would answer
        a BYE with 481. That second case is what a PBX-placed pair spends its
        setup in (click-to-dial rings the caller first, then dials the
        destination), so every path that ends one leg of a pair goes through
        here rather than leaving the other ringing into a dead call.

        The cross-link is cleared on both records first, so ending the peer
        cannot bounce the teardown back.
        """
        from pbx.core.call import CallState

        if self.pbx_core is None or not call.bridged_peer_call_id:
            return

        peer = self.pbx_core.call_manager.get_call(call.bridged_peer_call_id)
        call.bridged_peer_call_id = None
        if not peer:
            return
        peer.bridged_peer_call_id = None

        if peer.state == CallState.CONNECTED:
            self._send_leg_bye(peer)
        else:
            self.cancel_leg(peer)
            self.logger.info(f"Cancelled still-ringing bridged leg {peer.call_id}")
        self.pbx_core.end_call(peer.call_id)

    def cancel_leg(
        self,
        call: Any,
        invite: Any | None = None,
        addr: AddrTuple | None = None,
        *,
        answered_elsewhere: bool = False,
    ) -> None:
        """
        CANCEL the INVITE this call has outstanding toward its callee, so the
        destination stops ringing.

        Paired with _send_leg_bye(): both are the in-dialog teardown requests
        the PBX sends on a leg it INVITEd, and which one applies is decided by
        whether that INVITE has had a final response yet. A no-op on a leg
        with no INVITE in flight.

        Args:
            call: The call the leg belongs to (for its Call-ID and logging).
            invite: The INVITE to cancel, and `addr` where it went. Both default
                to the call's own callee leg. A caller ringing several
                destinations at once (Find Me/Follow Me) passes them explicitly,
                since only one of its legs can occupy the call's callee slot.
            addr: Where to send the CANCEL. Paired with `invite`.
            answered_elsewhere: This destination is being cancelled because
                somebody else took the call, not because it was missed. Adds the
                RFC 3326 Reason header that tells the phone so; without it a
                phone that merely stopped ringing logs a missed call, which is
                wrong for every losing leg of a simultaneous ring.
        """
        if self.pbx_core is None:
            return
        if invite is None or addr is None:
            invite, addr = call.callee_invite, call.callee_addr
        if not (addr and invite):
            return
        cancel = SIPMessageBuilder.build_request(
            method="CANCEL",
            uri=invite.uri,
            from_addr=invite.get_header("From"),
            to_addr=invite.get_header("To"),
            call_id=call.call_id,
            # RFC 3261 9.1: a CANCEL reuses the INVITE's CSeq number and Via
            # branch, which is what matches it to the transaction it cancels.
            cseq=self._parse_cseq_number(invite.get_header("CSeq")),
        )
        cancel.set_header("Via", invite.get_header("Via"))
        if answered_elsewhere:
            # RFC 3326. Phones that understand it (Cisco, Polycom, Yealink,
            # Snom, Grandstream and others) drop the call from their missed
            # list rather than logging it, which is what "somebody else picked
            # it up" should look like on a desk that was only ever one of
            # several ringing.
            cancel.set_header("Reason", 'SIP;cause=200;text="Call completed elsewhere"')
        self._send_message(cancel.build(), addr)
        self.logger.info(
            f"Sent CANCEL to {addr} to stop it ringing (call {call.call_id})"
            + (" - answered elsewhere" if answered_elsewhere else "")
        )

    def _send_leg_bye(self, call: Any, side: str | None = None) -> None:
        """
        Send a BYE to a party of a call leg, using the leg's own stored
        dialog identity.

        For the callee side (e.g. the transfer destination), the dialog is
        identified by the original INVITE's From header and the To header
        captured from the callee's 200 OK. For the caller side (e.g. the
        transferee parked by the transferor), the dialog headers are
        swapped, mirroring how in-dialog requests toward the caller are
        built elsewhere (see _send_transfer_notify).

        Args:
            call: Call record whose party should receive a BYE.
            side: Force which side to target ("caller" or "callee"). If
                None (default), auto-selects whichever side is populated,
                callee preferred -- correct for a leg where only one side
                is expected to still have a real address (e.g. a peer leg
                after a bridge, whose other side was already nulled). Pass
                "caller" or "callee" explicitly when both sides are still
                populated and a specific party must be targeted (e.g. the
                transferor's own original leg, ended alongside their
                consultation leg at transfer completion).
        """
        if self.pbx_core is None:
            return

        original_invite = call.original_invite
        callee_invite = getattr(call, "callee_invite", None)

        if call.callee_addr and side != "caller":
            # Callee side
            target_addr = call.callee_addr
            source = callee_invite or original_invite
            from_header = source.get_header("From") if source else ""
            to_header = call.callee_dialog_to or (source.get_header("To") if source else "")
            uri = f"sip:{call.to_extension}@{target_addr[0]}:{target_addr[1]}"
        elif call.caller_addr and original_invite and side != "callee":
            # Caller side -- swap dialog direction. original_invite's own
            # To header predates any response and so carries no tag; the
            # caller's real dialog identity is the tagged To header from
            # the 200 OK the PBX actually sent it.
            target_addr = call.caller_addr
            from_header = call.caller_dialog_to or (original_invite.get_header("To") or "")
            to_header = original_invite.get_header("From") or ""
            uri = f"sip:{call.from_extension}@{target_addr[0]}:{target_addr[1]}"
        else:
            return

        call.pbx_leg_cseq += 1
        bye_msg = SIPMessageBuilder.build_request(
            method="BYE",
            uri=uri,
            from_addr=from_header or "",
            to_addr=to_header or "",
            call_id=call.call_id,
            cseq=call.pbx_leg_cseq,
        )
        self._add_pbx_request_headers(bye_msg, target_addr)
        self._send_message(bye_msg.build(), target_addr)
        self.logger.info(f"Sent leg BYE for call {call.call_id} to {target_addr}")

    def _add_pbx_request_headers(self, message: SIPMessage, target_addr: AddrTuple) -> None:
        """
        Add the mandatory RFC 3261 headers a PBX-originated request needs to
        be accepted as a valid transaction by a real phone.

        SIPMessageBuilder.build_request() only sets From/To/Call-ID/CSeq;
        Via and Max-Forwards are required (RFC 3261 SS8.1.1.6-7) and their
        absence causes strict UAs to silently drop the request rather than
        act on it -- e.g. a BYE that never ends the peer's call timer.

        Args:
            message: The request to finish building (mutated in place).
            target_addr: Destination address, used to pick the local
                Contact identity.
        """
        if self.pbx_core is None:
            return

        import uuid

        server_ip = self.pbx_core._get_server_ip()
        sip_port = self.pbx_core.config.get("server.sip_port", 5060)
        branch_id = str(uuid.uuid4()).replace("-", "")
        message.set_header("Via", f"SIP/2.0/UDP {server_ip}:{sip_port};branch=z9hG4bK{branch_id}")
        message.set_header("Max-Forwards", "70")
        message.set_header("Contact", f"<sip:{server_ip}:{sip_port}>")

    def _handle_cancel(self, message: SIPMessage, addr: AddrTuple) -> None:
        """
        Handle CANCEL request per RFC 3261 Section 9.2.

        Sends 200 OK for the CANCEL itself, then terminates the pending
        INVITE transaction with a 487 Request Terminated response.

        Args:
            message: SIPMessage object.
            addr: Source address tuple.
        """
        self.logger.info(f"CANCEL request from {addr}")

        # Send 200 OK for the CANCEL request itself
        self._send_response(200, "OK", message, addr)

        # Find and terminate the matching call
        call_id = message.get_header("Call-ID")
        if call_id and self.pbx_core:
            call = self.pbx_core.call_manager.get_call(call_id)
            if call:
                # Send 487 Request Terminated for the original INVITE
                if hasattr(call, "original_invite") and call.original_invite:
                    response_487 = SIPMessageBuilder.build_response(
                        487, "Request Terminated", call.original_invite
                    )
                    self._send_message(response_487.build(), addr)

                # Cancel INVITE retransmission
                if hasattr(call, "invite_transaction") and call.invite_transaction:
                    call.invite_transaction.cancel()
                    call.invite_transaction = None

                # Forward CANCEL to callee to stop their phone from ringing
                self.cancel_leg(call)

                # End the call
                self.pbx_core.end_call(call_id)
                self.logger.info(f"Call {call_id} cancelled and terminated")

    def _handle_options(self, message: SIPMessage, addr: AddrTuple) -> None:
        """
        Handle OPTIONS request.

        Args:
            message: SIPMessage object.
            addr: Source address tuple.
        """
        self.logger.debug(f"OPTIONS request from {addr}")
        response = SIPMessageBuilder.build_response(200, "OK", message)
        response.set_header(
            "Allow",
            "INVITE, ACK, BYE, CANCEL, OPTIONS, REGISTER, SUBSCRIBE, NOTIFY, INFO, REFER, MESSAGE, PRACK, UPDATE, PUBLISH",
        )
        self._send_message(response.build(), addr)

    def _handle_subscribe(self, message: SIPMessage, addr: AddrTuple) -> None:
        """
        Handle SUBSCRIBE request for presence/event notifications (RFC 6665).

        Tracks subscriptions and sends initial NOTIFY with current state.
        Supported event packages: presence, dialog, message-summary, refer.

        Args:
            message: SIPMessage object.
            addr: Source address tuple.
        """
        import re
        import time

        self.logger.debug(f"SUBSCRIBE request from {addr}")

        event = message.get_header("Event")
        expires_str = message.get_header("Expires") or "3600"
        from_header = message.get_header("From") or ""
        to_header = message.get_header("To") or ""
        call_id = message.get_header("Call-ID") or ""

        try:
            expires = int(expires_str)
        except ValueError:
            expires = 3600

        if not event:
            self._send_response(489, "Bad Event", message, addr)
            return

        self.logger.info(f"SUBSCRIBE for event: {event}, expires: {expires}")

        # Extract subscriber URI for tracking
        subscriber_uri = from_header
        sub_match = re.search(r"sip:([^@>]+)", from_header)
        if sub_match:
            subscriber_uri = sub_match.group(1)

        # Track the subscription
        sub_key = (subscriber_uri, event)

        if expires == 0:
            # Unsubscribe - remove subscription
            if sub_key in self.subscriptions:
                del self.subscriptions[sub_key]
                self.logger.info(f"Removed subscription: {subscriber_uri} for {event}")
            response = SIPMessageBuilder.build_response(200, "OK", message)
            response.set_header("Expires", "0")
            self._send_message(response.build(), addr)

            # Send final NOTIFY with terminated state
            self._send_event_notify(
                from_header, to_header, call_id, addr, event, "terminated;reason=timeout", ""
            )
            return

        # Extract subscriber's Contact URI for NOTIFY Request-URI (RFC 6665 Section 4.2)
        contact_header = message.get_header("Contact")
        contact_uri = None
        if contact_header:
            contact_match = re.search(r"<([^>]+)>", contact_header)
            if contact_match:
                contact_uri = contact_match.group(1)
            elif contact_header.strip().startswith("sip:"):
                contact_uri = contact_header.strip()

        # Store subscription
        self.subscriptions[sub_key] = {
            "from": from_header,
            "to": to_header,
            "call_id": call_id,
            "addr": addr,
            "event": event,
            "expires": expires,
            "created": time.time(),
            "contact_uri": contact_uri,
        }

        # Accept the subscription
        response = SIPMessageBuilder.build_response(200, "OK", message)
        response.set_header("Expires", str(expires))
        # Add Contact header for dialog establishment
        server_ip = self.host if self.host != "0.0.0.0" else "127.0.0.1"  # nosec B104
        if self.pbx_core:
            server_ip = self.pbx_core._get_server_ip()
        response.set_header("Contact", f"<sip:{server_ip}:{self.port}>")
        self._add_via_nat_params(response, addr)
        self._send_message(response.build(), addr)

        # Send initial NOTIFY with current state
        notify_body = self._get_event_state(event, to_header)
        content_type = self._get_event_content_type(event)

        self._send_event_notify(
            from_header,
            to_header,
            call_id,
            addr,
            event,
            f"active;expires={expires}",
            notify_body,
            content_type,
            contact_uri=contact_uri,
        )

    def _send_event_notify(
        self,
        from_header: str,
        to_header: str,
        call_id: str,
        addr: AddrTuple,
        event: str,
        subscription_state: str,
        body: str,
        content_type: str = "application/pidf+xml",
        contact_uri: str | None = None,
    ) -> None:
        """
        Send a NOTIFY message for an event subscription.

        Args:
            from_header: From header value.
            to_header: To header value.
            call_id: Call-ID for the subscription dialog.
            addr: Address to send NOTIFY to.
            event: Event type.
            subscription_state: Subscription-State header value.
            body: NOTIFY body content.
            content_type: Content-Type for the body.
            contact_uri: Subscriber's Contact URI for Request-URI (RFC 6665).
        """
        import uuid

        # RFC 6665 Section 4.2: Request-URI should be the Contact from SUBSCRIBE
        request_uri = contact_uri or f"sip:{addr[0]}:{addr[1]}"

        notify_msg = SIPMessageBuilder.build_request(
            method="NOTIFY",
            uri=request_uri,
            from_addr=to_header,
            to_addr=from_header,
            call_id=call_id,
            cseq=1,
        )

        # RFC 3261 Section 8.1.1.7: Via header is mandatory on requests
        server_ip = self.host if self.host != "0.0.0.0" else "127.0.0.1"  # nosec B104
        if self.pbx_core:
            server_ip = self.pbx_core._get_server_ip()
        branch = f"z9hG4bK{uuid.uuid4().hex[:12]}"
        notify_msg.set_header("Via", f"SIP/2.0/UDP {server_ip}:{self.port};branch={branch}")

        # RFC 3261 Section 8.1.1.8: Contact header for in-dialog requests
        notify_msg.set_header("Contact", f"<sip:{server_ip}:{self.port}>")

        # RFC 3261 Section 8.1.1.6: Max-Forwards
        notify_msg.set_header("Max-Forwards", "70")

        notify_msg.set_header("Event", event)
        notify_msg.set_header("Subscription-State", subscription_state)

        if body:
            notify_msg.body = body
            notify_msg.set_header("Content-type", content_type)
            notify_msg.set_header("Content-Length", str(len(body.encode("utf-8"))))

        self._send_message(notify_msg.build(), addr)
        self.logger.debug(f"Sent NOTIFY for event {event} to {addr}")

    def _get_event_state(self, event: str, to_header: str) -> str:
        """
        Get current state for an event package.

        Args:
            event: Event type (presence, dialog, message-summary).
            to_header: To header to identify the monitored resource.

        Returns:
            XML/text body representing current state.
        """
        import re

        extension = ""
        ext_match = re.search(r"sip:(\d+)@", to_header or "")
        if ext_match:
            extension = ext_match.group(1)

        if event == "presence":
            # Return PIDF presence document
            status = "open"
            if self.pbx_core and extension and hasattr(self.pbx_core, "presence_system"):
                presence_info = self.pbx_core.presence_system.get_status(extension)
                if presence_info:
                    status = presence_info.get("status", "open")

            server_ip = "127.0.0.1"
            if self.pbx_core:
                server_ip = self.pbx_core._get_server_ip()

            return (
                '<?xml version="1.0" encoding="UTF-8"?>\r\n'
                '<presence xmlns="urn:ietf:params:xml:ns:pidf" '
                f'entity="sip:{extension}@{server_ip}">\r\n'
                "  <tuple>\r\n"
                f"    <status><basic>{status}</basic></status>\r\n"
                "  </tuple>\r\n"
                "</presence>"
            )

        if event == "message-summary":
            # Return message waiting indication (MWI)
            new_msgs = 0
            old_msgs = 0
            if self.pbx_core and extension and hasattr(self.pbx_core, "voicemail_system"):
                mailbox = self.pbx_core.voicemail_system.get_mailbox(extension)
                new_msgs = sum(1 for m in mailbox.messages if not m.get("listened"))
                old_msgs = sum(1 for m in mailbox.messages if m.get("listened"))

            waiting = "yes" if new_msgs > 0 else "no"
            return f"Messages-Waiting: {waiting}\r\nVoice-Message: {new_msgs}/{old_msgs}"

        if event == "dialog":
            # Return dialog info for BLF (busy lamp field)
            dialog_state = "terminated"
            if self.pbx_core and extension:
                calls = self.pbx_core.call_manager.get_extension_calls(extension)
                if calls:
                    dialog_state = "confirmed"

            server_ip = "127.0.0.1"
            if self.pbx_core:
                server_ip = self.pbx_core._get_server_ip()

            return (
                '<?xml version="1.0" encoding="UTF-8"?>\r\n'
                '<dialog-info xmlns="urn:ietf:params:xml:ns:dialog-info" '
                f'version="1" state="full" entity="sip:{extension}@{server_ip}">\r\n'
                f'  <dialog id="1" direction="initiator">\r\n'
                f"    <state>{dialog_state}</state>\r\n"
                "  </dialog>\r\n"
                "</dialog-info>"
            )

        return ""

    def _get_event_content_type(self, event: str) -> str:
        """
        Get the Content-Type for an event package.

        Args:
            event: Event type.

        Returns:
            Content-Type string.
        """
        content_types = {
            "presence": "application/pidf+xml",
            "dialog": "application/dialog-info+xml",
            "message-summary": "application/simple-message-summary",
            "refer": "message/sipfrag;version=2.0",
        }
        return content_types.get(event, "text/plain")

    def _handle_notify(self, message: SIPMessage, addr: AddrTuple) -> None:
        """
        Handle NOTIFY request.

        Args:
            message: SIPMessage object.
            addr: Source address tuple.
        """
        self.logger.debug(f"NOTIFY request from {addr}")
        # Acknowledge the notification
        self._send_response(200, "OK", message, addr)

    def _handle_refer(self, message: SIPMessage, addr: AddrTuple) -> None:
        """
        Handle REFER request for call transfer (RFC 3515).

        Validates the request, accepts the implicit subscription, and hands the
        transfer to TransferHandler. Everything after that -- originating or
        adopting the target leg, bridging, aborting, and reporting progress on
        the subscription -- belongs to the TransferSession, so that a transfer
        signaled over SIP behaves identically to one driven by the REST API or
        by the auto attendant.

        Args:
            message: SIPMessage object.
            addr: Source address tuple.
        """
        from pbx.core.transfer_session import NOTIFY_TRYING, ReferDialog, TransferMode

        self.logger.info(f"REFER request from {addr}")

        refer_to = message.get_header("Refer-To")
        if not refer_to:
            self._send_response(400, "Bad Request - Missing Refer-To", message, addr)
            return

        call_id = message.get_header("Call-ID")
        if not call_id:
            self._send_response(400, "Bad Request - Missing Call-ID", message, addr)
            return
        referred_by = message.get_header("Referred-By")

        self.logger.info(f"REFER to: {refer_to}, Call-ID: {call_id}")

        # Accept the REFER request (creates an implicit subscription)
        self._send_response(202, "Accepted", message, addr)

        # The subscription outlives this request: every NOTIFY for this
        # transfer is sent on it, with a strictly increasing CSeq.
        dialog = ReferDialog(
            call_id=call_id,
            from_header=message.get_header("To") or "",
            to_header=message.get_header("From") or "",
            addr=addr,
        )
        self.send_transfer_notify(dialog, NOTIFY_TRYING, terminated=False)

        if not self.pbx_core:
            self.send_transfer_notify(dialog, "SIP/2.0 503 Service Unavailable", terminated=True)
            return

        # Parse Refer-To: target user plus any embedded headers (RFC 3515
        # allows ?header=value pairs inside the URI; attended transfers
        # carry the dialog to replace in an embedded Replaces header,
        # RFC 3891).
        destination, embedded_headers = self._parse_refer_to(refer_to)
        if not destination:
            self.logger.error(f"Could not parse Refer-To URI: {refer_to}")
            self.send_transfer_notify(dialog, "SIP/2.0 400 Bad Request", terminated=True)
            return

        # Find the active call for the dialog the REFER arrived on
        call = self.pbx_core.call_manager.get_call(call_id)
        if not call:
            self.logger.warning(f"No active call found for REFER Call-ID: {call_id}")
            self.send_transfer_notify(
                dialog, "SIP/2.0 481 Call/Transaction Does Not Exist", terminated=True
            )
            return
        # Pin the dialog-owning record so every NOTIFY draws CSeq from the
        # same pbx_leg_cseq counter as any BYE sent on this dialog.
        dialog.call = call

        consult = None
        mode = TransferMode.BLIND
        replaces = embedded_headers.get("replaces")
        if replaces:
            # Attended transfer: Replaces names the consultation dialog.
            replaces_call_id = replaces.split(";", 1)[0]
            self.logger.info(
                f"Attended transfer: {destination}, replacing dialog {replaces_call_id}"
            )

            if replaces_call_id == call_id:
                self.logger.error(f"REFER Replaces names its own dialog ({call_id}); rejecting")
                self.send_transfer_notify(dialog, "SIP/2.0 400 Bad Request", terminated=True)
                return

            consult = self.pbx_core.call_manager.get_call(replaces_call_id)
            if not consult:
                self.logger.error(
                    f"Replaces dialog {replaces_call_id} not found for attended transfer"
                )
                self.send_transfer_notify(
                    dialog, "SIP/2.0 481 Call/Transaction Does Not Exist", terminated=True
                )
                return
            mode = TransferMode.ATTENDED
        else:
            self.logger.info(f"Blind transfer to {destination}")

        # Which side of the call the referrer occupies, captured now while
        # their address is still known (it is nulled as legs are dropped).
        transferor_side = "caller" if addr == call.caller_addr else "callee"

        # start_transfer reports its own failures on the subscription.
        self.pbx_core.transfer_handler.start_transfer(
            call,
            destination,
            mode=mode,
            transferor_side=transferor_side,
            refer_dialog=dialog,
            existing_consult=consult,
            referred_by=referred_by,
        )

    @staticmethod
    def _parse_refer_to(refer_to: str) -> tuple[str | None, dict[str, str]]:
        """
        Parse a Refer-To header into the target user and embedded headers.

        Handles display names, angle brackets, URI parameters, and
        URL-encoded embedded headers, e.g.::

            <sip:1517@pbx;user=phone?Replaces=abc%3Bto-tag%3Dx%3Bfrom-tag%3Dy>

        Args:
            refer_to: Raw Refer-To header value.

        Returns:
            Tuple of (target user or None, {lowercased header: decoded value}).
        """
        import re
        from urllib.parse import unquote

        bracket_match = re.search(r"<([^>]+)>", refer_to)
        uri = bracket_match.group(1) if bracket_match else refer_to.strip()

        embedded: dict[str, str] = {}
        if "?" in uri:
            uri, _, header_part = uri.partition("?")
            for item in header_part.split("&"):
                key, sep, value = item.partition("=")
                if sep:
                    embedded[unquote(key).lower()] = unquote(value)

        user_match = re.search(r"sip:([^@;>]+)", uri)
        return (user_match.group(1) if user_match else None, embedded)

    def send_transfer_notify(
        self,
        dialog: Any,
        sipfrag_status: str,
        *,
        terminated: bool,
    ) -> None:
        """
        Send a NOTIFY for a REFER subscription (RFC 3515 Section 2.4).

        The body is a message/sipfrag carrying the transfer's status. This is
        the only way a transferor's phone learns that a transfer finished, so
        two details it used to get wrong both left phones stuck displaying
        "transferring" indefinitely:

        - The request needs Via and Max-Forwards like any other (RFC 3261
          SS8.1.1.6-7); strict UAs silently drop requests without them, so
          `_add_pbx_request_headers` is not optional here.
        - Every NOTIFY in a subscription needs its own CSeq. Reusing one makes
          the phone treat the final NOTIFY as a retransmission of the initial
          "100 Trying" and ignore it, so the transfer never appears to end.

        At most one terminating NOTIFY is sent per subscription.

        Args:
            dialog: The `ReferDialog` identifying the subscription. Its CSeq
                counter and final-NOTIFY latch are updated in place.
            sipfrag_status: SIP status line for the sipfrag body.
            terminated: Whether this is the subscription's final NOTIFY.
        """
        if dialog.final_sent:
            return
        if terminated:
            dialog.final_sent = True

        # NOTIFYs share a dialog with any BYE the PBX later sends the
        # transferor on this same leg (same Call-ID, same direction), and
        # RFC 3261 SS12.2.1.1 requires one strictly increasing CSeq across
        # both -- not one sequence per method. Draw from the dialog-owning
        # record's pbx_leg_cseq, the counter _send_leg_bye also uses. The
        # record is pinned on the dialog so this works even for the final
        # NOTIFY, sent after end_call has unregistered the record.
        call = dialog.call
        if call is None and self.pbx_core:
            call = self.pbx_core.call_manager.get_call(dialog.call_id)
        if call is not None:
            call.pbx_leg_cseq = max(call.pbx_leg_cseq, dialog.notify_cseq) + 1
            dialog.notify_cseq = call.pbx_leg_cseq
        else:
            dialog.notify_cseq += 1
        notify_msg = SIPMessageBuilder.build_request(
            method="NOTIFY",
            uri=f"sip:{dialog.addr[0]}:{dialog.addr[1]}",
            from_addr=dialog.from_header,
            to_addr=dialog.to_header,
            call_id=dialog.call_id,
            cseq=dialog.notify_cseq,
        )

        notify_msg.set_header(
            "Subscription-State",
            "terminated;reason=noresource" if terminated else "active;expires=60",
        )
        notify_msg.set_header("Event", "refer")
        notify_msg.set_header("Content-type", "message/sipfrag;version=2.0")

        # Body is the SIP status line fragment
        notify_msg.body = sipfrag_status
        notify_msg.set_header("Content-Length", str(len(sipfrag_status.encode("utf-8"))))

        self._add_pbx_request_headers(notify_msg, dialog.addr)
        self._send_message(notify_msg.build(), dialog.addr)
        self.logger.debug(
            f"Sent transfer NOTIFY: {sipfrag_status} (CSeq {dialog.notify_cseq}, "
            f"terminated={terminated})"
        )

    def _handle_info(self, message: SIPMessage, addr: AddrTuple) -> None:
        """
        Handle INFO request (typically used for DTMF signaling).

        SIP INFO can carry DTMF digits in the message body with Content-type:
        - application/dtmf-relay (RFC 2833 style)
        - application/dtmf (simple format)

        Args:
            message: SIPMessage object.
            addr: Source address tuple.
        """
        self.logger.debug(f"INFO request from {addr}")

        # Get call context
        call_id = message.get_header("Call-ID")
        content_type = message.get_header("Content-type")

        # Extract DTMF digit from message body
        dtmf_digit: str | None = None
        if message.body and content_type:
            content_type_lower = content_type.lower()

            # Only process DTMF-related content types (handle charset and other
            # parameters)
            if content_type_lower.startswith(("application/dtmf-relay", "application/dtm")):
                # Parse DTMF from body
                # Format can be:
                # Signal=1
                # Signal=1\nDuration=160
                body_lines = message.body.strip().split("\n")
                for line in body_lines:
                    if line.startswith("Signal="):
                        parts = line.split("=", 1)
                        if len(parts) == 2:
                            digit = parts[1].strip()

                            # Check if it's already a valid DTMF digit
                            # character
                            if digit in VALID_DTMF_DIGITS:
                                dtmf_digit = digit
                                break
                            # Check if it's an RFC 2833 event code (some phones
                            # send "11" for "#")
                            if digit in RFC2833_EVENT_TO_DTMF:
                                dtmf_digit = RFC2833_EVENT_TO_DTMF[digit]
                                self.logger.debug(
                                    f"Converted RFC 2833 event code {digit} to DTMF digit {dtmf_digit}"
                                )
                                break
                            self.logger.warning(f"Invalid DTMF digit in SIP INFO: {digit}")
                        break

                if dtmf_digit:
                    self.logger.info(f"Received DTMF via SIP INFO: {dtmf_digit} for call {call_id}")

                    # Deliver DTMF to PBX core for processing
                    if self.pbx_core and call_id:
                        self.pbx_core.handle_dtmf_info(call_id, dtmf_digit)

        # Always respond with 200 OK to INFO requests
        self._send_response(200, "OK", message, addr)

    def _handle_sip_message_method(self, message: SIPMessage, addr: AddrTuple) -> None:
        """
        Handle MESSAGE request for instant messaging (RFC 3428).

        Routes SIP MESSAGE to the destination endpoint. If the recipient is
        registered, the message is forwarded. If not, a 404 is returned.

        Args:
            message: SIPMessage object.
            addr: Source address tuple.
        """
        import re

        self.logger.info(f"MESSAGE request from {addr}")

        from_header = message.get_header("From")
        to_header = message.get_header("To")
        content_type = message.get_header("Content-type")

        if not message.body:
            self.logger.warning("MESSAGE request received with empty body")
            self._send_response(200, "OK", message, addr)
            return

        self.logger.info(f"MESSAGE from {from_header} to {to_header}: {message.body[:100]}")

        if not self.pbx_core:
            self._send_response(503, "Service Unavailable", message, addr)
            return

        # Extract destination extension from To header
        dest_match = re.search(r"sip:(\d+)@", to_header or "")
        if not dest_match:
            self.logger.warning(f"Could not parse destination from To header: {to_header}")
            self._send_response(200, "OK", message, addr)
            return

        dest_extension = dest_match.group(1)

        # Check if destination is registered and route the message
        if self.pbx_core.extension_registry.is_registered(dest_extension):
            dest_addr = self.pbx_core.extension_registry.get_address(dest_extension)

            if dest_addr:
                # Forward the MESSAGE to the recipient
                cseq_header = message.get_header("CSeq") or "1 MESSAGE"
                cseq_num = cseq_header.split()[0]
                fwd_msg = SIPMessageBuilder.build_request(
                    method="MESSAGE",
                    uri=f"sip:{dest_extension}@{dest_addr[0]}:{dest_addr[1]}",
                    from_addr=from_header or "",
                    to_addr=to_header or "",
                    call_id=message.get_header("Call-ID") or "",
                    cseq=cseq_num,
                    body=message.body,
                )
                if content_type:
                    fwd_msg.set_header("Content-type", content_type)

                self._send_message(fwd_msg.build(), dest_addr)
                self.logger.info(f"Forwarded MESSAGE to {dest_extension} at {dest_addr}")

                # Trigger webhook if available
                if hasattr(self.pbx_core, "webhook_system"):
                    from pbx.features.webhooks import WebhookEvent

                    self.pbx_core.webhook_system.trigger_event(
                        WebhookEvent.MESSAGE_RECEIVED,
                        {
                            "from": from_header,
                            "to": to_header,
                            "content_type": content_type,
                            "body_preview": message.body[:200],
                        },
                    )

                self._send_response(200, "OK", message, addr)
            else:
                self.logger.warning(
                    f"Destination {dest_extension} registered but no address available"
                )
                self._send_response(480, "Temporarily Unavailable", message, addr)
        else:
            self.logger.info(f"MESSAGE destination {dest_extension} not registered")
            self._send_response(404, "Not Found", message, addr)

    def _handle_prack(self, message: SIPMessage, addr: AddrTuple) -> None:
        """
        Handle PRACK request for Provisional Response Acknowledgment (RFC 3262).

        PRACK acknowledges reliable provisional responses (1xx). This implementation:
        1. Validates the RAck header matches a pending provisional response
        2. Stops retransmission of that provisional response
        3. Responds with 200 OK

        Args:
            message: SIPMessage object.
            addr: Source address tuple.
        """
        self.logger.debug(f"PRACK request from {addr}")

        rack_header = message.get_header("RAck")
        call_id = message.get_header("Call-ID")

        if not rack_header:
            self._send_response(400, "Bad Request - Missing RAck", message, addr)
            return

        self.logger.info(f"PRACK acknowledging response: {rack_header} for call {call_id}")

        # Parse RAck header: "response-num cseq-num method"
        rack_parts = rack_header.split()
        if len(rack_parts) >= 2:
            try:
                rseq = int(rack_parts[0])
                # Look up the pending provisional response
                pending_key = (call_id or "", rseq)
                if pending_key in self.pending_provisional_responses:
                    # Stop retransmission of this provisional response
                    pending = self.pending_provisional_responses.pop(pending_key)
                    if pending.get("retransmit_timer"):
                        pending["retransmit_timer"].cancel()
                    self.logger.info(
                        f"Acknowledged provisional response RSeq={rseq} for call {call_id}"
                    )
                else:
                    self.logger.debug(
                        f"No pending provisional response for RSeq={rseq}, call {call_id}"
                    )
            except ValueError:
                self.logger.warning(f"Invalid RAck header format: {rack_header}")

        # Forward PRACK to callee if this is a proxied call
        if self.pbx_core and call_id:
            call = self.pbx_core.call_manager.get_call(call_id)
            if call and call.callee_addr and call.callee_addr != addr:
                self._send_message(message.build(), call.callee_addr)
                self.logger.debug(f"Forwarded PRACK to callee for call {call_id}")

        self._send_response(200, "OK", message, addr)

    def _handle_update(self, message: SIPMessage, addr: AddrTuple) -> None:
        """
        Handle UPDATE request for session modification (RFC 3311).

        UPDATE allows modification of session parameters (like SDP) without
        changing the dialog state. This implementation:
        1. Parses the new SDP offer
        2. Validates codec compatibility
        3. Updates the RTP relay endpoints if media address/port changed
        4. Responds with 200 OK and answer SDP

        Args:
            message: SIPMessage object.
            addr: Source address tuple.
        """
        self.logger.info(f"UPDATE request from {addr}")

        call_id = message.get_header("Call-ID")
        content_type = message.get_header("Content-type")

        if not message.body or not content_type or "sdp" not in content_type.lower():
            self.logger.debug(f"UPDATE without SDP for call {call_id}")
            self._send_response(200, "OK", message, addr)
            return

        self.logger.info(f"UPDATE with SDP for call {call_id}")

        if not self.pbx_core or not call_id:
            self._send_response(200, "OK", message, addr)
            return

        call = self.pbx_core.call_manager.get_call(call_id)
        if not call:
            self._send_response(481, "Call/Transaction Does Not Exist", message, addr)
            return

        # Parse the new SDP offer
        from pbx.sip.sdp import SDPBuilder, SDPSession

        new_sdp_obj = SDPSession()
        new_sdp_obj.parse(message.body)
        new_audio = new_sdp_obj.get_audio_info()

        if new_audio:
            new_address = new_audio.get("address")
            new_port = new_audio.get("port")

            # Determine which party sent the UPDATE and update their endpoint
            is_caller = addr == call.caller_addr
            if is_caller and new_address and new_port:
                call.caller_rtp = new_audio
                self.logger.info(f"Updated caller media: {new_address}:{new_port}")
                # Update RTP relay endpoint
                if call.rtp_ports:
                    caller_endpoint = (new_address, new_port)
                    callee_endpoint = None
                    if call.callee_rtp:
                        callee_endpoint = (
                            call.callee_rtp["address"],
                            call.callee_rtp["port"],
                        )
                    self.pbx_core.rtp_relay.set_endpoints(call_id, caller_endpoint, callee_endpoint)
            elif new_address and new_port:
                call.callee_rtp = new_audio
                self.logger.info(f"Updated callee media: {new_address}:{new_port}")
                if call.rtp_ports:
                    caller_endpoint = None
                    if call.caller_rtp:
                        caller_endpoint = (
                            call.caller_rtp["address"],
                            call.caller_rtp["port"],
                        )
                    callee_endpoint = (new_address, new_port)
                    self.pbx_core.rtp_relay.set_endpoints(call_id, caller_endpoint, callee_endpoint)

        # Build answer SDP using codecs from the UPDATE's offer (not defaults)
        server_ip = self.pbx_core._get_server_ip()
        rtp_port = call.rtp_ports[0] if call.rtp_ports else 10000

        # Determine which codecs to include in the answer
        update_codecs = new_audio.get("formats", None) if new_audio else None
        is_caller = addr == call.caller_addr
        ext_number = call.from_extension if is_caller else call.to_extension
        user_agent = self.pbx_core._get_phone_user_agent(ext_number)
        phone_model = self.pbx_core._detect_phone_model(user_agent)
        answer_codecs = self.pbx_core._get_compatible_codecs(phone_model, update_codecs)

        dtmf_pt = self.pbx_core._get_dtmf_payload_type()
        ilbc_mode = self.pbx_core._get_ilbc_mode()
        # Preserve media protocol, SRTP crypto, and rtpmap names from the UPDATE offer
        update_protocol = new_audio.get("protocol", "RTP/AVP") if new_audio else "RTP/AVP"
        update_crypto = (new_audio.get("crypto") or None) if new_audio else None
        update_rtpmap = (new_audio.get("rtpmap_names") or None) if new_audio else None

        answer_sdp = SDPBuilder.build_audio_sdp(
            server_ip,
            rtp_port,
            session_id=call_id,
            codecs=answer_codecs,
            dtmf_payload_type=dtmf_pt,
            ilbc_mode=ilbc_mode,
            protocol=update_protocol,
            crypto=update_crypto,
            rtpmap_overrides=update_rtpmap,
        )

        response = SIPMessageBuilder.build_response(200, "OK", message, body=answer_sdp)
        response.set_header("Content-type", "application/sdp")
        self._send_message(response.build(), addr)

    def _handle_publish(self, message: SIPMessage, addr: AddrTuple) -> None:
        """
        Handle PUBLISH request for event state publication (RFC 3903).

        Implements the event state compositor (ESC) which:
        1. Stores published event state with unique ETags
        2. Handles initial, refresh, modify, and remove publications
        3. Notifies active subscribers of state changes

        Args:
            message: SIPMessage object.
            addr: Source address tuple.
        """
        import time

        self.logger.info(f"PUBLISH request from {addr}")

        event = message.get_header("Event")
        expires_str = message.get_header("Expires") or "3600"
        sip_if_match = message.get_header("SIP-If-Match")
        content_type = message.get_header("Content-type")

        if not event:
            self._send_response(489, "Bad Event", message, addr)
            return

        try:
            expires = int(expires_str)
        except ValueError:
            expires = 3600

        self.logger.info(f"PUBLISH for event: {event}, expires: {expires}")

        if sip_if_match:
            # Refresh/modify/remove existing publication
            pub_key = (event, sip_if_match)
            if pub_key not in self.publications:
                # ETag not found - Conditional Request Failed
                self._send_response(412, "Conditional Request Failed", message, addr)
                return

            if expires == 0:
                # Remove publication
                del self.publications[pub_key]
                self.logger.info(f"Removed publication: {event}/{sip_if_match}")
                response = SIPMessageBuilder.build_response(200, "OK", message)
                response.set_header("Expires", "0")
                response.set_header("SIP-ETag", sip_if_match)
                self._send_message(response.build(), addr)
                # Notify subscribers of removal
                self._notify_subscribers(event)
                return

            if message.body:
                # Modify publication
                self.publications[pub_key]["body"] = message.body
                self.publications[pub_key]["content_type"] = content_type
                self.publications[pub_key]["expires"] = expires
                self.publications[pub_key]["updated"] = time.time()
                etag = sip_if_match
            else:
                # Refresh publication (extend expiry)
                self.publications[pub_key]["expires"] = expires
                self.publications[pub_key]["updated"] = time.time()
                etag = sip_if_match
        else:
            # Initial publication - generate new ETag
            self._etag_counter += 1
            etag = f"pub-{self._etag_counter}-{int(time.time())}"
            pub_key = (event, etag)

            self.publications[pub_key] = {
                "event": event,
                "etag": etag,
                "body": message.body or "",
                "content_type": content_type,
                "expires": expires,
                "addr": addr,
                "created": time.time(),
                "updated": time.time(),
            }
            self.logger.info(f"New publication: {event}/{etag}")

        # Respond with success and the ETag
        response = SIPMessageBuilder.build_response(200, "OK", message)
        response.set_header("Expires", str(expires))
        response.set_header("SIP-ETag", etag)
        self._send_message(response.build(), addr)

        # Notify active subscribers of the state change
        self._notify_subscribers(event)

    def _notify_subscribers(self, event: str) -> None:
        """
        Notify all active subscribers of a state change for an event type.

        Args:
            event: The event type that changed.
        """
        import time

        for sub_key, sub_info in list(self.subscriptions.items()):
            if sub_info["event"] != event:
                continue

            # Check if subscription has expired
            elapsed = time.time() - sub_info["created"]
            if elapsed > sub_info["expires"]:
                del self.subscriptions[sub_key]
                continue

            remaining = int(sub_info["expires"] - elapsed)
            body = self._get_event_state(event, sub_info["to"])
            content_type = self._get_event_content_type(event)

            self._send_event_notify(
                sub_info["from"],
                sub_info["to"],
                sub_info["call_id"],
                sub_info["addr"],
                event,
                f"active;expires={remaining}",
                body,
                content_type,
                contact_uri=sub_info.get("contact_uri"),
            )

    def _handle_response(self, message: SIPMessage, addr: AddrTuple) -> None:
        """
        Handle SIP response.

        Args:
            message: SIPMessage object.
            addr: Source address tuple.
        """
        self.logger.debug(f"Received response {message.status_code} from {addr}")

        # Handle responses to outbound trunk REGISTERs separately from
        # call-related responses below, since they aren't tied to a Call object.
        register_call_id = message.get_header("Call-ID")
        if register_call_id and register_call_id in self._pending_trunk_registrations:
            self._handle_trunk_register_response(message, addr, register_call_id)
            return

        # Handle responses from callee
        if self.pbx_core and message.status_code:
            call_id = message.get_header("Call-ID")
            cseq_header = message.get_header("CSeq") or ""

            # Cancel INVITE retransmission on any response from callee
            if call_id:
                call = self.pbx_core.call_manager.get_call(call_id)
                if call and hasattr(call, "invite_transaction") and call.invite_transaction:
                    call.invite_transaction.on_response_received()
                # Legs of a Find Me/Follow Me burst keep their own transactions,
                # since only one leg at a time can sit on the call.
                if call:
                    self.pbx_core.find_me_follow_me.note_leg_response(call, message)
                    call.invite_transaction = None

            if message.status_code == 180:
                # Ringing - build a proper 180 response to the caller's
                # original INVITE so the Via header matches the caller's
                # transaction.  Forwarding the callee's raw 180 would have
                # the PBX's Via, which the caller's SIP stack would not
                # match and would silently drop.
                self.logger.info(f"Callee ringing for call {call_id}")
                if call_id:
                    call = self.pbx_core.call_manager.get_call(call_id)
                    if call:
                        call.ring()  # Mark call as ringing
                        if call.caller_addr and call.original_invite:
                            ringing_response = SIPMessageBuilder.build_response(
                                180, "Ringing", call.original_invite
                            )
                            # The tag minted here establishes the caller's
                            # early dialog with the PBX. Keep it stable across
                            # provisionals and capture it: for a consultation
                            # leg adopted by a transfer while still ringing,
                            # no 200 OK is ever sent to the caller, so this is
                            # the only tag a teardown BYE can be matched by
                            # (the phone's REFER Replaces references it too).
                            if call.caller_dialog_to:
                                ringing_response.set_header("To", call.caller_dialog_to)
                            else:
                                call.caller_dialog_to = ringing_response.get_header("To")
                            self._send_message(ringing_response.build(), call.caller_addr)

            elif message.status_code == 183:
                # Session Progress (early media) - build proper response to
                # the caller's original INVITE for correct Via matching.
                self.logger.info(f"Session progress for call {call_id}")
                if call_id:
                    call = self.pbx_core.call_manager.get_call(call_id)
                    if call and call.caller_addr and call.original_invite:
                        progress_response = SIPMessageBuilder.build_response(
                            183, "Session Progress", call.original_invite
                        )
                        # Same early-dialog tag handling as the 180 above.
                        if call.caller_dialog_to:
                            progress_response.set_header("To", call.caller_dialog_to)
                        else:
                            call.caller_dialog_to = progress_response.get_header("To")
                        # Include SDP from callee's 183 for early media
                        if message.body:
                            progress_response.body = message.body
                            progress_response.set_header(
                                "Content-Length",
                                str(len(message.body.encode("utf-8"))),
                            )
                            progress_response.set_header("Content-type", "application/sdp")
                        self._send_message(progress_response.build(), call.caller_addr)

            elif message.status_code == 200:
                # OK - only process as callee answer if this is a response to INVITE
                # (200 OK is also sent for BYE, CANCEL, OPTIONS, etc.)
                if call_id and "INVITE" in cseq_header:
                    self.logger.info(f"Callee answered call {call_id}")

                    # First answer wins a Find Me/Follow Me burst: this promotes
                    # the winning leg onto the call and cancels the rest. It has
                    # to run before the ACK, which is built from that leg's own
                    # INVITE.
                    answered_call = self.pbx_core.call_manager.get_call(call_id)
                    if answered_call:
                        self.pbx_core.find_me_follow_me.on_leg_answered(answered_call, message)

                    # Per RFC 3261 Section 13.2.2.4, the UAC (PBX acting as
                    # B2BUA) MUST send an ACK to the callee after receiving
                    # their 200 OK.  Without this ACK, the callee retransmits
                    # the 200 OK and eventually tears down the call.
                    self._send_ack_to_callee(message, addr, call_id)

                    self.pbx_core.call_router.handle_callee_answer(call_id, message, addr)

            elif message.status_code and 300 <= message.status_code < 400:
                # Redirect (e.g. 302 Moved Temporarily) - the callee wants
                # the call sent elsewhere, typically a phone-side "always
                # forward" configuration.  Only meaningful as a response to
                # our INVITE (a redirect to a BYE/CANCEL/etc. is not
                # actionable).
                cseq_header = message.get_header("CSeq") or ""
                if call_id and "INVITE" in cseq_header:
                    self.logger.info(f"Callee redirect {message.status_code} for call {call_id}")

                    # Per RFC 3261 Section 17.1.1.3, the ACK for a non-2xx
                    # final response (which includes 3xx) is part of the
                    # same transaction as the INVITE and MUST reuse its Via
                    # branch.  Without this ACK, the callee's UAS transaction
                    # never completes and keeps retransmitting the 3xx.
                    self._send_ack_to_callee(message, addr, call_id, use_invite_branch=True)

                    # A call ringing through a Find Me/Follow Me list is already
                    # being steered by that list, and the list is the operator's
                    # explicit configuration. A destination's own phone-side
                    # "forward on no answer" must not hijack it: following the
                    # redirect would replace the destination's configured ring
                    # time with the default no-answer timeout and silently drop
                    # every remaining destination. Treat it as this destination
                    # not taking the call and move to the next one.
                    call = self.pbx_core.call_manager.get_call(call_id)
                    if call and self.pbx_core.find_me_follow_me.on_leg_failure(call, message):
                        return

                    contact_header = message.get_header("Contact")
                    self.pbx_core.call_router.handle_redirect(call_id, contact_header)

            elif message.status_code and message.status_code >= 400:
                # Error response from callee (4xx/5xx/6xx) - build a proper
                # error response to the caller's original INVITE so the Via
                # header matches their transaction.  Forwarding the callee's
                # raw response would have the PBX's Via, causing the caller's
                # SIP stack to silently discard it.
                if call_id:
                    call = self.pbx_core.call_manager.get_call(call_id)
                    if (
                        call
                        and "INVITE" in cseq_header
                        and message.status_code in (401, 407)
                        and getattr(call, "trunk", None)
                        and not getattr(call, "invite_auth_retried", False)
                    ):
                        # Trunk provider is challenging the INVITE itself
                        # (separately from REGISTER), e.g. some providers
                        # require Digest auth on every request. Retry once
                        # with credentials instead of failing the call.
                        self._retry_trunk_invite_with_auth(call, message, addr, call_id)
                        return

                self.logger.warning(f"Callee error {message.status_code} for call {call_id}")

                # Acknowledge first, unconditionally, before anything looks at application
                # state. RFC 3261 SS17.1.1.3 makes this the transaction layer's job: every
                # non-2xx final response to an INVITE is ACKed, whether or not the PBX still
                # has a call to attach it to.
                #
                # It used to sit inside `if call:` below, so a response arriving after the
                # call was gone got no ACK at all. A 487 answering our own CANCEL is exactly
                # that case -- the CANCEL tears the call down, then the 487 arrives to find
                # nothing -- and the far end then retransmits it until Timer H expires 32
                # seconds later, holding its INVITE transaction open the whole time. An
                # endpoint in that state answers the next call 486 Busy.
                if "INVITE" in cseq_header:
                    self._send_ack_to_callee(message, addr, call_id, use_invite_branch=True)

                if call_id:
                    call = self.pbx_core.call_manager.get_call(call_id)
                    if call:
                        from pbx.core.call import CallState

                        # Transfer destination declined or failed: ACK the
                        # error and let the transfer session resolve it --
                        # recalling the transferor or tearing every remaining
                        # leg down, per the configured failure policy.
                        if call.transfer_session_id and call.state != CallState.CONNECTED:
                            from pbx.core.transfer_session import LegEvent, LegEventResult

                            result = self.pbx_core.transfer_handler.on_leg_event(
                                call, LegEvent.REJECTED, side="callee"
                            )
                            if result is not LegEventResult.FORWARD:
                                return

                        # If the caller's leg was already answered (e.g. into
                        # voicemail after the no-answer timeout), this response
                        # is just the callee leg terminating -- typically the
                        # 487 Request Terminated acknowledging our CANCEL.
                        # ACK it to stop retransmissions, but leave the call
                        # alone: forwarding the error to the caller or ending
                        # the call would tear down the live voicemail
                        # recording session.
                        if call.routed_to_voicemail or call.state == CallState.CONNECTED:
                            self.logger.info(
                                f"Ignoring callee error {message.status_code} for call "
                                f"{call_id} - caller leg already answered "
                                "(callee leg cancelled)"
                            )
                            return
                        # A Find Me/Follow Me destination that is busy, on DND,
                        # or declines has not ended the call -- there are more
                        # places to try. FMFM decides for a call it is ringing;
                        # every other call, and anything it declines to handle,
                        # falls through to the mailbox below.
                        #
                        # Must come before the no-answer timer is touched. A leg
                        # FMFM abandoned answers our CANCEL with a 487 that lands
                        # *after* the next destination is already ringing, so
                        # cancelling here would kill the new leg's ring timer and
                        # leave that destination ringing forever. FMFM cancels
                        # the timer itself for a leg it really is giving up on.
                        if (
                            "INVITE" in cseq_header
                            and self.pbx_core.find_me_follow_me.on_leg_failure(call, message)
                        ):
                            return
                        # Cancel no-answer timer since the callee already responded
                        if call.no_answer_timer:
                            call.no_answer_timer.cancel()
                        if (
                            message.status_code in VOICEMAIL_ON_REJECT_STATUSES
                            and call.caller_addr
                            and call.original_invite
                            and not getattr(call, "trunk", None)
                            and self.pbx_core.config.get("voicemail.on_reject", True)
                        ):
                            self.logger.info(
                                f"Callee rejected call {call_id} with {message.status_code}; "
                                "routing caller to voicemail"
                            )
                            self.pbx_core.call_router._handle_no_answer(call_id)
                            return
                        # Same intent for a PBX-placed leg, reached a different
                        # way: it has no caller of its own to answer into the
                        # mailbox, because the waiting party is on the bridged
                        # peer -- a separate Call record.
                        if (
                            message.status_code in VOICEMAIL_ON_REJECT_STATUSES
                            and not call.original_invite
                            and call.bridged_peer_call_id
                            and self.pbx_core.config.get("voicemail.on_reject", True)
                            and self._record_peer_into_mailbox(call)
                        ):
                            self.logger.info(
                                f"Placed call {call_id} rejected with {message.status_code}; "
                                f"recording the waiting party into {call.to_extension}'s mailbox"
                            )
                            self.pbx_core.end_call(call_id)
                            return
                        # A PBX-placed leg reports its outcome through callbacks rather than
                        # a response to a caller, because it has no caller: CallOriginator
                        # promises on_failure for a leg that never connects, and until this
                        # existed the promise was only kept for a pre-flight failure or the
                        # no-answer timer. A leg rejected outright -- 486 from a busy phone,
                        # 488 from one that would not take the media -- resolved silently,
                        # so whatever placed it went on believing the leg was still ringing.
                        # A page kept relaying to an amplifier that had refused the call.
                        #
                        # Placed after the transfer, FMFM and voicemail branches, all of
                        # which return, so this only sees legs none of them claimed. The
                        # callbacks are cleared first: on_failure may end the call, which
                        # re-enters here, and the caller should hear about the failure once.
                        callbacks = getattr(call, "originate_callbacks", None)
                        if callbacks and callbacks.get("on_failure"):
                            call.originate_callbacks = None
                            reason = _ORIGINATE_FAILURE_REASONS.get(
                                message.status_code, "unreachable"
                            )
                            self.logger.info(
                                f"Originated leg {call_id} failed with "
                                f"{message.status_code}: {reason}"
                            )
                            try:
                                callbacks["on_failure"](call, reason)
                            except Exception as e:
                                self.logger.error(
                                    f"on_failure callback raised for leg {call_id}: {e}"
                                )

                        if call.caller_addr and call.original_invite:
                            error_response = SIPMessageBuilder.build_response(
                                message.status_code,
                                message.status_text or "Error",
                                call.original_invite,
                            )
                            self._send_message(error_response.build(), call.caller_addr)
                        # A rejected leg takes its bridged peer with it: on a
                        # PBX-placed pair there is no caller leg for the error
                        # to be relayed to, so nothing else would end the
                        # party already waiting on the other side.
                        self.end_bridged_peer(call)
                        # End the call on our side
                        self.pbx_core.end_call(call_id)

    def _add_via_nat_params(self, response: SIPMessage, addr: AddrTuple) -> None:
        """
        Add received= and rport= to Via header for NAT traversal (RFC 3261/3581).

        Compares the source IP/port from the network layer with the Via sent-by
        address. If they differ (phone is behind NAT or on a different subnet),
        adds received= and rport= so the phone knows its external address.

        Args:
            response: SIP response message to modify.
            addr: Actual source address tuple (host, port) from the network.
        """
        import re

        via = response.headers.get("Via", "")
        if not via:
            return

        # Extract sent-by address and port from Via
        via_match = re.search(r"SIP/2\.0/\w+\s+([^;:]+)(?::(\d+))?", via)
        if not via_match:
            return

        via_host = via_match.group(1).strip()
        via_port = int(via_match.group(2)) if via_match.group(2) else 5060
        source_host, source_port = addr

        # RFC 3261 Section 18.2.2: Add received= if Via host differs from source IP
        if via_host != source_host and ";received=" not in via:
            via = f"{via};received={source_host}"

        # RFC 3581: Add rport= with actual source port if rport was requested
        if ";rport" in via and f";rport={source_port}" not in via:
            # Replace bare ;rport with ;rport=<actual_port>
            via = re.sub(r";rport(?!=)", f";rport={source_port}", via)
        elif via_port != source_port and ";rport" not in via:
            # Even without rport request, add it if ports differ (common practice)
            via = f"{via};rport={source_port}"

        response.set_header("Via", via)

    @staticmethod
    def _parse_cseq_number(cseq_header: str | None) -> int:
        """Parse the sequence number from a CSeq header value, defaulting to 1."""
        try:
            return int((cseq_header or "1 INVITE").split()[0])
        except (ValueError, IndexError):
            return 1

    def _send_response(
        self, status_code: int, status_text: str, request: SIPMessage, addr: AddrTuple
    ) -> None:
        """
        Send SIP response.

        Args:
            status_code: Status code.
            status_text: Status text.
            request: Original request message.
            addr: Destination address.
        """
        response = SIPMessageBuilder.build_response(status_code, status_text, request)
        self._add_via_nat_params(response, addr)
        self._send_message(response.build(), addr)

    def _send_message(self, message: str, addr: AddrTuple) -> None:
        """
        Send SIP message over the network.

        Args:
            message: Message string.
            addr: Destination address tuple (host, port).
        """
        try:
            if not self.socket:
                self.logger.warning("Cannot send message: socket is closed")
                return
            self.socket.sendto(message.encode("utf-8"), addr)
            self.logger.debug(f"Sent message to {addr}")
        except OSError as e:
            # The destination belongs in the message: a send that fails in
            # name resolution is only diagnosable if the log says what host
            # was being resolved.
            self.logger.error(f"Error sending message to {addr}: {e}")
