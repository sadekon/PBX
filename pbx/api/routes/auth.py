"""Authentication Blueprint routes.

Handles login and logout for the PBX API. Login supports directory
authentication (Active Directory / Exchange username and password),
regular extension authentication (via voicemail PIN) and the special
license admin extension.
"""

import secrets
import traceback
from typing import Any

from flask import Blueprint, Response

from pbx.api.utils import get_pbx_core, get_request_body, send_json
from pbx.utils.logger import get_logger
from pbx.utils.security import RateLimiter

logger = get_logger()

auth_bp = Blueprint("auth", __name__, url_prefix="/api/auth")

# Longest directory username accepted (a UPN is at most 1024, sAMAccountName 20)
MAX_DIRECTORY_USERNAME_LENGTH = 256

# Failed directory logins count toward the directory's own account lockout,
# so throttle them here before they reach the directory server
_directory_rate_limiter = RateLimiter()


def _record_auth_metric(status: str) -> None:
    """Record authentication attempt metric if exporter is available."""
    pbx_core = get_pbx_core()
    exporter = getattr(pbx_core, "metrics_exporter", None) if pbx_core else None
    if exporter:
        exporter.record_auth_attempt(status=status)


def _directory_login_enabled(pbx_core: Any) -> bool:
    """Whether users can sign in with their directory username and password."""
    ad_integration = getattr(pbx_core, "ad_integration", None)
    return ad_integration is not None and getattr(ad_integration, "enabled", False) is True


