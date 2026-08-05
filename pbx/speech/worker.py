"""
Background transcription worker.

Transcription used to run inline in ``VoicemailBox.save_message``, on the thread tearing the
call down. This moves it onto its own queue and daemon threads, following
:class:`pbx.mail.dispatcher.Mailer` -- same lifecycle, same never-raises contract, same
drop-with-a-log behaviour when the queue fills.

The contract that matters to callers is :meth:`submit`:

    **If submit() returns True, the callback is guaranteed to fire exactly once.**
    **If it returns False, nothing was accepted and the caller must handle it itself.**

That is what lets a voicemail notification survive every failure mode: the caller sends its
email inline when submission is refused, and otherwise hands the decision to the callback.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pbx.speech.types import Transcript
from pbx.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pbx.speech.settings import TranscriptionSettings

#: How long a worker blocks on the queue before re-checking whether it should exit.
_POLL_INTERVAL = 0.5

#: Priority nudge applied to worker threads on Linux, where nice() is per-task. Transcription
#: is CPU-bound and shares the box with RTP relay threads that must not miss their 20 ms
#: cadence; this lets the kernel preempt transcription in their favour.
_WORKER_NICENESS = 10


@dataclass(slots=True)
class TranscriptionJob:
    """One unit of work: a file to transcribe and who to tell about it."""

    path: Path
    on_complete: Callable[[Transcript | None], None]
    language: str | None = None
    #: Free-text label used only in logs, e.g. a message id.
    label: str = ""
    want_words: bool = False
    submitted_at: float = field(default_factory=time.monotonic)


class TranscriptionWorker:
    """Runs transcription off the call path."""

    def __init__(
        self,
        backend: Any,
        settings: TranscriptionSettings,
        logger: Any | None = None,
    ) -> None:
        self.backend = backend
        self.settings = settings
        self.logger = logger or get_logger()

        self._queue: queue.Queue[TranscriptionJob] = queue.Queue(maxsize=settings.queue_size)
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._running = False
        self._in_flight = 0

        self._submitted = 0
        self._completed = 0
        self._failed = 0
        self._dropped = 0
        self._rtf_total = 0.0
        self._rtf_count = 0

    # ---------------------------------------------------------------- lifecycle

    @property
    def available(self) -> bool:
        """Whether a submission could be accepted right now."""
        if not self.settings.enabled:
            return False
        if not getattr(self.backend, "ready", False):
            return False
        return self.settings.runs_inline or self._running

    def start(self) -> None:
        """Spin up the worker threads. A no-op when configured to run inline."""
        if self.settings.runs_inline:
            self.logger.info("Transcription runs inline (workers=0); no background threads")
            return

        with self._lock:
            if self._running:
                return
            self._running = True
            self._threads = [
                threading.Thread(target=self._worker_loop, name=f"transcription-{n}", daemon=True)
                for n in range(self.settings.workers)
            ]
            for thread in self._threads:
                thread.start()

        self.logger.info(
            "Transcription worker started (%d thread(s), queue capacity %d)",
            self.settings.workers,
            self.settings.queue_size,
        )

    def stop(self, timeout: float = 15.0) -> None:
        """
        Stop accepting work, let the in-flight job finish, and release everything queued.

        Queued jobs are **not** transcribed during shutdown -- a single run can exceed the
        whole shutdown budget. Their callbacks are fired with ``None`` instead, which is what
        lets a pending voicemail still send its notification, just without a transcript.
        """
        with self._lock:
            if not self._running:
                self._release_queued()
                return
            self._running = False
            threads = list(self._threads)
            self._threads = []

        for thread in threads:
            thread.join(timeout=timeout)

        released = self._release_queued()
        self.logger.info(
            "Transcription worker stopped (%d queued job(s) released without a transcript)",
            released,
        )

    def _release_queued(self) -> int:
        """Fire every still-queued callback with no transcript. Never raises."""
        released = 0
        while True:
            try:
                job = self._queue.get_nowait()
            except queue.Empty:
                break
            self._invoke(job, None)
            released += 1
        return released

    # ---------------------------------------------------------------- submission

    def submit(
        self,
        path: Path,
        on_complete: Callable[[Transcript | None], None],
        *,
        language: str | None = None,
        label: str = "",
        want_words: bool = False,
        audio_seconds: float | None = None,
    ) -> bool:
        """
        Queue a file for transcription.

        Returns True only when the job was accepted, in which case `on_complete` is guaranteed
        to be called exactly once -- with a Transcript on success or failure, or with None if
        the process shuts down before the job runs. Returns False for every unservable
        condition, and the caller must then proceed without a transcript.

        `audio_seconds`, when the caller already knows it, lets an over-long recording be
        refused here rather than after it has occupied a worker.
        """
        if not self.settings.enabled:
            return False
        if not getattr(self.backend, "ready", False):
            return False
        if audio_seconds is not None and audio_seconds > self.settings.max_audio_seconds:
            self.logger.debug(
                "Not transcribing %s: %.0fs exceeds the %ds limit",
                label or path.name,
                audio_seconds,
                self.settings.max_audio_seconds,
            )
            return False
        if not path.is_file():
            self.logger.warning("Not transcribing %s: file is unreadable", path)
            return False

        job = TranscriptionJob(
            path=path,
            on_complete=on_complete,
            language=language,
            label=label or path.name,
            want_words=want_words,
        )

        # Inline mode runs the job on this thread. Used by tests, and a legitimate choice for
        # a deployment that would rather block than defer.
        if self.settings.runs_inline:
            self._submitted += 1
            self._run(job)
            return True

        if not self._running:
            return False

        try:
            self._queue.put_nowait(job)
        except queue.Full:
            self._dropped += 1
            self.logger.error(
                "Transcription queue is full (%d); %s will not be transcribed",
                self._queue.maxsize,
                job.label,
            )
            return False

        self._submitted += 1
        return True

    # ---------------------------------------------------------------- execution

    def _worker_loop(self) -> None:
        """Pull jobs until asked to stop. Never exits on an exception."""
        if sys.platform.startswith("linux"):
            try:
                os.nice(_WORKER_NICENESS)
            except OSError as e:  # pragma: no cover - depends on host policy
                self.logger.debug(f"Could not lower transcription thread priority: {e}")

        while True:
            try:
                job = self._queue.get(timeout=_POLL_INTERVAL)
            except queue.Empty:
                if not self._running:
                    return
                continue

            with self._lock:
                self._in_flight += 1
            try:
                self._run(job)
            finally:
                with self._lock:
                    self._in_flight -= 1

    def _run(self, job: TranscriptionJob) -> None:
        """Transcribe one job and notify. Every failure becomes an error Transcript."""
        try:
            transcript = self.backend.transcribe_file(
                job.path, language=job.language, want_words=job.want_words
            )
        except Exception as e:
            # A backend is supposed to return errors rather than raise, but a crash here must
            # never swallow the callback -- that is what would lose a voicemail notification.
            self.logger.error(f"Transcription of {job.label} raised: {e}")
            transcript = Transcript.failure(str(e), provider=getattr(self.backend, "provider", ""))

        if transcript.success:
            self._completed += 1
            if transcript.real_time_factor is not None:
                self._rtf_total += transcript.real_time_factor
                self._rtf_count += 1
        else:
            self._failed += 1

        self._invoke(job, transcript)

    def _invoke(self, job: TranscriptionJob, transcript: Transcript | None) -> None:
        """Call a job's callback, absorbing anything it throws."""
        try:
            job.on_complete(transcript)
        except Exception as e:
            self.logger.error(f"Transcription callback for {job.label} raised: {e}")

    # ---------------------------------------------------------------- reporting

    def stats(self) -> dict[str, Any]:
        """Counters for logging and the status API."""
        return {
            "enabled": self.settings.enabled,
            "ready": bool(getattr(self.backend, "ready", False)),
            "running": self._running,
            "runs_inline": self.settings.runs_inline,
            "workers": self.settings.workers,
            "queue_depth": self._queue.qsize(),
            "queue_capacity": self._queue.maxsize,
            "in_flight": self._in_flight,
            "submitted": self._submitted,
            "completed": self._completed,
            "failed": self._failed,
            "dropped": self._dropped,
            "mean_real_time_factor": (
                self._rtf_total / self._rtf_count if self._rtf_count else None
            ),
        }
