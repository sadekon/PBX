"""Paging System Blueprint routes.

Handles paging zone management, DAC device configuration,
active page session queries, and admin-initiated test pages.

Every collection response is wrapped under a stable key -- ``zones``,
``devices``, ``active_pages`` -- and that shape does not change when the
paging feature is disabled, so a client can parse one response shape
unconditionally and use ``GET /api/paging/status`` to tell "paging is off"
apart from "paging is on but nothing is configured yet".
"""

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

paging_bp = Blueprint("paging", __name__, url_prefix="/api/paging")

_DISABLED_MESSAGE = "Paging system is not enabled"


def _get_paging_system() -> Any:
    """Get paging system instance or None if not available."""
    pbx_core = get_pbx_core()
    if not pbx_core or not hasattr(pbx_core, "paging_system"):
        return None

    if not pbx_core.paging_system or not pbx_core.paging_system.enabled:
        return None

    return pbx_core.paging_system


def _disabled_response() -> Response:
    """Standard rejection for writes attempted while paging is disabled."""
    return send_json({"error": _DISABLED_MESSAGE}, 503)


@paging_bp.route("/status", methods=["GET"])
@require_auth
def handle_get_paging_status() -> Response:
    """Get paging system status, including whether the feature is enabled."""
    pbx_core = get_pbx_core()
    paging_system = getattr(pbx_core, "paging_system", None) if pbx_core else None

    if not paging_system:
        return send_json({"enabled": False})

    try:
        return send_json(paging_system.get_status())
    except Exception as e:
        logger.error(f"Error getting paging status: {e}")
        return send_json({"enabled": False})


@paging_bp.route("/zones", methods=["GET"])
@require_auth
def handle_get_paging_zones() -> Response:
    """Get all paging zones."""
    paging_system = _get_paging_system()
    if not paging_system:
        return send_json({"zones": []})

    try:
        return send_json({"zones": paging_system.get_zones()})
    except Exception as e:
        logger.error(f"Error getting paging zones: {e}")
        # Return empty zones instead of error to prevent UI errors
        return send_json({"zones": []})


@paging_bp.route("/devices", methods=["GET"])
@require_auth
def handle_get_paging_devices() -> Response:
    """Get all paging DAC devices."""
    paging_system = _get_paging_system()
    if not paging_system:
        return send_json({"devices": []})

    try:
        return send_json({"devices": paging_system.get_dac_devices()})
    except Exception as e:
        logger.error(f"Error getting paging devices: {e}")
        # Return empty devices instead of error to prevent UI errors
        return send_json({"devices": []})


@paging_bp.route("/active", methods=["GET"])
@require_auth
def handle_get_active_pages() -> Response:
    """Get all active paging sessions."""
    paging_system = _get_paging_system()
    if not paging_system:
        return send_json({"active_pages": []})

    try:
        return send_json({"active_pages": paging_system.get_active_pages()})
    except Exception as e:
        logger.error(f"Error getting active pages: {e}")
        # Return empty active pages instead of error to prevent UI errors
        return send_json({"active_pages": []})


@paging_bp.route("/zones", methods=["POST"])
@require_auth
def handle_add_paging_zone() -> Response:
    """Add a paging zone."""
    paging_system = _get_paging_system()
    if not paging_system:
        return _disabled_response()

    try:
        data = get_request_body()
        extension = data.get("extension")
        name = data.get("name")

        if not extension or not name:
            return send_json({"error": "Extension and name are required"}, 400)

        success = paging_system.add_zone(
            extension=extension,
            name=name,
            description=data.get("description"),
            dac_device=data.get("dac_device"),
        )

        if success:
            return send_json({"success": True, "message": f"Paging zone added: {extension}"})
        return send_json({"error": f"Paging zone {extension} already exists"}, 409)
    except (KeyError, TypeError, ValueError) as e:
        return send_json({"error": str(e)}, 500)


@paging_bp.route("/devices", methods=["POST"])
@require_auth
def handle_configure_paging_device() -> Response:
    """Configure a paging DAC device."""
    paging_system = _get_paging_system()
    if not paging_system:
        return _disabled_response()

    try:
        data = get_request_body()
        device_id = data.get("device_id")
        device_type = data.get("device_type")

        if not device_id or not device_type:
            return send_json({"error": "device_id and device_type are required"}, 400)

        success = paging_system.configure_dac_device(
            device_id=device_id,
            device_type=device_type,
            sip_uri=data.get("sip_uri"),
            ip_address=data.get("ip_address"),
            port=data.get("port", 5060),
            name=data.get("name"),
        )

        if success:
            return send_json({"success": True, "message": f"DAC device configured: {device_id}"})
        return send_json({"error": f"DAC device {device_id} already exists"}, 409)
    except (KeyError, TypeError, ValueError) as e:
        return send_json({"error": str(e)}, 500)


@paging_bp.route("/devices/<device_id>", methods=["DELETE"])
@require_auth
def handle_delete_paging_device(device_id: str) -> Response:
    """Delete a paging DAC device."""
    paging_system = _get_paging_system()
    if not paging_system:
        return _disabled_response()

    try:
        if paging_system.remove_dac_device(device_id):
            return send_json({"success": True, "message": f"Paging device removed: {device_id}"})
        return send_json({"error": f"DAC device {device_id} not found"}, 404)
    except (KeyError, TypeError, ValueError) as e:
        return send_json({"error": str(e)}, 500)


@paging_bp.route("/zones/<extension>", methods=["DELETE"])
@require_auth
def handle_delete_paging_zone(extension: str) -> Response:
    """Delete a paging zone."""
    paging_system = _get_paging_system()
    if not paging_system:
        return _disabled_response()

    try:
        if paging_system.remove_zone(extension):
            return send_json({"success": True, "message": f"Paging zone deleted: {extension}"})
        return send_json({"error": f"Paging zone {extension} not found"}, 404)
    except (KeyError, TypeError, ValueError) as e:
        return send_json({"error": str(e)}, 500)


@paging_bp.route("/test", methods=["POST"])
@require_auth
def handle_test_page() -> Response:
    """
    Place a test page: ring an extension and, once it answers, open a page
    from it to the given zone.

    Returns 202 rather than 200 -- the response only confirms the extension
    is being rung; the page itself does not exist until that call is answered.
    """
    paging_system = _get_paging_system()
    if not paging_system:
        return _disabled_response()

    pbx_core = get_pbx_core()
    if not pbx_core or not getattr(pbx_core, "paging_handler", None):
        return send_json({"error": "Paging handler is not available"}, 503)

    try:
        data = get_request_body()
        from_extension = data.get("from_extension")
        zone = data.get("zone")

        if not from_extension or not zone:
            return send_json({"error": "from_extension and zone are required"}, 400)

        result = pbx_core.paging_handler.start_test_page(from_extension, zone)
        return send_json(
            {
                "success": True,
                "message": f"Ringing {from_extension} to page {zone}",
                **result,
            },
            202,
        )
    except ValueError as e:
        return send_json({"error": str(e)}, 400)
    except (KeyError, TypeError) as e:
        logger.error(f"Error starting test page: {e}")
        return send_json({"error": str(e)}, 500)
