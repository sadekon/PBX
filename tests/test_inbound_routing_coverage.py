"""Tests for pbx/features/inbound_routing.py module."""

from unittest.mock import MagicMock

import pytest

from pbx.features.inbound_routing import InboundRoutingSystem


def _route(
    did_number: str,
    destination_value: str,
    trunk_id: str | None = None,
    destination_type: str = "extension",
    enabled: bool = True,
    priority: int = 100,
) -> dict:
    return {
        "id": 1,
        "did_number": did_number,
        "trunk_id": trunk_id,
        "destination_type": destination_type,
        "destination_value": destination_value,
        "enabled": enabled,
        "priority": priority,
    }


@pytest.mark.unit
class TestInboundRoutingSystemLoad:
    """Tests for route loading."""

    def test_loads_routes_from_db_keyed_by_did(self) -> None:
        db = MagicMock()
        db.get_all.return_value = [
            _route("12125551234", "1001"),
            _route("12125559999", "1002"),
        ]
        system = InboundRoutingSystem(inbound_route_db=db)
        assert set(system.routes.keys()) == {"12125551234", "12125559999"}

    def test_multiple_rows_same_did_grouped(self) -> None:
        db = MagicMock()
        db.get_all.return_value = [
            _route("12125551234", "1001", trunk_id="t1"),
            _route("12125551234", "1002", trunk_id="t2"),
        ]
        system = InboundRoutingSystem(inbound_route_db=db)
        assert len(system.routes["12125551234"]) == 2

    def test_no_db_leaves_routes_empty(self) -> None:
        system = InboundRoutingSystem()
        assert system.routes == {}

    def test_db_error_handled_gracefully(self) -> None:
        db = MagicMock()
        db.get_all.side_effect = KeyError("boom")
        system = InboundRoutingSystem(inbound_route_db=db)
        assert system.routes == {}

    def test_reload_routes_refetches(self) -> None:
        db = MagicMock()
        db.get_all.return_value = []
        system = InboundRoutingSystem(inbound_route_db=db)
        assert system.routes == {}

        db.get_all.return_value = [_route("12125551234", "1001")]
        system.reload_routes()
        assert "12125551234" in system.routes


@pytest.mark.unit
class TestInboundRoutingSystemLookup:
    """Tests for lookup()."""

    def test_trunk_specific_preferred_over_any_trunk(self) -> None:
        db = MagicMock()
        db.get_all.return_value = [
            _route("12125551234", "1001", trunk_id=None),
            _route("12125551234", "1002", trunk_id="t1"),
        ]
        system = InboundRoutingSystem(inbound_route_db=db)
        result = system.lookup("12125551234", trunk_id="t1")
        assert result["destination_value"] == "1002"
        assert result["source"] == "manual"

    def test_falls_back_to_any_trunk_when_no_trunk_specific_match(self) -> None:
        db = MagicMock()
        db.get_all.return_value = [_route("12125551234", "1001", trunk_id=None)]
        system = InboundRoutingSystem(inbound_route_db=db)
        result = system.lookup("12125551234", trunk_id="t1")
        assert result["destination_value"] == "1001"

    def test_lower_priority_wins_among_candidates(self) -> None:
        db = MagicMock()
        db.get_all.return_value = [
            _route("12125551234", "1001", trunk_id=None, priority=200),
            _route("12125551234", "1002", trunk_id=None, priority=50),
        ]
        system = InboundRoutingSystem(inbound_route_db=db)
        result = system.lookup("12125551234")
        assert result["destination_value"] == "1002"

    def test_disabled_route_is_skipped(self) -> None:
        db = MagicMock()
        db.get_all.return_value = [_route("12125551234", "1001", enabled=False)]
        ext_db = MagicMock()
        ext_db.get_by_did.return_value = None
        system = InboundRoutingSystem(inbound_route_db=db, extension_db=ext_db)
        assert system.lookup("12125551234") is None

    def test_falls_back_to_extension_did_number(self) -> None:
        db = MagicMock()
        db.get_all.return_value = []
        ext_db = MagicMock()
        ext_db.get_by_did.return_value = {"number": "1001", "did_number": "12125551234"}
        system = InboundRoutingSystem(inbound_route_db=db, extension_db=ext_db)
        result = system.lookup("12125551234")
        assert result["destination_type"] == "extension"
        assert result["destination_value"] == "1001"
        assert result["source"] == "extension"

    def test_explicit_route_wins_over_extension_did(self) -> None:
        db = MagicMock()
        db.get_all.return_value = [
            _route("12125551234", "auto-attendant-main", destination_type="auto_attendant")
        ]
        ext_db = MagicMock()
        system = InboundRoutingSystem(inbound_route_db=db, extension_db=ext_db)
        result = system.lookup("12125551234")
        assert result["source"] == "manual"
        ext_db.get_by_did.assert_not_called()

    def test_no_match_returns_none(self) -> None:
        db = MagicMock()
        db.get_all.return_value = []
        ext_db = MagicMock()
        ext_db.get_by_did.return_value = None
        system = InboundRoutingSystem(inbound_route_db=db, extension_db=ext_db)
        assert system.lookup("12125551234") is None


@pytest.mark.unit
class TestInboundRoutingSystemEffectiveRoutes:
    """Tests for get_effective_routes()."""

    def test_includes_manual_routes(self) -> None:
        db = MagicMock()
        db.get_all.return_value = [_route("12125551234", "1001")]
        system = InboundRoutingSystem(inbound_route_db=db)
        effective = system.get_effective_routes()
        assert len(effective) == 1
        assert effective[0]["source"] == "manual"

    def test_includes_extension_derived_routes(self) -> None:
        db = MagicMock()
        db.get_all.return_value = []
        ext_db = MagicMock()
        ext_db.get_all.return_value = [
            {"number": "1001", "did_number": "12125551234"},
            {"number": "1002", "did_number": None},
        ]
        system = InboundRoutingSystem(inbound_route_db=db, extension_db=ext_db)
        effective = system.get_effective_routes()
        assert len(effective) == 1
        assert effective[0]["source"] == "extension"
        assert effective[0]["destination_value"] == "1001"
        assert effective[0]["shadowed"] is False

    def test_extension_derived_route_marked_shadowed_when_manual_exists(self) -> None:
        db = MagicMock()
        db.get_all.return_value = [
            _route("12125551234", "auto-attendant-main", destination_type="auto_attendant")
        ]
        ext_db = MagicMock()
        ext_db.get_all.return_value = [{"number": "1001", "did_number": "12125551234"}]
        system = InboundRoutingSystem(inbound_route_db=db, extension_db=ext_db)
        effective = system.get_effective_routes()

        manual = next(r for r in effective if r["source"] == "manual")
        extension_derived = next(r for r in effective if r["source"] == "extension")
        assert manual["destination_type"] == "auto_attendant"
        assert extension_derived["shadowed"] is True

    def test_sorted_by_did_then_priority(self) -> None:
        db = MagicMock()
        db.get_all.return_value = [
            _route("22222222222", "1002"),
            _route("11111111111", "1001", priority=200),
            _route("11111111111", "1003", priority=10),
        ]
        system = InboundRoutingSystem(inbound_route_db=db)
        effective = system.get_effective_routes()
        assert [r["destination_value"] for r in effective] == ["1003", "1001", "1002"]
