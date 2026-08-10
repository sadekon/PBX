"""Features Blueprint routes for the PBX system.

Covers Auto-Attendant, SIP Trunks, LCR, FMFM, Time Routing,
Recording Retention, Fraud Detection, Callback Queue, Mobile Push,
Recording Announcements, and Skills-Based Routing.
"""

import json
import re
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, request

from pbx.api.utils import (
    get_pbx_core,
    get_request_body,
    require_auth,
    send_json,
    validate_limit_param,
    verify_authentication,
)
from pbx.features.retention_policies import (
    SEED_POLICY_ID,
    RetentionPolicy,
    validate_match_rules,
)
from pbx.utils.audit_logger import get_audit_logger
from pbx.utils.logger import get_logger

logger = get_logger()

features_bp = Blueprint("features", __name__)


# ==========================================================================
# Auto-Attendant Routes
# ==========================================================================


@features_bp.route("/api/auto-attendant/config", methods=["GET"])
@require_auth
def get_auto_attendant_config() -> tuple[Response, int]:
    """Get auto attendant configuration."""
    pbx_core = get_pbx_core()
    if not pbx_core or not hasattr(pbx_core, "auto_attendant"):
        return send_json({"error": "Auto attendant not available"}, 500), 500

    try:
        aa = pbx_core.auto_attendant
        config = {
            "enabled": aa.enabled,
            "extension": aa.extension,
            "timeout": aa.timeout,
            "max_retries": aa.max_retries,
            "audio_path": aa.audio_path,
        }
        return send_json(config), 200
    except Exception as e:
        return send_json({"error": str(e)}, 500), 500


@features_bp.route("/api/auto-attendant/config", methods=["PUT"])
@require_auth
def update_auto_attendant_config() -> tuple[Response, int]:
    """Update auto attendant configuration."""
    pbx_core = get_pbx_core()
    if not pbx_core or not hasattr(pbx_core, "auto_attendant"):
        return send_json({"error": "Auto attendant not available"}, 500), 500

    try:
        data = get_request_body()
        aa = pbx_core.auto_attendant

        # Update configuration using the new update_config method
        config_updates: dict[str, Any] = {}
        if "enabled" in data:
            config_updates["enabled"] = bool(data["enabled"])
        if "extension" in data:
            config_updates["extension"] = str(data["extension"])
        if "timeout" in data:
            config_updates["timeout"] = int(data["timeout"])
        if "max_retries" in data:
            config_updates["max_retries"] = int(data["max_retries"])

        # Apply updates and persist to database
        if config_updates:
            aa.update_config(**config_updates)
            config_changed = True
        else:
            config_changed = False

        # Check if prompts configuration was updated
        prompts_updated = "prompts" in data

        # Trigger voice regeneration if prompts or menu options changed
        if prompts_updated or config_changed:
            try:
                _regenerate_voice_prompts(pbx_core, data.get("prompts", {}))
            except (KeyError, TypeError, ValueError) as e:
                logger.warning(f"Failed to regenerate voice prompts: {e}")

        return send_json(
            {
                "success": True,
                "message": "Auto attendant configuration updated and persisted to database",
            }
        ), 200
    except (KeyError, TypeError, ValueError) as e:
        return send_json({"error": str(e)}, 500), 500


@features_bp.route("/api/auto-attendant/menu-options", methods=["GET"])
@require_auth
def get_auto_attendant_menu_options() -> tuple[Response, int]:
    """Get auto attendant menu options."""
    pbx_core = get_pbx_core()
    if not pbx_core or not hasattr(pbx_core, "auto_attendant"):
        return send_json({"error": "Auto attendant not available"}, 500), 500

    try:
        aa = pbx_core.auto_attendant
        options = []
        for digit, option in aa.menu_options.items():
            options.append(
                {
                    "digit": digit,
                    "destination": option["destination"],
                    "description": option["description"],
                }
            )
        return send_json({"menu_options": options}), 200
    except (KeyError, TypeError, ValueError) as e:
        return send_json({"error": str(e)}, 500), 500


@features_bp.route("/api/auto-attendant/menu-options", methods=["POST"])
@require_auth
def add_auto_attendant_menu_option() -> tuple[Response, int]:
    """Add auto attendant menu option."""
    pbx_core = get_pbx_core()
    if not pbx_core or not hasattr(pbx_core, "auto_attendant"):
        return send_json({"error": "Auto attendant not available"}, 500), 500

    try:
        data = get_request_body()
        digit = data.get("digit")
        destination = data.get("destination")
        description = data.get("description", "")

        if not digit or not destination:
            return send_json({"error": "digit and destination are required"}, 400), 400

        aa = pbx_core.auto_attendant
        # Use the new add_menu_option method which persists to database
        aa.add_menu_option(digit, destination, description)

        # Trigger voice regeneration after menu option addition
        try:
            _regenerate_voice_prompts(pbx_core, {})
        except Exception as e:
            logger.warning(f"Failed to regenerate voice prompts: {e}")

        return send_json(
            {"success": True, "message": f"Menu option {digit} added and persisted to database"}
        ), 200
    except Exception as e:
        return send_json({"error": str(e)}, 500), 500


@features_bp.route("/api/auto-attendant/menu-options/<path:subpath>", methods=["PUT"])
@require_auth
def update_auto_attendant_menu_option(subpath: str) -> tuple[Response, int]:
    """Update auto attendant menu option."""
    pbx_core = get_pbx_core()
    if not pbx_core or not hasattr(pbx_core, "auto_attendant"):
        return send_json({"error": "Auto attendant not available"}, 500), 500

    try:
        digit = subpath.rsplit("/", maxsplit=1)[-1]
        data = get_request_body()

        aa = pbx_core.auto_attendant
        if digit not in aa.menu_options:
            return send_json({"error": f"Menu option {digit} not found"}, 404), 404

        # Get current values
        destination = aa.menu_options[digit]["destination"]
        description = aa.menu_options[digit]["description"]

        # Update with new values if provided
        if "destination" in data:
            destination = data["destination"]
        if "description" in data:
            description = data["description"]

        # Use add_menu_option which will update and persist to database
        aa.add_menu_option(digit, destination, description)

        # Trigger voice regeneration after menu option update
        try:
            _regenerate_voice_prompts(pbx_core, {})
        except Exception as e:
            logger.warning(f"Failed to regenerate voice prompts: {e}")

        return send_json(
            {
                "success": True,
                "message": f"Menu option {digit} updated and persisted to database",
            }
        ), 200
    except Exception as e:
        return send_json({"error": str(e)}, 500), 500


@features_bp.route("/api/auto-attendant/menu-options/<digit>", methods=["DELETE"])
@require_auth
def delete_auto_attendant_menu_option(digit: str) -> tuple[Response, int]:
    """Delete auto attendant menu option."""
    pbx_core = get_pbx_core()
    if not pbx_core or not hasattr(pbx_core, "auto_attendant"):
        return send_json({"error": "Auto attendant not available"}, 500), 500

    try:
        aa = pbx_core.auto_attendant
        if digit in aa.menu_options:
            # Use the new remove_menu_option method which deletes from database
            aa.remove_menu_option(digit)

            # Trigger voice regeneration after menu option deletion
            try:
                _regenerate_voice_prompts(pbx_core, {})
            except Exception as e:
                logger.warning(f"Failed to regenerate voice prompts: {e}")

            return send_json(
                {
                    "success": True,
                    "message": f"Menu option {digit} deleted and removed from database",
                }
            ), 200
        return send_json({"error": f"Menu option {digit} not found"}, 404), 404
    except Exception as e:
        return send_json({"error": str(e)}, 500), 500


@features_bp.route("/api/auto-attendant/prompts", methods=["GET"])
@require_auth
def get_auto_attendant_prompts() -> tuple[Response, int]:
    """Get auto attendant prompt texts."""
    pbx_core = get_pbx_core()
    try:
        # Return current prompt configuration
        from pbx.utils.config import Config

        config = pbx_core.config if pbx_core else Config()
        aa_config = config.get("auto_attendant", {})

        prompts = aa_config.get(
            "prompts",
            {
                "welcome": "Thank you for calling {company_name}.",
                "main_menu": "For Sales, press 1. For Support, press 2. For Accounting, press 3. Or press 0 to speak with an operator.",
                "invalid": "That is not a valid option. Please try again.",
                "timeout": "We did not receive your selection. Please try again.",
                "transferring": "Please hold while we transfer your call.",
            },
        )

        company_name = config.get("company_name", "your company")

        return send_json({"prompts": prompts, "company_name": company_name}), 200
    except (KeyError, TypeError, ValueError) as e:
        return send_json({"error": str(e)}, 500), 500


@features_bp.route("/api/auto-attendant/prompts", methods=["PUT"])
@require_auth
def update_auto_attendant_prompts() -> tuple[Response, int]:
    """Update auto attendant prompt texts and regenerate voices."""
    pbx_core = get_pbx_core()
    try:
        data = get_request_body()
        prompts = data.get("prompts", {})
        company_name = data.get("company_name")

        # Update configuration
        from pbx.utils.config import Config

        config = pbx_core.config if pbx_core else Config()

        # Ensure auto_attendant section exists
        if "auto_attendant" not in config.config:
            config.config["auto_attendant"] = {}

        # Update prompts
        if prompts:
            config.config["auto_attendant"]["prompts"] = prompts

        # Update company name
        if company_name:
            config.config["company_name"] = company_name

        # Save configuration
        success = config.save()
        if not success:
            raise Exception("Failed to save configuration file")

        # Trigger voice regeneration
        _regenerate_voice_prompts(pbx_core, prompts, company_name)

        return send_json(
            {"success": True, "message": "Prompts updated and voices regenerated successfully"}
        ), 200
    except (KeyError, TypeError, ValueError) as e:
        logger.error(f"Error updating prompts: {e}")
        return send_json({"error": str(e)}, 500), 500


@features_bp.route("/api/auto-attendant/menus", methods=["GET"])
@require_auth
def get_menus() -> tuple[Response, int]:
    """Get list of all menus."""
    pbx_core = get_pbx_core()
    if not pbx_core or not hasattr(pbx_core, "auto_attendant"):
        return send_json({"error": "Auto attendant not available"}, 500), 500

    try:
        aa = pbx_core.auto_attendant
        menus = aa.list_menus()
        return send_json({"menus": menus}), 200
    except Exception as e:
        return send_json({"error": str(e)}, 500), 500


@features_bp.route("/api/auto-attendant/menus/<menu_id>", methods=["GET"])
@require_auth
def get_menu(menu_id: str) -> tuple[Response, int]:
    """Get details of a specific menu."""
    pbx_core = get_pbx_core()
    if not pbx_core or not hasattr(pbx_core, "auto_attendant"):
        return send_json({"error": "Auto attendant not available"}, 500), 500

    try:
        aa = pbx_core.auto_attendant
        menu = aa.get_menu(menu_id)

        if not menu:
            return send_json({"error": f"Menu '{menu_id}' not found"}, 404), 404

        return send_json({"menu": menu}), 200
    except Exception as e:
        return send_json({"error": str(e)}, 500), 500


