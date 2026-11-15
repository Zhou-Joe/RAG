# RAG: discover candidates, read selectively, choose final evidence

The normal chat retrieval workflow is:

1. `kb_search(query, kb_slug, k)` retrieves ranked candidates on the server. The default is 20,
   maximum 40. The model receives a compact directory grouped by knowledge base/document, candidate IDs,
   sections/pages, character counts and previews of at most 160 characters. Full chunk text stays in the
   per-answer server cache. Search does not create citations or verification evidence.
2. `kb_read_chunks(candidate_ids)` loads the complete text of the chosen candidates. The agent should
   choose the appropriate knowledge base, document and version before reading. IDs are specific to this
   answer and cannot be used to read arbitrary database chunks.
3. `kb_select_evidence(candidate_ids)` selects the **already read** chunks actually used for the answer.
   This replaces the previous document evidence and citations. Read-but-unused chunks are excluded.
   Empty selection is allowed for clarification or reporting insufficient evidence. Reading again requires
   confirming the final selection again.

Reading one library leaves the other retrieval tools available. The agent can search or read additional
permitted libraries and page through business data before making its final evidence selection. The middleware
reserves that selection when retrieval finishes or reaches its allowance. If a model ignores this and tries
to answer directly from candidate previews without confirming evidence, its unselected answer is not streamed.
The final selection is still the model's choice, not proof that its reasoning is correct; enhanced mode
continues to verify the answer against the selected original text and current source permissions.

## Counts

`candidate_count` and each document's `candidate_count` describe the bounded candidate set returned by this
retrieval operation. They are **not** an exact total of all semantically matching chunks in the knowledge base.
`total_matching_chunks` is null. `limit_reached` means the retrieval limit was reached, not proof that more
relevant results exist. `omitted_candidates`, `omitted_documents` and `preview_truncated` explicitly report
cache/directory omissions. Document groups include candidate IDs even when some individual previews cannot fit.

## Context limits

- Candidate cache: up to 240 distinct candidates / 1,000,000 text characters on the server.
- Candidate directory: up to 24,000 characters per response / 60,000 across this answer.
- Full-text reads: up to 24,000 characters per response / 120,000 across this answer, including text-response metadata.
- Up to 16 candidate IDs per read or final selection. A chunk is returned whole or listed under `not_loaded`;
  numbers, conditions and table rows are never silently clipped halfway through a chunk.
- Reading an already loaded chunk returns an `already_loaded` notice rather than replaying its full text.
- Normal search does not automatically attach extra image-lane candidates. If vision is enabled, explicitly
  reading an image candidate can attach the image, up to four per answer. The dedicated image-search workflow
  remains available for explicit image-finding questions.

These are character bounds for RAG tool payloads, not a token budget for the entire model request.
The agent allows up to 12 tool rounds per user turn. The last three rounds are reserved for final evidence
selection and presentation, including charts, value cards and exports; they are part of the 12-round total.
Presentation tools can also run earlier, and several independent views can be requested in one round.
Additional graph steps leave room for answering and evidence-selection recovery.
Three consecutive identical tool rounds trigger an early finish; an isolated retry or revisiting a previous
query does not disable the other tools. Relationship-navigation origins do not restrict which permitted
libraries may be explicitly queried.
Other tools, conversation memory and images have separate limits. Fewer full-text chunks can reduce context
use, although the selection steps add model calls; latency improvements depend on the model and question.

## Other document tools and citations

Normal `kb_fetch_doc` and `kb_neighbor_chunk` also produce candidate directories for the same selection pipeline.
The existing whole-manual overview path uses its own bounded chapter evidence preload; it is not exposed as
a way for normal tool calls to bypass selective reading.

Only final selected document chunks populate the citation chips and PDF page/highlight metadata. Citation
events use replacement snapshots, including empty snapshots, rather than accumulating discarded candidates.
This applies both to the live chat UI and persisted conversation messages. Graph and tracking-table citations
retain their own identities and are not removed by a document-evidence selection.

Document status, permission and content digest are rechecked before reading and final selection. Enhanced
answer publication additionally validates current chunk identities/text against the selected source snapshots.

## Follow-up conversations

Completed turns retain the original model messages, tool calls, tool results, loaded candidates and selected
evidence in `Conversation.agent_context`. The next turn appends the new user message without rewriting the
previous transcript. Normal answers remain in history, and naming a document changes the default search
target without clearing the conversation. Older chats bootstrap from all completed question/answer pairs;
tool results that were never saved by the old version cannot be reconstructed from those answers.

The model decides whether another lookup is useful. Previously loaded chunks can be reused or selected
again by the same candidate ID. Retrieval limits apply to each new turn, rather than exhausting a lifetime
quota. In enhanced mode, follow-ups can verify against retained evidence without rerunning the query planner
or requiring another search. Interrupted and rejected turns never replace the last completed context.

Stable system instructions and an unchanged message prefix allow the serving engine to reuse its prompt/KV
cache when supported. No provider-specific cache flag is sent to OpenAI-compatible local services. Actual
cache hits and prefill savings depend on the model server. The old three-pair/8,000-character truncation is
removed; the model server's context window still bounds the conversation. Source content and access are
checked before reusing saved tool material; changed or inaccessible material invalidates that cached context.

`kb_graph_search` is an optional shortcut to confirmed facts, not a validator. Using it does not enable
enhanced verification, change streaming behavior, or restrict ordinary analysis/export tools. The model can
answer directly from a useful graph result, reuse previous context, or consult source documents when needed.
