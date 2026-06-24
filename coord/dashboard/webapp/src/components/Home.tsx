/**
 * Home — placeholder screen that proves the /api/board data path.
 *
 * This is the scaffold view; full Pipeline UI is in subsequent issues
 * (Home / Detail screens of the Phone Control Center milestone).
 */
import { useQuery } from '@tanstack/react-query'
import { fetchBoard, type Assignment } from '@/api/client'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'

function statusVariant(
  status: string,
): 'default' | 'secondary' | 'destructive' | 'success' | 'warning' | 'outline' {
  switch (status) {
    case 'running':
      return 'default'
    case 'done':
      return 'success'
    case 'failed':
      return 'destructive'
    case 'cancelled':
      return 'warning'
    case 'advisory':
      return 'warning'
    default:
      return 'secondary'
  }
}

function AssignmentRow({ a }: { a: Assignment }) {
  return (
    <Card>
      <CardContent className="pt-4">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0 flex-1">
            <p className="truncate text-sm font-medium">
              #{a.issue_number} {a.issue_title}
            </p>
            <p className="mt-0.5 text-xs text-muted-foreground">
              {a.machine_name} · {a.repo_name}
            </p>
          </div>
          <Badge variant={statusVariant(a.status)} className="shrink-0">
            {a.status}
          </Badge>
        </div>
      </CardContent>
    </Card>
  )
}

export default function Home() {
  const { data, isLoading, isError, dataUpdatedAt } = useQuery({
    queryKey: ['board'],
    queryFn: fetchBoard,
    refetchInterval: 4_000,
  })

  const updatedLabel = dataUpdatedAt
    ? new Date(dataUpdatedAt).toLocaleTimeString()
    : null

  return (
    <div className="mx-auto max-w-lg px-4 py-6">
      {/* Header */}
      <header className="mb-6 flex items-baseline justify-between">
        <div>
          <h1 className="text-xl font-bold text-primary">coord</h1>
          <p className="text-xs text-muted-foreground">dashboard</p>
        </div>
        {updatedLabel && (
          <span className="text-xs text-muted-foreground">updated {updatedLabel}</span>
        )}
      </header>

      {/* Loading / error states */}
      {isLoading && (
        <p className="py-8 text-center text-sm text-muted-foreground">Loading board…</p>
      )}
      {isError && (
        <p className="py-8 text-center text-sm text-destructive">
          Failed to load board — is the dashboard server running?
        </p>
      )}

      {/* Board summary */}
      {data && (
        <div className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle>Board</CardTitle>
            </CardHeader>
            <CardContent>
              <dl className="space-y-2 text-sm">
                <div className="flex justify-between">
                  <dt className="text-muted-foreground">Active</dt>
                  <dd>
                    <Badge variant="secondary">{data.active.length}</Badge>
                  </dd>
                </div>
                <div className="flex justify-between">
                  <dt className="text-muted-foreground">Completed (last 20)</dt>
                  <dd>
                    <Badge variant="outline">{data.completed.length}</Badge>
                  </dd>
                </div>
                <div className="flex justify-between">
                  <dt className="text-muted-foreground">Round</dt>
                  <dd className="font-mono">#{data.round_number}</dd>
                </div>
              </dl>
            </CardContent>
          </Card>

          {/* Active assignments */}
          {data.active.length > 0 && (
            <section className="space-y-2">
              <h2 className="text-xs font-medium uppercase tracking-widest text-muted-foreground">
                Active
              </h2>
              {data.active.map((a) => (
                <AssignmentRow key={a.assignment_id} a={a} />
              ))}
            </section>
          )}

          {/* Empty state */}
          {data.active.length === 0 && (
            <p className="py-4 text-center text-sm text-muted-foreground">
              No active assignments
            </p>
          )}
        </div>
      )}
    </div>
  )
}
