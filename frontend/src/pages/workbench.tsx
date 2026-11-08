import { useEffect, useRef, useState } from "react"
import {
  CheckCircle2Icon,
  CircleSlashIcon,
  Loader2Icon,
  PlugIcon,
  PlugZapIcon,
  ShieldAlertIcon,
  ShieldCheckIcon,
  SquareIcon,
  XCircleIcon,
  ZapIcon,
} from "lucide-react"
import { toast } from "sonner"

import { ResultGrid, summarize } from "@/components/result-grid"
import { Navigator } from "@/components/workbench/navigator"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Textarea } from "@/components/ui/textarea"
import {
  api,
  ApiError,
  newId,
  type Engine,
  type QueryResponse,
  type SchemaNode,
  type WorkbenchConn,
  type WorkbenchSession,
} from "@/lib/api"

const ENGINE_DEFAULTS: Record<Engine, Omit<WorkbenchConn, "engine" | "password">> = {
  mysql: { host: "mysql", port: 3306, user: "root", database: "" },
  postgres: { host: "postgres", port: 5432, user: "postgres", database: "migrated" },
}
const ROW_LIMITS = [100, 500, 1000, 5000, 10000]
const STORE_KEY = "m2p.workbench"

type Session =
  | { kind: "idle" }
  | { kind: "connecting" }
  | { kind: "connected"; info: WorkbenchSession }
  | { kind: "error"; message: string }

interface OutputEntry {
  id: number
  time: Date
  sql: string
  outcome: "ok" | "error" | "cancelled"
  message: string
  durationMs: number
}

interface Stored {
  conn: Omit<WorkbenchConn, "password">
  sql: string
  readOnly: boolean
  rowLimit: number
  defaultSchema: string
}

function load(): Partial<Stored> {
  try {
    return JSON.parse(localStorage.getItem(STORE_KEY) ?? "{}")
  } catch {
    return {}
  }
}

function save(value: Stored) {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify(value))
  } catch {
    /* storage unavailable */
  }
}

const quoteIdent = (engine: Engine, s: string) =>
  engine === "mysql" ? "`" + s.replaceAll("`", "``") + "`" : '"' + s.replaceAll('"', '""') + '"'

let outputSeq = 0

