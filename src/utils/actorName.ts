/**
 * Resolve labels for the signed-in human without changing system/agent authors.
 * Older version rows store the generic markers `user` or `you`; keep those
 * rows immutable and translate them only when they are presented in the UI.
 */
export function displayUserName(userName: string | null | undefined): string {
  return userName?.trim() || 'User';
}

export function displayVersionAuthor(
  author: string | null | undefined,
  userName: string | null | undefined,
): string {
  const storedAuthor = author?.trim() || '';
  if (['user', 'you'].includes(storedAuthor.toLowerCase())) {
    return displayUserName(userName);
  }
  return storedAuthor || 'System';
}
