#!/usr/bin/env python3
"""
Stage a faster-whisper model where the PBX expects to find it.

The point of this script is that the PBX must **never** download a model at runtime.
``WhisperModel("small.en")`` fetches ~250 MB from HuggingFace on first use -- from a daemon
thread, on a production box, possibly with no outbound internet, the first time somebody
leaves a voicemail. Staging it here instead means a missing model is a loud startup warning
rather than a mystery stall in the middle of a call teardown.

It is idempotent: an existing, structurally valid model is left alone unless ``--force`` is
given.

Usage::

    python scripts/install_whisper_model.py
    python scripts/install_whisper_model.py --model base.en --dest /opt/warden/models/whisper/base.en
    python scripts/install_whisper_model.py --owner pbx
    python scripts/install_whisper_model.py --list

Exit codes: 0 installed or already present, 1 failed, 2 bad usage or missing dependency.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

#: Models worth offering. Sizes are the int8 on-disk size, which is what actually lands here.
#: RTF is processing seconds per audio second at cpu_threads=1, measured on the pilot host --
#: treat them as relative, not absolute. The ``.en`` variants are English-only and are both
#: faster and more accurate than the multilingual model of the same size on English audio.
_MODELS = {
    "tiny.en": "~40 MB    English. Fastest, noticeably worse punctuation and accuracy.",
    "base.en": "~75 MB    English. The fallback if small.en cannot keep up on this box.",
    "small.en": "~250 MB   English. Measured RTF ~0.47. The default, and the best trade here.",
    "medium.en": "~770 MB   English. Better again, but roughly 3x small.en -- benchmark first.",
    "small": "~250 MB   Multilingual. Use only if callers actually speak other languages.",
}

_DEFAULT_MODEL = "small.en"

#: A converted CTranslate2 model directory always contains these. Used both to skip an
#: existing install and to confirm the download produced something loadable.
_MODEL_MARKERS = ("model.bin", "config.json")


def _log(message: str = "") -> None:
    print(message, file=sys.stderr)


def _is_model_dir(path: Path) -> bool:
    """True if `path` looks like a converted CTranslate2 whisper model."""
    return path.is_dir() and all((path / marker).is_file() for marker in _MODEL_MARKERS)


def _default_dest(config_path: str, model: str) -> str:
    """Prefer the path config.yml already points at, so the two cannot disagree."""
    if Path(config_path).exists():
        try:
            from pbx.utils.config import Config

            configured = Config(config_path).get(
                "features.voicemail_transcription.whisper_model_dir"
            )
            if configured:
                return str(configured)
        except Exception as exc:  # A broken config.yml must not block the install.
            _log(f"note: could not read {config_path} ({exc}); falling back to models/whisper/")
    return f"models/whisper/{model}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download and stage a faster-whisper model for transcription.",
    )
    parser.add_argument(
        "--model",
        default=_DEFAULT_MODEL,
        help=f"Model to install (default: {_DEFAULT_MODEL}). See --list.",
    )
    parser.add_argument(
        "--dest",
        help="Install directory (default: features.voicemail_transcription.whisper_model_dir)",
    )
    parser.add_argument("--config", default="config.yml", help="Path to config.yml")
    parser.add_argument(
        "--owner",
        help="chown the installed model to this user (the PBX service account, e.g. 'pbx')",
    )
    parser.add_argument(
        "--force", action="store_true", help="Reinstall even if a valid model is already present"
    )
    parser.add_argument("--list", action="store_true", help="List the known models and exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list:
        print("Known models:")
        for name, description in _MODELS.items():
            marker = " (default)" if name == _DEFAULT_MODEL else ""
            print(f"  {name}{marker}\n      {description}")
        print("\nAny CTranslate2-converted model on HuggingFace also works, by full repo id.")
        return 0

    if args.model not in _MODELS:
        _log(f"warning: {args.model!r} is not in the known list; trying it as a HuggingFace id")

    dest = Path(args.dest or _default_dest(args.config, args.model)).expanduser()

    if _is_model_dir(dest) and not args.force:
        _log(f"A model is already installed at {dest}")
        _log("Nothing to do. Use --force to reinstall.")
        return 0

    # Imported here, not at module scope, so --list and --help work on a box where the
    # dependency has not been installed yet -- which is exactly the box that needs this script.
    try:
        from faster_whisper import download_model
    except ImportError:
        _venv = _REPO_ROOT / ".venv" / "bin" / "python"
        hint = f"{_venv} {sys.argv[0]}" if _venv.exists() else "make install, then rerun"
        _log(
            f"error: faster-whisper is not installed. This needs the project virtualenv:\n  {hint}"
        )
        return 2

    dest.parent.mkdir(parents=True, exist_ok=True)

    # Downloaded into a sibling temp directory and moved into place only once it is complete,
    # so an interrupted fetch never leaves a half-model that the PBX would load and fail on in
    # a much more confusing way.
    with tempfile.TemporaryDirectory(dir=dest.parent, prefix=".whisper-install-") as tmp:
        staging = Path(tmp) / "model"
        _log(f"Downloading {args.model} (this is a few hundred MB and is not resumable)...")
        try:
            download_model(args.model, output_dir=str(staging))
        except Exception as exc:
            # HuggingFace surfaces its own exception types for a bad repo id, a rate limit and
            # a network failure alike; none of them are reliably OSError.
            _log(f"error: download failed: {type(exc).__name__}: {exc}")
            _log("Is the model name spelled correctly? Try --list.")
            return 1

        if not _is_model_dir(staging):
            _log(f"error: the download did not produce a loadable model in {staging}")
            _log(f"Expected a directory containing {', '.join(_MODEL_MARKERS)}")
            return 1

        if dest.exists():
            shutil.rmtree(dest)
        shutil.move(str(staging), str(dest))

    if args.owner:
        try:
            for path in [dest, *dest.rglob("*")]:
                shutil.chown(path, user=args.owner)
            _log(f"Ownership set to {args.owner}")
        except (LookupError, PermissionError, OSError) as exc:
            _log(f"warning: could not chown to {args.owner!r}: {exc}")
            _log(f"         run: sudo chown -R {args.owner} {dest}")

    size_mb = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file()) / 1_048_576
    _log("")
    _log(f"Installed {args.model} to {dest} ({size_mb:,.0f} MiB)")
    _log("")
    _log("Next steps:")
    _log("  1. Point config.yml at it and switch the provider over:")
    _log("")
    _log("       features:")
    _log("         voicemail_transcription:")
    _log("           enabled: true")
    _log("           provider: faster-whisper")
    _log(f"           whisper_model: {args.model}")
    _log(f"           whisper_model_dir: {dest}")
    _log("")
    _log("  2. Compare it against Vosk on real voicemail before committing to it:")
    _log("")
    _log(
        f"       python scripts/benchmark_transcription.py voicemail/ --whisper-models {args.model}"
    )
    _log("")
    _log("  3. Restart the PBX. It logs whether the model loaded at startup.")

    if not args.owner:
        _log("")
        _log("If the PBX runs as a service account, make sure it can read the model:")
        _log(f"  sudo chown -R pbx:pbx {dest}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
