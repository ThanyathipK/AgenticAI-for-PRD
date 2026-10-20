// Tooltip — instant, CSS-only hover label for icon-only buttons.
// Native `title` tooltips are delayed ~1s and often suppressed by browsers,
// so icon buttons use this instead: the label renders immediately on hover
// or keyboard focus with a short fade + scale transition. Pure presentation:
// no state, no portal, no event handlers.
import type { ReactNode } from 'react';

export type TooltipSide = 'top' | 'bottom' | 'left' | 'right';
export type TooltipAlign = 'center' | 'start';

// Absolute placement of the bubble relative to its positioning parent, per side.
const SIDE_PLACEMENT: Record<TooltipSide, Record<TooltipAlign, string>> = {
  top: {
    center: 'bottom-full left-1/2 -translate-x-1/2 mb-1.5',
    start: 'bottom-full left-0 mb-1.5',
  },
  bottom: {
    center: 'top-full left-1/2 -translate-x-1/2 mt-1.5',
    start: 'top-full left-0 mt-1.5',
  },
  left: {
    center: 'right-full top-1/2 -translate-y-1/2 mr-1.5',
    start: 'right-full top-0 mr-1.5',
  },
  right: {
    center: 'left-full top-1/2 -translate-y-1/2 ml-1.5',
    start: 'left-full top-0 ml-1.5',
  },
};

// The bubble itself. Requires an ancestor (the trigger or its wrapper) to be
// positioned (`relative`/`fixed`/`absolute`) and carry the `group/tt` class.
export function TooltipBubble({
  label,
  side = 'top',
  align = 'center',
}: {
  label: string;
  side?: TooltipSide;
  align?: TooltipAlign;
}) {
  return (
    <span
      role="tooltip"
      className={`pointer-events-none absolute z-[60] whitespace-nowrap rounded-lg bg-slate-900/95 px-2.5 py-1 text-[11px] font-semibold text-white shadow-lg opacity-0 scale-95 transition-all duration-100 group-hover/tt:opacity-100 group-hover/tt:scale-100 group-focus/tt:opacity-100 group-focus/tt:scale-100 ${SIDE_PLACEMENT[side][align]}`}
    >
      {label}
    </span>
  );
}

interface TooltipProps {
  /** Text revealed on hover / keyboard focus. */
  label: string;
  /** Side of the trigger the bubble appears on (default 'top'). */
  side?: TooltipSide;
  /** Alignment along that side (default 'center'). */
  align?: TooltipAlign;
  /** Extra classes for the positioning wrapper (e.g. 'w-full', 'h-full'). */
  className?: string;
  children: ReactNode;
}

// Wraps a trigger (usually an icon button) in a hover group and renders the
// bubble next to it. For triggers that are themselves absolute/fixed positioned,
// put `group/tt` on the trigger and use <TooltipBubble> inside it instead, so
// this wrapper's `relative` cannot override the trigger's own positioning.
export function Tooltip({ label, side = 'top', align = 'center', className = '', children }: TooltipProps) {
  return (
    <span className={`relative inline-flex group/tt ${className}`}>
      {children}
      <TooltipBubble label={label} side={side} align={align} />
    </span>
  );
}
