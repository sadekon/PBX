"""Unit tests for pbx.features.call_queue — Agent, CallQueue, QueueSystem."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest


def _dialable(_ext: str) -> bool:
    return True


@pytest.mark.unit
@patch("pbx.features.call_queue.get_logger", return_value=MagicMock())
class TestAgent:
    """Tests for the global Agent state object."""

    def test_defaults(self, _mock_logger):
        """New agents start logged out with clean counters."""
        from pbx.features.call_queue import Agent

        agent = Agent("1001", "Alice")
        assert agent.extension == "1001"
        assert agent.name == "Alice"
        assert agent.logged_in is False
        assert agent.paused is False
        assert agent.pause_reason is None
        assert agent.consecutive_misses == 0
        assert agent.calls_taken == 0
        assert agent.last_call_time is None
        assert agent.last_offered_time is None

    def test_is_selectable(self, _mock_logger):
        """Selectable = logged in and not paused."""
        from pbx.features.call_queue import Agent

        agent = Agent("1001")
        assert agent.is_selectable() is False

        agent.logged_in = True
        assert agent.is_selectable() is True

        agent.paused = True
        assert agent.is_selectable() is False


@pytest.mark.unit
@patch("pbx.features.call_queue.get_logger", return_value=MagicMock())
class TestCallQueueSelection:
    """Tests for CallQueue.get_next_agent strategies."""

    def _make_agents(self, *extensions):
        from pbx.features.call_queue import Agent

        agents = {}
        for ext in extensions:
            agent = Agent(ext)
            agent.logged_in = True
            agents[ext] = agent
        return agents

    def _make_queue(self, strategy, members):
        from pbx.features.call_queue import CallQueue

        queue = CallQueue("8001", "Sales", strategy=strategy)
        queue.members.update(members)
        return queue

    def test_no_members(self, _mock_logger):
        from pbx.features.call_queue import QueueStrategy

        queue = self._make_queue(QueueStrategy.ROUND_ROBIN, [])
        assert queue.get_next_agent({}, set(), _dialable) is None

    def test_nobody_logged_in(self, _mock_logger):
        from pbx.features.call_queue import QueueStrategy

        agents = self._make_agents("1001", "1002")
        for agent in agents.values():
            agent.logged_in = False
        queue = self._make_queue(QueueStrategy.ROUND_ROBIN, agents)
        assert queue.get_next_agent(agents, set(), _dialable) is None

    def test_paused_agent_skipped(self, _mock_logger):
        from pbx.features.call_queue import QueueStrategy

        agents = self._make_agents("1001", "1002")
        agents["1001"].paused = True
        queue = self._make_queue(QueueStrategy.ROUND_ROBIN, agents)
        selected = queue.get_next_agent(agents, set(), _dialable)
        assert selected is agents["1002"]

    def test_excluded_agent_skipped(self, _mock_logger):
        from pbx.features.call_queue import QueueStrategy

        agents = self._make_agents("1001", "1002")
        queue = self._make_queue(QueueStrategy.ROUND_ROBIN, agents)
        selected = queue.get_next_agent(agents, {"1001"}, _dialable)
        assert selected is agents["1002"]

    def test_dialable_predicate_applied(self, _mock_logger):
        from pbx.features.call_queue import QueueStrategy

        agents = self._make_agents("1001", "1002")
        queue = self._make_queue(QueueStrategy.ROUND_ROBIN, agents)
        selected = queue.get_next_agent(agents, set(), lambda ext: ext != "1001")
        assert selected is agents["1002"]

    def test_round_robin_cycles(self, _mock_logger):
        from pbx.features.call_queue import QueueStrategy

        agents = self._make_agents("1001", "1002", "1003")
        queue = self._make_queue(QueueStrategy.ROUND_ROBIN, agents)

        picks = {queue.get_next_agent(agents, set(), _dialable).extension for _ in range(3)}
        assert picks == {"1001", "1002", "1003"}

    def test_least_recent(self, _mock_logger):
        from pbx.features.call_queue import QueueStrategy

        agents = self._make_agents("1001", "1002")
        agents["1001"].last_call_time = datetime.now(UTC) - timedelta(hours=2)
        agents["1002"].last_call_time = datetime.now(UTC) - timedelta(minutes=5)
        queue = self._make_queue(QueueStrategy.LEAST_RECENT, agents)
        assert queue.get_next_agent(agents, set(), _dialable) is agents["1001"]

    def test_least_recent_never_called_wins(self, _mock_logger):
        from pbx.features.call_queue import QueueStrategy

        agents = self._make_agents("1001", "1002")
        agents["1001"].last_call_time = None
        agents["1002"].last_call_time = datetime.now(UTC)
        queue = self._make_queue(QueueStrategy.LEAST_RECENT, agents)
        assert queue.get_next_agent(agents, set(), _dialable) is agents["1001"]

    def test_least_recent_rotates_among_never_answered(self, _mock_logger):
        """Regression: before last_offered_time, agents who never answered a
        call all tied at last_call_time=None, so the lowest extension was
        always picked -- e.g. offering three separate calls all landed on
        the same agent even though every agent was logged in and idle."""
        from pbx.features.call_queue import QueueStrategy

        agents = self._make_agents("1001", "1002", "1003")
        queue = self._make_queue(QueueStrategy.LEAST_RECENT, agents)

        picks = [queue.get_next_agent(agents, set(), _dialable).extension for _ in range(3)]
        assert picks == ["1001", "1002", "1003"]

    def test_fewest_calls(self, _mock_logger):
        from pbx.features.call_queue import QueueStrategy

        agents = self._make_agents("1001", "1002", "1003")
        agents["1001"].calls_taken = 5
        agents["1002"].calls_taken = 1
        agents["1003"].calls_taken = 3
        queue = self._make_queue(QueueStrategy.FEWEST_CALLS, agents)
        assert queue.get_next_agent(agents, set(), _dialable) is agents["1002"]

    def test_fewest_calls_rotates_when_tied(self, _mock_logger):
        """Same regression as least_recent: agents tied at calls_taken=0
        (fresh queue, or one who keeps being offered but never answers)
        must rotate by last_offered_time, not always lose to the lowest
        sorted extension."""
        from pbx.features.call_queue import QueueStrategy

        agents = self._make_agents("1001", "1002", "1003")
        queue = self._make_queue(QueueStrategy.FEWEST_CALLS, agents)

        picks = [queue.get_next_agent(agents, set(), _dialable).extension for _ in range(3)]
        assert picks == ["1001", "1002", "1003"]

    def test_random_returns_member(self, _mock_logger):
        from pbx.features.call_queue import QueueStrategy

        agents = self._make_agents("1001", "1002")
        queue = self._make_queue(QueueStrategy.RANDOM, agents)
        selected = queue.get_next_agent(agents, set(), _dialable)
        assert selected.extension in {"1001", "1002"}

    def test_ring_all_falls_back_to_round_robin(self, _mock_logger):
        """RING_ALL is unsupported: guard falls back to single-agent pick."""
        from pbx.features.call_queue import QueueStrategy

        agents = self._make_agents("1001", "1002")
        queue = self._make_queue(QueueStrategy.RING_ALL, agents)
        selected = queue.get_next_agent(agents, set(), _dialable)
        assert selected is not None
        assert not isinstance(selected, list)

    def test_overflow_mailbox_default_and_override(self, _mock_logger):
        from pbx.features.call_queue import CallQueue

        queue = CallQueue("8001", "Sales")
        assert queue.overflow_mailbox() == "8001"

        queue.fallback_mailbox = "1005"
        assert queue.overflow_mailbox() == "1005"


@pytest.mark.unit
@patch("pbx.features.call_queue.get_logger", return_value=MagicMock())
class TestQueueSystem:
    """Tests for QueueSystem (in-memory, no database)."""

    def _system(self):
        from pbx.features.call_queue import QueueSystem

        return QueueSystem()

    def test_create_and_get_queue(self, _mock_logger):
        system = self._system()
        queue = system.create_queue("8001", "Sales")
        assert system.get_queue("8001") is queue
        assert system.get_queue("9999") is None

    def test_delete_queue(self, _mock_logger):
        system = self._system()
        system.create_queue("8001", "Sales")
        assert system.delete_queue("8001") is True
        assert system.get_queue("8001") is None
        assert system.delete_queue("8001") is False

    def test_membership(self, _mock_logger):
        system = self._system()
        system.create_queue("8001", "Sales")
        system.create_queue("8002", "Support")

        assert system.add_member("8001", "1001") is True
        assert system.add_member("8002", "1001") is True
        assert system.add_member("9999", "1001") is False

        assert sorted(system.agent_queues("1001")) == ["8001", "8002"]
        assert system.get_agent("1001") is not None

        assert system.remove_member("8001", "1001") is True
        assert system.agent_queues("1001") == ["8002"]
        assert system.remove_member("8001", "1001") is False

    def test_login_returns_queues_and_clears_pause(self, _mock_logger):
        system = self._system()
        system.create_queue("8001", "Sales")
        system.add_member("8001", "1001")

        agent = system.get_agent("1001")
        agent.paused = True
        agent.pause_reason = "auto_missed"
        agent.consecutive_misses = 3

        queues = system.set_agent_login("1001", True)
        assert queues == ["8001"]
        assert agent.logged_in is True
        assert agent.paused is False
        assert agent.pause_reason is None
        assert agent.consecutive_misses == 0

    def test_login_non_member_returns_empty(self, _mock_logger):
        system = self._system()
        system.create_queue("8001", "Sales")
        assert system.set_agent_login("1099", True) == []
        # No phantom agent state should have been created
        assert system.get_agent("1099") is None

    def test_pause_unpause(self, _mock_logger):
        system = self._system()
        system.create_queue("8001", "Sales")
        system.add_member("8001", "1001")
        system.set_agent_login("1001", True)

        assert system.set_agent_pause("1001", True, "manual") is True
        agent = system.get_agent("1001")
        assert agent.paused is True
        assert agent.pause_reason == "manual"

        agent.consecutive_misses = 2
        assert system.set_agent_pause("1001", False) is True
        assert agent.paused is False
        assert agent.pause_reason is None
        assert agent.consecutive_misses == 0

        assert system.set_agent_pause("2000", True) is False

    def test_record_miss_auto_pause(self, _mock_logger):
        system = self._system()
        queue = system.create_queue("8001", "Sales")
        queue.auto_pause_misses = 2
        system.add_member("8001", "1001")
        system.set_agent_login("1001", True)

        assert system.record_miss("1001") is False
        triggered = system.record_miss("1001")
        assert triggered is True

        agent = system.get_agent("1001")
        assert agent.paused is True
        assert agent.pause_reason == "auto_missed"

    def test_record_miss_threshold_is_max_across_queues(self, _mock_logger):
        system = self._system()
        q1 = system.create_queue("8001", "Sales")
        q2 = system.create_queue("8002", "Support")
        q1.auto_pause_misses = 2
        q2.auto_pause_misses = 4
        system.add_member("8001", "1001")
        system.add_member("8002", "1001")
        system.set_agent_login("1001", True)

        assert system.record_miss("1001") is False
        assert system.record_miss("1001") is False  # 2 misses < max(2, 4)
        assert system.record_miss("1001") is False
        assert system.record_miss("1001") is True  # 4 misses reaches max

    def test_record_miss_disabled(self, _mock_logger):
        system = self._system()
        queue = system.create_queue("8001", "Sales")
        queue.auto_pause_misses = 0
        system.add_member("8001", "1001")
        system.set_agent_login("1001", True)

        for _ in range(10):
            assert system.record_miss("1001") is False
        assert system.get_agent("1001").paused is False

    def test_record_answered(self, _mock_logger):
        system = self._system()
        system.create_queue("8001", "Sales")
        system.add_member("8001", "1001")
        system.set_agent_login("1001", True)
        system.record_miss("1001")

        system.record_answered("1001")
        agent = system.get_agent("1001")
        assert agent.consecutive_misses == 0
        assert agent.calls_taken == 1
        assert agent.last_call_time is not None
        assert agent.last_call_time.tzinfo is not None

    def test_runtime_stats(self, _mock_logger):
        system = self._system()
        system.create_queue("8001", "Sales")
        system.create_queue("8002", "Support")
        system.set_runtime_stats("8001", 3, 42.0)
        system.set_runtime_stats("8002", 1, 5.0)

        assert system.total_waiting() == 4
        status = system.get_queue_status("8001")
        assert status["calls_waiting"] == 3
        assert status["longest_wait"] == 42.0

    def test_get_queue_status(self, _mock_logger):
        system = self._system()
        system.create_queue("8001", "Sales")
        system.add_member("8001", "1001")
        system.add_member("8001", "1002")
        system.set_agent_login("1001", True)

        status = system.get_queue_status("8001")
        assert status["queue_number"] == "8001"
        assert status["name"] == "Sales"
        assert status["strategy"] == "round_robin"
        assert status["fallback_mailbox"] == "8001"
        assert status["members"] == ["1001", "1002"]
        assert status["total_agents"] == 2
        assert status["available_agents"] == 1

        assert system.get_queue_status("9999") is None

    def test_get_all_status_sorted(self, _mock_logger):
        system = self._system()
        system.create_queue("8002", "Support")
        system.create_queue("8001", "Sales")

        result = system.get_all_status()
        assert [s["queue_number"] for s in result] == ["8001", "8002"]

    def test_get_all_status_empty(self, _mock_logger):
        assert self._system().get_all_status() == []


@pytest.mark.unit
@patch("pbx.features.call_queue.get_logger", return_value=MagicMock())
class TestQueueStrategyEnum:
    """Strategy enum values and the supported-strategies constant."""

    def test_strategy_values(self, _mock_logger):
        from pbx.features.call_queue import SUPPORTED_STRATEGIES, QueueStrategy

        assert QueueStrategy.RING_ALL.value == "ring_all"
        assert QueueStrategy.ROUND_ROBIN.value == "round_robin"
        assert QueueStrategy.LEAST_RECENT.value == "least_recent"
        assert QueueStrategy.FEWEST_CALLS.value == "fewest_calls"
        assert QueueStrategy.RANDOM.value == "random"
        assert "ring_all" not in SUPPORTED_STRATEGIES
        assert set(SUPPORTED_STRATEGIES) == {
            "round_robin",
            "least_recent",
            "fewest_calls",
            "random",
        }
