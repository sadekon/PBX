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
from unittest.mock import MagicMock

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
    """The header is derived from the device's vendor, not stored per destination."""

    def test_cisco_gets_call_info(self):
        header = _destination(vendor="cisco").auto_answer_header("10.0.0.1")
        assert header == ("Call-Info", "<sip:10.0.0.1>;answer-after=0")

    def test_grandstream_gets_alert_info(self):
        name, value = _destination(vendor="grandstream").auto_answer_header("10.0.0.1")
        assert name == "Alert-Info"
        assert "alert-autoanswer" in value

    def test_polycom_gets_ring_answer(self):
        assert _destination(vendor="polycom").auto_answer_header("10.0.0.1") == (
            "Alert-Info",
            "Ring Answer",
        )

    def test_vendor_case_is_ignored(self):
        assert _destination(vendor="CISCO").auto_answer_header("10.0.0.1")[0] == "Call-Info"

    def test_override_beats_vendor(self):
        """The escape hatch for hardware that does not behave like its vendor implies."""
        destination = _destination(vendor="cisco", auto_answer_override="grandstream")
        assert destination.auto_answer_header("10.0.0.1")[0] == "Alert-Info"

    def test_override_none_sends_no_header(self):
        """For an amplifier that seizes the line on ring voltage by itself."""
        destination = _destination(vendor="cisco", auto_answer_override="none")
        assert destination.auto_answer_header("10.0.0.1") is None

    def test_unprovisioned_endpoint_falls_back(self):
        """A destination whose extension has no device row still gets a usable header."""
        destination = _destination(vendor=None)
        assert destination.auto_answer_header("10.0.0.1") == (
            "Call-Info",
            "<sip:10.0.0.1>;answer-after=0",
        )

    def test_unknown_vendor_falls_back(self):
        assert _destination(vendor="acme-telecom").auto_answer_header("10.0.0.1")[0] == "Call-Info"

    def test_every_known_vendor_formats_without_error(self):
        for vendor in AUTO_ANSWER_HEADERS:
            _destination(vendor=vendor).auto_answer_header("10.0.0.1")


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

    def test_audio_from_a_destination_is_not_relayed(self):
        """
        Paging is one-way. Forwarding a destination's audio would put one amplifier's noise
        onto every other circuit.
        """
        listener_a, addr_a = self._listener()
        session = PagingMediaSession(self._free_port(), "page-test")
        assert session.start()

        try:
            session.add_target(1, addr_a)

            pager = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            pager.sendto(b"from-pager", ("127.0.0.1", session.local_port))
            assert listener_a.recv(2048) == b"from-pager"

            # A second sender is not the pager, so it must be ignored.
            stray = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            stray.sendto(b"from-amplifier", ("127.0.0.1", session.local_port))

            listener_a.settimeout(0.4)
            with pytest.raises(TimeoutError):
                listener_a.recv(2048)

            pager.close()
            stray.close()
        finally:
            session.stop()
            listener_a.close()

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
