# Spec: Architecture

## Components

```
Browser ──► frontend (nginx :80, host :8080)
              │  /api/*  proxied
              ▼
            backend (FastAPI :8000)
              ├── PyMySQL ──► MySQL 5.6  (source, read-only)
              └── psycopg 3 ─► PostgreSQL 18 (target)
```

- In development, `vite dev` (:5173) proxies `/api` to `API_URL` (default `http://localhost:8000`).
- The backend has no database of its own. All state is in process memory.

## Backend modules

| Module | Responsibility | Must not |
|---|---|---|
| `models.py` | Pydantic request models (the API input contract) | contain logic |
| `db.py` | Open connections and pin the UTC session; version probes; detect and create a missing target database | cache connections |
| `introspect.py` | Read `information_schema` into dataclasses | use features newer than MySQL 5.6 |
| `typemap.py` | Map one column: PG type, default, CHECK, select expression, value converter | touch connections |
| `planner.py` | Build the ordered `Plan` of SQL `Step`s; render preview SQL | execute SQL |
| `migrator.py` | Job store, background execution, progress and logs | build SQL by hand that belongs in the planner |
| `query.py` | Ad-hoc SQL on either engine (`run_postgres`, `run_mysql`): read-only guard, timeout, row cap, JSON-safe values, and the `running` cancellation registry. Shared by the migration console and the workbench. | be used by the migration pipeline |
| `workbench.py` | SQL Workbench: connect probe, schema tree, dispatch to `run_mysql`/`run_postgres` | hold query execution logic |
| `dumper/` | Schema Dump: `base.py` (output paths, `.partial` writer, jobs), `mysql.py`, `postgres.py`, `oracle.py`, `mssql.py` (each `inspect()` + `dump()`), `service.py` (dispatch and the server-side permission re-check). See `docs/specs/schema-dump.md`. | write outside `DUMP_ROOT` |
| `main.py` | HTTP routes; map driver errors to `400 {detail}` | hold business logic |

`make_plan(request)` is the only way a plan is produced. Both `/migrations/preview` and job execution call it, so the previewed SQL is the SQL that runs, apart from the data COPY.

## Missing target database

libpq reports "database does not exist" only as message text (no SQLSTATE). `db.pg_probe` therefore does not parse the message. When connecting to the target database fails, it reconnects with the same credentials to a maintenance database (`postgres`, then `template1`) and checks `pg_database`:
- the maintenance connection also fails → the original error is raised (bad host or credentials)
- the name exists → the original error is raised (e.g. no CONNECT privilege)
- the name is absent → `database_missing: true`

## Job lifecycle

```
pending → running → completed | completed_with_errors | failed | cancelled
```

- `JobStore.create` starts one daemon thread per job. There is no queue, so concurrent jobs run in parallel.
- **completed_with_errors:** at least one table failed or at least one `error` log was written. Warnings alone do not change the status.
- **failed:** an unexpected exception or a schema-creation failure. `job.error` holds the first line, and a `debug` log entry holds the traceback.
- **cancelled:** `cancel_event` is checked between steps and between COPY batches. The table's COPY transaction is rolled back.
- When the job ends, any table still in `creating`, `copying` or `indexing` is marked `failed`. Tables still `pending` stay `pending`.
- The log is capped at 5000 entries (`MAX_LOG_ENTRIES`). Entries carry an increasing `seq` used by `log_after` polling.
- Jobs are lost when the backend restarts. This is acceptable by design. Persisting jobs would need a store and a spec change.

## Per-table status

`pending → creating → copying → indexing → done`, and any stage can go to `failed`. Without `include_data`, `copying` is skipped.

## Frontend structure

- `App.tsx` `Shell` owns the header and navigation, with hash routes `#/` (Migrate), `#/workbench` (SQL Workbench) and `#/dump` (Schema Dump). Both pages stay mounted and the inactive one is `hidden`, so switching never loses a running migration's monitor or a workbench draft.
- **SQL Workbench** (`pages/workbench.tsx`, `components/workbench/navigator.tsx`):
  - The connection bar takes the engine, host, port, user, password and database.
  - The Navigator tree lists schemas and tables. Clicking a table runs `SELECT * … LIMIT <row limit>`. Double-clicking a schema (or "use") sets it as the default schema. The tree is keyed by connection, so nothing carries over between servers.
  - The editor runs the selection if there is one, otherwise the whole text (Ctrl+Enter).
  - Execute generates a `query_id` (`newId()`, because `crypto.randomUUID` needs a secure context). Cancel posts it to `/workbench/query/{id}/cancel`.
  - Results show one tab per result-set statement, plus an Output tab logging each run (ok / error / cancelled, with duration).
  - `current_schema` from each response becomes the next default schema, so `USE x` carries over even though every execution uses a new connection.
  - The connection (without password), draft SQL, limit, read-only flag and default schema are kept in `localStorage` (`m2p.workbench`).
- **Schema Dump** (`pages/schema-dump.tsx`, route `#/dump`): connect (with per-engine defaults and field labels), then a schema table with per-schema "Schema dump" / "Data dump" permission badges (issues in tooltips; a schema allowed for neither can't be ticked), then options (content radio, include CREATE SCHEMA, output path under the dumps folder, overwrite). Then a job monitor that polls `/dump/jobs/{id}` and offers Download. Stored in `localStorage` `m2p.dump` without the password.
- `components/result-grid.tsx` is the shared result table, used by both the migration console and the workbench.
- A three-step wizard: Connect → Select → Migrate (`App.tsx`, controlled `Tabs`).
- `Select` is enabled only after both connection tests succeed. Editing a connection resets its test state.
- When the MySQL test succeeds, the Connect step loads `POST /mysql/databases` and shows the **Source schemas** card (`components/source-schemas.tsx`). Its checkboxes write to the same `SelectionMap` as the Select step (through `withDbChecked`), so ticked schemas carry over. Editing any MySQL connection field clears the schema list and the selection, since they belong to the previous server. `Continue` refetches only when no list is loaded.
- When the PostgreSQL test reports `database_missing`, the target card enters the `missing` state and `CreateDatabaseDialog` asks for confirmation. If the user confirms, `POST /postgres/create-database` runs and the test is repeated. If they dismiss it, the card keeps a "Create database" button. The Select step stays disabled until the test returns `ok`.
- When a job finishes as `completed` or `completed_with_errors`, `MigrationMonitor` shows `QueryConsole` (`components/query-console.tsx`). It runs `POST /postgres/query` against the job's own target connection. `App` keeps that connection as `jobTarget`, captured at start, so later edits on the Connect step don't redirect the console. Writes are blocked until "Allow writes" is switched on. Snippet buttons generate checks (row counts, columns, constraints, indexes, views and triggers, identity sequences) and a table preview for every table that finished as `done`. The generated SQL quotes identifiers and literals.
- Connection settings (without passwords) and options are kept in `localStorage` under the `m2p.*` keys. Every access is wrapped in try/catch.
- `lib/api.ts` is the single typed client. Its types mirror `docs/specs/api.md`.
- Everything in `components/ui/*` is generated by shadcn (style `radix-nova`, Radix base, lucide icons).
