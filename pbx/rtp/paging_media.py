"""
RTP fan-out for overhead paging.

A page is one caller's audio arriving on one socket and leaving to several endpoints at once
-- one per amplifier circuit. Neither existing mechanism can do that:

    RTPRelayHandler is strictly two-party. set_endpoints() takes an A and a B, and the relay
    loop performs a single sendto per side. There is no third target.

    AudioTap looks like the answer -- it copies RTP off the media thread into a bounded queue
    and drops rather than stalling the call -- but in _relay_loop the tap.feed() call sits
    *below* the `if not target: continue` guard. A leg with no B endpoint never reaches it, so
    a zone whose only destination has not answered yet, and later a multicast-only zone with
    no SIP leg at all, would tap nothing.

So paging owns its own socket and its own loop: receive from the pager, send to every endpoint
currently answered. Packets are forwarded verbatim rather than rebuilt, which keeps the
pager's sequence numbers, timestamps and SSRC intact across every leg -- the same thing an RTP
relay does, and what endpoints expect.

Not symmetric, but not one-way either. The pager's audio goes to every destination; a
destination's audio goes only back to the pager, never to the other destinations. That
asymmetry is the point: an amplifier answers a keypress with its own prompt or confirmation
tone, and since everything past the FXS port is analog, that tone is the only evidence the
right zone opened. Fanning it sideways would put one circuit's noise onto every other.
"""

import contextlib
import socket
import threading
from typing import Any

from pbx.utils.logger import get_logger

#: Big enough for any RTP packet an endpoint will send; matches RTPRelayHandler's read size.
_READ_BYTES = 2048

#: How long the receive loop blocks before re-checking whether it should still be running.
#: Short enough that stop() returns promptly, long enough not to spin.
_RECV_TIMEOUT_SECONDS = 0.5