export function WorkbenchPage() {
  const [stored] = useState(load)
  const [conn, setConn] = useState<WorkbenchConn>(() => ({
    engine: "mysql",
    ...ENGINE_DEFAULTS.mysql,
    ...stored.conn,
    password: "",
  }))
  const [session, setSession] = useState<Session>({ kind: "idle" })
  const [schemas, setSchemas] = useState<SchemaNode[]>([])
  const [schemasLoading, setSchemasLoading] = useState(false)
  const [defaultSchema, setDefaultSchema] = useState(stored.defaultSchema ?? "")
  const [sql, setSql] = useState(stored.sql ?? "SELECT VERSION();")
  const [readOnly, setReadOnly] = useState(stored.readOnly ?? true)
  const [rowLimit, setRowLimit] = useState(stored.rowLimit ?? 1000)
  const [running, setRunning] = useState<{ id: string; started: number; cancelling: boolean } | null>(null)
  const [elapsed, setElapsed] = useState(0)
  const [response, setResponse] = useState<QueryResponse | null>(null)
  const [activeTab, setActiveTab] = useState("output")
  const [output, setOutput] = useState<OutputEntry[]>([])
  const editor = useRef<HTMLTextAreaElement>(null)

  const connected = session.kind === "connected"

  useEffect(() => {
    const { password: _password, ...rest } = conn
    save({ conn: rest, sql, readOnly, rowLimit, defaultSchema })
  }, [conn, sql, readOnly, rowLimit, defaultSchema])

  useEffect(() => {
    if (!running) return
    const t = setInterval(() => setElapsed((performance.now() - running.started) / 1000), 100)
    return () => clearInterval(t)
  }, [running])

  function updateConn(patch: Partial<WorkbenchConn>) {
    setConn((c) => ({ ...c, ...patch }))
    if (session.kind !== "idle") {
      setSession({ kind: "idle" })
      setSchemas([])
    }
  }

  function switchEngine(engine: Engine) {
    if (engine === conn.engine) return
    // Swap in the other engine's defaults, keeping the password field empty.
    updateConn({ engine, ...ENGINE_DEFAULTS[engine], password: "" })
    setDefaultSchema("")
  }

  async function connect() {
    setSession({ kind: "connecting" })
    try {
      const info = await api.workbench.connect(conn)
      setSession({ kind: "connected", info })
      if (conn.engine === "mysql" && info.database) setDefaultSchema(info.database)
      void loadSchemas()
    } catch (e) {
      setSession({ kind: "error", message: (e as Error).message })
    }
  }

  function disconnect() {
    setSession({ kind: "idle" })
    setSchemas([])
  }

  async function loadSchemas() {
    setSchemasLoading(true)
    try {
      setSchemas(await api.workbench.schemas(conn))
    } catch (e) {
      toast.error("Could not load schemas", { description: (e as Error).message })
    } finally {
      setSchemasLoading(false)
    }
  }

  /** Run `text`, or the editor's selection if there is one, or the whole editor. */
  async function execute(text?: string) {
    if (!connected || running) return
    let statement = text
    if (statement === undefined) {
      const el = editor.current
      const selected = el && el.selectionStart !== el.selectionEnd ? sql.slice(el.selectionStart, el.selectionEnd) : ""
      statement = selected.trim() ? selected : sql
    }
    if (!statement.trim()) return

    const id = newId()
    const started = performance.now()
    setRunning({ id, started, cancelling: false })
    setElapsed(0)
    setResponse(null) // never show the previous run's results next to this run's outcome
    const log = (outcome: OutputEntry["outcome"], message: string) =>
      setOutput((prev) =>
        [
          ...prev,
          { id: ++outputSeq, time: new Date(), sql: statement!, outcome, message, durationMs: performance.now() - started },
        ].slice(-200),
      )

    try {
      const res = await api.workbench.query({
        connection: conn,
        sql: statement,
        query_id: id,
        default_schema: defaultSchema,
        read_only: readOnly,
        max_rows: rowLimit,
      })
      setResponse(res)
      // `USE x` (MySQL) or `SET search_path` (PostgreSQL) carries over to the next run.
      if (res.current_schema && res.current_schema !== defaultSchema) setDefaultSchema(res.current_schema)
      const firstRows = res.results.findIndex((r) => r.kind === "rows" && r.columns.length > 0)
      setActiveTab(firstRows >= 0 ? `result-${firstRows}` : "output")
      const msg = res.results.map(summarize).join("; ") || "OK"
      log("ok", (res.read_only ? "[read-only] " : "") + msg + (res.omitted_result_sets ? `; ${res.omitted_result_sets} more result set(s) not shown` : ""))
    } catch (e) {
      const cancelled = e instanceof ApiError && e.status === 409
      log(cancelled ? "cancelled" : "error", (e as Error).message)
      setActiveTab("output")
    } finally {
      setRunning(null)
    }
  }

  async function cancel() {
    if (!running) return
    setRunning({ ...running, cancelling: true })
    try {
      await api.workbench.cancel(running.id)
    } catch (e) {
      toast.error("Cancel failed", { description: (e as Error).message })
    }
  }

  function selectRows(schema: string, table: string) {
    const q = `SELECT * FROM ${quoteIdent(conn.engine, schema)}.${quoteIdent(conn.engine, table)} LIMIT ${rowLimit};`
    setSql(q)
    void execute(q)
  }

  const resultTabs = response?.results ?? []

  return (
    <div className="flex flex-col gap-4">
      {/* Connection bar */}
      <Card className="py-4">
        <CardContent className="flex flex-col gap-3">
          <form
            className="grid grid-cols-2 items-end gap-3 md:grid-cols-[150px_minmax(0,1fr)_90px_minmax(0,1fr)_minmax(0,1fr)_minmax(0,1fr)_auto]"
            onSubmit={(e) => {
              e.preventDefault()
              if (connected) disconnect()
              else void connect()
            }}
          >
            <div className="grid gap-1.5">
              <Label htmlFor="wb-engine">Engine</Label>
              <Select value={conn.engine} onValueChange={(v) => switchEngine(v as Engine)}>
                <SelectTrigger id="wb-engine" className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="mysql">MySQL</SelectItem>
                  <SelectItem value="postgres">PostgreSQL</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <Field id="wb-host" label="Host" value={conn.host} onChange={(v) => updateConn({ host: v })} />
            <Field id="wb-port" label="Port" type="number" value={String(conn.port)} onChange={(v) => updateConn({ port: Number(v) || 0 })} />
            <Field id="wb-user" label="User" value={conn.user} onChange={(v) => updateConn({ user: v })} />
            <Field id="wb-password" label="Password" type="password" value={conn.password} onChange={(v) => updateConn({ password: v })} />
            <Field
              id="wb-database"
              label={conn.engine === "mysql" ? "Default schema" : "Database"}
              placeholder={conn.engine === "mysql" ? "optional" : "postgres"}
              value={conn.database}
              onChange={(v) => updateConn({ database: v })}
            />
            <Button type="submit" className="col-span-2 md:col-span-1" variant={connected ? "outline" : "default"} disabled={session.kind === "connecting"}>
              {session.kind === "connecting" ? <Loader2Icon className="animate-spin" /> : connected ? <PlugIcon /> : <PlugZapIcon />}
              {connected ? "Disconnect" : "Connect"}
            </Button>
          </form>
          <div className="flex flex-wrap items-center gap-2 text-xs">
            {session.kind === "connected" && (
              <>
                <Badge variant="outline" className="border-emerald-500/40 text-emerald-700 dark:text-emerald-400">
                  <CheckCircle2Icon /> Connected
                </Badge>
                <span className="text-muted-foreground">
                  {session.info.engine === "mysql" ? "MySQL" : "PostgreSQL"} {session.info.version} ·{" "}
                  <span className="font-mono">
                    {conn.user}@{conn.host}:{conn.port}
                  </span>
                  {session.info.engine === "postgres" && (
                    <>
                      {" "}· database <span className="font-mono">{session.info.database}</span>
                    </>
                  )}
                </span>
              </>
            )}
            {session.kind === "error" && (
              <span className="flex items-center gap-1.5 text-destructive">
                <XCircleIcon className="size-3.5" /> {session.message}
              </span>
            )}
            {session.kind === "idle" && (
              <span className="text-muted-foreground">
                Not connected. Hostnames are resolved by the backend; with Docker Compose use <span className="font-mono">mysql</span> or{" "}
                <span className="font-mono">postgres</span>.
              </span>
            )}
          </div>
        </CardContent>
      </Card>

      <div className="grid items-start gap-4 lg:grid-cols-[260px_minmax(0,1fr)]">
        <Navigator
          // A new connection starts with a fresh tree (no expansion/filter carried over).
          key={connected ? `${conn.engine}://${conn.user}@${conn.host}:${conn.port}/${conn.database}` : "disconnected"}
          schemas={schemas}
          loading={schemasLoading}
          connected={connected}
          defaultSchema={defaultSchema}
          onSetDefault={setDefaultSchema}
          onSelectRows={selectRows}
          onRefresh={loadSchemas}
        />

        <div className="flex min-w-0 flex-col gap-4">
          {/* Editor */}
          <Card className="gap-3 py-3">
            <CardContent className="flex flex-col gap-3 px-3">
              <div className="flex flex-wrap items-center gap-2">
                <Button size="sm" onClick={() => execute()} disabled={!connected || !!running || !sql.trim()} title="Execute (Ctrl+Enter). Runs the selection if text is selected.">
                  <ZapIcon /> Execute
                </Button>
                <Button size="sm" variant="destructive" onClick={cancel} disabled={!running || running.cancelling} title="Cancel the running query">
                  {running?.cancelling ? <Loader2Icon className="animate-spin" /> : <SquareIcon />}
                  Cancel
                </Button>
                <div className="mx-1 h-5 w-px bg-border" />
                <Label htmlFor="wb-limit" className="text-xs text-muted-foreground">
                  Limit
                </Label>
                <Select value={String(rowLimit)} onValueChange={(v) => setRowLimit(Number(v))}>
                  <SelectTrigger id="wb-limit" size="sm" className="w-28">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {ROW_LIMITS.map((n) => (
                      <SelectItem key={n} value={String(n)}>
                        {n.toLocaleString()} rows
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <div className="mx-1 h-5 w-px bg-border" />
                <Label htmlFor="wb-readonly" className={`text-xs ${readOnly ? "text-muted-foreground" : "text-destructive"}`}>
                  {readOnly ? <ShieldCheckIcon className="size-3.5" /> : <ShieldAlertIcon className="size-3.5" />}
                  Read-only
                </Label>
                <Switch id="wb-readonly" checked={readOnly} onCheckedChange={setReadOnly} />
                <div className="ml-auto flex items-center gap-2 text-xs text-muted-foreground">
                  {running ? (
                    <span className="flex items-center gap-1.5">
                      <Loader2Icon className="size-3.5 animate-spin" />
                      {running.cancelling ? "Cancelling…" : "Running…"} {elapsed.toFixed(1)}s
                    </span>
                  ) : (
                    connected && (
                      <span>
                        Default schema:{" "}
                        <span className="font-mono text-foreground">{defaultSchema || (conn.engine === "postgres" ? "public" : "none")}</span>
                      </span>
                    )
                  )}
                </div>
              </div>
              <Textarea
                ref={editor}
                aria-label="SQL editor"
                value={sql}
                onChange={(e) => setSql(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
                    e.preventDefault()
                    void execute()
                  }
                }}
                spellCheck={false}
                placeholder="Write SQL here. Separate statements with ;"
                className="min-h-56 font-mono text-xs leading-relaxed"
              />
              <p className="text-[11px] text-muted-foreground">
                Ctrl+Enter runs the selection, or everything if nothing is selected.{" "}
                {readOnly
                  ? "Read-only: writes are rejected by the server."
                  : conn.engine === "mysql"
                    ? "Writes are committed statement by statement (autocommit)."
                    : "Writes are committed together when every statement succeeds."}
              </p>
            </CardContent>
          </Card>

          {/* Results */}
          <Card className="gap-3 py-3">
            <CardContent className="px-3">
              <Tabs value={activeTab} onValueChange={setActiveTab} className="gap-3">
                <TabsList className="max-w-full flex-wrap justify-start">
                  {resultTabs.map((r, i) =>
                    r.kind === "rows" && r.columns.length > 0 ? (
                      <TabsTrigger key={i} value={`result-${i}`}>
                        Result {i + 1}
                      </TabsTrigger>
                    ) : null,
                  )}
                  <TabsTrigger value="output">
                    Output
                    {output.length > 0 && <Badge variant="secondary" className="ml-1 h-4 px-1.5 text-[10px]">{output.length}</Badge>}
                  </TabsTrigger>
                </TabsList>
                {resultTabs.map((r, i) => (
                  <TabsContent key={i} value={`result-${i}`}>
                    <ResultGrid result={r} rowNumbers className="max-h-[55vh]" />
                  </TabsContent>
                ))}
                <TabsContent value="output">
                  <OutputLog entries={output} onClear={() => setOutput([])} />
                </TabsContent>
              </Tabs>
              {response && response.notices.length > 0 && (
                <div className="mt-2 flex flex-col gap-0.5">
                  {response.notices.map((n, i) => (
                    <p key={i} className="text-xs text-amber-700 dark:text-amber-400">
                      NOTICE: {n}
                    </p>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  )
}

function Field(props: { id: string; label: string; value: string; type?: string; placeholder?: string; onChange: (v: string) => void }) {
  return (
    <div className="grid min-w-0 gap-1.5">
      <Label htmlFor={props.id}>{props.label}</Label>
      <Input
        id={props.id}
        type={props.type ?? "text"}
        value={props.value}
        placeholder={props.placeholder}
        autoComplete={props.type === "password" ? "new-password" : "off"}
        onChange={(e) => props.onChange(e.target.value)}
      />
    </div>
  )
}

function OutputLog({ entries, onClear }: { entries: OutputEntry[]; onClear: () => void }) {
  const end = useRef<HTMLTableRowElement>(null)
  // Braces matter: scroll methods return a Promise in current browsers, and an effect
  // must return nothing or a cleanup function.
  useEffect(() => {
    end.current?.scrollIntoView({ block: "nearest" })
  }, [entries.length])

  if (entries.length === 0) {
    return <p className="py-8 text-center text-sm text-muted-foreground">Executed statements and their outcome appear here.</p>
  }
  return (
    <div className="flex flex-col gap-2">
      <div className="max-h-[55vh] overflow-auto rounded-lg border">
        <Table>
          <TableHeader className="sticky top-0 z-10 bg-background">
            <TableRow>
              <TableHead className="w-8" />
              <TableHead className="w-10">#</TableHead>
              <TableHead className="w-20">Time</TableHead>
              <TableHead>Action</TableHead>
              <TableHead>Message</TableHead>
              <TableHead className="w-20 text-right">Duration</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {entries.map((e, i) => (
              <TableRow key={e.id} ref={i === entries.length - 1 ? end : undefined}>
                <TableCell>
                  {e.outcome === "ok" ? (
                    <CheckCircle2Icon className="size-4 text-emerald-600" />
                  ) : e.outcome === "cancelled" ? (
                    <CircleSlashIcon className="size-4 text-amber-600" />
                  ) : (
                    <XCircleIcon className="size-4 text-destructive" />
                  )}
                </TableCell>
                <TableCell className="text-xs text-muted-foreground tabular-nums">{e.id}</TableCell>
                <TableCell className="text-xs text-muted-foreground tabular-nums">{e.time.toLocaleTimeString()}</TableCell>
                <TableCell className="max-w-72 truncate font-mono text-xs" title={e.sql}>
                  {e.sql.replace(/\s+/g, " ").trim()}
                </TableCell>
                <TableCell
                  className={`max-w-96 text-xs whitespace-normal break-words ${e.outcome === "error" ? "text-destructive" : e.outcome === "cancelled" ? "text-amber-700 dark:text-amber-400" : ""}`}
                >
                  {e.message}
                </TableCell>
                <TableCell className="text-right text-xs text-muted-foreground tabular-nums">
                  {(e.durationMs / 1000).toFixed(3)}s
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
      <Button variant="ghost" size="xs" className="self-end" onClick={onClear}>
        Clear output
      </Button>
    </div>
  )
}
