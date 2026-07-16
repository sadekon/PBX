"""
Core PBX implementation
Central coordinator for all PBX functionality
"""

from __future__ import annotations

import re
import threading
import traceback
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pbx.core.auto_attendant_handler import AutoAttendantHandler
from pbx.core.call import CallManager
from pbx.core.call_router import CallRouter
from pbx.core.codec_negotiator import CodecNegotiator
from pbx.core.emergency_handler import EmergencyHandler
from pbx.core.feature_initializer import FeatureInitializer
from pbx.core.paging_handler import PagingHandler
from pbx.core.transfer_handler import TransferHandler
from pbx.core.voicemail_handler import VoicemailHandler
from pbx.features.extensions import ExtensionRegistry
from pbx.features.webhooks import WebhookEvent
from pbx.rtp.handler import RTPRelay
from pbx.sip.server import SIPServer
from pbx.utils.config import Config
from pbx.utils.database import DatabaseBackend, RegisteredPhonesDB
from pbx.utils.logger import PBXLogger, get_logger

if TYPE_CHECKING:
    from pbx.features.auto_attendant import AutoAttendant
    from pbx.features.call_parking import CallParkingSystem
    from pbx.features.call_queue import QueueSystem
    from pbx.features.call_recording import CallRecordingSystem
    from pbx.features.callback_queue import CallbackQueue
    from pbx.features.cdr import CDRSystem
    from pbx.features.conference import ConferenceSystem
    from pbx.features.crm_integration import CRMIntegration
    from pbx.features.dnd_scheduling import DNDScheduler
    from pbx.features.e911_location import E911LocationService
    from pbx.features.emergency_notification import EmergencyNotificationSystem
    from pbx.features.find_me_follow_me import FindMeFollowMe
    from pbx.features.fraud_detection import FraudDetectionSystem
    from pbx.features.hot_desking import HotDeskingSystem
    from pbx.features.karis_law import KarisLawCompliance
    from pbx.features.mfa import MFAManager
    from pbx.features.mobile_push import MobilePushNotifications
    from pbx.features.music_on_hold import MusicOnHold
    from pbx.features.paging import PagingSystem
    from pbx.features.phone_book import PhoneBook
    from pbx.features.phone_provisioning import PhoneProvisioning
    from pbx.features.presence import PresenceSystem
    from pbx.features.recording_announcements import RecordingAnnouncements
    from pbx.features.recording_retention import RecordingRetentionManager
    from pbx.features.session_border_controller import SessionBorderController
    from pbx.features.sip_trunk import SIPTrunkSystem
    from pbx.features.skills_routing import SkillsBasedRouter
    from pbx.features.statistics import StatisticsEngine
    from pbx.features.time_based_routing import TimeBasedRouting
    from pbx.features.voicemail import VoicemailSystem
    from pbx.features.webhooks import WebhookSystem
    from pbx.features.webrtc import WebRTCGateway, WebRTCSignalingServer
    from pbx.integrations.active_directory import ActiveDirectoryIntegration
    from pbx.integrations.espocrm import EspoCRMIntegration
    from pbx.integrations.jitsi import JitsiIntegration
    from pbx.integrations.matrix import MatrixIntegration
    from pbx.integrations.zoom import ZoomIntegration
    from pbx.utils.database import ExtensionDB
    from pbx.utils.prometheus_exporter import PBXMetricsExporter
    from pbx.utils.security import ThreatDetector
    from pbx.utils.security_monitor import SecurityMonitor

_RE_SIP_EXT = re.compile(r"sip:(\d+)@")
_RE_MAC_PARAM = re.compile(r"mac=([0-9a-fA-F:]{17}|[0-9a-fA-F-]{17})")
_RE_SIP_INSTANCE = re.compile(r'sip\.instance="<urn:uuid:([0-9a-f-]+)>"', re.IGNORECASE)
_RE_MAC_IN_UA = re.compile(r"([0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}")


