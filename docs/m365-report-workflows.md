# Scheduled reports through Microsoft 365 Workflows

Open an enabled data collection task in EnGenie and describe the report in the existing chat. Paste the workflow's HTTP trigger URL, say whether to send email or a Teams card, and provide the email recipient when needed. Report download links automatically use this Django application's deployment address; users do not need to supply a separate report site. You can provide everything in one message or continue the conversation.

There is no separate setup form. EnGenie checks the saved task and delivery details, then asks only for missing information using `ask_user_questions`. Its short form appears inside the conversation; users may fill it in or answer directly in the chat. Form answers use the same conversation and preserve its prior context. The supported input types are text, email, URL, date, number, selection and multiline text, with at most eight questions at a time.

Continue the same builder conversation to request the report. EnGenie can inspect the complete saved dataset and its date coverage, select a date range, filter rows, select and rename fields, calculate derived values, and perform grouped or overall aggregation. Missing date range, processing rules/fields and delivery timing are asked in the chat. Raw data and aggregation are separate report choices. Previewing a report does not recrawl the website or change the collection script. Download the Excel preview or use **发送测试报告** for one explicitly requested send. **确认并启用报告计划** approves an independent report schedule. For example, collection can run hourly while a report is sent every Monday at 09:00 for the preceding calendar week. Each report occurrence reads the saved data and recalculates its range and aggregates at delivery time. A preview uses the actual current time for relative data windows. The next delivery timestamp is shown separately and must never be interpreted as today. Each actual scheduled occurrence still evaluates its window at the scheduled timestamp. A currently empty future window can still be configured; no rows are fabricated. Sending against an empty window records a visible failure and does not send a misleading empty report. A manual report can be saved without enabling a schedule. The collection task, its schedule and its enabled/paused status are independent.

Numeric post-processing preserves the source track-sheet cells. `parse_number` accepts numbers and numeric strings, with optional bounded regex/group extraction, decimal/thousands separators and a scale factor. It raises on ambiguous or invalid input by default; explicit `on_error=null` preserves an unavailable result as missing, never zero. `preview_report_calculation` tests distinct real inputs before applying the rule across history. Previews show conversion samples and valid/missing counts; Excel includes a Transformations sheet and deliveries include a quality summary. Group totals with no valid values remain empty.

All EnGenie chat model requests receive one shared runtime date/time/timezone, including the main/floating assistant and both workbench chats. The clock is appended only at the request boundary so stored messages and reusable prompt prefixes remain unchanged.

Ordinary questions are answered in the same chat without generating a new connector or report preview. Replies remain in saved context, and users can continue editing afterward. Each turn includes the runtime clock in the application timezone; `get_current_time` can refresh it, and `inspect_task_history` explains saved failures without refetching data.

## Automatic app address

The authenticated builder request captures Django's validated scheme, host, port and script prefix for the current task. This address is retained in the encrypted report plan for background cron runs and reused in the conversation. `get_report_site_address` can inspect the same information; no external lookup is needed. Opening a task on a new public deployment refreshes an automatically captured address. An explicitly chosen per-task address remains an override.

For FRP or other tunnels accessed locally, set `FEED_REPORT_PUBLIC_BASE` once in the deployment's environment, including its public port and optional mount path. This provides the application address even when the browser uses localhost or a cron/CLI entry has no HTTP request. Without that setting, a local-only request cannot reveal a hidden external port mapping; EnGenie asks about deployment configuration only in that case. Loopback and unspecified addresses are never used in emailed links.

Behind a trusted reverse proxy that overwrites the forwarded host and protocol headers, opt in with `DJANGO_TRUST_PROXY_HEADERS=True`; otherwise these headers are ignored. Django's `ALLOWED_HOSTS` validation still applies. See [Django's request documentation](https://docs.djangoproject.com/en/6.0/ref/request-response/#django.http.HttpRequest.get_host) and [proxy configuration](https://docs.djangoproject.com/en/6.0/ref/settings/#secure-proxy-ssl-header).

## Workflow setup

Email workflows must expose an HTTP request trigger accepting three string inputs:

```json
{"t":"recipient@example.com","s":"Report title","b":"<h2>Report</h2><p>HTML body and Excel download link</p>"}
```

Map `t` to To, `s` to Subject and `b` to the HTML Body of the email action. Interactive Teams `RunFromLink` sharing links cannot be invoked as background HTTP triggers.

Teams uses the Workflows incoming-webhook template with an Adaptive Card message envelope. The card contains a short report preview and an OpenUrl action for the complete Excel file. See [Microsoft's Teams webhook documentation](https://learn.microsoft.com/en-us/microsoftteams/platform/webhooks-and-connectors/how-to/connectors-using).

Recipients must be able to access the configured public site address. Download links carry unguessable signed tokens, expire after seven days and stop working if the report owner's knowledge-base access is revoked. The complete workbook uses an ASCII filename and supports Unicode cell contents. Source text is written as literal Excel text rather than formulas.

## Delivery and operational behavior

- Workflow URLs are encrypted with the existing feed-credential key and stored separately by email/Teams type. Switching types selects only a saved link of that type; otherwise EnGenie asks for the corresponding link. A saved email URL never satisfies a Teams destination. The model receives the saved types and selected-type readiness without the secrets. They are not returned by settings APIs or passed to the model. URLs pasted into an existing task's follow-up request are extracted before the request is saved or sent to the model.
- Changing the destination pauses scheduled report delivery until another report approval. Email and Teams are alternative destination types for a task's report plan.
- Reports run on their own five-field cron and timezone, or are sent manually. A report uses already saved records even when collection is paused or its latest run failed. It never triggers another collection. Older approved collection-triggered reports retain their existing behavior until explicitly revised.
- Each report plan/scheduled time has one delivery record. Overlapping scheduler ticks claim an occurrence once; missed occurrences are coalesced rather than sending a backlog. Collection-triggered legacy plans retain their per-run delivery key. HTTP acceptance means the workflow accepted the request; final action success belongs to Power Automate's run history.
- An ambiguous network outcome is recorded as `unknown` and is not automatically retried. Check the workflow's run history before requesting another test.
- Raw exports are limited to 5,000 matching rows, 30 columns and two million characters. Aggregations stream all matching historical records; the 5,000 limit applies to output groups, not input records. No partial data is silently exported. Date ranges can use collection time or a saved ISO date/datetime field. Supported windows include all history, latest snapshot, today, yesterday, rolling days (optionally excluding today), explicit dates, previous calendar week and previous calendar month, evaluated in the report timezone. An overall aggregate does not require grouping.

`run_feed_scheduler` runs independent collection and report loops so a slow collection cannot delay the report loop. Existing system cron deployments using `sync_feeds --due` scan reports before starting collections. `run_report_scheduler` is also available as a standalone report-only process (`--once` for one tick).

Deployments must apply migrations `0049_feed_report_workflows` and `0050_independent_report_schedules` and preserve the existing credential encryption key alongside the database. Excel artifacts and delivery audit records are stored in the database. Expiry controls download access; it does not delete audit records or artifact bytes.
