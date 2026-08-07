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

#: L16 mono. The notice is already PCM, so this avoids an encode/decode round trip that
#: would only lose fidelity on the one piece of audio that might be read out in court.
PAYLOAD_L16 = 11

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
        """Send the notice to each named side over the relay's own socket."""
        from pbx.rtp.handler import RTPPlayer

        path = self.audio_path()
        if not path.is_file():
            self.logger.error(f"Recording notice audio missing at {path}")
            return False

        sock = getattr(handler, "socket", None)
        if sock is None or not getattr(handler, "running", False):
            self.logger.warning(f"Recording notice for {handler.call_id}: relay is not running")
            return False

        # Success means at least one leg heard it. Requiring both would discard a perfectly
        # lawful recording whenever one endpoint had not been learned yet -- and the leg that
        # matters legally is the outside one, which is always among these.
        played = False
        for target_side in sides:
            target = handler.get_endpoint(target_side)
            if target is None:
                self.logger.warning(
                    f"Recording notice for {handler.call_id}: side {target_side} has no endpoint"
                )
                continue

            # Reuse the relay's bound socket, exactly as music-on-hold does, so no second
            # bind is needed on the same port.
            player = RTPPlayer(
                local_port=handler.local_port,
                remote_host=target[0],
                remote_port=target[1],
                call_id=handler.call_id,
                external_socket=sock,
            )
            player.start()
            try:
                # The tap lives on _relay_loop, which this bypasses, so the notice would
                # otherwise be absent from its own recording. Feeding it here is what makes
                # the tape self-evidencing.
                self._feed_tap(handler, path)
                player.play_file(path)
                played = True
            finally:
                player.stop()

        return played

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
