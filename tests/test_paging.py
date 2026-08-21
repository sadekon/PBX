"""
Tests for the zone/destination paging system.

Replaces test_paging_integration.py and test_paging_handler_coverage.py, which exercised the
config-backed `add_zone`/`configure_dac_device`/`initiate_page` API and the `_paging_session`
thread. All of that was removed: zones live in Postgres, an ATA is an ordinary extension, and
the media path fans out rather than monitoring a single device.
"""

import socket
import threading
import time
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from pbx.features.paging import (
    AUTO_ANSWER_HEADERS,
    ActivePage,
    PagingDestination,
    PagingSystem,
)
from pbx.rtp.paging_media import PagingMediaSession


def _config(enabled=True, max_duration=120):
    """A config double answering the dotted lookups PagingSystem makes."""
    values = {
        "features.paging.enabled": enabled,
        "features.paging.max_duration": max_duration,
        "features.paging.default_auto_answer": None,
    }
    config = MagicMock()
    config.get.side_effect = lambda key, default=None: values.get(key, default)
    return config


def _system(zones=None, destinations=None, enabled=True, max_duration=120):
    """A PagingSystem with its two DB objects mocked out."""
    system = PagingSystem(_config(enabled=enabled, max_duration=max_duration), database=None)
    system.zones_db = MagicMock()
    system.destinations_db = MagicMock()
    system.zones_db.list_enabled_extensions.return_value = [z["extension"] for z in (zones or [])]
    system.zones_db.list_all.return_value = zones or []
    system.destinations_db.list_for_zone.return_value = destinations or []
    system.destinations_db.list_resolved_for_zone.return_value = destinations or []
    system.refresh_zone_cache()
    return system


def _destination(destination_id=1, extension="7801", vendor="cisco", **kwargs):
    """A resolved sip_endpoint destination."""
    return PagingDestination(
        destination_id=destination_id,
        kind="sip_endpoint",
        endpoint_extension=extension,
        vendor=vendor,
        **kwargs,
    )


# --------------------------------------------------------------------------- auto-answer


@pytest.mark.unit
class TestAutoAnswerHeader:
    """
    Nothing is sent unless a destination was explicitly given something to send.

    This inverted after the hardware disagreed with the design. An amplifier is not a SIP
    device: it is wired to an ATA's FXS port and seizes the line when it detects ring
    voltage, which the ATA raises only while it is actually ringing. Deriving
    `Call-Info: answer-after=0` from the ATA's vendor therefore defeated the mechanism it was
    meant to serve -- the ATA answered on the SIP side without ringing the port, and the
    amplifier, listening for a ring that never came, never seized.

    The map is kept for destinations that really do have to be told: a desk phone paged over
    SIP has no ring voltage to offer and would otherwise ring at somebody until answered.
    """

    def test_nothing_is_sent_by_default(self):
        """The amplifier case, and the overwhelmingly common one."""
        assert _destination(vendor="cisco").auto_answer_header("10.0.0.1") is None

    def test_a_vendor_alone_no_longer_implies_a_header(self):
        """
        The regression this class exists to prevent. Every one of these is a vendor whose
        endpoints do honour an auto-answer header -- and none of them should be told to use
        it merely for being that vendor.
        """
        for vendor in ("cisco", "polycom", "grandstream", "yealink", "zultys"):
            destination = _destination(vendor=vendor)
            assert destination.auto_answer_header("10.0.0.1") is None, (
                f"{vendor} was told to auto-answer without anyone asking for it"
            )

    def test_an_unprovisioned_endpoint_sends_nothing_either(self):
        assert _destination(vendor=None).auto_answer_header("10.0.0.1") is None

    def test_an_explicit_override_is_honoured(self):
        """How a desk phone gets paged once desk-phone zones exist."""
        destination = _destination(vendor=None, auto_answer_override="polycom")
        assert destination.auto_answer_header("10.0.0.1") == ("Alert-Info", "Ring Answer")

    def test_an_override_beats_the_devices_own_vendor(self):
        destination = _destination(vendor="cisco", auto_answer_override="grandstream")
        assert destination.auto_answer_header("10.0.0.1")[0] == "Alert-Info"

    def test_cisco_still_formats_call_info_when_asked_for(self):
        destination = _destination(vendor=None, auto_answer_override="cisco")
        assert destination.auto_answer_header("10.0.0.1") == (
            "Call-Info",
            "<sip:10.0.0.1>;answer-after=0",
        )

    def test_override_case_is_ignored(self):
        destination = _destination(vendor=None, auto_answer_override="CISCO")
        assert destination.auto_answer_header("10.0.0.1")[0] == "Call-Info"

    def test_override_none_sends_no_header(self):
        """Stored by older rows, and means the same as leaving it unset."""
        destination = _destination(vendor="cisco", auto_answer_override="none")
        assert destination.auto_answer_header("10.0.0.1") is None

    def test_every_known_vendor_formats_without_error(self):
        for vendor in AUTO_ANSWER_HEADERS:
            _destination(vendor=None, auto_answer_override=vendor).auto_answer_header("10.0.0.1")


# --------------------------------------------------------------------------- routing


@pytest.mark.unit
class TestPagingExtensionMatching:
    """Exact match against configured zones -- never a prefix."""

    def test_configured_zone_matches(self):
        system = _system(zones=[{"id": 1, "extension": "701", "name": "Warehouse"}])
        assert system.is_paging_extension("701")

    def test_unconfigured_number_does_not_match(self):
        system = _system(zones=[{"id": 1, "extension": "701", "name": "Warehouse"}])
        assert not system.is_paging_extension("702")

    def test_does_not_swallow_other_7xx_numbers(self):
        """
        The old startswith("7") claimed every extension beginning with a 7 -- a user at 7123,
        and call parking, which owns 7x via dialplan.parking_pattern.
        """
        system = _system(zones=[{"id": 1, "extension": "700", "name": "All Call"}])
        for number in ("7123", "750", "71", "7", "7001"):
            assert not system.is_paging_extension(number), f"{number} must not route to paging"

    def test_nothing_matches_when_disabled(self):
        system = _system(zones=[{"id": 1, "extension": "701", "name": "Warehouse"}], enabled=False)
        assert not system.is_paging_extension("701")


# --------------------------------------------------------------------------- zone CRUD


