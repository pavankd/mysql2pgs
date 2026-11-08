import { DatabaseIcon, Loader2Icon, RefreshCwIcon } from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Checkbox } from "@/components/ui/checkbox"
import type { SelectionMap } from "@/components/schema-picker"
import { formatBytes, plural, type DatabaseSummary } from "@/lib/api"

interface Props {
  host: string
  databases: DatabaseSummary[]
  loading: boolean
  selection: SelectionMap
  onToggle: (name: string, checked: boolean) => void
  onRefresh: () => void
}

/** Schemas found on the source server, shown on the Connect step once the MySQL test succeeds. */
export function SourceSchemas({ host, databases, loading, selection, onToggle, onRefresh }: Props) {
  const totalTables = databases.reduce((n, d) => n + d.table_count, 0)
  const totalSize = databases.reduce((n, d) => n + d.size_bytes, 0)
  const selectedCount = databases.filter((d) => selection[d.name]?.checked).length

  return (
    <Card>
      <CardHeader>
        <CardTitle>Source schemas</CardTitle>
        <CardDescription>
          {loading && databases.length === 0
            ? `Reading schemas on ${host}…`
            : `${plural(databases.length, "schema")} on ${host} · ${plural(totalTables, "table")} · ${formatBytes(totalSize)}. Ticked schemas carry over to the next step.`}
        </CardDescription>
        <CardAction className="flex items-center gap-2">
          {selectedCount > 0 && <Badge variant="secondary">{selectedCount} selected</Badge>}
          <Button variant="outline" size="sm" onClick={onRefresh} disabled={loading}>
            <RefreshCwIcon className={loading ? "animate-spin" : ""} /> Refresh
          </Button>
        </CardAction>
      </CardHeader>
      <CardContent>
        {loading && databases.length === 0 ? (
          <div className="flex items-center justify-center gap-2 py-8 text-sm text-muted-foreground">
            <Loader2Icon className="size-4 animate-spin" /> Loading schemas…
          </div>
        ) : databases.length === 0 ? (
          <p className="py-8 text-center text-sm text-muted-foreground">
            No user schemas found. System schemas (mysql, information_schema, performance_schema) are hidden.
          </p>
        ) : (
          <div className="max-h-80 overflow-y-auto">
            <ul className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
              {databases.map((db) => {
                const id = `src-schema-${db.name}`
                const checked = selection[db.name]?.checked ?? false
                return (
                  <li key={db.name}>
                    <label
                      htmlFor={id}
                      className="flex cursor-pointer items-start gap-3 rounded-lg border p-3 transition-colors hover:bg-muted/50 has-data-[state=checked]:border-primary/50 has-data-[state=checked]:bg-muted/40"
                    >
                      <Checkbox
                        id={id}
                        className="mt-0.5"
                        checked={checked}
                        onCheckedChange={(v) => onToggle(db.name, v === true)}
                      />
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-1.5 font-medium">
                          <DatabaseIcon className="size-3.5 shrink-0 text-muted-foreground" />
                          <span className="truncate">{db.name}</span>
                        </div>
                        <div className="mt-0.5 flex flex-wrap gap-x-3 text-xs text-muted-foreground">
                          <span>{plural(db.table_count, "table")}</span>
                          {db.view_count > 0 && <span>{plural(db.view_count, "view")}</span>}
                          <span>{formatBytes(db.size_bytes)}</span>
                          <span>{db.charset}</span>
                        </div>
                      </div>
                    </label>
                  </li>
                )
              })}
            </ul>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
