# Spec: HTTP API

Base path: `/api`. Request and response bodies are JSON. The TypeScript mirror of these types is `frontend/src/lib/api.ts`. Change both sides together.

## Errors

- A database or driver failure returns `400 {"detail": "<first line of the driver message>"}`.
- A validation error returns `422 {"detail": [{msg, …}]}` (FastAPI default). The client joins the `msg` fields.
- An unknown job id returns `404 {"detail": "Migration not found"}`.

## Shared types

```ts
MySQLConn = { host, port, user, password }
PGConn    = MySQLConn & { database }
MigrationOptions = {
  include_data: true, create_indexes: true, create_foreign_keys: true, create_views: true,
  drop_existing: false, tinyint1_as_boolean: true, lowercase_identifiers: true,
  verify_row_counts: true, batch_size: 5000   // 100..100000
}
DatabaseSelection = { name, tables: string[] | null /* null = all */, target_schema: string | null }
MigrationRequest  = { source: MySQLConn, target: PGConn, databases: DatabaseSelection[] /* min 1 */, options }
```

## Endpoints

| Method & path | Body | Response |
|---|---|---|
| `GET /health` | n/a | `{ok: true}` |
| `POST /mysql/test` | MySQLConn | `{ok, version, note}`; `note` is set when the server is not 5.6 |
| `POST /postgres/test` | PGConn | `{ok, version, note: null}` |
| `POST /mysql/databases` | MySQLConn | `[{name, charset, table_count, view_count, size_bytes}]`, without system schemas |
| `POST /mysql/tables` | `{connection: MySQLConn, database}` | `[{name, type: "table"\|"view", engine, row_estimate, size_bytes}]` |
| `POST /migrations/preview` | MigrationRequest | `{sql, warnings[], table_count}`; nothing is executed |
| `POST /migrations` | MigrationRequest | `202 {id}`; the job starts immediately |
| `GET /migrations` | n/a | job summaries, newest first (snapshot without `tables` and `logs`) |
| `GET /migrations/{id}?log_after=N` | n/a | MigrationSnapshot; `logs` contains only entries with `seq > N` |
| `POST /migrations/{id}/cancel` | n/a | `{ok: true}`; cancellation is cooperative |

```ts
MigrationSnapshot = {
  id, status: "pending"|"running"|"completed"|"completed_with_errors"|"failed"|"cancelled",
  phase, created_at, started_at, finished_at,          // epoch seconds
  error, warning_count, error_count,
  rows_copied, rows_estimated,                          // estimated = Σ max(row_estimate, rows_copied)
  tables_total, tables_done, tables_failed, databases: string[],
  tables: TableProgress[], logs: LogEntry[]
}
TableProgress = { key: "db.table", database, source_table, target: "schema.table",
  status: "pending"|"creating"|"copying"|"indexing"|"done"|"failed",
  row_estimate, rows_copied, source_rows|null, target_rows|null, error|null, seconds }
LogEntry = { seq, ts, level: "info"|"warning"|"error"|"debug", message }
```

Notes:
- `row_estimate` comes from `information_schema.TABLES.TABLE_ROWS`. On InnoDB it can be far off, so the UI caps progress at 99% until the job finishes.
- The client polls `GET /migrations/{id}` about every 800 ms until the status is final. There is no push channel (SSE or WebSocket).
- Credentials are sent with every request and never stored server-side beyond the job's in-memory request.
