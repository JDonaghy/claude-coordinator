import type { HTMLAttributes } from 'react'
import { cn } from '@/lib/utils'

/** Loading placeholder — a pulsing block the size of the content it stands in for. */
function Skeleton({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('animate-pulse rounded-md bg-surface-2', className)} {...props} />
}

export { Skeleton }
