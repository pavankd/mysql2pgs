# Spec: Schema Dump

The Schema Dump page (`#/dump`) connects to a MySQL/MariaDB, PostgreSQL, Oracle or SQL Server
server, checks what the login may dump, and writes the selected schemas to **one SQL file**.

Code: `backend/app/dumper/` (`base.py` shared, one module per engine, `service.py` entry points),
`frontend/src/pages/schema-dump.tsx`.

## Request

`DumpRequest = { connection: DumpConn, schemas[], content: "schema"|"data"|"both", include_create_schema, output_path, overwrite }`

`DumpConn.database` means: PostgreSQL / SQL Server: database; Oracle: service name (required); MySQL: unused.

## Output file

- `output_path` is **relative to `DUMP_ROOT`** (`/dumps` in the container, `./dumps` on the host).
  Absolute paths, drive letters, `..` escapes and symlinks that resolve outside the root are rejected (`resolve_output`).
- The file is written to `<name>.partial` and renamed only on success. A failed or cancelled dump leaves no file.
  **Known gap:** jobs live in the backend process, so restarting the backend mid-dump kills the job and leaves the `.partial` file behind (it is never renamed, so it is never mistaken for a finished dump).
- An existing file returns **409** unless `overwrite` is true. The UI resets overwrite after every dump.
- Two jobs can't write the same target at once.
- `GET /api/dump/jobs/{id}/download` serves the finished file.
- The backend runs as `${UID:-1000}:${GID:-1000}`, so files on the host belong to the user.

## Permission check

`inspect()` runs when the user connects, and **again when a job starts**, because the server must not trust the client. Each schema reports `can_dump_schema` / `can_dump_data` with human-readable issues. A job is refused (400) if any selected schema lacks the permission its `content` needs.

| Engine | Schema (DDL) needs | Data needs | Notes |
|---|---|---|---|
| MySQL | schema-level/global `SELECT`; `SHOW VIEW` for views | schema-level/global `SELECT` | Table-level grants alone are **not enough**: information_schema hides tables without privileges, so completeness can't be verified. Triggers / routines / events are skipped with a warning without `TRIGGER` / `SELECT` on `mysql.proc` (or `SHOW_ROUTINE`) / `EVENT`. These kinds are hidden from users who lack the privilege, so the warning is based on the privilege, not on the object count. MySQL 8 roles are not expanded. |
| PostgreSQL | `USAGE` on the schema + `SELECT` on every table, matview and foreign table | same + `SELECT` on sequences | pg_dump takes ACCESS SHARE locks even for schema-only dumps, and those need `SELECT`. |
| Oracle | own schema, or `SELECT_CATALOG_ROLE` / `EXP_FULL_DATABASE` / `DATAPUMP_EXP_FULL_DATABASE` / `DBA` (for `DBMS_METADATA` on other schemas) | own schema, `SELECT/READ ANY TABLE`, or a catalog role plus `SELECT` on every table | Without a catalog role, the `ALL_*` views show only accessible objects, so completeness can't be verified. With one, `DBA_*` views are used. |
| SQL Server | `VIEW DEFINITION` on the schema (`HAS_PERMS_BY_NAME`) | `SELECT` on the schema, or `VIEW DEFINITION` plus `SELECT` on every table | Encrypted modules are listed as notes and skipped. |

## How each engine dumps

| Engine | Method | Restore with |
|---|---|---|
| MySQL / MariaDB | Python, using the same `SHOW CREATE TABLE/VIEW/PROCEDURE/FUNCTION/TRIGGER/EVENT/DATABASE` statements as mysqldump, inside `START TRANSACTION WITH CONSISTENT SNAPSHOT` | `mysql < file` |
| PostgreSQL | `pg_dump 18` (plain format, `--schema` per schema, quoted as a literal pattern) | `psql -f file` (psql 17.6+/18 because of `\restrict`) |
| Oracle | Python + `DBMS_METADATA.GET_DDL` / `GET_DEPENDENT_DDL`; data as INSERTs | SQL\*Plus / SQLcl `@file` |
| SQL Server | Python, generating DDL from catalog views; data as multi-row INSERTs; `GO` batches | `sqlcmd -i file` / SSMS |

MySQL is not dumped with `mysqldump`/`mariadb-dump`. Current MariaDB clients query `information_schema.COLUMNS.generation_expression`, which MySQL 5.6 lacks, so they fail against 5.6.

### Ordering (all engines)

Phases run **across all selected schemas**, so cross-schema references restore cleanly:

