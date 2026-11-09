# Spec: HTTP API

Base path: `/api`. Request and response bodies are JSON. The TypeScript mirror of these types is `frontend/src/lib/api.ts`. Change both sides together.

## Errors

- A database or driver failure returns `400 {"detail": "<first line of the driver message>"}`.
- A validation error returns `422 {"detail": [{msg, …}]}` (FastAPI default). The client joins the `msg` fields.
- An unknown job id returns `404 {"detail": "Migration not found"}`.
- A query stopped by Cancel or by its timeout returns `409 {"detail": "<reason>"}` (`/postgres/query` and `/workbench/query`). The client shows 409 as "cancelled", not as an error.
- MySQL errors are reported as `Error <code>: <message>`. PostgreSQL errors add the character position and hint when available.

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
| `POST /postgres/test` | PGConn | `{ok, version, database_missing, note}`. If the server is reachable but `database` does not exist, the response is **200** with `ok: false, database_missing: true` and the server version. Any other failure (host, auth, privileges) is a 400. |
| `POST /postgres/create-database` | PGConn | `{ok, created, database, encoding, note}`. Creates `database` with server defaults, owned by `user`. If it already exists, returns `created: false` rather than an error. An empty name or one over 63 bytes is a 400, as is any server refusal (e.g. no CREATEDB privilege). |
| `POST /postgres/query` | `{connection: PGConn, sql, search_path: string[], read_only: true, max_rows: 500 (1..10000), timeout_ms: 30000 (100..600000)}` | QueryResponse (migration console). A SQL error is a 400. |
| `POST /workbench/connect` | WorkbenchConn | `{ok, engine, version, database}` |
| `POST /workbench/schemas` | WorkbenchConn | `[{name, tables: [{name, type: "table"\|"view"}], truncated}]`, without system schemas; at most 2000 objects per schema |
| `POST /workbench/query` | WorkbenchQuery | QueryResponse; 409 if cancelled or timed out |
| `POST /workbench/query/{query_id}/cancel` | n/a | `{ok: true, running}`. `running: false` means the query had not registered yet (the cancel is still applied when it does) or had already finished. |
| `GET /dump/config` | n/a | `{root}`: how the dumps folder is shown to users (e.g. `./dumps`) |
| `POST /dump/connect` | DumpConn | DumpServerInfo: version, user, notes, and per schema `{name, tables, views, routines, can_dump_schema, can_dump_data, schema_issues[], data_issues[], notes[]}` |
| `POST /dump/jobs` | DumpRequest | `202 {id}`. 400 if the path is invalid, a schema is unknown, or the permission check fails (re-run server-side). 409 if the file exists and `overwrite` is false. |
| `GET /dump/jobs/{id}?log_after=N` | n/a | DumpJobStatus `{id, status: pending\|running\|completed\|failed\|cancelled, phase, current, items_done, items_total, rows, bytes_written, file_size, output_path, engine, schemas, content, include_create_schema, error, warning_count, logs}` |
| `POST /dump/jobs/{id}/cancel` | n/a | `{ok: true}`; the partial file is deleted |
| `GET /dump/jobs/{id}/download` | n/a | the finished file (`application/sql`); 409 unless completed |
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
TableProgress = { key: "db.table", database, source_table, target: "schema.table", target_schema, target_table,
  status: "pending"|"creating"|"copying"|"indexing"|"done"|"failed",
  row_estimate, rows_copied, source_rows|null, target_rows|null, error|null, seconds }
LogEntry = { seq, ts, level: "info"|"warning"|"error"|"debug", message }

DumpConn    = { engine: "mysql"|"postgres"|"oracle"|"mssql", host, port, user, password, database /* PG/MSSQL db, Oracle service name */ }
DumpRequest = { connection: DumpConn, schemas: string[] /* min 1 */, content: "schema"|"data"|"both", include_create_schema, output_path /* relative to the dumps folder */, overwrite }

WorkbenchConn  = { engine: "mysql"|"postgres", host, port, user, password, database /* MySQL: optional default schema; PG: db, "postgres" if empty */ }
WorkbenchQuery = { connection: WorkbenchConn, sql, query_id /* [A-Za-z0-9_-]{8,64}, chosen by the client */,
  default_schema /* MySQL: USE; PG: first on search_path */, read_only: true, max_rows: 1000 (1..50000), timeout_ms: 300000 (1000..3600000) }

QueryResponse = { results: QueryResultSet[] /* one per statement, max 20 */, omitted_result_sets, notices: string[],
  read_only, current_schema /* MySQL DATABASE() / PG current_schema() after the run */, duration_ms }
QueryResultSet = { status /* PG: "SELECT 3", "INSERT 0 1"; MySQL: "3 row(s) returned", "1 row(s) affected" */,
  kind: "rows"|"command", columns: {name, type}[], rows: unknown[][], row_count /* full count; -1 if n/a */,
  truncated /* rows capped at max_rows */ }
```

Query value encoding (both engines): ints beyond ±2^53−1, numeric, and non-finite floats are sent as strings. bytea is sent as `\x…` hex. json/jsonb is sent as a JSON string. date, time, timestamp and interval are sent as the server's own text output: PostgreSQL `infinity`/`-infinity`, MySQL `0000-00-00` dates and `TIME` values beyond 24h all display as stored. MySQL column types are reported by SQL name (`bigint`, `text` vs `blob` by charset). NULL is sent as `null`.

Notes:
- `row_estimate` comes from `information_schema.TABLES.TABLE_ROWS`. On InnoDB it can be far off, so the UI caps progress at 99% until the job finishes.
- `search_path` is set to the given schemas followed by `public`.
- `read_only` (PostgreSQL) sets the session default *and* the wrapping transaction to read-only, then rolls back. MySQL uses `SET SESSION TRANSACTION READ ONLY` (5.6.5+), which blocks DML and DDL. A `COMMIT` inside the script therefore cannot re-enable writes. This guards against accidents; it is not a security boundary, because the caller holds the credentials. With `read_only: false`, the transaction is committed.
- **Cancellation:** the running query registers under `query_id`. PostgreSQL is stopped with the protocol cancel request (`cancel_safe`). MySQL 5.6 has none, so a second connection sends `KILL QUERY <thread id>`; its timeout is a timer doing the same, because 5.6 has no `max_execution_time`. Because `SLEEP()` returns normally when killed, a cancel is recognised from the registry, not only from error 1317.
- **Writes:** PostgreSQL runs the whole script in one transaction, committed only if every statement succeeds. MySQL runs with autocommit, so each statement commits as it runs.
- **Memory:** PostgreSQL results are read fully into backend memory before truncation. MySQL results are streamed (unbuffered cursor), so rows beyond `max_rows` are counted and discarded. Add your own `LIMIT` for very large PostgreSQL tables.
- The client polls `GET /migrations/{id}` about every 800 ms until the status is final. There is no push channel (SSE or WebSocket).
- Credentials are sent with every request and never stored server-side beyond the job's in-memory request.
