"""
EchoLocate — intent router node.

This is the ADK graph workflow's entry point. It does two things:

1. CLASSIFY: Calls a Gemma 4 E2B LlmAgent to classify the transcribed
   utterance into a structured JSON intent object. The classifier output
   is NEVER trusted for routing on its own.

2. DISPATCH (deterministic): A pure Python function checks BOTH
   confidence >= threshold AND required entities present before routing
   to a specialist node. Either check failing → Clarification node.

Why two-stage verification (Architecture Section 4.2):
  - Confidence alone is insufficient: small models (even E2B) can report
    high confidence on wrong answers. The entity check catches "the model
    is certain, but has nothing to act on."
  - Entity check alone is insufficient: an entity could be hallucinated.
    Confidence catches "the model doesn't know."
  - Both failing together → clarification, never a guess.

Date resolution: relative_date is extracted by the classifier as a raw span,
then resolved to an ISO date string by dateparser (deterministic, zero-LLM)
before dispatch. The File Search node never sees "last Tuesday" — only
"2026-06-23". This keeps date math off the small model entirely.
"""
from __future__ import annotations

import json
import time
import re
from datetime import date
from typing import Any, Optional

from pathlib import Path
from echolocate.state import ClassifierOutput, PendingIntent, SessionState
from echolocate.nodes.file_search import _detect_root_hint

# Intent labels
INTENTS = {
    "file_search",
    "document_qa",
    "document_read_aloud",
    "system_action",
    "clarification_needed",
}

# Required entities per intent — AT LEAST ONE must be non-null/non-empty
REQUIRED_ENTITIES: dict[str, list[list[str]]] = {
    # file_search needs file_reference OR relative_date
    "file_search": [["file_reference"], ["relative_date"]],
    # document_qa needs a file reference (from current turn or session state)
    "document_qa": [["file_reference"]],
    # document_read_aloud needs a file reference
    "document_read_aloud": [["file_reference"]],
    # system_action needs both a file reference AND a target action
    "system_action": [["file_reference", "target_action"]],
}

VALID_SYSTEM_ACTIONS = {
    "open", "move", "rename", "delete",
    "open_folder", "launch",
}

CONFIDENCE_THRESHOLD_DEFAULT = 0.7

# Deterministic yes/no recognition for resolving a pending file-identity
# confirmation ("Found X. Is that the one you meant?") without a full LLM
# classification round-trip -- faster and more reliable than hoping the
# classifier's general-purpose schema correctly interprets a bare "yes".
_YES_WORDS = {"yes", "yeah", "yep", "yup", "correct", "right", "affirmative", "sure", "confirm", "confirmed"}
_NO_WORDS = {"no", "nope", "nah", "incorrect", "wrong", "negative"}


def _parse_yes_no(utterance: str) -> Optional[bool]:
    """Returns True/False for a short, unambiguous yes/no reply, or None if
    the utterance doesn't look like a bare yes/no -- in which case the
    caller should fall through to normal classification rather than guess,
    since a longer reply may carry new information a bare bool would drop."""
    normalized = utterance.strip().lower().strip(".!?")
    words = normalized.split()
    if not words or len(words) > 4:
        return None
    if normalized in _YES_WORDS or words[0] in _YES_WORDS:
        return True
    if normalized in _NO_WORDS or words[0] in _NO_WORDS:
        return False
    return None


_ORDINAL_WORDS = {"first": 0, "second": 1, "third": 2, "last": -1, "final": -1}


def _resolve_pending_candidate(utterance: str, candidates: list) -> Optional[str]:
    """
    Deterministically resolve a disambiguation REPLY ("the one in sandbox
    root", "the first one") against a small, already-known candidate path
    list -- WITHOUT a fresh LLM classification call.

    Combines folder token overlaps and exact stem/filename matches to rank
    candidates accurately and break ties, e.g. distinguishing hello.txt
    from test_hello.txt in the same directory.
    """
    if not candidates:
        return None

    from echolocate.nodes.file_search import _detect_root_hint
    t = utterance.lower()

    # "the one in the root directory / root folder / top level"
    if _detect_root_hint(t):
        root_level = [c for c in candidates if "/" not in c]
        if len(root_level) == 1:
            return root_level[0]

    # Ordinal reference: "the first one", "the last one"
    for word, idx in _ORDINAL_WORDS.items():
        if re.search(rf"\b{word}\b", t):
            try:
                return candidates[idx]
            except IndexError:
                pass

    # Score each candidate using folder token overlap and filename stem matching
    utt_tokens = set(re.findall(r"[a-z0-9]+", t))
    scores = []
    for c in candidates:
        stem_lower = Path(c).stem.lower()
        name_lower = Path(c).name.lower()
        
        # Folder token overlap
        folder_tokens = set(re.findall(r"[a-z0-9]+", str(Path(c).parent.as_posix()).lower()))
        score = len(folder_tokens & utt_tokens)
        
        # Add points if the exact stem is mentioned in the utterance using regex word boundary
        if re.search(rf"\b{re.escape(stem_lower)}\b", t):
            score += 5
            # Extra points for exact filename match (stem + extension or full name in utterance)
            if re.search(rf"\b{re.escape(name_lower)}\b", t) or name_lower in t:
                score += 2
                
        scores.append(score)

    best = max(scores) if scores else 0
    if best >= 1 and scores.count(best) == 1:
        return candidates[scores.index(best)]

    return None


