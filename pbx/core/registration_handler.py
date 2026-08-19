"""
Extension registration handling for PBX Core.

Handles SIP REGISTER processing: parsing the extension number from the From
header, verifying the extension exists (database first, then config),
extracting the phone's listening address from the Contact header, extracting
a MAC address from Contact/User-Agent for phone-registration bookkeeping, and
the periodic sweep that unregisters extensions whose registration has
expired.

Called from SIPServer on every REGISTER request.
"""

from __future__ import annotations

import re
import threading
import traceback
import uuid
from datetime import UTC, datetime
from typing import Any

from pbx.features.extensions import ExtensionRegistry
from pbx.features.webhooks import WebhookEvent

_RE_SIP_EXT = re.compile(r"sip:(\d+)@")
_RE_MAC_PARAM = re.compile(r"mac=([0-9a-fA-F:]{17}|[0-9a-fA-F-]{17})")
_RE_SIP_INSTANCE = re.compile(r'sip\.instance="<urn:uuid:([0-9a-f-]+)>"', re.IGNORECASE)
_RE_MAC_IN_UA = re.compile(r"([0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}")

# Phones' own SIP listening port isn't persisted in registered_phones_db
# (only ip_address is) -- 5060 is the near-universal default, including for
# both Zultys ZIP models (see provisioning_templates/zultys_zip*.template).
_DEFAULT_PHONE_SIP_PORT = 5060


