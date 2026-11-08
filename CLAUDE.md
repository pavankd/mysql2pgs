# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A web app that migrates MySQL 5.6 databases (schema and data) into PostgreSQL 18. It has a FastAPI backend (`backend/`), a Vite/React/shadcn frontend (`frontend/`), and Docker Compose for MySQL 5.6, Postgres 18, the backend and the frontend (nginx).

Before changing behaviour, read the specs in `docs/specs/`. They are the contract the code is expected to follow:
- `docs/specs/architecture.md`: components, request flow, job lifecycle.
- `docs/specs/migration.md`: execution order, type and value mapping rules, invariants, error handling.
- `docs/specs/api.md`: the HTTP contract shared by `backend/app/main.py` and `frontend/src/lib/api.ts`.

If you change behaviour, update the matching spec and the README type-mapping table in the same change.

## Commands

npm is **not installed on the host**. Run Node tooling through a container, from the repo root:

```bash
node_run() { docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD/frontend:/app" -w /app node:24-alpine "$@"; }
node_run npm run build                          # tsc -b && vite build (this is the type check; there is no linter)
node_run npm install <pkg>
node_run npx shadcn@latest add -y <component>   # add shadcn/ui components (style radix-nova)
```

Backend (uses `uv`; the venv lives at `backend/.venv`):

```bash
cd backend && uv venv && uv pip install -r requirements.txt pytest
.venv/bin/python -m pytest tests                                   # all unit tests
.venv/bin/python -m pytest tests/test_planner.py::test_enum -q     # a single test
.venv/bin/uvicorn app.main:app --reload                            # dev server on :8000
```

Full stack and end-to-end checks:

```bash
docker compose up -d --build                 # UI on :8080 (nginx proxies /api to backend:8000)
docker compose up -d --build backend         # rebuild after backend changes (code is baked in, no reload)
docker compose exec -T postgres psql -U postgres -d migrated     # inspect migrated data
docker compose exec -T mysql mysql -uroot -pmysql shop           # inspect source data
docker compose down -v                       # reset; MySQL re-seeds from docker/mysql/init on next start
```

The unit tests only cover pure planning and type-mapping logic. Changes to introspection, COPY or execution need an end-to-end check against the compose databases: `POST /api/migrations` with `drop_existing: true`, poll `GET /api/migrations/{id}`, then compare values in psql. The seed in `docker/mysql/init/01-sample.sql` deliberately covers edge cases. Add a case there when you add a mapping rule.

## Architecture (big picture)

The backend pipeline goes **introspect → plan → execute**, and each stage is a separate module:

1. `app/introspect.py` reads `information_schema` (only queries that work on 5.6) into dataclasses: `DatabaseInfo` → `Table` → `Column` / `Index` / `ForeignKey`, plus `View`.
2. `app/typemap.py` maps a single column into a `MappedColumn`. That holds the PG type, identity flag, default SQL, an enum CHECK, the **MySQL select expression**, and a **per-value Python converter** that runs on every row during COPY. All MySQL→PG semantics for a column live here.
3. `app/planner.py` turns the introspected databases into a `Plan` of SQL `Step`s, grouped as schema steps, per-table `TablePlan`s (`create_sql`, then `post_steps` for PK/indexes/identity sync/triggers/comments), FK steps and view steps. It runs no SQL. `Plan.to_sql()` renders it for the preview endpoint, so preview and execution always share one code path through `migrator.make_plan()`.
4. `app/migrator.py` runs a `Plan` on a daemon thread per `Job`. Data is streamed with a MySQL unbuffered `SSCursor` → `fetchmany(batch_size)` → converters → psycopg `COPY ... FROM STDIN`, with one transaction per table. Job state, per-table progress and the log are kept in memory behind `job.lock`. The frontend polls `snapshot(log_after)` to fetch logs incrementally.

Separately from the migration pipeline, `app/query.py` runs ad-hoc SQL on either engine, for the post-migration console (`/api/postgres/query`) and the SQL Workbench (`app/workbench.py`, `/api/workbench/*`). Cancellation goes through its `running` registry, keyed by a client-chosen `query_id`: PostgreSQL uses `cancel_safe()`, MySQL uses `KILL QUERY` from a second connection.

Cross-cutting rules that are easy to break:
- **Both sessions are pinned to UTC** (`db.py`). MySQL `TIMESTAMP` → `timestamptz` relies on this.
- **Identifiers:** always quote through `typemap.qi()` (PG) and `typemap.mq()` (MySQL). Run target names through `planner.pg_ident()`, which applies the lowercase option. Derived names (indexes, triggers, PK) go through `planner.short_name()` so they fit in 63 bytes. PG index names are unique per schema, so they are prefixed with the table name.
- **Types referenced by FKs must stay compatible.** This is why BIGINT UNSIGNED maps to `bigint`, not `numeric(20)`. Check FK compatibility whenever you change an integer mapping.
- **Keys are built after the data load.** PKs, indexes and FKs are not part of `CREATE TABLE`. Identity sequences are re-synced with `setval` after the load.
- **Failures are isolated.** A failed table, index, FK or view is logged and the run continues. Only schema-creation failures abort the job. `Step.fallback_sql` gives a second attempt (used for prefix indexes).
- pymysql returns invalid dates (`0000-00-00`) as `str`, `TIME` as `timedelta` and `BIT` as `bytes`. The temporal and bit converters depend on this.

Frontend (`frontend/src`): `App.tsx`'s `Shell` routes `#/` (migration wizard) and `#/workbench` (`pages/workbench.tsx`). Both stay mounted, and the hidden one keeps its state. The migration wizard (`Migrator` in `App.tsx`) holds all wizard state (connections, selection map, options, job id) and the three-step `Tabs` (Connect → Select → Migrate). `components/schema-picker.tsx` lazy-loads tables per database. A selection sends `tables: null` (meaning all tables) unless a strict subset was chosen. `components/migration-monitor.tsx` polls every 800 ms and appends logs using `log_after`. Everything in `components/ui/` is generated by shadcn: regenerate it with the CLI rather than hand-editing it. `@/` aliases `src/`, and `cn` comes from the `cn` package via `lib/utils.ts`.

Connection defaults in the UI (`mysql:3306`, `postgres:5432`) are hostnames resolved **by the backend container**. From the host, the ports are `13306` and `15432`.

## Toolchain notes

- TypeScript 7: `baseUrl` is not supported. `paths` in tsconfig resolve relative to the config file.
- Vite 8: use `import.meta.dirname`, not `__dirname`, in `vite.config.ts`.
- Effects must use a block body when calling scroll APIs: `useEffect(() => { el.scrollIntoView() })`. Current browsers return a Promise from scroll methods, and React crashes ("is not a function") if an effect returns a non-function.
- psycopg 3 binds a Python list as an array, not an IN list: use `= ANY(%s)` / `<> ALL(%s)`.
- Tailwind v4 is configured in CSS (`src/index.css`); there is no `tailwind.config.js`.
- The `mysql:5.6` image is amd64-only (`platform: linux/amd64` in compose).
