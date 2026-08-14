"""Tests for the company directory served by the phone book blueprint.

Covers the two properties that matter most for a payload every authenticated
user can read: that it contains everyone, and that it contains nothing
administrative.
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from pbx.api.routes.phone_book import (
    _build_directory,
    _enrichment_by_extension,
    _matches,
)

MODULE = "pbx.api.routes.phone_book"


class FakeExtension:
    """Stand-in for a registry extension object."""

    def __init__(self, number: str, name: str, registered: bool = False, **config: Any) -> None:
        self.number = number
        self.name = name
        self.registered = registered
        self.config = config


def make_core(
    extensions: list[FakeExtension] | None = None,
    phone_book: Any = None,
) -> MagicMock:
    """Build a PBXCore double exposing just what the directory reads."""
    core = MagicMock()
    core.extension_registry.get_all.return_value = extensions or []
    core.phone_book = phone_book
    return core


def make_phone_book(entries: list[dict[str, Any]], enabled: bool = True) -> SimpleNamespace:
    """Build a PhoneBook double returning fixed enrichment entries."""
    return SimpleNamespace(enabled=enabled, get_all_entries=lambda: entries)


@pytest.fixture
def sample_extensions() -> list[FakeExtension]:
    return [
        FakeExtension(
            "1002",
            "Zoe Baker",
            registered=False,
            email="zoe@albl.com",
            did_number="5125550102",
            is_admin=True,
            sip_password="super-secret",
            voicemail_pin_hash="hashed",
            allow_external=True,
            forward_destination="9995551234",
        ),
        FakeExtension(
            "1001",
            "adam Cole",
            registered=True,
            email="adam@albl.com",
            dnd_enabled=True,
        ),
    ]


@pytest.mark.unit
class TestBuildDirectory:
    def test_returns_every_extension(self, sample_extensions: list[FakeExtension]) -> None:
        """The directory is not filtered per-caller; everyone appears."""
        with patch(f"{MODULE}.get_pbx_core", return_value=make_core(sample_extensions)):
            entries = _build_directory()

        assert len(entries) == 2
        assert {e["extension"] for e in entries} == {"1001", "1002"}

    def test_sorted_by_name_case_insensitively(
        self, sample_extensions: list[FakeExtension]
    ) -> None:
        """'adam Cole' must sort before 'Zoe Baker' despite the lowercase 'a'."""
        with patch(f"{MODULE}.get_pbx_core", return_value=make_core(sample_extensions)):
            entries = _build_directory()

        assert [e["name"] for e in entries] == ["adam Cole", "Zoe Baker"]

    def test_omits_administrative_fields(self, sample_extensions: list[FakeExtension]) -> None:
        """The field allowlist is the security boundary for this endpoint."""
        with patch(f"{MODULE}.get_pbx_core", return_value=make_core(sample_extensions)):
            entries = _build_directory()

        assert entries, "expected at least one entry to inspect"
        for entry in entries:
            for leaked in (
                "is_admin",
                "sip_password",
                "password_hash",
                "voicemail_pin_hash",
                "allow_external",
                "forward_destination",
            ):
                assert leaked not in entry, f"{leaked} must not reach the directory payload"

    def test_exposes_expected_fields(self, sample_extensions: list[FakeExtension]) -> None:
        with patch(f"{MODULE}.get_pbx_core", return_value=make_core(sample_extensions)):
            entries = _build_directory()

        assert set(entries[0]) == {
            "extension",
            "name",
            "email",
            "did_number",
            "registered",
            "dnd_enabled",
            "department",
            "mobile",
            "office_location",
        }

    def test_presence_reflects_registration(self, sample_extensions: list[FakeExtension]) -> None:
        with patch(f"{MODULE}.get_pbx_core", return_value=make_core(sample_extensions)):
            by_ext = {e["extension"]: e for e in _build_directory()}

        assert by_ext["1001"]["registered"] is True
        assert by_ext["1002"]["registered"] is False
        assert by_ext["1001"]["dnd_enabled"] is True
        assert by_ext["1002"]["dnd_enabled"] is False

    def test_missing_optional_fields_become_none(self) -> None:
        """An extension with no email or DID still yields a well-formed entry."""
        core = make_core([FakeExtension("1003", "Bare Minimum")])
        with patch(f"{MODULE}.get_pbx_core", return_value=core):
            entries = _build_directory()

        assert entries[0]["email"] is None
        assert entries[0]["did_number"] is None

    def test_returns_empty_without_registry(self) -> None:
        with patch(f"{MODULE}.get_pbx_core", return_value=None):
            assert _build_directory() == []


@pytest.mark.unit
class TestEnrichment:
    def test_merges_phone_book_detail(self) -> None:
        phone_book = make_phone_book(
            [{"extension": "1001", "department": "Support", "mobile": "5125550111"}]
        )
        core = make_core([FakeExtension("1001", "Adam Cole")], phone_book=phone_book)

        with patch(f"{MODULE}.get_pbx_core", return_value=core):
            entries = _build_directory()

        assert entries[0]["department"] == "Support"
        assert entries[0]["mobile"] == "5125550111"

    def test_directory_serves_when_feature_disabled(self) -> None:
        """Enrichment is optional; a disabled feature must not break the page."""
        phone_book = make_phone_book(
            [{"extension": "1001", "department": "Support"}], enabled=False
        )
        core = make_core([FakeExtension("1001", "Adam Cole")], phone_book=phone_book)

        with patch(f"{MODULE}.get_pbx_core", return_value=core):
            entries = _build_directory()

        assert len(entries) == 1
        assert entries[0]["department"] is None

    def test_enrichment_failure_degrades_gracefully(self) -> None:
        """A broken phone_book lookup costs the extra columns, not the directory."""

        def explode() -> list[dict[str, Any]]:
            raise KeyError("phone_book table missing")

        phone_book = SimpleNamespace(enabled=True, get_all_entries=explode)
        core = make_core([FakeExtension("1001", "Adam Cole")], phone_book=phone_book)

        with patch(f"{MODULE}.get_pbx_core", return_value=core):
            assert _enrichment_by_extension() == {}
            entries = _build_directory()

        assert len(entries) == 1
        assert entries[0]["department"] is None

    def test_no_phone_book_attribute(self) -> None:
        core = make_core([FakeExtension("1001", "Adam Cole")], phone_book=None)
        with patch(f"{MODULE}.get_pbx_core", return_value=core):
            assert _enrichment_by_extension() == {}


@pytest.mark.unit
class TestMatches:
    @pytest.fixture
    def entry(self) -> dict[str, Any]:
        return {
            "extension": "1001",
            "name": "Adam Cole",
            "email": "adam@albl.com",
            "did_number": "5125550101",
            "department": "Support",
            "mobile": None,
            "office_location": None,
        }

    @pytest.mark.parametrize(
        "needle",
        ["adam", "cole", "1001", "albl.com", "support", "5125550101"],
    )
    def test_matches_across_fields(self, entry: dict[str, Any], needle: str) -> None:
        assert _matches(entry, needle)

    def test_is_case_insensitive(self, entry: dict[str, Any]) -> None:
        assert _matches(entry, "ADAM".casefold())

    def test_rejects_non_matches(self, entry: dict[str, Any]) -> None:
        assert not _matches(entry, "nobody")

    def test_none_fields_do_not_raise(self, entry: dict[str, Any]) -> None:
        assert not _matches(entry, "missing-office")


@pytest.mark.unit
class TestRoutes:
    def test_directory_requires_authentication(self, api_client: Any) -> None:
        assert api_client.get("/api/phone-book").status_code == 401

    def test_search_requires_authentication(self, api_client: Any) -> None:
        assert api_client.get("/api/phone-book/search?q=adam").status_code == 401

    def test_get_returns_wrapped_entries(
        self,
        api_client: Any,
        mock_pbx_core: MagicMock,
        sample_extensions: list[FakeExtension],
    ) -> None:
        """Response shape is {entries, count}, which is what the frontend reads."""
        mock_pbx_core.extension_registry.get_all.return_value = sample_extensions
        mock_pbx_core.phone_book = None

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": False}),
        ):
            payload = api_client.get("/api/phone-book").get_json()

        assert payload["count"] == 2
        assert len(payload["entries"]) == 2
        assert payload["entries"][0]["name"] == "adam Cole"

    def test_non_admin_sees_every_entry(
        self,
        api_client: Any,
        mock_pbx_core: MagicMock,
        sample_extensions: list[FakeExtension],
    ) -> None:
        """The point of the endpoint: a regular user gets the whole directory.

        GET /api/extensions would return this caller a single row.
        """
        mock_pbx_core.extension_registry.get_all.return_value = sample_extensions
        mock_pbx_core.phone_book = None

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": False}),
        ):
            payload = api_client.get("/api/phone-book").get_json()

        assert {e["extension"] for e in payload["entries"]} == {"1001", "1002"}

    def test_search_filters_entries(
        self,
        api_client: Any,
        mock_pbx_core: MagicMock,
        sample_extensions: list[FakeExtension],
    ) -> None:
        mock_pbx_core.extension_registry.get_all.return_value = sample_extensions
        mock_pbx_core.phone_book = None

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": False}),
        ):
            payload = api_client.get("/api/phone-book/search?q=zoe").get_json()

        assert payload["count"] == 1
        assert payload["entries"][0]["extension"] == "1002"

    def test_search_rejects_blank_query(self, api_client: Any, mock_pbx_core: MagicMock) -> None:
        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": False}),
        ):
            response = api_client.get("/api/phone-book/search?q=%20%20")

        assert response.status_code == 400
