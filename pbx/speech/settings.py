"""
Transcription settings.

Reads the ``features.voicemail_transcription`` section. The name is historical -- the
subsystem now serves more than voicemail -- but the keys are what deployments already have in
config.yml, and moving them is a migration to make deliberately rather than as a side effect
of this refactor.

Follows :mod:`pbx.mail.settings`: a plain dict in, a frozen dataclass out, problems *reported*
rather than raised. A PBX with a broken transcription config must still boot and still record
voicemail; losing transcripts is survivable, failing to answer the phone is not.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Final

__all__ = ["TranscriptionSettings"]

#: Config section this reads. One place, so the eventual move to a top-level key is one edit.
CONFIG_SECTION: Final[str] = "features.voicemail_transcription"

DEFAULT_LANGUAGE: Final[str] = "en-US"
DEFAULT_MAX_AUDIO_SECONDS: Final[int] = 300
DEFAULT_VOSK_MODEL_PATH: Final[str] = "models/vosk-model-small-en-us-0.15"
DEFAULT_QUEUE_SIZE: Final[int] = 32

DEFAULT_WHISPER_MODEL: Final[str] = "small.en"
DEFAULT_WHISPER_MODEL_DIR: Final[str] = "models/whisper/small.en"
DEFAULT_WHISPER_COMPUTE_TYPE: Final[str] = "int8"
#: CTranslate2 quantisations that make sense on a CPU-only PBX. float16 is a GPU type and
#: silently falls back, which looks like a working config that is quietly slower.
WHISPER_COMPUTE_TYPES: Final[tuple[str, ...]] = ("int8", "int8_float32", "float32")
#: How long a notification waits for its transcript. At the measured RTF of ~0.47 for
#: whisper small.en this covers roughly two minutes of audio, so a normal voicemail --
#: plus one queued ahead of it -- still gets its transcript into the email. Anything
#: slower emails immediately without one; the transcript still reaches the database.
DEFAULT_DEADLINE_SECONDS: Final[float] = 60.0

#: Providers with a backend behind them. Google Cloud Speech was previously named here, but
#: ``google-cloud-speech`` has never been a dependency of this project, so that path could not
#: have run on any standard install -- the import guard simply reported it unavailable. A
#: config still asking for it is now told so by validate(), and degrades to no transcripts
#: rather than to a silent no-op.
KNOWN_PROVIDERS: Final[tuple[str, ...]] = ("vosk", "faster-whisper")

#: Accepted spellings for the whisper provider, normalised to ``faster-whisper``. The engine
#: is faster-whisper; "whisper" is what everyone types.
_PROVIDER_ALIASES: Final[dict[str, str]] = {
    "whisper": "faster-whisper",
    "faster_whisper": "faster-whisper",
}


def _is_unresolved(value: str) -> bool:
    """True for a value still looking like an unsubstituted ``${VAR}`` placeholder."""
    return "${" in value or value.startswith("$")


def _as_str(raw: Any, default: str = "") -> str:
    if raw is None:
        return default
    text = str(raw).strip()
    if not text or _is_unresolved(text):
        return default
    return text


def _as_int(raw: Any, default: int) -> int:
    text = _as_str(raw)
    if not text:
        return default
    try:
        return int(float(text))
    except ValueError:
        return default


def _as_float(raw: Any, default: float) -> float:
    text = _as_str(raw)
    if not text:
        return default
    try:
        return float(text)
    except ValueError:
        return default


def _as_bool(raw: Any, default: bool) -> bool:
    if isinstance(raw, bool):
        return raw
    text = _as_str(raw).lower()
    if text in ("true", "yes", "on", "1"):
        return True
    if text in ("false", "no", "off", "0"):
        return False
    return default


def _max_workers() -> int:
    """
    Ceiling on worker threads, leaving cores for the media path.

    Every active call owns an RTP relay thread that must not miss its 20 ms cadence. A
    transcription worker is CPU-bound and will happily starve them, so the cap is deliberate
    rather than advisory.
    """
    return max(1, (os.cpu_count() or 2) - 2)


@dataclass(frozen=True, slots=True)
class TranscriptionSettings:
    """Everything the subsystem needs, and nothing about who consumes the transcript."""

    enabled: bool = False
    provider: str = "vosk"
    language: str = DEFAULT_LANGUAGE
    max_audio_seconds: int = DEFAULT_MAX_AUDIO_SECONDS
    vosk_model_path: str = DEFAULT_VOSK_MODEL_PATH

    #: Whisper model name. ``.en`` variants are English-only and are both faster and better
    #: than the multilingual model of the same size on English telephony.
    whisper_model: str = DEFAULT_WHISPER_MODEL
    #: Directory holding the converted CTranslate2 model. Set this: with it, the model loads
    #: with ``local_files_only`` and a missing model fails loudly at startup. Without it,
    #: faster-whisper reaches for HuggingFace from a daemon thread on a production PBX.
    whisper_model_dir: str = DEFAULT_WHISPER_MODEL_DIR
    whisper_compute_type: str = DEFAULT_WHISPER_COMPUTE_TYPE
    #: Threads *inside* one transcription. CTranslate2 defaults to every core, which would
    #: starve the RTP relay threads; 1 is the measured-safe value and the RTF above is quoted
    #: at it. Raise only with `workers` at 1 and a spare core to give away.
    whisper_cpu_threads: int = 1
    #: 1 is greedy decoding. Beam search multiplies runtime for a marginal gain on clean audio.
    whisper_beam_size: int = 1
    #: Silero VAD, which drops non-speech before the decoder sees it. Turning this off invites
    #: confident hallucinated sentences on every silent voicemail -- see backends/whisper.py.
    whisper_vad_filter: bool = True

    #: Worker threads. 0 means run inline on the caller's thread -- used by tests, and a
    #: legitimate production choice for a box that would rather block than queue.
    workers: int = 1
    queue_size: int = DEFAULT_QUEUE_SIZE
    #: How long a caller will wait for a transcript before giving up on it. Bounds the
    #: *waiter*, never the work: a running recognition cannot be cancelled.
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS

    config_warnings: tuple[str, ...] = field(default=(), repr=False)

    @classmethod
    def from_dict(cls, section: dict[str, Any]) -> TranscriptionSettings:
        """Build from the raw config section. Takes a dict so this never imports Config."""
        warnings: list[str] = []

        provider = _as_str(section.get("provider"), "vosk").lower()
        provider = _PROVIDER_ALIASES.get(provider, provider)
        if provider not in KNOWN_PROVIDERS:
            warnings.append(
                f"{CONFIG_SECTION}.provider {provider!r} is not one of "
                f"{', '.join(KNOWN_PROVIDERS)}; transcription will not run"
            )

        workers = _as_int(section.get("workers"), 1)
        ceiling = _max_workers()
        if workers > ceiling:
            warnings.append(
                f"{CONFIG_SECTION}.workers {workers} exceeds the safe ceiling of {ceiling} "
                f"on this host ({os.cpu_count()} cores); using {ceiling} so transcription "
                "cannot starve the RTP relay threads"
            )
            workers = ceiling
        workers = max(0, workers)

        return cls(
            enabled=_as_bool(section.get("enabled"), False),
            provider=provider,
            language=_as_str(section.get("language"), DEFAULT_LANGUAGE),
            max_audio_seconds=_as_int(section.get("max_audio_seconds"), DEFAULT_MAX_AUDIO_SECONDS),
            vosk_model_path=_as_str(section.get("vosk_model_path"), DEFAULT_VOSK_MODEL_PATH),
            whisper_model=_as_str(section.get("whisper_model"), DEFAULT_WHISPER_MODEL),
            whisper_model_dir=_as_str(section.get("whisper_model_dir"), DEFAULT_WHISPER_MODEL_DIR),
            whisper_compute_type=_as_str(
                section.get("whisper_compute_type"), DEFAULT_WHISPER_COMPUTE_TYPE
            ).lower(),
            whisper_cpu_threads=_as_int(section.get("whisper_cpu_threads"), 1),
            whisper_beam_size=_as_int(section.get("whisper_beam_size"), 1),
            whisper_vad_filter=_as_bool(section.get("whisper_vad_filter"), True),
            workers=workers,
            queue_size=_as_int(section.get("queue_size"), DEFAULT_QUEUE_SIZE),
            deadline_seconds=_as_float(section.get("deadline_seconds"), DEFAULT_DEADLINE_SECONDS),
            config_warnings=tuple(warnings),
        )

    @property
    def runs_inline(self) -> bool:
        """True when jobs execute on the submitting thread instead of a worker."""
        return self.workers <= 0

    def validate(self) -> list[str]:
        """Every problem found, most-blocking first. Empty means usable."""
        problems: list[str] = list(self.config_warnings)

        if not self.enabled:
            return problems

        if self.provider not in KNOWN_PROVIDERS:
            problems.append(f"{CONFIG_SECTION}.provider {self.provider!r} is not supported")

        if self.provider == "vosk":
            if not self.vosk_model_path:
                problems.append(f"{CONFIG_SECTION}.vosk_model_path is not set")
            elif not Path(self.vosk_model_path).is_dir():
                problems.append(
                    f"{CONFIG_SECTION}.vosk_model_path {self.vosk_model_path!r} does not exist"
                )
            elif not Path(self.vosk_model_path).is_absolute():
                # Resolved against the process working directory, which a service unit may
                # set to something other than the install root.
                problems.append(
                    f"{CONFIG_SECTION}.vosk_model_path {self.vosk_model_path!r} is relative; "
                    "prefer an absolute path so a service unit's working directory cannot "
                    "change which model is loaded"
                )

        if self.provider == "faster-whisper":
            if not self.whisper_model:
                problems.append(f"{CONFIG_SECTION}.whisper_model is not set")
            if self.whisper_model_dir and not Path(self.whisper_model_dir).is_dir():
                problems.append(
                    f"{CONFIG_SECTION}.whisper_model_dir {self.whisper_model_dir!r} does not "
                    "exist; stage it with scripts/install_whisper_model.py"
                )
            elif not self.whisper_model_dir:
                # Not fatal, but it means a ~250 MB HuggingFace fetch from a daemon thread the
                # first time a voicemail arrives, on a host that may have no outbound internet.
                problems.append(
                    f"{CONFIG_SECTION}.whisper_model_dir is not set; the model will be "
                    "downloaded from HuggingFace at first use instead of loading locally"
                )
            if self.whisper_compute_type not in WHISPER_COMPUTE_TYPES:
                problems.append(
                    f"{CONFIG_SECTION}.whisper_compute_type {self.whisper_compute_type!r} is "
                    f"not one of {', '.join(WHISPER_COMPUTE_TYPES)}"
                )
            # Caught at startup rather than per message: an .en model given Spanish audio does
            # not fail, it transcribes confident nonsense, which is far harder to notice.
            if self.whisper_model.endswith(".en") and not self.language.lower().startswith("en"):
                problems.append(
                    f"{CONFIG_SECTION}.whisper_model {self.whisper_model!r} is English-only but "
                    f"language is {self.language!r}; use a multilingual model such as "
                    f"{self.whisper_model.removesuffix('.en')!r}"
                )
            if self.whisper_cpu_threads <= 0:
                problems.append(
                    f"{CONFIG_SECTION}.whisper_cpu_threads {self.whisper_cpu_threads} "
                    "must be positive"
                )
            if self.whisper_beam_size <= 0:
                problems.append(
                    f"{CONFIG_SECTION}.whisper_beam_size {self.whisper_beam_size} must be positive"
                )
            if not self.whisper_vad_filter:
                problems.append(
                    f"{CONFIG_SECTION}.whisper_vad_filter is off; whisper will hallucinate "
                    "sentences on silent recordings"
                )

        if self.max_audio_seconds <= 0:
            problems.append(
                f"{CONFIG_SECTION}.max_audio_seconds {self.max_audio_seconds} must be positive"
            )
        if self.queue_size <= 0:
            problems.append(f"{CONFIG_SECTION}.queue_size {self.queue_size} must be positive")
        if self.deadline_seconds <= 0:
            problems.append(
                f"{CONFIG_SECTION}.deadline_seconds {self.deadline_seconds} must be positive"
            )

        return problems

    def redacted(self) -> dict[str, Any]:
        """All settings as a dict, safe to log or return from an API."""
        result: dict[str, Any] = {
            spec.name: getattr(self, spec.name)
            for spec in fields(self)
            if spec.name != "config_warnings"
        }
        result["runs_inline"] = self.runs_inline
        return result
