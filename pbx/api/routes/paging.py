"""Paging System Blueprint routes.

Zones, their destinations, and the pages currently in flight.

EVERY COLLECTION ROUTE RETURNS THE SAME SHAPE, ENABLED OR NOT. The previous version returned
a bare array when paging was on and `{"zones": []}` when it was off, so the admin client --
which reads `data.zones` -- saw `undefined` on the only path that had data, and the tab
rendered empty however much was configured.
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


def _get_paging_system() -> Any:
    """Get paging system instance or None if not available."""
    pbx_core = get_pbx_core()
    if not pbx_core or not hasattr(pbx_core, "paging_system"):
        return None

    if not pbx_core.paging_system or not pbx_core.paging_system.enabled:
        return None

    return pbx_core.paging_system


def _int_or_none(value: Any) -> int | None:
    """Coerce a request value to int, or None if it is not one."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------- status


@paging_bp.route("/status", methods=["GET"])
@require_auth
def handle_paging_status() -> Response:
    """
    Report whether paging is running and whether it has anything configured.

    "Switched off" and "on but no zones yet" are different problems with different fixes,
    and without this they look identical from the admin page.
    """
    paging_system = _get_paging_system()
    if not paging_system:
        return send_json(
            {
                "enabled": False,
                "persistent": False,
                "zone_count": 0,
                "destination_count": 0,
                "active_page_count": 0,
            }
        )
    return send_json(paging_system.get_status())


# ---------------------------------------------------------------------------- zones


@paging_bp.route("/zones", methods=["GET"])
@require_auth
def handle_get_paging_zones() -> Response:
    """Get all paging zones, each with its destinations."""
    paging_system = _get_paging_system()
    if not paging_system:
        return send_json({"zones": []})

    try:
        return send_json({"zones": paging_system.get_zones()})
    except Exception as e:
        logger.error(f"Error getting paging zones: {e}")
        return send_json({"zones": []})


@paging_bp.route("/zones/<int:zone_id>", methods=["GET"])
@require_auth
def handle_get_paging_zone(zone_id: int) -> Response:
    """Get one paging zone."""
    paging_system = _get_paging_system()
    if not paging_system:
        return send_json({"error": "Paging is not enabled"}, 503)

    zone = paging_system.get_zone(zone_id)
    if not zone:
        return send_json({"error": "That zone no longer exists"}, 404)
    return send_json({"zone": zone})


@paging_bp.route("/zones", methods=["POST"])
@require_auth
def handle_add_paging_zone() -> Response:
    """Create a paging zone."""
    paging_system = _get_paging_system()
    if not paging_system:
        return send_json({"error": "Paging is not enabled"}, 503)

    data = get_request_body()
    zone_id, error = paging_system.create_zone(
        extension=data.get("extension"),
        name=data.get("name"),
        description=data.get("description"),
        max_duration_seconds=_int_or_none(data.get("max_duration_seconds")),
    )
    if error:
        # A number already taken is the caller's mistake to fix, not a server fault.
        status = 409 if "already" in error else 400
        return send_json({"error": error}, status)

    return send_json({"success": True, "zone": paging_system.get_zone(zone_id)}, 201)


@paging_bp.route("/zones/<int:zone_id>", methods=["PUT"])
@require_auth
def handle_update_paging_zone(zone_id: int) -> Response:
    """Update a paging zone's name, description, enabled flag or duration cap."""
    paging_system = _get_paging_system()
    if not paging_system:
        return send_json({"error": "Paging is not enabled"}, 503)

    if not paging_system.get_zone(zone_id):
        return send_json({"error": "That zone no longer exists"}, 404)

    data = get_request_body()
    fields: dict[str, Any] = {}
    for key in ("name", "description", "enabled"):
        if key in data:
            fields[key] = data[key]
    if "max_duration_seconds" in data:
        fields["max_duration_seconds"] = _int_or_none(data["max_duration_seconds"])

    if not fields:
        return send_json({"error": "Nothing to update"}, 400)

    if not paging_system.update_zone(zone_id, **fields):
        return send_json({"error": "Could not update the zone"}, 500)

    return send_json({"success": True, "zone": paging_system.get_zone(zone_id)})


@paging_bp.route("/zones/<int:zone_id>", methods=["DELETE"])
@require_auth
def handle_delete_paging_zone(zone_id: int) -> Response:
    """Delete a paging zone and its destinations."""
    paging_system = _get_paging_system()
    if not paging_system:
        return send_json({"error": "Paging is not enabled"}, 503)

    ok, error = paging_system.delete_zone(zone_id)
    if not ok:
        status = 409 if error and "paging right now" in error else 404
        return send_json({"error": error}, status)
    return send_json({"success": True})


# ---------------------------------------------------------------------------- destinations


