"""Tests for the Call Queue (ACD) API routes in pbx/api/routes/queues.py."""

import json
from unittest.mock import MagicMock, patch

import pytest
from flask.testing import FlaskClient

AUTH_PATCH = "pbx.api.utils.verify_authentication"
AUTH_RETURN = (True, {"extension": "1001", "is_admin": True})


def _json(response) -> dict:
    return json.loads(response.data)


@pytest.fixture
def queue_system(mock_pbx_core: MagicMock):
    """Attach a real (in-memory, no-DB) QueueSystem to the mock core."""
    from pbx.features.call_queue import QueueSystem

    with patch("pbx.features.call_queue.get_logger", return_value=MagicMock()):
        system = QueueSystem()
    mock_pbx_core.queue_system = system
    return system


@pytest.mark.unit
class TestQueueCrud:
    def test_list_empty(self, api_client: FlaskClient, queue_system) -> None:
        with patch(AUTH_PATCH, return_value=AUTH_RETURN):
            response = api_client.get("/api/queues")
        assert response.status_code == 200
        assert _json(response) == {"queues": []}

    def test_requires_auth(self, api_client: FlaskClient, queue_system) -> None:
        response = api_client.get("/api/queues")
        assert response.status_code == 401

    def test_create_and_list(self, api_client: FlaskClient, queue_system) -> None:
        with patch(AUTH_PATCH, return_value=AUTH_RETURN):
            response = api_client.post(
                "/api/queues",
                json={
                    "queue_number": "8001",
                    "name": "Sales",
                    "strategy": "least_recent",
                    "ring_timeout": 20,
                    "fallback_mailbox": "1005",
                },
            )
            assert response.status_code == 200, _json(response)

            listed = _json(api_client.get("/api/queues"))["queues"]
        assert len(listed) == 1
        assert listed[0]["queue_number"] == "8001"
        assert listed[0]["strategy"] == "least_recent"
        assert listed[0]["ring_timeout"] == 20
        assert listed[0]["fallback_mailbox"] == "1005"

    def test_create_requires_fields(self, api_client: FlaskClient, queue_system) -> None:
        with patch(AUTH_PATCH, return_value=AUTH_RETURN):
            response = api_client.post("/api/queues", json={"name": "NoNumber"})
        assert response.status_code == 400

    def test_create_rejects_bad_number(self, api_client: FlaskClient, queue_system) -> None:
        with patch(AUTH_PATCH, return_value=AUTH_RETURN):
            response = api_client.post("/api/queues", json={"queue_number": "9001", "name": "Bad"})
        assert response.status_code == 400
        assert "pattern" in _json(response)["error"]

    def test_create_rejects_ring_all(self, api_client: FlaskClient, queue_system) -> None:
        with patch(AUTH_PATCH, return_value=AUTH_RETURN):
            response = api_client.post(
                "/api/queues",
                json={"queue_number": "8001", "name": "Sales", "strategy": "ring_all"},
            )
        assert response.status_code == 400
        assert "not yet supported" in _json(response)["error"]

    def test_create_rejects_bad_strategy(self, api_client: FlaskClient, queue_system) -> None:
        with patch(AUTH_PATCH, return_value=AUTH_RETURN):
            response = api_client.post(
                "/api/queues",
                json={"queue_number": "8001", "name": "Sales", "strategy": "bogus"},
            )
        assert response.status_code == 400

    def test_create_rejects_out_of_bounds(self, api_client: FlaskClient, queue_system) -> None:
        with patch(AUTH_PATCH, return_value=AUTH_RETURN):
            response = api_client.post(
                "/api/queues",
                json={"queue_number": "8001", "name": "Sales", "ring_timeout": 999},
            )
        assert response.status_code == 400

    def test_create_duplicate(self, api_client: FlaskClient, queue_system) -> None:
        queue_system.create_queue("8001", "Sales")
        with patch(AUTH_PATCH, return_value=AUTH_RETURN):
            response = api_client.post(
                "/api/queues", json={"queue_number": "8001", "name": "Sales"}
            )
        assert response.status_code == 409

    def test_update(self, api_client: FlaskClient, queue_system) -> None:
        queue_system.create_queue("8001", "Sales")
        with patch(AUTH_PATCH, return_value=AUTH_RETURN):
            response = api_client.put(
                "/api/queues/8001",
                json={"name": "Sales Team", "strategy": "fewest_calls", "enabled": False},
            )
        assert response.status_code == 200
        queue = queue_system.get_queue("8001")
        assert queue.name == "Sales Team"
        assert queue.strategy.value == "fewest_calls"
        assert queue.enabled is False

    def test_update_not_found(self, api_client: FlaskClient, queue_system) -> None:
        with patch(AUTH_PATCH, return_value=AUTH_RETURN):
            response = api_client.put("/api/queues/8009", json={"name": "X"})
        assert response.status_code == 404

    def test_delete(self, api_client: FlaskClient, queue_system) -> None:
        queue_system.create_queue("8001", "Sales")
        with patch(AUTH_PATCH, return_value=AUTH_RETURN):
            response = api_client.delete("/api/queues/8001")
        assert response.status_code == 200
        assert queue_system.get_queue("8001") is None

    def test_delete_not_found(self, api_client: FlaskClient, queue_system) -> None:
        with patch(AUTH_PATCH, return_value=AUTH_RETURN):
            response = api_client.delete("/api/queues/8001")
        assert response.status_code == 404

    def test_delete_rejected_with_waiting_callers(
        self, api_client: FlaskClient, queue_system
    ) -> None:
        queue_system.create_queue("8001", "Sales")
        queue_system.set_runtime_stats("8001", 2, 30.0)
        with patch(AUTH_PATCH, return_value=AUTH_RETURN):
            response = api_client.delete("/api/queues/8001")
        assert response.status_code == 409
        assert queue_system.get_queue("8001") is not None


