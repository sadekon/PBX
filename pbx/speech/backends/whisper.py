"""
faster-whisper file-transcription backend.

Whisper via CTranslate2 rather than torch: roughly 50 MB of runtime instead of ~800 MB, which
is what makes it affordable on a box whose real job is carrying calls. Measured on the target
hardware at RTF ~0.47 for ``small.en`` at ``cpu_threads=1``, against Vosk's ~0.27 -- slower,
but markedly better on 8 kHz telephony, and it produces punctuation and capitalisation that
Vosk does not.

Three things here are not optional and are easy to remove by accident:

* **Whisper hallucinates on silence.** Voicemail is mostly silence and comfort noise, and the
  model will confidently emit "Thank you." or "Subtitles by the Amara.org community" for a
  caller who hung up on the beep. ``vad_filter`` plus the threshold arguments below plus a
  post-filter are all guarding against this. Vosk has no equivalent failure mode, so anyone
  arriving from that backend will not expect it.
* **Empty output is a success, not an error.** With VAD doing its job, a silent recording is
  the ordinary outcome for a hang-up, not a fault to log.
* **Confidence is always None.** Whisper exposes ``avg_logprob`` and ``no_speech_prob``;
  neither is a confidence and presenting one as "Estimated accuracy 87%" would reintroduce
  exactly the fabricated figure that was removed from the voicemail email.

Audio is decoded by :func:`pbx.utils.audio.read_wav_as_pcm16` and handed over as samples
rather than as a path. Passing the path would delegate decoding to ``av`` and give the project
a second decoding story for the same G.711 files -- the one :mod:`wave` already cannot read.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from pbx.speech.types import Segment, Transcript, Word
from pbx.utils.audio import read_wav_as_pcm16, resample_pcm16
from pbx.utils.logger import get_logger

if TYPE_CHECKING:
    from pbx.speech.settings import TranscriptionSettings

#: Whisper is trained at 16 kHz. Telephony is 8 kHz, so everything is resampled to this.
WHISPER_SAMPLE_RATE: Final[int] = 16000
PCM16_BYTES_PER_SAMPLE: Final[int] = 2
#: PCM16 is a signed 16-bit integer; whisper wants float32 in [-1, 1).
PCM16_FULL_SCALE: Final[float] = 32768.0

#: Phrases Whisper emits when decoding silence rather than speech. They come from its training
#: data -- YouTube subtitle tracks -- and appear verbatim, which is what makes them filterable.
#: Nobody leaves these on a voicemail, so they are dropped whatever the recording looks like.
#: Matched case-insensitively against the *whole* transcript: dropping them mid-sentence would
#: corrupt a genuine message that happens to mention one.
HALLUCINATION_PHRASES: Final[frozenset[str]] = frozenset(
    {
        "thanks for watching!",
        "thanks for watching.",
        "subtitles by the amara.org community",
        "subtitles by the amara.org community.",
        "amara.org",
        "please subscribe to my channel.",
        "transcription by castingwords",
        "www.mooji.org",
    }
)

#: Phrases that are *both* common silence artefacts and ordinary things to say. "Thank you." is
#: the single most frequent thing Whisper invents for silence -- and also a perfectly normal
#: entire voicemail. A blanket blocklist gets one of those two cases wrong every time.
#:
#: Length breaks the tie. Someone ringing to say thanks leaves a few seconds of audio; a silent
#: recording that yields nothing *but* one stock phrase is silence however long it ran. So these
#: are kept on short recordings and discarded on long ones.
AMBIGUOUS_PHRASES: Final[frozenset[str]] = frozenset(
    {"thank you", "thank you.", "you", "you.", "bye", "bye.", "."}
)

#: Recordings at or under this are short enough that a one-phrase transcript is plausibly real.
AMBIGUOUS_PHRASE_MAX_SECONDS: Final[float] = 10.0

# Set before ctranslate2 is imported, and therefore here rather than in pbx/main.py: the
# OpenMP backend reads this at load time and overrides the `cpu_threads` argument, so without
# it CTranslate2 spawns one thread per core no matter what the settings say. Only set if the
# operator has not already chosen a value.
os.environ.setdefault("OMP_NUM_THREADS", "1")

try:
    from faster_whisper import WhisperModel

    WHISPER_AVAILABLE = True
    WHISPER_IMPORT_ERROR = ""
except ImportError as _exc:  # pragma: no cover - depends on the install, not on logic
    WHISPER_AVAILABLE = False
    #: Kept verbatim. "Not installed" is only one of the reasons this import fails: a
    #: ctranslate2 build whose shared library will not load, a CPU without the instructions it
    #: was compiled for, or a package whose __init__ does not export WhisperModel all raise
    #: ImportError with quite different messages. Reporting a guess instead of the real text
    #: sends whoever is reading the log to reinstall a package that is already installed.
    WHISPER_IMPORT_ERROR = str(_exc)


class WhisperBackend:
    """Implements :class:`~pbx.speech.protocols.FileTranscriber` using faster-whisper."""

    provider = "faster-whisper"

    def __init__(self, settings: TranscriptionSettings, logger: Any | None = None) -> None:
        """
        Load the model, or leave the backend not-:attr:`ready`.

        Loading happens once, at startup, in ``FeatureInitializer`` -- the same place and for
        the same reason as Vosk. Nothing raises: a missing library or an unstaged model costs
        transcription and nothing else, and the PBX must still boot and still record voicemail.
        """
        self.settings = settings
        self.logger = logger or get_logger()
        self.model: Any | None = None
        #: What is actually handed to CTranslate2. Reported on the Transcript.
        self.model_id = _resolve_model(settings.whisper_model_dir, settings.whisper_model)
        #: What gets recorded against a transcript. The *name*, not the path: a stored
        #: transcript is compared against others months later, and "models/whisper" says
        #: nothing about whether it came from small.en or tiny.en, while a path is also
        #: specific to one host. The path stays in the logs, where it helps.
        self.model_name = settings.whisper_model or self.model_id

        if settings.enabled:
            self._load_model()

    def _load_model(self) -> None:
        """Load the model, reporting and swallowing every failure."""
        if not WHISPER_AVAILABLE:
            self.logger.warning(f"Cannot import faster-whisper: {WHISPER_IMPORT_ERROR}")
            self.logger.info(
                "If the package is installed, confirm the PBX runs the interpreter it is "
                f"installed into -- this process is {sys.executable}"
            )
            return

        if self.settings.whisper_model_dir and not Path(self.model_id).is_dir():
            self.logger.warning(f"Whisper model directory not found at {self.model_id}")
            self.logger.info(
                "Stage it with: python scripts/install_whisper_model.py "
                f"--model {self.settings.whisper_model}"
            )
            return

        if (
            self.model_id == self.settings.whisper_model_dir.rstrip("/")
            and (Path(self.model_id) / "model.bin").is_file()
        ):
            # The directory holds a model directly, so its name identifies nothing and
            # whisper_model is taken on trust. Nothing here can check it: a converted model
            # carries alignment heads and language ids, never its own name. Supported, because
            # existing installs are laid out this way, but the transcript record is only as
            # honest as the config.
            self.logger.warning(
                f"{self.model_id} holds a model directly, so its name cannot confirm this is "
                f"{self.settings.whisper_model!r}. Transcripts will record that name on trust. "
                f"Stage models as {self.model_id}/<model-name> and point whisper_model_dir at "
                f"{self.model_id} so the two cannot disagree."
            )

        if not self.settings.whisper_model_dir:
            # Loading by name reaches for HuggingFace, and this runs at startup on a production
            # PBX. Allowed, because a developer install has no staged model and this is a ~250 MB
            # one-off, but it should never be how a deployment is set up.
            self.logger.warning(
                f"No whisper_model_dir set; {self.settings.whisper_model!r} will be fetched from "
                "HuggingFace if it is not already cached. Stage the model locally instead."
            )

        try:
            self.model = WhisperModel(
                self.model_id,
                device="cpu",
                compute_type=self.settings.whisper_compute_type,
                # CTranslate2 defaults to *all* cores. Every active call owns an RTP relay
                # thread that must not miss its 20 ms cadence, so the ceiling is deliberate.
                # This argument alone is not enough -- the OpenMP backend overrides it, which
                # is why OMP_NUM_THREADS is pinned above, before the import.
                cpu_threads=self.settings.whisper_cpu_threads,
                # num_workers, *not* inter_threads. faster-whisper renames it on the way down
                # to CTranslate2, so passing the CTranslate2 name lands in **model_kwargs and
                # arrives as a duplicate: "got multiple values for keyword argument".
                num_workers=1,
                local_files_only=bool(self.settings.whisper_model_dir),
            )
        except Exception as e:
            # Catches broadly on purpose: a bad model name, an unreadable cache and an
            # unsupported compute_type each surface whatever ctranslate2 or huggingface_hub
            # feels like raising, and none of it is reliably an OSError.
            self.logger.error(f"Failed to load whisper model {self.model_id!r}: {e}")
            return

        self.logger.info(
            f"Whisper model loaded from {self.model_id} "
            f"({self.settings.whisper_compute_type}, {self.settings.whisper_cpu_threads} thread(s))"
        )

    @property
    def ready(self) -> bool:
        """Whether a transcription request can actually be served right now."""
        return self.model is not None

    def transcribe_file(
        self, path: Path, *, language: str | None = None, want_words: bool = False
    ) -> Transcript:
        """Transcribe a WAV file. Never raises; failures come back as an error Transcript."""
        requested = language or self.settings.language
        # faster-whisper wants ISO-639-1. Config carries "en-US", which it rejects outright.
        whisper_language = _iso_639_1(requested)
        started = time.monotonic()

        def failed(message: str) -> Transcript:
            self.logger.error(f"Whisper transcription failed: {message}")
            return Transcript.failure(
                message, provider=self.provider, language=requested, model=self.model_name
            )

        if not self.ready:
            return failed(f"Whisper model not loaded (model: {self.model_id})")

        try:
            pcm16, sample_rate = read_wav_as_pcm16(path)

            audio_duration = len(pcm16) / (PCM16_BYTES_PER_SAMPLE * float(sample_rate))
            if audio_duration > self.settings.max_audio_seconds:
                return failed(
                    f"Audio is {audio_duration:.0f}s, longer than the "
                    f"{self.settings.max_audio_seconds}s limit"
                )

            if sample_rate != WHISPER_SAMPLE_RATE:
                pcm16 = resample_pcm16(pcm16, sample_rate, WHISPER_SAMPLE_RATE)

            segments = self._recognise(pcm16, whisper_language, want_words=want_words)

        except (OSError, TypeError, ValueError) as e:
            return failed(str(e))

        text = " ".join(s.text for s in segments).strip()
        if _is_hallucination(text, audio_duration):
            # Logged at info, not debug: a discarded transcript is indistinguishable from
            # "the engine heard nothing" downstream, and that ambiguity has already cost
            # one debugging session.
            self.logger.info(
                f"Discarding likely hallucination from {path.name} "
                f"({audio_duration:.0f}s of audio): {text!r}"
            )
            text, segments = "", []

        # Empty text is a success: with VAD filtering, a caller who hangs up on the beep
        # produces no speech at all, and that is the ordinary case rather than a fault.
        return Transcript(
            text=text,
            segments=tuple(segments),
            language=requested,
            provider=self.provider,
            model=self.model_name,
            # Deliberately None -- see the module docstring. Word-level probabilities exist
            # below and are real, but averaging them into a headline "accuracy" figure is the
            # fabrication this field is guarding against.
            confidence=None,
            audio_duration=audio_duration,
            processing_duration=time.monotonic() - started,
        )

    def _recognise(self, pcm16: bytes, language: str | None, *, want_words: bool) -> list[Segment]:
        """Decode the audio and collect one Segment per decoded window."""
        import numpy as np

        samples = np.frombuffer(pcm16, dtype="<i2").astype("float32") / PCM16_FULL_SCALE

        raw_segments, _info = self.model.transcribe(
            samples,
            language=language,
            # beam_size=1 is greedy decoding. Beam search costs multiples of the runtime for a
            # marginal gain on clean audio, and this box is carrying live RTP.
            beam_size=self.settings.whisper_beam_size,
            # The four anti-hallucination controls. vad_filter drops non-speech before the
            # decoder ever sees it; the thresholds discard windows the model itself is unsure
            # about; condition_on_previous_text=False stops one hallucination from seeding the
            # next, which is what produces the infamous repetition loops.
            vad_filter=self.settings.whisper_vad_filter,
            # How Silero is allowed to cut. The default 2000 ms is too coarse for per-channel
            # call audio: the silence stripped from one participant's channel is the time the
            # other was speaking, so remarks either side of somebody else's turn reach the
            # decoder adjacent and come back as one segment spanning that turn. A shorter
            # threshold keeps the boundaries. Passed only when VAD is on, since it means
            # nothing otherwise.
            vad_parameters=(
                {
                    "min_silence_duration_ms": self.settings.whisper_vad_min_silence_ms,
                    # Silero's speech *probability*, not a level -- it has no power knob.
                    "threshold": self.settings.whisper_vad_threshold,
                    # Kept generous because the silence threshold above is short: cutting
                    # close to the speech is how a leading consonant gets clipped.
                    "speech_pad_ms": self.settings.whisper_vad_speech_pad_ms,
                }
                if self.settings.whisper_vad_filter
                else None
            ),
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
            log_prob_threshold=-1.0,
            compression_ratio_threshold=2.4,
            # Word timing costs roughly 20-30% and only the analytics consumers want it.
            word_timestamps=want_words,
        )

        segments: list[Segment] = []
        # transcribe() returns a generator; nothing is decoded until it is consumed, so the
        # timing above measures nothing unless this loop runs.
        for raw in raw_segments:
            text = (raw.text or "").strip()
            if not text:
                continue
            words = tuple(
                Word(
                    text=w.word.strip(),
                    start=float(w.start),
                    end=float(w.end),
                    confidence=float(w.probability),
                )
                for w in (getattr(raw, "words", None) or ())
            )
            segments.append(
                Segment(
                    text=text,
                    start=float(raw.start),
                    end=float(raw.end),
                    # Per-segment confidence is left unset for the same reason as the
                    # transcript's: avg_logprob is a log probability, not an accuracy.
                    confidence=None,
                    words=words,
                )
            )

        return segments


def _resolve_model(model_dir: str, model_name: str) -> str:
    """
    Work out what to hand CTranslate2, from a directory and a model name.

    ``whisper_model_dir`` is read as the directory models live *in*, so switching engines is
    one config key -- change ``whisper_model`` and the path follows. Without this the two keys
    can disagree silently, and the directory wins: a config naming ``base.en`` while pointing
    at a ``small.en`` directory loads small.en and says nothing.

    A directory holding ``model.bin`` directly is taken as the model itself, so installs that
    staged a single model into the configured path keep working untouched.
    """
    if not model_dir:
        return model_name
    root = Path(model_dir).expanduser()
    if (root / "model.bin").is_file():
        return str(root)
    return str(root / model_name)


def _iso_639_1(language: str) -> str | None:
    """
    Reduce a config language code to what faster-whisper accepts.

    ``en-US`` becomes ``en``; an empty or unset value becomes None, which tells whisper to
    detect the language itself. Detection is wrong more often than it is right on short,
    noisy telephony audio, so leaving the config value set is strongly preferred.
    """
    code = (language or "").strip().replace("_", "-")
    if not code:
        return None
    return code.split("-", 1)[0].lower()


def _is_hallucination(text: str, audio_duration: float) -> bool:
    """
    True when the whole transcript is one of Whisper's silence artefacts.

    Compared against the entire string rather than searched within it, and split into two
    cases. A pure artefact ("Subtitles by the Amara.org community") is never real speech and
    goes whatever the length. An ambiguous one ("Thank you.") is judged on duration: keeping it
    on a short recording risks a stray transcript, dropping it on a long one risks losing a
    genuine message -- and only the second is a message somebody was waiting for.
    """
    candidate = text.strip().lower()
    if not candidate:
        return False
    if candidate in HALLUCINATION_PHRASES:
        return True
    return candidate in AMBIGUOUS_PHRASES and audio_duration > AMBIGUOUS_PHRASE_MAX_SECONDS
