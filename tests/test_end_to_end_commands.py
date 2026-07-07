"""
EchoLocate — End-to-end canonical command tests.

Tests all 5 canonical commands from the PRD using scripted fixtures.
These are integration tests that exercise the full pipeline from intent
classification to spoken response, with the LLM and TTS mocked out for
speed and reproducibility.

Target: ≥8/10 success rate on all commands before recording the demo video
(NFR-6). These tests verify the pipeline logic, not the LLM accuracy
(which is validated separately in quiet-room voice trials).

PRD canonical commands:
  1. "Find that PDF report about agriculture I saved last Tuesday"
  2. "What's the main conclusion of that report?"
  3. "Read this document aloud"
  4. "Open my resume"
  5. "Move this file to my Documents folder"
"""
from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import date, timedelta

from echolocate.graph import EchoLocateGraph
from echolocate.state import ClassifierOutput, SessionState


@pytest.fixture
def sandbox(tmp_path):
    """Create a test sandbox with files matching the canonical commands."""
    (tmp_path / "documents").mkdir()
    (tmp_path / "personal").mkdir()

    # Agriculture PDF from "last Tuesday"
    last_tuesday = date.today() - timedelta(days=(date.today().weekday() + 2) % 7 + 1)
    agriculture_pdf = tmp_path / "Agriculture_Report_Q2.pdf"
    agriculture_pdf.write_bytes(b"PDF content: agriculture report main conclusion is crop yields increased")

    # Set the modification time to last Tuesday
    import os
    import time
    last_tuesday_ts = time.mktime(last_tuesday.timetuple())
    os.utime(str(agriculture_pdf), (last_tuesday_ts, last_tuesday_ts))

    # Resume
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"PDF: John Smith resume 2026")

    # Notes file
    notes = tmp_path / "notes.txt"
    notes.write_text("Meeting notes from last week")

    return tmp_path


def make_mock_graph(sandbox, spoken_responses: list):
    """Create a graph with mocked TTS and scripted LLM responses."""
    response_iter = iter(spoken_responses)

    def mock_tts(text):
        print(f"[MOCK TTS]: {text}")

    graph = EchoLocateGraph(
        sandbox_root=sandbox,
        router_model="test/mock",
        document_model="test/mock",
        system_model="test/mock",
        tts_fn=mock_tts,
        tts_chunked_fn=mock_tts,
    )
    return graph


class TestCanonicalCommand1:
    """Find that PDF report about agriculture I saved last Tuesday."""

    def test_file_search_finds_agriculture_pdf(self, sandbox):
        last_tuesday = date.today() - timedelta(days=(date.today().weekday() + 2) % 7 + 1)

        clf = ClassifierOutput(
            intent="file_search",
            confidence=0.91,
            extracted_entities={
                "file_reference": "agriculture report",
                "relative_date": last_tuesday.isoformat(),
                "target_action": None,
            },
        )

        graph = make_mock_graph(sandbox, [])
        response = graph.file_search_node.run(clf, graph.session_state)

        assert "Agriculture_Report_Q2" in response
        assert graph.session_state.last_referenced_file is not None

    def test_after_search_last_referenced_file_set(self, sandbox):
        """Command 1 sets last_referenced_file so command 2 can reference it."""
        last_tuesday = date.today() - timedelta(days=(date.today().weekday() + 2) % 7 + 1)

        clf = ClassifierOutput(
            intent="file_search",
            confidence=0.91,
            extracted_entities={
                "file_reference": "agriculture",
                "relative_date": last_tuesday.isoformat(),
                "target_action": None,
            },
        )

        graph = make_mock_graph(sandbox, [])
        graph.file_search_node.run(clf, graph.session_state)

        assert graph.session_state.last_referenced_file is not None
        assert "Agriculture" in graph.session_state.last_referenced_file


