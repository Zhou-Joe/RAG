# Changelog

## Unreleased

- Preserve HTML table cells when parser output omits span attributes, quotes them, reorders them, or uses header cells; reject malformed spans instead of silently dropping content.
- Bound embedding batches and request timeouts; support OpenAI-compatible WeMM endpoints and complete reranker URLs.
- Validate model test responses, distinguish inference from index compatibility, and provide a sequential test-all action. MinerU HTTP 200 health responses count as successful health checks.
- Bound conversation history and whole-turn execution; add streaming heartbeats, cancellation, timeout handling, and explicit incomplete-answer errors. Persist complete answers before sending the terminal SSE event.
- Improve citation navigation, page validation, Chinese IME handling, and add a return-to-site link in Django admin.
- Add optional structured query planning and verification-before-publication with current-source checks; keep enhancement opt-in.
- Correct evaluation ranks to use the globally merged retrieval order.
- Add opt-in local resource restrictions and configurable locally hosted Pyodide assets.

See [upgrade notes](docs/quality-upgrade.md) for configuration, migration, validation and limitations.
