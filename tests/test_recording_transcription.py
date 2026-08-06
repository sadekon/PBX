"""
Per-channel transcription of finished call recordings.

The point of transcribing a recording channel by channel is that every line comes out already
attributed, and overlapping speech stays clean because the speakers never shared any audio.
The tests that matter are the ones proving attribution survives the merge, and that a channel
nobody spoke on never reaches a model at all -- handing silence to whisper is how you get a
confident transcript of something nobody said.
"""

import json
import wave

import numpy as np
import pytest

from pbx.speech.recording import (
    SCRATCH_DIRNAME,
    RecordingTranscriber,
    combine_regions,
    merge_channels,
)
from pbx.speech.types import Segment, Transcript
from pbx.utils.audio import active_speech_seconds, speech_regions

RATE = 8000


def speech(seconds: float, amplitude: int = 6000) -> np.ndarray:
    """A tone loud enough to read as speech."""
    count = int(RATE * seconds)
    t = np.arange(count) / RATE
    return (amplitude * np.sin(2 * np.pi * 300 * t)).astype(np.int16)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(RATE * seconds), dtype=np.int16)


def write_recording(path, channels: list[np.ndarray], labels: list[str], session="conv-1"):
    """A multi-channel WAV plus the sidecar the recorder writes beside it."""
    length = max(len(c) for c in channels)
    padded = [np.pad(c, (0, length - len(c))) for c in channels]
    interleaved = np.stack(padded, axis=1).astype("<i2")

    with wave.open(str(path), "wb") as out:
        out.setnchannels(len(channels))
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(interleaved.tobytes())

    path.with_suffix(".json").write_text(
        json.dumps(
            {
                "session_id": session,
                "channels": [
                    {"channel": i, "source": f"s{i}", "label": label}
                    for i, label in enumerate(labels)
                ],
            }
        )
    )
    return path


class FakeWorker:
    """Runs jobs inline, like TranscriptionWorker with workers=0."""

    def __init__(self, *, available=True, accept=True, texts=None):
        self.available = available
        self.accept = accept
        self.texts = texts or {}
        #: One entry per region file, named "<speaker>-<n>".
        self.submitted: list[str] = []

    @property
    def speakers(self) -> list[str]:
        """Who was transcribed, without the per-region suffix."""
        return sorted({name.rsplit("-", 1)[0] for name in self.submitted})

    def submit(self, path, on_complete, *, label="", audio_seconds=None, **_kwargs):
        self.submitted.append(path.stem)
        if not self.accept:
            return False

        speaker = path.stem.rsplit("-", 1)[0]
        text = self.texts.get(speaker, f"{speaker} speaking")
        on_complete(
            Transcript(
                text=text,
                segments=(Segment(text=text, start=1.0, end=2.0),),
                provider="fake",
                model="fake-1",
                language="en",
                audio_duration=audio_seconds or 0.0,
                processing_duration=0.1,
            )
        )
        return True


class FakeStore:
    def __init__(self):
        self.saved: list[dict] = []

    def save(self, transcript, *, source, call_id=None, media_path=None):
        self.saved.append(
            {
                "transcript": transcript,
                "source": source,
                "call_id": call_id,
                "media_path": media_path,
            }
        )
        return True


@pytest.mark.unit
class TestActiveSpeechSeconds:
    def test_silence_measures_zero(self):
        assert active_speech_seconds(silence(2.0).tobytes(), RATE) == 0.0

    def test_speech_measures_its_length(self):
        assert active_speech_seconds(speech(1.0).tobytes(), RATE) == pytest.approx(1.0, abs=0.05)

    def test_only_the_loud_part_counts(self):
        clip = np.concatenate([silence(1.0), speech(1.0), silence(1.0)])

        assert active_speech_seconds(clip.tobytes(), RATE) == pytest.approx(1.0, abs=0.05)

    def test_comfort_noise_is_not_speech(self):
        """Line noise sits far below the floor; treating it as speech defeats the gate."""
        noise = (np.random.default_rng(0).normal(0, 50, RATE).astype(np.int16)).tobytes()

        assert active_speech_seconds(noise, RATE) == 0.0

    def test_empty_input(self):
        assert active_speech_seconds(b"", RATE) == 0.0


