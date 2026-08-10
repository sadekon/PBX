"""Comprehensive tests for Features Blueprint routes."""

import json
from datetime import UTC
from unittest.mock import MagicMock, patch

import pytest
from flask.testing import FlaskClient

from pbx.features.retention_policies import RetentionPolicy


@pytest.mark.unit
class TestAutoAttendantConfigRoutes:
    """Tests for Auto-Attendant configuration endpoints."""

    def test_get_auto_attendant_config_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        aa = MagicMock()
        aa.enabled = True
        aa.extension = "9000"
        aa.timeout = 10
        aa.max_retries = 3
        aa.audio_path = "/audio"
        mock_pbx_core.auto_attendant = aa

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/auto-attendant/config")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["enabled"] is True
        assert data["extension"] == "9000"
        assert data["timeout"] == 10

    def test_get_auto_attendant_config_not_available(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        if hasattr(mock_pbx_core, "auto_attendant"):
            del mock_pbx_core.auto_attendant

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/auto-attendant/config")
        assert resp.status_code == 500
        data = json.loads(resp.data)
        assert "error" in data

    def test_get_auto_attendant_config_exception(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        aa = MagicMock()
        type(aa).enabled = property(lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
        mock_pbx_core.auto_attendant = aa

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/auto-attendant/config")
        assert resp.status_code == 500

    def test_get_auto_attendant_config_unauthenticated(self, api_client: FlaskClient) -> None:
        with patch("pbx.api.utils.verify_authentication", return_value=(False, None)):
            resp = api_client.get("/api/auto-attendant/config")
        assert resp.status_code == 401

    def test_update_auto_attendant_config_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        aa = MagicMock()
        mock_pbx_core.auto_attendant = aa

        with (
            patch(
                "pbx.api.utils.verify_authentication",
                return_value=(True, {"extension": "1001", "is_admin": True}),
            ),
            patch("pbx.api.routes.features._regenerate_voice_prompts"),
        ):
            resp = api_client.put(
                "/api/auto-attendant/config",
                data=json.dumps({"enabled": True, "timeout": 15}),
                content_type="application/json",
            )
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["success"] is True
        aa.update_config.assert_called_once_with(enabled=True, timeout=15)

    def test_update_auto_attendant_config_with_prompts(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        aa = MagicMock()
        mock_pbx_core.auto_attendant = aa

        with (
            patch(
                "pbx.api.utils.verify_authentication",
                return_value=(True, {"extension": "1001", "is_admin": True}),
            ),
            patch("pbx.api.routes.features._regenerate_voice_prompts"),
        ):
            resp = api_client.put(
                "/api/auto-attendant/config",
                data=json.dumps({"prompts": {"welcome": "Hello"}}),
                content_type="application/json",
            )
        assert resp.status_code == 200

    def test_update_auto_attendant_config_not_available(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        if hasattr(mock_pbx_core, "auto_attendant"):
            del mock_pbx_core.auto_attendant

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.put(
                "/api/auto-attendant/config",
                data=json.dumps({"enabled": True}),
                content_type="application/json",
            )
        assert resp.status_code == 500


@pytest.mark.unit
class TestAutoAttendantMenuOptionRoutes:
    """Tests for Auto-Attendant menu options endpoints."""

    def test_get_menu_options_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        aa = MagicMock()
        aa.menu_options = {
            "1": {"destination": "1001", "description": "Sales"},
            "2": {"destination": "1002", "description": "Support"},
        }
        mock_pbx_core.auto_attendant = aa

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/auto-attendant/menu-options")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert len(data["menu_options"]) == 2

    def test_get_menu_options_not_available(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        if hasattr(mock_pbx_core, "auto_attendant"):
            del mock_pbx_core.auto_attendant

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/auto-attendant/menu-options")
        assert resp.status_code == 500

    def test_add_menu_option_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        aa = MagicMock()
        mock_pbx_core.auto_attendant = aa

        with (
            patch(
                "pbx.api.utils.verify_authentication",
                return_value=(True, {"extension": "1001", "is_admin": True}),
            ),
            patch("pbx.api.routes.features._regenerate_voice_prompts"),
        ):
            resp = api_client.post(
                "/api/auto-attendant/menu-options",
                data=json.dumps(
                    {
                        "digit": "3",
                        "destination": "1003",
                        "description": "Billing",
                    }
                ),
                content_type="application/json",
            )
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["success"] is True
        aa.add_menu_option.assert_called_once_with("3", "1003", "Billing")

    def test_add_menu_option_missing_fields(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        aa = MagicMock()
        mock_pbx_core.auto_attendant = aa

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/auto-attendant/menu-options",
                data=json.dumps({"digit": ""}),
                content_type="application/json",
            )
        assert resp.status_code == 400

    def test_update_menu_option_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        aa = MagicMock()
        aa.menu_options = {
            "1": {"destination": "1001", "description": "Sales"},
        }
        mock_pbx_core.auto_attendant = aa

        with (
            patch(
                "pbx.api.utils.verify_authentication",
                return_value=(True, {"extension": "1001", "is_admin": True}),
            ),
            patch("pbx.api.routes.features._regenerate_voice_prompts"),
        ):
            resp = api_client.put(
                "/api/auto-attendant/menu-options/1",
                data=json.dumps({"destination": "1010"}),
                content_type="application/json",
            )
        assert resp.status_code == 200

    def test_update_menu_option_not_found(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        aa = MagicMock()
        aa.menu_options = {}
        mock_pbx_core.auto_attendant = aa

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.put(
                "/api/auto-attendant/menu-options/9",
                data=json.dumps({"destination": "1010"}),
                content_type="application/json",
            )
        assert resp.status_code == 404

    def test_delete_menu_option_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        aa = MagicMock()
        aa.menu_options = {"1": {"destination": "1001", "description": "Sales"}}
        mock_pbx_core.auto_attendant = aa

        with (
            patch(
                "pbx.api.utils.verify_authentication",
                return_value=(True, {"extension": "1001", "is_admin": True}),
            ),
            patch("pbx.api.routes.features._regenerate_voice_prompts"),
        ):
            resp = api_client.delete("/api/auto-attendant/menu-options/1")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["success"] is True

    def test_delete_menu_option_not_found(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        aa = MagicMock()
        aa.menu_options = {}
        mock_pbx_core.auto_attendant = aa

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.delete("/api/auto-attendant/menu-options/9")
        assert resp.status_code == 404


@pytest.mark.unit
class TestAutoAttendantPromptRoutes:
    """Tests for Auto-Attendant prompt endpoints."""

    def test_get_prompts_success(self, api_client: FlaskClient, mock_pbx_core: MagicMock) -> None:
        mock_pbx_core.config.get.side_effect = lambda key, default=None: (
            {"welcome": "Hi"}
            if key == "auto_attendant"
            else "TestCo"
            if key == "company_name"
            else default
        )

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/auto-attendant/prompts")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "prompts" in data

    def test_update_prompts_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        mock_pbx_core.config.config = {"auto_attendant": {}}
        mock_pbx_core.config.save.return_value = True

        with (
            patch(
                "pbx.api.utils.verify_authentication",
                return_value=(True, {"extension": "1001", "is_admin": True}),
            ),
            patch("pbx.api.routes.features._regenerate_voice_prompts"),
        ):
            resp = api_client.put(
                "/api/auto-attendant/prompts",
                data=json.dumps(
                    {
                        "prompts": {"welcome": "Welcome"},
                        "company_name": "ACME",
                    }
                ),
                content_type="application/json",
            )
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["success"] is True


@pytest.mark.unit
class TestAutoAttendantMenuRoutes:
    """Tests for Auto-Attendant multi-level menu endpoints."""

    def test_get_menus_success(self, api_client: FlaskClient, mock_pbx_core: MagicMock) -> None:
        aa = MagicMock()
        aa.list_menus.return_value = [{"menu_id": "main", "name": "Main Menu"}]
        mock_pbx_core.auto_attendant = aa

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/auto-attendant/menus")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert len(data["menus"]) == 1

    def test_get_menu_by_id_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        aa = MagicMock()
        aa.get_menu.return_value = {"menu_id": "main", "name": "Main Menu"}
        mock_pbx_core.auto_attendant = aa

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/auto-attendant/menus/main")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["menu"]["menu_id"] == "main"

    def test_get_menu_by_id_not_found(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        aa = MagicMock()
        aa.get_menu.return_value = None
        mock_pbx_core.auto_attendant = aa

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/auto-attendant/menus/nonexistent")
        assert resp.status_code == 404

    def test_create_menu_success(self, api_client: FlaskClient, mock_pbx_core: MagicMock) -> None:
        aa = MagicMock()
        aa.create_menu.return_value = True
        aa.audio_path = "/audio"
        mock_pbx_core.auto_attendant = aa

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/auto-attendant/menus",
                data=json.dumps(
                    {
                        "menu_id": "support-menu",
                        "menu_name": "Support Menu",
                        "prompt_text": "Press 1 for help",
                    }
                ),
                content_type="application/json",
            )
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["success"] is True

    def test_create_menu_missing_fields(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        aa = MagicMock()
        mock_pbx_core.auto_attendant = aa

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/auto-attendant/menus",
                data=json.dumps({"menu_id": "test"}),
                content_type="application/json",
            )
        assert resp.status_code == 400

    def test_create_menu_invalid_id_format(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        aa = MagicMock()
        mock_pbx_core.auto_attendant = aa

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/auto-attendant/menus",
                data=json.dumps(
                    {
                        "menu_id": "INVALID ID!!",
                        "menu_name": "Bad",
                    }
                ),
                content_type="application/json",
            )
        assert resp.status_code == 400

    def test_create_menu_failure(self, api_client: FlaskClient, mock_pbx_core: MagicMock) -> None:
        aa = MagicMock()
        aa.create_menu.return_value = False
        mock_pbx_core.auto_attendant = aa

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/auto-attendant/menus",
                data=json.dumps(
                    {
                        "menu_id": "test-menu",
                        "menu_name": "Test Menu",
                    }
                ),
                content_type="application/json",
            )
        assert resp.status_code == 400

    def test_update_menu_success(self, api_client: FlaskClient, mock_pbx_core: MagicMock) -> None:
        aa = MagicMock()
        aa.get_menu.return_value = {"menu_id": "main", "name": "Main"}
        aa.update_menu.return_value = True
        aa.audio_path = "/audio"
        mock_pbx_core.auto_attendant = aa

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.put(
                "/api/auto-attendant/menus/main",
                data=json.dumps({"menu_name": "Updated Main"}),
                content_type="application/json",
            )
        assert resp.status_code == 200

    def test_update_menu_not_found(self, api_client: FlaskClient, mock_pbx_core: MagicMock) -> None:
        aa = MagicMock()
        aa.get_menu.return_value = None
        mock_pbx_core.auto_attendant = aa

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.put(
                "/api/auto-attendant/menus/nonexistent",
                data=json.dumps({"menu_name": "X"}),
                content_type="application/json",
            )
        assert resp.status_code == 404

    def test_delete_menu_success(self, api_client: FlaskClient, mock_pbx_core: MagicMock) -> None:
        aa = MagicMock()
        aa.delete_menu.return_value = True
        mock_pbx_core.auto_attendant = aa

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.delete("/api/auto-attendant/menus/sub-menu")
        assert resp.status_code == 200

    def test_delete_menu_failure(self, api_client: FlaskClient, mock_pbx_core: MagicMock) -> None:
        aa = MagicMock()
        aa.delete_menu.return_value = False
        mock_pbx_core.auto_attendant = aa

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.delete("/api/auto-attendant/menus/main")
        assert resp.status_code == 400

    def test_get_menu_items_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        aa = MagicMock()
        aa.get_menu.return_value = {"menu_id": "main"}
        aa.get_menu_items.return_value = [
            {
                "digit": "1",
                "destination_type": "extension",
                "destination_value": "1001",
                "description": "Sales",
            },
        ]
        mock_pbx_core.auto_attendant = aa

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/auto-attendant/menus/main/items")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["menu_id"] == "main"
        assert len(data["items"]) == 1

    def test_get_menu_items_menu_not_found(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        aa = MagicMock()
        aa.get_menu.return_value = None
        mock_pbx_core.auto_attendant = aa

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/auto-attendant/menus/missing/items")
        assert resp.status_code == 404

    def test_add_menu_item_success(self, api_client: FlaskClient, mock_pbx_core: MagicMock) -> None:
        aa = MagicMock()
        aa.get_menu.return_value = {"menu_id": "main"}
        aa.add_menu_item.return_value = True
        mock_pbx_core.auto_attendant = aa

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/auto-attendant/menus/main/items",
                data=json.dumps(
                    {
                        "digit": "1",
                        "destination_type": "extension",
                        "destination_value": "1001",
                        "description": "Sales",
                    }
                ),
                content_type="application/json",
            )
        assert resp.status_code == 200

    def test_add_menu_item_missing_fields(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        aa = MagicMock()
        mock_pbx_core.auto_attendant = aa

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/auto-attendant/menus/main/items",
                data=json.dumps({"digit": "1"}),
                content_type="application/json",
            )
        assert resp.status_code == 400

    def test_add_menu_item_menu_not_found(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        aa = MagicMock()
        aa.get_menu.return_value = None
        mock_pbx_core.auto_attendant = aa

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/auto-attendant/menus/missing/items",
                data=json.dumps(
                    {
                        "digit": "1",
                        "destination_type": "extension",
                        "destination_value": "1001",
                    }
                ),
                content_type="application/json",
            )
        assert resp.status_code == 404

    def test_update_menu_item_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        aa = MagicMock()
        aa.get_menu_items.return_value = [
            {
                "digit": "1",
                "destination_type": "extension",
                "destination_value": "1001",
                "description": "Sales",
            },
        ]
        aa.add_menu_item.return_value = True
        mock_pbx_core.auto_attendant = aa

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.put(
                "/api/auto-attendant/menus/main/items/1",
                data=json.dumps({"destination_value": "1010"}),
                content_type="application/json",
            )
        assert resp.status_code == 200

    def test_update_menu_item_not_found(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        aa = MagicMock()
        aa.get_menu_items.return_value = []
        mock_pbx_core.auto_attendant = aa

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.put(
                "/api/auto-attendant/menus/main/items/9",
                data=json.dumps({"destination_value": "1010"}),
                content_type="application/json",
            )
        assert resp.status_code == 404

    def test_update_menu_item_no_fields_provided(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        aa = MagicMock()
        mock_pbx_core.auto_attendant = aa

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.put(
                "/api/auto-attendant/menus/main/items/1",
                data=json.dumps({}),
                content_type="application/json",
            )
        assert resp.status_code == 400

    def test_delete_menu_item_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        aa = MagicMock()
        aa.remove_menu_item.return_value = True
        mock_pbx_core.auto_attendant = aa

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.delete("/api/auto-attendant/menus/main/items/1")
        assert resp.status_code == 200

    def test_delete_menu_item_failure(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        aa = MagicMock()
        aa.remove_menu_item.return_value = False
        mock_pbx_core.auto_attendant = aa

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.delete("/api/auto-attendant/menus/main/items/9")
        assert resp.status_code == 500

    def test_get_menu_tree_success(self, api_client: FlaskClient, mock_pbx_core: MagicMock) -> None:
        aa = MagicMock()
        aa.get_menu_tree.return_value = {"menu_id": "main", "children": []}
        mock_pbx_core.auto_attendant = aa

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/auto-attendant/menu-tree")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "menu_tree" in data

    def test_get_menu_tree_failure(self, api_client: FlaskClient, mock_pbx_core: MagicMock) -> None:
        aa = MagicMock()
        aa.get_menu_tree.return_value = None
        mock_pbx_core.auto_attendant = aa

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/auto-attendant/menu-tree")
        assert resp.status_code == 500


@pytest.mark.unit
class TestSIPTrunkRoutes:
    """Tests for SIP Trunk endpoints."""

    def test_get_sip_trunks_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        ts = MagicMock()
        ts.get_trunk_status.return_value = [{"trunk_id": "t1", "status": "active"}]
        mock_pbx_core.trunk_system = ts

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/sip-trunks")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["count"] == 1

    def test_get_sip_trunks_not_initialized(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        if hasattr(mock_pbx_core, "trunk_system"):
            del mock_pbx_core.trunk_system

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/sip-trunks")
        assert resp.status_code == 500

    def test_get_trunk_health_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        trunk = MagicMock()
        trunk.trunk_id = "t1"
        trunk.name = "Trunk 1"
        trunk.get_health_metrics.return_value = {"latency": 50}
        ts = MagicMock()
        ts.trunks = {"t1": trunk}
        ts.monitoring_active = True
        ts.failover_enabled = True
        mock_pbx_core.trunk_system = ts

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/sip-trunks/health")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["monitoring_active"] is True

    def test_add_sip_trunk_success(self, api_client: FlaskClient, mock_pbx_core: MagicMock) -> None:
        ts = MagicMock()
        trunk_instance = MagicMock()
        trunk_instance.name = "New Trunk"
        trunk_data = {
            "trunk_id": "t1",
            "name": "New Trunk",
            "host": "sip.provider.com",
            "username": "user",
            "password": "pass",
        }
        trunk_instance.to_dict.return_value = trunk_data
        # First call is the pre-add duplicate check (must be falsy), second call
        # is the post-reload lookup used to build the response.
        ts.get_trunk.side_effect = [None, trunk_instance]
        mock_pbx_core.trunk_system = ts

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/sip-trunks",
                data=json.dumps(trunk_data),
                content_type="application/json",
            )
        assert resp.status_code == 200

    def test_test_sip_trunk_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        trunk = MagicMock()
        trunk.check_health.return_value = MagicMock(value="healthy")
        trunk.get_health_metrics.return_value = {"latency": 20}
        ts = MagicMock()
        ts.get_trunk.return_value = trunk
        mock_pbx_core.trunk_system = ts

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/sip-trunks/test",
                data=json.dumps({"trunk_id": "t1"}),
                content_type="application/json",
            )
        assert resp.status_code == 200

    def test_test_sip_trunk_not_found(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        ts = MagicMock()
        ts.get_trunk.return_value = None
        mock_pbx_core.trunk_system = ts

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/sip-trunks/test",
                data=json.dumps({"trunk_id": "nonexistent"}),
                content_type="application/json",
            )
        assert resp.status_code == 404

    def test_delete_sip_trunk_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        ts = MagicMock()
        ts.get_trunk.return_value = MagicMock()
        mock_pbx_core.trunk_system = ts

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.delete("/api/sip-trunks/t1")
        assert resp.status_code == 200

    def test_delete_sip_trunk_not_found(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        ts = MagicMock()
        ts.get_trunk.return_value = None
        mock_pbx_core.trunk_system = ts

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.delete("/api/sip-trunks/nonexistent")
        assert resp.status_code == 404


@pytest.mark.unit
class TestInboundRouteRoutes:
    """Tests for Inbound DID Routing endpoints."""

    def test_get_inbound_routes_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        routing = MagicMock()
        routing.get_effective_routes.return_value = [
            {"did_number": "12125551234", "destination_value": "1001", "source": "manual"}
        ]
        mock_pbx_core.inbound_routing = routing

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/inbound-routes")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["count"] == 1

    def test_get_inbound_routes_not_initialized(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        if hasattr(mock_pbx_core, "inbound_routing"):
            del mock_pbx_core.inbound_routing

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/inbound-routes")
        assert resp.status_code == 500

    def test_add_inbound_route_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        mock_pbx_core.inbound_routing = MagicMock()
        mock_pbx_core.inbound_route_db = MagicMock()
        mock_pbx_core.inbound_route_db.add.return_value = True

        route_data = {
            "did_number": "12125551234",
            "destination_type": "extension",
            "destination_value": "1001",
        }

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/inbound-routes",
                data=json.dumps(route_data),
                content_type="application/json",
            )
        assert resp.status_code == 200
        mock_pbx_core.inbound_routing.reload_routes.assert_called_once()

    def test_add_inbound_route_missing_fields(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        mock_pbx_core.inbound_routing = MagicMock()
        mock_pbx_core.inbound_route_db = MagicMock()

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/inbound-routes",
                data=json.dumps({"did_number": "12125551234"}),
                content_type="application/json",
            )
        assert resp.status_code == 400

    def test_add_inbound_route_invalid_destination_type(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        mock_pbx_core.inbound_routing = MagicMock()
        mock_pbx_core.inbound_route_db = MagicMock()

        route_data = {
            "did_number": "12125551234",
            "destination_type": "queue",
            "destination_value": "1001",
        }

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/inbound-routes",
                data=json.dumps(route_data),
                content_type="application/json",
            )
        assert resp.status_code == 400

    def test_update_inbound_route_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        mock_pbx_core.inbound_routing = MagicMock()
        mock_pbx_core.inbound_route_db = MagicMock()
        mock_pbx_core.inbound_route_db.get.return_value = {"id": 1, "did_number": "12125551234"}
        mock_pbx_core.inbound_route_db.update.return_value = True

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.put(
                "/api/inbound-routes/1",
                data=json.dumps({"destination_value": "1002"}),
                content_type="application/json",
            )
        assert resp.status_code == 200

    def test_update_inbound_route_not_found(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        mock_pbx_core.inbound_routing = MagicMock()
        mock_pbx_core.inbound_route_db = MagicMock()
        mock_pbx_core.inbound_route_db.get.return_value = None

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.put(
                "/api/inbound-routes/999",
                data=json.dumps({"destination_value": "1002"}),
                content_type="application/json",
            )
        assert resp.status_code == 404

    def test_update_inbound_route_invalid_destination_type(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        mock_pbx_core.inbound_routing = MagicMock()
        mock_pbx_core.inbound_route_db = MagicMock()
        mock_pbx_core.inbound_route_db.get.return_value = {"id": 1, "did_number": "12125551234"}

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.put(
                "/api/inbound-routes/1",
                data=json.dumps({"destination_type": "queue"}),
                content_type="application/json",
            )
        assert resp.status_code == 400

    def test_delete_inbound_route_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        mock_pbx_core.inbound_routing = MagicMock()
        mock_pbx_core.inbound_route_db = MagicMock()
        mock_pbx_core.inbound_route_db.get.return_value = {"id": 1, "did_number": "12125551234"}
        mock_pbx_core.inbound_route_db.delete.return_value = True

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.delete("/api/inbound-routes/1")
        assert resp.status_code == 200

    def test_delete_inbound_route_not_found(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        mock_pbx_core.inbound_routing = MagicMock()
        mock_pbx_core.inbound_route_db = MagicMock()
        mock_pbx_core.inbound_route_db.get.return_value = None

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.delete("/api/inbound-routes/999")
        assert resp.status_code == 404


@pytest.mark.unit
class TestLCRRoutes:
    """Tests for LCR (Least-Cost Routing) endpoints."""

    def test_get_lcr_rates_success(self, api_client: FlaskClient, mock_pbx_core: MagicMock) -> None:
        rate_entry = MagicMock()
        rate_entry.trunk_id = "t1"
        rate_entry.pattern.pattern = "^1"
        rate_entry.pattern.description = "US"
        rate_entry.rate_per_minute = 0.01
        rate_entry.connection_fee = 0.0
        rate_entry.minimum_seconds = 0
        rate_entry.billing_increment = 1

        lcr = MagicMock()
        lcr.rate_entries = [rate_entry]
        lcr.time_based_rates = []
        mock_pbx_core.lcr = lcr

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/lcr/rates")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["count"] == 1

    def test_get_lcr_rates_not_initialized(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        if hasattr(mock_pbx_core, "lcr"):
            del mock_pbx_core.lcr

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/lcr/rates")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["count"] == 0

    def test_get_lcr_statistics_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        lcr = MagicMock()
        lcr.get_statistics.return_value = {"total_calls": 100}
        mock_pbx_core.lcr = lcr

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/lcr/statistics")
        assert resp.status_code == 200

    def test_get_lcr_statistics_not_initialized(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        if hasattr(mock_pbx_core, "lcr"):
            del mock_pbx_core.lcr

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/lcr/statistics")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["total_calls"] == 0

    def test_add_lcr_rate_success(self, api_client: FlaskClient, mock_pbx_core: MagicMock) -> None:
        lcr = MagicMock()
        mock_pbx_core.lcr = lcr

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/lcr/rate",
                data=json.dumps(
                    {
                        "trunk_id": "t1",
                        "pattern": "^1",
                        "rate_per_minute": 0.01,
                    }
                ),
                content_type="application/json",
            )
        assert resp.status_code == 200

    def test_add_lcr_rate_not_initialized(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        if hasattr(mock_pbx_core, "lcr"):
            del mock_pbx_core.lcr

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/lcr/rate",
                data=json.dumps(
                    {
                        "trunk_id": "t1",
                        "pattern": "^1",
                        "rate_per_minute": 0.01,
                    }
                ),
                content_type="application/json",
            )
        assert resp.status_code == 500

    def test_add_lcr_time_rate_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        lcr = MagicMock()
        mock_pbx_core.lcr = lcr

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/lcr/time-rate",
                data=json.dumps(
                    {
                        "name": "Off-Peak",
                        "start_hour": 18,
                        "start_minute": 0,
                        "end_hour": 8,
                        "end_minute": 0,
                        "days": [0, 1, 2, 3, 4],
                        "multiplier": 0.5,
                    }
                ),
                content_type="application/json",
            )
        assert resp.status_code == 200

    def test_clear_lcr_rates_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        lcr = MagicMock()
        mock_pbx_core.lcr = lcr

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post("/api/lcr/clear-rates")
        assert resp.status_code == 200
        lcr.clear_rates.assert_called_once()

    def test_clear_lcr_time_rates_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        lcr = MagicMock()
        mock_pbx_core.lcr = lcr

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post("/api/lcr/clear-time-rates")
        assert resp.status_code == 200
        lcr.clear_time_rates.assert_called_once()


@pytest.mark.unit
class TestFMFMRoutes:
    """Tests for Find Me/Follow Me endpoints."""

    def test_get_fmfm_extensions_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        fmfm = MagicMock()
        fmfm.list_extensions_with_fmfm.return_value = ["1001"]
        fmfm.get_config.return_value = {"extension": "1001", "enabled": True}
        mock_pbx_core.find_me_follow_me = fmfm

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/fmfm/extensions")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["count"] == 1

    def test_get_fmfm_extensions_not_initialized(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        if hasattr(mock_pbx_core, "find_me_follow_me"):
            del mock_pbx_core.find_me_follow_me

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/fmfm/extensions")
        assert resp.status_code == 500

    def test_get_fmfm_config_found(self, api_client: FlaskClient, mock_pbx_core: MagicMock) -> None:
        fmfm = MagicMock()
        fmfm.get_config.return_value = {"extension": "1001", "enabled": True}
        mock_pbx_core.find_me_follow_me = fmfm

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/fmfm/config/1001")
        assert resp.status_code == 200

    def test_get_fmfm_config_not_found(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        fmfm = MagicMock()
        fmfm.get_config.return_value = None
        mock_pbx_core.find_me_follow_me = fmfm

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/fmfm/config/9999")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["enabled"] is False

    def test_get_fmfm_statistics_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        fmfm = MagicMock()
        fmfm.get_statistics.return_value = {"total_calls": 50}
        mock_pbx_core.find_me_follow_me = fmfm

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/fmfm/statistics")
        assert resp.status_code == 200

    def test_set_fmfm_config_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        fmfm = MagicMock()
        fmfm.set_config.return_value = True
        fmfm.get_config.return_value = {"extension": "1001", "enabled": True}
        mock_pbx_core.find_me_follow_me = fmfm

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/fmfm/config",
                data=json.dumps({"extension": "1001", "enabled": True}),
                content_type="application/json",
            )
        assert resp.status_code == 200

    def test_set_fmfm_config_missing_extension(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        fmfm = MagicMock()
        mock_pbx_core.find_me_follow_me = fmfm

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/fmfm/config",
                data=json.dumps({"enabled": True}),
                content_type="application/json",
            )
        assert resp.status_code == 400

    def test_set_fmfm_config_failure(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        fmfm = MagicMock()
        fmfm.set_config.return_value = False
        mock_pbx_core.find_me_follow_me = fmfm

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/fmfm/config",
                data=json.dumps({"extension": "1001", "enabled": True}),
                content_type="application/json",
            )
        assert resp.status_code == 500

    def test_add_fmfm_destination_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        fmfm = MagicMock()
        fmfm.add_destination.return_value = True
        fmfm.get_config.return_value = {"extension": "1001"}
        mock_pbx_core.find_me_follow_me = fmfm

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/fmfm/destination",
                data=json.dumps({"extension": "1001", "number": "5551234"}),
                content_type="application/json",
            )
        assert resp.status_code == 200

    def test_add_fmfm_destination_missing_fields(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        fmfm = MagicMock()
        mock_pbx_core.find_me_follow_me = fmfm

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/fmfm/destination",
                data=json.dumps({"extension": "1001"}),
                content_type="application/json",
            )
        assert resp.status_code == 400

    def test_remove_fmfm_destination_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        fmfm = MagicMock()
        fmfm.remove_destination.return_value = True
        mock_pbx_core.find_me_follow_me = fmfm

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.delete("/api/fmfm/destination/1001/5551234")
        assert resp.status_code == 200

    def test_remove_fmfm_destination_not_found(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        fmfm = MagicMock()
        fmfm.remove_destination.return_value = False
        mock_pbx_core.find_me_follow_me = fmfm

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.delete("/api/fmfm/destination/1001/9999999")
        assert resp.status_code == 404

    def test_disable_fmfm_success(self, api_client: FlaskClient, mock_pbx_core: MagicMock) -> None:
        fmfm = MagicMock()
        fmfm.delete_config.return_value = True
        mock_pbx_core.find_me_follow_me = fmfm

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.delete("/api/fmfm/config/1001")
        assert resp.status_code == 200

    def test_disable_fmfm_not_found(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        fmfm = MagicMock()
        fmfm.delete_config.return_value = False
        mock_pbx_core.find_me_follow_me = fmfm

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.delete("/api/fmfm/config/9999")
        assert resp.status_code == 404


@pytest.mark.unit
class TestTimeRoutingRoutes:
    """Tests for Time-Based Routing endpoints."""

    def test_get_time_routing_rules_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        tbr = MagicMock()
        tbr.list_rules.return_value = [{"rule_id": "r1", "name": "Business Hours"}]
        mock_pbx_core.time_based_routing = tbr

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/time-routing/rules")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["count"] == 1

    def test_get_time_routing_rules_not_initialized(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        if hasattr(mock_pbx_core, "time_based_routing"):
            del mock_pbx_core.time_based_routing

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/time-routing/rules")
        assert resp.status_code == 500

    def test_get_time_routing_statistics_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        tbr = MagicMock()
        tbr.get_statistics.return_value = {"total": 10}
        mock_pbx_core.time_based_routing = tbr

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/time-routing/statistics")
        assert resp.status_code == 200

    def test_add_time_routing_rule_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        tbr = MagicMock()
        tbr.add_rule.return_value = "r1"
        mock_pbx_core.time_based_routing = tbr

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/time-routing/rule",
                data=json.dumps(
                    {
                        "name": "Business Hours",
                        "destination": "1001",
                        "route_to": "1002",
                        "time_conditions": [{"start": "09:00", "end": "17:00"}],
                    }
                ),
                content_type="application/json",
            )
        assert resp.status_code == 200

    def test_add_time_routing_rule_missing_fields(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        tbr = MagicMock()
        mock_pbx_core.time_based_routing = tbr

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/time-routing/rule",
                data=json.dumps({"name": "Test"}),
                content_type="application/json",
            )
        assert resp.status_code == 400

    def test_add_time_routing_rule_failure(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        tbr = MagicMock()
        tbr.add_rule.return_value = None
        mock_pbx_core.time_based_routing = tbr

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/time-routing/rule",
                data=json.dumps(
                    {
                        "name": "Test",
                        "destination": "1001",
                        "route_to": "1002",
                        "time_conditions": [],
                    }
                ),
                content_type="application/json",
            )
        assert resp.status_code == 500

    def test_delete_time_routing_rule_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        tbr = MagicMock()
        tbr.delete_rule.return_value = True
        mock_pbx_core.time_based_routing = tbr

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.delete("/api/time-routing/rule/r1")
        assert resp.status_code == 200

    def test_delete_time_routing_rule_not_found(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        tbr = MagicMock()
        tbr.delete_rule.return_value = False
        mock_pbx_core.time_based_routing = tbr

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.delete("/api/time-routing/rule/nonexistent")
        assert resp.status_code == 404


@pytest.mark.unit
class TestRecordingRetentionRoutes:
    """
    Tests for the retention endpoints.

    These address the sweeper, which is now the single retention object: `recording_retention`
    and `retention_sweeper` are the same instance. Policies come off a persisted store rather
    than the in-memory dict that used to lose them on restart.
    """

    @staticmethod
    def _auth():
        return patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        )

    @staticmethod
    def _retention(mock_pbx_core: MagicMock) -> MagicMock:
        rr = MagicMock()
        rr.settings.audio_days = 90
        rr.settings.transcript_days = 365
        rr.policies.all.return_value = []
        # Default: nothing exists yet, so a POST is a create. A MagicMock would otherwise
        # return a truthy policy and make every save look like an edit.
        rr.policies.get.return_value = None
        mock_pbx_core.recording_retention = rr
        return rr

    def test_get_retention_policies_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        rr = self._retention(mock_pbx_core)
        rr.policies.all.return_value = [
            RetentionPolicy(
                policy_id="default",
                name="Default retention",
                audio_days=90,
                transcript_days=365,
                priority=1000,
                origin="config",
            )
        ]

        with self._auth():
            resp = api_client.get("/api/recording-retention/policies")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["count"] == 1
        policy = data["policies"][0]
        assert policy["policy_id"] == "default"
        assert policy["audio_days"] == 90
        assert policy["transcript_days"] == 365
        # A policy with no rules governs everything; the UI has to be able to say so.
        assert policy["catch_all"] is True

    def test_policies_report_the_fallback_periods(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        """A null period on a policy means 'inherit', so the page needs the inherited value."""
        self._retention(mock_pbx_core)

        with self._auth():
            resp = api_client.get("/api/recording-retention/policies")
        data = json.loads(resp.data)
        assert data["fallback_audio_days"] == 90
        assert data["fallback_transcript_days"] == 365

    def test_get_retention_policies_not_initialized(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        if hasattr(mock_pbx_core, "recording_retention"):
            del mock_pbx_core.recording_retention

        with self._auth():
            resp = api_client.get("/api/recording-retention/policies")
        assert resp.status_code == 500

    def test_get_retention_statistics_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        rr = self._retention(mock_pbx_core)
        rr.statistics.return_value = {
            "enabled": True,
            "dry_run": False,
            "policies": 1,
            "active_holds": 2,
            "managed_recordings": 100,
            "fallback_audio_days": 90,
            "fallback_transcript_days": 365,
            "last_sweep": None,
            "lifetime_audio_deleted": 50,
            "lifetime_transcripts_deleted": 5,
            "last_sweep_summary": None,
        }

        with self._auth():
            resp = api_client.get("/api/recording-retention/statistics")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["total_policies"] == 1
        assert data["total_recordings"] == 100
        assert data["deleted_count"] == 50
        assert data["active_holds"] == 2

    def test_statistics_surface_dry_run(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        """
        The most important field on the page.

        Without it "0 deleted, never cleaned" reads as "retention ran and had nothing to do",
        when it actually means retention is in report-only mode. The old UI showed exactly
        that ambiguous zero.
        """
        rr = self._retention(mock_pbx_core)
        rr.statistics.return_value = {
            "enabled": True,
            "dry_run": True,
            "policies": 1,
            "active_holds": 0,
            "managed_recordings": 7,
            "fallback_audio_days": 90,
            "fallback_transcript_days": 365,
            "last_sweep": "2026-08-07T00:00:00+00:00",
            "lifetime_audio_deleted": 0,
            "lifetime_transcripts_deleted": 0,
            "last_sweep_summary": "would delete 3 audio file(s) (1.0 MB) and 0 transcript(s)",
        }

        with self._auth():
            resp = api_client.get("/api/recording-retention/statistics")
        data = json.loads(resp.data)
        assert data["dry_run"] is True
        assert data["enabled"] is True
        assert data["deleted_count"] == 0
        assert "would delete" in data["last_sweep_summary"]

    def test_add_retention_policy_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        rr = self._retention(mock_pbx_core)
        rr.policies.save.return_value = True

        with self._auth():
            resp = api_client.post(
                "/api/recording-retention/policy",
                data=json.dumps(
                    {"name": "Support calls", "audio_days": 30, "transcript_days": 180}
                ),
                content_type="application/json",
            )
        assert resp.status_code == 200
        saved = rr.policies.save.call_args[0][0]
        assert saved.audio_days == 30
        assert saved.transcript_days == 180
        assert saved.origin == "api"

    def test_add_policy_accepts_match_rules(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        rr = self._retention(mock_pbx_core)
        rr.policies.save.return_value = True

        with self._auth():
            resp = api_client.post(
                "/api/recording-retention/policy",
                data=json.dumps(
                    {
                        "name": "Voicemail",
                        "audio_days": 30,
                        "match_rules": {"media": "voicemail"},
                    }
                ),
                content_type="application/json",
            )
        assert resp.status_code == 200
        assert rr.policies.save.call_args[0][0].match_rules == {"media": "voicemail"}

    def test_add_policy_rejects_unknown_match_key(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        """
        A typo must not be silently dropped.

        Ignoring an unrecognised condition makes a policy match *more* files, and this
        subsystem deletes them -- so a misspelled key would quietly widen a narrow policy
        into a catch-all.
        """
        rr = self._retention(mock_pbx_core)

        with self._auth():
            resp = api_client.post(
                "/api/recording-retention/policy",
                data=json.dumps(
                    {"name": "Typo", "audio_days": 30, "match_rules": {"medai": "voicemail"}}
                ),
                content_type="application/json",
            )
        assert resp.status_code == 400
        rr.policies.save.assert_not_called()

    def test_add_policy_allows_one_period_only(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        """Setting only audio_days leaves transcripts on the fallback, not on zero days."""
        rr = self._retention(mock_pbx_core)
        rr.policies.save.return_value = True

        with self._auth():
            resp = api_client.post(
                "/api/recording-retention/policy",
                data=json.dumps({"name": "Audio only", "audio_days": 30}),
                content_type="application/json",
            )
        assert resp.status_code == 200
        assert rr.policies.save.call_args[0][0].transcript_days is None

    def test_add_retention_policy_missing_fields(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        self._retention(mock_pbx_core)

        with self._auth():
            resp = api_client.post(
                "/api/recording-retention/policy",
                data=json.dumps({"audio_days": 30}),
                content_type="application/json",
            )
        assert resp.status_code == 400

    def test_add_policy_requires_at_least_one_period(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        self._retention(mock_pbx_core)

        with self._auth():
            resp = api_client.post(
                "/api/recording-retention/policy",
                data=json.dumps({"name": "Empty"}),
                content_type="application/json",
            )
        assert resp.status_code == 400

    def test_add_retention_policy_invalid_days(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        self._retention(mock_pbx_core)

        with self._auth():
            resp = api_client.post(
                "/api/recording-retention/policy",
                data=json.dumps({"name": "Bad", "audio_days": 9999}),
                content_type="application/json",
            )
        assert resp.status_code == 400

    def test_add_retention_policy_invalid_name(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        self._retention(mock_pbx_core)

        with self._auth():
            resp = api_client.post(
                "/api/recording-retention/policy",
                data=json.dumps({"name": "<script>alert(1)</script>", "audio_days": 30}),
                content_type="application/json",
            )
        assert resp.status_code == 400

    def test_delete_retention_policy_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        rr = self._retention(mock_pbx_core)
        rr.policies.delete.return_value = True

        with self._auth():
            resp = api_client.delete("/api/recording-retention/policy/p1")
        assert resp.status_code == 200

    def test_delete_retention_policy_not_found(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        rr = self._retention(mock_pbx_core)
        rr.policies.delete.return_value = False

        with self._auth():
            resp = api_client.delete("/api/recording-retention/policy/nonexistent")
        assert resp.status_code == 404


@pytest.mark.unit
class TestDefaultPolicyProtection:
    """
    The seeded catch-all is editable but not removable.

    It governs everything no other policy matches, so deleting it silently hands that job back
    to the ``retention:`` block in config.yml -- which this page never shows. That is the same
    "the UI says one thing, deletion follows another" trap the two retention systems created.
    """

    @staticmethod
    def _auth():
        return patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        )

    def test_the_default_policy_cannot_be_deleted(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        rr = MagicMock()
        mock_pbx_core.recording_retention = rr

        with self._auth():
            resp = api_client.delete("/api/recording-retention/policy/default")

        assert resp.status_code == 403
        rr.policies.delete.assert_not_called()

    def test_other_policies_are_still_deletable(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        rr = MagicMock()
        rr.policies.delete.return_value = True
        mock_pbx_core.recording_retention = rr

        with self._auth():
            resp = api_client.delete("/api/recording-retention/policy/vip")

        assert resp.status_code == 200

    def test_editing_the_default_keeps_its_config_origin(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        """
        An edit must not relabel the seeded row as user-created.

        Origin is what tells seed() the table has been provisioned; flipping it on first edit
        would be a lie about where the policy came from.
        """
        rr = MagicMock()
        rr.settings.audio_days = 90
        rr.settings.transcript_days = 365
        rr.policies.save.return_value = True
        rr.policies.get.return_value = RetentionPolicy(
            policy_id="default", name="Default retention", audio_days=90, origin="config"
        )
        mock_pbx_core.recording_retention = rr

        with self._auth():
            resp = api_client.post(
                "/api/recording-retention/policy",
                data=json.dumps(
                    {"policy_id": "default", "name": "Default retention", "audio_days": 180}
                ),
                content_type="application/json",
            )

        assert resp.status_code == 200
        saved = rr.policies.save.call_args[0][0]
        assert saved.policy_id == "default"
        assert saved.audio_days == 180
        assert saved.origin == "config"

    def test_a_new_policy_is_marked_api_created(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        rr = MagicMock()
        rr.settings.audio_days = 90
        rr.settings.transcript_days = 365
        rr.policies.save.return_value = True
        rr.policies.get.return_value = None
        mock_pbx_core.recording_retention = rr

        with self._auth():
            resp = api_client.post(
                "/api/recording-retention/policy",
                data=json.dumps({"name": "Support", "audio_days": 30}),
                content_type="application/json",
            )

        assert resp.status_code == 200
        assert rr.policies.save.call_args[0][0].origin == "api"

    def test_posting_an_existing_id_updates_rather_than_duplicates(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        """The edit path: same policy_id in, same policy_id saved."""
        rr = MagicMock()
        rr.settings.audio_days = 90
        rr.settings.transcript_days = 365
        rr.policies.save.return_value = True
        rr.policies.get.return_value = RetentionPolicy(
            policy_id="support", name="Support", audio_days=30
        )
        mock_pbx_core.recording_retention = rr

        with self._auth():
            resp = api_client.post(
                "/api/recording-retention/policy",
                data=json.dumps({"policy_id": "support", "name": "Renamed", "audio_days": 45}),
                content_type="application/json",
            )

        assert resp.status_code == 200
        assert rr.policies.save.call_args[0][0].policy_id == "support"


@pytest.mark.unit
class TestRetentionHoldRoutes:
    """
    Tests for legal holds.

    A hold is not a long retention period: it suspends expiry entirely and must be released
    deliberately. That is why both a reason and an actor are mandatory.
    """

    @staticmethod
    def _auth():
        return patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        )

    def test_place_hold_success(self, api_client: FlaskClient, mock_pbx_core: MagicMock) -> None:
        rr = MagicMock()
        rr.holds.place.return_value = True
        mock_pbx_core.recording_retention = rr

        with self._auth():
            resp = api_client.post(
                "/api/recording-retention/hold",
                data=json.dumps({"session_id": "abc-123", "reason": "Case 2026-14"}),
                content_type="application/json",
            )
        assert resp.status_code == 200
        rr.holds.place.assert_called_once()
        assert rr.holds.place.call_args[0][0] == "abc-123"

    def test_place_hold_requires_a_reason(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        """A hold nobody can explain is one nobody will ever dare release."""
        rr = MagicMock()
        mock_pbx_core.recording_retention = rr

        with self._auth():
            resp = api_client.post(
                "/api/recording-retention/hold",
                data=json.dumps({"session_id": "abc-123"}),
                content_type="application/json",
            )
        assert resp.status_code == 400
        rr.holds.place.assert_not_called()

    def test_place_hold_requires_a_session(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        rr = MagicMock()
        mock_pbx_core.recording_retention = rr

        with self._auth():
            resp = api_client.post(
                "/api/recording-retention/hold",
                data=json.dumps({"reason": "Case 2026-14"}),
                content_type="application/json",
            )
        assert resp.status_code == 400
        rr.holds.place.assert_not_called()

    def test_release_hold_success(self, api_client: FlaskClient, mock_pbx_core: MagicMock) -> None:
        rr = MagicMock()
        rr.holds.release.return_value = True
        mock_pbx_core.recording_retention = rr

        with self._auth():
            resp = api_client.delete("/api/recording-retention/hold/abc-123")
        assert resp.status_code == 200

    def test_list_holds_excludes_released_by_default(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        rr = MagicMock()
        rr.holds.list_holds.return_value = [{"session_id": "abc-123"}]
        mock_pbx_core.recording_retention = rr

        with self._auth():
            resp = api_client.get("/api/recording-retention/holds")
        assert resp.status_code == 200
        assert json.loads(resp.data)["count"] == 1
        rr.holds.list_holds.assert_called_once_with(False)

    def test_list_holds_can_include_released(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        rr = MagicMock()
        rr.holds.list_holds.return_value = []
        mock_pbx_core.recording_retention = rr

        with self._auth():
            resp = api_client.get("/api/recording-retention/holds?include_released=true")
        assert resp.status_code == 200
        rr.holds.list_holds.assert_called_once_with(True)


@pytest.mark.unit
class TestFraudDetectionRoutes:
    """Tests for Fraud Detection endpoints."""

    def test_get_fraud_alerts_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        fd = MagicMock()
        fd.get_alerts.return_value = [{"alert_id": "a1", "type": "suspicious"}]
        mock_pbx_core.fraud_detection = fd

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/fraud-detection/alerts")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["count"] == 1

    def test_get_fraud_alerts_with_params(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        fd = MagicMock()
        fd.get_alerts.return_value = []
        mock_pbx_core.fraud_detection = fd

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/fraud-detection/alerts?extension=1001&hours=48")
        assert resp.status_code == 200

    def test_get_fraud_alerts_not_initialized(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        if hasattr(mock_pbx_core, "fraud_detection"):
            del mock_pbx_core.fraud_detection

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/fraud-detection/alerts")
        assert resp.status_code == 500

    def test_get_fraud_statistics_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        fd = MagicMock()
        fd.get_statistics.return_value = {
            "total_alerts": 10,
            "blocked_patterns": 2,
            "total_extensions_tracked": 5,
            "alerts_24h": 3,
        }
        fd.alerts = [{"fraud_score": 0.8}, {"fraud_score": 0.3}]
        fd.blocked_patterns = ["^900"]
        mock_pbx_core.fraud_detection = fd

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/fraud-detection/statistics")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["high_risk_alerts"] == 1

    def test_get_fraud_extension_stats_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        fd = MagicMock()
        fd.get_extension_statistics.return_value = {"extension": "1001", "alerts": 0}
        mock_pbx_core.fraud_detection = fd

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/fraud-detection/extension/1001")
        assert resp.status_code == 200

    def test_get_fraud_extension_stats_invalid_format(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        fd = MagicMock()
        mock_pbx_core.fraud_detection = fd

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/fraud-detection/extension/abc")
        assert resp.status_code == 400

    def test_add_blocked_pattern_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        fd = MagicMock()
        fd.add_blocked_pattern.return_value = True
        mock_pbx_core.fraud_detection = fd

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/fraud-detection/blocked-pattern",
                data=json.dumps({"pattern": "^900", "reason": "Premium rate"}),
                content_type="application/json",
            )
        assert resp.status_code == 200

    def test_add_blocked_pattern_missing_fields(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        fd = MagicMock()
        mock_pbx_core.fraud_detection = fd

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/fraud-detection/blocked-pattern",
                data=json.dumps({"pattern": "^900"}),
                content_type="application/json",
            )
        assert resp.status_code == 400

    def test_add_blocked_pattern_invalid_regex(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        fd = MagicMock()
        mock_pbx_core.fraud_detection = fd

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/fraud-detection/blocked-pattern",
                data=json.dumps({"pattern": "[invalid", "reason": "Test"}),
                content_type="application/json",
            )
        assert resp.status_code == 400

    def test_delete_blocked_pattern_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        fd = MagicMock()
        fd.blocked_patterns = ["^900", "^976"]
        mock_pbx_core.fraud_detection = fd

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.delete("/api/fraud-detection/blocked-pattern/0")
        assert resp.status_code == 200

    def test_delete_blocked_pattern_not_found(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        fd = MagicMock()
        fd.blocked_patterns = []
        mock_pbx_core.fraud_detection = fd

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.delete("/api/fraud-detection/blocked-pattern/99")
        assert resp.status_code == 404

    def test_delete_blocked_pattern_invalid_id(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        fd = MagicMock()
        fd.blocked_patterns = []
        mock_pbx_core.fraud_detection = fd

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.delete("/api/fraud-detection/blocked-pattern/abc")
        assert resp.status_code == 400


@pytest.mark.unit
class TestCallbackQueueRoutes:
    """Tests for Callback Queue endpoints."""

    def test_get_callback_statistics_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        cq = MagicMock()
        cq.get_statistics.return_value = {"pending": 3, "completed": 10}
        mock_pbx_core.callback_queue = cq

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/callback-queue/statistics")
        assert resp.status_code == 200

    def test_get_callback_statistics_not_initialized(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        if hasattr(mock_pbx_core, "callback_queue"):
            del mock_pbx_core.callback_queue

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/callback-queue/statistics")
        assert resp.status_code == 500

    def test_get_callback_list_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        cq = MagicMock()
        cq.callbacks = ["cb_1", "cb_2"]
        cq.get_callback_info.side_effect = [
            {"callback_id": "cb_1", "requested_at": "2024-01-01"},
            {"callback_id": "cb_2", "requested_at": "2024-01-02"},
        ]
        mock_pbx_core.callback_queue = cq

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/callback-queue/list")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert len(data["callbacks"]) == 2

    def test_get_queue_callbacks_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        cq = MagicMock()
        cq.list_queue_callbacks.return_value = []
        cq.get_queue_statistics.return_value = {"pending": 0}
        mock_pbx_core.callback_queue = cq

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/callback-queue/queue/sales")
        assert resp.status_code == 200

    def test_get_queue_callbacks_invalid_id(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        cq = MagicMock()
        mock_pbx_core.callback_queue = cq

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/callback-queue/queue/invalid id!!!")
        assert resp.status_code == 400

    def test_get_callback_info_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        cq = MagicMock()
        cq.get_callback_info.return_value = {"callback_id": "cb_abc123", "status": "pending"}
        mock_pbx_core.callback_queue = cq

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/callback-queue/info/cb_abc123")
        assert resp.status_code == 200

    def test_get_callback_info_not_found(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        cq = MagicMock()
        cq.get_callback_info.return_value = None
        mock_pbx_core.callback_queue = cq

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/callback-queue/info/cb_nonexistent")
        assert resp.status_code == 404

    def test_get_callback_info_invalid_id(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        cq = MagicMock()
        mock_pbx_core.callback_queue = cq

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/callback-queue/info/bad_id")
        assert resp.status_code == 400

    def test_request_callback_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        cq = MagicMock()
        cq.request_callback.return_value = {"callback_id": "cb_new", "position": 1}
        mock_pbx_core.callback_queue = cq

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/callback-queue/request",
                data=json.dumps(
                    {
                        "queue_id": "sales",
                        "caller_number": "5551234",
                    }
                ),
                content_type="application/json",
            )
        assert resp.status_code == 200

    def test_request_callback_missing_fields(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        cq = MagicMock()
        mock_pbx_core.callback_queue = cq

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/callback-queue/request",
                data=json.dumps({"queue_id": "sales"}),
                content_type="application/json",
            )
        assert resp.status_code == 400

    def test_request_callback_error_result(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        cq = MagicMock()
        cq.request_callback.return_value = {"error": "Queue full"}
        mock_pbx_core.callback_queue = cq

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/callback-queue/request",
                data=json.dumps(
                    {
                        "queue_id": "sales",
                        "caller_number": "5551234",
                    }
                ),
                content_type="application/json",
            )
        assert resp.status_code == 400

    def test_start_callback_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        cq = MagicMock()
        cq.start_callback.return_value = {"status": "in_progress"}
        mock_pbx_core.callback_queue = cq

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/callback-queue/start",
                data=json.dumps(
                    {
                        "callback_id": "cb_1",
                        "agent_id": "agent_1",
                    }
                ),
                content_type="application/json",
            )
        assert resp.status_code == 200

    def test_start_callback_missing_fields(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        cq = MagicMock()
        mock_pbx_core.callback_queue = cq

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/callback-queue/start",
                data=json.dumps({"callback_id": "cb_1"}),
                content_type="application/json",
            )
        assert resp.status_code == 400

    def test_complete_callback_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        cq = MagicMock()
        cq.complete_callback.return_value = True
        mock_pbx_core.callback_queue = cq

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/callback-queue/complete",
                data=json.dumps(
                    {
                        "callback_id": "cb_1",
                        "success": True,
                    }
                ),
                content_type="application/json",
            )
        assert resp.status_code == 200

    def test_complete_callback_not_found(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        cq = MagicMock()
        cq.complete_callback.return_value = False
        mock_pbx_core.callback_queue = cq

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/callback-queue/complete",
                data=json.dumps(
                    {
                        "callback_id": "cb_nonexistent",
                        "success": False,
                    }
                ),
                content_type="application/json",
            )
        assert resp.status_code == 404

    def test_complete_callback_missing_fields(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        cq = MagicMock()
        mock_pbx_core.callback_queue = cq

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/callback-queue/complete",
                data=json.dumps({"callback_id": "cb_1"}),
                content_type="application/json",
            )
        assert resp.status_code == 400

    def test_cancel_callback_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        cq = MagicMock()
        cq.cancel_callback.return_value = True
        mock_pbx_core.callback_queue = cq

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/callback-queue/cancel",
                data=json.dumps({"callback_id": "cb_1"}),
                content_type="application/json",
            )
        assert resp.status_code == 200

    def test_cancel_callback_not_found(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        cq = MagicMock()
        cq.cancel_callback.return_value = False
        mock_pbx_core.callback_queue = cq

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/callback-queue/cancel",
                data=json.dumps({"callback_id": "cb_missing"}),
                content_type="application/json",
            )
        assert resp.status_code == 404

    def test_cancel_callback_missing_fields(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        cq = MagicMock()
        mock_pbx_core.callback_queue = cq

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/callback-queue/cancel",
                data=json.dumps({}),
                content_type="application/json",
            )
        assert resp.status_code == 400


@pytest.mark.unit
class TestMobilePushRoutes:
    """Tests for Mobile Push Notification endpoints."""

    def test_get_all_devices_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        from datetime import datetime, timezone

        mp = MagicMock()
        now = datetime.now(tz=UTC)
        mp.device_tokens = {
            "user1": [
                {"platform": "ios", "registered_at": now, "last_seen": now},
            ],
        }
        mock_pbx_core.mobile_push = mp

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/mobile-push/devices")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["total"] == 1

    def test_get_all_devices_not_initialized(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        if hasattr(mock_pbx_core, "mobile_push"):
            del mock_pbx_core.mobile_push

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/mobile-push/devices")
        assert resp.status_code == 500

    def test_get_user_devices_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        mp = MagicMock()
        mp.get_user_devices.return_value = [{"platform": "ios"}]
        mock_pbx_core.mobile_push = mp

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/mobile-push/devices/user1")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["count"] == 1

    def test_get_user_devices_invalid_id(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        mp = MagicMock()
        mock_pbx_core.mobile_push = mp

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/mobile-push/devices/invalid user!!")
        assert resp.status_code == 400

    def test_get_push_statistics_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        mp = MagicMock()
        mp.device_tokens = {
            "user1": [{"platform": "ios"}],
        }
        mp.notification_history = [{"sent_at": "2024-01-01"}]
        mock_pbx_core.mobile_push = mp

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/mobile-push/statistics")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["total_devices"] == 1
        assert data["total_users"] == 1

    def test_register_mobile_device_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        mp = MagicMock()
        mp.register_device.return_value = True
        mock_pbx_core.mobile_push = mp

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/mobile-push/register",
                data=json.dumps(
                    {
                        "user_id": "user1",
                        "device_token": "abc123",
                        "platform": "ios",
                    }
                ),
                content_type="application/json",
            )
        assert resp.status_code == 200

    def test_register_mobile_device_missing_fields(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        mp = MagicMock()
        mock_pbx_core.mobile_push = mp

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/mobile-push/register",
                data=json.dumps({"user_id": "user1"}),
                content_type="application/json",
            )
        assert resp.status_code == 400

    def test_unregister_mobile_device_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        mp = MagicMock()
        mp.unregister_device.return_value = True
        mock_pbx_core.mobile_push = mp

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/mobile-push/unregister",
                data=json.dumps(
                    {
                        "user_id": "user1",
                        "device_token": "abc123",
                    }
                ),
                content_type="application/json",
            )
        assert resp.status_code == 200

    def test_unregister_mobile_device_not_found(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        mp = MagicMock()
        mp.unregister_device.return_value = False
        mock_pbx_core.mobile_push = mp

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/mobile-push/unregister",
                data=json.dumps(
                    {
                        "user_id": "user1",
                        "device_token": "nonexistent",
                    }
                ),
                content_type="application/json",
            )
        assert resp.status_code == 404

    def test_test_push_notification_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        mp = MagicMock()
        mp.send_test_notification.return_value = {"sent": 1}
        mock_pbx_core.mobile_push = mp

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/mobile-push/test",
                data=json.dumps({"user_id": "user1"}),
                content_type="application/json",
            )
        assert resp.status_code == 200

    def test_test_push_notification_missing_user(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        mp = MagicMock()
        mock_pbx_core.mobile_push = mp

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/mobile-push/test",
                data=json.dumps({}),
                content_type="application/json",
            )
        assert resp.status_code == 400

    def test_test_push_notification_error(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        mp = MagicMock()
        mp.send_test_notification.return_value = {"error": "No devices"}
        mock_pbx_core.mobile_push = mp

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/mobile-push/test",
                data=json.dumps({"user_id": "user1"}),
                content_type="application/json",
            )
        assert resp.status_code == 400


@pytest.mark.unit
class TestRecordingAnnouncementsRoutes:
    """Tests for Recording Announcements endpoints."""

    def test_get_announcement_statistics_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        ra = MagicMock()
        ra.stats.return_value = {
            "enabled": True,
            "announce_for": "external",
            "announced": 100,
            "failed": 2,
            "audio_file": "auto_attendant/recording_notice.wav",
            "audio_present": True,
            "text": "This call is being recorded.",
        }
        mock_pbx_core.recording_announcements = ra

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/recording-announcements/statistics")

        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["enabled"] is True
        assert data["announcements_played"] == 100
        # Each failure discarded a recording, so this is the number worth surfacing.
        assert data["announcements_failed"] == 2
        assert data["announce_for"] == "external"

    def test_statistics_report_a_missing_prompt(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        """
        Without the prompt no notice can play, so external calls stop recording entirely.
        Nothing else on the page would say so.
        """
        ra = MagicMock()
        ra.stats.return_value = {
            "enabled": True,
            "announce_for": "external",
            "announced": 0,
            "failed": 12,
            "audio_file": "missing.wav",
            "audio_present": False,
            "text": "x",
        }
        mock_pbx_core.recording_announcements = ra

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/recording-announcements/statistics")

        assert json.loads(resp.data)["audio_present"] is False

    def test_get_announcement_statistics_not_initialized(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        if hasattr(mock_pbx_core, "recording_announcements"):
            del mock_pbx_core.recording_announcements

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/recording-announcements/statistics")
        assert resp.status_code == 500

    def test_the_notice_log_is_readable(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        """The durable proof that a caller was told; counters are memory-only."""
        ra = MagicMock()
        ra.recent.return_value = [
            {"session_id": "s-1", "played": True, "notice_text": "recorded", "legs": 2}
        ]
        mock_pbx_core.recording_announcements = ra

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/recording-announcements/log")

        assert resp.status_code == 200
        assert json.loads(resp.data)["count"] == 1


@pytest.mark.unit
class TestSkillsBasedRoutingRoutes:
    """Tests for Skills-Based Routing endpoints."""

    def test_get_all_skills_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        sr = MagicMock()
        sr.get_all_skills.return_value = [{"skill_id": "english", "name": "English"}]
        mock_pbx_core.skills_router = sr

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/skills/all")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert len(data["skills"]) == 1

    def test_get_all_skills_not_available(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        if hasattr(mock_pbx_core, "skills_router"):
            del mock_pbx_core.skills_router

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/skills/all")
        assert resp.status_code == 500

    def test_get_agent_skills_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        sr = MagicMock()
        sr.get_agent_skills.return_value = [{"skill_id": "english", "proficiency": 8}]
        mock_pbx_core.skills_router = sr

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/skills/agent/1001")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["agent_extension"] == "1001"

    def test_get_queue_requirements_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        sr = MagicMock()
        sr.get_queue_requirements.return_value = [{"skill_id": "english", "min_proficiency": 5}]
        mock_pbx_core.skills_router = sr

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/skills/queue/100")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["queue_number"] == "100"

    def test_add_skill_success(self, api_client: FlaskClient, mock_pbx_core: MagicMock) -> None:
        sr = MagicMock()
        sr.add_skill.return_value = True
        mock_pbx_core.skills_router = sr

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/skills/skill",
                data=json.dumps(
                    {
                        "skill_id": "french",
                        "name": "French",
                        "description": "French language",
                    }
                ),
                content_type="application/json",
            )
        assert resp.status_code == 200

    def test_add_skill_missing_fields(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        sr = MagicMock()
        mock_pbx_core.skills_router = sr

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/skills/skill",
                data=json.dumps({"skill_id": "french"}),
                content_type="application/json",
            )
        assert resp.status_code == 400

    def test_add_skill_already_exists(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        sr = MagicMock()
        sr.add_skill.return_value = False
        mock_pbx_core.skills_router = sr

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/skills/skill",
                data=json.dumps({"skill_id": "english", "name": "English"}),
                content_type="application/json",
            )
        assert resp.status_code == 409

    def test_assign_skill_success(self, api_client: FlaskClient, mock_pbx_core: MagicMock) -> None:
        sr = MagicMock()
        sr.assign_skill_to_agent.return_value = True
        mock_pbx_core.skills_router = sr

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/skills/assign",
                data=json.dumps(
                    {
                        "agent_extension": "1001",
                        "skill_id": "english",
                        "proficiency": 8,
                    }
                ),
                content_type="application/json",
            )
        assert resp.status_code == 200

    def test_assign_skill_missing_fields(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        sr = MagicMock()
        mock_pbx_core.skills_router = sr

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/skills/assign",
                data=json.dumps({"agent_extension": "1001"}),
                content_type="application/json",
            )
        assert resp.status_code == 400

    def test_set_queue_requirements_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        sr = MagicMock()
        sr.set_queue_requirements.return_value = True
        mock_pbx_core.skills_router = sr

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/skills/queue-requirements",
                data=json.dumps(
                    {
                        "queue_number": "100",
                        "requirements": [{"skill_id": "english"}],
                    }
                ),
                content_type="application/json",
            )
        assert resp.status_code == 200

    def test_set_queue_requirements_missing_queue(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        sr = MagicMock()
        mock_pbx_core.skills_router = sr

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.post(
                "/api/skills/queue-requirements",
                data=json.dumps({"requirements": []}),
                content_type="application/json",
            )
        assert resp.status_code == 400

    def test_remove_skill_from_agent_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        sr = MagicMock()
        sr.remove_skill_from_agent.return_value = True
        mock_pbx_core.skills_router = sr

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.delete("/api/skills/assign/1001/english")
        assert resp.status_code == 200

    def test_remove_skill_from_agent_not_found(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        sr = MagicMock()
        sr.remove_skill_from_agent.return_value = False
        mock_pbx_core.skills_router = sr

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.delete("/api/skills/assign/1001/nonexistent")
        assert resp.status_code == 404


@pytest.mark.unit
class TestPushHistoryRoute:
    """Tests for push notification history endpoint."""

    def test_get_push_history_success(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        from datetime import datetime, timezone

        mp = MagicMock()
        now = datetime.now(tz=UTC)
        mp.notification_history = [
            {
                "user_id": "user1",
                "title": "Incoming Call",
                "body": "From 1001",
                "sent_at": now,
                "success_count": 1,
                "failure_count": 0,
            },
        ]
        mock_pbx_core.mobile_push = mp

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/mobile-push/history")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert len(data["history"]) == 1

    def test_get_push_history_not_initialized(
        self, api_client: FlaskClient, mock_pbx_core: MagicMock
    ) -> None:
        if hasattr(mock_pbx_core, "mobile_push"):
            del mock_pbx_core.mobile_push

        with patch(
            "pbx.api.utils.verify_authentication",
            return_value=(True, {"extension": "1001", "is_admin": True}),
        ):
            resp = api_client.get("/api/mobile-push/history")
        assert resp.status_code == 500
