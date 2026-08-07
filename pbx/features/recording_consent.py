"""
Telling people the call is being recorded.

Recording someone without notice is unlawful in two-party-consent jurisdictions, so this is
the gate the recorder was always supposed to sit behind. It plays a spoken notice on a
bridged call, and **the recording is discarded if the notice did not play** -- no notice,
no lawful recording, so there is nothing worth keeping.

*Which calls* get a notice is a policy question (``announce_for``), but *who hears it* is
not: it plays to both legs, always. Only the outside party needs the disclosure -- staff
have signed a phone agreement -- but the notice is also the thing that tells the employee
why the line is quiet. Playing it to the outside party alone means muting the employee so
they cannot talk over it, which leaves them listening to dead air and saying "hello?"
into a muted channel. Letting them hear it costs four seconds of a message they do not
strictly need and removes that problem entirely.

Three things make this less obvious than it sounds:

**It runs on its own thread.** The bridge hook fires from the relay's own loop, and blocking
there for four seconds would stall live media for every call on the box. The announcement is
started and the hook returns immediately; the fail-closed decision happens on the announcement
thread when playback finishes.

**Injected audio is invisible to the recorder.** ``RTPPlayer`` writes straight to the socket,
while the tap is fed inside ``_relay_loop`` -- two different code paths. Music-on-hold has
always been absent from recordings for this reason. So the notice is fed to the tap explicitly,
on its own source, which is what puts it on the tape as evidence that it was given.

**The text is not transcribed, it is injected.** We already know what the notice says; it is
the configured string that generated the audio. Recognising it would cost a full whisper window
to recover a sentence we have, and could come back paraphrased -- which is the last sentence
you would want approximated if the recording is ever evidence.
"""

from __future__ import annotations

import contextlib
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pbx.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "ANNOUNCE_ALL",
    "ANNOUNCE_EXTERNAL",
    "ANNOUNCE_OFF",
    "SYSTEM_LABEL",
    "SYSTEM_SOURCE",
    "ConsentAnnouncer",
    "ConsentSettings",
]

CONFIG_SECTION = "recording.consent"

#: Announce on calls with a party that is not a provisioned extension. The production setting.
ANNOUNCE_EXTERNAL = "external"
#: Announce on every bridged call, internal ones included. The testing setting.
ANNOUNCE_ALL = "all"
#: Announce on nothing. Recording still happens -- this says an operator decided notice is not
#: required here (a one-party-consent jurisdiction, or internal-only recording), out loud,
#: rather than by leaving a switch off somewhere.
ANNOUNCE_OFF = "off"

VALID_MODES = frozenset({ANNOUNCE_EXTERNAL, ANNOUNCE_ALL, ANNOUNCE_OFF})

#: The notice's own channel in the recording. Not a participant, so it gets a source id that
#: cannot collide with the relay's ("a0"/"b0", bumped per transfer).
SYSTEM_SOURCE = "sys0"
SYSTEM_LABEL = "system"

DEFAULT_TEXT = "This call is being recorded for quality assurance."

#: 20 ms at 8 kHz -- the same framing everything else on the media path uses.
FRAME_SAMPLES = 160

#: L16 mono, used only to hand the notice to the recorder -- no encode/decode round trip
#: on the one piece of audio that might be read out in court.
PAYLOAD_L16 = 11

#: What goes on the wire. PT 0 is the one codec every endpoint here negotiates.
PAYLOAD_ULAW = 0

SAMPLE_RATE = 8000
FRAME_INTERVAL_SECONDS = 0.02

#: Where a synthesised notice is cached. Generated once at startup, not per call: gTTS is a
#: network round trip and this is on the path of every external call.
DEFAULT_AUDIO_FILE = "auto_attendant/recording_notice.wav"

#: Give up on playback after this. A notice that never finishes would otherwise hold the
#: recording in limbo for the length of the call.
PLAYBACK_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class ConsentSettings:
    """Read once at construction, like every other settings object here."""

    announce_for: str = ANNOUNCE_EXTERNAL
    text: str = DEFAULT_TEXT
    audio_file: str = DEFAULT_AUDIO_FILE

    @classmethod
    def from_dict(cls, section: dict[str, Any]) -> ConsentSettings:
        return cls(
            announce_for=str(section.get("announce_for", ANNOUNCE_EXTERNAL) or "").lower()
            or ANNOUNCE_EXTERNAL,
            text=str(section.get("text") or DEFAULT_TEXT),
            audio_file=str(section.get("audio_file") or DEFAULT_AUDIO_FILE),
        )

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.announce_for not in VALID_MODES:
            problems.append(
                f"{CONFIG_SECTION}.announce_for must be one of {sorted(VALID_MODES)}, "
                f"got {self.announce_for!r}"
            )
        if self.announce_for != ANNOUNCE_OFF and not self.text.strip():
            problems.append(
                f"{CONFIG_SECTION}.text is empty; it is both what callers hear and what the "
                "transcript records as having been said"
            )
        return problems


