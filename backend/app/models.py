from __future__ import annotations

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
