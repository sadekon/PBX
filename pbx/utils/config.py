"""Configuration management for PBX system."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, overload

import yaml

from pbx.utils.env_loader import EnvironmentLoader, get_env_loader, load_env_file

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)


class Config:
    """Configuration manager for PBX"""

    # Email validation regex pattern
    EMAIL_PATTERN = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")

    def __init__(self, config_file: str = "config.yml", load_env: bool = True) -> None:
        """
        Initialize configuration

        Args:
            config_file: Path to configuration file
            load_env: Whether to load .env file and resolve environment variables
        """
        self.config_file = config_file
        self.config = {}
        self.env_loader = None
        self.env_enabled = load_env

        # Dotted path -> the raw "${VAR}" text as it appears in the YAML file, and the value
        # that resolving that text produced. save() consults both so resolved secrets are
        # never written back to disk. See _restore_placeholders().
        self._placeholders: dict[str, str] = {}
        self._resolved_placeholders: dict[str, Any] = {}

        # Load .env file if it exists
        if load_env:
            env_file = str(Path(config_file).parent / ".env")
            load_env_file(env_file)
            self.env_loader = get_env_loader()

        self.load()

    @staticmethod
    def validate_email(email: str) -> bool:
        """
        Validate email format

        Args:
            email: Email address to validate

        Returns:
            True if valid, False otherwise
        """
        if not email:
            return False
        return bool(Config.EMAIL_PATTERN.match(email))

    def load(self) -> None:
        """Load configuration from YAML file and resolve environment variables"""
        if not Path(self.config_file).exists():
            raise FileNotFoundError(f"Configuration file not found: {self.config_file}")

        with Path(self.config_file).open() as f:
            raw_config = yaml.safe_load(f) or {}

        self._placeholders = {}
        self._resolved_placeholders = {}

        # Resolve environment variables in configuration
        if self.env_enabled and self.env_loader:
            self._placeholders = {
                path: value
                for path, value in self._walk_leaves(raw_config)
                if isinstance(value, str) and EnvironmentLoader.ENV_VAR_PATTERN.search(value)
            }
            self.config = self.env_loader.resolve_config(raw_config)
            self._resolved_placeholders = {
                path: value
                for path, value in self._walk_leaves(self.config)
                if path in self._placeholders
            }
        else:
            self.config = raw_config

    @staticmethod
    def _walk_leaves(node: Any, path: str = "") -> Iterator[tuple[str, Any]]:
        """
        Yield (dotted_path, value) for every scalar leaf in a nested config structure.

        List elements are addressed as ``key[0]`` so a placeholder inside a list of
        dicts (e.g. sip_trunks) keeps a stable identity across load and save.
        """
        if isinstance(node, dict):
            for key, value in node.items():
                yield from Config._walk_leaves(value, f"{path}.{key}" if path else str(key))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                yield from Config._walk_leaves(value, f"{path}[{index}]")
        else:
            yield path, node

    def _restore_placeholders(self, node: Any, path: str = "") -> Any:
        """
        Rebuild the config tree with resolved environment values swapped back to ``${VAR}``.

        A leaf reverts to its placeholder only if it still holds exactly what resolution
        produced. If it differs, someone changed it deliberately (an admin editing the SMTP
        host) and the new literal is kept. Comparing types as well as values keeps Python's
        ``True == 1`` equivalence from mistaking a changed value for an untouched one.
        """
        if isinstance(node, dict):
            return {
                key: self._restore_placeholders(value, f"{path}.{key}" if path else str(key))
                for key, value in node.items()
            }
        if isinstance(node, list):
            return [
                self._restore_placeholders(value, f"{path}[{index}]")
                for index, value in enumerate(node)
            ]

        if path in self._placeholders and path in self._resolved_placeholders:
            resolved = self._resolved_placeholders[path]
            if type(node) is type(resolved) and node == resolved:
                return self._placeholders[path]

        return node

    @overload
    def get(self, key: str) -> Any: ...

    @overload
    def get(self, key: str, default: bool) -> bool: ...

    @overload
    def get(self, key: str, default: int) -> int: ...

    @overload
    def get(self, key: str, default: float) -> float: ...

    @overload
    def get(self, key: str, default: str) -> str: ...

    @overload
    def get(self, key: str, default: list[Any]) -> list[Any]: ...

    @overload
    def get(self, key: str, default: dict[str, Any]) -> dict[str, Any]: ...

    @overload
    def get(self, key: str, default: Any) -> Any: ...

    def get(self, key: str, default: Any = None) -> Any:
        """
        Get configuration value

        Args:
            key: Configuration key (supports dot notation, e.g., 'server.sip_port')
            default: Default value if key not found

        Returns:
            Configuration value
        """
        keys = key.split(".")
        value = self.config

        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
                if value is None:
                    return default
            else:
                return default

        return value

    def get_extensions(self) -> list[dict]:
        """Get all configured extensions"""
        return self.config.get("extensions", [])

    def get_extension(self, number: str | int) -> dict | None:
        """
        Get extension by number

        Args:
            number: Extension number

        Returns:
            Extension configuration or None
        """
        extensions = self.get_extensions()
        for ext in extensions:
            if ext.get("number") == str(number):
                return ext
        return None

    def save(self) -> bool:
        """Save current configuration to YAML file"""
        try:
            to_write = self._restore_placeholders(self.config)
            with Path(self.config_file).open("w") as f:
                yaml.dump(to_write, f, default_flow_style=False, sort_keys=False)
            return True
        except PermissionError as e:
            logger.error("Error saving config: Permission denied - %s", e)
            return False
        except OSError as e:
            logger.error("Error saving config: Disk error - %s", e)
            return False

    def add_extension(
        self, number: str | int, name: str, email: str, password: str, allow_external: bool = True
    ) -> bool:
        """
        Add a new extension to configuration

        Args:
            number: Extension number
            name: Display name
            email: Email address
            password: Password
            allow_external: Allow external calls

        Returns:
            True if successful, False otherwise
        """
        try:
            if "extensions" not in self.config:
                self.config["extensions"] = []

            # Check if extension already exists
            for ext in self.config["extensions"]:
                if ext.get("number") == str(number):
                    return False

            # Validate email format if provided
            if email and not self.validate_email(email):
                logger.error("Error adding extension: Invalid email format")
                return False

            # Add new extension
            new_ext = {
                "number": str(number),
                "name": name,
                "password": password,
                "allow_external": allow_external,
            }

            if email:
                new_ext["email"] = email

            self.config["extensions"].append(new_ext)
            return self.save()
        except (KeyError, TypeError, ValueError) as e:
            logger.error("Error adding extension: %s", e)
            return False

    def update_extension(
        self,
        number: str | int,
        name: str | None = None,
        email: str | None = None,
        password: str | None = None,
        allow_external: bool | None = None,
    ) -> bool:
        """
        Update an existing extension

        Args:
            number: Extension number
            name: New display name (optional)
            email: New email address (optional)
            password: New password (optional)
            allow_external: New allow_external setting (optional)

        Returns:
            True if successful, False otherwise
        """
        try:
            if "extensions" not in self.config:
                return False

            # Validate email format if provided
            if email is not None and email and not self.validate_email(email):
                logger.error("Error updating extension: Invalid email format")
                return False

            # Find and update extension
            for ext in self.config["extensions"]:
                if ext.get("number") == str(number):
                    if name is not None:
                        ext["name"] = name
                    if email is not None:
                        ext["email"] = email
                    if password is not None:
                        ext["password"] = password
                    if allow_external is not None:
                        ext["allow_external"] = allow_external
                    return self.save()

            return False
        except (KeyError, TypeError, ValueError) as e:
            logger.error("Error updating extension: %s", e)
            return False

    def delete_extension(self, number: str | int) -> bool:
        """
        Delete an extension from configuration

        Args:
            number: Extension number

        Returns:
            True if successful, False otherwise
        """
        try:
            if "extensions" not in self.config:
                return False

            # Find and remove extension
            original_length = len(self.config["extensions"])
            self.config["extensions"] = [
                ext for ext in self.config["extensions"] if ext.get("number") != str(number)
            ]

            if len(self.config["extensions"]) < original_length:
                return self.save()

            return False
        except (KeyError, TypeError, ValueError) as e:
            logger.error("Error deleting extension: %s", e)
            return False

    def get_sip_trunks(self) -> list[dict]:
        """Get all configured SIP trunks"""
        return self.config.get("sip_trunks", [])

    def get_sip_trunk(self, trunk_id: str) -> dict | None:
        """
        Get SIP trunk by ID

        Args:
            trunk_id: Trunk identifier

        Returns:
            Trunk configuration or None
        """
        for trunk in self.get_sip_trunks():
            if trunk.get("id") == trunk_id or trunk.get("trunk_id") == trunk_id:
                return trunk
        return None

    def add_sip_trunk(
        self,
        trunk_id: str,
        name: str,
        host: str,
        username: str,
        password: str,
        port: int = 5060,
        codec_preferences: list | None = None,
        priority: int = 100,
        max_channels: int = 10,
    ) -> bool:
        """
        Add a new SIP trunk to configuration

        Args:
            trunk_id: Trunk identifier
            name: Trunk name
            host: SIP provider host
            username: SIP username
            password: SIP password
            port: SIP port
            codec_preferences: list of preferred codecs
            priority: Trunk priority (lower is better, for failover)
            max_channels: Maximum concurrent channels

        Returns:
            True if successful, False otherwise
        """
        try:
            if "sip_trunks" not in self.config:
                self.config["sip_trunks"] = []

            if self.get_sip_trunk(trunk_id):
                return False

            new_trunk = {
                "id": str(trunk_id),
                "name": name,
                "host": host,
                "port": port,
                "username": username,
                "password": password,
                "priority": priority,
                "max_channels": max_channels,
            }

            if codec_preferences:
                new_trunk["codec_preferences"] = codec_preferences

            self.config["sip_trunks"].append(new_trunk)
            return self.save()
        except (KeyError, TypeError, ValueError) as e:
            logger.error("Error adding SIP trunk: %s", e)
            return False

    def update_sip_trunk(
        self,
        trunk_id: str,
        name: str | None = None,
        host: str | None = None,
        username: str | None = None,
        password: str | None = None,
        port: int | None = None,
        codec_preferences: list | None = None,
        priority: int | None = None,
        max_channels: int | None = None,
    ) -> bool:
        """
        Update an existing SIP trunk

        Args:
            trunk_id: Trunk identifier
            name: New trunk name (optional)
            host: New SIP provider host (optional)
            username: New SIP username (optional)
            password: New SIP password (optional)
            port: New SIP port (optional)
            codec_preferences: New codec preference list (optional)
            priority: New trunk priority (optional)
            max_channels: New maximum concurrent channels (optional)

        Returns:
            True if successful, False otherwise
        """
        try:
            if "sip_trunks" not in self.config:
                return False

            for trunk in self.config["sip_trunks"]:
                if trunk.get("id") == trunk_id or trunk.get("trunk_id") == trunk_id:
                    if name is not None:
                        trunk["name"] = name
                    if host is not None:
                        trunk["host"] = host
                    if username is not None:
                        trunk["username"] = username
                    if password is not None:
                        trunk["password"] = password
                    if port is not None:
                        trunk["port"] = port
                    if codec_preferences is not None:
                        trunk["codec_preferences"] = codec_preferences
                    if priority is not None:
                        trunk["priority"] = priority
                    if max_channels is not None:
                        trunk["max_channels"] = max_channels
                    return self.save()

            return False
        except (KeyError, TypeError, ValueError) as e:
            logger.error("Error updating SIP trunk: %s", e)
            return False

    def delete_sip_trunk(self, trunk_id: str) -> bool:
        """
        Delete a SIP trunk from configuration

        Args:
            trunk_id: Trunk identifier

        Returns:
            True if successful, False otherwise
        """
        try:
            if "sip_trunks" not in self.config:
                return False

            original_length = len(self.config["sip_trunks"])
            self.config["sip_trunks"] = [
                trunk
                for trunk in self.config["sip_trunks"]
                if trunk.get("id") != trunk_id and trunk.get("trunk_id") != trunk_id
            ]

            if len(self.config["sip_trunks"]) < original_length:
                return self.save()

            return False
        except (KeyError, TypeError, ValueError) as e:
            logger.error("Error deleting SIP trunk: %s", e)
            return False

    #: Fields of the top-level ``smtp:`` section the admin UI may write. The password is
    #: absent on purpose -- it is read from SMTP_PASSWORD and must never reach config.yml.
    SMTP_TEXT_FIELDS = (
        "host",
        "username",
        "from_address",
        "from_name",
        "ca_file",
        "helo_hostname",
        "envelope_from",
    )
    SMTP_INT_FIELDS = ("port", "timeout", "max_retries", "max_attachment_bytes")
    SMTP_BOOL_FIELDS = ("verify_cert",)
    SMTP_CHOICE_FIELDS: ClassVar[dict[str, tuple[str, ...]]] = {
        "security": ("starttls", "smtps"),
        "auth": ("none", "login"),
    }

    def update_email_config(self, config_data: dict) -> bool:
        """
        Update the SMTP transport configuration from the admin UI.

        Writes the top-level ``smtp:`` section, which is where transport settings live;
        ``voicemail.email.*`` remains the feature's own content settings and is untouched
        here. Values are coerced and choice fields validated, so a malformed submission is
        rejected rather than persisted.

        The password is never accepted. It comes from the SMTP_PASSWORD environment
        variable, and writing a submitted secret here would land it in config.yml on save.

        Args:
            config_data: ``{"smtp": {...}, "email_notifications": bool}``. Absent keys are
                left unchanged.

        Returns:
            True if the configuration was written.
        """
        try:
            smtp_data = config_data.get("smtp")
            if isinstance(smtp_data, dict):
                smtp = self.config.setdefault("smtp", {})

                for field in self.SMTP_TEXT_FIELDS:
                    if field in smtp_data:
                        smtp[field] = str(smtp_data[field] or "").strip()

                for field in self.SMTP_INT_FIELDS:
                    if field in smtp_data and str(smtp_data[field]).strip():
                        try:
                            smtp[field] = int(smtp_data[field])
                        except (TypeError, ValueError):
                            logger.error("Invalid %s for smtp.%s", smtp_data[field], field)
                            return False

                for field in self.SMTP_BOOL_FIELDS:
                    if field in smtp_data:
                        smtp[field] = bool(smtp_data[field])

                for field, allowed in self.SMTP_CHOICE_FIELDS.items():
                    if field in smtp_data:
                        value = str(smtp_data[field] or "").strip().lower()
                        if value not in allowed:
                            logger.error("smtp.%s must be one of %s", field, ", ".join(allowed))
                            return False
                        smtp[field] = value

            if "email_notifications" in config_data:
                voicemail = self.config.setdefault("voicemail", {})
                voicemail["email_notifications"] = bool(config_data["email_notifications"])

            return self.save()
        except (KeyError, TypeError, ValueError) as e:
            logger.error("Error updating email config: %s", e)
            return False

    def update_voicemail_pin(self, extension_number: str | int, pin: str | int) -> bool:
        """
        Update voicemail PIN for an extension

        Args:
            extension_number: Extension number
            pin: Voicemail PIN (4 digits)

        Returns:
            True if successful, False otherwise
        """
        try:
            if "extensions" not in self.config:
                return False

            # Validate PIN format
            if not pin or len(str(pin)) != 4 or not str(pin).isdigit():
                logger.error("Error updating voicemail PIN: Invalid PIN format")
                return False

            # Find and update extension
            for ext in self.config["extensions"]:
                if ext.get("number") == str(extension_number):
                    ext["voicemail_pin"] = str(pin)
                    return self.save()

            return False
        except (KeyError, TypeError, ValueError) as e:
            logger.error("Error updating voicemail PIN: %s", e)
            return False

    def get_dtmf_config(self) -> dict | None:
        """
        Get DTMF configuration

        Returns:
            Dictionary with DTMF configuration
        """
        try:
            # Get DTMF config from features.webrtc.dtmf section
            dtmf_config = {
                "mode": self.get("features.webrtc.dtmf.mode", "RFC2833"),
                "payload_type": self.get("features.webrtc.dtmf.payload_type", 101),
                "duration": self.get("features.webrtc.dtmf.duration", 160),
                "sip_info_fallback": self.get("features.webrtc.dtmf.sip_info_fallback", True),
                "inband_fallback": self.get("features.webrtc.dtmf.inband_fallback", True),
                "detection_threshold": self.get("features.webrtc.dtmf.detection_threshold", 0.3),
                "relay_enabled": self.get("features.webrtc.dtmf.relay_enabled", True),
            }
            return dtmf_config
        except (KeyError, TypeError, ValueError) as e:
            logger.error("Error getting DTMF config: %s", e)
            return None

    def _ensure_dtmf_config_structure(self) -> dict:
        """Ensure DTMF config structure exists"""
        if "features" not in self.config:
            self.config["features"] = {}
        if "webrtc" not in self.config["features"]:
            self.config["features"]["webrtc"] = {}
        if "dtmf" not in self.config["features"]["webrtc"]:
            self.config["features"]["webrtc"]["dtmf"] = {}
        return self.config["features"]["webrtc"]["dtmf"]

    def _update_dtmf_simple_fields(self, dtmf_config: dict, dtmf: dict) -> None:
        """Update simple boolean/string DTMF fields"""
        if "mode" in dtmf:
            dtmf_config["mode"] = dtmf["mode"]
        if "sip_info_fallback" in dtmf:
            dtmf_config["sip_info_fallback"] = bool(dtmf["sip_info_fallback"])
        if "inband_fallback" in dtmf:
            dtmf_config["inband_fallback"] = bool(dtmf["inband_fallback"])
        if "relay_enabled" in dtmf:
            dtmf_config["relay_enabled"] = bool(dtmf["relay_enabled"])

    def _validate_dtmf_payload_type(self, payload_type: int | str) -> int | None:
        """Validate DTMF payload type"""
        payload_type = int(payload_type)
        if payload_type < 96 or payload_type > 127:
            logger.error(
                "Error updating DTMF config: Invalid payload type %d. Must be between 96 and 127",
                payload_type,
            )
            return None
        return payload_type

    def _validate_dtmf_duration(self, duration: int | str) -> int | None:
        """Validate DTMF duration"""
        duration = int(duration)
        if duration < 80 or duration > 500:
            logger.error(
                "Error updating DTMF config: Invalid duration %dms. Must be between 80 and 500ms",
                duration,
            )
            return None
        return duration

    def _validate_dtmf_threshold(self, threshold: float | str) -> float | None:
        """Validate DTMF detection threshold"""
        threshold = float(threshold)
        if threshold < 0.1 or threshold > 0.9:
            logger.error(
                "Error updating DTMF config: Invalid detection threshold %s. Must be between 0.1 and 0.9",
                threshold,
            )
            return None
        return threshold

    def update_dtmf_config(self, config_data: dict) -> bool:
        """
        Update DTMF configuration

        Args:
            config_data: Dictionary with DTMF configuration

        Returns:
            True if successful, False otherwise
        """
        try:
            dtmf_config = self._ensure_dtmf_config_structure()
            dtmf = config_data.get("dtmf", config_data)

            # Update simple fields
            self._update_dtmf_simple_fields(dtmf_config, dtmf)

            # Update and validate complex fields
            if "payload_type" in dtmf:
                validated = self._validate_dtmf_payload_type(dtmf["payload_type"])
                if validated is None:
                    return False
                dtmf_config["payload_type"] = validated

            if "duration" in dtmf:
                validated = self._validate_dtmf_duration(dtmf["duration"])
                if validated is None:
                    return False
                dtmf_config["duration"] = validated

            if "detection_threshold" in dtmf:
                validated = self._validate_dtmf_threshold(dtmf["detection_threshold"])
                if validated is None:
                    return False
                dtmf_config["detection_threshold"] = validated

            return self.save()
        except (KeyError, TypeError, ValueError) as e:
            logger.error("Error updating DTMF config: %s", e)
            return False
