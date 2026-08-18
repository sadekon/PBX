"""
Regression tests for the no-answer voicemail recording flow.

Covers the bug where the callee's 487 Request Terminated (the normal
acknowledgment of the CANCEL sent when routing to voicemail) tore down the
just-answered voicemail call, leaving the recorder attached to a dead call:
no save on timeout, no save on #, no save on hangup, and no BYE to the
caller.

Also covers the # keypress paths during recording:
  - out-of-band DTMF (SIP INFO / RFC 2833) via call.dtmf_info_queue
  - in-band DTMF tones with proper G.711 u-law decoding
"""

import math
import struct
from unittest.mock import MagicMock, patch

import pytest

from pbx.core.call import CallState
from pbx.core.voicemail_handler import VoicemailHandler
from pbx.rtp.dtmf_monitor import DTMFMonitor
from pbx.sip.server import SIPServer
from pbx.utils.audio import g711_to_float_samples, pcm16_to_ulaw
from pbx.utils.dtmf import DTMFDetector

ADDR = ("192.168.1.100", 5060)
CALL_ID = "test-call-id-487"


def _make_response_message(status_code: int, cseq: str = "1 INVITE") -> MagicMock:
    """Build a MagicMock that behaves like a SIPMessage response."""
    msg = MagicMock()
    msg.method = None
    msg.status_code = status_code
    msg.status_text = "Request Terminated"
    msg.is_request.return_value = False
    msg.is_response.return_value = True
    msg.build.return_value = f"SIP/2.0 {status_code} Request Terminated\r\n\r\n"
    headers = {"Call-ID": CALL_ID, "CSeq": cseq}
    msg.get_header.side_effect = headers.get
    return msg


def _make_dtmf_pcm16(low_freq: int, high_freq: int, num_samples: int = 6400) -> bytes:
    """Generate a 16-bit PCM dual-tone (DTMF) signal at 8kHz."""
    pcm = bytearray()
    for n in range(num_samples):
        t = n / 8000.0
        sample = int(
            12000 * math.sin(2 * math.pi * low_freq * t)
            + 12000 * math.sin(2 * math.pi * high_freq * t)
        )
        pcm += struct.pack("<h", sample)
    return bytes(pcm)


class _FakeRecorder:
    """Minimal recorder stand-in whose running flag turns off after a few loops.

    Guards against the monitor loop spinning forever if detection regresses.
    """

    def __init__(self, recorded_data: list[bytes], max_polls: int = 50) -> None:
        self.recorded_data = recorded_data
        self.detected_codec = 0
        self._polls = 0
        self._max_polls = max_polls

    @property
    def running(self) -> bool:
        self._polls += 1
        return self._polls <= self._max_polls


@pytest.mark.unit
class TestCalleeErrorAfterVoicemailAnswer:
    """487/4xx from the cancelled callee must not tear down the live voicemail call."""

    @patch("pbx.sip.server.get_logger")
    def test_487_ignored_when_routed_to_voicemail(self, mock_get_logger: MagicMock) -> None:
        pbx = MagicMock()
        call = MagicMock()
        call.bridged_peer_call_id = None
        call.transfer_session_id = None
        call.routed_to_voicemail = True
        call.caller_addr = ("10.0.0.1", 5060)
        pbx.call_manager.get_call.return_value = call

        server = SIPServer(pbx_core=pbx)
        server._send_message = MagicMock()
        server._send_ack_to_callee = MagicMock()

        msg = _make_response_message(487)
        server._handle_response(msg, ADDR)

        # The callee leg's 487 is ACKed to stop retransmissions, reusing
        # the INVITE's Via branch (RFC 3261 Section 17.1.1.3)...
        server._send_ack_to_callee.assert_called_once_with(
            msg, ADDR, CALL_ID, use_invite_branch=True
        )
        # ...but the live voicemail call is left alone: no error forwarded
        # to the caller, no teardown.
        server._send_message.assert_not_called()
        pbx.end_call.assert_not_called()

    @patch("pbx.sip.server.get_logger")
    def test_non_2xx_ack_reuses_invite_via_branch(self, mock_get_logger: MagicMock) -> None:
        """RFC 3261 17.1.1.3: the ACK for a non-2xx must reuse the INVITE's branch."""
        invite_via = "SIP/2.0/UDP 10.0.0.1:5060;branch=z9hG4bKinvite-branch-42"
        pbx = MagicMock()
        call = MagicMock()
        call.bridged_peer_call_id = None
        call.transfer_session_id = None
        call.callee_invite.uri = "sip:1001@192.168.1.50:5060"
        call.callee_invite.get_header.side_effect = {"Via": invite_via}.get
        pbx.call_manager.get_call.return_value = call

        server = SIPServer(pbx_core=pbx)
        server._send_message = MagicMock()

        msg = _make_response_message(487)
        msg.get_header.side_effect = {
            "From": "<sip:2001@10.0.0.1>;tag=ft",
            "To": "<sip:1001@10.0.0.1>;tag=tt",
            "CSeq": "1 INVITE",
        }.get
        server._send_ack_to_callee(msg, ADDR, CALL_ID, use_invite_branch=True)

        raw = server._send_message.call_args[0][0]
        assert raw.startswith("ACK sip:1001@192.168.1.50:5060 SIP/2.0")
        assert "branch=z9hG4bKinvite-branch-42" in raw

    @patch("pbx.sip.server.get_logger")
    def test_4xx_ignored_when_call_connected(self, mock_get_logger: MagicMock) -> None:
        pbx = MagicMock()
        call = MagicMock()
        call.bridged_peer_call_id = None
        call.transfer_session_id = None
        call.routed_to_voicemail = False
        call.state = CallState.CONNECTED
        pbx.call_manager.get_call.return_value = call

        server = SIPServer(pbx_core=pbx)
        server._send_message = MagicMock()
        server._send_ack_to_callee = MagicMock()

        server._handle_response(_make_response_message(486), ADDR)

        server._send_message.assert_not_called()
        pbx.end_call.assert_not_called()

    @patch("pbx.sip.server.get_logger")
    def test_4xx_still_ends_unanswered_call(self, mock_get_logger: MagicMock) -> None:
        pbx = MagicMock()
        call = MagicMock()
        call.bridged_peer_call_id = None
        call.transfer_session_id = None
        call.routed_to_voicemail = False
        call.state = CallState.RINGING
        call.caller_addr = None  # skip error forwarding, assert teardown only
        pbx.call_manager.get_call.return_value = call
        # Not a Find Me/Follow Me call, so the 486 is ours to handle. A MagicMock
        # would be truthy and swallow it.
        pbx.find_me_follow_me.on_leg_failure.return_value = False

        server = SIPServer(pbx_core=pbx)
        server._send_message = MagicMock()

        server._handle_response(_make_response_message(486), ADDR)

        pbx.end_call.assert_called_once_with(CALL_ID)


