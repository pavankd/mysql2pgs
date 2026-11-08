import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { formatNumber, plural, type QueryResultSet } from "@/lib/api"
import { cn } from "@/lib/utils"

interface Props {
  result: QueryResultSet
  /** Show a leading row-number column (Workbench style). */
  rowNumbers?: boolean
  className?: string
}

/** Tabular view of one statement's result. Commands render as their status line. */
export function ResultGrid({ result, rowNumbers = false, className }: Props) {
  if (result.kind === "command" || result.columns.length === 0) {
    return <p className="rounded-lg border bg-muted/30 px-3 py-2 font-mono text-xs">{result.status}</p>
  }
  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      <div className={cn("max-h-96 overflow-auto rounded-lg border", className)}>
        <Table>
          <TableHeader className="sticky top-0 z-10 bg-background shadow-[0_1px_0_var(--border)]">
            <TableRow>
              {rowNumbers && <TableHead className="w-10 text-right text-xs text-muted-foreground">#</TableHead>}
              {result.columns.map((c, i) => (
                <TableHead key={i} className="font-mono text-xs">
                  {c.name}
                  <span className="ml-1.5 font-normal text-muted-foreground">{c.type}</span>
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {result.rows.length === 0 ? (
              <TableRow>
                <TableCell colSpan={result.columns.length + (rowNumbers ? 1 : 0)} className="text-center text-muted-foreground">
                  No rows
                </TableCell>
              </TableRow>
            ) : (
              result.rows.map((row, ri) => (
                <TableRow key={ri}>
                  {rowNumbers && (
                    <TableCell className="text-right text-xs text-muted-foreground tabular-nums">{ri + 1}</TableCell>
                  )}
                  {row.map((v, ci) => (
                    <TableCell key={ci} className="max-w-80 truncate font-mono text-xs" title={display(v)}>
                      {v === null ? <span className="text-muted-foreground italic">NULL</span> : display(v)}
                    </TableCell>
                  ))}
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </div>
      <p className="text-xs text-muted-foreground">{summarize(result)}</p>
    </div>
  )
}

export function summarize(result: QueryResultSet): string {
  if (result.kind === "command" || result.columns.length === 0) return result.status
  const total = result.row_count >= 0 ? result.row_count : result.rows.length
  return result.truncated
    ? `Showing first ${formatNumber(result.rows.length)} of ${formatNumber(total)} rows (row limit reached)`
    : `${plural(result.rows.length, "row")} returned`
}

function display(v: unknown): string {
  if (v === null) return "NULL"
  if (typeof v === "object") return JSON.stringify(v)
  return String(v)
}