@features_bp.route("/api/auto-attendant/menus", methods=["POST"])
@require_auth
def create_menu() -> tuple[Response, int]:
    """Create a new menu or submenu."""
    pbx_core = get_pbx_core()
    if not pbx_core or not hasattr(pbx_core, "auto_attendant"):
        return send_json({"error": "Auto attendant not available"}, 500), 500

    try:
        data = get_request_body()
        menu_id = data.get("menu_id")
        parent_menu_id = data.get("parent_menu_id")
        menu_name = data.get("menu_name")
        prompt_text = data.get("prompt_text", "")

        if not menu_id or not menu_name:
            return send_json({"error": "menu_id and menu_name are required"}, 400), 400

        # Validate menu_id format (alphanumeric, dashes, underscores only)
        if not re.match(r"^[a-z0-9_-]+$", menu_id):
            return send_json(
                {
                    "error": "menu_id must contain only lowercase letters, numbers, dashes, and underscores"
                },
                400,
            ), 400

        aa = pbx_core.auto_attendant
        success = aa.create_menu(menu_id, parent_menu_id, menu_name, prompt_text)

        if not success:
            return send_json(
                {
                    "error": "Failed to create menu (check depth limit, circular references, or duplicate ID)"
                },
                400,
            ), 400

        # Generate voice prompt for the submenu if prompt_text provided
        if prompt_text:
            try:
                from pbx.features.auto_attendant import generate_submenu_prompt

                audio_path = aa.audio_path
                audio_file = generate_submenu_prompt(menu_id, prompt_text, audio_path)
                if audio_file:
                    aa.update_menu(menu_id, audio_file=audio_file)
                    logger.info(f"Generated voice prompt for menu '{menu_id}'")
            except Exception as e:
                logger.warning(f"Failed to generate voice prompt: {e}")

        return send_json(
            {
                "success": True,
                "message": f"Menu '{menu_id}' created successfully",
                "menu_id": menu_id,
            }
        ), 200
    except Exception as e:
        return send_json({"error": str(e)}, 500), 500


@features_bp.route("/api/auto-attendant/menus/<menu_id>", methods=["PUT"])
@require_auth
def update_menu(menu_id: str) -> tuple[Response, int]:
    """Update an existing menu."""
    pbx_core = get_pbx_core()
    if not pbx_core or not hasattr(pbx_core, "auto_attendant"):
        return send_json({"error": "Auto attendant not available"}, 500), 500

    try:
        data = get_request_body()
        menu_name = data.get("menu_name")
        prompt_text = data.get("prompt_text")

        aa = pbx_core.auto_attendant

        # Check if menu exists
        if not aa.get_menu(menu_id):
            return send_json({"error": f"Menu '{menu_id}' not found"}, 404), 404

        # Update menu
        success = aa.update_menu(menu_id, menu_name=menu_name, prompt_text=prompt_text)

        if not success:
            return send_json({"error": "Failed to update menu"}, 500), 500

        # Regenerate voice prompt if prompt_text changed
        if prompt_text:
            try:
                from pbx.features.auto_attendant import generate_submenu_prompt

                audio_path = aa.audio_path
                audio_file = generate_submenu_prompt(menu_id, prompt_text, audio_path)
                if audio_file:
                    aa.update_menu(menu_id, audio_file=audio_file)
                    logger.info(f"Regenerated voice prompt for menu '{menu_id}'")
            except Exception as e:
                logger.warning(f"Failed to regenerate voice prompt: {e}")

        return send_json(
            {"success": True, "message": f"Menu '{menu_id}' updated successfully"}
        ), 200
    except Exception as e:
        return send_json({"error": str(e)}, 500), 500


@features_bp.route("/api/auto-attendant/menus/<menu_id>", methods=["DELETE"])
@require_auth
def delete_menu(menu_id: str) -> tuple[Response, int]:
    """Delete a menu."""
    pbx_core = get_pbx_core()
    if not pbx_core or not hasattr(pbx_core, "auto_attendant"):
        return send_json({"error": "Auto attendant not available"}, 500), 500

    try:
        aa = pbx_core.auto_attendant
        success = aa.delete_menu(menu_id)

        if not success:
            return send_json(
                {
                    "error": "Failed to delete menu (cannot delete main menu or menu is referenced by other items)"
                },
                400,
            ), 400

        return send_json(
            {"success": True, "message": f"Menu '{menu_id}' deleted successfully"}
        ), 200
    except Exception as e:
        return send_json({"error": str(e)}, 500), 500


@features_bp.route("/api/auto-attendant/menus/<menu_id>/items", methods=["GET"])
@require_auth
def get_menu_items(menu_id: str) -> tuple[Response, int]:
    """Get menu items for a specific menu."""
    pbx_core = get_pbx_core()
    if not pbx_core or not hasattr(pbx_core, "auto_attendant"):
        return send_json({"error": "Auto attendant not available"}, 500), 500

    try:
        aa = pbx_core.auto_attendant

        # Check if menu exists
        if not aa.get_menu(menu_id):
            return send_json({"error": f"Menu '{menu_id}' not found"}, 404), 404

        items = aa.get_menu_items(menu_id)
        return send_json({"menu_id": menu_id, "items": items}), 200
    except Exception as e:
        return send_json({"error": str(e)}, 500), 500


@features_bp.route("/api/auto-attendant/menus/<menu_id>/items", methods=["POST"])
@require_auth
def add_menu_item(menu_id: str) -> tuple[Response, int]:
    """Add an item to a menu."""
    pbx_core = get_pbx_core()
    if not pbx_core or not hasattr(pbx_core, "auto_attendant"):
        return send_json({"error": "Auto attendant not available"}, 500), 500

    try:
        data = get_request_body()
        digit = data.get("digit")
        destination_type = data.get("destination_type")
        destination_value = data.get("destination_value")
        description = data.get("description", "")

        if not digit or not destination_type or not destination_value:
            return send_json(
                {"error": "digit, destination_type, and destination_value are required"}, 400
            ), 400

        aa = pbx_core.auto_attendant

        # Check if menu exists
        if not aa.get_menu(menu_id):
            return send_json({"error": f"Menu '{menu_id}' not found"}, 404), 404

        success = aa.add_menu_item(menu_id, digit, destination_type, destination_value, description)

        if not success:
            return send_json(
                {"error": "Failed to add menu item (check destination_type validity)"}, 400
            ), 400

        return send_json(
            {
                "success": True,
                "message": f"Menu item {digit} added to menu '{menu_id}' successfully",
            }
        ), 200
    except (KeyError, TypeError, ValueError) as e:
        return send_json({"error": str(e)}, 500), 500


@features_bp.route("/api/auto-attendant/menus/<menu_id>/items/<digit>", methods=["PUT"])
@require_auth
def update_menu_item(menu_id: str, digit: str) -> tuple[Response, int]:
    """Update a menu item."""
    pbx_core = get_pbx_core()
    if not pbx_core or not hasattr(pbx_core, "auto_attendant"):
        return send_json({"error": "Auto attendant not available"}, 500), 500

    try:
        data = get_request_body()
        destination_type = data.get("destination_type")
        destination_value = data.get("destination_value")
        description = data.get("description")

        if not destination_type and not destination_value and description is None:
            return send_json({"error": "At least one field must be provided to update"}, 400), 400

        aa = pbx_core.auto_attendant

        # Get current item to check if it exists
        current_items = aa.get_menu_items(menu_id)
        item_exists = any(item["digit"] == digit for item in current_items)

        if not item_exists:
            return send_json(
                {"error": f"Menu item {digit} not found in menu '{menu_id}'"}, 404
            ), 404

        # Get current values if not provided
        current_item = next(item for item in current_items if item["digit"] == digit)
        final_dest_type = destination_type or current_item["destination_type"]
        final_dest_value = destination_value or current_item["destination_value"]
        final_description = description if description is not None else current_item["description"]

        # Update (add_menu_item handles both insert and update)
        success = aa.add_menu_item(
            menu_id, digit, final_dest_type, final_dest_value, final_description
        )

        if not success:
            return send_json({"error": "Failed to update menu item"}, 500), 500

        return send_json(
            {
                "success": True,
                "message": f"Menu item {digit} in menu '{menu_id}' updated successfully",
            }
        ), 200
    except (KeyError, TypeError, ValueError) as e:
        return send_json({"error": str(e)}, 500), 500


@features_bp.route("/api/auto-attendant/menus/<menu_id>/items/<digit>", methods=["DELETE"])
@require_auth
def delete_menu_item(menu_id: str, digit: str) -> tuple[Response, int]:
    """Delete a menu item."""
    pbx_core = get_pbx_core()
    if not pbx_core or not hasattr(pbx_core, "auto_attendant"):
        return send_json({"error": "Auto attendant not available"}, 500), 500

    try:
        aa = pbx_core.auto_attendant
        success = aa.remove_menu_item(menu_id, digit)

        if not success:
            return send_json(
                {"error": f"Failed to delete menu item {digit} from menu '{menu_id}'"}, 500
            ), 500

        return send_json(
            {
                "success": True,
                "message": f"Menu item {digit} deleted from menu '{menu_id}' successfully",
            }
        ), 200
    except Exception as e:
        return send_json({"error": str(e)}, 500), 500


@features_bp.route("/api/auto-attendant/menu-tree", methods=["GET"])
@require_auth
def get_menu_tree() -> tuple[Response, int]:
    """Get complete menu hierarchy as a tree."""
    pbx_core = get_pbx_core()
    if not pbx_core or not hasattr(pbx_core, "auto_attendant"):
        return send_json({"error": "Auto attendant not available"}, 500), 500

    try:
        aa = pbx_core.auto_attendant
        tree = aa.get_menu_tree("main")

        if not tree:
            return send_json({"error": "Failed to build menu tree"}, 500), 500

        return send_json({"menu_tree": tree}), 200
    except Exception as e:
        return send_json({"error": str(e)}, 500), 500


