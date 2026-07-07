---
name: system-action
description: Performs filesystem and application actions requested by voice inside the user's sandboxed directory — opening a file or folder, launching an application, moving, renaming, or deleting a file. Use when the user issues a command to open, launch, start, move, rename, or delete something.
license: Apache-2.0
compatibility: Requires the EchoLocate MCP filesystem server's open_file, move_file, and delete_file tools, and ADK 2.0's ToolConfirmation primitive (tool_context.request_confirmation()) for every destructive action.
metadata:
  project: EchoLocate
  node: system_executor
  version: "1.1"
---

# System Action Skill

## Purpose

Carry out a filesystem or application action the user asked for by voice — and never let a destructive one happen without the user explicitly hearing what's about to happen and saying yes. This skill is the only one in EchoLocate that changes anything on disk; treat that responsibility as the organizing principle behind every rule below.

## When this activates

The router dispatches here for `system_action` intents with a resolved `target_action` and `file_reference`. Before you do anything, classify the requested action into exactly one of two categories — this classification determines everything that follows.

## Step 1: classify destructive vs. non-destructive

| Category | Actions | Tool call |
|---|---|---|
| **Non-destructive** | open a file, open a folder, launch an application | Call the MCP tool directly — no confirmation needed |
| **Destructive** | move, rename, delete | **Must** go through the confirmation protocol below before the actual tool call |

If you're genuinely unsure which category an ambiguous request falls into (rare, but possible with a garbled transcript), treat it as destructive — the cost of an unnecessary confirmation question is one extra turn; the cost of an unconfirmed destructive action is data loss.

## Step 2: non-destructive actions

Call `open_file` (or the equivalent tool) directly. Confirm verbally what you did: *"Opening resume.pdf."* Don't ask permission first — asking "should I open this?" for a read-only action adds friction without adding safety.

## Step 3: destructive actions — the confirmation protocol

**Never call `move_file` or `delete_file` directly.** Every destructive action follows this exact sequence:

1. Call `tool_context.request_confirmation()` **from inside the tool-calling function**, before the actual filesystem operation runs. This is what pauses the graph and surfaces a `ToolConfirmation` request.
2. Speak a confirmation prompt that names the **exact file and the exact action** — never a vague "are you sure?" Say what will happen: *"Move resume.pdf to Documents? Say yes to confirm."* or *"Delete draft_notes.txt? This can't be undone. Say yes to confirm."*
3. Wait for the `ToolConfirmation` result. Do not proceed on anything except an explicit affirmative.
4. **On approval:** perform the actual `move_file`/`delete_file` call, then confirm verbally that it's done: *"Done — moved to Documents."*
5. **On denial:** respond *"Cancelled, nothing was changed"* and stop. This is a normal, successful outcome — not an error, and not something to retry or re-prompt about.

See `references/confirmation-protocol.md` for the exact `ToolConfirmation` payload shape and audit log fields this produces.

## Destination constraints

`move_file` destinations must already exist as a directory inside the sandbox. If the user names a destination that doesn't exist ("move it to my Archive folder" when no `Archive` folder exists), don't create it silently — tell the user the destination doesn't exist and ask what they'd like to do, rather than guessing whether they meant to create a new folder.

## What this skill must NOT do

- Never skip the confirmation step for a destructive action regardless of how confident the router's classification was — confirmation is gated on the action type, not on classifier confidence.
- Never batch multiple destructive actions behind a single confirmation ("delete all the old PDFs" needs either one confirmation per file or an explicit, clearly-worded confirmation that names all affected files — never a vague blanket "yes" covering an unenumerated set).
- Never retry a denied action automatically. If the user changes their mind, that's a new command in a new turn.
- Never construct a shell command or invoke anything outside the six allowlisted MCP tools — this skill's tool_filter only exposes `open_file`, `move_file`, and `delete_file`; there is nothing else to reach for.

## Edge cases

- **User says "yes" to something that wasn't a pending confirmation:** if there's no `active_confirmation` in session state, treat a bare "yes" as unrelated conversational noise, not as approval of a prior turn's action — don't act on it.
- **Confirmation prompt times out / user goes silent:** treat as an implicit denial after a reasonable pause; don't leave a destructive action in limbo indefinitely.
- **File already doesn't exist at confirmation time** (e.g. deleted by something else between the request and the confirmation): report that the file can no longer be found rather than proceeding or erroring silently.
