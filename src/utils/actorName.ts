/**
 * Resolve labels for the project author without changing system/agent authors.
 * Older version rows store the generic markers `user` or `you`; keep those
 * rows immutable and translate them only when they are presented in the UI.
 */
export function displayUserName(userName: string | null | undefined): string {
  return userName?.trim() || 'User';
}

export function displayVersionAuthor(
  author: string | null | undefined,
  projectAuthor: string | null | undefined,
  changeType?: string | null,
): string {
  if (changeType?.trim().toLowerCase() === 'ai') return 'AI Assistance';
  const storedAuthor = author?.trim() || '';
  if (['user', 'you'].includes(storedAuthor.toLowerCase())) {
    return projectAuthor?.trim() || 'User';
  }
  return storedAuthor || 'System';
}

/** Read the Author supplied at project creation from Markdown or rendered LaTeX. */
export function extractProjectAuthor(document: string | null | undefined): string {
  const source = document || '';
  const markdown = source.match(/\|\s*\*\*Author:\*\*\s*\|\s*([^|\n]+)\|/i)?.[1];
  const latex = source.match(/\\prdlbl\{Author:\}\}\s*&\s*\\coverfont\{([^}\n]+)\}/i)?.[1];
  return (markdown || latex || '').trim().replace(/\\([&%#$])/g, '$1');
}
