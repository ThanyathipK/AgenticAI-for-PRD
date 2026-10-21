// Relative-time formatting helpers for the dashboard "Updated" column.
//
// Pure string helpers (no state, no hooks) matching the src/utils convention:
// they render an ISO-8601 timestamp the way the dashboard table wants it —
// a coarse humanized age ("12 min ago", "2 hr ago") for edits under 24 hours
// old and a plain calendar date (YYYY-MM-DD) thereafter.

/** Coarse dashboard "Updated" cell: humanized age for fresh edits, calendar date otherwise. */
export function formatRelativeUpdated(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return '-';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '-';

  const diffMs = now.getTime() - date.getTime();
  if (diffMs < 0) return formatDate(iso);

  const minutes = Math.floor(diffMs / 60_000);
  if (minutes < 1) return 'Just now';
  if (minutes < 60) return `${minutes} min ago`;

  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} hr ago`;

  return formatDate(iso);
}

/** Calendar-date fallback: YYYY-MM-DD in local time (empty string when missing/invalid). */
export function formatDate(iso: string | null | undefined): string {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}
