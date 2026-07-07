# Entity Handling Reference — File Search Skill

## Ranking heuristic

When `search_files` returns multiple results, use this priority order:

1. **Exact filename match** (the cleaned file_reference appears verbatim in the filename stem, case-insensitive)
2. **Most recently modified** (within the past 7 days preferred over older files)
3. **File type match** (if the user said "that PDF", prefer .pdf over .docx even if names are similar)
4. **Content keyword density** (for text files only — prefer files where the keyword appears more frequently)

A result is "clearly better" if it satisfies criterion 1 OR is more than 7 days more recent than the next best match. Anything else is "ambiguous" — trigger disambiguation instead of guessing.

## What counts as a "close match"

Two or more results are "close" if:
- Their filename stems normalize to the same string after removing punctuation and common suffixes (e.g. "Agriculture Report Q2" and "Agriculture_Report_Q2_final" are close)
- All given filters match both files (same date, same type, same keyword)
- Neither is more than 7 days more recent than the other

## Relative date entities

`relative_date` arrives already resolved to an ISO date string (e.g. `2026-06-23`). The date math was done upstream by `dateparser` so you don't have to compute it. Use it as a plain filter: "files modified on this date."

If the user said "last week" or "this month" and the upstream resolver returned a range rather than a single date, it will be represented as the start of that range. Apply it as an approximate filter — prefer files within ±2 days of that date if no exact match exists.

## Pronoun resolution

If `file_reference` is null but `last_referenced_file` in session state is set, the user is referring to the most recently mentioned file. This covers:
- "Open it"
- "Read that one"  
- "What does this document say?"
- "Move this file"

Always confirm the filename back to the user when using an implicit reference: *"Opening Agriculture_Report_Q2.pdf..."* rather than silently opening it.
