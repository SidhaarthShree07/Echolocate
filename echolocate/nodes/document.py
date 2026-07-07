"""
EchoLocate — document specialist node.

Handles three output modes: summarize, answer a specific question, read aloud.

Internal pipeline (Architecture Section 4.3):
  1. Resolve the target file from session state or current intent
  2. Extract text using the appropriate parser (pymupdf4llm for PDF,
     markitdown for DOCX/PPTX, direct read for TXT)
  3. Pass extracted Markdown to Gemma 4 E4B via LiteLLM/Ollama
  4. Return spoken-optimised response

EventsCompactionConfig equivalent: for large documents, the text is chunked
at the max_context_chars boundary. Older chunks are summarized with a
short running-summary that replaces them before the next chunk is added.
This prevents a 50-page PDF from overflowing the 32K context window.

The document node's MCP toolset has tool_filter=["read_file", "get_metadata"]
— no destructive tools visible to this node at all (Architecture Section 7.3).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator, Optional

from echolocate.nodes.fuzzy_resolver import FileResolution, describe_candidates, resolve_fuzzy_file
from echolocate.state import ClassifierOutput, PendingIntent, SessionState


class DocumentNode:
    """
    Document specialist: summarize / answer / read-aloud.
    """

    def __init__(
        self,
        sandbox_root: Path,
        llm_model: str = "ollama_chat/gemma4:e4b",
        max_context_chars: int = 48_000,
        compaction_trigger_chars: int = 36_000,
    ) -> None:
        self.sandbox_root = sandbox_root
        self.llm_model = llm_model
        self.max_context_chars = max_context_chars
        self.compaction_trigger_chars = compaction_trigger_chars

    def run(
        self,
        clf: ClassifierOutput,
        session_state: SessionState,
        on_chunk: Optional[callable] = None,
        stream: bool = False,
    ) -> str | Iterator[str]:
        """
        Process document intent and return spoken response.

        Args:
            clf: Classifier output with intent and entities.
            session_state: Current session state.
            on_chunk: Optional callback for streaming TTS (kept as hook).
            stream: Return a token iterator for generated summary/QA output.

        Returns:
            Spoken response string.
        """
        entities = clf.extracted_entities

        # Fast path: router already resolved a pending disambiguation
        # deterministically (yes/no confirmation or candidate selection).
        # Skip re-searching and go straight to reading the confirmed file.
        if entities.get("_confirmed_file"):
            file_ref = entities["file_reference"]
            session_state.last_referenced_file = file_ref
            file_path = self.sandbox_root / file_ref
            if not file_path.exists() or not file_path.is_file():
                return f"I can't find the file '{file_ref}'. Has it been moved?"
            text = self._extract(file_path)
            if not text:
                return self._handle_empty_extraction(file_path)
            session_state.add_turn("user", f"[Document request: {clf.intent}]")
            if clf.intent == "document_read_aloud":
                return self._read_aloud(text, file_path.name)
            elif clf.intent == "document_qa":
                question = entities.get("question") or entities.get("file_reference", "")
                if _is_direct_content_request(question):
                    return self._read_aloud(text, file_path.name)
                return self._answer_question(text, question, file_path.name, stream=stream)
            else:
                return self._summarize(text, file_path.name, stream=stream)

        # Resolve target file
        file_ref = clf.file_reference or session_state.last_referenced_file
        if not file_ref:
            return "I'm not sure which document you'd like me to look at. Which file?"

        # Extract file_type for filtered searching
        from echolocate.nodes.file_search import _extract_file_type_hint
        raw_file_type = entities.get("file_type")
        file_type = (_extract_file_type_hint(raw_file_type) if raw_file_type else None) or raw_file_type
        file_type = file_type or _extract_file_type_hint(file_ref) or _extract_file_type_hint(
            getattr(clf, "_raw_utterance", "") or ""
        )

        # Fuzzy-resolve via the shared, index-backed resolver (same matching
        # engine as FileSearchNode). This used to silently pick a single
        # best guess with no ambiguity handling at all -- meaning two files
        # that happen to share a name in different folders (very possible
        # once the sandbox root covers an entire drive) resolved to
        # whichever one the earlier version's search happened to see first,
        # with no way to ask "which one?" and no way to describe the two
        # candidates differently in an error message, since only the bare
        # name was ever tracked.
        location_hint = entities.get("location_hint")
        resolution = resolve_fuzzy_file(self.sandbox_root, file_ref, location_hint=location_hint, file_type=file_type)

        if resolution.status == "not_found":
            return f"I can't find a file matching '{file_ref}'. Can you describe it differently?"

        if resolution.status == "ambiguous":
            if len(resolution.candidates) == 1:
                match = resolution.candidates[0]
                from echolocate.nodes.file_search import _describe_location
                session_state.pending_intent = PendingIntent(
                    raw_utterance=getattr(clf, "_raw_utterance", "") or file_ref,
                    partial_entities={
                        "candidate_file": match["path"],
                        "candidate_name": match["name"],
                        "original_entities": dict(entities),
                    },
                    awaiting="file_confirmation",
                    original_intent=clf.intent,
                )
                return f"The closest match I found is {_describe_location(match['path'])}. Is that the one you meant?"

            labels = describe_candidates(resolution.candidates)
            label_list = ", ".join(f"'{c}'" for c in labels[:-1]) + f", or '{labels[-1]}'" if len(labels) > 1 else f"'{labels[0]}'"
            session_state.pending_intent = PendingIntent(
                raw_utterance=getattr(clf, "_raw_utterance", "") or file_ref,
                partial_entities={
                    "candidates": [c["path"] for c in resolution.candidates],
                    "original_entities": dict(entities),
                },
                awaiting="file_reference",
                original_intent=clf.intent,
            )
            return f"I found more than one possible match: {label_list}. Which one did you mean?"

        file_ref = resolution.path
        file_path = self.sandbox_root / file_ref
        if not file_path.exists() or not file_path.is_file():
            return f"I can't find the file '{file_ref}'. Has it been moved?"

        incomplete_caveat = (
            " (I'm still finishing indexing your files, so I might find a better match once that's done.)"
            if resolution.possibly_incomplete else ""
        )

        # Extract text
        text = self._extract(file_path)
        if not text:
            return self._handle_empty_extraction(file_path)

        # Update last referenced file
        session_state.last_referenced_file = file_ref
        session_state.add_turn("user", f"[Document request: {clf.intent}]")

        # Dispatch to output mode
        if clf.intent == "document_read_aloud":
            return self._read_aloud(text, file_path.name) + incomplete_caveat
        elif clf.intent == "document_qa":
            question = clf.extracted_entities.get("question") or clf.extracted_entities.get("file_reference", "")
            if _is_direct_content_request(question):
                return self._read_aloud(text, file_path.name) + incomplete_caveat
            result = self._answer_question(text, question, file_path.name, stream=stream)
            return _append_to_stream(result, incomplete_caveat) if stream else result + incomplete_caveat
        else:
            result = self._summarize(text, file_path.name, stream=stream)
            return _append_to_stream(result, incomplete_caveat) if stream else result + incomplete_caveat

    def _extract(self, file_path: Path) -> str:
        """Extract text from the file using the appropriate parser."""
        suffix = file_path.suffix.lower()
        try:
            if suffix == ".pdf":
                from echolocate.parsers.pdf_parser import extract_pdf, is_image_only_pdf
                if is_image_only_pdf(file_path):
                    return "__IMAGE_ONLY__"
                return extract_pdf(file_path)
            elif suffix in {".docx", ".pptx", ".txt", ".md"}:
                from echolocate.parsers.docx_parser import extract_document
                return extract_document(file_path)
            else:
                return ""
        except Exception as exc:
            print(f"[Document] extraction error: {exc}")
            return ""

    def _handle_empty_extraction(self, file_path: Path) -> str:
        """Generate appropriate spoken response for empty/unreadable files."""
        text = self._extract(file_path)
        if text == "__IMAGE_ONLY__":
            return (
                f"The file '{file_path.name}' appears to be a scanned image "
                f"without extractable text. I can't summarize its contents."
            )
        ext = file_path.suffix.lower()
        if ext == ".pdf":
            return f"I couldn't extract text from '{file_path.name}'. It may be a scanned image or corrupted."
        return f"The file '{file_path.name}' appears to be empty or unreadable."

    def _summarize(self, text: str, filename: str, stream: bool = False) -> str | Iterator[str]:
        """Summarize the document content."""
        chunked_text = self._compact_if_needed(text)
        fallback = _extractive_summary(text, filename)
        prompt = f"""You are summarizing a document for a visually impaired user who will hear this as audio.

