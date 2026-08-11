"""
Call Recording Analytics
AI analysis of recorded calls using FREE open-source libraries
"""

import json
import re
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pbx.utils.logger import get_logger

# Import spaCy for NLP and sentiment analysis
try:
    import spacy

    SPACY_AVAILABLE = True
except ImportError:
    SPACY_AVAILABLE = False


class AnalysisType(Enum):
    """Analysis type enumeration"""

    SENTIMENT = "sentiment"
    KEYWORDS = "keywords"
    COMPLIANCE = "compliance"
    QUALITY = "quality"
    SUMMARY = "summary"
    TRANSCRIPT = "transcript"


#: A dialogue line: ``[04:12] 1512 (overlapping): the words``.
#:
#: Both the timestamp and the speaker label are optional in the pattern but the timestamp
#: anchors it, so a plain sentence that happens to contain a colon is never touched. The label
#: is bounded because a long line with a mid-sentence colon should keep its first clause.
_DIALOGUE_PREFIX = re.compile(r"^\[\d{1,2}:\d{2}(?::\d{2})?\]\s*(?:[^:\n]{0,40}:\s*)?")


def spoken_text(transcript: str) -> str:
    """
    Strip the speaker and timestamp prefixes, leaving only what was said.

    Transcripts are stored as formatted dialogue -- ``[04:12] 1512: ...`` -- because that is
    what a person reads. Handing that to the analysers feeds them the furniture: the summary
    picks sentences and can return "[04:12] 1512" as though it were content, sentiment counts
    extension numbers toward its totals, and keyword matching scans timestamps.

    Speaker labels are not lost information here. Nothing consults them -- ``agent_sentiment``
    is hardcoded -- so there is nothing to preserve by keeping them in the text.
    """
    return "\n".join(_DIALOGUE_PREFIX.sub("", line) for line in transcript.splitlines()).strip()


