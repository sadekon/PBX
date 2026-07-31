"""
Registrations must survive an extension reload, and the registering port must survive a
round-trip through the database.

Both defects had the same signature in the field: a phone that stopped ringing after an
unrelated admin edit and stayed silent until the PBX was restarted.

`reload()` runs after every admin add/update/delete. It rebuilt every Extension object, and
registration state lives only on those objects -- so any edit silently de-registered every
phone. Phones are never told this happened (there is no SIP message for it), so a phone with
a long registration interval stays unreachable until its own expiry.

The database fallback that should have rescued it stored only the IP and assumed port 5060,
so any phone registering from another port was sent INVITEs at the wrong port.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from pbx.features.extensions import Extension, ExtensionRegistry

ADDRESS = ("192.168.4.55", 5062)  # deliberately not 5060


def _db_row(number: str, name: str = "Test") -> dict:
    return {
        "number": number,
        "name": name,
        "email": f"{number}@corp.local",
        "sip_password": "secret",
        "password_hash": "hash",
        "allow_external": True,
        "voicemail_pin_hash": "",
        "ad_synced": False,
        "is_admin": False,
        "did_number": None,
    }


@pytest.fixture
def registry():
    """A registry backed by a stub database returning two extensions."""
    database = MagicMock()
    database.enabled = True
    reg = ExtensionRegistry(config=MagicMock(), database=database)
    reg.extensions = {
        "1517": ExtensionRegistry.create_extension_from_db(_db_row("1517")),
        "1518": ExtensionRegistry.create_extension_from_db(_db_row("1518")),
    }

    # _load_extensions() is stubbed to rebuild from the same rows, as a real reload would.
    def fake_load() -> None:
        for number in ("1517", "1518"):
            reg.extensions[number] = ExtensionRegistry.create_extension_from_db(_db_row(number))

    reg._load_extensions = fake_load  # type: ignore[method-assign]
    return reg


@pytest.mark.unit
class TestRegistrationSurvivesReload:
    def test_registration_is_preserved(self, registry):
        registry.get("1517").register(ADDRESS, expires=3600)

        registry.reload()

        ext = registry.get("1517")
        assert ext.registered is True
        assert ext.address == ADDRESS

    def test_expiry_is_preserved_so_the_sweep_does_not_drop_it(self, registry):
        registry.get("1517").register(ADDRESS, expires=3600)
        before = registry.get("1517").expires_at

        registry.reload()

        assert registry.get("1517").expires_at == before
        assert registry.get("1517").is_expired() is False

    def test_registration_time_is_preserved(self, registry):
        registry.get("1517").register(ADDRESS, expires=3600)
        before = registry.get("1517").registration_time

        registry.reload()

        assert registry.get("1517").registration_time == before

    def test_editing_one_extension_does_not_deregister_another(self, registry):
        """The reported symptom: any edit dropped every phone, not just the edited one."""
        registry.get("1517").register(ADDRESS, expires=3600)
        registry.get("1518").register(("192.168.4.56", 5060), expires=3600)

        registry.reload()

        assert registry.get("1517").registered is True
        assert registry.get("1518").registered is True

    def test_unregistered_extension_stays_unregistered(self, registry):
        registry.reload()

        assert registry.get("1517").registered is False
        assert registry.get("1517").address is None

    def test_config_is_still_refreshed(self, registry):
        """Preserving registration must not stop the reload picking up edits."""
        registry.get("1517").register(ADDRESS, expires=3600)
        registry.get("1517").config["email"] = "stale@corp.local"

        registry.reload()

        assert registry.get("1517").config["email"] == "1517@corp.local"
        assert registry.get("1517").registered is True

    def test_expired_registration_is_carried_over_as_expired(self, registry):
        ext = registry.get("1517")
        ext.register(ADDRESS, expires=3600)
        ext.expires_at = datetime.now(UTC) - timedelta(seconds=1)

        registry.reload()

        assert registry.get("1517").is_expired() is True


@pytest.mark.unit
class TestAdoptRegistration:
    def test_copies_all_four_fields(self):
        source = Extension("1517", "Test", {})
        source.register(ADDRESS, expires=120)
        target = Extension("1517", "Test", {})

        target.adopt_registration(source)

        assert target.registered == source.registered
        assert target.address == source.address
        assert target.registration_time == source.registration_time
        assert target.expires_at == source.expires_at

    def test_adopting_an_unregistered_extension_is_a_no_op(self):
        target = Extension("1517", "Test", {})

        target.adopt_registration(Extension("1517", "Test", {}))

        assert target.registered is False
        assert target.address is None
