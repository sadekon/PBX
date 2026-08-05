"""
One way to run something on a timer.

Three places in this codebase needed a background loop and each grew its own: a daemon thread,
a ``running`` flag, a sleep, and a try/except. They differed in exactly the details that matter
at shutdown -- one slept in one-second slices so it stopped promptly, another slept the whole
interval and could hold shutdown for five minutes, and only one gave its thread a name, so the
other was anonymous in a stack dump.

This is that loop, written once.

Sleeping is done with an :class:`threading.Event`, not :func:`time.sleep`. Waiting on an event
returns the instant it is set, so a task with an hour-long interval still stops immediately --
which is what lets a sweep run on a slow schedule without becoming a shutdown problem.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from pbx.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["PeriodicTask"]

#: How long to wait before retrying after the task raises. Deliberately short relative to a
#: long interval: a task that failed once should be retried soon, not in an hour.
DEFAULT_ERROR_INTERVAL = 60.0


class PeriodicTask:
    """
    Runs a callable on an interval, on a named daemon thread.

    The callable owns its own correctness; this owns only the timing. An exception is logged
    and the loop continues, because a background sweep that dies silently on one bad file is
    worse than one that complains every interval.
    """

    def __init__(
        self,
        name: str,
        interval: float,
        run: Callable[[], Any],
        *,
        logger: Any | None = None,
        error_interval: float = DEFAULT_ERROR_INTERVAL,
        run_on_start: bool = False,
    ) -> None:
        """
        Args:
            name: Thread name. Shows up in stack dumps and ``ps -T``; make it identifiable.
            interval: Seconds between runs.
            run: What to call. Takes nothing, returns anything, must not block forever.
            logger: Defaults to the shared PBX logger.
            error_interval: Seconds to wait after a raise, instead of the normal interval.
            run_on_start: Run once immediately rather than waiting out the first interval.
                Off by default -- a sweep firing during startup competes with everything else
                booting, and one interval of delay costs nothing.
        """
        self.name = name
        self.interval = interval
        self.run = run
        self.logger = logger or get_logger()
        self.error_interval = error_interval
        self.run_on_start = run_on_start

        self._thread: threading.Thread | None = None
        #: Set to stop. Also what the loop waits on, so stopping is immediate rather than
        #: taking up to `interval` seconds.
        self._stop = threading.Event()

    @property
    def running(self) -> bool:
        """Whether the loop is alive and has not been asked to stop."""
        return self._thread is not None and self._thread.is_alive() and not self._stop.is_set()

    def start(self) -> None:
        """Begin running. Calling this twice is a no-op rather than a second thread."""
        if self.running:
            self.logger.debug(f"{self.name} is already running")
            return

        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=self.name, daemon=True)
        self._thread.start()
        self.logger.info(f"{self.name} started (every {self.interval:.0f}s)")

    def stop(self, timeout: float = 5.0) -> None:
        """
        Ask the loop to finish and wait briefly for it.

        A run already in progress is not interrupted -- there is no safe way to do that -- so
        the timeout bounds how long we wait, not how long the task may take.
        """
        if self._thread is None:
            return

        self._stop.set()
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            self.logger.warning(f"{self.name} did not stop within {timeout:.0f}s; leaving it")
        else:
            self.logger.info(f"{self.name} stopped")
        self._thread = None

    def _loop(self) -> None:
        """Wait, run, repeat, until stopped."""
        # wait() returns True when the event was set, i.e. we are stopping rather than
        # waking up for the next run.
        if not self.run_on_start and self._stop.wait(self.interval):
            return

        while not self._stop.is_set():
            delay = self.interval
            try:
                self.run()
            except Exception as e:
                # Broad on purpose. The alternative is a background thread that dies on the
                # first unexpected input and is never noticed again.
                self.logger.error(f"{self.name} failed: {e}", exc_info=True)
                delay = self.error_interval

            if self._stop.wait(delay):
                return
