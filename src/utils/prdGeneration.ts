import type { PRDSection, PrdSectionLockState } from '../components/types';

/** Only human edits in unlocked, AI-managed parts can be replaced. */
export function hasManualPrdEditsAtRisk(
  sectionLocks: Record<string, PrdSectionLockState>,
  sections: PRDSection[],
  editingSectionId: string | null,
  editBuffer: string,
): boolean {
  if (editingSectionId !== null) {
    const saved = sections.find(item => item.id === editingSectionId);
    const metadata = sectionLocks[editingSectionId];
    const canBeReplaced = metadata === undefined
      || (!metadata.is_locked && metadata.ai_generatable);
    if (saved !== undefined && editBuffer !== saved.content && canBeReplaced) return true;
  }

  return Object.values(sectionLocks).some((section) => {
    if (section.is_locked || !section.ai_generatable) return false;
    return section.content_source === 'human';
  });
}
