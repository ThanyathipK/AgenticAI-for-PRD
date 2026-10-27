// useProjects — project list management extracted from the former
// useProjectState god-hook. Owns the project collection, the active project
// selection, pending merge actions, and the sidebar rename/delete/create
// interactions. All backend calls go through the typed `api` client.
import { useEffect, useRef, useState } from 'react';
import type { Dispatch, SetStateAction } from 'react';
import { api, detailFromJsonError } from '../api/client';
import type { PendingActionPayload, ProjectStatus, ProjectSummary } from '../api/types';
import { handleError } from '../components/Toast';
import { applyProjectPinOrder } from '../utils/projectOrdering';

export interface ProjectContextMenuState {
  projectId: string;
  x: number;
  y: number;
}

export type CreateProjectResult =
  | { projectId: string; error: null }
  | { projectId: null; error: string };

export interface CreateProjectOptions {
  /** Keep the project unselected while a caller seeds its initial PRD data. */
  select?: boolean;
  /** Business summary stored on the project and shown in the dashboard. */
  description?: string;
}

export interface UseProjectsResult {
  projectId: string | null;
  setProjectId: Dispatch<SetStateAction<string | null>>;
  projects: ProjectSummary[];
  setProjects: Dispatch<SetStateAction<ProjectSummary[]>>;
  pendingActions: PendingActionPayload[];
  setPendingActions: Dispatch<SetStateAction<PendingActionPayload[]>>;
  projectContextMenu: ProjectContextMenuState | null;
  setProjectContextMenu: Dispatch<SetStateAction<ProjectContextMenuState | null>>;
  renameProjectId: string | null;
  setRenameProjectId: Dispatch<SetStateAction<string | null>>;
  renameProjectName: string;
  setRenameProjectName: Dispatch<SetStateAction<string>>;
  handleRenameProject: (projectId: string, newName: string) => Promise<void>;
  /** Deletes a project; resolves to true on success. */
  handleDeleteProject: (projIdToDelete: string) => Promise<boolean>;
  /**
   * Creates a project with the given name. Resolves to null on success (the
   * caller may close its dialog), or to a user-facing error message — e.g. a
   * duplicate-name conflict from the backend — for inline display.
   */
  handleCreateProject: (name: string, options?: CreateProjectOptions) => Promise<CreateProjectResult>;
  handleTogglePin: (projectId: string) => Promise<void>;
  /** Flags/unflags a project (dashboard ★ marker) — independent of pinning. */
  handleToggleFlag: (projectId: string) => Promise<void>;
  /** Sets the project's user-editable workflow status (dashboard table), optimistically. */
  handleUpdateProjectStatus: (projectId: string, status: ProjectStatus, comment?: string) => Promise<boolean>;
  /** Whether the styled "New Project" modal is shown (replaces native prompt()). */
  isCreateModalOpen: boolean;
  setIsCreateModalOpen: Dispatch<SetStateAction<boolean>>;
  /** Project awaiting delete confirmation in the styled ConfirmModal (replaces native confirm()). */
  projectPendingDelete: string | null;
  setProjectPendingDelete: Dispatch<SetStateAction<string | null>>;
  /** Sidebar search — matches projects by name OR by their conversation message content. */
  projectSearchQuery: string;
  setProjectSearchQuery: Dispatch<SetStateAction<string>>;
}