Document: "{filename}"

Content:
{chunked_text}

Give a 3-5 sentence spoken summary. Prioritize what the document is about, key conclusions, and anything notable. 
Do NOT speak Markdown syntax (no ## headers, no ** bold). Write as natural spoken sentences.
If the content provided is a partial summary of a longer document, say so."""

        result = self._llm_call(prompt, max_tokens=1500, stream=stream)
        return _stream_with_fallback(result, fallback) if stream else _fallback_if_llm_failed(result, fallback)

    def _answer_question(self, text: str, question: str, filename: str, stream: bool = False) -> str | Iterator[str]:
        """Answer a specific question about the document."""
        if _is_summary_request(question):
            return self._summarize(text, filename, stream=stream)
        chunked_text = self._compact_if_needed(text)
        fallback = _extractive_answer(text, question, filename)
        prompt = f"""A user is asking about the document "{filename}".

Question: {question or "What is this document about?"}

Document content:
{chunked_text}

Answer the question in 1-2 spoken sentences. Answer ONLY from the document content. 
If the answer isn't in the document, say "That isn't covered in this document."
Do NOT speak Markdown syntax. Write as natural spoken sentences."""

        result = self._llm_call(prompt, max_tokens=1500, stream=stream)
        return _stream_with_fallback(result, fallback) if stream else _fallback_if_llm_failed(result, fallback)

    def _read_aloud(self, text: str, filename: str) -> str:
        """Format document for read-aloud delivery."""
        # Convert Markdown to spoken-friendly text
        spoken = _markdown_to_spoken(text)
        # Return chunked text — graph.py will call speak_chunked() on TTS
        # Truncate to a reasonable read-aloud length
        if len(spoken) > 5000:
            return spoken[:5000] + f" ... [End of readable excerpt from {filename}]"
        return spoken

    def _compact_if_needed(self, text: str) -> str:
        """
        If text exceeds compaction threshold, summarize early chunks and
        return compacted version. This is the EventsCompactionConfig
        equivalent — prevents 50-page PDFs from overflowing the context.
        """
        if len(text) <= self.compaction_trigger_chars:
            return text[:self.max_context_chars]

        # Split into chunks
        chunk1 = text[:self.compaction_trigger_chars]
        remainder = text[self.compaction_trigger_chars:]

        # Summarize first chunk
        summary_prompt = f"""Summarize the following text in 5-8 sentences for context:

{chunk1}

Output ONLY the summary, no preamble."""
        try:
            early_summary = self._llm_call(summary_prompt, max_tokens=1500)
        except Exception:
            early_summary = "[Earlier content summarized]"

        # Combine summary + remainder (truncated)
        combined = f"[Earlier content summarized:] {early_summary}\n\n[Continuing:]\n{remainder}"
        return combined[:self.max_context_chars]

    def _llm_call(self, prompt: str, max_tokens: int = 512, stream: bool = False) -> str | Iterator[str]:
        """Call the local Gemma 4 model via direct Ollama HTTP (bypasses LiteLLM)."""
        import urllib.request as _req
        import json as _json

        def _error_stream(message: str) -> Iterator[str]:
            yield message

        model_name = self.llm_model.replace("ollama_chat/", "").replace("ollama/", "")
        payload = _json.dumps({
            "model": model_name,
            "messages": [{"role": "user", "content": prompt}],
            "stream": stream,
            # keep_alive=-1 (never unload) permanently reserves this
            # model's VRAM against faster-whisper (CUDA) and Kokoro/
            # openWakeWord (ONNX runtime, also GPU) -- on a consumer GPU
            # running two Gemma 4 models plus STT plus TTS/wake-word
            # simultaneously, that's real, documented VRAM oversubscription,
            # and NVIDIA's default driver behavior on Windows silently
            # falls back to paging over PCIe instead of failing loudly,
            # which is what turns a normal STT call into a multi-minute
            # stall. keep_alive=0 (unload immediately) overcorrects the
            # other way: it forces a full model reload on EVERY call,
            # including the router->document handoff that happens within
            # a few seconds of the same turn. "2m" is a middle ground --
            # long enough to survive back-to-back calls within one turn
            # (including a quick yes/no follow-up), short enough to
            # release VRAM well before the next wake-word activation, which
            # is typically many seconds to minutes away. Tune based on your
            # actual turn-taking pace; watch `ollama ps` / `nvidia-smi`
            # while using it to see whether this is actually the balance
            # point for your hardware.
            "keep_alive": "2m",
            "options": {"temperature": 0.3, "num_predict": max_tokens},
        }).encode()

        request = _req.Request(
            "http://127.0.0.1:11434/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        if stream:
            def _iter_response() -> Iterator[str]:
                try:
                    with _req.urlopen(request, timeout=60) as resp:
                        for line in resp:
                            if not line:
                                continue
                            data = _json.loads(line.decode("utf-8"))
                            chunk = data.get("message", {}).get("content", "") or ""
                            if chunk:
                                yield chunk
                            if data.get("done"):
                                break
                except Exception as exc:
                    print(f"[Document] Ollama stream failed: {exc}")
                    yield "I had trouble processing that document. Please try again."

            return _iter_response()

        try:
            with _req.urlopen(request, timeout=60) as resp:
                data = _json.loads(resp.read())
                return (data.get("message", {}).get("content", "") or "").strip()
        except Exception as exc:
            print(f"[Document] Ollama call failed: {exc}")
            message = "I had trouble processing that document. Please try again."
            return _error_stream(message) if stream else message



def _markdown_to_spoken(text: str) -> str:
    """
    Convert Markdown to natural spoken text.
    Removes Markdown syntax that would be read literally as "hash hash" etc.
    """
    # Remove heading markers (## → prefix with newline instead)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    # Remove bold/italic
    text = re.sub(r"\*{1,2}([^*]+)\*{1,2}", r"\1", text)
    text = re.sub(r"_{1,2}([^_]+)_{1,2}", r"\1", text)
    # Remove inline code
    text = re.sub(r"`([^`]+)`", r"\1", text)
    # Remove links, keep text
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # Remove horizontal rules
    text = re.sub(r"^[-*_]{3,}$", "", text, flags=re.MULTILINE)
    # Remove table separators
    text = re.sub(r"\|[-:]+\|", "", text)
    # Clean up multiple blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _append_to_stream(result: str | Iterator[str], suffix: str) -> str | Iterator[str]:
    if not suffix:
        return result
    if isinstance(result, str):
        return result + suffix

    def _iter() -> Iterator[str]:
        yield from result
        yield suffix

    return _iter()


def _is_direct_content_request(question: str) -> bool:
    q = (question or "").lower()
    return bool(re.search(r"\b(retrieve|get|read|show|extract|keep)\b.*\b(content|contents|text)\b", q))


def _is_summary_request(question: str) -> bool:
    return bool(re.search(r"\b(summarize|summarized|summary|brief|overview)\b", (question or "").lower()))


def _fallback_if_llm_failed(result: str | Iterator[str], fallback: str) -> str | Iterator[str]:
    if isinstance(result, str) and _is_llm_error_text(result):
        return fallback
    return result


def _stream_with_fallback(result: str | Iterator[str], fallback: str) -> Iterator[str]:
    if isinstance(result, str):
        yield fallback if _is_llm_error_text(result) else result
        return

    emitted: list[str] = []
    for chunk in result:
        emitted.append(chunk)
        if len(emitted) == 1 and _is_llm_error_text(chunk):
            continue
        yield chunk

    full = "".join(emitted).strip()
    if _is_llm_error_text(full):
        yield fallback


def _is_llm_error_text(text: str) -> bool:
    lower = (text or "").lower()
    return "trouble processing" in lower or "please try again" in lower


def _extractive_summary(text: str, filename: str) -> str:
    sentences = _plain_sentences(text)
    if not sentences:
        return f"I could read {filename}, but there was not enough text to summarize."
    picked = sentences[:4]
    return f"Here is a quick summary of {filename}: " + " ".join(picked)


def _extractive_answer(text: str, question: str, filename: str) -> str:
    q_words = {
        w for w in re.findall(r"[a-z0-9]+", (question or "").lower())
        if len(w) > 3 and w not in {"what", "which", "that", "this", "file", "document", "about"}
    }
    sentences = _plain_sentences(text)
    if q_words:
        scored = []
        for sentence in sentences:
            s_words = set(re.findall(r"[a-z0-9]+", sentence.lower()))
            scored.append((len(q_words & s_words), sentence))
        best = [s for score, s in sorted(scored, reverse=True) if score > 0][:2]
        if best:
            return " ".join(best)
    return _extractive_summary(text, filename)


def _plain_sentences(text: str) -> list[str]:
    spoken = _markdown_to_spoken(text)
    parts = re.split(r"(?<=[.!?])\s+|\n+", spoken)
    return [p.strip() for p in parts if len(p.strip()) >= 12]
