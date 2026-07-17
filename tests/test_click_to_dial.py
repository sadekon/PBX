#!/usr/bin/env python3
"""
Test Click-to-Dial functionality
"""

import sqlite3
from typing import Any

from pbx.features.click_to_dial import ClickToDialEngine


class MockDB:
    """SQLite-backed mock for DatabaseBackend, translating %s -> ? for tests."""

    def __init__(self) -> None:
        self.db_type = "postgresql"
        self.conn = sqlite3.connect(":memory:")
        self.enabled = True
        self._init_tables()

    @staticmethod
    def _convert(sql: str) -> str:
        return sql.replace("%s", "?").replace(
            "SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT"
        )

    def _init_tables(self) -> None:
        """Initialize test tables"""
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS click_to_dial_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                extension TEXT NOT NULL UNIQUE,
                enabled INTEGER DEFAULT 1,
                default_caller_id TEXT,
                auto_answer INTEGER DEFAULT 0,
                browser_notification INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS click_to_dial_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                extension TEXT NOT NULL,
                destination TEXT NOT NULL,
                call_id TEXT NOT NULL,
                source TEXT DEFAULT 'web',
                initiated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                connected_at TIMESTAMP,
                status TEXT DEFAULT 'initiated'
            )
        """)
        self.conn.commit()

    def execute(self, query: str, params: Any = None) -> list[Any]:
        """Execute query with %s -> ? translation."""
        cursor = self.conn.cursor()
        sql = self._convert(query)
        if params:
            cursor.execute(sql, params)
        else:
            cursor.execute(sql)
        self.conn.commit()
        return cursor.fetchall()

    def close(self) -> None:
        """Close connection"""
        self.conn.close()


def test_click_to_dial_init() -> None:
    """Test click-to-dial engine initialization"""

    config = {"click_to_dial.enabled": True}
    db = MockDB()

    # Initialize without PBX core (framework mode)
    engine = ClickToDialEngine(db, config)
    assert engine.enabled is True
    assert engine.pbx_core is None

    # Initialize with mock PBX core
    class MockPBXCore:
        def __init__(self) -> None:
            self.call_manager = None

    mock_pbx = MockPBXCore()
    engine_with_pbx = ClickToDialEngine(db, config, mock_pbx)
    assert engine_with_pbx.pbx_core is mock_pbx

    db.close()


def test_click_to_dial_config() -> None:
    """Test click-to-dial configuration management"""

    config = {"click_to_dial.enabled": True}
    db = MockDB()

    engine = ClickToDialEngine(db, config)

    # Update configuration
    test_config = {
        "enabled": True,
        "default_caller_id": "1001",
        "auto_answer": True,
        "browser_notification": True,
    }

    result = engine.update_config("1001", test_config)
    assert result is True

    # Get configuration
    retrieved_config = engine.get_config("1001")
    assert retrieved_config is not None
    assert retrieved_config["extension"] == "1001"
    assert retrieved_config["enabled"] is True
    assert retrieved_config["auto_answer"] is True

    db.close()


def test_click_to_dial_call_initiation() -> None:
    """Test click-to-dial call initiation (framework mode)"""

    config = {"click_to_dial.enabled": True}
    db = MockDB()

    engine = ClickToDialEngine(db, config)

    # Initiate call (framework mode - no PBX core)
    call_id = engine.initiate_call("1001", "5551234", "web")
    assert call_id is not None
    assert call_id.startswith("c2d-1001-")

    # Get call history
    history = engine.get_call_history("1001")
    assert len(history) == 1
    assert history[0]["destination"] == "5551234"
    assert history[0]["status"] == "initiated"

    # Update call status
    result = engine.update_call_status(call_id, "connected")
    assert result is True

    history = engine.get_call_history("1001")
    assert history[0]["status"] == "connected"

    db.close()


def test_click_to_dial_with_mock_pbx() -> None:
    """Test click-to-dial with mock PBX core"""

    config = {"click_to_dial.enabled": True}
    db = MockDB()

    # Create mock PBX core with a call_originator, mirroring the real
    # PBXCore.call_originator wired up by CallOriginator.
    class MockCall:
        def __init__(self, call_id: str, from_ext: str, to_ext: str) -> None:
            self.call_id = call_id
            self.from_extension = from_ext
            self.to_extension = to_ext

    class MockCallOriginator:
        def __init__(self) -> None:
            self.bridge_calls: list[tuple[str, str]] = []

        def originate_and_bridge(
            self, leg_a: str, leg_b: str, **kwargs: Any
        ) -> tuple[MockCall, None]:
            self.bridge_calls.append((leg_a, leg_b))
            return MockCall("sip-call-1", leg_a, leg_b), None

    class MockPBXCore:
        def __init__(self) -> None:
            self.call_originator = MockCallOriginator()

    mock_pbx = MockPBXCore()
    engine = ClickToDialEngine(db, config, mock_pbx)

    # Initiate call with PBX integration
    call_id = engine.initiate_call("1001", "5551234", "web")
    assert call_id is not None
    assert mock_pbx.call_originator.bridge_calls == [("1001", "5551234")]

    # Verify call history shows ringing status
    history = engine.get_call_history("1001")
    assert len(history) == 1
    assert history[0]["status"] == "ringing"

    db.close()


def test_click_to_dial_all_configs() -> None:
    """Test getting all click-to-dial configurations"""

    config = {"click_to_dial.enabled": True}
    db = MockDB()

    engine = ClickToDialEngine(db, config)

    # Add multiple configurations
    for ext in ["1001", "1002", "1003"]:
        engine.update_config(ext, {"enabled": True, "auto_answer": False})

    # Get all configs
    all_configs = engine.get_all_configs()
    assert len(all_configs) == 3
    assert all(c["extension"] in ["1001", "1002", "1003"] for c in all_configs)

    db.close()
