import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"
import type { MigrationOptions } from "@/lib/api"

type BoolKey = { [K in keyof MigrationOptions]: MigrationOptions[K] extends boolean ? K : never }[keyof MigrationOptions]

const TOGGLES: { key: BoolKey; label: string; hint: string; danger?: boolean }[] = [
  { key: "include_data", label: "Copy data", hint: "Stream rows with COPY. Off = schema only." },
  { key: "create_indexes", label: "Indexes", hint: "Unique, secondary, prefix and FULLTEXT (as GIN)." },
  { key: "create_foreign_keys", label: "Foreign keys", hint: "Added after all data has been loaded." },
  { key: "create_views", label: "Views", hint: "Best-effort SQL rewrite; failures are reported." },
  { key: "tinyint1_as_boolean", label: "TINYINT(1) → boolean", hint: "Otherwise mapped to smallint." },
  { key: "lowercase_identifiers", label: "Lowercase names", hint: "Avoids quoting mixed-case names in Postgres." },
  { key: "verify_row_counts", label: "Verify row counts", hint: "COUNT(*) on both sides after copying." },
  { key: "drop_existing", label: "Drop existing schemas", hint: "DROP SCHEMA … CASCADE before creating.", danger: true },
]

export function OptionsPanel({ value, onChange }: { value: MigrationOptions; onChange: (v: MigrationOptions) => void }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Options</CardTitle>
        <CardDescription>How the schema and data are translated.</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {TOGGLES.map((t) => (
          <div key={t.key} className="flex items-start justify-between gap-4">
            <div className="grid gap-0.5">
              <Label htmlFor={`opt-${t.key}`} className={t.danger ? "text-destructive" : undefined}>
                {t.label}
              </Label>
              <p className="text-xs text-muted-foreground">{t.hint}</p>
            </div>
            <Switch
              id={`opt-${t.key}`}
              checked={value[t.key]}
              onCheckedChange={(checked) => onChange({ ...value, [t.key]: checked })}
            />
          </div>
        ))}
        <div className="grid gap-2">
          <Label htmlFor="opt-batch">Batch size</Label>
          <Input
            id="opt-batch"
            type="number"
            min={100}
            max={100000}
            step={500}
            value={value.batch_size}
            onChange={(e) => onChange({ ...value, batch_size: Number(e.target.value) || 5000 })}
          />
          <p className="text-xs text-muted-foreground">Rows fetched from MySQL per round trip (100–100,000).</p>
        </div>
      </CardContent>
    </Card>
  )
}
