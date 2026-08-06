#!/usr/bin/env python3
"""
Transcribe a call recording and print the dialogue.

The recording pipeline's diagnostic: it runs the same per-channel path a finished call runs,
but inline and without a database, so you can check segmentation and attribution on a real
file instead of placing a call and waiting for a row to appear.

Needs the ``.json`` sidecar the recorder writes beside the ``.wav`` -- that is what says which
channel is whom. Without it there is nothing to attribute to.

Usage::

    python scripts/transcribe_recording.py --file recordings/1001_to_1002_20260806_143022_abc.wav
    python scripts/transcribe_recording.py -f call.wav --segments
    python scripts/transcribe_recording.py -f call.wav --quiet > transcript.txt

The transcript goes to stdout and everything else to stderr, so redirecting stdout gives a
clean text file.

Exit codes: 0 transcribed, 1 nothing recognised, 2 bad usage or missing dependency.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

try:
    from pbx.speech import TranscriptionSettings, TranscriptionWorker, build_backend
    from pbx.speech.recording import RecordingTranscriber
    from pbx.speech.settings import CONFIG_SECTION as TRANSCRIPTION_SECTION
except ImportError as exc:  # pragma: no cover - depends on the environment
    print(f"error: could not import the PBX speech package ({exc})", file=sys.stderr)
    raise SystemExit(2) from exc


def _log(message: str, *, quiet: bool = False) -> None:
    """Diagnostics go to stderr so stdout stays a clean transcript."""
    if not quiet:
        print(message, file=sys.stderr)


class _Capture:
    """Stands in for TranscriptStore so the script needs no database."""

    def __init__(self) -> None:
        self.transcript: Any | None = None

    def save(self, transcript: Any, **_kwargs: Any) -> bool:
        self.transcript = transcript
        return True


def _build_settings(args: argparse.Namespace) -> TranscriptionSettings:
    """Config section first, then command-line overrides. workers=0 runs inline."""
    section: dict[str, object] = {}
    if Path(args.config).exists():
        try:
            from pbx.utils.config import Config

            section = dict(Config(args.config).get(TRANSCRIPTION_SECTION, {}) or {})
        except Exception as exc:  # A broken config.yml must not stop a diagnostic.
            _log(f"note: could not read {args.config} ({exc}); using defaults")

    if args.provider:
        section["provider"] = args.provider
    if args.model:
        resolved = str(section.get("provider", "vosk")).lower()
        key = "whisper_model" if resolved.endswith("whisper") else "vosk_model_path"
        section[key] = args.model
    if args.model_dir:
        section["whisper_model_dir"] = args.model_dir
    if args.vad_threshold is not None:
        section["whisper_vad_threshold"] = args.vad_threshold
    if args.min_silence_ms is not None:
        section["whisper_vad_min_silence_ms"] = args.min_silence_ms

    # Forced on: the feature ships disabled, and a diagnostic that refuses to run because a
    # flag is off is not a diagnostic. workers=0 keeps everything on this thread.
    section.update({"enabled": True, "workers": 0})
    return TranscriptionSettings.from_dict(section)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Transcribe a call recording per participant and print the dialogue.",
        epilog="Requires the .json sidecar the recorder writes next to the .wav.",
    )
    parser.add_argument("--file", "-f", required=True, help="Path to the recording WAV")
    parser.add_argument(
        "--provider",
        choices=["vosk", "faster-whisper", "whisper"],
        help="Engine to use (default: from config.yml)",
    )
    parser.add_argument(
        "--model",
        help="Vosk model directory, or the whisper model name (default: from config.yml)",
    )
    parser.add_argument(
        "--model-dir", help="Directory whisper models are staged in (default: from config.yml)"
    )
    parser.add_argument("--config", default="config.yml", help="Path to config.yml")
    parser.add_argument(
        "--silence-floor",
        type=float,
        default=None,
        help="RMS below which a channel counts as silent and is skipped entirely",
    )
    parser.add_argument(
        "--vad-threshold",
        type=float,
        default=None,
        help="Silero speech probability, 0.0-1.0 (lower catches quieter speech)",
    )
    parser.add_argument(
        "--min-silence-ms",
        type=int,
        default=None,
        help="Silence before Silero ends a speech chunk; lower splits turns more readily",
    )
    parser.add_argument(
        "--segments",
        action="store_true",
        help="Also print the raw segment timings, for diagnosing grouping",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="Suppress diagnostics; print only the transcript"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    quiet = args.quiet

    path = Path(args.file)
    if not path.is_file():
        print(f"error: no such file: {path}", file=sys.stderr)
        return 2

    sidecar = path.with_suffix(".json")
    if not sidecar.is_file():
        print(
            f"error: no sidecar at {sidecar}\n"
            "       The recorder writes it beside the .wav; without it there is no way to "
            "know which channel is whom.",
            file=sys.stderr,
        )
        return 2

    manifest = json.loads(sidecar.read_text())
    channels = manifest.get("channels", [])
    _log(f"Recording : {path.name}", quiet=quiet)
    _log(f"Session   : {manifest.get('session_id', '?')}", quiet=quiet)
    _log(f"Channels  : {', '.join(c.get('label', '?') for c in channels)}", quiet=quiet)

    settings = _build_settings(args)
    for problem in settings.validate():
        _log(f"config: {problem}", quiet=quiet)

    backend = build_backend(settings)
    if not getattr(backend, "ready", False):
        print(
            f"error: the {settings.provider} engine did not load. See the warning above.",
            file=sys.stderr,
        )
        return 2

    floor = args.silence_floor if args.silence_floor is not None else settings.silence_rms_floor
    _log(f"Engine    : {settings.provider} ({getattr(backend, 'model_name', '?')})", quiet=quiet)
    _log(f"Floor     : {floor:.0f} RMS below which a channel is skipped", quiet=quiet)
    _log("Transcribing, one pass per channel...", quiet=quiet)

    worker = TranscriptionWorker(backend, settings)
    capture = _Capture()
    transcriber = RecordingTranscriber(worker=worker, store=capture, silence_floor=floor)

    transcriber.submit(path)

    if capture.transcript is None:
        print("error: nothing was recognised on any channel.", file=sys.stderr)
        return 1

    transcript = capture.transcript
    _log(
        f"Done      : {len(transcript.segments)} segment(s), "
        f"{transcript.processing_duration:.1f}s of processing\n",
        quiet=quiet,
    )

    print(transcript.text)

    if args.segments:
        print("\n--- segments ---", file=sys.stderr)
        for segment in transcript.segments:
            print(
                f"  {segment.start:7.2f} - {segment.end:7.2f}  "
                f"{segment.speaker or '?':>12}  {segment.text}",
                file=sys.stderr,
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
