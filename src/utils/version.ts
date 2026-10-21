/** Render a PRD version as SemVer. Prefer the ledger value whenever available. */
export function formatPrdSemVer(
  semVersion: string | null | undefined,
  ledgerVersion?: number | null,
): string {
  const normalized = semVersion?.trim().replace(/^v/i, "");
  if (normalized && /^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/.test(normalized)) {
    return normalized;
  }
  return `${ledgerVersion ?? 0}.0.0`;
}

export function formatPrdVersionLabel(
  semVersion: string | null | undefined,
  ledgerVersion?: number | null,
): string {
  return `v${formatPrdSemVer(semVersion, ledgerVersion)}`;
}
