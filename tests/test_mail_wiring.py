"""
Proves the PBX is wired for email, without touching a network.

This is the layer between "the mail package works" (test_mail_*.py) and "this deployment can
reach Exchange" (which only the PBX host can answer). What it pins is the plumbing that was
broken before: emergency notification read ``pbx_core.email_notifier``, an attribute nothing
in pbx/core ever set, so Kari's Law email silently logged instead of sending. Nothing failed
loudly, and no test noticed, because no test asserted the wiring existed.
"""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from pbx.mail import Mailer, SmtpSettings

SMTP_CONFIG = {
    "host": "exchange.corp.local",
    "port": 587,
    "security": "starttls",
    "auth": "none",
    "from_address": "voicemail@corp.local",
}


def _pbx_core(smtp: dict[str, Any] | None) -> MagicMock:
    """A PBXCore stand-in whose config answers like the real one."""
    core = MagicMock()
    core.logger = MagicMock()
    core.database.enabled = False

    values: dict[str, Any] = {"smtp": smtp if smtp is not None else {}}

    def config_get(key: str, default: Any = None) -> Any:
        return values.get(key, default)

    core.config.get.side_effect = config_get
    return core


@pytest.mark.unit
class TestPBXCoreGetsAMailer:
    def test_mailer_is_set_on_pbx_core(self):
        from pbx.core.feature_initializer import FeatureInitializer

        core = _pbx_core(SMTP_CONFIG)
        with patch.object(FeatureInitializer, "initialize", FeatureInitializer.initialize):
            FeatureInitializer.initialize(core)

        assert isinstance(core.mailer, Mailer)
        assert core.mailer.enabled is True
        core.mailer.stop(timeout=2)

    def test_mailer_is_set_even_when_smtp_is_absent(self):
        """
        The disabled case is the important one. A Mailer that always exists is what removes
        the need for a hasattr() guard at every call site -- and a missing guard is exactly
        how emergency email came to be dead code.
        """
        from pbx.core.feature_initializer import FeatureInitializer

        core = _pbx_core(None)
        FeatureInitializer.initialize(core)

        assert isinstance(core.mailer, Mailer)
        assert core.mailer.enabled is False

    def test_disabled_mailer_reports_a_config_error_rather_than_crashing(self):
        from pbx.core.feature_initializer import FeatureInitializer

        core = _pbx_core(None)
        FeatureInitializer.initialize(core)

        result = core.mailer.send("someone@corp.local", "subject", "body")

        assert result.ok is False
        assert "not configured" in result.error.message

    def test_mailer_is_created_before_voicemail(self):
        """VoicemailSystem needs it at construction time, so ordering is load-bearing."""
        from pbx.core.feature_initializer import FeatureInitializer

        core = _pbx_core(SMTP_CONFIG)
        with patch("pbx.core.feature_initializer.VoicemailSystem") as vm:
            FeatureInitializer.initialize(core)

            assert vm.call_args.kwargs["mailer"] is core.mailer
        core.mailer.stop(timeout=2)


@pytest.mark.unit
class TestVoicemailReachesTheMailer:
    def test_saving_a_voicemail_queues_mail_through_the_shared_mailer(self, tmp_path):
        from pbx.features.voicemail import VoicemailSystem

        mailer = Mailer(SmtpSettings.from_dict(SMTP_CONFIG), transport_factory=MagicMock())
        config = MagicMock()
        config.get.side_effect = lambda key, default=None: {
            "voicemail.email.include_attachment": False,
            "voicemail.email.subject_template": "New Voicemail from {caller_id}",
        }.get(key, default)
        config.get_extension.return_value = {"number": "1001", "email": "user@corp.local"}

        system = VoicemailSystem(storage_path=str(tmp_path), config=config, mailer=mailer)
        system.save_message("1001", "5551234567", b"RIFF" + b"\x00" * 64, 5)

        assert mailer.get_statistics()["queued"] == 1


@pytest.mark.unit
class TestEmergencyReachesTheMailer:
    def test_emergency_notification_sends_through_pbx_core_mailer(self):
        """The path that never sent. It must call the mailer, not log an intention."""
        from pbx.features.emergency_notification import (
            EmergencyContact,
            EmergencyNotificationSystem,
        )

        core = _pbx_core(SMTP_CONFIG)
        core.mailer = MagicMock()
        core.mailer.send.return_value = MagicMock(ok=True, message_id="<x@y>", error=None)

        system = EmergencyNotificationSystem(pbx_core=core, config=MagicMock())
        contact = EmergencyContact(name="Front Desk", email="desk@corp.local")

        with patch("pbx.features.emergency_notification.get_audit_logger"):
            system._send_email_notification(contact, "911_call", {"caller": "1001"})

        core.mailer.send.assert_called_once()
        assert core.mailer.send.call_args[0][0] == "desk@corp.local"

    def test_no_hasattr_guard_remains_on_the_emergency_path(self):
        """
        Regression guard. The old code did `hasattr(self.pbx_core, "email_notifier")`, which
        turned a wiring mistake into a silent no-op instead of a visible failure.
        """
        import inspect

        from pbx.features.emergency_notification import EmergencyNotificationSystem

        source = inspect.getsource(EmergencyNotificationSystem._send_email_notification)

        assert "hasattr" not in source
        assert "email_notifier" not in source
        assert "self.pbx_core.mailer.send(" in source