@pytest.mark.unit
class TestSpeechRegions:
    """
    Where the audio gets cut before a model ever sees it.

    Every cut costs the model the context it uses for punctuation and capitalisation, so the
    rule is: only cut at a gap no sentence would contain, and never inside continuous speech
    however long it runs.
    """

    def test_a_turn_gap_splits(self):
        clip = np.concatenate([speech(2.0), silence(4.0), speech(2.0)])

        regions = speech_regions(clip.tobytes(), RATE)

        assert len(regions) == 2

    def test_a_pause_within_a_sentence_does_not(self):
        """0.4s is a breath, not a turn. Cutting here is what mangles punctuation."""
        clip = np.concatenate([speech(2.0), silence(0.4), speech(2.0)])

        regions = speech_regions(clip.tobytes(), RATE)

        assert len(regions) == 1

    def test_a_clause_pause_does_not_split_either(self):
        clip = np.concatenate([speech(1.5), silence(1.0), speech(1.5)])

        regions = speech_regions(clip.tobytes(), RATE)

        assert len(regions) == 1

    def test_continuous_speech_is_never_cut(self):
        regions = speech_regions(speech(30.0).tobytes(), RATE)

        assert len(regions) == 1

    def test_the_gap_threshold_is_configurable(self):
        clip = np.concatenate([speech(1.0), silence(1.0), speech(1.0)])

        assert len(speech_regions(clip.tobytes(), RATE, min_gap_seconds=0.5)) == 2
        assert len(speech_regions(clip.tobytes(), RATE, min_gap_seconds=2.0)) == 1

    def test_regions_are_padded_but_do_not_overlap(self):
        clip = np.concatenate([speech(2.0), silence(4.0), speech(2.0)])

        (first_start, first_end), (second_start, _) = speech_regions(clip.tobytes(), RATE)

        assert first_start == 0.0, "padding must clamp at the start of the audio"
        assert first_end > 2.0, "the tail of speech should be padded, not clipped"
        assert second_start < 6.0, "the onset should be padded, or consonants get clipped"
        assert second_start > first_end, "regions must not overlap"

    def test_a_brief_click_is_discarded(self):
        """Too short to carry context, which is exactly when a model invents words."""
        clip = np.concatenate([speech(0.05), silence(4.0), speech(2.0)])

        regions = speech_regions(clip.tobytes(), RATE)

        assert len(regions) == 1

    def test_silence_yields_nothing(self):
        assert speech_regions(silence(5.0).tobytes(), RATE) == []

    def test_empty_input(self):
        assert speech_regions(b"", RATE) == []


@pytest.mark.unit
class TestCombineRegions:
    def _t(self, *segments, duration=5.0):
        return Transcript(
            text=" ".join(s.text for s in segments),
            segments=segments,
            provider="p",
            model="m",
            language="en",
            audio_duration=duration,
            processing_duration=1.0,
        )

    def test_timestamps_are_offset_back_onto_the_call_timeline(self):
        """
        Each region is transcribed as its own file, so its timestamps start at zero. Without
        the offset every region would claim to have happened at the start of the call.
        """
        combined = combine_regions(
            [
                (0.0, self._t(Segment("first", 0.0, 2.0))),
                (30.0, self._t(Segment("later", 0.0, 2.0))),
            ]
        )

        assert [(s.start, s.end) for s in combined.segments] == [(0.0, 2.0), (30.0, 32.0)]

    def test_regions_are_ordered_by_offset(self):
        combined = combine_regions(
            [
                (30.0, self._t(Segment("later", 0.0, 1.0))),
                (0.0, self._t(Segment("first", 0.0, 1.0))),
            ]
        )

        assert [s.text for s in combined.segments] == ["first", "later"]

    def test_duration_spans_the_gap_between_regions(self):
        """The silence between regions is part of the call even though none was transcribed."""
        combined = combine_regions([(0.0, self._t(duration=2.0)), (30.0, self._t(duration=2.0))])

        assert combined.audio_duration == 32.0

    def test_processing_cost_is_the_sum(self):
        combined = combine_regions([(0.0, self._t()), (30.0, self._t())])

        assert combined.processing_duration == 2.0


