from __future__ import annotations

from echolocate.graph import EchoLocateGraph
from echolocate.state import SessionState


def test_retrieve_content_followup_preserves_original_request(tmp_path):
    (tmp_path / "Echolocate" / "sandbox_root").mkdir(parents=True)
    (tmp_path / "Echolocate" / "requirements-windows.txt").write_text("not the target", encoding="utf-8")
    (tmp_path / "hello.txt").write_text("Root hello.", encoding="utf-8")
    (tmp_path / "Echolocate" / "sandbox_root" / "hello.txt").write_text("Hello content.", encoding="utf-8")

    spoken: list[str] = []
    graph = EchoLocateGraph(
        sandbox_root=tmp_path,
        router_model="test/mock",
        document_model="test/mock",
        system_model="test/mock",
        tts_fn=spoken.append,
        tts_chunked_fn=spoken.append,
        tts_stream_fn=lambda iterator: spoken.append("".join(iterator)) or spoken[-1],
    )
    graph.router._ollama_chat = lambda prompt: ""  # type: ignore[method-assign]
    graph.document_node._llm_call = lambda *args, **kwargs: "LLM should not be used."  # type: ignore[method-assign]

    first_response = graph.process_utterance(
        "Find me hello file which is in text format and retrieve the content from it."
    )

    assert graph.session_state.pending_intent is not None
    assert "Which one" in first_response

    second_response = graph.process_utterance("the one in the sandbox room folder.")

    assert second_response == "Hello content."
    assert graph.session_state.last_referenced_file == "Echolocate/sandbox_root/hello.txt"


def test_fallback_file_reference_drops_find_me_filler(tmp_path):
    router = EchoLocateGraph(sandbox_root=tmp_path).router
    clf = router._fallback_classify(
        "Find me hello file which is in text format and retrieve the content from it.",
        SessionState(),
    )

    assert clf.extracted_entities["file_reference"] == "hello"
