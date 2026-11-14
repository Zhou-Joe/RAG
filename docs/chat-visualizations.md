# Interactive Charts in EnGenie Chat

Normal EnGenie chat can display interactive charts directly below an answer. It uses the same bundled offline SVG renderer as the EnGenie Digest HTML reports. Hover, focus, or tap a plotted point, bar, or slice to see its value. Line markers remain small while invisible hit areas make them easier to select. Legends can toggle individual series.

Interactive tables and answer tables start expanded. Charts, single-value cards, and generated image previews start compact. Click the card header or **Expand** to use the chat's full content width; **Collapse** returns it to the compact size. Each card retains its own size choice during streamed redraws and interface language changes, including tables explicitly collapsed by the user. Compact tables scroll within their card. Generated image previews can also expand or collapse by clicking the image. Selected KB photos, single-value cards, and charts share a wrapping row within each answer; expanding one card uses the full row, and collapsing it returns it to the compact layout. KB image captions retain the numbered document source and exact page when available. Uploaded photos keep their original presentation.

Single-value cards display only a title and highlighted numeric value, with an optional unit included in that value. KB/source subtitles and record/quality counts are not shown on the card. Source metadata remains in the saved evidence and numbered citation list. A true zero displays as zero; an unavailable value displays as a dash.

Simple exact numeric questions, including casual questions, default to a compact value card above the answer followed by its citations. The card is the answer: do not repeat its value in prose or add unrequested insights, caveats, suggestions, or boilerplate. Explanations remain appropriate when requested or when the answer needs clarification. Text-only and table-only requests take precedence. Comparisons use bars and time changes use lines. Plots use the selected fields and the same verified query result; record update time must not be presented as a business-date axis. Download links are included when the user asks for an export.

`tracker_lookup` defaults to `presentation="auto"`: one ungrouped aggregate or one explicitly selected numeric column with a unique result gets a source-validated card automatically. A latest-value query must explicitly sort by the real relevant time field and use `limit=1`; an arbitrary first page is never turned into a KPI. `card_title` and the known `unit` control the compact label. Null, nonnumeric, boolean, grouped, multi-value, and truncated results do not invent a scalar. Set `presentation="text"` for an explicit text-only answer. Internal and legacy queries retain their original text-only result format.

Exact manual/specification values use `kb_show_value` after reading and selecting the supporting passage. The supplied quote must be present verbatim, with the exact numeric value and its original unit together; the tool cannot calculate values or convert units. Both normal and enhanced chat support it. Saved cards retain source fingerprints, and publication/history checks reject modified cards or changed sources. Enhanced verification checks the visible card facts even when the textual answer consists only of citations.

## Tracker Analysis

`tracker_analyze` defaults to `output="auto"`: an ungrouped summary displays a value card, while other modes return a preview. Explicit `output="preview"` remains available for text-only output.

`tracker_analyze` with `output="plot"` or `"both"` publishes a visualization from the full permission-checked result, alongside its existing downloadable files. The `chart_type` argument accepts `auto`, `metric`, `line`, `bar`, `pie`, `combo`, `area`, `scatter`, and `doughnut`. Auto selects a metric card for one ungrouped summary value, bars for grouped summaries, and lines for trends. Explicit metric mode requires exactly one ungrouped value.

Summary charts support up to 50 groups. Trends support up to eight series and 500 periods. Missing values remain gaps, zero remains zero, and change/percentage metrics retain their actual values. Pie charts require one nonnegative count or sum series; combo charts require multiple trend series; scatter charts require numeric summary groups. Invalid requests return a useful error before creating a file.

The chart is attached to the existing tracker citation snapshot, outside the model's bounded textual preview. Publication and history access rebuild and compare it with the underlying query result. Refreshing a conversation restores its tracker charts only while the associated source data and permissions remain valid.

## Extended Business Views

`tracker_visualize` adds the following views in normal and enhanced chat. It queries the current visible fields directly, keeps the full visualization payload outside the model's text preview, and saves permission-checked source snapshots for history restoration.

| View | Required data | Bounds and behavior |
|---|---|---|
| Progress / target bar | One count or numeric aggregate, explicit positive target | Actual values above target remain visible; the physical bar caps at 100%. No invented target. |
| Sparkline | Actual business-date `x_field`, one count/numeric aggregate | Day/month/year buckets; up to 500 periods; preserves real time spacing and missing values. |
| Heatmap | Two actual grouping fields, one aggregate | Up to 32 categories per axis and 1,024 cells; missing combinations remain missing. |
| Pareto | Category field, nonnegative count or sum | Up to 50 categories; descending bars and cumulative percentage use all matched contributions. |
| Timeline / Gantt | Label and ISO start/end fields, optional group | Up to 100 tasks; real time positions and durations; rejects reversed or missing dates. |
| Interactive table | Selected columns and optional row filters | Up to 12 columns and 500 loaded rows; search, column filter, sorting and pagination apply only to loaded rows. Loaded/total counts and truncation remain explicit. |

Interactive tables start expanded in the shared visualization row; the other five views start compact. All support expansion/collapse, light/dark themes and bilingual controls. Interactive table search/sort/page preferences survive redraws and language changes. Oversized charts reject the request rather than silently omitting groups; only tables can show an explicitly marked subset. Hidden fields remain unavailable.

Digest HTML reports support the same views with declarative field-based specs. Progress adds `target`/`unit`; heatmap adds `y`; timeline references `label`/`start`/`end` and optional `group`; table uses a `columns` list. Sparkline and Pareto use `x` with one series. All renderer dependencies are bundled in the HTML for offline use.

## Python Analysis

The existing browser Python sandbox captures standard Matplotlib line, bar, pie, doughnut, scatter, and mixed bar/line figures from their plotted artist data. Ordinary Python chart requests therefore gain inline interactivity without asking the model to generate JavaScript or a second copy of its data.

For a computed scalar, Python analysis can call `engenie_metric('Total Requests', len(df), unit='', subtitle='Selected Period')`. The helper accepts a finite scalar or `None`, and optional text units/subtitle. Multiple calls create separate cards, within the eight-visualization limit. Scalar values are taken from the executed analysis rather than a second model-authored data array.

PNG downloads and PDF export remain available. Unsupported or oversized figures retain their image presentation; image copies of successfully converted charts are grouped beneath the interactive preview. Browser-generated chart results have the same session lifetime as the existing sandbox output; tracker citation charts are persisted with conversation history.

## Rendering and Language

Only bounded chart data and typed options reach the shared renderer. Titles, category names, and series labels use text nodes. Model-produced HTML and scripts are not accepted as chart definitions. All assets are served by the app, with no external chart service.

The chat page loads the chart renderer in both standalone and embedded modes. Shared tooltip resources are removed when conversations are cleared or redrawn. System controls and validation errors support the interface language; source names and plotted content retain their original wording.

Card surfaces, titles, controls, chart labels and grid lines follow the app's active light/dark theme. Chart series use a brighter dark-theme palette; pie/doughnut surfaces and center labels change with the theme without rebuilding the chart. Original photos retain their original colors.

Deploy by collecting static files and restarting the web service. This feature adds no database migration.
