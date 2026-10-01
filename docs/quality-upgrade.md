# Quality and streaming reliability upgrade

This change retains Django, MinerU and Chroma, without introducing a new model or vector backend.

## Apply

Run `python manage.py migrate`. Migrations `0022_message_verified` and `0023_message_completion_status` add verification and completion flags. Existing messages default to unverified/complete. Interrupted visible output is stored separately as incomplete and is not reused as factual context. Enhanced mode only carries forward currently accessible, verified source-backed answers. Ordinary mode preserves recent question context but omits unverified answer facts to avoid repeating unsupported names and parameters. Old checkpoint files remain on disk but are no longer replayed into new turns.

No automatic corpus reparse or destructive index rebuild is performed. Existing indexes must be rebuilt to recover table text omitted by earlier parsing. Back up business data and indexes before rebuilding. Changing an embedding model requires a matching new index even if model dimensions happen to agree.

## Settings

| Variable | Default | Purpose |
|---|---|---|
| `EMBEDDING_BATCH_SIZE` | `16` | Between 1 and 32 texts per embedding call |
| `LLM_MAX_OUTPUT_TOKENS` | `2048` | Output budget; truncated results are explicitly incomplete |
| `LLM_EXTRA_BODY` | `{}` | Optional engine-specific JSON request fields |
| `QA_TURN_TIMEOUT` | `240` | Whole-turn timeout in seconds |
| `FOS_LOCAL_ONLY` | `0` | Opt-in Python outbound restriction and browser CSP |
| `PYODIDE_INDEX_URL` | Local path in local-only mode, otherwise CDN | Asset base URL |

For local-only operation, provision the complete Pyodide distribution and required package files under `static/vendor/pyodide/` before disconnecting. This directory is not versioned. Python socket checks and browser policy do not provide OS-level isolation for independent model servers or native code.

The browser terminates on `done`, provides a stop button, and enforces a 45-second idle and 270-second total wait. The server sends periodic progress while waiting. If increasing the server deadline, align browser deadlines as well. Synchronous blocking code and third-party services can still delay cancellation; cancelling a request does not promise immediate termination of external inference.

## Behavior and limits

- “Show a table” requests use Markdown; explicit exports and analysis can still use the existing analysis tool in ordinary mode.
- QueryPlan validates structured intent and preserves original identifiers, but retrieval is still executed by the existing Agent rather than a deterministic subquestion scheduler.
- Enhancement remains opt-in. It buffers drafts until source checks and semantic verification succeed. This does not constitute a guarantee of factual correctness.
- MinerU tests validate health only. Embedding inference and index compatibility are reported separately. A successful test is a point-in-time result, not continuous monitoring.
- Citation fixes reject unsupported page coordinates rather than falling back to an invented first-page location.

## Validation

```sh
python manage.py test kb.tests accounts --noinput
node --test scripts/tests/chat_input.test.cjs scripts/tests/chat_stream.test.cjs
```

The implementation has passed 143 Django and 15 Node regressions, plus local model-backed streaming checks. There is no claim of a representative corpus accuracy benchmark or production P95 latency target being met.

## Rollback

Stop the application and restore the previous code with a matching database/index backup. The new migration is additive, and this change does not remove source files, old messages or checkpoint files. Launcher changes specific to the development machine are intentionally outside this PR.

Interrupted visible answers remain available after switching conversations, with an explicit incomplete warning. Private drafts in enhanced mode are never exposed or saved by this mechanism. Device names must follow the user or retrieved source rather than arbitrary examples in a system prompt.

## Background-answer follow-up (migrations 0024–0026)

The current behavior supersedes the earlier page-switch cancellation behavior:
leaving a page detaches the subscriber while the answer continues in the ASGI
process. Returning reads durable snapshots until the answer finishes. Explicit
stop and conversation deletion cancel the task. Current deployment must use one
Web process; model inference and queued jobs do not resume across process restarts.
See [background-answer operations](background-answers.md) for the full lifecycle.

Before upgrade, wait for active answers to finish, back up SQLite, apply migrations
and restart Web. Refresh the browser for updated assets. No model or index change
is required. Migration 0026 initializes existing complete answers as read.

The follow-up adds server-enforced named-library scopes, bounded retrieval rounds,
serialized-tool-output rejection, persistent unread badges and a 1680px responsive
Q&A layout. It does not turn the existing agent into a fully deterministic query
planner or constitute a factual-accuracy benchmark.

Run all JavaScript regressions with `node --test scripts/tests/*.test.cjs`.
Current totals are 162 Django tests and 23 Node tests. Before rolling this entire
follow-up back, stop Web, normalize active message states to `incomplete`, and use
the current code to migrate `kb` to `0023` before restoring previous code. This
removes failure reasons and read cursors; keep the backup if those must be retained.
