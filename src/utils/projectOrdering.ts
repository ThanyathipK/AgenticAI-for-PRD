import type { ProjectSummary } from '../api/types';

/** Apply a pin state while keeping the sidebar grouping deterministic. */
export function applyProjectPinOrder(
  projects: ProjectSummary[],
  projectId: string,
  isPinned: boolean,
): ProjectSummary[] {
  const target = projects.find((project) => project.id === projectId);
  if (!target) return projects;

  const updatedTarget = { ...target, is_pinned: isPinned };
  const others = projects.filter((project) => project.id !== projectId);

  if (isPinned) return [updatedTarget, ...others];

  const pinned = others.filter((project) => Boolean(project.is_pinned));
  const recent = others.filter((project) => !project.is_pinned);
  return [...pinned, updatedTarget, ...recent];
}
