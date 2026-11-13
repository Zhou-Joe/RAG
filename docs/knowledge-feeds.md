# Auto-updating knowledge bases / 自动更新知识库

## Collector dialogue and structured records

The collector supports complete conversation replay, direct answers to its questions, and cumulative revision context.
During the current page session, follow-ups reuse the actual model messages, tool results, working memory, captured
samples and the same interactive browser. Answering a question or clicking confirmation does not close the dialogue.
Body-only mapping edits can reuse an already validated full-batch capture; changes to browser actions require a new replay.
Temporary state is held in the web process, scoped to the owner and kept alive by page polling. Leaving/clearing the page
releases it; without polling it expires after 30 minutes. A web-process restart loses the temporary browser state.
Approved tasks additionally keep an encrypted snapshot of model messages, tool results and working memory in Django
(`0042_connector_saved_context`). Their dialogue and subsequent revisions survive page close and process restart.
Open **自动更新 → 与 AI 对话修改脚本** to resume a task; the approved chat also has a direct continue button.
The script editor has a **清空上下文** button. It discards the previous conversation, stored model messages and temporary
browser/tool state, then starts a clean edit thread from the currently approved connector. Unapproved edits are discarded;
the script, saved credentials, schedule, enabled/paused state and collected records are retained. No model call or collection
is started by clearing context. The button is disabled during generation; stop the current edit first. New edits still need
real sample validation and explicit approval. The fresh thread remains the task's edit entry after page reload.
Later editing restores context and the saved specification, then fetches fresh samples before asking for approval.
The browser process is reopened when needed. Old tasks whose temporary history was already deleted can be edited
from the saved script; deleted historical messages cannot be reconstructed. Unapproved standalone chats remain temporary.
Tool progress patches existing DOM nodes in place, so streamed output does not restart spinners or replay completed checks.
Browser observations return up to **200 elements**, 40,000 characters of cleaned HTML and 20,000 characters of text.
The connector agent's character budget is 400,000 (a character budget, not a claim of 400,000 model tokens).
Mapping tools receive a structured schema requiring stable IDs and nested `browser.output` configuration.

Migration `0041_tracker_feed_rows` enables feed-backed tracker rows without a document. Apply database migrations before
restarting web/scheduler workers. Feed records write `TrackerRow` directly according to the source's write mode;
they create no Markdown files or per-item documents. Existing documents are retained as historical versions when a record
is next updated. Missing items remain; explicit deletes never remove historical snapshots. A table can contain rows from
multiple sources without removing another source's columns. Tracker viewing, export and chat lookup support these rows.
Duration fields use the configured deterministic `computed` time difference and are included in preview and tracker values.

The integration standard is **FOS Knowledge Feed v1**, identified by `schema: "fos-rag-feed/v1"`.
It is independent of upstream vendors, script languages and LLM providers. A connector fetches an existing API or webpage,
converts reports or data into this JSON contract, and either exposes it over HTTP or submits it to RAG.
No OCR or LLM extraction is needed for this path. Feed records are stored directly as tracker rows without vector indexing.

## Create a source

Open **Knowledge Library → Data collection**. Create a new knowledge base or choose an existing folder, then select:

- **定时拉取 API**: the project periodically GETs a `fos-rag-feed/v1` endpoint. Configure a five-field cron and IANA timezone.
- **外部脚本推送**: run a connector using your own cron, Windows Task Scheduler, CI job or service. Generate a source-scoped token and POST JSON to its ingest URL.

### Business data and history

Automatically created libraries belong to **业务数据**, not **手册库**. Existing automatically created feed libraries
are reclassified by migration `0043_feed_history_modes`; records are retained and manually selected libraries are unchanged.

Each source has a separate `write_mode`, independent of its pull/push connection:

| Mode | Behavior |
| --- | --- |
| `append` (default) | Every successful collection appends a row per returned item, even if its content and upstream ID are unchanged. Historical observations remain available for yearly comparisons and trend analysis. |
| `upsert` | Update the current row for the same source and stable ID; skip unchanged content and reject older upstream versions. Previously appended historical rows remain untouched. |

The agent selects a mode in its connector specification. It prefers `append` for operating hours, show schedules,
prices, inventory observations, metrics and historical comparisons. It uses `upsert` when the user explicitly requests
overwriting or latest-state storage. Users can change the selection in the preview before approval, and later in
**编辑配置 → 数据写入方式**, without deleting existing history. Existing feeds also default to retaining future observations
after the migration (an explicit `write_mode` in an existing script is respected).

Each published row includes `采集时间` (ISO timestamp in the source timezone) and `来源ID`, plus mapped business fields.
Snapshots reference their successful `FeedRun`. There is no need to manufacture random IDs or timestamp IDs to retain
history. Push callers should use one stable `batch_id` per collection and reuse it on retries: duplicate delivery returns
the same run and does not append twice. A new batch represents a new observation. Missing records do not remove anything;
explicit `deleted` events mark source items deleted while keeping snapshots in append mode. In upsert mode only the
current row can be removed. Changing modes does not erase historical rows. A failed batch commits no partial snapshots.
The tracking table and spreadsheet export include historical rows in newest-first order.

