// LOCK 2.0 — Coordinate frontend requirement locking.
// Trigger: lock-panel action, project change, or requirement-list change.
// Input: shared RequirementStore and active project id.
// Calls: LOCK 2.1–2.4 → LOCK 3.1.
// Writes: optimistic lock maps, authoritative target list, and sync status.
// Failure: request failures roll back local flags; list failures retain prior data.
// Note: PRD-section buttons use RequirementStore's separate section path.
//
// It ALSO owns the authoritative, lockable requirement list for the active
// project (`GET /api/project/{id}/requirements`). The generic lock endpoints
// reject any `artifact_id` that is not a UUID, while requirements produced by a
// gather run only carry a synthesized identity (their requirement code), so the
// list is what makes "Lock" actually work instead of silently no-op'ing or
// failing with HTTP 400.
import { useCallback, useEffect, useState } from 'react';
import { api } from '../api/client';
import { toRequirementItem } from '../api/transforms';
import type { RequirementPayload } from '../api/types';
import type {
  ArtifactLockState,
  RequirementItem,
  RequirementLockState,
} from '../components/types';
import { handleError } from '../components/Toast';
import type { RequirementStore } from './useRequirementStore';

export interface UseArtifactLocksResult {
  lockedArtifacts: Record<string, ArtifactLockState>;
  lockedRequirements: Record<string, RequirementLockState>;
  /** Authoritative requirement rows (real UUIDs + lock metadata) for the project. */
  requirementList: RequirementItem[];
  /** Re-read the authoritative requirement list from the backend. */
  refreshRequirementList: () => Promise<void>;
  handleLockArtifact: (artifactType: string, artifactId: string, artifactCode: string) => Promise<boolean>;
  handleUnlockArtifact: (artifactType: string, artifactId: string, artifactCode: string) => Promise<boolean>;
  handleLockRequirement: (requirementCode: string) => Promise<void>;
  handleUnlockRequirement: (requirementCode: string) => Promise<void>;
}

// The lock endpoints require a UUID; a requirement code ("REQ-001") is rejected
// with HTTP 400 by the backend.
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * True when an artifact id can be sent to the lock API.
 *
 * Requirement entries synthesized by a gather run (before the merge is saved)
 * carry their requirement code as `id`, which the backend rejects — the UI uses
 * this to mark such rows as "not saved yet" instead of offering a dead button.
 */
export const isLockableRequirementId = (id?: string | null): boolean =>
  typeof id === 'string' && UUID_RE.test(id);

/** Requirements that should be listed/lockable (archived ones ARE listed, like
 *  the Requirements tab's traceability matrix does; records marked deleted
 *  are excluded). */
function isListableRequirement(req: RequirementPayload): boolean {
  return (req.status || 'active').toLowerCase() !== 'deleted';
}

const isActiveRequirement = (req: RequirementItem): boolean =>
  (req.status || 'active').toLowerCase() !== 'archived';

/**
 * Deterministic display order: ACTIVE requirements first, then archived ones,
 * each group ordered by requirement code. Without this the panel inherited the
 * raw table order, which put a live REQ-001 below four archived rows.
 */
export function sortLockTargets(reqs: RequirementItem[]): RequirementItem[] {
  return [...reqs].sort((a, b) => {
    const activeDelta = Number(!isActiveRequirement(a)) - Number(!isActiveRequirement(b));
    if (activeDelta !== 0) return activeDelta;
    const num = (code: string) => Number(/(\d+)/.exec(code)?.[1] ?? Number.MAX_SAFE_INTEGER);
    const byNum = num(a.requirement_code) - num(b.requirement_code);
    return byNum !== 0 ? byNum : a.requirement_code.localeCompare(b.requirement_code);
  });
}

/** Wire payloads -> the panel's display list (listed, ordered, lock-ready). */
export function toLockTargets(rows: RequirementPayload[] | null | undefined): RequirementItem[] {
  return sortLockTargets(
    (Array.isArray(rows) ? rows : []).filter(isListableRequirement).map(toRequirementItem),
  );
}