@pytest.mark.unit
class TestMergeChannels:
    def _t(self, *segments):
        return Transcript(
            text=" ".join(s.text for s in segments),
            segments=segments,
            provider="p",
            model="m",
            language="en",
            audio_duration=10.0,
            processing_duration=1.0,
        )

    def test_segments_are_ordered_by_time_across_speakers(self):
        merged = merge_channels(
            {
                "1001": self._t(Segment("first", 0.0, 1.0), Segment("third", 4.0, 5.0)),
                "1002": self._t(Segment("second", 2.0, 3.0)),
            }
        )

        assert [s.text for s in merged.segments] == ["first", "second", "third"]

    def test_every_segment_is_attributed(self):
        merged = merge_channels(
            {"1001": self._t(Segment("hello", 0.0, 1.0)), "1002": self._t(Segment("hi", 1.0, 2.0))}
        )

        assert [s.speaker for s in merged.segments] == ["1001", "1002"]

    def test_text_is_a_timestamped_labelled_dialogue(self):
        merged = merge_channels(
            {"1001": self._t(Segment("hello", 0.0, 1.0)), "1002": self._t(Segment("hi", 1.0, 2.0))}
        )

        assert merged.text == "[00:00] 1001: hello\n[00:01] 1002: hi"

    def test_one_speakers_consecutive_segments_become_one_line(self):
        """
        Whisper emits a segment every few seconds, so a single sentence arrives in pieces.
        The stored text should read as a transcript, not as decoder output.
        """
        merged = merge_channels(
            {
                "1001": self._t(
                    Segment("so what I wanted", 0.0, 2.0), Segment("to ask was", 2.0, 4.0)
                ),
            }
        )

        assert merged.text == "[00:00] 1001: so what I wanted to ask was"

    def test_confidence_stays_none(self):
        """Averaging per-channel confidences produces a number describing nothing."""
        merged = merge_channels({"1001": self._t(Segment("hello", 0.0, 1.0))})

        assert merged.confidence is None

    def test_durations_combine_sensibly(self):
        merged = merge_channels(
            {"1001": self._t(Segment("a", 0.0, 1.0)), "1002": self._t(Segment("b", 0.0, 1.0))}
        )

        assert merged.audio_duration == 10.0, "audio duration is the call, not the sum"
        assert merged.processing_duration == 2.0, "processing cost is the sum of the passes"

    def test_overlapping_speech_keeps_both_speakers(self):
        """
        Two people talking at once must both survive. This is the whole reason the channels
        are transcribed separately: the audio never mixed, so neither recogniser ever heard
        the other voice and neither utterance can mask the other.
        """
        merged = merge_channels(
            {
                "1001": self._t(Segment("no listen to me", 5.0, 8.0)),
                "1002": self._t(Segment("but the invoice says", 5.5, 8.5)),
            }
        )

        assert [s.text for s in merged.segments] == ["no listen to me", "but the invoice says"]
        assert [s.speaker for s in merged.segments] == ["1001", "1002"]

    def test_overlap_is_preserved_in_the_timings(self):
        """
        The rendered text is one line per segment, so an overlap reads sequentially. The
        actual simultaneity is not lost -- it lives in the start/end times.
        """
        merged = merge_channels(
            {
                "1001": self._t(Segment("talking", 5.0, 8.0)),
                "1002": self._t(Segment("over you", 5.5, 8.5)),
            }
        )

        first, second = merged.segments
        assert second.start < first.end, "segments should still overlap in time"

    def test_a_transcript_without_segments_still_contributes_text(self):
        plain = Transcript(text="no timings here", provider="p", model="m")

        merged = merge_channels({"1001": plain})

        assert "no timings here" in merged.text


