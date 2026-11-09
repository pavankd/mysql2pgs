import { useEffect, useMemo, useRef, useState } from "react"
import {
  CheckCircle2Icon,
  DownloadIcon,
  FileDownIcon,
  InfoIcon,
  Loader2Icon,
  OctagonXIcon,
  PlugIcon,
  PlugZapIcon,
  RotateCcwIcon,
  SearchIcon,
  ShieldAlertIcon,
  ShieldCheckIcon,
  SquareIcon,
  TriangleAlertIcon,
  XCircleIcon,
} from "lucide-react"
import { toast } from "sonner"

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Progress } from "@/components/ui/progress"
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import {
  api,
  ApiError,
  formatBytes,
  formatDuration,
  type DumpConn,
  type DumpContent,
  type DumpEngine,
  type DumpJobStatus,
  type DumpSchemaInfo,
  type DumpServerInfo,
  type LogEntry,
} from "@/lib/api"

interface EngineSpec {
  label: string
  defaults: Omit<DumpConn, "engine" | "password">
  /** Label for the `database` field; null hides it. */
  databaseLabel: string | null
  createSchemaHint: string
}

// Defaults match docker-compose.yml (hostnames are resolved by the backend container).
const ENGINES: Record<DumpEngine, EngineSpec> = {
  mysql: {
    label: "MySQL / MariaDB",
    defaults: { host: "mysql", port: 3306, user: "root", database: "" },
    databaseLabel: null,
    createSchemaHint: "Adds CREATE DATABASE IF NOT EXISTS for each schema (USE statements are always included).",
  },
  postgres: {
    label: "PostgreSQL",
    defaults: { host: "postgres", port: 5432, user: "postgres", database: "migrated" },
    databaseLabel: "Database",
    createSchemaHint: "Keeps CREATE SCHEMA statements (data-only dumps get CREATE SCHEMA IF NOT EXISTS).",
  },
  oracle: {
    label: "Oracle",
    defaults: { host: "oracle", port: 1521, user: "system", database: "FREEPDB1" },
    databaseLabel: "Service name",
    createSchemaHint: "Oracle schemas are users: adds CREATE USER … NO AUTHENTICATION with a tablespace quota.",
  },
  mssql: {
    label: "SQL Server",
    defaults: { host: "mssql", port: 1433, user: "sa", database: "shop" },
    databaseLabel: "Database",
    createSchemaHint: "Adds CREATE SCHEMA (if it doesn't exist) before the objects of each schema.",
  },
}

const CONTENT: { value: DumpContent; label: string; hint: string }[] = [
  { value: "both", label: "Schema + data", hint: "Structure and rows" },
  { value: "schema", label: "Schema only", hint: "Tables, views, code" },
  { value: "data", label: "Data only", hint: "Rows into existing tables" },
]

const STORE_KEY = "m2p.dump"
const FINISHED = ["completed", "failed", "cancelled"]

type Session =
  | { kind: "idle" }
  | { kind: "connecting" }
  | { kind: "connected"; info: DumpServerInfo }
  | { kind: "error"; message: string }

function loadStored(): { conn?: Omit<DumpConn, "password">; content?: DumpContent; includeCreate?: boolean } {
  try {
    return JSON.parse(localStorage.getItem(STORE_KEY) ?? "{}")
  } catch {
    return {}
  }
}

function defaultFileName(engine: DumpEngine, schemas: string[]): string {
  const d = new Date()
  const pad = (n: number) => String(n).padStart(2, "0")
  const stamp = `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}`
  const name = schemas.length === 1 ? schemas[0] : schemas.length > 1 ? `${schemas[0]}-and-${schemas.length - 1}-more` : "dump"
  return `${engine}/${name.replace(/[^\w.-]+/g, "_")}-${stamp}.sql`
}

