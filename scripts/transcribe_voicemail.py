#!/usr/bin/env python3
"""
Transcribe a single voicemail WAV and print the text.

A diagnostic for the two things that break transcription independently: the audio format and
the Vosk model. It reports what the file actually is *before* transcribing, because the
long-standing failure here was silent -- voicemail is stored as G.711 u-law, Python's `wave`
module supports only linear PCM, and every real message failed with "unknown format: 7". If
this script reports a u-law file and still produces text, that path is genuinely fixed.

Transcription is forced on regardless of ``features.voicemail_transcription.enabled``, which
ships false, so this works on a box where the feature has not been switched on yet.

Usage::

    python scripts/transcribe_voicemail.py --file voicemail/1001/555_20260803_101500.wav
    python scripts/transcribe_voicemail.py -f message.wav --model /opt/warden/models/vosk-model-small-en-us-0.15
    python scripts/transcribe_voicemail.py -f message.wav --quiet > transcript.txt

The transcript goes to stdout and everything else to stderr, so redirecting stdout gives a
clean text file.

Exit codes: 0 transcribed, 1 failed, 2 bad usage or missing dependency.
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

try:
    from pbx.speech import TranscriptionSettings, build_backend
    from pbx.speech.backends.vosk import VOSK_AVAILABLE
    from pbx.speech.settings import (
        DEFAULT_LANGUAGE,
        DEFAULT_MAX_AUDIO_SECONDS,
        DEFAULT_VOSK_MODEL_PATH,
    )
    from pbx.utils.audio import read_wav_as_pcm16
except ModuleNotFoundError as exc:
    _venv = _REPO_ROOT / ".venv" / "bin" / "python"
    _script = sys.argv[0] or str(Path(__file__))
    _hint = f"{_venv} {_script}" if _venv.exists() else f"make install, then rerun {_script}"
    print(
        f"error: missing dependency {exc.name!r}.\n"
        f"This script needs the project's virtualenv. Try:\n"
        f"  {_hint} --file message.wav\n",
        file=sys.stderr,
    )
    raise SystemExit(2) from exc


#: WAV format codes, for reporting. Kept local because this is the one place we want to name
#: a format we cannot decode -- that is precisely the diagnostic.
_FORMAT_NAMES = {
    1: "linear PCM",
    6: "G.711 A-law",
    7: "G.711 u-law",
    0x0067: "G.722",
}


def _log(message: str = "") -> None:
    """Diagnostics go to stderr so stdout stays a clean transcript."""
    print(message, file=sys.stderr)


def _describe_wav(path: Path) -> dict[str, Any] | None:
    """
    Read just the fmt chunk, for reporting.

    Deliberately separate from read_wav_as_pcm16: this has to describe files that the reader
    refuses, since knowing *which* format was refused is the whole point of running this.
    """
    try:
        with path.open("rb") as f:
            if f.read(4) != b"RIFF":
                return None
            f.read(4)
            if f.read(4) != b"WAVE":
                return None
            while True:
                header = f.read(8)
                if len(header) < 8:
                    return None
                chunk_id, chunk_size = struct.unpack("<4sI", header)
                payload = f.read(chunk_size)
                if chunk_size % 2:
                    f.read(1)
                if chunk_id == b"fmt " and len(payload) >= 16:
                    audio_format, channels, sample_rate = struct.unpack("<HHI", payload[:8])
                    bits = struct.unpack("<H", payload[14:16])[0]
                    return {
                        "format": audio_format,
                        "format_name": _FORMAT_NAMES.get(audio_format, "unknown"),
                        "channels": channels,
                        "sample_rate": sample_rate,
                        "bits": bits,
                    }
    except OSError:
        return None
    return None


def _resolve_model_path(args: argparse.Namespace) -> str:
    """--model wins, then config.yml, then the built-in default."""
    if args.model:
        return args.model

    config_path = Path(args.config)
    if config_path.exists():
        try:
            from pbx.utils.config import Config

            configured = Config(args.config).get("features.voicemail_transcription.vosk_model_path")
            if configured:
                return str(configured)
        except Exception as exc:  # A broken config.yml must not stop a diagnostic.
            _log(f"note: could not read {args.config} ({exc}); using the built-in default")

    return DEFAULT_VOSK_MODEL_PATH


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Transcribe a voicemail WAV and print the text to stdout.",
        epilog="Voicemail files live under the voicemail/<extension>/ directory.",
    )
    parser.add_argument("-f", "--file", required=True, help="Path to the WAV file")
    parser.add_argument(
        "--model",
        help="Path to the Vosk model directory (default: from config.yml, else models/...)",
    )
    parser.add_argument("--config", default="config.yml", help="Path to config.yml")
    parser.add_argument(
        "--language", default=None, help=f"Language code (default: {DEFAULT_LANGUAGE})"
    )
    parser.add_argument(
        "--max-seconds",
        type=int,
        default=DEFAULT_MAX_AUDIO_SECONDS,
        help=f"Refuse audio longer than this (default: {DEFAULT_MAX_AUDIO_SECONDS})",
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="Suppress diagnostics; print only the text"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    quiet = args.quiet

    audio_path = Path(args.file).expanduser()
    if not audio_path.is_file():
        print(f"error: no such file: {audio_path}", file=sys.stderr)
        return 2

    model_path = _resolve_model_path(args)
    language = args.language or DEFAULT_LANGUAGE

    if not quiet:
        _log(f"File:   {audio_path}")
        _log(f"Size:   {audio_path.stat().st_size:,} bytes")

        described = _describe_wav(audio_path)
        if described:
            _log(
                f"Format: {described['format_name']} "
                f"(code {described['format']}), "
                f"{described['channels']} ch, "
                f"{described['sample_rate']} Hz, "
                f"{described['bits']}-bit"
            )
        else:
            _log("Format: could not read a WAV header -- is this really a WAV file?")

        # Decode separately from transcription so a failure here is unambiguous.
        try:
            pcm16, rate = read_wav_as_pcm16(audio_path)
            _log(f"Decoded: {len(pcm16) // 2:,} samples, {len(pcm16) / 2 / rate:.1f}s of audio")
        except (OSError, ValueError) as exc:
            _log(f"Decode:  FAILED -- {exc}")
            _log("")
            _log("The audio could not be converted to linear PCM, so Vosk was never called.")
            return 1

        _log(f"Model:  {model_path}")
        if not Path(model_path).is_dir():
            _log("        ^ this directory does not exist")
            _log("")
            _log("Install it with: python scripts/install_vosk_model.py")
            return 1
        _log("")

    settings = TranscriptionSettings.from_dict(
        {
            "enabled": True,
            "provider": "vosk",
            "vosk_model_path": model_path,
            "language": language,
            "max_audio_seconds": args.max_seconds,
            "workers": 0,
        }
    )
    service = build_backend(settings)

    if service is None or not service.ready:
        # Two very different problems reach here, and the remedy for one does nothing for the
        # other. Naming the wrong one sends you off installing a 40 MB model you already have.
        if not VOSK_AVAILABLE:
            print(
                "error: the vosk library is not installed in this interpreter, so no model "
                "can be loaded.",
                file=sys.stderr,
            )
            print("Install it with: pip install vosk    (or: make install)", file=sys.stderr)
        else:
            print(
                f"error: the vosk library is present but the model at {model_path} could not "
                "be loaded -- see the log lines above.",
                file=sys.stderr,
            )
            print(
                "Reinstall it with: python scripts/install_vosk_model.py --force",
                file=sys.stderr,
            )
        return 1

    result = service.transcribe_file(audio_path, language=language)

    if not result.success:
        print(f"error: transcription failed: {result.error}", file=sys.stderr)
        return 1

    # The transcript, and only the transcript, on stdout.
    print(result.text)

    if not quiet:
        _log("")
        _log(f"Provider:   {result.provider}")
        _log(f"Language:   {result.language}")
        _log(f"Characters: {len(result.text):,}")
        _log(f"Segments:   {len(result.segments)}")
        if result.confidence is not None:
            _log(f"Confidence: {result.confidence:.1%}")
        if result.real_time_factor is not None:
            _log(
                f"Speed:      {result.processing_duration:.1f}s for "
                f"{result.audio_duration:.1f}s of audio "
                f"(RTF {result.real_time_factor:.2f})"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