def _make_rfc2833_rtp_packet(
    pt: int, event: int, end: bool, seq: int, marker: bool = False
) -> bytes:
    """Build a raw RTP packet carrying an RFC 2833 telephone-event payload."""
    byte0 = 0x80  # V=2
    byte1 = (0x80 if marker else 0x00) | (pt & 0x7F)
    header = struct.pack("!BBHII", byte0, byte1, seq, 1234, 0xABCD)
    payload = struct.pack("!BBH", event, (0x80 if end else 0x00) | 10, 160)
    return header + payload


@pytest.mark.unit
class TestDtmfPayloadTypeNegotiation:
    """DTMF must be accepted on the caller's offered PT, not just the configured one."""

    def test_receiver_accepts_callers_offered_payload_type(self) -> None:
        from pbx.rtp.rfc2833 import RFC2833Receiver

        pbx = MagicMock()
        # Configured PT is 101, but the caller offered telephone-event on 96
        # and sends with its own PT.
        rx = RFC2833Receiver(
            local_port=0,
            pbx_core=pbx,
            call_id=CALL_ID,
            payload_type=101,
            extra_payload_types={96},
        )

        hash_event = 11  # RFC 2833 event code for '#'
        rx.handle_rtp_packet(_make_rfc2833_rtp_packet(96, hash_event, False, 1, marker=True), ADDR)
        rx.handle_rtp_packet(_make_rfc2833_rtp_packet(96, hash_event, True, 2), ADDR)

        pbx.handle_dtmf_info.assert_called_once_with(CALL_ID, "#")

    def test_recorder_filters_callers_offered_payload_type(self) -> None:
        from pbx.rtp.handler import RTPRecorder

        handler = MagicMock()
        recorder = RTPRecorder(
            0,
            CALL_ID,
            rfc2833_handler=handler,
            dtmf_payload_type=101,
            extra_dtmf_payload_types={96},
        )
        assert recorder.dtmf_payload_types == {96, 101}


@pytest.mark.unit
class TestByeNotForwardedToCancelledCallee:
    """The caller's BYE must not be forwarded to a callee leg cancelled by voicemail."""

    @patch("pbx.sip.server.get_logger")
    def test_bye_not_forwarded_when_routed_to_voicemail(self, mock_get_logger: MagicMock) -> None:
        caller_addr = ("192.168.1.10", 5060)
        pbx = MagicMock()
        call = MagicMock()
        call.bridged_peer_call_id = None
        call.transfer_session_id = None
        call.queue_ctx = None  # not a queued call
        call.routed_to_voicemail = True
        call.voicemail_access = False
        call.caller_addr = caller_addr
        call.callee_addr = ("192.168.1.50", 5060)
        pbx.call_manager.get_call.return_value = call

        server = SIPServer(pbx_core=pbx)
        server._send_message = MagicMock()  # used for forwarding
        server._send_response = MagicMock()  # used for the 200 OK to the BYE

        bye = MagicMock()
        bye.method = "BYE"
        bye.get_header.side_effect = {"Call-ID": CALL_ID}.get
        server._handle_bye(bye, caller_addr)

        # No BYE forwarded to the cancelled callee (it would answer 481)
        server._send_message.assert_not_called()
        # The call is still ended (saving the voicemail) and the BYE answered
        pbx.end_call.assert_called_once_with(CALL_ID)
        server._send_response.assert_called_once_with(200, "OK", bye, caller_addr)


