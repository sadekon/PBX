"""
Music on Hold (MOH) System
Streams looped hold audio to a held party while a call is on hold.
"""

from __future__ import annotations

import random
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pbx.utils.logger import get_logger

if TYPE_CHECKING:
    from pbx.rtp.handler import RTPRelayHandler


class MusicOnHold:
    """Manages music on hold.

    MOH drives an ``RTPRelayHandler`` it is handed: it pauses the relay so the
    two legs stop hearing each other, then streams a looped WAV to the held
    party over the relay's own socket via ``RTPPlayer``. Ending hold sets a
    stop event that interrupts playback within one packet (~20ms) and resumes
    normal relaying.
    """

    def __init__(self, moh_directory: str = "moh", default_class: str = "default") -> None:
        """
        Initialize MOH system.

        Args:
            moh_directory: Directory containing MOH files.
            default_class: Default MOH class name.
        """
        self.moh_directory = moh_directory
        self.default_class = default_class
        self.classes: dict[str, list[Path]] = {}  # class_name -> list of audio files
        self.logger = get_logger()
        self.active_sessions: dict[str, dict[str, Any]] = {}  # call_id -> session state

        Path(moh_directory).mkdir(parents=True, exist_ok=True)
        self._load_classes()

    def _load_classes(self) -> None:
        """Load MOH classes (one per subdirectory) and their audio files."""
        # Ensure the default class directory exists.
        (Path(self.moh_directory) / self.default_class).mkdir(parents=True, exist_ok=True)

        for class_path in Path(self.moh_directory).iterdir():
            if class_path.is_dir():
                audio_files = self._scan_audio_files(class_path)
                if audio_files:
                    self.classes[class_path.name] = audio_files
                    self.logger.info(
                        f"Loaded MOH class '{class_path.name}' with {len(audio_files)} files"
                    )

    def _scan_audio_files(self, directory: Path) -> list[Path]:
        """Return the sorted audio files in a directory."""
        audio_extensions = (".wav", ".mp3", ".ogg", ".flac", ".aac")
        return sorted(
            entry for entry in directory.iterdir() if entry.suffix.lower() in audio_extensions
        )

    def start_moh(
        self,
        call_id: str,
        relay_handler: RTPRelayHandler,
        held_side: str,
        moh_class: str | None = None,
    ) -> Path | None:
        """
        Put a call on hold: pause its relay and stream looped hold music to
        the held party.

        The relay is paused even when no audio file is available, so the held
        party never hears the other leg while on hold.

        Args:
            call_id: Call identifier.
            relay_handler: The call's relay handler to drive.
            held_side: Which side ("a" or "b") should hear the music.
            moh_class: MOH class to play (defaults to the configured default).

        Returns:
            The audio file being looped, or None if none was available.
        """
        moh_class = moh_class or self.default_class
        audio_files = self.classes.get(moh_class, [])

        relay_handler.pause_relay()
        stop_event = threading.Event()
        session: dict[str, Any] = {
            "class": moh_class,
            "relay_handler": relay_handler,
            "stop_event": stop_event,
            "thread": None,
            "file": None,
        }
        self.active_sessions[call_id] = session

        if not audio_files:
            self.logger.warning(
                f"No MOH files for class '{moh_class}'; holding call {call_id} in silence"
            )
            return None

        audio_file = random.choice(audio_files)
        session["file"] = audio_file
        thread = threading.Thread(
            target=self._stream_loop,
            args=(relay_handler, held_side, audio_file, stop_event),
            daemon=True,
        )
        session["thread"] = thread
        thread.start()

        self.logger.info(f"Started MOH for call {call_id}: {audio_file} -> side {held_side}")
        return audio_file

    def stop_moh(self, call_id: str) -> None:
        """
        End hold for a call: interrupt playback and resume normal relaying.

        Idempotent -- safe to call for a call that is not on hold.

        Args:
            call_id: Call identifier.
        """
        session = self.active_sessions.pop(call_id, None)
        if session is None:
            return

        session["stop_event"].set()
        thread: threading.Thread | None = session.get("thread")
        if thread and thread.is_alive():
            thread.join(timeout=1.0)
        session["relay_handler"].resume_relay()
        self.logger.info(f"Stopped MOH for call {call_id}")

    def _stream_loop(
        self,
        relay_handler: RTPRelayHandler,
        held_side: str,
        moh_file: Path,
        stop_event: threading.Event,
    ) -> None:
        """Loop ``moh_file`` to the held party until stop_event is set."""
        from pbx.rtp.handler import RTPPlayer

        sock = relay_handler.socket
        if sock is None:
            return

        # Reuse the relay's already-bound socket so no second bind is needed
        # on the same port.
        player = RTPPlayer(
            local_port=relay_handler.local_port,
            remote_host="",
            remote_port=0,
            call_id=relay_handler.call_id,
            external_socket=sock,
        )
        player.start()

        # Exit if the call is torn down (relay stopped) even without a resume.
        while not stop_event.is_set() and relay_handler.running:
            target = relay_handler.get_endpoint(held_side)
            if target is None:
                # Endpoint not learned yet; wait briefly and retry.
                stop_event.wait(0.1)
                continue
            player.remote_host, player.remote_port = target
            # Barge-in: is_set() only peeks, so hold ends within one packet.
            player.play_file(moh_file, interrupt_check=stop_event.is_set)

    def add_moh_class(self, class_name: str, files: list[Path]) -> None:
        """Register a MOH class with an explicit list of files."""
        self.classes[class_name] = files
        self.logger.info(f"Added MOH class '{class_name}' with {len(files)} files")

    def get_classes(self) -> list[str]:
        """Return the available MOH class names."""
        return list(self.classes)

    def get_class_files(self, class_name: str) -> list[Path]:
        """Return the files in a MOH class."""
        return self.classes.get(class_name, [])
