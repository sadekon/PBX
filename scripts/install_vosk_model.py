#!/usr/bin/env python3
"""
Download and install a Vosk speech model where the PBX expects to find it.

Transcription needs a model that is not shipped with the PBX and has to be fetched
separately, which is why ``features.voicemail_transcription.enabled`` defaults to false. This
script puts one in place and tells you what to set afterwards.

It is idempotent: an existing, structurally valid model is left alone unless ``--force`` is
given. It also fixes the mistake that a manual ``unzip`` invites -- the archive contains a
top-level directory, so unzipping it straight into the destination produces
``models/vosk-model-small-en-us-0.15/vosk-model-small-en-us-0.15/``, which loads as "model
not found". The extracted tree is searched for the real model root and only that is installed.

Usage::

    python scripts/install_vosk_model.py
    python scripts/install_vosk_model.py --dest /opt/warden/models/vosk-model-small-en-us-0.15
    python scripts/install_vosk_model.py --model vosk-model-en-us-0.22 --owner pbx
    python scripts/install_vosk_model.py --list

Exit codes: 0 installed or already present, 1 failed, 2 bad usage.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

_BASE_URL = "https://alphacephei.com/vosk/models"

#: The models worth offering here. Sizes are approximate download sizes.
#: The full catalogue is at https://alphacephei.com/vosk/models
_MODELS = {
    "vosk-model-small-en-us-0.15": "~40 MB   English (US), small. The right default for 8 kHz phone audio.",
    "vosk-model-en-us-0.22": "~1.8 GB  English (US), full. More accurate, much slower and heavier.",
    "vosk-model-small-en-gb-0.15": "~40 MB   English (UK), small.",
    "vosk-model-small-nl-0.22": "~40 MB   Dutch, small.",
    "vosk-model-small-de-0.15": "~45 MB   German, small.",
    "vosk-model-small-fr-0.22": "~40 MB   French, small.",
    "vosk-model-small-es-0.42": "~40 MB   Spanish, small.",
}

_DEFAULT_MODEL = "vosk-model-small-en-us-0.15"

#: A Vosk model directory always contains these. Used both to skip an existing install and to
#: locate the real root inside the extracted archive.
_MODEL_MARKERS = ("am", "conf")


def _log(message: str = "") -> None:
    print(message, file=sys.stderr)


def _is_model_dir(path: Path) -> bool:
    """True if `path` looks like a Vosk model root."""
    return path.is_dir() and all((path / marker).is_dir() for marker in _MODEL_MARKERS)


def _find_model_root(search_root: Path) -> Path | None:
    """
    Locate the model root inside an extracted archive.

    Handles both a flat archive and the usual one-directory-deep layout, and tolerates an
    extra level of nesting from a previous bad unzip.
    """
    if _is_model_dir(search_root):
        return search_root
    for candidate in sorted(search_root.rglob("*")):
        if _is_model_dir(candidate):
            return candidate
    return None


def _default_dest(config_path: str) -> str:
    """Prefer the path config.yml already points at, so the two cannot disagree."""
    if Path(config_path).exists():
        try:
            from pbx.utils.config import Config

            configured = Config(config_path).get("features.voicemail_transcription.vosk_model_path")
            if configured:
                return str(configured)
        except Exception as exc:  # A broken config.yml must not block the install.
            _log(f"note: could not read {config_path} ({exc}); falling back to models/")
    return f"models/{_DEFAULT_MODEL}"


def _download(url: str, target: Path) -> None:
    """Fetch `url` to `target`, reporting progress on stderr."""
    _log(f"Downloading {url}")

    def report(block_num: int, block_size: int, total_size: int) -> None:
        if total_size <= 0:
            return
        done = min(block_num * block_size, total_size)
        pct = done * 100 // total_size
        # \r keeps this to one line; it goes to stderr so it never pollutes piped output.
        print(
            f"\r  {pct:3d}%  {done / 1_048_576:,.0f} / {total_size / 1_048_576:,.0f} MiB",
            end="",
            file=sys.stderr,
            flush=True,
        )

    urllib.request.urlretrieve(url, target, reporthook=report)
    print(file=sys.stderr)


def _safe_extract(archive: Path, destination: Path) -> None:
    """
    Extract `archive` into `destination`, refusing members that escape it.

    The archive comes off the network, so a member named ``../../etc/cron.d/x`` would
    otherwise be written wherever it pointed.
    """
    with zipfile.ZipFile(archive) as zf:
        base = destination.resolve()
        for member in zf.namelist():
            target = (base / member).resolve()
            if not target.is_relative_to(base):
                raise ValueError(f"archive member escapes the destination: {member!r}")
        zf.extractall(destination)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download and install a Vosk model for voicemail transcription.",
    )
    parser.add_argument(
        "--model",
        default=_DEFAULT_MODEL,
        help=f"Model name to install (default: {_DEFAULT_MODEL}). See --list.",
    )
    parser.add_argument(
        "--dest",
        help="Install directory (default: features.voicemail_transcription.vosk_model_path)",
    )
    parser.add_argument("--config", default="config.yml", help="Path to config.yml")
    parser.add_argument("--url", help="Download a specific archive URL instead of --model")
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
        print(f"\nFull catalogue: {_BASE_URL}")
        return 0

    if not args.url and args.model not in _MODELS:
        _log(f"warning: {args.model!r} is not in the known list; trying it anyway")

    dest = Path(args.dest or _default_dest(args.config)).expanduser()
    url = args.url or f"{_BASE_URL}/{args.model}.zip"

    if _is_model_dir(dest) and not args.force:
        _log(f"A model is already installed at {dest}")
        _log("Nothing to do. Use --force to reinstall.")
        return 0

    if dest.exists() and not _is_model_dir(dest) and any(dest.iterdir()):
        _log(f"note: {dest} exists but does not look like a Vosk model; it will be replaced")

    dest.parent.mkdir(parents=True, exist_ok=True)

    # Staged in a sibling temp directory so a failed download never leaves a half-model in
    # place -- the service would load it and fail in a much more confusing way.
    with tempfile.TemporaryDirectory(dir=dest.parent, prefix=".vosk-install-") as tmp:
        tmp_path = Path(tmp)
        archive = tmp_path / "model.zip"

        try:
            _download(url, archive)
        except urllib.error.HTTPError as exc:
            _log(f"error: download failed ({exc.code} {exc.reason})")
            if exc.code == 404:
                _log(f"Is {args.model!r} spelled correctly? Try --list, or check {_BASE_URL}")
            return 1
        except (urllib.error.URLError, OSError) as exc:
            _log(f"error: download failed: {exc}")
            return 1

        _log("Extracting...")
        extracted = tmp_path / "extracted"
        try:
            _safe_extract(archive, extracted)
        except (zipfile.BadZipFile, ValueError) as exc:
            _log(f"error: could not extract the archive: {exc}")
            return 1

        model_root = _find_model_root(extracted)
        if model_root is None:
            _log("error: no Vosk model found in the archive")
            _log(f"Expected a directory containing {', '.join(_MODEL_MARKERS)}/")
            return 1

        if dest.exists():
            shutil.rmtree(dest)
        shutil.move(str(model_root), str(dest))

    if not _is_model_dir(dest):
        _log(f"error: install finished but {dest} does not look like a model")
        return 1

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
    _log("  1. Point config.yml at it and switch transcription on:")
    _log("")
    _log("       features:")
    _log("         voicemail_transcription:")
    _log("           enabled: true")
    _log(f"           vosk_model_path: {dest}")
    _log("")
    _log("  2. Verify it against a real voicemail:")
    _log("")
    _log(f"       python scripts/transcribe_voicemail.py -f <file.wav> --model {dest}")
    _log("")
    _log("  3. Restart the PBX. It logs whether the model loaded at startup.")

    if not args.owner:
        _log("")
        _log("If the PBX runs as a service account, make sure it can read the model:")
        _log(f"  sudo chown -R pbx:pbx {dest}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
