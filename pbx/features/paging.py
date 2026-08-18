"""
Paging System Feature

Overhead paging: dial a zone's extension and the PBX answers, opens a one-way leg to every
destination in that zone, and relays your audio to them.

THREE WORDS, USED CONSISTENTLY
    zone         A place you can address by dialling a number. "Warehouse" = 701. Zones exist
                 for scoping -- so the loading dock can be paged without the executive
                 offices. They own no hardware.
    destination  One mechanism that puts audio into a zone. Today a `sip_endpoint`: an ATA's
                 FXS port, driving an amplifier and its horn speakers. `multicast` -- desk
                 phone speakers, which have no SIP dialog at all -- is accepted by the schema
                 and not yet sent to.
    page         One live session, from one caller to one zone.

WHAT THIS MODULE NO LONGER DOES
    It used to keep a `dac_devices` list holding device_id, device_type, sip_uri, ip_address
    and port -- a second hardware registry shadowing provisioned_devices, in memory only,
    maintained by hand. It is gone. An ATA is an ordinary extension; its MAC, vendor, model
    and IP are in provisioned_devices, and a destination names only the extension its FXS
    port answers on. A dual-port ATA is two extensions off one device row, which is correct:
    two ports drive two independent amplifier circuits.

    Zones and destinations also used to live in `config.yml` and in instance attributes that a
    restart emptied. They are in Postgres now. Configuration carries only deployment-level
    settings -- whether paging runs at all, and the default page cutoff.
"""

import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pbx.utils.logger import get_logger

#: Vendor -> the auto-answer header that vendor's endpoints honour. Derived from
#: provisioned_devices.vendor rather than stored per destination, so vendor knowledge lives in
#: one place and a re-vendored ATA cannot leave a stale value behind.
#:
#: `{server_ip}` is substituted at send time. A value of None means send no header at all --
#: which is also what the "none" override means, for an amplifier that seizes the line on ring
#: voltage itself and needs no SIP-level auto-answer.
AUTO_ANSWER_HEADERS: dict[str, tuple[str, str] | None] = {
    "cisco": ("Call-Info", "<sip:{server_ip}>;answer-after=0"),
    "yealink": ("Call-Info", "<sip:{server_ip}>;answer-after=0"),
    "zultys": ("Call-Info", "<sip:{server_ip}>;answer-after=0"),
    "grandstream": ("Alert-Info", "<http://127.0.0.1>;info=alert-autoanswer"),
    "polycom": ("Alert-Info", "Ring Answer"),
    "none": None,
}

#: Used when a destination's extension has no provisioned device, or its vendor is not in the
#: table above. `Call-Info: answer-after=0` is the most widely honoured of the forms, so it is
#: the least bad guess -- and a destination that guesses wrong is fixed by setting
#: auto_answer_override, not by editing code.
DEFAULT_AUTO_ANSWER_VENDOR = "cisco"


