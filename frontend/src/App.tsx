import { useEffect, useState } from "react"
import { ThemeProvider, useTheme } from "next-themes"
import {
  ArrowLeftIcon,
  ArrowRightLeftIcon,
  TerminalSquareIcon,
  ArrowRightIcon,
  DatabaseZapIcon,
  FileCode2Icon,
  Loader2Icon,
  MoonIcon,
  PlayIcon,
  SunIcon,
  TriangleAlertIcon,
} from "lucide-react"
import { toast } from "sonner"

import { ConnectionCard, type ConnState } from "@/components/connection-card"
import { CreateDatabaseDialog } from "@/components/create-database-dialog"
import { MigrationMonitor } from "@/components/migration-monitor"
import { OptionsPanel } from "@/components/options-panel"
import { emptySelection, SchemaPicker, selectedTables, withDbChecked, type SelectionMap } from "@/components/schema-picker"
import { SourceSchemas } from "@/components/source-schemas"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Toaster } from "@/components/ui/sonner"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { WorkbenchPage } from "@/pages/workbench"
import { TooltipProvider } from "@/components/ui/tooltip"
import {
  api,
  type DatabaseSummary,
  type MigrationOptions,
  type MigrationRequest,
  type MySQLConn,
  type PGConn,
  type PreviewResult,
} from "@/lib/api"

// Defaults match docker-compose.yml; hosts are resolved by the backend container.
const DEFAULT_MYSQL: MySQLConn = { host: "mysql", port: 3306, user: "root", password: "mysql" }
const DEFAULT_PG: PGConn = { host: "postgres", port: 5432, user: "postgres", password: "postgres", database: "migrated" }
const DEFAULT_OPTIONS: MigrationOptions = {
  include_data: true,
  create_indexes: true,
  create_foreign_keys: true,
  create_views: true,
  drop_existing: false,
  tinyint1_as_boolean: true,
  lowercase_identifiers: true,
  verify_row_counts: true,
  batch_size: 5000,
}

type Step = "connect" | "select" | "migrate"

function loadStored<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key)
    return raw ? { ...fallback, ...JSON.parse(raw) } : fallback
  } catch {
    return fallback
  }
}

function store(key: string, value: unknown) {
  try {
    localStorage.setItem(key, JSON.stringify(value))
  } catch {
    /* storage unavailable */
  }
}

export default function App() {
  return (
    <ThemeProvider attribute="class" defaultTheme="system" enableSystem disableTransitionOnChange>
      <TooltipProvider>
        <Shell />
        <Toaster richColors position="bottom-right" />
      </TooltipProvider>
    </ThemeProvider>
  )
}

