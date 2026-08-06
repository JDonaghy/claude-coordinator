import type { ReactNode } from 'react'
import { cn } from '@/lib/utils'

export interface EmptyStateProps {
  icon?: ReactNode
  title: string
  description?: string
  action?: ReactNode
  className?: string
}

/**
 * A blank/zero-result state — not in a mock verbatim (none of the three
 * Gate-A screens hit one), but built from the same vocabulary: faint icon,
 * ui-sans title, text-dim description, generous padding, no card chrome of
 * its own since it usually sits inside one (e.g. an empty `.list` or
 * `.card-body`).
 */
export function EmptyState({ icon, title, description, action, className }: EmptyStateProps) {
  return (
    <div
      className={cn(
        'flex flex-col items-center gap-3 px-6 py-12 text-center',
        className,
      )}
    >
      {icon && <div className="text-faint [&_svg]:h-8 [&_svg]:w-8">{icon}</div>}
      <div className="flex flex-col gap-1">
        <p className="text-sm font-medium text-foreground">{title}</p>
        {description && <p className="text-sm text-muted-foreground">{description}</p>}
      </div>
      {action && <div className="mt-1">{action}</div>}
    </div>
  )
}
