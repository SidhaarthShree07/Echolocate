"""
EchoLocate — clarification node.

Engaged when the router determines that intent is ambiguous (confidence below
threshold or required entities missing). Asks a targeted spoken question to
resolve the ambiguity, then re-routes.

This is the ADK equivalent of a RequestInput node: it pauses the workflow
and waits for the user's next utterance, which is parsed against the pending
intent context rather than re-classified from scratch.

Architecture Section 4.2 + Section 6 (state management).
"""
from __future__ import annotations

from typing import Optional

from echolocate.state import ClassifierOutput, PendingIntent, SessionState


CLARIFICATION_QUESTIONS = {
    "file_reference": "Which file are you referring to?",
    "relative_date": "When was the file saved? For example, last Tuesday, or this month.",
    "target_action": "What would you like to do with the file — open it, move it, or something else?",
    "default": "I didn't quite catch that. Could you say that again?",
}


class ClarificationNode:
    """
    Manages the clarification loop.

    When the router can't dispatch with confidence, this node:
    1. Determines what's missing (what to ask)
    2. Returns a spoken question for TTS
    3. Records the pending intent in session state
    4. On the next turn, the router enriches the utterance with context
    """

    def ask(
        self,
        session_state: SessionState,
        reason: Optional[str] = None,
    ) -> str:
        """
        Determine what to ask based on the current session state.

        Args:
            session_state: Current session state.
            reason: Optional reason code ("low_confidence", "missing_entity", etc.)

        Returns:
            The spoken clarification question for TTS.
        """
        clf = session_state.last_classifier_output

        # Determine what's missing
        missing_field = self._determine_missing_field(clf, session_state)

        # Build the spoken question
        question = self._build_question(clf, missing_field, session_state)

        # Update session state with pending intent
        if clf:
            session_state.pending_intent = PendingIntent(
                raw_utterance=getattr(clf, "_raw_utterance", ""),
                partial_entities=clf.extracted_entities,
                awaiting=missing_field,
            )

        return question

    def resolve_pending(
        self,
        clarifying_utterance: str,
        session_state: SessionState,
    ) -> str:
        """
        Called when the user responds to a clarification question.
        Clears the pending intent and returns the enriched utterance
        for the router to re-classify.

        Args:
            clarifying_utterance: The user's response to the clarification.
            session_state: Current session state.

        Returns:
            Enriched utterance combining original context + clarifying response.
        """
        pi = session_state.pending_intent
        if not pi:
            return clarifying_utterance

        # Clear pending intent — will be repopulated if still unclear
        session_state.pending_intent = None

        # Build enriched utterance for re-classification
        if pi.awaiting:
            enriched = (
                f"Previously the user said: '{pi.raw_utterance}'. "
                f"They were asked about: '{pi.awaiting}'. "
                f"They responded: '{clarifying_utterance}'"
            )
        else:
            enriched = clarifying_utterance

        return enriched

    def _determine_missing_field(
        self,
        clf: Optional[ClassifierOutput],
        session_state: SessionState,
    ) -> str:
        """Figure out which entity is missing or why confidence is low."""
        if not clf or clf.intent == "clarification_needed":
            return "default"

        entities = clf.extracted_entities or {}

        # Check file reference (most commonly missing)
        if clf.intent in {"file_search", "document_qa", "document_read_aloud", "system_action"}:
            if not entities.get("file_reference") and not session_state.last_referenced_file:
                return "file_reference"

        # Check target action for system_action
        if clf.intent == "system_action" and not entities.get("target_action"):
            return "target_action"

        # Low confidence but entities look OK
        if clf.confidence < 0.5:
            return "default"

        return "file_reference"  # fallback

    def _build_question(
        self,
        clf: Optional[ClassifierOutput],
        missing_field: str,
        session_state: SessionState,
    ) -> str:
        """Build a targeted spoken question."""
        # Context-aware questions
        if clf and clf.intent == "system_action" and missing_field == "file_reference":
            action = clf.extracted_entities.get("target_action", "action")
            return f"Which file would you like to {action}?"

        if clf and clf.intent == "document_qa" and missing_field == "file_reference":
            return "Which document would you like me to look at?"

        if clf and clf.intent == "document_read_aloud" and missing_field == "file_reference":
            return "Which document would you like me to read aloud?"

        if clf and clf.intent == "file_search" and missing_field == "file_reference":
            return "What file are you looking for? Can you describe it or say its name?"

        return CLARIFICATION_QUESTIONS.get(
            missing_field,
            CLARIFICATION_QUESTIONS["default"]
        )
