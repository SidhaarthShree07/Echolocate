"""
EchoLocate — ADK 2.0 graph workflow orchestration.

This module implements the router + specialist nodes as an ADK graph workflow.
The high-level architecture is:

  Voice input → STT → [router → {file_search | document | system_executor | clarification}] → TTS

The graph uses google.adk.agents.Agent (LlmAgent) for specialist reasoning,
with deterministic Python dispatch functions for routing decisions. This
matches Architecture Section 4.2's design: the LLM classifies, but a
deterministic function decides routing.

ADK 2.0 (google-adk==2.3.0) is pinned exactly — the Skills API and graph
Workflow features require >=1.25.0, and 2.3.0 is the confirmed stable GA
release per PyPI.

Reference: Architecture Section 3 (process topology), Section 4.2 (router),
Section 4.7 (skills wiring).
"""
from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Optional, Callable

from echolocate.state import SessionState
from echolocate.nodes.router import IntentRouter
from echolocate.nodes.clarification import ClarificationNode
from echolocate.nodes.file_search import FileSearchNode
from echolocate.nodes.document import DocumentNode
from echolocate.nodes.system_executor import SystemExecutorNode


class EchoLocateGraph:
    """
    The main ADK-style orchestration graph.

    Implements the router + 3 specialist nodes + clarification node as a
    Python orchestration loop. The router dispatch is deterministic; only
    the classification step uses the local LLM.

    In a full ADK 2.0 Workflow, these would be @node-decorated functions
    connected by conditional edges. This implementation is equivalent in
    behavior and uses the same ADK LlmAgent primitives for the specialist
    reasoning steps.
    """

    def __init__(
        self,
        sandbox_root: Path,
        router_model: str = "ollama_chat/gemma4:e2b",
        document_model: str = "ollama_chat/gemma4:e4b",
        system_model: str = "ollama_chat/gemma4:e4b",
        confidence_threshold: float = 0.7,
        confirm_fn: Optional[Callable[[str], bool]] = None,
        tts_fn: Optional[Callable[[str], None]] = None,
        tts_chunked_fn: Optional[Callable[[str], None]] = None,
        tts_stream_fn: Optional[Callable[[Iterator[str]], str]] = None,
    ) -> None:
        """
        Args:
            sandbox_root: Absolute path to the sandbox directory.
            router_model: LiteLLM model string for the intent classifier.
            document_model: LiteLLM model string for document reasoning.
            system_model: LiteLLM model string for system executor reasoning.
            confidence_threshold: Routing confidence threshold (0.7 default).
            confirm_fn: Callable(prompt: str) -> bool for confirmation gate.
                        If None, uses text-based default.
            tts_fn: Callable(text: str) for single-utterance TTS.
            tts_chunked_fn: Callable(text: str) for chunked read-aloud TTS.
            tts_stream_fn: Callable(iterator) for live LLM-to-TTS playback.
        """
        self.sandbox_root = sandbox_root

        # Nodes
        self.router = IntentRouter(
            sandbox_root=sandbox_root,
            llm_model=router_model,
            confidence_threshold=confidence_threshold,
        )
        self.clarification_node = ClarificationNode()
        self.file_search_node = FileSearchNode(sandbox_root)
        self.document_node = DocumentNode(
            sandbox_root,
            llm_model=document_model,
        )
        self.system_executor_node = SystemExecutorNode(
            sandbox_root,
            confirm_fn=confirm_fn,
        )

        # TTS integration
        self._tts = tts_fn or _print_response
        self._tts_chunked = tts_chunked_fn or _print_response
        self._tts_stream = tts_stream_fn or _consume_stream_response

        # Session state (in-memory, per-session)
        self.session_state = SessionState()

    def process_utterance(self, utterance: str) -> str:
        """
        Process a transcribed utterance through the full pipeline.

        This is the main entry point called by main.py for each STT result.

        Args:
            utterance: Transcribed text from STT.

        Returns:
            The spoken response text (also sent to TTS).
        """
        if not utterance.strip():
            response = "I didn't catch that. Could you say that again?"
            self._tts(response)
            return response

        self.session_state.add_turn("user", utterance)

        # Acknowledge receipt
        self._tts("Got it, working on it.")

        # Route
        destination, clf = self.router.route(utterance, self.session_state)

        # Dispatch
        response_or_stream = self._dispatch(destination, clf)

        # Speak the response. Streaming document answers return the final
        # concatenated string so history still has the complete assistant turn.
        if _is_text_iterator(response_or_stream):
            response = self._tts_stream(response_or_stream)
        elif clf.intent == "document_read_aloud" and destination == "document":
            response = response_or_stream
            self._tts_chunked(response)
        else:
            response = response_or_stream
            self._tts(response)

        self.session_state.add_turn("assistant", response)
        return response

    def _dispatch(self, destination: str, clf) -> str | Iterator[str]:
        """
        Deterministic dispatch to the correct specialist node.
        This function has zero LLM calls — routing is pure Python.
        """
        if destination == "file_search":
            return self.file_search_node.run(clf, self.session_state)

        elif destination == "document":
            return self.document_node.run(
                clf,
                self.session_state,
                stream=clf.intent in {"document_qa"},
            )

        elif destination == "system_executor":
            return self.system_executor_node.run(clf, self.session_state)

        elif destination == "clarification":
            return self.clarification_node.ask(self.session_state)

        else:
            return "I'm not sure what to do with that. Could you say it differently?"

    def reset_session(self) -> None:
        """Clear all in-memory session state. Called at session end (NFR-4)."""
        self.session_state = SessionState()

    def get_session_id(self) -> str:
        return self.session_state.session_id


def _print_response(text: str) -> None:
    """Fallback TTS: print to console (used when TTS is not initialized)."""
    print(f"\n[EchoLocate]: {text}\n")


def _consume_stream_response(text_iterator: Iterator[str]) -> str:
    text = "".join(text_iterator).strip()
    _print_response(text)
    return text


def _is_text_iterator(value: object) -> bool:
    return not isinstance(value, str) and hasattr(value, "__iter__")