@pytest.mark.unit
class TestZoneCreation:
    """A zone number is drawn from the same dial plan as everything else."""

    def test_creates_and_refreshes_cache(self):
        system = _system()
        system.zones_db.extension_conflict.return_value = None
        system.zones_db.create.return_value = 7
        system.zones_db.list_enabled_extensions.return_value = ["701"]

        zone_id, error = system.create_zone("701", "Warehouse")

        assert (zone_id, error) == (7, None)
        assert system.is_paging_extension("701")

    def test_refuses_a_number_another_zone_holds(self):
        system = _system()
        system.zones_db.extension_conflict.return_value = "zone"
        zone_id, error = system.create_zone("701", "Warehouse")
        assert zone_id is None
        assert "already" in error

    def test_refuses_a_number_a_user_holds(self):
        """A zone at 701 would make the user at 701 unreachable."""
        system = _system()
        system.zones_db.extension_conflict.return_value = "extension"
        zone_id, error = system.create_zone("701", "Warehouse")
        assert zone_id is None
        assert "already in use" in error
        system.zones_db.create.assert_not_called()

    def test_requires_number_and_name(self):
        system = _system()
        system.zones_db.extension_conflict.return_value = None
        assert system.create_zone("", "Warehouse")[0] is None
        assert system.create_zone("701", "  ")[0] is None

    def test_uses_configured_default_duration(self):
        system = _system(max_duration=45)
        system.zones_db.extension_conflict.return_value = None
        system.zones_db.create.return_value = 1
        system.create_zone("701", "Warehouse")
        assert system.zones_db.create.call_args.kwargs["max_duration_seconds"] == 45


@pytest.mark.unit
class TestZoneDeletion:
    def test_refuses_while_the_zone_is_paging(self):
        system = _system()
        system.active_pages["p1"] = ActivePage(
            page_id="p1",
            from_extension="1001",
            zone_id=3,
            zone_extension="701",
            zone_name="Warehouse",
            destinations=[],
            started_at=datetime.now(UTC),
        )
        ok, error = system.delete_zone(3)
        assert not ok
        assert "paging right now" in error
        system.zones_db.delete.assert_not_called()

    def test_deletes_an_idle_zone(self):
        system = _system()
        system.zones_db.delete.return_value = True
        assert system.delete_zone(3) == (True, None)


# --------------------------------------------------------------------------- destinations


@pytest.mark.unit
class TestDestinations:
    def test_rejects_unknown_auto_answer_mode(self):
        system = _system()
        system.zones_db.get.return_value = {"id": 1}
        _, error = system.add_sip_destination(1, "7801", auto_answer_override="carrier-pigeon")
        assert "Unknown auto-answer mode" in error

    def test_rejects_a_missing_zone(self):
        system = _system()
        system.zones_db.get.return_value = None
        _, error = system.add_sip_destination(99, "7801")
        assert "no longer exists" in error

    def test_requires_an_extension(self):
        system = _system()
        system.zones_db.get.return_value = {"id": 1}
        _, error = system.add_sip_destination(1, "")
        assert "needs an extension" in error

    def test_adds_a_valid_endpoint(self):
        system = _system()
        system.zones_db.get.return_value = {"id": 1}
        system.destinations_db.add_sip_endpoint.return_value = 5
        destination_id, error = system.add_sip_destination(1, "7801", label="Ceiling horns")
        assert (destination_id, error) == (5, None)

    def test_refuses_to_remove_a_destination_mid_page(self):
        system = _system()
        system.destinations_db.get.return_value = {"id": 5, "endpoint_extension": "7801"}
        system._busy_endpoints["7801"] = "p1"
        ok, error = system.remove_destination(5)
        assert not ok
        assert "paging right now" in error


# --------------------------------------------------------------------------- pages


@pytest.mark.unit
class TestPageLifecycle:
    def _ready(self, destinations):
        system = _system(zones=[{"id": 1, "extension": "701", "name": "Warehouse"}])
        system.zones_db.get_by_extension.return_value = {
            "id": 1,
            "extension": "701",
            "name": "Warehouse",
            "enabled": True,
            "max_duration_seconds": 0,
        }
        system.destinations_db.list_resolved_for_zone.return_value = destinations
        return system

    def _row(self, destination_id, extension):
        return {
            "id": destination_id,
            "kind": "sip_endpoint",
            "endpoint_extension": extension,
            "vendor": "cisco",
        }

    def test_starts_and_reserves_its_endpoints(self):
        system = self._ready([self._row(1, "7801")])
        page, error = system.begin_page("1001", "701", call_id="c1")
        assert error is None
        assert page.zone_name == "Warehouse"
        assert system._busy_endpoints["7801"] == page.page_id

    def test_refuses_a_zone_with_no_destinations(self):
        system = self._ready([])
        page, error = system.begin_page("1001", "701")
        assert page is None
        assert "no destinations" in error

    def test_second_page_to_the_same_zone_is_busy(self):
        system = self._ready([self._row(1, "7801")])
        system.begin_page("1001", "701", call_id="c1")
        page, error = system.begin_page("1002", "701", call_id="c2")
        assert page is None
        assert error == "busy"

    def test_overlapping_zones_conflict_through_the_shared_endpoint(self):
        """
        All-call and a specific zone share an amplifier by design, so paging one while the
        other is live must be refused even though the second zone is itself idle.
        """
        system = self._ready([self._row(1, "7801")])
        system.begin_page("1001", "700", call_id="c1")

        # A different zone, different destination row, same physical circuit.
        system.zones_db.get_by_extension.return_value = {
            "id": 2,
            "extension": "701",
            "name": "Warehouse",
            "enabled": True,
            "max_duration_seconds": 0,
        }
        system.destinations_db.list_resolved_for_zone.return_value = [self._row(9, "7801")]

        page, error = system.begin_page("1002", "701", call_id="c2")
        assert page is None
        assert error == "busy"

    def test_disjoint_zones_page_concurrently(self):
        system = self._ready([self._row(1, "7801")])
        first, _ = system.begin_page("1001", "701", call_id="c1")

        system.zones_db.get_by_extension.return_value = {
            "id": 2,
            "extension": "702",
            "name": "Dock",
            "enabled": True,
            "max_duration_seconds": 0,
        }
        system.destinations_db.list_resolved_for_zone.return_value = [self._row(2, "7802")]

        second, error = system.begin_page("1002", "702", call_id="c2")
        assert error is None
        assert second.page_id != first.page_id

    def test_refuses_a_disabled_zone(self):
        system = self._ready([self._row(1, "7801")])
        system.zones_db.get_by_extension.return_value["enabled"] = False
        page, error = system.begin_page("1001", "701")
        assert page is None
        assert "switched off" in error

    def test_ending_releases_the_endpoints(self):
        system = self._ready([self._row(1, "7801")])
        page, _ = system.begin_page("1001", "701", call_id="c1")
        assert system.end_page(page.page_id)
        assert "7801" not in system._busy_endpoints
        assert system.get_active_pages() == []

    def test_ending_twice_is_harmless(self):
        """Teardown is reachable from hangup, the duration timer and the admin kill."""
        system = self._ready([self._row(1, "7801")])
        page, _ = system.begin_page("1001", "701", call_id="c1")
        assert system.end_page(page.page_id)
        assert not system.end_page(page.page_id)

    def test_ending_frees_the_zone_for_the_next_page(self):
        system = self._ready([self._row(1, "7801")])
        first, _ = system.begin_page("1001", "701", call_id="c1")
        system.end_page(first.page_id)
        second, error = system.begin_page("1002", "701", call_id="c2")
        assert error is None
        assert second is not None

    def test_page_is_findable_by_call_id(self):
        system = self._ready([self._row(1, "7801")])
        page, _ = system.begin_page("1001", "701", call_id="c1")
        assert system.get_page_by_call("c1").page_id == page.page_id
        assert system.get_page_by_call("nope") is None

    def test_destination_state_is_tracked(self):
        system = self._ready([self._row(1, "7801")])
        page, _ = system.begin_page("1001", "701", call_id="c1")
        system.set_destination_state(page.page_id, 1, "answered")
        assert page.to_dict()["destinations"][0]["state"] == "answered"


