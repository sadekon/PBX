"""
Feature initialization for PBX Core

Extracts the feature subsystem initialization logic from PBXCore.__init__
into a dedicated class for better modularity and maintainability.
"""

import logging
from typing import Any

from pbx.features.call_parking import CallParkingSystem
from pbx.features.call_queue import QueueSystem
from pbx.features.call_recording import CONSENT_KEY, CallRecordingSystem
from pbx.features.cdr import CDRSystem
from pbx.features.conference import ConferenceSystem
from pbx.features.find_me_follow_me import FindMeFollowMe
from pbx.features.fraud_detection import FraudDetectionSystem
from pbx.features.inbound_routing import InboundRoutingSystem
from pbx.features.music_on_hold import MusicOnHold
from pbx.features.phone_provisioning import PhoneProvisioning
from pbx.features.presence import PresenceSystem
from pbx.features.recording_retention import RecordingRetentionManager
from pbx.features.retention import (
    CONFIG_SECTION as RETENTION_CONFIG_SECTION,
    RetentionSettings,
    RetentionSweeper,
)
from pbx.features.sip_trunk import SIPTrunkSystem
from pbx.features.time_based_routing import TimeBasedRouting
from pbx.features.voicemail import VoicemailSystem
from pbx.mail import Mailer, SmtpSettings
from pbx.speech import (
    TranscriptionSettings,
    TranscriptionWorker,
    TranscriptStore,
    build_backend,
)
from pbx.speech.settings import CONFIG_SECTION as TRANSCRIPTION_CONFIG_SECTION