Managers can create sources only in knowledge bases they may manage. A source rechecks its owner's active account
and current library permissions each run. Managers enter their own API credentials in the page; only private-network
access and legacy server environment references require global-administrator privileges. Ordinary readers cannot configure sources or view tokens/run histories.
The web application never uploads or executes connector code.

## Generate a connector from a prompt

Open **自动更新知识库 → 让 AI 生成 API 连接器** (`/kb/feeds/builder/`). Describe the webpage or API URL,
documentation link, desired report fields, stable item identity, and update frequency in ordinary language.
The agent uses the site's configured model, including local models. **No JSON sample or field mapping is required in the UI.**
When given HTML, it can open a real browser, interact with the page and extract DOM or browser-observed network data;
HTML is not rejected merely because it is not JSON. The builder also supports plain-language retries of older failed drafts.
Provide source access information in the dialogue when needed: a Bearer Token, an API Key and header name,
Basic or webpage login username/password, or custom header name/value pairs. The agent binds it to the source through
`set_source_access`. The data-source management page also supports credential rotation and a direct connection test.

A collector can keep up to eight named credentials, combining API keys, bearer/basic authentication, custom headers and website logins. Adding a second service with `set_source_access(url, authentication, name)` preserves the primary URL and other saved credentials. Each credential is scoped to its configured scheme, host and port. Same-service headers can be combined unless values conflict; same-service login accounts use `credential: "profile.username"` / `"profile.password"` references. Multiple headers on one service can also use the existing custom-header method.

In **Edit settings → Access credentials → Multiple credentials**, each row has a name, service URL and authentication method. Saved rows can be reused while rotating or removing another row; no saved secrets are displayed. A structured request can use `authentication: {"type":"multiple","profiles":[{"name":"api","url":"https://api.example.com","authentication":{"type":"bearer","token":"…"}}]}`. There may be at most 20 distinct header names across the bundle. Exported scripts include `connector_credentials.json` with per-profile environment references and `.env.example` placeholders, never saved values; populate those variables in the execution environment. Managed schedules use the encrypted credentials automatically.
The app automatically encrypts and stores credentials; users do not edit environment variables or backend files.
For an intranet source (10.x, 192.168.x, 172.16-31.x, 127.0.0.1), a global administrator ticks
**允许访问内网或本机 API** in the collapsible **内网配置** panel above the composer before sending the requirement.
The switch belongs to that conversation, is inherited by every later reply in the same thread, and is saved onto the
data source when the preview is approved, so the scheduled runs keep working. Non-administrators get a clear message
instead of a doomed run, and link-local/metadata, multicast and unspecified addresses stay blocked in every mode.

Available tools:

- Search public API documentation (`search_api_docs`).
- Read a bounded section of a documentation page (`read_api_documentation`).
- GET a bounded JSON sample from a user-declared API origin (`probe_api`).
- Inspect the connector contract, runtime and dependencies (`inspect_connector_environment`).
- Validate the field mapping against the actual sample (`test_mapping`).
- Display formatted examples from a real response to the user (`show_data_preview`).
- Open/read an actual browser page, including JavaScript-rendered content (`browser_open`, `browser_snapshot`).
- Click, fill, select, wait and scroll (`browser_action`). Page login fields reference saved `username`/`password` credentials.
- Inspect real JSON requests/responses (`browser_network`, `browser_read_response`).
- Inspect CSS-selected rows and fields from current DOM (`browser_extract`).
- Replay the complete browser recipe in a fresh context and validate real output (`test_browser_connector`).

The agent returns a validated declarative specification, and the project generates a runnable Python script
from a trusted template. It cannot execute arbitrary model-written code or install packages on the server.
The builder page is a **conversation**: each requirement or adjustment is a user message, and the agent's phases,
tool activity, real samples, questions and preview cards are assistant messages in the same thread.
Unclear requirements, missing stable IDs or inaccessible endpoints produce concrete questions in the conversation;
answer in the chat composer and the agent continues with the original requirement plus your reply.
A nonempty sample must pass the real feed validator before a draft becomes ready.
The preview shows mapped titles, IDs and report contents, with the field configuration and documentation sources.
Each follow-up message creates a new draft using the prior mapping, real examples, feedback and saved
credential reference. It may be repeated until satisfactory. Earlier drafts in the thread are marked revised and cannot be approved.
Only **确认数据并开始自动更新** creates and enables a data source, starts the first import into Django, and schedules
future pulls. Preview data stays in the draft and is not published as knowledge before approval. The page shows the initial
import outcome. Page reconnects retrieve the existing task.
Model/tool activities and public model output are shown in the progress panel; private reasoning is not collected.

