"""
Queueing, retry and statistics -- the object the rest of the PBX actually holds.

Two entry points, and the choice between them is about where the caller is standing:

* :meth:`Mailer.send` blocks and hands back a real result. Use it where the outcome matters
  and the caller is not on a latency-critical path (emergency notification).
* :meth:`Mailer.send_async` hands the message to a worker thread and returns immediately.
  Use it anywhere a call is being torn down -- SMTP to a dead server takes as long as the
  connect timeout, and nothing about ending a call should wait for that.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pbx.mail.client import SendResult, SmtpClient
from pbx.mail.errors import EmailConfigError, classify
from pbx.mail.message import build_message
from pbx.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from email.message import EmailMessage

    from pbx.mail.client import TransportFactory
    from pbx.mail.message import Attachment
    from pbx.mail.settings import SmtpSettings

__all__ = ["Mailer"]

#: Bounded so a wedged mail server cannot turn into unbounded memory growth. A PBX sends
#: tens of messages an hour; 500 pending means something is very wrong already.
DEFAULT_QUEUE_SIZE = 500

#: Ceiling on a single backoff sleep, so a large max_retries cannot park a message for hours.
MAX_BACKOFF_SECONDS = 60.0


@dataclass(slots=True)
class _QueuedMessage:
    """A message awaiting delivery, plus what we have already tried."""

    message: EmailMessage
    attempts: int = 0
    label: str = ""


@dataclass(slots=True)
class _Statistics:
    """Delivery counters. Read through Mailer.get_statistics()."""

    queued: int = 0
    sent: int = 0
    failed: int = 0
    dropped: int = 0
    retries: int = 0
    last_error: str | None = None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None


class Mailer:
    """
    Owns the send queue and the retry policy.

    Constructed unconditionally, even when SMTP is unconfigured. A disabled Mailer answers
    :meth:`send` with an :class:`EmailConfigError` result and drops async sends with a log
    line. That is deliberate: the previous design required every caller to guard with
    ``hasattr``, and the one place that forgot is why Kari's Law email never sent.
    """

    def __init__(
        self,
        settings: SmtpSettings,
        *,
        transport_factory: TransportFactory | None = None,
        queue_size: int = DEFAULT_QUEUE_SIZE,
    ) -> None:
        self.settings = settings
        self.logger = get_logger()
        self.client = SmtpClient(settings, transport_factory=transport_factory)

        self._queue: queue.Queue[_QueuedMessage] = queue.Queue(maxsize=queue_size)
        self._worker: threading.Thread | None = None
        self._running = False
        self._shutdown = threading.Event()
        self._state_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._stats = _Statistics()

        if not settings.is_configured:
            self.logger.info("Mailer disabled: SMTP is not configured (smtp.host/from_address)")
        else:
            problems = settings.validate()
            for problem in problems:
                self.logger.warning("SMTP configuration: %s", problem)
            if settings.redirect_to:
                # Loud on purpose: left on in production this silently stops every
                # notification reaching the person it was meant for.
                self.logger.warning(
                    "ALL MAIL IS BEING REDIRECTED TO %s -- no real recipient will receive "
                    "anything. This is a testing aid; unset smtp.redirect_to for production.",
                    settings.redirect_to,
                )
            self.logger.info(
                "Mailer ready: %s:%d security=%s auth=%s from=%s",
                settings.host,
                settings.port,
                settings.security,
                settings.auth,
                settings.from_address,
            )

    @property
    def enabled(self) -> bool:
        """True when there is enough configuration to attempt a send."""
        return self.settings.is_configured

    def start(self) -> None:
        """Start the delivery worker. Idempotent, and a no-op when unconfigured."""
        if not self.enabled:
            self.logger.debug("Mailer not started: SMTP is not configured")
            return

        with self._state_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._shutdown.clear()
            self._running = True
            self._worker = threading.Thread(
                target=self._worker_loop, name="mailer-delivery", daemon=True
            )
            self._worker.start()

        self.logger.info("Mail delivery worker started")

    def stop(self, timeout: float = 15.0) -> None:
        """
        Drain the queue and stop the worker.

        Named ``stop`` because :class:`~pbx.utils.graceful_shutdown.GracefulShutdownHandler`
        calls ``.stop()`` by name off a hardcoded list of subsystem attributes.
        """
        with self._state_lock:
            worker = self._worker
            if worker is None:
                self._running = False
                return
            self._running = False

        # Wakes any in-progress retry sleep so shutdown is not held up by backoff.
        self._shutdown.set()
        worker.join(timeout=timeout)

        if worker.is_alive():
            self.logger.warning(
                "Mail delivery worker did not stop within %.0fs; %d message(s) undelivered",
                timeout,
                self._queue.qsize(),
            )
        else:
            self.logger.info("Mail delivery worker stopped")

        with self._state_lock:
            self._worker = None

    def send(
        self,
        to: str | Iterable[str],
        subject: str,
        body: str,
        **kwargs: Any,
    ) -> SendResult:
        """
        Build and deliver a message on the calling thread.

        Never raises: a build failure comes back as a failed result, exactly like a wire
        failure, so callers have one thing to check.
        """
        if not self.enabled:
            return SendResult(
                ok=False,
                error=EmailConfigError(
                    "SMTP is not configured", detail="Set smtp.host and smtp.from_address"
                ),
            )

        try:
            message = self._build(to, subject, body, **kwargs)
        except Exception as exc:
            error = classify(exc)
            self._record_failure(error.message)
            return SendResult(ok=False, error=error)

        result = self.client.send(message)
        self._record_result(result)
        return result

    def send_async(
        self,
        to: str | Iterable[str],
        subject: str,
        body: str,
        **kwargs: Any,
    ) -> None:
        """
        Queue a message for background delivery. Never raises, never blocks.

        Safe to call before :meth:`start`; the message waits in the queue and goes out when
        the worker comes up.
        """
        if not self.enabled:
            self.logger.debug("Mail not queued: SMTP is not configured (subject=%r)", subject)
            self._record_drop()
            return

        try:
            message = self._build(to, subject, body, **kwargs)
        except Exception as exc:
            error = classify(exc)
            self.logger.error("Could not build message (subject=%r): %s", subject, error)
            self._record_failure(error.message)
            return

        item = _QueuedMessage(message=message, label=subject)
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            self._record_drop()
            self.logger.error(
                "Mail queue is full (%d); dropping message (subject=%r)",
                self._queue.maxsize,
                subject,
            )
            return

        with self._stats_lock:
            self._stats.queued += 1

    def verify(self) -> SendResult:
        """Connect, secure and authenticate without sending. See :meth:`SmtpClient.verify`."""
        if not self.enabled:
            return SendResult(
                ok=False,
                error=EmailConfigError(
                    "SMTP is not configured", detail="Set smtp.host and smtp.from_address"
                ),
            )
        return self.client.verify()

    def get_statistics(self) -> dict[str, Any]:
        """Counters and last-seen state, for the status API and for support calls."""
        with self._stats_lock:
            return {
                "enabled": self.enabled,
                "running": self._running,
                "queue_depth": self._queue.qsize(),
                "queue_capacity": self._queue.maxsize,
                "queued": self._stats.queued,
                "sent": self._stats.sent,
                "failed": self._stats.failed,
                "dropped": self._stats.dropped,
                "retries": self._stats.retries,
                "last_error": self._stats.last_error,
                "last_success_at": (
                    self._stats.last_success_at.isoformat() if self._stats.last_success_at else None
                ),
                "last_failure_at": (
                    self._stats.last_failure_at.isoformat() if self._stats.last_failure_at else None
                ),
                "settings": self.settings.redacted(),
            }

    def _build(
        self,
        to: str | Iterable[str],
        subject: str,
        body: str,
        *,
        attachments: Iterable[Attachment] = (),
        headers: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> EmailMessage:
        return build_message(
            self.settings,
            to,
            subject,
            body,
            attachments=attachments,
            headers=headers,
            **kwargs,
        )

    def _worker_loop(self) -> None:
        """Drain the queue until stopped, then finish whatever is still pending."""
        while self._running or not self._queue.empty():
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue

            try:
                self._deliver(item)
            finally:
                self._queue.task_done()

    def _deliver(self, item: _QueuedMessage) -> None:
        """Send one queued message, retrying only failures the taxonomy calls retryable."""
        while True:
            item.attempts += 1
            result = self.client.send(item.message)
            self._record_result(result)

            if result.ok or not result.retryable:
                return

            if item.attempts > self.settings.max_retries:
                self.logger.error(
                    "Giving up on mail (subject=%r) after %d attempt(s): %s",
                    item.label,
                    item.attempts,
                    result.error,
                )
                return

            if not self._running:
                self.logger.warning(
                    "Shutting down; abandoning retry of mail (subject=%r) after %d attempt(s)",
                    item.label,
                    item.attempts,
                )
                return

            delay = min(self.settings.retry_backoff**item.attempts, MAX_BACKOFF_SECONDS)
            with self._stats_lock:
                self._stats.retries += 1
            self.logger.warning(
                "Mail attempt %d/%d failed (subject=%r): %s; retrying in %.1fs",
                item.attempts,
                self.settings.max_retries + 1,
                item.label,
                result.error,
                delay,
            )

            # Event.wait rather than time.sleep so stop() cuts the backoff short.
            if self._shutdown.wait(delay):
                self.logger.warning(
                    "Shutdown during backoff; abandoning mail (subject=%r)", item.label
                )
                return

    def _record_result(self, result: SendResult) -> None:
        if result.ok:
            with self._stats_lock:
                self._stats.sent += 1
                self._stats.last_success_at = datetime.now(tz=UTC)
        else:
            self._record_failure(str(result.error) if result.error else "unknown failure")

    def _record_failure(self, message: str) -> None:
        with self._stats_lock:
            self._stats.failed += 1
            self._stats.last_error = message
            self._stats.last_failure_at = datetime.now(tz=UTC)

    def _record_drop(self) -> None:
        with self._stats_lock:
            self._stats.dropped += 1