@paging_bp.route("/zones/<int:zone_id>/destinations", methods=["GET"])
@require_auth
def handle_get_zone_destinations(zone_id: int) -> Response:
    """Get a zone's destinations."""
    paging_system = _get_paging_system()
    if not paging_system:
        return send_json({"destinations": []})
    return send_json({"destinations": paging_system.get_destinations(zone_id)})


@paging_bp.route("/zones/<int:zone_id>/destinations", methods=["POST"])
@require_auth
def handle_add_zone_destination(zone_id: int) -> Response:
    """
    Add a destination to a zone.

    kind "sip_endpoint" takes the extension an ATA's FXS port answers on -- hardware detail
    is not accepted here, because it already lives in provisioned_devices and a second copy
    would drift. kind "multicast" is stored but not yet streamed to.
    """
    paging_system = _get_paging_system()
    if not paging_system:
        return send_json({"error": "Paging is not enabled"}, 503)

    data = get_request_body()
    kind = (data.get("kind") or "sip_endpoint").strip()

    if kind == "sip_endpoint":
        destination_id, error = paging_system.add_sip_destination(
            zone_id=zone_id,
            endpoint_extension=data.get("endpoint_extension"),
            label=data.get("label"),
            auto_answer_override=data.get("auto_answer_override"),
        )
    elif kind == "multicast":
        port = _int_or_none(data.get("multicast_port"))
        if not data.get("multicast_address") or port is None:
            return send_json({"error": "A multicast destination needs an address and port"}, 400)
        destination_id, error = paging_system.add_multicast_destination(
            zone_id=zone_id,
            multicast_address=data.get("multicast_address"),
            multicast_port=port,
            label=data.get("label"),
        )
    else:
        return send_json({"error": "kind must be sip_endpoint or multicast"}, 400)

    if error:
        return send_json({"error": error}, 400)

    return send_json(
        {
            "success": True,
            "destination_id": destination_id,
            "zone": paging_system.get_zone(zone_id),
        },
        201,
    )


@paging_bp.route("/destinations/<int:destination_id>", methods=["DELETE"])
@require_auth
def handle_delete_destination(destination_id: int) -> Response:
    """Remove a destination from its zone."""
    paging_system = _get_paging_system()
    if not paging_system:
        return send_json({"error": "Paging is not enabled"}, 503)

    ok, error = paging_system.remove_destination(destination_id)
    if not ok:
        status = 409 if error and "paging right now" in error else 404
        return send_json({"error": error}, status)
    return send_json({"success": True})


@paging_bp.route("/endpoints", methods=["GET"])
@require_auth
def handle_get_candidate_endpoints() -> Response:
    """
    list the ATAs available to be paging destinations.

    Read straight from provisioned_devices, filtered to device_type 'ata'. A dual-port ATA
    contributes both of its extensions, because each FXS port drives its own amplifier
    circuit and belongs to its own zone.
    """
    pbx_core = get_pbx_core()
    if not pbx_core or not getattr(pbx_core, "phone_provisioning", None):
        return send_json({"endpoints": []})

    try:
        devices_db = getattr(pbx_core.phone_provisioning, "devices_db", None)
        if not devices_db:
            return send_json({"endpoints": []})

        endpoints = []
        for device in devices_db.list_atas() or []:
            for extension, port in (
                (device.get("extension_number"), 1),
                (device.get("extension_number_2"), 2),
            ):
                if extension:
                    endpoints.append(
                        {
                            "extension": extension,
                            "port": port,
                            "vendor": device.get("vendor"),
                            "model": device.get("model"),
                            "mac_address": device.get("mac_address"),
                        }
                    )
        return send_json({"endpoints": endpoints})
    except Exception as e:
        logger.error(f"Error listing paging endpoints: {e}")
        return send_json({"endpoints": []})


# ---------------------------------------------------------------------------- active pages


@paging_bp.route("/active", methods=["GET"])
@require_auth
def handle_get_active_pages() -> Response:
    """Get the pages currently in flight."""
    paging_system = _get_paging_system()
    if not paging_system:
        return send_json({"active_pages": []})

    try:
        return send_json({"active_pages": paging_system.get_active_pages()})
    except Exception as e:
        logger.error(f"Error getting active pages: {e}")
        return send_json({"active_pages": []})


@paging_bp.route("/active/<page_id>", methods=["DELETE"])
@require_auth
def handle_kill_active_page(page_id: str) -> Response:
    """End a page from the admin side, for one that has stuck."""
    paging_system = _get_paging_system()
    if not paging_system:
        return send_json({"error": "Paging is not enabled"}, 503)

    page = paging_system.get_page(page_id)
    if not page:
        return send_json({"error": "That page is not running"}, 404)

    pbx_core = get_pbx_core()
    if page.call_id and pbx_core:
        # Through end_call so the pager's leg is hung up too, rather than left connected to
        # a page that no longer exists.
        pbx_core.end_call(page.call_id)
    else:
        paging_system.end_page(page_id)

    return send_json({"success": True})
