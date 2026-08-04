#!/usr/bin/env python3
"""
Send one new-voicemail notification for a WAV file you choose, with the engine you choose.

This is the missing half of the transcription test loop. ``benchmark_transcription.py`` shows
what an engine produces; ``send_test_email.py`` shows whether SMTP works. Neither answers the
question that actually matters -- *does a transcript reach a mailbox, and does it read well* --
and until now the only way to find out was to ring an extension and leave a message.

It drives the real code: the same ``VoicemailBox._send_notification_email``, the same subject
and body builders, the same ``voicemail.email.*`` toggles, the same disclaimer. What arrives is
what production would send. Only the recipient, the recording and the engine are overridden.

Every model setting can be overridden per run, so switching engines costs a flag rather than a
config edit and a restart::

    python scripts/send_test_voicemail_notification.py -f voicemail/1001/msg.wav -t you@corp.com
    python scripts/send_test_voicemail_notification.py -f msg.wav -t you@corp.com -n \\
        -p faster-whisper --whisper-model-dir /opt/warden/models/whisper/small.en
    python scripts/send_test_voicemail_notification.py -f msg.wav -t you@corp.com \\
        -p vosk --vosk-model /opt/warden/models/vosk-model-small-en-us-0.15

Transcription runs **inline** here, not on the worker thread, and there is no deadline: the
point is to see the transcript, not to reproduce the timing. A message that would have missed
the 60 s deadline in production will still show its transcript here -- compare the reported
real-time factor against ``deadline_seconds`` to know which way it would have gone.

Runs without a PBXCore. Reads config.yml directly, so it works on a box where the PBX will
not start.

Exit codes: 0 sent, 1 failed, 2 bad usage or missing dependency.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

try:
    from pbx.features.voicemail import VoicemailSystem
    from pbx.mail import Mailer, SmtpSettings
    from pbx.speech import TranscriptionSettings, build_backend
    from pbx.speech.settings import CONFIG_SECTION as TRANSCRIPTION_SECTION
    from pbx.utils.audio import read_wav_as_pcm16
    from pbx.utils.config import Config
except ModuleNotFoundError as exc:
    # Almost always "run with the system interpreter instead of the project venv". The bare
    # traceback names a transitive dependency, which points nowhere useful.
    _venv = _REPO_ROOT / ".venv" / "bin" / "python"
    _script = sys.argv[0] or str(__file__)
    _hint = f"{_venv} {_script}" if _venv.exists() else f"make install, then rerun {_script}"
    print(
        f"error: missing dependency {exc.name!r}.\n"
        f"This script needs the project's virtualenv. Try:\n"
        f"  {_hint} -f message.wav -t you@example.com\n",
        file=sys.stderr,
    )
    raise SystemExit(2) from exc

#: How long to wait for the queued message to actually leave. The Mailer is asynchronous by
#: design, so without this the script would exit before its own send happened.
FLUSH_TIMEOUT = 60.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Send one voicemail notification for a WAV file, with a chosen engine.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Model flags override config.yml for this run only; nothing is written back.\n"
            "Anything not given falls back to features.voicemail_transcription in config.yml."
        ),
    )
    # Short form first, then long -- the GNU order, and what argparse renders as "-f, --file".
    # Only the options you would type interactively get a short form; the tuning flags below
    # stay long-only, because a single letter for every one of them stops being a mnemonic and
    # starts being a lookup table.
    parser.add_argument("-f", "--file", required=True, help="WAV file to transcribe and attach")
    parser.add_argument("-t", "--to", required=True, help="Where to send the notification")
    parser.add_argument(
        "-e", "--extension", default="1001", help="Mailbox the message is for (default: 1001)"
    )
    parser.add_argument(
        "--from-number", default="5551234567", help="Caller ID to show (default: 5551234567)"
    )

    engine = parser.add_argument_group("engine selection (overrides config.yml)")
    engine.add_argument(
        "-p",
        "--provider",
        choices=["vosk", "faster-whisper", "whisper"],
        help="Which engine to use",
    )
    engine.add_argument("--vosk-model", help="Vosk model directory")
    engine.add_argument("--whisper-model", help="Whisper model name, e.g. small.en or base.en")
    engine.add_argument("--whisper-model-dir", help="Directory holding the staged whisper model")
    engine.add_argument(
        "--compute-type", help="CTranslate2 quantisation: int8, int8_float32 or float32"
    )
    engine.add_argument("--beam-size", type=int, help="Whisper beam size (1 is greedy)")
    engine.add_argument("--cpu-threads", type=int, help="Threads inside one transcription")
    engine.add_argument("-l", "--language", help="Language code, e.g. en-US")
    engine.add_argument("--max-seconds", type=int, help="Refuse audio longer than this")

    parser.add_argument(
        "--no-transcription",
        action="store_true",
        help="Send the notification without transcribing, to compare against the plain email",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Print the email that would be sent, without sending",
    )
    parser.add_argument("-c", "--config", default="config.yml", help="Path to config.yml")
    return parser


def _settings_from(args: argparse.Namespace, config: Config) -> TranscriptionSettings:
    """Config section first, then any flag the caller gave, then force it on and inline."""
    section: dict[str, Any] = dict(config.get(TRANSCRIPTION_SECTION, {}) or {})

    overrides = {
        "provider": args.provider,
        "vosk_model_path": args.vosk_model,
        "whisper_model": args.whisper_model,
        "whisper_model_dir": args.whisper_model_dir,
        "whisper_compute_type": args.compute_type,
        "whisper_beam_size": args.beam_size,
        "whisper_cpu_threads": args.cpu_threads,
        "language": args.language,
        "max_audio_seconds": args.max_seconds,
    }
    section.update({key: value for key, value in overrides.items() if value is not None})

    # Forced on regardless of config: being asked to run this script *is* the enable, and
    # `enabled: false` in config.yml would otherwise leave the backend unloaded and the whole
    # exercise silently transcript-free. workers=0 keeps the job on this thread.
    section["enabled"] = True
    section["workers"] = 0
    return TranscriptionSettings.from_dict(section)


def _transcribe(args: argparse.Namespace, config: Config, path: Path) -> Any | None:
    """Run the chosen engine over `path`, reporting what it did. None means no transcript."""
    settings = _settings_from(args, config)

    print(f"Engine:     {settings.provider}")
    if settings.provider == "faster-whisper":
        print(f"Model:      {settings.whisper_model_dir or settings.whisper_model}")
        print(f"Compute:    {settings.whisper_compute_type}, beam {settings.whisper_beam_size}")
    else:
        print(f"Model:      {settings.vosk_model_path}")

    for problem in settings.validate():
        print(f"  config: {problem}")

    backend = build_backend(settings)
    if backend is None:
        print(f"\nerror: no backend for provider {settings.provider!r}", file=sys.stderr)
        return None

    # The loader logs the real reason on its own -- a missing model directory, or the actual
    # ImportError text when faster-whisper cannot be imported. That output is the diagnostic;
    # this only says which way it went.
    if not backend.ready:
        print("\nerror: the engine did not load. See the warning above for why.", file=sys.stderr)
        return None

    print("\nTranscribing...")
    transcript = backend.transcribe_file(path)
    if not transcript.success:
        print(f"error: transcription failed: {transcript.error}", file=sys.stderr)
        return None

    rtf = transcript.real_time_factor
    print(f"Took:       {transcript.processing_duration:.1f}s for {transcript.audio_duration:.1f}s")
    if rtf is not None:
        deadline = float(config.get(f"{TRANSCRIPTION_SECTION}.deadline_seconds", 60))
        would_make_it = transcript.processing_duration <= deadline
        verdict = "within" if would_make_it else "OVER"
        print(f"RTF:        {rtf:.2f}  ({verdict} the {deadline:.0f}s notification deadline)")

    if not transcript.text:
        # Not an error: with VAD on, a caller who hung up on the beep produces no speech.
        print("Text:       (nothing recognised)")
    else:
        print(f"Text:       {transcript.text}")

    return transcript


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    audio_path = Path(args.file).expanduser()
    if not audio_path.is_file():
        print(f"error: no such file: {audio_path}", file=sys.stderr)
        return 2

    try:
        config = Config(args.config)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # Decoded rather than assumed: voicemail is G.711, which `wave` cannot open at all, and
    # the duration shown in the email should be the real one.
    try:
        pcm16, sample_rate = read_wav_as_pcm16(audio_path)
        duration = len(pcm16) / 2 / float(sample_rate)
    except (OSError, ValueError) as exc:
        print(f"error: could not read {audio_path}: {exc}", file=sys.stderr)
        return 1

    print(f"File:       {audio_path} ({duration:.1f}s @ {sample_rate} Hz)")

    transcript = None if args.no_transcription else _transcribe(args, config, audio_path)
    if transcript is None and not args.no_transcription:
        print("\nSending the notification anyway, without a transcript -- which is exactly")
        print("what production does when transcription is unavailable.")

    settings = SmtpSettings.from_dict(config.get("smtp", {}) or {})
    for problem in settings.validate():
        print(f"  smtp: {problem}")

    mailer = Mailer(settings)
    system = VoicemailSystem(
        storage_path=config.get("voicemail.storage_path", "voicemail"),
        config=config,
        mailer=mailer,
    )
    mailbox = system.get_mailbox(args.extension)
    timestamp = datetime.now(tz=UTC)

    if args.dry_run:
        print("\n" + "=" * 70)
        print(f"To:      {args.to}")
        print(f"Subject: {mailbox._notification_subject(args.from_number, timestamp)}")
        print("=" * 70)
        print(
            mailbox._notification_body(
                args.from_number,
                timestamp,
                duration,
                transcription=transcript.text if transcript else None,
                confidence=transcript.confidence if transcript else None,
                provider=transcript.provider if transcript else None,
            )
        )
        print("=" * 70)
        print("\nDry run: nothing was sent.")
        return 0

    if not mailer.enabled:
        print(
            "\nerror: SMTP is not configured; set smtp.host and smtp.from_address", file=sys.stderr
        )
        return 1

    mailer.start()
    mailbox._send_notification_email(
        args.to,
        args.from_number,
        timestamp,
        audio_file_path=audio_path,
        duration=duration,
        transcription=transcript.text if transcript else None,
        confidence=transcript.confidence if transcript else None,
        provider=transcript.provider if transcript else None,
    )
    # send_async only queues. Draining is what actually delivers, and the exit code should
    # reflect delivery rather than enqueueing.
    mailer.stop(timeout=FLUSH_TIMEOUT)

    print(f"\nQueued and flushed to {args.to}.")
    if settings.redirect_to:
        print(f"Note: smtp.redirect_to is set, so it went to {settings.redirect_to} instead.")
    print("If it does not arrive, check the PBX log for the SMTP error, or run")
    print("  python scripts/send_test_email.py --to <address> --verify-only --verbose")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
