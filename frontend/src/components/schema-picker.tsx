import { useMemo, useState } from "react"
import { ArrowRightIcon, ChevronRightIcon, Loader2Icon, RefreshCwIcon, SearchIcon } from "lucide-react"
import { toast } from "sonner"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Checkbox } from "@/components/ui/checkbox"
import { Collapsible, CollapsibleContent } from "@/components/ui/collapsible"
import { Input } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api, formatBytes, formatNumber, type DatabaseSummary, type MySQLConn, type TableSummary } from "@/lib/api"

export interface DbSelection {
  checked: boolean
  tables: TableSummary[] | null
  selected: Set<string>
  targetSchema: string
  expanded: boolean
  loading: boolean
}

export type SelectionMap = Record<string, DbSelection>

export function emptySelection(db: DatabaseSummary, lowercase: boolean): DbSelection {
  return {
    checked: false,
    tables: null,
    selected: new Set(),
    targetSchema: lowercase ? db.name.toLowerCase() : db.name,
    expanded: false,
    loading: false,
  }
}

/** Selected table names, or null meaning "all tables" (also when not yet loaded). */
export function selectedTables(sel: DbSelection): string[] | null {
  if (!sel.tables) return null
  if (sel.selected.size === sel.tables.length) return null
  return sel.tables.filter((t) => sel.selected.has(t.name)).map((t) => t.name)
}

interface Props {
  connection: MySQLConn
  databases: DatabaseSummary[]
  loading: boolean
  selection: SelectionMap
  onSelectionChange: (updater: (prev: SelectionMap) => SelectionMap) => void
  onRefresh: () => void
}

