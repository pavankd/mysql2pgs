export interface MySQLConn {
  host: string
  port: number
  user: string
  password: string
}

export interface PGConn extends MySQLConn {
  database: string
}

export interface TestResult {
  ok: boolean
  version: string
  note: string | null
}

export interface DatabaseSummary {
  name: string
  charset: string
  table_count: number
  view_count: number
  size_bytes: number
}

export interface TableSummary {
  name: string
  type: "table" | "view"
  engine: string | null
  row_estimate: number
  size_bytes: number
}

export interface MigrationOptions {
  include_data: boolean
  create_indexes: boolean
  create_foreign_keys: boolean
  create_views: boolean
  drop_existing: boolean
  tinyint1_as_boolean: boolean
  lowercase_identifiers: boolean
  verify_row_counts: boolean
  batch_size: number
}

export interface DatabaseSelection {
  name: string
  tables: string[] | null
  target_schema: string | null
}

export interface MigrationRequest {
  source: MySQLConn
  target: PGConn
  databases: DatabaseSelection[]
  options: MigrationOptions
}

export type TableStatus = "pending" | "creating" | "copying" | "indexing" | "done" | "failed"

export interface TableProgress {
  key: string
  database: string
  source_table: string
  target: string
  status: TableStatus
  row_estimate: number
  rows_copied: number
  source_rows: number | null
  target_rows: number | null
  error: string | null
  seconds: number
}

export interface LogEntry {
  seq: number
  ts: number
  level: "info" | "warning" | "error" | "debug"
  message: string
}

export type JobStatus = "pending" | "running" | "completed" | "completed_with_errors" | "failed" | "cancelled"

export interface MigrationSnapshot {
  id: string
  status: JobStatus
  phase: string
  created_at: number
  started_at: number | null
  finished_at: number | null
  error: string | null
  warning_count: number
  error_count: number
  rows_copied: number
  rows_estimated: number
  tables_total: number
  tables_done: number
  tables_failed: number
  databases: string[]
  tables: TableProgress[]
  logs: LogEntry[]
}

export interface PreviewResult {
  sql: string
  warnings: string[]
  table_count: number
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`
    try {
      const data = await res.json()
      if (typeof data.detail === "string") detail = data.detail
      else if (Array.isArray(data.detail)) detail = data.detail.map((d: { msg: string }) => d.msg).join("; ")
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail)
  }
  return res.json() as Promise<T>
}

export const api = {
  testMySQL: (c: MySQLConn) => request<TestResult>("POST", "/mysql/test", c),
  testPostgres: (c: PGConn) => request<TestResult>("POST", "/postgres/test", c),
  databases: (c: MySQLConn) => request<DatabaseSummary[]>("POST", "/mysql/databases", c),
  tables: (c: MySQLConn, database: string) =>
    request<TableSummary[]>("POST", "/mysql/tables", { connection: c, database }),
  preview: (r: MigrationRequest) => request<PreviewResult>("POST", "/migrations/preview", r),
  start: (r: MigrationRequest) => request<{ id: string }>("POST", "/migrations", r),
  get: (id: string, logAfter: number) =>
    request<MigrationSnapshot>("GET", `/migrations/${id}?log_after=${logAfter}`),
  cancel: (id: string) => request<{ ok: boolean }>("POST", `/migrations/${id}/cancel`),
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`
  const units = ["KB", "MB", "GB", "TB"]
  let v = n / 1024
  let i = 0
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i++
  }
  return `${v.toFixed(v < 10 ? 1 : 0)} ${units[i]}`
}

export const formatNumber = (n: number) => n.toLocaleString()

export function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds.toFixed(1)}s`
  const m = Math.floor(seconds / 60)
  const s = Math.round(seconds % 60)
  return `${m}m ${s}s`
}
