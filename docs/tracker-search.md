# Tracking-table tools for the chat agent

The chat agent uses three analysis tools. None modifies records or starts a collector.

- `tracker_preview`: inspect actual field names, sample types, two small examples and row counts.
- `tracker_lookup`: filter all matching database history, select columns, sort, paginate or compute summaries.
- `tracker_analyze`: use the same summary/period comparison as the KB page and generate Excel or SVG plots directly from the database, in both ordinary and enhanced modes.

All tools require an explicit knowledge-base slug; chats never pick a default. For preview/lookup, a library with its own tracking table uses that table;
otherwise the tool can find tables on its children or parent, within the caller's knowledge-base scope.
The preview returns `kb_slug` so the agent can address a particular table directly.

## Period analysis and files

```json
{
  "kb_slug": "park-hours",
  "mode": "trend",
  "operation": "avg",
  "measure": "等候时间",
  "object_field": "项目名称",
  "object_values": ["海盗演出", "飞越地平线"],
  "date_from": "2026-11-01T00:00:00+08:00",
  "date_to": "2026-11-08T00:00:00+08:00",
  "period": "hour",
  "span": 1,
  "output": "both"
}
```

`mode="options"` discovers numeric fields and exact object values. `summary` accepts
one `group` field (blank means overall); `trend` compares up to 8 objects in up to
500 periods. Time bounds filter **record update time**, not a business-date column.
Local times use the selected feed/site timezone. Datetime ends are exclusive;
date-only ends include that calendar day. Missing periods stay blank, and a zero
previous value has no percentage change. `chart_metric` selects `value`, `change`
or `change_pct` for trend plots.

`output="excel"`, `"plot"` or `"both"` creates real files and returns `files` with
authenticated download URLs (24-hour lifetime, bound to the requesting account).
Excel includes all matching groups, up to 50,000, regardless of JSON preview or
page pagination, with numeric values, validity counts, change/rate columns and
query metadata. SVG summary plots allow up to 50 groups. Oversized exports fail
explicitly instead of producing a silently partial file. Source text is written
as Excel text, never as executable formulas. Enhanced publication rechecks both
the database result and file fingerprints. Downloading rechecks current KB access.

The KB page's **Export Excel** button exports the last successfully applied
analysis settings. Changing settings disables it until the new analysis is applied.

## Preview

```json
{"kb_slug":"park-hours"}
```

The preview is a schema/sample inspection, not evidence of full-table totals. `row_count` includes all
statuses; `completed_rows` counts completed records. `next_offset` pages tables and
`next_field_offset` pages schemas with more than 40 fields. Sample types describe only the examples.

## Filter and select data

```json
{
  "kb_slug":"park-hours",
  "columns":["项目名称","采集日期","场次数"],
  "filters":[
    {"field":"项目名称","op":"contains","value":"海盗"},
    {"field":"采集日期","op":"between","type":"date","value":["2025-01-01","2025-12-31"]}
  ],
  "order_by":[{"field":"采集日期","type":"date","direction":"asc"}],
  "limit":20,
  "offset":0
}
```

Filters are combined with AND and run before counting/pagination. Supported operators are
`eq`, `ne`, `contains`, `in`, `between`, `gt`, `gte`, `lt`, `lte` and `is_empty`.
Dates use `YYYY-MM-DD`; range bounds are inclusive. Use `type:"number"` to compare numbers stored as text.
`is_empty` takes a boolean value. Defaults return only `status:"done"`; pending/proposed/failed rows
require an explicit status, or `"all"`. Unapproved proposals are not substituted for saved values.

`total_matched` counts all filtered history, including records beyond the former 200-row cutoff.
It is not the number returned in the current page. Follow `next_offset` while `has_more` is true.
Rows are ordered deterministically with a row-ID tie break. Offset pages describe the database at query time;
concurrent inserts or edits may change page boundaries. Query snapshots are rechecked before enhanced answers publish.

`$row_id`, `$source_id`, `$updated_at`, `$status` and `$document_name` are supported filter/sort fields for record metadata.
Select 1–12 business columns; the default is the first six fields plus `采集日期` if present.
Keyword `query` searches document names, source IDs and configured field values across all history;
large schemas with over 80 fields require a narrower library or explicit filters.

## Summarize without loading the full table

```json
{
  "kb_slug":"park-hours",
  "filters":[{"field":"项目名称","op":"eq","value":"海盗演出"}],
  "metrics":[
    {"op":"count","as":"记录数"},
    {"op":"sum","field":"场次数","as":"总场次"},
    {"op":"avg","field":"持续时长","as":"平均时长"}
  ],
  "group_by":[{"field":"采集日期","period":"month"}]
}
```

Metrics run in the database across **all filtered rows**. `limit`/`offset` page the groups, not the
records included in each calculation. Supported operations: row `count`, field `distinct_count`,
numeric `sum`, `avg`, `min`, `max`. Numeric operations accept numeric strings, exclude missing/non-numeric values,
and report `valid_numeric_values`. Empty numeric aggregates return null, not an invented zero.
Grouping periods are `value`, `day`, `month`, `year`; month includes the year, allowing year-over-year comparisons.
Counts represent stored observations. For a count of unique attractions use `distinct_count` on their stable identifier.

## Context and source safeguards

- Up to 100 rows/groups per page, 10,000 characters per tool response and 20,000 characters shared across
  both tracking tools in one answer. Parallel calls share the same locked budget.
- Cells default to 800 characters. A shortened value is an object with `text`, `truncated:true` and
  `original_chars`. It is never silently presented as the whole cell.
- To read a long cell, filter by `$row_id`, select just that column and increase `cell_chars` (maximum 8,000).
  The response and turn limits still apply. Prefer database summaries to paging an entire table into the model.
- If a page cannot fit, the response says no records were returned and asks for fewer columns; it does not
  advance the offset past unseen records. Exhaustion is reported explicitly with `budget_exhausted`.
- Structured parameters use allowlisted operations and ORM expressions, not model-generated SQL or Python.
- Lookup evidence includes the applied scope/filters and result digest. Enhanced answers replay the read-only query
  with current permissions before publication. Changed results or revoked access invalidate the old evidence.
  Conversation history uses the same validation for tracking-table citations.

These bounds cover tracking-table output. Other retrieval tools and the model's configured context window have
their own limits; this does not claim a token-based budget for the entire chat request.
