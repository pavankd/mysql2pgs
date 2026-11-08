import { useMemo, useState } from "react"
import { ChevronRightIcon, DatabaseIcon, EyeIcon, Loader2Icon, RefreshCwIcon, SearchIcon, TableIcon } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Card, CardAction, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import type { SchemaNode } from "@/lib/api"
import { cn } from "@/lib/utils"

interface Props {
  schemas: SchemaNode[]
  loading: boolean
  connected: boolean
  defaultSchema: string
  onSetDefault: (schema: string) => void
  onSelectRows: (schema: string, table: string) => void
  onRefresh: () => void
}

/** Schema/table tree, like MySQL Workbench's Navigator. */
export function Navigator({ schemas, loading, connected, defaultSchema, onSetDefault, onSelectRows, onRefresh }: Props) {
  const [filter, setFilter] = useState("")
  const [open, setOpen] = useState<Set<string>>(new Set())

  const visible = useMemo(() => {
    const f = filter.trim().toLowerCase()
    if (!f) return schemas
    return schemas
      .map((s) =>
        s.name.toLowerCase().includes(f) ? s : { ...s, tables: s.tables.filter((t) => t.name.toLowerCase().includes(f)) },
      )
      .filter((s) => s.name.toLowerCase().includes(f) || s.tables.length > 0)
  }, [schemas, filter])

  const toggle = (name: string) =>
    setOpen((prev) => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })

  return (
    <Card className="gap-3 lg:sticky lg:top-4">
      <CardHeader>
        <CardTitle className="text-sm">Navigator</CardTitle>
        <CardAction>
          <Button variant="ghost" size="icon-sm" aria-label="Refresh schemas" onClick={onRefresh} disabled={!connected || loading}>
            <RefreshCwIcon className={loading ? "animate-spin" : ""} />
          </Button>
        </CardAction>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        <div className="relative">
          <SearchIcon className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input
            placeholder="Filter objects"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            className="h-8 pl-8 text-xs"
            disabled={!connected}
          />
        </div>
        <div className="max-h-[60vh] min-h-40 overflow-y-auto text-sm">
          {!connected ? (
            <p className="px-1 py-6 text-center text-xs text-muted-foreground">Connect to a server to browse its schemas.</p>
          ) : loading && schemas.length === 0 ? (
            <p className="flex items-center justify-center gap-2 py-6 text-xs text-muted-foreground">
              <Loader2Icon className="size-3.5 animate-spin" /> Loading schemas…
            </p>
          ) : visible.length === 0 ? (
            <p className="px-1 py-6 text-center text-xs text-muted-foreground">No schemas found.</p>
          ) : (
            <ul>
              {visible.map((s) => {
                const expanded = open.has(s.name) || filter.trim() !== ""
                const isDefault = s.name === defaultSchema
                return (
                  <li key={s.name}>
                    <div className="group flex items-center gap-1 rounded-md pr-1 hover:bg-muted/60">
                      <button
                        type="button"
                        className="flex size-6 shrink-0 items-center justify-center text-muted-foreground"
                        aria-label={expanded ? `Collapse ${s.name}` : `Expand ${s.name}`}
                        onClick={() => toggle(s.name)}
                      >
                        <ChevronRightIcon className={cn("size-3.5 transition-transform", expanded && "rotate-90")} />
                      </button>
                      <button
                        type="button"
                        className={cn("flex min-w-0 flex-1 items-center gap-1.5 py-1 text-left", isDefault && "font-semibold")}
                        title="Double-click to set as default schema"
                        onClick={() => toggle(s.name)}
                        onDoubleClick={() => onSetDefault(s.name)}
                      >
                        <DatabaseIcon className={cn("size-3.5 shrink-0", isDefault ? "text-primary" : "text-muted-foreground")} />
                        <span className="truncate">{s.name}</span>
                      </button>
                      {!isDefault && (
                        <button
                          type="button"
                          className="hidden shrink-0 rounded px-1.5 text-[11px] text-muted-foreground group-hover:block hover:bg-background hover:text-foreground"
                          onClick={() => onSetDefault(s.name)}
                        >
                          use
                        </button>
                      )}
                    </div>
                    {expanded && (
                      <ul className="ml-6 border-l pl-1">
                        {s.tables.length === 0 && <li className="px-2 py-1 text-xs text-muted-foreground">No tables</li>}
                        {s.tables.map((t) => (
                          <li key={t.name}>
                            <button
                              type="button"
                              className="flex w-full items-center gap-1.5 rounded-md px-1.5 py-1 text-left text-xs hover:bg-muted/60"
                              title={`Select rows from ${s.name}.${t.name}`}
                              onClick={() => onSelectRows(s.name, t.name)}
                            >
                              {t.type === "view" ? (
                                <EyeIcon className="size-3.5 shrink-0 text-muted-foreground" />
                              ) : (
                                <TableIcon className="size-3.5 shrink-0 text-muted-foreground" />
                              )}
                              <span className="truncate font-mono">{t.name}</span>
                            </button>
                          </li>
                        ))}
                        {s.truncated && <li className="px-2 py-1 text-xs text-muted-foreground">List truncated</li>}
                      </ul>
                    )}
                  </li>
                )
              })}
            </ul>
          )}
        </div>
        {connected && (
          <p className="text-[11px] leading-snug text-muted-foreground">
            Click a table to select its rows. Double-click a schema (or <span className="font-medium">use</span>) to make
            it the default.
          </p>
        )}
      </CardContent>
    </Card>
  )
}
