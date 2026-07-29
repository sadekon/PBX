"""
Call Queue and ACD (Automatic Call Distribution) system

Configuration, membership, agent-state, and strategy engine for call queues.
Queue definitions, membership, and agent runtime state (login/pause/miss
counters) are persisted to the database (migration 1013) so they survive
restarts; config.yml's ``queues:`` section is used only to seed an empty
database on first run.

This module never touches Call objects or SIP/RTP — live queued calls are
orchestrated by pbx.core.queue_handler.QueueCallHandler, which consults this
engine for agent selection and pushes back runtime stats (waiting counts) for
status reporting. Agent dialability beyond login/pause state (registration,
busy) is supplied by the handler as an injected predicate.
"""

import random
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pbx.utils.logger import get_logger

# Strategies the engine can actually serve today (one agent leg at a time).
# RING_ALL remains in the enum for forward compatibility but is rejected by
# config/API validation until multi-leg origination is implemented.
SUPPORTED_STRATEGIES = ("round_robin", "least_recent", "fewest_calls", "random")


class QueueStrategy(Enum):
    """Call distribution strategies"""

    RING_ALL = "ring_all"  # Ring all agents simultaneously (not yet supported)
    ROUND_ROBIN = "round_robin"  # Distribute evenly
    LEAST_RECENT = "least_recent"  # Agent who hasn't taken call longest
    FEWEST_CALLS = "fewest_calls"  # Agent with fewest calls
    RANDOM = "random"  # Random distribution


class Agent:
    """
    Global runtime state for a queue agent (one per extension, shared across
    all queues the extension is a member of — matching the global *61/*62
    login semantics). Persisted in queue_agent_state.
    """

    def __init__(self, extension: str, name: str = "") -> None:
        """
        Initialize agent

        Args:
            extension: Agent's extension number
            name: Agent's display name
        """
        self.extension = extension
        self.name = name
        self.logged_in = False
        self.paused = False
        # 'manual' | 'auto_missed' | (future) 'ad_calendar'
        self.pause_reason: str | None = None
        self.consecutive_misses = 0
        self.calls_taken = 0
        self.last_call_time: datetime | None = None
        # Least-recent tiebreak only (see CallQueue.get_next_agent): when
        # last_call_time is None (never yet answered a call), agents would
        # otherwise all tie and always lose to sorted-extension order.
        # Not persisted -- ephemeral fairness state, same as round_robin_last.
        self.last_offered_time: datetime | None = None

    def is_selectable(self) -> bool:
        """Engine-level availability: logged in and not paused"""
        return self.logged_in and not self.paused