def _regenerate_voice_prompts(
    pbx_core: Any, custom_prompts: dict[str, str] | None = None, company_name: str | None = None
) -> None:
    """Regenerate voice prompts using gTTS.

    Args:
        pbx_core: The PBX core instance.
        custom_prompts: Optional dict of custom prompt texts.
        company_name: Optional company name override.
    """
    try:
        from pbx.utils.tts import get_tts_requirements, is_tts_available, text_to_wav_telephony

        # Check if TTS is available
        if not is_tts_available():
            raise ImportError(
                f"TTS dependencies not available. Install with: {get_tts_requirements()}"
            )

        # Get configuration
        from pbx.utils.config import Config

        config = pbx_core.config if pbx_core else Config()
        aa_config = config.get("auto_attendant", {})

        if not company_name:
            company_name = config.get("company_name", "your company")

        # Get prompt texts
        default_prompts = {
            "welcome": f"Thank you for calling {company_name}.",
            "main_menu": "For Sales, press 1. For Support, press 2. For Accounting, press 3. Or press 0 to speak with an operator.",
            "invalid": "That is not a valid option. Please try again.",
            "timeout": "We did not receive your selection. Please try again.",
            "transferring": "Please hold while we transfer your call.",
        }

        # Merge custom prompts
        prompts = {**default_prompts}
        if custom_prompts:
            prompts.update(custom_prompts)

        # Substitute company name placeholder in case custom prompts still
        # contain the raw template (e.g. unedited text from the prompts GET endpoint)
        for filename, text in prompts.items():
            if "{company_name}" in text:
                prompts[filename] = text.replace("{company_name}", company_name)

        # Get output directory
        audio_path = aa_config.get("audio_path", "auto_attendant")
        if not Path(audio_path).exists():
            Path(audio_path).mkdir(parents=True, exist_ok=True)

        # Generate each prompt using shared TTS utility
        logger.info("Regenerating voice prompts using gTTS...")
        for filename, text in prompts.items():
            output_file = Path(audio_path) / f"{filename}.wav"

            try:
                # Use shared utility function for TTS generation with 8kHz
                # for PCMU
                if text_to_wav_telephony(
                    text, output_file, language="en", tld="com", slow=False, sample_rate=8000
                ):
                    logger.info(f"Generated {filename}.wav using gTTS")
            except Exception as e:
                logger.error(f"Failed to generate {filename}.wav: {e}")

        logger.info("Voice prompt regeneration complete")
    except Exception as e:
        logger.error(f"Error regenerating voice prompts: {e}")
        raise


# ==========================================================================
# SIP Trunks Routes
# ==========================================================================


@features_bp.route("/api/sip-trunks", methods=["GET"])
@require_auth
def get_sip_trunks() -> tuple[Response, int]:
    """Get all SIP trunks."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "trunk_system"):
        try:
            trunks = pbx_core.trunk_system.get_trunk_status()
            return send_json({"trunks": trunks, "count": len(trunks)}), 200
        except Exception as e:
            logger.error(f"Error getting SIP trunks: {e}")
            return send_json({"error": f"Error getting SIP trunks: {e!s}"}, 500), 500
    else:
        return send_json({"error": "SIP trunk system not initialized"}, 500), 500


@features_bp.route("/api/sip-trunks/health", methods=["GET"])
@require_auth
def get_trunk_health() -> tuple[Response, int]:
    """Get health status of all trunks."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "trunk_system"):
        try:
            health_data = []
            for trunk in pbx_core.trunk_system.trunks.values():
                health_metrics = trunk.get_health_metrics()
                health_metrics["trunk_id"] = trunk.trunk_id
                health_metrics["name"] = trunk.name
                health_data.append(health_metrics)

            return send_json(
                {
                    "health": health_data,
                    "monitoring_active": pbx_core.trunk_system.monitoring_active,
                    "failover_enabled": pbx_core.trunk_system.failover_enabled,
                }
            ), 200
        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"Error getting trunk health: {e}")
            return send_json({"error": f"Error getting trunk health: {e!s}"}, 500), 500
    else:
        return send_json({"error": "SIP trunk system not initialized"}, 500), 500


@features_bp.route("/api/sip-trunks", methods=["POST"])
@require_auth
def add_sip_trunk() -> tuple[Response, int]:
    """Add a new SIP trunk."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "trunk_system"):
        try:
            data = get_request_body()

            required_fields = ["trunk_id", "name", "host", "username", "password"]
            missing = [f for f in required_fields if f not in data]
            if missing:
                return send_json(
                    {"error": f"Missing required fields: {', '.join(missing)}"}, 400
                ), 400

            trunk_id = data["trunk_id"]
            if pbx_core.trunk_system.get_trunk(trunk_id):
                return send_json({"error": "Trunk already exists"}, 400), 400

            port = data.get("port", 5060)
            codec_preferences = data.get("codec_preferences", ["G.711", "G.729"])
            priority = data.get("priority", 100)
            max_channels = data.get("max_channels", 10)
            health_check_interval = data.get("health_check_interval", 60)

            # Try to persist to the database first, fall back to config.yml
            if pbx_core.trunk_db:
                success = pbx_core.trunk_db.add(
                    trunk_id=trunk_id,
                    name=data["name"],
                    host=data["host"],
                    username=data["username"],
                    password=data["password"],
                    port=port,
                    codec_preferences=codec_preferences,
                    priority=priority,
                    max_channels=max_channels,
                    health_check_interval=health_check_interval,
                )
            else:
                success = pbx_core.config.add_sip_trunk(
                    trunk_id=trunk_id,
                    name=data["name"],
                    host=data["host"],
                    username=data["username"],
                    password=data["password"],
                    port=port,
                    codec_preferences=codec_preferences,
                    priority=priority,
                    max_channels=max_channels,
                )

            if not success:
                return send_json({"error": "Failed to add SIP trunk"}, 500), 500

            # Reload trunks from the persisted source and register the new one
            pbx_core.trunk_system.reload_trunks()
            trunk = pbx_core.trunk_system.get_trunk(trunk_id)
            if trunk:
                trunk.register(pbx_core.sip_server)

            return send_json(
                {
                    "success": True,
                    "message": f"Trunk {data['name']} added successfully",
                    "trunk": trunk.to_dict() if trunk else None,
                }
            ), 200

        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"Error adding SIP trunk: {e}")
            return send_json({"error": f"Error adding SIP trunk: {e!s}"}, 500), 500
    else:
        return send_json({"error": "SIP trunk system not initialized"}, 500), 500


@features_bp.route("/api/sip-trunks/<trunk_id>", methods=["PUT"])
@require_auth
def update_sip_trunk(trunk_id: str) -> tuple[Response, int]:
    """Update an existing SIP trunk."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "trunk_system"):
        try:
            if not pbx_core.trunk_system.get_trunk(trunk_id):
                return send_json({"error": "Trunk not found"}, 404), 404

            data = get_request_body()

            # Try to persist to the database first, fall back to config.yml
            if pbx_core.trunk_db:
                success = pbx_core.trunk_db.update(
                    trunk_id=trunk_id,
                    name=data.get("name"),
                    host=data.get("host"),
                    username=data.get("username"),
                    password=data.get("password"),
                    port=data.get("port"),
                    codec_preferences=data.get("codec_preferences"),
                    priority=data.get("priority"),
                    max_channels=data.get("max_channels"),
                    health_check_interval=data.get("health_check_interval"),
                )
            else:
                success = pbx_core.config.update_sip_trunk(
                    trunk_id=trunk_id,
                    name=data.get("name"),
                    host=data.get("host"),
                    username=data.get("username"),
                    password=data.get("password"),
                    port=data.get("port"),
                    codec_preferences=data.get("codec_preferences"),
                    priority=data.get("priority"),
                    max_channels=data.get("max_channels"),
                )

            if not success:
                return send_json({"error": "Failed to update SIP trunk"}, 500), 500

            # Reload trunks from the persisted source and re-register the
            # updated trunk (its credentials/host may have changed)
            pbx_core.trunk_system.reload_trunks()
            trunk = pbx_core.trunk_system.get_trunk(trunk_id)
            if trunk:
                trunk.register(pbx_core.sip_server)

            return send_json(
                {
                    "success": True,
                    "message": f"Trunk {trunk_id} updated successfully",
                    "trunk": trunk.to_dict() if trunk else None,
                }
            ), 200

        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"Error updating SIP trunk: {e}")
            return send_json({"error": f"Error updating SIP trunk: {e!s}"}, 500), 500
    else:
        return send_json({"error": "SIP trunk system not initialized"}, 500), 500


@features_bp.route("/api/sip-trunks/test", methods=["POST"])
@require_auth
def test_sip_trunk() -> tuple[Response, int]:
    """Test a SIP trunk connection."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "trunk_system"):
        try:
            data = get_request_body()

            trunk_id = data.get("trunk_id")
            trunk = pbx_core.trunk_system.get_trunk(trunk_id)

            if trunk:
                # Perform health check
                health_status = trunk.check_health()

                return send_json(
                    {
                        "success": True,
                        "trunk_id": trunk_id,
                        "health_status": health_status.value,
                        "metrics": trunk.get_health_metrics(),
                    }
                ), 200
            return send_json({"error": "Trunk not found"}, 404), 404

        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"Error testing SIP trunk: {e}")
            return send_json({"error": f"Error testing SIP trunk: {e!s}"}, 500), 500
    else:
        return send_json({"error": "SIP trunk system not initialized"}, 500), 500


@features_bp.route("/api/sip-trunks/<trunk_id>", methods=["DELETE"])
@require_auth
def delete_sip_trunk(trunk_id: str) -> tuple[Response, int]:
    """Delete a SIP trunk."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "trunk_system"):
        try:
            trunk = pbx_core.trunk_system.get_trunk(trunk_id)
            if not trunk:
                return send_json({"error": "Trunk not found"}, 404), 404

            # Try to delete from the database first, fall back to config.yml
            if pbx_core.trunk_db:
                success = pbx_core.trunk_db.delete(trunk_id)
            else:
                success = pbx_core.config.delete_sip_trunk(trunk_id)

            if not success:
                return send_json({"error": "Failed to delete SIP trunk"}, 500), 500

            pbx_core.trunk_system.remove_trunk(trunk_id)
            return send_json(
                {"success": True, "message": f"Trunk {trunk_id} removed successfully"}
            ), 200

        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"Error deleting SIP trunk: {e}")
            return send_json({"error": f"Error deleting SIP trunk: {e!s}"}, 500), 500
    else:
        return send_json({"error": "SIP trunk system not initialized"}, 500), 500


# ==========================================================================
# Inbound DID Routing Routes
# ==========================================================================

_INBOUND_ROUTE_DESTINATION_TYPES = {"extension", "auto_attendant", "voicemail"}


@features_bp.route("/api/inbound-routes", methods=["GET"])
@require_auth
def get_inbound_routes() -> tuple[Response, int]:
    """Get the effective inbound DID routes (explicit routes plus extension-derived DIDs)."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "inbound_routing"):
        try:
            routes = pbx_core.inbound_routing.get_effective_routes()
            return send_json({"routes": routes, "count": len(routes)}), 200
        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"Error getting inbound routes: {e}")
            return send_json({"error": f"Error getting inbound routes: {e!s}"}, 500), 500
    else:
        return send_json({"error": "Inbound routing system not initialized"}, 500), 500


@features_bp.route("/api/inbound-routes", methods=["POST"])
@require_auth
def add_inbound_route() -> tuple[Response, int]:
    """Add a new inbound DID route."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "inbound_routing"):
        try:
            data = get_request_body()

            required_fields = ["did_number", "destination_type", "destination_value"]
            missing = [f for f in required_fields if not data.get(f)]
            if missing:
                return send_json(
                    {"error": f"Missing required fields: {', '.join(missing)}"}, 400
                ), 400

            destination_type = data["destination_type"]
            if destination_type not in _INBOUND_ROUTE_DESTINATION_TYPES:
                return send_json(
                    {
                        "error": "destination_type must be one of: "
                        + ", ".join(sorted(_INBOUND_ROUTE_DESTINATION_TYPES))
                    },
                    400,
                ), 400

            if not pbx_core.inbound_route_db:
                return send_json({"error": "Database not available"}, 500), 500

            success = pbx_core.inbound_route_db.add(
                did_number=data["did_number"],
                destination_type=destination_type,
                destination_value=data["destination_value"],
                trunk_id=data.get("trunk_id") or None,
                enabled=data.get("enabled", True),
                priority=data.get("priority", 100),
            )

            if not success:
                return send_json({"error": "Failed to add inbound route"}, 500), 500

            pbx_core.inbound_routing.reload_routes()
            return send_json(
                {"success": True, "message": f"Inbound route for {data['did_number']} added"}
            ), 200

        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"Error adding inbound route: {e}")
            return send_json({"error": f"Error adding inbound route: {e!s}"}, 500), 500
    else:
        return send_json({"error": "Inbound routing system not initialized"}, 500), 500


