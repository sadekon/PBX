"""
The timezone times are *shown* in.

The PBX stores and reasons in UTC throughout, which is correct and should not change: a
recording made during a DST transition still has one unambiguous ordering. But a voicemail
email saying "09:15 AM" when the caller rang at 05:15 is wrong in the only way a recipient can
check, and there was no configuration anywhere to correct it.

So: UTC everywhere internally, converted once at the edge, for display only. Nothing here
should ever be used to compute a duration, an expiry or a sort order.
"""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pbx.utils.logger import get_logger

__all__ = ["display_timezone", "to_display"]

#: Where the display timezone is read from. Top-level, because it is not a voicemail concern --
#: emergency alerts and call logs render timestamps for people too.
CONFIG_KEY = "timezone"


def display_timezone(config: Any | None = None) -> tzinfo:
    """
    The timezone to render times in.

    Falls back to the host's local timezone when unset, which is what an operator who has not
    thought about it expects. Note that a server frequently *is* UTC, so a deployment whose
    users are not in UTC should set this explicitly rather than rely on the fallback.

    An unknown name is reported and falls back rather than raising: a typo in config.yml
    should cost a correct clock, not the ability to send voicemail notifications.
    """
    name = ""
    if config is not None:
        try:
            name = str(config.get(CONFIG_KEY, "") or "").strip()
        except Exception:
            # Config objects vary across the callers here, and a missing key must not be fatal.
            name = ""

    if not name:
        # astimezone() with no argument resolves the host's local zone, DST included.
        return datetime.now().astimezone().tzinfo or UTC

    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        get_logger().warning(
            f"{CONFIG_KEY} {name!r} is not a known IANA zone (expected e.g. "
            "'America/New_York'); showing times in the host's local timezone instead"
        )
        return datetime.now().astimezone().tzinfo or UTC


def to_display(value: datetime, tz: tzinfo | None = None) -> datetime:
    """
    Move a stored timestamp into the display timezone.

    A naive datetime is taken as UTC, because that is what this codebase writes -- database
    drivers hand back naive values for a TIMESTAMP column, and treating those as host-local
    would shift every timestamp read from the database by the host's offset.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(tz or display_timezone())
