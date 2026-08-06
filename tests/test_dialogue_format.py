"""
Turning segments into a readable speaker-labelled transcript.

The thing being tested is mostly grouping. A recogniser emits a segment every few seconds, so
one sentence arrives as several; a transcript that prints them line by line is complete and
unreadable. The other half is overlap: per-channel transcription genuinely captures two people
talking at once, and a flat list of lines would quietly imply they took turns.
"""

import pytest

from pbx.speech.dialogue import OVERLAP_MARKER, Turn, clock, format_dialogue, group_turns
from pbx.speech.types import Segment


def seg(text, start, end, speaker=""):
    return Segment(text=text, start=start, end=end, speaker=speaker)


@pytest.mark.unit
class TestClock:
    def test_under_a_minute(self):
        assert clock(7.4) == "00:07"

    def test_minutes_and_seconds(self):
        assert clock(125) == "02:05"

    def test_hours_only_appear_when_needed(self):
        assert clock(3725) == "1:02:05"

    def test_negative_is_clamped(self):
        assert clock(-5) == "00:00"


@pytest.mark.unit
class TestGroupTurns:
    def test_consecutive_segments_from_one_speaker_become_one_turn(self):
        turns = group_turns(
            [
                seg("so what I wanted to ask", 0.0, 2.0, "1001"),
                seg("was whether the invoice", 2.0, 4.0, "1001"),
                seg("had gone out yet", 4.0, 6.0, "1001"),
            ]
        )

        assert len(turns) == 1
        assert turns[0].text == "so what I wanted to ask was whether the invoice had gone out yet"
        assert (turns[0].start, turns[0].end) == (0.0, 6.0)

    def test_a_change_of_speaker_starts_a_new_turn(self):
        turns = group_turns([seg("hello", 0.0, 1.0, "1001"), seg("hi there", 1.0, 2.0, "1002")])

        assert [t.speaker for t in turns] == ["1001", "1002"]

    def test_a_long_silence_breaks_a_turn(self):
        """Otherwise two remarks a minute apart are glued under one timestamp."""
        turns = group_turns(
            [seg("hello", 0.0, 1.0, "1001"), seg("still there?", 60.0, 61.0, "1001")]
        )

        assert len(turns) == 2

    def test_a_short_pause_does_not(self):
        turns = group_turns(
            [seg("hello", 0.0, 1.0, "1001"), seg("are you there", 1.5, 2.5, "1001")]
        )

        assert len(turns) == 1

    def test_an_interjection_splits_the_interrupted_speaker(self):
        turns = group_turns(
            [
                seg("so the thing is", 0.0, 2.0, "1001"),
                seg("wait", 2.0, 3.0, "1002"),
                seg("the invoice went out", 3.0, 5.0, "1001"),
            ]
        )

        assert [t.speaker for t in turns] == ["1001", "1002", "1001"]

    def test_out_of_order_input_is_sorted(self):
        turns = group_turns([seg("second", 5.0, 6.0, "1002"), seg("first", 0.0, 1.0, "1001")])

        assert [t.text for t in turns] == ["first", "second"]

    def test_empty_segments_are_dropped(self):
        """Recognisers emit empty segments for non-speech; they would open silent turns."""
        turns = group_turns([seg("", 0.0, 1.0, "1001"), seg("  ", 1.0, 2.0, "1001")])

        assert turns == []

    def test_no_segments_at_all(self):
        assert group_turns([]) == []


@pytest.mark.unit
class TestOverlap:
    def test_the_earlier_start_leads(self):
        turns = group_turns(
            [
                seg("but the invoice says", 5.5, 8.5, "1002"),
                seg("no listen to me", 5.0, 8.0, "1001"),
            ]
        )

        assert [t.speaker for t in turns] == ["1001", "1002"]

    def test_the_later_turn_is_marked(self):
        turns = group_turns(
            [seg("no listen to me", 5.0, 8.0, "1001"), seg("but the invoice", 5.5, 8.5, "1002")]
        )

        assert turns[0].overlaps_previous is False
        assert turns[1].overlaps_previous is True

    def test_clean_turn_taking_is_not_marked(self):
        turns = group_turns([seg("hello", 0.0, 2.0, "1001"), seg("hi there", 2.5, 4.0, "1002")])

        assert all(not t.overlaps_previous for t in turns)

    def test_untimed_segments_are_not_all_called_overlapping(self):
        """
        A backend that reports no timings puts everything at 0.0. Marking every line as an
        overlap would be worse than saying nothing.
        """
        turns = group_turns([seg("hello", 0.0, 0.0, "1001"), seg("hi there", 0.0, 0.0, "1002")])

        assert all(not t.overlaps_previous for t in turns)


@pytest.mark.unit
class TestFormatDialogue:
    def test_a_normal_exchange(self):
        text = format_dialogue(
            [
                seg("can you check the invoice", 4.0, 6.0, "1001"),
                seg("sure, one moment", 7.0, 8.0, "1002"),
            ]
        )

        assert text == ("[00:04] 1001: can you check the invoice\n[00:07] 1002: sure, one moment")

    def test_overlap_is_noted_in_the_text(self):
        text = format_dialogue(
            [seg("no listen to me", 5.0, 8.0, "1001"), seg("but the invoice", 5.5, 8.5, "1002")]
        )

        assert OVERLAP_MARKER in text
        assert text.index("no listen") < text.index("but the invoice")

    def test_overlap_marking_can_be_turned_off(self):
        text = format_dialogue(
            [seg("one", 5.0, 8.0, "1001"), seg("two", 5.5, 8.5, "1002")],
            mark_overlap=False,
        )

        assert OVERLAP_MARKER not in text

    def test_timestamps_can_be_turned_off(self):
        text = format_dialogue([seg("hello", 4.0, 6.0, "1001")], include_timestamps=False)

        assert text == "1001: hello"

    def test_unattributed_segments_get_no_label(self):
        """Voicemail has one speaker by definition, so its segments carry no name."""
        text = format_dialogue([seg("please call me back", 1.0, 3.0)])

        assert text == "[00:01] please call me back"

    def test_nothing_to_say(self):
        assert format_dialogue([]) == ""


@pytest.mark.unit
class TestTurn:
    def test_turns_are_immutable(self):
        turn = Turn(speaker="1001", text="hello", start=0.0, end=1.0)

        with pytest.raises(AttributeError):
            turn.text = "changed"