@features_bp.route("/api/inbound-routes/<int:route_id>", methods=["PUT"])
@require_auth
def update_inbound_route(route_id: int) -> tuple[Response, int]:
    """Update an existing inbound DID route."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "inbound_routing"):
        try:
            if not pbx_core.inbound_route_db or not pbx_core.inbound_route_db.get(route_id):
                return send_json({"error": "Inbound route not found"}, 404), 404

            data = get_request_body()

            destination_type = data.get("destination_type")
            if (
                destination_type is not None
                and destination_type not in _INBOUND_ROUTE_DESTINATION_TYPES
            ):
                return send_json(
                    {
                        "error": "destination_type must be one of: "
                        + ", ".join(sorted(_INBOUND_ROUTE_DESTINATION_TYPES))
                    },
                    400,
                ), 400

            success = pbx_core.inbound_route_db.update(
                route_id,
                did_number=data.get("did_number"),
                trunk_id=data.get("trunk_id"),
                destination_type=destination_type,
                destination_value=data.get("destination_value"),
                enabled=data.get("enabled"),
                priority=data.get("priority"),
            )

            if not success:
                return send_json({"error": "Failed to update inbound route"}, 500), 500

            pbx_core.inbound_routing.reload_routes()
            return send_json({"success": True, "message": f"Inbound route {route_id} updated"}), 200

        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"Error updating inbound route: {e}")
            return send_json({"error": f"Error updating inbound route: {e!s}"}, 500), 500
    else:
        return send_json({"error": "Inbound routing system not initialized"}, 500), 500


@features_bp.route("/api/inbound-routes/<int:route_id>", methods=["DELETE"])
@require_auth
def delete_inbound_route(route_id: int) -> tuple[Response, int]:
    """Delete an inbound DID route."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "inbound_routing"):
        try:
            if not pbx_core.inbound_route_db or not pbx_core.inbound_route_db.get(route_id):
                return send_json({"error": "Inbound route not found"}, 404), 404

            success = pbx_core.inbound_route_db.delete(route_id)
            if not success:
                return send_json({"error": "Failed to delete inbound route"}, 500), 500

            pbx_core.inbound_routing.reload_routes()
            return send_json({"success": True, "message": f"Inbound route {route_id} removed"}), 200

        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"Error deleting inbound route: {e}")
            return send_json({"error": f"Error deleting inbound route: {e!s}"}, 500), 500
    else:
        return send_json({"error": "Inbound routing system not initialized"}, 500), 500


# ==========================================================================
# LCR (Least-Cost Routing) Routes
# ==========================================================================


@features_bp.route("/api/lcr/rates", methods=["GET"])
@require_auth
def get_lcr_rates() -> tuple[Response, int]:
    """Get all LCR rates."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "lcr"):
        try:
            rates = [
                {
                    "trunk_id": rate_entry.trunk_id,
                    "pattern": rate_entry.pattern.pattern,
                    "description": rate_entry.pattern.description,
                    "rate_per_minute": rate_entry.rate_per_minute,
                    "connection_fee": rate_entry.connection_fee,
                    "minimum_seconds": rate_entry.minimum_seconds,
                    "billing_increment": rate_entry.billing_increment,
                }
                for rate_entry in pbx_core.lcr.rate_entries
            ]

            time_rates = [
                {
                    "name": time_rate.name,
                    "start_time": time_rate.start_time.strftime("%H:%M"),
                    "end_time": time_rate.end_time.strftime("%H:%M"),
                    "days_of_week": time_rate.days_of_week,
                    "rate_multiplier": time_rate.rate_multiplier,
                }
                for time_rate in pbx_core.lcr.time_based_rates
            ]

            return send_json({"rates": rates, "time_rates": time_rates, "count": len(rates)}), 200

        except Exception as e:
            logger.error(f"Error getting LCR rates: {e}")
            # Return empty rates instead of error to prevent UI errors
            return send_json({"rates": [], "time_rates": [], "count": 0}), 200
    else:
        # Return empty rates when LCR is not initialized
        return send_json({"rates": [], "time_rates": [], "count": 0}), 200


@features_bp.route("/api/lcr/statistics", methods=["GET"])
@require_auth
def get_lcr_statistics() -> tuple[Response, int]:
    """Get LCR statistics."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "lcr"):
        try:
            stats = pbx_core.lcr.get_statistics()
            return send_json(stats), 200

        except Exception as e:
            logger.error(f"Error getting LCR statistics: {e}")
            # Return empty statistics instead of error to prevent UI errors
            return send_json(
                {"total_calls": 0, "total_cost": 0.0, "total_savings": 0.0, "routes_by_trunk": {}}
            ), 200
    else:
        # Return empty statistics when LCR is not initialized
        return send_json(
            {"total_calls": 0, "total_cost": 0.0, "total_savings": 0.0, "routes_by_trunk": {}}
        ), 200


@features_bp.route("/api/lcr/rate", methods=["POST"])
@require_auth
def add_lcr_rate() -> tuple[Response, int]:
    """Add a new LCR rate."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "lcr"):
        try:
            data = get_request_body()

            pbx_core.lcr.add_rate(
                trunk_id=data["trunk_id"],
                pattern=data["pattern"],
                rate_per_minute=float(data["rate_per_minute"]),
                description=data.get("description", ""),
                connection_fee=float(data.get("connection_fee", 0.0)),
                minimum_seconds=int(data.get("minimum_seconds", 0)),
                billing_increment=int(data.get("billing_increment", 1)),
            )

            return send_json({"success": True, "message": "LCR rate added successfully"}), 200

        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"Error adding LCR rate: {e}")
            return send_json({"error": f"Error adding LCR rate: {e!s}"}, 500), 500
    else:
        return send_json({"error": "LCR system not initialized"}, 500), 500


@features_bp.route("/api/lcr/time-rate", methods=["POST"])
@require_auth
def add_lcr_time_rate() -> tuple[Response, int]:
    """Add a time-based rate modifier."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "lcr"):
        try:
            data = get_request_body()

            pbx_core.lcr.add_time_based_rate(
                name=data["name"],
                start_hour=int(data["start_hour"]),
                start_minute=int(data["start_minute"]),
                end_hour=int(data["end_hour"]),
                end_minute=int(data["end_minute"]),
                days=data["days"],  # list of day indices
                multiplier=float(data["multiplier"]),
            )

            return send_json(
                {"success": True, "message": "Time-based rate added successfully"}
            ), 200

        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"Error adding time-based rate: {e}")
            return send_json({"error": f"Error adding time-based rate: {e!s}"}, 500), 500
    else:
        return send_json({"error": "LCR system not initialized"}, 500), 500


@features_bp.route("/api/lcr/clear-rates", methods=["POST"])
@require_auth
def clear_lcr_rates() -> tuple[Response, int]:
    """Clear all LCR rates."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "lcr"):
        try:
            pbx_core.lcr.clear_rates()
            return send_json(
                {"success": True, "message": "All LCR rates cleared successfully"}
            ), 200

        except Exception as e:
            logger.error(f"Error clearing LCR rates: {e}")
            return send_json({"error": f"Error clearing LCR rates: {e!s}"}, 500), 500
    else:
        return send_json({"error": "LCR system not initialized"}, 500), 500


@features_bp.route("/api/lcr/clear-time-rates", methods=["POST"])
@require_auth
def clear_lcr_time_rates() -> tuple[Response, int]:
    """Clear all time-based rates."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "lcr"):
        try:
            pbx_core.lcr.clear_time_rates()
            return send_json(
                {"success": True, "message": "All time-based rates cleared successfully"}
            ), 200

        except Exception as e:
            logger.error(f"Error clearing time-based rates: {e}")
            return send_json({"error": f"Error clearing time-based rates: {e!s}"}, 500), 500
    else:
        return send_json({"error": "LCR system not initialized"}, 500), 500


# ==========================================================================
# FMFM (Find Me/Follow Me) Routes
# ==========================================================================


@features_bp.route("/api/fmfm/extensions", methods=["GET"])
@require_auth
def get_fmfm_extensions() -> tuple[Response, int]:
    """Get all extensions with FMFM configured."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "find_me_follow_me"):
        try:
            extensions = pbx_core.find_me_follow_me.list_extensions_with_fmfm()
            configs = []
            for ext in extensions:
                config = pbx_core.find_me_follow_me.get_config(ext)
                if config:
                    configs.append(config)

            return send_json({"extensions": configs, "count": len(configs)}), 200
        except Exception as e:
            logger.error(f"Error getting FMFM extensions: {e}")
            return send_json({"error": f"Error getting FMFM extensions: {e!s}"}, 500), 500
    else:
        return send_json({"error": "Find Me/Follow Me not initialized"}, 500), 500


@features_bp.route("/api/fmfm/config/<extension>", methods=["GET"])
@require_auth
def get_fmfm_config(extension: str) -> tuple[Response, int]:
    """Get FMFM configuration for an extension."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "find_me_follow_me"):
        try:
            config = pbx_core.find_me_follow_me.get_config(extension)
            if config:
                return send_json(config), 200
            return send_json(
                {
                    "extension": extension,
                    "enabled": False,
                    "message": "No FMFM configuration found",
                }
            ), 200
        except Exception as e:
            logger.error(f"Error getting FMFM config for {extension}: {e}")
            return send_json({"error": f"Error getting FMFM config: {e!s}"}, 500), 500
    else:
        return send_json({"error": "Find Me/Follow Me not initialized"}, 500), 500


