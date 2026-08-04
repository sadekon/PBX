"""
Voicemail system
"""

import textwrap
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from pbx.mail import Attachment, EmailError
from pbx.utils.logger import get_logger, get_vm_ivr_logger

try:
    from pbx.utils.database import ExtensionDB

    EXTENSIONDB_AVAILABLE = True
except ImportError:
    EXTENSIONDB_AVAILABLE = False

try:
    from pbx.utils.encryption import get_encryption

    ENCRYPTION_AVAILABLE = True
except ImportError:
    ENCRYPTION_AVAILABLE = False


# Constants
GREETING_FILENAME = "greeting.wav"
MIN_WAV_HEADER_SIZE = 12  # Minimum size for RIFF/WAVE header check

# Cache the debug PIN logging flag at module level to avoid repeated environment lookups
# This value is set once when the module is loaded and doesn't change during runtime
import os

_DEBUG_PIN_LOGGING_ENABLED = os.environ.get("DEBUG_VM_PIN", "false").lower() in ("true", "1", "yes")


def is_subscribed_to_voicemail_email(extension_config: dict | None) -> bool:
    """
    Whether an extension wants new voicemail emailed to it.

    Defaults to True when the key is absent. Extensions predating the
    `voicemail_email_enabled` column, and those defined in config.yml rather than the
    database, have no value for it — and before the column existed every extension with an
    email address was notified unconditionally. Defaulting to False here would silently
    unsubscribe everyone the moment the migration ran.

    Args:
        extension_config: Extension row or config dict. None counts as unsubscribed, since
            there is no extension to notify.

    Returns:
        True if a notification should be sent.
    """
    if not extension_config:
        return False
    return bool(extension_config.get("voicemail_email_enabled", True))


class _PendingNotification:
    """
    Sends a voicemail notification once, whoever gets there first.

    Several things race to produce the email: the transcript arriving, a deadline expiring,
    the worker refusing the job, and the worker being drained at shutdown. All of them call
    :meth:`fire`; the first wins and the rest are no-ops. That compare-and-swap is the only
    thing standing between this design and either a duplicate email or a lost one.
    """

    def __init__(self, send: Any, logger: Any, label: str = "") -> None:
        self._send = send
        self._logger = logger
        self._label = label
        self._lock = threading.Lock()
        self._sent = False
        self._timer: threading.Timer | None = None

    def fire(self, transcript: Any | None) -> bool:
        """Send the notification if nobody has yet. Returns True if this call sent it."""
        with self._lock:
            if self._sent:
                self._logger.debug(f"Notification for {self._label} already sent; ignoring")
                return False
            self._sent = True
            timer, self._timer = self._timer, None

        if timer is not None:
            timer.cancel()

        try:
            self._send(transcript)
        except Exception as e:
            # Nothing above this catches, and losing the notification is the failure this
            # class exists to prevent -- so it is reported rather than propagated.
            self._logger.error(f"Failed to send voicemail notification for {self._label}: {e}")
        return True

    def arm_deadline(self, seconds: float) -> None:
        """
        Send without a transcript if one has not arrived within `seconds`.

        Bounds the *wait*, not the work. A running recognition cannot be cancelled; if it
        finishes after the deadline the transcript still reaches the database, it just missed
        the email.
        """
        if seconds <= 0:
            return
        timer = threading.Timer(seconds, self._on_deadline)
        # Timer threads are non-daemon by default and would hold the interpreter open for the
        # full deadline on shutdown.
        timer.daemon = True
        with self._lock:
            if self._sent:
                return
            self._timer = timer
        timer.start()

    def _on_deadline(self) -> None:
        if self.fire(None):
            self._logger.warning(
                f"Transcript for {self._label} did not arrive in time; notification sent without it"
            )