class RegistrationHandler:
    """Handles SIP extension registration and its expiry sweep."""

    def __init__(self, pbx_core: Any) -> None:
        """
        Initialize RegistrationHandler with a reference to PBXCore.

        Args:
            pbx_core: The PBXCore instance.
        """
        self.pbx_core: Any = pbx_core

        # Per-extension locks to prevent race conditions during concurrent REGISTER
        self._registration_locks: dict[str, threading.Lock] = {}
        self._registration_locks_guard = threading.Lock()
        self._reg_expiry_timer: threading.Timer | None = None

    def _extract_contact_address(
        self, contact: str | None, addr: tuple[str, int]
    ) -> tuple[str, int]:
        """
        Decide where a phone can actually be reached, from its Contact header and the
        address its REGISTER arrived from.

        The Contact header is worth reading for one thing: the port the phone listens on,
        which is often 5060 even when it registered from an ephemeral source port. It is not
        worth trusting for the host. A phone advertises whatever its own config says, and
        that has been observed wrong in two different ways on this deployment -- an ATA
        claiming 127.0.0.1, which made the PBX INVITE itself, and the same ATA claiming
        10.0.0.27 while sitting on 192.168.10.120, which made every call to it disappear
        into a subnet with nothing on it. Both survived restarts, because the bad address
        was recorded and then recovered from the database.

        So the host comes from the packet, which cannot lie about where it came from, and
        the port comes from the Contact only when the Contact's host agrees with it. When
        they disagree the whole Contact is suspect, and the source address is used entire.

        This is what a SIP proxy's `nat=yes` does, and it is right here for the same reason:
        every endpoint is on the LAN with the PBX, so the source address is both reachable
        and authoritative.

        Args:
            contact: Contact header value (e.g., "<sip:1501@192.168.1.100:5060>")
            addr: The UDP source address the REGISTER arrived from

        Returns:
            Tuple of (ip_address, port) to reach this phone on
        """
        pbx = self.pbx_core

        if not contact:
            return addr

        # Format: <sip:extension@ip:port[;params]> or <sip:extension@ip[;params]>
        contact_match = re.search(r"sip:[^@]+@([^:;>]+)(?::(\d+))?", contact)
        if not contact_match:
            return addr

        contact_host = contact_match.group(1)
        port_str = contact_match.group(2)
        contact_port = int(port_str) if port_str else _DEFAULT_PHONE_SIP_PORT

        if not contact_host:
            return addr

        if contact_host == addr[0]:
            # Consistent: take the port it named, which is the one it listens on.
            return (contact_host, contact_port)

        pbx.logger.warning(
            f"Phone at {addr[0]} advertised Contact host {contact_host}, which is not where "
            f"its REGISTER came from. Registering it at {addr[0]}:{addr[1]} instead -- a "
            f"phone reachable only at the address it advertises would be unreachable from "
            f"here. Check that device's network configuration."
        )
        return addr

    def register_extension(
        self,
        from_header: str,
        addr: tuple[str, int],
        user_agent: str | None = None,
        contact: str | None = None,
        expires: int = 3600,
    ) -> bool:
        """
        Register extension and store phone information

        Args:
            from_header: SIP From header
            addr: Network address (host, port)
            user_agent: User-Agent header from SIP REGISTER
            contact: Contact header from SIP REGISTER
            expires: Registration lifetime in seconds (from SIP Expires header)

        Returns:
            True if registration successful
        """
        pbx = self.pbx_core

        # Parse extension number from header
        # Format: "Display Name" <sip:1001@host>
        match = _RE_SIP_EXT.search(from_header)
        if match:
            extension_number = match.group(1)

            # Acquire per-extension lock to prevent race conditions
            with self._registration_locks_guard:
                if extension_number not in self._registration_locks:
                    self._registration_locks[extension_number] = threading.Lock()
                ext_lock = self._registration_locks[extension_number]

            with ext_lock:
                return self._register_extension_locked(
                    extension_number, addr, user_agent, contact, expires
                )

        pbx.logger.warning(f"Could not parse extension from {from_header}")
        return False

    def _register_extension_locked(
        self,
        extension_number: str,
        addr: tuple[str, int],
        user_agent: str | None,
        contact: str | None,
        expires: int,
    ) -> bool:
        """Perform the actual registration under the per-extension lock."""
        pbx = self.pbx_core
        registered_addr = addr

        # Verify extension exists - check database first, then config
        extension_exists = False

        # Check extensions database table first (if available)
        if pbx.extension_db:
            try:
                db_extension = pbx.extension_db.get(extension_number)
                if db_extension:
                    extension_exists = True
                    pbx.logger.debug(f"Extension {extension_number} found in database")

                    # Ensure extension is loaded in registry (if not already)
                    if not pbx.extension_registry.get(extension_number):
                        extension_obj = ExtensionRegistry.create_extension_from_db(db_extension)
                        pbx.extension_registry.extensions[extension_number] = extension_obj
                        pbx.logger.debug(
                            f"Loaded extension {extension_number} into registry from database"
                        )
            except (KeyError, TypeError, ValueError) as e:
                pbx.logger.debug(f"Error checking extension in database: {e}")

        # Fall back to config if not found in database or database not available
        if not extension_exists:
            extension = pbx.config.get_extension(extension_number)
            if extension:
                extension_exists = True
                pbx.logger.debug(f"Extension {extension_number} found in config")

        if extension_exists:
            # Handle Expires: 0 as unregistration per RFC 3261 Section 10.2.2.
            if expires == 0:
                pbx.extension_registry.unregister(extension_number)
                pbx.logger.info(f"Extension {extension_number} unregistered (Expires: 0)")
            else:
                # Extract the phone's listening address from Contact header
                registered_addr = self._extract_contact_address(contact, addr)
                pbx.extension_registry.register(extension_number, registered_addr, expires=expires)
                pbx.logger.info(f"Extension {extension_number} registered from {registered_addr}")

                # Store phone registration in database (skip for unregistration)
            # Store phone registration in database (skip for unregistration)
            if pbx.registered_phones_db and expires > 0:
                ip_address, sip_port = registered_addr
                mac_address = self._extract_mac_address(contact, user_agent)

                try:
                    _, stored_mac = pbx.registered_phones_db.register_phone(
                        extension_number=extension_number,
                        ip_address=ip_address,
                        mac_address=mac_address,
                        user_agent=user_agent,
                        contact_uri=contact,
                        sip_port=sip_port,
                    )

                    if stored_mac:
                        pbx.logger.info(
                            f"Stored phone registration: ext={extension_number}, ip={ip_address}, mac={stored_mac}"
                        )
                    else:
                        pbx.logger.info(
                            f"Stored phone registration: ext={extension_number}, ip={ip_address} (no MAC)"
                        )
                except Exception as e:
                    pbx.logger.error(f"Failed to store phone registration in database: {e}")
                    pbx.logger.error(f"  Extension: {extension_number}")
                    pbx.logger.error(f"  IP Address: {ip_address}")
                    pbx.logger.error(f"  MAC Address: {mac_address}")
                    pbx.logger.error(f"  User Agent: {user_agent}")
                    pbx.logger.error(f"  Contact URI: {contact}")
                    pbx.logger.error(f"  Traceback: {traceback.format_exc()}")

            # Record successful registration metric
            if pbx.metrics_exporter:
                event = "unregistration" if expires == 0 else "success"
                pbx.metrics_exporter.record_extension_registration(event)

            # Trigger webhook event for registrations (not unregistrations)
            if expires > 0:
                pbx.webhook_system.trigger_event(
                    WebhookEvent.EXTENSION_REGISTERED,
                    {
                        "extension": extension_number,
                        "ip_address": registered_addr[0],
                        "port": registered_addr[1],
                        "user_agent": user_agent,
                        "timestamp": datetime.now(UTC).isoformat(),
                    },
                )

            return True

        # Record failed registration metric
        if pbx.metrics_exporter:
            pbx.metrics_exporter.record_extension_registration("failure")
        pbx.logger.warning(f"Unknown extension {extension_number} attempted registration")
        return False

    def resync_known_phones(self) -> None:
        """
        Kick every previously-seen phone into re-registering immediately.

        extension_registry is in-memory only, so it starts empty on every
        process restart; a phone stays unreachable for inbound calls (see
        is_registered / _handle_invite) until it sends a fresh REGISTER on
        its own. Most phones notice a lost connection and do that right
        away, but some (observed with the Zultys ZIP 37G) don't and just
        wait out their full Expires interval -- up to an hour of being
        unreachable after a routine restart, even though registered_phones_db
        (which does survive restarts) already has their last-known address.

        Sends a check-sync NOTIFY to each one, the same event phones already
        honor as a "reload now" signal from reboot_phone(). Best-effort: an
        unreachable or since-moved phone just doesn't answer.
        """
        from pbx.sip.message import SIPMessageBuilder

        pbx = self.pbx_core

        if not pbx.registered_phones_db:
            return

        try:
            known_phones = list(pbx.registered_phones_db.list_all())
        except Exception as e:
            pbx.logger.error(f"Failed to load known phones for startup resync: {e}")
            return

        server_ip = pbx._get_server_ip()
        server_sip_port = pbx.config.get("server.sip_port", 5060)
        sent = 0

        for phone in known_phones:
            extension_number = phone.get("extension_number")
            ip_address = phone.get("ip_address")
            if not extension_number or not ip_address:
                continue

            try:
                notify_msg = SIPMessageBuilder.build_request(
                    method="NOTIFY",
                    uri=f"sip:{extension_number}@{ip_address}:{_DEFAULT_PHONE_SIP_PORT}",
                    from_addr=f"<sip:{server_ip}:{server_sip_port}>",
                    to_addr=f"<sip:{extension_number}@{server_ip}>",
                    call_id=f"notify-resync-{extension_number}-{uuid.uuid4()}",
                    cseq=1,
                )
                notify_msg.set_header("Event", "check-sync")
                notify_msg.set_header("Subscription-State", "terminated")
                notify_msg.set_header("Content-Length", "0")
                branch_id = str(uuid.uuid4()).replace("-", "")
                notify_msg.set_header(
                    "Via",
                    f"SIP/2.0/UDP {server_ip}:{server_sip_port};branch=z9hG4bK{branch_id}",
                )
                notify_msg.set_header("Max-Forwards", "70")

                pbx.sip_server._send_message(
                    notify_msg.build(), (ip_address, _DEFAULT_PHONE_SIP_PORT)
                )
                sent += 1
            except (KeyError, TypeError, ValueError, OSError) as e:
                pbx.logger.debug(f"Failed to send resync NOTIFY to {extension_number}: {e}")

        if sent:
            pbx.logger.info(
                f"Startup resync: sent check-sync NOTIFY to {sent} previously-registered "
                "phone(s) to speed up re-registration after restart"
            )

    def _extract_mac_address(self, contact: str | None, user_agent: str | None) -> str | None:
        """
        Extract MAC address from SIP headers

        Args:
            contact: Contact header
            user_agent: User-Agent header

        Returns:
            MAC address string or None
        """
        pbx = self.pbx_core
        try:
            mac_address = None

            # Try to extract from Contact header
            if contact:
                mac_match = _RE_MAC_PARAM.search(contact)
                if mac_match:
                    mac_address = mac_match.group(1).lower()

                instance_match = _RE_SIP_INSTANCE.search(contact)
                if not mac_address and instance_match:
                    # Some devices use UUID derived from MAC
                    uuid_str = instance_match.group(1).replace("-", "")
                    # Last 12 chars might be MAC
                    if len(uuid_str) >= 12:
                        potential_mac = uuid_str[-12:]
                        mac_address = ":".join([potential_mac[i : i + 2] for i in range(0, 12, 2)])

            # Try to extract from User-Agent
            if not mac_address and user_agent:
                mac_match = _RE_MAC_IN_UA.search(user_agent)
                if mac_match:
                    mac_address = mac_match.group(0).lower()

            # Normalize MAC address format (remove separators, lowercase)
            if mac_address:
                mac_address = mac_address.replace(":", "").replace("-", "").lower()

            return mac_address
        except (re.error, AttributeError, IndexError, TypeError) as e:
            pbx.logger.debug(f"Failed to parse MAC address from SIP headers: {e}")
            return None

    def _start_registration_expiry_timer(self) -> None:
        """Start periodic timer to clean up expired registrations."""
        pbx = self.pbx_core
        interval = pbx.config.get("sip.registration_expiry_check_interval", 60)
        self._reg_expiry_timer = threading.Timer(interval, self._check_expired_registrations)
        self._reg_expiry_timer.daemon = True
        self._reg_expiry_timer.start()

    def _check_expired_registrations(self) -> None:
        """Unregister extensions whose registration has expired."""
        pbx = self.pbx_core
        try:
            for ext in pbx.extension_registry.get_registered():
                if ext.is_expired():
                    pbx.logger.info(f"Extension {ext.number} registration expired, unregistering")
                    pbx.extension_registry.unregister(ext.number)
        except Exception as e:
            pbx.logger.error(f"Error checking expired registrations: {e}")
        finally:
            # Reschedule if still running
            if pbx.running:
                self._start_registration_expiry_timer()

    def stop(self) -> None:
        """Cancel the registration expiry timer, if running."""
        if self._reg_expiry_timer is not None:
            self._reg_expiry_timer.cancel()