@features_bp.route("/api/fmfm/statistics", methods=["GET"])
@require_auth
def get_fmfm_statistics() -> tuple[Response, int]:
    """Get FMFM statistics."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "find_me_follow_me"):
        try:
            stats = pbx_core.find_me_follow_me.get_statistics()
            return send_json(stats), 200
        except Exception as e:
            logger.error(f"Error getting FMFM statistics: {e}")
            return send_json({"error": f"Error getting FMFM statistics: {e!s}"}, 500), 500
    else:
        return send_json({"error": "Find Me/Follow Me not initialized"}, 500), 500


@features_bp.route("/api/fmfm/config", methods=["POST"])
@require_auth
def set_fmfm_config() -> tuple[Response, int]:
    """set FMFM configuration for an extension."""
    logger.info("Received FMFM config save request")

    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "find_me_follow_me"):
        try:
            data = get_request_body()

            logger.info(f"FMFM config data: {data}")

            extension = data.get("extension")
            if not extension:
                logger.warning("FMFM config request missing extension")
                return send_json({"error": "Extension required"}, 400), 400

            success = pbx_core.find_me_follow_me.set_config(extension, data)

            if success:
                logger.info(f"Successfully configured FMFM for extension {extension}")
                return send_json(
                    {
                        "success": True,
                        "message": f"FMFM configured for extension {extension}",
                        "config": pbx_core.find_me_follow_me.get_config(extension),
                    }
                ), 200
            logger.error(f"Failed to set FMFM configuration for extension {extension}")
            return send_json({"error": "Failed to set FMFM configuration"}, 500), 500

        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"Error setting FMFM config: {e}")
            return send_json({"error": f"Error setting FMFM config: {e!s}"}, 500), 500
    else:
        logger.error("Find Me/Follow Me not initialized")
        return send_json({"error": "Find Me/Follow Me not initialized"}, 500), 500


@features_bp.route("/api/fmfm/destination", methods=["POST"])
@require_auth
def add_fmfm_destination() -> tuple[Response, int]:
    """Add a destination to FMFM config."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "find_me_follow_me"):
        try:
            data = get_request_body()

            extension = data.get("extension")
            number = data.get("number")
            ring_time = data.get("ring_time", 20)

            if not extension or not number:
                return send_json({"error": "Extension and number required"}, 400), 400

            success = pbx_core.find_me_follow_me.add_destination(extension, number, ring_time)

            if success:
                return send_json(
                    {
                        "success": True,
                        "message": f"Destination {number} added to {extension}",
                        "config": pbx_core.find_me_follow_me.get_config(extension),
                    }
                ), 200
            return send_json({"error": "Failed to add destination"}, 500), 500

        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"Error adding FMFM destination: {e}")
            return send_json({"error": f"Error adding FMFM destination: {e!s}"}, 500), 500
    else:
        return send_json({"error": "Find Me/Follow Me not initialized"}, 500), 500


@features_bp.route("/api/fmfm/destination/<extension>/<number>", methods=["DELETE"])
@require_auth
def remove_fmfm_destination(extension: str, number: str) -> tuple[Response, int]:
    """Remove a destination from FMFM config."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "find_me_follow_me"):
        try:
            success = pbx_core.find_me_follow_me.remove_destination(extension, number)

            if success:
                return send_json(
                    {
                        "success": True,
                        "message": f"Destination {number} removed from {extension}",
                    }
                ), 200
            return send_json({"error": "Failed to remove destination"}, 404), 404

        except Exception as e:
            logger.error(f"Error removing FMFM destination: {e}")
            return send_json({"error": f"Error removing FMFM destination: {e!s}"}, 500), 500
    else:
        return send_json({"error": "Find Me/Follow Me not initialized"}, 500), 500


@features_bp.route("/api/fmfm/config/<extension>", methods=["DELETE"])
@require_auth
def disable_fmfm(extension: str) -> tuple[Response, int]:
    """Delete FMFM configuration for an extension."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "find_me_follow_me"):
        try:
            success = pbx_core.find_me_follow_me.delete_config(extension)

            if success:
                return send_json(
                    {
                        "success": True,
                        "message": f"FMFM configuration deleted for extension {extension}",
                    }
                ), 200
            return send_json({"error": "FMFM configuration not found"}, 404), 404

        except Exception as e:
            logger.error(f"Error deleting FMFM config: {e}")
            return send_json({"error": f"Error deleting FMFM config: {e!s}"}, 500), 500
    else:
        return send_json({"error": "Find Me/Follow Me not initialized"}, 500), 500


# ==========================================================================
# Time-Based Routing Routes
# ==========================================================================


@features_bp.route("/api/time-routing/rules", methods=["GET"])
@require_auth
def get_time_routing_rules() -> tuple[Response, int]:
    """Get all time-based routing rules."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "time_based_routing"):
        try:
            # Parse query parameters for filtering
            destination = request.args.get("destination")

            rules = pbx_core.time_based_routing.list_rules(destination=destination)
            return send_json({"rules": rules, "count": len(rules)}), 200
        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"Error getting time routing rules: {e}")
            return send_json({"error": f"Error getting time routing rules: {e!s}"}, 500), 500
    else:
        return send_json({"error": "Time-based routing not initialized"}, 500), 500


@features_bp.route("/api/time-routing/statistics", methods=["GET"])
@require_auth
def get_time_routing_statistics() -> tuple[Response, int]:
    """Get time-based routing statistics."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "time_based_routing"):
        try:
            stats = pbx_core.time_based_routing.get_statistics()
            return send_json(stats), 200
        except Exception as e:
            logger.error(f"Error getting time routing statistics: {e}")
            return send_json({"error": f"Error getting time routing statistics: {e!s}"}, 500), 500
    else:
        return send_json({"error": "Time-based routing not initialized"}, 500), 500


@features_bp.route("/api/time-routing/rule", methods=["POST"])
@require_auth
def add_time_routing_rule() -> tuple[Response, int]:
    """Add a time-based routing rule."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "time_based_routing"):
        try:
            data = get_request_body()

            # Validate required fields
            required_fields = ["name", "destination", "route_to", "time_conditions"]
            if not all(field in data for field in required_fields):
                return send_json({"error": "Missing required fields"}, 400), 400

            rule_id = pbx_core.time_based_routing.add_rule(data)

            if rule_id:
                return send_json(
                    {
                        "success": True,
                        "rule_id": rule_id,
                        "message": f'Time routing rule "{data["name"]}" added successfully',
                    }
                ), 200
            return send_json({"error": "Failed to add time routing rule"}, 500), 500

        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"Error adding time routing rule: {e}")
            return send_json({"error": f"Error adding time routing rule: {e!s}"}, 500), 500
    else:
        return send_json({"error": "Time-based routing not initialized"}, 500), 500


@features_bp.route("/api/time-routing/rule/<rule_id>", methods=["DELETE"])
@require_auth
def delete_time_routing_rule(rule_id: str) -> tuple[Response, int]:
    """Delete a time-based routing rule."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "time_based_routing"):
        try:
            success = pbx_core.time_based_routing.delete_rule(rule_id)

            if success:
                return send_json(
                    {"success": True, "message": f"Time routing rule {rule_id} deleted"}
                ), 200
            return send_json({"error": "Rule not found"}, 404), 404

        except Exception as e:
            logger.error(f"Error deleting time routing rule: {e}")
            return send_json({"error": f"Error deleting time routing rule: {e!s}"}, 500), 500
    else:
        return send_json({"error": "Time-based routing not initialized"}, 500), 500


# ==========================================================================
# Recording Retention Routes
# ==========================================================================


def _current_username() -> str:
    """
    Who is acting, for the audit trail. The session token identifies users by extension.

    Falls back to "unknown" rather than raising: an unattributable hold is still better than
    a hold that failed to be placed because the token lacked a display name.
    """
    _, payload = verify_authentication()
    if not payload:
        return "unknown"
    return str(payload.get("name") or payload.get("extension") or "unknown")


def _audit_hold(action: str, user: str, session_id: str, details: dict) -> None:
    """
    Record a hold change. Never raises into the request.

    Holds exist to satisfy a legal obligation, so who placed or lifted one is the part that
    makes the mechanism worth anything -- but a failing audit sink must not block the hold.
    """
    try:
        get_audit_logger().log_action(
            action=action,
            user=user,
            resource="retention_hold",
            resource_id=session_id,
            details=details,
            ip_address=request.remote_addr,
        )
    except Exception as e:
        logger.error(f"Could not audit {action} for session {session_id}: {e}")


@features_bp.route("/api/recording-retention/policies", methods=["GET"])
@require_auth
def get_retention_policies() -> tuple[Response, int]:
    """Get all retention policies, in the order the sweeper resolves them."""
    pbx_core = get_pbx_core()
    if not (pbx_core and hasattr(pbx_core, "recording_retention")):
        return send_json({"error": "Recording retention not initialized"}, 500), 500

    try:
        retention = pbx_core.recording_retention
        policies = [
            {
                "policy_id": p.policy_id,
                "name": p.name,
                "description": p.description,
                # Null means "inherit the fallback", which the UI has to render as such
                # rather than as zero days.
                "audio_days": p.audio_days,
                "transcript_days": p.transcript_days,
                "priority": p.priority,
                "match_rules": p.match_rules,
                "enabled": p.enabled,
                "origin": p.origin,
                "catch_all": p.is_catch_all,
            }
            for p in retention.policies.all()
        ]
        return send_json(
            {
                "policies": policies,
                "count": len(policies),
                "fallback_audio_days": retention.settings.audio_days,
                "fallback_transcript_days": retention.settings.transcript_days,
            }
        ), 200
    except (AttributeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"Error getting retention policies: {e}")
        return send_json({"error": "Error getting retention policies"}, 500), 500


@features_bp.route("/api/recording-retention/statistics", methods=["GET"])
@require_auth
def get_retention_statistics() -> tuple[Response, int]:
    """Get retention statistics."""
    pbx_core = get_pbx_core()
    if not (pbx_core and hasattr(pbx_core, "recording_retention")):
        return send_json({"error": "Recording retention not initialized"}, 500), 500

    try:
        stats = pbx_core.recording_retention.statistics()
        return send_json(
            {
                "total_policies": stats["policies"],
                "total_recordings": stats["managed_recordings"],
                "deleted_count": stats["lifetime_audio_deleted"],
                "transcripts_deleted": stats["lifetime_transcripts_deleted"],
                "last_cleanup": stats["last_sweep"],
                "last_sweep_summary": stats["last_sweep_summary"],
                "active_holds": stats["active_holds"],
                # The UI must surface these two. Without them "0 deleted, never cleaned"
                # reads as "retention is working and had nothing to do", when it usually
                # means retention is disabled or still in report-only mode.
                "enabled": stats["enabled"],
                "dry_run": stats["dry_run"],
                "fallback_audio_days": stats["fallback_audio_days"],
                "fallback_transcript_days": stats["fallback_transcript_days"],
            }
        ), 200
    except (AttributeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"Error getting retention statistics: {e}")
        return send_json({"error": "Error getting retention statistics"}, 500), 500


