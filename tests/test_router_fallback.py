from __future__ import annotations

from echolocate.nodes.router import IntentRouter
from echolocate.state import SessionState


def test_router_timeout_fallback_extracts_text_file_request(tmp_path):
    router = IntentRouter(sandbox_root=tmp_path, llm_model="test/mock")
    router._ollama_chat = lambda prompt: ""  # type: ignore[method-assign]

    clf = router.classify(
        "Can you find meel hello file which is in text format and keep the content from it?",
        SessionState(),
    )

    assert clf.intent == "document_qa"
    assert clf.confidence >= 0.7
    assert clf.extracted_entities["file_type"] == "txt"
    assert clf.extracted_entities["file_reference"] == "meel hello"
    assert clf.extracted_entities["question"] == "Retrieve the content from this file."


def test_router_prompt_includes_recent_context(tmp_path):
    router = IntentRouter(sandbox_root=tmp_path, llm_model="test/mock")
    session = SessionState()
    session.last_referenced_file = "hello.txt"
    session.add_turn("user", "find hello text file")
    session.add_turn("assistant", "I found hello.txt.")

    prompt = router._build_classification_prompt("summarize it", session)

    assert "Recent conversation" in prompt
    assert "hello.txt" in prompt
    assert "Resolve follow-up turns" in prompt
