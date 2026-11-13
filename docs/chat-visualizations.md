# Interactive Charts in EnGenie Chat

Normal EnGenie chat can display interactive charts directly below an answer. It uses the same bundled offline SVG renderer as the EnGenie Digest HTML reports. Hover, focus, or tap a plotted point, bar, or slice to see its value. Line markers remain small while invisible hit areas make them easier to select. Legends can toggle individual series.

## Tracker Analysis

`tracker_analyze` with `output="plot"` or `"both"` publishes a chart from the full permission-checked result, alongside its existing downloadable files. The `chart_type` argument accepts `auto`, `line`, `bar`, `pie`, `combo`, `area`, `scatter`, and `doughnut`. Auto selects bars for summaries and lines for trends.

Summary charts support up to 50 groups. Trends support up to eight series and 500 periods. Missing values remain gaps, zero remains zero, and change/percentage metrics retain their actual values. Pie charts require one nonnegative count or sum series; combo charts require multiple trend series; scatter charts require numeric summary groups. Invalid requests return a useful error before creating a file.

The chart is attached to the existing tracker citation snapshot, outside the model's bounded textual preview. Publication and history access rebuild and compare it with the underlying query result. Refreshing a conversation restores its tracker charts only while the associated source data and permissions remain valid.

## Python Analysis

The existing browser Python sandbox captures standard Matplotlib line, bar, pie, doughnut, scatter, and mixed bar/line figures from their plotted artist data. Ordinary Python chart requests therefore gain inline interactivity without asking the model to generate JavaScript or a second copy of its data.

PNG downloads and PDF export remain available. Unsupported or oversized figures retain their image presentation; image copies of successfully converted charts are grouped beneath the interactive preview. Browser-generated chart results have the same session lifetime as the existing sandbox output; tracker citation charts are persisted with conversation history.

## Rendering and Language

Only bounded chart data and typed options reach the shared renderer. Titles, category names, and series labels use text nodes. Model-produced HTML and scripts are not accepted as chart definitions. All assets are served by the app, with no external chart service.

The chat page loads the chart renderer in both standalone and embedded modes. Shared tooltip resources are removed when conversations are cleared or redrawn. System controls and validation errors support the interface language; source names and plotted content retain their original wording.

Deploy by collecting static files and restarting the web service. This feature adds no database migration.
