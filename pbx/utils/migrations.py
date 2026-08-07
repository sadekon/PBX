"""
Database migration system for PBX features
Manages schema versioning and migrations
"""

from pbx.utils.logger import get_logger


class MigrationManager:
    """
    Manages database schema migrations
    Tracks versions and applies migrations in order
    """

    def __init__(self, db_backend: object) -> None:
        """
        Initialize migration manager

        Args:
            db_backend: DatabaseBackend instance
        """
        self.logger = get_logger()
        self.db = db_backend
        self.migrations = []

    def _build_migration_sql(self, template: str) -> str:
        """
        Build PostgreSQL SQL from a template

        Converts template placeholders to PostgreSQL syntax.

        Args:
            template: SQL template with placeholders

        Returns:
            PostgreSQL SQL string
        """
        replacements = {
            "{SERIAL}": "SERIAL PRIMARY KEY",
            "{BOOLEAN_TRUE}": "TRUE",
            "{BOOLEAN_FALSE}": "FALSE",
            "{BYTEA}": "BYTEA",
            "{TEXT}": "TEXT",
        }

        result = template
        for placeholder, value in replacements.items():
            result = result.replace(placeholder, value)
        return result

    def register_migration(self, version: int, name: str, sql: str) -> None:
        """
        Register a migration

        Args:
            version: Migration version number
            name: Migration name/description
            sql: SQL to execute
        """
        self.migrations.append({"version": version, "name": name, "sql": sql})

    def init_migrations_table(self) -> bool:
        """Create migrations tracking table if it doesn't exist"""
        try:
            sql = """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """

            self.db.execute(sql)
            self.logger.info("Migrations table initialized")
            return True
        except Exception as e:
            self.logger.error(f"Failed to initialize migrations table: {e}")
            return False

    def get_current_version(self) -> int:
        """
        Get current schema version

        Returns:
            int: Current version number
        """
        try:
            result = self.db.fetch_one("SELECT MAX(version) as max_version FROM schema_migrations")
            if result and result.get("max_version") is not None:
                return result["max_version"]
            return 0
        except (KeyError, TypeError, ValueError) as e:
            self.logger.warning(f"Could not get current version: {e}")
            return 0

    def apply_migrations(self, target_version: int | None = None) -> bool:
        """
        Apply pending migrations

        Args:
            target_version: Version to migrate to (None = latest)

        Returns:
            bool: True if successful
        """
        try:
            self.init_migrations_table()
            current_version = self.get_current_version()

            # Sort migrations by version
            self.migrations.sort(key=lambda x: x["version"])

            # Filter migrations to apply
            pending = [m for m in self.migrations if m["version"] > current_version]

            if target_version:
                pending = [m for m in pending if m["version"] <= target_version]

            if not pending:
                self.logger.info("No pending migrations")
                return True

            self.logger.info(f"Applying {len(pending)} migrations...")

            for migration in pending:
                self.logger.info(f"Applying migration {migration['version']}: {migration['name']}")

                # Execute migration SQL using execute_script for multi-statement support
                self.db.execute_script(migration["sql"])

                # Record migration
                self.db.execute(
                    "INSERT INTO schema_migrations (version, name) VALUES (%s, %s)",
                    (migration["version"], migration["name"]),
                )

                self.logger.info(f"✓ Migration {migration['version']} applied")

            self.logger.info("All migrations applied successfully")
            return True

        except (KeyError, TypeError, ValueError) as e:
            self.logger.error(f"Migration failed: {e}")
            return False

    def get_migration_status(self) -> list[dict]:
        """
        Get status of all migrations

        Returns:
            list of migration status dictionaries
        """
        try:
            self.get_current_version()

            # Get applied migrations
            applied = self.db.fetch_all(
                "SELECT version, name, applied_at FROM schema_migrations ORDER BY version"
            )
            applied_versions = {row["version"]: row for row in (applied or [])}

            status = []
            for migration in sorted(self.migrations, key=lambda x: x["version"]):
                version = migration["version"]
                if version in applied_versions:
                    status.append(
                        {
                            "version": version,
                            "name": migration["name"],
                            "status": "applied",
                            "applied_at": applied_versions[version]["applied_at"],
                        }
                    )
                else:
                    status.append(
                        {
                            "version": version,
                            "name": migration["name"],
                            "status": "pending",
                            "applied_at": None,
                        }
                    )

            return status
        except (KeyError, TypeError, ValueError) as e:
            self.logger.error(f"Failed to get migration status: {e}")
            return []


