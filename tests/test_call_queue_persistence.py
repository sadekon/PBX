"""
Tests for Call Queue Database Persistence

Ensures queue definitions, membership, and agent runtime state persist
across restarts (a second QueueSystem instance against the same database).
"""

import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SCHEMA = """
CREATE TABLE IF NOT EXISTS call_queues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_number VARCHAR(20) UNIQUE NOT NULL,
    name VARCHAR(255) NOT NULL,
    strategy VARCHAR(20) NOT NULL DEFAULT 'round_robin',
    ring_timeout INTEGER DEFAULT 15,
    max_wait_time INTEGER DEFAULT 300,
    max_queue_size INTEGER DEFAULT 10,
    fallback_mailbox VARCHAR(20),
    auto_pause_misses INTEGER DEFAULT 3,
    enabled BOOLEAN DEFAULT 1,
    announcement_enabled BOOLEAN DEFAULT 0,
    announcement_interval INTEGER DEFAULT 30,
    announcement_text VARCHAR(500),
    announcement_file VARCHAR(255),
    announcement_position BOOLEAN DEFAULT 0,
    overflow_action VARCHAR(20) DEFAULT 'voicemail',
    max_redials INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS queue_agents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_number VARCHAR(20) NOT NULL,
    extension VARCHAR(20) NOT NULL,
    penalty INTEGER DEFAULT 0,
    source VARCHAR(20) DEFAULT 'manual',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (queue_number, extension)
);
CREATE TABLE IF NOT EXISTS queue_agent_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    extension VARCHAR(20) UNIQUE NOT NULL,
    logged_in BOOLEAN DEFAULT 0,
    paused BOOLEAN DEFAULT 0,
    pause_reason VARCHAR(30),
    consecutive_misses INTEGER DEFAULT 0,
    calls_taken INTEGER DEFAULT 0,
    last_call_time TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class _MockDB:
    """SQLite-backed mock for DatabaseBackend, translating %s -> ? for tests."""

    def __init__(self, db_path: str) -> None:
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.enabled = True
        self.conn.executescript(_SCHEMA)

    @staticmethod
    def _convert(sql: str) -> str:
        return sql.replace("%s", "?")

    @staticmethod
    def _adapt(params: tuple) -> tuple:
        # Python 3.12+ deprecated the default datetime adapter; store ISO text.
        return tuple(p.isoformat() if isinstance(p, datetime) else p for p in params or ())

    def execute(self, sql: str, params: tuple = ()) -> bool:
        cursor = self.conn.execute(self._convert(sql), self._adapt(params))
        self.conn.commit()
        return cursor.rowcount > 0 or cursor.description is not None

    def fetch_one(self, sql: str, params: tuple = ()) -> dict | None:
        cursor = self.conn.execute(self._convert(sql), self._adapt(params))
        row = cursor.fetchone()
        return dict(row) if row else None

    def fetch_all(self, sql: str, params: tuple = ()) -> list[dict]:
        cursor = self.conn.execute(self._convert(sql), self._adapt(params))
        return [dict(row) for row in cursor.fetchall()]


class _Config:
    def __init__(self, queues):
        self._queues = queues

    def get(self, key, default=None):
        if key == "queues":
            return self._queues
        return default


SEED_CONFIG = _Config(
    [
        {
            "number": "8001",
            "name": "Sales Queue",
            "strategy": "round_robin",
            "max_wait_time": 300,
            "agents": ["1001", "1002"],
        },
        {
            "number": "8002",
            "name": "Support Queue",
            "strategy": "least_recent",
            "max_wait_time": 180,
        },
    ]
)


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "queues.db")


def _system(db_path: str, config=None):
    from pbx.features.call_queue import QueueSystem

    system = QueueSystem(database=_MockDB(db_path), config=config)
    system.load_or_seed()
    return system


@pytest.mark.unit
@patch("pbx.features.call_queue.get_logger", return_value=MagicMock())
class TestSeedAndReload:
    def test_seed_from_config_once(self, _mock_logger, db_path):
        from pbx.features.call_queue import QueueStrategy

        system = _system(db_path, SEED_CONFIG)
        assert set(system.queues) == {"8001", "8002"}
        assert system.get_queue("8001").members == {"1001", "1002"}
        assert system.get_queue("8002").strategy == QueueStrategy.LEAST_RECENT

        # Second instance loads from DB, not config: mutate config to prove it
        system2 = _system(db_path, _Config([{"number": "8009", "name": "New"}]))
        assert set(system2.queues) == {"8001", "8002"}
        assert system2.get_queue("8009") is None
        assert system2.get_queue("8001").members == {"1001", "1002"}

    def test_queue_crud_persists(self, _mock_logger, db_path):
        from pbx.features.call_queue import QueueStrategy

        system = _system(db_path)
        queue = system.create_queue("8003", "Billing", QueueStrategy.FEWEST_CALLS)
        queue.ring_timeout = 20
        queue.fallback_mailbox = "1005"
        queue.auto_pause_misses = 5
        queue.enabled = False
        system.save_queue(queue)

        reloaded = _system(db_path).get_queue("8003")
        assert reloaded is not None
        assert reloaded.name == "Billing"
        assert reloaded.strategy == QueueStrategy.FEWEST_CALLS
        assert reloaded.ring_timeout == 20
        assert reloaded.fallback_mailbox == "1005"
        assert reloaded.overflow_mailbox() == "1005"
        assert reloaded.auto_pause_misses == 5
        assert reloaded.enabled is False

    def test_announcement_fields_persist(self, _mock_logger, db_path):
        system = _system(db_path)
        queue = system.create_queue("8004", "Support")
        queue.announcement_enabled = True
        queue.announcement_interval = 45
        queue.announcement_text = "Please continue to hold"
        queue.announcement_file = "support.wav"
        queue.announcement_position = True
        system.save_queue(queue)

        reloaded = _system(db_path).get_queue("8004")
        assert reloaded is not None
        assert reloaded.announcement_enabled is True
        assert reloaded.announcement_interval == 45
        assert reloaded.announcement_text == "Please continue to hold"
        assert reloaded.announcement_file == "support.wav"
        assert reloaded.announcement_position is True

    def test_announcement_fields_default_off(self, _mock_logger, db_path):
        system = _system(db_path)
        queue = system.create_queue("8005", "Billing")
        system.save_queue(queue)

        reloaded = _system(db_path).get_queue("8005")
        assert reloaded is not None
        assert reloaded.announcement_enabled is False
        assert reloaded.announcement_interval == 30
        assert reloaded.announcement_text is None
        assert reloaded.announcement_file is None
        assert reloaded.announcement_position is False
        assert reloaded.overflow_action == "voicemail"

    def test_max_redials_persists(self, _mock_logger, db_path):
        system = _system(db_path)
        queue = system.create_queue("8007", "Redial Test")
        queue.max_redials = 3
        system.save_queue(queue)

        reloaded = _system(db_path).get_queue("8007")
        assert reloaded is not None
        assert reloaded.max_redials == 3

    def test_max_redials_defaults_to_zero(self, _mock_logger, db_path):
        system = _system(db_path)
        system.save_queue(system.create_queue("8008", "Default Redials"))

        assert _system(db_path).get_queue("8008").max_redials == 0

    def test_overflow_action_persists(self, _mock_logger, db_path):
        system = _system(db_path)
        queue = system.create_queue("8006", "Overflow Test")
        queue.overflow_action = "drop"
        system.save_queue(queue)

        reloaded = _system(db_path).get_queue("8006")
        assert reloaded is not None
        assert reloaded.overflow_action == "drop"

    def test_delete_queue_removes_rows(self, _mock_logger, db_path):
        system = _system(db_path, SEED_CONFIG)
        assert system.delete_queue("8001") is True

        system2 = _system(db_path)
        assert system2.get_queue("8001") is None
        assert system2.get_queue("8002") is not None

    def test_membership_persists(self, _mock_logger, db_path):
        system = _system(db_path, SEED_CONFIG)
        system.add_member("8002", "1003")
        system.remove_member("8001", "1002")

        system2 = _system(db_path)
        assert system2.get_queue("8002").members == {"1003"}
        assert system2.get_queue("8001").members == {"1001"}


@pytest.mark.unit
@patch("pbx.features.call_queue.get_logger", return_value=MagicMock())
class TestAgentStatePersistence:
    def test_login_state_survives_restart(self, _mock_logger, db_path):
        system = _system(db_path, SEED_CONFIG)
        assert system.set_agent_login("1001", True) == ["8001"]

        system2 = _system(db_path)
        agent = system2.get_agent("1001")
        assert agent is not None
        assert agent.logged_in is True
        assert agent.paused is False

    def test_pause_state_survives_restart(self, _mock_logger, db_path):
        system = _system(db_path, SEED_CONFIG)
        system.set_agent_login("1001", True)
        system.set_agent_pause("1001", True, "manual")

        agent = _system(db_path).get_agent("1001")
        assert agent.paused is True
        assert agent.pause_reason == "manual"

    def test_miss_counter_and_auto_pause_survive_restart(self, _mock_logger, db_path):
        system = _system(db_path, SEED_CONFIG)
        system.get_queue("8001").auto_pause_misses = 2
        system.save_queue(system.get_queue("8001"))
        system.set_agent_login("1001", True)

        assert system.record_miss("1001") is False
        assert system.record_miss("1001") is True  # auto-pause

        agent = _system(db_path).get_agent("1001")
        assert agent.consecutive_misses == 2
        assert agent.paused is True
        assert agent.pause_reason == "auto_missed"

    def test_answered_stats_survive_restart(self, _mock_logger, db_path):
        system = _system(db_path, SEED_CONFIG)
        system.set_agent_login("1001", True)
        system.record_answered("1001")
        system.record_answered("1001")

        agent = _system(db_path).get_agent("1001")
        assert agent.calls_taken == 2
        assert agent.last_call_time is not None
        assert agent.last_call_time.tzinfo is not None
        assert agent.consecutive_misses == 0