@features_bp.route("/api/recording-retention/policy", methods=["POST"])
@require_auth
def add_retention_policy() -> tuple[Response, int]:
    """Create or update a retention policy."""
    pbx_core = get_pbx_core()
    if not (pbx_core and hasattr(pbx_core, "recording_retention")):
        return send_json({"error": "Recording retention not initialized"}, 500), 500

    try:
        data = get_request_body()

        if "name" not in data:
            return send_json({"error": "Missing required field: name"}, 400), 400

        if not re.match(r"^[a-zA-Z0-9_\-\s]+$", str(data["name"])):
            return send_json({"error": "Policy name contains invalid characters"}, 400), 400

        # Both periods are optional and both may be null, meaning "inherit the fallback".
        # A policy that only lengthens audio retention leaves transcript_days unset, and
        # that must not be stored as zero.
        periods: dict[str, int | None] = {}
        for key in ("audio_days", "transcript_days"):
            raw = data.get(key)
            if raw is None or raw == "":
                periods[key] = None
                continue
            try:
                days = int(raw)
            except (TypeError, ValueError):
                return send_json({"error": f"{key} must be a valid integer"}, 400), 400
            if days < 1 or days > 3650:
                return send_json({"error": f"{key} must be between 1 and 3650"}, 400), 400
            periods[key] = days

        if periods["audio_days"] is None and periods["transcript_days"] is None:
            return send_json(
                {"error": "Set at least one of audio_days or transcript_days"}, 400
            ), 400

        rules = data.get("match_rules") or {}
        problems = validate_match_rules(rules)
        if problems:
            return send_json({"error": "; ".join(problems)}, 400), 400

        policy_id = str(data.get("policy_id") or data["name"]).strip()
        if not re.match(r"^[a-zA-Z0-9_\-]+$", policy_id.replace(" ", "_")):
            return send_json({"error": "policy_id contains invalid characters"}, 400), 400

        policy_id = policy_id.replace(" ", "_").lower()
        # Editing keeps whatever origin the row already had, so the seeded catch-all stays
        # marked as config-seeded rather than being relabelled user-created on first edit.
        existing = pbx_core.recording_retention.policies.get(policy_id)

        policy = RetentionPolicy(
            policy_id=policy_id,
            name=str(data["name"]),
            description=str(data.get("description") or ""),
            audio_days=periods["audio_days"],
            transcript_days=periods["transcript_days"],
            priority=int(data.get("priority", 100)),
            match_rules=rules,
            enabled=bool(data.get("enabled", True)),
            origin=existing.origin if existing else "api",
        )

        if not pbx_core.recording_retention.policies.save(policy):
            return send_json({"error": "Failed to save retention policy"}, 500), 500

        return send_json(
            {
                "success": True,
                "policy_id": policy.policy_id,
                "message": f'Retention policy "{policy.name}" saved',
            }
        ), 200

    except json.JSONDecodeError:
        return send_json({"error": "Invalid JSON"}, 400), 400
    except (AttributeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"Error adding retention policy: {e}")
        return send_json({"error": "Error adding retention policy"}, 500), 500


@features_bp.route("/api/recording-retention/policy/<policy_id>", methods=["DELETE"])
@require_auth
def delete_retention_policy(policy_id: str) -> tuple[Response, int]:
    """Delete a retention policy."""
    pbx_core = get_pbx_core()
    if not (pbx_core and hasattr(pbx_core, "recording_retention")):
        return send_json({"error": "Recording retention not initialized"}, 500), 500

    if policy_id == SEED_POLICY_ID:
        # It governs everything no other policy matches. Deleting it would silently hand that
        # job back to config.yml, which this page never shows -- edit it instead.
        return send_json(
            {"error": "The default policy cannot be deleted. Edit its periods instead."}, 403
        ), 403

    try:
        if pbx_core.recording_retention.policies.delete(policy_id):
            return send_json(
                {"success": True, "message": f"Retention policy {policy_id} deleted"}
            ), 200
        return send_json({"error": "Policy not found"}, 404), 404
    except Exception as e:
        logger.error(f"Error deleting retention policy: {e}")
        return send_json({"error": "Error deleting retention policy"}, 500), 500


@features_bp.route("/api/recording-retention/holds", methods=["GET"])
@require_auth
def get_retention_holds() -> tuple[Response, int]:
    """List legal holds. Released ones are included only on request."""
    pbx_core = get_pbx_core()
    if not (pbx_core and hasattr(pbx_core, "recording_retention")):
        return send_json({"error": "Recording retention not initialized"}, 500), 500

    try:
        include_released = request.args.get("include_released", "").lower() in ("1", "true")
        holds = pbx_core.recording_retention.holds.list_holds(include_released)
        return send_json({"holds": holds, "count": len(holds)}), 200
    except (AttributeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"Error getting retention holds: {e}")
        return send_json({"error": "Error getting retention holds"}, 500), 500


@features_bp.route("/api/recording-retention/hold", methods=["POST"])
@require_auth
def place_retention_hold() -> tuple[Response, int]:
    """
    Put a call session beyond the reach of the sweeper until it is released.

    A hold is not a long retention period: it suspends expiry entirely and has to be lifted
    deliberately. Both the reason and the person are required, because a hold nobody can
    explain is one nobody will ever dare release.
    """
    pbx_core = get_pbx_core()
    if not (pbx_core and hasattr(pbx_core, "recording_retention")):
        return send_json({"error": "Recording retention not initialized"}, 500), 500

    try:
        data = get_request_body()
        session_id = str(data.get("session_id") or "").strip()
        reason = str(data.get("reason") or "").strip()

        if not session_id:
            return send_json({"error": "Missing required field: session_id"}, 400), 400
        if not reason:
            return send_json({"error": "A hold requires a reason"}, 400), 400

        placed_by = _current_username()
        if not pbx_core.recording_retention.holds.place(session_id, reason, placed_by):
            return send_json({"error": "Failed to place hold"}, 500), 500

        _audit_hold("retention_hold_placed", placed_by, session_id, {"reason": reason})

        return send_json({"success": True, "session_id": session_id, "placed_by": placed_by}), 200
    except json.JSONDecodeError:
        return send_json({"error": "Invalid JSON"}, 400), 400
    except (AttributeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"Error placing retention hold: {e}")
        return send_json({"error": "Error placing retention hold"}, 500), 500


@features_bp.route("/api/recording-retention/hold/<session_id>", methods=["DELETE"])
@require_auth
def release_retention_hold(session_id: str) -> tuple[Response, int]:
    """Release every active hold on a session. The rows are stamped, not deleted."""
    pbx_core = get_pbx_core()
    if not (pbx_core and hasattr(pbx_core, "recording_retention")):
        return send_json({"error": "Recording retention not initialized"}, 500), 500

    try:
        released_by = _current_username()
        if not pbx_core.recording_retention.holds.release(session_id, released_by):
            return send_json({"error": "Failed to release hold"}, 500), 500

        _audit_hold("retention_hold_released", released_by, session_id, {})

        return send_json(
            {"success": True, "session_id": session_id, "released_by": released_by}
        ), 200
    except (AttributeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"Error releasing retention hold: {e}")
        return send_json({"error": "Error releasing retention hold"}, 500), 500


# ==========================================================================
# Fraud Detection Routes
# ==========================================================================


@features_bp.route("/api/fraud-detection/alerts", methods=["GET"])
@require_auth
def get_fraud_alerts() -> tuple[Response, int]:
    """Get fraud detection alerts."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "fraud_detection"):
        try:
            # Parse query parameters
            extension = request.args.get("extension")
            # Note: backend get_alerts uses 'hours' parameter, not 'limit'
            hours = int(request.args.get("hours", 24))

            # Validate hours
            hours = min(hours, 720)  # Max 30 days

            alerts = pbx_core.fraud_detection.get_alerts(extension=extension, hours=hours)

            return send_json({"alerts": alerts, "count": len(alerts)}), 200
        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"Error getting fraud alerts: {e}")
            return send_json({"error": "Error getting fraud alerts"}, 500), 500
    else:
        return send_json({"error": "Fraud detection not initialized"}, 500), 500


@features_bp.route("/api/fraud-detection/statistics", methods=["GET"])
@require_auth
def get_fraud_statistics() -> tuple[Response, int]:
    """Get fraud detection statistics."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "fraud_detection"):
        try:
            stats = pbx_core.fraud_detection.get_statistics()

            # Transform to match frontend expectations
            result = {
                "total_alerts": stats.get("total_alerts", 0),
                "high_risk_alerts": sum(
                    1 for a in pbx_core.fraud_detection.alerts if a.get("fraud_score", 0) > 0.7
                ),
                "blocked_patterns_count": stats.get("blocked_patterns", 0),
                "extensions_flagged": stats.get("total_extensions_tracked", 0),
                "alerts_24h": stats.get("alerts_24h", 0),
                "blocked_patterns": pbx_core.fraud_detection.blocked_patterns,
            }

            return send_json(result), 200
        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"Error getting fraud statistics: {e}")
            return send_json({"error": "Error getting fraud statistics"}, 500), 500
    else:
        return send_json({"error": "Fraud detection not initialized"}, 500), 500


@features_bp.route("/api/fraud-detection/extension/<extension>", methods=["GET"])
@require_auth
def get_fraud_extension_stats(extension: str) -> tuple[Response, int]:
    """Get fraud statistics for a specific extension."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "fraud_detection"):
        try:
            # Validate extension format
            if not re.match(r"^\d{3,5}$", extension):
                return send_json({"error": "Invalid extension format"}, 400), 400

            stats = pbx_core.fraud_detection.get_extension_statistics(extension)
            return send_json(stats), 200
        except Exception as e:
            logger.error(f"Error getting extension fraud stats: {e}")
            return send_json({"error": "Error getting extension statistics"}, 500), 500
    else:
        return send_json({"error": "Fraud detection not initialized"}, 500), 500


@features_bp.route("/api/fraud-detection/blocked-pattern", methods=["POST"])
@require_auth
def add_blocked_pattern() -> tuple[Response, int]:
    """Add a blocked number pattern."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "fraud_detection"):
        try:
            data = get_request_body()

            # Validate required fields
            if "pattern" not in data or "reason" not in data:
                return send_json({"error": "Missing required fields: pattern, reason"}, 400), 400

            # Validate pattern is a valid regex (prevent ReDoS)
            try:
                re.compile(data["pattern"])
            except re.error:
                return send_json({"error": "Invalid regex pattern"}, 400), 400

            # Sanitize reason
            reason = str(data["reason"])[:200]  # Limit length

            success = pbx_core.fraud_detection.add_blocked_pattern(data["pattern"], reason)

            if success:
                return send_json(
                    {"success": True, "message": "Blocked pattern added successfully"}
                ), 200
            return send_json({"error": "Failed to add blocked pattern"}, 500), 500

        except json.JSONDecodeError:
            return send_json({"error": "Invalid JSON"}, 400), 400
        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"Error adding blocked pattern: {e}")
            return send_json({"error": "Error adding blocked pattern"}, 500), 500
    else:
        return send_json({"error": "Fraud detection not initialized"}, 500), 500


