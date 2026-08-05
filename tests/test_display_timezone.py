"""
Display-timezone resolution (``pbx/utils/timezone.py``).

Regression cover for voicemail emails showing UTC. The PBX stores UTC everywhere, which is
correct, but rendered it straight into the email -- so a caller who rang at 09:15 local was
reported as 13:15, with nothing in the text to say which zone that was.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from pbx.utils.timezone import display_timezone, to_display


def _config(**values):
    stub = MagicMock()
    stub.get.side_effect = lambda key, default=None: values.get(key, default)
    return stub


@pytest.mark.unit
class TestDisplayTimezone:
    def test_configured_zone_is_used(self):
        assert display_timezone(_config(timezone="America/New_York")) == ZoneInfo(
            "America/New_York"
        )

    def test_unset_falls_back_to_the_host_zone(self):
        """Whatever the host is, it must be a usable tzinfo rather than None."""
        assert display_timezone(_config()) is not None

    def test_no_config_at_all_is_survivable(self):
        assert display_timezone(None) is not None

    def test_an_unknown_zone_falls_back_instead_of_raising(self):
        """A typo should cost a correct clock, not the ability to send notifications."""
        assert display_timezone(_config(timezone="Mars/Olympus_Mons")) is not None

    def test_a_config_that_raises_is_survivable(self):
        broken = MagicMock()
        broken.get.side_effect = RuntimeError("config exploded")

        assert display_timezone(broken) is not None


@pytest.mark.unit
class TestToDisplay:
    def test_utc_is_converted(self):
        moved = to_display(datetime(2026, 7, 30, 13, 15, tzinfo=UTC), ZoneInfo("America/New_York"))

        assert (moved.hour, moved.minute) == (9, 15)

    def test_naive_is_treated_as_utc(self):
        """
        The important one.

        Database drivers return naive datetimes for a TIMESTAMP column. Treating those as
        host-local would shift every timestamp read back from the database by the host offset,
        so a message would change its displayed time depending on where it was read from.
        """
        naive = datetime(2026, 7, 30, 13, 15)  # noqa: DTZ001 - naive is the whole point
        moved = to_display(naive, ZoneInfo("America/New_York"))

        assert (moved.hour, moved.minute) == (9, 15)

    def test_an_already_local_timestamp_is_not_shifted_twice(self):
        eastern = ZoneInfo("America/New_York")
        already = datetime(2026, 7, 30, 9, 15, tzinfo=eastern)

        assert to_display(already, eastern) == already

    def test_daylight_saving_is_applied_per_date(self):
        eastern = ZoneInfo("America/New_York")
        winter = to_display(datetime(2026, 1, 15, 17, 0, tzinfo=UTC), eastern)
        summer = to_display(datetime(2026, 7, 15, 17, 0, tzinfo=UTC), eastern)

        # Same UTC hour, one hour apart locally: EST is -5, EDT is -4.
        assert winter.hour == 12
        assert summer.hour == 13
