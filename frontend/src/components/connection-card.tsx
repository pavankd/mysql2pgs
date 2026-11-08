import { useState } from "react"
import { CheckCircle2Icon, DatabaseIcon, Loader2Icon, PlugZapIcon, XCircleIcon } from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardAction, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import type { TestResult } from "@/lib/api"

export type ConnState =
  | { kind: "idle" }
  | { kind: "testing" }
  | { kind: "ok"; result: TestResult }
  | { kind: "error"; message: string }

interface Field<T> {
  key: keyof T & string
  label: string
  type?: "text" | "number" | "password"
  className?: string
}

interface Props<T extends object> {
  title: string
  description: string
  accent: string
  value: T
  fields: Field<T>[]
  state: ConnState
  onChange: (value: T) => void
  onTest: () => void
}

export function ConnectionCard<T extends object>({
  title,
  description,
  accent,
  value,
  fields,
  state,
  onChange,
  onTest,
}: Props<T>) {
  const [touched, setTouched] = useState(false)
  const record = value as Record<string, string | number>

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <span className={`flex size-8 items-center justify-center rounded-lg ${accent}`}>
            <DatabaseIcon className="size-4" />
          </span>
          {title}
        </CardTitle>
        <CardDescription>{description}</CardDescription>
        <CardAction>
          <StatusBadge state={state} />
        </CardAction>
      </CardHeader>
      <CardContent className="flex-1">
        <form
          className="grid grid-cols-6 gap-4"
          onSubmit={(e) => {
            e.preventDefault()
            onTest()
          }}
        >
          {fields.map((f) => {
            const id = `${title}-${f.key}`.replace(/\W+/g, "-").toLowerCase()
            return (
              <div key={f.key} className={`grid gap-2 ${f.className ?? "col-span-6 sm:col-span-3"}`}>
                <Label htmlFor={id}>{f.label}</Label>
                <Input
                  id={id}
                  type={f.type ?? "text"}
                  value={record[f.key] ?? ""}
                  autoComplete={f.type === "password" ? "new-password" : "off"}
                  onChange={(e) => {
                    setTouched(true)
                    const raw = e.target.value
                    onChange({ ...value, [f.key]: f.type === "number" ? Number(raw) || 0 : raw })
                  }}
                />
              </div>
            )
          })}
          <button type="submit" hidden />
        </form>
      </CardContent>
      <CardFooter className="flex flex-col items-stretch gap-3">
        <Button variant={state.kind === "ok" && !touched ? "outline" : "default"} onClick={() => { setTouched(false); onTest() }} disabled={state.kind === "testing"}>
          {state.kind === "testing" ? <Loader2Icon className="animate-spin" /> : <PlugZapIcon />}
          Test connection
        </Button>
        {state.kind === "error" && (
          <p className="text-sm text-destructive break-words">{state.message}</p>
        )}
        {state.kind === "ok" && state.result.note && (
          <p className="text-sm text-amber-600 dark:text-amber-400">{state.result.note}</p>
        )}
      </CardFooter>
    </Card>
  )
}

function StatusBadge({ state }: { state: ConnState }) {
  switch (state.kind) {
    case "ok":
      return (
        <Badge variant="outline" className="border-emerald-500/40 text-emerald-700 dark:text-emerald-400">
          <CheckCircle2Icon /> v{state.result.version}
        </Badge>
      )
    case "error":
      return (
        <Badge variant="destructive">
          <XCircleIcon /> Failed
        </Badge>
      )
    case "testing":
      return (
        <Badge variant="secondary">
          <Loader2Icon className="animate-spin" /> Testing
        </Badge>
      )
    default:
      return <Badge variant="secondary">Not tested</Badge>
  }
}