Each generation is bounded to **50 model calls, 100 tool calls, 20,000,000 cumulative input characters,
8,192 output tokens per request, 120 seconds per model request, and 120 minutes overall**.
Context stays append-only until approximately 400,000 characters; then it is compacted in one batch to roughly half
that size, preserving the original request, actual page structures, samples, failed selectors and recent complete tool groups.
This avoids changing the prompt prefix every round, allowing compatible local engines to reuse their prompt cache.
Six consecutive rounds without new page content or useful data trigger a final response instead of further exploration.
A successful `show_data_preview` ends generation immediately. The 50-call cap is not a target.
Browser replay also ends generation directly after a valid real capture is formatted. By default, changed field formats
produce a raw-content preview with visible warnings, rather than more model retries. The user can approve or refine it.
Users can stop a generation from the page, and a stopped worker cannot publish a late candidate.
There is no autonomous retry loop. Mapping repairs use the same budget; expired jobs become failed on the next page check.
Each network tool reads at most 500 MB, uses a 30-second elapsed-response bound plus connection/read timeouts,
pins DNS, preserves TLS validation, and rejects redirects. Credentials and common authentication fields are redacted
before samples reach the model. Server permissions are rechecked during the task and when enabling it.

### Entry points: page, in-process service and command line

Every entry point calls the same implementation in `kb/collector_api.py`, so permissions, intranet switches,
credential ownership, cron calculation, the CAS state machine and the first import behave identically everywhere.
The service layer takes plain arguments, raises `ValueError` for business errors and never touches `request`.

| Function | Purpose |
| --- | --- |
| `create_draft(user, requirement, api_url=…, authentication=…, allow_private=…, kb=…, created_via=…)` | start generation in a background thread and return the draft immediately |
| `refine_draft(user, job, requirement, …)` | next conversation turn; keeps credential, intranet flag and thread |
| `approve(user, job, write_mode=…, department=…, run_now=True)` | the only place that creates/enables the scheduled source (page **确认数据并开始自动更新**) |
| `cancel(user, job)` / `status(job)` / `source_url(job)` | stop, non-HTTP status view, effective source host |

Command line (same rules as the page; the key is read from an environment variable so it never reaches argv,
shell history or the task title):

```sh
# 默认等最多 15 分钟，实时把工具活动打到 stderr，结束后打印真实样例与候选脚本
.venv/bin/python manage.py build_feed "每天13点拉 http://host/api/v1/lightboard/board/ 的每车当前里程" \
  --url http://host/api/v1/lightboard/board/ --token-env LIGHTBOARD_TOKEN --private

# 看过样例后批准，建立定时数据源并立即首跑
.venv/bin/python manage.py build_feed "……" --token-env LIGHTBOARD_TOKEN --wait 900 --approve

# 事后批准已生成好的任务
.venv/bin/python manage.py build_feed --job 6c1f… --approve
```

生成跑在命令自己的进程里，所以 `--wait 0`（立即返回）会让任务停在「进行中」直到两小时后被回收——
需要长时间后台运行请用页面。

Exit status: `0` approved/ready, `1` failed, `2` the agent still needs information (printed as questions).
`--json` prints the same status payload used by automation. No entry point approves on its own: the model can only
produce a candidate script plus real samples, and a human (or an explicit `--approve`) publishes it.

### Credential shapes

`parse_auth` requires an explicit `type`, and infers it only when the shape is unambiguous:
`{"token": …}` → `bearer`, `{"username", "password"}` → `basic`, `{"headers": [{"name", "value"}]}` → `headers`.
Anything else without `type` — including `{"type": null}` or `{"api_key": …}` whose header name is unknown —
raises an error that lists the accepted shapes. A missing `type` is never treated as "no authentication":
the earlier silent fallback stored no credential while reporting success, so every later request answered 401 and
the assistant concluded the key was wrong. `set_source_access` now returns `credentials_attached` plus a
remediation note when nothing was stored, and an explicitly replaced credential row is discarded.

Attaching the credential when the draft is created (page form, `--token-env`, or a structured `authentication`)
also replaces the key with `[REDACTED]` in the stored requirement, so the model reads the bound credential
through `request_headers()` instead of copying a secret into a URL or query parameter.

### Credential storage and updates

