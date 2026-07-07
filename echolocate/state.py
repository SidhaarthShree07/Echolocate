"""
EchoLocate — typed session state.

This is the single typed object carried through every node in the ADK graph
workflow. Using a dataclass (not a plain dict) means type errors surface at
construction time and every field's purpose is documented here rather than
scattered across node implementations.

Session state is in-memory only, cleared at session end (NFR-4). No
persistence across restarts at MVP.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PendingIntent:
    """
    Populated when the router routes to the Clarification node instead of a
    specialist. Tracks what we already know and what we're still waiting for,
    so the user's next utterance is parsed against this context rather than
    re-classified from scratch.
    """
    raw_utterance: str = ""
    # Partially-resolved entities from the first pass
    partial_entities: dict = field(default_factory=dict)
    # Which entity field we're waiting for ("file_reference", "target_action", etc.)
    awaiting: Optional[str] = None
    # The original intent that triggered this pending disambiguation
    original_intent: Optional[str] = None


@dataclass
class ClassifierOutput:
    """
    Structured JSON output from the intent classifier (Gemma 4 E2B).
    The router function reads this — never raw LLM text.
    """
    intent: str = "clarification_needed"
    confidence: float = 0.0
    extracted_entities: dict = field(default_factory=dict)

    @property
    def file_reference(self) -> Optional[str]:
        return self.extracted_entities.get("file_reference")

    @property
    def relative_date(self) -> Optional[str]:
        return self.extracted_entities.get("relative_date")

    @property
    def target_action(self) -> Optional[str]:
        return self.extracted_entities.get("target_action")


@dataclass
class SessionState:
    """
    The authoritative, typed session state object.

    Principles:
    - No closures, no module-level globals. Everything here.
    - Fields are reset at session end; nothing persists across restarts.
    - last_referenced_file backs pronoun resolution ("open it", "read that one")
      across the File Search → Document node handoff.
    """
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    # Turn history — list of dicts with "role" and "content" keys
    turn_history: list = field(default_factory=list)

    # Populated when routing to the Clarification node; cleared after resolution
    pending_intent: Optional[PendingIntent] = None

    # Most recently confirmed file path (sandbox-relative). Used for pronoun
    # resolution: "open it", "read that document", "move this file".
    last_referenced_file: Optional[str] = None

    # Active confirmation gate: set while waiting for user's yes/no on a
    # destructive action. Cleared after confirmation result arrives.
    active_confirmation: Optional[dict] = None

    # Last classifier output — stored so clarification node can enrich it
    last_classifier_output: Optional[ClassifierOutput] = None

    # TTS playback state
    is_tts_playing: bool = False

    # Speech rate (persisted within session, configurable without restart)
    speech_rate: float = 1.0

    def add_turn(self, role: str, content: str) -> None:
        """Append a turn to the conversation history."""
        self.turn_history.append({"role": role, "content": content})

    def resolve_file_reference(self, ref: Optional[str]) -> Optional[str]:
        """
        Returns ref if non-null; falls back to last_referenced_file for
        pronoun resolution ("it", "that one", "this file").
        """
        if ref and ref.lower() not in {"it", "that", "this", "that one", "this one"}:
            return ref
        return self.last_referenced_file

    def to_dict(self) -> dict:
        """Serialise to a plain dict for ADK state passing."""
        return {
            "session_id": self.session_id,
            "turn_history": self.turn_history,
            "pending_intent": (
                {
                    "raw_utterance": self.pending_intent.raw_utterance,
                    "partial_entities": self.pending_intent.partial_entities,
                    "awaiting": self.pending_intent.awaiting,
                }
                if self.pending_intent
                else None
            ),
            "last_referenced_file": self.last_referenced_file,
            "active_confirmation": self.active_confirmation,
            "is_tts_playing": self.is_tts_playing,
            "speech_rate": self.speech_rate,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SessionState":
        """Reconstruct from a plain dict (e.g. after ADK state round-trip)."""
        state = cls(session_id=d.get("session_id", uuid.uuid4().hex[:8]))
        state.turn_history = d.get("turn_history", [])
        state.last_referenced_file = d.get("last_referenced_file")
        state.active_confirmation = d.get("active_confirmation")
        state.is_tts_playing = d.get("is_tts_playing", False)
        state.speech_rate = d.get("speech_rate", 1.0)

        pi = d.get("pending_intent")
        if pi:
            state.pending_intent = PendingIntent(
                raw_utterance=pi.get("raw_utterance", ""),
                partial_entities=pi.get("partial_entities", {}),
                awaiting=pi.get("awaiting"),
            )
        return state
