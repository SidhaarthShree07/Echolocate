---
name: file-search
description: Locates files inside the user's sandboxed directory by filename, file type, content keyword, or a relative date such as "last Tuesday" or "yesterday." Use when the user asks to find, locate, search for, or look up a file, or refers to a file indirectly from memory (e.g. "that PDF I saved," "the report from last week," "my resume").
license: Apache-2.0
compatibility: Requires the EchoLocate MCP filesystem server's search_files and get_metadata tools, exposed to this node via ADK 2.0 tool_filter as read-only.
metadata:
  project: EchoLocate
  node: file_search
  version: "1.1"
---

# File Search Skill

## Purpose

Find a file the user is describing from memory — by name, type, rough content, or when they last touched it — and either state the single best match or ask a short clarifying question. This skill never guesses when more than one file is a plausible match, and it never modifies anything: it only reads and reports.

## When this activates

The router dispatches to this skill when the classified intent is `file_search` with an acceptable confidence and at least one populated entity (`file_reference` or `relative_date`). If you are invoked, assume the router has already decided this is a search task — don't re-derive intent, just execute the search.

## What you receive

A JSON entity block from the router, already partially resolved upstream:

```json
{
  "file_reference": "string or null",
  "relative_date": "an ISO date string, already resolved — or null",
  "target_action": "string or null"
}
```

**Important:** `relative_date`, if present, is already an absolute date (e.g. `2026-06-23`), resolved by a deterministic date-parsing utility *before* it reached you. Do not attempt to compute what "last Tuesday" or "yesterday" means relative to today yourself — that computation has already happened outside the model, specifically to avoid the kind of date-math error a language model is prone to. Treat it as a plain filter value.

## Step-by-step procedure

1. **Build the filter set.** Map whatever entities are populated onto the `search_files` tool's parameters (filename fragment, file type, date, content keyword). Don't invent filters that weren't given to you.
2. **Call `search_files`.** This is the only tool you need for a normal search. `get_metadata` is available if you need to confirm a candidate's exact modified date or size before describing it to the user.
3. **Rank results.** If more than one file comes back, prefer the most recently modified match — but ranking is a tiebreaker for *presenting* results in order, not a license to silently pick the top one when several are genuinely close (see "Ambiguous matches" below).
4. **Zero results:** broaden the search **once** by dropping the single most restrictive filter (usually the content keyword), then report clearly if still nothing is found. Say what you searched for, don't just say "not found."
5. **One clear result:** report it directly — file name, and one relevant detail (date or type) — and update `last_referenced_file` in session state so a follow-up like "open it" or "read that one" can resolve the pronoun without a new search.
6. **Multiple close results:** do **not** pick one. Return a `clarification_needed`-shaped response listing the top 2–3 candidates by name so the router can ask the user to disambiguate. "Close" means: same normalized filename stem, or multiple files matching all given filters with no single standout by recency.

## Output format (this becomes spoken audio)

- One sentence for a single match: *"Found Agriculture_Report_Q2.pdf, saved last Tuesday."*
- Short and enumerable for a disambiguation prompt: *"I found two matching files: Agriculture_Report_Q2.pdf and Agriculture_Notes.pdf. Which one?"*
- Never speak a full file path — sandbox paths are an implementation detail, not something a screen-reader user needs to hear. Just the filename.

## What this skill must NOT do

- Never call `move_file`, `delete_file`, or `open_file` — those tools are not in this node's `tool_filter` in the first place, so calling them isn't just discouraged, it's unavailable. If the user's request sounds like it also wants an action taken on the found file ("find and delete that old PDF"), report the search result and let the router hand the follow-up to the `system-action` skill — don't try to do both in one turn.
- Never fabricate a filename that wasn't actually returned by `search_files`.
- Never silently expand the search outside the sandboxed root — you only ever see paths the MCP server already scoped to the sandbox; there is nothing outside it to search.

## Edge cases

- **Vague reference, no session context:** if `file_reference` is null, `relative_date` is null, and `last_referenced_file` in session state is also empty, don't guess — ask "Which file are you looking for?" rather than running a match-everything search.
- **File type mismatch:** if the user says "that PDF" but the closest name match is a `.docx`, don't silently substitute it — mention the type mismatch explicitly so the user can confirm.
- **Homophone / fuzzy filenames:** voice transcription can mangle filenames (e.g. "agriculture" vs "agri-culture"). If a fuzzy match on the transcribed reference returns exactly one strong candidate, it's fine to use it — but say the actual filename back to the user so they can correct you if the transcription was wrong.

See `references/entity-handling.md` for more detail on the ranking heuristic and what counts as a "close" match.
