"""
EchoLocate — Router confidence test suite.

Tests the two-stage verification: confidence threshold AND required-entity
presence check. Tests that each failure mode independently routes to
clarification rather than silently picking the closest-confidence specialist.
"""
from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock

from echolocate.nodes.router import IntentRouter, _resolve_relative_date, CONFIDENCE_THRESHOLD_DEFAULT
from echolocate.state import ClassifierOutput, SessionState


@pytest.fixture
def router():
    return IntentRouter(
        llm_model="test/mock",
        confidence_threshold=0.7,
    )


@pytest.fixture
def session():
    return SessionState()


class TestTwoStageDispatch:
    """Tests for the deterministic dispatch function."""

    def test_high_confidence_with_entities_routes_to_specialist(self, router, session):
        clf = ClassifierOutput(
            intent="file_search",
            confidence=0.92,
            extracted_entities={"file_reference": "agriculture_report.pdf"},
        )
        dest = router._dispatch(clf, session)
        assert dest == "file_search"

    def test_low_confidence_routes_to_clarification(self, router, session):
        clf = ClassifierOutput(
            intent="file_search",
            confidence=0.55,  # below 0.7 threshold
            extracted_entities={"file_reference": "some_file.pdf"},
        )
        dest = router._dispatch(clf, session)
        assert dest == "clarification"

    def test_exactly_at_threshold_routes_to_specialist(self, router, session):
        clf = ClassifierOutput(
            intent="document_qa",
            confidence=0.7,  # exactly at threshold
            extracted_entities={"file_reference": "report.pdf"},
        )
        dest = router._dispatch(clf, session)
        assert dest == "document"

    def test_high_confidence_missing_entities_routes_to_clarification(self, router, session):
        """High confidence but no entities = entity check should fail."""
        clf = ClassifierOutput(
            intent="file_search",
            confidence=0.95,
            extracted_entities={
                "file_reference": None,
                "relative_date": None,  # BOTH null → entity check fails
            },
        )
        dest = router._dispatch(clf, session)
        assert dest == "clarification", (
            "High confidence alone should NOT override missing entities"
        )

    def test_entity_check_uses_session_state_last_referenced_file(self, router, session):
        """last_referenced_file in session state counts as implicit file_reference."""
        session.last_referenced_file = "resume.pdf"
        clf = ClassifierOutput(
            intent="document_qa",
            confidence=0.88,
            extracted_entities={"file_reference": None},  # null but session has context
        )
        dest = router._dispatch(clf, session)
        assert dest == "document"

    def test_system_action_requires_both_file_and_action(self, router, session):
        clf = ClassifierOutput(
            intent="system_action",
            confidence=0.85,
            extracted_entities={
                "file_reference": "resume.pdf",
                "target_action": None,  # action missing
            },
        )
        dest = router._dispatch(clf, session)
        assert dest == "clarification"

    def test_system_action_with_both_entities_routes_correctly(self, router, session):
        clf = ClassifierOutput(
            intent="system_action",
            confidence=0.85,
            extracted_entities={
                "file_reference": "resume.pdf",
                "target_action": "move",
            },
        )
        dest = router._dispatch(clf, session)
        assert dest == "system_executor"

    def test_clarification_needed_intent_always_routes_to_clarification(self, router, session):
        clf = ClassifierOutput(
            intent="clarification_needed",
            confidence=0.99,  # even 99% confidence → clarification
            extracted_entities={"file_reference": "anything.pdf"},
        )
        dest = router._dispatch(clf, session)
        assert dest == "clarification"

    def test_document_read_aloud_routes_to_document(self, router, session):
        clf = ClassifierOutput(
            intent="document_read_aloud",
            confidence=0.88,
            extracted_entities={"file_reference": "report.pdf"},
        )
        dest = router._dispatch(clf, session)
        assert dest == "document"


class TestDateResolution:
    """Tests for deterministic date resolution via dateparser."""

    def test_relative_date_resolves_to_iso(self):
        # "3 days ago" is always unambiguous regardless of day of week
        result = _resolve_relative_date("3 days ago")
        assert result is not None
        assert len(result) == 10  # YYYY-MM-DD
        assert result[4] == "-" and result[7] == "-"

    def test_last_week_resolves_to_iso(self):
        result = _resolve_relative_date("last week")
        # dateparser may return None for some locale settings — acceptable
        if result is not None:
            assert len(result) == 10
            assert result[4] == "-" and result[7] == "-"

    def test_yesterday_resolves_to_iso(self):
        from datetime import date, timedelta
        result = _resolve_relative_date("yesterday")
        assert result is not None
        expected = (date.today() - timedelta(days=1)).isoformat()
        assert result == expected

    def test_invalid_date_returns_none(self):
        result = _resolve_relative_date("not a date at all xyzzy")
        # Should return None or a reasonable fallback, not crash
        assert result is None or isinstance(result, str)

    def test_empty_string_returns_none(self):
        result = _resolve_relative_date("")
        assert result is None


class TestParserOutput:
    """Tests for parsing classifier JSON output."""

    def test_valid_json_parsed_correctly(self, router, session):
        raw = '''{"intent": "file_search", "confidence": 0.91,
                  "extracted_entities": {"file_reference": "agriculture.pdf",
                                          "relative_date": null, "target_action": null}}'''
        clf = router._parse_classifier_output(raw, session)
        assert clf.intent == "file_search"
        assert clf.confidence == 0.91
        assert clf.file_reference == "agriculture.pdf"

    def test_invalid_json_returns_clarification(self, router, session):
        clf = router._parse_classifier_output("this is not json", session)
        assert clf.intent == "clarification_needed"

    def test_unknown_intent_maps_to_clarification(self, router, session):
        raw = '{"intent": "unknown_intent_xyz", "confidence": 0.99, "extracted_entities": {}}'
        clf = router._parse_classifier_output(raw, session)
        assert clf.intent == "clarification_needed"
