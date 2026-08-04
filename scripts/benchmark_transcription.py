#!/usr/bin/env python3
"""
Benchmark transcription engines against real voicemail, and print their output side by side.

This exists to answer one question before any code commits to a model: **can this box afford
it?** The number that matters is the real-time factor -- processing seconds per audio second.
Below 1.0 is faster than real time. A 60-second voicemail at RTF 2.0 takes two minutes, which
loses the race against the notification deadline and makes the whole async design pointless.

It also prints each engine's transcript, because speed is only half the decision. Whisper is
expected to be slower than Vosk and markedly more accurate on 8 kHz telephony, and only your
own recordings can say whether that trade is worth it.

Run it against **real** voicemail, not synthetic audio. Phone recordings are 8 kHz G.711 with
line noise, and that is precisely where engines diverge.

Usage::

    python scripts/benchmark_transcription.py voicemail/1001/*.wav
    python scripts/benchmark_transcription.py voicemail/ --engines vosk,whisper
    python scripts/benchmark_transcription.py voicemail/ --whisper-models tiny.en,base.en,small.en

Every engine runs with the same single-threaded settings the PBX will use in production, so
the numbers reflect a box that is also carrying calls -- not a benchmark with all cores free.

Exit codes: 0 ran, 1 nothing to measure, 2 bad usage or missing dependency.
"""

from __future__ import annotations

import argparse
import gc
import sys
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

try:
    from pbx.speech import TranscriptionSettings, build_backend
    from pbx.speech.settings import DEFAULT_VOSK_MODEL_PATH
    from pbx.utils.audio import read_wav_as_pcm16, resample_pcm16
except ModuleNotFoundError as exc:
    _venv = _REPO_ROOT / ".venv" / "bin" / "python"
    _hint = f"{_venv} {sys.argv[0]}" if _venv.exists() else "make install, then rerun"
    print(
        f"error: missing dependency {exc.name!r}. This needs the project virtualenv:\n  {_hint}\n",
        file=sys.stderr,
    )
    raise SystemExit(2) from exc

#: Whisper is trained at 16 kHz. Telephony is 8 kHz, so everything is resampled to this.
WHISPER_SAMPLE_RATE = 16000


def _log(message: str = "") -> None:
    print(message, file=sys.stderr)


def _peak_rss_mb() -> float | None:
    """Peak resident memory for this process, in MiB."""
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports KiB, macOS reports bytes.
        return peak / 1024 if sys.platform.startswith("linux") else peak / 1_048_576
    except Exception:
        return None


def _load_audio(path: Path) -> tuple[bytes, int, float]:
    """
    Decode a recording the way the PBX does.

    Deliberately the production path: voicemail is G.711, which `wave` cannot open, and any
    benchmark that sidesteps that is measuring a file the PBX never produces.
    """
    pcm16, rate = read_wav_as_pcm16(path)
    duration = len(pcm16) / 2 / float(rate)
    return pcm16, rate, duration


class VoskRunner:
    """Wraps the production Vosk backend so both engines are measured identically."""

    label = "vosk"

    def __init__(self, model_path: str) -> None:
        settings = TranscriptionSettings.from_dict(
            {
                "enabled": True,
                "provider": "vosk",
                "vosk_model_path": model_path,
                "workers": 0,
                "max_audio_seconds": 100000,
            }
        )
        self.backend = build_backend(settings)
        if self.backend is None or not self.backend.ready:
            raise RuntimeError(f"Vosk model not loadable at {model_path}")

    def run(self, path: Path, pcm16: bytes, rate: int) -> str:
        result = self.backend.transcribe_file(path)
        if not result.success:
            raise RuntimeError(result.error or "unknown error")
        return result.text


class WhisperRunner:
    """
    Runs faster-whisper with the settings the PBX would use.

    The non-default options are all deliberate and all about telephony:

    * ``vad_filter`` -- voicemail is mostly silence and comfort noise, and Whisper hallucinates
      confident sentences on silence ("Thank you.", "Subtitles by..."). Vosk does not do this.
    * ``condition_on_previous_text=False`` -- without it, one hallucination feeds the next and
      the output degenerates into repetition loops.
    * ``beam_size=1`` and ``cpu_threads=1`` -- the box is carrying live RTP; a benchmark run
      with all cores is measuring a machine that does not exist in production.
    """

    def __init__(self, model_name: str, model_dir: str | None = None, threads: int = 1) -> None:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError(
                "faster-whisper is not installed. Add it and run `make lock`, or: "
                "pip install faster-whisper"
            ) from exc

        self.label = f"whisper:{model_name}"
        kwargs: dict[str, Any] = {"compute_type": "int8", "cpu_threads": threads}
        if model_dir:
            # Never reach for HuggingFace at runtime on a production PBX.
            kwargs["download_root"] = model_dir
            kwargs["local_files_only"] = True
        self.model = WhisperModel(model_name, device="cpu", **kwargs)

    def run(self, path: Path, pcm16: bytes, rate: int) -> str:
        import numpy as np

        if rate != WHISPER_SAMPLE_RATE:
            pcm16 = resample_pcm16(pcm16, rate, WHISPER_SAMPLE_RATE)
        # Hand it samples, not a path: reusing the PBX's own decoder keeps one decoding story
        # and avoids inheriting whatever `av` would do with a G.711 WAV.
        samples = np.frombuffer(pcm16, dtype="<i2").astype("float32") / 32768.0

        segments, _info = self.model.transcribe(
            samples,
            beam_size=1,
            vad_filter=True,
            condition_on_previous_text=False,
            language="en",
        )
        # transcribe() returns a generator; nothing runs until it is consumed.
        return " ".join(s.text.strip() for s in segments).strip()