@features_bp.route("/api/fraud-detection/blocked-pattern/<pattern_id>", methods=["DELETE"])
@require_auth
def delete_blocked_pattern(pattern_id: str) -> tuple[Response, int]:
    """Delete a blocked pattern."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "fraud_detection"):
        try:
            # Find and remove pattern by ID/index
            try:
                index = int(pattern_id)
                if 0 <= index < len(pbx_core.fraud_detection.blocked_patterns):
                    del pbx_core.fraud_detection.blocked_patterns[index]
                    return send_json({"success": True, "message": "Blocked pattern deleted"}), 200
                return send_json({"error": "Pattern not found"}, 404), 404
            except (ValueError, IndexError):
                return send_json({"error": "Invalid pattern ID"}, 400), 400

        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"Error deleting blocked pattern: {e}")
            return send_json({"error": "Error deleting blocked pattern"}, 500), 500
    else:
        return send_json({"error": "Fraud detection not initialized"}, 500), 500


# ==========================================================================
# Callback Queue Routes
# ==========================================================================


@features_bp.route("/api/callback-queue/statistics", methods=["GET"])
@require_auth
def get_callback_statistics() -> tuple[Response, int]:
    """Get callback queue statistics."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "callback_queue"):
        try:
            stats = pbx_core.callback_queue.get_statistics()
            return send_json(stats), 200
        except Exception as e:
            logger.error(f"Error getting callback statistics: {e}")
            return send_json({"error": "Error getting callback statistics"}, 500), 500
    else:
        return send_json({"error": "Callback queue not initialized"}, 500), 500


@features_bp.route("/api/callback-queue/list", methods=["GET"])
@require_auth
def get_callback_list() -> tuple[Response, int]:
    """Get list of all callbacks."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "callback_queue"):
        try:
            callbacks = []
            for callback_id in pbx_core.callback_queue.callbacks:
                info = pbx_core.callback_queue.get_callback_info(callback_id)
                if info:
                    callbacks.append(info)

            # Sort by requested_at descending (most recent first)
            callbacks.sort(key=lambda x: x.get("requested_at", ""), reverse=True)

            return send_json({"callbacks": callbacks}), 200
        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"Error getting callback list: {e}")
            return send_json({"error": "Error getting callback list"}, 500), 500
    else:
        return send_json({"error": "Callback queue not initialized"}, 500), 500


@features_bp.route("/api/callback-queue/queue/<queue_id>", methods=["GET"])
@require_auth
def get_queue_callbacks(queue_id: str) -> tuple[Response, int]:
    """Get callbacks for a specific queue."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "callback_queue"):
        try:
            # Sanitize queue_id
            if not re.match(r"^[\w-]{1,50}$", queue_id):
                return send_json({"error": "Invalid queue_id format"}, 400), 400

            callbacks = pbx_core.callback_queue.list_queue_callbacks(queue_id)
            stats = pbx_core.callback_queue.get_queue_statistics(queue_id)

            return send_json(
                {"queue_id": queue_id, "callbacks": callbacks, "statistics": stats}
            ), 200
        except Exception as e:
            logger.error(f"Error getting queue callbacks: {e}")
            return send_json({"error": "Error getting queue callbacks"}, 500), 500
    else:
        return send_json({"error": "Callback queue not initialized"}, 500), 500


@features_bp.route("/api/callback-queue/info/<callback_id>", methods=["GET"])
@require_auth
def get_callback_info(callback_id: str) -> tuple[Response, int]:
    """Get information about a specific callback."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "callback_queue"):
        try:
            # Sanitize callback_id
            if not re.match(r"^cb_[\w]{1,100}$", callback_id):
                return send_json({"error": "Invalid callback_id format"}, 400), 400

            info = pbx_core.callback_queue.get_callback_info(callback_id)

            if info:
                return send_json(info), 200
            return send_json({"error": "Callback not found"}, 404), 404
        except Exception as e:
            logger.error(f"Error getting callback info: {e}")
            return send_json({"error": "Error getting callback info"}, 500), 500
    else:
        return send_json({"error": "Callback queue not initialized"}, 500), 500


@features_bp.route("/api/callback-queue/request", methods=["POST"])
@require_auth
def request_callback() -> tuple[Response, int]:
    """Request a callback from queue."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "callback_queue"):
        try:
            data = get_request_body()

            # Validate required fields
            if "queue_id" not in data or "caller_number" not in data:
                return send_json(
                    {"error": "Missing required fields: queue_id, caller_number"}, 400
                ), 400

            # Sanitize inputs
            queue_id = str(data["queue_id"])[:50]
            caller_number = str(data["caller_number"])[:50]
            caller_name = (
                str(data.get("caller_name", ""))[:100] if data.get("caller_name") else None
            )

            # Parse preferred_time if provided
            preferred_time = None
            if "preferred_time" in data:
                try:
                    from datetime import datetime

                    preferred_time = datetime.fromisoformat(data["preferred_time"])
                except (ValueError, TypeError):
                    return send_json(
                        {"error": "Invalid preferred_time format. Use ISO 8601 format."}, 400
                    ), 400

            result = pbx_core.callback_queue.request_callback(
                queue_id, caller_number, caller_name, preferred_time
            )

            if "error" in result:
                return send_json(result, 400), 400
            return send_json({"success": True, **result}), 200
        except json.JSONDecodeError:
            return send_json({"error": "Invalid JSON"}, 400), 400
        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"Error requesting callback: {e}")
            return send_json({"error": "Error requesting callback"}, 500), 500
    else:
        return send_json({"error": "Callback queue not initialized"}, 500), 500


@features_bp.route("/api/callback-queue/start", methods=["POST"])
@require_auth
def start_callback() -> tuple[Response, int]:
    """Start processing a callback."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "callback_queue"):
        try:
            data = get_request_body()

            if "callback_id" not in data or "agent_id" not in data:
                return send_json(
                    {"error": "Missing required fields: callback_id, agent_id"}, 400
                ), 400

            callback_id = str(data["callback_id"])[:100]
            agent_id = str(data["agent_id"])[:50]

            result = pbx_core.callback_queue.start_callback(callback_id, agent_id)

            if "error" in result:
                return send_json(result, 404), 404
            return send_json({"success": True, **result}), 200
        except json.JSONDecodeError:
            return send_json({"error": "Invalid JSON"}, 400), 400
        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"Error starting callback: {e}")
            return send_json({"error": "Error starting callback"}, 500), 500
    else:
        return send_json({"error": "Callback queue not initialized"}, 500), 500


@features_bp.route("/api/callback-queue/complete", methods=["POST"])
@require_auth
def complete_callback() -> tuple[Response, int]:
    """Complete a callback."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "callback_queue"):
        try:
            data = get_request_body()

            if "callback_id" not in data or "success" not in data:
                return send_json(
                    {"error": "Missing required fields: callback_id, success"}, 400
                ), 400

            callback_id = str(data["callback_id"])[:100]
            success = bool(data["success"])
            notes = str(data.get("notes", ""))[:500] if data.get("notes") else None

            result = pbx_core.callback_queue.complete_callback(callback_id, success, notes)

            if result:
                return send_json({"success": True, "message": "Callback completed"}), 200
            return send_json({"error": "Callback not found"}, 404), 404
        except json.JSONDecodeError:
            return send_json({"error": "Invalid JSON"}, 400), 400
        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"Error completing callback: {e}")
            return send_json({"error": "Error completing callback"}, 500), 500
    else:
        return send_json({"error": "Callback queue not initialized"}, 500), 500


@features_bp.route("/api/callback-queue/cancel", methods=["POST"])
@require_auth
def cancel_callback() -> tuple[Response, int]:
    """Cancel a pending callback."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "callback_queue"):
        try:
            data = get_request_body()

            if "callback_id" not in data:
                return send_json({"error": "Missing required field: callback_id"}, 400), 400

            callback_id = str(data["callback_id"])[:100]

            result = pbx_core.callback_queue.cancel_callback(callback_id)

            if result:
                return send_json({"success": True, "message": "Callback cancelled"}), 200
            return send_json({"error": "Callback not found"}, 404), 404
        except json.JSONDecodeError:
            return send_json({"error": "Invalid JSON"}, 400), 400
        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"Error cancelling callback: {e}")
            return send_json({"error": "Error cancelling callback"}, 500), 500
    else:
        return send_json({"error": "Callback queue not initialized"}, 500), 500


# ==========================================================================
# Mobile Push Notification Routes
# ==========================================================================


@features_bp.route("/api/mobile-push/devices", methods=["GET"])
@require_auth
def get_all_devices() -> tuple[Response, int]:
    """Get all registered mobile devices."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "mobile_push"):
        try:
            all_devices = [
                {
                    "user_id": user_id,
                    "platform": device["platform"],
                    "registered_at": device["registered_at"].isoformat(),
                    "last_seen": device["last_seen"].isoformat(),
                }
                for user_id, devices in pbx_core.mobile_push.device_tokens.items()
                for device in devices
            ]

            # Sort by last_seen descending
            all_devices.sort(key=lambda x: x["last_seen"], reverse=True)

            return send_json({"devices": all_devices, "total": len(all_devices)}), 200
        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"Error getting all devices: {e}")
            return send_json({"error": "Error getting devices"}, 500), 500
    else:
        return send_json({"error": "Mobile push notifications not initialized"}, 500), 500


@features_bp.route("/api/mobile-push/devices/<user_id>", methods=["GET"])
@require_auth
def get_user_devices(user_id: str) -> tuple[Response, int]:
    """Get devices for a specific user."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "mobile_push"):
        try:
            # Sanitize user_id
            if not re.match(r"^[\w]{1,50}$", user_id):
                return send_json({"error": "Invalid user_id format"}, 400), 400

            devices = pbx_core.mobile_push.get_user_devices(user_id)
            return send_json({"user_id": user_id, "devices": devices, "count": len(devices)}), 200
        except Exception as e:
            logger.error(f"Error getting user devices: {e}")
            return send_json({"error": "Error getting user devices"}, 500), 500
    else:
        return send_json({"error": "Mobile push notifications not initialized"}, 500), 500


@features_bp.route("/api/mobile-push/statistics", methods=["GET"])
@require_auth
def get_push_statistics() -> tuple[Response, int]:
    """Get push notification statistics."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "mobile_push"):
        try:
            # Count devices
            total_devices = sum(
                len(devices) for devices in pbx_core.mobile_push.device_tokens.values()
            )
            total_users = len(pbx_core.mobile_push.device_tokens)

            # Count by platform
            platform_counts: dict[str, int] = {}
            for devices in pbx_core.mobile_push.device_tokens.values():
                for device in devices:
                    platform = device["platform"]
                    platform_counts[platform] = platform_counts.get(platform, 0) + 1

            # Recent notifications
            recent_notifications = len(pbx_core.mobile_push.notification_history)

            return send_json(
                {
                    "total_devices": total_devices,
                    "total_users": total_users,
                    "platforms": platform_counts,
                    "recent_notifications": recent_notifications,
                }
            ), 200
        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"Error getting push statistics: {e}")
            return send_json({"error": "Error getting push statistics"}, 500), 500
    else:
        return send_json({"error": "Mobile push notifications not initialized"}, 500), 500