1. CREATE SCHEMA / DATABASE / USER (if requested)
2. types, sequences, tables (with PK/unique/check)
3. data
4. indexes, grants on tables (Oracle), foreign keys
5. views (MySQL: sorted so dependencies come first), routines and packages
6. **triggers last**, so they never fire during the load

Data-only dumps disable foreign keys and triggers around the load:
- **MySQL:** `FOREIGN_KEY_CHECKS=0` in the header.
- **Oracle:** `DISABLE CONSTRAINT` / `DISABLE ALL TRIGGERS`.
- **SQL Server:** `NOCHECK CONSTRAINT ALL` / `DISABLE TRIGGER ALL`, re-enabled `WITH CHECK`.
- **PostgreSQL:** pg_dump's own handling.

### "Include CREATE SCHEMA"

| Engine | On | Off |
|---|---|---|
| MySQL | `CREATE DATABASE IF NOT EXISTS` (from `SHOW CREATE DATABASE`) | omitted. `USE db;` is always written, because one file holds several schemas. |
| PostgreSQL | pg_dump's `CREATE SCHEMA`. Data-only dumps get `CREATE SCHEMA IF NOT EXISTS`. | `CREATE SCHEMA` / `ALTER SCHEMA … OWNER` lines are filtered from pg_dump's output |
| Oracle | `CREATE USER … NO AUTHENTICATION DEFAULT TABLESPACE … QUOTA UNLIMITED` (schemas are users) | omitted |
| SQL Server | `IF SCHEMA_ID(…) IS NULL EXEC('CREATE SCHEMA …')` | omitted |

### Value fidelity rules

- **MySQL:** values are read raw (no decoders). Numbers and dates are emitted as the server's text, so zero dates and DECIMAL scale are exact. Binary-charset columns become `0x…`, BIT becomes `b'…'`, generated columns are excluded from INSERTs, and the session time zone is pinned to `+00:00` on both dump and restore.
- **Oracle:**
  - **Dates and intervals:** `TO_CHAR` in the SELECT, then `TO_DATE` / `TO_TIMESTAMP(_TZ)` / `TO_DSINTERVAL` / `TO_YMINTERVAL` with explicit formats.
  - **Numbers:** NUMBER is fetched as `Decimal`. BINARY_DOUBLE infinities and NaN use the named constants.
  - **Binary:** RAW, and BLOBs up to 2000 bytes, use `HEXTORAW`. Larger LOBs are inserted through a PL/SQL block using `DBMS_LOB.WRITEAPPEND` / `APPEND`.
  - **Newlines** are emitted as `CHR(10)`. No literal spans lines, because a line containing only `/` would execute the SQL\*Plus buffer. Pieces are at most 500 characters, under SQL\*Plus' 2499-character line limit. `SET DEFINE OFF` is used for `&`.
  - **`GENERATED ALWAYS` identity columns** are switched to `BY DEFAULT` for the load, then restored with `START WITH LIMIT VALUE`.
  - **`ALTER SESSION SET CURRENT_SCHEMA`** is emitted per schema section. DBMS_METADATA reproduces trigger text verbatim (`ON orders`), and unqualified names resolve in the session schema.
  - **Unsupported column types** (object types, BFILE, LONG RAW, VECTOR) skip that table's data with a warning.
- **SQL Server:**
  - **Line breaks** in string values are emitted as `NCHAR(13)`/`NCHAR(10)` concatenations, so no literal spans lines. SSMS and sqlcmd end a batch at a line consisting only of `GO`, even inside a string. Values over 4000 characters get a `CAST(N'' AS nvarchar(max))` prefix so the concatenation doesn't truncate at nvarchar(4000).
  - **Sequences** restart at `last_used_value + increment`. After a restore, `sys.sequences.current_value` shows the next value (the source shows the last one used), and the next value issued is the same on both sides.
  - **Text values:** temporal, uniqueidentifier, xml and sql_variant values are converted to text in the SELECT (style 126), then emitted as `N'…'`.
  - **Binary:** `0x…`.
  - **Spatial and hierarchy types:** geography/geometry are recreated with `STGeomFromText(…, srid)`, hierarchyid with `CAST(… AS hierarchyid)`.
  - **Skipped columns:** rowversion and computed columns are not inserted. `IDENTITY_INSERT` is switched on around tables with identity columns.

## Verification standard

Changes to a dumper need a **restore round-trip**: snapshot the source, dump, restore into an empty target (fresh container, database, or dropped schema), snapshot again, and diff. The 2026-11 implementation was verified this way on all four engines. Compare values, not just counts: checksums, LOB hashes, constraints, grants, object validity, and behaviour after the restore (identity continues, triggers fire on new rows only).