# --------------------------------------------------------------------------- media fan-out


@pytest.mark.unit
class TestPagingMediaSession:
    """
    Real sockets on loopback. This is the claim the whole rebuild rests on -- one inbound
    stream reaching several endpoints -- so it is tested against the network stack rather
    than a mock.
    """

    @staticmethod
    def _listener():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(2.0)
        return sock, sock.getsockname()

    @staticmethod
    def _free_port():
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        return port

    def test_one_packet_reaches_every_destination(self):
        listener_a, addr_a = self._listener()
        listener_b, addr_b = self._listener()
        listener_c, addr_c = self._listener()
        session = PagingMediaSession(self._free_port(), "page-test")
        assert session.start()

        try:
            session.add_target(1, addr_a)
            session.add_target(2, addr_b)
            session.add_target(3, addr_c)

            pager = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            pager.sendto(b"\x80\x00\x00\x01rtp-payload", ("127.0.0.1", session.local_port))

            for listener in (listener_a, listener_b, listener_c):
                assert listener.recv(2048) == b"\x80\x00\x00\x01rtp-payload"

            pager.close()
        finally:
            session.stop()
            for listener in (listener_a, listener_b, listener_c):
                listener.close()

    def test_a_removed_destination_stops_receiving(self):
        listener_a, addr_a = self._listener()
        listener_b, addr_b = self._listener()
        session = PagingMediaSession(self._free_port(), "page-test")
        assert session.start()

        try:
            session.add_target(1, addr_a)
            session.add_target(2, addr_b)
            pager = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

            pager.sendto(b"first", ("127.0.0.1", session.local_port))
            assert listener_a.recv(2048) == b"first"
            assert listener_b.recv(2048) == b"first"

            session.remove_target(2)
            assert session.target_count() == 1

            pager.sendto(b"second", ("127.0.0.1", session.local_port))
            assert listener_a.recv(2048) == b"second"
            with pytest.raises(TimeoutError):
                listener_b.settimeout(0.4)
                listener_b.recv(2048)

            pager.close()
        finally:
            session.stop()
            listener_a.close()
            listener_b.close()

    def test_a_destination_reaches_the_pager_but_not_other_destinations(self):
        """
        The asymmetry the whole design turns on.

        An amplifier answers a zone keypress with its own prompt or confirmation tone, and
        everything past the FXS port is analog, so that tone is the only evidence the right
        zone opened. It must reach the pager. It must NOT reach the other amplifiers, which
        would put one circuit's noise onto every other.
        """
        listener_a, addr_a = self._listener()
        listener_b, addr_b = self._listener()
        pager = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        pager.bind(("127.0.0.1", 0))
        pager.settimeout(2.0)

        session = PagingMediaSession(self._free_port(), "page-test")
        assert session.start()

        try:
            session.add_target(1, addr_a)
            session.add_target(2, addr_b)

            # The pager speaks first, so the session learns which address it is.
            pager.sendto(b"from-pager", ("127.0.0.1", session.local_port))
            assert listener_a.recv(2048) == b"from-pager"
            assert listener_b.recv(2048) == b"from-pager"

            # Amplifier A answers with its confirmation tone.
            listener_a.sendto(b"confirm-tone", ("127.0.0.1", session.local_port))

            # The pager hears it...
            assert pager.recv(2048) == b"confirm-tone"

            # ...and amplifier B does not.
            listener_b.settimeout(0.4)
            with pytest.raises(TimeoutError):
                listener_b.recv(2048)

            pager.close()
        finally:
            session.stop()
            listener_a.close()
            listener_b.close()

    def test_a_stray_sender_is_still_ignored(self):
        """Only the pager and the destinations we are streaming to are relayed."""
        listener_a, addr_a = self._listener()
        pager = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        pager.bind(("127.0.0.1", 0))
        pager.settimeout(2.0)

        session = PagingMediaSession(self._free_port(), "page-test")
        assert session.start()

        try:
            session.add_target(1, addr_a)
            pager.sendto(b"from-pager", ("127.0.0.1", session.local_port))
            assert listener_a.recv(2048) == b"from-pager"

            stray = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            stray.sendto(b"who-is-this", ("127.0.0.1", session.local_port))

            listener_a.settimeout(0.4)
            with pytest.raises(TimeoutError):
                listener_a.recv(2048)
            pager.settimeout(0.4)
            with pytest.raises(TimeoutError):
                pager.recv(2048)

            pager.close()
            stray.close()
        finally:
            session.stop()
            listener_a.close()

    def test_an_amplifier_answering_first_is_not_mistaken_for_the_pager(self):
        """
        Ordering makes this unlikely -- the pager sends as soon as it gets our 200 OK -- but
        an amplifier taken for the pager would have its audio fanned out to every other
        amplifier, which is the one thing this must never do.
        """
        listener_a, addr_a = self._listener()
        listener_b, addr_b = self._listener()

        session = PagingMediaSession(self._free_port(), "page-test")
        assert session.start()

        try:
            session.add_target(1, addr_a)
            session.add_target(2, addr_b)

            # Amplifier A speaks before the pager ever does.
            listener_a.sendto(b"early-tone", ("127.0.0.1", session.local_port))

            listener_b.settimeout(0.4)
            with pytest.raises(TimeoutError):
                listener_b.recv(2048)
            assert session._source is None
        finally:
            session.stop()
            listener_a.close()
            listener_b.close()

    def test_packets_before_any_destination_answers_are_counted_not_crashed(self):
        session = PagingMediaSession(self._free_port(), "page-test")
        assert session.start()
        try:
            pager = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            pager.sendto(b"too-early", ("127.0.0.1", session.local_port))
            deadline = time.time() + 2.0
            while session.packets_dropped_no_targets == 0 and time.time() < deadline:
                time.sleep(0.02)
            assert session.packets_dropped_no_targets >= 1
            pager.close()
        finally:
            session.stop()

    def test_stop_is_idempotent(self):
        session = PagingMediaSession(self._free_port(), "page-test")
        assert session.start()
        session.stop()
        session.stop()
        assert not session.running

    def test_stats_report_per_destination_counts(self):
        listener_a, addr_a = self._listener()
        session = PagingMediaSession(self._free_port(), "page-test")
        assert session.start()
        try:
            session.add_target(1, addr_a)
            pager = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            pager.sendto(b"payload", ("127.0.0.1", session.local_port))
            listener_a.recv(2048)

            stats = session.get_stats()
            assert stats["packets_received"] >= 1
            assert stats["packets_sent"][1] >= 1
            pager.close()
        finally:
            session.stop()
            listener_a.close()