def register_all_migrations(manager: MigrationManager) -> None:
    """
    Register all framework feature migrations

    Args:
        manager: MigrationManager instance
    """
    # Migration 1000: AI-Powered Features Framework
    manager.register_migration(
        1000,
        "AI Features Framework",
        manager._build_migration_sql("""
        -- Real-time speech analytics
        CREATE TABLE IF NOT EXISTS speech_analytics_configs (
            id {SERIAL},
            extension VARCHAR(20) NOT NULL,
            enabled BOOLEAN DEFAULT {BOOLEAN_TRUE},
            transcription_enabled BOOLEAN DEFAULT {BOOLEAN_TRUE},
            sentiment_enabled BOOLEAN DEFAULT {BOOLEAN_TRUE},
            summarization_enabled BOOLEAN DEFAULT {BOOLEAN_TRUE},
            keywords {TEXT},
            alert_threshold FLOAT DEFAULT 0.7,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Conversational AI assistant
        CREATE TABLE IF NOT EXISTS ai_assistant_configs (
            id {SERIAL},
            extension VARCHAR(20) NOT NULL,
            enabled BOOLEAN DEFAULT {BOOLEAN_FALSE},
            auto_response_enabled BOOLEAN DEFAULT {BOOLEAN_FALSE},
            greeting_template {TEXT},
            response_language VARCHAR(10) DEFAULT 'en',
            confidence_threshold FLOAT DEFAULT 0.8,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Voice biometrics
        CREATE TABLE IF NOT EXISTS voice_biometrics (
            id {SERIAL},
            extension VARCHAR(20) NOT NULL,
            voiceprint_data {BYTEA},
            enrollment_status VARCHAR(20) DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_verified TIMESTAMP
        );

        -- Call quality prediction
        CREATE TABLE IF NOT EXISTS call_quality_predictions (
            id {SERIAL},
            call_id VARCHAR(50),
            predicted_mos FLOAT,
            predicted_issues {TEXT},
            actual_mos FLOAT,
            prediction_accuracy FLOAT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """),
    )

    # Migration 1001: Video Conferencing Framework
    manager.register_migration(
        1001,
        "Video Conferencing Framework",
        manager._build_migration_sql("""
        -- Video conferencing rooms
        CREATE TABLE IF NOT EXISTS video_conference_rooms (
            id {SERIAL},
            room_name VARCHAR(100) NOT NULL UNIQUE,
            owner_extension VARCHAR(20),
            max_participants INTEGER DEFAULT 10,
            enable_4k BOOLEAN DEFAULT {BOOLEAN_FALSE},
            enable_screen_share BOOLEAN DEFAULT {BOOLEAN_TRUE},
            recording_enabled BOOLEAN DEFAULT {BOOLEAN_FALSE},
            password_hash VARCHAR(255),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Video conference participants
        CREATE TABLE IF NOT EXISTS video_conference_participants (
            id {SERIAL},
            room_id INTEGER REFERENCES video_conference_rooms(id),
            extension VARCHAR(20),
            display_name VARCHAR(100),
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            left_at TIMESTAMP,
            video_enabled BOOLEAN DEFAULT {BOOLEAN_TRUE},
            audio_enabled BOOLEAN DEFAULT {BOOLEAN_TRUE},
            screen_sharing BOOLEAN DEFAULT {BOOLEAN_FALSE}
        );

        -- Video codec configurations
        CREATE TABLE IF NOT EXISTS video_codec_configs (
            id {SERIAL},
            codec_name VARCHAR(50) NOT NULL,
            enabled BOOLEAN DEFAULT {BOOLEAN_TRUE},
            priority INTEGER DEFAULT 100,
            max_resolution VARCHAR(20) DEFAULT '1920x1080',
            max_bitrate INTEGER DEFAULT 2000,
            min_bitrate INTEGER DEFAULT 500
        );
    """),
    )

    # Migration 1002: Emergency Services Framework
    manager.register_migration(
        1002,
        "Emergency Services Framework",
        manager._build_migration_sql("""
        -- Nomadic E911 locations
        CREATE TABLE IF NOT EXISTS nomadic_e911_locations (
            id {SERIAL},
            extension VARCHAR(20) NOT NULL,
            ip_address VARCHAR(45),
            location_name VARCHAR(100),
            street_address VARCHAR(255),
            city VARCHAR(100),
            state VARCHAR(50),
            postal_code VARCHAR(20),
            country VARCHAR(50) DEFAULT 'USA',
            building VARCHAR(100),
            floor VARCHAR(20),
            room VARCHAR(20),
            latitude DECIMAL(10, 8),
            longitude DECIMAL(11, 8),
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            auto_detected BOOLEAN DEFAULT {BOOLEAN_FALSE}
        );

        -- E911 location updates log
        CREATE TABLE IF NOT EXISTS e911_location_updates (
            id {SERIAL},
            extension VARCHAR(20),
            old_location TEXT,
            new_location TEXT,
            update_source VARCHAR(50),
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Multi-site E911 configurations
        CREATE TABLE IF NOT EXISTS multi_site_e911_configs (
            id {SERIAL},
            site_name VARCHAR(100) NOT NULL,
            ip_range_start VARCHAR(45),
            ip_range_end VARCHAR(45),
            emergency_trunk VARCHAR(50),
            psap_number VARCHAR(20),
            elin VARCHAR(20),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """),
    )

    # Migration 1003: Analytics & Reporting Framework
    manager.register_migration(
        1003,
        "Analytics & Reporting Framework",
        manager._build_migration_sql("""
        -- BI integration configs
        CREATE TABLE IF NOT EXISTS bi_integration_configs (
            id {SERIAL},
            integration_type VARCHAR(50) NOT NULL,
            enabled BOOLEAN DEFAULT {BOOLEAN_FALSE},
            api_key_encrypted TEXT,
            endpoint_url VARCHAR(255),
            sync_interval INTEGER DEFAULT 3600,
            last_sync TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Call tags and categories
        CREATE TABLE IF NOT EXISTS call_tags (
            id {SERIAL},
            tag_name VARCHAR(50) NOT NULL UNIQUE,
            category VARCHAR(50),
            color VARCHAR(20),
            auto_apply_rules TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS call_tag_assignments (
            id {SERIAL},
            call_id VARCHAR(50) NOT NULL,
            tag_id INTEGER REFERENCES call_tags(id),
            assigned_by VARCHAR(50),
            assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            auto_assigned BOOLEAN DEFAULT {BOOLEAN_FALSE}
        );
    """),
    )

    # Migration 1004: Integration Framework
    manager.register_migration(
        1004,
        "Integration Framework",
        manager._build_migration_sql("""
        -- HubSpot integration
        CREATE TABLE IF NOT EXISTS hubspot_integration (
            id {SERIAL},
            enabled BOOLEAN DEFAULT {BOOLEAN_FALSE},
            api_key_encrypted TEXT,
            portal_id VARCHAR(50),
            sync_contacts BOOLEAN DEFAULT {BOOLEAN_TRUE},
            sync_deals BOOLEAN DEFAULT {BOOLEAN_TRUE},
            auto_create_contacts BOOLEAN DEFAULT {BOOLEAN_FALSE},
            last_sync TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Zendesk integration
        CREATE TABLE IF NOT EXISTS zendesk_integration (
            id {SERIAL},
            enabled BOOLEAN DEFAULT {BOOLEAN_FALSE},
            subdomain VARCHAR(100),
            api_token_encrypted TEXT,
            email VARCHAR(255),
            auto_create_tickets BOOLEAN DEFAULT {BOOLEAN_FALSE},
            default_priority VARCHAR(20) DEFAULT 'normal',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Integration activity log
        CREATE TABLE IF NOT EXISTS integration_activity_log (
            id {SERIAL},
            integration_type VARCHAR(50),
            action VARCHAR(100),
            status VARCHAR(20),
            details TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """),
    )

    # Migration 1005: Mobile Framework
    manager.register_migration(
        1005,
        "Mobile Framework",
        manager._build_migration_sql("""
        -- Mobile app installations
        CREATE TABLE IF NOT EXISTS mobile_app_installations (
            id {SERIAL},
            extension VARCHAR(20) NOT NULL,
            platform VARCHAR(20) NOT NULL,
            app_version VARCHAR(20),
            device_token VARCHAR(255),
            device_model VARCHAR(100),
            os_version VARCHAR(50),
            install_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_active TIMESTAMP,
            enabled BOOLEAN DEFAULT {BOOLEAN_TRUE}
        );

        -- Mobile number portability
        CREATE TABLE IF NOT EXISTS mobile_number_portability (
            id {SERIAL},
            extension VARCHAR(20) NOT NULL,
            mobile_number VARCHAR(20),
            carrier VARCHAR(100),
            port_status VARCHAR(20) DEFAULT 'pending',
            port_date TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """),
    )

    # Migration 1006: Advanced Call Features Framework
    manager.register_migration(
        1006,
        "Advanced Call Features Framework",
        manager._build_migration_sql("""
        -- Call blending configurations
        CREATE TABLE IF NOT EXISTS call_blending_configs (
            id {SERIAL},
            queue_name VARCHAR(100) NOT NULL,
            enabled BOOLEAN DEFAULT {BOOLEAN_FALSE},
            inbound_priority INTEGER DEFAULT 1,
            outbound_campaign_id INTEGER,
            blend_ratio FLOAT DEFAULT 0.5,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Predictive voicemail drop
        CREATE TABLE IF NOT EXISTS voicemail_drop_templates (
            id {SERIAL},
            template_name VARCHAR(100) NOT NULL,
            audio_file VARCHAR(255),
            duration_seconds INTEGER,
            enabled BOOLEAN DEFAULT {BOOLEAN_TRUE},
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Call recording analytics
        CREATE TABLE IF NOT EXISTS call_recording_analytics (
            id {SERIAL},
            recording_id VARCHAR(100),
            sentiment_score FLOAT,
            keywords_detected TEXT,
            compliance_score FLOAT,
            quality_score FLOAT,
            analyzed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """),
    )

    # Migration 1007: SIP Trunking Framework
    manager.register_migration(
        1007,
        "SIP Trunking Framework",
        manager._build_migration_sql("""
        -- Geographic redundancy
        CREATE TABLE IF NOT EXISTS trunk_geographic_regions (
            id {SERIAL},
            region_name VARCHAR(100) NOT NULL,
            trunk_ids TEXT,
            priority INTEGER DEFAULT 100,
            enabled BOOLEAN DEFAULT {BOOLEAN_TRUE},
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- DNS SRV failover
        CREATE TABLE IF NOT EXISTS dns_srv_configs (
            id {SERIAL},
            trunk_id INTEGER,
            srv_record VARCHAR(255),
            priority INTEGER,
            weight INTEGER,
            port INTEGER,
            last_tested TIMESTAMP,
            status VARCHAR(20) DEFAULT 'active'
        );

        -- Session Border Controller configs
        CREATE TABLE IF NOT EXISTS sbc_configs (
            id {SERIAL},
            sbc_name VARCHAR(100) NOT NULL,
            sbc_address VARCHAR(255),
            sbc_port INTEGER DEFAULT 5060,
            topology_hiding BOOLEAN DEFAULT {BOOLEAN_TRUE},
            nat_traversal BOOLEAN DEFAULT {BOOLEAN_TRUE},
            security_profiles TEXT,
            enabled BOOLEAN DEFAULT {BOOLEAN_TRUE}
        );
    """),
    )

    # Migration 1008: Collaboration Framework
    manager.register_migration(
        1008,
        "Collaboration Framework",
        manager._build_migration_sql("""
        -- Team messaging
        CREATE TABLE IF NOT EXISTS team_messaging_channels (
            id {SERIAL},
            channel_name VARCHAR(100) NOT NULL UNIQUE,
            description TEXT,
            is_private BOOLEAN DEFAULT {BOOLEAN_FALSE},
            created_by VARCHAR(20),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS team_messaging_members (
            id {SERIAL},
            channel_id INTEGER REFERENCES team_messaging_channels(id),
            extension VARCHAR(20),
            role VARCHAR(20) DEFAULT 'member',
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS team_messages (
            id {SERIAL},
            channel_id INTEGER REFERENCES team_messaging_channels(id),
            sender_extension VARCHAR(20),
            message_text TEXT,
            message_type VARCHAR(20) DEFAULT 'text',
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- File sharing
        CREATE TABLE IF NOT EXISTS shared_files (
            id {SERIAL},
            file_name VARCHAR(255) NOT NULL,
            file_path VARCHAR(500),
            file_size BIGINT,
            mime_type VARCHAR(100),
            uploaded_by VARCHAR(20),
            shared_with TEXT,
            description TEXT,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP
        );
    """),
    )

    # Migration 1009: Compliance Framework (SOC 2 type 2 only)
    # Note: PCI DSS and GDPR tables commented out as not required
    manager.register_migration(
        1009,
        "Compliance Framework",
        manager._build_migration_sql("""
        -- SOC 2 type 2 enhanced
        CREATE TABLE IF NOT EXISTS soc2_controls (
            id {SERIAL},
            control_id VARCHAR(50) NOT NULL,
            control_category VARCHAR(50),
            description TEXT,
            implementation_status VARCHAR(20),
            last_tested TIMESTAMP,
            test_results TEXT
        );

        -- Data residency (used by SOC 2)
        CREATE TABLE IF NOT EXISTS data_residency_configs (
            id {SERIAL},
            data_type VARCHAR(50),
            region VARCHAR(50),
            storage_location VARCHAR(255),
            encryption_required BOOLEAN DEFAULT {BOOLEAN_TRUE},
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """),
    )

    # Migration 1010: Click-to-Dial Framework
    manager.register_migration(
        1010,
        "Click-to-Dial Framework",
        manager._build_migration_sql("""
        -- Click-to-dial configurations
        CREATE TABLE IF NOT EXISTS click_to_dial_configs (
            id {SERIAL},
            extension VARCHAR(20) NOT NULL,
            enabled BOOLEAN DEFAULT {BOOLEAN_TRUE},
            default_caller_id VARCHAR(20),
            auto_answer BOOLEAN DEFAULT {BOOLEAN_FALSE},
            browser_notification BOOLEAN DEFAULT {BOOLEAN_TRUE},
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Click-to-dial history
        CREATE TABLE IF NOT EXISTS click_to_dial_history (
            id {SERIAL},
            extension VARCHAR(20),
            destination VARCHAR(20),
            call_id VARCHAR(50),
            source VARCHAR(50),
            initiated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            connected_at TIMESTAMP,
            status VARCHAR(20)
        );
    """),
    )

    # Migration 1011: SIP Trunk Persistence
    manager.register_migration(
        1011,
        "SIP Trunk Persistence",
        manager._build_migration_sql("""
        -- SIP trunk definitions (carrier/provider trunks for outbound routing)
        CREATE TABLE IF NOT EXISTS sip_trunks (
            id {SERIAL},
            trunk_id VARCHAR(50) UNIQUE NOT NULL,
            name VARCHAR(255) NOT NULL,
            host VARCHAR(255) NOT NULL,
            port INTEGER DEFAULT 5060,
            username VARCHAR(255) NOT NULL,
            password VARCHAR(255) NOT NULL,
            codec_preferences TEXT,
            priority INTEGER DEFAULT 100,
            max_channels INTEGER DEFAULT 10,
            health_check_interval INTEGER DEFAULT 60,
            enabled BOOLEAN DEFAULT {BOOLEAN_TRUE},
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """),
    )

    # Migration 1012: Inbound DID Routing
    manager.register_migration(
        1012,
        "Inbound DID Routing",
        manager._build_migration_sql("""
        -- DID -> internal destination mappings for calls arriving on a SIP trunk
        CREATE TABLE IF NOT EXISTS inbound_routes (
            id {SERIAL},
            did_number VARCHAR(20) NOT NULL,
            trunk_id VARCHAR(50),
            destination_type VARCHAR(20) NOT NULL,
            destination_value VARCHAR(50) NOT NULL,
            enabled BOOLEAN DEFAULT {BOOLEAN_TRUE},
            priority INTEGER DEFAULT 100,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (did_number, trunk_id)
        );
    """),
    )

    # Migration 1013: Call Queues (ACD)
    manager.register_migration(
        1013,
        "Call Queues",
        manager._build_migration_sql("""
        -- Queue definitions (config.yml queues: section seeds this once;
        -- the database is authoritative thereafter)
        CREATE TABLE IF NOT EXISTS call_queues (
            id {SERIAL},
            queue_number VARCHAR(20) UNIQUE NOT NULL,
            name VARCHAR(255) NOT NULL,
            strategy VARCHAR(20) NOT NULL DEFAULT 'round_robin',
            ring_timeout INTEGER DEFAULT 15,
            max_wait_time INTEGER DEFAULT 300,
            max_queue_size INTEGER DEFAULT 10,
            fallback_mailbox VARCHAR(20),
            auto_pause_misses INTEGER DEFAULT 3,
            enabled BOOLEAN DEFAULT {BOOLEAN_TRUE},
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Queue membership (which extensions serve which queue); kept
        -- separate from runtime state so external sync (e.g. AD calendar)
        -- can manage membership without touching login state
        CREATE TABLE IF NOT EXISTS queue_agents (
            id {SERIAL},
            queue_number VARCHAR(20) NOT NULL,
            extension VARCHAR(20) NOT NULL,
            penalty INTEGER DEFAULT 0,
            source VARCHAR(20) DEFAULT 'manual',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (queue_number, extension)
        );

        -- Global per-agent runtime state (login/pause survive restarts);
        -- one row per extension, shared across all queues
        CREATE TABLE IF NOT EXISTS queue_agent_state (
            id {SERIAL},
            extension VARCHAR(20) UNIQUE NOT NULL,
            logged_in BOOLEAN DEFAULT {BOOLEAN_FALSE},
            paused BOOLEAN DEFAULT {BOOLEAN_FALSE},
            pause_reason VARCHAR(30),
            consecutive_misses INTEGER DEFAULT 0,
            calls_taken INTEGER DEFAULT 0,
            last_call_time TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """),
    )

    # Migration 1014: Queue Hold Announcements
    # call_queues already exists on live installs (1013's CREATE TABLE
    # IF NOT EXISTS is a no-op there), so these columns are added with
    # ALTER TABLE rather than folded into the 1013 CREATE TABLE.
    manager.register_migration(
        1014,
        "Queue Hold Announcements",
        manager._build_migration_sql("""
        ALTER TABLE call_queues ADD COLUMN announcement_enabled BOOLEAN DEFAULT {BOOLEAN_FALSE};
        ALTER TABLE call_queues ADD COLUMN announcement_interval INTEGER DEFAULT 30;
        ALTER TABLE call_queues ADD COLUMN announcement_text VARCHAR(500);
        ALTER TABLE call_queues ADD COLUMN announcement_file VARCHAR(255);
        ALTER TABLE call_queues ADD COLUMN announcement_position BOOLEAN DEFAULT {BOOLEAN_FALSE};
    """),
    )

    # Migration 1015: Queue Overflow Action
    manager.register_migration(
        1015,
        "Queue Overflow Action",
        manager._build_migration_sql("""
        ALTER TABLE call_queues ADD COLUMN overflow_action VARCHAR(20) DEFAULT 'voicemail';
    """),
    )

    # Migration 1016: Per-queue redial cap
    manager.register_migration(
        1016,
        "Queue Redial Cap",
        manager._build_migration_sql("""
        ALTER TABLE call_queues ADD COLUMN max_redials INTEGER DEFAULT 0;
    """),
    )

    # Migration 1017: Transcript storage
    #
    # Three things at once because they are one concern. call_summaries is not new -- it is
    # written at speech_analytics.py:452 and read at :521, and has never been created by any
    # migration, so every summary write has failed silently on every install since.
    #
    # call_transcripts is deliberately shaped so live transcription can use it unchanged when
    # that lands. A live session has no file on disk and no duration until it ends, so
    # media_path and both duration columns are nullable, source is a plain VARCHAR rather than
    # a constrained enum, and call_id is *not* unique -- one call can hold several transcripts
    # (per leg, or a live pass plus a better post-call re-run). StreamSession.close() already
    # returns a Transcript, so a finished live session stores through exactly this shape.
    manager.register_migration(
        1017,
        "Transcript Storage",
        manager._build_migration_sql("""
        -- One row per transcription run, not per media file: re-transcribing a recording
        -- with a better model appends rather than overwriting what was read before.
        CREATE TABLE IF NOT EXISTS call_transcripts (
            id {SERIAL},
            call_id VARCHAR(100),
            source VARCHAR(20) NOT NULL DEFAULT 'recording',
            media_path VARCHAR(255),
            provider VARCHAR(30),
            model VARCHAR(255),
            language VARCHAR(20),
            transcript_text {TEXT},
            -- JSON, stored as text: _build_table_sql has no JSON placeholder and SQLite has
            -- no JSON column type. Nullable because word/segment timing is opt-in.
            segments {TEXT},
            -- Null when the engine cannot report one. Whisper genuinely cannot, and a 0.0
            -- default would be rendered to a user as "Estimated accuracy 0%".
            confidence FLOAT,
            audio_duration FLOAT,
            processing_duration FLOAT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_transcripts_call ON call_transcripts(call_id);
        CREATE INDEX IF NOT EXISTS idx_transcripts_created ON call_transcripts(created_at);
        CREATE INDEX IF NOT EXISTS idx_transcripts_source ON call_transcripts(source);

        -- Referenced by speech_analytics since it was written; created here for the first time.
        CREATE TABLE IF NOT EXISTS call_summaries (
            id {SERIAL},
            call_id VARCHAR(100) NOT NULL,
            transcript {TEXT},
            summary {TEXT},
            sentiment VARCHAR(20),
            sentiment_score FLOAT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_summaries_call ON call_summaries(call_id);
    """),
    )

    # Migration 1018: Retention policies, legal holds, transcript participants
    #
    # Retention policies existed before this only as a dict on RecordingRetentionManager, so
    # the admin UI's "Add Policy" wrote to memory and lost it on restart -- and, because
    # features.recording_retention was never in config.yml, the manager was disabled and the
    # write failed outright. Policies are operator configuration; they belong in a table.
    #
    # Two periods, not one. Audio is the bytes and the privacy weight; transcripts are ~2% of
    # the size and carry most of the value. Either may be NULL, meaning "inherit the fallback
    # from the retention: config block", so a policy can extend audio without touching text.
    #
    # match_rules is JSON, evaluated in Python rather than SQL: the facts it matches against
    # live in a sidecar file on disk, not in this database, so the join could not happen here
    # anyway. Policy counts are in the tens, so a linear scan per file costs nothing.
    manager.register_migration(
        1018,
        "Retention Policies and Legal Holds",
        manager._build_migration_sql("""
        CREATE TABLE IF NOT EXISTS retention_policies (
            id {SERIAL},
            policy_id VARCHAR(100) NOT NULL UNIQUE,
            name VARCHAR(255) NOT NULL,
            description {TEXT},
            -- NULL means inherit the corresponding retention.* fallback rather than "delete
            -- immediately". A policy that only lengthens audio leaves transcript_days NULL.
            audio_days INTEGER,
            transcript_days INTEGER,
            -- Lowest number wins. The seeded catch-all sits at 1000 so anything added later
            -- outranks it without the operator having to think about ordering.
            priority INTEGER NOT NULL DEFAULT 100,
            -- JSON object of ANDed conditions; '{}' matches every recording. Keys are a fixed
            -- vocabulary (media, extensions, min/max_duration_seconds) -- deliberately closed,
            -- because an open expression language would mean evaluating operator-supplied
            -- strings on a live PBX and could not be rendered as a form.
            match_rules {TEXT} NOT NULL DEFAULT '{}',
            enabled BOOLEAN NOT NULL DEFAULT {BOOLEAN_TRUE},
            -- 'config' rows were seeded from config.yml on a first, empty run; 'api' rows came
            -- from an operator. Seeding happens only when the table is empty, so an operator
            -- editing a seeded row keeps that edit across restarts.
            origin VARCHAR(20) NOT NULL DEFAULT 'api',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_retention_policies_priority
            ON retention_policies(enabled, priority);

        -- A legal hold is not a retention policy and must not be modelled as one: it applies
        -- to one specific call rather than a class, it is placed by a person after an event,
        -- it suspends expiry rather than setting a period, and it has to be releasable. The
        -- tag vocabulary this replaces mapped 'legal' to a fixed 2555 days, which is wrong in
        -- both directions -- a dispute lasting longer still lost the audio, and one settled in
        -- a month held the recording for another seven years with no way to let it go.
        CREATE TABLE IF NOT EXISTS retention_holds (
            id {SERIAL},
            -- session_id, not call_id: a session spans transfers and re-INVITEs, so holding a
            -- conversation holds every leg of it. Recordings are named by session.
            session_id VARCHAR(100) NOT NULL,
            reason {TEXT} NOT NULL,
            placed_by VARCHAR(100) NOT NULL,
            placed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            -- NULL means active. Released rows are kept, never deleted: the audit value of a
            -- hold is the record that it existed and who lifted it.
            released_at TIMESTAMP,
            released_by VARCHAR(100)
        );

        CREATE INDEX IF NOT EXISTS idx_retention_holds_session
            ON retention_holds(session_id, released_at);

        -- Who was on the call, as a JSON array of extension labels. Two consumers: the
        -- transcript sweep, which cannot re-read the sidecar because audio expires first and
        -- the manifest goes with it; and the review surface, where "was I on this call?" must
        -- not mean parsing the segments JSON of every row.
        ALTER TABLE call_transcripts ADD COLUMN IF NOT EXISTS participants {TEXT};

        -- The session this transcript belongs to, so a hold placed on a conversation covers
        -- its text as well as its audio.
        ALTER TABLE call_transcripts ADD COLUMN IF NOT EXISTS session_id VARCHAR(100);

        CREATE INDEX IF NOT EXISTS idx_transcripts_session ON call_transcripts(session_id);
    """),
    )