class FeatureInitializer:
    """Handles initialization of all PBX feature subsystems"""

    @staticmethod
    def initialize(pbx_core: Any) -> None:
        """
        Initialize all feature subsystems on a PBXCore instance.

        This method sets up all the feature subsystems (voicemail, conferencing,
        recording, queues, presence, parking, CDR, music-on-hold, trunks,
        integrations, security, and more) on the given PBXCore instance.

        Args:
            pbx_core: The PBXCore instance to initialize features on
        """
        config = pbx_core.config
        logger = pbx_core.logger
        database = pbx_core.database

        # Mail transport comes first: voicemail and emergency notification both need it.
        # Set unconditionally, even when SMTP is unconfigured -- an unconfigured Mailer is a
        # real object that reports EmailConfigError, so no caller needs a hasattr() guard.
        # Guarding is exactly what emergency notification used to do, and getting it wrong is
        # why Kari's Law email silently logged instead of sending.
        smtp_config: dict[str, Any] = config.get("smtp", {}) or {}
        pbx_core.mailer = Mailer(SmtpSettings.from_dict(smtp_config))
        pbx_core.mailer.start()
        logger.info("Mail subsystem initialized (enabled=%s)", pbx_core.mailer.enabled)

        # Transcription is constructed on the same terms as the mailer, and for the same
        # reasons: unconditionally, so a disabled or model-less worker is still a real object
        # that reports its own unavailability rather than something callers must guard
        # against; and exactly once, because the model is far too large to load per mailbox.
        transcription_settings = TranscriptionSettings.from_dict(
            config.get(TRANSCRIPTION_CONFIG_SECTION, {}) or {}
        )
        backend = build_backend(transcription_settings, logger=logger)
        pbx_core.transcription_service = TranscriptionWorker(backend, transcription_settings)
        pbx_core.transcription_service.start()
        # Reachable from the API the same way every other subsystem is, so admin routes
        # read transcripts through one place instead of writing their own SQL.
        pbx_core.transcript_store = TranscriptStore(database, logger)

        # Report configuration problems once, here, rather than per message. A model that was
        # never downloaded -- or unzipped one level too deep -- is the likely failure after a
        # deploy and is otherwise invisible until someone notices transcripts are missing.
        for problem in transcription_settings.validate():
            logger.warning("Transcription config: %s", problem)
        logger.info(
            "Transcription subsystem initialized (enabled=%s, ready=%s, workers=%d)",
            transcription_settings.enabled,
            bool(backend is not None and getattr(backend, "ready", False)),
            transcription_settings.workers,
        )

        # Initialize advanced features
        voicemail_path: str = config.get("voicemail.storage_path", "voicemail")
        pbx_core.voicemail_system = VoicemailSystem(
            storage_path=voicemail_path,
            config=config,
            database=database if hasattr(pbx_core, "database") and database.enabled else None,
            mailer=pbx_core.mailer,
            transcription_service=pbx_core.transcription_service,
        )
        pbx_core.conference_system = ConferenceSystem()
        pbx_core.recording_system = CallRecordingSystem(
            auto_record=config.get("features.call_recording", False),
            consent_acknowledged=config.get(CONSENT_KEY, False),
        )
        FeatureInitializer._wire_call_recording(pbx_core)
        pbx_core.queue_system = QueueSystem(
            database=database if database.enabled else None, config=config
        )
        if config.get("features.call_queues", True):
            pbx_core.queue_system.load_or_seed()
        pbx_core.presence_system = PresenceSystem()
        pbx_core.parking_system = CallParkingSystem()
        pbx_core.cdr_system = CDRSystem()
        pbx_core.moh_system = MusicOnHold()
        pbx_core.trunk_system = SIPTrunkSystem(
            config=config, sip_server=pbx_core.sip_server, trunk_db=pbx_core.trunk_db
        )
        pbx_core.inbound_routing = InboundRoutingSystem(
            inbound_route_db=pbx_core.inbound_route_db, extension_db=pbx_core.extension_db
        )

        # Initialize statistics engine for analytics
        from pbx.features.statistics import StatisticsEngine

        pbx_core.statistics_engine = StatisticsEngine(pbx_core.cdr_system)
        pbx_core._log_startup("Statistics and analytics engine initialized")
        logger.info("QoS monitoring system initialized and integrated with RTP relay")

        # Initialize auto attendant if enabled
        if config.get("features.auto_attendant", False):
            from pbx.features.auto_attendant import AutoAttendant

            pbx_core.auto_attendant = AutoAttendant(config, pbx_core)
            logger.info(
                f"Auto Attendant initialized on extension {pbx_core.auto_attendant.get_extension()}"
            )
        else:
            pbx_core.auto_attendant = None

        # Initialize phone provisioning if enabled
        if config.get("provisioning.enabled", False):
            pbx_core.phone_provisioning = PhoneProvisioning(
                config, database=database if database.enabled else None
            )
            pbx_core._load_provisioning_devices()
        else:
            pbx_core.phone_provisioning = None

        # Initialize Active Directory integration
        if config.get("integrations.active_directory.enabled", False):
            FeatureInitializer._init_active_directory(pbx_core, config)
        else:
            pbx_core.ad_integration = None

        # Initialize open-source integrations
        FeatureInitializer._init_open_source_integrations(pbx_core, config, logger)

        # Initialize phone book if enabled
        if config.get("features.phone_book.enabled", False):
            from pbx.features.phone_book import PhoneBook

            pbx_core.phone_book = PhoneBook(config, database=database if database.enabled else None)
            logger.info("Phone book feature initialized")
        else:
            pbx_core.phone_book = None

        # Initialize emergency notification system if enabled
        if config.get("features.emergency_notification.enabled", True):
            from pbx.features.emergency_notification import EmergencyNotificationSystem

            pbx_core.emergency_notification = EmergencyNotificationSystem(
                pbx_core, config=config, database=database if database.enabled else None
            )
            logger.info("Emergency notification system initialized")
        else:
            pbx_core.emergency_notification = None

        # Initialize paging system if enabled
        if config.get("features.paging.enabled", False):
            from pbx.features.paging import PagingSystem

            pbx_core.paging_system = PagingSystem(
                config, database=database if database.enabled else None
            )
            pbx_core.paging_system.pbx_core = pbx_core
            pbx_core._log_startup("Paging system initialized")
        else:
            pbx_core.paging_system = None

        # Initialize E911 location service if enabled
        if config.get("features.e911.enabled", False):
            from pbx.features.e911_location import E911LocationService

            pbx_core.e911_location = E911LocationService(config=config)
            logger.info("E911 location service initialized")
        else:
            pbx_core.e911_location = None

        # Initialize Kari's Law compliance (federal requirement for direct 911 dialing)
        if config.get("features.karis_law.enabled", True):
            from pbx.features.karis_law import KarisLawCompliance

            pbx_core.karis_law = KarisLawCompliance(pbx_core, config=config)
            logger.info("Kari's Law compliance initialized (direct 911 dialing enabled)")
        else:
            pbx_core.karis_law = None
            logger.warning("Kari's Law compliance DISABLED - not recommended for production")

        # Initialize webhook system
        from pbx.features.webhooks import WebhookSystem

        pbx_core.webhook_system = WebhookSystem(config)

        # Initialize WebRTC if enabled
        if config.get("features.webrtc.enabled", False):
            from pbx.features.webrtc import WebRTCGateway, WebRTCSignalingServer

            pbx_core.webrtc_signaling = WebRTCSignalingServer(config, pbx_core)
            pbx_core.webrtc_gateway = WebRTCGateway(pbx_core)
            logger.info("WebRTC browser calling initialized")
        else:
            pbx_core.webrtc_signaling = None
            pbx_core.webrtc_gateway = None

        # Initialize CRM integration if enabled
        if config.get("features.crm_integration.enabled", False):
            from pbx.features.crm_integration import CRMIntegration

            pbx_core.crm_integration = CRMIntegration(config, pbx_core)
            logger.info("CRM integration and screen pop initialized")
        else:
            pbx_core.crm_integration = None

        # Initialize hot-desking if enabled
        if config.get("features.hot_desking.enabled", False):
            from pbx.features.hot_desking import HotDeskingSystem

            pbx_core.hot_desking = HotDeskingSystem(config, pbx_core)
            logger.info("Hot-desking system initialized")
        else:
            pbx_core.hot_desking = None

        # Initialize Find Me/Follow Me
        pbx_core.find_me_follow_me = FindMeFollowMe(
            config=config, database=database if database.enabled else None
        )
        if pbx_core.find_me_follow_me.enabled:
            logger.info("Find Me/Follow Me initialized")

        # Initialize Time-Based Routing
        pbx_core.time_based_routing = TimeBasedRouting(config=config)
        if pbx_core.time_based_routing.enabled:
            logger.info("Time-based routing initialized")

        # Initialize Recording Retention Manager
        # Retention owns the only thing in the PBX that deletes user data on a timer, so it
        # is constructed with dry_run defaulted on and says so at startup.
        pbx_core.retention_sweeper = RetentionSweeper(
            RetentionSettings.from_dict(config.get(RETENTION_CONFIG_SECTION, {}) or {}),
            database=database,
            logger=logger,
        )
        pbx_core.retention_sweeper.start()

        pbx_core.recording_retention = RecordingRetentionManager(config=config)
        if pbx_core.recording_retention.enabled:
            logger.info("Recording retention manager initialized")

        # Initialize Fraud Detection System
        pbx_core.fraud_detection = FraudDetectionSystem(config=config)
        if pbx_core.fraud_detection.enabled:
            logger.info("Fraud detection system initialized")

        # Initialize Callback Queue
        from pbx.features.callback_queue import CallbackQueue

        pbx_core.callback_queue = CallbackQueue(config=config, database=database)
        if pbx_core.callback_queue.enabled:
            logger.info("Callback queue system initialized")

        # Initialize Mobile Push Notifications
        from pbx.features.mobile_push import MobilePushNotifications

        pbx_core.mobile_push = MobilePushNotifications(config=config, database=database)
        if pbx_core.mobile_push.enabled:
            logger.info("Mobile push notifications initialized")

        # Initialize Recording Announcements
        from pbx.features.recording_announcements import RecordingAnnouncements

        pbx_core.recording_announcements = RecordingAnnouncements(config=config, database=database)
        if pbx_core.recording_announcements.enabled:
            logger.info("Recording announcements initialized")

        # Initialize MFA if enabled
        if config.get("security.mfa.enabled", False):
            from pbx.features.mfa import MFAManager

            pbx_core.mfa_manager = MFAManager(
                database=(database if hasattr(pbx_core, "database") and database.enabled else None),
                config=config,
            )
            logger.info("Multi-Factor Authentication (MFA) initialized")
        else:
            pbx_core.mfa_manager = None

        # Initialize enhanced threat detection if enabled
        if config.get("security.threat_detection.enabled", True):
            from pbx.utils.security import get_threat_detector

            pbx_core.threat_detector = get_threat_detector(
                database=(database if hasattr(pbx_core, "database") and database.enabled else None),
                config=config,
            )
            logger.info("Enhanced threat detection initialized")
        else:
            pbx_core.threat_detector = None

        # Initialize security runtime monitor (always enabled for FIPS compliance)
        # Note: webhook_system is initialized earlier, so it's always available
        from pbx.utils.security_monitor import get_security_monitor

        pbx_core.security_monitor = get_security_monitor(
            config=config, webhook_system=pbx_core.webhook_system
        )
        logger.info("Security runtime monitor initialized")

        # Initialize DND scheduler if enabled
        if config.get("features.dnd_scheduling.enabled", False):
            from pbx.features.dnd_scheduling import get_dnd_scheduler

            # Get Outlook integration if available
            outlook = None
            if hasattr(pbx_core, "integrations") and "outlook" in pbx_core.integrations:
                outlook = pbx_core.integrations["outlook"]

            pbx_core.dnd_scheduler = get_dnd_scheduler(
                presence_system=pbx_core.presence_system
                if hasattr(pbx_core, "presence_system")
                else None,
                outlook_integration=outlook,
                config=config,
            )
            pbx_core._log_startup("DND Scheduler initialized")
        else:
            pbx_core.dnd_scheduler = None

        # Initialize Warden SBC (Session Border Controller)
        if config.get("features.sbc.enabled", False):
            from pbx.features.session_border_controller import SessionBorderController

            pbx_core.sbc = SessionBorderController(config=config)
            if pbx_core.sbc.enabled:
                logger.info("Warden SBC (Session Border Controller) initialized")
        else:
            pbx_core.sbc = None

        # Initialize skills-based routing if enabled
        if config.get("features.skills_routing.enabled", False):
            from pbx.features.skills_routing import get_skills_router

            pbx_core.skills_router = get_skills_router(
                database=(database if hasattr(pbx_core, "database") and database.enabled else None),
                config=config,
            )
            logger.info("Skills-Based Routing initialized")
        else:
            pbx_core.skills_router = None

    @staticmethod
    def _wire_call_recording(pbx_core: Any) -> None:
        """
        Have every bridged call start recording itself.

        The trigger lives on the RTP relay rather than in the call router, because the relay
        is where audio actually is. A router-level hook only covers the one signalling path
        it was added to -- the earlier version missed WebRTC and PBX-originated calls
        entirely -- whereas anything that bridges two endpoints necessarily goes through
        here.

        This function is the only place that knows about both layers. ``pbx/rtp/`` holds a
        plain callback and never learns that recording exists, which keeps the dependency
        pointing the right way (features depend on rtp, not the reverse).
        """
        relay = getattr(pbx_core, "rtp_relay", None)
        if relay is None:
            return

        def on_bridged(handler: Any) -> None:
            """Called by the relay once both endpoints are known. Must not raise."""
            recording_system = getattr(pbx_core, "recording_system", None)
            if recording_system is None or not recording_system.auto_record:
                return

            # The relay knows a call_id and nothing else; names and the session come from
            # the call record, which this layer can reach and the RTP layer cannot.
            call = pbx_core.call_manager.get_call(handler.call_id)
            caller = getattr(call, "from_extension", None) or "unknown"
            callee = getattr(call, "to_extension", None) or "unknown"

            tap = recording_system.start_recording(
                handler.call_id,
                caller,
                callee,
                session_id=getattr(call, "session_id", None) or handler.call_id,
                # First-generation sources. A transfer bumps these, so the party who
                # arrives gets their own channel instead of the departed party's.
                labels={"a0": caller, "b0": callee},
            )
            if tap is not None:
                handler.attach_tap(tap)

        relay.on_bridged = on_bridged

        # Relays allocated before this ran would otherwise never record. Startup order puts
        # this well before any call, but a reload should not silently stop recording.
        for entry in getattr(relay, "active_relays", {}).values():
            entry["handler"].on_bridged = on_bridged

        FeatureInitializer._wire_recording_transcription(pbx_core)

    @staticmethod
    def _wire_recording_transcription(pbx_core: Any) -> None:
        """
        Transcribe each recording once it is finished.

        Post-call rather than live: a completed recording is just a file, and the shared
        worker already transcribes files. One pass per participant, because the recording
        already separated them and that is what makes the transcript say who spoke.
        """
        from pbx.speech.recording import DEFAULT_MAX_REGION_SECONDS, RecordingTranscriber
        from pbx.speech.store import TranscriptStore
        from pbx.utils.audio import SILENCE_RMS_FLOOR

        recording_system = getattr(pbx_core, "recording_system", None)
        worker = getattr(pbx_core, "transcription_service", None)
        if recording_system is None or worker is None:
            return

        database = getattr(pbx_core, "database", None)
        settings = getattr(worker, "settings", None)

        # Stay clear of the worker's own limit: it refuses anything longer, and a refused
        # region is audio that never gets transcribed at all. 90% leaves room for the padding
        # a region carries either side of its speech.
        cap = getattr(settings, "max_audio_seconds", 0) or 0
        max_region = (
            min(DEFAULT_MAX_REGION_SECONDS, cap * 0.9) if cap else DEFAULT_MAX_REGION_SECONDS
        )

        transcriber = RecordingTranscriber(
            worker=worker,
            store=TranscriptStore(database if getattr(database, "enabled", False) else None),
            silence_floor=getattr(settings, "silence_rms_floor", SILENCE_RMS_FLOOR),
            max_region_seconds=max_region,
        )
        pbx_core.recording_transcriber = transcriber
        recording_system.on_recording_finished = transcriber.submit

        # Anything still in the scratch directory belongs to a run that died; nothing can be
        # transcribing in a process that has only just started.
        transcriber.clear_scratch(recording_system.recording_path)

    @staticmethod
    def _init_active_directory(pbx_core: Any, config: Any) -> None:
        """Initialize Active Directory integration"""
        from pbx.integrations.active_directory import ActiveDirectoryIntegration

        config_file: str = config._config_file if hasattr(config, "_config_file") else "config.yml"

        ad_config: dict[str, Any] = {
            "integrations.active_directory.enabled": config.get(
                "integrations.active_directory.enabled"
            ),
            "integrations.active_directory.server": config.get(
                "integrations.active_directory.server"
            ),
            "integrations.active_directory.base_dn": config.get(
                "integrations.active_directory.base_dn"
            ),
            "integrations.active_directory.bind_dn": config.get(
                "integrations.active_directory.bind_dn"
            ),
            "integrations.active_directory.bind_password": config.get(
                "integrations.active_directory.bind_password"
            ),
            "integrations.active_directory.use_ssl": config.get(
                "integrations.active_directory.use_ssl", True
            ),
            "integrations.active_directory.auto_provision": config.get(
                "integrations.active_directory.auto_provision", False
            ),
            "integrations.active_directory.user_search_base": config.get(
                "integrations.active_directory.user_search_base"
            ),
            "integrations.active_directory.deactivate_removed_users": config.get(
                "integrations.active_directory.deactivate_removed_users", True
            ),
            "config_file": config_file,
        }
        pbx_core.ad_integration = ActiveDirectoryIntegration(ad_config)
        if pbx_core.ad_integration.enabled:
            pbx_core._log_startup("Active Directory integration initialized")

            # Auto-sync users from AD at startup if auto_provision is
            # enabled
            if pbx_core.ad_integration.auto_provision:
                pbx_core.logger.info(
                    "Auto-provisioning enabled - syncing users from Active Directory..."
                )
                try:
                    sync_result = pbx_core.ad_integration.sync_users(
                        extension_registry=pbx_core.extension_registry,
                        extension_db=pbx_core.extension_db,
                        phone_provisioning=None,  # Will be set after provisioning init
                    )

                    # Handle both int and dict return types
                    synced_count: int = (
                        sync_result
                        if isinstance(sync_result, int)
                        else sync_result.get("synced_count", 0)
                    )

                    if synced_count > 0:
                        pbx_core.logger.info(
                            f"Auto-synced {synced_count} extension(s) from Active Directory at startup"
                        )
                        # Reload extension registry to ensure all synced extensions are loaded
                        pbx_core.logger.info("Reloading extension registry from database...")
                        pbx_core.extension_registry.reload()
                        pbx_core.logger.info(
                            f"Extension registry reloaded: {len(pbx_core.extension_registry.extensions)} total extensions"
                        )
                    else:
                        pbx_core.logger.warning(
                            "AD auto-sync completed but no extensions were synced"
                        )
                except (KeyError, TypeError, ValueError) as e:
                    pbx_core.logger.error(
                        f"Failed to auto-sync users from Active Directory at startup: {e}"
                    )
                    import traceback

                    pbx_core.logger.debug(traceback.format_exc())
        else:
            pbx_core.logger.warning(
                "Active Directory integration enabled in config but failed to initialize"
            )
            pbx_core.ad_integration = None

    @staticmethod
    def _init_open_source_integrations(pbx_core: Any, config: Any, _logger: logging.Logger) -> None:
        """Initialize open-source and third-party integrations"""
        # Jitsi Meet - Video conferencing
        if config.get("integrations.jitsi.enabled", False):
            from pbx.integrations.jitsi import JitsiIntegration

            pbx_core.jitsi_integration = JitsiIntegration(config)
            if pbx_core.jitsi_integration.enabled:
                pbx_core._log_startup("Jitsi Meet video conferencing integration initialized")
        else:
            pbx_core.jitsi_integration = None

        # Matrix - Team messaging
        if config.get("integrations.matrix.enabled", False):
            from pbx.integrations.matrix import MatrixIntegration

            pbx_core.matrix_integration = MatrixIntegration(config)
            if pbx_core.matrix_integration.enabled:
                pbx_core._log_startup("Matrix team messaging integration initialized")
        else:
            pbx_core.matrix_integration = None

        # EspoCRM - Customer relationship management
        if config.get("integrations.espocrm.enabled", False):
            from pbx.integrations.espocrm import EspoCRMIntegration

            pbx_core.espocrm_integration = EspoCRMIntegration(config)
            if pbx_core.espocrm_integration.enabled:
                pbx_core._log_startup("EspoCRM integration initialized")
        else:
            pbx_core.espocrm_integration = None

        # Zoom integration (proprietary - requires license)
        if config.get("integrations.zoom.enabled", False):
            from pbx.integrations.zoom import ZoomIntegration

            pbx_core.zoom_integration = ZoomIntegration(config)
            if pbx_core.zoom_integration.enabled:
                pbx_core._log_startup("Zoom integration initialized")
        else:
            pbx_core.zoom_integration = None
