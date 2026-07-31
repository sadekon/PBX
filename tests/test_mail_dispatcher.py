"""Tests for pbx.mail.dispatcher -- queueing, retry policy, shutdown and statistics."""

import smtplib
import threading
import time

import pytest

from conftest import FakeSmtp
from pbx.mail import Mailer, SmtpSettings


def make_mailer(settings, transport, **kwargs):
    return Mailer(settings, transport_factory=lambda _s: transport, **kwargs)


def wait_until(predicate, timeout=5.0):
    """Poll until predicate holds. Keeps thread tests from being timing-fragile."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.mark.unit
class TestEnabledState:
    def test_unconfigured_mailer_is_disabled(self):
        assert Mailer(SmtpSettings()).enabled is False

    def test_configured_mailer_is_enabled(self, smtp_settings):
        assert Mailer(smtp_settings).enabled is True

    def test_disabled_send_returns_a_config_error_rather_than_raising(self):
        result = Mailer(SmtpSettings()).send("a@corp.local", "s", "b")

        assert result.ok is False
        assert "not configured" in result.error.message

    def test_disabled_send_async_is_silent_and_counts_a_drop(self):
        mailer = Mailer(SmtpSettings())

        mailer.send_async("a@corp.local", "s", "b")

        assert mailer.get_statistics()["dropped"] == 1

    def test_disabled_start_is_a_no_op(self):
        mailer = Mailer(SmtpSettings())
        mailer.start()

        assert mailer.get_statistics()["running"] is False


@pytest.mark.unit
class TestSynchronousSend:
    def test_send_delivers_and_counts(self, smtp_settings):
        transport = FakeSmtp()
        mailer = make_mailer(smtp_settings, transport)

        result = mailer.send("user@corp.local", "Subject", "body")

        assert result.ok is True
        assert len(transport.sent) == 1
        assert mailer.get_statistics()["sent"] == 1

    def test_build_failure_comes_back_as_a_result_not_an_exception(self, smtp_settings):
        mailer = make_mailer(smtp_settings, FakeSmtp())

        result = mailer.send("not-an-address", "Subject", "body")

        assert result.ok is False
        assert mailer.get_statistics()["failed"] == 1

    def test_send_does_not_retry(self, smtp_settings):
        """Retry belongs to the queue worker; a synchronous caller decides for itself."""
        transport = FakeSmtp(fail_times=5)
        mailer = make_mailer(smtp_settings, transport)

        result = mailer.send("user@corp.local", "Subject", "body")

        assert result.ok is False
        assert transport.conversation.count("send_message:from=None") == 1


@pytest.mark.unit
class TestAsynchronousDelivery:
    def test_queued_message_is_delivered_by_the_worker(self, smtp_settings):
        transport = FakeSmtp()
        mailer = make_mailer(smtp_settings, transport)
        mailer.start()
        try:
            mailer.send_async("user@corp.local", "Subject", "body")

            assert wait_until(lambda: len(transport.sent) == 1)
        finally:
            mailer.stop(timeout=5)

    def test_send_async_before_start_is_delivered_once_started(self, smtp_settings):
        transport = FakeSmtp()
        mailer = make_mailer(smtp_settings, transport)

        mailer.send_async("user@corp.local", "Subject", "body")
        mailer.start()
        try:
            assert wait_until(lambda: len(transport.sent) == 1)
        finally:
            mailer.stop(timeout=5)

    def test_unbuildable_message_is_dropped_without_raising(self, smtp_settings):
        mailer = make_mailer(smtp_settings, FakeSmtp())

        mailer.send_async("not-an-address", "Subject", "body")

        assert mailer.get_statistics()["failed"] == 1

    def test_full_queue_drops_with_a_counter(self, smtp_settings):
        mailer = make_mailer(smtp_settings, FakeSmtp(), queue_size=2)

        for _ in range(5):
            mailer.send_async("user@corp.local", "Subject", "body")

        stats = mailer.get_statistics()
        assert stats["dropped"] == 3
        assert stats["queue_depth"] == 2


@pytest.mark.unit
class TestRetryPolicy:
    def test_transient_failure_is_retried_then_succeeds(self, smtp_settings):
        transport = FakeSmtp(fail_times=1)
        mailer = make_mailer(smtp_settings, transport)
        mailer.start()
        try:
            mailer.send_async("user@corp.local", "Subject", "body")

            assert wait_until(lambda: len(transport.sent) == 1)
            assert mailer.get_statistics()["retries"] >= 1
        finally:
            mailer.stop(timeout=5)

    def test_permanent_failure_is_not_retried(self, smtp_settings):
        transport = FakeSmtp(
            fail_times=5, failure=smtplib.SMTPResponseException(550, b"5.7.1 rejected")
        )
        mailer = make_mailer(smtp_settings, transport)
        mailer.start()
        try:
            mailer.send_async("user@corp.local", "Subject", "body")

            assert wait_until(lambda: mailer.get_statistics()["failed"] >= 1)
            time.sleep(0.15)
            assert transport.conversation.count("send_message:from=None") == 1
            assert mailer.get_statistics()["retries"] == 0
        finally:
            mailer.stop(timeout=5)

    def test_retries_stop_at_max_retries(self, smtp_settings):
        transport = FakeSmtp(fail_times=99)
        mailer = make_mailer(smtp_settings, transport)
        mailer.start()
        try:
            mailer.send_async("user@corp.local", "Subject", "body")
            # max_retries=2 means one initial attempt plus two retries.
            assert wait_until(lambda: transport.conversation.count("send_message:from=None") == 3)
            time.sleep(0.15)
            assert transport.conversation.count("send_message:from=None") == 3
        finally:
            mailer.stop(timeout=5)


@pytest.mark.unit
class TestShutdown:
    def test_stop_joins_the_worker(self, smtp_settings):
        mailer = make_mailer(smtp_settings, FakeSmtp())
        mailer.start()

        mailer.stop(timeout=5)

        assert mailer.get_statistics()["running"] is False
        assert not any(t.name == "mailer-delivery" for t in threading.enumerate())

    def test_stop_drains_queued_mail(self, smtp_settings):
        """A SIGTERM with mail pending must not silently discard it."""
        transport = FakeSmtp()
        mailer = make_mailer(smtp_settings, transport)

        for _ in range(5):
            mailer.send_async("user@corp.local", "Subject", "body")
        mailer.start()
        mailer.stop(timeout=10)

        assert len(transport.sent) == 5

    def test_stop_without_start_is_safe(self, smtp_settings):
        make_mailer(smtp_settings, FakeSmtp()).stop(timeout=1)

    def test_start_is_idempotent(self, smtp_settings):
        mailer = make_mailer(smtp_settings, FakeSmtp())
        mailer.start()
        mailer.start()
        try:
            workers = [t for t in threading.enumerate() if t.name == "mailer-delivery"]
            assert len(workers) == 1
        finally:
            mailer.stop(timeout=5)


@pytest.mark.unit
class TestStatistics:
    def test_statistics_shape_and_redaction(self, smtp_settings):
        settings = SmtpSettings(
            host=smtp_settings.host,
            from_address=smtp_settings.from_address,
            password="hunter2",
        )
        mailer = make_mailer(settings, FakeSmtp())

        mailer.send("user@corp.local", "Subject", "body")
        stats = mailer.get_statistics()

        assert stats["sent"] == 1
        assert stats["last_success_at"] is not None
        assert stats["settings"]["password"] == "***"
        assert "hunter2" not in str(stats)

    def test_last_error_is_recorded(self, smtp_settings):
        mailer = make_mailer(smtp_settings, FakeSmtp(fail_times=1))

        mailer.send("user@corp.local", "Subject", "body")

        assert mailer.get_statistics()["last_error"]
        assert mailer.get_statistics()["last_failure_at"] is not None