class IntentRouter:
    """
    Two-stage intent router: LLM classifier + deterministic dispatch.

    The LLM classifier produces a structured JSON object. The dispatch
    function decides whether to route to a specialist or the clarification
    node — no LLM is involved in the routing decision itself.
    """

    def __init__(
        self,
        sandbox_root: Path | None = None,
        llm_model: str = "ollama_chat/gemma4:e2b",
        confidence_threshold: float = CONFIDENCE_THRESHOLD_DEFAULT,
    ) -> None:
        self.sandbox_root = sandbox_root or Path.cwd()
        self.llm_model = llm_model
        self.confidence_threshold = confidence_threshold

    def classify(
        self,
        utterance: str,
        session_state: SessionState,
    ) -> ClassifierOutput:
        """
        Classify the utterance using the local Gemma 4 E2B model via direct
        Ollama HTTP call (bypasses LiteLLM to avoid empty-response bug on Windows).

        Returns a ClassifierOutput with intent, confidence, and entities.
        Relative dates are resolved to ISO strings by dateparser before
        returning — the downstream nodes never see raw relative date spans.
        """
        prompt = self._build_classification_prompt(utterance, session_state)
        _t0 = time.time()
        raw = self._ollama_chat(prompt)
        self._last_raw_utterance = utterance
        if not raw.strip():
            return self._fallback_classify(utterance, session_state)
        parsed = self._parse_classifier_output(raw, session_state)
        if parsed.intent == "clarification_needed" and parsed.confidence == 0.0:
            return self._fallback_classify(utterance, session_state)
        return parsed

    def _ollama_chat(self, prompt: str, model: str | None = None) -> str:
        """
        Call Ollama /api/chat directly via urllib. Returns the content string,
        or "" on any error. This bypasses LiteLLM entirely.
        """
        import urllib.request
        import urllib.error

        model_name = (model or self.llm_model).replace("ollama_chat/", "").replace("ollama/", "")
        payload = json.dumps({
            "model": model_name,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            # See document.py's _llm_call for the keep_alive reasoning.
            "keep_alive": "2m",
            "options": {
                "temperature": 0.1,
                # Reverted from 200 back to 1024: Gemma 4 runs a built-in
                # "thinking" pass by default (a <|think|>-token-triggered
                # internal reasoning block Ollama inserts automatically),
                # which can run to hundreds of tokens even for a
                # classification task that looks simple from the outside.
                # 200 cut generation off mid-thought, before the model ever
                # reached the actual JSON -- confirmed empirically (200 ->
                # no answer at all, 1024 -> works). No stop sequence here
                # either now, for the same reason: a "}" appearing inside
                # the reasoning text, before the real JSON, would trigger
                # a premature stop on the wrong brace.
                "num_predict": 1024,
            },
        }).encode()

        req = urllib.request.Request(
            "http://127.0.0.1:11434/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read())
                if data.get("done_reason") == "length":
                    print("[Router] Warning: Ollama truncated response (hit token limit)")
                return data.get("message", {}).get("content", "") or ""
        except Exception as exc:
            print(f"[Router] Ollama call failed: {exc}")
            return ""



    def route(
        self,
        utterance: str,
        session_state: SessionState,
    ) -> tuple[str, ClassifierOutput]:
        """
        Classify and route. Returns (destination, classifier_output).

        destination is one of:
          "file_search", "document", "system_executor", "clarification"
        """
        original_utterance = utterance
        pi = session_state.pending_intent

        # Fast path: resolve a pending file-identity confirmation
        # deterministically. Also closes the bug where FileSearchNode used
        # to commit last_referenced_file to a GUESS before the user had
        # confirmed it -- the candidate is now stashed here via
        # pending_intent instead of committed directly (see file_search.py).
        if pi and pi.awaiting == "file_confirmation":
            answer = _parse_yes_no(utterance)
            if answer is not None:
                candidate = (pi.partial_entities or {}).get("candidate_file")
                session_state.pending_intent = None
                if answer:
                    clf = ClassifierOutput(
                        intent=pi.original_intent or "file_search", confidence=1.0,
                        extracted_entities={"file_reference": candidate, "_confirmed_file": True},
                    )
                else:
                    clf = ClassifierOutput(
                        intent=pi.original_intent or "file_search", confidence=1.0,
                        extracted_entities={"file_reference": None, "_rejected_file": candidate},
                    )
                clf._raw_utterance = original_utterance
                session_state.last_classifier_output = clf
                return self._dispatch(clf, session_state), clf
            # Not a recognizable bare yes/no (e.g. user named a different
            # file outright) -- fall through, pending context stays intact.

        # Fast path: resolve a pending multi-candidate disambiguation reply
        # ("the one in sandbox root", "the first one") deterministically.
        # This is the fix for turns that used to burn a full 12-15s
        # classify() call trying to interpret a disambiguation ANSWER as a
        # fresh command, and often failed anyway (STT garbling "sandbox
        # root" into "Sandberg through" made it worse, but even a clean
        # utterance was asking the wrong tool for this specific job).
        if pi and pi.awaiting == "file_reference":
            candidates = (pi.partial_entities or {}).get("candidates")
            if candidates:
                matched = _resolve_pending_candidate(utterance, candidates)
                if matched:
                    entities = dict((pi.partial_entities or {}).get("original_entities") or {})
                    entities.update({"file_reference": matched, "_confirmed_file": True})
                    session_state.pending_intent = None
                    clf = ClassifierOutput(
                        intent=pi.original_intent or "file_search", confidence=1.0,
                        extracted_entities=entities,
                    )
                    clf._raw_utterance = original_utterance
                    session_state.last_classifier_output = clf
                    return self._dispatch(clf, session_state), clf
                # No confident deterministic match -- fall through to the
                # classifier with enriched context (candidates are already
                # surfaced there via _enrich_with_pending), rather than
                # silently failing with no attempt at all.

        if session_state.pending_intent and session_state.pending_intent.awaiting:
            enriched = self._enrich_with_pending(utterance, session_state)
            if enriched:
                utterance = enriched

        fast_clf = self._fast_classify_if_obvious(utterance, session_state)
        if fast_clf:
            fast_clf._raw_utterance = original_utterance
            session_state.last_classifier_output = fast_clf
            return self._dispatch(fast_clf, session_state), fast_clf

        clf = self.classify(utterance, session_state)
        clf._raw_utterance = original_utterance
        session_state.last_classifier_output = clf

        destination = self._dispatch(clf, session_state)
        return destination, clf

    def _fast_classify_if_obvious(
        self,
        utterance: str,
        session_state: SessionState,
    ) -> Optional[ClassifierOutput]:
        """
        Deterministic first pass for common voice commands. This keeps the
        hot path under a few hundred milliseconds instead of waiting for a
        router LLM that may be busy with document generation.
        """
        lower = utterance.lower()
        direct_content = bool(re.search(
            r"\b(retrieve|get|read|show|extract|keep)\b.*\b(content|contents|text|from it|from the file)\b",
            lower,
        ))
        explicit_name = bool(re.search(r"\b(named|called|name is|named as)\b", lower))
        explicit_type = bool(re.search(r"\b(txt|text|pdf|docx|docs|doc|markdown|md)\b", lower))
        plain_find = bool(re.search(r"\b(find|locate|search|look for)\b", lower))

        if not ((direct_content and (explicit_name or explicit_type)) or (plain_find and explicit_name and explicit_type)):
            return None

        clf = self._fallback_classify(utterance, session_state)
        if clf.intent == "clarification_needed" or clf.confidence < self.confidence_threshold:
            return None
        if not self._entities_satisfied(clf, session_state):
            return None
        print(f"[Router] fast deterministic route: intent={clf.intent}, confidence={clf.confidence:.2f}")
        return clf

    def _dispatch(
        self,
        clf: ClassifierOutput,
        session_state: SessionState,
    ) -> str:
        """
        Pure deterministic routing function. Never calls LLM.

        Returns one of: "file_search", "document", "system_executor", "clarification"
        """
        if clf.intent == "clarification_needed":
            return "clarification"

        if clf.intent not in INTENTS:
            return "clarification"

        # Stage 1: confidence check
        if clf.confidence < self.confidence_threshold:
            return "clarification"

        # Stage 2: required entity presence check
        if not self._entities_satisfied(clf, session_state):
            return "clarification"

        # Route to specialist
        if clf.intent == "file_search":
            return "file_search"
        elif clf.intent in {"document_qa", "document_read_aloud"}:
            return "document"
        elif clf.intent == "system_action":
            return "system_executor"

        return "clarification"

    def _entities_satisfied(
        self,
        clf: ClassifierOutput,
        session_state: SessionState,
    ) -> bool:
        """
        Check that at least one required-entity group for this intent is
        fully populated. For file/document intents, last_referenced_file in
        session state counts as a valid file_reference.
        """
        intent = clf.intent
        entity_groups = REQUIRED_ENTITIES.get(intent)
        if not entity_groups:
            return True  # no entity requirements for this intent

        entities = dict(clf.extracted_entities)

        # Pronoun/implicit file resolution: "it", "that", "this"
        if not entities.get("file_reference") and session_state.last_referenced_file:
            # Implicit reference is acceptable for document/system intents
            entities["file_reference"] = session_state.last_referenced_file

        # Check that at least one entity group is fully satisfied
        for group in entity_groups:
            if all(entities.get(key) for key in group):
                return True

        return False

    def _enrich_with_pending(
        self,
        utterance: str,
        session_state: SessionState,
    ) -> Optional[str]:
        """
        When in a clarification loop, prepend the pending context so the
        classifier sees: "Previously I wanted to X, and now the user says Y."
        """
        pi = session_state.pending_intent
        if not pi:
            return None

        extra = ""
        candidates = (pi.partial_entities or {}).get("candidates")
        if candidates:
            candidate_names = ", ".join(str(c) for c in candidates)
            extra = f" The candidates offered were: {candidate_names}."

        enriched = (
            f"[CONTEXT: Previously the user said: '{pi.raw_utterance}'. "
            f"We were waiting for: '{pi.awaiting}'.{extra} "
            f"The user's response is:] {utterance}"
        )
        return enriched

    def _build_classification_prompt(
        self,
        utterance: str,
        session_state: SessionState,
    ) -> str:
        context = ""
        if session_state.last_referenced_file:
            context = f"\nContext: The user recently referenced file: '{session_state.last_referenced_file}'"
        if session_state.turn_history:
            recent = session_state.turn_history[-6:]
            lines = []
            for turn in recent:
                role = turn.get("role", "unknown")
                content = str(turn.get("content", ""))[:500]
                lines.append(f"- {role}: {content}")
            context += "\nRecent conversation:\n" + "\n".join(lines)

        today = date.today().isoformat()

        return f"""You are an intent classifier for an accessibility voice agent. 
Classify the user's utterance into exactly one intent and extract entities.
Today's date is: {today}{context}

User utterance: "{utterance}"

First, think step-by-step about the user's intent and entities inside <|channel>thought...\\n<channel|> tags.
Then, output ONLY a valid JSON object with this exact structure. Treat this
as a tool plan: choose the next tool/node and provide the arguments it needs.

{{
  "intent": "<one of: file_search | document_qa | document_read_aloud | system_action | clarification_needed>",
  "confidence": <float 0.0-1.0>,
  "extracted_entities": {{
    "file_reference": "<filename, description, or null>",
    "file_type": "<file extension without a dot if the user mentioned or implied one, e.g. 'pdf', 'txt', 'docx' — or null>",
    "location_hint": "<'root' if the user said root/root directory/root folder/top level, OR a specific folder name they mentioned, OR null>",
    "question": "<specific question asked about the document content, or null>",
    "relative_date": "<relative date span exactly as spoken, e.g. 'last Tuesday', or null>",
    "target_action": "<one of: open | move | rename | delete | open_folder | launch, or null>"
  }}
}}

Rules:
- Use "file_search" when finding/locating files only.
- Use "document_qa" when asking about document content, including commands like "find the file and retrieve/read/get the content from it."
- If the user asks to summarize, summarize it, summarized it, explain, read, retrieve content, or answer a question about a named file, choose "document_qa" or "document_read_aloud", not "file_search".
- If the user gives a folder in the same command, extract it as location_hint, for example "in the quiz master copy folder" -> "quiz master copy".
- Use "document_read_aloud" when requesting narration.
- Use "system_action" when performing file operations.
- Use "clarification_needed" when the intent is unclear.
- Voice/Audio smoothing: Account for spoken phonetic artifacts (e.g., if the user says "in the docks folder", map it to "docs", or "a text file" to file_type="txt").
- Resolve follow-up turns against the recent conversation. If the user says "it", "that", "that file", "read it", or "summarize it", use the recent file context.
- Carefully extract filenames even if they sound like common words or greetings (e.g., "welcome file" -> file_reference="welcome").
- Extract file_type whenever the user names or implies a format (e.g. "PDF report", "in TXT format") — this must be separated from the file_reference.
- Extract location_hint whenever the user specifies WHERE the file is (e.g. "in the root directory" -> "root", "in my Documents folder" -> "Documents") — null if no location was mentioned.
- Extract relative_date as the EXACT spoken phrase (e.g. "last Tuesday") — do not resolve it to a date yourself.
- Set confidence honestly: 0.9+ only when very certain, 0.7-0.9 for likely, below 0.7 when unsure.

Example Interaction:
User utterance: "Can you read aloud the quarterly report pdf in my downloads from last week?"
<|channel>thought
The user wants me to read a file aloud. Intent: document_read_aloud. 
Target file is "quarterly report". Format is "pdf". 
Location is "downloads". Timeframe is "last week". 
No specific question or system action is requested.
<channel|>
{{
  "intent": "document_read_aloud",
  "confidence": 0.95,
  "extracted_entities": {{
    "file_reference": "quarterly report",
    "file_type": "pdf",
    "location_hint": "downloads",
    "question": null,
    "relative_date": "last week",
    "target_action": null
  }}
}}"""

    def _parse_classifier_output(
        self,
        raw: str,
        session_state: SessionState,
    ) -> ClassifierOutput:
        """Parse the LLM's JSON response into a ClassifierOutput."""
        # Extract JSON block from the response
        json_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not json_match:
            return ClassifierOutput(intent="clarification_needed", confidence=0.0)

        try:
            data = json.loads(json_match.group())
        except json.JSONDecodeError:
            return ClassifierOutput(intent="clarification_needed", confidence=0.0)

        intent = data.get("intent", "clarification_needed")
        if intent not in INTENTS:
            intent = "clarification_needed"

        confidence = float(data.get("confidence", 0.0))
        entities = data.get("extracted_entities", {})

        # If a question was extracted, this is a QA task, even if the LLM
        # got distracted by the word 'find' and guessed file_search.
        raw_lower = getattr(self, "_last_raw_utterance", "").lower()
        content_requested = bool(re.search(r"\b(retrieve|get|read|tell me|show|extract|keep)\b.*\b(content|contents|text|from it|from the file)\b", raw_lower))
        if (entities.get("question") or content_requested) and intent == "file_search":
            intent = "document_qa"
            entities["question"] = entities.get("question") or "Retrieve the content from this file."

        # Resolve relative_date to ISO string (deterministic, zero-LLM)
        raw_date = entities.get("relative_date")
        if raw_date:
            entities["relative_date"] = _resolve_relative_date(raw_date)

        # Normalize file_type (strip leading dot, lowercase) if extracted.
        raw_type = entities.get("file_type")
        if raw_type:
            entities["file_type"] = str(raw_type).strip().lstrip(".").lower() or None

        return ClassifierOutput(
            intent=intent,
            confidence=confidence,
            extracted_entities=entities,
        )

    def _fallback_classify(
        self,
        utterance: str,
        session_state: SessionState,
    ) -> ClassifierOutput:
        """
        Lightweight deterministic fallback used when the local router model
        times out or returns malformed JSON. It keeps obvious voice commands
        moving instead of collapsing into a generic clarification.
        """
        text = utterance.strip()
        lower = text.lower()
        entities: dict[str, Any] = {
            "file_reference": None,
            "file_type": None,
            "location_hint": None,
            "question": None,
            "relative_date": None,
            "target_action": None,
        }

        type_match = re.search(r"\b(?:txt|text|pdf|docx|docs|doc|markdown|md)\b", lower)
        if type_match:
            token = type_match.group(0)
            entities["file_type"] = {"text": "txt", "markdown": "md", "docs": "docx", "doc": "docx"}.get(token, token)

        location_matches = re.findall(r"\bin\s+(?:the\s+)?([a-z0-9\s_-]+?)\s+folder\b", lower)
        if location_matches:
            location_raw = location_matches[-1]
            if " in " in location_raw:
                location_raw = location_raw.split(" in ")[-1]
            location = location_raw.strip(" '\".,?!")
            location = re.sub(r"\b(?:a|an|the|please)\b", " ", location)
            location = re.sub(r"\s+", " ", location).strip()
            if location:
                entities["location_hint"] = location

        if _detect_root_hint(lower):
            entities["location_hint"] = "root"

        action_patterns = {
            "delete": r"\b(delete|remove)\b",
            "move": r"\b(move)\b",
            "rename": r"\b(rename)\b",
            "open_folder": r"\b(open folder|show folder|open containing folder)\b",
            "open": r"\b(open|launch)\b",
        }
        for action, pattern in action_patterns.items():
            if re.search(pattern, lower):
                entities["target_action"] = action
                break

        read_aloud = bool(re.search(r"\b(read aloud|speak|narrate)\b", lower))
        content_requested = bool(re.search(r"\b(retrieve|get|read|tell me|show|extract|keep)\b.*\b(content|contents|text|from it|from the file)\b", lower))
        qa_or_summary = bool(re.search(r"\b(summarize|summarized|summary|what|why|how|tell me|explain)\b", lower)) or content_requested
        find_file = bool(re.search(r"\b(find|locate|search|where is|look for)\b", lower))

        file_ref = self._extract_fallback_file_reference(lower)
        if file_ref:
            entities["file_reference"] = file_ref
        elif session_state.last_referenced_file and re.search(r"\b(it|that|this|that file|this file|the file)\b", lower):
            entities["file_reference"] = session_state.last_referenced_file

        if entities["target_action"] and (entities["file_reference"] or session_state.last_referenced_file):
            intent = "system_action"
            confidence = 0.78
        elif read_aloud:
            intent = "document_read_aloud"
            confidence = 0.78
        elif qa_or_summary and (entities["file_reference"] or session_state.last_referenced_file):
            intent = "document_qa"
            entities["question"] = "Retrieve the content from this file." if content_requested else text
            confidence = 0.75
        elif find_file or entities["file_type"] or entities["file_reference"]:
            intent = "file_search"
            confidence = 0.75
        else:
            intent = "clarification_needed"
            confidence = 0.0

        print(f"[Router] using deterministic fallback classifier: intent={intent}, confidence={confidence:.2f}")
        return ClassifierOutput(intent=intent, confidence=confidence, extracted_entities=entities)

    def _extract_fallback_file_reference(self, lower: str) -> Optional[str]:
        patterns = [
            r"\b(?:called|named)\s+(.+?)(?:\s+(?:file|document))?(?:\s+(?:which|that|in|with|from)\b|$)",
            r"\b(?:find|locate|search for|look for|open|read|summarize|take)\s+(?:the\s+)?(.+?)(?:\s+(?:file|document))?(?:\s+(?:which|that|in|with|from)\b|$)",
            r"\bthere is\s+(?:a\s+|an\s+|the\s+)?(.+?)\s+(?:file|document)(?:\s+(?:which|that|in|with|from|can)\b|$)",
            r"^(?:the\s+)?(.+?)\s+(?:file|document)(?:\s+(?:which|that|in|with|from|can|summarized|summarize)\b|$)",
        ]
        for pattern in patterns:
            match = re.search(pattern, lower)
            if not match:
                continue
            ref = match.group(1).strip(" '\".,?!")
            ref = re.sub(r"\b(?:a|an|as|the|me|please|text|txt|pdf|docx|docs|doc|format|file|document|report)\b", " ", ref)
            ref = re.sub(r"\s+", " ", ref).strip()
            if ref and ref not in {"it", "that", "this"}:
                return ref
        return None


def _resolve_relative_date(raw_span: str) -> Optional[str]:
    """
    Convert a relative date span (e.g. "last Tuesday") to an ISO date string.
    Uses dateparser — deterministic, zero-LLM.
    Returns None if parsing fails.
    """
    if not raw_span:
        return None
    try:
        import dateparser  # type: ignore
        parsed = dateparser.parse(
            raw_span,
            settings={"PREFER_DATES_FROM": "past", "RETURN_AS_TIMEZONE_AWARE": False},
        )
        if parsed:
            return parsed.date().isoformat()
    except Exception:
        pass
    return None