class ConsentAnnouncer:
    """
    Decides whether a call needs a notice, plays it, and reports whether it was given.

    Constructed unconditionally so callers get a real object rather than something to guard
    with hasattr -- a disabled announcer simply says no call needs a notice.
    """

    def __init__(
        self,
        settings: ConsentSettings,
        is_internal: Callable[[str], bool] | None = None,
        logger: Any | None = None,
    ) -> None:
        self.settings = settings
        self.logger = logger or get_logger()
        # Injected rather than reached for, so this module never imports PBXCore. Defaults to
        # "everyone is external", which is the cautious direction: it announces more, not less.
        self._is_internal = is_internal or (lambda _number: False)
        self.announced = 0
        self.failed = 0

    # ------------------------------------------------------------------ policy

    @property
    def enabled(self) -> bool:
        return self.settings.announce_for != ANNOUNCE_OFF

    def audio_path(self) -> Path:
        return Path(self.settings.audio_file)

    def required_for(self, caller: str, callee: str) -> bool:
        """Whether this call may not be recorded without a notice first."""
        if not self.enabled:
            return False
        if self.settings.announce_for == ANNOUNCE_ALL:
            return True
        return self.is_external_call(caller, callee)

    def is_external_call(self, caller: str, callee: str) -> bool:
        """
        True when either party is not a provisioned extension.

        Identity by extension lookup rather than by a flag on the call: it is the same
        question the recorder already answers when it labels a channel, so the two cannot
        disagree about who was on the call.
        """
        return not (self._is_internal(caller) and self._is_internal(callee))

    # ------------------------------------------------------------------ playback

    def prepare(self) -> bool:
        """
        Make sure there is something to play, synthesising it once if needed.

        Called at startup so a missing prompt is a loud line in the boot log rather than a
        silent loss of every external recording later.
        """
        if not self.enabled:
            return True

        path = self.audio_path()
        if path.is_file():
            self.logger.info(f"Recording notice: using {path}")
            return True

        try:
            from pbx.utils.tts import text_to_wav_telephony

            path.parent.mkdir(parents=True, exist_ok=True)
            if text_to_wav_telephony(self.settings.text, path):
                self.logger.info(f"Recording notice: synthesised {path}")
                return True
        except Exception as e:
            self.logger.error(f"Recording notice could not be synthesised: {e}")

        self.logger.error(
            f"Recording notice audio is missing ({path}) and could not be generated. "
            "External calls will NOT be recorded until this is fixed."
        )
        return False

    def announce(self, handler: Any, on_complete: Callable[[bool], None]) -> None:
        """
        Play the notice to both legs, then report success through `on_complete`.

        Returns immediately. The caller is the relay's bridge hook, which runs on the media
        loop -- four seconds of playback there would stall audio for the call.
        """
        thread = threading.Thread(
            target=self._play,
            args=(handler, on_complete),
            name=f"ConsentNotice-{getattr(handler, 'call_id', '?')}",
            daemon=True,
        )
        thread.start()

    def _play(self, handler: Any, on_complete: Callable[[bool], None]) -> None:
        """Play to both legs. Never raises -- it owns a thread of its own."""
        ok = False
        try:
            ok = self._play_to_sides(handler, ["a", "b"])
        except Exception as e:
            self.logger.error(f"Recording notice for {handler.call_id} failed: {e}")
        finally:
            if ok:
                self.announced += 1
            else:
                self.failed += 1
            try:
                on_complete(ok)
            except Exception as e:
                self.logger.error(f"Recording notice callback for {handler.call_id} raised: {e}")

    def _play_to_sides(self, handler: Any, sides: list[str]) -> bool:
        """
        Send the notice to every leg at once, with the relay muted while it plays.

        Two things here were learned the hard way.

        **The relay is paused first.** Injecting a second RTP stream into a live session means
        two SSRCs arriving on one port, and phones lock onto whichever they saw first -- so the
        notice reached one end, the other end, or neither, differing per call. Pausing leaves
        exactly one stream on the wire. It also mutes both microphones for the duration, so
        nobody talks over the notice.

        **Both legs are driven from one loop.** Playing to them in turn meant the second party
        heard the notice only after the first had finished it.
        """
        path = self.audio_path()
        if not path.is_file():
            self.logger.error(f"Recording notice audio missing at {path}")
            return False

        sock = getattr(handler, "socket", None)
        if sock is None or not getattr(handler, "running", False):
            self.logger.warning(f"Recording notice for {handler.call_id}: relay is not running")
            return False

        targets = [t for t in (handler.get_endpoint(side) for side in sides) if t is not None]
        if not targets:
            self.logger.warning(f"Recording notice for {handler.call_id}: no endpoints learned")
            return False

        payload = self._ulaw_payload(path)
        if payload is None:
            return False

        handler.pause_relay()
        try:
            self._feed_tap(handler, path)
            self._stream(sock, targets, payload)
        finally:
            handler.resume_relay()

        return True

    def _ulaw_payload(self, path: Path) -> bytes | None:
        """
        Read the notice as G.711 µ-law.

        µ-law rather than whatever the file happens to hold: PT 0 is the one codec every
        endpoint here negotiates. ``RTPPlayer.play_file`` re-encodes 16-bit PCM to G.722, which
        a phone that agreed on µ-law cannot decode -- that alone made the notice inaudible.
        """
        import wave

        from pbx.utils.audio import pcm16_to_ulaw, resample_pcm16

        try:
            with wave.open(str(path), "rb") as wav:
                channels = wav.getnchannels()
                width = wav.getsampwidth()
                rate = wav.getframerate()
                raw = wav.readframes(wav.getnframes())
        except Exception as e:
            self.logger.error(f"Recording notice audio is unreadable ({path}): {e}")
            return None

        if channels != 1:
            self.logger.error(f"Recording notice audio must be mono ({path})")
            return None
        if width == 1:
            # Already 8-bit; assume it is the µ-law a telephony prompt normally is.
            return raw
        if width != 2:
            self.logger.error(f"Recording notice audio must be 8- or 16-bit ({path})")
            return None

        if rate != SAMPLE_RATE:
            raw = resample_pcm16(raw, rate, SAMPLE_RATE)
        return pcm16_to_ulaw(raw)

    def _stream(self, sock: Any, targets: list[tuple[str, int]], payload: bytes) -> None:
        """
        Packetise once and send each packet to every target, paced at 20 ms.

        One loop rather than one player per leg, so the legs stay sample-aligned instead of
        drifting apart by however long the first one took.
        """
        import random
        import struct
        import time

        ssrc = random.getrandbits(32)
        sequence = random.getrandbits(16)
        timestamp = 0
        next_send = time.monotonic()

        for start in range(0, len(payload), FRAME_SAMPLES):
            chunk = payload[start : start + FRAME_SAMPLES]
            header = struct.pack("!BBHII", 0x80, PAYLOAD_ULAW, sequence & 0xFFFF, timestamp, ssrc)
            for target in targets:
                with contextlib.suppress(OSError):
                    sock.sendto(header + chunk, target)

            sequence += 1
            timestamp += FRAME_SAMPLES
            next_send += FRAME_INTERVAL_SECONDS
            delay = next_send - time.monotonic()
            if delay > 0:
                time.sleep(delay)

    def _feed_tap(self, handler: Any, path: Path) -> None:
        """
        Put the notice on the recording, on its own channel.

        Fed as 20 ms frames with advancing timestamps rather than one blob, because a channel
        places audio by RTP timestamp -- one frame claiming to be seconds long would be written
        contiguously but would misreport where it sits relative to the speakers.

        Best-effort throughout: failing to record the notice is not a reason to withhold it
        from the person it protects, so nothing here stops playback.
        """
        tap = getattr(handler, "tap", None)
        if tap is None:
            return

        try:
            import wave

            from pbx.rtp.tap import RtpFrame

            with wave.open(str(path), "rb") as wav:
                if wav.getsampwidth() != 2 or wav.getnchannels() != 1:
                    self.logger.debug("Recording notice is not 16-bit mono; not added to the tape")
                    return
                pcm = wav.readframes(wav.getnframes())
        except Exception as e:
            self.logger.debug(f"Recording notice not added to the tape: {e}")
            return

        step = FRAME_SAMPLES * 2
        try:
            for index, start in enumerate(range(0, len(pcm), step)):
                tap.feed_frame(
                    RtpFrame(
                        source=SYSTEM_SOURCE,
                        timestamp=index * FRAME_SAMPLES,
                        payload_type=PAYLOAD_L16,
                        sequence=index,
                        payload=pcm[start : start + step],
                    )
                )
        except Exception as e:
            self.logger.debug(f"Recording notice not added to the tape: {e}")

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "announce_for": self.settings.announce_for,
            "audio_file": str(self.audio_path()),
            "audio_present": self.audio_path().is_file(),
            "announced": self.announced,
            "failed": self.failed,
            "text": self.settings.text,
        }
