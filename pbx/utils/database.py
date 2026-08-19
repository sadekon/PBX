"""
Database backend for PBX features
Provides PostgreSQL storage for VIP callers, CDR, and other data
"""

import contextlib
import json
import traceback
from datetime import UTC, datetime
from typing import Any

from pbx.utils.device_types import detect_device_type
from pbx.utils.logger import get_logger

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor

    POSTGRES_AVAILABLE = True
except ImportError:
    psycopg2 = None
    RealDictCursor = None
    POSTGRES_AVAILABLE = False


class DatabaseBackend:
    """
    Database backend using PostgreSQL
    Provides unified interface for database operations
    """

    def __init__(self, config: Any) -> None:
        """
        Initialize database backend

        Args:
            config: Database configuration (Config object or dict)
        """
        self.logger = get_logger()
        self.config = config
        self.db_type = "postgresql"
        self.connection = None
        self.enabled = False
        self._autocommit = False
        self._was_connected = False

        if not POSTGRES_AVAILABLE:
            self.logger.error(
                "PostgreSQL driver (psycopg2) not installed. "
                "Install with: pip install psycopg2-binary"
            )
            return

        self.logger.info("Database backend: postgresql")

    def connect(self) -> bool:
        """
        Connect to PostgreSQL database

        Returns:
            bool: True if connected successfully
        """
        self.logger.info("Initiating PostgreSQL database connection...")
        try:
            return self._connect_postgresql()
        except Exception as e:
            self.logger.error(f"Database connection error: {e}")
            return False

    def _connect_postgresql(self) -> bool:
        """Connect to PostgreSQL database"""
        if not POSTGRES_AVAILABLE:
            self.logger.error("PostgreSQL driver (psycopg2) not available")
            return False

        host = self.config.get("database.host", "localhost")
        port = self.config.get("database.port", 5432)
        database = self.config.get("database.name", "pbx_system")
        user = self.config.get("database.user", "pbx_user")

        self.logger.info("Connecting to PostgreSQL database...")
        self.logger.info(f"  Host: {host}")
        self.logger.info(f"  Port: {port}")
        self.logger.info(f"  Database: {database}")
        self.logger.info(f"  User: {user}")

        try:
            self.connection = psycopg2.connect(
                host=host,
                port=port,
                database=database,
                user=user,
                password=self.config.get("database.password", ""),
            )
            # Enable autocommit mode to prevent transaction state issues
            # This ensures each query is automatically committed and errors don't
            # leave the connection in a failed transaction state
            self.connection.autocommit = True
            self._autocommit = True
            self.enabled = True
            self._was_connected = True
            self.logger.info("✓ Successfully connected to PostgreSQL database")
            self.logger.info(f"  Connection established: {host}:{port}/{database}")
            return True
        except Exception as e:
            self.logger.error(f"✗ PostgreSQL connection failed: {e}")
            self.logger.warning("Voicemail and other data will be stored ONLY in file system")
            self.logger.warning(
                "To fix: Ensure PostgreSQL is running and accessible, or run 'python scripts/verify_database.py' for diagnostics"
            )
            # Clean up partially-initialized connection
            if self.connection:
                with contextlib.suppress(Exception):
                    self.connection.close()
                self.connection = None
            return False

    def disconnect(self) -> None:
        """Disconnect from database"""
        if self.connection:
            with contextlib.suppress(Exception):
                self.connection.close()
            self.connection = None
            self.enabled = False
            self.logger.info("Database disconnected")

    def _safe_rollback(self) -> None:
        """Safely attempt a rollback, handling dead/closed connections.

        If the rollback fails (e.g. connection dropped), the connection
        is marked as disabled so subsequent operations skip the dead
        connection and the next call to ``connect()`` can re-establish it.
        """
        if not self.connection:
            return
        try:
            self.connection.rollback()
        except Exception:
            self.logger.warning(
                "Database connection lost (rollback failed). Disabling database until reconnection."
            )
            self.connection = None
            self.enabled = False

    def _check_connection(self) -> bool:
        """Verify the database connection is still alive.

        If the connection was previously established but has since been
        lost (e.g. server restart, network timeout), attempts to
        reconnect once.  Does nothing if the database was never
        successfully connected (avoids interfering with intentionally
        disabled setups).

        Returns:
            True if the connection is usable.
        """
        if not self.enabled or not self.connection:
            # Only attempt reconnection if we previously had a working connection
            if self._was_connected:
                self.logger.info("Attempting database reconnection...")
                return self.connect()
            return False

        try:
            cursor = self.connection.cursor()
            cursor.execute("SELECT 1")
            cursor.close()
            return True
        except Exception:
            self.logger.warning("Database connection check failed, attempting reconnection...")
            with contextlib.suppress(Exception):
                self.connection.close()
            self.connection = None
            self.enabled = False
            return self.connect()

    def _execute_with_context(
        self, query: str, context: str = "query", params: tuple | None = None, critical: bool = True
    ) -> bool:
        """
        Execute a query with better error context

        Args:
            query: SQL query
            context: Description of the operation (e.g., "table creation", "index creation")
            params: Query parameters
            critical: If False, log permission errors as warnings instead of errors

        Returns:
            bool: True if successful
        """
        if (not self.enabled or not self.connection) and not self._check_connection():
            return False

        try:
            cursor = self.connection.cursor()
            if params:
                cursor.execute(query, params)
            else:
                cursor.execute(query)
            # Only commit if not in autocommit mode
            if not self._autocommit:
                self.connection.commit()
            cursor.close()
            return True
        except Exception as e:
            error_msg = str(e).lower()
            # Check if this is a permission error on existing objects
            permission_errors = [
                "must be owner",  # PostgreSQL
                "permission denied",  # PostgreSQL
                "access denied",
                "insufficient privileges",
            ]
            already_exists_errors = [
                "already exists",
                "duplicate",
            ]

            if not critical and any(pattern in error_msg for pattern in permission_errors):
                # This is expected when tables/indexes exist but user lacks ownership
                # Log as debug instead of error to avoid alarming users
                self.logger.debug(f"Skipping {context}: {e}")
                self._safe_rollback()
                return True  # Return True since this is not a critical failure
            if any(pattern in error_msg for pattern in already_exists_errors):
                # Object already exists - this is fine
                self.logger.debug(f"{context.capitalize()} already exists: {e}")
                self._safe_rollback()
                return True
            # Check for UNIQUE constraint violations - only suppress for schema operations
            # Data operations (INSERT/UPDATE/DELETE) should fail visibly
            if "unique constraint" in error_msg:
                is_schema_operation = any(
                    keyword in context.lower()
                    for keyword in ["table creation", "index creation", "schema"]
                )
                if is_schema_operation:
                    self.logger.debug(f"UNIQUE constraint already exists: {e}")
                    self._safe_rollback()
                    return True
                # For data operations, treat as a real error - don't suppress
            # This is an actual error - log verbosely
            self.logger.error(f"Error during {context}: {e}")
            self.logger.error(f"  Query: {query}")
            self.logger.error(f"  Parameters: {params}")
            self.logger.error(f"  Database type: {self.db_type}")
            self.logger.error(f"  Traceback: {traceback.format_exc()}")
            self._safe_rollback()
            return False

    def execute(self, query: str, params: tuple | None = None) -> bool:
        """
        Execute a query (INSERT, UPDATE, DELETE)

        Args:
            query: SQL query
            params: Query parameters

        Returns:
            bool: True if successful
        """
        if (not self.enabled or not self.connection) and not self._check_connection():
            return False
        return self._execute_with_context(query, "query execution", params, critical=True)

    def execute_rowcount(self, query: str, params: tuple | None = None) -> int | None:
        """
        Execute a query and return the number of affected rows.

        Args:
            query: SQL query (INSERT, UPDATE, DELETE)
            params: Query parameters

        Returns:
            Number of affected rows, or None on failure.
        """
        if (not self.enabled or not self.connection) and not self._check_connection():
            return None
        try:
            cursor = self.connection.cursor()
            if params:
                cursor.execute(query, params)
            else:
                cursor.execute(query)
            rowcount = cursor.rowcount
            if not self._autocommit:
                self.connection.commit()
            cursor.close()
            return rowcount
        except Exception as e:
            self.logger.error(f"Error during query execution: {e}")
            self._safe_rollback()
            return None

    def execute_script(self, script: str) -> bool:
        """
        Execute a multi-statement SQL script
        Splits statements by semicolon and executes individually

        Args:
            script: SQL script with multiple statements

        Returns:
            bool: True if successful
        """
        if (not self.enabled or not self.connection) and not self._check_connection():
            return False

        try:
            # Split and execute individual statements
            # Remove comments and split by semicolon
            statements = []
            current = []
            for line in script.split("\n"):
                stripped = line.strip()
                # Skip comments
                if stripped.startswith("--") or not stripped:
                    continue
                current.append(line)
                if ";" in line:
                    statements.append("\n".join(current))
                    current = []

            # Execute each statement
            cursor = self.connection.cursor()
            for stmt in statements:
                stripped_stmt = stmt.strip()
                if stripped_stmt:
                    cursor.execute(stripped_stmt)
            cursor.close()
            if not self._autocommit:
                self.connection.commit()

            return True
        except Exception as e:
            self.logger.error(f"Error during script execution: {e}")
            self.logger.error(f"  Script length: {len(script)} characters")
            self.logger.error(f"  Database type: {self.db_type}")
            self.logger.error(f"  Traceback: {traceback.format_exc()}")
            self._safe_rollback()
            return False

    def fetch_one(self, query: str, params: tuple | None = None) -> dict | None:
        """
        Fetch single row

        Args:
            query: SQL query
            params: Query parameters

        Returns:
            dict: Row data or None
        """
        if (not self.enabled or not self.connection) and not self._check_connection():
            return None

        try:
            cursor = self.connection.cursor(cursor_factory=RealDictCursor)

            if params:
                cursor.execute(query, params)
            else:
                cursor.execute(query)

            row = cursor.fetchone()
            cursor.close()

            if row:
                return dict(row)
            return None
        except Exception as e:
            self.logger.error(f"Fetch one error: {e}")
            self.logger.error(f"  Query: {query}")
            self.logger.error(f"  Parameters: {params}")
            self.logger.error(f"  Database type: {self.db_type}")
            self.logger.error(f"  Traceback: {traceback.format_exc()}")
            self._safe_rollback()
            return None

    def fetch_all(self, query: str, params: tuple | None = None) -> list[dict]:
        """
        Fetch all rows

        Args:
            query: SQL query
            params: Query parameters

        Returns:
            list: list of row dictionaries
        """
        if (not self.enabled or not self.connection) and not self._check_connection():
            return []

        try:
            cursor = self.connection.cursor(cursor_factory=RealDictCursor)

            if params:
                cursor.execute(query, params)
            else:
                cursor.execute(query)

            rows = cursor.fetchall()
            cursor.close()

            return [dict(row) for row in rows]
        except Exception as e:
            self.logger.error(f"Fetch all error: {e}")
            self.logger.error(f"  Query: {query}")
            self.logger.error(f"  Parameters: {params}")
            self.logger.error(f"  Database type: {self.db_type}")
            self.logger.error(f"  Traceback: {traceback.format_exc()}")
            self._safe_rollback()
            return []

    def _build_table_sql(self, template: str) -> str:
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
        }

        result = template
        for placeholder, value in replacements.items():
            result = result.replace(placeholder, value)
        return result

    def create_tables(self) -> bool:
        """Create database tables if they don't exist"""
        if not self.enabled:
            return False

        self.logger.info("Creating database tables...")

        # VIP Callers table
        vip_table = self._build_table_sql(
            """
        CREATE TABLE IF NOT EXISTS vip_callers (
            id {SERIAL},
            caller_id VARCHAR(20) UNIQUE NOT NULL,
            name VARCHAR(255),
            priority_level INTEGER DEFAULT 1,
            notes TEXT,
            special_routing VARCHAR(50),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
        )

        # Registered phones table - tracks phones by MAC (if available) or IP address
        registered_phones_table = self._build_table_sql(
            """
        CREATE TABLE IF NOT EXISTS registered_phones (
            id {SERIAL},
            mac_address VARCHAR(20),
            extension VARCHAR(20) NOT NULL,
            user_agent VARCHAR(255),
            ip_address VARCHAR(50) NOT NULL,
            sip_port INTEGER,
            registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP,
            UNIQUE(mac_address, extension),
            UNIQUE(ip_address, extension)
        )
        """
        )

        # Provisioned devices table - stores phone provisioning configuration
        provisioned_devices_table = self._build_table_sql(
            """
        CREATE TABLE IF NOT EXISTS provisioned_devices (
            id {SERIAL},
            mac_address VARCHAR(20) UNIQUE NOT NULL,
            extension_number VARCHAR(20) NOT NULL,
            extension_number_2 VARCHAR(20),
            vendor VARCHAR(50) NOT NULL,
            model VARCHAR(50) NOT NULL,
            device_type VARCHAR(20) DEFAULT 'phone',
            static_ip VARCHAR(50),
            config_url VARCHAR(255),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_provisioned TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
        )

        # Extensions table - stores user extensions/phone numbers
        extensions_table = self._build_table_sql(
            """
        CREATE TABLE IF NOT EXISTS extensions (
            id {SERIAL},
            number VARCHAR(20) UNIQUE NOT NULL,
            name VARCHAR(255) NOT NULL,
            email VARCHAR(255),
            password_hash VARCHAR(255) NOT NULL,
            password_salt VARCHAR(255),
            allow_external BOOLEAN DEFAULT {BOOLEAN_TRUE},
            voicemail_pin_hash VARCHAR(255),
            voicemail_pin_salt VARCHAR(255),
            is_admin BOOLEAN DEFAULT {BOOLEAN_FALSE},
            ad_synced BOOLEAN DEFAULT {BOOLEAN_FALSE},
            ad_username VARCHAR(100),
            password_changed_at TIMESTAMP,
            failed_login_attempts INTEGER DEFAULT 0,
            account_locked_until TIMESTAMP,
            did_number VARCHAR(20) UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
        )

        # Security audit log table
        security_audit_table = self._build_table_sql(
            """
        CREATE TABLE IF NOT EXISTS security_audit (
            id {SERIAL},
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            event_type VARCHAR(50) NOT NULL,
            identifier VARCHAR(100) NOT NULL,
            ip_address VARCHAR(45),
            success BOOLEAN DEFAULT {BOOLEAN_TRUE},
            details TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
        )

        # Emergency contacts table
        emergency_contacts_table = self._build_table_sql(
            """
        CREATE TABLE IF NOT EXISTS emergency_contacts (
            id VARCHAR(100) PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            extension VARCHAR(20),
            phone VARCHAR(50),
            email VARCHAR(255),
            priority INTEGER DEFAULT 1,
            notification_methods TEXT,
            active BOOLEAN DEFAULT {BOOLEAN_TRUE},
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
        )

        # Emergency notifications table
        emergency_notifications_table = self._build_table_sql(
            """
        CREATE TABLE IF NOT EXISTS emergency_notifications (
            id VARCHAR(100) PRIMARY KEY,
            timestamp VARCHAR(50) NOT NULL,
            trigger_type VARCHAR(50) NOT NULL,
            details TEXT,
            contacts_notified TEXT,
            methods_used TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
        )

        # System configuration table - stores key-value config pairs
        system_config_table = self._build_table_sql(
            """
        CREATE TABLE IF NOT EXISTS system_config (
            id {SERIAL},
            config_key VARCHAR(100) UNIQUE NOT NULL,
            config_value TEXT,
            config_type VARCHAR(20) DEFAULT 'string',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_by VARCHAR(20)
        )
        """
        )

        # Execute table creation
        success = True
        for table_sql in [
            vip_table,
            registered_phones_table,
            provisioned_devices_table,
            extensions_table,
            security_audit_table,
            emergency_contacts_table,
            emergency_notifications_table,
            system_config_table,
        ]:
            if not self._execute_with_context(table_sql, "table creation"):
                success = False

        # Create indexes - use permissive execution that handles existing
        # indexes gracefully
        indexes = [
            "CREATE INDEX IF NOT EXISTS idx_vip_caller_id ON vip_callers(caller_id)",
            "CREATE INDEX IF NOT EXISTS idx_vip_priority ON vip_callers(priority_level)",
            "CREATE INDEX IF NOT EXISTS idx_phones_mac ON registered_phones(mac_address)",
            "CREATE INDEX IF NOT EXISTS idx_phones_extension ON registered_phones(extension)",
            "CREATE INDEX IF NOT EXISTS idx_provisioned_mac ON provisioned_devices(mac_address)",
            "CREATE INDEX IF NOT EXISTS idx_provisioned_extension ON provisioned_devices(extension_number)",
            "CREATE INDEX IF NOT EXISTS idx_provisioned_vendor ON provisioned_devices(vendor)",
            "CREATE INDEX IF NOT EXISTS idx_ext_number ON extensions(number)",
            "CREATE INDEX IF NOT EXISTS idx_ext_email ON extensions(email)",
            "CREATE INDEX IF NOT EXISTS idx_ext_ad_synced ON extensions(ad_synced)",
            "CREATE INDEX IF NOT EXISTS idx_security_audit_timestamp ON security_audit(timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_security_audit_identifier ON security_audit(identifier)",
            "CREATE INDEX IF NOT EXISTS idx_security_audit_event_type ON security_audit(event_type)",
            "CREATE INDEX IF NOT EXISTS idx_emergency_contacts_active ON emergency_contacts(active)",
            "CREATE INDEX IF NOT EXISTS idx_emergency_contacts_priority ON emergency_contacts(priority)",
            "CREATE INDEX IF NOT EXISTS idx_emergency_notifications_timestamp ON emergency_notifications(timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_emergency_notifications_trigger_type ON emergency_notifications(trigger_type)",
        ]

        for index_sql in indexes:
            # Index creation failures are non-critical - indexes may already exist
            # or user may lack permissions on pre-existing tables
            self._execute_with_context(index_sql, "index creation", critical=False)

        # Perform schema migrations for existing tables
        self._migrate_schema()

        if success:
            self.logger.info("Database tables created successfully")
        else:
            self.logger.error("Failed to create some database tables")

        return success

    def _add_missing_columns(self, table: str, columns: list[tuple[str, str]]) -> None:
        """
        Add any of `columns` the table does not already have.

        Idempotent, and non-critical by design: failing to add one column is logged and
        skipped rather than aborting startup. Covers tables whose schema predates Alembic,
        and acts as a safety net for columns the query layer names explicitly -- a column
        missing there would fail every lookup against the table.

        Args:
            table: Table name.
            columns: (name, type) pairs, e.g. [("sip_port", "INTEGER")].
        """
        check_query = """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name=%s AND column_name=%s
        """

        for column_name, column_type in columns:
            try:
                cursor = self.connection.cursor()
                cursor.execute(check_query, (table, column_name))
                exists = cursor.fetchone() is not None
                cursor.close()

                if exists:
                    self.logger.debug(f"Column {column_name} already exists in {table}")
                    continue

                self.logger.info(f"Adding column to {table}: {column_name}")
                self._execute_with_context(
                    f"ALTER TABLE {table} ADD COLUMN {column_name} {column_type}",
                    f"add column {column_name} to {table}",
                    critical=False,
                )
            except Exception as e:
                self.logger.debug(f"Column check/add for {column_name} in {table}: {e}")
                self._safe_rollback()

    def _migrate_schema(self) -> None:
        """
        Migrate database schema to add new columns
        Safe migrations that handle existing columns gracefully
        """
        self.logger.info("Checking for schema migrations...")

        # Migration: Add security columns to extensions table
        extensions_columns = [
            ("password_salt", "VARCHAR(255)"),
            ("voicemail_pin_hash", "VARCHAR(255)"),
            ("voicemail_pin_salt", "VARCHAR(255)"),
            ("is_admin", "BOOLEAN DEFAULT FALSE"),
            ("ad_synced", "BOOLEAN DEFAULT FALSE"),
            ("ad_username", "VARCHAR(100)"),
            ("password_changed_at", "TIMESTAMP"),
            ("failed_login_attempts", "INTEGER DEFAULT 0"),
            ("account_locked_until", "TIMESTAMP"),
            ("sip_password", "VARCHAR(255)"),  # SIP authentication password for phone provisioning
            # Per-extension subscription to voicemail notification email. Defaults TRUE so
            # rows predating the column keep the notifications they already received.
            # Also created by alembic revision 004; kept here because the extension SELECTs
            # name this column explicitly, so a database missing it would fail every lookup.
            ("voicemail_email_enabled", "BOOLEAN DEFAULT TRUE"),
        ]

        self._add_missing_columns("extensions", extensions_columns)

        # Migration: Add the registering port to registered_phones. Without it the call
        # router has to assume 5060 when recovering a registration, which silently sends
        # INVITEs to the wrong port for any phone that registers from another port.
        self._add_missing_columns("registered_phones", [("sip_port", "INTEGER")])

        # Migration: Add device_type column to provisioned_devices table
        device_type_column = ("device_type", "VARCHAR(20) DEFAULT 'phone'")

        # Check if column exists
        check_query = """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name='provisioned_devices' AND column_name=%s
        """

        try:
            cursor = self.connection.cursor()
            cursor.execute(check_query, (device_type_column[0],))
            exists = cursor.fetchone() is not None
            cursor.close()

            if not exists:
                # Add column
                alter_query = f"ALTER TABLE provisioned_devices ADD COLUMN {device_type_column[0]} {device_type_column[1]}"
                self.logger.info(f"Adding column to provisioned_devices: {device_type_column[0]}")
                self._execute_with_context(
                    alter_query,
                    f"add column {device_type_column[0]} to provisioned_devices",
                    critical=False,
                )
            else:
                self.logger.debug(
                    f"Column {device_type_column[0]} already exists in provisioned_devices"
                )
        except Exception as e:
            self.logger.debug(
                f"Column check/add for {device_type_column[0]} in provisioned_devices: {e}"
            )
            self._safe_rollback()

        # Add extension_number_2 column for dual-port ATA support (Line 2)
        ext2_column = ("extension_number_2", "VARCHAR(20)")
        try:
            cursor = self.connection.cursor()
            cursor.execute(check_query, (ext2_column[0],))
            exists = cursor.fetchone() is not None
            cursor.close()

            if not exists:
                alter_query = (
                    f"ALTER TABLE provisioned_devices ADD COLUMN {ext2_column[0]} {ext2_column[1]}"
                )
                self.logger.info(f"Adding column to provisioned_devices: {ext2_column[0]}")
                self._execute_with_context(
                    alter_query,
                    f"add column {ext2_column[0]} to provisioned_devices",
                    critical=False,
                )
            else:
                self.logger.debug(f"Column {ext2_column[0]} already exists in provisioned_devices")
        except Exception as e:
            self.logger.debug(f"Column check/add for {ext2_column[0]} in provisioned_devices: {e}")
            self._safe_rollback()

        # Apply framework feature migrations
        self._apply_framework_migrations()

        self.logger.info("Schema migration check complete")

    def _apply_framework_migrations(self) -> None:
        """Apply framework feature migrations"""
        try:
            from pbx.utils.migrations import MigrationManager, register_all_migrations

            self.logger.info("Applying framework feature migrations...")
            migration_manager = MigrationManager(self)
            register_all_migrations(migration_manager)

            if migration_manager.apply_migrations():
                self.logger.info("✓ Framework migrations applied successfully")
            else:
                # Startup deliberately continues -- a PBX that will not answer calls because
                # of a schema problem is worse than one running on a stale schema. But this is
                # an ERROR, not a warning: the failed migration was not recorded, so the
                # process is now running against a schema it believes is current and is not.
                self.logger.error(
                    "✗ A framework migration FAILED and was not recorded. The database schema "
                    "is incomplete; features touching the affected tables will misbehave. "
                    "Fix the cause and restart -- it will be retried."
                )

        except Exception as e:
            self.logger.error(f"Failed to apply framework migrations: {e}")
            # Don't fail startup if migrations fail
            import traceback

            self.logger.debug(traceback.format_exc())


class VIPCallerDB:
    """VIP Caller database operations"""

    def __init__(self, db: DatabaseBackend) -> None:
        """
        Initialize VIP caller database

        Args:
            db: Database backend instance
        """
        self.db = db
        self.logger = get_logger()

    def add_vip(
        self,
        caller_id: str,
        priority_level: int = 1,
        name: str | None = None,
        notes: str | None = None,
    ) -> bool:
        """Add or update VIP caller"""
        query = """
        INSERT INTO vip_callers (caller_id, priority_level, name, notes, updated_at)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (caller_id) DO UPDATE
        SET priority_level = EXCLUDED.priority_level,
            name = EXCLUDED.name,
            notes = EXCLUDED.notes,
            updated_at = EXCLUDED.updated_at
        """

        params = (caller_id, priority_level, name, notes, datetime.now(UTC))
        return self.db.execute(query, params)

    def remove_vip(self, caller_id: str) -> bool:
        """Remove VIP caller"""
        query = "DELETE FROM vip_callers WHERE caller_id = %s"
        return self.db.execute(query, (caller_id,))

    def get_vip(self, caller_id: str) -> dict | None:
        """Get VIP caller information"""
        query = "SELECT id, caller_id, name, priority_level, notes, special_routing, created_at, updated_at FROM vip_callers WHERE caller_id = %s"
        return self.db.fetch_one(query, (caller_id,))

    def list_vips(self, priority_level: int | None = None) -> list[dict]:
        """list all VIP callers"""
        if priority_level is not None:
            query = "SELECT id, caller_id, name, priority_level, notes, special_routing, created_at, updated_at FROM vip_callers WHERE priority_level = %s ORDER BY name"
            return self.db.fetch_all(query, (priority_level,))
        query = "SELECT id, caller_id, name, priority_level, notes, special_routing, created_at, updated_at FROM vip_callers ORDER BY priority_level, name"
        return self.db.fetch_all(query)

    def is_vip(self, caller_id: str) -> bool:
        """Check if caller is VIP"""
        return self.get_vip(caller_id) is not None


class RegisteredPhonesDB:
    """Registered phone (SIP endpoint) database operations"""

    # Columns every phone lookup returns. Declared once so the queries below cannot drift
    # apart -- sip_port in particular has to reach the call router, which needs the port the
    # phone actually registered from, not an assumed 5060.
    _PHONE_COLUMNS = (
        "id, mac_address, extension AS extension_number, user_agent, "
        "ip_address, sip_port, registered_at"
    )

    def __init__(self, db: DatabaseBackend) -> None:
        """
        Initialize registered phones database

        Args:
            db: Database backend instance
        """
        self.db = db
        self.logger = get_logger()

    def register_phone(
        self,
        extension_number: str,
        ip_address: str,
        mac_address: str | None = None,
        user_agent: str | None = None,
        contact_uri: str | None = None,
        sip_port: int | None = None,
        address_is_authoritative: bool = True,
    ) -> tuple[bool, str | None]:
        """
        Register or update a phone registration

        Args:
            extension_number: Extension number
            ip_address: IP address of the phone
            mac_address: MAC address (optional, can be None)
            user_agent: User-Agent header from SIP message
            contact_uri: Contact URI from SIP message
            sip_port: Port the phone registered from. Stored so the call router can reach
                it after a restart; phones do not always use 5060.
            address_is_authoritative: Whether `ip_address` is where this phone can be
                *called*. True for a SIP REGISTER, whose source address is exactly that.
                False for anything that merely observed the device at an address -- a
                provisioning fetch says where it asked for a file, which is a different
                fact, and behind a reverse proxy is not even the device's address. A
                non-authoritative caller can create a row but never overwrites one.

        Returns:
            tuple[bool, str | None]: Success status and the actual MAC address stored (or None)
        """
        # First, check if this MAC or IP is registered to a DIFFERENT extension
        # This handles reprovisioning: when a phone is moved from one extension
        # to another
        old_registrations = []
        if mac_address:
            # Check if this MAC is registered to a different extension
            old_by_mac = self.get_by_mac(mac_address)
            if old_by_mac and old_by_mac["extension_number"] != extension_number:
                old_registrations.append(old_by_mac)
                self.logger.info(
                    f"Phone MAC {mac_address} was registered to extension {old_by_mac['extension_number']}, will update to {extension_number}"
                )

        # Check if this IP is registered to a different extension.
        #
        # Only when the address really identifies the device. A proxied provisioning fetch
        # reports the proxy's address for every phone, so this would read each fetch as the
        # device having moved and delete the previous phone's registration -- phones
        # deleting each other in turn, none of them reachable for long.
        old_by_ip = self.get_by_ip(ip_address) if address_is_authoritative else None
        if (
            old_by_ip
            and old_by_ip["extension_number"] != extension_number
            # Only add if it's not already in the list (avoid duplicates if MAC
            # and IP point to same record)
            and not any(r["id"] == old_by_ip["id"] for r in old_registrations)
        ):
            old_registrations.append(old_by_ip)
            self.logger.info(
                f"Phone IP {ip_address} was registered to extension {old_by_ip['extension_number']}, will update to {extension_number}"
            )

        # Delete old registrations to different extensions
        for old_reg in old_registrations:
            delete_query = """
            DELETE FROM registered_phones WHERE id = %s
            """
            self.db.execute(delete_query, (old_reg["id"],))
            self.logger.info(
                f"Removed old registration: ext={old_reg['extension_number']}, ip={old_reg.get('ip_address')}, mac={old_reg.get('mac_address')}"
            )

        # Now check if this phone is already registered to THIS extension (by
        # MAC or IP)
        existing = None
        if mac_address:
            existing = self.get_by_mac(mac_address, extension_number)
        if not existing:
            existing = self.get_by_ip(ip_address, extension_number)

        if existing:
            # Update existing registration
            # Preserve existing values if new values are None (device didn't send them)
            updated_mac = mac_address if mac_address is not None else existing.get("mac_address")
            # A caller that only observed the device leaves the stored address alone: the
            # row already holds one learned from a REGISTER, which is the only message that
            # says where the phone can be called.
            updated_ip = (
                ip_address
                if ip_address is not None and address_is_authoritative
                else existing.get("ip_address")
            )
            updated_user_agent = (
                user_agent if user_agent is not None else existing.get("user_agent")
            )
            updated_port = sip_port if sip_port is not None else existing.get("sip_port")

            query = """
            UPDATE registered_phones
            SET mac_address = %s, ip_address = %s, user_agent = %s, sip_port = %s
            WHERE id = %s
            """
            params = (
                updated_mac,
                updated_ip,
                updated_user_agent,
                updated_port,
                existing["id"],
            )
            success = self.db.execute(query, params)
            return (success, updated_mac)
        # Insert new registration
        query = """
            INSERT INTO registered_phones
            (mac_address, extension, ip_address, user_agent, sip_port)
            VALUES (%s, %s, %s, %s, %s)
            """
        params = (mac_address, extension_number, ip_address, user_agent, sip_port)
        success = self.db.execute(query, params)
        return (success, mac_address)

    def get_by_mac(self, mac_address: str, extension_number: str | None = None) -> dict | None:
        """
        Get phone registration by MAC address

        Args:
            mac_address: MAC address
            extension_number: Optional extension filter

        Returns:
            dict: Phone registration data or None
        """
        if extension_number:
            query = f"""
            SELECT {self._PHONE_COLUMNS} FROM registered_phones
            WHERE mac_address = %s AND extension = %s
            """
            return self.db.fetch_one(query, (mac_address, extension_number))
        query = f"""
            SELECT {self._PHONE_COLUMNS} FROM registered_phones WHERE mac_address = %s
            """
        return self.db.fetch_one(query, (mac_address,))

    def get_by_ip(self, ip_address: str, extension_number: str | None = None) -> dict | None:
        """
        Get phone registration by IP address

        Args:
            ip_address: IP address
            extension_number: Optional extension filter

        Returns:
            dict: Phone registration data or None
        """
        if extension_number:
            query = f"""
            SELECT {self._PHONE_COLUMNS} FROM registered_phones
            WHERE ip_address = %s AND extension = %s
            """
            return self.db.fetch_one(query, (ip_address, extension_number))
        query = f"""
            SELECT {self._PHONE_COLUMNS} FROM registered_phones WHERE ip_address = %s
            """
        return self.db.fetch_one(query, (ip_address,))

    def get_by_extension(self, extension_number: str) -> list[dict]:
        """
        Get all phone registrations for an extension

        Args:
            extension_number: Extension number

        Returns:
            list: list of phone registration data
        """
        query = f"""
        SELECT {self._PHONE_COLUMNS} FROM registered_phones
        WHERE extension = %s
        ORDER BY registered_at DESC
        """
        return self.db.fetch_all(query, (extension_number,))

    def list_all(self) -> list[dict]:
        """
        list all registered phones

        Returns:
            list: list of all phone registrations
        """
        query = f"""
        SELECT {self._PHONE_COLUMNS} FROM registered_phones
        ORDER BY registered_at DESC
        """
        return self.db.fetch_all(query)

    def remove_phone(self, phone_id: int) -> bool:
        """
        Remove a phone registration

        Args:
            phone_id: Phone registration ID

        Returns:
            bool: True if successful
        """
        query = """
        DELETE FROM registered_phones WHERE id = %s
        """
        return self.db.execute(query, (phone_id,))

    def update_phone_extension(self, mac_address: str, new_extension_number: str) -> bool:
        """
        Update the extension number for a phone identified by MAC address.
        This is useful when reprovisioning a phone to a different extension.

        Note: This will update ALL registrations with the given MAC address,
        effectively moving the phone to the new extension.

        Args:
            mac_address: MAC address of the phone to update
            new_extension_number: New extension number to assign

        Returns:
            bool: True if the SQL execution succeeded, False on error.
            Note: Returns True even if no matching rows were found to update.
        """
        if not mac_address:
            self.logger.error("Cannot update phone extension: MAC address is required")
            return False

        query = """
        UPDATE registered_phones
        SET extension = %s
        WHERE mac_address = %s
        """

        params = (new_extension_number, mac_address)
        success = self.db.execute(query, params)

        if success:
            self.logger.info(f"Updated phone {mac_address} to extension {new_extension_number}")

        return success

    def cleanup_incomplete_registrations(self) -> tuple[bool, int]:
        """
        Remove phone registrations that could never be used to reach a phone.

        Two kinds. A row missing its MAC, IP or extension is incomplete and always was
        dropped here. A row whose address is loopback is worse than incomplete: it looks
        valid, survives restarts, and sends every call to that extension straight back into
        the PBX -- which is how a paging destination came to resolve to the PBX itself.
        Nothing legitimate registers from 127.0.0.1, since a phone that shares a host with
        the PBX is not a phone.

        The "webrtc" marker rows are deliberately spared: that is a session identifier
        rather than an address, and the call router already knows not to dial it.

        Called at startup, so a bad row left by an older build is gone before it can route
        anything. The phone re-registers on its own timer and the row comes back correct.

        Returns:
            tuple[bool, int]: Success status and count of removed registrations
        """
        try:
            delete_query = """
            DELETE FROM registered_phones
            WHERE mac_address IS NULL OR mac_address = ''
               OR ip_address IS NULL OR ip_address = ''
               OR extension IS NULL OR extension = ''
               OR ip_address LIKE '127.%'
               OR ip_address IN ('localhost', '::1')
            """
            count = self.db.execute_rowcount(delete_query)

            if count is None:
                self.logger.error("Failed to cleanup incomplete phone registrations")
                return (False, 0)

            if count == 0:
                self.logger.info("No unusable phone registrations found")
            else:
                self.logger.info(
                    f"Cleaned up {count} unusable phone registration(s) from database "
                    f"(incomplete, or pointing at the PBX itself)"
                )

            return (True, count)
        except (KeyError, TypeError, ValueError) as e:
            self.logger.error(f"Error cleaning up incomplete phone registrations: {e}")
            return (False, 0)

    def clear_all(self) -> bool:
        """
        Clear all phone registrations from the table.
        This is typically called on server boot to remove stale registrations.

        Returns:
            bool: True if successful
        """
        query = "DELETE FROM registered_phones"
        success = self.db.execute(query)
        if success:
            self.logger.info("Cleared all phone registrations from database")
        return success


class ExtensionDB:
    """Extension database operations"""

    def __init__(self, db: DatabaseBackend) -> None:
        """
        Initialize extension database

        Args:
            db: Database backend instance
        """
        self.db = db
        self.logger = get_logger()

    def _hash_voicemail_pin(self, pin: str) -> tuple[str | None, str | None]:
        """
        Hash a voicemail PIN using FIPS-compliant encryption

        Args:
            pin: Voicemail PIN to hash

        Returns:
            tuple: (pin_hash, pin_salt) or (None, None) if hashing fails
        """
        if not pin:
            return None, None

        try:
            from pbx.utils.encryption import get_encryption

            enc = get_encryption()
            pin_hash, pin_salt = enc.hash_password(pin)
            return pin_hash, pin_salt
        except Exception as e:
            self.logger.error(f"Failed to hash voicemail PIN: {e}")
            return None, None

    def add(
        self,
        number: str,
        name: str,
        password_hash: str,
        email: str | None = None,
        allow_external: bool = True,
        voicemail_pin: str | None = None,
        ad_synced: bool = False,
        ad_username: str | None = None,
        is_admin: bool = False,
        sip_password: str | None = None,
        did_number: str | None = None,
        *,
        voicemail_email_enabled: bool = True,
    ) -> bool:
        """
        Add a new extension

        Args:
            number: Extension number
            name: Display name
            password_hash: Hashed password
            email: Email address (optional)
            allow_external: Allow external calls
            voicemail_pin: Voicemail PIN (optional) - will be stored as hash/salt
            ad_synced: Whether synced from Active Directory
            ad_username: Active Directory username (optional)
            is_admin: Whether extension has admin privileges (optional)
            sip_password: SIP authentication password for phone provisioning (optional)
            did_number: Carrier-assigned DID this extension answers directly (optional)
            voicemail_email_enabled: Whether new voicemail is emailed to `email` (optional)

        Returns:
            bool: True if successful
        """
        # Hash the voicemail PIN if provided
        voicemail_pin_hash, voicemail_pin_salt = self._hash_voicemail_pin(voicemail_pin)

        # If PIN was provided but hashing failed, return False
        if voicemail_pin and not voicemail_pin_hash:
            self.logger.error(f"Cannot add extension {number}: voicemail PIN hashing failed")
            return False

        query = """
        INSERT INTO extensions (number, name, email, password_hash, allow_external, voicemail_pin_hash, voicemail_pin_salt, ad_synced, ad_username, is_admin, sip_password, did_number, voicemail_email_enabled)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """

        return self.db.execute(
            query,
            (
                number,
                name,
                email,
                password_hash,
                allow_external,
                voicemail_pin_hash,
                voicemail_pin_salt,
                ad_synced,
                ad_username,
                is_admin,
                sip_password,
                did_number,
                voicemail_email_enabled,
            ),
        )

    def get(self, number: str) -> dict | None:
        """
        Get extension by number

        Args:
            number: Extension number

        Returns:
            dict: Extension data or None
        """
        query = """
        SELECT id, number, name, email, password_hash, password_salt, allow_external, voicemail_pin_hash, voicemail_pin_salt, voicemail_email_enabled, is_admin, ad_synced, ad_username, password_changed_at, failed_login_attempts, account_locked_until, created_at, updated_at, sip_password, did_number FROM extensions WHERE number = %s
        """
        return self.db.fetch_one(query, (number,))

    def get_by_did(self, did_number: str) -> dict | None:
        """
        Get the extension that directly answers a given DID number, if any.

        Args:
            did_number: Carrier-assigned DID number

        Returns:
            dict: Extension data or None
        """
        query = """
        SELECT id, number, name, email, password_hash, password_salt, allow_external, voicemail_pin_hash, voicemail_pin_salt, voicemail_email_enabled, is_admin, ad_synced, ad_username, password_changed_at, failed_login_attempts, account_locked_until, created_at, updated_at, sip_password, did_number FROM extensions WHERE did_number = %s
        """
        return self.db.fetch_one(query, (did_number,))

    def get_all(self) -> list[dict]:
        """
        Get all extensions

        Returns:
            list: list of all extensions
        """
        query = """
        SELECT id, number, name, email, password_hash, password_salt, allow_external, voicemail_pin_hash, voicemail_pin_salt, voicemail_email_enabled, is_admin, ad_synced, ad_username, password_changed_at, failed_login_attempts, account_locked_until, created_at, updated_at, sip_password, did_number FROM extensions ORDER BY number
        """
        return self.db.fetch_all(query)

    def get_ad_synced(self) -> list[dict]:
        """
        Get all AD-synced extensions

        Returns:
            list: list of AD-synced extensions
        """
        query = """
        SELECT id, number, name, email, password_hash, password_salt, allow_external, voicemail_pin_hash, voicemail_pin_salt, voicemail_email_enabled, is_admin, ad_synced, ad_username, password_changed_at, failed_login_attempts, account_locked_until, created_at, updated_at, sip_password, did_number FROM extensions WHERE ad_synced = %s ORDER BY number
        """
        return self.db.fetch_all(query, (True,))

    def update(
        self,
        number: str,
        name: str | None = None,
        email: str | None = None,
        password_hash: str | None = None,
        allow_external: bool | None = None,
        voicemail_pin: str | None = None,
        ad_synced: bool | None = None,
        ad_username: str | None = None,
        is_admin: bool | None = None,
        sip_password: str | None = None,
        did_number: str | None = None,
        *,
        voicemail_email_enabled: bool | None = None,
    ) -> bool:
        """
        Update an extension

        Args:
            number: Extension number
            name: Display name (optional)
            email: Email address (optional)
            password_hash: Hashed password (optional)
            allow_external: Allow external calls (optional)
            voicemail_pin: Voicemail PIN (optional)
            ad_synced: Whether synced from Active Directory (optional)
            ad_username: Active Directory username (optional)
            is_admin: Whether extension has admin privileges (optional)
            sip_password: SIP authentication password for phone provisioning (optional)
            did_number: Carrier-assigned DID this extension answers directly (optional).
                An empty string clears it back to NULL; None leaves it untouched
                (same convention every other field here uses).
            voicemail_email_enabled: Whether new voicemail is emailed to `email` (optional)

        Returns:
            bool: True if successful
        """
        # Build update query dynamically based on provided fields
        updates = []
        params = []

        if name is not None:
            updates.append("name = %s")
            params.append(name)

        if email is not None:
            updates.append("email = %s")
            params.append(email)

        if password_hash is not None:
            updates.append("password_hash = %s")
            params.append(password_hash)

        if allow_external is not None:
            updates.append("allow_external = %s")
            params.append(allow_external)

        if voicemail_pin is not None:
            # Hash the voicemail PIN before storing
            voicemail_pin_hash, voicemail_pin_salt = self._hash_voicemail_pin(voicemail_pin)

            # If PIN was provided but hashing failed, return False
            if voicemail_pin and not voicemail_pin_hash:
                self.logger.error(f"Cannot update extension {number}: voicemail PIN hashing failed")
                return False

            updates.append("voicemail_pin_hash = %s")
            params.append(voicemail_pin_hash)
            updates.append("voicemail_pin_salt = %s")
            params.append(voicemail_pin_salt)

        if ad_synced is not None:
            updates.append("ad_synced = %s")
            params.append(ad_synced)

        if ad_username is not None:
            updates.append("ad_username = %s")
            params.append(ad_username)

        if is_admin is not None:
            updates.append("is_admin = %s")
            params.append(is_admin)

        if sip_password is not None:
            updates.append("sip_password = %s")
            params.append(sip_password)

        if did_number is not None:
            updates.append("did_number = %s")
            params.append(did_number if did_number != "" else None)

        if voicemail_email_enabled is not None:
            updates.append("voicemail_email_enabled = %s")
            params.append(voicemail_email_enabled)

        if not updates:
            return True  # Nothing to update

        # Add updated_at timestamp
        updates.append("updated_at = CURRENT_TIMESTAMP")

        # Add number to params for WHERE clause
        params.append(number)

        query = f"""
        UPDATE extensions
        SET {", ".join(updates)}
        WHERE number = %s
        """  # nosec B608 - updates are validated field names, placeholder is safe

        return self.db.execute(query, tuple(params))

    def delete(self, number: str) -> bool:
        """
        Delete an extension

        Args:
            number: Extension number

        Returns:
            bool: True if successful
        """
        query = """
        DELETE FROM extensions WHERE number = %s
        """
        return self.db.execute(query, (number,))

    def search(self, query_str: str) -> list[dict]:
        """
        Search extensions by number, name, or email

        Args:
            query_str: Search query

        Returns:
            list: list of matching extensions
        """
        search_pattern = f"%{query_str}%"
        query = """
        SELECT id, number, name, email, password_hash, password_salt, allow_external, voicemail_pin_hash, voicemail_pin_salt, voicemail_email_enabled, is_admin, ad_synced, ad_username, password_changed_at, failed_login_attempts, account_locked_until, created_at, updated_at, did_number FROM extensions
        WHERE number LIKE %s OR name LIKE %s OR email LIKE %s
        ORDER BY number
        """
        return self.db.fetch_all(query, (search_pattern, search_pattern, search_pattern))

    def get_config(self, key: str, default: object = None) -> object:
        """
        Get a configuration value by key

        Args:
            key: Configuration key
            default: Default value if not found

        Returns:
            Configuration value or default
        """
        query = "SELECT config_value, config_type FROM system_config WHERE config_key = %s"

        result = self.db.fetch_one(query, (key,))
        if result:
            value = result.get("config_value")
            config_type = result.get("config_type")
            # Convert value based on type with error handling
            try:
                if config_type == "int":
                    return int(value) if value else default
                if config_type == "bool":
                    if value and isinstance(value, str):
                        return value.lower() in ("true", "1", "yes")
                    return default
                if config_type == "json":
                    return json.loads(value) if value else default
                return value or default
            except (ValueError, json.JSONDecodeError, AttributeError) as e:
                self.logger.warning(
                    f"Error parsing config value for key '{key}': {e}. Returning default."
                )
                return default
        return default

    def set_config(
        self, key: str, value: object, config_type: str = "string", updated_by: str | None = None
    ) -> bool:
        """
        set a configuration value

        Args:
            key: Configuration key
            value: Configuration value
            config_type: type of value (string, int, bool, json)
            updated_by: User who updated the config

        Returns:
            bool: True if successful
        """
        # Convert value to string for storage with error handling
        try:
            if config_type == "json":
                str_value = json.dumps(value)
            elif config_type == "bool":
                str_value = "true" if value else "false"
            else:
                str_value = str(value)
        except (TypeError, ValueError) as e:
            self.logger.error(f"Error serializing config value for key '{key}': {e}")
            return False

        # Check if key exists
        check_query = "SELECT config_key FROM system_config WHERE config_key = %s"

        exists = self.db.fetch_one(check_query, (key,))

        if exists:
            # Update existing
            query = """
            UPDATE system_config
            SET config_value = %s, config_type = %s, updated_at = %s, updated_by = %s
            WHERE config_key = %s
            """
            return self.db.execute(
                query, (str_value, config_type, datetime.now(UTC), updated_by, key)
            )
        # Insert new
        query = """
            INSERT INTO system_config (config_key, config_value, config_type, updated_at, updated_by)
            VALUES (%s, %s, %s, %s, %s)
            """
        return self.db.execute(query, (key, str_value, config_type, datetime.now(UTC), updated_by))


class TrunkDB:
    """SIP trunk database operations"""

    def __init__(self, db: DatabaseBackend) -> None:
        """
        Initialize trunk database

        Args:
            db: Database backend instance
        """
        self.db = db
        self.logger = get_logger()

    def add(
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
        health_check_interval: int = 60,
        enabled: bool = True,
    ) -> bool:
        """
        Add a new SIP trunk

        Args:
            trunk_id: Trunk identifier
            name: Trunk name
            host: SIP provider host
            username: SIP username
            password: SIP password
            port: SIP port
            codec_preferences: list of preferred codecs (stored as JSON)
            priority: Trunk priority (lower is better, for failover)
            max_channels: Maximum concurrent channels
            health_check_interval: Seconds between health checks
            enabled: Whether the trunk should be loaded/active

        Returns:
            bool: True if successful
        """
        query = """
        INSERT INTO sip_trunks (trunk_id, name, host, port, username, password, codec_preferences, priority, max_channels, health_check_interval, enabled)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        return self.db.execute(
            query,
            (
                trunk_id,
                name,
                host,
                port,
                username,
                password,
                json.dumps(codec_preferences) if codec_preferences is not None else None,
                priority,
                max_channels,
                health_check_interval,
                enabled,
            ),
        )

    def get(self, trunk_id: str) -> dict | None:
        """
        Get trunk by trunk_id

        Args:
            trunk_id: Trunk identifier

        Returns:
            dict: Trunk data or None
        """
        query = """
        SELECT id, trunk_id, name, host, port, username, password, codec_preferences, priority, max_channels, health_check_interval, enabled, created_at, updated_at FROM sip_trunks WHERE trunk_id = %s
        """
        return self._deserialize_row(self.db.fetch_one(query, (trunk_id,)))

    def get_all(self) -> list[dict]:
        """
        Get all trunks

        Returns:
            list: list of all trunks
        """
        query = """
        SELECT id, trunk_id, name, host, port, username, password, codec_preferences, priority, max_channels, health_check_interval, enabled, created_at, updated_at FROM sip_trunks ORDER BY trunk_id
        """
        return [row for row in (self._deserialize_row(r) for r in self.db.fetch_all(query)) if row]

    def update(
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
        health_check_interval: int | None = None,
        enabled: bool | None = None,
    ) -> bool:
        """
        Update a SIP trunk

        Args:
            trunk_id: Trunk identifier
            name: Trunk name (optional)
            host: SIP provider host (optional)
            username: SIP username (optional)
            password: SIP password (optional)
            port: SIP port (optional)
            codec_preferences: list of preferred codecs (optional)
            priority: Trunk priority (optional)
            max_channels: Maximum concurrent channels (optional)
            health_check_interval: Seconds between health checks (optional)
            enabled: Whether the trunk should be loaded/active (optional)

        Returns:
            bool: True if successful
        """
        updates = []
        params: list[object] = []

        if name is not None:
            updates.append("name = %s")
            params.append(name)

        if host is not None:
            updates.append("host = %s")
            params.append(host)

        if username is not None:
            updates.append("username = %s")
            params.append(username)

        if password is not None:
            updates.append("password = %s")
            params.append(password)

        if port is not None:
            updates.append("port = %s")
            params.append(port)

        if codec_preferences is not None:
            updates.append("codec_preferences = %s")
            params.append(json.dumps(codec_preferences))

        if priority is not None:
            updates.append("priority = %s")
            params.append(priority)

        if max_channels is not None:
            updates.append("max_channels = %s")
            params.append(max_channels)

        if health_check_interval is not None:
            updates.append("health_check_interval = %s")
            params.append(health_check_interval)

        if enabled is not None:
            updates.append("enabled = %s")
            params.append(enabled)

        if not updates:
            return True  # Nothing to update

        updates.append("updated_at = CURRENT_TIMESTAMP")
        params.append(trunk_id)

        query = f"""
        UPDATE sip_trunks
        SET {", ".join(updates)}
        WHERE trunk_id = %s
        """  # nosec B608 - updates are validated field names, placeholder is safe

        return self.db.execute(query, tuple(params))

    def delete(self, trunk_id: str) -> bool:
        """
        Delete a SIP trunk

        Args:
            trunk_id: Trunk identifier

        Returns:
            bool: True if successful
        """
        query = """
        DELETE FROM sip_trunks WHERE trunk_id = %s
        """
        return self.db.execute(query, (trunk_id,))

    def _deserialize_row(self, row: dict | None) -> dict | None:
        """Decode the JSON-encoded ``codec_preferences`` column on a fetched row."""
        if not row:
            return row
        codec_value = row.get("codec_preferences")
        if codec_value:
            try:
                row["codec_preferences"] = json.loads(codec_value)
            except (TypeError, ValueError):
                self.logger.warning(
                    f"Could not decode codec_preferences for trunk {row.get('trunk_id')}"
                )
                row["codec_preferences"] = None
        return row


class InboundRouteDB:
    """Inbound DID routing database operations"""

    def __init__(self, db: DatabaseBackend) -> None:
        """
        Initialize inbound route database

        Args:
            db: Database backend instance
        """
        self.db = db
        self.logger = get_logger()

    def add(
        self,
        did_number: str,
        destination_type: str,
        destination_value: str,
        trunk_id: str | None = None,
        enabled: bool = True,
        priority: int = 100,
    ) -> bool:
        """
        Add a new inbound route

        Args:
            did_number: Carrier-assigned DID number
            destination_type: One of "extension", "auto_attendant", "voicemail"
            destination_value: Extension number, AA id, or mailbox, per destination_type
            trunk_id: Scope this route to one trunk; None matches any trunk
            enabled: Whether the route is active
            priority: Lower priority value wins when multiple rows could match

        Returns:
            bool: True if successful
        """
        query = """
        INSERT INTO inbound_routes (did_number, trunk_id, destination_type, destination_value, enabled, priority)
        VALUES (%s, %s, %s, %s, %s, %s)
        """
        return self.db.execute(
            query,
            (did_number, trunk_id, destination_type, destination_value, enabled, priority),
        )

    def get(self, route_id: int) -> dict | None:
        """
        Get an inbound route by id

        Args:
            route_id: Inbound route id

        Returns:
            dict: Route data or None
        """
        query = """
        SELECT id, did_number, trunk_id, destination_type, destination_value, enabled, priority, created_at, updated_at FROM inbound_routes WHERE id = %s
        """
        return self.db.fetch_one(query, (route_id,))

    def get_all(self) -> list[dict]:
        """
        Get all inbound routes

        Returns:
            list: list of all routes
        """
        query = """
        SELECT id, did_number, trunk_id, destination_type, destination_value, enabled, priority, created_at, updated_at FROM inbound_routes ORDER BY did_number, priority
        """
        return self.db.fetch_all(query)

    def get_by_did(self, did_number: str, trunk_id: str | None = None) -> dict | None:
        """
        Look up the effective enabled route for a DID, preferring a
        trunk-specific match over a "matches any trunk" (NULL trunk_id) row.

        Args:
            did_number: Carrier-assigned DID number
            trunk_id: Trunk the call arrived on, if known

        Returns:
            dict: The best-matching route, or None
        """
        if trunk_id is not None:
            query = """
            SELECT id, did_number, trunk_id, destination_type, destination_value, enabled, priority, created_at, updated_at
            FROM inbound_routes
            WHERE did_number = %s AND enabled = %s AND (trunk_id = %s OR trunk_id IS NULL)
            ORDER BY trunk_id IS NULL, priority
            LIMIT 1
            """
            return self.db.fetch_one(query, (did_number, True, trunk_id))

        query = """
        SELECT id, did_number, trunk_id, destination_type, destination_value, enabled, priority, created_at, updated_at
        FROM inbound_routes
        WHERE did_number = %s AND enabled = %s AND trunk_id IS NULL
        ORDER BY priority
        LIMIT 1
        """
        return self.db.fetch_one(query, (did_number, True))

    def update(
        self,
        route_id: int,
        did_number: str | None = None,
        trunk_id: str | None = None,
        destination_type: str | None = None,
        destination_value: str | None = None,
        enabled: bool | None = None,
        priority: int | None = None,
    ) -> bool:
        """
        Update an inbound route

        Args:
            route_id: Inbound route id
            did_number: Carrier-assigned DID number (optional)
            trunk_id: Trunk to scope this route to (optional). An empty string
                clears it back to NULL (any trunk); None leaves it untouched.
            destination_type: One of "extension", "auto_attendant", "voicemail" (optional)
            destination_value: Extension number, AA id, or mailbox (optional)
            enabled: Whether the route is active (optional)
            priority: Lower priority value wins when multiple rows could match (optional)

        Returns:
            bool: True if successful
        """
        updates = []
        params: list[object] = []

        if did_number is not None:
            updates.append("did_number = %s")
            params.append(did_number)

        if trunk_id is not None:
            updates.append("trunk_id = %s")
            params.append(trunk_id if trunk_id != "" else None)

        if destination_type is not None:
            updates.append("destination_type = %s")
            params.append(destination_type)

        if destination_value is not None:
            updates.append("destination_value = %s")
            params.append(destination_value)

        if enabled is not None:
            updates.append("enabled = %s")
            params.append(enabled)

        if priority is not None:
            updates.append("priority = %s")
            params.append(priority)

        if not updates:
            return True  # Nothing to update

        updates.append("updated_at = CURRENT_TIMESTAMP")
        params.append(route_id)

        query = f"""
        UPDATE inbound_routes
        SET {", ".join(updates)}
        WHERE id = %s
        """  # nosec B608 - updates are validated field names, placeholder is safe

        return self.db.execute(query, tuple(params))

    def delete(self, route_id: int) -> bool:
        """
        Delete an inbound route

        Args:
            route_id: Inbound route id

        Returns:
            bool: True if successful
        """
        query = """
        DELETE FROM inbound_routes WHERE id = %s
        """
        return self.db.execute(query, (route_id,))


class ProvisionedDevicesDB:
    """Provisioned devices database operations"""

    def __init__(self, db: DatabaseBackend) -> None:
        """
        Initialize provisioned devices database

        Args:
            db: Database backend instance
        """
        self.db = db
        self.logger = get_logger()

    def add_device(
        self,
        mac_address: str,
        extension_number: str,
        vendor: str,
        model: str,
        device_type: str | None = None,
        static_ip: str | None = None,
        config_url: str | None = None,
        extension_number_2: str | None = None,
    ) -> bool:
        """
        Add or update a provisioned device

        Args:
            mac_address: MAC address (normalized format)
            extension_number: Extension number (Line 1)
            vendor: Phone vendor
            model: Phone model
            device_type: Device type ('phone' or 'ata', auto-detected if None)
            static_ip: Static IP address (optional)
            config_url: Configuration URL (optional)
            extension_number_2: Optional second extension (Line 2, for dual-port ATAs)

        Returns:
            bool: True if successful
        """
        # Auto-detect device type if not provided
        if device_type is None:
            device_type = self._detect_device_type(vendor, model)

        # Check if device already exists
        existing = self.get_device(mac_address)

        if existing:
            # Update existing device
            query = """
            UPDATE provisioned_devices
            SET extension_number = %s, extension_number_2 = %s,
                vendor = %s, model = %s, device_type = %s,
                static_ip = %s, config_url = %s, updated_at = %s
            WHERE mac_address = %s
            """
            params = (
                extension_number,
                extension_number_2,
                vendor,
                model,
                device_type,
                static_ip,
                config_url,
                datetime.now(UTC),
                mac_address,
            )
        else:
            # Insert new device
            query = """
            INSERT INTO provisioned_devices
            (mac_address, extension_number, extension_number_2, vendor, model,
             device_type, static_ip, config_url, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            now = datetime.now(UTC)
            params = (
                mac_address,
                extension_number,
                extension_number_2,
                vendor,
                model,
                device_type,
                static_ip,
                config_url,
                now,
                now,
            )

        return self.db.execute(query, params)

    def get_device(self, mac_address: str) -> dict | None:
        """
        Get provisioned device by MAC address

        Args:
            mac_address: MAC address (normalized format)

        Returns:
            dict: Device data or None
        """
        query = """
        SELECT id, mac_address, extension_number, extension_number_2, vendor, model, device_type, static_ip, config_url, created_at, last_provisioned, updated_at FROM provisioned_devices WHERE mac_address = %s
        """
        return self.db.fetch_one(query, (mac_address,))

    def get_device_by_extension(self, extension_number: str) -> dict | None:
        """
        Get provisioned device by extension number

        Args:
            extension_number: Extension number

        Returns:
            dict: Device data or None
        """
        query = """
        SELECT id, mac_address, extension_number, extension_number_2, vendor, model, device_type, static_ip, config_url, created_at, last_provisioned, updated_at FROM provisioned_devices WHERE extension_number = %s
        """
        return self.db.fetch_one(query, (extension_number,))

    def get_device_by_ip(self, static_ip: str) -> dict | None:
        """
        Get provisioned device by static IP address

        Args:
            static_ip: Static IP address

        Returns:
            dict: Device data or None
        """
        query = """
        SELECT id, mac_address, extension_number, extension_number_2, vendor, model, device_type, static_ip, config_url, created_at, last_provisioned, updated_at FROM provisioned_devices WHERE static_ip = %s
        """
        return self.db.fetch_one(query, (static_ip,))

    def list_all(self) -> list[dict]:
        """
        list all provisioned devices

        Returns:
            list: list of all provisioned devices
        """
        query = """
        SELECT id, mac_address, extension_number, extension_number_2, vendor, model, device_type, static_ip, config_url, created_at, last_provisioned, updated_at FROM provisioned_devices
        ORDER BY extension_number
        """
        return self.db.fetch_all(query)

    def list_by_type(self, device_type: str) -> list[dict]:
        """
        list provisioned devices by type

        Args:
            device_type: Device type ('phone' or 'ata')

        Returns:
            list: list of provisioned devices of specified type
        """
        query = """
        SELECT id, mac_address, extension_number, extension_number_2, vendor, model, device_type, static_ip, config_url, created_at, last_provisioned, updated_at FROM provisioned_devices
        WHERE device_type = %s
        ORDER BY extension_number
        """
        return self.db.fetch_all(query, (device_type,))

    def list_atas(self) -> list[dict]:
        """
        list all provisioned ATAs

        Returns:
            list: list of all provisioned ATA devices
        """
        return self.list_by_type("ata")

    def list_phones(self) -> list[dict]:
        """
        list all provisioned phones (excluding ATAs)

        Returns:
            list: list of all provisioned phone devices
        """
        return self.list_by_type("phone")

    def _detect_device_type(self, vendor: str, model: str) -> str:
        """
        Detect device type based on vendor and model

        Args:
            vendor: Device vendor
            model: Device model

        Returns:
            str: 'ata' or 'phone'
        """
        return detect_device_type(vendor, model)

    def remove_device(self, mac_address: str) -> bool:
        """
        Remove a provisioned device

        Args:
            mac_address: MAC address

        Returns:
            bool: True if successful
        """
        query = """
        DELETE FROM provisioned_devices WHERE mac_address = %s
        """
        return self.db.execute(query, (mac_address,))

    def mark_provisioned(self, mac_address: str) -> bool:
        """
        Mark device as provisioned (update last_provisioned timestamp)

        Args:
            mac_address: MAC address

        Returns:
            bool: True if successful
        """
        query = """
        UPDATE provisioned_devices
        SET last_provisioned = %s
        WHERE mac_address = %s
        """
        return self.db.execute(query, (datetime.now(UTC), mac_address))

    def set_static_ip(self, mac_address: str, static_ip: str) -> bool:
        """
        set or update static IP for a device

        Args:
            mac_address: MAC address
            static_ip: Static IP address

        Returns:
            bool: True if successful
        """
        query = """
        UPDATE provisioned_devices
        SET static_ip = %s, updated_at = %s
        WHERE mac_address = %s
        """
        return self.db.execute(query, (static_ip, datetime.now(UTC), mac_address))


class PagingZonesDB:
    """
    Paging zone persistence.

    A zone is a place you can address by dialling a number -- "Warehouse" = 701. It owns no
    hardware; the mechanisms that put audio into it are rows in paging_destinations.
    """

    #: Columns update() will write. Anything else is ignored rather than interpolated, so a
    #: caller cannot reach the SQL through a field name.
    _UPDATABLE = ("name", "description", "enabled", "max_duration_seconds")

    def __init__(self, db: DatabaseBackend) -> None:
        """
        Initialize paging zones database.

        Args:
            db: DatabaseBackend instance
        """
        self.db = db
        self.logger = get_logger()

    def extension_conflict(self, extension: str, exclude_zone_id: int | None = None) -> str | None:
        """
        Report what already answers to `extension`, if anything.

        Checked against both tables on purpose. A zone number and a user extension are drawn
        from the same dial plan, so a zone at 701 colliding with a real user at 701 makes one
        of them unreachable -- which is the failure the old `startswith(prefix)` matcher
        caused, and re-entering it through zone creation would be no better.

        Args:
            extension: The number to test
            exclude_zone_id: Zone to ignore, when re-checking during an update

        Returns:
            "zone", "extension", or None if the number is free
        """
        if exclude_zone_id is None:
            row = self.db.fetch_one(
                "SELECT id FROM paging_zones WHERE extension = %s", (extension,)
            )
        else:
            row = self.db.fetch_one(
                "SELECT id FROM paging_zones WHERE extension = %s AND id <> %s",
                (extension, exclude_zone_id),
            )
        if row:
            return "zone"

        if self.db.fetch_one("SELECT id FROM extensions WHERE number = %s", (extension,)):
            return "extension"

        return None

    def create(
        self,
        extension: str,
        name: str,
        description: str | None = None,
        enabled: bool = True,
        max_duration_seconds: int = 120,
    ) -> int | None:
        """
        Create a paging zone.

        Args:
            extension: Number dialled to reach this zone
            name: Human name, e.g. "Warehouse"
            description: Optional longer note
            enabled: Whether the zone answers
            max_duration_seconds: Page cutoff; 0 for unlimited

        Returns:
            int: New zone id, or None if the write failed
        """
        now = datetime.now(UTC)
        row = self.db.fetch_one(
            """
            INSERT INTO paging_zones
                (extension, name, description, enabled, max_duration_seconds,
                 created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (extension, name, description, enabled, max_duration_seconds, now, now),
        )
        try:
            return int(row["id"]) if row else None
        except (KeyError, TypeError, ValueError):
            self.logger.error(f"Paging zone {extension} created but its id could not be read")
            return None

    def get(self, zone_id: int) -> dict | None:
        """
        Get a zone by id.

        Args:
            zone_id: Zone id

        Returns:
            dict: Zone row or None
        """
        return self.db.fetch_one("SELECT * FROM paging_zones WHERE id = %s", (zone_id,))

    def get_by_extension(self, extension: str) -> dict | None:
        """
        Get a zone by the number dialled to reach it.

        Args:
            extension: Zone extension

        Returns:
            dict: Zone row or None
        """
        return self.db.fetch_one("SELECT * FROM paging_zones WHERE extension = %s", (extension,))

    def list_all(self) -> list[dict]:
        """
        list every zone, dial order.

        Returns:
            list: Zone rows
        """
        return self.db.fetch_all("SELECT * FROM paging_zones ORDER BY extension")

    def list_enabled_extensions(self) -> list[str]:
        """
        list the numbers that currently route to paging.

        This is what replaces the prefix match: an extension is a paging extension when a
        zone claims it, not when it happens to start with a 7.

        Returns:
            list: Zone extensions that are enabled
        """
        rows = self.db.fetch_all(
            "SELECT extension FROM paging_zones WHERE enabled = TRUE ORDER BY extension"
        )
        return [row["extension"] for row in (rows or [])]

    def update(self, zone_id: int, **fields: object) -> bool:
        """
        Update a zone's mutable fields.

        `extension` is not updatable: it is the dialled identity, and changing it in place
        would silently move a zone out from under anyone who had memorised the number.
        Delete and recreate instead.

        Args:
            zone_id: Zone id
            **fields: Any of name, description, enabled, max_duration_seconds

        Returns:
            bool: True if a write was attempted and succeeded
        """
        sets = [(k, v) for k, v in fields.items() if k in self._UPDATABLE]
        if not sets:
            return False

        assignments = ", ".join(f"{k} = %s" for k, _ in sets)
        params = (*(v for _, v in sets), datetime.now(UTC), zone_id)
        return self.db.execute(
            f"UPDATE paging_zones SET {assignments}, updated_at = %s WHERE id = %s",
            params,
        )

    def delete(self, zone_id: int) -> bool:
        """
        Delete a zone. Its destinations cascade.

        Args:
            zone_id: Zone id

        Returns:
            bool: True if successful
        """
        return self.db.execute("DELETE FROM paging_zones WHERE id = %s", (zone_id,))


class PagingDestinationsDB:
    """
    Paging destination persistence.

    A destination is one mechanism that puts audio into a zone. Today that means a SIP
    endpoint -- an ATA's FXS port, named by the extension it answers on. It deliberately
    stores no hardware detail: vendor, model, MAC and IP all live in provisioned_devices,
    and are joined in by :meth:`list_resolved_for_zone` when a page needs them.
    """

    _UPDATABLE = (
        "label",
        "enabled",
        "sort_order",
        "auto_answer_override",
        "dtmf_sequence",
        "multicast_address",
        "multicast_port",
    )

    def __init__(self, db: DatabaseBackend) -> None:
        """
        Initialize paging destinations database.

        Args:
            db: DatabaseBackend instance
        """
        self.db = db
        self.logger = get_logger()

    def add_sip_endpoint(
        self,
        zone_id: int,
        endpoint_extension: str,
        label: str | None = None,
        auto_answer_override: str | None = None,
        dtmf_sequence: str | None = None,
        sort_order: int = 0,
    ) -> int | None:
        """
        Add an ATA (or other SIP endpoint) to a zone.

        Args:
            zone_id: Owning zone
            endpoint_extension: The extension the FXS port answers on. For a dual-port ATA
                this is either of the device's two extensions -- each port is a separate
                amplifier circuit and so a separate destination.
            label: Optional name for this circuit, e.g. "Ceiling horns"
            auto_answer_override: Force a header form, or "none" to send none. Leave NULL to
                derive it from the device's vendor.
            dtmf_sequence: Digits the PBX plays to pick a circuit on this amplifier once it
                answers. NULL leaves the choice to whoever is paging, who dials it on their
                own keypad -- which is how the analogue system worked and remains the
                default.
            sort_order: Display order within the zone

        Returns:
            int: New destination id, or None if the write failed
        """
        row = self.db.fetch_one(
            """
            INSERT INTO paging_destinations
                (zone_id, kind, label, enabled, sort_order,
                 endpoint_extension, auto_answer_override, dtmf_sequence, created_at)
            VALUES (%s, 'sip_endpoint', %s, TRUE, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                zone_id,
                label,
                sort_order,
                endpoint_extension,
                auto_answer_override,
                dtmf_sequence,
                datetime.now(UTC),
            ),
        )
        try:
            return int(row["id"]) if row else None
        except (KeyError, TypeError, ValueError):
            self.logger.error(
                f"Paging destination {endpoint_extension} created but its id could not be read"
            )
            return None

    def add_multicast(
        self,
        zone_id: int,
        multicast_address: str,
        multicast_port: int,
        label: str | None = None,
        sort_order: int = 0,
    ) -> int | None:
        """
        Add a multicast group to a zone.

        Accepted by the schema so desk-phone paging needs no migration later. Nothing sends
        to one yet.

        Args:
            zone_id: Owning zone
            multicast_address: Group address, conventionally in 239.0.0.0/8
            multicast_port: Group port
            label: Optional name
            sort_order: Display order within the zone

        Returns:
            int: New destination id, or None if the write failed
        """
        row = self.db.fetch_one(
            """
            INSERT INTO paging_destinations
                (zone_id, kind, label, enabled, sort_order,
                 multicast_address, multicast_port, created_at)
            VALUES (%s, 'multicast', %s, TRUE, %s, %s, %s, %s)
            RETURNING id
            """,
            (zone_id, label, sort_order, multicast_address, multicast_port, datetime.now(UTC)),
        )
        try:
            return int(row["id"]) if row else None
        except (KeyError, TypeError, ValueError):
            self.logger.error("Multicast destination created but its id could not be read")
            return None

    def get(self, destination_id: int) -> dict | None:
        """
        Get a destination by id.

        Args:
            destination_id: Destination id

        Returns:
            dict: Destination row or None
        """
        return self.db.fetch_one(
            "SELECT * FROM paging_destinations WHERE id = %s", (destination_id,)
        )

    def list_for_zone(self, zone_id: int) -> list[dict]:
        """
        list a zone's destinations, unresolved.

        Args:
            zone_id: Zone id

        Returns:
            list: Destination rows in display order
        """
        return self.db.fetch_all(
            "SELECT * FROM paging_destinations WHERE zone_id = %s ORDER BY sort_order, id",
            (zone_id,),
        )

    def list_resolved_for_zone(self, zone_id: int, enabled_only: bool = True) -> list[dict]:
        """
        list a zone's destinations with their hardware joined in.

        This is what a page runs on, so it is one query: the handler must not go back to the
        database once audio is flowing.

        The device join matches `extension_number_2` as well as `extension_number`, because a
        dual-port ATA is one provisioned_devices row covering two extensions, and the second
        FXS port is every bit as real a destination as the first. Matching only the first
        column would leave port 2 with no vendor and silently fall back to the default
        auto-answer header.

        LEFT JOIN throughout: a destination whose extension has no provisioned device is
        still a valid destination -- it just has no vendor to derive a header from.

        Args:
            zone_id: Zone id
            enabled_only: Skip destinations that are switched off

        Returns:
            list: Destination rows widened with mac_address, vendor, model, device_type,
                static_ip and the extension's display name
        """
        query = """
        SELECT d.id, d.zone_id, d.kind, d.label, d.enabled, d.sort_order,
               d.endpoint_extension, d.auto_answer_override, d.dtmf_sequence,
               d.multicast_address, d.multicast_port,
               pd.mac_address, pd.vendor, pd.model, pd.device_type, pd.static_ip,
               e.name AS endpoint_name
        FROM paging_destinations d
        LEFT JOIN provisioned_devices pd
               ON pd.extension_number = d.endpoint_extension
               OR pd.extension_number_2 = d.endpoint_extension
        LEFT JOIN extensions e
               ON e.number = d.endpoint_extension
        WHERE d.zone_id = %s
        """
        if enabled_only:
            query += " AND d.enabled = TRUE"
        query += " ORDER BY d.sort_order, d.id"

        return self.db.fetch_all(query, (zone_id,))

    def zones_using_extension(self, endpoint_extension: str) -> list[dict]:
        """
        Find every zone that pages through a given endpoint.

        Contention is per destination, not per zone: all-call and a specific zone overlap by
        definition, so paging the warehouse while all-call is live has to be refused even
        though the warehouse zone itself is idle.

        Args:
            endpoint_extension: The endpoint's extension

        Returns:
            list: Rows of zone id, extension and name
        """
        return self.db.fetch_all(
            """
            SELECT z.id, z.extension, z.name
            FROM paging_destinations d
            JOIN paging_zones z ON z.id = d.zone_id
            WHERE d.endpoint_extension = %s AND d.kind = 'sip_endpoint'
            ORDER BY z.extension
            """,
            (endpoint_extension,),
        )

    def update(self, destination_id: int, **fields: object) -> bool:
        """
        Update a destination's mutable fields.

        `kind`, `zone_id` and `endpoint_extension` are fixed: changing any of them turns the
        row into a different destination, and the zone's page would follow it mid-flight.

        Args:
            destination_id: Destination id
            **fields: Any of label, enabled, sort_order, auto_answer_override,
                multicast_address, multicast_port

        Returns:
            bool: True if a write was attempted and succeeded
        """
        sets = [(k, v) for k, v in fields.items() if k in self._UPDATABLE]
        if not sets:
            return False

        assignments = ", ".join(f"{k} = %s" for k, _ in sets)
        params = (*(v for _, v in sets), destination_id)
        return self.db.execute(
            f"UPDATE paging_destinations SET {assignments} WHERE id = %s", params
        )

    def delete(self, destination_id: int) -> bool:
        """
        Delete a destination.

        Args:
            destination_id: Destination id

        Returns:
            bool: True if successful
        """
        return self.db.execute("DELETE FROM paging_destinations WHERE id = %s", (destination_id,))


# Global instance
_database = None


def get_database(config: dict | None = None) -> DatabaseBackend:
    """
    Get or create database backend instance.

    Args:
        config: Configuration dict. Required for first initialization.

    Returns:
        DatabaseBackend instance or None if not yet initialized.
        Callers must check for None before using.
    """
    global _database
    if _database is None and config is not None:
        _database = DatabaseBackend(config)
    return _database
