"""Shared utilities for Flask API routes."""

import json
from collections.abc import Callable
from datetime import date, datetime
from functools import wraps
from pathlib import PurePath
from typing import Any, Final

from flask import Response, current_app, jsonify, request

from pbx.utils.logger import get_logger

logger = get_logger()


class DateTimeEncoder(json.JSONEncoder):
    """Custom JSON encoder that handles datetime and Path objects."""

    def default(self, obj: object) -> Any:
        if isinstance(obj, datetime | date):
            return obj.isoformat()
        if isinstance(obj, PurePath):
            return str(obj)
        return super().default(obj)


def get_pbx_core() -> Any:
    """Get PBX core instance from Flask app config."""
    return current_app.config.get("PBX_CORE")


def send_json(data: Any, status: int = 200) -> Response:
    """Send JSON response with DateTimeEncoder support."""
    response = current_app.response_class(
        response=json.dumps(data, cls=DateTimeEncoder),
        status=status,
        mimetype="application/json",
    )
    return response


def get_auth_token() -> str | None:
    """Extract authentication token from request headers."""
    auth_header: str = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return str(auth_header[7:])
    return None


def verify_authentication() -> tuple[bool, dict[str, Any] | None]:
    """Verify authentication token and return payload.

    Returns:
        tuple of (is_authenticated, payload)
    """
    token = get_auth_token()
    if not token:
        return False, None

    from pbx.utils.session_token import get_session_token_manager

    token_manager = get_session_token_manager()
    result: tuple[bool, dict[str, Any] | None] = token_manager.verify_token(token)
    return result


def require_auth(f: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator that requires authentication."""

    @wraps(f)
    def decorated(*args: Any, **kwargs: Any) -> Any:
        is_authenticated, payload = verify_authentication()
        if not is_authenticated:
            return jsonify({"error": "Authentication required"}), 401
        request.auth_payload = payload  # type: ignore[attr-defined]
        return f(*args, **kwargs)

    return decorated


def require_admin(f: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator that requires admin privileges."""

    @wraps(f)
    def decorated(*args: Any, **kwargs: Any) -> Any:
        is_authenticated, payload = verify_authentication()
        if not is_authenticated:
            return jsonify({"error": "Authentication required"}), 401
        if not payload or not payload.get("is_admin", False):
            return jsonify({"error": "Admin privileges required"}), 403
        request.auth_payload = payload  # type: ignore[attr-defined]
        return f(*args, **kwargs)

    return decorated


def get_request_body() -> dict[str, Any]:
    """Get request body as JSON dict."""
    return request.get_json(silent=True) or {}


def check_extension_access(extension: str) -> tuple[bool, Response | None]:
    """Check if the authenticated user can access the given extension's resources.

    Admins can access any extension. Regular users can only access their own.

    Returns:
        tuple of (allowed, error_response). If allowed is False, error_response
        contains the 403 JSON response to return.
    """
    payload = getattr(request, "auth_payload", None)
    if not payload:
        return False, send_json({"error": "Authentication required"}, 401)

    is_admin = payload.get("is_admin", False)
    if is_admin:
        return True, None

    user_extension = payload.get("extension")
    if str(user_extension) != str(extension):
        return False, send_json({"error": "Not authorized to access this extension"}, 403)

    return True, None


#: Config key controlling whether a non-admin may reach recordings of calls they were on.
#: Defaults to false: a recording is the most sensitive thing this system stores, and plenty
#: of deployments are required to keep it to compliance staff. Opting in is a decision someone
#: has to make deliberately, not one they have to remember to undo.
#:
#: Lives under ``recording`` rather than ``features.call_recording``, which is a bool -- a
#: dotted lookup through it returns the default at the non-dict and the flag could never be
#: switched on.
PARTICIPANT_ACCESS_KEY: Final[str] = "recording.participant_access"


def participant_access_enabled() -> bool:
    """Whether non-admins may read recordings of calls they were party to."""
    pbx_core = get_pbx_core()
    if not pbx_core or not getattr(pbx_core, "config", None):
        return False
    return bool(pbx_core.config.get(PARTICIPANT_ACCESS_KEY, False))


def check_recording_access(recording: dict[str, Any]) -> tuple[bool, str | None]:
    """Check whether the authenticated user may read this recording or its transcript.

    Admins may read anything. Everyone else may read only recordings they were a party to,
    and only when ``participant_access`` is switched on. There is no middle tier -- the
    schema has no team or org model to hang a "supervisor sees their reports" rule on, and
    inventing one from extension numbers would be guesswork.

    Participation is matched against ``recordings.participants``, which the recorder
    denormalises from the channel map precisely so this check does not have to parse JSON.

    Returns:
        tuple of (allowed, denial_reason). ``denial_reason`` is None when allowed, and
        otherwise a short string for the audit log -- not for the caller, who gets a flat
        403 either way so the response cannot be used to probe who was on a call.
    """
    payload = getattr(request, "auth_payload", None)
    if not payload:
        return False, "unauthenticated"

    if payload.get("is_admin", False):
        return True, None

    if not participant_access_enabled():
        return False, "participant_access_disabled"

    participants = recording.get("participants") or []
    if not isinstance(participants, list):
        return False, "participants_unreadable"

    user_extension = str(payload.get("extension", ""))
    if not user_extension:
        return False, "no_extension_on_token"

    if user_extension not in {str(p) for p in participants}:
        return False, "not_a_participant"

    return True, None


def get_query_params() -> dict[str, Any]:
    """Get query parameters as a dict."""
    return dict(request.args)


def validate_limit_param(default: int = 50, max_value: int = 1000) -> int | None:
    """Validate a 'limit' query parameter.

    Returns:
        Validated limit value or None with error response sent.
    """
    try:
        limit = int(request.args.get("limit", default))
        if limit < 1:
            return None
        if limit > max_value:
            return None
        return limit
    except ValueError:
        return None