class PBXCore:
    """Main PBX system coordinator"""

    # Attributes set by FeatureInitializer.initialize()
    voicemail_system: VoicemailSystem
    conference_system: ConferenceSystem
    recording_system: CallRecordingSystem
    queue_system: QueueSystem
    presence_system: PresenceSystem
    parking_system: CallParkingSystem
    cdr_system: CDRSystem
    moh_system: MusicOnHold
    trunk_system: SIPTrunkSystem
    statistics_engine: StatisticsEngine
    auto_attendant: AutoAttendant | None
    phone_provisioning: PhoneProvisioning | None
    ad_integration: ActiveDirectoryIntegration | None
    phone_book: PhoneBook | None
    emergency_notification: EmergencyNotificationSystem | None
    paging_system: PagingSystem | None
    e911_location: E911LocationService | None
    karis_law: KarisLawCompliance | None
    webhook_system: WebhookSystem
    webrtc_signaling: WebRTCSignalingServer | None
    webrtc_gateway: WebRTCGateway | None
    crm_integration: CRMIntegration | None
    hot_desking: HotDeskingSystem | None
    find_me_follow_me: FindMeFollowMe
    time_based_routing: TimeBasedRouting
    recording_retention: RecordingRetentionManager
    fraud_detection: FraudDetectionSystem
    callback_queue: CallbackQueue
    mobile_push: MobilePushNotifications
    recording_announcements: RecordingAnnouncements
    mfa_manager: MFAManager | None
    threat_detector: ThreatDetector | None
    security_monitor: SecurityMonitor
    dnd_scheduler: DNDScheduler | None
    sbc: SessionBorderController | None
    skills_router: SkillsBasedRouter | None
    jitsi_integration: JitsiIntegration | None
    matrix_integration: MatrixIntegration | None
    espocrm_integration: EspoCRMIntegration | None
    zoom_integration: ZoomIntegration | None

    # Database attributes (set conditionally in __init__)
    extension_db: ExtensionDB | None
    registered_phones_db: RegisteredPhonesDB | None
    metrics_exporter: PBXMetricsExporter | None

    def __init__(self, config_file: str = "config.yml") -> None:
        """
        Initialize PBX core

        Args:
            config_file: Path to configuration file
        """
        # Load configuration
        self.config = Config(config_file)

        # Setup logging
        log_config = self.config.get("logging", {})
        PBXLogger().setup(
            log_level=log_config.get("level", "INFO"),
            log_file=log_config.get("file", "logs/pbx.log"),
            console=log_config.get("console", True),
        )
        self.logger = get_logger()

        # Check if quiet startup is enabled
        self.quiet_startup = self.config.get("logging.quiet_startup", False)

        # Track system start time for uptime calculation
        self.start_time = datetime.now(UTC)

        # Initialize database backend
        self.database = DatabaseBackend(self.config)
        self.registered_phones_db = None
        self.extension_db = None
        if self.database.connect():
            self._run_alembic_migrations()
            self.database.create_tables()
            from pbx.utils.database import ExtensionDB

            self.registered_phones_db = RegisteredPhonesDB(self.database)
            self.extension_db = ExtensionDB(self.database)
            self._log_startup(
                f"Database backend initialized successfully ({self.database.db_type})"
            )
            self._log_startup(
                "Extensions, voicemail metadata, and phone registrations will be stored in database"
            )

            # Auto-seed critical extensions if they don't exist
            self._auto_seed_critical_extensions()

            # Clean up incomplete phone registrations at startup
            # Only phones with MAC, IP, and Extension should be retained
            success, count = self.registered_phones_db.cleanup_incomplete_registrations()
            if success and count > 0:
                self._log_startup(
                    f"Startup cleanup: Removed {count} incomplete phone registration(s)"
                )
        else:
            self.logger.warning("Database backend not available - running without database")
            self.logger.warning("Extensions will be loaded from config.yml only")
            self.logger.warning("Voicemails will be stored ONLY as files (no database metadata)")
            self.logger.warning("Phone registrations will not be persisted")

        # Initialize core components
        # Pass database to extension registry so it can load extensions from DB
        self.extension_registry = ExtensionRegistry(
            self.config, database=self.database if self.database.enabled else None
        )
        self.call_manager = CallManager()

        # Per-extension locks to prevent race conditions during concurrent REGISTER
        self._registration_locks: dict[str, threading.Lock] = {}
        self._registration_locks_guard = threading.Lock()

        # Initialize QoS monitoring system first (needed by RTP relay)
        from pbx.features.qos_monitoring import QoSMonitor

        self.qos_monitor = QoSMonitor(self)

        self.rtp_relay = RTPRelay(
            self.config.get("server.rtp_port_range_start", 10000),
            self.config.get("server.rtp_port_range_end", 20000),
            qos_monitor=self.qos_monitor,
        )

        # Initialize SIP server
        self.sip_server = SIPServer(
            host=self.config.get("server.sip_host", "0.0.0.0"),  # nosec B104 - SIP server needs to bind to all interfaces
            port=self.config.get("server.sip_port", 5060),
            pbx_core=self,
        )

        # Initialize all feature subsystems via FeatureInitializer
        FeatureInitializer.initialize(self)

        # Initialize Prometheus metrics exporter
        self.metrics_exporter = None
        self._metrics_running = False
        self._metrics_thread: threading.Thread | None = None
        if self.config.get("monitoring.prometheus.enabled", False):
            from pbx.utils.prometheus_exporter import PBXMetricsExporter

            self.metrics_exporter = PBXMetricsExporter()
            self.metrics_exporter.set_system_info(
                version=self.config.get("server.version", "1.0.0"),
                server_name=self.config.get("server.server_name", "Warden VoIP"),
            )
            self._log_startup("Prometheus metrics exporter initialized")

        # Initialize API server
        api_host = self.config.get("api.host", "0.0.0.0")  # nosec B104 - API server needs to bind to all interfaces
        api_port = self.config.get("api.port", 9000)
        from pbx.api.server import PBXFlaskServer

        self.api_server = PBXFlaskServer(self, api_host, api_port)

        # Initialize handler classes for delegated functionality
        self.call_router = CallRouter(self)
        self.voicemail_handler = VoicemailHandler(self)
        self.auto_attendant_handler = AutoAttendantHandler(self)
        self.emergency_handler = EmergencyHandler(self)
        self.paging_handler = PagingHandler(self)
        self.transfer_handler = TransferHandler(self)
        self.codec_negotiator = CodecNegotiator(self)

        self.running = False

        self.logger.info("PBX Core initialized with all features")

    def _log_startup(self, message: str, level: str = "info") -> None:
        """
        Log a startup message, respecting quiet_startup setting

        Args:
            message: The message to log
            level: Log level ('info', 'warning', 'error', 'debug')
        """
        if self.quiet_startup and level == "info":
            # In quiet mode, log INFO messages as DEBUG
            self.logger.debug(f"[STARTUP] {message}")
        else:
            # Normal logging
            log_method = getattr(self.logger, level, self.logger.info)
            log_method(message)

    def _run_alembic_migrations(self) -> None:
        """
        Run pending Alembic database migrations before starting the application.

        Called during startup after the database connection is established but
        before create_tables(). This ensures schema migrations from new deployments
        (git pull + systemctl restart) are applied automatically.

        Non-fatal: if Alembic is unavailable or migrations fail, the app logs a
        warning and continues (create_tables will handle missing tables via
        CREATE TABLE IF NOT EXISTS).
        """
        try:
            from alembic.command import upgrade
            from alembic.config import Config as AlembicConfig

            alembic_cfg = AlembicConfig("alembic.ini")

            # Build database URL from the same config the app uses
            db_host = self.config.get("database.host", "localhost")
            db_port = self.config.get("database.port", 5432)
            db_name = self.config.get("database.name", "pbx_system")
            db_user = self.config.get("database.user", "pbx_user")
            db_password = self.config.get("database.password", "")
            db_url = f"postgresql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"
            alembic_cfg.set_main_option("sqlalchemy.url", db_url)

            upgrade(alembic_cfg, "head")
            self._log_startup("Alembic database migrations applied successfully")
        except Exception as e:
            self.logger.warning(f"Alembic migration skipped or failed: {e}")

    def _auto_seed_critical_extensions(self) -> None:
        """
        Auto-seed critical extensions at startup if they don't exist

        This ensures essential extensions (auto-attendant, webrtc-admin, operator)
        are always available without manual setup.
        """
        if not self.extension_db:
            return

        from pbx.utils.encryption import get_encryption

        # Initialize encryption for password hashing
        fips_mode = self.config.get("security.fips_mode", False)
        encryption = get_encryption(fips_mode)

        # Define critical extensions that should always exist
        # These use secure default passwords that MUST be changed after first
        # login
        critical_extensions = [
            {
                "number": "0",
                "name": "Auto Attendant",
                "email": "autoattendant@pbx.local",
                # Random secure default
                "password": "ChangeMe-AutoAttendant-" + str(uuid.uuid4())[:8],
                "allow_external": True,
                "voicemail_pin": "0000",
                "is_admin": False,
                "description": "Automated greeting and call routing system",
            },
            {
                "number": "1001",
                "name": "Operator / WebAdmin Phone",
                "email": "operator@pbx.local",
                # Random secure default
                "password": "ChangeMe-Operator-" + str(uuid.uuid4())[:8],
                "allow_external": True,
                "voicemail_pin": "1001",
                "is_admin": True,
                "description": "Primary operator extension and web-based admin phone",
            },
        ]

        seeded_count = 0

        for ext_config in critical_extensions:
            number = str(ext_config["number"])

            # Check if extension already exists
            existing = self.extension_db.get(number)
            if existing:
                continue  # Skip if already exists

            try:
                # Hash the password
                password_hash, _ = encryption.hash_password(str(ext_config["password"]))

                # Add extension to database
                success = self.extension_db.add(
                    number=number,
                    name=str(ext_config["name"]),
                    password_hash=password_hash,
                    email=str(ext_config.get("email", "")),
                    allow_external=bool(ext_config.get("allow_external", True)),
                    voicemail_pin=str(ext_config.get("voicemail_pin", "")),
                    ad_synced=False,
                    ad_username=None,
                    is_admin=bool(ext_config.get("is_admin", False)),
                )

                if success:
                    self.logger.info(
                        f"Auto-seeded critical extension {number}: {ext_config['name']}"
                    )
                    seeded_count += 1

                    # Log password for first-time setup
                    if number == "1001":
                        self.logger.warning("=" * 70)
                        self.logger.warning("FIRST-TIME SETUP - EXTENSION 1001 CREDENTIALS")
                        self.logger.warning("=" * 70)
                        self.logger.warning("Extension: 1001")
                        self.logger.warning(f"Password:  {ext_config['password']}")
                        self.logger.warning(f"Voicemail PIN: {ext_config['voicemail_pin']}")
                        self.logger.warning("")
                        self.logger.warning("⚠️  CHANGE THIS PASSWORD IMMEDIATELY via admin panel!")
                        self.logger.warning(
                            "   Access admin panel: https://<your-server-ip>:9000/admin/"
                        )
                        self.logger.warning("=" * 70)

            except (KeyError, TypeError, ValueError) as e:
                self.logger.error(f"Failed to auto-seed extension {number}: {e}")

        if seeded_count > 0:
            self.logger.info(f"Auto-seeded {seeded_count} critical extension(s) at startup")

    def _start_metrics_collector(self) -> None:
        """Start background thread for periodic system resource metrics."""
        if not self.metrics_exporter:
            return
        self._metrics_running = True
        interval = self.config.get("monitoring.prometheus.resource_collection_interval", 15)
        self._metrics_interval = max(1, int(interval) if interval else 15)
        self._metrics_thread = threading.Thread(
            target=self._metrics_collection_loop,
            name="MetricsCollector",
            daemon=True,
        )
        self._metrics_thread.start()
        self._log_startup("Prometheus metrics collector started")

    def _stop_metrics_collector(self) -> None:
        """Stop background metrics collection thread."""
        self._metrics_running = False
        if self._metrics_thread and self._metrics_thread.is_alive():
            self._metrics_thread.join(timeout=5)
        self._metrics_thread = None

    def _metrics_collection_loop(self) -> None:
        """Periodically collect system resource metrics."""
        import time

        import psutil

        exporter = self.metrics_exporter
        if exporter is None:
            return

        while self._metrics_running:
            try:
                cpu = psutil.cpu_percent(interval=1)
                mem = psutil.virtual_memory()
                exporter.update_system_resources(
                    cpu_percent=cpu,
                    memory_bytes=mem.used,
                )

                disk = psutil.disk_usage("/")
                exporter.disk_usage_bytes.labels(mount_point="/").set(disk.used)

                exporter.update_extensions(self.extension_registry.get_registered_count())
                exporter.active_calls.set(len(self.call_manager.get_active_calls()))

                if hasattr(self, "conference_system") and self.conference_system is not None:
                    exporter.conferences_active.set(len(self.conference_system.get_active_rooms()))

            except Exception as e:
                self.logger.debug(f"Metrics collection error: {e}")

            # Sleep in small increments so stop is responsive
            for _ in range(self._metrics_interval):
                if not self._metrics_running:
                    return  # type: ignore[unreachable]
                time.sleep(1)

    def _load_provisioning_devices(self) -> None:
        """Load provisioning devices from configuration"""
        if not self.phone_provisioning:
            return

        devices_config = self.config.get("provisioning.devices", [])
        for device_config in devices_config:
            mac = device_config.get("mac")
            extension = device_config.get("extension")
            vendor = device_config.get("vendor")
            model = device_config.get("model")

            if all([mac, extension, vendor, model]):
                try:
                    self.phone_provisioning.register_device(mac, extension, vendor, model)
                    self.logger.info(f"Loaded provisioning device {mac} for extension {extension}")
                except Exception as e:
                    self.logger.error(f"Failed to load provisioning device {mac}: {e}")

    def start(self) -> bool:
        """Start PBX system"""
        self.logger.info("Starting PBX system...")

        # Enforce security requirements before starting
        if hasattr(self, "security_monitor"):
            self.logger.info("Enforcing security requirements...")
            if not self.security_monitor.enforce_security_requirements():
                self.logger.error("CRITICAL: Security enforcement failed - system cannot start")
                return False
            self.logger.info("✓ Security requirements verified")

        # Start SIP server
        if not self.sip_server.start():
            self.logger.error("Failed to start SIP server")
            return False

        # Start API server
        if not self.api_server.start():
            self.logger.warning("Failed to start API server (non-critical)")

        # Start DND scheduler
        if self.dnd_scheduler:
            self.dnd_scheduler.start()

        # Register SIP trunks
        self.trunk_system.register_all()

        # Start security runtime monitor
        if hasattr(self, "security_monitor"):
            self.security_monitor.start()
            self.logger.info("Security runtime monitoring active")

        self.running = True

        # Start Prometheus metrics collector
        self._start_metrics_collector()

        # Start registration expiry sweep
        self._start_registration_expiry_timer()

        self.logger.info("PBX system started successfully")
        return True

    def _start_registration_expiry_timer(self) -> None:
        """Start periodic timer to clean up expired registrations."""
        interval = self.config.get("sip.registration_expiry_check_interval", 60)
        self._reg_expiry_timer = threading.Timer(interval, self._check_expired_registrations)
        self._reg_expiry_timer.daemon = True
        self._reg_expiry_timer.start()

    def _check_expired_registrations(self) -> None:
        """Unregister extensions whose registration has expired."""
        try:
            for ext in self.extension_registry.get_registered():
                if ext.is_expired():
                    self.logger.info(f"Extension {ext.number} registration expired, unregistering")
                    self.extension_registry.unregister(ext.number)
        except Exception as e:
            self.logger.error(f"Error checking expired registrations: {e}")
        finally:
            # Reschedule if still running
            if self.running:
                self._start_registration_expiry_timer()

    def stop(self) -> None:
        """Stop PBX system"""
        self.logger.info("Stopping PBX system...")
        self.running = False

        # Stop registration expiry timer
        if hasattr(self, "_reg_expiry_timer") and self._reg_expiry_timer is not None:
            self._reg_expiry_timer.cancel()

        # Stop Prometheus metrics collector
        self._stop_metrics_collector()

        # Stop security monitor
        if hasattr(self, "security_monitor"):
            self.security_monitor.stop()

        # Stop DND scheduler
        if self.dnd_scheduler:
            self.dnd_scheduler.stop()

        # Stop API server
        self.api_server.stop()

        # Stop SIP server
        self.sip_server.stop()

        # End all active calls with full cleanup (CDR, voicemail, timers, RTP)
        for call in self.call_manager.get_active_calls():
            self.end_call(call.call_id)

        self.logger.info("PBX system stopped")

    def _extract_contact_address(
        self, contact: str | None, addr: tuple[str, int]
    ) -> tuple[str, int]:
        """
        Extract SIP address and port from Contact header.

        SIP phones send their listening address in the Contact header during
        REGISTER. This is more reliable than the UDP source address, which
        might use an ephemeral port.

        Args:
            contact: Contact header value (e.g., "<sip:1501@192.168.1.100:5060>")
            addr: Fallback address (UDP source address)

        Returns:
            Tuple of (ip_address, port) to use for phone registration
        """
        if not contact:
            return addr

        # Parse Contact header to extract SIP address and port
        # Format: <sip:extension@ip:port[;params]> or <sip:extension@ip[;params]>
        contact_match = re.search(r"sip:[^@]+@([^:;>]+)(?::(\d+))?", contact)
        if contact_match:
            ip_address = contact_match.group(1)
            port_str = contact_match.group(2)
            port = int(port_str) if port_str else 5060

            # Validate the extracted address
            if ip_address and port:
                contact_addr = (ip_address, port)
                # Log when we use Contact header address instead of UDP source
                if contact_addr != addr:
                    self.logger.debug(
                        f"Using Contact address {contact_addr} instead of UDP source {addr}"
                    )
                return contact_addr

        # If we can't parse the Contact header, fall back to UDP source address
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

        self.logger.warning(f"Could not parse extension from {from_header}")
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
        registered_addr = addr

        # Verify extension exists - check database first, then config
        extension_exists = False

        # Check extensions database table first (if available)
        if self.extension_db:
            try:
                db_extension = self.extension_db.get(extension_number)
                if db_extension:
                    extension_exists = True
                    self.logger.debug(f"Extension {extension_number} found in database")

                    # Ensure extension is loaded in registry (if not already)
                    if not self.extension_registry.get(extension_number):
                        extension_obj = ExtensionRegistry.create_extension_from_db(db_extension)
                        self.extension_registry.extensions[extension_number] = extension_obj
                        self.logger.debug(
                            f"Loaded extension {extension_number} into registry from database"
                        )
            except (KeyError, TypeError, ValueError) as e:
                self.logger.debug(f"Error checking extension in database: {e}")

        # Fall back to config if not found in database or database not available
        if not extension_exists:
            extension = self.config.get_extension(extension_number)
            if extension:
                extension_exists = True
                self.logger.debug(f"Extension {extension_number} found in config")

        if extension_exists:
            # Handle Expires: 0 as unregistration per RFC 3261 Section 10.2.2.
            if expires == 0:
                self.extension_registry.unregister(extension_number)
                self.logger.info(f"Extension {extension_number} unregistered (Expires: 0)")
            else:
                # Extract the phone's listening address from Contact header
                registered_addr = self._extract_contact_address(contact, addr)
                self.extension_registry.register(extension_number, registered_addr, expires=expires)
                self.logger.info(f"Extension {extension_number} registered from {registered_addr}")

                # Store phone registration in database (skip for unregistration)
            # Store phone registration in database (skip for unregistration)
            if self.registered_phones_db and expires > 0:
                ip_address = registered_addr[0]
                mac_address = self._extract_mac_address(contact, user_agent)

                try:
                    _, stored_mac = self.registered_phones_db.register_phone(
                        extension_number=extension_number,
                        ip_address=ip_address,
                        mac_address=mac_address,
                        user_agent=user_agent,
                        contact_uri=contact,
                    )

                    if stored_mac:
                        self.logger.info(
                            f"Stored phone registration: ext={extension_number}, ip={ip_address}, mac={stored_mac}"
                        )
                    else:
                        self.logger.info(
                            f"Stored phone registration: ext={extension_number}, ip={ip_address} (no MAC)"
                        )
                except Exception as e:
                    self.logger.error(f"Failed to store phone registration in database: {e}")
                    self.logger.error(f"  Extension: {extension_number}")
                    self.logger.error(f"  IP Address: {ip_address}")
                    self.logger.error(f"  MAC Address: {mac_address}")
                    self.logger.error(f"  User Agent: {user_agent}")
                    self.logger.error(f"  Contact URI: {contact}")
                    self.logger.error(f"  Traceback: {traceback.format_exc()}")

            # Record successful registration metric
            if self.metrics_exporter:
                event = "unregistration" if expires == 0 else "success"
                self.metrics_exporter.record_extension_registration(event)

            # Trigger webhook event for registrations (not unregistrations)
            if expires > 0:
                self.webhook_system.trigger_event(
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
        if self.metrics_exporter:
            self.metrics_exporter.record_extension_registration("failure")
        self.logger.warning(f"Unknown extension {extension_number} attempted registration")
        return False

    def _extract_mac_address(self, contact: str | None, user_agent: str | None) -> str | None:
        """
        Extract MAC address from SIP headers

        Args:
            contact: Contact header
            user_agent: User-Agent header

        Returns:
            MAC address string or None
        """
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
            self.logger.debug(f"Failed to parse MAC address from SIP headers: {e}")
            return None

    def _detect_phone_model(self, user_agent: str | None) -> str | None:
        """
        Detect phone model from User-Agent string.

        Delegates to :meth:`CodecNegotiator._detect_phone_model`.
        """
        return self.codec_negotiator._detect_phone_model(user_agent)

    def _should_skip_static_rtpmap(self, phone_model: str | None) -> bool:
        """
        Check whether to omit a=rtpmap lines for static payload types.

        Delegates to :meth:`CodecNegotiator._should_skip_static_rtpmap`.
        """
        return self.codec_negotiator._should_skip_static_rtpmap(phone_model)

    def _get_codecs_for_phone_model(
        self, phone_model: str | None, default_codecs: list[str] | None = None
    ) -> list[str]:
        """
        Get appropriate codec list for a specific phone model.

        Delegates to :meth:`CodecNegotiator._get_codecs_for_phone_model`.
        """
        return self.codec_negotiator._get_codecs_for_phone_model(phone_model, default_codecs)

    def _get_compatible_codecs(
        self, phone_model: str | None, answered_codecs: list[str] | None
    ) -> list[str]:
        """
        Compute codecs compatible with both the phone model and the answered codec set.

        Delegates to :meth:`CodecNegotiator._get_compatible_codecs`.
        """
        return self.codec_negotiator._get_compatible_codecs(phone_model, answered_codecs)

    def _get_phone_user_agent(self, extension_number: str) -> str | None:
        """
        Get User-Agent string for a registered phone by extension number.

        Delegates to :meth:`CodecNegotiator._get_phone_user_agent`.
        """
        return self.codec_negotiator._get_phone_user_agent(extension_number)

    def _get_dtmf_payload_type(self) -> int:
        """
        Get DTMF payload type from configuration.

        Delegates to :meth:`CodecNegotiator._get_dtmf_payload_type`.
        """
        return self.codec_negotiator._get_dtmf_payload_type()

    def _get_ilbc_mode(self) -> int:
        """
        Get iLBC mode from configuration.

        Delegates to :meth:`CodecNegotiator._get_ilbc_mode`.
        """
        return self.codec_negotiator._get_ilbc_mode()

    def _get_server_ip(self) -> str:
        """
        Get server's IP address for SDP

        Returns:
            Server IP address as string
        """
        # First, try to get configured external IP
        external_ip = self.config.get("server.external_ip")
        if external_ip:
            return str(external_ip)

        # Fallback: try to detect local IP
        import socket

        try:
            # Create a socket to determine the local IP
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip: str = s.getsockname()[0]
            s.close()
            return ip
        except OSError:
            return "127.0.0.1"  # Last resort fallback

    def end_call(self, call_id: str) -> None:
        """
        End call

        Args:
            call_id: Call identifier
        """
        call = self.call_manager.get_call(call_id)
        if call:
            self.logger.info(f"Ending call {call_id}")

            # Cancel INVITE retransmission if still running
            if hasattr(call, "invite_transaction") and call.invite_transaction:
                call.invite_transaction.cancel()
                call.invite_transaction = None

            # Cancel no-answer timer if still running
            if call.no_answer_timer:
                call.no_answer_timer.cancel()
                call.no_answer_timer = None

            # If this is a voicemail recording, complete it first
            if call.routed_to_voicemail and hasattr(call, "voicemail_recorder"):
                # Cancel the timer if it exists
                if hasattr(call, "voicemail_timer") and call.voicemail_timer:
                    call.voicemail_timer.cancel()

                # Get the recorder
                recorder = call.voicemail_recorder
                if recorder and recorder.running:
                    # Stop recording
                    recorder.stop()

                    # Get recorded audio
                    audio_data = recorder.get_recorded_audio()
                    duration = recorder.get_duration()

                    if audio_data and len(audio_data) > 0:
                        # Build WAV file using the codec detected during recording
                        codec_pt = getattr(recorder, "detected_codec", 0) or 0
                        wav_data = self._build_wav_file(audio_data, codec_payload_type=codec_pt)

                        # Save to voicemail system
                        self.voicemail_system.save_message(
                            extension_number=call.to_extension,
                            caller_id=call.from_extension,
                            audio_data=wav_data,
                            duration=duration,
                        )
                        self.logger.info(
                            f"Saved voicemail (on hangup) for extension {call.to_extension} from {call.from_extension}, duration: {duration}s"
                        )
                    else:
                        self.logger.warning(f"No audio recorded for voicemail on call {call_id}")

            # Record call end metric
            if self.metrics_exporter:
                call_duration = call.get_duration() if hasattr(call, "get_duration") else 0.0
                self.metrics_exporter.record_call_end(
                    duration=call_duration,
                    status="completed",
                    direction=getattr(call, "direction", "inbound"),
                )

            self.call_manager.end_call(call_id)
            # Stop any hold music before releasing the relay it runs on
            # (no-op when the call was not on hold).
            self.moh_system.stop_moh(call_id)
            self.rtp_relay.release_relay(call_id)

            # End CDR record for analytics
            self.cdr_system.end_record(call_id, hangup_cause="normal_clearing")

    def handle_dtmf_info(self, call_id: str, dtmf_digit: str) -> None:
        """
        Handle DTMF digit received via SIP INFO message

        This method queues DTMF digits received via out-of-band SIP INFO
        signaling for processing by IVR systems (voicemail, auto-attendant).

        Args:
            call_id: Call identifier
            dtmf_digit: DTMF digit ('0'-'9', '*', '#', 'A'-'D')

        Note:
            IVR loops (voicemail_handler, auto_attendant_handler) check
            call.dtmf_info_queue for queued SIP INFO digits and fall back
            to in-band DTMF detection when the queue is empty.
        """
        call = self.call_manager.get_call(call_id)
        if not call:
            # Silently ignore DTMF for calls that have already ended
            # This is common when phones buffer DTMF and send it after BYE
            self.logger.debug(f"Received DTMF INFO for ended/unknown call {call_id} - ignoring")
            return

        # Create DTMF queue if it doesn't exist
        if not hasattr(call, "dtmf_info_queue"):
            call.dtmf_info_queue = []

        # Queue the DTMF digit for processing
        call.dtmf_info_queue.append(dtmf_digit)

        # Log based on call type
        if hasattr(call, "voicemail_ivr") and call.voicemail_ivr:
            self.logger.info(
                f"Queued DTMF '{dtmf_digit}' from SIP INFO for voicemail IVR on call {call_id}"
            )
        elif hasattr(call, "auto_attendant_active") and call.auto_attendant_active:
            self.logger.info(
                f"Queued DTMF '{dtmf_digit}' from SIP INFO for auto-attendant on call {call_id}"
            )
        else:
            self.logger.debug(f"Queued DTMF '{dtmf_digit}' from SIP INFO for call {call_id}")

    def _resolve_hold_relay(self, call: Any, held_by: str) -> tuple[str, str]:
        """
        Resolve which relay carries a call's media and which relay side
        should hear MOH while `held_by` holds the call.

        Mirrors SIPServer._handle_reinvite's relay resolution: a peer leg
        left over from an earlier transfer bridge owns no relay of its own,
        so the live media flows through the bridged relay owner instead.

        Args:
            call: Call to resolve.
            held_by: Which leg is holding ("caller" or "callee").

        Returns:
            Tuple of (relay_call_id, held_side).
        """
        relay_call_id = call.call_id
        peer_side: str | None = None
        if (
            self.rtp_relay.get_handler(call.call_id) is None
            and call.bridged_peer_call_id
            and call.bridge_peer_side
        ):
            owner = self.call_manager.get_call(call.bridged_peer_call_id)
            if owner and self.rtp_relay.get_handler(owner.call_id) is not None:
                relay_call_id = owner.call_id
                peer_side = call.bridge_peer_side

        held_side = (
            ("a" if peer_side == "b" else "b")
            if peer_side
            else ("b" if held_by == "caller" else "a")
        )
        return relay_call_id, held_side

    def hold_call(self, call_id: str, held_by: str = "callee") -> bool:
        """
        Put a call on hold and start music-on-hold for the other party.

        Mirrors the hold behavior of a phone-initiated re-INVITE (see
        SIPServer._handle_reinvite) so a hold placed through the API or
        internally (e.g. consultation_transfer_start) sounds the same to the
        held party as one signaled by a phone.

        Args:
            call_id: Call identifier
            held_by: Which leg is initiating the hold ("caller" or "callee").
                Defaults to "callee" since API/PBX-initiated holds represent
                the local extension holding the other party.

        Returns:
            True if call put on hold
        """
        call = self.call_manager.get_call(call_id)
        if not call:
            return False

        call.hold(held_by=held_by)

        relay_call_id, held_side = self._resolve_hold_relay(call, held_by)
        relay_handler = self.rtp_relay.get_handler(relay_call_id)
        if relay_handler:
            self.moh_system.start_moh(relay_call_id, relay_handler, held_side)

        self.logger.info(f"Call {call_id} put on hold")
        return True

    def resume_call(self, call_id: str) -> bool:
        """
        Resume call from hold and stop music-on-hold.

        Args:
            call_id: Call identifier

        Returns:
            True if call resumed
        """
        call = self.call_manager.get_call(call_id)
        if not call:
            return False

        relay_call_id, _ = self._resolve_hold_relay(call, call.held_by or "callee")
        call.resume()
        self.moh_system.stop_moh(relay_call_id)

        self.logger.info(f"Call {call_id} resumed")
        return True

    def _build_wav_file(self, audio_data: bytes, codec_payload_type: int = 0) -> bytes:
        """
        Build a proper WAV file from raw audio data.

        Handles multiple codec types by detecting the RTP payload type used
        during recording.  For codecs without a standard WAV format (G.722,
        G.729), the raw data is stored as u-law to ensure compatibility with
        standard WAV players.

        Args:
            audio_data: Raw audio payload data
            codec_payload_type: RTP payload type that was used during recording.
                0 = PCMU (u-law), 8 = PCMA (A-law), 9 = G.722, etc.

        Returns:
            bytes: Complete WAV file
        """
        from pbx.utils.audio import WAV_FORMAT_ALAW, WAV_FORMAT_ULAW, build_wav_header

        if codec_payload_type == 8:
            audio_format = WAV_FORMAT_ALAW
        else:
            if codec_payload_type == 9:
                # G.722 has no standard WAV format code and there is no G.722
                # decoder integrated yet, so the raw data is tagged as u-law
                # instead. This won't sound perfect, but phones typically
                # negotiate PCMU for voicemail recording, so this path is rare.
                self.logger.info("Voicemail recorded in G.722, converting to u-law for WAV storage")
            audio_format = WAV_FORMAT_ULAW

        header = build_wav_header(
            len(audio_data),
            sample_rate=8000,
            channels=1,
            bits_per_sample=8,
            audio_format=audio_format,
        )
        return header + audio_data

    def get_status(self) -> dict[str, Any]:
        """
        Get PBX status

        Returns:
            Dictionary with status information
        """
        return {
            "running": self.running,
            "registered_extensions": self.extension_registry.get_registered_count(),
            "active_calls": len(self.call_manager.get_active_calls()),
            "total_calls": len(self.call_manager.call_history),
            "active_recordings": len(self.recording_system.active_recordings),
            "active_conferences": len(self.conference_system.get_active_rooms()),
            "parked_calls": len(self.parking_system.get_parked_calls()),
            "queued_calls": sum(len(q.queue) for q in self.queue_system.queues.values()),
        }

    def get_ad_integration_status(self) -> dict[str, Any]:
        """
        Get Active Directory integration status

        Returns:
            Dictionary with AD integration status
        """
        if not self.ad_integration:
            return {
                "enabled": False,
                "connected": False,
                "auto_provision": False,
                "server": None,
                "last_sync": None,
                "synced_users": 0,
                "error": None,
            }

        # Try to connect to check status
        connected = False
        error = None
        try:
            connected = self.ad_integration.connect()
        except Exception as e:
            error = str(e)

        # Get count of AD-synced extensions
        synced_count = 0
        if self.extension_db:
            try:
                synced_extensions = self.extension_db.get_ad_synced()
                synced_count = len(synced_extensions)
            except Exception as e:
                self.logger.error(f"Error getting AD-synced extension count: {e}")

        return {
            "enabled": self.ad_integration.enabled,
            "connected": connected,
            "auto_provision": self.ad_integration.auto_provision,
            "server": self.ad_integration.ldap_server,
            "synced_users": synced_count,
            "error": error,
        }

    def sync_ad_users(self) -> dict[str, Any]:
        """
        Manually trigger Active Directory user synchronization

        Returns:
            dict: Sync results with count and status
        """
        if not self.ad_integration:
            return {
                "success": False,
                "error": "Active Directory integration is not enabled",
                "synced_count": 0,
            }

        if not self.ad_integration.enabled:
            return {
                "success": False,
                "error": "Active Directory integration is disabled",
                "synced_count": 0,
            }

        try:
            self.logger.info("Manual AD user sync triggered")
            sync_result = self.ad_integration.sync_users(
                extension_registry=self.extension_registry,
                extension_db=self.extension_db,
                phone_provisioning=(
                    self.phone_provisioning if hasattr(self, "phone_provisioning") else None
                ),
            )

            # Handle both old (int) and new (dict) return types for backward
            # compatibility
            if isinstance(sync_result, int):
                synced_count = sync_result
                extensions_to_reboot = []
            else:
                synced_count = sync_result.get("synced_count", 0)
                extensions_to_reboot = sync_result.get("extensions_to_reboot", [])

            # Reload extensions after sync
            self.extension_registry.reload()

            # Automatically sync phone book from AD if enabled
            phone_book_synced = 0
            if (
                hasattr(self, "phone_book")
                and self.phone_book
                and self.phone_book.enabled
                and self.phone_book.auto_sync_from_ad
            ):
                self.logger.info("Auto-syncing phone book from Active Directory after AD user sync")
                phone_book_synced = self.phone_book.sync_from_ad(
                    self.ad_integration, self.extension_registry
                )
                self.logger.info(
                    f"Phone book synced {phone_book_synced} entries from Active Directory"
                )

            # Automatically trigger phone reboots for updated extensions
            rebooted_count = 0
            if (
                extensions_to_reboot
                and hasattr(self, "phone_provisioning")
                and self.phone_provisioning
            ):
                self.logger.info(
                    f"Auto-provisioning: Automatically rebooting {len(extensions_to_reboot)} phones after AD sync"
                )
                for extension_number in extensions_to_reboot:
                    try:
                        if self.phone_provisioning.reboot_phone(extension_number, self.sip_server):
                            rebooted_count += 1
                    except Exception as reboot_error:
                        self.logger.warning(
                            f"Could not reboot phone for extension {extension_number}: {reboot_error}"
                        )

                if rebooted_count > 0:
                    self.logger.info(
                        f"Auto-provisioning: Successfully triggered reboot for {rebooted_count} phones"
                    )

            return {
                "success": True,
                "synced_count": synced_count,
                "rebooted_count": rebooted_count,
                "phone_book_synced": phone_book_synced,
                "error": None,
            }
        except Exception as e:
            self.logger.error(f"Error during AD sync: {e}")
            import traceback

            traceback.print_exc()
            return {
                "success": False,
                "error": str(e),
                "synced_count": 0,
                "rebooted_count": 0,
                "phone_book_synced": 0,
            }
