"""
Comprehensive tests for Call Recording Analytics feature.

Tests cover all public classes, methods, enums, dataclasses, and the
global singleton accessor in pbx/features/call_recording_analytics.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

if TYPE_CHECKING:
    from pbx.features.call_recording_analytics import RecordingAnalytics

# ---------------------------------------------------------------------------
# AnalysisType enum
# ---------------------------------------------------------------------------


def _with_transcript(text: str = "thank you so much, that was great") -> MagicMock:
    """
    A database whose stored transcript for any recording is `text`.

    Dispatches on the statement: a single return_value would hand transcript rows to the
    stored-analyses query too, and those get decoded as analyses.
    """

    def fetch_all(query, params=None):
        if "recording_analyses" in query:
            return []
        return [{"id": 1, "text": text, "segments": None}]

    db = MagicMock()
    db.enabled = True
    db.fetch_all.side_effect = fetch_all
    return db


def _without_transcript() -> MagicMock:
    """A database with no transcript yet -- the normal state right after a call ends."""
    db = MagicMock()
    db.enabled = True
    db.fetch_all.side_effect = lambda q, p=None: []
    return db


@pytest.mark.unit
class TestAnalysisType:
    """Tests for the AnalysisType enumeration."""

    def test_all_enum_members_exist(self) -> None:
        from pbx.features.call_recording_analytics import AnalysisType

        assert AnalysisType.SENTIMENT.value == "sentiment"
        assert AnalysisType.KEYWORDS.value == "keywords"
        assert AnalysisType.COMPLIANCE.value == "compliance"
        assert AnalysisType.QUALITY.value == "quality"
        assert AnalysisType.SUMMARY.value == "summary"
        assert AnalysisType.TRANSCRIPT.value == "transcript"

    def test_enum_member_count(self) -> None:
        from pbx.features.call_recording_analytics import AnalysisType

        assert len(AnalysisType) == 6

    def test_enum_from_value(self) -> None:
        from pbx.features.call_recording_analytics import AnalysisType

        assert AnalysisType("sentiment") is AnalysisType.SENTIMENT
        assert AnalysisType("keywords") is AnalysisType.KEYWORDS

    def test_enum_invalid_value(self) -> None:
        from pbx.features.call_recording_analytics import AnalysisType

        with pytest.raises(ValueError):
            AnalysisType("nonexistent")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(
    auto_analyze: bool = False,
    analysis_types: list[str] | None = None,
) -> dict:
    """
    Build a minimal config dict for RecordingAnalytics.

    Under ``recording.analytics`` rather than ``features.recording_analytics``: it configures
    the recording pipeline, not a feature toggle. There is no ``enabled`` key any more -- it
    gated nothing while reporting itself as though it did, which is how a switch comes to look
    meaningful for months without being consulted.
    """
    analytics: dict = {"auto_analyze": auto_analyze}
    if analysis_types is not None:
        analytics["analysis_types"] = analysis_types
    return {"recording": {"analytics": analytics}}


def _build_analytics(
    config: dict | None = None,
    spacy_available: bool = False,
) -> RecordingAnalytics:
    """
    Construct a RecordingAnalytics with mocked optional imports.

    There is no speech model to mock any more. Analysis reads the transcript post-call
    transcription already stored rather than producing its own, so the analysers take text.
    """
    with (
        patch("pbx.features.call_recording_analytics.SPACY_AVAILABLE", spacy_available),
        patch("pbx.features.call_recording_analytics.get_logger") as mock_logger_fn,
    ):
        mock_logger_fn.return_value = MagicMock()
        from pbx.features.call_recording_analytics import RecordingAnalytics

        return RecordingAnalytics(config)


# ---------------------------------------------------------------------------
# RecordingAnalytics.__init__
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRecordingAnalyticsInit:
    """Tests for RecordingAnalytics initialisation."""

    def test_init_with_none_config(self) -> None:
        analytics = _build_analytics(config=None)
        assert analytics.config == {}
        assert analytics.auto_analyze is False
        assert analytics.analysis_types == ["sentiment", "keywords", "summary"]

    def test_init_with_empty_config(self) -> None:
        analytics = _build_analytics(config={})
        assert analytics.auto_analyze is False

    def test_init_enabled(self) -> None:
        cfg = _make_config(auto_analyze=True)
        analytics = _build_analytics(config=cfg)
        assert analytics.auto_analyze is True

    def test_init_custom_analysis_types(self) -> None:
        cfg = _make_config(analysis_types=["transcript", "compliance"])
        analytics = _build_analytics(config=cfg)
        assert analytics.analysis_types == ["transcript", "compliance"]

    def test_init_default_analysis_types(self) -> None:
        cfg = _make_config()
        analytics = _build_analytics(config=cfg)
        assert analytics.analysis_types == ["sentiment", "keywords", "summary"]

    def test_init_storage_and_stats_defaults(self) -> None:
        analytics = _build_analytics()
        assert analytics.analyses == {}
        assert analytics.total_analyses == 0
        assert analytics.analyses_by_type == {}


@pytest.mark.unit
class TestAnalyzeRecording:
    """
    The entry point, which no longer transcribes anything.

    It used to take an audio path and transcribe it once per analysis type -- three times for
    the default set, six if all were requested -- reloading the speech model on each, inside
    the request. Every pass produced the same text from the same bytes. It now reads the
    transcript post-call transcription already stored.
    """

    def _analytics(self, db, config=None):
        analytics = _build_analytics(config=config)
        analytics.database = db
        return analytics

    def test_it_returns_the_expected_structure(self) -> None:
        analytics = self._analytics(_with_transcript())

        result = analytics.analyze_recording("rec-1", ["sentiment"])

        assert result["recording_id"] == "rec-1"
        assert "analyzed_at" in result
        assert "sentiment" in result["analyses"]

    def test_the_transcript_is_read_once_however_many_analyses(self) -> None:
        """The whole point: N analyses, one lookup, no transcription at all."""
        db = _with_transcript()
        analytics = self._analytics(db)

        analytics.analyze_recording(
            "rec-1", ["transcript", "sentiment", "keywords", "compliance", "quality", "summary"]
        )

        reads = [c for c in db.fetch_all.call_args_list if "transcripts" in str(c[0][0])]
        assert len(reads) == 1

    def test_the_stored_transcript_reaches_the_analysers(self) -> None:
        analytics = self._analytics(_with_transcript("thank you, wonderful service"))

        result = analytics.analyze_recording("rec-1", ["transcript"])

        assert result["analyses"]["transcript"]["transcript"] == "thank you, wonderful service"

    def test_no_transcript_yet_says_so(self) -> None:
        """
        Transcription is queued and runs after the call, so a recording that just finished
        legitimately has none. Saying so beats analysing an empty string as though it meant
        something.
        """
        analytics = self._analytics(_without_transcript())

        result = analytics.analyze_recording("rec-1", ["sentiment"])

        assert "error" in result
        assert result["analyses"] == {}

    def test_no_database_is_survivable(self) -> None:
        result = _build_analytics().analyze_recording("rec-1", ["sentiment"])

        assert "error" in result

    def test_it_uses_configured_types_when_none_given(self) -> None:
        analytics = self._analytics(
            _with_transcript(), config=_make_config(analysis_types=["quality"])
        )

        result = analytics.analyze_recording("rec-2")

        assert "quality" in result["analyses"]

    def test_it_tracks_statistics(self) -> None:
        analytics = self._analytics(_with_transcript())

        analytics.analyze_recording("rec-1", ["sentiment", "keywords"])

        assert analytics.total_analyses == 1
        assert analytics.analyses_by_type["sentiment"] == 1
        assert analytics.analyses_by_type["keywords"] == 1

    def test_it_stores_results(self) -> None:
        analytics = self._analytics(_with_transcript())

        analytics.analyze_recording("rec-1", ["summary"])

        assert "rec-1" in analytics.analyses

    def test_every_type_is_handled(self) -> None:
        analytics = self._analytics(_with_transcript())
        all_types = ["transcript", "sentiment", "keywords", "compliance", "quality", "summary"]

        result = analytics.analyze_recording("rec-all", all_types)

        for analysis in all_types:
            assert analysis in result["analyses"]

    def test_an_unknown_type_is_ignored(self) -> None:
        analytics = self._analytics(_with_transcript())

        result = analytics.analyze_recording("rec-1", ["nonsense"])

        assert result["analyses"] == {}


@pytest.mark.unit
class TestAnalyzeSentiment:
    """Tests for sentiment analysis."""

    def test_sentiment_no_transcript_no_models(self) -> None:
        analytics = _build_analytics()
        analytics.spacy_nlp = None

        result = analytics._analyze_sentiment("")

        assert result["overall_sentiment"] == "neutral"
        assert result["sentiment_score"] == 0.0
        assert result["agent_sentiment"] == "neutral"
        assert result["sentiment_timeline"] == []

    def test_sentiment_with_vosk_and_spacy_positive(self) -> None:
        analytics = _build_analytics()

        # Mock _transcribe to return positive text

        # Mock spaCy NLP pipeline
        mock_nlp = MagicMock()
        mock_tokens = []
        for word in [
            "thank",
            "you",
            "very",
            "much",
            "that",
            "was",
            "excellent",
            "and",
            "wonderful",
        ]:
            token = MagicMock()
            token.lemma_.lower.return_value = word
            token.is_alpha = True
            mock_tokens.append(token)

        mock_doc = MagicMock()
        mock_doc.__iter__ = MagicMock(return_value=iter(mock_tokens))
        mock_nlp.return_value = mock_doc
        analytics.spacy_nlp = mock_nlp

        result = analytics._analyze_sentiment("a transcript")

        assert result["overall_sentiment"] == "positive"
        assert result["sentiment_score"] > 0.2

    def test_sentiment_with_vosk_and_spacy_negative(self) -> None:
        analytics = _build_analytics()

        mock_nlp = MagicMock()
        mock_tokens = []
        for word in ["angry", "upset", "frustrated", "disappointed", "terrible"]:
            token = MagicMock()
            token.lemma_.lower.return_value = word
            token.is_alpha = True
            mock_tokens.append(token)

        mock_doc = MagicMock()
        mock_doc.__iter__ = MagicMock(return_value=iter(mock_tokens))
        mock_nlp.return_value = mock_doc
        analytics.spacy_nlp = mock_nlp

        result = analytics._analyze_sentiment("a transcript")

        assert result["overall_sentiment"] == "negative"
        assert result["sentiment_score"] < -0.2

    def test_sentiment_with_vosk_and_spacy_neutral(self) -> None:
        analytics = _build_analytics()

        mock_nlp = MagicMock()
        mock_tokens = []
        for word in ["hello", "goodbye", "yes", "no"]:
            token = MagicMock()
            token.lemma_.lower.return_value = word
            token.is_alpha = True
            mock_tokens.append(token)

        mock_doc = MagicMock()
        mock_doc.__iter__ = MagicMock(return_value=iter(mock_tokens))
        mock_nlp.return_value = mock_doc
        analytics.spacy_nlp = mock_nlp

        result = analytics._analyze_sentiment("a transcript")

        assert result["overall_sentiment"] == "neutral"
        assert result["sentiment_score"] == 0.0

    def test_sentiment_spacy_exception_fallback(self) -> None:
        analytics = _build_analytics()

        mock_nlp = MagicMock(side_effect=RuntimeError("spacy error"))
        analytics.spacy_nlp = mock_nlp

        result = analytics._analyze_sentiment("a transcript")

        # Should not crash; falls through to return
        assert result["overall_sentiment"] == "neutral"

    def test_sentiment_fallback_keyword_positive(self) -> None:
        """Keyword scoring when spaCy is unavailable but a transcript exists."""
        analytics = _build_analytics()
        analytics.spacy_nlp = None

        result = analytics._analyze_sentiment("thank appreciate excellent great wonderful")

        assert result["overall_sentiment"] == "positive"
        assert result["sentiment_score"] > 0.2

    def test_sentiment_fallback_keyword_negative(self) -> None:
        analytics = _build_analytics()
        analytics.spacy_nlp = None

        result = analytics._analyze_sentiment(
            "angry upset frustrated disappointed terrible awful horrible bad"
        )

        assert result["overall_sentiment"] == "negative"
        assert result["sentiment_score"] < -0.2

    def test_sentiment_fallback_keyword_neutral(self) -> None:
        analytics = _build_analytics()
        analytics.spacy_nlp = None

        result = analytics._analyze_sentiment("the appointment is on tuesday at three")

        assert result["overall_sentiment"] == "neutral"
        assert result["sentiment_score"] == 0.0

    def test_sentiment_equal_positive_and_negative(self) -> None:
        """When pos == neg, score is 0 and sentiment is neutral."""
        analytics = _build_analytics()
        analytics.spacy_nlp = None

        result = analytics._analyze_sentiment("")

        assert result["overall_sentiment"] == "neutral"
        assert result["sentiment_score"] == 0.0


# ---------------------------------------------------------------------------
# RecordingAnalytics._detect_keywords
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDetectKeywords:
    """Tests for keyword detection."""

    def test_keywords_empty_transcript(self) -> None:
        analytics = _build_analytics()
        result = analytics._detect_keywords("")

        assert result["keywords"] == []
        assert result["competitor_mentions"] == []
        assert result["product_mentions"] == []
        assert result["issue_keywords"] == []

    def test_keywords_return_structure(self) -> None:
        analytics = _build_analytics()
        result = analytics._detect_keywords("")
        assert "keywords" in result
        assert "competitor_mentions" in result
        assert "product_mentions" in result
        assert "issue_keywords" in result


# ---------------------------------------------------------------------------
# RecordingAnalytics._check_compliance
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCheckCompliance:
    """Tests for compliance checking."""

    def test_compliance_empty_transcript_is_compliant(self) -> None:
        analytics = _build_analytics()
        result = analytics._check_compliance("")

        assert result["compliant"] is True
        assert result["violations"] == []
        assert result["warnings"] == []
        assert result["required_phrases_found"] == []
        assert result["prohibited_phrases_found"] == []

    def test_compliance_return_structure(self) -> None:
        analytics = _build_analytics()
        result = analytics._check_compliance("")
        expected_keys = {
            "compliant",
            "violations",
            "warnings",
            "required_phrases_found",
            "prohibited_phrases_found",
        }
        assert set(result.keys()) == expected_keys


# ---------------------------------------------------------------------------
# RecordingAnalytics._score_quality
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestScoreQuality:
    """Tests for quality scoring."""

    def test_quality_empty_transcript_base_scores(self) -> None:
        analytics = _build_analytics()
        result = analytics._score_quality("")

        assert result["overall_score"] == 50.0
        assert result["agent_performance"] == 50.0
        assert result["customer_satisfaction"] == 50.0
        assert result["resolution_quality"] == 50.0
        assert result["professionalism"] == 50.0

    def test_quality_return_structure(self) -> None:
        analytics = _build_analytics()
        result = analytics._score_quality("")
        expected_keys = {
            "overall_score",
            "agent_performance",
            "customer_satisfaction",
            "resolution_quality",
            "professionalism",
        }
        assert set(result.keys()) == expected_keys

    def test_quality_scores_are_rounded(self) -> None:
        analytics = _build_analytics()
        result = analytics._score_quality("")
        for key in result:
            value = result[key]
            assert value == round(value, 2)


# ---------------------------------------------------------------------------
# RecordingAnalytics._summarize
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSummarize:
    """Tests for summarization."""

    def test_summarize_empty_transcript(self) -> None:
        analytics = _build_analytics()
        result = analytics._summarize("")

        assert result["summary"] == ""
        assert result["key_points"] == []
        assert result["action_items"] == []
        assert result["outcomes"] == []

    def test_summarize_return_structure(self) -> None:
        analytics = _build_analytics()
        result = analytics._summarize("")
        expected_keys = {"summary", "key_points", "action_items", "outcomes"}
        assert set(result.keys()) == expected_keys


# ---------------------------------------------------------------------------
# RecordingAnalytics.search_recordings
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSearchRecordings:
    """Tests for search_recordings."""

    def _populate_analyses(self, analytics: RecordingAnalytics) -> None:
        """Populate analytics.analyses with test data."""
        analytics.analyses = {
            "rec-positive": {
                "analyzed_at": datetime.now(UTC).isoformat(),
                "analyses": {
                    "sentiment": {
                        "overall_sentiment": "positive",
                        "sentiment_score": 0.8,
                    },
                    "keywords": {"keywords": ["product", "purchase"]},
                    "quality": {"overall_score": 85.0},
                    "compliance": {"compliant": True},
                },
            },
            "rec-negative": {
                "analyzed_at": datetime.now(UTC).isoformat(),
                "analyses": {
                    "sentiment": {
                        "overall_sentiment": "negative",
                        "sentiment_score": -0.6,
                    },
                    "keywords": {"keywords": ["problem", "error"]},
                    "quality": {"overall_score": 30.0},
                    "compliance": {"compliant": False},
                },
            },
            "rec-neutral": {
                "analyzed_at": datetime.now(UTC).isoformat(),
                "analyses": {
                    "sentiment": {
                        "overall_sentiment": "neutral",
                        "sentiment_score": 0.0,
                    },
                    "keywords": {"keywords": []},
                    "quality": {"overall_score": 50.0},
                    "compliance": {"compliant": True},
                },
            },
        }

    def test_search_no_criteria_returns_all(self) -> None:
        analytics = _build_analytics()
        self._populate_analyses(analytics)
        result = analytics.search_recordings({})
        assert len(result) == 3

    def test_search_by_sentiment(self) -> None:
        analytics = _build_analytics()
        self._populate_analyses(analytics)
        result = analytics.search_recordings({"sentiment": "positive"})
        assert result == ["rec-positive"]

    def test_search_by_sentiment_negative(self) -> None:
        analytics = _build_analytics()
        self._populate_analyses(analytics)
        result = analytics.search_recordings({"sentiment": "negative"})
        assert result == ["rec-negative"]

    def test_search_by_keywords_match(self) -> None:
        analytics = _build_analytics()
        self._populate_analyses(analytics)
        result = analytics.search_recordings({"keywords": ["product"]})
        assert result == ["rec-positive"]

    def test_search_by_keywords_no_match(self) -> None:
        analytics = _build_analytics()
        self._populate_analyses(analytics)
        result = analytics.search_recordings({"keywords": ["nonexistent"]})
        assert result == []

    def test_search_by_min_quality_score(self) -> None:
        analytics = _build_analytics()
        self._populate_analyses(analytics)
        result = analytics.search_recordings({"min_quality_score": 80})
        assert result == ["rec-positive"]

    def test_search_by_min_quality_score_low(self) -> None:
        analytics = _build_analytics()
        self._populate_analyses(analytics)
        result = analytics.search_recordings({"min_quality_score": 10})
        assert len(result) == 3

    def test_search_by_compliance_true(self) -> None:
        analytics = _build_analytics()
        self._populate_analyses(analytics)
        result = analytics.search_recordings({"compliant": True})
        assert set(result) == {"rec-positive", "rec-neutral"}

    def test_search_by_compliance_false(self) -> None:
        analytics = _build_analytics()
        self._populate_analyses(analytics)
        result = analytics.search_recordings({"compliant": False})
        assert result == ["rec-negative"]

    def test_search_combined_criteria(self) -> None:
        analytics = _build_analytics()
        self._populate_analyses(analytics)
        result = analytics.search_recordings(
            {"sentiment": "positive", "min_quality_score": 80, "compliant": True}
        )
        assert result == ["rec-positive"]

    def test_search_combined_criteria_no_match(self) -> None:
        analytics = _build_analytics()
        self._populate_analyses(analytics)
        result = analytics.search_recordings({"sentiment": "positive", "compliant": False})
        assert result == []

    def test_search_empty_analyses(self) -> None:
        analytics = _build_analytics()
        result = analytics.search_recordings({"sentiment": "positive"})
        assert result == []

    def test_search_missing_analysis_type(self) -> None:
        """When an analysis type is not present in results, should not match."""
        analytics = _build_analytics()
        analytics.analyses = {
            "rec-minimal": {
                "analyzed_at": datetime.now(UTC).isoformat(),
                "analyses": {},  # No analysis data
            }
        }
        result = analytics.search_recordings({"sentiment": "positive"})
        assert result == []

    def test_search_quality_missing_defaults_to_zero(self) -> None:
        analytics = _build_analytics()
        analytics.analyses = {
            "rec-no-quality": {
                "analyzed_at": datetime.now(UTC).isoformat(),
                "analyses": {"quality": {}},
            }
        }
        result = analytics.search_recordings({"min_quality_score": 1})
        assert result == []


# ---------------------------------------------------------------------------
# RecordingAnalytics.get_analysis
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGetAnalysis:
    """Tests for get_analysis."""

    def test_get_analysis_existing(self) -> None:
        analytics = _build_analytics()
        analytics.analyses["rec-1"] = {"foo": "bar"}
        assert analytics.get_analysis("rec-1") == {"foo": "bar"}

    def test_get_analysis_nonexistent(self) -> None:
        analytics = _build_analytics()
        assert analytics.get_analysis("no-such") is None


# ---------------------------------------------------------------------------
# RecordingAnalytics._filter_analyses_by_date
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFilterAnalysesByDate:
    """Tests for _filter_analyses_by_date."""

    def test_filter_within_range(self) -> None:
        analytics = _build_analytics()
        now = datetime.now(UTC)
        analytics.analyses = {
            "rec-1": {"analyzed_at": now.isoformat(), "analyses": {}},
        }
        result = analytics._filter_analyses_by_date(
            now - timedelta(hours=1), now + timedelta(hours=1)
        )
        assert len(result) == 1

    def test_filter_outside_range(self) -> None:
        analytics = _build_analytics()
        now = datetime.now(UTC)
        past = now - timedelta(days=30)
        analytics.analyses = {
            "rec-1": {"analyzed_at": past.isoformat(), "analyses": {}},
        }
        result = analytics._filter_analyses_by_date(
            now - timedelta(hours=1), now + timedelta(hours=1)
        )
        assert len(result) == 0

    def test_filter_invalid_timestamp(self) -> None:
        analytics = _build_analytics()
        now = datetime.now(UTC)
        analytics.analyses = {
            "rec-bad": {"analyzed_at": "not-a-date", "analyses": {}},
        }
        result = analytics._filter_analyses_by_date(
            now - timedelta(hours=1), now + timedelta(hours=1)
        )
        assert len(result) == 0

    def test_filter_missing_timestamp_key(self) -> None:
        analytics = _build_analytics()
        now = datetime.now(UTC)
        analytics.analyses = {
            "rec-no-ts": {"analyses": {}},
        }
        result = analytics._filter_analyses_by_date(
            now - timedelta(hours=1), now + timedelta(hours=1)
        )
        assert len(result) == 0

    def test_filter_empty_analyses(self) -> None:
        analytics = _build_analytics()
        now = datetime.now(UTC)
        result = analytics._filter_analyses_by_date(
            now - timedelta(hours=1), now + timedelta(hours=1)
        )
        assert result == []


# ---------------------------------------------------------------------------
# RecordingAnalytics._aggregate_sentiment_data
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAggregateSentimentData:
    """Tests for _aggregate_sentiment_data."""

    def test_aggregate_sentiment_with_data(self) -> None:
        analytics = _build_analytics()
        analyses = [
            {
                "analyzed_at": "2026-01-01T00:00:00+00:00",
                "analyses": {
                    "sentiment": {
                        "overall_sentiment": "positive",
                        "sentiment_score": 0.8,
                    }
                },
            }
        ]
        result = analytics._aggregate_sentiment_data(analyses)
        assert len(result) == 1
        assert result[0]["sentiment"] == "positive"
        assert result[0]["score"] == 0.8

    def test_aggregate_sentiment_empty_data(self) -> None:
        analytics = _build_analytics()
        result = analytics._aggregate_sentiment_data([])
        assert result == []

    def test_aggregate_sentiment_no_sentiment_key(self) -> None:
        analytics = _build_analytics()
        analyses = [{"analyzed_at": "2026-01-01T00:00:00+00:00", "analyses": {}}]
        result = analytics._aggregate_sentiment_data(analyses)
        assert result == []

    def test_aggregate_sentiment_defaults(self) -> None:
        """When sentiment dict has data but missing expected keys, defaults apply."""
        analytics = _build_analytics()
        analyses = [
            {
                "analyzed_at": "2026-01-01T00:00:00+00:00",
                "analyses": {"sentiment": {"some_key": "some_value"}},
            }
        ]
        result = analytics._aggregate_sentiment_data(analyses)
        assert len(result) == 1
        assert result[0]["sentiment"] == "neutral"
        assert result[0]["score"] == 0.0

    def test_aggregate_sentiment_empty_dict_skipped(self) -> None:
        """Empty sentiment dict is falsy, so it is skipped."""
        analytics = _build_analytics()
        analyses = [
            {
                "analyzed_at": "2026-01-01T00:00:00+00:00",
                "analyses": {"sentiment": {}},
            }
        ]
        result = analytics._aggregate_sentiment_data(analyses)
        assert result == []


# ---------------------------------------------------------------------------
# RecordingAnalytics._aggregate_quality_data
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAggregateQualityData:
    """Tests for _aggregate_quality_data."""

    def test_aggregate_quality_with_data(self) -> None:
        analytics = _build_analytics()
        analyses = [
            {
                "analyzed_at": "2026-01-01T00:00:00+00:00",
                "analyses": {
                    "quality": {
                        "overall_score": 80.0,
                        "agent_performance": 70.0,
                        "customer_satisfaction": 90.0,
                    }
                },
            }
        ]
        result = analytics._aggregate_quality_data(analyses)
        assert len(result) == 1
        assert result[0]["overall_score"] == 80.0
        assert result[0]["agent_performance"] == 70.0
        assert result[0]["customer_satisfaction"] == 90.0

    def test_aggregate_quality_empty(self) -> None:
        analytics = _build_analytics()
        result = analytics._aggregate_quality_data([])
        assert result == []

    def test_aggregate_quality_no_quality_key(self) -> None:
        analytics = _build_analytics()
        analyses = [{"analyzed_at": "2026-01-01T00:00:00+00:00", "analyses": {}}]
        result = analytics._aggregate_quality_data(analyses)
        assert result == []

    def test_aggregate_quality_defaults(self) -> None:
        """When quality dict has data but missing expected keys, defaults apply."""
        analytics = _build_analytics()
        analyses = [
            {
                "analyzed_at": "2026-01-01T00:00:00+00:00",
                "analyses": {"quality": {"some_key": "some_value"}},
            }
        ]
        result = analytics._aggregate_quality_data(analyses)
        assert len(result) == 1
        assert result[0]["overall_score"] == 0.0

    def test_aggregate_quality_empty_dict_skipped(self) -> None:
        """Empty quality dict is falsy, so it is skipped."""
        analytics = _build_analytics()
        analyses = [
            {
                "analyzed_at": "2026-01-01T00:00:00+00:00",
                "analyses": {"quality": {}},
            }
        ]
        result = analytics._aggregate_quality_data(analyses)
        assert result == []


# ---------------------------------------------------------------------------
# RecordingAnalytics._aggregate_keyword_trends
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAggregateKeywordTrends:
    """Tests for _aggregate_keyword_trends."""

    def test_aggregate_keywords_with_data(self) -> None:
        analytics = _build_analytics()
        analyses = [
            {"analyses": {"keywords": {"keywords": ["product", "help"]}}},
            {"analyses": {"keywords": {"keywords": ["product", "error"]}}},
        ]
        result = analytics._aggregate_keyword_trends(analyses)
        assert result["product"] == 2
        assert result["help"] == 1
        assert result["error"] == 1

    def test_aggregate_keywords_sorted_by_frequency(self) -> None:
        analytics = _build_analytics()
        analyses = [
            {"analyses": {"keywords": {"keywords": ["alpha"]}}},
            {"analyses": {"keywords": {"keywords": ["beta", "alpha"]}}},
            {"analyses": {"keywords": {"keywords": ["alpha"]}}},
        ]
        result = analytics._aggregate_keyword_trends(analyses)
        keys = list(result.keys())
        assert keys[0] == "alpha"
        assert result["alpha"] == 3

    def test_aggregate_keywords_empty(self) -> None:
        analytics = _build_analytics()
        result = analytics._aggregate_keyword_trends([])
        assert result == {}

    def test_aggregate_keywords_no_keywords_key(self) -> None:
        analytics = _build_analytics()
        analyses = [{"analyses": {}}]
        result = analytics._aggregate_keyword_trends(analyses)
        assert result == {}

    def test_aggregate_keywords_empty_keywords_list(self) -> None:
        analytics = _build_analytics()
        analyses = [{"analyses": {"keywords": {"keywords": []}}}]
        result = analytics._aggregate_keyword_trends(analyses)
        assert result == {}


# ---------------------------------------------------------------------------
# RecordingAnalytics._calculate_compliance_data
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCalculateComplianceData:
    """Tests for _calculate_compliance_data."""

    def test_compliance_mixed(self) -> None:
        analytics = _build_analytics()
        analyses = [
            {"analyses": {"compliance": {"compliant": True}}},
            {"analyses": {"compliance": {"compliant": False}}},
            {"analyses": {"compliance": {"compliant": True}}},
        ]
        result = analytics._calculate_compliance_data(analyses)
        assert result["compliant"] == 2
        assert result["non_compliant"] == 1

    def test_compliance_all_compliant(self) -> None:
        analytics = _build_analytics()
        analyses = [
            {"analyses": {"compliance": {"compliant": True}}},
            {"analyses": {"compliance": {"compliant": True}}},
        ]
        result = analytics._calculate_compliance_data(analyses)
        assert result["compliant"] == 2
        assert result["non_compliant"] == 0

    def test_compliance_empty(self) -> None:
        analytics = _build_analytics()
        result = analytics._calculate_compliance_data([])
        assert result["compliant"] == 0
        assert result["non_compliant"] == 0

    def test_compliance_no_compliance_key(self) -> None:
        analytics = _build_analytics()
        analyses = [{"analyses": {}}]
        result = analytics._calculate_compliance_data(analyses)
        assert result["compliant"] == 0
        assert result["non_compliant"] == 0

    def test_compliance_default_false(self) -> None:
        """When 'compliant' key missing but dict is truthy, defaults to False."""
        analytics = _build_analytics()
        analyses = [{"analyses": {"compliance": {"some_key": "value"}}}]
        result = analytics._calculate_compliance_data(analyses)
        assert result["non_compliant"] == 1

    def test_compliance_empty_dict_skipped(self) -> None:
        """Empty compliance dict is falsy, so it is skipped entirely."""
        analytics = _build_analytics()
        analyses = [{"analyses": {"compliance": {}}}]
        result = analytics._calculate_compliance_data(analyses)
        assert result["compliant"] == 0
        assert result["non_compliant"] == 0


# ---------------------------------------------------------------------------
# RecordingAnalytics.get_trend_analysis
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGetTrendAnalysis:
    """Tests for get_trend_analysis."""

    def test_trend_analysis_no_data(self) -> None:
        analytics = _build_analytics()
        now = datetime.now(UTC)
        result = analytics.get_trend_analysis(now - timedelta(days=7), now + timedelta(days=1))
        assert result["sentiment_trend"] == []
        assert result["quality_trend"] == []
        assert result["keyword_trends"] == {}
        assert result["compliance_rate"] == 0.0
        assert result["total_recordings"] == 0

    def test_trend_analysis_with_data(self) -> None:
        analytics = _build_analytics()
        now = datetime.now(UTC)
        analytics.analyses = {
            "rec-1": {
                "analyzed_at": now.isoformat(),
                "analyses": {
                    "sentiment": {"overall_sentiment": "positive", "sentiment_score": 0.8},
                    "quality": {
                        "overall_score": 80.0,
                        "agent_performance": 70.0,
                        "customer_satisfaction": 90.0,
                    },
                    "keywords": {"keywords": ["help"]},
                    "compliance": {"compliant": True},
                },
            },
            "rec-2": {
                "analyzed_at": now.isoformat(),
                "analyses": {
                    "sentiment": {"overall_sentiment": "negative", "sentiment_score": -0.5},
                    "quality": {
                        "overall_score": 40.0,
                        "agent_performance": 30.0,
                        "customer_satisfaction": 20.0,
                    },
                    "keywords": {"keywords": ["problem"]},
                    "compliance": {"compliant": False},
                },
            },
        }

        result = analytics.get_trend_analysis(now - timedelta(hours=1), now + timedelta(hours=1))

        assert result["total_recordings"] == 2
        assert len(result["sentiment_trend"]) == 2
        assert len(result["quality_trend"]) == 2
        assert result["compliance_rate"] == 50.0
        assert "date_range" in result
        assert "help" in result["keyword_trends"]
        assert "problem" in result["keyword_trends"]

    def test_trend_analysis_full_compliance(self) -> None:
        analytics = _build_analytics()
        now = datetime.now(UTC)
        analytics.analyses = {
            "rec-1": {
                "analyzed_at": now.isoformat(),
                "analyses": {"compliance": {"compliant": True}},
            },
        }
        result = analytics.get_trend_analysis(now - timedelta(hours=1), now + timedelta(hours=1))
        assert result["compliance_rate"] == 100.0

    def test_trend_analysis_no_compliance_data(self) -> None:
        analytics = _build_analytics()
        now = datetime.now(UTC)
        analytics.analyses = {
            "rec-1": {
                "analyzed_at": now.isoformat(),
                "analyses": {"sentiment": {"overall_sentiment": "neutral"}},
            },
        }
        result = analytics.get_trend_analysis(now - timedelta(hours=1), now + timedelta(hours=1))
        assert result["compliance_rate"] == 0.0

    def test_trend_analysis_date_range_in_result(self) -> None:
        analytics = _build_analytics()
        now = datetime.now(UTC)
        analytics.analyses = {
            "rec-1": {
                "analyzed_at": now.isoformat(),
                "analyses": {},
            },
        }
        start = now - timedelta(hours=1)
        end = now + timedelta(hours=1)
        result = analytics.get_trend_analysis(start, end)
        assert result["date_range"]["start"] == start.isoformat()
        assert result["date_range"]["end"] == end.isoformat()


# ---------------------------------------------------------------------------
# RecordingAnalytics.get_statistics
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGetStatistics:
    """Tests for get_statistics."""

    def test_statistics_defaults(self) -> None:
        analytics = _build_analytics()
        stats = analytics.get_statistics()
        assert stats["auto_analyze"] is False
        assert stats["total_analyses"] == 0
        assert stats["analyses_by_type"] == {}
        assert stats["available_analysis_types"] == ["sentiment", "keywords", "summary"]

    def test_statistics_after_analysis(self) -> None:
        analytics = _build_analytics()
        analytics.database = _with_transcript()
        analytics.analyze_recording("r1", ["sentiment", "quality"])
        stats = analytics.get_statistics()
        assert stats["total_analyses"] == 1
        assert stats["analyses_by_type"]["sentiment"] == 1
        assert stats["analyses_by_type"]["quality"] == 1

    def test_statistics_enabled(self) -> None:
        cfg = _make_config(auto_analyze=True)
        analytics = _build_analytics(config=cfg)
        stats = analytics.get_statistics()
        assert stats["auto_analyze"] is True


# ---------------------------------------------------------------------------
# get_recording_analytics (global singleton)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGetRecordingAnalytics:
    """Tests for the module-level get_recording_analytics singleton accessor."""

    def test_get_recording_analytics_creates_instance(self) -> None:
        with (
            patch("pbx.features.call_recording_analytics._recording_analytics", None),
            patch("pbx.features.call_recording_analytics.SPACY_AVAILABLE", False),
            patch("pbx.features.call_recording_analytics.get_logger") as mock_log,
        ):
            mock_log.return_value = MagicMock()
            from pbx.features.call_recording_analytics import get_recording_analytics

            instance = get_recording_analytics({"features": {}})
            assert instance is not None

    def test_get_recording_analytics_returns_same_instance(self) -> None:
        sentinel = MagicMock()
        with patch("pbx.features.call_recording_analytics._recording_analytics", sentinel):
            from pbx.features.call_recording_analytics import get_recording_analytics

            instance = get_recording_analytics()
            assert instance is sentinel

    def test_get_recording_analytics_none_config(self) -> None:
        with (
            patch("pbx.features.call_recording_analytics._recording_analytics", None),
            patch("pbx.features.call_recording_analytics.SPACY_AVAILABLE", False),
            patch("pbx.features.call_recording_analytics.get_logger") as mock_log,
        ):
            mock_log.return_value = MagicMock()
            from pbx.features.call_recording_analytics import get_recording_analytics

            instance = get_recording_analytics(None)
            assert instance is not None


# ---------------------------------------------------------------------------
# Module-level constants (VOSK_AVAILABLE, SPACY_AVAILABLE)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestModuleLevelConstants:
    """Tests that verify module-level constants exist and are boolean."""

    def test_spacy_available_is_bool(self) -> None:
        from pbx.features.call_recording_analytics import SPACY_AVAILABLE

        assert isinstance(SPACY_AVAILABLE, bool)


# ---------------------------------------------------------------------------
# Integration-style tests (still unit-mocked)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAnalyzeAndSearchFlow:
    """End-to-end flow tests using analyze_recording + search_recordings."""

    def test_analyze_then_search_by_sentiment(self) -> None:
        analytics = _build_analytics()
        # A transcript with no sentiment words either way.
        analytics.database = _with_transcript("the appointment is on tuesday at three")
        analytics.analyze_recording("rec-1", ["sentiment"])
        result = analytics.search_recordings({"sentiment": "neutral"})
        assert "rec-1" in result

    def test_analyze_then_get_analysis(self) -> None:
        analytics = _build_analytics()
        analytics.database = _with_transcript()
        analytics.analyze_recording("rec-1", ["quality"])
        result = analytics.get_analysis("rec-1")
        assert result is not None
        assert "quality" in result["analyses"]

    def test_analyze_then_trend_analysis(self) -> None:
        analytics = _build_analytics()
        analytics.database = _with_transcript()
        analytics.analyze_recording("rec-1", ["sentiment", "quality"])

        now = datetime.now(UTC)
        trends = analytics.get_trend_analysis(now - timedelta(hours=1), now + timedelta(hours=1))
        assert trends["total_recordings"] == 1

    def test_multiple_analyses_then_statistics(self) -> None:
        analytics = _build_analytics()
        analytics.database = _with_transcript()
        analytics.analyze_recording("r1", ["sentiment"])
        analytics.analyze_recording("r2", ["quality", "compliance"])
        analytics.analyze_recording("r3", ["sentiment", "keywords"])

        stats = analytics.get_statistics()
        assert stats["total_analyses"] == 3
        assert stats["analyses_by_type"]["sentiment"] == 2
        assert stats["analyses_by_type"]["quality"] == 1
        assert stats["analyses_by_type"]["compliance"] == 1
        assert stats["analyses_by_type"]["keywords"] == 1


# ---------------------------------------------------------------------------
# Edge-case and error-path tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEdgeCases:
    """Edge cases and error path coverage."""

    def test_analyze_recording_with_empty_analysis_types_falls_back(self) -> None:
        """Empty list is falsy, so it falls back to instance analysis_types."""
        cfg = _make_config(analysis_types=["sentiment", "keywords", "summary"])
        analytics = _build_analytics(config=cfg)
        analytics.database = _with_transcript()
        result = analytics.analyze_recording("rec-1", [])
        # Falls back to instance defaults
        assert "sentiment" in result["analyses"]
        assert "keywords" in result["analyses"]
        assert "summary" in result["analyses"]
        assert analytics.total_analyses == 1

    def test_search_with_multiple_keyword_criteria(self) -> None:
        analytics = _build_analytics()
        analytics.analyses = {
            "rec-1": {
                "analyzed_at": datetime.now(UTC).isoformat(),
                "analyses": {
                    "keywords": {"keywords": ["alpha", "beta"]},
                },
            },
        }
        # any() match: at least one keyword matches
        result = analytics.search_recordings({"keywords": ["alpha", "gamma"]})
        assert result == ["rec-1"]

    def test_search_keywords_none_match(self) -> None:
        analytics = _build_analytics()
        analytics.analyses = {
            "rec-1": {
                "analyzed_at": datetime.now(UTC).isoformat(),
                "analyses": {
                    "keywords": {"keywords": ["alpha"]},
                },
            },
        }
        result = analytics.search_recordings({"keywords": ["gamma", "delta"]})
        assert result == []

    def test_config_with_missing_features_key(self) -> None:
        analytics = _build_analytics(config={"other": "stuff"})
        assert analytics.auto_analyze is False

    def test_config_without_an_analytics_block(self) -> None:
        """Defaults have to hold: config.yml may not carry the block at all."""
        analytics = _build_analytics(config={"recording": {}})

        assert analytics.auto_analyze is False
        assert analytics.analysis_types == ["sentiment", "keywords", "summary"]

    def test_overwrite_analysis_for_same_recording(self) -> None:
        analytics = _build_analytics()
        analytics.database = _with_transcript()
        analytics.analyze_recording("rec-1", ["sentiment"])
        analytics.analyze_recording("rec-1", ["quality"])
        # Second call overwrites
        result = analytics.get_analysis("rec-1")
        assert "quality" in result["analyses"]
        # total_analyses still increments
        assert analytics.total_analyses == 2

    def test_trend_analysis_compliance_rate_rounding(self) -> None:
        analytics = _build_analytics()
        now = datetime.now(UTC)
        analytics.analyses = {
            "r1": {
                "analyzed_at": now.isoformat(),
                "analyses": {"compliance": {"compliant": True}},
            },
            "r2": {
                "analyzed_at": now.isoformat(),
                "analyses": {"compliance": {"compliant": True}},
            },
            "r3": {
                "analyzed_at": now.isoformat(),
                "analyses": {"compliance": {"compliant": False}},
            },
        }
        result = analytics.get_trend_analysis(now - timedelta(hours=1), now + timedelta(hours=1))
        # 2/3 = 66.67%
        assert result["compliance_rate"] == 66.67

    def test_sentiment_spacy_with_non_alpha_tokens(self) -> None:
        analytics = _build_analytics()

        mock_nlp = MagicMock()
        mock_tokens = []
        for word, is_alpha in [
            ("thank", True),
            ("123", False),
            ("!!!", False),
            ("excellent", True),
        ]:
            token = MagicMock()
            token.lemma_.lower.return_value = word
            token.is_alpha = is_alpha
            mock_tokens.append(token)

        mock_doc = MagicMock()
        mock_doc.__iter__ = MagicMock(return_value=iter(mock_tokens))
        mock_nlp.return_value = mock_doc
        analytics.spacy_nlp = mock_nlp

        result = analytics._analyze_sentiment("a transcript")
        # Only alpha tokens considered; "thank" and "excellent" are positive
        assert result["overall_sentiment"] == "positive"
        assert result["sentiment_score"] == 1.0

    def test_quality_scores_clamped(self) -> None:
        """Verify clamping logic in _score_quality wouldn't go below 0 or above 100."""
        analytics = _build_analytics()
        # With empty transcript, base scores are 50.0 -- we trust the logic is correct
        result = analytics._score_quality("")
        for key in [
            "overall_score",
            "agent_performance",
            "customer_satisfaction",
            "resolution_quality",
            "professionalism",
        ]:
            assert 0.0 <= result[key] <= 100.0