@dataclass
class PagingDestination:
    """
    One destination, resolved against its hardware and ready to page.

    Built once when a page starts and then read from the media path, so the handler never
    goes back to the database while audio is flowing.
    """

    destination_id: int
    kind: str
    label: str | None = None

    # kind == "sip_endpoint"
    endpoint_extension: str | None = None
    endpoint_name: str | None = None
    auto_answer_override: str | None = None

    # kind == "multicast" -- carried, not yet sent to
    multicast_address: str | None = None
    multicast_port: int | None = None

    # joined from provisioned_devices; all optional, since a destination may name an
    # extension that was never provisioned through this PBX
    mac_address: str | None = None
    vendor: str | None = None
    model: str | None = None
    device_type: str | None = None
    static_ip: str | None = None

    @property
    def display_name(self) -> str:
        """A name for logs and the admin view, falling back through what is known."""
        return (
            self.label
            or self.endpoint_name
            or self.endpoint_extension
            or self.multicast_address
            or f"destination {self.destination_id}"
        )

    def auto_answer_header(self, server_ip: str) -> tuple[str, str] | None:
        """
        The header that makes this endpoint answer without a human.

        Args:
            server_ip: The PBX address to advertise inside the header

        Returns:
            (name, value), or None when this destination should be INVITEd with no
            auto-answer header at all
        """
        vendor = (self.auto_answer_override or self.vendor or DEFAULT_AUTO_ANSWER_VENDOR).lower()
        form = AUTO_ANSWER_HEADERS.get(vendor, AUTO_ANSWER_HEADERS[DEFAULT_AUTO_ANSWER_VENDOR])
        if form is None:
            return None
        name, template = form
        return name, template.format(server_ip=server_ip)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "PagingDestination":
        """
        Build from a `list_resolved_for_zone` row.

        Args:
            row: Joined destination + device row

        Returns:
            PagingDestination
        """
        return cls(
            destination_id=row["id"],
            kind=row["kind"],
            label=row.get("label"),
            endpoint_extension=row.get("endpoint_extension"),
            endpoint_name=row.get("endpoint_name"),
            auto_answer_override=row.get("auto_answer_override"),
            multicast_address=row.get("multicast_address"),
            multicast_port=row.get("multicast_port"),
            mac_address=row.get("mac_address"),
            vendor=row.get("vendor"),
            model=row.get("model"),
            device_type=row.get("device_type"),
            static_ip=row.get("static_ip"),
        )