// PROJECT flow index (frontend side of the project lifecycle). Each entry maps
// to a backend route/repository step; the whole hook is gated by AUTH 1.6.
//   PROJECT 1.2 — mount/auth effect  → api.listProjects    (list,   route 1.4)
//   PROJECT 2.1 — projectId selection → useProjectSync      (open,   route 2.3)
//   PROJECT 3.2 — debounced search   → api.searchProjects   (search, route 3.4)
//   PROJECT 4.2 — handleCreateProject                       (create, route 4.4)
//   PROJECT 5.1 — handleRenameProject                       (rename, route 5.3)
//   PROJECT 6.1 — handleDeleteProject                       (delete, route 6.3)
//   PROJECT 7.1 / 7.4.1 / 7.5.1 — pin / flag / status       (routes 7.3/7.4.3/7.5.3)
export function useProjects(isAuthenticated: boolean, authUserId: string | null): UseProjectsResult {
  const [projectId, setProjectId] = useState<string | null>(null);
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [pendingActions, setPendingActions] = useState<PendingActionPayload[]>([]);

  // PROJECT 1.2 — Mount/auth effect: the sidebar's project list load. Trigger is
  //              the AUTH 1.6 signed-in state; the guard below clears the list on
  //              sign-out so the previous user's projects never linger on screen.
  // Load the project list ONLY while signed in. The backend enforces the same
  // boundary (GET /api/projects requires a Bearer token and scopes to the JWT
  // subject), so before login the sidebar is empty by construction and after
  // login it shows exactly the signed-in user's projects. Signing out clears
  // the list so the previous user's data never lingers on screen.
  useEffect(() => {
    let isMounted = true;
    if (!isAuthenticated || !authUserId) {
      setProjects([]);
      setProjectId(null);
      return () => { isMounted = false; };
    }
    // PROJECT 1.2.1 — Fetch with bounded retry (8 × 1 s, then a failure toast):
    //                at SPA mount the API may still be booting (lifespan runs
    //                migrations + the LM Studio probe before serving).
    const fetchProjects = async (retries = 8, delay = 1000) => {
      try {
        // PROJECT 1.3 — API boundary → GET /api/projects (route PROJECT 1.4 →
        //               PROJECT 1.4.1 repository read).
        const projs = await api.listProjects();
        if (isMounted) {
          // PROJECT 1.5 — Response applied to UI state: the sidebar list, plus
          //              auto-selection of a project. Setting projectId is what
          //              fires the state load (PROJECT 2.1); an already-selected
          //              project is kept if it still exists.
          setProjects(projs);
          if (projs.length > 0) {
            setProjectId(prev => (prev && projs.some(p => p.id === prev)) ? prev : projs[0].id);
          }
        }
      } catch (error) {
        // PROJECT 1.2.1 (retry / terminal branch) — retry while budget remains
        // (asyncio-free: these are plain setTimeout retries), otherwise surface
        // the failure toast and leave the list empty.
        if (retries > 0 && isMounted) {
          setTimeout(() => fetchProjects(retries - 1, delay), delay);
        } else {
          handleError('Failed to load projects from the server. Please check your connection.', error);
        }
      }
    };
    fetchProjects();
    return () => { isMounted = false; };
  }, [isAuthenticated, authUserId]);

  // "New Project" modal visibility (the styled replacement for prompt()).
  const [isCreateModalOpen, setIsCreateModalOpen] = useState<boolean>(false);

  // Project awaiting delete confirmation in the styled ConfirmModal
  // (the styled replacement for confirm()). Holds the target project id.
  const [projectPendingDelete, setProjectPendingDelete] = useState<string | null>(null);

  // Context menu state for project rename/delete
  const [projectContextMenu, setProjectContextMenu] = useState<ProjectContextMenuState | null>(null);
  const [renameProjectId, setRenameProjectId] = useState<string | null>(null);
  const [renameProjectName, setRenameProjectName] = useState<string>('');

  // Sidebar project search. The query drives a debounced server search below;
  // matching happens against project names AND conversation message content.
  const [projectSearchQuery, setProjectSearchQuery] = useState<string>('');

  // Debounced server search: matches project names AND persisted conversation
  // message content (GET /api/projects/search). Clearing the query restores
  // the full list. A monotonic sequence guard drops stale responses so a slow
  // earlier request can never overwrite fresher results.
  // PROJECT 3.2 (state) — Monotonic sequence guard for the debounced search: a
  // slow earlier request can never overwrite fresher results; `searching` tracks
  // whether a search actually replaced the list (so the clear-branch restore in
  // PROJECT 3.4 leaves the mount-time load untouched).
  const searchStateRef = useRef<{ searching: boolean; seq: number }>({ searching: false, seq: 0 });

  // PROJECT 3.2 — Debounced sidebar search (250 ms) → PROJECT 3.3 → route 3.4.
  //              Runs only for a signed-in user (the server scopes results to
  //              the JWT subject anyway). Clearing the query restores the full
  //              list via PROJECT 3.4 → PROJECT 1.3.
  useEffect(() => {
    const query = projectSearchQuery.trim();

    // Per-user boundary: searching (and the unfiltered restore) only run for a
    // signed-in user; the server scopes results to the JWT subject anyway.
    if (!isAuthenticated) {
      // Invalidate an already-started search/restore request. Its promise can
      // resolve after logout, and without advancing the sequence it would put
      // the previous user's projects back into the now-signed-out sidebar.
      searchStateRef.current.searching = false;
      searchStateRef.current.seq += 1;
      return;
    }

    // Query emptied -> restore the unfiltered list, but only after a search
    // actually replaced it (keeps the mount-time load untouched).
    if (!query) {
      if (!searchStateRef.current.searching) return;
      searchStateRef.current.searching = false;
      const seq = ++searchStateRef.current.seq;
      // PROJECT 3.4 — Query emptied: restore the unfiltered list (PROJECT 1.3);
      // failure keeps whatever is loaded (the next keystroke retries).
      api.listProjects()
        .then((projs) => {
          if (searchStateRef.current.seq === seq) setProjects(projs);
        })
        .catch(() => {
          // Keep showing whatever is loaded; the next keystroke retries.
        });
      return;
    }

    const timer = setTimeout(() => {
      const seq = ++searchStateRef.current.seq;
      searchStateRef.current.searching = true;
      // PROJECT 3.3 — API boundary → GET /api/projects/search (route PROJECT 3.4
      //               → PROJECT 3.4.1 repository search with per-project snippets).
      api.searchProjects(query)
        .then((results) => {
          if (searchStateRef.current.seq === seq) setProjects(results);
        })
        .catch((err) => {
          handleError('Failed to search projects.', err);
        });
    }, 250);
    return () => clearTimeout(timer);
  }, [projectSearchQuery, isAuthenticated]);

  // Close context menu on outside click
  useEffect(() => {
    const handleClick = () => setProjectContextMenu(null);
    if (projectContextMenu) {
      document.addEventListener('click', handleClick);
      return () => document.removeEventListener('click', handleClick);
    }
  }, [projectContextMenu]);

  // Handle project rename. Failures — including the backend's 409
  // duplicate-name conflict — surface the server's own detail message in the
  // toast so the user knows exactly which name is already taken.
  // PROJECT 5.1 — Rename handler (UI: sidebar context menu). Layer chain:
  //              PROJECT 5.1 → PROJECT 5.2 (api.renameProject) → route 5.3 →
  //              repository 5.3.2 → projects UPDATE. A 409 duplicate answers with
  //              the server's own detail, shown verbatim in the toast.
  const handleRenameProject = async (projectId: string, newName: string) => {
    if (!newName.trim()) return;
    try {
      // PROJECT 5.2 — API boundary → PUT /api/projects/{id} (route PROJECT 5.3).
      await api.renameProject(projectId, newName);
      setProjects(prev => prev.map(p => (p.id === projectId ? { ...p, name: newName.trim() } : p)));
      setRenameProjectId(null);
      setRenameProjectName('');
    } catch (err) {
      handleError(detailFromJsonError(err) ?? 'Failed to rename the project.', err);
    }
  };

  // Handle project delete (submit from the styled ConfirmModal). Resolves to
  // true on success so the modal knows whether to close; failures are
  // surfaced through the toast handler.
  // PROJECT 6.1 — Delete handler (UI: styled ConfirmModal). Chain: 6.1 → 6.2
  //              (api.deleteProject) → route 6.3 → repository 6.3.1 (lock gate +
  //              event log) → projects DELETE (FK CASCADE). Resolves false so the
  //              modal stays open when the server refuses.
  const handleDeleteProject = async (projIdToDelete: string): Promise<boolean> => {
    try {
      // PROJECT 6.2 — API boundary → DELETE /api/projects/{id} (route PROJECT 6.3).
      await api.deleteProject(projIdToDelete);
      setProjects(prev => prev.filter(p => p.id !== projIdToDelete));
      // If the deleted project was the active one, switch to the first remaining project
      // PROJECT 6.4 — UI follow-up: switch the active project to the first
      // remaining one (or to none) so the workspace never points at a deleted id.
      if (projIdToDelete === projectId && projects.length > 1) {
        const remaining = projects.filter(p => p.id !== projIdToDelete);
        setProjectId(remaining[0].id);
      } else if (projIdToDelete === projectId) {
        setProjectId(null);
      }
      setProjectContextMenu(null);
      return true;
    } catch (err) {
      handleError('Failed to delete the project.', err);
      return false;
    }
  };

  // Handle project create (submit from the sidebar's New Project modal).
  // Resolves to null on success so the modal closes, or to a user-facing
  // error message — typically the backend's 409 "name already exists" detail —
  // that the modal renders inline next to the input. No toast here: the modal
  // is the presentation layer for create failures.
  // PROJECT 4.2 — Create handler (UI: New Project modal). Client-side guards
  //              (empty / > 50 chars) mirror the ProjectCreate schema; the
  //              server's 409 duplicate detail is returned for inline display.
  //              Chain: 4.2 → 4.3 (api.createProject) → route 4.4 → repository
  //              4.4.2 (projects + requirement_states INSERT) → 4.5 select/open.
  const handleCreateProject = async (name: string, options: CreateProjectOptions = {}): Promise<CreateProjectResult> => {
    const trimmed = name.trim();
    if (!trimmed) return { projectId: null, error: 'Project name must not be empty.' };
    if (trimmed.length > 50) return { projectId: null, error: 'Project name must be 50 characters or fewer.' };
    try {
      // PROJECT 4.3 — API boundary → POST /api/projects (route PROJECT 4.4).
      const created = await api.createProject(trimmed, options.description);
      // PROJECT 4.5 — Re-read the authoritative list, then select the returned
      // project id. Changing projectId triggers useProjectSync, which loads the
      // new project's empty state immediately after the modal closes.
      const refreshed = await api.listProjects();
      searchStateRef.current.searching = false;
      searchStateRef.current.seq += 1;
      setProjectSearchQuery('');
      setProjects(refreshed);
      if (options.select !== false) setProjectId(created.id);
      return { projectId: created.id, error: null };
    } catch (err) {
      return { projectId: null, error: detailFromJsonError(err) ?? 'Failed to create the project.' };
    }
  };

  // Handle project pin/unpin — pins the chat to the top of the sidebar.
  // PROJECT 7.1 — Pin/unpin handler (UI: sidebar pin + context menu). Optimistic:
  //              the local row flips first, then the server response reconciles,
  //              and any failure rolls the flip back with a toast.
  //              Chain: 7.1 → 7.2 (api.toggleProjectPin) → route 7.3 → repository
  //              7.3.1 (no lock gate by design — metadata, not content).
  const handleTogglePin = async (projectIdToPin: string) => {
    const target = projects.find(p => p.id === projectIdToPin);
    const nextPinned = !(target && target.is_pinned);
    // Optimistic update for snappy UI, then reconcile with the server.
    setProjects(prev => applyProjectPinOrder(prev, projectIdToPin, nextPinned));
    try {
      // PROJECT 7.2 — API boundary → PUT /api/projects/{id}/pin (route PROJECT 7.3).
      const updated = await api.toggleProjectPin(projectIdToPin, nextPinned);
      setProjects(prev =>
        applyProjectPinOrder(prev, projectIdToPin, Boolean(updated.is_pinned))
      );
    } catch (err) {
      setProjects(prev =>
        applyProjectPinOrder(prev, projectIdToPin, !nextPinned)
      );
      handleError('Failed to update the pin.', err);
    }
    setProjectContextMenu(null);
  };

  // Handle project flag/unflag — dashboard-only ★ marker. Deliberately
  // independent of pinning: flagging marks the project for attention in the
  // projects overview table and never affects the sidebar's pinned ordering.
  // PROJECT 7.4.1 — Flag/unflag handler (UI: dashboard ★ marker). Same optimistic
  //                pattern as pinning; deliberately independent of pin so the
  //                sidebar's pinned-first ordering is never affected.
  //                Chain: 7.4.1 → 7.4.2 (api.toggleProjectFlag) → route 7.4.3 →
  //                repository 7.4.4.
  const handleToggleFlag = async (projectIdToFlag: string) => {
    const target = projects.find(p => p.id === projectIdToFlag);
    const nextFlagged = !(target && target.is_flagged);
    // Optimistic update for snappy UI, then reconcile with the server.
    setProjects(prev =>
      prev.map(p => (p.id === projectIdToFlag ? { ...p, is_flagged: nextFlagged } : p))
    );
    try {
      // PROJECT 7.4.2 — API boundary → PUT /api/projects/{id}/flag (route 7.4.3).
      const updated = await api.toggleProjectFlag(projectIdToFlag, nextFlagged);
      setProjects(prev => prev.map(p => (p.id === projectIdToFlag ? { ...p, is_flagged: updated.is_flagged } : p)));
    } catch (err) {
      setProjects(prev =>
        prev.map(p => (p.id === projectIdToFlag ? { ...p, is_flagged: !nextFlagged } : p))
      );
      handleError('Failed to update the flag.', err);
    }
    setProjectContextMenu(null);
  };

  // Handle project workflow status change (dashboard status badge dropdown).
  // Same optimistic pattern as pinning: apply locally for snappy UI, reconcile
  // with the server, and roll back on failure with a toast.
  // PROJECT 7.5.1 — Workflow-status handler (UI: dashboard status dropdown).
  //                Optimistic like the pin/flag handlers, but the rollback also
  //                restores the previous status value.
  //                Chain: 7.5.1 → 7.5.2 (api.updateProjectStatus) → route 7.5.3
  //                (allowed-status validation) → repository 7.5.4.
  const handleUpdateProjectStatus = async (projectIdToUpdate: string, newStatus: ProjectStatus, comment?: string): Promise<boolean> => {
    const previousStatus = projects.find(p => p.id === projectIdToUpdate)?.status;
    setProjects(prev =>
      prev.map(p => (p.id === projectIdToUpdate ? { ...p, status: newStatus } : p))
    );
    try {
      // PROJECT 7.5.2 — API boundary → PUT /api/projects/{id}/status (route 7.5.3).
      const updated = await api.updateProjectStatus(projectIdToUpdate, newStatus, comment);
      setProjects(prev =>
        prev.map(p => (p.id === projectIdToUpdate ? { ...p, ...updated } : p))
      );
      setProjectContextMenu(null);
      return true;
    } catch (err) {
      setProjects(prev =>
        prev.map(p => (p.id === projectIdToUpdate ? { ...p, status: previousStatus } : p))
      );
      handleError(detailFromJsonError(err) ?? 'Failed to update the project status.', err);
      setProjectContextMenu(null);
      return false;
    }
  };

  return {
    projectId,
    setProjectId,
    projects,
    setProjects,
    pendingActions,
    setPendingActions,
    projectContextMenu,
    setProjectContextMenu,
    renameProjectId,
    setRenameProjectId,
    renameProjectName,
    setRenameProjectName,
    handleRenameProject,
    handleDeleteProject,
    handleCreateProject,
    handleTogglePin,
    handleToggleFlag,
    handleUpdateProjectStatus,
    isCreateModalOpen,
    setIsCreateModalOpen,
    projectPendingDelete,
    setProjectPendingDelete,
    projectSearchQuery,
    setProjectSearchQuery,
  };
}