export function SchemaDumpPage() {
  const [stored] = useState(loadStored)
  const [conn, setConn] = useState<DumpConn>(() => ({
    engine: "mysql",
    ...ENGINES.mysql.defaults,
    ...stored.conn,
    password: "",
  }))
  const [session, setSession] = useState<Session>({ kind: "idle" })
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [filter, setFilter] = useState("")
  const [content, setContent] = useState<DumpContent>(stored.content ?? "both")
  const [includeCreate, setIncludeCreate] = useState(stored.includeCreate ?? true)
  const [outputPath, setOutputPath] = useState("")
  const [pathEdited, setPathEdited] = useState(false)
  const [overwrite, setOverwrite] = useState(false)
  const [root, setRoot] = useState("./dumps")
  const [starting, setStarting] = useState(false)
  const [startError, setStartError] = useState<{ message: string; exists: boolean } | null>(null)
  const [jobId, setJobId] = useState<string | null>(null)

  const spec = ENGINES[conn.engine]
  const info = session.kind === "connected" ? session.info : null

  useEffect(() => {
    api.dump.config().then((c) => setRoot(c.root), () => {})
  }, [])

  useEffect(() => {
    const { password: _password, ...rest } = conn
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify({ conn: rest, content, includeCreate }))
    } catch {
      /* storage unavailable */
    }
  }, [conn, content, includeCreate])

  // Suggest a file name until the user types their own.
  const selectedList = useMemo(() => info?.schemas.filter((s) => selected.has(s.name)).map((s) => s.name) ?? [], [info, selected])
  useEffect(() => {
    if (!pathEdited) setOutputPath(defaultFileName(conn.engine, selectedList))
  }, [conn.engine, selectedList, pathEdited])

  function updateConn(patch: Partial<DumpConn>) {
    setConn((c) => ({ ...c, ...patch }))
    if (session.kind !== "idle") {
      setSession({ kind: "idle" })
      setSelected(new Set())
    }
  }

  async function connect() {
    setSession({ kind: "connecting" })
    setSelected(new Set())
    setStartError(null)
    try {
      setSession({ kind: "connected", info: await api.dump.connect(conn) })
    } catch (e) {
      setSession({ kind: "error", message: (e as Error).message })
    }
  }

  const allowed = (s: DumpSchemaInfo, c: DumpContent = content) =>
    (c === "data" || s.can_dump_schema) && (c === "schema" || s.can_dump_data)
  const denied = info?.schemas.filter((s) => selected.has(s.name) && !allowed(s)) ?? []
  const visible = info?.schemas.filter((s) => s.name.toLowerCase().includes(filter.trim().toLowerCase())) ?? []

  async function start() {
    setStarting(true)
    setStartError(null)
    try {
      const { id } = await api.dump.start({
        connection: conn,
        schemas: selectedList,
        content,
        include_create_schema: includeCreate,
        output_path: outputPath,
        overwrite,
      })
      setJobId(id)
      // Overwriting is destructive: never carry it (or the last path) over to the next dump.
      setOverwrite(false)
      setPathEdited(false)
    } catch (e) {
      setStartError({ message: (e as Error).message, exists: e instanceof ApiError && e.status === 409 })
    } finally {
      setStarting(false)
    }
  }

  if (jobId) {
    return <DumpMonitor jobId={jobId} onDone={() => setJobId(null)} />
  }

  return (
    <div className="flex flex-col gap-6">
      {/* 1. Connection */}
      <Card>
        <CardHeader>
          <CardTitle>Database server</CardTitle>
          <CardDescription>
            Connect to the server to dump from. The permission check runs as soon as you connect. Hostnames are resolved by the
            backend.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form
            className="grid grid-cols-2 items-end gap-3 md:grid-cols-[170px_minmax(0,1fr)_90px_minmax(0,1fr)_minmax(0,1fr)_minmax(0,1fr)_auto]"
            onSubmit={(e) => {
              e.preventDefault()
              if (session.kind === "connected") updateConn({})
              else void connect()
            }}
          >
            <div className="col-span-2 grid gap-1.5 md:col-span-1">
              <Label htmlFor="dump-engine">Server type</Label>
              <Select
                value={conn.engine}
                onValueChange={(v) => {
                  const engine = v as DumpEngine
                  if (engine !== conn.engine) updateConn({ engine, ...ENGINES[engine].defaults, password: "" })
                }}
              >
                <SelectTrigger id="dump-engine" className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {(Object.keys(ENGINES) as DumpEngine[]).map((e) => (
                    <SelectItem key={e} value={e}>
                      {ENGINES[e].label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <Field id="dump-host" label="Host" value={conn.host} onChange={(v) => updateConn({ host: v })} />
            <Field id="dump-port" label="Port" type="number" value={String(conn.port)} onChange={(v) => updateConn({ port: Number(v) || 0 })} />
            <Field id="dump-user" label="User" value={conn.user} onChange={(v) => updateConn({ user: v })} />
            <Field id="dump-password" label="Password" type="password" value={conn.password} onChange={(v) => updateConn({ password: v })} />
            {spec.databaseLabel ? (
              <Field id="dump-database" label={spec.databaseLabel} value={conn.database} onChange={(v) => updateConn({ database: v })} />
            ) : (
              <div className="hidden md:block" />
            )}
            <Button type="submit" className="col-span-2 md:col-span-1" variant={info ? "outline" : "default"} disabled={session.kind === "connecting"}>
              {session.kind === "connecting" ? <Loader2Icon className="animate-spin" /> : info ? <PlugIcon /> : <PlugZapIcon />}
              {info ? "Disconnect" : "Connect"}
            </Button>
          </form>
          <div className="mt-3 text-xs">
            {info && (
              <div className="flex flex-col gap-1">
                <span className="flex flex-wrap items-center gap-2">
                  <Badge variant="outline" className="border-emerald-500/40 text-emerald-700 dark:text-emerald-400">
                    <CheckCircle2Icon /> Connected
                  </Badge>
                  <span className="text-muted-foreground">
                    {spec.label} {info.version} · signed in as <span className="font-mono text-foreground">{info.user}</span>
                  </span>
                </span>
                {info.notes.map((n, i) => (
                  <span key={i} className="flex items-center gap-1.5 text-muted-foreground">
                    <InfoIcon className="size-3.5 shrink-0" /> {n}
                  </span>
                ))}
              </div>
            )}
            {session.kind === "error" && (
              <span className="flex items-start gap-1.5 text-destructive">
                <XCircleIcon className="mt-0.5 size-3.5 shrink-0" /> {session.message}
              </span>
            )}
          </div>
        </CardContent>
      </Card>

      {info && (
        <div className="grid items-start gap-6 lg:grid-cols-[minmax(0,1fr)_340px]">
          {/* 2. Schemas */}
          <Card>
            <CardHeader>
              <CardTitle>Schemas</CardTitle>
              <CardDescription>
                {info.schemas.length} schema{info.schemas.length === 1 ? "" : "s"} visible to {info.user}. Permissions are checked
                for both a schema dump (DDL) and a data dump.
              </CardDescription>
              <CardAction>
                <Badge variant="secondary">{selected.size} selected</Badge>
              </CardAction>
            </CardHeader>
            <CardContent className="flex flex-col gap-3">
              <div className="flex flex-wrap items-center gap-2">
                <div className="relative min-w-0 flex-1">
                  <SearchIcon className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
                  <Input placeholder="Filter schemas…" value={filter} onChange={(e) => setFilter(e.target.value)} className="pl-8" />
                </div>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setSelected(new Set([...selected, ...visible.filter((s) => allowed(s)).map((s) => s.name)]))}
                >
                  Select all allowed
                </Button>
                <Button variant="ghost" size="sm" onClick={() => setSelected(new Set())}>
                  Clear
                </Button>
              </div>
              <div className="max-h-[55vh] overflow-auto rounded-lg border">
                <Table>
                  <TableHeader className="sticky top-0 z-10 bg-background">
                    <TableRow>
                      <TableHead className="w-8" />
                      <TableHead>Schema</TableHead>
                      <TableHead className="text-right">Tables</TableHead>
                      <TableHead className="text-right">Views</TableHead>
                      <TableHead className="text-right">Code</TableHead>
                      <TableHead>Schema dump</TableHead>
                      <TableHead>Data dump</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {visible.length === 0 && (
                      <TableRow>
                        <TableCell colSpan={7} className="py-8 text-center text-muted-foreground">
                          No schemas found.
                        </TableCell>
                      </TableRow>
                    )}
                    {visible.map((s) => {
                      const usable = s.can_dump_schema || s.can_dump_data
                      return (
                        <TableRow key={s.name} data-state={selected.has(s.name) ? "selected" : undefined}>
                          <TableCell>
                            <Checkbox
                              aria-label={`Select schema ${s.name}`}
                              checked={selected.has(s.name)}
                              disabled={!usable}
                              onCheckedChange={(v) => {
                                const next = new Set(selected)
                                if (v === true) next.add(s.name)
                                else next.delete(s.name)
                                setSelected(next)
                              }}
                            />
                          </TableCell>
                          <TableCell className="font-mono text-xs">
                            <span className="flex items-center gap-1.5">
                              {s.name}
                              {s.notes.length > 0 && (
                                <Tooltip>
                                  <TooltipTrigger asChild>
                                    <TriangleAlertIcon className="size-3.5 text-amber-600" aria-label="Notes" />
                                  </TooltipTrigger>
                                  <TooltipContent className="max-w-xs">
                                    {s.notes.map((n, i) => (
                                      <p key={i}>{n}</p>
                                    ))}
                                  </TooltipContent>
                                </Tooltip>
                              )}
                            </span>
                          </TableCell>
                          <TableCell className="text-right tabular-nums">{s.tables}</TableCell>
                          <TableCell className="text-right tabular-nums">{s.views}</TableCell>
                          <TableCell className="text-right tabular-nums">{s.routines}</TableCell>
                          <TableCell>
                            <PermissionBadge ok={s.can_dump_schema} issues={s.schema_issues} />
                          </TableCell>
                          <TableCell>
                            <PermissionBadge ok={s.can_dump_data} issues={s.data_issues} />
                          </TableCell>
                        </TableRow>
                      )
                    })}
                  </TableBody>
                </Table>
              </div>
            </CardContent>
          </Card>

          {/* 3. Options */}
          <Card className="lg:sticky lg:top-4">
            <CardHeader>
              <CardTitle>Dump options</CardTitle>
              <CardDescription>The dump is always written as a single .sql file.</CardDescription>
            </CardHeader>
            <CardContent className="flex flex-col gap-5">
              <div className="grid gap-2">
                <Label>What to dump</Label>
                <RadioGroup value={content} onValueChange={(v) => setContent(v as DumpContent)} className="gap-2">
                  {CONTENT.map((c) => (
                    <Label
                      key={c.value}
                      htmlFor={`content-${c.value}`}
                      className="flex cursor-pointer items-center gap-3 rounded-lg border p-2.5 font-normal has-data-[state=checked]:border-primary/60 has-data-[state=checked]:bg-muted/40"
                    >
                      <RadioGroupItem id={`content-${c.value}`} value={c.value} />
                      <span className="grid gap-0.5">
                        <span className="text-sm font-medium">{c.label}</span>
                        <span className="text-xs text-muted-foreground">{c.hint}</span>
                      </span>
                    </Label>
                  ))}
                </RadioGroup>
              </div>

              <div className="flex items-start justify-between gap-4">
                <div className="grid gap-0.5">
                  <Label htmlFor="dump-create-schema">Include CREATE SCHEMA</Label>
                  <p className="text-xs text-muted-foreground">{spec.createSchemaHint}</p>
                </div>
                <Switch id="dump-create-schema" checked={includeCreate} onCheckedChange={setIncludeCreate} />
              </div>

              <div className="grid gap-2">
                <Label htmlFor="dump-path">Save as</Label>
                <div className="flex items-center rounded-lg border focus-within:border-ring focus-within:ring-3 focus-within:ring-ring/50">
                  <span className="border-r bg-muted/50 px-2 py-1.5 font-mono text-xs text-muted-foreground">{root.replace(/\/$/, "")}/</span>
                  <input
                    id="dump-path"
                    value={outputPath}
                    onChange={(e) => {
                      setOutputPath(e.target.value)
                      setPathEdited(true)
                      setStartError(null)
                    }}
                    spellCheck={false}
                    className="min-w-0 flex-1 bg-transparent px-2 py-1.5 font-mono text-xs outline-none"
                  />
                </div>
                <p className="text-xs text-muted-foreground">
                  Relative to the dumps folder; sub-folders are created. A download link is offered when the dump finishes.
                </p>
                <div className="flex items-center justify-between gap-4">
                  <Label htmlFor="dump-overwrite" className="text-xs font-normal">
                    Overwrite if the file exists
                  </Label>
                  <Switch id="dump-overwrite" checked={overwrite} onCheckedChange={setOverwrite} />
                </div>
              </div>

              {denied.length > 0 && (
                <Alert variant="destructive">
                  <ShieldAlertIcon />
                  <AlertTitle>Not permitted for “{CONTENT.find((c) => c.value === content)?.label}”</AlertTitle>
                  <AlertDescription>
                    {denied.map((s) => (
                      <p key={s.name}>
                        <span className="font-mono">{s.name}</span>:{" "}
                        {[...(content !== "data" ? s.schema_issues : []), ...(content !== "schema" ? s.data_issues : [])].join("; ")}
                      </p>
                    ))}
                  </AlertDescription>
                </Alert>
              )}
              {startError && (
                <Alert variant="destructive">
                  <OctagonXIcon />
                  <AlertTitle>{startError.exists ? "File already exists" : "Could not start the dump"}</AlertTitle>
                  <AlertDescription>{startError.message}</AlertDescription>
                </Alert>
              )}

              <Button
                size="lg"
                onClick={start}
                disabled={starting || selected.size === 0 || denied.length > 0 || !outputPath.trim()}
              >
                {starting ? <Loader2Icon className="animate-spin" /> : <FileDownIcon />}
                Create dump{selected.size > 0 && ` (${selected.size} schema${selected.size === 1 ? "" : "s"})`}
              </Button>
            </CardContent>
          </Card>
        </div>
      )}
    </div>
  )
}

function Field(props: { id: string; label: string; value: string; type?: string; onChange: (v: string) => void }) {
  return (
    <div className="grid min-w-0 gap-1.5">
      <Label htmlFor={props.id}>{props.label}</Label>
      <Input
        id={props.id}
        type={props.type ?? "text"}
        value={props.value}
        autoComplete={props.type === "password" ? "new-password" : "off"}
        onChange={(e) => props.onChange(e.target.value)}
      />
    </div>
  )
}

function PermissionBadge({ ok, issues }: { ok: boolean; issues: string[] }) {
  if (ok) {
    return (
      <Badge variant="outline" className="border-emerald-500/40 text-emerald-700 dark:text-emerald-400">
        <ShieldCheckIcon /> Allowed
      </Badge>
    )
  }
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Badge variant="destructive" className="cursor-help">
          <ShieldAlertIcon /> Denied
        </Badge>
      </TooltipTrigger>
      <TooltipContent className="max-w-xs">
        {issues.map((n, i) => (
          <p key={i}>{n}</p>
        ))}
      </TooltipContent>
    </Tooltip>
  )
}

function DumpMonitor({ jobId, onDone }: { jobId: string; onDone: () => void }) {
  const [job, setJob] = useState<DumpJobStatus | null>(null)
  const [logs, setLogs] = useState<LogEntry[]>([])
  const [now, setNow] = useState(() => Date.now() / 1000)
  const lastSeq = useRef(0)
  const logEnd = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let stopped = false
    let timer: ReturnType<typeof setTimeout>
    async function poll() {
      try {
        const s = await api.dump.status(jobId, lastSeq.current)
        if (stopped) return
        if (s.logs.length) {
          lastSeq.current = s.logs[s.logs.length - 1].seq
          setLogs((prev) => [...prev, ...s.logs].slice(-2000))
        }
        setJob(s)
        if (FINISHED.includes(s.status)) {
          if (s.status === "completed") toast.success("Dump completed", { description: s.output_path })
          else if (s.status === "failed") toast.error("Dump failed", { description: s.error ?? undefined })
          return
        }
      } catch (e) {
        if (stopped) return
        toast.error("Lost contact with the backend", { description: (e as Error).message })
      }
      timer = setTimeout(poll, 600)
    }
    void poll()
    return () => {
      stopped = true
      clearTimeout(timer)
    }
  }, [jobId])

  const running = !job || !FINISHED.includes(job.status)
  useEffect(() => {
    if (!running) return
    const t = setInterval(() => setNow(Date.now() / 1000), 500)
    return () => clearInterval(t)
  }, [running])

  useEffect(() => {
    logEnd.current?.scrollIntoView({ block: "nearest" })
  }, [logs.length])

  if (!job) {
    return (
      <div className="flex items-center justify-center gap-2 py-24 text-muted-foreground">
        <Loader2Icon className="size-5 animate-spin" /> Starting dump…
      </div>
    )
  }

  const elapsed = job.started_at ? (job.finished_at ?? now) - job.started_at : 0
  const pct = job.status === "completed" ? 100 : job.items_total > 0 ? Math.min(99, (job.items_done / job.items_total) * 100) : 0
  const visibleLogs = logs.filter((l) => l.level !== "debug")

  return (
    <div className="flex flex-col gap-6">
      <Card>
        <CardHeader>
          <CardTitle className="flex flex-wrap items-center gap-3">
            Schema dump <span className="font-mono text-sm text-muted-foreground">{job.id}</span>
            <StatusBadge status={job.status} />
          </CardTitle>
          <CardDescription>
            {ENGINES[job.engine].label} · {job.schemas.join(", ")} · {CONTENT.find((c) => c.value === job.content)?.label} ·{" "}
            {formatDuration(elapsed)}
          </CardDescription>
          <CardAction>
            {running ? (
              <Button variant="destructive" onClick={() => api.dump.cancel(job.id).then(() => toast("Cancelling…"))}>
                <SquareIcon /> Cancel
              </Button>
            ) : (
              <Button variant="outline" onClick={onDone}>
                <RotateCcwIcon /> New dump
              </Button>
            )}
          </CardAction>
        </CardHeader>
        <CardContent className="flex flex-col gap-5">
          <div className="flex flex-col gap-2">
            <div className="flex justify-between gap-4 text-sm">
              <span className="truncate text-muted-foreground">
                {running ? (
                  <>
                    {job.phase}
                    {job.current && <span className="font-mono"> · {job.current}</span>}
                  </>
                ) : (
                  job.status
                )}
              </span>
              <span className="shrink-0 tabular-nums">
                {job.items_total > 0 ? `${job.items_done} / ${job.items_total} tables` : `${pct.toFixed(0)}%`}
              </span>
            </div>
            <Progress value={pct} />
          </div>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <Stat label="Written" value={formatBytes(job.file_size ?? job.bytes_written)} />
            <Stat label="Rows" value={job.rows > 0 ? job.rows.toLocaleString() : "—"} />
            <Stat label="Warnings" value={String(job.warning_count)} tone={job.warning_count ? "warn" : undefined} />
            <Stat label="File" value={job.output_path.split("/").pop() ?? ""} mono />
          </div>
          {job.status === "completed" && (
            <Alert>
              <CheckCircle2Icon />
              <AlertTitle>Dump saved</AlertTitle>
              <AlertDescription className="flex flex-wrap items-center justify-between gap-3">
                <span>
                  <span className="font-mono">{job.output_path}</span> ({formatBytes(job.file_size ?? 0)})
                </span>
                <Button asChild size="sm">
                  <a href={api.dump.downloadUrl(job.id)} download>
                    <DownloadIcon /> Download
                  </a>
                </Button>
              </AlertDescription>
            </Alert>
          )}
          {job.status === "failed" && job.error && (
            <Alert variant="destructive">
              <OctagonXIcon />
              <AlertTitle>Dump failed — no file was written</AlertTitle>
              <AlertDescription>{job.error}</AlertDescription>
            </Alert>
          )}
          {job.status === "cancelled" && (
            <Alert>
              <InfoIcon />
              <AlertTitle>Dump cancelled</AlertTitle>
              <AlertDescription>The partial file was removed.</AlertDescription>
            </Alert>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Log</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="h-72 overflow-y-auto rounded-lg border bg-muted/30 p-3 font-mono text-xs leading-relaxed">
            {visibleLogs.length === 0 && <p className="text-muted-foreground">Waiting for output…</p>}
            {visibleLogs.map((l) => (
              <div
                key={l.seq}
                className={`flex gap-3 ${l.level === "error" ? "text-destructive" : l.level === "warning" ? "text-amber-700 dark:text-amber-400" : ""}`}
              >
                <span className="shrink-0 text-muted-foreground tabular-nums">{new Date(l.ts * 1000).toLocaleTimeString()}</span>
                <span className="w-14 shrink-0 uppercase">{l.level}</span>
                <span className="break-all whitespace-pre-wrap">{l.message}</span>
              </div>
            ))}
            <div ref={logEnd} />
          </div>
        </CardContent>
      </Card>
    </div>
  )
}

function StatusBadge({ status }: { status: DumpJobStatus["status"] }) {
  if (status === "completed")
    return (
      <Badge className="bg-emerald-600 text-white">
        <CheckCircle2Icon /> completed
      </Badge>
    )
  if (status === "failed")
    return (
      <Badge variant="destructive">
        <XCircleIcon /> failed
      </Badge>
    )
  if (status === "cancelled") return <Badge variant="secondary">cancelled</Badge>
  return (
    <Badge variant="outline">
      <Loader2Icon className="animate-spin" /> {status}
    </Badge>
  )
}

function Stat({ label, value, tone, mono }: { label: string; value: string; tone?: "warn"; mono?: boolean }) {
  return (
    <div className="min-w-0 rounded-lg border p-3">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div
        className={`truncate font-semibold tabular-nums ${mono ? "font-mono text-sm leading-7" : "text-xl"} ${tone === "warn" ? "text-amber-600 dark:text-amber-400" : ""}`}
        title={value}
      >
        {value}
      </div>
    </div>
  )
}