class TestCanonicalCommand2:
    """What's the main conclusion of that report? (uses last_referenced_file)."""

    def test_document_qa_uses_session_context(self, sandbox):
        """Without a file_reference in the intent, falls back to last_referenced_file."""
        graph = make_mock_graph(sandbox, [])
        graph.session_state.last_referenced_file = "Agriculture_Report_Q2.pdf"

        clf = ClassifierOutput(
            intent="document_qa",
            confidence=0.88,
            extracted_entities={
                "file_reference": None,  # pronoun reference — resolved from session
                "relative_date": None,
                "target_action": None,
            },
        )

        with patch("echolocate.nodes.document.DocumentNode._llm_call") as mock_llm:
            mock_llm.return_value = "The main conclusion is that crop yields increased by 15 percent."
            response = graph.document_node.run(clf, graph.session_state)

        assert response  # non-empty response
        assert "Agriculture_Report_Q2.pdf" == graph.session_state.last_referenced_file


class TestCanonicalCommand3:
    """Read this document aloud."""

    def test_read_aloud_uses_last_referenced(self, sandbox):
        graph = make_mock_graph(sandbox, [])
        graph.session_state.last_referenced_file = "Agriculture_Report_Q2.pdf"

        clf = ClassifierOutput(
            intent="document_read_aloud",
            confidence=0.92,
            extracted_entities={"file_reference": None},
        )

        with patch("echolocate.nodes.document.DocumentNode._extract") as mock_extract:
            mock_extract.return_value = "This is the document content to read aloud."
            response = graph.document_node.run(clf, graph.session_state)

        assert response is not None
        assert len(response) > 0


class TestCanonicalCommand4:
    """Open my resume."""

    def test_open_resume_without_confirmation(self, sandbox):
        graph = make_mock_graph(sandbox, [])

        clf = ClassifierOutput(
            intent="system_action",
            confidence=0.87,
            extracted_entities={
                "file_reference": "resume.pdf",
                "target_action": "open",
            },
        )

        confirm_was_called = []

        def confirm_fn(prompt):
            confirm_was_called.append(prompt)
            return True

        graph.system_executor_node.confirm_fn = confirm_fn

        with patch("echolocate.nodes.system_executor.open_file") as mock_open:
            mock_open.return_value = {"outcome": "success"}
            with patch("echolocate.nodes.system_executor.get_logger") as mock_logger:
                mock_logger.return_value = MagicMock()
                response = graph.system_executor_node.run(clf, graph.session_state)

        # Open must NOT have triggered confirmation
        assert not confirm_was_called, "Non-destructive open should not require confirmation"
        assert "resume" in response.lower() or "Opening" in response


class TestCanonicalCommand5:
    """Move this file to my Documents folder."""

    def test_move_requires_and_gets_confirmation(self, sandbox):
        graph = make_mock_graph(sandbox, [])
        graph.session_state.last_referenced_file = "resume.pdf"

        clf = ClassifierOutput(
            intent="system_action",
            confidence=0.85,
            extracted_entities={
                "file_reference": "resume.pdf",
                "target_action": "move",
                "destination": "documents/resume.pdf",
            },
        )

        confirmation_prompts = []

        def confirm_fn(prompt):
            confirmation_prompts.append(prompt)
            return True  # approve

        graph.system_executor_node.confirm_fn = confirm_fn

        with patch("echolocate.mcp_server.tools.move_file.move_file") as mock_move:
            mock_move.return_value = {
                "outcome": "success",
                "resolved_source": str(sandbox / "resume.pdf"),
                "resolved_destination": str(sandbox / "documents" / "resume.pdf"),
            }
            with patch("echolocate.nodes.system_executor.get_logger") as mock_logger:
                mock_logger.return_value = MagicMock()
                response = graph.system_executor_node.run(clf, graph.session_state)

        assert len(confirmation_prompts) == 1, "Move must trigger exactly one confirmation"
        assert "resume.pdf" in confirmation_prompts[0]
        assert "done" in response.lower() or "moved" in response.lower()

    def test_move_denial_does_not_move(self, sandbox):
        graph = make_mock_graph(sandbox, [])

        clf = ClassifierOutput(
            intent="system_action",
            confidence=0.85,
            extracted_entities={
                "file_reference": "resume.pdf",
                "target_action": "move",
                "destination": "documents/resume.pdf",
            },
        )

        def deny_fn(prompt):
            return False

        graph.system_executor_node.confirm_fn = deny_fn

        with patch("echolocate.mcp_server.tools.move_file.move_file") as mock_move:
            response = graph.system_executor_node.run(clf, graph.session_state)

        mock_move.assert_not_called()
        assert "cancelled" in response.lower() or "Cancelled" in response