class CallQueue:
    """A call queue definition plus its member set"""

    def __init__(
        self,
        queue_number: str,
        name: str,
        strategy: QueueStrategy = QueueStrategy.ROUND_ROBIN,
        ring_timeout: int = 15,
        max_wait_time: int = 300,
        max_queue_size: int = 10,
        fallback_mailbox: str | None = None,
        auto_pause_misses: int = 3,
        enabled: bool = True,
        announcement_enabled: bool = False,
        announcement_interval: int = 30,
        announcement_text: str | None = None,
        announcement_file: str | None = None,
        announcement_position: bool = False,
        overflow_action: str = "voicemail",
        max_redials: int = 0,
    ) -> None:
        """
        Initialize call queue

        Args:
            queue_number: Queue identifier (dialable number, e.g. '8001')
            name: Queue display name
            strategy: Distribution strategy
            ring_timeout: Seconds to ring one agent before trying the next
            max_wait_time: Maximum caller wait before voicemail overflow
            max_queue_size: Maximum waiting callers before overflow at entry
            fallback_mailbox: Overflow mailbox (None => the queue number)
            auto_pause_misses: Consecutive misses before auto-pause (0 = off)
            enabled: Whether the queue accepts callers
            announcement_enabled: Whether to periodically interrupt MOH with
                a hold announcement
            announcement_interval: Seconds between hold announcements
            announcement_text: Custom message text (TTS); None => default
            announcement_file: Pre-recorded WAV filename under
                ``<moh_directory>/announcements/``; takes priority over TTS
            announcement_position: Whether to append "you are caller number
                N" to the TTS announcement (ignored for announcement_file)
            overflow_action: What happens to a caller on overflow (queue
                full, no agents, or max wait reached) -- "voicemail"
                (default, records into fallback_mailbox) or "drop" (hang up)
            max_redials: Extra times one caller may be offered to the same
                agent after the first attempt (0 = offer each agent once).
                Once every selectable agent is spent the caller overflows
                rather than holding for max_wait_time.
        """
        self.queue_number = queue_number
        self.name = name
        self.strategy = strategy
        self.ring_timeout = ring_timeout
        self.max_wait_time = max_wait_time
        self.max_queue_size = max_queue_size
        self.fallback_mailbox = fallback_mailbox
        self.auto_pause_misses = auto_pause_misses
        self.enabled = enabled
        self.announcement_enabled = announcement_enabled
        self.announcement_interval = announcement_interval
        self.announcement_text = announcement_text
        self.announcement_file = announcement_file
        self.announcement_position = announcement_position
        self.overflow_action = (
            overflow_action if overflow_action in ("voicemail", "drop") else "voicemail"
        )
        self.max_redials = max(0, max_redials)
        # Member extensions; Agent objects are owned globally by QueueSystem.
        self.members: set[str] = set()
        # Extension last offered a call under round-robin; the rotation
        # continues from the next member after it (see get_next_agent).
        self.round_robin_last: str | None = None
        self.logger = get_logger()

    def overflow_mailbox(self) -> str:
        """Mailbox that receives overflow voicemail for this queue"""
        return self.fallback_mailbox or self.queue_number

    def get_next_agent(
        self,
        agents: dict[str, Agent],
        exclude: set[str],
        is_dialable: Callable[[str], bool],
    ) -> Agent | None:
        """
        Select the next agent to offer a call to, per strategy.

        Args:
            agents: Global agent-state map (QueueSystem.agents)
            exclude: Extensions to skip (already tried this cycle, or
                currently being offered another call)
            is_dialable: Handler-supplied predicate covering registration
                and busy state

        Returns:
            Selected Agent, or None if nobody is currently offerable
        """
        candidates = [
            agents[ext]
            for ext in sorted(self.members)
            if ext in agents
            and ext not in exclude
            and agents[ext].is_selectable()
            and is_dialable(ext)
        ]
        if not candidates:
            return None

        if self.strategy == QueueStrategy.RING_ALL:
            # Unreachable via validated config/API; guard anyway.
            self.logger.warning(
                f"Queue {self.queue_number}: ring_all strategy not supported, "
                "falling back to round_robin"
            )
            # Fall through to round-robin behavior below.

        if self.strategy == QueueStrategy.RANDOM:
            return random.choice(candidates)

        if self.strategy == QueueStrategy.LEAST_RECENT:
            # Primary key: recency of last *answered* call. Secondary key:
            # recency of last *offer*, so agents who have never yet answered
            # a call (all tied at "never" on the primary key) still rotate
            # fairly instead of always losing the tie to the lowest sorted
            # extension -- see Agent.last_offered_time.
            epoch = datetime.min.replace(tzinfo=UTC)
            chosen = min(
                candidates,
                key=lambda a: (a.last_call_time or epoch, a.last_offered_time or epoch),
            )
            chosen.last_offered_time = datetime.now(tz=UTC)
            return chosen

        if self.strategy == QueueStrategy.FEWEST_CALLS:
            # Same tiebreak issue as LEAST_RECENT: calls_taken only advances
            # on answer, so agents tied at 0 (fresh queue, or one who keeps
            # being offered but never answers) need last_offered_time to
            # rotate fairly instead of always losing to sorted-extension
            # order.
            epoch = datetime.min.replace(tzinfo=UTC)
            chosen = min(
                candidates,
                key=lambda a: (a.calls_taken, a.last_offered_time or epoch),
            )
            chosen.last_offered_time = datetime.now(tz=UTC)
            return chosen

        # ROUND_ROBIN (and RING_ALL fallback): continue the rotation from the
        # member after the one last offered. Tracking the extension (not a
        # positional index into this filtered list) keeps the order stable as
        # the eligible set changes size between offers -- so a missed offer or
        # a busy agent no longer skips the next agent or double-serves one.
        # candidates is sorted by extension, so "next in rotation" is the first
        # candidate greater than the last, wrapping to the lowest.
        last = self.round_robin_last
        agent = next((a for a in candidates if a.extension > last), None) if last else None
        if agent is None:
            agent = candidates[0]
        self.round_robin_last = agent.extension
        return agent


