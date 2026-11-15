# Task builder requests and credentials

API collectors support `method: "GET"` (the default) and `method: "POST"`. `query` always supplies URL parameters. A POST can include either `json_body` (an object or array) or `form_body` (URL-encoded fields with string or string-array values).

```json
{
  "url": "https://service.example/reports/search",
  "method": "POST",
  "json_body": {"from": "2026-11-01", "limit": 100},
  "records_path": "data.records"
}
```

This is a request fragment; the full collector also requires its name, stable IDs, output fields and schedule. EnGenie must probe the same URL, method, query and body before validating a mapping. Changing any of these invalidates the previous sample. POST is for the requested data lookup; the builder does not use it to alter business records.

Preview, scheduled collection and exported Python scripts replay the same request. Cursor parameters stay in the URL by default. For a documented top-level POST body cursor, set `cursor_location: "body"` and `cursor_param` to the real field name. Only successfully published collection commits the returned cursor.

Credentials entered in question forms or as explicit `username:`, `password:`, `token:` or `Bearer ...` chat inputs are encrypted before masking. EnGenie receives opaque source-input references and passes them to `set_source_access`; the tool resolves them only within the same conversation and permitted service. These inputs survive saved-context recovery. The latest saved authentication status is included in every resumed turn.

HTTP credentials are injected into headers automatically. Browser login steps use `credential: "username"` or a named profile such as `portal.password`. A POST login field can similarly use a saved reference:

```json
{
  "method": "POST",
  "json_body": {
    "username": {"credential": "portal.username"},
    "password": {"credential": "portal.password"}
  }
}
```

The corresponding `login` credential must already be saved for that service. Plaintext passwords, redacted placeholders and temporary source-input references cannot be saved as request-body secrets. Exported collectors use environment-variable references and do not include saved secret values. No database migration is required.
