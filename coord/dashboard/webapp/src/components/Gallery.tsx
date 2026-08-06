/**
 * Component gallery (#1546) — dev-only route (`/gallery`, guarded by
 * `import.meta.env.DEV` in App.tsx) rendering every ui/* primitive. This is
 * the fastest way for a human, or a Playwright acceptance slice, to see the
 * whole design-token + primitive system at once instead of hunting through
 * the app for one usage of each.
 *
 * Two proof mechanisms for "renders correctly in both themes":
 *  - The live `<ThemeToggle>` at the top drives the real `data-theme` on
 *    <html>, exactly like the shipped app. Flipping it re-themes *every*
 *    primitive on this page, including the ones below that portal to
 *    `document.body` (Dialog/Sheet/DropdownMenu/Toast) — portals only
 *    change where a node sits in the React tree, not the DOM, so they stay
 *    inside <html> and inherit its `data-theme` cascade regardless.
 *  - The "side by side" section at the bottom forces dark and light in two
 *    adjacent panes at once via a *non-portalled* subtree, for the
 *    non-overlay primitives (Button/Badge/Card/Skeleton/EmptyState) where a
 *    true simultaneous comparison is cheap. Overlay primitives aren't
 *    duplicated there — they'd portal out of the forced-theme pane and
 *    misleadingly render in whatever theme is live instead.
 */
import { useState, type ReactNode } from 'react'
import { AlertTriangle, Inbox, MoreHorizontal, Play, Terminal as TerminalIcon } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from '@/components/ui/card'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuShortcut,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from '@/components/ui/sheet'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/ui/empty-state'
import { Toaster } from '@/components/ui/toaster'
import { toast } from '@/components/ui/use-toast'
import { ThemeProvider, useTheme } from '@/components/ui/theme-provider'
import { ThemeToggle } from '@/components/ui/theme-toggle'

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-3">
      <h2 className="text-step-1 font-semibold tracking-tight">{title}</h2>
      <div className="flex flex-wrap items-center gap-3">{children}</div>
    </section>
  )
}