class VoicemailBox:
    """Represents a voicemail box for an extension"""

    def __init__(
        self,
        extension_number: str,
        storage_path: str = "voicemail",
        config: Any | None = None,
        mailer: Any | None = None,
        database: Any | None = None,
        transcription_service: Any | None = None,
    ) -> None:
        """
        Initialize voicemail box

        Args:
            extension_number: Extension number
            storage_path: Path to store voicemail files
            config: Config object
            mailer: pbx.mail.Mailer used to send notifications (optional)
            database: DatabaseBackend object (optional)
            transcription_service: pbx.speech.TranscriptionWorker (optional)
        """
        self.extension_number = extension_number
        self.storage_path = Path(storage_path) / extension_number
        self.messages = []
        self.logger = get_logger()
        self.config = config
        self.mailer = mailer
        self.database = database
        self.transcription_service = transcription_service
        self.pin = None  # Voicemail PIN (plaintext, for config file PINs)
        self.pin_hash = None  # Voicemail PIN hash (for database PINs)
        self.pin_salt = None  # Voicemail PIN salt (for database PINs)
        self.greeting_path = Path(self.storage_path) / GREETING_FILENAME  # Custom greeting file

        # Load PIN from database first (if available), then fall back to config
        pin_loaded = False
        # Note: Use getattr for 'enabled' attribute to safely handle cases where
        # database object may not have this attribute (e.g., in tests or older code)
        if database and getattr(database, "enabled", False) and EXTENSIONDB_AVAILABLE:
            try:
                ext_db = ExtensionDB(database)
                db_extension = ext_db.get(extension_number)
                if db_extension:
                    self.pin_hash = db_extension.get("voicemail_pin_hash")
                    self.pin_salt = db_extension.get("voicemail_pin_salt")
                    if self.pin_hash and self.pin_salt:
                        pin_loaded = True
                        self.logger.debug(
                            f"Loaded voicemail PIN hash from database for extension {extension_number}"
                        )
            except Exception as e:
                self.logger.error(
                    f"Error loading voicemail PIN from database for extension {extension_number}: {e}"
                )

        # Fall back to config file if PIN not loaded from database
        if not pin_loaded and config:
            ext_config = config.get_extension(extension_number)
            if ext_config:
                self.pin = ext_config.get("voicemail_pin")
                if self.pin:
                    self.logger.debug(
                        f"Loaded voicemail PIN from config file for extension {extension_number}"
                    )

        # Create storage directory
        Path(self.storage_path).mkdir(parents=True, exist_ok=True)

        # Load existing messages from disk
        self._load_messages()

    def _get_db_placeholder(self) -> str:
        """Get database parameter placeholder"""
        return "%s"

    def save_message(self, caller_id: str, audio_data: bytes, duration: float | None = None) -> str:
        """
        Save voicemail message

        Args:
            caller_id: ID of caller
            audio_data: Audio data bytes
            duration: Duration in seconds (optional)

        Returns:
            Message ID
        """
        timestamp = datetime.now(UTC)
        timestamp_str = timestamp.strftime("%Y%m%d_%H%M%S")
        message_id = f"{caller_id}_{timestamp_str}"

        file_path = Path(self.storage_path) / f"{message_id}.wav"

        with file_path.open("wb") as f:
            f.write(audio_data)

        message = {
            "id": message_id,
            "caller_id": caller_id,
            "timestamp": timestamp,
            "file_path": file_path,
            "listened": False,
            "duration": duration,
        }

        self.messages.append(message)
        self.logger.info(f"Voicemail saved to file system for extension {self.extension_number}")
        self.logger.info(f"  Message ID: {message_id}")
        self.logger.info(f"  Caller ID: {caller_id}")
        self.logger.info(f"  File path: {file_path}")
        self.logger.info(f"  Duration: {duration}s" if duration else "  Duration: unknown")

        # Save to database if available
        if self.database and self.database.enabled:
            self.logger.info("Saving voicemail metadata to database...")
            try:
                placeholder = self._get_db_placeholder()
                query = f"""
                INSERT INTO voicemail_messages
                (message_id, extension_number, caller_id, file_path, duration, listened, created_at)
                VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})
                """  # nosec B608 - placeholder is safely parameterized

                params = (
                    message_id,
                    self.extension_number,
                    caller_id,
                    str(file_path),
                    duration,
                    False,
                    timestamp,
                )

                self.logger.debug(f"  Database type: {self.database.db_type}")
                self.logger.debug(f"  Executing INSERT query for message_id: {message_id}")

                if self.database.execute(query, params):
                    self.logger.info(
                        f"✓ Voicemail metadata successfully saved to {self.database.db_type} database"
                    )
                    self.logger.info(f"  Extension: {self.extension_number}")
                    self.logger.info(f"  Message ID: {message_id}")
                    self.logger.info(f"  Caller: {caller_id}")
                else:
                    self.logger.warning(
                        f"✗ Failed to save voicemail metadata to database for extension {self.extension_number}"
                    )
                    self.logger.warning(f"  Message ID: {message_id}")
            except Exception as e:
                self.logger.error(f"✗ Error saving voicemail to database: {e}")
                self.logger.error(f"  Message ID: {message_id}")
                self.logger.error(f"  Extension: {self.extension_number}")
        else:
            self.logger.warning("Database not available - voicemail metadata NOT saved to database")
            self.logger.warning(f"  Message ID: {message_id} stored as file only")

        # Resolved once and shared by the transcription and email gates below.
        extension_config, email_address = self._lookup_extension()
        self._dispatch_notification(
            message=message,
            message_id=message_id,
            caller_id=caller_id,
            timestamp=timestamp,
            file_path=file_path,
            duration=duration,
            extension_config=extension_config,
            email_address=email_address,
        )

        return message_id

    def _dispatch_notification(
        self,
        *,
        message: dict,
        message_id: str,
        caller_id: str,
        timestamp: Any,
        file_path: Path,
        duration: float | None,
        extension_config: dict | None,
        email_address: str | None,
    ) -> None:
        """
        Arrange for exactly one notification email, with a transcript when one is available.

        The invariant: **every voicemail written to disk produces exactly one email, exactly
        once**, no matter what transcription does. A :class:`_PendingNotification` enforces it
        -- whichever of the transcript, the deadline, or an outright refusal to transcribe
        gets there first wins, and the rest are no-ops.

        Transcription itself is submitted, never awaited. It used to run inline here, on the
        thread tearing the call down.
        """
        if not (self.mailer and self.config and extension_config and email_address):
            return

        if not is_subscribed_to_voicemail_email(extension_config):
            self.logger.debug(
                f"Extension {self.extension_number} is unsubscribed from voicemail "
                "email; skipping notification"
            )
            return

        # Whether the transcript goes *in* the email is separate from whether we transcribe at
        # all: a site may want transcripts stored and searchable but kept out of mail. Only
        # the first case makes the email worth delaying.
        include_transcription = self.config.get("voicemail.email.include_transcription", True)

        notification = _PendingNotification(
            send=lambda transcript: self._send_notification_email(
                to_email=email_address,
                caller_id=caller_id,
                timestamp=timestamp,
                audio_file_path=file_path,
                duration=duration,
                transcription=transcript.text if transcript and transcript.text else None,
                confidence=transcript.confidence if transcript else None,
                provider=transcript.provider if transcript else None,
            ),
            logger=self.logger,
            label=message_id,
        )

        submitted = self._submit_transcription(
            message=message,
            message_id=message_id,
            file_path=file_path,
            duration=duration,
            notification=notification if include_transcription else None,
        )

        # The email is only allowed to wait when a job was actually accepted *and* the
        # transcript is wanted in the body. Anything else sends now.
        if not (submitted and include_transcription):
            notification.fire(None)

    def _submit_transcription(
        self,
        *,
        message: dict,
        message_id: str,
        file_path: Path,
        duration: float | None,
        notification: _PendingNotification | None,
    ) -> bool:
        """
        Hand the recording to the transcription worker.

        Returns True only when the worker accepted the job, which is its guarantee that the
        callback will fire exactly once. False means nothing was queued and the caller owns
        the outcome.
        """
        worker = self.transcription_service
        if worker is None:
            return False

        def on_complete(transcript: Any | None) -> None:
            # Fire the notification *before* touching the database. send_async never raises,
            # and a DB failure must not cost the email.
            if notification is not None:
                notification.fire(transcript)
            if transcript is not None and transcript.success:
                self._store_transcript(message, message_id, transcript)
            elif transcript is not None:
                self.logger.warning(
                    f"Voicemail {message_id} transcription failed: {transcript.error}"
                )

        accepted = worker.submit(
            file_path,
            on_complete,
            label=message_id,
            audio_seconds=duration,
        )
        if accepted and notification is not None:
            notification.arm_deadline(getattr(worker.settings, "deadline_seconds", 30.0))
        return accepted

    def _store_transcript(self, message: dict, message_id: str, transcript: Any) -> None:
        """Record the transcript on the in-memory message and in the database."""
        message["transcription"] = transcript.text
        message["transcription_confidence"] = transcript.confidence
        message["transcription_language"] = transcript.language
        message["transcription_provider"] = transcript.provider
        message["transcribed_at"] = datetime.now(UTC)

        if not (self.database and self.database.enabled):
            return

        try:
            placeholder = self._get_db_placeholder()
            query = f"""
            UPDATE voicemail_messages
            SET transcription_text = {placeholder},
                transcription_confidence = {placeholder},
                transcription_language = {placeholder},
                transcription_provider = {placeholder},
                transcribed_at = {placeholder}
            WHERE message_id = {placeholder}
            """  # nosec B608 - placeholder is safely parameterized
            self.database.execute(
                query,
                (
                    transcript.text,
                    transcript.confidence,
                    transcript.language,
                    transcript.provider,
                    message["transcribed_at"],
                    message_id,
                ),
            )
            self.logger.info(f"✓ Transcription saved to database for {message_id}")
        except Exception as e:
            self.logger.error(f"✗ Error saving transcription to database: {e}")

    def _lookup_extension(self) -> tuple[dict | None, str | None]:
        """
        Resolve this extension's configuration and email address.

        Database first, then config.yml, matching how the rest of this class resolves
        extension data. Both lookups are best-effort: a failure costs the notification or the
        transcript, never the recording, which is already safely on disk by this point.

        Returns:
            Tuple of (extension config, email address), either of which may be None.
        """
        extension_config = None
        email_address = None

        if self.database and getattr(self.database, "enabled", False):
            try:
                from pbx.utils.database import ExtensionDB

                db_extension = ExtensionDB(self.database).get(self.extension_number)
                if db_extension:
                    extension_config = db_extension
                    email_address = db_extension.get("email")
                    self.logger.debug(f"Found extension {self.extension_number} in database")
            except Exception as e:
                self.logger.error(f"Error getting extension from database: {e}")

        if not extension_config and self.config:
            extension_config = self.config.get_extension(self.extension_number)
            if extension_config:
                email_address = extension_config.get("email")
                self.logger.debug(f"Found extension {self.extension_number} in config")

        return extension_config, email_address

    @staticmethod
    def _format_timestamp(timestamp: Any) -> str:
        """
        Render a timestamp as ``09:15 AM, July 30, 2026``.

        Shared by the subject and the body so the two can never disagree. Anything that is
        not a datetime is passed through unchanged rather than guessed at.
        """
        if isinstance(timestamp, datetime):
            return timestamp.strftime("%I:%M %p, %B %d, %Y")
        return str(timestamp)

    def _caller_display(self, caller_id: str) -> str:
        """
        Resolve a caller ID to the name on that extension, when it is one of ours.

        An internal caller arrives as a bare extension number, which tells the recipient
        nothing. The name is looked up the same way the recipient's address is -- database
        first, then config.yml -- and the number is kept alongside it so nothing is lost.
        External callers have no extension to match and are returned unchanged.

        Args:
            caller_id: Caller ID as it arrived from the SIP wire.

        Returns:
            ``"Jane Smith (1001)"`` when the extension is known, otherwise the caller ID.
        """
        if not caller_id:
            return caller_id

        name = None
        # Every lookup below is best-effort: a failure must cost the name, never the
        # notification, so both paths are guarded and fall through to the raw caller ID.
        if self.database and getattr(self.database, "enabled", False):
            try:
                from pbx.utils.database import ExtensionDB

                db_extension = ExtensionDB(self.database).get(caller_id)
                if db_extension:
                    name = db_extension.get("name")
            except Exception as e:
                self.logger.debug(f"Could not resolve caller name for {caller_id}: {e}")

        if not name and self.config:
            try:
                extension_config = self.config.get_extension(caller_id)
                if extension_config:
                    name = extension_config.get("name")
            except Exception as e:
                self.logger.debug(f"Could not resolve caller name for {caller_id}: {e}")

        # Only a real string is usable here; anything else is treated as no name at all.
        if isinstance(name, str) and name.strip():
            return f"{name.strip()} ({caller_id})"
        return caller_id

    def _notification_subject(self, caller_id: str, timestamp: Any) -> str:
        """
        Render the subject line from the configured template.

        Content, so it lives with the feature rather than the transport. A template that
        references an unknown field is a config error, not a reason to lose the notification.
        """
        template = "New Voicemail from {caller_id}"
        if self.config:
            template = self.config.get(
                "voicemail.email.subject_template", "New Voicemail from {caller_id}"
            )

        caller_display = self._caller_display(caller_id)
        formatted_time = self._format_timestamp(timestamp)
        try:
            return template.format(
                caller_id=caller_display,
                timestamp=formatted_time,
                extension=self.extension_number,
            )
        except (IndexError, KeyError) as e:
            self.logger.warning(f"Invalid voicemail.email.subject_template ({e}); using default")
            return f"New Voicemail from {caller_display}"

    #: Human-readable names for the engines behind a transcript. Recipients are told which
    #: one produced the text so they can judge it -- an offline phone-audio model and a cloud
    #: service are not equally trustworthy, and the difference matters when acting on it.
    TRANSCRIPTION_ENGINES: ClassVar[dict[str, str]] = {
        "vosk": "Vosk, an offline speech recognition engine",
        "faster-whisper": "Whisper, an offline speech recognition model",
        "google": "Google Cloud Speech-to-Text",
    }

    @classmethod
    def _transcription_disclaimer(
        cls, provider: str | None = None, confidence: float | None = None
    ) -> str:
        """
        Name the engine and warn that its output is not reliable.

        Both halves matter. Naming the engine tells the reader what produced this; the warning
        stops the transcript being treated as a record of what was said. Phone audio is 8 kHz
        and narrowband, so names, numbers and unusual words are where it fails first -- which
        is exactly the content someone is most likely to act on without listening.
        """
        engine = cls.TRANSCRIPTION_ENGINES.get(provider or "", provider or "an automated service")
        note = f"Transcribed automatically by {engine}."
        if confidence:
            note += f" Estimated accuracy {confidence:.0%}."
        return (
            f"{note} Machine transcription is often wrong about names, numbers and "
            "spelling -- listen to the recording before acting on anything important."
        )

    def _notification_body(
        self,
        caller_id: str,
        timestamp: Any,
        duration: float | None = None,
        transcription: str | None = None,
        confidence: float | None = None,
        provider: str | None = None,
    ) -> str:
        """
        Build the plain-text body of a new-voicemail notification.

        The transcript is rendered as its own section rather than inline with the metadata,
        because it is the part a recipient reads to decide whether to listen at all. It is
        labelled as machine-generated: a G.711 phone recording through a small offline model
        misreads names and numbers often enough that acting on it unverified is a real risk.
        """
        body = "Hello,\n\n"
        body += "You have received a new voicemail message.\n\n"
        body += "Message Details:\n"
        body += f"  Extension: {self.extension_number}\n"
        body += f"  From: {self._caller_display(caller_id)}\n"
        body += f"  Received: {self._format_timestamp(timestamp)}\n"

        if duration:
            mins = int(duration // 60)
            secs = int(duration % 60)
            body += f"  Duration: {mins}:{secs:02d}\n"

        if transcription:
            body += "\nTranscription:\n"
            # Wrapped rather than emitted as one long line: this is plain text, and mail
            # clients that do not reflow leave an unwrapped paragraph running off-screen.
            body += textwrap.fill(
                transcription.strip(), width=78, initial_indent="  ", subsequent_indent="  "
            )
            body += "\n\n"
            body += textwrap.fill(
                self._transcription_disclaimer(provider, confidence),
                width=78,
                initial_indent="  ",
                subsequent_indent="  ",
            )
            body += "\n"

        body += f"\nTo listen to this message, please dial *{self.extension_number}\n"
        body += "\nBest regards,\n"
        body += "Warden VoIP\n"

        return body

    def _send_notification_email(
        self,
        to_email: str,
        caller_id: str,
        timestamp: Any,
        audio_file_path: str | Path | None = None,
        duration: float | None = None,
        transcription: str | None = None,
        confidence: float | None = None,
        provider: str | None = None,
    ) -> None:
        """
        Queue the new-voicemail notification.

        Asynchronous on purpose: this runs while the call is being torn down, and SMTP to an
        unreachable server blocks for the full connect timeout. The old code sent inline and
        held up teardown on a pooled thread for every message.
        """
        if not self.mailer:
            return

        attachments = []
        include_attachment = True
        include_transcription = True
        if self.config:
            include_attachment = self.config.get("voicemail.email.include_attachment", True)
            # Separate from whether transcription runs at all: a site may want transcripts
            # stored and searchable but kept out of email, which is a compliance question
            # rather than a feature toggle.
            include_transcription = self.config.get("voicemail.email.include_transcription", True)

        if include_attachment and audio_file_path:
            audio_path = Path(audio_file_path)
            if audio_path.exists():
                try:
                    attachments.append(Attachment.from_path(audio_path, mime_type="audio/wav"))
                except EmailError as e:
                    # A missing or unreadable recording must not cost us the notification.
                    self.logger.error(f"Could not attach voicemail recording: {e}")

        self.mailer.send_async(
            to_email,
            self._notification_subject(caller_id, timestamp),
            self._notification_body(
                caller_id,
                timestamp,
                duration,
                transcription=transcription if include_transcription else None,
                confidence=confidence if include_transcription else None,
                provider=provider if include_transcription else None,
            ),
            attachments=attachments,
        )

    def get_messages(self, unread_only: bool = False) -> list:
        """
        Get voicemail messages

        Args:
            unread_only: Only return unread messages

        Returns:
            list of message dictionaries
        """
        if unread_only:
            return [msg for msg in self.messages if not msg["listened"]]
        return self.messages

    def mark_listened(self, message_id: str) -> None:
        """
        Mark message as listened

        Args:
            message_id: Message identifier
        """
        for msg in self.messages:
            if msg["id"] == message_id:
                msg["listened"] = True
                self.logger.info(f"Marked voicemail {message_id} as listened")

                # Update database if available
                if self.database and self.database.enabled:
                    self.logger.info("Updating voicemail listened status in database...")
                    try:
                        placeholder = self._get_db_placeholder()
                        query = f"""
                        UPDATE voicemail_messages
                        SET listened = {placeholder}
                        WHERE message_id = {placeholder}
                        """  # nosec B608 - placeholder is safely parameterized
                        self.database.execute(query, (True, message_id))
                        self.logger.info(
                            f"✓ Successfully updated voicemail {message_id} as listened in {self.database.db_type} database"
                        )
                    except Exception as e:
                        self.logger.error(f"✗ Error updating voicemail in database: {e}")
                else:
                    self.logger.warning(
                        "Database not available - listened status not persisted to database"
                    )

                break

    def delete_message(self, message_id: str) -> bool:
        """
        Delete message

        Args:
            message_id: Message identifier

        Returns:
            True if deleted
        """
        for i, msg in enumerate(self.messages):
            if msg["id"] == message_id:
                self.logger.info(f"Deleting voicemail {message_id}...")

                # Delete file
                if Path(msg["file_path"]).exists():
                    Path(msg["file_path"]).unlink()
                    self.logger.info(f"  ✓ Deleted audio file: {msg['file_path']}")

                # Delete from database if available
                if self.database and self.database.enabled:
                    self.logger.info("Deleting voicemail from database...")
                    try:
                        placeholder = self._get_db_placeholder()
                        query = f"""
                        DELETE FROM voicemail_messages
                        WHERE message_id = {placeholder}
                        """  # nosec B608 - placeholder is safely parameterized
                        self.database.execute(query, (message_id,))
                        self.logger.info(
                            f"  ✓ Successfully deleted voicemail {message_id} from {self.database.db_type} database"
                        )
                    except Exception as e:
                        self.logger.error(f"  ✗ Error deleting voicemail from database: {e}")
                else:
                    self.logger.warning("  Database not available - only file deleted")

                # Remove from list
                self.messages.pop(i)
                self.logger.info(f"✓ Voicemail {message_id} deleted successfully")
                return True
        return False

    def _load_messages(self) -> None:
        """Load existing voicemail messages from database or disk"""
        # Try loading from database first if available
        if self.database and self.database.enabled:
            self.logger.info(
                f"Loading voicemail messages from database for extension {self.extension_number}..."
            )
            try:
                placeholder = self._get_db_placeholder()
                # Build query safely - placeholder is only '%s' or '?' from
                # internal method
                query = f"""
                SELECT message_id, caller_id, file_path, duration, listened, created_at,
                       transcription_text, transcription_confidence, transcription_language,
                       transcription_provider, transcribed_at
                FROM voicemail_messages
                WHERE extension_number = {placeholder}
                ORDER BY created_at DESC
                """  # nosec B608 - placeholder is safely parameterized
                self.logger.debug(
                    f"  Query: SELECT from voicemail_messages WHERE extension_number = {self.extension_number}"  # nosec B608 - log statement only
                )
                rows = self.database.fetch_all(query, (self.extension_number,))

                for row in rows:
                    # Convert created_at to datetime if it's a string
                    timestamp = row["created_at"]
                    if isinstance(timestamp, str):
                        try:
                            # Try ISO format first (Python 3.7+)
                            if hasattr(datetime, "fromisoformat"):
                                timestamp = datetime.fromisoformat(timestamp)
                            else:
                                # Fallback for Python < 3.7
                                # Try common timestamp formats
                                for fmt in ["%Y-%m-%d %H:%M:%S.%", "%Y-%m-%d %H:%M:%S"]:
                                    try:
                                        timestamp = datetime.strptime(timestamp, fmt).replace(
                                            tzinfo=UTC
                                        )
                                        break
                                    except ValueError:
                                        continue
                                else:
                                    # If parsing fails, use current time and
                                    # log warning
                                    self.logger.warning(
                                        f"Could not parse timestamp '{timestamp}' for voicemail {row['message_id']}, using current time"
                                    )
                                    timestamp = datetime.now(UTC)
                        except ValueError:
                            self.logger.warning(
                                f"Invalid timestamp format for voicemail {row['message_id']}, using current time"
                            )
                            timestamp = datetime.now(UTC)

                    message = {
                        "id": row["message_id"],
                        "caller_id": row["caller_id"],
                        "timestamp": timestamp,
                        "file_path": row["file_path"],
                        "listened": bool(row["listened"]),
                        "duration": row["duration"],
                    }

                    # Add transcription data if available
                    if row.get("transcription_text"):
                        message["transcription"] = row["transcription_text"]
                        message["transcription_confidence"] = row.get("transcription_confidence")
                        message["transcription_language"] = row.get("transcription_language")
                        message["transcription_provider"] = row.get("transcription_provider")
                        message["transcribed_at"] = row.get("transcribed_at")

                    self.messages.append(message)
                    self.logger.debug(
                        f"  Loaded message: {row['message_id']} from {row['caller_id']}"
                    )

                self.logger.info(
                    f"✓ Successfully loaded {len(self.messages)} voicemail message(s) from {self.database.db_type} database"
                )
                if len(self.messages) > 0:
                    unread_count = sum(1 for m in self.messages if not m["listened"])
                    self.logger.info(
                        f"  Total: {len(self.messages)} messages ({unread_count} unread)"
                    )
                return
            except Exception as e:
                self.logger.error(f"✗ Error loading voicemail messages from database: {e}")
                self.logger.warning("  Falling back to loading from file system")
                # Fall back to loading from disk
        else:
            self.logger.warning("Database not available - loading voicemails from file system only")

        # Load from disk if database is not available or failed
        if not Path(self.storage_path).exists():
            return

        for entry in Path(self.storage_path).iterdir():
            filename = entry.name
            if filename.endswith(".wav"):
                file_path = Path(self.storage_path) / filename
                # Parse message info from filename: {caller_id}_{timestamp}.wav
                name_without_ext = filename[:-4]
                parts = name_without_ext.split("_")

                if len(parts) >= 3:
                    caller_id = parts[0]
                    date_str = parts[1]
                    time_str = parts[2]

                    try:
                        timestamp = datetime.strptime(
                            f"{date_str}_{time_str}", "%Y%m%d_%H%M%S"
                        ).replace(tzinfo=UTC)

                        message = {
                            "id": name_without_ext,
                            "caller_id": caller_id,
                            "timestamp": timestamp,
                            "file_path": file_path,
                            "listened": False,  # Assume unlistened when loaded
                            "duration": None,  # Duration not stored, would need to parse WAV
                        }

                        self.messages.append(message)
                    except ValueError:
                        self.logger.warning(
                            f"Could not parse timestamp from voicemail file: {filename}"
                        )

    def set_pin(self, pin: str) -> bool:
        """
        set voicemail PIN

        Args:
            pin: 4-digit PIN string

        Returns:
            True if PIN was set successfully
        """
        if not pin or len(str(pin)) != 4 or not str(pin).isdigit():
            self.logger.warning(f"Invalid PIN format for extension {self.extension_number}")
            return False

        self.pin = str(pin)
        self.logger.info(f"Updated voicemail PIN for extension {self.extension_number}")
        return True

    def verify_pin(self, pin: str) -> bool:
        """
        Verify voicemail PIN

        Args:
            pin: PIN to verify

        Returns:
            True if PIN is correct
        """
        # First try hashed PIN from database (if available)
        if self.pin_hash and self.pin_salt and ENCRYPTION_AVAILABLE:
            try:
                enc = get_encryption()
                return enc.verify_password(str(pin), self.pin_hash, self.pin_salt)
            except Exception as e:
                self.logger.error(
                    f"Error verifying voicemail PIN hash for extension {self.extension_number}: {e}"
                )
                return False

        # Fall back to plaintext PIN from config (for backward compatibility)
        if self.pin:
            return str(pin) == str(self.pin)

        # No PIN configured
        return False

    def has_custom_greeting(self) -> bool:
        """
        Check if a custom greeting has been recorded

        Returns:
            True if custom greeting exists
        """
        exists = Path(self.greeting_path).exists()
        self.logger.debug(
            f"Checking custom greeting for extension {self.extension_number}: "
            f"{'exists' if exists else 'not found'} at {self.greeting_path}"
        )
        return exists

    def save_greeting(self, audio_data: bytes) -> bool:
        """
        Save custom voicemail greeting

        Args:
            audio_data: Audio data bytes (WAV format)

        Returns:
            True if greeting was saved successfully
        """
        try:
            # Verify audio data exists
            if not audio_data:
                self.logger.error("Cannot save greeting: audio data is empty")
                return False

            # Check for complete WAV/RIFF header (warn but don't fail for tests)
            if len(audio_data) >= MIN_WAV_HEADER_SIZE and not (
                audio_data.startswith(b"RIFF") and audio_data[8:12] == b"WAVE"
            ):
                self.logger.warning(
                    "Audio data may not be in WAV format (invalid or missing RIFF/WAVE header)"
                )

            with self.greeting_path.open("wb") as f:
                f.write(audio_data)
            self.logger.info(
                f"Saved custom greeting for extension {self.extension_number} ({len(audio_data)} bytes) to {self.greeting_path}"
            )

            # Verify the file was written successfully
            if Path(self.greeting_path).exists():
                file_size = Path(self.greeting_path).stat().st_size
                self.logger.info(f"Verified greeting file exists on disk ({file_size} bytes)")
                return True
            self.logger.error(f"Greeting file was not created at {self.greeting_path}")
            return False
        except (KeyError, OSError, TypeError, ValueError) as e:
            self.logger.error(f"Error saving greeting for extension {self.extension_number}: {e}")
            return False

    def get_greeting_path(self) -> Path | None:
        """
        Get path to custom greeting file

        Returns:
            Path to greeting file if it exists, None otherwise
        """
        if self.has_custom_greeting():
            self.logger.debug(
                f"Custom greeting path for extension {self.extension_number}: {self.greeting_path}"
            )
            return self.greeting_path
        self.logger.debug(f"No custom greeting for extension {self.extension_number}")
        return None

    def delete_greeting(self) -> bool:
        """
        Delete custom greeting

        Returns:
            True if greeting was deleted
        """
        try:
            if Path(self.greeting_path).exists():
                Path(self.greeting_path).unlink()
                self.logger.info(f"Deleted custom greeting for extension {self.extension_number}")
                return True
            return False
        except OSError as e:
            self.logger.error(f"Error deleting greeting for extension {self.extension_number}: {e}")
            return False


class VoicemailSystem:
    """Manages voicemail for all extensions"""

    def __init__(
        self,
        storage_path: str = "voicemail",
        config: Any | None = None,
        database: Any | None = None,
        mailer: Any | None = None,
        transcription_service: Any | None = None,
    ) -> None:
        """
        Initialize voicemail system

        Args:
            storage_path: Path to store voicemail files
            config: Config object
            database: DatabaseBackend object (optional)
            mailer: pbx.mail.Mailer owned by PBXCore (optional)
            transcription_service: pbx.speech.TranscriptionWorker owned by PBXCore (optional)
        """
        self.storage_path = storage_path
        self.mailboxes = {}
        self.logger = get_logger()
        self.config = config
        self.database = database
        # The mailer is a PBX-wide subsystem, not something voicemail constructs. Sharing one
        # transport is what lets emergency notification use it too.
        self.mailer = mailer
        # Same reasoning for the transcriber, plus a hard constraint: the Vosk model is ~40 MB
        # resident, so one instance per mailbox would not survive a few hundred extensions.
        self.transcription_service = transcription_service

        Path(storage_path).mkdir(parents=True, exist_ok=True)

    def get_mailbox(self, extension_number: str) -> VoicemailBox:
        """
        Get or create mailbox for extension

        Args:
            extension_number: Extension number

        Returns:
            VoicemailBox object
        """
        if extension_number not in self.mailboxes:
            self.mailboxes[extension_number] = VoicemailBox(
                extension_number,
                self.storage_path,
                config=self.config,
                mailer=self.mailer,
                database=self.database,
                transcription_service=self.transcription_service,
            )
        return self.mailboxes[extension_number]

    def save_message(
        self,
        extension_number: str,
        caller_id: str,
        audio_data: bytes,
        duration: float | None = None,
    ) -> str:
        """
        Save voicemail message

        Args:
            extension_number: Extension to save message for
            caller_id: Caller ID
            audio_data: Audio data
            duration: Duration in seconds (optional)

        Returns:
            Message ID
        """
        mailbox = self.get_mailbox(extension_number)
        return mailbox.save_message(caller_id, audio_data, duration)

    def send_daily_reminders(self) -> int:
        """
        Send daily reminders for unread voicemails

        Note: nothing calls this yet. The reminder scheduler is separate work; the send path
        below is current and works against the shared Mailer.

        Returns:
            Number of reminders sent
        """
        if not self.mailer or not self.config:
            return 0
        if not self.config.get("voicemail.reminders.enabled", False):
            return 0

        count = 0
        for extension_number, mailbox in self.mailboxes.items():
            unread_messages = mailbox.get_messages(unread_only=True)
            if not unread_messages:
                continue

            extension_config = self.config.get_extension(extension_number)
            if not extension_config:
                continue

            email_address = extension_config.get("email")
            if not email_address:
                continue

            if not is_subscribed_to_voicemail_email(extension_config):
                continue

            self.mailer.send_async(
                email_address,
                self._reminder_subject(len(unread_messages)),
                self._reminder_body(
                    extension_number,
                    unread_messages,
                    total_count=len(mailbox.get_messages(unread_only=False)),
                ),
            )
            count += 1

        return count

    @staticmethod
    def _reminder_subject(unread_count: int) -> str:
        """Subject for the unread-voicemail reminder."""
        plural = "s" if unread_count > 1 else ""
        return f"Voicemail Reminder: {unread_count} Unread Message{plural}"

    @staticmethod
    def _reminder_body(
        extension_number: str, messages: list, total_count: int | None = None
    ) -> str:
        """
        Body listing each unread message. Content, so it lives with the feature.

        Args:
            extension_number: Mailbox the summary is for.
            messages: The unread messages, which are the ones listed.
            total_count: Messages in the mailbox including those already heard. Reported
                alongside the unread count so the figure matches what the desk phone shows
                -- MWI advertises new *and* old counts, and a summary quoting only the
                unread number reads as though messages have gone missing. Omitted when it
                would merely repeat the unread count.
        """
        unread_count = len(messages)
        plural = "s" if unread_count > 1 else ""

        body = "Hello,\n\n"
        body += f"You have {unread_count} unread voicemail message{plural} "
        body += f"in your mailbox (Extension {extension_number})"
        if total_count is not None and total_count > unread_count:
            body += f", {total_count} in total"
        body += ":\n\n"

        for index, msg_info in enumerate(messages, 1):
            caller = msg_info.get("caller_id", "Unknown")
            ts = msg_info.get("timestamp")
            ts_str = VoicemailBox._format_timestamp(ts)
            body += f"{index}. From: {caller}, Received: {ts_str}\n"

        body += f"\nPlease check your voicemail by dialing *{extension_number}\n\n"
        body += "Best regards,\nWarden VoIP"

        return body

    def get_message_count(self, extension_number: str, unread_only: bool = True) -> int:
        """
        Get message count for extension

        Args:
            extension_number: Extension number
            unread_only: Only count unread messages

        Returns:
            Message count
        """
        mailbox = self.get_mailbox(extension_number)
        return len(mailbox.get_messages(unread_only))


class VoicemailIVR:
    """
    Interactive Voice Response system for voicemail access
    Handles DTMF-based menu navigation
    """

    # IVR States
    STATE_WELCOME = "welcome"
    STATE_PIN_ENTRY = "pin_entry"
    STATE_MAIN_MENU = "main_menu"
    STATE_PLAYING_MESSAGE = "playing_message"
    STATE_MESSAGE_MENU = "message_menu"
    STATE_OPTIONS_MENU = "options_menu"
    STATE_RECORDING_GREETING = "recording_greeting"
    STATE_GREETING_REVIEW = "greeting_review"
    STATE_GOODBYE = "goodbye"

    def __init__(self, voicemail_system: VoicemailSystem, extension_number: str) -> None:
        """
        Initialize voicemail IVR for an extension

        Args:
            voicemail_system: VoicemailSystem instance
            extension_number: Extension accessing voicemail
        """
        self.voicemail_system = voicemail_system
        self.extension_number = extension_number
        self.mailbox = voicemail_system.get_mailbox(extension_number)
        self.logger = get_vm_ivr_logger()  # Use dedicated VM IVR logger

        # IVR state
        self.state = self.STATE_WELCOME
        self.pin_attempts = 0
        self.max_pin_attempts = 3
        self.current_message_index = 0
        self.current_messages = []
        self.entered_pin = ""  # Collect PIN digits from user
        self.recorded_greeting_data = None  # Temporary storage for recorded greeting

        # Debug flag for PIN logging (controlled by DEBUG_VM_PIN environment variable)
        # WARNING: Only enable for testing/debugging - logs sensitive PIN data
        self.debug_pin_logging = _DEBUG_PIN_LOGGING_ENABLED
        if self.debug_pin_logging:
            self.logger.warning(
                f"[VM IVR] ⚠️  PIN DEBUG LOGGING ENABLED for extension {extension_number} - TESTING ONLY!"
            )
            self.logger.warning(
                "[VM IVR] ⚠️  set DEBUG_VM_PIN=false to disable sensitive PIN logging"
            )

        self.logger.info(f"Voicemail IVR initialized for extension {extension_number}")

    def handle_dtmf(self, digit: str) -> dict:
        """
        Handle DTMF digit based on current state

        Args:
            digit: DTMF digit ('0'-'9', '*', '#')

        Returns:
            dict: Action to take {'action': str, 'prompt': str, ...}
        """
        self.logger.debug(f"IVR state={self.state}, digit={digit}")

        if self.state == self.STATE_WELCOME:
            return self._handle_welcome(digit)
        if self.state == self.STATE_PIN_ENTRY:
            return self._handle_pin_entry(digit)
        if self.state == self.STATE_MAIN_MENU:
            return self._handle_main_menu(digit)
        if self.state == self.STATE_PLAYING_MESSAGE:
            return self._handle_playing_message(digit)
        if self.state == self.STATE_MESSAGE_MENU:
            return self._handle_message_menu(digit)
        if self.state == self.STATE_OPTIONS_MENU:
            return self._handle_options_menu(digit)
        if self.state == self.STATE_RECORDING_GREETING:
            return self._handle_recording_greeting(digit)
        if self.state == self.STATE_GREETING_REVIEW:
            return self._handle_greeting_review(digit)
        return {"action": "unknown_state", "prompt": "goodbye"}

    def _handle_welcome(self, digit: str) -> dict:
        """Handle welcome state"""
        # Transition to PIN entry automatically
        self.state = self.STATE_PIN_ENTRY

        # If the digit is a valid PIN digit (0-9), process it immediately
        # to avoid losing the first digit
        # Note: We use explicit string check instead of isdigit() for consistency
        # with _handle_pin_entry and to avoid accepting unicode digits
        if digit in "0123456789":
            return self._handle_pin_entry(digit)

        # Otherwise, just prompt for PIN entry (handles initialization with '*' or other non-digit)
        return {
            "action": "play_prompt",
            "prompt": "enter_pin",
            "message": "Please enter your PIN followed by pound",
        }

    def _handle_pin_entry(self, digit: str) -> dict:
        """Handle PIN entry state"""
        if digit == "#":
            # PIN entry complete - verify the entered PIN
            self.logger.info(f"[VM IVR PIN] # pressed, entered_pin length: {len(self.entered_pin)}")
            self.logger.debug(f"[VM IVR PIN] Verifying PIN for extension {self.extension_number}")

            # DEBUG LOGGING FOR TESTING PURPOSES ONLY (controlled by DEBUG_VM_PIN env var)
            # WARNING: This logs sensitive PIN data - only enable for troubleshooting
            if self.debug_pin_logging:
                self.logger.info(
                    f"[VM IVR PIN DEBUG] ⚠️  TESTING ONLY - Entered PIN: '{self.entered_pin}'"
                )
                self.logger.info(
                    f"[VM IVR PIN DEBUG] ⚠️  TESTING ONLY - Expected PIN: '{self.mailbox.pin}'"
                )

            pin_valid = self.mailbox.verify_pin(self.entered_pin)
            self.logger.info(
                f"[VM IVR PIN] PIN verification result: {'VALID' if pin_valid else 'INVALID'}"
            )

            # Clear PIN immediately after verification for security
            self.entered_pin = ""

            if pin_valid:
                self.state = self.STATE_MAIN_MENU
                unread_count = len(self.mailbox.get_messages(unread_only=True))
                self.logger.info("[VM IVR PIN] ✓ PIN accepted, transitioning to main menu")
                return {
                    "action": "play_prompt",
                    "prompt": "main_menu",
                    "message": f"You have {unread_count} new messages. Press 1 to listen, 2 for options, * to exit",
                }
            self.pin_attempts += 1
            self.logger.warning(
                f"[VM IVR PIN] ✗ Invalid PIN attempt {self.pin_attempts}/{self.max_pin_attempts}"
            )
            if self.pin_attempts >= self.max_pin_attempts:
                self.state = self.STATE_GOODBYE
                self.logger.warning("[VM IVR PIN] Maximum PIN attempts reached, hanging up")
                return {
                    "action": "hangup",
                    "prompt": "goodbye",
                    "message": "Too many failed attempts. Goodbye.",
                }
            return {
                "action": "play_prompt",
                "prompt": "invalid_pin",
                "message": "Invalid PIN. Please try again.",
            }
        if digit in "0123456789":
            # Collect PIN digit (limit length to prevent abuse)
            if len(self.entered_pin) < 10:  # Max 10 digits
                self.entered_pin += digit
                self.logger.debug(
                    f"[VM IVR PIN] Collected digit, entered_pin length now: {len(self.entered_pin)}"
                )
                # DEBUG LOGGING FOR TESTING PURPOSES ONLY (controlled by DEBUG_VM_PIN env var)
                # WARNING: This logs sensitive PIN data - only enable for troubleshooting
                if self.debug_pin_logging:
                    self.logger.info(
                        f"[VM IVR PIN DEBUG] ⚠️  TESTING ONLY - Digit '{digit}' collected, current PIN buffer: '{self.entered_pin}'"
                    )
            return {"action": "collect_digit", "prompt": "continue"}
        # Invalid input, ignore
        self.logger.debug(f"[VM IVR PIN] Ignoring invalid digit: {digit}")
        return {"action": "collect_digit", "prompt": "continue"}

    def _handle_main_menu(self, digit: str) -> dict:
        """Handle main menu state"""
        if digit == "1":
            # Listen to messages
            self.current_messages = self.mailbox.get_messages(unread_only=True)
            if not self.current_messages:
                self.current_messages = self.mailbox.get_messages(unread_only=False)

            if self.current_messages:
                self.current_message_index = 0
                self.state = self.STATE_PLAYING_MESSAGE
                msg = self.current_messages[0]
                return {
                    "action": "play_message",
                    "message_id": msg["id"],
                    "file_path": msg["file_path"],
                    "caller_id": msg["caller_id"],
                }
            return {
                "action": "play_prompt",
                "prompt": "no_messages",
                "message": "You have no messages",
            }

        if digit == "2":
            # Options menu
            self.state = self.STATE_OPTIONS_MENU
            return {
                "action": "play_prompt",
                "prompt": "options_menu",
                "message": "Press 1 to record greeting, * to return to main menu",
            }

        if digit == "*":
            # Exit
            self.state = self.STATE_GOODBYE
            return {"action": "hangup", "prompt": "goodbye", "message": "Goodbye"}

        return {
            "action": "play_prompt",
            "prompt": "invalid_option",
            "message": "Invalid option. Please try again.",
        }

    def _handle_playing_message(self, digit: str) -> dict:
        """Handle message playback state"""
        # Automatically transition to message menu after playback
        self.state = self.STATE_MESSAGE_MENU
        return {
            "action": "play_prompt",
            "prompt": "message_menu",
            "message": "Press 1 to replay, 2 for next message, 3 to delete, * for main menu",
        }

    def _handle_message_menu(self, digit: str) -> dict:
        """Handle message menu state"""
        if digit == "1":
            # Replay current message
            msg = self.current_messages[self.current_message_index]
            self.state = self.STATE_PLAYING_MESSAGE
            return {
                "action": "play_message",
                "message_id": msg["id"],
                "file_path": msg["file_path"],
                "caller_id": msg["caller_id"],
            }

        if digit == "2":
            # Next message
            self.current_message_index += 1
            if self.current_message_index < len(self.current_messages):
                msg = self.current_messages[self.current_message_index]
                self.state = self.STATE_PLAYING_MESSAGE
                return {
                    "action": "play_message",
                    "message_id": msg["id"],
                    "file_path": msg["file_path"],
                    "caller_id": msg["caller_id"],
                }
            self.state = self.STATE_MAIN_MENU
            return {
                "action": "play_prompt",
                "prompt": "no_more_messages",
                "message": "No more messages. Returning to main menu.",
            }

        if digit == "3":
            # Delete current message
            msg = self.current_messages[self.current_message_index]
            self.mailbox.delete_message(msg["id"])

            # Move to next message or main menu
            if self.current_message_index < len(self.current_messages) - 1:
                self.current_message_index += 1
                msg = self.current_messages[self.current_message_index]
                self.state = self.STATE_PLAYING_MESSAGE
                return {
                    "action": "play_message",
                    "message_id": msg["id"],
                    "file_path": msg["file_path"],
                    "caller_id": msg["caller_id"],
                }
            self.state = self.STATE_MAIN_MENU
            return {
                "action": "play_prompt",
                "prompt": "message_deleted",
                "message": "Message deleted. Returning to main menu.",
            }

        if digit == "*":
            # Return to main menu
            self.state = self.STATE_MAIN_MENU
            unread_count = len(self.mailbox.get_messages(unread_only=True))
            return {
                "action": "play_prompt",
                "prompt": "main_menu",
                "message": f"You have {unread_count} new messages. Press 1 to listen, 2 for options, * to exit",
            }

        return {
            "action": "play_prompt",
            "prompt": "invalid_option",
            "message": "Invalid option. Please try again.",
        }

    def _handle_options_menu(self, digit: str) -> dict:
        """Handle options menu state"""
        if digit == "1":
            # Record greeting
            self.state = self.STATE_RECORDING_GREETING
            return {
                "action": "start_recording",
                "recording_type": "greeting",
                "prompt": "record_greeting",
                "message": "Record your greeting after the tone. Press # when finished.",
            }

        if digit == "*":
            # Return to main menu
            self.state = self.STATE_MAIN_MENU
            unread_count = len(self.mailbox.get_messages(unread_only=True))
            return {
                "action": "play_prompt",
                "prompt": "main_menu",
                "message": f"You have {unread_count} new messages. Press 1 to listen, 2 for options, * to exit",
            }

        return {
            "action": "play_prompt",
            "prompt": "invalid_option",
            "message": "Invalid option. Please try again.",
        }

    def _handle_recording_greeting(self, digit: str) -> dict:
        """Handle recording greeting state"""
        if digit == "#":
            # Finish recording - transition to review state
            self.state = self.STATE_GREETING_REVIEW
            return {
                "action": "stop_recording",
                "save_as": "greeting",
                "prompt": "greeting_review_menu",
                "message": "Greeting recorded. Press 1 to listen, 2 to re-record, 3 to delete and use default, * to save and return to main menu",
            }

        # During recording, other digits are ignored
        return {"action": "continue_recording", "prompt": "continue"}

    def _handle_greeting_review(self, digit: str) -> dict:
        """Handle greeting review state after recording"""
        if digit == "1":
            # Play back the recorded greeting
            return {
                "action": "play_greeting",
                "prompt": "greeting_playback",
                "message": "Playing your greeting...",
            }

        if digit == "2":
            # Re-record the greeting
            self.recorded_greeting_data = None  # Clear previous recording
            self.state = self.STATE_RECORDING_GREETING
            return {
                "action": "start_recording",
                "recording_type": "greeting",
                "prompt": "record_greeting",
                "message": "Record your greeting after the tone. Press # when finished.",
            }

        if digit == "3":
            # Delete greeting and use default
            self.recorded_greeting_data = None
            if self.mailbox.has_custom_greeting():
                self.mailbox.delete_greeting()
            self.state = self.STATE_MAIN_MENU
            unread_count = len(self.mailbox.get_messages(unread_only=True))
            return {
                "action": "play_prompt",
                "prompt": "greeting_deleted",
                "message": f"Custom greeting deleted, using default. You have {unread_count} new messages. Press 1 to listen, 2 for options, * to exit",
            }

        if digit == "*":
            # Save the greeting and return to main menu
            if self.recorded_greeting_data:
                try:
                    success = self.mailbox.save_greeting(self.recorded_greeting_data)
                    if success:
                        self.recorded_greeting_data = None  # Clear only on successful save
                        self.state = self.STATE_MAIN_MENU
                        unread_count = len(self.mailbox.get_messages(unread_only=True))
                        return {
                            "action": "play_prompt",
                            "prompt": "greeting_saved",
                            "message": f"Greeting saved. You have {unread_count} new messages. Press 1 to listen, 2 for options, * to exit",
                        }
                    self.logger.error(
                        f"Failed to save greeting for extension {self.extension_number}"
                    )
                    return {
                        "action": "play_prompt",
                        "prompt": "error",
                        "message": "Error saving greeting. Press 2 to try again or 3 to cancel.",
                    }
                except Exception as e:
                    self.logger.error(f"Error saving greeting: {e}")
                    return {
                        "action": "play_prompt",
                        "prompt": "error",
                        "message": "Error saving greeting. Press 2 to try again or 3 to cancel.",
                    }
            self.state = self.STATE_MAIN_MENU
            unread_count = len(self.mailbox.get_messages(unread_only=True))
            return {
                "action": "play_prompt",
                "prompt": "main_menu",
                "message": f"You have {unread_count} new messages. Press 1 to listen, 2 for options, * to exit",
            }

        return {
            "action": "play_prompt",
            "prompt": "invalid_option",
            "message": "Invalid option. Press 1 to listen, 2 to re-record, 3 to delete, * to save.",
        }

    def save_recorded_greeting(self, audio_data: bytes) -> bool:
        """
        Save the recorded greeting temporarily for review

        Args:
            audio_data: Audio data bytes

        Returns:
            True if greeting was stored successfully
        """
        self.recorded_greeting_data = audio_data
        return True

    def get_recorded_greeting(self) -> bytes | None:
        """
        Get the temporarily stored greeting for playback

        Returns:
            bytes: Audio data or None
        """
        return self.recorded_greeting_data