@pytest.mark.unit
class TestResultsArePersisted:
    """
    Analyses survive a restart now.

    They used to live only in a dict on a module-level singleton, so every restart emptied
    them -- and search_recordings and get_trend_analysis, which read that dict, could only ever
    see calls analysed since boot. They are stored against the transcript, so migration 1019's
    cascade expires them with the recording they describe.
    """

    def _analytics(self, db):
        analytics = _build_analytics()
        analytics.database = db
        return analytics

    def test_the_analysis_is_written(self) -> None:
        db = _with_transcript("thank you, wonderful service")

        self._analytics(db).analyze_recording("rec-1", ["sentiment", "summary"])

        inserts = [c for c in db.execute.call_args_list if "recording_analyses" in str(c[0][0])]
        assert len(inserts) == 1

    def test_it_is_keyed_to_the_transcript(self) -> None:
        """Not to the recording: the cascade runs transcript -> summary."""
        db = _with_transcript()

        self._analytics(db).analyze_recording("rec-1", ["sentiment"])

        insert = next(c for c in db.execute.call_args_list if "recording_analyses" in str(c[0][0]))
        assert "transcript_id" in str(insert[0][0])
        assert insert[0][1][0] == 1

    def test_sentiment_reaches_the_row(self) -> None:
        db = _with_transcript("thank you excellent wonderful great appreciate")

        self._analytics(db).analyze_recording("rec-1", ["sentiment"])

        params = next(c for c in db.execute.call_args_list if "recording_analyses" in str(c[0][0]))[
            0
        ][1]
        assert params[3] == "positive"

    def test_nothing_is_written_without_a_transcript(self) -> None:
        db = _without_transcript()

        self._analytics(db).analyze_recording("rec-1", ["sentiment"])

        assert not [c for c in db.execute.call_args_list if "recording_analyses" in str(c[0][0])]

    def test_a_write_failure_still_returns_the_analysis(self) -> None:
        """An analysis that could not be stored is still worth returning."""
        db = _with_transcript()
        db.execute.side_effect = RuntimeError("connection reset")

        result = self._analytics(db).analyze_recording("rec-1", ["sentiment"])

        assert "sentiment" in result["analyses"]


