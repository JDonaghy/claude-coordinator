/**
 * The activity rail's contents (#1547).
 *
 * "Panels not yet built are visibly *coming*, not silently absent — the rail
 * is the program's own progress bar." So this list is the whole planned M-W1
 * surface, and each entry carries its own truth about whether it exists yet.
 * A `'soon'` entry renders dimmed with a `soon` pill and is not activatable
 * (the Gate-A mock, docs/mocks/web/pipeline-wide.html, gives them
 * `opacity:.42` and `cursor:default`); flipping one to `'ready'` when its
 * story lands is a one-line change here plus a case in `ShellLayout`.
 *
 * Grouping follows the mock exactly: an ungrouped top block, then Flow, then
 * Insight, with Theme / Settings / Collapse pinned to the rail foot.
 */
import {
  BarChart3,
  GitMerge,
  LayoutDashboard,
  Milestone,
  ScrollText,
  Server,
  SquareTerminal,
  Terminal,
  Workflow,
  type LucideIcon,
} from 'lucide-react'

import type { ShellView } from './shellState'

export type RailItemStatus = 'ready' | 'soon'

export interface RailItem {
  id: ShellView
  label: string
  icon: LucideIcon
  status: RailItemStatus
  /** Group heading this item sits under; `undefined` = the top block. */
  group?: string
  /** Shown on hover / in the `soon` pill's tooltip. */
  hint?: string
}

export const RAIL_ITEMS: readonly RailItem[] = [
  { id: 'pipeline', label: 'Pipeline', icon: Workflow, status: 'ready' },
  { id: 'board', label: 'Board', icon: LayoutDashboard, status: 'soon', hint: 'Board panel — M-W2' },
  { id: 'sessions', label: 'Sessions', icon: SquareTerminal, status: 'ready' },
  {
    id: 'terminal',
    label: 'Terminal',
    icon: Terminal,
    status: 'soon',
    hint: 'Standalone terminal panel — open one from Sessions for now',
  },
  { id: 'machines', label: 'Machines', icon: Server, status: 'soon', hint: 'Machines panel — M-W2' },

  {
    id: 'merge-queue',
    label: 'Merge queue',
    icon: GitMerge,
    status: 'soon',
    group: 'Flow',
    hint: 'Merge queue panel — M-W2',
  },
  {
    id: 'milestones',
    label: 'Milestones',
    icon: Milestone,
    status: 'soon',
    group: 'Flow',
    hint: 'Milestone DAG — M-W3',
  },

  {
    id: 'audit',
    label: 'Audit',
    icon: ScrollText,
    status: 'soon',
    group: 'Insight',
    hint: 'Audit trail — milestone #33',
  },
  {
    id: 'spend',
    label: 'Spend',
    icon: BarChart3,
    status: 'soon',
    group: 'Insight',
    hint: 'Spend & time observability — milestone #37',
  },
] as const

/**
 * Rail items in render order, split into their heading groups. Computed once
 * at module scope: `RAIL_ITEMS` is a constant, so recomputing per render would
 * be pure waste and would break `React.memo` on any consumer.
 */
export const RAIL_GROUPS: ReadonlyArray<{ heading?: string; items: RailItem[] }> =
  RAIL_ITEMS.reduce<Array<{ heading?: string; items: RailItem[] }>>((groups, item) => {
    const last = groups[groups.length - 1]
    if (last && last.heading === item.group) {
      last.items.push(item)
    } else {
      groups.push({ heading: item.group, items: [item] })
    }
    return groups
  }, [])