def _handle_directory_login(pbx_core: Any, username: str, password: str) -> Response:
    """Authenticate against the directory and sign in to the linked extension."""
    if not _directory_login_enabled(pbx_core):
        _record_auth_metric("failure")
        return send_json(
            {"error": "Directory sign-in is not enabled. Use your extension number."}, 401
        )

    if not pbx_core.extension_db:
        return send_json({"error": "Database not available"}, 500)

    rate_limit_key = username.lower()
    is_limited, retry_after = _directory_rate_limiter.is_rate_limited(rate_limit_key)
    if is_limited:
        _record_auth_metric("failure")
        minutes = max(1, ((retry_after or 0) + 59) // 60)
        return send_json(
            {"error": f"Too many failed sign-in attempts. Try again in {minutes} minute(s)."},
            429,
        )

    user = pbx_core.ad_integration.authenticate_user(username, password)
    if not user:
        _directory_rate_limiter.record_attempt(rate_limit_key)
        _record_auth_metric("failure")
        return send_json({"error": "Invalid credentials"}, 401)

    _directory_rate_limiter.record_attempt(rate_limit_key, successful=True)

    ext = pbx_core.extension_db.get_by_ad_username(user["username"])
    if not ext:
        logger.warning(f"Directory user {user['username']} has no linked extension")
        _record_auth_metric("failure")
        return send_json(
            {
                "error": "Your account is not linked to a phone extension. "
                "Contact your administrator."
            },
            403,
        )

    from pbx.utils.session_token import get_session_token_manager

    name = ext.get("name") or user.get("display_name")
    email = ext.get("email") or user.get("email")
    token = get_session_token_manager().generate_token(
        extension=ext["number"],
        is_admin=ext.get("is_admin", False),
        name=name,
        email=email,
    )

    _record_auth_metric("success")
    return send_json(
        {
            "success": True,
            "token": token,
            "extension": ext["number"],
            "is_admin": ext.get("is_admin", False),
            "name": name or "User",
            "email": email or "",
        }
    )


@auth_bp.route("/methods", methods=["GET"])
def handle_login_methods() -> Response:
    """Report which sign-in methods the login page should offer."""
    pbx_core = get_pbx_core()
    return send_json({"directory_login": _directory_login_enabled(pbx_core)})


@auth_bp.route("/login", methods=["POST"])
def handle_login() -> Response:
    """Authenticate a directory user or extension and return session token."""
    pbx_core = get_pbx_core()
    if not pbx_core:
        return send_json({"error": "PBX not initialized"}, 500)

    try:
        body = get_request_body()
        extension_number = body.get("extension")
        username = body.get("username")
        password = body.get("password")

        # A username without an extension is a directory (AD / Exchange) login
        if not extension_number and username:
            if not isinstance(username, str) or not isinstance(password, str) or not password:
                return send_json({"error": "Username and password required"}, 400)
            username = username.strip()
            if not username or len(username) > MAX_DIRECTORY_USERNAME_LENGTH:
                return send_json({"error": "Username and password required"}, 400)
            return _handle_directory_login(pbx_core, username, password)

        if not extension_number or not password:
            return send_json({"error": "Extension and password required"}, 400)

        # Check if this is the special license admin extension (9322)
        from pbx.utils.license_admin import (
            LICENSE_ADMIN_USERNAME,
            is_license_admin_extension,
            verify_license_admin_credentials,
        )

        if is_license_admin_extension(extension_number):
            # Handle license admin authentication separately
            # For license admin, the password is the PIN
            # Username defaults to LICENSE_ADMIN_USERNAME if not provided in request
            username = body.get("username", LICENSE_ADMIN_USERNAME)
            if verify_license_admin_credentials(extension_number, username, password):
                # Generate session token for license admin
                from pbx.utils.session_token import get_session_token_manager

                token_manager = get_session_token_manager()
                token = token_manager.generate_token(
                    extension=extension_number,
                    is_admin=True,  # License admin has admin privileges
                    name="License Administrator",
                    email="",
                )

                _record_auth_metric("success")
                return send_json(
                    {
                        "success": True,
                        "token": token,
                        "extension": extension_number,
                        "is_admin": True,
                        "name": "License Administrator",
                        "email": "",
                    }
                )
            _record_auth_metric("failure")
            return send_json({"error": "Invalid credentials"}, 401)

        # Get extension from database
        if not pbx_core.extension_db:
            return send_json({"error": "Database not available"}, 500)

        ext = pbx_core.extension_db.get(extension_number)
        if not ext:
            _record_auth_metric("failure")
            return send_json({"error": "Invalid credentials"}, 401)

        # Verify password using voicemail PIN
        # For Phase 3 authentication, the login password is the user's voicemail PIN
        # This provides a single credential for users to remember (their voicemail PIN)
        voicemail_pin_hash = ext.get("voicemail_pin_hash", "")

        # Check if voicemail PIN is hashed (contains salt) or plain text
        # For backwards compatibility, we support both
        from pbx.utils.encryption import get_encryption

        fips_mode = pbx_core.config.get("security.fips_mode", False)
        encryption = get_encryption(fips_mode)

        voicemail_pin_salt = ext.get("voicemail_pin_salt")
        if voicemail_pin_salt:
            # Voicemail PIN is hashed - verify using encryption
            if not encryption.verify_password(password, voicemail_pin_hash, voicemail_pin_salt):
                _record_auth_metric("failure")
                return send_json({"error": "Invalid credentials"}, 401)
        else:
            # Voicemail PIN is plain text (legacy) or not set
            # If no voicemail PIN is configured, deny access for security
            if not voicemail_pin_hash or voicemail_pin_hash == "":
                _record_auth_metric("failure")
                return send_json({"error": "Invalid credentials"}, 401)

            # Ensure both values are strings before comparison
            password_str = password if isinstance(password, str) else password.decode("utf-8")
            voicemail_pin_str = (
                voicemail_pin_hash
                if isinstance(voicemail_pin_hash, str)
                else str(voicemail_pin_hash)
            )
            if not secrets.compare_digest(
                password_str.encode("utf-8"), voicemail_pin_str.encode("utf-8")
            ):
                _record_auth_metric("failure")
                return send_json({"error": "Invalid credentials"}, 401)

        # Generate session token
        from pbx.utils.session_token import get_session_token_manager

        token_manager = get_session_token_manager()
        token = token_manager.generate_token(
            extension=extension_number,
            is_admin=ext.get("is_admin", False),
            name=ext.get("name"),
            email=ext.get("email"),
        )

        _record_auth_metric("success")
        return send_json(
            {
                "success": True,
                "token": token,
                "extension": extension_number,
                "is_admin": ext.get("is_admin", False),
                "name": ext.get("name", "User"),
                "email": ext.get("email", ""),
            }
        )

    except (KeyError, TypeError, ValueError) as e:
        logger.error(f"Login error: {e}")
        logger.error(traceback.format_exc())
        return send_json({"error": "Authentication failed"}, 500)


@auth_bp.route("/logout", methods=["POST"])
def handle_logout() -> Response:
    """Handle logout (client-side token removal).

    Logout is primarily handled client-side by removing the token.
    This endpoint is here for completeness and future server-side
    token invalidation.
    """
    return send_json({"success": True, "message": "Logged out successfully"})