export function useArtifactLocks(
  store: RequirementStore,
  projectId: string | null
): UseArtifactLocksResult {
  // LOCK 2.1 — Load authoritative lockable requirement rows with real UUIDs.
  // Trigger: project/requirement signature change or fallback UUID resolution.
  // Calls: API requirement list read.
  // Writes: local requirementList only.
  // Failure: fail-soft; retain the prior/fallback in-memory list.
  // project's stored requirement records and refreshed whenever the project
  // changes or a lock action succeeds, so the "Requirement Lock Status" panel
  // always lists EVERY requirement instead of only those the in-memory gather
  // preview happened to carry.
  const [requirementList, setRequirementList] = useState<RequirementItem[]>([]);

  const refreshRequirementList = useCallback(async () => {
    if (!projectId || projectId === 'null') {
      setRequirementList([]);
      return;
    }
    try {
      const rows = await api.listRequirements(projectId);
      setRequirementList(toLockTargets(rows));
    } catch (err) {
      // A failed refresh must not break the panel: the caller falls back to the
      // requirement groups it already holds in memory.
      handleError('Could not load the lockable requirement list.', err);
    }
  }, [projectId]);

  // Refresh on project change AND whenever the in-memory requirement set
  // changes (a gather run, a saved merge, an SSE-driven reload) so requirement
  // rows created by that change are listed and lockable right away.
  const requirementSignature = (store.structuredRequirements.requirements || [])
    .map(r => `${r.requirement_code}:${r.id ?? ''}`)
    .join('|');
  useEffect(() => {
    void refreshRequirementList();
  }, [refreshRequirementList, requirementSignature]);

  /** Resolve a requirement code to the database UUID the lock API demands. */
  const resolveRequirementId = useCallback(
    (requirementCode: string): string | null => {
      const fromList = requirementList.find(r => r.requirement_code === requirementCode)?.id;
      if (fromList && UUID_RE.test(fromList)) return fromList;

      const fromStore = (store.structuredRequirements.requirements || []).find(
        r => r.requirement_code === requirementCode,
      )?.id;
      if (fromStore && UUID_RE.test(fromStore)) return fromStore;

      const fromLocks = store.lockedRequirements[requirementCode]?.artifact_id;
      if (fromLocks && UUID_RE.test(fromLocks)) return fromLocks;

      return null;
    },
    [requirementList, store.structuredRequirements.requirements, store.lockedRequirements],
  );

  /** Apply a lock-state change to the authoritative list without a refetch. */
  const patchRequirementList = useCallback(
    (requirementCode: string, patch: Partial<RequirementItem>) => {
      setRequirementList(prev =>
        prev.map(r => (r.requirement_code === requirementCode ? { ...r, ...patch } : r)),
      );
    },
    [],
  );

  // LOCK 2.2 — Optimistically lock one persisted artifact.
  // Trigger: requirement lock UI (generic helper also accepts PRD-section UUIDs).
  // Calls: LOCK 3.1 → LOCK 4.1.
  // Writes: local lock maps/status before the request.
  // Failure: roll local maps back; no full project reload.
  // Next: SYNC 2.x may later reconcile server metadata.
  // Returns true only when the backend accepted the lock, so callers can keep
  // their derived views (e.g. the lockable requirement list) in sync instead of
  // showing a lock that was actually rolled back.
  const handleLockArtifact = async (artifactType: string, artifactId: string, artifactCode: string): Promise<boolean> => {
    if (!projectId) return false;

    // Optimistic update - immediately update UI
    const optimisticLock: ArtifactLockState = {
      is_locked: true,
      locked_by: 'user',
      locked_at: new Date().toISOString(),
    };

    store.setLockedArtifacts(prev => ({
      ...prev,
      [artifactCode]: optimisticLock,
    }));

    if (artifactType === 'requirement') {
      store.setLockedRequirements(prev => ({
        ...prev,
        [artifactCode]: { ...optimisticLock, artifact_id: artifactId },
      }));
    }

    store.setSyncStatus(`${artifactType.replace('_', ' ')} ${artifactCode} locked.`);

    try {
      await api.lockArtifact(projectId, artifactType, artifactId, 'user');
      // Success - optimistic update already applied
      return true;
    } catch (err) {
      // Rollback on failure
      handleError(`Failed to lock the ${artifactType}.`, err);
      store.setLockedArtifacts(prev => ({
        ...prev,
        [artifactCode]: { is_locked: false },
      }));
      if (artifactType === 'requirement') {
        store.setLockedRequirements(prev => ({
          ...prev,
          [artifactCode]: { is_locked: false, artifact_id: artifactId },
        }));
      }
      store.setSyncStatus(`Failed to lock ${artifactType}.`);
      return false;
    }
  };

  // LOCK 2.3 — Optimistically unlock one persisted artifact.
  // Trigger/Calls: unlock UI → LOCK 3.1 → LOCK 4.2.
  // Writes: local maps/status; success keeps them.
  // Failure: restore only is_locked/artifact_id, losing prior owner/time locally.
  const handleUnlockArtifact = async (artifactType: string, artifactId: string, artifactCode: string): Promise<boolean> => {
    if (!projectId) return false;

    // Optimistic update - immediately update UI
    store.setLockedArtifacts(prev => ({
      ...prev,
      [artifactCode]: { is_locked: false },
    }));

    if (artifactType === 'requirement') {
      store.setLockedRequirements(prev => ({
        ...prev,
        [artifactCode]: { is_locked: false, artifact_id: artifactId },
      }));
    }

    store.setSyncStatus(`${artifactType.replace('_', ' ')} ${artifactCode} unlocked.`);

    try {
      await api.unlockArtifact(projectId, artifactType, artifactId, 'user');
      // Success - optimistic update already applied
      return true;
    } catch (err) {
      // Rollback on failure
      handleError(`Failed to unlock the ${artifactType}.`, err);
      store.setLockedArtifacts(prev => ({
        ...prev,
        [artifactCode]: { is_locked: true },
      }));
      if (artifactType === 'requirement') {
        store.setLockedRequirements(prev => ({
          ...prev,
          [artifactCode]: { is_locked: true, artifact_id: artifactId },
        }));
      }
      store.setSyncStatus(`Failed to unlock ${artifactType}.`);
      return false;
    }
  };

  /** Last-resort lookup: re-read the list once (the local copy may be stale). */
  const resolveRequirementIdFromServer = useCallback(
    async (requirementCode: string): Promise<string | null> => {
      if (!projectId || projectId === 'null') return null;
      try {
        const rows = await api.listRequirements(projectId);
        const lockable = toLockTargets(rows);
        setRequirementList(lockable);
        const id = lockable.find(r => r.requirement_code === requirementCode)?.id;
        return id && UUID_RE.test(id) ? id : null;
      } catch (err) {
        handleError('Could not resolve the requirement record for locking.', err);
        return null;
      }
    },
    [projectId],
  );

  // LOCK 2.4 — Requirement-code adapters. Resolve a real persisted UUID from
  // local sources, retry one server list read, and refuse unsaved/synthetic rows.
  const handleLockRequirement = async (requirementCode: string) => {
    const reqId = resolveRequirementId(requirementCode) || await resolveRequirementIdFromServer(requirementCode);
    if (!reqId) {
      // Previously a missing id made this a SILENT no-op (the button appeared
      // dead). Explain why the lock cannot be applied instead.
      handleError(
        `Cannot lock ${requirementCode}: it has no saved requirement record yet. Save the pending merge preview first, then lock it.`,
      );
      store.setSyncStatus(`Cannot lock ${requirementCode}: not saved yet.`);
      return;
    }
    const lockedOk = await handleLockArtifact('requirement', reqId, requirementCode);
    if (lockedOk) {
      patchRequirementList(requirementCode, {
        is_locked: true,
        locked_by: 'user',
        locked_at: new Date().toISOString(),
      });
    }
  };

  const handleUnlockRequirement = async (requirementCode: string) => {
    const reqId = resolveRequirementId(requirementCode) || await resolveRequirementIdFromServer(requirementCode);
    if (!reqId) {
      handleError(
        `Cannot unlock ${requirementCode}: it has no saved requirement record yet.`,
      );
      store.setSyncStatus(`Cannot unlock ${requirementCode}: not saved yet.`);
      return;
    }
    const unlockedOk = await handleUnlockArtifact('requirement', reqId, requirementCode);
    if (unlockedOk) {
      patchRequirementList(requirementCode, { is_locked: false, locked_by: null, locked_at: null });
    }
  };

  return {
    lockedArtifacts: store.lockedArtifacts,
    lockedRequirements: store.lockedRequirements,
    requirementList,
    refreshRequirementList,
    handleLockArtifact,
    handleUnlockArtifact,
    handleLockRequirement,
    handleUnlockRequirement,
  };
}
