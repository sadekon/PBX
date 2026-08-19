#!/usr/bin/env python3
"""
Tests for how a phone's reachable address is decided and kept.

Three separate things kept resetting an ATA's address to 127.0.0.1, each on its own timer,
so overriding it by hand never held:

  * registration trusted the phone's Contact header over the packet's source
  * the provisioning route wrote the HTTP client's address as if it were a registration,
    which behind a reverse proxy is the proxy's address for every phone
  * startup recovery read the bad value back, so a restart reinstated it

The consequence was not subtle: a paging destination resolved to the PBX itself, and the
guard against dialling ourselves refused the page outright.
"""

from unittest.mock import MagicMock

import pytest

from pbx.core.registration_handler import RegistrationHandler


def _handler():
    pbx = MagicMock()
    return RegistrationHandler(pbx), pbx


@pytest.mark.unit
class TestContactVersusSource:
    """The packet cannot lie about where it came from; the Contact header can."""

    def test_a_matching_contact_supplies_its_port(self):
        """
        The one thing Contact is good for: a phone often registers from an ephemeral port
        while listening on 5060, and only the Contact says so.
        """
        handler, _ = _handler()
        result = handler._extract_contact_address(
            "<sip:1501@192.168.10.120:5060>", ("192.168.10.120", 51234)
        )
        assert result == ("192.168.10.120", 5060)

    def test_a_loopback_contact_is_ignored(self):
        """This one made the PBX INVITE itself."""
        handler, _ = _handler()
        result = handler._extract_contact_address(
            "<sip:1501@127.0.0.1:5060>", ("192.168.10.120", 5060)
        )
        assert result == ("192.168.10.120", 5060)

    def test_a_contact_on_another_subnet_is_ignored(self):
        """The ATA claimed 10.0.0.27 while sitting on 192.168.10.120."""
        handler, _ = _handler()
        result = handler._extract_contact_address(
            "<sip:1501@10.0.0.27:5060>", ("192.168.10.120", 5060)
        )
        assert result == ("192.168.10.120", 5060)

    def test_a_disagreeing_contact_is_reported(self):
        """Silently correcting it would hide a misconfigured device."""
        handler, pbx = _handler()
        handler._extract_contact_address("<sip:1501@10.0.0.27>", ("192.168.10.120", 5060))
        pbx.logger.warning.assert_called_once()
        assert "10.0.0.27" in pbx.logger.warning.call_args[0][0]

    def test_no_contact_falls_back_to_the_source(self):
        handler, _ = _handler()
        assert handler._extract_contact_address(None, ("192.168.10.120", 5060)) == (
            "192.168.10.120",
            5060,
        )

    def test_an_unparseable_contact_falls_back_to_the_source(self):
        handler, _ = _handler()
        assert handler._extract_contact_address("garbage", ("192.168.10.120", 5060)) == (
            "192.168.10.120",
            5060,
        )

    def test_a_contact_without_a_port_defaults_to_5060(self):
        handler, _ = _handler()
        assert handler._extract_contact_address(
            "<sip:1501@192.168.10.120>", ("192.168.10.120", 5060)
        ) == ("192.168.10.120", 5060)


@pytest.mark.unit
class TestOnlyRegistrationOwnsTheAddress:
    """
    A config fetch says where a device asked for a file. A REGISTER says where it can be
    called. Only the second belongs in ip_address.
    """

    def _db(self, existing=None):
        from pbx.utils.database import RegisteredPhonesDB

        backend = MagicMock()
        db = RegisteredPhonesDB(backend)
        db.get_by_mac = MagicMock(return_value=existing)
        db.get_by_ip = MagicMock(return_value=None)
        db.get_device = MagicMock(return_value=None)
        return db, backend

    def test_a_registration_updates_the_address(self):
        db, backend = self._db(
            existing={
                "id": 1,
                "mac_address": "aa",
                "ip_address": "10.0.0.5",
                "extension_number": "1501",
            }
        )
        db.register_phone("1501", "192.168.10.120", mac_address="aa")

        params = backend.execute.call_args[0][1]
        assert "192.168.10.120" in params

    def test_a_provisioning_fetch_leaves_the_address_alone(self):
        db, backend = self._db(
            existing={
                "id": 1,
                "mac_address": "aa",
                "ip_address": "192.168.10.120",
                "extension_number": "1501",
            }
        )
        db.register_phone("1501", "127.0.0.1", mac_address="aa", address_is_authoritative=False)

        params = backend.execute.call_args[0][1]
        assert "192.168.10.120" in params
        assert "127.0.0.1" not in params

    def test_a_provisioning_fetch_can_still_create_a_first_row(self):
        """Better a row with the proxy's address than no record of the device at all."""
        db, backend = self._db(existing=None)
        db.register_phone("1501", "127.0.0.1", mac_address="aa", address_is_authoritative=False)
        assert backend.execute.called

    def test_a_non_authoritative_write_does_not_steal_another_extension(self):
        """
        Behind a proxy every phone reports the same address, so this would read each config
        fetch as the device having moved and delete the previous phone's registration --
        phones deleting each other in turn.
        """
        db, backend = self._db(existing=None)
        db.get_by_ip = MagicMock(
            return_value={"id": 9, "extension_number": "1502", "ip_address": "127.0.0.1"}
        )
        db.register_phone("1501", "127.0.0.1", mac_address="aa", address_is_authoritative=False)

        deletes = [call for call in backend.execute.call_args_list if "DELETE" in str(call[0][0])]
        assert not deletes, "a config fetch must not unregister another phone"
