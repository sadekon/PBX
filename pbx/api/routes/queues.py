"""Call Queue (ACD) Blueprint routes.

Queue CRUD, agent membership management, agent runtime state (the endpoint a
future AD calendar sync drives), and live status for the admin dashboard.
"""

import re
from typing import Any

from flask import Blueprint, Response

from pbx.api.utils import (
    get_pbx_core,
    get_request_body,
    require_auth,
    send_json,
)
from pbx.utils.logger import get_logger

logger = get_logger()

queues_bp = Blueprint("queues", __name__, url_prefix="/api/queues")

DEFAULT_QUEUE_PATTERN = r"^8[0-9]{3}$"

# Integer config fields and their sane bounds (min, max)
_INT_FIELDS = {
    "ring_timeout": (5, 120),
    "max_wait_time": (10, 3600),
    "max_queue_size": (1, 100),
    "auto_pause_misses": (0, 20),
    "announcement_interval": (10, 600),
}

_PAUSE_REASONS = ("manual", "auto_missed", "ad_calendar")


def _get_queue_system() -> Any:
    """Get queue system instance or None if not available/enabled."""
    pbx_core = get_pbx_core()
    if not pbx_core or not hasattr(pbx_core, "queue_system"):
        return None
    if not pbx_core.config.get("features.call_queues", True):
        return None
    return pbx_core.queue_system


def _queue_pattern() -> str:
    """Configured dialplan queue pattern (falls back to 8xxx)."""
    pbx_core = get_pbx_core()
    if pbx_core:
        return pbx_core.config.get("dialplan.queue_pattern", DEFAULT_QUEUE_PATTERN)
    return DEFAULT_QUEUE_PATTERN


def _validate_strategy(value: str) -> str | None:
    """Return an error message if the strategy is invalid, else None."""
    from pbx.features.call_queue import SUPPORTED_STRATEGIES

    if value == "ring_all":
        return "Strategy 'ring_all' is not yet supported"
    if value not in SUPPORTED_STRATEGIES:
        return f"Invalid strategy '{value}' (supported: {', '.join(SUPPORTED_STRATEGIES)})"
    return None


def _validate_overflow_action(value: str) -> str | None:
    """Return an error message if the overflow action is invalid, else None."""
    if value not in ("voicemail", "drop"):
        return f"Invalid overflow_action '{value}' (supported: voicemail, drop)"
    return None


def _validate_int_fields(data: dict) -> str | None:
    """Bounds-check integer config fields present in the payload."""
    for field, (lo, hi) in _INT_FIELDS.items():
        if field in data and data[field] is not None:
            try:
                value = int(data[field])
            except (TypeError, ValueError):
                return f"{field} must be an integer"
            if not lo <= value <= hi:
                return f"{field} must be between {lo} and {hi}"
    return None


def _apply_queue_fields(queue: Any, data: dict) -> None:
    """Copy validated payload fields onto a CallQueue object."""
    from pbx.features.call_queue import QueueStrategy

    if data.get("name"):
        queue.name = str(data["name"])
    if data.get("strategy"):
        queue.strategy = QueueStrategy(data["strategy"])
    for field in _INT_FIELDS:
        if field in data and data[field] is not None:
            setattr(queue, field, int(data[field]))
    if "fallback_mailbox" in data:
        mailbox = data["fallback_mailbox"]
        queue.fallback_mailbox = str(mailbox) if mailbox else None
    if "enabled" in data:
        queue.enabled = bool(data["enabled"])
    if "announcement_enabled" in data:
        queue.announcement_enabled = bool(data["announcement_enabled"])
    if "announcement_position" in data:
        queue.announcement_position = bool(data["announcement_position"])
    if "announcement_text" in data:
        text = data["announcement_text"]
        queue.announcement_text = str(text) if text else None
    if "announcement_file" in data:
        file_name = data["announcement_file"]
        queue.announcement_file = str(file_name) if file_name else None
    if data.get("overflow_action"):
        queue.overflow_action = data["overflow_action"]


@queues_bp.route("", methods=["GET"])
@require_auth
def handle_list_queues() -> Response:
    """List all queues with configuration, membership, and live stats."""
    queue_system = _get_queue_system()
    if not queue_system:
        return send_json({"queues": []})

    try:
        return send_json({"queues": queue_system.get_all_status()})
    except Exception as e:
        logger.error(f"Error listing queues: {e}")
        return send_json({"queues": []})


