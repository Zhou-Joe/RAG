# Quality and streaming reliability upgrade

This change retains Django, MinerU and Chroma, without introducing a new model or vector backend.

## Apply

Run `python manage.py migrate`. Migration `0022_message_verified` adds a flag for verified assistant messages; existing messages default to false. Enhanced mode only carries forward currently accessible, verified source-backed answers. Ordinary mode uses recent completed conversation pairs. Old checkpoint files remain on disk but are no longer replayed into new turns.

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

The implementation has passed 141 Django and 15 Node regressions, plus local model-backed streaming checks. There is no claim of a representative corpus accuracy benchmark or production P95 latency target being met.

## Rollback

Stop the application and restore the previous code with a matching database/index backup. The new migration is additive, and this change does not remove source files, old messages or checkpoint files. Launcher changes specific to the development machine are intentionally outside this PR.
