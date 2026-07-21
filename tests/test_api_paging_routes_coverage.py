"""
Tests for the Paging Blueprint routes (pbx.api.routes.paging).

The central property under test is the response contract: every collection
endpoint returns the same wrapper key whether paging is enabled or disabled,
so a client can parse one shape unconditionally, and ``/status`` is what
distinguishes "paging is off" from "paging is on but nothing is configured".
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from flask.testing import FlaskClient

AUTH_PATCH = "pbx.api.utils.verify_authentication"
AUTH_OK = (True, {"extension": "1001", "is_admin": True})


def _enabled_paging_system() -> MagicMock:
    """A PagingSystem mock that reports itself enabled."""
    paging = MagicMock()
    paging.enabled = True
    return paging


def _disable_paging(mock_pbx_core: MagicMock) -> None:
    """Turn the feature off the way FeatureInitializer does."""
    mock_pbx_core.paging_system = None


@pytest.mark.unit
class TestPagingStatus:
    """Tests for GET /api/paging/status."""

    def test_status_reports_enabled(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        paging = _enabled_paging_system()
        paging.get_status.return_value = {
            "enabled": True,
            "prefix": "7",
            "all_call_extension": "700",
            "zone_count": 2,
        }
        mock_pbx_core.paging_system = paging

        with patch(AUTH_PATCH, return_value=AUTH_OK):
            resp = api_client.get("/api/paging/status")

        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["enabled"] is True
        assert data["all_call_extension"] == "700"

    def test_status_reports_disabled(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        _disable_paging(mock_pbx_core)

        with patch(AUTH_PATCH, return_value=AUTH_OK):
            resp = api_client.get("/api/paging/status")

        assert resp.status_code == 200
        assert json.loads(resp.data)["enabled"] is False

    def test_status_requires_auth(self, api_client: FlaskClient) -> None:
        with patch(AUTH_PATCH, return_value=(False, None)):
            resp = api_client.get("/api/paging/status")
        assert resp.status_code == 401


@pytest.mark.unit
class TestPagingCollectionShape:
    """The wrapper key must not change between enabled and disabled."""

    @pytest.mark.parametrize(
        ("path", "key", "getter"),
        [
            ("/api/paging/zones", "zones", "get_zones"),
            ("/api/paging/devices", "devices", "get_dac_devices"),
            ("/api/paging/active", "active_pages", "get_active_pages"),
        ],
    )
    def test_enabled_returns_wrapped_collection(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock, path: str, key: str, getter: str
    ) -> None:
        paging = _enabled_paging_system()
        getattr(paging, getter).return_value = [{"id": "one"}]
        mock_pbx_core.paging_system = paging

        with patch(AUTH_PATCH, return_value=AUTH_OK):
            resp = api_client.get(path)

        assert resp.status_code == 200
        data = json.loads(resp.data)
        # Not a bare list: the client reads data[key] unconditionally.
        assert isinstance(data, dict)
        assert data[key] == [{"id": "one"}]

    @pytest.mark.parametrize(
        ("path", "key"),
        [
            ("/api/paging/zones", "zones"),
            ("/api/paging/devices", "devices"),
            ("/api/paging/active", "active_pages"),
        ],
    )
    def test_disabled_returns_same_shape_empty(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock, path: str, key: str
    ) -> None:
        _disable_paging(mock_pbx_core)

        with patch(AUTH_PATCH, return_value=AUTH_OK):
            resp = api_client.get(path)

        assert resp.status_code == 200
        assert json.loads(resp.data) == {key: []}

    @pytest.mark.parametrize(
        ("path", "key", "getter"),
        [
            ("/api/paging/zones", "zones", "get_zones"),
            ("/api/paging/devices", "devices", "get_dac_devices"),
            ("/api/paging/active", "active_pages", "get_active_pages"),
        ],
    )
    def test_backend_error_degrades_to_empty_collection(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock, path: str, key: str, getter: str
    ) -> None:
        paging = _enabled_paging_system()
        getattr(paging, getter).side_effect = RuntimeError("boom")
        mock_pbx_core.paging_system = paging

        with patch(AUTH_PATCH, return_value=AUTH_OK):
            resp = api_client.get(path)

        assert resp.status_code == 200
        assert json.loads(resp.data) == {key: []}


@pytest.mark.unit
class TestPagingZoneWrites:
    """Tests for zone create/delete."""

    def test_add_zone_success(self, api_client: FlaskClient, mock_pbx_core: MagicMock) -> None:
        paging = _enabled_paging_system()
        paging.add_zone.return_value = True
        mock_pbx_core.paging_system = paging

        with patch(AUTH_PATCH, return_value=AUTH_OK):
            resp = api_client.post(
                "/api/paging/zones",
                json={
                    "extension": "701",
                    "name": "Warehouse",
                    "description": "Back racks",
                    "dac_device": "dac-1",
                },
            )

        assert resp.status_code == 200
        assert json.loads(resp.data)["success"] is True
        paging.add_zone.assert_called_once_with(
            extension="701",
            name="Warehouse",
            description="Back racks",
            dac_device="dac-1",
        )

    def test_add_zone_requires_extension_and_name(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        mock_pbx_core.paging_system = _enabled_paging_system()

        with patch(AUTH_PATCH, return_value=AUTH_OK):
            resp = api_client.post("/api/paging/zones", json={"extension": "701"})

        assert resp.status_code == 400
        assert "error" in json.loads(resp.data)

    def test_add_duplicate_zone_conflicts(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        paging = _enabled_paging_system()
        paging.add_zone.return_value = False
        mock_pbx_core.paging_system = paging

        with patch(AUTH_PATCH, return_value=AUTH_OK):
            resp = api_client.post(
                "/api/paging/zones", json={"extension": "701", "name": "Warehouse"}
            )

        assert resp.status_code == 409
        assert "already exists" in json.loads(resp.data)["error"]

    def test_delete_zone_success(self, api_client: FlaskClient, mock_pbx_core: MagicMock) -> None:
        paging = _enabled_paging_system()
        paging.remove_zone.return_value = True
        mock_pbx_core.paging_system = paging

        with patch(AUTH_PATCH, return_value=AUTH_OK):
            resp = api_client.delete("/api/paging/zones/701")

        assert resp.status_code == 200
        paging.remove_zone.assert_called_once_with("701")

    def test_delete_unknown_zone_is_404(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        paging = _enabled_paging_system()
        paging.remove_zone.return_value = False
        mock_pbx_core.paging_system = paging

        with patch(AUTH_PATCH, return_value=AUTH_OK):
            resp = api_client.delete("/api/paging/zones/999")

        assert resp.status_code == 404


@pytest.mark.unit
class TestPagingDeviceWrites:
    """Tests for DAC device create/delete."""

    def test_configure_device_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        paging = _enabled_paging_system()
        paging.configure_dac_device.return_value = True
        mock_pbx_core.paging_system = paging

        with patch(AUTH_PATCH, return_value=AUTH_OK):
            resp = api_client.post(
                "/api/paging/devices",
                json={
                    "device_id": "dac-1",
                    "name": "Main PA",
                    "device_type": "sip_gateway",
                    "sip_uri": "sip:paging@192.168.1.10",
                    "ip_address": "192.168.1.10",
                    "port": 5060,
                },
            )

        assert resp.status_code == 200
        paging.configure_dac_device.assert_called_once_with(
            device_id="dac-1",
            device_type="sip_gateway",
            sip_uri="sip:paging@192.168.1.10",
            ip_address="192.168.1.10",
            port=5060,
            name="Main PA",
        )

    def test_configure_device_requires_id_and_type(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        mock_pbx_core.paging_system = _enabled_paging_system()

        with patch(AUTH_PATCH, return_value=AUTH_OK):
            resp = api_client.post("/api/paging/devices", json={"device_id": "dac-1"})

        assert resp.status_code == 400

    def test_delete_device_success(self, api_client: FlaskClient, mock_pbx_core: MagicMock) -> None:
        paging = _enabled_paging_system()
        paging.remove_dac_device.return_value = True
        mock_pbx_core.paging_system = paging

        with patch(AUTH_PATCH, return_value=AUTH_OK):
            resp = api_client.delete("/api/paging/devices/dac-1")

        assert resp.status_code == 200
        paging.remove_dac_device.assert_called_once_with("dac-1")

    def test_delete_unknown_device_is_404(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        paging = _enabled_paging_system()
        paging.remove_dac_device.return_value = False
        mock_pbx_core.paging_system = paging

        with patch(AUTH_PATCH, return_value=AUTH_OK):
            resp = api_client.delete("/api/paging/devices/nope")

        assert resp.status_code == 404


@pytest.mark.unit
class TestPagingWritesWhileDisabled:
    """Writes must fail loudly, with a reason the UI can display."""

    @pytest.mark.parametrize(
        ("method", "path", "body"),
        [
            ("post", "/api/paging/zones", {"extension": "701", "name": "Warehouse"}),
            ("post", "/api/paging/devices", {"device_id": "d1", "device_type": "sip_gateway"}),
            ("delete", "/api/paging/zones/701", None),
            ("delete", "/api/paging/devices/dac-1", None),
            ("post", "/api/paging/test", {"from_extension": "1001", "zone": "701"}),
        ],
    )
    def test_write_rejected_with_reason(
        self,
        api_client: FlaskClient,
        mock_pbx_core: MagicMock,
        method: str,
        path: str,
        body: dict | None,
    ) -> None:
        _disable_paging(mock_pbx_core)

        with patch(AUTH_PATCH, return_value=AUTH_OK):
            resp = getattr(api_client, method)(path, json=body)

        assert resp.status_code == 503
        # The message lands under "error" -- which is the key the admin UI reads.
        assert "not enabled" in json.loads(resp.data)["error"]


@pytest.mark.unit
class TestTestPageRoute:
    """Tests for POST /api/paging/test."""

    def test_test_page_accepted(self, api_client: FlaskClient, mock_pbx_core: MagicMock) -> None:
        mock_pbx_core.paging_system = _enabled_paging_system()
        mock_pbx_core.paging_handler.start_test_page.return_value = {
            "call_id": "call-1",
            "from_extension": "1001",
            "zone": "701",
        }

        with patch(AUTH_PATCH, return_value=AUTH_OK):
            resp = api_client.post(
                "/api/paging/test", json={"from_extension": "1001", "zone": "701"}
            )

        # 202, not 200: the page does not exist until the extension answers.
        assert resp.status_code == 202
        data = json.loads(resp.data)
        assert data["call_id"] == "call-1"
        mock_pbx_core.paging_handler.start_test_page.assert_called_once_with("1001", "701")

    def test_test_page_requires_both_fields(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        mock_pbx_core.paging_system = _enabled_paging_system()

        with patch(AUTH_PATCH, return_value=AUTH_OK):
            resp = api_client.post("/api/paging/test", json={"from_extension": "1001"})

        assert resp.status_code == 400

    def test_invalid_zone_is_client_error(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        mock_pbx_core.paging_system = _enabled_paging_system()
        mock_pbx_core.paging_handler.start_test_page.side_effect = ValueError(
            "999 is not a paging extension"
        )

        with patch(AUTH_PATCH, return_value=AUTH_OK):
            resp = api_client.post(
                "/api/paging/test", json={"from_extension": "1001", "zone": "999"}
            )

        assert resp.status_code == 400
        assert "not a paging extension" in json.loads(resp.data)["error"]