@queues_bp.route("/status", methods=["GET"])
@require_auth
def handle_queue_status() -> Response:
    """Live status dashboard payload (same shape as the list endpoint)."""
    queue_system = _get_queue_system()
    if not queue_system:
        return send_json({"queues": []})

    try:
        return send_json({"queues": queue_system.get_all_status()})
    except Exception as e:
        logger.error(f"Error getting queue status: {e}")
        return send_json({"queues": []})


@queues_bp.route("", methods=["POST"])
@require_auth
def handle_create_queue() -> Response:
    """Create a queue."""
    queue_system = _get_queue_system()
    if not queue_system:
        return send_json({"error": "Call queues not enabled"}, 500)

    try:
        data = get_request_body()
        queue_number = str(data.get("queue_number", "")).strip()
        name = str(data.get("name", "")).strip()

        if not queue_number or not name:
            return send_json({"error": "queue_number and name are required"}, 400)
        if not re.match(_queue_pattern(), queue_number):
            return send_json(
                {"error": f"queue_number must match dialplan pattern {_queue_pattern()}"}, 400
            )
        if queue_system.get_queue(queue_number):
            return send_json({"error": f"Queue {queue_number} already exists"}, 409)

        strategy = data.get("strategy", "round_robin")
        strategy_error = _validate_strategy(strategy)
        if strategy_error:
            return send_json({"error": strategy_error}, 400)

        if data.get("overflow_action"):
            overflow_error = _validate_overflow_action(data["overflow_action"])
            if overflow_error:
                return send_json({"error": overflow_error}, 400)

        bounds_error = _validate_int_fields(data)
        if bounds_error:
            return send_json({"error": bounds_error}, 400)

        queue = queue_system.create_queue(queue_number, name)
        _apply_queue_fields(queue, data)
        queue_system.save_queue(queue)

        return send_json({"success": True, "message": f"Queue {queue_number} created"})
    except (KeyError, TypeError, ValueError) as e:
        return send_json({"error": str(e)}, 400)


@queues_bp.route("/<queue_number>", methods=["PUT"])
@require_auth
def handle_update_queue(queue_number: str) -> Response:
    """Update a queue's configuration."""
    queue_system = _get_queue_system()
    if not queue_system:
        return send_json({"error": "Call queues not enabled"}, 500)

    queue = queue_system.get_queue(queue_number)
    if not queue:
        return send_json({"error": f"Queue {queue_number} not found"}, 404)

    try:
        data = get_request_body()

        if data.get("strategy"):
            strategy_error = _validate_strategy(data["strategy"])
            if strategy_error:
                return send_json({"error": strategy_error}, 400)

        if data.get("overflow_action"):
            overflow_error = _validate_overflow_action(data["overflow_action"])
            if overflow_error:
                return send_json({"error": overflow_error}, 400)

        bounds_error = _validate_int_fields(data)
        if bounds_error:
            return send_json({"error": bounds_error}, 400)

        _apply_queue_fields(queue, data)
        queue_system.save_queue(queue)

        return send_json({"success": True, "message": f"Queue {queue_number} updated"})
    except (KeyError, TypeError, ValueError) as e:
        return send_json({"error": str(e)}, 400)


@queues_bp.route("/<queue_number>", methods=["DELETE"])
@require_auth
def handle_delete_queue(queue_number: str) -> Response:
    """Delete a queue (rejected while callers are waiting)."""
    queue_system = _get_queue_system()
    if not queue_system:
        return send_json({"error": "Call queues not enabled"}, 500)

    status = queue_system.get_queue_status(queue_number)
    if status is None:
        return send_json({"error": f"Queue {queue_number} not found"}, 404)
    if status.get("calls_waiting", 0) > 0:
        return send_json({"error": f"Queue {queue_number} has waiting callers; cannot delete"}, 409)

    if queue_system.delete_queue(queue_number):
        return send_json({"success": True, "message": f"Queue {queue_number} deleted"})
    return send_json({"error": "Failed to delete queue"}, 500)