class RecordingAnalytics:
    """
    Call Recording Analytics

    AI-powered analysis of recorded calls using FREE open-source tools.
    Features:
    - Sentiment analysis (spaCy)
    - Keyword detection (spaCy)
    - Compliance checking
    - Quality scoring
    - Automatic summarization (spaCy)
    - Trend analysis

    Uses FREE open-source libraries:
    - spaCy for NLP and sentiment analysis
    - NLTK for text processing

    Can also integrate with:
    - OpenAI Whisper (transcription)
    - GPT models (summarization, sentiment)
    - Custom ML models (compliance, quality)
    """

    def __init__(self, config: Any | None = None, database: Any | None = None) -> None:
        """Initialize recording analytics"""
        self.logger = get_logger()
        self.config = config or {}
        #: Where the transcript comes from. Analysis no longer transcribes anything: the
        #: recording was already transcribed post-call by faster-whisper and stored, so
        #: re-deriving it here would be slower, worse and redundant.
        self.database = database

        # Configuration
        # Under recording.* with the rest of the recording pipeline, not features.*: it is
        # configuration for recordings, not a feature toggle.
        analytics_config = self.config.get("recording", {}).get("analytics", {})
        self.auto_analyze = analytics_config.get("auto_analyze", False)
        self.analysis_types = analytics_config.get(
            "analysis_types", ["sentiment", "keywords", "summary"]
        )

        # Analysis results storage
        self.analyses: dict[str, dict] = {}

        # Statistics
        self.total_analyses = 0
        self.analyses_by_type = {}

        # Initialize NLP models
        self.spacy_nlp = None
        self._initialize_models()

        self.logger.info("Call recording analytics initialized")
        self.logger.info(f"  Auto-analyze: {self.auto_analyze}")
        self.logger.info(f"  Analysis types: {', '.join(self.analysis_types)}")
        self.logger.info(f"  spaCy available: {SPACY_AVAILABLE}")

    def _initialize_models(self) -> None:
        """Initialize NLP models for analysis"""

        # Initialize spaCy for NLP
        if SPACY_AVAILABLE:
            try:
                self.spacy_nlp = spacy.load("en_core_web_sm")
                self.logger.info("spaCy model loaded successfully")
            except Exception as e:
                self.logger.warning(f"Could not load spaCy model: {e}")
                self.logger.info("Download with: python -m spacy download en_core_web_sm")

    def analyze_recording(self, recording_id: str, analysis_types: list[str] | None = None) -> dict:
        """
        Analyze a call recording from its stored transcript.

        Args:
            recording_id: Recording identifier
            analysis_types: Types of analysis to perform

        Returns:
            dict: Analysis results

        This used to take an ``audio_path`` and transcribe it once per analysis type -- three
        times for the default set, six if all were requested -- reloading the Vosk model on
        every one, synchronously inside the request. Each pass produced the same text from the
        same bytes.

        None of it was necessary. Post-call transcription already ran faster-whisper over this
        recording and stored the result, so analysis reads that: one query instead of N model
        loads, better text than Vosk produced, and no audio file touched. Taking a recording id
        rather than a caller-supplied path also closes the arbitrary file read that came with it.
        """
        analysis_types = analysis_types or self.analysis_types

        results: dict[str, Any] = {
            "recording_id": recording_id,
            "analyzed_at": datetime.now(UTC).isoformat(),
            "analyses": {},
        }

        row = self._transcript_row(recording_id)
        stored = None if row is None else str(row.get("text") or "")
        # The analysers get the words; the transcript analysis returns the
        # dialogue as stored, which is the form a person wants to read.
        transcript = None if stored is None else spoken_text(stored)
        if transcript is None:
            # Transcription runs after the call and is queued, so a recording that has just
            # finished legitimately has no transcript yet. Saying so beats transcribing it
            # again here and beats returning analyses of an empty string as though they meant
            # something.
            results["error"] = "no transcript stored for this recording yet"
            return results

        for analysis_type in analysis_types:
            if analysis_type == "transcript":
                results["analyses"]["transcript"] = {"transcript": stored}
            elif analysis_type == "sentiment":
                results["analyses"]["sentiment"] = self._analyze_sentiment(transcript)
            elif analysis_type == "keywords":
                results["analyses"]["keywords"] = self._detect_keywords(transcript)
            elif analysis_type == "compliance":
                results["analyses"]["compliance"] = self._check_compliance(transcript)
            elif analysis_type == "quality":
                results["analyses"]["quality"] = self._score_quality(transcript)
            elif analysis_type == "summary":
                results["analyses"]["summary"] = self._summarize(transcript)

            self.analyses_by_type[analysis_type] = self.analyses_by_type.get(analysis_type, 0) + 1

        self.analyses[recording_id] = results
        self._persist(row.get("id"), recording_id, results)
        self.total_analyses += 1

        self.logger.info(f"Analyzed recording {recording_id}")
        self.logger.info(f"  Analysis types: {', '.join(analysis_types)}")

        return results

    def _persist(self, transcript_id: Any, recording_id: str, results: dict[str, Any]) -> None:
        """
        Store the whole analysis, not just its summary.

        Results used to live only in ``self.analyses`` on a module-level singleton, so a
        restart emptied them and ``search_recordings`` could only ever see calls analysed since
        boot. Keywords and quality are stored too, because those are what search filters on.

        Never raises: an analysis that could not be stored is still worth returning.
        """
        if not (self.database and getattr(self.database, "enabled", False)):
            return
        if transcript_id is None:
            return

        analyses = results.get("analyses", {})
        sentiment = analyses.get("sentiment") or {}
        summary = analyses.get("summary") or {}
        quality = analyses.get("quality") or {}

        try:
            self.database.execute(
                """
                INSERT INTO recording_analyses (
                    transcript_id, recording_id, summary, sentiment, sentiment_score,
                    keywords, compliance, quality, analysis_types, quality_score
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    transcript_id,
                    recording_id,
                    summary.get("summary") if isinstance(summary, dict) else None,
                    sentiment.get("overall_sentiment"),
                    sentiment.get("sentiment_score"),
                    json.dumps(analyses.get("keywords")) if "keywords" in analyses else None,
                    json.dumps(analyses.get("compliance")) if "compliance" in analyses else None,
                    json.dumps(quality) if quality else None,
                    json.dumps(sorted(analyses)),
                    quality.get("overall_score") if isinstance(quality, dict) else None,
                ),
            )
        except Exception as e:
            self.logger.error(f"Could not store analysis for transcript {transcript_id}: {e}")

    def stored_analyses(self, limit: int = 200) -> list[dict[str, Any]]:
        """
        Every stored analysis, newest first, shaped like the in-memory results.

        This is what makes search and trends survive a restart: they read here rather than
        from a dict that only ever held what this process analysed.
        """
        if not (self.database and getattr(self.database, "enabled", False)):
            return []

        try:
            rows = self.database.fetch_all(
                "SELECT id, transcript_id, recording_id, summary, sentiment, sentiment_score, "
                "keywords, compliance, quality, analysis_types, quality_score, analyzed_at "
                f"FROM recording_analyses ORDER BY analyzed_at DESC LIMIT {int(limit)}"
            )
        except Exception as e:
            self.logger.error(f"Could not read stored analyses: {e}")
            return []

        return [self._row_to_result(row) for row in rows or []]

    @staticmethod
    def _row_to_result(row: dict[str, Any]) -> dict[str, Any]:
        """Turn a stored row back into the nested shape callers already expect."""

        def decode(value: Any) -> Any:
            if isinstance(value, str):
                try:
                    return json.loads(value)
                except (TypeError, ValueError):
                    return None
            return value

        analyses: dict[str, Any] = {}
        if row.get("sentiment"):
            analyses["sentiment"] = {
                "overall_sentiment": row.get("sentiment"),
                "sentiment_score": row.get("sentiment_score"),
            }
        if row.get("summary"):
            analyses["summary"] = {"summary": row.get("summary")}
        for column in ("keywords", "compliance", "quality"):
            decoded = decode(row.get(column))
            if decoded is not None:
                analyses[column] = decoded

        return {
            "recording_id": row.get("recording_id"),
            "transcript_id": row.get("transcript_id"),
            "analyzed_at": row.get("analyzed_at"),
            "analyses": analyses,
        }

    def _all_analyses(self) -> dict[str, dict]:
        """
        Everything analysed, stored first and falling back to memory.

        Without a database the in-memory dict is all there is, which is what a test or a
        database-less install has anyway.
        """
        stored = {
            str(r["recording_id"]): r for r in self.stored_analyses() if r.get("recording_id")
        }
        merged = dict(self.analyses)
        merged.update(stored)
        return merged

    def _transcript_row(self, recording_id: str) -> dict[str, Any] | None:
        """
        The transcript row for this recording, or None if there is not one yet.

        Read once and reused for both the text and the id the analysis is stored against --
        fetching it twice was the kind of thing this whole change exists to stop doing.

        Newest first: re-transcribing with a better model appends a row rather than replacing
        the old one, so the most recent is the best available.
        """
        if not (self.database and getattr(self.database, "enabled", False)):
            return None

        from pbx.speech.store import TranscriptStore

        rows = TranscriptStore(self.database, self.logger).for_recording(recording_id, limit=1)
        return rows[0] if rows else None

    def _analyze_sentiment(self, transcript: str) -> dict:
        """
        Analyze call sentiment from the stored transcript

        Uses FREE open-source tools:
        - spaCy for NLP and sentiment analysis

        Can also integrate with:
        - OpenAI GPT for semantic sentiment analysis
        - Google Cloud Natural Language API
        - Custom ML sentiment models

        Args:
            transcript: The call's stored transcript

        Returns:
            dict: Sentiment analysis results
        """

        # Sentiment keywords for fallback
        positive_words = {
            "thank",
            "thanks",
            "grateful",
            "appreciate",
            "excellent",
            "great",
            "wonderful",
            "happy",
            "satisfied",
            "love",
            "perfect",
            "amazing",
            "fantastic",
            "pleased",
            "good",
            "helpful",
            "friendly",
        }
        negative_words = {
            "angry",
            "upset",
            "frustrated",
            "disappointed",
            "terrible",
            "awful",
            "horrible",
            "bad",
            "worst",
            "hate",
            "annoyed",
            "complaint",
            "problem",
            "issue",
            "broken",
            "failed",
            "error",
            "wrong",
            "unhappy",
        }

        sentiment_score = 0.0
        confidence = 0.0
        overall_sentiment = "neutral"

        # Use spaCy for enhanced sentiment analysis if available
        if transcript and self.spacy_nlp:
            try:
                doc = self.spacy_nlp(transcript)

                # Count sentiment indicators using lemmatization
                tokens = [token.lemma_.lower() for token in doc if token.is_alpha]
                positive_count = sum(1 for token in tokens if token in positive_words)
                negative_count = sum(1 for token in tokens if token in negative_words)

                # Calculate sentiment score (-1.0 to 1.0)
                total_indicators = positive_count + negative_count
                if total_indicators > 0:
                    sentiment_score = (positive_count - negative_count) / total_indicators
                    # Confidence based on number of indicators (more = higher confidence)

                # Determine overall sentiment
                if sentiment_score > 0.2:
                    overall_sentiment = "positive"
                elif sentiment_score < -0.2:
                    overall_sentiment = "negative"

                self.logger.debug(
                    f"spaCy sentiment analysis: {overall_sentiment} (score: {sentiment_score:.2f})"
                )

            except Exception as e:
                self.logger.error(f"spaCy sentiment analysis failed: {e}")

        # Fallback to basic keyword analysis if spaCy not available
        elif transcript:
            transcript_lower = transcript.lower()

            # Count sentiment indicators
            positive_count = sum(1 for word in positive_words if word in transcript_lower)
            negative_count = sum(1 for word in negative_words if word in transcript_lower)

            # Calculate sentiment score (-1.0 to 1.0)
            total_indicators = positive_count + negative_count
            if total_indicators > 0:
                sentiment_score = (positive_count - negative_count) / total_indicators
                confidence = min(total_indicators / 10.0, 1.0)

            # Determine overall sentiment
            if sentiment_score > 0.2:
                overall_sentiment = "positive"
            elif sentiment_score < -0.2:
                overall_sentiment = "negative"

        return {
            "overall_sentiment": overall_sentiment,
            "sentiment_score": sentiment_score,
            "confidence": confidence,
            "customer_sentiment": overall_sentiment,
            "agent_sentiment": "neutral",
            "sentiment_timeline": [],
        }

    def _detect_keywords(self, transcript: str) -> dict:
        """
        Detect important keywords and topics

        In production, integrate with:
        - spaCy for Named Entity Recognition
        - YAKE or KeyBERT for keyword extraction
        - Custom domain-specific keyword models

        Args:
            transcript: The call's stored transcript

        Returns:
            dict: Detected keywords and topics
        """
        # Transcribe audio to get text for keyword analysis

        # Keyword categories
        competitor_keywords = ["competitor", "alternative", "other company", "switch"]
        product_keywords = ["product", "service", "feature", "plan", "package"]
        issue_keywords = ["problem", "issue", "broken", "not working", "error", "fail"]
        sales_keywords = ["purchase", "buy", "price", "cost", "discount", "deal"]
        support_keywords = ["help", "support", "assistance", "troubleshoot", "fix"]

        keywords = []
        competitor_mentions = []
        product_mentions = []
        issue_keywords_found = []

        if transcript:
            transcript_lower = transcript.lower()

            # Detect competitor mentions
            competitor_mentions.extend(
                keyword for keyword in competitor_keywords if keyword in transcript_lower
            )

            # Detect product mentions
            product_mentions.extend(
                keyword for keyword in product_keywords if keyword in transcript_lower
            )

            # Detect issue keywords
            issue_keywords_found.extend(
                keyword for keyword in issue_keywords if keyword in transcript_lower
            )

            # Combine all for general keywords
            all_keyword_sets = [
                sales_keywords,
                support_keywords,
                issue_keywords,
                product_keywords,
                competitor_keywords,
            ]
            for keyword_set in all_keyword_sets:
                for keyword in keyword_set:
                    if keyword in transcript_lower and keyword not in keywords:
                        keywords.append(keyword)

        return {
            "keywords": keywords,
            "competitor_mentions": competitor_mentions,
            "product_mentions": product_mentions,
            "issue_keywords": issue_keywords_found,
        }

    def _check_compliance(self, transcript: str) -> dict:
        """
        Check compliance requirements

        In production, integrate with:
        - Custom compliance rule engine
        - Speech recognition for required phrases
        - Regulatory compliance databases

        Args:
            transcript: The call's stored transcript

        Returns:
            dict: Compliance check results
        """
        # Transcribe audio to get text for compliance checking

        # Compliance requirements
        required_phrases = [
            "this call may be recorded",
            "for quality assurance",
            "terms and conditions",
            "do you agree",
        ]

        prohibited_phrases = [
            "guaranteed",
            "no risk",
            "can't lose",
            "promise",  # In certain contexts
        ]

        violations = []
        warnings = []
        required_found = []
        prohibited_found = []

        if transcript:
            transcript_lower = transcript.lower()

            # Check for required phrases
            required_found.extend(
                phrase for phrase in required_phrases if phrase in transcript_lower
            )

            # Check for prohibited phrases
            for phrase in prohibited_phrases:
                if phrase in transcript_lower:
                    prohibited_found.append(phrase)
                    violations.append(f"Prohibited phrase detected: '{phrase}'")

            # Check if all required phrases were found
            missing_required = [p for p in required_phrases if p not in required_found]
            if missing_required:
                warnings.extend([f"Missing required phrase: '{p}'" for p in missing_required])

        compliant = len(violations) == 0

        return {
            "compliant": compliant,
            "violations": violations,
            "warnings": warnings,
            "required_phrases_found": required_found,
            "prohibited_phrases_found": prohibited_found,
        }

    def _score_quality(self, transcript: str) -> dict:
        """
        Score call quality based on multiple factors

        In production, integrate with:
        - Speech analytics for tone and pace
        - ML models for agent performance
        - Customer satisfaction prediction models

        Args:
            transcript: The call's stored transcript

        Returns:
            dict: Quality scores
        """
        # Transcribe audio to get text for quality scoring

        # Quality indicators
        positive_indicators = [
            "thank you",
            "appreciate",
            "understand",
            "help you",
            "certainly",
            "absolutely",
            "of course",
            "glad to help",
        ]

        negative_indicators = [
            "wait",
            "hold on",
            "I don't know",
            "not sure",
            "can't help",
            "impossible",
            "no way",
        ]

        professionalism_indicators = [
            "sir",
            "ma'am",
            "please",
            "thank you",
            "may I",
            "would you like",
        ]

        overall_score = 50.0  # Base score
        agent_performance = 50.0
        customer_satisfaction = 50.0
        resolution_quality = 50.0
        professionalism = 50.0

        if transcript:
            transcript_lower = transcript.lower()

            # Calculate agent performance based on positive/negative indicators
            positive_count = sum(
                1 for indicator in positive_indicators if indicator in transcript_lower
            )
            negative_count = sum(
                1 for indicator in negative_indicators if indicator in transcript_lower
            )

            if positive_count + negative_count > 0:
                agent_performance = 50 + (positive_count - negative_count) * 5
                agent_performance = max(0, min(100, agent_performance))  # Clamp to 0-100

            # Calculate professionalism
            professionalism_count = sum(
                1 for indicator in professionalism_indicators if indicator in transcript_lower
            )
            professionalism = min(100, 50 + professionalism_count * 10)

            # Customer satisfaction based on positive sentiment words
            satisfaction_words = ["satisfied", "happy", "thank", "great", "excellent"]
            dissatisfaction_words = ["unhappy", "disappointed", "frustrated", "angry"]

            sat_count = sum(1 for word in satisfaction_words if word in transcript_lower)
            dissat_count = sum(1 for word in dissatisfaction_words if word in transcript_lower)

            if sat_count + dissat_count > 0:
                customer_satisfaction = 50 + (sat_count - dissat_count) * 10
                customer_satisfaction = max(0, min(100, customer_satisfaction))

            # Resolution quality based on resolution keywords
            resolution_words = ["resolved", "fixed", "solved", "working now", "taken care of"]
            resolution_count = sum(1 for word in resolution_words if word in transcript_lower)
            resolution_quality = min(100, 50 + resolution_count * 15)

            # Overall score is weighted average
            overall_score = (
                agent_performance * 0.3
                + customer_satisfaction * 0.3
                + resolution_quality * 0.25
                + professionalism * 0.15
            )

        return {
            "overall_score": round(overall_score, 2),  # 0-100
            "agent_performance": round(agent_performance, 2),
            "customer_satisfaction": round(customer_satisfaction, 2),
            "resolution_quality": round(resolution_quality, 2),
            "professionalism": round(professionalism, 2),
        }

    def _summarize(self, transcript: str) -> dict:
        """
        Generate call summary using extractive summarization

        In production, integrate with:
        - OpenAI GPT for abstractive summarization
        - BART or T5 models for summarization
        - Custom domain-specific summarization models

        Args:
            transcript: The call's stored transcript

        Returns:
            dict: Call summary with key points and action items
        """
        # Transcribe audio to get text for summarization

        summary = ""
        key_points = []
        action_items = []
        outcomes = []

        if transcript:
            # Split into sentences
            sentences = [s.strip() for s in transcript.split(".") if s.strip()]

            # Extractive summarization - select most important sentences
            # In production, use more sophisticated methods (TF-IDF, TextRank, etc.)

            # Look for action items (sentences with action verbs)
            action_verbs = ["will", "need to", "must", "should", "going to", "have to"]
            action_items.extend(
                sentence
                for sentence in sentences
                if any(verb in sentence.lower() for verb in action_verbs)
            )

            # Look for outcomes (sentences with resolution indicators)
            outcome_indicators = [
                "resolved",
                "fixed",
                "completed",
                "done",
                "finished",
                "successful",
            ]
            outcomes.extend(
                sentence
                for sentence in sentences
                if any(indicator in sentence.lower() for indicator in outcome_indicators)
            )

            # Generate key points from first few sentences and important indicators
            important_keywords = [
                "issue",
                "problem",
                "request",
                "question",
                "concern",
                "solution",
                "answer",
                "resolution",
                "next steps",
            ]
            key_points.extend(
                sentence
                for sentence in sentences[:5]  # First 5 sentences
                if any(keyword in sentence.lower() for keyword in important_keywords)
            )

            # Create summary from key points
            if key_points:
                summary = ". ".join(key_points[:3]) + "."
            elif sentences:
                summary = ". ".join(sentences[:2]) + "."

        return {
            "summary": summary,
            "key_points": key_points[:5],  # Top 5 key points
            "action_items": action_items[:5],  # Top 5 action items
            "outcomes": outcomes[:3],  # Top 3 outcomes
        }

    def search_recordings(self, criteria: dict) -> list[str]:
        """
        Search recordings by analysis criteria with improved matching

        Args:
            criteria: Search criteria dict with keys:
                - sentiment: 'positive', 'negative', or 'neutral'
                - keywords: list of keywords to search for
                - min_quality_score: minimum quality score (0-100)
                - compliant: True/False for compliance status

        Returns:
            list[str]: Matching recording IDs
        """
        matching = []

        for recording_id, analysis in self._all_analyses().items():
            match = True

            # Check sentiment criteria
            if "sentiment" in criteria:
                sentiment_result = analysis["analyses"].get("sentiment", {})
                if sentiment_result.get("overall_sentiment") != criteria["sentiment"]:
                    match = False

            # Check keyword criteria
            if "keywords" in criteria and match:
                keyword_result = analysis["analyses"].get("keywords", {})
                found_keywords = keyword_result.get("keywords", [])
                if not any(k in found_keywords for k in criteria["keywords"]):
                    match = False

            # Check quality score criteria
            if "min_quality_score" in criteria and match:
                quality_result = analysis["analyses"].get("quality", {})
                overall_score = quality_result.get("overall_score", 0)
                if overall_score < criteria["min_quality_score"]:
                    match = False

            # Check compliance criteria
            if "compliant" in criteria and match:
                compliance_result = analysis["analyses"].get("compliance", {})
                if compliance_result.get("compliant") != criteria["compliant"]:
                    match = False

            if match:
                matching.append(recording_id)

        return matching

    def get_analysis(self, recording_id: str) -> dict | None:
        """
        Get analysis results for a recording

        Args:
            recording_id: Recording identifier

        Returns:
            dict | None: Analysis results or None if not found
        """
        return self._all_analyses().get(recording_id)

    def _filter_analyses_by_date(self, start_date: datetime, end_date: datetime) -> list:
        """Filter analyses by date range"""
        filtered_analyses = []
        for recording_id, analysis in self._all_analyses().items():
            try:
                analyzed_at = datetime.fromisoformat(analysis["analyzed_at"])
                if start_date <= analyzed_at <= end_date:
                    filtered_analyses.append(analysis)
            except (ValueError, KeyError) as e:
                self.logger.warning(
                    f"Skipping analysis {recording_id} due to invalid timestamp: {e}"
                )
        return filtered_analyses

    def _aggregate_sentiment_data(self, filtered_analyses: list) -> list:
        """Aggregate sentiment data from analyses"""
        sentiment_trend = []
        for analysis in filtered_analyses:
            sentiment_data = analysis["analyses"].get("sentiment", {})
            if sentiment_data:
                sentiment_trend.append(
                    {
                        "date": analysis["analyzed_at"],
                        "sentiment": sentiment_data.get("overall_sentiment", "neutral"),
                        "score": sentiment_data.get("sentiment_score", 0.0),
                    }
                )
        return sentiment_trend

    def _aggregate_quality_data(self, filtered_analyses: list) -> list:
        """Aggregate quality data from analyses"""
        quality_trend = []
        for analysis in filtered_analyses:
            quality_data = analysis["analyses"].get("quality", {})
            if quality_data:
                quality_trend.append(
                    {
                        "date": analysis["analyzed_at"],
                        "overall_score": quality_data.get("overall_score", 0.0),
                        "agent_performance": quality_data.get("agent_performance", 0.0),
                        "customer_satisfaction": quality_data.get("customer_satisfaction", 0.0),
                    }
                )
        return quality_trend

    def _aggregate_keyword_trends(self, filtered_analyses: list) -> dict:
        """Aggregate and sort keyword trends"""
        keyword_trends = {}
        for analysis in filtered_analyses:
            keyword_data = analysis["analyses"].get("keywords", {})
            if keyword_data:
                for keyword in keyword_data.get("keywords", []):
                    keyword_trends[keyword] = keyword_trends.get(keyword, 0) + 1
        return dict(sorted(keyword_trends.items(), key=lambda x: x[1], reverse=True))

    def _calculate_compliance_data(self, filtered_analyses: list) -> dict:
        """Calculate compliance statistics"""
        compliance_data = {"compliant": 0, "non_compliant": 0}
        for analysis in filtered_analyses:
            compliance_data_item = analysis["analyses"].get("compliance", {})
            if compliance_data_item:
                if compliance_data_item.get("compliant", False):
                    compliance_data["compliant"] += 1
                else:
                    compliance_data["non_compliant"] += 1
        return compliance_data

    def get_trend_analysis(self, start_date: datetime, end_date: datetime) -> dict:
        """
        Analyze trends over time with aggregated metrics

        Args:
            start_date: Start date for analysis
            end_date: End date for analysis

        Returns:
            dict: Trend analysis with sentiment, quality, keywords, and compliance data
        """
        filtered_analyses = self._filter_analyses_by_date(start_date, end_date)

        if not filtered_analyses:
            return {
                "sentiment_trend": [],
                "quality_trend": [],
                "keyword_trends": {},
                "compliance_rate": 0.0,
                "total_recordings": 0,
            }

        sentiment_trend = self._aggregate_sentiment_data(filtered_analyses)
        quality_trend = self._aggregate_quality_data(filtered_analyses)
        keyword_trends = self._aggregate_keyword_trends(filtered_analyses)
        compliance_data = self._calculate_compliance_data(filtered_analyses)

        total_compliance_checks = compliance_data["compliant"] + compliance_data["non_compliant"]
        compliance_rate = (
            compliance_data["compliant"] / total_compliance_checks
            if total_compliance_checks > 0
            else 0.0
        )

        return {
            "sentiment_trend": sentiment_trend,
            "quality_trend": quality_trend,
            "keyword_trends": keyword_trends,
            "compliance_rate": round(compliance_rate * 100, 2),  # As percentage
            "total_recordings": len(filtered_analyses),
            "date_range": {"start": start_date.isoformat(), "end": end_date.isoformat()},
        }

    def get_statistics(self) -> dict:
        """Get analytics statistics"""
        return {
            "auto_analyze": self.auto_analyze,
            "total_analyses": self.total_analyses,
            "analyses_by_type": self.analyses_by_type,
            "available_analysis_types": self.analysis_types,
        }


# Global instance
_recording_analytics = None


def get_recording_analytics(
    config: Any | None = None, database: Any | None = None
) -> RecordingAnalytics:
    """Get or create recording analytics instance"""
    global _recording_analytics
    if _recording_analytics is None:
        _recording_analytics = RecordingAnalytics(config, database)
    return _recording_analytics
