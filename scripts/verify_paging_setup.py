#!/usr/bin/env python3
"""
Check a deployed PBX's paging setup end to end, without placing a call.

Answers the questions you cannot answer from the repo: did migration 1024 actually apply,
did the zone you created survive a restart, and is the ATA behind your destination
registered and reachable right now.

    python3 scripts/verify_paging_setup.py                # check what is there
    python3 scripts/verify_paging_setup.py --create-demo  # add a Test Page zone first

Run it on the PBX host, from the repo root, so config.yml and the database credentials
resolve the same way the server resolves them.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pbx.utils.config import Config
from pbx.utils.database import (
    DatabaseBackend,
    PagingDestinationsDB,
    PagingZonesDB,
)

OK = "\033[92m✓\033[0m"
BAD = "\033[91m✗\033[0m"
WARN = "\033[93m!\033[0m"


def main() -> int:
    """Run the checks. Returns a shell exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--create-demo",
        action="store_true",
        help="Create a 'Test Page' zone on 799 pointing at --ata, then re-check",
    )
    parser.add_argument(
        "--ata",
        default=None,
        help="Extension of the ATA FXS port to use with --create-demo",
    )
    parser.add_argument(
        "--recreate",
        action="store_true",
        help="With --create-demo, delete zone 799 first and build it again from scratch",
    )
    parser.add_argument(
        "--zone",
        default="799",
        help="Zone number to create or recreate (default: 799)",
    )
    args = parser.parse_args()

    config = Config()
    database = DatabaseBackend(config)
    if not database.connect():
        print(f"{BAD} Could not connect to the database. Check database.* in config.yml.")
        return 1
    print(f"{OK} Database connected")

    # --- schema -----------------------------------------------------------------
    applied = database.fetch_one(
        "SELECT version, name, applied_at FROM schema_migrations WHERE version = 1024"
    )
    if not applied:
        print(f"{BAD} Migration 1024 has NOT been applied -- restart the PBX to apply it")
        return 1
    print(f"{OK} Migration 1024 applied at {applied['applied_at']}")

    for table in ("paging_zones", "paging_destinations"):
        exists = database.fetch_one("SELECT to_regclass(%s) AS reg", (f"public.{table}",))
        if not exists or not exists.get("reg"):
            print(f"{BAD} Table {table} is missing")
            return 1
        print(f"{OK} Table {table} exists")

    zones_db = PagingZonesDB(database)
    destinations_db = PagingDestinationsDB(database)

    # --- optional demo zone -----------------------------------------------------
    if args.create_demo:
        if not args.ata:
            print(f"{BAD} --create-demo needs --ata <extension>")
            return 1

        existing = zones_db.get_by_extension(args.zone)

        if existing and args.recreate:
            # Destinations cascade with the zone, so this clears both.
            if not zones_db.delete(existing["id"]):
                print(f"{BAD} Could not delete the existing zone {args.zone}")
                return 1
            print(f"{OK} Deleted the old zone {args.zone}")
            existing = None

        if existing:
            print(
                f"{WARN} Zone {args.zone} already exists, leaving it alone "
                f"(pass --recreate to rebuild it)"
            )
        else:
            conflict = zones_db.extension_conflict(args.zone)
            if conflict:
                print(f"{BAD} {args.zone} is already taken by a {conflict}")
                return 1
            zone_id = zones_db.create(args.zone, "Test Page", description="Created by this script")
            if not zone_id:
                print(f"{BAD} Could not create the zone")
                return 1
            destination_id = destinations_db.add_sip_endpoint(
                zone_id, args.ata, label="Test amplifier"
            )
            if not destination_id:
                print(
                    f"{BAD} Could not add {args.ata} as a destination. Is it a real extension? "
                    f"The foreign key requires a row in `extensions`."
                )
                return 1
            print(f"{OK} Created zone {args.zone} -> {args.ata}")

    # --- what is configured -----------------------------------------------------
    zones = zones_db.list_all() or []
    if not zones:
        print(f"{WARN} No paging zones configured. Nothing will route to paging.")
        return 0

    print(f"\n{len(zones)} zone(s):\n")
    problems = 0

    for zone in zones:
        state = "enabled" if zone["enabled"] else "DISABLED"
        print(f"  [{zone['extension']}] {zone['name']} ({state})")

        resolved = destinations_db.list_resolved_for_zone(zone["id"], enabled_only=False) or []
        if not resolved:
            print(f"      {BAD} no destinations -- a page here reaches nothing")
            problems += 1
            continue

        for destination in resolved:
            if destination["kind"] != "sip_endpoint":
                print(
                    f"      {WARN} multicast {destination['multicast_address']}:"
                    f"{destination['multicast_port']} -- not implemented yet, nothing streams here"
                )
                continue

            extension = destination["endpoint_extension"]
            label = destination["label"] or "unnamed"
            vendor = destination["vendor"]

            # Is the ATA registered right now? Without this the INVITE has nowhere to go.
            registration = database.fetch_one(
                """
                SELECT ip_address, user_agent, expires_at
                FROM registered_phones
                WHERE extension = %s
                ORDER BY registered_at DESC
                LIMIT 1
                """,
                (extension,),
            )

            if not vendor:
                print(
                    f"      {WARN} {label} -> {extension}: no provisioned device, so the "
                    f"auto-answer header falls back to the Cisco form"
                )
            else:
                override = destination["auto_answer_override"]
                source = f"override={override}" if override else f"vendor={vendor}"
                print(f"      {OK} {label} -> {extension} ({destination['model']}, {source})")

            if registration:
                print(
                    f"          registered at {registration['ip_address']}"
                    f" ({registration['user_agent'] or 'no user agent'})"
                )
            else:
                print(f"          {BAD} NOT REGISTERED -- this destination cannot be reached")
                problems += 1

    print()
    if problems:
        print(f"{BAD} {problems} problem(s) above would stop a page reaching its speakers")
        return 1

    print(f"{OK} Paging looks ready. Dial a zone number from a desk phone to test.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
