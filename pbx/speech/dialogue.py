"""
Rendering speaker-labelled segments as something a person would read.

Segments are not turns. A recogniser emits one per decoded window -- whisper roughly every
few seconds -- so a single uninterrupted sentence arrives as three or four segments, and
printing one line each produces a transcript that is technically complete and unreadable::

    1001: so what I wanted to ask
    1001: was whether the invoice
    1001: had gone out yet

This groups consecutive segments from the same speaker back into one turn, which is what a
transcript is supposed to look like::

    [00:04] 1001: so what I wanted to ask was whether the invoice had gone out yet

**Overlap is real and is kept.** Because each participant is recorded and transcribed on their
own channel, two people talking at once produce two genuine, complete turns rather than one
garbled one -- neither recogniser ever heard the other voice. A flat list has to put one first,
so the one that *started* first leads and the other is marked, rather than silently implying
they took turns::

    [00:05] 1001: no listen to me
    [00:05] 1002 (overlapping): but the invoice says

The timings are the authority on what actually happened; the marker just stops the text
version from lying about it.

Works on unattributed transcripts too -- voicemail has one speaker by definition, so its
segments carry no label and the speaker prefix is simply omitted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from pbx.speech.types import Segment

__all__ = ["Turn", "format_dialogue", "group_turns"]

#: A same-speaker pause longer than this starts a new turn rather than continuing the old one.
#:
#: Without it, a participant who says something at the start of a call and nothing again for
#: two minutes has both remarks glued into one sentence under a single timestamp. Two seconds
#: is comfortably longer than the pauses inside ordinary speech and shorter than a silence that
#: means the conversation moved on.
DEFAULT_TURN_GAP_SECONDS = 2.0

#: Appended to the speaker label when a turn began before the previous one had finished.
OVERLAP_MARKER = "(overlapping)"


@dataclass(frozen=True, slots=True)
class Turn:
    """One uninterrupted stretch of one speaker, assembled from consecutive segments."""

    speaker: str
    text: str
    start: float
    end: float
    #: True when this turn began before the previous turn ended -- people talking over
    #: each other, which per-channel transcription captures rather than losing.
    overlaps_previous: bool = False


def group_turns(
    segments: Sequence[Segment] | Iterable[Segment],
    *,
    max_gap_seconds: float = DEFAULT_TURN_GAP_SECONDS,
) -> list[Turn]:
    """
    Collapse segments into speaker turns, ordered by when each began.

    Args:
        segments: Segments in any order; they are sorted here.
        max_gap_seconds: A same-speaker silence longer than this breaks the turn.

    Returns:
        Turns in start order. Segments with no text are dropped -- a recogniser emits empty
        ones for non-speech, and they would otherwise open turns nobody spoke.
    """
    usable = [segment for segment in segments if segment.text and segment.text.strip()]
    if not usable:
        return []

    # Sorting by speaker as well keeps the order stable when two segments share a start time,
    # which happens whenever timings are absent and everything sits at 0.0.
    usable.sort(key=lambda segment: (segment.start, segment.speaker))

    # Who else was talking during each segment. Computed up front because a single forward
    # pass cannot see it: when two people speak at once their segments share a start time, so
    # the second speaker's segment sorts *after* the first speaker's and has not been reached
    # yet at the moment the merge decision is made. Without this, A-then-B-then-A-then-both
    # silently glues A's solo turn onto A's half of the overlap.
    concurrency = _concurrent_speakers(usable)

    turns: list[Turn] = []
    speaker = usable[0].speaker
    parts = [usable[0].text.strip()]
    start = usable[0].start
    end = usable[0].end
    others = concurrency[0]

    def flush(previous_end: float | None) -> None:
        """Close the turn being built, marking it if it started before the last one ended."""
        overlaps = previous_end is not None and start < previous_end and end > start
        turns.append(
            Turn(
                speaker=speaker,
                text=" ".join(parts),
                start=start,
                end=end,
                overlaps_previous=overlaps,
            )
        )

    for index, segment in enumerate(usable[1:], start=1):
        text = segment.text.strip()
        continues = (
            segment.speaker == speaker
            and segment.start - end <= max_gap_seconds
            # Only merge while the conversation around this speaker is unchanged. Someone
            # else starting or stopping is a new situation and belongs in its own turn --
            # otherwise a solo remark and the speaker's half of a subsequent argument read
            # as one uninterrupted sentence.
            and concurrency[index] == others
        )

        if continues:
            parts.append(text)
            end = max(end, segment.end)
            continue

        flush(turns[-1].end if turns else None)
        speaker, parts, start, end = segment.speaker, [text], segment.start, segment.end
        others = concurrency[index]

    flush(turns[-1].end if turns else None)
    return turns


def _concurrent_speakers(segments: list[Segment]) -> list[frozenset[str]]:
    """
    For each segment, the other speakers talking at the same time.

    Zero-length segments never overlap anything, which is what keeps untimed transcripts --
    where everything sits at 0.0 -- from being read as one continuous interruption.
    """
    result: list[frozenset[str]] = []
    for segment in segments:
        others = {
            other.speaker
            for other in segments
            if other.speaker != segment.speaker
            and other.start < segment.end
            and segment.start < other.end
        }
        result.append(frozenset(others))
    return result


def format_dialogue(
    segments: Sequence[Segment] | Iterable[Segment],
    *,
    include_timestamps: bool = True,
    mark_overlap: bool = True,
    max_gap_seconds: float = DEFAULT_TURN_GAP_SECONDS,
) -> str:
    """
    Render segments as a readable, speaker-labelled transcript.

    Args:
        segments: Segments to render, in any order.
        include_timestamps: Prefix each turn with its start time.
        mark_overlap: Note turns that began before the previous one ended.
        max_gap_seconds: A same-speaker silence longer than this breaks the turn.

    Returns:
        One line per turn. Empty string when there is nothing to say.
    """
    lines = []
    for turn in group_turns(segments, max_gap_seconds=max_gap_seconds):
        prefix = f"[{clock(turn.start)}] " if include_timestamps else ""

        label = turn.speaker
        if label and mark_overlap and turn.overlaps_previous:
            label = f"{label} {OVERLAP_MARKER}"
        elif not label and mark_overlap and turn.overlaps_previous:
            label = OVERLAP_MARKER

        lines.append(f"{prefix}{label}: {turn.text}" if label else f"{prefix}{turn.text}")

    return "\n".join(lines)


def clock(seconds: float) -> str:
    """Seconds as mm:ss, growing to h:mm:ss only when a call actually runs that long."""
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"
