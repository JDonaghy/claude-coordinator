import * as React from 'react'
import { cva, type VariantProps } from 'class-variance-authority'
import { cn } from '@/lib/utils'

// eslint-disable-next-line react-refresh/only-export-components -- cva() returns a function, not the primitive constant `allowConstantExport` permits, so this still needs the escape hatch even though it never re-renders.
export const badgeVariants = cva(
  'inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-xs font-semibold transition-colors focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2',
  {
    variants: {
      variant: {
        default: 'border-transparent bg-primary text-primary-foreground',
        secondary: 'border-transparent bg-secondary text-secondary-foreground',
        outline: 'border-line-strong text-foreground',
        // The mocks never fill a badge solid with a status colour and put
        // white/black text on top -- at these desaturated, mid-lightness
        // state colours that fails contrast in one theme or the other (see
        // src/index.css). Instead they use a translucent "wash" of the
        // colour as the background and the solid colour as the text, same
        // as `.tag.ok` / `.tag.warn` / `.tag.bad` and the stage-verdict
        // text in docs/mocks/web/*.html -- and it happens to read AA-clean
        // in both themes for free.
        success: 'border-transparent bg-pass-wash text-pass',
        warning: 'border-transparent bg-attn-wash text-attn',
        destructive: 'border-transparent bg-fail-wash text-fail',
      },
    },
    defaultVariants: {
      variant: 'default',
    },
  },
)

export interface BadgeProps
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps) {
  return <div className={cn(badgeVariants({ variant }), className)} {...props} />
}

export { Badge }