class PagingMediaSession:
    """
    Receives the pager's RTP on one port and copies it to every answered destination.

    Destinations are added as their legs answer and removed as they fail or hang up, so a page
    can start streaming to the first amplifier that picks up without waiting for the slowest.
    """

    def __init__(
        self,
        local_port: int,
        page_id: str,
        logger: Any | None = None,
    ) -> None:
        """
        Initialize the session.

        Args:
            local_port: UDP port to receive the pager's RTP on
            page_id: Owning page, for log lines
            logger: Optional logger override
        """
        self.local_port = local_port
        self.page_id = page_id
        self.logger = logger or get_logger()

        self.socket: socket.socket | None = None
        self.running = False
        self._thread: threading.Thread | None = None

        # destination_id -> (host, port)
        self._targets: dict[int, tuple[str, int]] = {}
        self._targets_lock = threading.Lock()

        #: Where the pager's audio is actually coming from. Learned from the first packet
        #: rather than trusted from its SDP, for the same reason RTPRelayHandler learns it:
        #: a phone behind NAT advertises an address its packets do not come from, and
        #: filtering on the advertised one drops the whole page.
        self._source: tuple[str, int] | None = None
        self._expected_source: tuple[str, int] | None = None

        self.packets_received = 0
        self.packets_dropped_no_targets = 0
        #: Packets sent back from an amplifier to the pager -- its prompt and confirmation
        #: tones, which are the only feedback that a zone selection worked.
        self.packets_returned = 0
        #: destination_id -> count
        self.packets_sent: dict[int, int] = {}

    def start(self, expected_source: tuple[str, int] | None = None) -> bool:
        """
        Bind the socket and begin receiving.

        Args:
            expected_source: The pager's advertised RTP address, used only to log when the
                learned source differs from it.

        Returns:
            bool: True if the session is listening
        """
        if self.running:
            return True

        self._expected_source = expected_source
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind(("0.0.0.0", self.local_port))
            self.socket.settimeout(_RECV_TIMEOUT_SECONDS)
        except OSError as e:
            self.logger.error(f"Paging media session could not bind port {self.local_port}: {e}")
            self._close_socket()
            return False

        self.running = True
        self._thread = threading.Thread(
            target=self._receive_loop, name=f"paging-media-{self.page_id[:16]}", daemon=True
        )
        self._thread.start()
        self.logger.info(
            f"Paging media session listening on port {self.local_port} for page {self.page_id}"
        )
        return True

    def add_target(self, destination_id: int, endpoint: tuple[str, int]) -> None:
        """
        Start sending to a destination whose leg has answered.

        Args:
            destination_id: Destination this endpoint belongs to
            endpoint: (host, port) taken from the leg's answer SDP -- never guessed. The
                previous implementation sent to the PBX's own advertised port on the ATA's
                IP, which is why no audio ever arrived.
        """
        with self._targets_lock:
            self._targets[destination_id] = endpoint
            self.packets_sent.setdefault(destination_id, 0)
        self.logger.info(
            f"Page {self.page_id}: streaming to destination {destination_id} "
            f"at {endpoint[0]}:{endpoint[1]}"
        )

    def remove_target(self, destination_id: int) -> None:
        """
        Stop sending to a destination.

        Args:
            destination_id: Destination to drop
        """
        with self._targets_lock:
            removed = self._targets.pop(destination_id, None)
        if removed:
            self.logger.info(f"Page {self.page_id}: stopped streaming to {destination_id}")

    def target_count(self) -> int:
        """
        How many destinations are currently receiving.

        Returns:
            int: Live target count
        """
        with self._targets_lock:
            return len(self._targets)

    def _receive_loop(self) -> None:
        """Relay the pager out to every destination, and the destinations back to the pager."""
        sock = self.socket
        if sock is None:
            return

        while self.running:
            try:
                data, addr = sock.recvfrom(_READ_BYTES)
            except TimeoutError:
                continue
            except OSError:
                # Socket closed under us by stop(); the loop condition catches it.
                if self.running:
                    self.logger.debug(f"Paging media socket error on page {self.page_id}")
                continue

            with self._targets_lock:
                targets = list(self._targets.items())
            target_endpoints = {endpoint for _, endpoint in targets}

            # Learn the pager from the first packet that is not already a known destination.
            # Ordering makes this safe in practice -- the pager starts sending the moment it
            # gets our 200 OK, before any leg answers -- but an amplifier that answered
            # first would otherwise be mistaken for the pager, and its audio then fanned out
            # to every other amplifier.
            if self._source is None and addr not in target_endpoints:
                self._source = addr
                if self._expected_source and addr != self._expected_source:
                    self.logger.info(
                        f"Page {self.page_id}: pager audio arriving from {addr[0]}:{addr[1]}, "
                        f"not the {self._expected_source[0]}:{self._expected_source[1]} it "
                        f"advertised -- using the address the packets came from"
                    )

            if addr in target_endpoints:
                # An amplifier talking back. It is not mixed into the page: it goes only to
                # the pager, so pressing a zone digit produces the amp's own prompt or
                # confirmation tone in the pager's ear. That tone is the only evidence the
                # selection landed -- everything past the FXS port is analog and silent to
                # the PBX -- so discarding it, as this loop used to, left the person paging
                # with no way to tell which zone they had opened.
                if self._source is not None:
                    try:
                        sock.sendto(data, self._source)
                        self.packets_returned += 1
                    except OSError as e:
                        self.logger.debug(f"Page {self.page_id}: return to pager failed: {e}")
                continue

            if self._source is not None and addr != self._source:
                # Neither the pager nor a destination we are streaming to.
                continue

            self.packets_received += 1

            if not targets:
                self.packets_dropped_no_targets += 1
                continue

            for destination_id, endpoint in targets:
                try:
                    sock.sendto(data, endpoint)
                    self.packets_sent[destination_id] = self.packets_sent.get(destination_id, 0) + 1
                except OSError as e:
                    # One unreachable circuit must not silence the others, so this is counted
                    # and stepped over rather than raised.
                    self.logger.debug(
                        f"Page {self.page_id}: send to destination {destination_id} failed: {e}"
                    )

    def stop(self) -> None:
        """Stop receiving, close the socket, and log what the page actually delivered."""
        if not self.running:
            return

        self.running = False
        self._close_socket()

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

        sent = ", ".join(f"{dest}={count}" for dest, count in sorted(self.packets_sent.items()))
        self.logger.info(
            f"Paging media session for {self.page_id} stopped: "
            f"{self.packets_received} received, sent [{sent or 'none'}]"
            + (
                f", {self.packets_dropped_no_targets} dropped with no destination up"
                if self.packets_dropped_no_targets
                else ""
            )
        )

    def _close_socket(self) -> None:
        """Close the socket, ignoring an already-closed one."""
        if self.socket is not None:
            with contextlib.suppress(OSError):
                self.socket.close()
            self.socket = None

    def get_stats(self) -> dict[str, Any]:
        """
        Counters for the admin view.

        Returns:
            dict: Received, per-destination sent, and drops
        """
        with self._targets_lock:
            targets = dict(self._targets)
        return {
            "local_port": self.local_port,
            "packets_received": self.packets_received,
            "packets_returned": self.packets_returned,
            "packets_dropped_no_targets": self.packets_dropped_no_targets,
            "packets_sent": dict(self.packets_sent),
            "targets": {str(k): f"{v[0]}:{v[1]}" for k, v in targets.items()},
        }
