"""
Call management and session handling
"""

import threading
from collections import deque
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pbx.rtp.handler import RTPRecorder


class CallState(Enum):
    """Call states"""

    IDLE = "idle"
    CALLING = "calling"
    RINGING = "ringing"
    CONNECTED = "connected"
    HOLD = "hold"
    TRANSFERRING = "transferring"
    ENDED = "ended"


class Call:
    """Represents a call session"""

    def __init__(self, call_id: str, from_extension: str, to_extension: str) -> None:
        """
        Initialize call

        Args:
            call_id: Unique call identifier
            from_extension: Calling extension
            to_extension: Called extension
        """
        self.call_id: str = call_id
        self.from_extension: str = from_extension
        self.to_extension: str = to_extension
        self.state: CallState = CallState.IDLE
        self.start_time: datetime | None = None
        self.answer_time: datetime | None = None
        self.end_time: datetime | None = None
        self.rtp_ports: tuple[int, int] | None = None
        self.recording: bool = False
        self.on_hold: bool = False
        # Which leg put the call on hold via re-INVITE: "caller" or
        # "callee". None if hold was not initiated via SIP re-INVITE
        # (e.g. triggered through the API) or if not currently on hold.
        self.held_by: str | None = None
        self.caller_rtp: dict[str, Any] | None = None  # Caller's RTP endpoint info
        self.caller_addr: tuple[str, int] | None = None  # Caller's SIP address
        self.callee_rtp: dict[str, Any] | None = None  # Callee's RTP endpoint info
        self.callee_addr: tuple[str, int] | None = None  # Callee's SIP address
        self.original_invite: Any | None = None  # Original INVITE message from caller
        self.callee_invite: Any | None = None  # INVITE sent to callee (for CANCEL reference)
        self.no_answer_timer: Any | None = None  # Timer for routing to voicemail
        self.routed_to_voicemail: bool = False  # Flag to track if routed to VM
        self.transferred: bool = False  # Flag to track if call has been transferred
        self.transfer_destination: str | None = None  # Destination extension for transfer

        # REFER-based transfer state (RFC 3515 / RFC 3891). The consultation
        # call is the transferor's leg to the transfer destination; on
        # completion the two surviving legs are bridged, each keeping its own
        # Call record cross-linked via bridged_peer_call_id.
        self.is_transfer_consult: bool = False  # True on the consultation call
        self.pending_transfer_consult_id: str | None = None  # On the original call
        # while awaiting the consultation call's answer (deferred bridge)
        self.bridged_peer_call_id: str | None = None  # Other leg's record after bridge
        self.bridge_peer_side: str | None = None  # Relay side ("a"/"b") this leg's
        # party occupies on the bridged peer's relay
        self.transfer_referrer_addr: tuple[str, int] | None = None  # REFER sender
        self.transfer_referrer_is_caller: bool | None = None  # Referrer's side on
        # the original call, recorded at REFER time (addresses may be nulled later)
        self.uses_peer_relay: bool = False  # Consult leg was originated by the PBX
        # advertising the original call's relay port (blind transfer)
        # Optional hook invoked by abort_pending_transfer() instead of its
        # default BYE+end_call when a pending transfer's destination never
        # answers/declines. Lets a transfer initiator with no real transferor
        # phone to fall back to (e.g. an IVR session) keep its own call alive
        # and handle the failure itself, without abort_pending_transfer
        # needing to know who initiated the transfer.
        self.transfer_failure_callback: Any | None = None
        self.callee_dialog_to: str | None = None  # To header (with tag) from the
        # callee's 200 OK -- dialog identity for PBX-originated in-dialog requests
        # toward the callee leg
        self.caller_dialog_to: str | None = None  # To header (with the tag the PBX
        # generated) from the 200 OK the PBX sent the caller -- dialog identity for
        # PBX-originated in-dialog requests toward the caller leg. build_response()
        # mints this tag fresh per call; it is never derivable from original_invite
        # (whose To header predates any response and so carries no tag).
        self.pbx_leg_cseq: int = 1  # CSeq counter for PBX-originated requests
        # toward the callee leg (1 = the INVITE)

        # Voicemail access attributes
        self.voicemail_access: bool = False  # Flag indicating voicemail access call
        self.voicemail_extension: str | None = None  # Target extension for voicemail access
        self.voicemail_ivr: Any | None = None  # VoicemailIVR instance for interactive menus
        self.voicemail_recorder: RTPRecorder | None = None
        self.voicemail_timer: threading.Timer | None = None

        # Auto attendant attributes
        self.auto_attendant_active: bool = False  # Flag indicating auto attendant call
        self.aa_session: dict[str, Any] | None = None  # Auto attendant session data

        # DTMF handling attributes
        # Queue for out-of-band DTMF digits (SIP INFO)
        self.dtmf_info_queue: list[str] = []

        # INVITE transaction for retransmission (RFC 3261)
        self.invite_transaction: Any | None = None

        # Count of SIP 3xx redirects followed for this call (loop guard)
        self.redirect_count: int = 0

        # Set by CallOriginator for a PBX-placed call (no original_invite):
        # {"on_answer": Callable[[Call], None] | None,
        #  "on_failure": Callable[[Call, str], None] | None}. Dispatched from
        # handle_callee_answer() and CallOriginator's own no-answer/failure
        # paths. None for calls reacting to an inbound INVITE.
        self.originate_callbacks: dict[str, Any] | None = None

    def start(self) -> None:
        """Start the call"""
        self.state = CallState.CALLING
        self.start_time = datetime.now(UTC)

    def ring(self) -> None:
        """set call state to ringing"""
        self.state = CallState.RINGING

    def connect(self) -> None:
        """Connect the call"""
        self.state = CallState.CONNECTED
        self.answer_time = datetime.now(UTC)

    def hold(self, held_by: str | None = None) -> None:
        """
        Put call on hold

        Args:
            held_by: Which leg initiated the hold ("caller" or "callee"),
                when triggered by a SIP re-INVITE. None for API-triggered hold.
        """
        self.state = CallState.HOLD
        self.on_hold = True
        self.held_by = held_by

    def resume(self) -> None:
        """Resume call from hold"""
        self.state = CallState.CONNECTED
        self.on_hold = False
        self.held_by = None

    def end(self) -> None:
        """End the call"""
        self.state = CallState.ENDED
        self.end_time = datetime.now(UTC)

    def get_duration(self) -> float:
        """Get call duration in seconds"""
        if not self.start_time:
            return 0

        end = self.end_time or datetime.now(UTC)
        return (end - self.start_time).total_seconds()

    def __str__(self) -> str:
        return f"Call {self.call_id}: {self.from_extension} -> {self.to_extension} ({self.state.value})"


