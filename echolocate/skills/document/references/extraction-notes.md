# Extraction Notes Reference — Document Skill

## PDF extraction quirks (pymupdf4llm)

**Multi-column layouts:** pymupdf4llm preserves reading order for most two-column academic papers. If the text you receive reads oddly (e.g. every other sentence seems unrelated), it may be a three-column or non-standard layout where the Markdown ordering isn't perfect. In this case, summarize at the paragraph/section level rather than sentence level — the section headers will still be correct even if line-by-line order is imperfect.

**Tables:** pymupdf4llm converts PDF tables to Markdown pipe tables. When summarizing a table, describe it conceptually ("a table showing quarterly revenue by region") and call out the most notable values, rather than reading every cell.

**Headers and footers:** Page numbers, running headers, and footnotes may appear as short lines in the Markdown. Don't include page numbers or boilerplate headers in summaries — they're not document content.

**Math and formulas:** LaTeX-style math may come through as LaTeX or as garbled text. If you see `\frac{}{}` or similar, describe the formula in plain English (e.g. "a mathematical formula for calculating...") rather than attempting to read the LaTeX literally.

## DOCX/PPTX extraction quirks (markitdown)

**Track changes:** markitdown may include deleted/revised text marked with strikethrough or revision markers. Summarize the final/accepted text, not the revision history.

**PowerPoint:** Slide content comes through as Markdown sections per slide. When summarizing a presentation, treat each `##` heading as a slide title.

**Embedded images:** markitdown extracts text only; embedded images are not described. If a document appears text-light and the user seems to expect image descriptions, note that the document may contain images that can't be read.

## Context compaction behavior

When `EventsCompactionConfig` triggers (document exceeds context budget), earlier chunks are replaced by a running summary. The running summary is prepended with `[Earlier content summarized:]`. When you see this marker:

1. Treat the summary as accurate ground truth for those earlier sections
2. Don't say "I was told earlier that..." — speak as if you read it normally
3. If the user asks about something that was in the compacted section and the summary doesn't cover it, acknowledge that the document was long and you may have seen only a partial view

## Voice formatting rules

| Written form | Spoken form |
|---|---|
| `03/15/2026` | "March 15th, 2026" |
| `Q3 2026` | "the third quarter of 2026" |
| `$1.2M` | "1.2 million dollars" |
| `Fig. 3` | "Figure 3" |
| `et al.` | "and colleagues" |
| `e.g.` | "for example" |
| `i.e.` | "that is" |
