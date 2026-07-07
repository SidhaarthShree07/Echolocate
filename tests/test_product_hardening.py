from __future__ import annotations

from echolocate.graph import EchoLocateGraph
from echolocate.main import load_model_config
from echolocate.nodes.document import DocumentNode
from echolocate.nodes.file_search import _local_search


def test_obvious_document_command_skips_router_llm(tmp_path):
    graph = EchoLocateGraph(sandbox_root=tmp_path)
    calls = []
    graph.router._ollama_chat = lambda prompt: calls.append(prompt) or ""  # type: ignore[method-assign]

    destination, clf = graph.router.route(
        "Take the file which is named as hello which is in the text format and retrieve the content from it.",
        graph.session_state,
    )

    assert calls == []
    assert destination == "document"
    assert clf.intent == "document_qa"
    assert clf.extracted_entities["file_reference"] == "hello"
    assert clf.extracted_entities["file_type"] == "txt"


def test_indirect_summary_command_uses_router_llm_when_available(tmp_path):
    graph = EchoLocateGraph(sandbox_root=tmp_path)
    calls = []
    graph.router._ollama_chat = lambda prompt: calls.append(prompt) or """
    {"intent": "document_qa", "confidence": 0.91,
     "extracted_entities": {"file_reference": "quiz master project", "file_type": "docx",
      "location_hint": null, "question": "Can you summarize it?",
      "relative_date": null, "target_action": null}}
    """  # type: ignore[method-assign]

    destination, clf = graph.router.route(
        "There is a quiz master project report file in docs format. Can you summarize it?",
        graph.session_state,
    )

    assert len(calls) == 1
    assert destination == "document"
    assert clf.intent == "document_qa"
    assert clf.extracted_entities["file_reference"] == "quiz master project"
    assert clf.extracted_entities["file_type"] == "docx"


def test_timeout_fallback_understands_single_turn_location_summary(tmp_path):
    graph = EchoLocateGraph(sandbox_root=tmp_path)
    graph.router._ollama_chat = lambda prompt: ""  # type: ignore[method-assign]

    destination, clf = graph.router.route(
        "The quiz master project report file in docs format summarized it. It is in the quiz master copy folder.",
        graph.session_state,
    )

    assert destination == "document"
    assert clf.intent == "document_qa"
    assert clf.extracted_entities["file_reference"] == "quiz master project"
    assert clf.extracted_entities["file_type"] == "docx"
    assert clf.extracted_entities["location_hint"] == "quiz master copy"


def test_exact_local_search_does_not_use_broad_walk(tmp_path, monkeypatch):
    (tmp_path / "Echolocate" / "sandbox_root").mkdir(parents=True)
    (tmp_path / "Echolocate" / "sandbox_root" / "hello.txt").write_text("hello", encoding="utf-8")

    def fail_walk(*args, **kwargs):
        raise AssertionError("broad walk should not be used for exact candidate")

    monkeypatch.setattr("echolocate.nodes.file_search._iter_files_bounded", fail_walk)

    results = _local_search(tmp_path, filename_fragment="hello", file_type="txt")

    assert results[0]["path"] == "Echolocate/sandbox_root/hello.txt"
    assert results[0]["match_score"] >= 100


def test_streaming_summary_falls_back_to_extractive_text(tmp_path):
    node = DocumentNode(tmp_path)
    node._llm_call = lambda *args, **kwargs: iter(["I had trouble processing that document. Please try again."])  # type: ignore[method-assign]

    result = node._summarize(
        "Quiz Master is a project report. It describes gameplay, scoring, and implementation. "
        "The conclusion says the app is ready for classroom demos.",
        "quiz-master.docx",
        stream=True,
    )

    spoken = "".join(result)

    assert "quick summary" in spoken.lower()
    assert "Quiz Master is a project report" in spoken


def test_constrained_model_override_uses_one_model_for_all_roles():
    config = load_model_config("constrained")

    assert config["router_model"].endswith("gemma4:e4b")
    assert config["document_model"].endswith("gemma4:e4b")
    assert config["system_model"].endswith("gemma4:e4b")