class CallManager:
    """Manages active calls"""

    MAX_HISTORY_SIZE = 10000

    def __init__(self) -> None:
        """Initialize call manager"""
        self.active_calls: dict[str, Call] = {}
        self.call_history: deque[Call] = deque(maxlen=self.MAX_HISTORY_SIZE)
        self._lock = threading.Lock()

    def create_call(self, call_id: str, from_extension: str, to_extension: str) -> Call:
        """
        Create new call

        Args:
            call_id: Unique call identifier
            from_extension: Calling extension
            to_extension: Called extension

        Returns:
            Call object
        """
        call = Call(call_id, from_extension, to_extension)
        with self._lock:
            self.active_calls[call_id] = call
        return call

    def get_call(self, call_id: str) -> Call | None:
        """
        Get call by ID

        Args:
            call_id: Call identifier

        Returns:
            Call object or None
        """
        return self.active_calls.get(call_id)

    def end_call(self, call_id: str) -> bool:
        """
        End call

        Args:
            call_id: Call identifier

        Returns:
            True if call was ended
        """
        with self._lock:
            call = self.active_calls.get(call_id)
            if call:
                call.end()
                self.call_history.append(call)
                del self.active_calls[call_id]
                return True
        return False

    def get_active_calls(self) -> list[Call]:
        """Get all active calls"""
        return list(self.active_calls.values())

    def get_extension_calls(self, extension: str) -> list[Call]:
        """
        Get calls for an extension

        Args:
            extension: Extension number

        Returns:
            list of Call objects
        """
        calls = [
            call
            for call in self.active_calls.values()
            if extension in (call.from_extension, call.to_extension)
        ]
        return calls