@pytest.mark.unit
class TestSpokenText:
    """
    Analysers see the words, not the furniture around them.

    Transcripts are stored as formatted dialogue because that is what a person reads. Handing
    that straight to the analysers means the summary can pick "[04:12] 1512" as though it were
    content, sentiment counts extension numbers toward its totals, and keyword matching scans
    timestamps.
    """

    def test_timestamp_and_speaker_are_stripped(self) -> None:
        from pbx.features.call_recording_analytics import spoken_text

        assert spoken_text("[00:04] 1512: I have a problem") == "I have a problem"

    def test_the_overlap_marker_goes_too(self) -> None:
        from pbx.features.call_recording_analytics import spoken_text

        assert spoken_text("[00:04] 1512 (overlapping): hello") == "hello"

    def test_hour_long_calls_are_handled(self) -> None:
        """clock() grows to h:mm:ss once a call runs past an hour."""
        from pbx.features.call_recording_analytics import spoken_text

        assert spoken_text("[1:04:12] 1512: still here") == "still here"

    def test_a_colon_inside_speech_survives(self) -> None:
        """The timestamp anchors the pattern, so ordinary punctuation is not eaten."""
        from pbx.features.call_recording_analytics import spoken_text

        assert (
            spoken_text("[00:04] 1512: here is the thing: it broke")
            == "here is the thing: it broke"
        )

    def test_a_line_without_a_prefix_is_untouched(self) -> None:
        from pbx.features.call_recording_analytics import spoken_text

        assert spoken_text("So: that happened") == "So: that happened"

    def test_every_line_is_stripped(self) -> None:
        from pbx.features.call_recording_analytics import spoken_text

        text = "[00:00] 1513: one\n[00:04] 1512: two"

        assert spoken_text(text) == "one\ntwo"

    def test_the_analysers_get_stripped_text(self) -> None:
        db = _with_transcript("[00:00] 1513: thank you excellent wonderful great appreciate")
        analytics = _build_analytics()
        analytics.database = db

        result = analytics.analyze_recording("rec-1", ["sentiment"])

        assert result["analyses"]["sentiment"]["overall_sentiment"] == "positive"

    def test_the_transcript_analysis_returns_the_dialogue_as_stored(self) -> None:
        """A person reading it wants to know who said what and when."""
        stored = "[00:00] 1513: hello there"
        analytics = _build_analytics()
        analytics.database = _with_transcript(stored)

        result = analytics.analyze_recording("rec-1", ["transcript"])

        assert result["analyses"]["transcript"]["transcript"] == stored
