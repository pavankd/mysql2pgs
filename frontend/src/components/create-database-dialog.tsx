import { DatabasePlusIcon, Loader2Icon } from "lucide-react"

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogMedia,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"

interface Props {
  open: boolean
  busy: boolean
  database: string
  server: string
  user: string
  onOpenChange: (open: boolean) => void
  onConfirm: () => void
}

/** Asks before creating a missing PostgreSQL target database. */
export function CreateDatabaseDialog({ open, busy, database, server, user, onOpenChange, onConfirm }: Props) {
  return (
    <AlertDialog open={open} onOpenChange={(o) => !busy && onOpenChange(o)}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogMedia className="bg-amber-500/15 text-amber-700 dark:text-amber-300">
            <DatabasePlusIcon />
          </AlertDialogMedia>
          <AlertDialogTitle>Create database “{database}”?</AlertDialogTitle>
          <AlertDialogDescription>
            The PostgreSQL server at <span className="font-mono">{server}</span> is reachable, but this database does
            not exist. It will be created with the server's default settings and owned by{" "}
            <span className="font-mono">{user}</span>.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={busy}>Not now</AlertDialogCancel>
          <AlertDialogAction
            disabled={busy}
            onClick={(e) => {
              // Keep the dialog open until the request finishes.
              e.preventDefault()
              onConfirm()
            }}
          >
            {busy ? <Loader2Icon className="animate-spin" /> : <DatabasePlusIcon />}
            Create database
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