# --------------------------------------------------------------------------- concurrency


@pytest.mark.unit
def test_concurrent_pages_to_one_zone_yield_exactly_one_winner():
    """
    Reservation and registration happen under one lock, so two phones dialling the same zone
    at the same instant cannot both be told the amplifier was free.
    """
    system = _system(zones=[{"id": 1, "extension": "701", "name": "Warehouse"}])
    system.zones_db.get_by_extension.return_value = {
        "id": 1,
        "extension": "701",
        "name": "Warehouse",
        "enabled": True,
        "max_duration_seconds": 0,
    }
    system.destinations_db.list_resolved_for_zone.return_value = [
        {"id": 1, "kind": "sip_endpoint", "endpoint_extension": "7801", "vendor": "cisco"}
    ]

    results = []
    barrier = threading.Barrier(8)

    def _page(index):
        barrier.wait()
        page, error = system.begin_page(f"100{index}", "701", call_id=f"c{index}")
        results.append((page, error))

    threads = [threading.Thread(target=_page, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    winners = [page for page, _ in results if page is not None]
    assert len(winners) == 1
    assert all(error == "busy" for page, error in results if page is None)


# --------------------------------------------------------------------------- reachability


@pytest.mark.unit
class TestDestinationReachability:
    """
    A destination registered at this PBX's own SIP address is refused.

    A stale registered_phones row -- 127.0.0.1 is the one seen in the field -- makes
    resolve_extension() hand back the PBX's own address. The leg then INVITEs this server,
    which receives its own INVITE as a new inbound call and routes it to the same extension.
    """

    def _handler(self, address):
        from pbx.core.paging_handler import PagingHandler

        pbx = MagicMock()
        pbx.config.get.side_effect = lambda key, default=None: (
            5060 if key == "server.sip_port" else default
        )
        resolved = MagicMock()
        resolved.address = address
        pbx.call_router.resolve_extension.return_value = resolved
        return PagingHandler(pbx)

    def test_loopback_registration_is_refused(self):
        handler = self._handler(("127.0.0.1", 5060))
        assert not handler._destination_is_reachable(_destination(), "192.168.1.14", 5060)

    def test_own_address_is_refused(self):
        handler = self._handler(("192.168.1.14", 5060))
        assert not handler._destination_is_reachable(_destination(), "192.168.1.14", 5060)

    def test_ipv6_loopback_is_refused(self):
        handler = self._handler(("::1", 5060))
        assert not handler._destination_is_reachable(_destination(), "192.168.1.14", 5060)

    def test_a_real_ata_is_accepted(self):
        handler = self._handler(("192.168.10.50", 5060))
        assert handler._destination_is_reachable(_destination(), "192.168.1.14", 5060)

    def test_loopback_on_another_port_is_accepted(self):
        """
        A SIP endpoint genuinely running on this host, on its own port, is not the PBX --
        only the PBX's own SIP port is the loop.
        """
        handler = self._handler(("127.0.0.1", 5080))
        assert handler._destination_is_reachable(_destination(), "192.168.1.14", 5060)

    def test_unregistered_destination_is_refused(self):
        from pbx.core.paging_handler import PagingHandler

        pbx = MagicMock()
        pbx.call_router.resolve_extension.return_value = None
        handler = PagingHandler(pbx)
        assert not handler._destination_is_reachable(_destination(), "192.168.1.14", 5060)

    def test_resolution_failure_is_refused_not_raised(self):
        from pbx.core.paging_handler import PagingHandler

        pbx = MagicMock()
        pbx.call_router.resolve_extension.side_effect = RuntimeError("registry down")
        handler = PagingHandler(pbx)
        assert not handler._destination_is_reachable(_destination(), "192.168.1.14", 5060)


# --------------------------------------------------------------------------- leg failures


@pytest.mark.unit
class TestOriginatedLegFailureReasons:
    """
    A rejected leg has to reach whatever placed it.

    CallOriginator documents on_failure for a leg that never connects, but until the SIP
    layer learned to dispatch it, only a pre-flight failure or the no-answer timer ever fired
    it. A 486 from a busy FXS port, or a 488 from an ATA refusing the media, resolved
    silently -- so a page went on relaying to an amplifier that had declined the call.
    """

    def test_busy_statuses_map_to_busy(self):
        from pbx.sip.server import _ORIGINATE_FAILURE_REASONS

        for status in (486, 600, 603):
            assert _ORIGINATE_FAILURE_REASONS[status] == "busy"

    def test_timeout_statuses_map_to_no_answer(self):
        from pbx.sip.server import _ORIGINATE_FAILURE_REASONS

        for status in (408, 480):
            assert _ORIGINATE_FAILURE_REASONS[status] == "no_answer"

    def test_unknown_destination_maps_to_no_route(self):
        from pbx.sip.server import _ORIGINATE_FAILURE_REASONS

        for status in (404, 410, 484, 604):
            assert _ORIGINATE_FAILURE_REASONS[status] == "no_route"

    def test_media_rejection_falls_through_to_unreachable(self):
        """
        488 is the one a Cisco ATA returned for `a=sendonly` on an FXS port. It has no
        dedicated reason -- what matters is that it is reported at all.
        """
        from pbx.sip.server import _ORIGINATE_FAILURE_REASONS

        assert _ORIGINATE_FAILURE_REASONS.get(488, "unreachable") == "unreachable"
        assert _ORIGINATE_FAILURE_REASONS.get(500, "unreachable") == "unreachable"


@pytest.mark.unit
class TestPageFailureHandling:
    """What the pager is told when a zone's only destination refuses the call."""

    def _session_with_one_destination(self):
        from pbx.core.paging_handler import PagingHandler, _PageSession

        pbx = MagicMock()
        handler = PagingHandler(pbx)
        destination = _destination(destination_id=1)
        page = ActivePage(
            page_id="p1",
            from_extension="1513",
            zone_id=1,
            zone_extension="799",
            zone_name="Test Page",
            destinations=[destination],
            started_at=datetime.now(UTC),
            call_id="c1",
        )
        session = _PageSession(page=page, media=MagicMock(), rtp_port=10000, call_id="c1")
        return handler, session, destination, pbx

    def test_a_rejected_only_destination_ends_the_page(self):
        handler, session, destination, pbx = self._session_with_one_destination()
        handler._on_leg_failed(session, destination, "unreachable")
        # The failure tone is played on its own thread and hangs the pager up afterwards.
        assert destination.destination_id in session.failed
        pbx.paging_system.set_destination_state.assert_called_with(
            "p1", destination.destination_id, "failed"
        )

    def test_a_failure_after_teardown_is_ignored(self):
        """The duration timer and a late 486 can both arrive after the pager hung up."""
        handler, session, destination, _ = self._session_with_one_destination()
        session.torn_down = True
        handler._on_leg_failed(session, destination, "busy")
        assert destination.destination_id not in session.failed


# --------------------------------------------------------------------------- SDP validity


@pytest.mark.unit
class TestSdpOriginIsNumeric:
    """
    RFC 4566 specifies the o= line's sess-id as "a numeric string".

    Callers pass whatever identifies the call -- a SIP Call-ID, or a UUID for a
    PBX-originated leg -- so every SDP the PBX built carried a malformed origin line.
    Lenient endpoints ignore it, which is why it went unnoticed; a Cisco ATA 191 answers
    one with 488 Not Acceptable Here, which is how a page to it failed.
    """

    def test_uuid_session_id_becomes_numeric(self):
        import uuid

        from pbx.sip.sdp import SDPBuilder

        sdp = SDPBuilder.build_audio_sdp(
            "192.168.1.14", 10000, session_id=str(uuid.uuid4()), codecs=["0"]
        )
        origin = next(line for line in sdp.splitlines() if line.startswith("o="))
        assert origin.split()[1].isdigit()

    def test_sip_call_id_becomes_numeric(self):
        from pbx.sip.sdp import SDPBuilder

        sdp = SDPBuilder.build_audio_sdp(
            "192.168.1.14", 10000, session_id="0_3466822012@192.168.10.139", codecs=["0"]
        )
        origin = next(line for line in sdp.splitlines() if line.startswith("o="))
        assert origin.split()[1].isdigit()

    def test_a_numeric_id_is_left_alone(self):
        from pbx.sip.sdp import _numeric_session_id

        assert _numeric_session_id("1234567890") == "1234567890"

    def test_the_same_call_keeps_its_session_id(self):
        """
        A changed sess-id means a different session per RFC 4566, so a re-INVITE for hold or
        a codec change must not appear to start one.
        """
        from pbx.sip.sdp import _numeric_session_id

        assert _numeric_session_id("call-abc") == _numeric_session_id("call-abc")
        assert _numeric_session_id("call-abc") != _numeric_session_id("call-def")

    def test_it_fits_the_range_endpoints_parse(self):
        from pbx.sip.sdp import _numeric_session_id

        assert int(_numeric_session_id("some-call-id")) < 2**64


# --------------------------------------------------------------------------- hanging up


@pytest.mark.unit
class TestHangUpPager:
    """
    A PBX-initiated hangup has to tell the pager's phone.

    PBXCore.end_call only tears internal state down -- correct when the pager hung up first,
    since their BYE is what got us there, and wrong for every hangup the PBX starts. Without
    the BYE the phone keeps counting against a page that is already over, which is what a
    failed page did on real hardware.

    The BYE itself is SIPServer's job. It reads caller_dialog_to for the to-tag, draws CSeq
    from the call's own counter, and adds the Via, Max-Forwards and Contact a strict UA wants
    before it will act on a BYE -- none of which a hand-built one here would carry.
    """

    def _handler(self, call):
        from pbx.core.paging_handler import PagingHandler

        pbx = MagicMock()
        pbx.call_manager.get_call.return_value = call
        return PagingHandler(pbx), pbx

    def test_the_server_is_asked_to_bye_the_caller_side(self):
        handler, pbx = self._handler(MagicMock())
        handler.hang_up_pager("c1")

        pbx.sip_server._send_leg_bye.assert_called_once()
        _, kwargs = pbx.sip_server._send_leg_bye.call_args
        assert kwargs["side"] == "caller"

    def test_the_call_is_still_ended(self):
        handler, pbx = self._handler(MagicMock())
        handler.hang_up_pager("c1")
        pbx.end_call.assert_called_once_with("c1")

    def test_a_call_that_is_already_gone_still_ends_cleanly(self):
        handler, pbx = self._handler(None)
        handler.hang_up_pager("c1")

        pbx.sip_server._send_leg_bye.assert_not_called()
        pbx.end_call.assert_called_once_with("c1")

    def test_a_failed_bye_does_not_stop_the_teardown(self):
        """
        Whatever the phone does with the BYE, the PBX still has a port, a media session and
        a zone reservation to release.
        """
        handler, pbx = self._handler(MagicMock())
        pbx.sip_server._send_leg_bye.side_effect = OSError("network gone")
        handler.hang_up_pager("c1")

        pbx.end_call.assert_called_once_with("c1")


@pytest.mark.unit
class TestPagerDialogIsRecorded:
    """
    The to-tag our 200 OK minted is this dialog's identity, and it has to be stored where the
    rest of the PBX looks for it -- `caller_dialog_to`, which SIPServer._send_leg_bye reads,
    the same attribute the auto attendant and queue handler set after their own 200 OK.
    """

    def test_answering_records_the_dialog_to_header(self):
        from pbx.core.paging_handler import PagingHandler

        pbx = MagicMock()
        pbx._get_server_ip.return_value = "192.168.1.14"
        pbx._get_dtmf_payload_type.return_value = 101
        pbx.config.get.side_effect = lambda key, default=None: (
            5060 if key == "server.sip_port" else default
        )

        call = MagicMock()
        call.call_id = "c1"
        call.to_extension = "799"
        call.caller_addr = ("192.168.10.139", 5060)

        handler = PagingHandler(pbx)
        assert handler._answer_pager(call, 10000)

        # Set from the response actually sent, not invented alongside it.
        assert call.caller_dialog_to is not None


# --------------------------------------------------------------------------- legs ending


@pytest.mark.unit
class TestDestinationHangsUp:
    """
    A destination that answered and then hung up has to be noticed.

    PBXCore.end_call runs for every call, and a destination's BYE arrives under that leg's
    call id -- not the pager's. Matching only the pager left the page running with nothing on
    the other end: still relaying to an amplifier that had hung up, and still holding the
    zone busy against the next page.
    """

    def _page_with(self, destination_ids):
        from pbx.core.paging_handler import PagingHandler, _PageSession

        pbx = MagicMock()
        handler = PagingHandler(pbx)
        destinations = [
            _destination(destination_id=d, extension=f"780{d}") for d in destination_ids
        ]
        page = ActivePage(
            page_id="p1",
            from_extension="1513",
            zone_id=1,
            zone_extension="799",
            zone_name="Test Page",
            destinations=destinations,
            started_at=datetime.now(UTC),
            call_id="pager-call",
        )
        session = _PageSession(page=page, media=MagicMock(), rtp_port=10000, call_id="pager-call")
        for d in destination_ids:
            session.leg_call_ids[d] = f"leg-{d}"
            session.answered.add(d)
        handler._sessions["p1"] = session
        return handler, session, pbx

    def test_a_leg_call_id_is_recognised_as_part_of_a_page(self):
        handler, _, _ = self._page_with([1])
        assert handler.teardown_page("leg-1")

    def test_an_unrelated_call_is_not(self):
        handler, _, _ = self._page_with([1])
        assert not handler.teardown_page("some-other-call")

    def test_one_of_several_hanging_up_leaves_the_page_running(self):
        handler, session, pbx = self._page_with([1, 2])
        handler.teardown_page("leg-1")

        session.media.remove_target.assert_called_once_with(1)
        assert session.answered == {2}
        assert not session.torn_down
        pbx.end_call.assert_not_called()

    def test_the_last_one_hanging_up_ends_the_page(self):
        handler, session, pbx = self._page_with([1])
        handler.teardown_page("leg-1")

        session.media.remove_target.assert_called_once_with(1)
        # Ends the pager's call, not the leg's.
        pbx.end_call.assert_called_once_with("pager-call")

    def test_a_leg_ending_after_teardown_is_ignored(self):
        """The pager's BYE and a destination's can cross on the wire."""
        handler, session, pbx = self._page_with([1])
        session.torn_down = True
        handler.teardown_page("leg-1")

        session.media.remove_target.assert_not_called()
        pbx.end_call.assert_not_called()


# --------------------------------------------------------------------------- circuit select


@pytest.mark.unit
class TestDtmfSequenceValidation:
    """
    Checked on write, because the generator does not check on send.

    DTMFGenerator.generate_tone returns an empty list for an unknown digit and only logs a
    warning, so a typo would produce a page that selects nothing, plays over whatever circuit
    the amplifier defaulted to, and gives no sign of why.
    """

    def test_digits_pass_through(self):
        from pbx.features.paging import normalise_dtmf_sequence

        # The amplifiers here take 1-3 with 4 for all-call, or 5-7 with 8. Both banks work.
        for digits in ("1", "2", "3", "4", "5", "6", "7", "8"):
            assert normalise_dtmf_sequence(digits) == (digits, None)

    def test_star_and_hash_are_dialable(self):
        from pbx.features.paging import normalise_dtmf_sequence

        assert normalise_dtmf_sequence("*") == ("*", None)
        assert normalise_dtmf_sequence("#") == ("#", None)

    def test_whitespace_is_trimmed(self):
        from pbx.features.paging import normalise_dtmf_sequence

        assert normalise_dtmf_sequence("  2  ") == ("2", None)

    def test_none_and_empty_mean_manual_selection(self):
        from pbx.features.paging import normalise_dtmf_sequence

        assert normalise_dtmf_sequence(None) == (None, None)
        assert normalise_dtmf_sequence("") == (None, None)
        assert normalise_dtmf_sequence("   ") == (None, None)

    def test_a_letter_is_rejected_with_the_offending_character(self):
        from pbx.features.paging import normalise_dtmf_sequence

        sequence, error = normalise_dtmf_sequence("1X")
        assert sequence is None
        assert "X" in error

    def test_multiple_digits_are_allowed(self):
        from pbx.features.paging import normalise_dtmf_sequence

        assert normalise_dtmf_sequence("12") == ("12", None)

    def test_a_bad_sequence_never_reaches_the_database(self):
        system = _system()
        system.zones_db.get.return_value = {"id": 1}
        destination_id, error = system.add_sip_destination(1, "1501", dtmf_sequence="9!")
        assert destination_id is None
        assert error is not None
        system.destinations_db.add_sip_endpoint.assert_not_called()

    def test_a_good_sequence_is_stored(self):
        system = _system()
        system.zones_db.get.return_value = {"id": 1}
        system.destinations_db.add_sip_endpoint.return_value = 5
        destination_id, error = system.add_sip_destination(1, "1501", dtmf_sequence="2")
        assert (destination_id, error) == (5, None)
        assert system.destinations_db.add_sip_endpoint.call_args.kwargs["dtmf_sequence"] == "2"


@pytest.mark.unit
class TestCircuitSelectionSequencing:
    """
    The branch that decides when a destination joins the fan-out.

    Manual: it must join immediately, or the pager's own keypresses never reach the
    amplifier. Direct-dial: it must NOT join until the digits have been sent, or the pager is
    broadcast into a building before anyone has chosen which circuit.
    """

    def _answered(self, dtmf_sequence):
        from pbx.core.paging_handler import PagingHandler, _PageSession

        pbx = MagicMock()
        handler = PagingHandler(pbx)
        destination = _destination(destination_id=1)
        destination.dtmf_sequence = dtmf_sequence
        page = ActivePage(
            page_id="p1",
            from_extension="1513",
            zone_id=1,
            zone_extension="701",
            zone_name="Building 1",
            destinations=[destination],
            started_at=datetime.now(UTC),
            call_id="c1",
        )
        session = _PageSession(page=page, media=MagicMock(), rtp_port=10000, call_id="c1")
        leg = MagicMock()
        leg.callee_rtp = {"address": "192.168.10.120", "port": 16400}
        return handler, session, destination, leg

    def test_manual_joins_the_fanout_immediately(self):
        handler, session, destination, leg = self._answered(None)
        handler._on_leg_answered(session, destination, leg)

        session.media.add_target.assert_called_once_with(1, ("192.168.10.120", 16400))

    def test_direct_dial_does_not_join_before_its_digits_are_sent(self):
        handler, session, destination, leg = self._answered("2")
        with patch("threading.Thread") as thread:
            handler._on_leg_answered(session, destination, leg)

        # Handed to the selection thread rather than added here.
        session.media.add_target.assert_not_called()
        thread.assert_called_once()
        assert thread.call_args.kwargs["target"] == handler._select_circuit_then_stream

    def test_a_leg_answering_without_media_is_dropped(self):
        handler, session, destination, leg = self._answered("2")
        leg.callee_rtp = None
        handler._on_leg_answered(session, destination, leg)

        session.media.add_target.assert_not_called()
        assert 1 in session.failed

    def test_selection_still_joins_the_fanout_when_the_tones_fail(self):
        """
        An amplifier that could not be told which circuit to use is better joined than
        dropped: it pages whatever it defaults to, which beats silence.
        """
        handler, session, destination, _leg = self._answered("2")
        session.media.socket = None  # nothing to send tones on

        handler.pbx_core.config.get.side_effect = lambda key, default=None: (
            0 if "ms" in key else default
        )
        handler._select_circuit_then_stream(session, destination, ("192.168.10.120", 16400))

        session.media.add_target.assert_called_once_with(1, ("192.168.10.120", 16400))

    def test_selection_is_abandoned_if_the_page_ended_first(self):
        handler, session, destination, _leg = self._answered("2")
        session.torn_down = True
        handler.pbx_core.config.get.side_effect = lambda key, default=None: (
            0 if "ms" in key else default
        )
        handler._select_circuit_then_stream(session, destination, ("192.168.10.120", 16400))

        session.media.add_target.assert_not_called()


@pytest.mark.unit
class TestDtmfToneEncoding:
    """The generator works in floats; the RTP path wants companded bytes."""

    def test_a_digit_encodes_to_the_expected_length(self):
        from pbx.utils.dtmf import DTMFGenerator

        samples = DTMFGenerator().generate_sequence("1", tone_ms=120, gap_ms=80)
        assert len(samples) == 1600  # 200ms at 8kHz

    def test_summed_tones_clamp_rather_than_wrap(self):
        """
        A DTMF digit is two sine waves added together and peaks above 1.0. An integer that
        wrapped would turn a clean tone into noise the amplifier cannot recognise.
        """
        import struct

        from pbx.utils.audio import float_samples_to_pcm16

        encoded = float_samples_to_pcm16([2.0, -2.0, 0.0])
        assert struct.unpack("<3h", encoded) == (32767, -32767, 0)


# --------------------------------------------------------------------------- test page


@pytest.mark.unit
class TestTestPageStartup:
    """
    A page fired from the admin view, which reaches the amplifier the same way a dialled one
    does but arrives at the pager's leg from the opposite direction.

    Dialled, the pager offers media in an INVITE and the PBX answers it. Fired from the
    admin page, the PBX sends the INVITE and the media only comes back in the 200 OK -- so
    the socket has to be listening before the INVITE goes out, and a BYE toward the pager is
    addressed to the other side of the dialog. Both are easy to get wrong in ways that only
    show up against real hardware, which is the thing this path exists to avoid needing.
    """

    #: Distinguishes "the caller did not care about the zone" from "the zone is gone",
    #: which None alone cannot express and which are opposite test cases.
    _UNSET = object()

    def _handler(self, *, zone=_UNSET, page=None, error=None, leg=None):
        from pbx.core.paging_handler import PagingHandler

        pbx = MagicMock()
        pbx.paging_system.enabled = True
        pbx.paging_system.get_zone.return_value = (
            {"id": 1, "extension": "799", "name": "All buildings"} if zone is self._UNSET else zone
        )
        pbx.paging_system.begin_page.return_value = (page, error)
        pbx.rtp_relay.allocate_port.return_value = (10000, 10001)
        pbx.call_originator.originate_call.return_value = leg
        pbx.config.get.side_effect = lambda key, default=None: default

        handler = PagingHandler(pbx)
        handler._open_destination_legs = MagicMock()
        return handler, pbx

    def _page(self):
        page = MagicMock()
        page.page_id = "p1"
        page.zone_name = "All buildings"
        page.from_extension = "1513"
        page.destinations = [_destination()]
        return page

    def _leg(self, call_id="leg-1", rtp=None):
        leg = MagicMock()
        leg.call_id = call_id
        leg.callee_rtp = rtp
        return leg

    def test_the_zone_is_reserved_before_anyone_is_rung(self):
        """
        Ringing first and reserving after would ring someone for an amplifier that turns out
        to be busy -- and two testers could both be told to pick up.
        """
        handler, pbx = self._handler(page=None, error="Building 1 amplifier is already paging")

        page_id, error = handler.start_test_page("1513", 1)

        assert page_id is None
        assert "already paging" in error
        pbx.call_originator.originate_call.assert_not_called()

    def test_the_socket_is_listening_before_the_invite_goes_out(self):
        """
        A phone set to auto-answer can reply before originate_call has returned. Binding
        after would drop the opening packets onto a closed port.
        """
        listening = {}

        with patch("pbx.rtp.paging_media.PagingMediaSession") as media_class:
            media = media_class.return_value
            media.start.return_value = True

            handler, pbx = self._handler(page=self._page(), leg=self._leg())
            pbx.call_originator.originate_call.side_effect = lambda *a, **k: (
                listening.update(bound=media.start.called) or self._leg()
            )

            handler.start_test_page("1513", 1)

        assert listening["bound"] is True

    def test_the_leg_advertises_the_pages_own_port(self):
        """
        Without the override, originate_call would allocate a relay and bind a second socket
        to a port this session already owns.
        """
        with patch("pbx.rtp.paging_media.PagingMediaSession") as media_class:
            media_class.return_value.start.return_value = True
            handler, pbx = self._handler(page=self._page(), leg=self._leg())
            handler.start_test_page("1513", 1)

        kwargs = pbx.call_originator.originate_call.call_args.kwargs
        assert kwargs["rtp_ports_override"] == (10000, 10001)

    def test_the_leg_offers_pcmu_alone_and_two_way_audio(self):
        """
        Every endpoint on a page has to already agree, since the PBX does not transcode --
        and the pager needs a return path or the amplifier's own tones never arrive.
        """
        with patch("pbx.rtp.paging_media.PagingMediaSession") as media_class:
            media_class.return_value.start.return_value = True
            handler, pbx = self._handler(page=self._page(), leg=self._leg())
            handler.start_test_page("1513", 1)

        kwargs = pbx.call_originator.originate_call.call_args.kwargs
        assert kwargs["codecs"] == ["0"]
        assert kwargs["sdp_direction"] == "sendrecv"

    def test_the_handset_is_shown_the_zone_rather_than_the_pbx(self):
        with patch("pbx.rtp.paging_media.PagingMediaSession") as media_class:
            media_class.return_value.start.return_value = True
            handler, pbx = self._handler(page=self._page(), leg=self._leg())
            handler.start_test_page("1513", 1)

        assert pbx.call_originator.originate_call.call_args.kwargs["caller_id"] == (
            "799",
            "All buildings",
        )

    def test_the_page_is_findable_by_the_legs_call_id(self):
        """
        Teardown is keyed on it: without this the tester's hangup would end their call and
        leave the page running, still relaying to the amplifier.
        """
        with patch("pbx.rtp.paging_media.PagingMediaSession") as media_class:
            media_class.return_value.start.return_value = True
            handler, _ = self._handler(page=self._page(), leg=self._leg("leg-1"))
            handler.start_test_page("1513", 1)

        assert handler._sessions["p1"].call_id == "leg-1"

    def test_a_leg_that_never_starts_gives_the_port_and_the_zone_back(self):
        with patch("pbx.rtp.paging_media.PagingMediaSession") as media_class:
            media_class.return_value.start.return_value = True
            handler, pbx = self._handler(page=self._page(), leg=None)

            page_id, error = handler.start_test_page("1513", 1)

        assert page_id is None
        assert "1513" in error
        pbx.rtp_relay.release_port.assert_called_once_with(10000)
        pbx.paging_system.end_page.assert_called_once_with("p1")

    def test_a_media_session_that_cannot_bind_gives_the_zone_back(self):
        with patch("pbx.rtp.paging_media.PagingMediaSession") as media_class:
            media_class.return_value.start.return_value = False
            handler, pbx = self._handler(page=self._page(), leg=self._leg())

            page_id, error = handler.start_test_page("1513", 1)

        assert page_id is None
        assert error is not None
        pbx.call_originator.originate_call.assert_not_called()
        pbx.rtp_relay.release_port.assert_called_once_with(10000)
        pbx.paging_system.end_page.assert_called_once_with("p1")

    def test_a_zone_that_is_gone_is_refused_without_reserving_anything(self):
        handler, pbx = self._handler(zone=None, page=None)

        page_id, error = handler.start_test_page("1513", 99)

        assert page_id is None
        assert error is not None
        pbx.paging_system.begin_page.assert_not_called()


@pytest.mark.unit
class TestTestPageAnswer:
    """What happens when the handset picks up, which is where the page really begins."""

    def _session(self, pager_is_originated=True):
        from pbx.core.paging_handler import PagingHandler, _PageSession

        pbx = MagicMock()
        handler = PagingHandler(pbx)
        handler._open_destination_legs = MagicMock()

        page = MagicMock()
        page.page_id = "p1"
        page.zone_name = "All buildings"
        page.from_extension = "1513"

        session = _PageSession(
            page=page,
            media=MagicMock(),
            rtp_port=10000,
            call_id="",
            pager_is_originated=pager_is_originated,
        )
        return handler, pbx, session

    def test_the_pager_is_taken_from_the_answer_not_an_offer(self):
        handler, _, session = self._session()
        call = MagicMock()
        call.call_id = "leg-1"
        call.callee_rtp = {"address": "192.168.10.50", "port": 16400}

        handler._on_test_pager_answered(session, call)

        assert session.pager_endpoint == ("192.168.10.50", 16400)
        session.media.expect_source.assert_called_once_with(("192.168.10.50", 16400))

    def test_the_zone_opens_only_once_the_handset_is_up(self):
        handler, _, session = self._session()
        call = MagicMock()
        call.call_id = "leg-1"
        call.callee_rtp = {"address": "192.168.10.50", "port": 16400}

        handler._on_test_pager_answered(session, call)

        handler._open_destination_legs.assert_called_once_with(session)

    def test_an_answer_with_no_media_is_abandoned_rather_than_left_open(self):
        """
        Nothing could be relayed, and the page would otherwise hold the zone reserved
        against the next attempt.
        """
        handler, _, session = self._session()
        handler.hang_up_pager = MagicMock()
        call = MagicMock()
        call.call_id = "leg-1"
        call.callee_rtp = None

        handler._on_test_pager_answered(session, call)

        handler._open_destination_legs.assert_not_called()
        handler.hang_up_pager.assert_called_once_with("leg-1")

    def test_a_handset_that_never_answers_releases_everything(self):
        handler, pbx, session = self._session()
        handler._sessions["p1"] = session

        handler._on_test_pager_failed(session, "no_answer")

        assert session.torn_down is True
        pbx.rtp_relay.release_port.assert_called_once_with(10000)
        pbx.paging_system.end_page.assert_called_once_with("p1")
        session.media.stop.assert_called_once()


@pytest.mark.unit
class TestOriginatedPagerHangup:
    """
    Which side of the dialog a BYE is addressed to depends on who placed the leg.

    On a dialled page the pager is the caller and the tag we need is the one our own 200 OK
    minted. On a test page the PBX sent the INVITE, so the pager is the callee -- addressing
    a BYE to the caller side there builds it from headers that were never populated.
    """

    def _handler(self, session=None):
        from pbx.core.paging_handler import PagingHandler

        pbx = MagicMock()
        pbx.call_manager.get_call.return_value = MagicMock()
        handler = PagingHandler(pbx)
        if session is not None:
            handler._sessions["p1"] = session
        return handler, pbx

    def _session(self, call_id, originated):
        from pbx.core.paging_handler import _PageSession

        return _PageSession(
            page=MagicMock(),
            media=MagicMock(),
            rtp_port=10000,
            call_id=call_id,
            pager_is_originated=originated,
        )

    def test_an_originated_pager_is_byed_on_the_side_the_pbx_dialled(self):
        handler, pbx = self._handler(self._session("leg-1", originated=True))
        handler.hang_up_pager("leg-1")

        assert pbx.sip_server._send_leg_bye.call_args.kwargs["side"] is None

    def test_a_dialled_pager_is_still_byed_on_the_caller_side(self):
        handler, pbx = self._handler(self._session("c1", originated=False))
        handler.hang_up_pager("c1")

        assert pbx.sip_server._send_leg_bye.call_args.kwargs["side"] == "caller"

    def test_a_page_with_no_session_left_falls_back_to_the_caller_side(self):
        """The dialled case is the one that survives losing its session, so it is the default."""
        handler, pbx = self._handler()
        handler.hang_up_pager("c1")

        assert pbx.sip_server._send_leg_bye.call_args.kwargs["side"] == "caller"