@pytest.mark.unit
class TestRecordingTranscriber:
    def _recording(self, tmp_path, channels, labels):
        return write_recording(tmp_path / "call.wav", channels, labels)

    def test_each_channel_is_transcribed_separately(self, tmp_path):
        path = self._recording(tmp_path, [speech(1.0), speech(1.0)], ["1001", "1002"])
        worker, store = FakeWorker(), FakeStore()

        assert RecordingTranscriber(worker, store).submit(path)

        assert worker.speakers == ["1001", "1002"]

    def test_the_result_is_one_attributed_transcript(self, tmp_path):
        path = self._recording(tmp_path, [speech(1.0), speech(1.0)], ["1001", "1002"])
        store = FakeStore()

        RecordingTranscriber(FakeWorker(), store).submit(path)

        assert len(store.saved) == 1
        saved = store.saved[0]
        assert saved["source"] == "recording"
        assert saved["call_id"] == "conv-1", "stored against the session, not the file"
        assert {s.speaker for s in saved["transcript"].segments} == {"1001", "1002"}

    def test_a_silent_channel_never_reaches_a_model(self, tmp_path):
        """
        The headline saving. A participant who only listened costs nothing, and cannot
        produce a hallucination, because no model runs on their channel.
        """
        path = self._recording(tmp_path, [speech(1.0), silence(1.0)], ["1001", "1002"])
        worker = FakeWorker()

        RecordingTranscriber(worker, FakeStore()).submit(path)

        assert worker.speakers == ["1001"]

    def test_an_entirely_silent_recording_submits_nothing(self, tmp_path):
        path = self._recording(tmp_path, [silence(1.0), silence(1.0)], ["1001", "1002"])
        worker, store = FakeWorker(), FakeStore()

        assert RecordingTranscriber(worker, store).submit(path) is False
        assert worker.submitted == []
        assert store.saved == []

    def test_temporary_channel_files_are_cleaned_up(self, tmp_path):
        path = self._recording(tmp_path, [speech(1.0), speech(1.0)], ["1001", "1002"])

        RecordingTranscriber(FakeWorker(), FakeStore()).submit(path)

        scratch = tmp_path / SCRATCH_DIRNAME
        assert list(scratch.iterdir()) == [], "a workspace survived the job"
        assert {p.name for p in tmp_path.iterdir()} == {"call.wav", "call.json", SCRATCH_DIRNAME}

    def test_working_files_live_in_the_scratch_directory(self, tmp_path):
        """
        They must not sit loose in recordings/, where retention's *.wav sweep would treat
        them as recordings and delete a running job's input.
        """
        path = self._recording(tmp_path, [speech(1.0)], ["1001"])
        seen: list[str] = []

        class Watcher(FakeWorker):
            def submit(self, region_path, on_complete, **kwargs):
                seen.append(str(region_path.relative_to(tmp_path)))
                return super().submit(region_path, on_complete, **kwargs)

        RecordingTranscriber(Watcher(), FakeStore()).submit(path)

        assert seen and all(name.startswith(f"{SCRATCH_DIRNAME}/") for name in seen)

    def test_channels_are_split_by_their_own_audio(self, tmp_path):
        """Each mono file must carry that channel's audio, not the interleaved stream."""
        path = self._recording(
            tmp_path, [speech(1.0, amplitude=9000), silence(1.0)], ["loud", "quiet"]
        )
        worker = FakeWorker()

        RecordingTranscriber(worker, FakeStore()).submit(path)

        assert worker.speakers == ["loud"]

    def test_a_refused_channel_does_not_strand_the_merge(self, tmp_path):
        """
        A refused job's callback never fires, so the outstanding count has to be released at
        the call site or the transcript is never assembled.
        """
        path = self._recording(tmp_path, [speech(1.0), speech(1.0)], ["1001", "1002"])
        worker, store = FakeWorker(accept=False), FakeStore()

        RecordingTranscriber(worker, store).submit(path)

        assert store.saved == [], "nothing was transcribed, so nothing should be stored"

    def test_a_disabled_worker_is_a_no_op(self, tmp_path):
        path = self._recording(tmp_path, [speech(1.0)], ["1001"])

        assert RecordingTranscriber(FakeWorker(available=False), FakeStore()).submit(path) is False

    def test_no_worker_at_all_is_a_no_op(self, tmp_path):
        path = self._recording(tmp_path, [speech(1.0)], ["1001"])

        assert RecordingTranscriber(None, FakeStore()).submit(path) is False

    def test_a_missing_manifest_is_not_fatal(self, tmp_path):
        path = self._recording(tmp_path, [speech(1.0)], ["1001"])
        path.with_suffix(".json").unlink()

        assert RecordingTranscriber(FakeWorker(), FakeStore()).submit(path) is False

    def test_a_missing_file_is_not_fatal(self, tmp_path):
        assert (
            RecordingTranscriber(FakeWorker(), FakeStore()).submit(tmp_path / "gone.wav") is False
        )

    def test_more_than_two_participants(self, tmp_path):
        path = self._recording(
            tmp_path,
            [speech(1.0), speech(1.0), speech(1.0)],
            ["1001", "1002", "+15551234567"],
        )
        store = FakeStore()

        RecordingTranscriber(FakeWorker(), store).submit(path)

        speakers = {s.speaker for s in store.saved[0]["transcript"].segments}
        assert speakers == {"1001", "1002", "+15551234567"}

    def test_simultaneous_speech_is_transcribed_on_both_channels(self, tmp_path):
        """
        End to end: both parties speaking across the same seconds. Each channel is submitted
        and each produces its own transcript, because the split happens before any model sees
        the audio. A mixed-mono pass would have one voice masking the other here.
        """
        path = self._recording(
            tmp_path,
            [speech(2.0, amplitude=7000), speech(2.0, amplitude=7000)],
            ["1001", "1002"],
        )
        worker, store = FakeWorker(), FakeStore()

        RecordingTranscriber(worker, store).submit(path)

        assert worker.speakers == ["1001", "1002"]
        assert {s.speaker for s in store.saved[0]["transcript"].segments} == {"1001", "1002"}

    def test_the_silence_floor_is_configurable(self, tmp_path):
        """
        A quiet talker's channel should not be silently dropped. Lowering the floor is the
        lever, and it is a real power threshold -- unlike Silero's, which is a probability.
        """
        quiet = speech(1.0, amplitude=150)  # RMS ~106, under the default floor of 200
        path = self._recording(tmp_path, [quiet], ["1001"])

        default = FakeWorker()
        RecordingTranscriber(default, FakeStore()).submit(path)
        assert default.speakers == [], "expected the default floor to skip this"

        sensitive = FakeWorker()
        RecordingTranscriber(sensitive, FakeStore(), silence_floor=50.0).submit(path)
        assert sensitive.speakers == ["1001"]

    def test_raising_the_floor_skips_more(self, tmp_path):
        path = self._recording(tmp_path, [speech(1.0, amplitude=6000)], ["1001"])
        worker = FakeWorker()

        RecordingTranscriber(worker, FakeStore(), silence_floor=20000.0).submit(path)

        assert worker.submitted == []