@queues_bp.route("/<queue_number>/agents", methods=["POST"])
@require_auth
def handle_add_queue_agent(queue_number: str) -> Response:
    """Add an agent extension to a queue."""
    queue_system = _get_queue_system()
    if not queue_system:
        return send_json({"error": "Call queues not enabled"}, 500)

    try:
        data = get_request_body()
        extension = str(data.get("extension", "")).strip()
        if not extension:
            return send_json({"error": "extension is required"}, 400)

        if not queue_system.get_queue(queue_number):
            return send_json({"error": f"Queue {queue_number} not found"}, 404)

        if queue_system.add_member(queue_number, extension):
            return send_json(
                {"success": True, "message": f"Agent {extension} added to queue {queue_number}"}
            )
        return send_json({"error": "Failed to add agent"}, 500)
    except (KeyError, TypeError, ValueError) as e:
        return send_json({"error": str(e)}, 400)


@queues_bp.route("/<queue_number>/agents/<extension>", methods=["DELETE"])
@require_auth
def handle_remove_queue_agent(queue_number: str, extension: str) -> Response:
    """Remove an agent extension from a queue."""
    queue_system = _get_queue_system()
    if not queue_system:
        return send_json({"error": "Call queues not enabled"}, 500)

    if queue_system.remove_member(queue_number, extension):
        return send_json(
            {"success": True, "message": f"Agent {extension} removed from queue {queue_number}"}
        )
    return send_json({"error": f"Agent {extension} not in queue {queue_number}"}, 404)


@queues_bp.route("/agents/state", methods=["GET"])
@require_auth
def handle_get_agent_states() -> Response:
    """Get runtime state for all known agents."""
    queue_system = _get_queue_system()
    if not queue_system:
        return send_json({"agents": []})

    try:
        pbx_core = get_pbx_core()
        queue_handler = getattr(pbx_core, "queue_handler", None)
        agents = [
            {
                "extension": agent.extension,
                "logged_in": agent.logged_in,
                "paused": agent.paused,
                "pause_reason": agent.pause_reason,
                "consecutive_misses": agent.consecutive_misses,
                "calls_taken": agent.calls_taken,
                "last_call_time": (
                    agent.last_call_time.isoformat() if agent.last_call_time else None
                ),
                "queues": queue_system.agent_queues(agent.extension),
                # Live reachability: registered and not on a call right now
                "dialable": (
                    bool(queue_handler._agent_dialable(agent.extension))
                    if queue_handler is not None
                    else False
                ),
            }
            for agent in sorted(queue_system.agents.values(), key=lambda a: a.extension)
        ]
        return send_json({"agents": agents})
    except Exception as e:
        logger.error(f"Error getting agent states: {e}")
        return send_json({"agents": []})


@queues_bp.route("/agents/<extension>/state", methods=["PUT"])
@require_auth
def handle_set_agent_state(extension: str) -> Response:
    """
    Update an agent's runtime state: login/logout and pause/unpause.

    This is the endpoint external automation (e.g. an AD vacation-calendar
    sync) should drive, passing pause_reason='ad_calendar'.
    """
    queue_system = _get_queue_system()
    if not queue_system:
        return send_json({"error": "Call queues not enabled"}, 500)

    try:
        data = get_request_body()

        if "logged_in" not in data and "paused" not in data:
            return send_json({"error": "logged_in or paused is required"}, 400)

        if "logged_in" in data:
            queues = queue_system.set_agent_login(extension, bool(data["logged_in"]))
            if not queues:
                return send_json(
                    {"error": f"Extension {extension} is not a member of any queue"}, 404
                )

        if "paused" in data:
            paused = bool(data["paused"])
            reason = data.get("pause_reason", "manual") if paused else None
            if reason is not None and reason not in _PAUSE_REASONS:
                return send_json(
                    {"error": f"pause_reason must be one of: {', '.join(_PAUSE_REASONS)}"}, 400
                )
            if not queue_system.set_agent_pause(extension, paused, reason):
                return send_json({"error": f"No agent state for extension {extension}"}, 404)

        agent = queue_system.get_agent(extension)
        return send_json(
            {
                "success": True,
                "agent": {
                    "extension": extension,
                    "logged_in": agent.logged_in if agent else False,
                    "paused": agent.paused if agent else False,
                    "pause_reason": agent.pause_reason if agent else None,
                },
            }
        )
    except (KeyError, TypeError, ValueError) as e:
        return send_json({"error": str(e)}, 400)