@pytest.mark.unit
class TestMonitorVoicemailDtmf:
    """# keypress during recording must complete the voicemail."""

    def _make_call(self) -> MagicMock:
        call = MagicMock()
        call.bridged_peer_call_id = None
        call.transfer_session_id = None
        state = MagicMock()
        state.value = "connected"
        call.state = state
        call.dtmf_info_queue = []
        return call

    def test_out_of_band_hash_completes_recording(self) -> None:
        pbx = MagicMock()
        handler = VoicemailHandler(pbx)
        handler.complete_voicemail_recording = MagicMock()

        call = self._make_call()
        call.dtmf_info_queue = ["#"]
        recorder = _FakeRecorder(recorded_data=[])

        with patch("time.sleep"):
            handler.monitor_voicemail_dtmf(CALL_ID, call, recorder)

        handler.complete_voicemail_recording.assert_called_once_with(CALL_ID)

    def test_in_band_hash_tone_completes_recording(self) -> None:
        pbx = MagicMock()
        handler = VoicemailHandler(pbx)
        handler.complete_voicemail_recording = MagicMock()

        call = self._make_call()
        # DTMF '#' is 941 Hz + 1477 Hz; encode as G.711 u-law RTP payloads
        ulaw = pcm16_to_ulaw(_make_dtmf_pcm16(941, 1477))
        packets = [ulaw[i : i + 160] for i in range(0, len(ulaw), 160)]
        recorder = _FakeRecorder(recorded_data=packets)

        # Mirror production wiring: the RTPRecorder feeds each audio payload
        # to its attached DTMFMonitor, which monitor_voicemail_dtmf consumes.
        recorder.dtmf_monitor = DTMFMonitor(call)
        for packet in packets:
            recorder.dtmf_monitor.on_audio_packet(0, packet)

        with patch("time.sleep"):
            handler.monitor_voicemail_dtmf(CALL_ID, call, recorder)

        handler.complete_voicemail_recording.assert_called_once_with(CALL_ID)

    def test_g711_decode_roundtrip_detectable(self) -> None:
        """u-law bytes decoded via g711_to_float_samples must be detectable."""
        ulaw = pcm16_to_ulaw(_make_dtmf_pcm16(941, 1477, num_samples=800))
        samples = g711_to_float_samples(ulaw, payload_type=0)
        assert DTMFDetector(sample_rate=8000).detect_tone(samples) == "#"


@pytest.mark.unit
class TestCompleteRecordingSendsBye:
    """PBX-side completion (timeout or #) must hang up the caller's phone."""

    def _make_call(self) -> MagicMock:
        call = MagicMock()
        call.bridged_peer_call_id = None
        call.transfer_session_id = None
        call.from_extension = "2001"
        call.to_extension = "1001"
        call.caller_addr = ("192.168.1.10", 5060)
        call.voicemail_dialog_to = "<sip:1001@10.0.0.1>;tag=pbx-tag"
        invite = MagicMock()
        invite.get_header.side_effect = {
            "From": '"Caller" <sip:2001@10.0.0.1>;tag=caller-tag',
            "To": "<sip:1001@10.0.0.1>",
            "Contact": "<sip:2001@192.168.1.10:5060>",
        }.get
        call.original_invite = invite
        return call

    def _make_pbx(self) -> MagicMock:
        pbx = MagicMock()
        pbx._get_server_ip.return_value = "10.0.0.1"
        pbx.config.get.return_value = 5060
        return pbx

    def test_send_bye_to_caller_builds_in_dialog_bye(self) -> None:
        pbx = self._make_pbx()
        handler = VoicemailHandler(pbx)
        call = self._make_call()

        handler._send_bye_to_caller(call, CALL_ID)

        pbx.sip_server._send_message.assert_called_once()
        raw, addr = pbx.sip_server._send_message.call_args[0]
        assert addr == ("192.168.1.10", 5060)
        assert raw.startswith("BYE sip:2001@192.168.1.10:5060 SIP/2.0")
        # From is our side of the dialog (with the 200 OK's to-tag),
        # To is the caller's side (with their tag).
        assert "tag=pbx-tag" in raw
        assert "tag=caller-tag" in raw
        assert f"Call-ID: {CALL_ID}" in raw

    def test_complete_voicemail_recording_sends_bye(self) -> None:
        pbx = self._make_pbx()
        pbx._build_wav_file.return_value = b"RIFF_WAV_DATA"
        handler = VoicemailHandler(pbx)
        handler._send_bye_to_caller = MagicMock()

        call = self._make_call()
        recorder = MagicMock()
        recorder.get_recorded_audio.return_value = b"\x00" * 1000
        recorder.get_duration.return_value = 5
        call.voicemail_recorder = recorder
        pbx.call_manager.get_call.return_value = call

        handler.complete_voicemail_recording(CALL_ID)

        pbx.voicemail_system.save_message.assert_called_once()
        handler._send_bye_to_caller.assert_called_once_with(call, CALL_ID)
        pbx.end_call.assert_called_with(CALL_ID)