@pytest.mark.unit
class TestQueueAgents:
    def test_add_and_remove_member(self, api_client: FlaskClient, queue_system) -> None:
        queue_system.create_queue("8001", "Sales")
        with patch(AUTH_PATCH, return_value=AUTH_RETURN):
            response = api_client.post("/api/queues/8001/agents", json={"extension": "1001"})
            assert response.status_code == 200
            assert queue_system.get_queue("8001").members == {"1001"}

            response = api_client.delete("/api/queues/8001/agents/1001")
            assert response.status_code == 200
            assert queue_system.get_queue("8001").members == set()

    def test_add_member_queue_not_found(self, api_client: FlaskClient, queue_system) -> None:
        with patch(AUTH_PATCH, return_value=AUTH_RETURN):
            response = api_client.post("/api/queues/8009/agents", json={"extension": "1001"})
        assert response.status_code == 404

    def test_remove_member_not_found(self, api_client: FlaskClient, queue_system) -> None:
        queue_system.create_queue("8001", "Sales")
        with patch(AUTH_PATCH, return_value=AUTH_RETURN):
            response = api_client.delete("/api/queues/8001/agents/1001")
        assert response.status_code == 404


@pytest.mark.unit
class TestAgentState:
    def _setup(self, queue_system):
        queue_system.create_queue("8001", "Sales")
        queue_system.add_member("8001", "1001")

    def test_login_logout(self, api_client: FlaskClient, queue_system) -> None:
        self._setup(queue_system)
        with patch(AUTH_PATCH, return_value=AUTH_RETURN):
            response = api_client.put("/api/queues/agents/1001/state", json={"logged_in": True})
            assert response.status_code == 200
            assert _json(response)["agent"]["logged_in"] is True
            assert queue_system.get_agent("1001").logged_in is True

            response = api_client.put("/api/queues/agents/1001/state", json={"logged_in": False})
            assert response.status_code == 200
            assert queue_system.get_agent("1001").logged_in is False

    def test_login_non_member(self, api_client: FlaskClient, queue_system) -> None:
        self._setup(queue_system)
        with patch(AUTH_PATCH, return_value=AUTH_RETURN):
            response = api_client.put("/api/queues/agents/2000/state", json={"logged_in": True})
        assert response.status_code == 404

    def test_pause_with_reason(self, api_client: FlaskClient, queue_system) -> None:
        self._setup(queue_system)
        queue_system.set_agent_login("1001", True)
        with patch(AUTH_PATCH, return_value=AUTH_RETURN):
            response = api_client.put(
                "/api/queues/agents/1001/state",
                json={"paused": True, "pause_reason": "ad_calendar"},
            )
        assert response.status_code == 200
        agent = queue_system.get_agent("1001")
        assert agent.paused is True
        assert agent.pause_reason == "ad_calendar"

    def test_pause_invalid_reason(self, api_client: FlaskClient, queue_system) -> None:
        self._setup(queue_system)
        queue_system.set_agent_login("1001", True)
        with patch(AUTH_PATCH, return_value=AUTH_RETURN):
            response = api_client.put(
                "/api/queues/agents/1001/state",
                json={"paused": True, "pause_reason": "vacation"},
            )
        assert response.status_code == 400

    def test_requires_some_field(self, api_client: FlaskClient, queue_system) -> None:
        self._setup(queue_system)
        with patch(AUTH_PATCH, return_value=AUTH_RETURN):
            response = api_client.put("/api/queues/agents/1001/state", json={})
        assert response.status_code == 400

    def test_get_agent_states(self, api_client: FlaskClient, queue_system) -> None:
        self._setup(queue_system)
        queue_system.set_agent_login("1001", True)
        with patch(AUTH_PATCH, return_value=AUTH_RETURN):
            response = api_client.get("/api/queues/agents/state")
        assert response.status_code == 200
        agents = _json(response)["agents"]
        assert len(agents) == 1
        assert agents[0]["extension"] == "1001"
        assert agents[0]["logged_in"] is True
        assert agents[0]["queues"] == ["8001"]


@pytest.mark.unit
class TestQueueStatus:
    def test_status_endpoint(self, api_client: FlaskClient, queue_system) -> None:
        queue_system.create_queue("8001", "Sales")
        queue_system.add_member("8001", "1001")
        queue_system.set_agent_login("1001", True)
        queue_system.set_runtime_stats("8001", 2, 17.5)

        with patch(AUTH_PATCH, return_value=AUTH_RETURN):
            response = api_client.get("/api/queues/status")
        assert response.status_code == 200
        queues = _json(response)["queues"]
        assert queues[0]["calls_waiting"] == 2
        assert queues[0]["longest_wait"] == 17.5
        assert queues[0]["available_agents"] == 1