@pytest.mark.unit
class TestScratchCleanup:
    """
    A crash between creating a workspace and finishing with it leaks region files that are
    the same size as the recording. Nothing can be transcribing in a process that has just
    started, so anything still there at startup is orphaned.
    """

    def test_stale_workspaces_are_removed(self, tmp_path):
        scratch = tmp_path / SCRATCH_DIRNAME
        (scratch / "job-dead").mkdir(parents=True)
        (scratch / "job-dead" / "1001-0.wav").write_bytes(b"x" * 1024)

        removed = RecordingTranscriber(FakeWorker(), FakeStore()).clear_scratch(tmp_path)

        assert removed == 1
        assert list(scratch.iterdir()) == []

    def test_recordings_themselves_are_untouched(self, tmp_path):
        keep = tmp_path / "call.wav"
        keep.write_bytes(b"x")
        (tmp_path / SCRATCH_DIRNAME / "job-dead").mkdir(parents=True)

        RecordingTranscriber(FakeWorker(), FakeStore()).clear_scratch(tmp_path)

        assert keep.exists()

    def test_no_scratch_directory_is_fine(self, tmp_path):
        assert RecordingTranscriber(FakeWorker(), FakeStore()).clear_scratch(tmp_path) == 0

    def test_a_missing_recordings_directory_is_fine(self, tmp_path):
        assert RecordingTranscriber(FakeWorker(), FakeStore()).clear_scratch(tmp_path / "nope") == 0
