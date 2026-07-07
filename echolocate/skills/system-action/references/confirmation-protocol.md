# Confirmation Protocol Reference — System Action Skill

## ToolConfirmation payload shape

When `tool_context.request_confirmation()` is called, it surfaces a `ToolConfirmation` payload with this structure:

```json
{
  "tool_name": "move_file",
  "args": {
    "source": "resume.pdf",
    "destination": "Documents/resume.pdf"
  },
  "confirmation_prompt": "Move resume.pdf to Documents? Say yes to confirm.",
  "session_id": "a1b2c3"
}
```

The system executor node listens for a `ToolConfirmation` result with `approved: true` or `approved: false` before proceeding.

## Audit log fields produced by a confirmed destructive action

```json
{
  "timestamp": "2026-07-02T14:33:10Z",
  "tool": "move_file",
  "args": {"source": "resume.pdf", "destination": "Documents/resume.pdf"},
  "resolved_source_path": "/sandbox/resume.pdf",
  "resolved_dest_path": "/sandbox/Documents/resume.pdf",
  "destructive": true,
  "confirmation_required": true,
  "confirmation_result": "confirmed",
  "outcome": "success",
  "session_id": "a1b2c3"
}
```

## Audit log fields produced by a denied action

```json
{
  "timestamp": "2026-07-02T14:33:10Z",
  "tool": "move_file",
  "args": {"source": "resume.pdf", "destination": "Documents/resume.pdf"},
  "destructive": true,
  "confirmation_required": true,
  "confirmation_result": "denied",
  "outcome": "cancelled",
  "session_id": "a1b2c3"
}
```

Note: a denied action has `outcome: "cancelled"`, not `outcome: "error"`. Cancellation is a normal, expected outcome — the user changed their mind.

## Spoken confirmation message templates

| Action | Confirmation prompt | Post-action response |
|---|---|---|
| Move | "Move [filename] to [destination]? Say yes to confirm." | "Done — moved to [destination]." |
| Rename | "Rename [filename] to [new name]? Say yes to confirm." | "Done — renamed to [new name]." |
| Delete | "Delete [filename]? This can't be undone. Say yes to confirm." | "Done — [filename] has been deleted." |
| Denied (any) | — | "Cancelled, nothing was changed." |

## What counts as an affirmative confirmation

The voice confirmation expects explicit "yes" or affirmative equivalents:
- "yes", "yeah", "yep", "confirm", "do it", "go ahead", "sure" → approved
- anything else (silence, "no", "cancel", "wait", "stop") → denied

When in doubt, deny. The cost of an unnecessary re-prompt is far lower than the cost of an unintended file operation.