def _collect_files(paths: list[str], limit: int) -> list[Path]:
    """Expand directories to the WAV files inside them."""
    files: list[Path] = []
    for raw in paths:
        p = Path(raw).expanduser()
        if p.is_dir():
            files.extend(sorted(p.rglob("*.wav")))
        elif p.is_file():
            files.append(p)
    # Greetings are prompts, not messages -- they skew both speed and quality.
    files = [f for f in files if f.name != "greeting.wav"]
    return files[:limit]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare transcription engines on real voicemail recordings.",
    )
    parser.add_argument("paths", nargs="+", help="WAV files, or directories to search")
    parser.add_argument("--engines", default="vosk,whisper", help="Comma-separated: vosk, whisper")
    parser.add_argument(
        "--whisper-models",
        default="small.en",
        help="Comma-separated whisper models to compare, e.g. tiny.en,base.en,small.en",
    )
    parser.add_argument("--whisper-model-dir", help="Local model directory (no HuggingFace)")
    parser.add_argument("--vosk-model", default=DEFAULT_VOSK_MODEL_PATH, help="Vosk model path")
    parser.add_argument("--threads", type=int, default=1, help="cpu_threads for whisper")
    parser.add_argument("--limit", type=int, default=10, help="Maximum recordings to process")
    parser.add_argument("--no-text", action="store_true", help="Timings only, no transcripts")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    files = _collect_files(args.paths, args.limit)
    if not files:
        print("error: no .wav files found", file=sys.stderr)
        return 1

    engines = [e.strip() for e in args.engines.split(",") if e.strip()]
    runners: list[Any] = []

    if "vosk" in engines:
        try:
            runners.append(VoskRunner(args.vosk_model))
        except RuntimeError as e:
            _log(f"skipping vosk: {e}")

    if "whisper" in engines:
        for name in (m.strip() for m in args.whisper_models.split(",") if m.strip()):
            try:
                _log(f"loading {name}...")
                runners.append(WhisperRunner(name, args.whisper_model_dir, args.threads))
            except RuntimeError as e:
                _log(f"skipping whisper:{name}: {e}")

    if not runners:
        print("error: no engine could be loaded", file=sys.stderr)
        return 1

    _log(f"\n{len(files)} recording(s), {len(runners)} engine(s), cpu_threads={args.threads}\n")

    totals: dict[str, list[tuple[float, float]]] = {r.label: [] for r in runners}

    for path in files:
        try:
            pcm16, rate, duration = _load_audio(path)
        except (OSError, ValueError) as e:
            _log(f"{path.name}: unreadable ({e})")
            continue

        print(f"\n=== {path.name}  ({duration:.1f}s of audio @ {rate} Hz) ===")
        for runner in runners:
            gc.collect()
            started = time.monotonic()
            try:
                text = runner.run(path, pcm16, rate)
            except Exception as e:
                print(f"  {runner.label:<18} FAILED: {e}")
                continue
            elapsed = time.monotonic() - started
            rtf = elapsed / duration if duration else 0.0
            totals[runner.label].append((elapsed, duration))

            print(f"  {runner.label:<18} {elapsed:6.2f}s   RTF {rtf:5.2f}")
            if not args.no_text:
                print(f"      {text or '(nothing recognised)'}")

    print("\n" + "=" * 70)
    print(f"{'engine':<18} {'audio':>8} {'wall':>8} {'RTF':>6}   verdict")
    print("-" * 70)
    for label, runs in totals.items():
        if not runs:
            continue
        wall = sum(e for e, _ in runs)
        audio = sum(d for _, d in runs)
        rtf = wall / audio if audio else 0.0
        # A 60 s voicemail against the 30 s notification deadline is the practical test.
        verdict = "ok" if rtf * 60 <= 30 else f"a 60s message takes {rtf * 60:.0f}s"
        print(f"{label:<18} {audio:7.1f}s {wall:7.1f}s {rtf:6.2f}   {verdict}")

    peak = _peak_rss_mb()
    if peak:
        print(f"\nPeak RSS for this process: {peak:,.0f} MiB (all engines loaded at once)")
    print(
        "\nRTF is processing seconds per audio second. The deadline that matters is\n"
        "features.voicemail_transcription.deadline_seconds (default 30)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