@features_bp.route("/api/mobile-push/history", methods=["GET"])
@require_auth
def get_push_history() -> tuple[Response, int]:
    """Get push notification history."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "mobile_push"):
        try:
            # Get recent notification history
            history = [
                {
                    "user_id": notif["user_id"],
                    "title": notif["title"],
                    "body": notif["body"],
                    "sent_at": notif["sent_at"].isoformat(),
                    "success_count": notif.get("success_count", 0),
                    "failure_count": notif.get("failure_count", 0),
                }
                for notif in pbx_core.mobile_push.notification_history[-100:]  # Last 100
            ]

            # Sort by sent_at descending
            history.sort(key=lambda x: x["sent_at"], reverse=True)

            return send_json({"history": history}), 200
        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"Error getting push history: {e}")
            return send_json({"error": "Error getting push history"}, 500), 500
    else:
        return send_json({"error": "Mobile push notifications not initialized"}, 500), 500


@features_bp.route("/api/mobile-push/register", methods=["POST"])
@require_auth
def register_mobile_device() -> tuple[Response, int]:
    """Register a mobile device for push notifications."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "mobile_push"):
        try:
            data = get_request_body()

            if "user_id" not in data or "device_token" not in data:
                return send_json(
                    {"error": "Missing required fields: user_id, device_token"}, 400
                ), 400

            user_id = str(data["user_id"])[:50]
            device_token = str(data["device_token"])[:255]
            platform = str(data.get("platform", "unknown"))[:20]

            success = pbx_core.mobile_push.register_device(user_id, device_token, platform)

            if success:
                return send_json(
                    {"success": True, "message": "Device registered successfully"}
                ), 200
            return send_json({"error": "Failed to register device"}, 500), 500
        except json.JSONDecodeError:
            return send_json({"error": "Invalid JSON"}, 400), 400
        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"Error registering device: {e}")
            return send_json({"error": "Error registering device"}, 500), 500
    else:
        return send_json({"error": "Mobile push notifications not initialized"}, 500), 500


@features_bp.route("/api/mobile-push/unregister", methods=["POST"])
@require_auth
def unregister_mobile_device() -> tuple[Response, int]:
    """Unregister a mobile device."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "mobile_push"):
        try:
            data = get_request_body()

            if "user_id" not in data or "device_token" not in data:
                return send_json(
                    {"error": "Missing required fields: user_id, device_token"}, 400
                ), 400

            user_id = str(data["user_id"])[:50]
            device_token = str(data["device_token"])[:255]

            success = pbx_core.mobile_push.unregister_device(user_id, device_token)

            if success:
                return send_json(
                    {"success": True, "message": "Device unregistered successfully"}
                ), 200
            return send_json({"error": "Device not found"}, 404), 404
        except json.JSONDecodeError:
            return send_json({"error": "Invalid JSON"}, 400), 400
        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"Error unregistering device: {e}")
            return send_json({"error": "Error unregistering device"}, 500), 500
    else:
        return send_json({"error": "Mobile push notifications not initialized"}, 500), 500


@features_bp.route("/api/mobile-push/test", methods=["POST"])
@require_auth
def test_push_notification() -> tuple[Response, int]:
    """Send a test push notification."""
    pbx_core = get_pbx_core()
    if pbx_core and hasattr(pbx_core, "mobile_push"):
        try:
            data = get_request_body()

            if "user_id" not in data:
                return send_json({"error": "Missing required field: user_id"}, 400), 400

            user_id = str(data["user_id"])[:50]

            result = pbx_core.mobile_push.send_test_notification(user_id)

            if "error" in result:
                return send_json(result, 400), 400
            return send_json({"success": True, **result}), 200
        except json.JSONDecodeError:
            return send_json({"error": "Invalid JSON"}, 400), 400
        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"Error sending test notification: {e}")
            return send_json({"error": "Error sending test notification"}, 500), 500
    else:
        return send_json({"error": "Mobile push notifications not initialized"}, 500), 500


# ==========================================================================
# Recording Announcements Routes
# ==========================================================================


@features_bp.route("/api/recording-announcements/statistics", methods=["GET"])
@require_auth
def get_announcement_statistics() -> tuple[Response, int]:
    """
    Statistics for the recording notice.

    These used to come from a module whose playback path was unreachable, so the page showed
    zeroes while a different system played the notices correctly. Both are the same object now.
    """
    pbx_core = get_pbx_core()
    if not (pbx_core and hasattr(pbx_core, "recording_announcements")):
        return send_json({"error": "Recording announcements not initialized"}, 500), 500

    try:
        stats = pbx_core.recording_announcements.stats()
        return send_json(
            {
                "enabled": stats["enabled"],
                "announce_for": stats["announce_for"],
                "announcements_played": stats["announced"],
                # The number that matters: each failure discarded a recording, because notice
                # is what makes keeping it lawful.
                "announcements_failed": stats["failed"],
                "audio_file": stats["audio_file"],
                "audio_present": stats["audio_present"],
                "text": stats["text"],
            }
        ), 200
    except (AttributeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"Error getting announcement statistics: {e}")
        return send_json({"error": "Error getting announcement statistics"}, 500), 500


@features_bp.route("/api/recording-announcements/log", methods=["GET"])
@require_auth
def get_announcement_log() -> tuple[Response, int]:
    """
    The record that callers were told, newest first.

    Kept indefinitely and not swept by retention: it is the evidence justifying a recording,
    and a call expiring at 90 days must not take that with it.
    """
    pbx_core = get_pbx_core()
    if not (pbx_core and hasattr(pbx_core, "recording_announcements")):
        return send_json({"error": "Recording announcements not initialized"}, 500), 500

    try:
        limit = validate_limit_param(default=50, max_value=500) or 50
        notices = pbx_core.recording_announcements.recent(limit)
        return send_json({"notices": notices, "count": len(notices)}), 200
    except (AttributeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"Error getting announcement log: {e}")
        return send_json({"error": "Error getting announcement log"}, 500), 500


# ==========================================================================
# Skills-Based Routing Routes
# ==========================================================================


@features_bp.route("/api/skills/all", methods=["GET"])
@require_auth
def get_all_skills() -> tuple[Response, int]:
    """Get all skills."""
    pbx_core = get_pbx_core()
    if not pbx_core or not hasattr(pbx_core, "skills_router"):
        return send_json({"error": "Skills routing not available"}, 500), 500

    try:
        skills = pbx_core.skills_router.get_all_skills()
        return send_json({"skills": skills}), 200
    except Exception as e:
        return send_json({"error": str(e)}, 500), 500


@features_bp.route("/api/skills/agent/<path:subpath>", methods=["GET"])
@require_auth
def get_agent_skills(subpath: str) -> tuple[Response, int]:
    """Get agent skills."""
    pbx_core = get_pbx_core()
    if not pbx_core or not hasattr(pbx_core, "skills_router"):
        return send_json({"error": "Skills routing not available"}, 500), 500

    try:
        agent_extension = subpath.rsplit("/", maxsplit=1)[-1]
        skills = pbx_core.skills_router.get_agent_skills(agent_extension)

        return send_json({"agent_extension": agent_extension, "skills": skills}), 200
    except Exception as e:
        return send_json({"error": str(e)}, 500), 500


@features_bp.route("/api/skills/queue/<path:subpath>", methods=["GET"])
@require_auth
def get_queue_requirements(subpath: str) -> tuple[Response, int]:
    """Get queue skill requirements."""
    pbx_core = get_pbx_core()
    if not pbx_core or not hasattr(pbx_core, "skills_router"):
        return send_json({"error": "Skills routing not available"}, 500), 500

    try:
        queue_number = subpath.rsplit("/", maxsplit=1)[-1]
        requirements = pbx_core.skills_router.get_queue_requirements(queue_number)

        return send_json({"queue_number": queue_number, "requirements": requirements}), 200
    except Exception as e:
        return send_json({"error": str(e)}, 500), 500


@features_bp.route("/api/skills/skill", methods=["POST"])
@require_auth
def add_skill() -> tuple[Response, int]:
    """Handle adding a new skill."""
    pbx_core = get_pbx_core()
    if not pbx_core or not hasattr(pbx_core, "skills_router"):
        return send_json({"error": "Skills routing not available"}, 500), 500

    try:
        data = get_request_body()
        skill_id = data.get("skill_id")
        name = data.get("name")
        description = data.get("description", "")

        if not skill_id or not name:
            return send_json({"error": "skill_id and name are required"}, 400), 400

        success = pbx_core.skills_router.add_skill(skill_id, name, description)

        if success:
            return send_json(
                {"success": True, "skill_id": skill_id, "message": "Skill added successfully"}
            ), 200
        return send_json({"error": "Skill already exists"}, 409), 409
    except (KeyError, TypeError, ValueError) as e:
        return send_json({"error": str(e)}, 500), 500


@features_bp.route("/api/skills/assign", methods=["POST"])
@require_auth
def assign_skill() -> tuple[Response, int]:
    """Handle assigning skill to agent."""
    pbx_core = get_pbx_core()
    if not pbx_core or not hasattr(pbx_core, "skills_router"):
        return send_json({"error": "Skills routing not available"}, 500), 500

    try:
        data = get_request_body()
        agent_extension = data.get("agent_extension")
        skill_id = data.get("skill_id")
        proficiency = data.get("proficiency", 5)

        if not agent_extension or not skill_id:
            return send_json({"error": "agent_extension and skill_id are required"}, 400), 400

        success = pbx_core.skills_router.assign_skill_to_agent(
            agent_extension, skill_id, proficiency
        )

        if success:
            return send_json(
                {"success": True, "message": f"Skill assigned to agent {agent_extension}"}
            ), 200
        return send_json({"error": "Failed to assign skill"}, 500), 500
    except (KeyError, TypeError, ValueError) as e:
        return send_json({"error": str(e)}, 500), 500


@features_bp.route("/api/skills/queue-requirements", methods=["POST"])
@require_auth
def set_queue_requirements() -> tuple[Response, int]:
    """Handle setting queue skill requirements."""
    pbx_core = get_pbx_core()
    if not pbx_core or not hasattr(pbx_core, "skills_router"):
        return send_json({"error": "Skills routing not available"}, 500), 500

    try:
        data = get_request_body()
        queue_number = data.get("queue_number")
        requirements = data.get("requirements", [])

        if not queue_number:
            return send_json({"error": "queue_number is required"}, 400), 400

        success = pbx_core.skills_router.set_queue_requirements(queue_number, requirements)

        if success:
            return send_json(
                {"success": True, "message": f"Requirements set for queue {queue_number}"}
            ), 200
        return send_json({"error": "Failed to set requirements"}, 500), 500
    except (KeyError, TypeError, ValueError) as e:
        return send_json({"error": str(e)}, 500), 500


@features_bp.route("/api/skills/assign/<agent_extension>/<skill_id>", methods=["DELETE"])
@require_auth
def remove_skill_from_agent(agent_extension: str, skill_id: str) -> tuple[Response, int]:
    """Handle removing skill from agent."""
    pbx_core = get_pbx_core()
    if not pbx_core or not hasattr(pbx_core, "skills_router"):
        return send_json({"error": "Skills routing not available"}, 500), 500

    try:
        success = pbx_core.skills_router.remove_skill_from_agent(agent_extension, skill_id)

        if success:
            return send_json(
                {"success": True, "message": f"Skill removed from agent {agent_extension}"}
            ), 200
        return send_json({"error": "Skill not found for agent"}, 404), 404
    except Exception as e:
        return send_json({"error": str(e)}, 500), 500