function GalleryBody() {
  const { theme } = useTheme()
  const [tab, setTab] = useState('overview')

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-10 p-6 sm:p-10">
      <header className="flex items-start justify-between gap-4 border-b border-border pb-6">
        <div>
          <h1 className="text-step-3 font-semibold tracking-tight">Component gallery</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            coord web's design tokens + shadcn/ui primitive inventory (#1546). Dev-only —
            route-guarded out of reach in production (see App.tsx).
          </p>
        </div>
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <span className="mono">{theme}</span>
          <ThemeToggle />
        </div>
      </header>

      <Section title="Button">
        <Button>Primary</Button>
        <Button variant="outline">Outline</Button>
        <Button variant="secondary">Secondary</Button>
        <Button variant="ghost">Ghost</Button>
        <Button variant="destructive">Destructive</Button>
        <Button variant="link">Link</Button>
        <Button size="sm">Small</Button>
        <Button size="lg">Large</Button>
        <Button size="icon" aria-label="Play">
          <Play />
        </Button>
        <Button disabled>Disabled</Button>
      </Section>

      <Section title="Badge">
        <Badge>Default</Badge>
        <Badge variant="secondary">Secondary</Badge>
        <Badge variant="outline">Outline</Badge>
        <Badge variant="success">Passed</Badge>
        <Badge variant="warning">3 findings</Badge>
        <Badge variant="destructive">Failed</Badge>
      </Section>

      <Section title="Card">
        <Card className="w-full max-w-sm">
          <CardHeader>
            <CardTitle>W1-1: design tokens</CardTitle>
            <CardDescription>#1546 · claude-coordinator</CardDescription>
          </CardHeader>
          <CardContent className="text-sm text-muted-foreground">
            Dark/light theme, component primitive baseline.
          </CardContent>
          <CardFooter className="gap-2">
            <Badge variant="success">Test · passed</Badge>
            <span className="mono ml-auto text-xs text-faint">18m</span>
          </CardFooter>
        </Card>
      </Section>

      <Section title="Tabs">
        <Tabs value={tab} onValueChange={setTab} className="w-full">
          <TabsList>
            <TabsTrigger value="overview">Overview</TabsTrigger>
            <TabsTrigger value="findings">
              Findings <span className="ml-1 rounded-full bg-secondary px-1.5 text-faint">3</span>
            </TabsTrigger>
            <TabsTrigger value="log">Log</TabsTrigger>
          </TabsList>
          <TabsContent value="overview" className="text-sm text-muted-foreground">
            Overview panel content.
          </TabsContent>
          <TabsContent value="findings" className="text-sm text-muted-foreground">
            Findings panel content.
          </TabsContent>
          <TabsContent value="log" className="text-sm text-muted-foreground">
            Log panel content.
          </TabsContent>
        </Tabs>
      </Section>

      <Section title="Dialog">
        <Dialog>
          <DialogTrigger asChild>
            <Button variant="outline">Open dialog</Button>
          </DialogTrigger>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Drop to backlog?</DialogTitle>
              <DialogDescription>
                The session will stop and the branch stays as-is. You can resume from the
                backlog later.
              </DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button variant="outline">Cancel</Button>
              <Button variant="destructive">Drop to backlog</Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </Section>

      <Section title="Dropdown menu">
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="outline" size="icon" aria-label="More actions">
              <MoreHorizontal />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuLabel>Start — interactive</DropdownMenuLabel>
            <DropdownMenuItem>
              <Play /> Work
              <DropdownMenuShortcut>W</DropdownMenuShortcut>
            </DropdownMenuItem>
            <DropdownMenuItem>
              <TerminalIcon /> Testing
              <DropdownMenuShortcut>T</DropdownMenuShortcut>
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem variant="hot">
              <AlertTriangle /> Address review findings
            </DropdownMenuItem>
            <DropdownMenuItem variant="danger">Stop session</DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </Section>

      <Section title="Sheet (mobile action sheet)">
        <Sheet>
          <SheetTrigger asChild>
            <Button variant="outline">Open action sheet</Button>
          </SheetTrigger>
          <SheetContent side="bottom">
            <SheetHeader>
              <SheetTitle>#1289 · Tab-group focus ring</SheetTitle>
              <SheetDescription>Choose an action</SheetDescription>
            </SheetHeader>
            <div className="flex flex-col gap-1 py-2">
              <Button variant="ghost" className="justify-start">
                Read diff
              </Button>
              <Button variant="ghost" className="justify-start text-attn">
                Address findings
              </Button>
            </div>
          </SheetContent>
        </Sheet>
      </Section>

      <Section title="Tooltip">
        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button variant="outline" size="icon" aria-label="Help">
                ?
              </Button>
            </TooltipTrigger>
            <TooltipContent>Stage strip shows Work → Test → Review → Merge</TooltipContent>
          </Tooltip>
        </TooltipProvider>
      </Section>

      <Section title="Skeleton">
        <div className="flex flex-col gap-2">
          <Skeleton className="h-4 w-48" />
          <Skeleton className="h-4 w-32" />
        </div>
      </Section>

      <Section title="Empty state">
        <Card className="w-full">
          <EmptyState
            icon={<Inbox />}
            title="No issues match this filter"
            description="Try a different repo or clear the search."
            action={
              <Button variant="outline" size="sm">
                Clear filters
              </Button>
            }
          />
        </Card>
      </Section>

      <Section title="Toast">
        <Button
          variant="outline"
          onClick={() =>
            toast({ title: 'Drive started', description: 'dellserver · issue-1538' })
          }
        >
          Default
        </Button>
        <Button
          variant="outline"
          onClick={() =>
            toast({
              variant: 'success',
              title: 'Test passed',
              description: '612 tests · 3m 18s',
            })
          }
        >
          Success
        </Button>
        <Button
          variant="outline"
          onClick={() =>
            toast({
              variant: 'destructive',
              title: 'Merge blocked',
              description: 'CI is red on develop',
            })
          }
        >
          Destructive
        </Button>
        <Toaster />
      </Section>

      <section className="flex flex-col gap-3 border-t border-border pt-8">
        <h2 className="text-step-1 font-semibold tracking-tight">Side by side</h2>
        <p className="text-sm text-muted-foreground">
          Both themes at once, forced independent of the live toggle above (non-overlay
          primitives only — see the file header comment for why).
        </p>
        <div className="grid gap-4 sm:grid-cols-2">
          {(['dark', 'light'] as const).map(t => (
            <div
              key={t}
              data-theme={t}
              className="flex flex-col gap-4 rounded-lg border border-border bg-background p-5 text-foreground"
            >
              <span className="mono text-xs uppercase tracking-wide text-faint">{t}</span>
              <div className="flex flex-wrap gap-2">
                <Button size="sm">Primary</Button>
                <Button size="sm" variant="outline">
                  Outline
                </Button>
                <Badge variant="success">Passed</Badge>
                <Badge variant="warning">Findings</Badge>
                <Badge variant="destructive">Failed</Badge>
              </div>
              <Card>
                <CardHeader>
                  <CardTitle>#1538</CardTitle>
                  <CardDescription>coord web --fixture</CardDescription>
                </CardHeader>
              </Card>
            </div>
          ))}
        </div>
      </section>
    </div>
  )
}

/**
 * Wrapped in its own ThemeProvider so the gallery is a self-contained route
 * (works even if it's ever linked to directly) — App.tsx already wraps the
 * whole app in one too, and nesting is harmless since both read/write the
 * same `data-theme` attribute and localStorage key.
 */
export default function Gallery() {
  return (
    <ThemeProvider>
      <GalleryBody />
    </ThemeProvider>
  )
}