export function SchemaPicker({ connection, databases, loading, selection, onSelectionChange, onRefresh }: Props) {
  const [filter, setFilter] = useState("")

  const visible = useMemo(
    () => databases.filter((d) => d.name.toLowerCase().includes(filter.toLowerCase())),
    [databases, filter],
  )
  const checkedCount = databases.filter((d) => selection[d.name]?.checked).length

  const update = (name: string, patch: Partial<DbSelection>) =>
    onSelectionChange((prev) => ({ ...prev, [name]: { ...prev[name], ...patch } }))

  async function loadTables(name: string) {
    update(name, { loading: true })
    try {
      const tables = await api.tables(connection, name)
      onSelectionChange((prev) => {
        const cur = prev[name]
        // Default every table to the database's current checked state.
        const selected = new Set(cur.checked ? tables.map((t) => t.name) : [])
        return { ...prev, [name]: { ...cur, tables, selected, loading: false } }
      })
    } catch (e) {
      update(name, { loading: false, expanded: false })
      toast.error(`Could not list tables in ${name}`, { description: (e as Error).message })
    }
  }

  function toggleExpanded(name: string) {
    const sel = selection[name]
    update(name, { expanded: !sel.expanded })
    if (!sel.expanded && !sel.tables && !sel.loading) void loadTables(name)
  }

  function setDbChecked(name: string, checked: boolean) {
    const sel = selection[name]
    update(name, {
      checked,
      selected: new Set(checked && sel.tables ? sel.tables.map((t) => t.name) : []),
    })
  }

  function setTableChecked(db: string, table: string, checked: boolean) {
    onSelectionChange((prev) => {
      const cur = prev[db]
      const selected = new Set(cur.selected)
      if (checked) selected.add(table)
      else selected.delete(table)
      return { ...prev, [db]: { ...cur, selected, checked: selected.size > 0 } }
    })
  }

  function setAll(checked: boolean) {
    onSelectionChange((prev) => {
      const next = { ...prev }
      for (const d of visible) {
        const cur = next[d.name]
        next[d.name] = { ...cur, checked, selected: new Set(checked && cur.tables ? cur.tables.map((t) => t.name) : []) }
      }
      return next
    })
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Source databases</CardTitle>
        <CardDescription>
          Pick databases (and optionally individual tables) to copy. Each database becomes a PostgreSQL schema.
        </CardDescription>
        <CardAction>
          <Button variant="outline" size="sm" onClick={onRefresh} disabled={loading}>
            <RefreshCwIcon className={loading ? "animate-spin" : ""} /> Refresh
          </Button>
        </CardAction>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex flex-wrap items-center gap-2">
          <div className="relative min-w-0 flex-1">
            <SearchIcon className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input placeholder="Filter databases…" value={filter} onChange={(e) => setFilter(e.target.value)} className="pl-8" />
          </div>
          <Button variant="ghost" size="sm" onClick={() => setAll(true)}>Select all</Button>
          <Button variant="ghost" size="sm" onClick={() => setAll(false)}>Clear</Button>
          <Badge variant="secondary">{checkedCount} selected</Badge>
        </div>

        {loading && databases.length === 0 ? (
          <div className="flex items-center justify-center gap-2 py-10 text-sm text-muted-foreground">
            <Loader2Icon className="size-4 animate-spin" /> Loading databases…
          </div>
        ) : visible.length === 0 ? (
          <p className="py-10 text-center text-sm text-muted-foreground">
            {databases.length === 0 ? "No user databases found on this server." : "No databases match the filter."}
          </p>
        ) : (
          <div className="divide-y rounded-lg border">
            {visible.map((db) => {
              const sel = selection[db.name]
              if (!sel) return null
              const partial = sel.tables && sel.selected.size > 0 && sel.selected.size < sel.tables.length
              return (
                <Collapsible key={db.name} open={sel.expanded}>
                  <div className="flex flex-wrap items-center gap-3 px-3 py-2.5">
                    <Button
                      variant="ghost"
                      size="icon-sm"
                      aria-label={sel.expanded ? "Collapse" : "Expand"}
                      onClick={() => toggleExpanded(db.name)}
                    >
                      <ChevronRightIcon className={`transition-transform ${sel.expanded ? "rotate-90" : ""}`} />
                    </Button>
                    <Checkbox
                      id={`db-${db.name}`}
                      checked={partial ? "indeterminate" : sel.checked}
                      onCheckedChange={(v) => setDbChecked(db.name, v === true)}
                    />
                    <label htmlFor={`db-${db.name}`} className="min-w-0 flex-1 cursor-pointer">
                      <div className="truncate font-medium">{db.name}</div>
                      <div className="flex flex-wrap gap-x-3 text-xs text-muted-foreground">
                        <span>{db.table_count} tables</span>
                        {db.view_count > 0 && <span>{db.view_count} views</span>}
                        <span>{formatBytes(db.size_bytes)}</span>
                        <span>{db.charset}</span>
                      </div>
                    </label>
                    <div className="flex items-center gap-2">
                      <ArrowRightIcon className="size-4 text-muted-foreground" />
                      <Input
                        aria-label={`Target schema for ${db.name}`}
                        className="h-8 w-40"
                        value={sel.targetSchema}
                        onChange={(e) => update(db.name, { targetSchema: e.target.value })}
                      />
                    </div>
                  </div>
                  <CollapsibleContent>
                    <div className="border-t bg-muted/30 px-3 py-2">
                      {sel.loading || !sel.tables ? (
                        <div className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
                          <Loader2Icon className="size-4 animate-spin" /> Loading tables…
                        </div>
                      ) : sel.tables.length === 0 ? (
                        <p className="py-4 text-sm text-muted-foreground">This database is empty.</p>
                      ) : (
                        <Table>
                          <TableHeader>
                            <TableRow>
                              <TableHead className="w-8" />
                              <TableHead>Table</TableHead>
                              <TableHead>Engine</TableHead>
                              <TableHead className="text-right">Rows (est.)</TableHead>
                              <TableHead className="text-right">Size</TableHead>
                            </TableRow>
                          </TableHeader>
                          <TableBody>
                            {sel.tables.map((t) => (
                              <TableRow key={t.name}>
                                <TableCell>
                                  <Checkbox
                                    aria-label={`Include ${t.name}`}
                                    checked={sel.selected.has(t.name)}
                                    onCheckedChange={(v) => setTableChecked(db.name, t.name, v === true)}
                                  />
                                </TableCell>
                                <TableCell className="font-mono text-xs">
                                  {t.name}
                                  {t.type === "view" && <Badge variant="outline" className="ml-2">view</Badge>}
                                </TableCell>
                                <TableCell className="text-muted-foreground">{t.engine ?? "—"}</TableCell>
                                <TableCell className="text-right tabular-nums">
                                  {t.type === "view" ? "—" : formatNumber(t.row_estimate)}
                                </TableCell>
                                <TableCell className="text-right tabular-nums">
                                  {t.type === "view" ? "—" : formatBytes(t.size_bytes)}
                                </TableCell>
                              </TableRow>
                            ))}
                          </TableBody>
                        </Table>
                      )}
                    </div>
                  </CollapsibleContent>
                </Collapsible>
              )
            })}
          </div>
        )}
      </CardContent>
    </Card>
  )
}