function Migrator() {
  const [step, setStep] = useState<Step>("connect")
  const [mysql, setMysql] = useState<MySQLConn>(() => loadStored("m2p.mysql", DEFAULT_MYSQL))
  const [pg, setPg] = useState<PGConn>(() => loadStored("m2p.pg", DEFAULT_PG))
  const [mysqlState, setMysqlState] = useState<ConnState>({ kind: "idle" })
  const [pgState, setPgState] = useState<ConnState>({ kind: "idle" })
  const [createDb, setCreateDb] = useState({ open: false, busy: false })
  const [databases, setDatabases] = useState<DatabaseSummary[]>([])
  const [dbLoading, setDbLoading] = useState(false)
  const [selection, setSelection] = useState<SelectionMap>({})
  const [options, setOptions] = useState<MigrationOptions>(() => loadStored("m2p.options", DEFAULT_OPTIONS))
  const [jobId, setJobId] = useState<string | null>(null)
  // Target of the running/finished job; the query console must not follow later edits to `pg`.
  const [jobTarget, setJobTarget] = useState<PGConn | null>(null)
  const [starting, setStarting] = useState(false)
  const [preview, setPreview] = useState<{ open: boolean; loading: boolean; data?: PreviewResult }>({
    open: false,
    loading: false,
  })

  // Persist everything except passwords.
  useEffect(() => store("m2p.mysql", { ...mysql, password: undefined }), [mysql])
  useEffect(() => store("m2p.pg", { ...pg, password: undefined }), [pg])
  useEffect(() => store("m2p.options", options), [options])

  const bothConnected = mysqlState.kind === "ok" && pgState.kind === "ok"
  const chosen = databases.filter((d) => selection[d.name]?.checked)

  async function test(kind: "mysql" | "pg") {
    const set = kind === "mysql" ? setMysqlState : setPgState
    set({ kind: "testing" })
    try {
      const result = kind === "mysql" ? await api.testMySQL(mysql) : await api.testPostgres(pg)
      if (kind === "pg" && result.database_missing) {
        // Server reachable, database absent: offer to create it.
        set({ kind: "missing", result })
        setCreateDb({ open: true, busy: false })
        return
      }
      set({ kind: "ok", result })
      // A working source connection immediately lists its schemas on the Connect step.
      if (kind === "mysql") void loadDatabases()
    } catch (e) {
      set({ kind: "error", message: (e as Error).message })
    }
  }

  async function createTargetDatabase() {
    setCreateDb({ open: true, busy: true })
    try {
      const res = await api.createPostgresDatabase(pg)
      toast.success(res.created ? `Created database “${res.database}”` : `Database “${res.database}” already exists`, {
        description: res.note ?? `Encoding ${res.encoding}`,
      })
      setCreateDb({ open: false, busy: false })
      await test("pg")
    } catch (e) {
      setCreateDb({ open: true, busy: false })
      toast.error("Could not create database", { description: (e as Error).message })
    }
  }

  async function loadDatabases() {
    setDbLoading(true)
    try {
      const list = await api.databases(mysql)
      setDatabases(list)
      setSelection((prev) => {
        const next: SelectionMap = {}
        for (const d of list) next[d.name] = prev[d.name] ?? emptySelection(d, options.lowercase_identifiers)
        return next
      })
    } catch (e) {
      toast.error("Could not list databases", { description: (e as Error).message })
    } finally {
      setDbLoading(false)
    }
  }

  function buildRequest(): MigrationRequest {
    return {
      source: mysql,
      target: pg,
      options,
      databases: chosen.map((d) => {
        const sel = selection[d.name]
        return { name: d.name, tables: selectedTables(sel), target_schema: sel.targetSchema.trim() || null }
      }),
    }
  }

  async function openPreview() {
    setPreview({ open: true, loading: true })
    try {
      setPreview({ open: true, loading: false, data: await api.preview(buildRequest()) })
    } catch (e) {
      setPreview({ open: false, loading: false })
      toast.error("Preview failed", { description: (e as Error).message })
    }
  }

  async function start() {
    setStarting(true)
    try {
      const req = buildRequest()
      const { id } = await api.start(req)
      setJobTarget(req.target)
      setJobId(id)
      setStep("migrate")
    } catch (e) {
      toast.error("Could not start migration", { description: (e as Error).message })
    } finally {
      setStarting(false)
    }
  }

  return (
    <>
        <Tabs value={step} onValueChange={(v) => setStep(v as Step)} className="gap-6">
          <TabsList className="w-full sm:w-auto">
            <TabsTrigger value="connect">1 · Connect</TabsTrigger>
            <TabsTrigger value="select" disabled={!bothConnected}>
              2 · Select
            </TabsTrigger>
            <TabsTrigger value="migrate" disabled={!jobId}>
              3 · Migrate
            </TabsTrigger>
          </TabsList>

          <TabsContent value="connect" className="flex flex-col gap-6">
            <div className="grid gap-6 md:grid-cols-2">
              <ConnectionCard
                title="MySQL source"
                description="The MySQL 5.6 server to read from."
                accent="bg-sky-500/15 text-sky-700 dark:text-sky-300"
                value={mysql}
                state={mysqlState}
                onChange={(v) => {
                  setMysql(v)
                  setMysqlState({ kind: "idle" })
                  // The schema list and selection belong to the previous server.
                  setDatabases([])
                  setSelection({})
                }}
                onTest={() => test("mysql")}
                fields={[
                  { key: "host", label: "Host", className: "col-span-6 sm:col-span-4" },
                  { key: "port", label: "Port", type: "number", className: "col-span-6 sm:col-span-2" },
                  { key: "user", label: "User" },
                  { key: "password", label: "Password", type: "password" },
                ]}
              />
              <ConnectionCard
                title="PostgreSQL target"
                description="The database schemas will be created in."
                accent="bg-indigo-500/15 text-indigo-700 dark:text-indigo-300"
                value={pg}
                state={pgState}
                onChange={(v) => {
                  setPg(v)
                  setPgState({ kind: "idle" })
                }}
                onTest={() => test("pg")}
                onCreateDatabase={() => setCreateDb({ open: true, busy: false })}
                fields={[
                  { key: "host", label: "Host", className: "col-span-6 sm:col-span-4" },
                  { key: "port", label: "Port", type: "number", className: "col-span-6 sm:col-span-2" },
                  { key: "user", label: "User" },
                  { key: "password", label: "Password", type: "password" },
                  { key: "database", label: "Database", className: "col-span-6" },
                ]}
              />
            </div>
            {mysqlState.kind === "ok" && (
              <SourceSchemas
                host={`${mysql.host}:${mysql.port}`}
                databases={databases}
                loading={dbLoading}
                selection={selection}
                onToggle={(name, checked) =>
                  setSelection((prev) => ({ ...prev, [name]: withDbChecked(prev[name], checked) }))
                }
                onRefresh={loadDatabases}
              />
            )}
            <p className="text-sm text-muted-foreground">
              Hostnames are resolved by the backend. With Docker Compose use <code className="font-mono">mysql</code> and{" "}
              <code className="font-mono">postgres</code>.
            </p>
            <div className="flex justify-end">
              <Button
                size="lg"
                disabled={!bothConnected}
                onClick={() => {
                  setStep("select")
                  if (databases.length === 0) void loadDatabases()
                }}
              >
                Continue <ArrowRightIcon />
              </Button>
            </div>
          </TabsContent>

          <TabsContent value="select" className="flex flex-col gap-6">
            <div className="grid items-start gap-6 lg:grid-cols-[minmax(0,1fr)_320px]">
              <SchemaPicker
                connection={mysql}
                databases={databases}
                loading={dbLoading}
                selection={selection}
                onSelectionChange={setSelection}
                onRefresh={loadDatabases}
              />
              <OptionsPanel value={options} onChange={setOptions} />
            </div>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <Button variant="ghost" onClick={() => setStep("connect")}>
                <ArrowLeftIcon /> Back
              </Button>
              <div className="flex gap-3">
                <Button variant="outline" disabled={chosen.length === 0} onClick={openPreview}>
                  <FileCode2Icon /> Preview SQL
                </Button>
                <Button disabled={chosen.length === 0 || starting} onClick={start}>
                  {starting ? <Loader2Icon className="animate-spin" /> : <PlayIcon />}
                  Start migration{chosen.length > 0 && ` (${chosen.length})`}
                </Button>
              </div>
            </div>
          </TabsContent>

          <TabsContent value="migrate">
            {jobId && jobTarget && (
              <MigrationMonitor
                jobId={jobId}
                target={jobTarget}
                onReset={() => {
                  setStep("select")
                  void loadDatabases()
                }}
              />
            )}
          </TabsContent>
        </Tabs>

      <CreateDatabaseDialog
        open={createDb.open}
        busy={createDb.busy}
        database={pg.database}
        server={`${pg.host}:${pg.port}`}
        user={pg.user}
        onOpenChange={(open) => setCreateDb({ open, busy: false })}
        onConfirm={createTargetDatabase}
      />

      <Dialog open={preview.open} onOpenChange={(open) => setPreview((p) => ({ ...p, open }))}>
        <DialogContent className="sm:max-w-4xl">
          <DialogHeader>
            <DialogTitle>Generated PostgreSQL DDL</DialogTitle>
            <DialogDescription>
              {preview.data
                ? `${preview.data.table_count} table(s). Data is streamed with COPY between the table and index steps.`
                : "Reading schema metadata…"}
            </DialogDescription>
          </DialogHeader>
          {preview.loading || !preview.data ? (
            <div className="flex items-center justify-center gap-2 py-16 text-muted-foreground">
              <Loader2Icon className="size-4 animate-spin" /> Generating…
            </div>
          ) : (
            <div className="flex min-w-0 flex-col gap-3">
              {preview.data.warnings.length > 0 && (
                <div className="flex gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-800 dark:text-amber-300">
                  <TriangleAlertIcon className="mt-0.5 size-4 shrink-0" />
                  <span>{preview.data.warnings.length} warning(s) — listed at the end of the script.</span>
                </div>
              )}
              <ScrollArea className="h-[60vh] rounded-lg border bg-muted/30">
                <pre className="p-4 font-mono text-xs leading-relaxed">{preview.data.sql}</pre>
              </ScrollArea>
              <div className="flex justify-end gap-2">
                <Button
                  variant="outline"
                  onClick={() => {
                    void navigator.clipboard.writeText(preview.data!.sql)
                    toast.success("Copied to clipboard")
                  }}
                >
                  Copy
                </Button>
                <Button
                  onClick={() => {
                    const blob = new Blob([preview.data!.sql], { type: "text/sql" })
                    const a = document.createElement("a")
                    a.href = URL.createObjectURL(blob)
                    a.download = "mysql2pgs-schema.sql"
                    a.click()
                    URL.revokeObjectURL(a.href)
                  }}
                >
                  Download .sql
                </Button>
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </>
  )
}

type Route = "migrate" | "workbench"

const routeFromHash = (): Route => (window.location.hash.startsWith("#/workbench") ? "workbench" : "migrate")

function Shell() {
  const [route, setRoute] = useState<Route>(routeFromHash)
  useEffect(() => {
    const onHash = () => setRoute(routeFromHash())
    window.addEventListener("hashchange", onHash)
    return () => window.removeEventListener("hashchange", onHash)
  }, [])

  const nav: { route: Route; href: string; label: string; icon: typeof DatabaseZapIcon }[] = [
    { route: "migrate", href: "#/", label: "Migrate", icon: ArrowRightLeftIcon },
    { route: "workbench", href: "#/workbench", label: "SQL Workbench", icon: TerminalSquareIcon },
  ]
  const width = route === "workbench" ? "max-w-screen-2xl" : "max-w-6xl"

  return (
    <div className="min-h-svh bg-background">
      <header className="border-b">
        <div className={`mx-auto flex ${width} flex-wrap items-center gap-x-6 gap-y-3 px-4 py-3`}>
          <div className="flex min-w-0 items-center gap-3">
            <span className="flex size-9 items-center justify-center rounded-lg bg-primary text-primary-foreground">
              <DatabaseZapIcon className="size-5" />
            </span>
            <div className="min-w-0">
              <h1 className="text-lg leading-tight font-semibold">mysql2pgs</h1>
              <p className="truncate text-sm text-muted-foreground">MySQL 5.6 → PostgreSQL migration</p>
            </div>
          </div>
          <nav className="order-last flex w-full gap-1 sm:order-none sm:w-auto" aria-label="Main">
            {nav.map((n) => (
              <a
                key={n.route}
                href={n.href}
                aria-current={route === n.route ? "page" : undefined}
                className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium transition-colors ${
                  route === n.route ? "bg-muted text-foreground" : "text-muted-foreground hover:bg-muted/60 hover:text-foreground"
                }`}
              >
                <n.icon className="size-4" /> {n.label}
              </a>
            ))}
          </nav>
          <div className="ml-auto">
            <ThemeToggle />
          </div>
        </div>
      </header>
      {/* Both pages stay mounted so switching never loses a running migration or a draft query. */}
      <main className={`mx-auto ${width} px-4 py-6`}>
        <div hidden={route !== "migrate"}>
          <Migrator />
        </div>
        <div hidden={route !== "workbench"}>
          <WorkbenchPage />
        </div>
      </main>
    </div>
  )
}

function ThemeToggle() {
  const { resolvedTheme, setTheme } = useTheme()
  const dark = resolvedTheme === "dark"
  return (
    <Button variant="ghost" size="icon" aria-label="Toggle theme" onClick={() => setTheme(dark ? "light" : "dark")}>
      {dark ? <SunIcon /> : <MoonIcon />}
    </Button>
  )
}
