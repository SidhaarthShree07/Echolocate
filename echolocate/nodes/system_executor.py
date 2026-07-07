"""
EchoLocate — system executor specialist node.

Handles filesystem and application actions via the MCP toolset.
The CRITICAL design property: every destructive action (move, rename, delete)
requires explicit spoken confirmation via ToolConfirmation BEFORE the MCP
tool is called.

Architecture Section 4.3 + FR-12, FR-13, FR-14:
  - Non-destructive (open, open folder): call MCP tool directly, confirm
    verbally after.
  - Destructive (move, rename, delete): announce what will happen, wait for
    "yes", then call MCP tool, then confirm after. On denial: "Cancelled,
    nothing was changed" — never a silent no-op.

The MCP toolset for this node has tool_filter=["open_file", "move_file",
"delete_file"] — search_files and read_file are not in scope here (defense
in depth: even a prompt-injected command can't read documents via this node).
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from echolocate.mcp_server.audit import get_logger
from echolocate.mcp_server.tools.open_file import open_file
from echolocate.nodes.fuzzy_resolver import resolve_fuzzy_file
from echolocate.state import ClassifierOutput, PendingIntent, SessionState


class SystemExecutorNode:
    """
    System action specialist: open, move, rename, delete.
    """

    DESTRUCTIVE_ACTIONS = {"move", "rename", "delete"}
    NON_DESTRUCTIVE_ACTIONS = {"open", "open_folder", "launch"}

    def __init__(
        self,
        sandbox_root: Path,
        confirm_fn: Optional[Callable[[str], bool]] = None,
    ) -> None:
        """
        Args:
            sandbox_root: Absolute path to the sandbox directory.
            confirm_fn: Callable that speaks a prompt and returns True/False.
                        If None, a default text-based confirmation is used.
                        In the full voice pipeline, this calls TTS + STT.
        """
        self.sandbox_root = sandbox_root
        self.confirm_fn = confirm_fn or _default_confirm

    def run(
        self,
        clf: ClassifierOutput,
        session_state: SessionState,
    ) -> str:
        """
        Execute the system action.

        Returns:
            Spoken response string for TTS.
        """
        entities = clf.extracted_entities
        target_action = entities.get("target_action", "").lower()

        # Resolve file reference (may be pronoun)
        raw_ref = session_state.resolve_file_reference(entities.get("file_reference"))
        if not raw_ref:
            return "Which file would you like me to act on?"

        # Fuzzy-resolve via the same index-backed search FileSearchNode uses
        # — same gap this had as document.py: resolve_and_check() requires
        # an exact existing path, so "delete the hello file" would fail
        # outright unless raw_ref happened to be the literal filename. The
        # mandatory spoken confirmation for destructive actions (below)
        # names the resolved file explicitly, which is what makes it safe
        # to auto-resolve here rather than requiring an exact match: the
        # user hears exactly what will be acted on before anything happens.
        resolution = resolve_fuzzy_file(self.sandbox_root, raw_ref, location_hint=entities.get("location_hint"))

        if resolution.status == "not_found":
            return f"I can't find a file matching '{raw_ref}'."

        if resolution.status == "ambiguous":
            from echolocate.nodes.fuzzy_resolver import describe_candidates
            names = ", ".join(f"'{c}'" for c in describe_candidates(resolution.candidates))
            session_state.pending_intent = PendingIntent(
                raw_utterance=getattr(clf, "_raw_utterance", "") or raw_ref,
                partial_entities={"candidates": [c["path"] for c in resolution.candidates]},
                awaiting="file_reference",
                original_intent=clf.intent,
            )
            return f"I found a few possible matches: {names}. Which one did you mean?"

        file_ref = resolution.path

        # Validate target action
        all_actions = self.DESTRUCTIVE_ACTIONS | self.NON_DESTRUCTIVE_ACTIONS
        if target_action not in all_actions:
            return f"I'm not sure what action to take. I can open, move, rename, or delete files."

        # Non-destructive path
        if target_action in self.NON_DESTRUCTIVE_ACTIONS:
            return self._run_open(file_ref, session_state)

        # Destructive path — confirmation required
        return self._run_destructive(
            action=target_action,
            file_ref=file_ref,
            entities=entities,
            session_state=session_state,
        )

    def _run_open(self, file_ref: str, session_state: SessionState) -> str:
        """Open file without confirmation."""
        logger = get_logger()
        result = open_file(self.sandbox_root, file_ref)

        logger.log(
            tool="open_file",
            args={"path": file_ref},
            session_id=session_state.session_id,
            destructive=False,
            confirmation_required=False,
            outcome=result.get("outcome", "success"),
            error=result.get("error"),
        )

        if result.get("outcome") == "success":
            name = Path(file_ref).name
            session_state.last_referenced_file = file_ref
            return f"Opening {name}."
        else:
            return f"I couldn't open that file: {result.get('error', 'unknown error')}."

    def _run_destructive(
        self,
        action: str,
        file_ref: str,
        entities: dict,
        session_state: SessionState,
    ) -> str:
        """Handle a destructive action with mandatory confirmation gate."""
        file_name = Path(file_ref).name

        # Build the spoken confirmation prompt
        if action == "move":
            destination = entities.get("destination") or entities.get("target_action_detail")
            if not destination:
                return "Where would you like to move this file? Please specify the destination folder."
            confirm_prompt = f"Move {file_name} to {destination}? Say yes to confirm."
        elif action == "rename":
            new_name = entities.get("new_name") or entities.get("target_action_detail")
            if not new_name:
                return "What would you like to rename this file to?"
            confirm_prompt = f"Rename {file_name} to {new_name}? Say yes to confirm."
        elif action == "delete":
            confirm_prompt = (
                f"Delete {file_name}? This can't be undone. Say yes to confirm."
            )
        else:
            return f"I don't know how to {action} files."

        # Store pending confirmation in session state
        session_state.active_confirmation = {
            "action": action,
            "file_ref": file_ref,
            "entities": entities,
            "prompt": confirm_prompt,
        }

        # Call confirmation function (speaks the prompt + waits for answer)
        confirmed = self.confirm_fn(confirm_prompt)

        # Clear confirmation state
        session_state.active_confirmation = None

        if not confirmed:
            return "Cancelled, nothing was changed."

        # Execute the confirmed action
        return self._execute_confirmed(action, file_ref, entities, session_state)

    def _execute_confirmed(
        self,
        action: str,
        file_ref: str,
        entities: dict,
        session_state: SessionState,
    ) -> str:
        """Execute a destructive action after confirmed approval."""
        logger = get_logger()

        try:
            if action == "delete":
                from echolocate.mcp_server.tools.delete_file import delete_file
                result = delete_file(
                    self.sandbox_root,
                    file_ref,
                    session_id=session_state.session_id,
                    confirmation_result="confirmed",
                )
                logger.log(
                    tool="delete_file",
                    args={"path": file_ref},
                    session_id=session_state.session_id,
                    destructive=True,
                    confirmation_required=True,
                    confirmation_result="confirmed",
                    outcome=result.get("outcome", "success"),
                    resolved_source_path=result.get("resolved_path"),
                )
                if result.get("outcome") == "success":
                    if session_state.last_referenced_file == file_ref:
                        session_state.last_referenced_file = None
                    return f"Done — {Path(file_ref).name} has been deleted."
                return f"I couldn't delete the file: {result.get('error', 'unknown error')}"

            elif action == "move":
                destination = entities.get("destination") or entities.get("target_action_detail", "")
                from echolocate.mcp_server.tools.move_file import move_file
                result = move_file(
                    self.sandbox_root,
                    file_ref,
                    destination,
                    session_id=session_state.session_id,
                    confirmation_result="confirmed",
                )
                logger.log(
                    tool="move_file",
                    args={"source": file_ref, "destination": destination},
                    session_id=session_state.session_id,
                    destructive=True,
                    confirmation_required=True,
                    confirmation_result="confirmed",
                    outcome=result.get("outcome", "success"),
                    resolved_source_path=result.get("resolved_source"),
                    resolved_dest_path=result.get("resolved_destination"),
                )
                if result.get("outcome") == "success":
                    session_state.last_referenced_file = destination
                    return f"Done — moved to {destination}."
                return f"I couldn't move the file: {result.get('error', 'unknown error')}"

            elif action == "rename":
                new_name = entities.get("new_name") or entities.get("target_action_detail", "")
                parent = str(Path(file_ref).parent)
                destination = f"{parent}/{new_name}" if parent != "." else new_name
                from echolocate.mcp_server.tools.move_file import move_file
                result = move_file(
                    self.sandbox_root,
                    file_ref,
                    destination,
                    session_id=session_state.session_id,
                    confirmation_result="confirmed",
                )
                logger.log(
                    tool="move_file",
                    args={"source": file_ref, "destination": destination},
                    session_id=session_state.session_id,
                    destructive=True,
                    confirmation_required=True,
                    confirmation_result="confirmed",
                    outcome=result.get("outcome", "success"),
                    resolved_source_path=result.get("resolved_source"),
                    resolved_dest_path=result.get("resolved_destination"),
                )
                if result.get("outcome") == "success":
                    session_state.last_referenced_file = destination
                    return f"Done — renamed to {new_name}."
                return f"I couldn't rename the file: {result.get('error', 'unknown error')}"

        except Exception as exc:
            return f"Something went wrong: {str(exc)}"

        return "Action complete."


def _default_confirm(prompt: str) -> bool:
    """
    Text-based fallback confirmation. Used when TTS/STT confirmation
    is not available (e.g., during testing or headless runs).
    In the full voice pipeline, this is replaced by a TTS+STT callback.
    """
    print(f"\n[Confirmation needed] {prompt}")
    response = input("Type 'yes' to confirm, anything else to cancel: ").strip().lower()
    return response in {"yes", "y"}
