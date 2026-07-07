from __future__ import annotations

from echolocate.graph import EchoLocateGraph
from echolocate.tts.synth import TTSSynthesizer
from echolocate.voice.barge_in import BargeInController


def test_speak_stream_speaks_sentences_and_returns_full_text():
    spoken: list[str] = []
    tts = TTSSynthesizer(engine="text")
    tts.speak = lambda text: spoken.append(text)  # type: ignore[method-assign]

    full = tts.speak_stream(iter(["Hello", " there. Second", " sentence!"]))

    assert full == "Hello there. Second sentence!"
    assert spoken == ["Hello there.", "Second sentence!"]


def test_graph_stream_response_is_saved_to_history(tmp_path):
    spoken_streams: list[str] = []

    graph = EchoLocateGraph(
        sandbox_root=tmp_path,
        router_model="test/mock",
        document_model="test/mock",
        system_model="test/mock",
        tts_fn=lambda text: None,
        tts_stream_fn=lambda iterator: spoken_streams.append("".join(iterator)) or spoken_streams[-1],
    )

    graph.router.route = lambda utterance, state: (  # type: ignore[method-assign]
        "document",
        type("Clf", (), {"intent": "document_qa"})(),
    )
    graph._dispatch = lambda destination, clf: iter(["A streamed ", "answer."])  # type: ignore[method-assign]

    response = graph.process_utterance("summarize it")

    assert response == "A streamed answer."
    assert graph.session_state.turn_history[-1] == {
        "role": "assistant",
        "content": "A streamed answer.",
    }


def test_barge_in_nested_tts_keeps_mic_muted_until_outer_end():
    barge_in = BargeInController()

    barge_in.start_tts()
    barge_in.start_tts()
    barge_in.end_tts()

    assert barge_in.is_muted

    barge_in.end_tts()

    assert not barge_in._muted.is_set()
