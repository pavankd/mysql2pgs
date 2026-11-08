import { useMemo, useState } from "react"
import { Loader2Icon, OctagonXIcon, PlayIcon, ShieldCheckIcon, ShieldAlertIcon, TerminalSquareIcon } from "lucide-react"

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"
import { Textarea } from "@/components/ui/textarea"
import { ResultGrid } from "@/components/result-grid"
import { api, plural, type PGConn, type QueryResponse } from "@/lib/api"

const MAX_ROWS = 500
const MAX_COUNT_TABLES = 50

const ident = (s: string) => `"${s.replaceAll('"', '""')}"`
const literal = (s: string) => `'${s.replaceAll("'", "''")}'`

export interface MigratedTable {
  schema: string
  table: string
}

interface Props {
  connection: PGConn
  tables: MigratedTable[]
}

/** SQL console against the migration target, shown once a migration has finished. */
export function QueryConsole({ connection, tables }: Props) {
  const schemas = useMemo(() => [...new Set(tables.map((t) => t.schema))], [tables])
  const snippets = useMemo(() => buildSnippets(schemas, tables), [schemas, tables])
  const [sql, setSql] = useState(() => snippets[0]?.sql ?? "SELECT version();")
  const [allowWrites, setAllowWrites] = useState(false)
  const [running, setRunning] = useState(false)
  const [response, setResponse] = useState<QueryResponse | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function run() {
    if (!sql.trim() || running) return
    setRunning(true)
    setError(null)
    try {
      setResponse(await api.query(connection, sql, { searchPath: schemas, readOnly: !allowWrites, maxRows: MAX_ROWS }))
    } catch (e) {
      setResponse(null)
      setError((e as Error).message)
    } finally {
      setRunning(false)
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <TerminalSquareIcon className="size-4" /> Test the migrated schema
        </CardTitle>
        <CardDescription>
          Run SQL against <span className="font-mono">{connection.database}</span> on{" "}
          <span className="font-mono">
            {connection.host}:{connection.port}
          </span>
          . Search path: <span className="font-mono">{[...schemas, "public"].join(", ")}</span>, so tables need no
          schema prefix.
        </CardDescription>
        <CardAction className="flex items-center gap-2">
          <Label htmlFor="allow-writes" className={allowWrites ? "text-destructive" : "text-muted-foreground"}>
            {allowWrites ? <ShieldAlertIcon className="size-4" /> : <ShieldCheckIcon className="size-4" />}
            Allow writes
          </Label>
          <Switch id="allow-writes" checked={allowWrites} onCheckedChange={setAllowWrites} />
        </CardAction>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="mr-1 text-xs text-muted-foreground">Checks:</span>
          {snippets.map((s) => (
            <Button key={s.label} variant="outline" size="xs" onClick={() => setSql(s.sql)}>
              {s.label}
            </Button>
          ))}
        </div>
        {tables.length > 0 && (
          <div className="flex max-h-20 flex-wrap items-center gap-1.5 overflow-y-auto">
            <span className="mr-1 text-xs text-muted-foreground">Preview:</span>
            {tables.map((t) => (
              <Button
                key={`${t.schema}.${t.table}`}
                variant="ghost"
                size="xs"
                className="font-mono"
                onClick={() => setSql(`SELECT *\nFROM ${ident(t.schema)}.${ident(t.table)}\nLIMIT 100;`)}
              >
                {t.schema}.{t.table}
              </Button>
            ))}
          </div>
        )}

        <Textarea
          aria-label="SQL query"
          value={sql}
          onChange={(e) => setSql(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
              e.preventDefault()
              void run()
            }
          }}
          spellCheck={false}
          className="min-h-36 font-mono text-xs leading-relaxed"
        />
        <div className="flex flex-wrap items-center justify-between gap-3">
          <p className="text-xs text-muted-foreground">
            {allowWrites
              ? "Writes are committed. Changes affect the migrated database."
              : "Read-only: runs in a read-only transaction that is rolled back."}{" "}
            Ctrl+Enter to run. Up to {MAX_ROWS} rows are shown per result.
          </p>
          <Button onClick={run} disabled={running || !sql.trim()} variant={allowWrites ? "destructive" : "default"}>
            {running ? <Loader2Icon className="animate-spin" /> : <PlayIcon />}
            Run
          </Button>
        </div>

        {error && (
          <Alert variant="destructive">
            <OctagonXIcon />
            <AlertTitle>Query failed</AlertTitle>
            <AlertDescription className="font-mono text-xs break-words">{error}</AlertDescription>
          </Alert>
        )}
        {response && <Results response={response} />}
      </CardContent>
    </Card>
  )
}

function Results({ response }: { response: QueryResponse }) {
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <span>
          {plural(response.results.length, "statement")} · {response.duration_ms} ms
        </span>
        <Badge variant={response.read_only ? "secondary" : "destructive"}>
          {response.read_only ? "read-only, rolled back" : "committed"}
        </Badge>
      </div>
      {response.notices.map((n, i) => (
        <p key={i} className="text-xs text-amber-700 dark:text-amber-400">
          NOTICE: {n}
        </p>
      ))}
      {response.results.map((r, i) => (
        <ResultGrid key={i} result={r} />
      ))}
    </div>
  )
}

function buildSnippets(schemas: string[], tables: MigratedTable[]) {
  if (schemas.length === 0) return []
  const inSchemas = schemas.map(literal).join(", ")
  const counted = tables.slice(0, MAX_COUNT_TABLES)
  const snippets: { label: string; sql: string }[] = []
  if (counted.length > 0) {
    snippets.push({
      label: "Row counts",
      sql:
        counted
          .map((t) => `SELECT ${literal(`${t.schema}.${t.table}`)} AS "table", count(*) AS "rows" FROM ${ident(t.schema)}.${ident(t.table)}`)
          .join("\nUNION ALL\n") + "\nORDER BY 1;",
    })
  }
  snippets.push(
    {
      label: "Columns & types",
      sql: `SELECT table_schema, table_name, column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema IN (${inSchemas})
ORDER BY table_schema, table_name, ordinal_position;`,
    },
    {
      label: "Constraints",
      sql: `SELECT conrelid::regclass AS "table", conname, contype, pg_get_constraintdef(oid) AS definition
FROM pg_constraint
WHERE connamespace IN (SELECT oid FROM pg_namespace WHERE nspname IN (${inSchemas}))
  AND contype IN ('p', 'u', 'f', 'c')
ORDER BY 1, 3, 2;`,
    },
    {
      label: "Indexes",
      sql: `SELECT schemaname, tablename, indexname, indexdef
FROM pg_indexes
WHERE schemaname IN (${inSchemas})
ORDER BY 1, 2, 3;`,
    },
    {
      label: "Views & triggers",
      sql: `SELECT 'view' AS kind, table_schema AS schema, table_name AS name, NULL AS "on"
FROM information_schema.views WHERE table_schema IN (${inSchemas})
UNION ALL
SELECT 'trigger', n.nspname, t.tgname, c.relname
FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE NOT t.tgisinternal AND n.nspname IN (${inSchemas})
ORDER BY 1, 2, 3;`,
    },
    {
      label: "Identity sequences",
      sql: `SELECT table_schema, table_name, column_name,
       pg_get_serial_sequence(format('%I.%I', table_schema, table_name), column_name) AS sequence
FROM information_schema.columns
WHERE is_identity = 'YES' AND table_schema IN (${inSchemas})
ORDER BY 1, 2;`,
    },
  )
  return snippets
}
