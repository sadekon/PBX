"""Tests for pbx.mail.settings -- config coercion, validation and redaction."""

import pytest

from pbx.mail.settings import PASSWORD_ENV_VAR, SmtpSettings


@pytest.mark.unit
class TestFromDict:
    """Building settings from the smtp: config section."""

    def test_empty_dict_yields_unconfigured_defaults(self):
        settings = SmtpSettings.from_dict({})

        assert settings.host == ""
        assert settings.security == "starttls"
        assert settings.auth == "none"
        assert settings.is_configured is False

    def test_reads_every_field(self, monkeypatch):
        monkeypatch.setenv(PASSWORD_ENV_VAR, "s3cret")
        settings = SmtpSettings.from_dict(
            {
                "host": "exchange.corp.local",
                "port": 465,
                "security": "smtps",
                "auth": "login",
                "username": "pbx",
                "from_address": "voicemail@corp.local",
                "from_name": "Corp Voicemail",
                "verify_cert": False,
                "ca_file": "/etc/ssl/corp.pem",
                "helo_hostname": "pbx.corp.local",
                "timeout": 30,
                "max_retries": 5,
                "max_attachment_bytes": 1024,
            }
        )

        assert settings.host == "exchange.corp.local"
        assert settings.port == 465
        assert settings.security == "smtps"
        assert settings.auth == "login"
        assert settings.password == "s3cret"
        assert settings.verify_cert is False
        assert settings.timeout == 30.0
        assert settings.is_configured is True

    def test_password_never_comes_from_config(self, monkeypatch):
        """A password in config.yml is inert; only the environment is consulted."""
        monkeypatch.delenv(PASSWORD_ENV_VAR, raising=False)

        settings = SmtpSettings.from_dict({"host": "h", "password": "from-config-file"})

        assert settings.password == ""

    def test_unresolved_placeholder_is_treated_as_unset(self):
        """An unset ${VAR} survives resolution as literal text and must not be used as a host."""
        settings = SmtpSettings.from_dict({"host": "${SMTP_HOST}", "port": "${SMTP_PORT}"})

        assert settings.host == ""
        assert settings.port == 587
        assert settings.is_configured is False

    def test_security_and_auth_are_independent(self):
        """The whole point of replacing use_tls: implicit TLS with no authentication."""
        settings = SmtpSettings.from_dict({"host": "h", "security": "smtps", "auth": "none"})

        assert settings.security == "smtps"
        assert settings.auth == "none"

    def test_plaintext_is_rejected_and_upgraded_to_starttls(self):
        """TLS is mandatory; a config asking for plaintext is corrected, loudly."""
        settings = SmtpSettings.from_dict({"host": "h", "security": "none"})

        assert settings.security == "starttls"
        assert any("no longer supported" in p for p in settings.validate())

    def test_legacy_basic_auth_is_accepted_as_login(self):
        """'basic' and 'login' were synonyms; existing configs keep working silently."""
        settings = SmtpSettings.from_dict({"host": "h", "auth": "basic"})

        assert settings.auth == "login"
        # Accepted silently: no "is not one of" fallback warning. A separate complaint about
        # the missing username is expected and correct.
        assert not any("is not one of" in p for p in settings.validate())

    def test_smtps_defaults_to_port_465(self):
        assert SmtpSettings.from_dict({"host": "h", "security": "smtps"}).port == 465

    def test_invalid_security_falls_back_and_warns(self):
        settings = SmtpSettings.from_dict({"host": "h", "security": "ssl"})

        assert settings.security == "starttls"
        assert any("smtp.security" in problem for problem in settings.validate())

    def test_invalid_auth_falls_back_and_warns(self):
        settings = SmtpSettings.from_dict({"host": "h", "auth": "kerberos"})

        assert settings.auth == "none"
        assert any("smtp.auth" in problem for problem in settings.validate())

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("true", True), ("false", False), ("yes", True), ("no", False), (True, True)],
    )
    def test_boolean_coercion(self, raw, expected):
        assert SmtpSettings.from_dict({"verify_cert": raw}).verify_cert is expected

    def test_port_given_as_string_is_coerced(self):
        assert SmtpSettings.from_dict({"port": "2525"}).port == 2525

    def test_garbage_port_falls_back_to_default(self):
        assert SmtpSettings.from_dict({"port": "not-a-port"}).port == 587


@pytest.mark.unit
class TestIsConfigured:
    def test_requires_both_host_and_from_address(self):
        assert SmtpSettings(host="h").is_configured is False
        assert SmtpSettings(from_address="a@b.com").is_configured is False
        assert SmtpSettings(host="h", from_address="a@b.com").is_configured is True


@pytest.mark.unit
class TestValidate:
    def test_clean_settings_report_no_problems(self):
        settings = SmtpSettings(host="mail.corp", from_address="pbx@corp.com")

        assert settings.validate() == []

    def test_missing_host_reported(self):
        assert any("smtp.host" in p for p in SmtpSettings(from_address="a@b.com").validate())

    def test_invalid_from_address_reported(self):
        settings = SmtpSettings(host="h", from_address="not-an-address")

        assert any("from_address" in p for p in settings.validate())

    def test_auth_without_credentials_reported(self):
        settings = SmtpSettings(host="h", from_address="a@b.com", auth="login")
        problems = settings.validate()

        assert any("username" in p for p in problems)
        assert any(PASSWORD_ENV_VAR in p for p in problems)

    def test_disabled_certificate_verification_is_flagged(self):
        settings = SmtpSettings(host="h", from_address="a@b.com", verify_cert=False)

        assert any("verify_cert" in p for p in settings.validate())

    def test_missing_ca_file_reported(self):
        settings = SmtpSettings(host="h", from_address="a@b.com", ca_file="/nope/missing.pem")

        assert any("ca_file" in p for p in settings.validate())

    def test_out_of_range_port_reported(self):
        settings = SmtpSettings(host="h", from_address="a@b.com", port=70000)

        assert any("smtp.port" in p for p in settings.validate())


@pytest.mark.unit
class TestRedacted:
    def test_password_is_masked_and_never_present(self):
        settings = SmtpSettings(host="h", from_address="a@b.com", password="hunter2")
        redacted = settings.redacted()

        assert redacted["password"] == "***"
        assert "hunter2" not in str(redacted)

    def test_empty_password_stays_empty(self):
        assert SmtpSettings(host="h").redacted()["password"] == ""

    def test_repr_does_not_leak_the_password(self):
        assert "hunter2" not in repr(SmtpSettings(host="h", password="hunter2"))
