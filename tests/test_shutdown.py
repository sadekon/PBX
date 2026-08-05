#!/usr/bin/env python3
"""
Test PBX shutdown functionality
"""

import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock

import yaml

from pbx.core.pbx import PBXCore
from pbx.utils.graceful_shutdown import GracefulShutdownHandler


def test_pbx_shutdown() -> None:
    """Test that PBX shuts down properly when stop() is called"""

    # Create a minimal config file
    config_data = {
        "server": {
            "sip_host": "127.0.0.1",
            "sip_port": 15060,  # Use non-standard port for testing
            "external_ip": "127.0.0.1",
            "rtp_port_range_start": 20000,
            "rtp_port_range_end": 20100,
        },
        "api": {"host": "127.0.0.1", "port": 18080},  # Use non-standard port for testing
        "logging": {"level": "ERROR", "console": False},  # Reduce log noise during testing
        "extensions": [
            {
                "number": "1001",
                "name": "Test User",
                "password": "test1001",
                "email": "test@example.com",
            }
        ],
        "dialplan": {"internal_pattern": "^1[0-9]{3}$"},
        "features": {"call_recording": False, "voicemail": False},
        "voicemail": {"storage_path": "/tmp/test_voicemail"},
        "provisioning": {"enabled": False},
    }

    # Write config to temporary file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        yaml.dump(config_data, f)
        config_file = f.name

    try:
        # Create and start PBX
        pbx = PBXCore(config_file)
        assert pbx.start(), "PBX should start successfully"

        # Give it a moment to fully initialize
        time.sleep(2)

        # Verify it's running
        assert pbx.running, "PBX should be running"

        # Stop the PBX
        pbx.stop()

        # Give threads a moment to stop
        time.sleep(2)

        # Verify it stopped
        assert not pbx.running, "PBX should not be running after stop()"

    finally:
        # Clean up config file
        if Path(config_file).exists():
            Path(config_file).unlink()


def test_signal_handling_simulation() -> None:
    """Test that signal handling mechanism works"""

    # Create a minimal config file
    config_data = {
        "server": {
            "sip_host": "127.0.0.1",
            "sip_port": 15061,  # Use different port
            "external_ip": "127.0.0.1",
            "rtp_port_range_start": 20100,
            "rtp_port_range_end": 20200,
        },
        "api": {"host": "127.0.0.1", "port": 18081},  # Use different port
        "logging": {"level": "ERROR", "console": False},
        "extensions": [
            {
                "number": "1001",
                "name": "Test User",
                "password": "test1001",
                "email": "test@example.com",
            }
        ],
        "dialplan": {"internal_pattern": "^1[0-9]{3}$"},
        "features": {"call_recording": False, "voicemail": False},
        "voicemail": {"storage_path": "/tmp/test_voicemail"},
        "provisioning": {"enabled": False},
    }

    # Write config to temporary file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        yaml.dump(config_data, f)
        config_file = f.name

    running = True
    pbx = None

    def signal_handler_test() -> None:
        """Simulate the signal handler"""
        nonlocal running
        running = False
        if pbx:
            pbx.stop()

    try:
        # Create and start PBX
        pbx = PBXCore(config_file)
        assert pbx.start(), "PBX should start successfully"

        # Simulate the main loop
        loop_iterations = 0
        max_iterations = 5

        # After a few iterations, simulate Ctrl+C
        while running and loop_iterations < max_iterations:
            time.sleep(0.5)
            loop_iterations += 1

            # Simulate signal after 2 iterations
            if loop_iterations == 2:
                signal_handler_test()

        # Verify the loop exited because running was set to False
        assert not running, "Running flag should be False after signal"
        assert not pbx.running, "PBX should not be running"

    finally:
        # Clean up
        if Path(config_file).exists():
            Path(config_file).unlink()


class TestShutdownPreservesVoicemail:
    """
    A voicemail left while the PBX is shutting down must still reach its recipient.

    Both tests drive the shutdown code against a mock rather than a live PBX: the ordering is
    the whole point, and a real instance adds sockets and multi-second sleeps without making
    the assertion any stronger.
    """

    def test_mail_is_drained_after_active_calls_are_ended(self) -> None:
        """
        Ending a call can record a voicemail, and saving one queues a notification.

        stop() used to drain the mailer before ending active calls, so those notifications
        were handed to a worker that had already been joined and were silently lost.
        """
        order: list[str] = []
        core = MagicMock()
        call = MagicMock()
        call.call_id = "call-1"
        core.call_manager.get_active_calls.return_value = [call]
        core.end_call.side_effect = lambda _cid: order.append("end_call")
        core.mailer.stop.side_effect = lambda *a, **k: order.append("mailer.stop")

        PBXCore.stop(core)

        assert order == ["end_call", "mailer.stop"], (
            "active calls must be ended before the mailer is drained, or voicemail "
            "recorded during shutdown loses its notification"
        )

    def test_forced_shutdown_ends_calls_through_pbx_core(self) -> None:
        """
        The timeout path must use PBXCore.end_call, which saves the voicemail.

        CallManager.end_call only drops the call from the active dict -- no voicemail save, no
        CDR close, no RTP release -- so a caller mid-message when the PBX was signalled lost
        the recording outright.
        """
        core = MagicMock()
        call = MagicMock()
        call.call_id = "call-1"
        core.call_manager.get_active_calls.return_value = [call]
        handler = GracefulShutdownHandler(core, shutdown_timeout=30)

        # timeout=0 skips the grace period and goes straight to the force-end branch.
        handler._wait_for_calls_to_complete(timeout=0)

        core.end_call.assert_called_once_with("call-1")
        core.call_manager.end_call.assert_not_called()

    def test_forced_shutdown_continues_after_a_failing_call(self) -> None:
        """One call failing to end must not strand the rest."""
        core = MagicMock()
        first, second = MagicMock(), MagicMock()
        first.call_id, second.call_id = "bad", "good"
        core.call_manager.get_active_calls.return_value = [first, second]
        core.end_call.side_effect = lambda cid: (
            (_ for _ in ()).throw(RuntimeError("boom")) if cid == "bad" else None
        )
        handler = GracefulShutdownHandler(core, shutdown_timeout=30)

        handler._wait_for_calls_to_complete(timeout=0)

        assert core.end_call.call_count == 2