class QueueSystem:
    """
    Manages all call queues: definitions, membership, and global agent state,
    all persisted to the database. Thread-safe: every mutator takes the
    internal lock, updates memory, then persists.
    """

    def __init__(self, database: Any | None = None, config: Any | None = None) -> None:
        """
        Initialize queue system

        Args:
            database: DatabaseBackend (pbx_core.database) or None
            config: Config object, used only to seed an empty database
        """
        self.logger = get_logger()
        self.config = config
        self.db = database if database is not None and getattr(database, "enabled", False) else None
        self._lock = threading.RLock()
        self.queues: dict[str, CallQueue] = {}
        # Global agent state keyed by extension (shared across queues).
        self.agents: dict[str, Agent] = {}
        # Live stats pushed by QueueCallHandler: queue_number -> (waiting, longest_wait_secs)
        self._runtime_stats: dict[str, tuple[int, float]] = {}

    # ------------------------------------------------------------------
    # Load / seed
    # ------------------------------------------------------------------

    def load_or_seed(self) -> None:
        """
        Load queues, membership, and agent state from the database. If no
        queues exist yet and config.yml has a ``queues:`` section, seed the
        database from it (one-time; the database is authoritative thereafter).
        """
        with self._lock:
            if self.db is None:
                self._load_from_config()
                self.logger.warning(
                    "Queue system: database unavailable, loaded config-only "
                    "(agent state will not persist)"
                )
                return

            self._load_from_db()
            if not self.queues:
                seeded = self._seed_from_config()
                if seeded:
                    self.logger.info(f"Queue system: seeded {seeded} queue(s) from config")
            self.logger.info(
                f"Queue system loaded: {len(self.queues)} queue(s), "
                f"{len(self.agents)} agent(s) with state"
            )

    def _load_from_db(self) -> None:
        """Populate memory from queue tables"""
        try:
            rows = self.db.fetch_all("SELECT * FROM call_queues") or []
            for row in rows:
                queue = self._queue_from_row(row)
                self.queues[queue.queue_number] = queue

            member_rows = self.db.fetch_all("SELECT queue_number, extension FROM queue_agents")
            for row in member_rows or []:
                queue = self.queues.get(row["queue_number"])
                if queue:
                    queue.members.add(row["extension"])
                    self._ensure_agent(row["extension"], persist=False)

            state_rows = self.db.fetch_all("SELECT * FROM queue_agent_state")
            for row in state_rows or []:
                agent = self._ensure_agent(row["extension"], persist=False)
                agent.logged_in = bool(row.get("logged_in"))
                agent.paused = bool(row.get("paused"))
                agent.pause_reason = row.get("pause_reason")
                agent.consecutive_misses = row.get("consecutive_misses") or 0
                agent.calls_taken = row.get("calls_taken") or 0
                last_call = row.get("last_call_time")
                if isinstance(last_call, str):
                    # SQLite-backed test shims return ISO strings
                    try:
                        last_call = datetime.fromisoformat(last_call)
                    except ValueError:
                        last_call = None
                if last_call is not None and last_call.tzinfo is None:
                    last_call = last_call.replace(tzinfo=UTC)
                agent.last_call_time = last_call
        except Exception as e:
            self.logger.error(f"Queue system: failed to load from database: {e}")

    def _queue_from_row(self, row: dict) -> CallQueue:
        """Build a CallQueue from a call_queues row"""
        try:
            strategy = QueueStrategy(row.get("strategy") or "round_robin")
        except ValueError:
            strategy = QueueStrategy.ROUND_ROBIN
        return CallQueue(
            queue_number=row["queue_number"],
            name=row.get("name") or row["queue_number"],
            strategy=strategy,
            ring_timeout=row.get("ring_timeout") or 15,
            max_wait_time=row.get("max_wait_time") or 300,
            max_queue_size=row.get("max_queue_size") or 10,
            fallback_mailbox=row.get("fallback_mailbox"),
            auto_pause_misses=(
                row["auto_pause_misses"] if row.get("auto_pause_misses") is not None else 3
            ),
            enabled=bool(row.get("enabled", True)),
            announcement_enabled=bool(row.get("announcement_enabled", False)),
            announcement_interval=row.get("announcement_interval") or 30,
            announcement_text=row.get("announcement_text"),
            announcement_file=row.get("announcement_file"),
            announcement_position=bool(row.get("announcement_position", False)),
            overflow_action=row.get("overflow_action") or "voicemail",
            max_redials=row.get("max_redials") or 0,
        )

    def _config_queue_entries(self) -> list[dict]:
        """Read the queues: section from config (seed source only)"""
        if not self.config:
            return []
        entries = self.config.get("queues", [])
        return entries if isinstance(entries, list) else []

    def _seed_from_config(self) -> int:
        """Seed the database from config.yml's queues: section. Returns count."""
        seeded = 0
        for entry in self._config_queue_entries():
            number = str(entry.get("number", "")).strip()
            if not number:
                continue
            try:
                strategy = QueueStrategy(entry.get("strategy", "round_robin"))
            except ValueError:
                strategy = QueueStrategy.ROUND_ROBIN
            if strategy == QueueStrategy.RING_ALL:
                self.logger.warning(
                    f"Queue {number}: ring_all not supported, seeding as round_robin"
                )
                strategy = QueueStrategy.ROUND_ROBIN
            queue = CallQueue(
                queue_number=number,
                name=entry.get("name", number),
                strategy=strategy,
                ring_timeout=int(entry.get("ring_timeout", 15)),
                max_wait_time=int(entry.get("max_wait_time", 300)),
                max_queue_size=int(entry.get("max_queue_size", 10)),
                fallback_mailbox=entry.get("fallback_mailbox"),
                auto_pause_misses=int(entry.get("auto_pause_misses", 3)),
                announcement_enabled=bool(entry.get("announcement_enabled", False)),
                announcement_interval=int(entry.get("announcement_interval", 30)),
                announcement_text=entry.get("announcement_text"),
                announcement_file=entry.get("announcement_file"),
                announcement_position=bool(entry.get("announcement_position", False)),
                overflow_action=entry.get("overflow_action") or "voicemail",
                max_redials=int(entry.get("max_redials", 0)),
            )
            for ext in entry.get("agents", []) or []:
                queue.members.add(str(ext))
            self.queues[number] = queue
            self._persist_queue(queue)
            for ext in queue.members:
                self._persist_member(number, ext)
                self._ensure_agent(ext)
            seeded += 1
        return seeded

    def _load_from_config(self) -> None:
        """Config-only fallback when the database is unavailable"""
        for entry in self._config_queue_entries():
            number = str(entry.get("number", "")).strip()
            if not number:
                continue
            try:
                strategy = QueueStrategy(entry.get("strategy", "round_robin"))
            except ValueError:
                strategy = QueueStrategy.ROUND_ROBIN
            queue = CallQueue(
                queue_number=number,
                name=entry.get("name", number),
                strategy=strategy,
                max_wait_time=int(entry.get("max_wait_time", 300)),
            )
            for ext in entry.get("agents", []) or []:
                queue.members.add(str(ext))
                self._ensure_agent(str(ext), persist=False)
            self.queues[number] = queue

    # ------------------------------------------------------------------
    # Queue CRUD
    # ------------------------------------------------------------------

    def get_queue(self, queue_number: str) -> CallQueue | None:
        """Get queue by number"""
        return self.queues.get(queue_number)

    def create_queue(
        self, queue_number: str, name: str, strategy: QueueStrategy = QueueStrategy.ROUND_ROBIN
    ) -> CallQueue:
        """
        Create and persist a new queue

        Args:
            queue_number: Queue identifier
            name: Queue name
            strategy: Distribution strategy

        Returns:
            CallQueue object
        """
        with self._lock:
            queue = CallQueue(queue_number, name, strategy)
            self.queues[queue_number] = queue
            self._persist_queue(queue)
            self.logger.info(f"Created queue {queue_number}: {name}")
            return queue

    def save_queue(self, queue: CallQueue) -> None:
        """Persist a queue's current definition (upsert)"""
        with self._lock:
            self.queues[queue.queue_number] = queue
            self._persist_queue(queue)

    def delete_queue(self, queue_number: str) -> bool:
        """Delete a queue, its membership rows, and runtime stats"""
        with self._lock:
            if queue_number not in self.queues:
                return False
            del self.queues[queue_number]
            self._runtime_stats.pop(queue_number, None)
            if self.db is not None:
                try:
                    self.db.execute(
                        "DELETE FROM queue_agents WHERE queue_number = %s", (queue_number,)
                    )
                    self.db.execute(
                        "DELETE FROM call_queues WHERE queue_number = %s", (queue_number,)
                    )
                except Exception as e:
                    self.logger.error(f"Failed to delete queue {queue_number}: {e}")
            self.logger.info(f"Deleted queue {queue_number}")
            return True

    # ------------------------------------------------------------------
    # Membership
    # ------------------------------------------------------------------

    def add_member(self, queue_number: str, extension: str) -> bool:
        """Add an agent extension to a queue (persisted)"""
        with self._lock:
            queue = self.queues.get(queue_number)
            if not queue:
                return False
            queue.members.add(extension)
            self._ensure_agent(extension)
            self._persist_member(queue_number, extension)
            self.logger.info(f"Added agent {extension} to queue {queue_number}")
            return True

    def remove_member(self, queue_number: str, extension: str) -> bool:
        """Remove an agent extension from a queue (persisted)"""
        with self._lock:
            queue = self.queues.get(queue_number)
            if not queue or extension not in queue.members:
                return False
            queue.members.discard(extension)
            if self.db is not None:
                try:
                    self.db.execute(
                        "DELETE FROM queue_agents WHERE queue_number = %s AND extension = %s",
                        (queue_number, extension),
                    )
                except Exception as e:
                    self.logger.error(
                        f"Failed to remove member {extension} from {queue_number}: {e}"
                    )
            self.logger.info(f"Removed agent {extension} from queue {queue_number}")
            return True

    def agent_queues(self, extension: str) -> list[str]:
        """Queue numbers the extension is a member of"""
        with self._lock:
            return [q.queue_number for q in self.queues.values() if extension in q.members]

    # ------------------------------------------------------------------
    # Agent state
    # ------------------------------------------------------------------

    def get_agent(self, extension: str) -> Agent | None:
        """Get global agent state for an extension"""
        return self.agents.get(extension)

    def set_agent_login(self, extension: str, logged_in: bool) -> list[str]:
        """
        Log an agent in or out of all their queues.

        Login clears any pause (including auto-pause); logout resets the
        consecutive-miss counter.

        Returns:
            Queue numbers the agent is a member of (empty list = not a
            member of any queue; callers should treat that as an error).
        """
        with self._lock:
            queues = self.agent_queues(extension)
            if not queues:
                return []
            agent = self._ensure_agent(extension)
            agent.logged_in = logged_in
            agent.paused = False
            agent.pause_reason = None
            agent.consecutive_misses = 0
            self._persist_agent_state(agent)
            self.logger.info(
                f"Agent {extension} logged {'in' if logged_in else 'out'} "
                f"(queues: {', '.join(queues)})"
            )
            return queues

    def set_agent_pause(self, extension: str, paused: bool, reason: str | None = None) -> bool:
        """
        Pause/unpause an agent (manual, auto_missed, or future ad_calendar).
        Unpausing resets the consecutive-miss counter.
        """
        with self._lock:
            agent = self.agents.get(extension)
            if agent is None:
                return False
            agent.paused = paused
            agent.pause_reason = reason if paused else None
            if not paused:
                agent.consecutive_misses = 0
            self._persist_agent_state(agent)
            self.logger.info(
                f"Agent {extension} {'paused' if paused else 'unpaused'}"
                + (f" ({reason})" if paused and reason else "")
            )
            return True

    def record_miss(self, extension: str) -> bool:
        """
        Record a missed (unanswered/declined) offer for an agent.

        Auto-pauses the agent once their consecutive misses reach the highest
        auto_pause_misses threshold among their queues (0 = disabled).

        Returns:
            True if this miss triggered an auto-pause.
        """
        with self._lock:
            agent = self.agents.get(extension)
            if agent is None:
                return False
            agent.consecutive_misses += 1

            thresholds = [
                q.auto_pause_misses
                for q in self.queues.values()
                if extension in q.members and q.auto_pause_misses > 0
            ]
            threshold = max(thresholds) if thresholds else 0
            triggered = bool(threshold) and agent.consecutive_misses >= threshold
            if triggered:
                agent.paused = True
                agent.pause_reason = "auto_missed"
                self.logger.warning(
                    f"Agent {extension} auto-paused after "
                    f"{agent.consecutive_misses} consecutive missed calls"
                )
            self._persist_agent_state(agent)
            return triggered

    def record_answered(self, extension: str) -> None:
        """Record an answered queue call: resets misses, bumps stats"""
        with self._lock:
            agent = self._ensure_agent(extension)
            agent.consecutive_misses = 0
            agent.calls_taken += 1
            agent.last_call_time = datetime.now(tz=UTC)
            self._persist_agent_state(agent)

    def _ensure_agent(self, extension: str, persist: bool = True) -> Agent:
        """Get or create the global Agent record for an extension"""
        agent = self.agents.get(extension)
        if agent is None:
            agent = Agent(extension)
            self.agents[extension] = agent
            if persist:
                self._persist_agent_state(agent)
        return agent

    # ------------------------------------------------------------------
    # Runtime stats (pushed by QueueCallHandler)
    # ------------------------------------------------------------------

    def set_runtime_stats(self, queue_number: str, calls_waiting: int, longest_wait: float) -> None:
        """Record live waiting-caller stats for a queue"""
        with self._lock:
            self._runtime_stats[queue_number] = (calls_waiting, longest_wait)

    def total_waiting(self) -> int:
        """Total callers currently waiting across all queues"""
        with self._lock:
            return sum(waiting for waiting, _ in self._runtime_stats.values())

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_queue_status(self, queue_number: str) -> dict | None:
        """Status snapshot for one queue"""
        with self._lock:
            queue = self.queues.get(queue_number)
            if not queue:
                return None
            waiting, longest_wait = self._runtime_stats.get(queue_number, (0, 0.0))
            members = sorted(queue.members)
            available = sum(
                1 for ext in members if ext in self.agents and self.agents[ext].is_selectable()
            )
            return {
                "queue_number": queue.queue_number,
                "name": queue.name,
                "strategy": queue.strategy.value,
                "enabled": queue.enabled,
                "ring_timeout": queue.ring_timeout,
                "max_wait_time": queue.max_wait_time,
                "max_queue_size": queue.max_queue_size,
                "fallback_mailbox": queue.overflow_mailbox(),
                "auto_pause_misses": queue.auto_pause_misses,
                "announcement_enabled": queue.announcement_enabled,
                "announcement_interval": queue.announcement_interval,
                "announcement_text": queue.announcement_text,
                "announcement_file": queue.announcement_file,
                "announcement_position": queue.announcement_position,
                "overflow_action": queue.overflow_action,
                "max_redials": queue.max_redials,
                "members": members,
                "calls_waiting": waiting,
                "longest_wait": longest_wait,
                "total_agents": len(members),
                "available_agents": available,
            }

    def get_all_status(self) -> list[dict]:
        """Status snapshots for all queues"""
        with self._lock:
            statuses = [self.get_queue_status(number) for number in sorted(self.queues)]
            return [s for s in statuses if s is not None]

    # ------------------------------------------------------------------
    # Persistence internals (%s params, PostgreSQL upserts, autocommit)
    # ------------------------------------------------------------------

    def _persist_queue(self, queue: CallQueue) -> None:
        if self.db is None:
            return
        try:
            self.db.execute(
                """
                INSERT INTO call_queues
                    (queue_number, name, strategy, ring_timeout, max_wait_time,
                     max_queue_size, fallback_mailbox, auto_pause_misses, enabled,
                     announcement_enabled, announcement_interval, announcement_text,
                     announcement_file, announcement_position, overflow_action,
                     max_redials, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (queue_number) DO UPDATE SET
                    name = EXCLUDED.name,
                    strategy = EXCLUDED.strategy,
                    ring_timeout = EXCLUDED.ring_timeout,
                    max_wait_time = EXCLUDED.max_wait_time,
                    max_queue_size = EXCLUDED.max_queue_size,
                    fallback_mailbox = EXCLUDED.fallback_mailbox,
                    auto_pause_misses = EXCLUDED.auto_pause_misses,
                    enabled = EXCLUDED.enabled,
                    announcement_enabled = EXCLUDED.announcement_enabled,
                    announcement_interval = EXCLUDED.announcement_interval,
                    announcement_text = EXCLUDED.announcement_text,
                    announcement_file = EXCLUDED.announcement_file,
                    announcement_position = EXCLUDED.announcement_position,
                    overflow_action = EXCLUDED.overflow_action,
                    max_redials = EXCLUDED.max_redials,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    queue.queue_number,
                    queue.name,
                    queue.strategy.value,
                    queue.ring_timeout,
                    queue.max_wait_time,
                    queue.max_queue_size,
                    queue.fallback_mailbox,
                    queue.auto_pause_misses,
                    queue.enabled,
                    queue.announcement_enabled,
                    queue.announcement_interval,
                    queue.announcement_text,
                    queue.announcement_file,
                    queue.announcement_position,
                    queue.overflow_action,
                    queue.max_redials,
                    datetime.now(tz=UTC),
                ),
            )
        except Exception as e:
            self.logger.error(f"Failed to persist queue {queue.queue_number}: {e}")

    def _persist_member(self, queue_number: str, extension: str) -> None:
        if self.db is None:
            return
        try:
            self.db.execute(
                """
                INSERT INTO queue_agents (queue_number, extension)
                VALUES (%s, %s)
                ON CONFLICT (queue_number, extension) DO NOTHING
                """,
                (queue_number, extension),
            )
        except Exception as e:
            self.logger.error(f"Failed to persist member {extension} in {queue_number}: {e}")

    def _persist_agent_state(self, agent: Agent) -> None:
        if self.db is None:
            return
        try:
            self.db.execute(
                """
                INSERT INTO queue_agent_state
                    (extension, logged_in, paused, pause_reason, consecutive_misses,
                     calls_taken, last_call_time, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (extension) DO UPDATE SET
                    logged_in = EXCLUDED.logged_in,
                    paused = EXCLUDED.paused,
                    pause_reason = EXCLUDED.pause_reason,
                    consecutive_misses = EXCLUDED.consecutive_misses,
                    calls_taken = EXCLUDED.calls_taken,
                    last_call_time = EXCLUDED.last_call_time,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    agent.extension,
                    agent.logged_in,
                    agent.paused,
                    agent.pause_reason,
                    agent.consecutive_misses,
                    agent.calls_taken,
                    agent.last_call_time,
                    datetime.now(tz=UTC),
                ),
            )
        except Exception as e:
            self.logger.error(f"Failed to persist agent state for {agent.extension}: {e}")
