# Traceable EnGenie Sources

Source-supported statements in normal EnGenie chat can use inline markers such as `[1]`. The model receives a numbered source map after retrieval and selection, so it can place the correct marker beside a supported sentence or table caption. General advice and guesses should not receive source markers. Numeric table cells, code, filenames, and URLs remain intact.

The source list shows the matching number and source name. Clicking an inline marker focuses and highlights its source entry. Document entries retain their page/chunk evidence action; graph entries open the underlying fact; tracker entries open the named knowledge base.

Selected KB image results appear as compact image cards within the answer content, rather than thumbnails in the source list. Their captions retain the numbered source and available document/page details. The image-card controls expand to the chat's full width and collapse again. Source links remain in the numbered list, and protected image URLs keep the existing access checks. Selected caption matches are not automatically described as visually verified matches.

Reading a text candidate also returns a bounded `linked_images` directory when its original Markdown, HTML, or extraction order establishes an image association. Existing cleaned text chunks can recover these links without re-indexing. A reference within the passage, an adjacent boundary reference, and an extraction-order association are distinguished; sharing a PDF page alone does not establish a link. Page labels are included only when the extraction metadata supplies an unambiguous page.

These image candidates are separate from the text evidence. EnGenie can read their `candidate_id` with `kb_read_chunks` when the question needs image content, then include the image and relevant text IDs in `kb_select_evidence`. Listing an image does not load its pixels or publish its card. Image reads report whether pixels were actually delivered, previously delivered, unavailable, or limited to a caption. Each delivered image has an immediately preceding candidate/source/page label. Access, document version, and image bytes are checked again before publication; only selected images become conversation cards and citations.

Compact photos, value cards, and charts share a wrapping row. In the evidence viewer, **Previous Citation** and **Next Citation** move through the answer's cited document excerpts, including across documents. Buttons disable only at the first or last excerpt; a separate page selector remains available for a citation that spans several pages. Each navigation request uses the existing permission-checked preview endpoint.

Multiple tracker queries from the same KB share one visible source number and one source entry. A query count indicates multiple snapshots. All underlying queries remain stored and independently checked; grouping the display does not discard filters, ranges, or permission validation.

Enhanced answers are checked against the specific numbered source cited beside the claim. Unknown source numbers cannot become interactive source links. Conflicting display numbers remain unlinked rather than pointing to an arbitrary source.

Conversation history resolves source names after the existing source/permission checks. Existing answers are not rewritten to add citation claims. System controls support both interface languages; source names and document text keep their original wording.

The same rendering runs for streamed answers, final responses, restored history, and embedded chat. Inline references are built from sanitized text nodes; code blocks, existing links, and form controls are excluded.
