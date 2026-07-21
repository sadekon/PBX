"""
Inbound DID routing: maps a carrier-assigned DID number (optionally scoped to
one trunk) to an internal destination (extension, auto attendant, or
voicemail box).

Two independent sources feed this: explicit ``InboundRoute`` rows (managed via
the admin UI/API) and an extension's own optional ``did_number`` field. They
are kept as two separate tables rather than one generated from the other --
see ``get_effective_routes()`` for how they're merged for display, and
``lookup()`` for how they're merged for call-time resolution.
"""

from typing import Any

from pbx.utils.logger import get_logger


class InboundRoutingSystem:
    """In-memory cache of explicit inbound DID routes, with extension-DID fallback."""

    def __init__(
        self,
        inbound_route_db: Any | None = None,
        extension_db: Any | None = None,
    ) -> None:
        """
        Initialize the inbound routing system

        Args:
            inbound_route_db: InboundRouteDB instance used to load persisted
                routes from the database. If omitted, only the extension-DID
                fallback is available.
            extension_db: ExtensionDB instance used to resolve an extension's
                own ``did_number`` when no explicit route matches.
        """
        self.inbound_route_db = inbound_route_db
        self.extension_db = extension_db
        self.logger = get_logger()
        # did_number -> list of route dicts (both trunk-scoped and any-trunk)
        self.routes: dict[str, list[dict]] = {}
        self._load_routes()

    def _load_routes(self) -> None:
        """Load persisted inbound routes from the database into ``self.routes``."""
        self.routes = {}
        if self.inbound_route_db is None:
            return

        try:
            rows = self.inbound_route_db.get_all()
        except (KeyError, TypeError, ValueError) as e:
            self.logger.error(f"Failed to load inbound routes from database: {e}")
            return

        for row in rows:
            self.routes.setdefault(row["did_number"], []).append(row)

    def reload_routes(self) -> None:
        """Discard all in-memory routes and reload them from the database (e.g. after an admin API write)."""
        self._load_routes()

    def lookup(self, did_number: str, trunk_id: str | None = None) -> dict | None:
        """
        Resolve the destination for an inbound call to ``did_number``.

        Prefers an explicit ``InboundRoute`` scoped to ``trunk_id`` over one
        that matches any trunk (``trunk_id`` is ``NULL``), and only falls back
        to an extension's own ``did_number`` if no explicit route matches at
        all -- an explicit route always wins if one exists for this DID.

        Args:
            did_number: The dialed DID number
            trunk_id: The trunk the call arrived on, if known

        Returns:
            dict: The best-matching route (with a "source" key of "manual" or
                "extension"), or None if nothing matches
        """
        candidates = [r for r in self.routes.get(did_number, []) if r.get("enabled", True)]

        if trunk_id is not None:
            trunk_specific = [r for r in candidates if r.get("trunk_id") == trunk_id]
            if trunk_specific:
                best = min(trunk_specific, key=lambda r: r.get("priority", 100))
                return {**best, "source": "manual"}

        any_trunk = [r for r in candidates if r.get("trunk_id") is None]
        if any_trunk:
            best = min(any_trunk, key=lambda r: r.get("priority", 100))
            return {**best, "source": "manual"}

        if self.extension_db is not None:
            extension = self.extension_db.get_by_did(did_number)
            if extension:
                return {
                    "did_number": did_number,
                    "trunk_id": None,
                    "destination_type": "extension",
                    "destination_value": extension["number"],
                    "enabled": True,
                    "priority": 100,
                    "source": "extension",
                }

        return None

    def get_effective_routes(self) -> list[dict]:
        """
        Return every route that could resolve an inbound call, for display in
        the admin dashboard: explicit ``InboundRoute`` rows plus one synthesized
        entry per extension with a ``did_number`` set.

        An extension-derived entry is marked ``shadowed: True`` when a manual
        route exists for the same DID -- ``lookup()`` always prefers the
        manual route, so a shadowed entry is informational only (it explains
        why that extension's DID isn't actually live), not a competing match.
        This is a simplification for trunk-scoped manual routes: a manual
        route scoped to one trunk technically only shadows calls arriving on
        that trunk, but the effective-routes view doesn't model per-trunk
        shadowing -- it flags the DID as shadowed regardless.

        Returns:
            list: Route dicts, each tagged with "source": "manual" | "extension"
        """
        effective: list[dict] = []
        manual_dids: set[str] = set(self.routes.keys())

        for routes in self.routes.values():
            effective.extend({**route, "source": "manual"} for route in routes)

        if self.extension_db is not None:
            for extension in self.extension_db.get_all():
                did_number = extension.get("did_number")
                if not did_number:
                    continue
                effective.append(
                    {
                        "did_number": did_number,
                        "trunk_id": None,
                        "destination_type": "extension",
                        "destination_value": extension["number"],
                        "enabled": True,
                        "priority": 100,
                        "source": "extension",
                        "shadowed": did_number in manual_dids,
                    }
                )

        effective.sort(key=lambda r: (r["did_number"], r.get("priority", 100)))
        return effective