@dataclass
class ActivePage:
    """One page in flight."""

    page_id: str
    from_extension: str
    zone_id: int
    zone_extension: str
    zone_name: str
    destinations: list[PagingDestination]
    started_at: datetime
    call_id: str | None = None
    #: destination_id -> "ringing" | "answered" | "failed"
    destination_state: dict[int, str] = field(default_factory=dict)
    timeout_timer: threading.Timer | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialisable view for the API."""
        return {
            "page_id": self.page_id,
            "from_extension": self.from_extension,
            "zone_id": self.zone_id,
            "zone_extension": self.zone_extension,
            "zone_name": self.zone_name,
            "call_id": self.call_id,
            "started_at": self.started_at.isoformat(),
            "destinations": [
                {
                    "destination_id": d.destination_id,
                    "name": d.display_name,
                    "kind": d.kind,
                    "extension": d.endpoint_extension,
                    "state": self.destination_state.get(d.destination_id, "pending"),
                }
                for d in self.destinations
            ],
        }


class PagingSystem:
    """
    Zone and destination registry, and the register of pages in flight.

    Persistence is Postgres. Without a database this object still constructs and reports
    itself enabled, but every lookup returns empty -- paging is a configured feature, and a
    feature whose configuration cannot be stored has nothing to do.
    """

    def __init__(self, config: dict, database: Any | None = None) -> None:
        """
        Initialize the paging system.

        Args:
            config: Configuration dictionary
            database: DatabaseBackend instance, or None
        """
        self.logger = get_logger()
        self.config = config
        self.database = database
        self.enabled = config.get("features.paging.enabled", False)

        # Deployment-level settings. Everything describing a zone is in the database.
        self.default_max_duration = config.get("features.paging.max_duration", 120)
        self.default_auto_answer = config.get("features.paging.default_auto_answer")

        self.zones_db: Any | None = None
        self.destinations_db: Any | None = None
        if database is not None:
            from pbx.utils.database import PagingDestinationsDB, PagingZonesDB

            self.zones_db = PagingZonesDB(database)
            self.destinations_db = PagingDestinationsDB(database)

        # Reentrant: reserve_destinations() is called from begin_page(), which already holds it.
        self._lock = threading.RLock()

        self.active_pages: dict[str, ActivePage] = {}

        #: endpoint_extension -> page_id. The busy check is per destination rather than per
        #: zone because zones overlap by design: all-call and the warehouse both contain the
        #: warehouse ATA, so paging one while the other is live has to be refused even though
        #: the second zone is idle.
        self._busy_endpoints: dict[str, str] = {}

        #: Dialled numbers that route to paging. Refreshed on every zone write, so the SIP
        #: path never queries the database to classify a call.
        self._zone_extensions: set[str] = set()

        # Reference to PBX core, set by FeatureInitializer after construction.
        self.pbx_core: Any = None

        if self.enabled:
            self.refresh_zone_cache()
            self.logger.info("Paging system enabled")
            self.logger.info(f"Configured zones: {len(self._zone_extensions)}")
            if not self.zones_db:
                self.logger.warning(
                    "Paging is enabled but no database is available. Zones cannot be "
                    "loaded or stored, so no paging extension will route."
                )
            elif not self._zone_extensions:
                self.logger.info("No paging zones configured yet")
        else:
            self.logger.info("Paging system disabled")

    # ------------------------------------------------------------------ routing

    def refresh_zone_cache(self) -> None:
        """Reload the set of dialled numbers that route to paging."""
        if not self.zones_db:
            self._zone_extensions = set()
            return
        try:
            self._zone_extensions = set(self.zones_db.list_enabled_extensions())
        except Exception as e:
            self.logger.error(f"Could not refresh paging zone cache: {e}")

    def is_paging_extension(self, extension: str) -> bool:
        """
        Whether dialling `extension` reaches a paging zone.

        An exact match against configured zones, not a prefix test. The old
        `startswith("7")` claimed every extension beginning with a 7 -- including real users
        at 7123 or 750, whose calls then failed instead of ringing, and without even checking
        that a zone existed for the number.

        Args:
            extension: Dialled number

        Returns:
            bool: True if a zone answers to this number
        """
        if not self.enabled:
            return False
        return extension in self._zone_extensions

    # ------------------------------------------------------------------ zones

    def get_zones(self) -> list[dict]:
        """
        Every zone, dial order, each with its destinations attached.

        Returns:
            list: Zone dicts with a "destinations" key
        """
        if not self.enabled or not self.zones_db or not self.destinations_db:
            return []
        try:
            zones = self.zones_db.list_all() or []
            for zone in zones:
                zone["destinations"] = self.destinations_db.list_for_zone(zone["id"]) or []
            return zones
        except Exception as e:
            self.logger.error(f"Error listing paging zones: {e}")
            return []

    def get_zone(self, zone_id: int) -> dict | None:
        """
        One zone with its destinations.

        Args:
            zone_id: Zone id

        Returns:
            dict: Zone, or None
        """
        if not self.enabled or not self.zones_db or not self.destinations_db:
            return None
        zone = self.zones_db.get(zone_id)
        if zone:
            zone["destinations"] = self.destinations_db.list_for_zone(zone_id) or []
        return zone

    def get_zone_by_extension(self, extension: str) -> dict | None:
        """
        The zone reached by dialling `extension`.

        Args:
            extension: Dialled number

        Returns:
            dict: Zone row, or None
        """
        if not self.enabled or not self.zones_db:
            return None
        return self.zones_db.get_by_extension(extension)

    def create_zone(
        self,
        extension: str,
        name: str,
        description: str | None = None,
        max_duration_seconds: int | None = None,
    ) -> tuple[int | None, str | None]:
        """
        Create a zone.

        Args:
            extension: Number dialled to reach it
            name: Human name
            description: Optional note
            max_duration_seconds: Page cutoff; falls back to the configured default

        Returns:
            (zone_id, error). Exactly one is non-None.
        """
        if not self.enabled or not self.zones_db:
            return None, "Paging system is not available"

        extension = (extension or "").strip()
        name = (name or "").strip()
        if not extension or not name:
            return None, "A zone needs both a number and a name"

        conflict = self.zones_db.extension_conflict(extension)
        if conflict == "zone":
            return None, f"A paging zone already answers to {extension}"
        if conflict == "extension":
            return None, f"Extension {extension} is already in use by a user"

        zone_id = self.zones_db.create(
            extension=extension,
            name=name,
            description=description,
            max_duration_seconds=(
                self.default_max_duration if max_duration_seconds is None else max_duration_seconds
            ),
        )
        if zone_id is None:
            return None, "Could not save the zone"

        self.refresh_zone_cache()
        self.logger.info(f"Created paging zone {extension} ({name})")
        return zone_id, None

    def update_zone(self, zone_id: int, **fields: object) -> bool:
        """
        Update a zone's name, description, enabled flag or cutoff.

        Args:
            zone_id: Zone id
            **fields: Fields to change

        Returns:
            bool: True if written
        """
        if not self.enabled or not self.zones_db:
            return False
        updated = self.zones_db.update(zone_id, **fields)
        if updated:
            self.refresh_zone_cache()
        return bool(updated)

    def delete_zone(self, zone_id: int) -> tuple[bool, str | None]:
        """
        Delete a zone and its destinations.

        Refused while the zone is paging: tearing the row out from under a live page would
        leave legs up with nothing to hang them up.

        Args:
            zone_id: Zone id

        Returns:
            (ok, error)
        """
        if not self.enabled or not self.zones_db:
            return False, "Paging system is not available"

        with self._lock:
            for page in self.active_pages.values():
                if page.zone_id == zone_id:
                    return False, "That zone is paging right now"

        if not self.zones_db.delete(zone_id):
            return False, "Could not delete the zone"

        self.refresh_zone_cache()
        self.logger.info(f"Deleted paging zone {zone_id}")
        return True, None

    # ------------------------------------------------------------------ destinations

    def get_destinations(self, zone_id: int) -> list[dict]:
        """
        A zone's destinations.

        Args:
            zone_id: Zone id

        Returns:
            list: Destination rows
        """
        if not self.enabled or not self.destinations_db:
            return []
        return self.destinations_db.list_for_zone(zone_id) or []

    def add_sip_destination(
        self,
        zone_id: int,
        endpoint_extension: str,
        label: str | None = None,
        auto_answer_override: str | None = None,
    ) -> tuple[int | None, str | None]:
        """
        Add an ATA's FXS port to a zone.

        Args:
            zone_id: Owning zone
            endpoint_extension: The extension that port answers on
            label: Optional circuit name
            auto_answer_override: Force a header form, or "none"; NULL derives from vendor

        Returns:
            (destination_id, error)
        """
        if not self.enabled or not self.destinations_db or not self.zones_db:
            return None, "Paging system is not available"

        endpoint_extension = (endpoint_extension or "").strip()
        if not endpoint_extension:
            return None, "A destination needs an extension"

        if not self.zones_db.get(zone_id):
            return None, "That zone no longer exists"

        if auto_answer_override and auto_answer_override.lower() not in AUTO_ANSWER_HEADERS:
            known = ", ".join(sorted(AUTO_ANSWER_HEADERS))
            return None, f"Unknown auto-answer mode. Use one of: {known}"

        destination_id = self.destinations_db.add_sip_endpoint(
            zone_id=zone_id,
            endpoint_extension=endpoint_extension,
            label=label,
            auto_answer_override=auto_answer_override,
        )
        if destination_id is None:
            return None, (
                f"Could not add {endpoint_extension}. It may already be in this zone, "
                f"or may not be a known extension."
            )

        self.logger.info(f"Added paging destination {endpoint_extension} to zone {zone_id}")
        return destination_id, None

    def add_multicast_destination(
        self,
        zone_id: int,
        multicast_address: str,
        multicast_port: int,
        label: str | None = None,
    ) -> tuple[int | None, str | None]:
        """
        Add a multicast group to a zone.

        Stored, not yet streamed to.

        Args:
            zone_id: Owning zone
            multicast_address: Group address
            multicast_port: Group port
            label: Optional name

        Returns:
            (destination_id, error)
        """
        if not self.enabled or not self.destinations_db:
            return None, "Paging system is not available"

        destination_id = self.destinations_db.add_multicast(
            zone_id=zone_id,
            multicast_address=multicast_address,
            multicast_port=multicast_port,
            label=label,
        )
        if destination_id is None:
            return None, "Could not add the multicast destination"
        return destination_id, None

    def remove_destination(self, destination_id: int) -> tuple[bool, str | None]:
        """
        Remove a destination.

        Args:
            destination_id: Destination id

        Returns:
            (ok, error)
        """
        if not self.enabled or not self.destinations_db:
            return False, "Paging system is not available"

        destination = self.destinations_db.get(destination_id)
        if not destination:
            return False, "That destination no longer exists"

        endpoint = destination.get("endpoint_extension")
        with self._lock:
            if endpoint and endpoint in self._busy_endpoints:
                return False, "That destination is paging right now"

        if not self.destinations_db.delete(destination_id):
            return False, "Could not remove the destination"

        self.logger.info(f"Removed paging destination {destination_id}")
        return True, None

    def resolve_destinations(self, zone_id: int) -> list[PagingDestination]:
        """
        A zone's enabled destinations, with hardware joined in.

        Args:
            zone_id: Zone id

        Returns:
            list: Resolved destinations, display order
        """
        if not self.enabled or not self.destinations_db:
            return []
        rows = self.destinations_db.list_resolved_for_zone(zone_id) or []
        return [PagingDestination.from_row(row) for row in rows]

    # ------------------------------------------------------------------ pages

    def busy_endpoints(self, destinations: list[PagingDestination]) -> list[str]:
        """
        Which of these destinations are already carrying a page.

        Args:
            destinations: Resolved destinations

        Returns:
            list: Extensions that are busy
        """
        with self._lock:
            return [
                d.endpoint_extension
                for d in destinations
                if d.endpoint_extension and d.endpoint_extension in self._busy_endpoints
            ]

    def begin_page(
        self, from_extension: str, zone_extension: str, call_id: str | None = None
    ) -> tuple[ActivePage | None, str | None]:
        """
        Reserve a zone's destinations and register a page.

        Reservation and registration happen under one lock, so two callers dialling the same
        zone at the same instant cannot both be told the destinations were free.

        Args:
            from_extension: Who is paging
            zone_extension: The number they dialled
            call_id: The caller's SIP call id, if known yet

        Returns:
            (page, error). The error is caller-facing; "busy" specifically means answer 486.
        """
        if not self.enabled:
            return None, "Paging system is disabled"

        zone = self.get_zone_by_extension(zone_extension)
        if not zone:
            self.logger.warning(f"No paging zone answers to {zone_extension}")
            return None, "No such zone"

        if not zone.get("enabled", True):
            return None, "That zone is switched off"

        destinations = self.resolve_destinations(zone["id"])
        if not destinations:
            self.logger.warning(
                f"Paging zone {zone_extension} ({zone.get('name')}) has no enabled "
                f"destinations; nothing would hear this page"
            )
            return None, "That zone has no destinations"

        with self._lock:
            busy = self.busy_endpoints(destinations)
            if busy:
                self.logger.info(
                    f"Page to {zone_extension} refused: {', '.join(busy)} already paging"
                )
                return None, "busy"

            page_id = (
                f"page-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
                f"-{from_extension}-{uuid.uuid4().hex[:8]}"
            )
            page = ActivePage(
                page_id=page_id,
                from_extension=from_extension,
                zone_id=zone["id"],
                zone_extension=zone_extension,
                zone_name=zone.get("name") or zone_extension,
                destinations=destinations,
                started_at=datetime.now(UTC),
                call_id=call_id,
            )
            for destination in destinations:
                if destination.endpoint_extension:
                    self._busy_endpoints[destination.endpoint_extension] = page_id
                page.destination_state[destination.destination_id] = "pending"

            self.active_pages[page_id] = page

        max_duration = zone.get("max_duration_seconds", self.default_max_duration)
        if max_duration and max_duration > 0:
            timer = threading.Timer(max_duration, self._expire_page, args=(page_id,))
            timer.daemon = True
            timer.start()
            page.timeout_timer = timer

        self.logger.info(
            f"Paging {from_extension} -> {page.zone_name} ({zone_extension}), "
            f"{len(destinations)} destination(s), page {page_id}"
        )
        return page, None

    def set_destination_state(self, page_id: str, destination_id: int, state: str) -> None:
        """
        Record whether a destination's leg came up.

        Args:
            page_id: Page id
            destination_id: Destination id
            state: "ringing", "answered" or "failed"
        """
        with self._lock:
            page = self.active_pages.get(page_id)
            if page:
                page.destination_state[destination_id] = state

    def end_page(self, page_id: str) -> bool:
        """
        End a page and release its destinations.

        Idempotent: teardown can be reached from the caller hanging up, the duration timer,
        and an operator killing the page, and any of those may arrive second.

        Args:
            page_id: Page id

        Returns:
            bool: True if this call was the one that ended it
        """
        with self._lock:
            page = self.active_pages.pop(page_id, None)
            if not page:
                return False

            for destination in page.destinations:
                extension = destination.endpoint_extension
                if extension and self._busy_endpoints.get(extension) == page_id:
                    del self._busy_endpoints[extension]

        if page.timeout_timer:
            page.timeout_timer.cancel()

        duration = (datetime.now(UTC) - page.started_at).total_seconds()
        self.logger.info(f"Page {page_id} to {page.zone_name} ended after {duration:.0f}s")
        return True

    def _expire_page(self, page_id: str) -> None:
        """
        End a page that has run past its zone's cutoff.

        Args:
            page_id: Page id
        """
        with self._lock:
            page = self.active_pages.get(page_id)
        if not page:
            return

        self.logger.warning(f"Page {page_id} hit its duration limit, ending it")
        call_id = page.call_id
        if self.pbx_core and call_id:
            try:
                # Through the handler rather than end_call directly: the pager did not hang
                # up -- the PBX cut them off -- so their phone needs a BYE to leave the
                # dialog. end_call alone would leave it counting against a finished page.
                handler = getattr(self.pbx_core, "paging_handler", None)
                if handler:
                    handler.hang_up_pager(call_id)
                else:
                    self.pbx_core.end_call(call_id)
                return
            except Exception as e:
                self.logger.error(f"Could not end call {call_id} for expired page: {e}")

        self.end_page(page_id)

    def get_active_pages(self) -> list[dict]:
        """
        Every page in flight.

        Returns:
            list: Serialisable page views
        """
        with self._lock:
            return [page.to_dict() for page in self.active_pages.values()]

    def get_page(self, page_id: str) -> ActivePage | None:
        """
        One page in flight.

        Args:
            page_id: Page id

        Returns:
            ActivePage or None
        """
        with self._lock:
            return self.active_pages.get(page_id)

    def get_page_by_call(self, call_id: str) -> ActivePage | None:
        """
        The page a given call is carrying, if any.

        Used by teardown, which knows the call and not the page.

        Args:
            call_id: SIP call id

        Returns:
            ActivePage or None
        """
        with self._lock:
            for page in self.active_pages.values():
                if page.call_id == call_id:
                    return page
        return None

    def get_status(self) -> dict[str, Any]:
        """
        Whether paging is on, and whether it has anything to do.

        Lets the admin page tell "switched off" apart from "on but nothing configured yet",
        which are very different problems and used to look identical.

        Returns:
            dict: Status summary
        """
        zones = self.get_zones()
        destination_count = sum(len(zone.get("destinations", [])) for zone in zones)
        return {
            "enabled": self.enabled,
            "persistent": bool(self.zones_db),
            "zone_count": len(zones),
            "destination_count": destination_count,
            "active_page_count": len(self.active_pages),
            "default_max_duration": self.default_max_duration,
        }
