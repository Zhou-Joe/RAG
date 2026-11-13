# Table Query Libraries

For business-data and table-query libraries, **Displayed Fields** also limits EnGenie. Hidden fields are omitted from field discovery, samples, query results, keyword matching and calculations, including cron/report data previews. They remain stored and become available again when unhidden. Document children inherit their library's selection; while fields are hidden, EnGenie uses structured queries rather than reading the unfiltered source table. A selection change refreshes saved model context so old tool results cannot reintroduce hidden data; the visible conversation history is preserved.

When creating a manual knowledge base, enable **Use for Table Queries** for flat Excel or CSV data used for filtering and statistics. Leave it off for document questions and semantic search. Existing knowledge bases keep their current behavior; the purpose is chosen at creation.

Table mode accepts XLSX, XLSM, CSV and TSV. Each sheet's first nonempty row is its header. Duplicate or blank headers receive distinct field names, and the source header mapping is retained in the document manifest. Every following nonempty row is stored with its original file, worksheet and row number. Multiple sheets and uploads contribute to the library's available fields.

The original uploaded file is retained. Table rows do not pass through AI extraction or embedding, so the document extractor's 24,000-character input and 2,000-character output limits do not shorten imported data. Formulas are retained as source strings and are not calculated; export calculated values from Excel if those values must participate in numeric statistics.

Use the knowledge base's record browser for data and summary statistics. EnGenie discovers the actual fields with `tracker_preview` and filters, groups or aggregates all matching rows with `tracker_lookup`. Business-date filtering and grouping use the selected spreadsheet field. The existing page's date ranges and period charts use import/update time, as identified in its table-mode guidance.

Display previews remain bounded and visibly identify shortened cells. Long imported text cannot be replaced through a shortened editor preview. XLSX export refuses cells above Excel's 32,767-character limit instead of silently shortening them; download the original source file to retain those values.

Parsing retains the existing 25 MiB file, 100 MiB expanded Excel archive, 100,000 physical rows per sheet and 1,000-column bounds. Exceeding a bound fails the import rather than publishing partial rows. Reprocessing a document replaces its rows atomically; failed replacements preserve prior data.

Apply migration `0058_table_query_libraries` before restarting with this feature. No existing documents are automatically converted or reindexed.
