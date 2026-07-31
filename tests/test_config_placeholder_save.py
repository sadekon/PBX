"""
Config.save() must never write a resolved secret back to config.yml.

Config.load() resolves ``${VAR}`` placeholders in memory, and save() used to yaml.dump that
resolved dict straight back to the git-tracked config file -- materialising every credential
at once. These tests pin the re-substitution that prevents it.
"""

import pytest

from pbx.utils.config import Config

CONFIG_TEMPLATE = """\
database:
  host: ${DB_HOST}
  password: ${DB_PASSWORD}
smtp:
  host: ${SMTP_HOST}
  port: ${SMTP_PORT}
  from_address: pbx@corp.local
integrations:
  matrix:
    bot_password: ${MATRIX_BOT_PASSWORD}
sip_trunks:
  - id: main
    password: ${TRUNK_PASSWORD}
"""

SECRETS = {
    "DB_HOST": "db.corp.local",
    "DB_PASSWORD": "db-s3cret",
    "SMTP_HOST": "mail.corp.local",
    "SMTP_PORT": "2525",
    "MATRIX_BOT_PASSWORD": "matrix-s3cret",
    "TRUNK_PASSWORD": "trunk-s3cret",
}


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    path = tmp_path / "config.yml"
    path.write_text(CONFIG_TEMPLATE)
    return path


@pytest.mark.unit
class TestPlaceholderRoundTrip:
    def test_values_are_resolved_in_memory(self, config_file):
        config = Config(str(config_file))

        assert config.get("database.password") == "db-s3cret"
        assert config.get("smtp.host") == "mail.corp.local"
        assert config.get("smtp.port") == 2525

    def test_save_writes_placeholders_not_secrets(self, config_file):
        config = Config(str(config_file))

        assert config.save() is True

        text = config_file.read_text()
        for placeholder in (
            "${DB_HOST}",
            "${DB_PASSWORD}",
            "${SMTP_HOST}",
            "${SMTP_PORT}",
            "${MATRIX_BOT_PASSWORD}",
            "${TRUNK_PASSWORD}",
        ):
            assert placeholder in text, f"lost placeholder {placeholder}"

        for secret in SECRETS.values():
            assert secret not in text, f"materialised secret {secret}"

    def test_placeholders_inside_lists_survive(self, config_file):
        """List elements are addressed by index, so a trunk password is tracked too."""
        Config(str(config_file)).save()

        assert "${TRUNK_PASSWORD}" in config_file.read_text()

    def test_repeated_saves_stay_stable(self, config_file):
        for _ in range(3):
            config = Config(str(config_file))
            assert config.save() is True

        text = config_file.read_text()
        assert "${DB_PASSWORD}" in text
        assert "db-s3cret" not in text


@pytest.mark.unit
class TestDeliberateEdits:
    def test_a_changed_value_persists_as_a_literal(self, config_file):
        config = Config(str(config_file))
        config.config["smtp"]["host"] = "exchange.corp.local"

        assert config.save() is True

        text = config_file.read_text()
        assert "exchange.corp.local" in text
        assert "${SMTP_HOST}" not in text

    def test_changing_one_value_does_not_disturb_the_others(self, config_file):
        config = Config(str(config_file))
        config.config["smtp"]["host"] = "exchange.corp.local"
        config.save()

        text = config_file.read_text()
        assert "${DB_PASSWORD}" in text
        assert "${MATRIX_BOT_PASSWORD}" in text
        assert "db-s3cret" not in text

    def test_a_newly_added_key_is_written_verbatim(self, config_file):
        config = Config(str(config_file))
        config.config["smtp"]["helo_hostname"] = "pbx.corp.local"
        config.save()

        assert "pbx.corp.local" in config_file.read_text()


@pytest.mark.unit
class TestUpdateEmailConfig:
    def test_a_submitted_password_is_not_written_to_the_config(self, config_file):
        """SMTP credentials come from the environment; the API must not persist one."""
        config = Config(str(config_file))

        config.update_email_config({"smtp": {"host": "relay.corp.local", "password": "leaked"}})

        text = config_file.read_text()
        assert "relay.corp.local" in text
        assert "leaked" not in text
