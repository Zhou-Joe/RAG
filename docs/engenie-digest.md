# EnGenie Digest

EnGenie Digest is an optional stage for scheduled and condition-based M365 report delivery. It adds a model-written summary and deterministic aggregate figures to the email body or Teams message. It is off by default.

Open the report's **Add EnGenie Digest** action to enable it, set instructions and choose English or Simplified Chinese. Digest-only edits retain the report's schedule and paused state. The frequency editor can save the schedule and digest together.

The existing cron task chat can create, modify or disable a digest using `preview_engenie_digest`. For example: “Add an English digest to this report. Include total wait time and average wait by attraction, and highlight missing values. Keep the existing schedule and destination.” EnGenie resolves requested metrics and grouping to actual report output columns, presents a preview, and uses the existing confirmation step before enabling the updated report. Collection settings are not rebuilt.

Conditions are evaluated on saved report data before the digest model is called. Unmatched or already-notified checks do not call the model. At delivery time the report range, filters, computed columns and aggregates are recalculated; the digest uses that result rather than an old preview. A digest error prevents the workflow request. Pausing or editing the report during generation cancels the pending send. An uncertain workflow response retains the existing duplicate-send protection.

The model receives exact whole-result metrics, explicit quality counts and bounded examples. It has no tools, recipient information or workflow credentials. Metrics operate on the final report output: if the report is already grouped, its output rows are groups, not the original source records. Configure report-level calculations when a source-weighted result is required. Numeric sums use Decimal; averages are rounded to at most 12 decimal places.

Optional grouped digest detail includes up to 20 groups ranked by row count, with omitted group and row counts disclosed. Whole-report metrics always include the complete selected result. Teams displays a compact set of figures; the delivered Excel file includes readable **EnGenie Digest** and structured **Digest Aggregates** sheets. All computed facts and generation metadata are retained on the delivery record.

The model request has a 45-second deadline and does not retry automatically. Digest instructions are limited to 2,000 characters, generated prose to 2,500 characters, and custom metrics to 12. Existing reports without a digest follow the original path.

## HTML Reports and Source Links

Choose **Digest Body**, **HTML Report Link**, or **HTML Email Attachment**. Report links work in email and Teams and use the existing signed, expiring report access rules. Attachments are email-only and can be delivered without a public app address. Source links are an independent option: they come from actual HTTP(S) URL values in the selected report columns; EnGenie does not invent URLs or scan hidden fields. Unsafe or credential-bearing URLs are excluded and omissions are disclosed.

The offline HTML template includes an overview, KPI cards, aggregate details, source links, and a searchable, sortable, paginated table. Standard views include line, bar, pie and combo charts. EnGenie can configure additional area, scatter and doughnut views, dual axes, and layout options through the same bundled SVG renderer. Every chart uses reviewed output fields and full-result aggregation; models cannot inject JavaScript, literal fabricated datasets or external chart dependencies. Omitted chart groups are explicit, and pie charts include an Other slice when groups are capped.

Custom chart settings live in `digest.charts` (up to eight). Omitting this setting selects four automatic views; an empty list disables charts. Definitions select `type`, `x`, `series`, and typed `options`. Set these through the cron chat and review the generated HTML preview before confirming the report. HTML preview downloads require the report owner and reject a stale preview when its source data has changed.

HTML files are limited to 1,000,000 UTF-8 bytes and are never silently truncated. They include their chart/table code and do not fetch external assets. Linked HTML is served with a sandboxed origin, while source/model values are escaped and chart configuration is data rather than executable code.

For email attachments, update the existing Power Automate HTTP trigger to accept an optional `attachments` array and map it to the email action's Attachments input:

```json
{
  "attachments": [
    {"Name": "EnGenie-report.html", "ContentBytes": "BASE64_UTF8_HTML"}
  ]
}
```

The original `t`, `s`, and `b` email fields remain present. After mapping the array, confirm **This email workflow maps attachments** in the task settings. The app blocks attachment sends until this capability is confirmed; replacing the email workflow URL requires reconfirmation. No cloud workflow is changed automatically. Microsoft documents the email attachment `Name` and `ContentBytes` fields in the [Office 365 Outlook connector](https://learn.microsoft.com/en-us/connectors/office365/).

Deployment requires migration `0061_feedreportdelivery_html_content` and its preceding migrations, static collection and restart of the web and report scheduler processes. Tests mock model and workflow requests; no email or Teams test messages are sent.
