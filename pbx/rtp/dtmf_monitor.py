"""
Unified DTMF monitoring for IVR call legs (auto attendant, voicemail).

A phone may deliver a keypress three different ways, and negotiation only
says what it *should* do -- the wire shows what it *did*:

  1. RFC 2833 telephone-event RTP packets (the common VoIP default),
  2. SIP INFO messages,
  3. in-band audio tones (analog gateways, misbehaving phones).

Before this module, every IVR loop hand-assembled an RFC2833Receiver, an
RTPRecorder, a DTMFDetector, G.711 decoding, debounce, and buffer-clearing --
and had to remember to check two digit sources in the right order.
DTMFMonitor merges all three sources behind one queue:

    recorder, monitor = build_ivr_dtmf_channel(pbx, call, call_id, port)
    digit = monitor.get_digit(timeout=1.0)      # any source, deduped
    player.play_file(f, interrupt_check=monitor.has_digit)   # barge-in

Out-of-band digits (RFC 2833 via the wired receiver, and SIP INFO via the
SIP server) both land on ``call.dtmf_info_queue`` through
``PBXCore.handle_dtmf_info``; the monitor drains that queue. In-band tones
are detected from the G.711 audio packets the RTPRecorder feeds in via
``on_audio_packet``. Per-packet payload-type dispatch makes this robust to
phones that send DTMF on the payload type they offered rather than the one
we answered, or that send both out-of-band events and audible tones for the
same keypress (collapsed by the dedupe window).

In-band detection only applies to G.711 (PT 0/8) audio; wideband/compressed
codecs would need a decoder, and phones using them do RFC 2833 in practice.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from pbx.rtp.handler import RTPRecorder
from pbx.rtp.rfc2833 import RFC2833Receiver
from pbx.utils.audio import g711_to_float_samples
from pbx.utils.dtmf import DTMFDetector
from pbx.utils.logger import get_logger

# G.711 payload types eligible for in-band tone detection.
_G711_PAYLOAD_TYPES = (0, 8)


class DTMFMonitor:git
    """Single digit source merging RFC 2833 / SIP INFO / in-band DTMF.

    Thread model: ``on_audio_packet`` is called from the RTPRecorder's
    receive thread and only appends bytes (cheap, bounded). Detection and
    out-of-band draining happen lazily in the consumer's thread inside
    ``get_digit``/``has_digit``/``peek_digits``, throttled to the same
    ~10 Hz cadence the voicemail IVR proved out on real phones.
    """

    def __init__(
        self,
        call: Any,
        sample_rate: int = 8000,
        debounce_seconds: float = 0.5,
        window_packets: int = 40,
        min_window_bytes: int = 1600,
    ) -> None:
        """
        Initialize the monitor.

        Args:
            call: Call object; out-of-band digits are drained from its
                ``dtmf_info_queue`` (populated by PBXCore.handle_dtmf_info).
            sample_rate: Audio sample rate for in-band detection (Hz).
            debounce_seconds: Window in which a repeat *in-band* detection of
                the same digit is dropped (echo / lingering tone), and an
                in-band echo of an out-of-band digit is suppressed.
                Out-of-band digits are never debounced -- a deliberate
                double-press always counts.
            window_packets: In-band analysis window, in 20 ms G.711 packets
                (40 packets = 0.8 s of audio, matching the voicemail IVR).
            min_window_bytes: Minimum buffered audio before running detection.
        """
        self.call = call
        self.logger = get_logger()
        self.debounce_seconds = debounce_seconds
        self.min_window_bytes = min_window_bytes
        self._window_bytes = window_packets * 160  # 160 bytes per 20ms G.711 packet

        self._detector = DTMFDetector(sample_rate=sample_rate)
        self._lock = threading.Lock()
        self._digits: list[str] = []  # merged, ready-to-consume digits
        self._audio = bytearray()  # G.711 bytes pending in-band analysis
        self._audio_pt: int = 0  # payload type of buffered audio (0=PCMU, 8=PCMA)

        # Debounce/dedupe state: last digit emitted from any source.
        self._last_emitted_digit: str | None = None
        self._last_emitted_time: float = 0.0

        # Throttle for the (comparatively expensive) in-band detection pass,
        # so barge-in predicates polled 50x/s stay cheap.
        self._last_inband_scan: float = 0.0
        self._inband_scan_interval: float = 0.1

    # ------------------------------------------------------------------
    # Producer side (RTPRecorder receive thread)
    # ------------------------------------------------------------------

    def on_audio_packet(self, payload_type: int, payload: bytes) -> None:
        """Feed one RTP audio payload for in-band analysis (recorder thread).

        Non-G.711 payloads are ignored: in-band detection is not possible
        without a codec decoder, and such endpoints use RFC 2833 in practice.
        """
        if payload_type not in _G711_PAYLOAD_TYPES:
            return
        with self._lock:
            self._audio_pt = payload_type
            self._audio.extend(payload)
            # Bound memory: keep at most one analysis window of backlog.
            if len(self._audio) > 2 * self._window_bytes:
                del self._audio[: len(self._audio) - self._window_bytes]

    # ------------------------------------------------------------------
    # Consumer side (IVR thread / player barge-in predicate)
    # ------------------------------------------------------------------

    def get_digit(self, timeout: float = 1.0) -> str | None:
        """Return the next digit from any source, or None after timeout."""
        deadline = time.time() + timeout
        while True:
            self._poll()
            with self._lock:
                if self._digits:
                    return self._digits.pop(0)
            if time.time() >= deadline:
                return None
            time.sleep(0.05)

    def has_digit(self) -> bool:
        """Peek: is a digit pending from any source? (barge-in predicate).

        Does not consume -- the IVR loop still pops the digit via
        ``get_digit`` and advances its state machine.
        """
        self._poll()
        with self._lock:
            return bool(self._digits)

    def peek_digits(self) -> tuple[str, ...]:
        """Peek at all pending digits without consuming them.

        Lets state-dependent barge-in predicates look for a specific digit,
        e.g. voicemail PIN entry interrupts only on '#'.
        """
        self._poll()
        with self._lock:
            return tuple(self._digits)

    def clear(self) -> None:
        """Drop all pending digits and buffered audio (fresh start)."""
        with self._lock:
            self._digits.clear()
            self._audio.clear()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _poll(self) -> None:
        """Drain out-of-band digits and (throttled) scan in-band audio."""
        self._drain_out_of_band()

        now = time.time()
        if now - self._last_inband_scan >= self._inband_scan_interval:
            self._last_inband_scan = now
            self._scan_inband()

    def _drain_out_of_band(self) -> None:
        """Move RFC 2833 / SIP INFO digits from the call queue to ours."""
        queue = getattr(self.call, "dtmf_info_queue", None)
        if not queue:
            return
        while queue:
            digit = queue.pop(0)
            with self._lock:
                self._digits.append(digit)
                # The same keypress may also leak into the audio as a tone;
                # drop buffered audio so it isn't re-detected in-band.
                self._audio.clear()
                self._last_emitted_digit = digit
                self._last_emitted_time = time.time()
            self.logger.info(f"DTMF (out-of-band): '{digit}'")

    def _scan_inband(self) -> None:
        """Run tone detection over the buffered audio window."""
        with self._lock:
            if len(self._audio) <= self.min_window_bytes:
                return
            window = bytes(self._audio[-self._window_bytes :])
            audio_pt = self._audio_pt

        try:
            # Decode companded G.711 to linear samples first; raw µ-law/A-law
            # bytes fed to the detector scramble into spurious digits.
            samples = g711_to_float_samples(window, audio_pt)
            # detect_sequence slides a frame across the whole window
            # (detect_tone alone would only inspect the first ~25 ms).
            sequence = self._detector.detect_sequence(samples)
            digit = sequence[-1] if sequence else None
        except (KeyError, TypeError, ValueError) as e:
            self.logger.error(f"In-band DTMF detection error: {e}")
            return

        if not digit:
            return

        now = time.time()
        with self._lock:
            # Debounce: repeat of the last emitted digit (from any source)
            # within the window is an echo, not a new keypress.
            if (
                digit == self._last_emitted_digit
                and (now - self._last_emitted_time) < self.debounce_seconds
            ):
                # Drop the buffered tone too, or the same window would be
                # rescanned (and re-debounced) every scan interval.
                self._audio.clear()
                return
            self._digits.append(digit)
            self._last_emitted_digit = digit
            self._last_emitted_time = now
            # Start the next detection from fresh audio.
            self._audio.clear()
        self.logger.info(f"DTMF (in-band audio): '{digit}'")


def build_ivr_dtmf_channel(
    pbx_core: Any, call: Any, call_id: str, local_port: int
) -> tuple[RTPRecorder, DTMFMonitor]:
    """Build the fully-wired receive side of an IVR leg.

    Assembles the pieces every IVR needs and returns them ready to start:
    an RTPRecorder that owns the socket, delegates telephone-event packets
    to an RFC2833Receiver (accepting both our negotiated payload type and
    any telephone-event PTs from the caller's own SDP offer, since many
    phones send on the PT they offered), and feeds audio packets to a
    DTMFMonitor for in-band detection.

    Args:
        pbx_core: PBXCore instance (for config and DTMF routing).
        call: Call object (source of caller SDP info and the digit queue).
        call_id: Call identifier.
        local_port: Local RTP port for this leg.

    Returns:
        (recorder, monitor) -- call ``recorder.start()`` to begin receiving;
        every digit then arrives via ``monitor.get_digit()``.
    """
    dtmf_pt = pbx_core._get_dtmf_payload_type()

    caller_dtmf_pts: set[int] = set()
    rtpmap_names = (getattr(call, "caller_rtp", None) or {}).get("rtpmap_names") or {}
    for pt_str, rtpmap_name in rtpmap_names.items():
        if str(rtpmap_name).lower().startswith("telephone-event") and str(pt_str).isdigit():
            caller_dtmf_pts.add(int(pt_str))

    # Constructed but not started: the recorder owns the socket and hands
    # telephone-event packets to the receiver, which routes digits into
    # call.dtmf_info_queue via PBXCore.handle_dtmf_info -- where the
    # monitor's out-of-band drain picks them up alongside SIP INFO digits.
    rfc2833_rx = RFC2833Receiver(
        local_port=local_port,
        pbx_core=pbx_core,
        call_id=call_id,
        payload_type=dtmf_pt,
        extra_payload_types=caller_dtmf_pts,
    )
    monitor = DTMFMonitor(call)
    recorder = RTPRecorder(
        local_port,
        call_id,
        rfc2833_handler=rfc2833_rx,
        dtmf_payload_type=dtmf_pt,
        extra_dtmf_payload_types=caller_dtmf_pts,
        dtmf_monitor=monitor,
    )
    return recorder, monitor
