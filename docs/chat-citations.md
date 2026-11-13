# Traceable EnGenie Sources

Source-supported statements in normal EnGenie chat can use inline markers such as `[1]`. The model receives a numbered source map after retrieval and selection, so it can place the correct marker beside a supported sentence or table caption. General advice and guesses should not receive source markers. Numeric table cells, code, filenames, and URLs remain intact.

The source list shows the matching number and source name. Clicking an inline marker focuses and highlights its source entry. Document entries retain their page/chunk evidence action; graph entries open the underlying fact; tracker entries open the named knowledge base.

Multiple tracker queries from the same KB share one visible source number and one source entry. A query count indicates multiple snapshots. All underlying queries remain stored and independently checked; grouping the display does not discard filters, ranges, or permission validation.

Enhanced answers are checked against the specific numbered source cited beside the claim. Unknown source numbers cannot become interactive source links. Conflicting display numbers remain unlinked rather than pointing to an arbitrary source.

Conversation history resolves source names after the existing source/permission checks. Existing answers are not rewritten to add citation claims. System controls support both interface languages; source names and document text keep their original wording.

The same rendering runs for streamed answers, final responses, restored history, and embedded chat. Inline references are built from sanitized text nodes; code blocks, existing links, and form controls are excluded.