`FeedCredential` stores an authenticated encrypted bundle containing headers and credential ownership/origin bindings.
The encryption uses the already-installed [Cryptography Fernet implementation](https://cryptography.io/en/latest/fernet/).
The app automatically creates a random key under `data/secrets/feed-credentials.key`; this key is separate from
the database and is reused by web and scheduler processes. Back up this key with the deployment: losing it makes
stored credentials unreadable. No saved key or credential value is included in generated scripts, progress events or
downloadable packages. Credentials supplied directly in the chat are visible to the configured model in that request;
the app then stores the encrypted bundle and redacts saved values from subsequent samples and activity logs.

The model sees the authentication type, header names and opaque references. `probe_api` resolves those references
to real values automatically for the approved API origin. The same resolver is used by scheduled pulls.
For webpage login, the browser fills saved credentials only on the user-approved host, then uses session cookies within
that isolated context. Login steps are replayed in a fresh context on each run; the app does not use the user's personal browser profile.
Provide replacement credentials in the dialogue, or change authentication in the source editor, to replace credentials.
The next validation request and scheduled pull use the replacement immediately, without a server restart.
401/403 errors ask the user to update credentials and preserve previous indexed reports.
Tokens with an upstream expiry still need renewal when the provider requires it; automatic OAuth refresh is outside
the current GET adapter scope. References in downloaded scripts are environment placeholders for external execution;
running the source inside this app needs no environment setup.

Current adapter scope: **GET JSON and browser-based webpage extraction**, nested object/array paths, composite stable IDs, required/optional fields,
units, Markdown detail tables, metadata, source URLs, ISO/Unix timestamps, explicit deletes, committed cursor pagination,
and composable computed fields: arithmetic, counting, aggregation, ratios, conditional classification, filtering, grouping,
text parsing and time/date operations. Legacy `computed: {op:"duration", start, end, format, unit}` remains supported.
`hhmm` durations support `HH:MM` values that cross midnight; time units include seconds/minutes/hours/days.
Paths support `dot.path` and JSON Pointer (`/data/0/name`, with `~1` for `/` and `~0` for `~`).
API mode requests one bounded API batch. Browser mode can apply filters, paginate up to 100 list pages, follow actual
detail links or click-only cards, and merge detail fields into each record. Browser-generated POST queries and login requests
are supported; arbitrary API write operations and automatic OAuth refresh are not part of direct API mode.

### Browser recipes and generated scripts

Derived fields use `body_fields[].computed`, with expressions shaped as `{op, args, ...options}`. Inputs can be
`{"field":"path"}`, scalar constants, `{"literal":[...]}`, or nested expressions. `map` and `filter` evaluate the second
argument against each item; `$root.path` accesses the original record and `$records` the complete current batch.
`group_by` takes a grouping `path` and optional `aggregate` expression evaluated against each group's array.

```json
{"label":"场次数","computed":{"op":"count","args":[{"field":"show_times"}],"separator":"[|,;\\n]+"}}
```

For `show_times = "11:00|13:00|16:00"` this produces the integer **3**. Other examples:

- Revenue: `sum(map(lines, multiply(qty, price)))`.
- Margin percentage: `round(multiply(100, divide(subtract(revenue, cost), revenue)), digits=2)`.
- Paid-order totals: `sum(map(filter(orders, eq(status, "paid")), amount))`.
- Inventory classification: `if(gte(subtract(stock, reserved), threshold), "充足", "补货")`.
- Regional totals: `group_by(orders, path="region", aggregate=sum(map(current_group, amount)))`.

The last examples are explanatory notation; saved expressions are JSON trees, never executable formula strings.
`preview_calculation` tests a formula against an actual sample and reports its value/type/inputs; it does not replace
full capture validation or user approval. The same standard-library calculation engine ships in downloaded scripts.
Arithmetic uses decimal operations, with half-up rounding when `round` is requested. Missing input, unmatched text
or division by zero produces a raw-input warning, not an invented zero. An explicitly empty array has count zero.
Rules are bounded to 12 nested levels, 200 nodes, 1,000 collection items and 10,000 evaluation steps per record.
Formula-only edits reuse the current dialogue's validated full batch, including when the model calls
`test_browser_connector` again. A changed capture configuration/credential/timezone, a different collection date,
or explicit `refresh: true` requires a new capture. Loading a saved dialogue after browser release also revalidates
fresh input before approval. Reused captures are identified in progress and preview metadata.

Browser connectors set `source_type: "browser"` and include a validated `browser` recipe with `actions` and `output`.
Actions support `goto`, `click`, `fill`, `select`, `wait`, and `scroll`. Extraction uses CSS selectors and optional attributes,
arrays, simple regular expressions, source URLs, and the current date in the configured timezone. `output.kind` can be
`dom` for page rows/fields, with optional detail-page and pagination rules, or `network` for a JSON response actually
observed during the page's own requests. The result is normalized to a `records` array and goes through the same mapping,
validation, approval, atomic publication, stable-ID updates, and RAG indexing as API connectors.
Body fields and network/DOM fields support `source: "today"` for a date and `source: "now"` for an ISO timestamp.
These values come from the collection run in its configured timezone; they are never searched for on the website.
Network outputs can inject fields and traverse `detail.url_field` pages, just like DOM lists. Browser URLs retain SPA
hash routes, and Tailwind arbitrary-value class tokens are normalized into quoted class selectors.
A page without upstream
update timestamps should omit `updated_at_path`. Regular expression capture values require an explicit `group: 1`.
Trial results include field coverage counts so empty optional fields are visible rather than presented as complete data.

Browser trial validation now replays the **entire configured batch**, with the same record/page limits as scheduled runs;
it is not limited to the first three rows. Approval requires `browser_state.validation_scope = "full_batch"`.
The default `on_error: "raw"` keeps actual captured text when selectors or field formats drift, with `采集提示` and
`原始内容` tracker columns and run warning counts. It never fabricates hours or a zero duration. DOM data waits are
bounded to two seconds in this mode. `on_error: "fail"` restores strict field checks and six-second waits.
Stable IDs, valid configuration, transport failures and batch limits are still checked to protect existing records.
When a selector matches several nodes, its regex is applied to all candidates before choosing the first match.
Field `prefix` adds a declared prefix after extraction (for example to canonicalize an observed path onto the official public host).
The ordinary network guards still apply to the final URL. Browser resources accept gzip/deflate and enforce the size cap
on decompressed bytes.
Detail pages default to `detail.render: "auto"`: first read the actual source HTML and check the requested fields;
load JavaScript only if data is absent or actions are needed. `html` forces document-only loading and `browser`
explicitly loads dynamic content. Bulk collection skips unused per-page snapshots and unrelated third-party tracking
requests. The response reports request/byte totals and static/dynamic detail counts.
`browser.max_records` defaults to 50,000 and accepts 1–50,000; explicitly configured smaller limits are retained. It belongs beside `browser.output`. A misplaced `output.max_records` is repaired automatically;
unknown output keys report their exact path. The tool schema includes record/page limits and nested field options.
The browser allows up to 600 network requests and 500 MB of resource data per run. HTTP connections are reused only within one isolated browser run,
with separate TLS pools for each original host/IP. Public assets can be reused when explicitly cacheable; private assets
are keyed by authentication/cookies, and `no-store`/`no-cache` responses are not reused.

Use a real source status field to handle unavailable values: body/computed fields may declare
`required_unless: {path: "hours_status", regex: "^Closed"}`. A missing value is then blank, never zero, and coverage reports
it as `not_applicable`. Other records use the configured raw fallback or strict checks. An optional DOM `default` can give an honest
missing-information label when a loaded details section has no published hours; it must not invent an operating status.
The source status should itself be retained as a report/tracker field.

Within the current page users can keep refining an enabled connector. The approved source keeps running while a new
candidate is generated and validated. Approving that candidate updates the same feed and knowledge base; it does not
create a duplicate. The UI displays the latest run outcome, including recovery after a failed initial import.

A recipe must be replayed successfully in a fresh browser before its preview becomes ready. The model cannot submit
invented rows as proof. The UI shows the current browser screenshot through an authenticated owner-scoped endpoint;
screenshots are stored under the private data directory, not public media storage. Actual browser content and tools
remain available to the same 50-model-call/100-tool-call budget. Network and DOM excerpts are bounded so a large page
does not evict its entire tool response from the model context.

Browser observations contain visible text, cleaned semantic HTML, real element references (`@e12`), reusable CSS selectors,
repeated card/list structures and table cells. Scripts, SVG paths, framework/event attributes and navigation chrome are omitted.
Missing snapshot selectors return the current page and a warning immediately; partial/empty field extraction is not marked successful.
Transient element references work for live operations but cannot be saved in a replay recipe.
When the configured model accepts vision, a changed screenshot is also sent as a bounded JPEG alongside the DOM;
text-only models continue using DOM observations. The UI distinguishes queued images from images actually accepted by the model.

The collection workbench shows per-turn model timing, tool arguments, outcomes, record counts and elapsed time, plus input/output
tokens and cached tokens when the service reports them. Missing cache counters display **服务未上报**, not a fabricated zero.
Streaming requests ask for usage explicitly. `ConnectorEvent` stores the complete append-only activity and the page reconnects
using event cursors; the legacy last-180-event field remains for compatibility. Earlier retained events are preserved on migration.

The server uses Python **Playwright 1.58.0**, with Chromium or an existing Microsoft Edge installation. Windows browsers
run on a dedicated Proactor event loop so Django's ASGI event-loop policy does not prevent browser subprocesses.
Playwright's locator and network capabilities follow the [official browser documentation](https://playwright.dev/python/docs/browsers),
[locator documentation](https://playwright.dev/python/docs/locators), and [network documentation](https://playwright.dev/python/docs/network).
New installations should install the project requirements and run `python -m playwright install chromium` on hosts
without Edge. `FOS_BROWSER_EXECUTABLE` optionally selects an existing browser executable; users do not configure it in normal operation.

Browser traffic is proxied through bounded HTTP requests with pinned DNS/TLS checks. Private-network requests require
the administrator opt-in and an approved source origin; link-local/metadata addresses remain blocked. Service workers,
WebSockets and file URLs are not enabled. Images, media and fonts are skipped to focus on data and reduce load; public
scripts/styles are cached within each isolated run. A run is bounded to 50,000 records, 100 list pages, 30 setup actions,
600 actual network requests, 500 MB of resources and 1,200 seconds (20 minutes) of browser execution. The enclosing feed publication
budget and lease remain in effect, and a revoked task/lease stops further browser work. Requests to purchase, pay,
delete, or submit unrelated business changes are outside the collection flow.

CAPTCHA and interactive access controls are not bypassed. A page that requires an unsupported human interaction reports
that limitation; it is not replaced with fabricated records or a demand that the user write a JSON proxy.

The download contains `connector.py`, `connector.json`, `connector_runtime.py`, `feed_client.py`, `requirements.txt`,
`.env.example` and a README with a standard cron command. API scripts use **Python 3.10+ standard library only**.
Browser packages additionally include `browser_runtime.py` and `browser_dom.py` and declare Playwright and HTTPX, with browser installation instructions.
Run `python connector.py --preview` to inspect the normalized JSON. For external cron use, create a separate push source,
set `FOS_FEED_INGEST_URL` and `FOS_FEED_PUSH_TOKEN`, then run `python connector.py --push --state-file /path/checkpoint.json`.
The downloaded script does not load `.env` automatically: export variables in the job environment. For external webpage
login use `FOS_FEED_LOGIN_USERNAME`/`FOS_FEED_LOGIN_PASSWORD`. Internal scheduled sources resolve stored credentials automatically.
Choose either built-in pull scheduling or external script scheduling for a source to avoid duplicates.

### Optional web search configuration

Default search uses public DuckDuckGo HTML, which may be unavailable on restricted networks.
For a dedicated search provider set one of these in the web server environment:

```dotenv
# Optional: SearXNG endpoint with JSON output enabled; takes precedence.
FOS_FEED_SEARCH_URL=https://search.example.com/search
# Optional: otherwise use Tavily. Never enter the key in an agent prompt.
FOS_FEED_SEARCH_API_KEY=replace-with-tavily-key
```

Search uses the providers' documented [SearXNG search API](https://docs.searxng.org/dev/search_api.html)
or [Tavily search endpoint](https://docs.tavily.com/documentation/api-reference/endpoint/search).
No search provider is needed when the API documentation and sample are supplied directly.
Existing `FOS_LOCAL_ONLY` restrictions remain effective: external search, documentation and API calls require
an allowed network configuration. Offline users can supply a redacted sample and download the connector for an online host.

## Versioned batch contract

```json
{
  "schema": "fos-rag-feed/v1",
  "batch_id": "daily-report-20261031-attempt-1",
  "cursor": "opaque-next-checkpoint",
  "items": [
    {
      "id": "pump-001-daily-report",
      "title": "Pump 001 daily report",
      "content": "## Summary\n\nRunning hours: 12\nStatus: Normal",
      "format": "markdown",
      "source_url": "https://example.com/reports/001",
      "updated_at": "2026-10-31T08:00:00+08:00",
      "fields": {"运行时长": 720},
      "metadata": {"equipment_id": "pump-001"}
    }
  ],
  "deleted": []
}
```

Machine-readable schema: `docs/schemas/knowledge-feed-v1.schema.json`.
Reference files: `scripts/connectors/example_feed.json`, `example_api_feed.py`, and `feed_client.py`.
The UI's download button packages these files with this guide; no credentials or project data are included.

| Field | Meaning |
|---|---|
| `schema` | Required exact version string. Unknown versions/fields are rejected. |
| `items` | Required array, possibly empty. Each item requires stable string `id`, `title`, and nonempty `content`. |
| `id` | Stable identity within a source. Reuse it across collections: append mode stores a new observation; upsert mode updates the current row. Never generate a random ID every run. |
| `format` | `markdown` (default) or literal `text`. Convert JSON records/tables to readable content in the connector. |
| `fields` | Optional object of scalar values (≤ 20,000 serialized JSON characters per record, including field names and JSON structure). Shown as tracker-table columns; generated automatically by AI connectors from each mapped field, including computed durations. Whole-object metadata mapped to a column is preserved as JSON text within this budget. |
| `source_url` | Optional original HTTP(S) report link. No embedded credentials. Stored for provenance; RAG does not fetch it. |
| `updated_at` | Optional upstream ISO 8601 timestamp with timezone. Upsert mode prevents older timestamps overwriting a newer current item; append mode retains each observation. |
| `metadata` | Optional JSON object, at most 20,000 serialized JSON characters per record. Auxiliary metadata; put facts that must be searchable into `content`. The 500 MB total batch limit still applies. |
| `deleted` | Explicit deleted source IDs. Append mode retains history; upsert mode removes only current rows. Missing IDs are always preserved. A later observation can restore the source item. |
| `cursor` | Optional opaque string, at most 2,048 characters. Committed only after the whole batch publishes. An empty string resets it. |
| `batch_id` | Optional idempotency key, strongly recommended for pushes. Repeating a running/completed batch returns the existing run; a failed batch may retry with the same ID/content and produces a new attempt in history. Reusing an ID for different content is rejected. |

Limits: UTF-8 JSON, **500 MB per batch**, **50,000 total item upserts plus deletions**, 100,000 characters per item,
200 characters per ID/title, and no duplicate/conflicting IDs. There is no implicit snapshot deletion.
Whitespace-only content and non-finite JSON numbers are rejected. The server is authoritative for limits that JSON Schema cannot express.

## Scheduled HTTP pulls

The configured endpoint receives `GET`. After a successful batch with a cursor, subsequent requests include
`?cursor=<committed cursor>` (preserving other query parameters). Each run processes one bounded batch;
paginate in your connector and continue from the committed cursor on the next scheduled/manual run.
For a first historical collection, ask EnGenie to set the source's supported history/date range and larger batch size;
after it succeeds, refine the same task's date range and cron for daily collection. No separate backfill stage is required.
The source's own page-size limits still apply. Existing explicit `max_records`/`max_pages` settings are retained until edited.
The endpoint must return `200 application/json` and this contract. Empty `items` is a valid no-op.
For cursorless upsert feeds, an ETag may be saved and sent as `If-None-Match`; `304` records a successful unchanged run.
Append feeds do not send conditional ETags, so unchanged upstream content can be observed again at the next collection.
ETags are not reused across cursor changes. Redirects are rejected; configure the final API address.

An original API that returns another shape needs an adapter. Edit `map_record` in `example_api_feed.py`;
the script deliberately performs no guessing about API pagination, authentication or field meanings.
Emit logs to stderr, the single complete JSON object to stdout, and exit nonzero on upstream failures.
Do not turn an API error into an empty snapshot or inferred deletions.

### Legacy API authentication (optional deployment integration)

Existing sources may still use environment references. New users should use the page's credential form instead.
For deployment integrations, global administrators can map headers to **environment variable names**, for example:

```json
{"Authorization": "FOS_FEED_REPORT_AUTH"}
```

Set the variable in the server/scheduler environment (or the project's `.env`) to the full header value:

```dotenv
FOS_FEED_REPORT_AUTH=Bearer replace-with-upstream-token
```

Restart the worker after changing its environment. Values are never returned through source configuration.
Only names beginning `FOS_FEED_` are accepted; other application secrets cannot be referenced.
Private/loopback APIs require the global administrator's explicit **允许访问内网或本机 API** switch — in the data-source
form, or in the **内网配置** panel of the AI builder composer (see [Generate a connector from a prompt](#generate-a-connector-from-a-prompt)).
Link-local/metadata, multicast and unspecified destinations remain blocked. DNS is validated and pinned for each request,
TLS hostname verification remains enabled, environment proxies are ignored, and redirects do not forward credentials.

## Start a scheduler

Cron expressions use five fields: **minute hour day-of-month month day-of-week**. Numeric lists/ranges/steps,
English month/weekday names, and Sunday `0` or `7` are supported. If both day fields are restricted, either can match.
Times use the source's IANA timezone. DST missing times are skipped; repeated local minutes can have two UTC executions.
An overdue source runs once after downtime, then resumes its next scheduled time; missed intervals are not replayed in a loop.
See the primary [Cronie crontab specification](https://github.com/cronie-crond/cronie/blob/master/man/crontab.5).

Examples: `*/15 * * * *` every quarter hour, `0 8 * * *` daily at 08:00,
`0 9 * * mon-fri` weekdays at 09:00, timezone `Asia/Shanghai`.

**Linux cron** (use absolute paths and your deployment account):

```cron
* * * * * cd /srv/rag && /srv/rag/.venv/bin/python manage.py sync_feeds --due >> /srv/rag/logs/feeds.log 2>&1
```

**Windows or a foreground service**:

```powershell
.venv\Scripts\python.exe manage.py run_feed_scheduler --interval 30
```

Run this as a supervised service or a Windows Task Scheduler task that starts at login/startup and restarts on failure.
For a single scan use `run_feed_scheduler --once`; for one specific pull use `sync_feeds --source SOURCE_UUID`.
The development Web server does not itself start a scheduler. The page displays the last scheduler scan, so a saved cron
is not mistaken for a running worker. Multiple workers/overlapping cron processes share a per-source database lease.

## Push from a script

Create a **外部脚本推送** source, generate its token, and keep the token in the connector's environment:

```text
FOS_FEED_INGEST_URL=https://your-rag.example/kb/api/feeds/SOURCE_UUID/ingest/
FOS_FEED_PUSH_TOKEN=the-token-shown-once
UPSTREAM_API_URL=https://your-existing-api.example/reports
UPSTREAM_API_AUTH=Bearer upstream-token
```

```bash
python scripts/connectors/example_api_feed.py | python scripts/connectors/feed_client.py -
```

Or call the API from any language:

```http
POST /kb/api/feeds/SOURCE_UUID/ingest/
Authorization: Bearer SOURCE_TOKEN
Content-Type: application/json

{ "schema": "fos-rag-feed/v1", "batch_id": "delivery-001", "items": [], "deleted": [] }
```

`202` means accepted, **not yet published**. The response contains `run.id`, `run.status` and `status_url`.
Poll that URL with the same Bearer token until `completed` or `failed`. The example client waits by default;
`--no-wait` returns after acceptance. A duplicate completed batch returns `200` with its existing run.
`400` indicates invalid input, `401` an invalid token, `409` a busy source, and `413` an oversized body.
On a network timeout, check the run or resend the same batch ID; never invent a new item ID to retry.
Tokens authorize this source only, are stored as hashes, and are revoked by rotation or pausing the source.

**Local command alternative**, without an HTTP token:

```bash
python manage.py feed_import scripts/connectors/example_feed.json --validate-only
python scripts/connectors/example_api_feed.py | python manage.py feed_import - --source SOURCE_UUID
```

The local command runs under deployment credentials and still checks the source owner's permissions.
It exits nonzero on failure. It is suitable for a cron job with the connector's own cadence; no web UI script execution is involved.

## Publication, failure and retrieval behavior

Stable IDs map to **tracker-table observations** (append) or one current row (upsert) in the source's knowledge base.
Publication enables that KB's tracker table (`追踪表`) by default and derives its columns from the mapped fields plus
`标题 / 更新时间 / 来源链接 / 采集时间 / 来源ID`; each item's
structured values (including computed durations) are written through `item.fields`. Rows have no per-record Markdown
document — no per-record sub-library is created and no vector indexing
runs for feed records. The whole batch publishes in a single database transaction: upserts, explicit deletions and the
cursor/ETag commit together, so a failed fetch or validation keeps every previous row exactly as it was.

Runs are limited to 60 minutes with a 65-minute fencing lease; a stopped worker's lease is recovered by the scheduler. Browser collection has a separate 20-minute limit, with a 10-second outer cancellation margin. Individual navigation, element and network timeouts are unchanged. These are ceilings and do not delay completed work; collection scheduling is sequential, so a slow source can occupy the scheduler until it completes or reaches its limit.

The AI preview lets users select multiple output fields before confirming. All mapped fields are selected by default;
unselected business fields are omitted from future records, while stable IDs and inputs needed for calculations are kept
in the capture configuration. `采集日期` is always retained and generated by the collector in the task's time zone
(default `Asia/Shanghai`), rather than searched for on the website. Existing historical rows and columns are preserved
when editing a source. The standard title, source and collection metadata remain available.

`manage.py build_feed --job JOB_ID --approve` waits for the first import to finish before exiting. A failed first import
returns a nonzero exit status and keeps the approved schedule available for retry. Use `--no-first-run` to approve and
schedule without importing immediately; web approval continues to run its first import in the background.
Deletion in upsert mode removes only the current tracker row and container document; historical snapshots are retained.
Any legacy vectors from older deployments are
cleaned best effort. An existing manually configured tracker keeps its enabled flag; feed publication only appends
missing columns (bumping the field-schema version so rows can be marked for review). Normal KB permissions apply.
Automatic updates do not auto-approve graph facts or invoke an LLM; graph extraction and per-item confirmation remain explicit.

## Parameter analysis in business-data details

Open a business library's detail page from **业务数据 → 查看追踪表**. The main view provides an item selector,
parameter selector, inclusive start/end date-time filters, and hourly/daily/weekly/monthly/yearly grouping.
It defaults to **表格**; the **表格 / 图表** switch changes the presentation without resetting filters or statistics.
The chart and **周期分析表** show period aggregates and changes compared with the previous period containing valid data;
the metric selector supports mean, latest valid value, minimum, maximum and sum. Single-value summary cards are omitted;
the sample count covers the entire filtered range. Text/status parameters instead show value frequency and change counts. Opening/closing times are
interpreted as `HH:MM[:SS]` clock times, with differences shown as elapsed minutes; sums are unavailable for clock times.
Clock means are linear within the day, not calculations of overnight duration. Select a derived duration field for that.

Objects are matched by source plus stable external ID, never just by title. Sampling time uses the stored batch's
`started_at`, then (for the single legacy/upsert row) saved `采集时间`, then the row's update time. Naive filter input
and calendar periods use the source task's time zone. Missing/non-numeric values are excluded from numeric metrics,
with counts shown; missing periods are not filled with zero. Charts/tables display at most the latest 120 sampled periods,
with an explicit notice if older periods are omitted; the sample count still includes all matching observations.
Object searches return at most 200 deduplicated choices. Analysis streams one selected parameter from the database;
it does not send all raw history to the browser or invoke an LLM.

The original records table is collapsed by default. Expand it and select **趋势分析** on a row to analyze that item.
Existing management permissions apply. This view does not change collection scripts or write modes; a task using
overwrite mode can only analyze observations still stored in the tracker, and a single observation initially gives one point.

Run history reports created/updated/deleted/unchanged counts and failure reasons. `stale` is a subset of unchanged items
whose upstream timestamps were older. Pausing blocks future scheduled/push updates; an already accepted run can finish.


### Manual M365 report controls

Each configured report appears on its feed card in `kb/feeds`, with creator-only controls to pause/resume delivery and edit its frequency. Presets cover daily, weekly and evenly spaced hourly delivery; custom five-field cron, time zone, and on-demand-only mode are also supported. Saving a frequency preserves the paused/enabled state; resume computes the next future occurrence and never sends immediately. Pausing cancels future scheduling and any claimed delivery still preparing; a request already submitted to M365 may complete. Collection configuration, collection enablement, report content and encrypted workflow URLs are not changed. Legacy after-collection reports remain supported.
