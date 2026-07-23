"""Coverage tests for call_queue config loading, seeding, and edge cases."""

from unittest.mock import MagicMock, patch

import pytest


class _Config:
    """Minimal config stub exposing .get(key, default)."""

    def __init__(self, queues):
        self._queues = queues

    def get(self, key, default=None):
        if key == "queues":
            return self._queues
        return default


@pytest.mark.unit
@patch("pbx.features.call_queue.get_logger", return_value=MagicMock())
class TestConfigFallbackLoad:
    """load_or_seed with no database loads config-only."""

    def test_loads_queues_and_members(self, _mock_logger):
        from pbx.features.call_queue import QueueStrategy, QueueSystem

        config = _Config(
            [
                {
                    "number": "8001",
                    "name": "Sales",
                    "strategy": "least_recent",
                    "max_wait_time": 120,
                    "agents": ["1001", "1002"],
                },
                {"number": "8002", "name": "Support"},
            ]
        )
        system = QueueSystem(database=None, config=config)
        system.load_or_seed()

        sales = system.get_queue("8001")
        assert sales is not None
        assert sales.strategy == QueueStrategy.LEAST_RECENT
        assert sales.max_wait_time == 120
        assert sales.members == {"1001", "1002"}
        assert system.get_agent("1001") is not None

        support = system.get_queue("8002")
        assert support is not None
        assert support.strategy == QueueStrategy.ROUND_ROBIN

    def test_skips_entries_without_number(self, _mock_logger):
        from pbx.features.call_queue import QueueSystem

        system = QueueSystem(database=None, config=_Config([{"name": "Nameless"}]))
        system.load_or_seed()
        assert system.queues == {}

    def test_invalid_strategy_falls_back(self, _mock_logger):
        from pbx.features.call_queue import QueueStrategy, QueueSystem

        system = QueueSystem(
            database=None,
            config=_Config([{"number": "8001", "name": "X", "strategy": "bogus"}]),
        )
        system.load_or_seed()
        assert system.get_queue("8001").strategy == QueueStrategy.ROUND_ROBIN

    def test_no_config(self, _mock_logger):
        from pbx.features.call_queue import QueueSystem

        system = QueueSystem()
        system.load_or_seed()
        assert system.queues == {}

    def test_non_list_queues_section(self, _mock_logger):
        from pbx.features.call_queue import QueueSystem

        system = QueueSystem(database=None, config=_Config({"number": "8001"}))
        system.load_or_seed()
        assert system.queues == {}


@pytest.mark.unit
@patch("pbx.features.call_queue.get_logger", return_value=MagicMock())
class TestDisabledDatabase:
    """A database object with enabled=False is treated as no database."""

    def test_disabled_db_ignored(self, _mock_logger):
        from pbx.features.call_queue import QueueSystem

        db = MagicMock()
        db.enabled = False
        system = QueueSystem(database=db, config=_Config([{"number": "8001", "name": "S"}]))
        system.load_or_seed()

        assert system.db is None
        assert system.get_queue("8001") is not None
        db.fetch_all.assert_not_called()
        db.execute.assert_not_called()


@pytest.mark.unit
@patch("pbx.features.call_queue.get_logger", return_value=MagicMock())
class TestDbErrorResilience:
    """Database errors are logged, never raised."""

    def _failing_db(self):
        db = MagicMock()
        db.enabled = True
        db.execute.side_effect = RuntimeError("db down")
        db.fetch_all.side_effect = RuntimeError("db down")
        return db

    def test_load_survives_db_error(self, _mock_logger):
        from pbx.features.call_queue import QueueSystem

        system = QueueSystem(database=self._failing_db())
        system.load_or_seed()
        assert system.queues == {}

    def test_mutators_survive_db_error(self, _mock_logger):
        from pbx.features.call_queue import QueueSystem

        system = QueueSystem(database=self._failing_db())
        queue = system.create_queue("8001", "Sales")
        assert queue is not None
        assert system.add_member("8001", "1001") is True
        assert system.set_agent_login("1001", True) == ["8001"]
        assert system.record_miss("1001") is False
        system.record_answered("1001")
        assert system.remove_member("8001", "1001") is True
        assert system.delete_queue("8001") is True
