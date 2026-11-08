import { useEffect, useRef, useState } from "react"
import {
  AlertTriangleIcon,
  CheckCircle2Icon,
  CircleDashedIcon,
  Loader2Icon,
  OctagonXIcon,
  RotateCcwIcon,
  SquareIcon,
  XCircleIcon,
} from "lucide-react"
import { toast } from "sonner"

import { QueryConsole } from "@/components/query-console"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Progress } from "@/components/ui/progress"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import {
  api,
  formatDuration,
  formatNumber,
  type JobStatus,
  type LogEntry,
  type MigrationSnapshot,
  type PGConn,
  type TableProgress,
} from "@/lib/api"

const FINISHED: JobStatus[] = ["completed", "completed_with_errors", "failed", "cancelled"]

export function MigrationMonitor({ jobId, target, onReset }: { jobId: string; target: PGConn; onReset: () => void }) {
  const [snap, setSnap] = useState<MigrationSnapshot | null>(null)
  const [logs, setLogs] = useState<LogEntry[]>([])
  const [logFilter, setLogFilter] = useState<"all" | "problems">("all")
  const [now, setNow] = useState(() => Date.now() / 1000)
  const lastSeq = useRef(0)
  const logEnd = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let stopped = false
    let timer: ReturnType<typeof setTimeout>
    lastSeq.current = 0
    setLogs([])

    async function poll() {
      try {
        const s = await api.get(jobId, lastSeq.current)
        if (stopped) return
        if (s.logs.length) {
          lastSeq.current = s.logs[s.logs.length - 1].seq
          setLogs((prev) => [...prev, ...s.logs].slice(-5000))
        }
        setSnap(s)
        if (FINISHED.includes(s.status)) {
          notify(s)
          return
        }
      } catch (e) {
        if (stopped) return
        toast.error("Lost contact with the backend", { description: (e as Error).message })
      }
      timer = setTimeout(poll, 800)
    }
    void poll()
    return () => {
      stopped = true
      clearTimeout(timer)
    }
  }, [jobId])

  // Tick the elapsed-time clock while running.
  const running = snap ? !FINISHED.includes(snap.status) : true
  useEffect(() => {
    if (!running) return
    const t = setInterval(() => setNow(Date.now() / 1000), 1000)
    return () => clearInterval(t)
  }, [running])

  useEffect(() => {
    logEnd.current?.scrollIntoView({ block: "end" })
  }, [logs.length, logFilter])

  if (!snap) {
    return (
      <div className="flex items-center justify-center gap-2 py-24 text-muted-foreground">
        <Loader2Icon className="size-5 animate-spin" /> Starting migration…
      </div>
    )
  }

  const finished = !running
  const elapsed = snap.started_at ? (snap.finished_at ?? now) - snap.started_at : 0
  const pct = finished
    ? 100
    : snap.rows_estimated > 0
      ? Math.min(99, (snap.rows_copied / snap.rows_estimated) * 100)
      : 0
  const shownLogs = logs.filter((l) =>
    logFilter === "all" ? l.level !== "debug" : l.level === "warning" || l.level === "error",
  )

  return (
    <div className="flex flex-col gap-6">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-3">
            Migration <span className="font-mono text-sm text-muted-foreground">{snap.id}</span>
            <JobBadge status={snap.status} />
          </CardTitle>
          <CardDescription>
            {snap.databases.join(", ")} · {finished ? "finished" : snap.phase} · {formatDuration(elapsed)}
          </CardDescription>
          <CardAction>
            {finished ? (
              <Button onClick={onReset}>
                <RotateCcwIcon /> New migration
              </Button>
            ) : (
              <Button
                variant="destructive"
                onClick={() => api.cancel(snap.id).then(() => toast("Cancelling…"))}
              >
                <SquareIcon /> Cancel
              </Button>
            )}
          </CardAction>
        </CardHeader>
        <CardContent className="flex flex-col gap-5">
          <div className="flex flex-col gap-2">
            <div className="flex justify-between text-sm">
              <span className="text-muted-foreground capitalize">{finished ? snap.status.replaceAll("_", " ") : snap.phase}</span>
              <span className="tabular-nums">{pct.toFixed(0)}%</span>
            </div>
            <Progress value={pct} />
          </div>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <Stat label="Tables" value={`${snap.tables_done + snap.tables_failed} / ${snap.tables_total}`} />
            <Stat label="Rows copied" value={formatNumber(snap.rows_copied)} />
            <Stat label="Warnings" value={String(snap.warning_count)} tone={snap.warning_count ? "warn" : undefined} />
            <Stat label="Errors" value={String(snap.error_count)} tone={snap.error_count ? "error" : undefined} />
          </div>
          {snap.status === "failed" && snap.error && (
            <Alert variant="destructive">
              <OctagonXIcon />
              <AlertTitle>Migration failed</AlertTitle>
              <AlertDescription>{snap.error}</AlertDescription>
            </Alert>
          )}
        </CardContent>
      </Card>

      {(snap.status === "completed" || snap.status === "completed_with_errors") && (
        <QueryConsole
          connection={target}
          tables={snap.tables
            .filter((t) => t.status === "done")
            .map((t) => ({ schema: t.target_schema, table: t.target_table }))}
        />
      )}

      <Card>
        <CardHeader>
          <CardTitle>Tables</CardTitle>
          <CardDescription>Per-table progress. Row counts are verified against MySQL when enabled.</CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Source</TableHead>
                <TableHead>Target</TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="w-40">Progress</TableHead>
                <TableHead className="text-right">Rows</TableHead>
                <TableHead className="text-right">Verified</TableHead>
                <TableHead className="text-right">Time</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {snap.tables.map((t) => (
                <TableRow key={t.key}>
                  <TableCell className="font-mono text-xs">{t.key}</TableCell>
                  <TableCell className="font-mono text-xs">{t.target}</TableCell>
                  <TableCell>
                    <TableBadge t={t} />
                  </TableCell>
                  <TableCell>
                    <Progress value={tablePct(t)} className="h-1.5" />
                  </TableCell>
                  <TableCell className="text-right tabular-nums">{formatNumber(t.rows_copied)}</TableCell>
                  <TableCell className="text-right">
                    <Verified t={t} />
                  </TableCell>
                  <TableCell className="text-right text-muted-foreground tabular-nums">
                    {t.seconds ? formatDuration(t.seconds) : "—"}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Log</CardTitle>
          <CardAction>
            <Tabs value={logFilter} onValueChange={(v) => setLogFilter(v as "all" | "problems")}>
              <TabsList>
                <TabsTrigger value="all">All</TabsTrigger>
                <TabsTrigger value="problems">Warnings &amp; errors</TabsTrigger>
              </TabsList>
            </Tabs>
          </CardAction>
        </CardHeader>
        <CardContent>
          <ScrollArea className="h-80 rounded-lg border bg-muted/30">
            <div className="p-3 font-mono text-xs leading-relaxed">
              {shownLogs.length === 0 && <p className="text-muted-foreground">Nothing to show.</p>}
              {shownLogs.map((l) => (
                <div key={l.seq} className={`flex gap-3 ${LOG_TONE[l.level]}`}>
                  <span className="shrink-0 text-muted-foreground tabular-nums">
                    {new Date(l.ts * 1000).toLocaleTimeString()}
                  </span>
                  <span className="w-14 shrink-0 uppercase">{l.level}</span>
                  <span className="break-all whitespace-pre-wrap">{l.message}</span>
                </div>
              ))}
              <div ref={logEnd} />
            </div>
          </ScrollArea>
        </CardContent>
      </Card>
    </div>
  )
}

const LOG_TONE: Record<LogEntry["level"], string> = {
  info: "",
  debug: "text-muted-foreground",
  warning: "text-amber-700 dark:text-amber-400",
  error: "text-destructive",
}

function notify(s: MigrationSnapshot) {
  if (s.status === "completed") toast.success("Migration completed", { description: `${formatNumber(s.rows_copied)} rows copied` })
  else if (s.status === "completed_with_errors") toast.warning("Migration finished with errors")
  else if (s.status === "failed") toast.error("Migration failed", { description: s.error ?? undefined })
  else if (s.status === "cancelled") toast("Migration cancelled")
}

function tablePct(t: TableProgress) {
  if (t.status === "done") return 100
  if (t.status === "pending" || t.status === "creating") return 0
  if (t.status === "indexing") return 100
  return t.row_estimate > 0 ? Math.min(99, (t.rows_copied / t.row_estimate) * 100) : 50
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: "warn" | "error" }) {
  const color = tone === "error" ? "text-destructive" : tone === "warn" ? "text-amber-600 dark:text-amber-400" : ""
  return (
    <div className="rounded-lg border p-3">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={`text-xl font-semibold tabular-nums ${color}`}>{value}</div>
    </div>
  )
}

function JobBadge({ status }: { status: JobStatus }) {
  const label = status.replaceAll("_", " ")
  if (status === "completed")
    return <Badge className="bg-emerald-600 text-white"><CheckCircle2Icon /> {label}</Badge>
  if (status === "completed_with_errors")
    return <Badge className="bg-amber-500 text-white"><AlertTriangleIcon /> {label}</Badge>
  if (status === "failed") return <Badge variant="destructive"><XCircleIcon /> {label}</Badge>
  if (status === "cancelled") return <Badge variant="secondary">{label}</Badge>
  return <Badge variant="outline"><Loader2Icon className="animate-spin" /> {label}</Badge>
}

function TableBadge({ t }: { t: TableProgress }) {
  switch (t.status) {
    case "done":
      return <Badge variant="outline" className="border-emerald-500/40 text-emerald-700 dark:text-emerald-400"><CheckCircle2Icon /> done</Badge>
    case "failed":
      return (
        <Badge variant="destructive" title={t.error ?? undefined} className="max-w-56">
          <XCircleIcon /> <span className="truncate">{t.error ?? "failed"}</span>
        </Badge>
      )
    case "pending":
      return <Badge variant="secondary"><CircleDashedIcon /> pending</Badge>
    default:
      return <Badge variant="outline"><Loader2Icon className="animate-spin" /> {t.status}</Badge>
  }
}

function Verified({ t }: { t: TableProgress }) {
  if (t.source_rows == null || t.target_rows == null) return <span className="text-muted-foreground">—</span>
  if (t.source_rows === t.target_rows)
    return <span className="inline-flex items-center gap-1 text-emerald-700 dark:text-emerald-400"><CheckCircle2Icon className="size-3.5" /> match</span>
  return (
    <span className="text-destructive tabular-nums" title={`MySQL ${t.source_rows} vs PostgreSQL ${t.target_rows}`}>
      {formatNumber(t.source_rows)} ≠ {formatNumber(t.target_rows)}
    </span>
  )
}
