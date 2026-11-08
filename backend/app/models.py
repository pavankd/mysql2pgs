from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class MySQLConn(BaseModel):
    host: str = "mysql"
    port: int = 3306
    user: str = "root"
    password: str = ""


class PGConn(BaseModel):
    host: str = "postgres"
    port: int = 5432
    user: str = "postgres"
    password: str = ""
    database: str = "postgres"


class TablesRequest(BaseModel):
    connection: MySQLConn
    database: str


class DatabaseSelection(BaseModel):
    name: str
    # None means "every table in the database"
    tables: list[str] | None = None
    # Target PostgreSQL schema; defaults to the (normalised) MySQL database name
    target_schema: str | None = None


class MigrationOptions(BaseModel):
    include_data: bool = True
    create_indexes: bool = True
    create_foreign_keys: bool = True
    create_views: bool = True
    drop_existing: bool = False
    tinyint1_as_boolean: bool = True
    lowercase_identifiers: bool = True
    verify_row_counts: bool = True
    batch_size: int = Field(5000, ge=100, le=100_000)


class MigrationRequest(BaseModel):
    source: MySQLConn
    target: PGConn
    databases: list[DatabaseSelection] = Field(min_length=1)
    options: MigrationOptions = MigrationOptions()


class QueryRequest(BaseModel):
    connection: PGConn
    sql: str = Field(min_length=1, max_length=100_000)
    # Schemas put ahead of "public" on the search_path, so migrated tables need no prefix.
    search_path: list[str] = []
    read_only: bool = True
    max_rows: int = Field(500, ge=1, le=10_000)
    timeout_ms: int = Field(30_000, ge=100, le=600_000)


class WorkbenchConn(BaseModel):
    """Connection for the SQL Workbench: either engine, any server."""

    engine: Literal["mysql", "postgres"]
    host: str
    port: int
    user: str
    password: str = ""
    # MySQL: optional default database. PostgreSQL: database to connect to ("postgres" if empty).
    database: str = ""

    def as_mysql(self) -> MySQLConn:
        return MySQLConn(host=self.host, port=self.port, user=self.user, password=self.password)

    def as_postgres(self) -> PGConn:
        return PGConn(host=self.host, port=self.port, user=self.user, password=self.password,
                      database=self.database.strip() or "postgres")


class WorkbenchQuery(BaseModel):
    connection: WorkbenchConn
    sql: str = Field(min_length=1, max_length=1_000_000)
    # Chosen by the client so it can cancel while the request is still running.
    query_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")
    # MySQL: database to USE. PostgreSQL: schema put first on the search_path.
    default_schema: str = ""
    read_only: bool = True
    max_rows: int = Field(1000, ge=1, le=50_000)
    timeout_ms: int = Field(300_000, ge=1_000, le=3_600_000)
