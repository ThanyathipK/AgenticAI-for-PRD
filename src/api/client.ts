// ============================================================================
// Typed API client — every backend endpoint used by the app is wrapped in a
// strongly-typed function so callers never touch raw `axios` responses or cast
// payloads to `any`. Mirrors `backend/app/routes/*` and `backend/app/schemas.py`.
// ============================================================================

import axios from 'axios';
import type {
  ActionStatusResponse,
  ArtifactLockResponse,
  AuthConfigPayload,
  AuthAccountDeleteResponse,
  AuthMessageResponse,
  AuthRegisterRequest,
  AuthRegisterResponse,
  AuthSessionPayload,
  AuthUserPayload,
  ConfirmActionResponse,
  ConversationMessageCreatePayload,
  ConversationMessagePayload,
  ConvertedPrdMarkdownPayload,
  DocumentDeleteResponse,
  DocumentMarkdownPayload,
  HealthPayload,
  ImpactAnalysisPayload,
  PendingActionPayload,
  PrdSectionListPayload,
  PrdSectionUpdateResponse,
  PrdVersionDiffPayload,
  PrdVersionPayload,
  PrdVersionRestorePayload,
  ProcessDocumentResponse,
  ProcessRequirementsRequest,
  ProcessRequirementsResponse,
  ProjectCreated,
  ProjectDeleteResponse,
  ProjectReviewEventPayload,
  ProjectHealthPayload,
  AuditRunPayload,
  AuditWaiverPayload,
  ProjectStatus,
  ProjectSummary,
  RequirementPayload,
  RequirementStatePayload,
  UploadedDocumentPayload,
  TraceabilityPayload,
  ArtifactReferencePayload,
  DependencyGraphPayload,
  RegenerationPlanPayload,
  RegenerationResultPayload,
  GenerationJobPayload,
} from './types';

// ============================================================================
// Auth plumbing
// ============================================================================
// The session token is mirrored into axios defaults by setAuthToken() so that
// any endpoint the backend later protects is authenticated automatically; the
// auth helpers below also accept an explicit token because they must work
// before (login) or independently of that global default.
// AUTH 1.5.1 — Session sink #1 (token → transport): mirror the JWT into axios
//              defaults so every later call on a protected route is authenticated
//              automatically. Written in AUTH 1.3.1 (login) and AUTH 2.1
//              (restore); removed in AUTH 4.3 (logout / stale session).
export function setAuthToken(token: string | null): void {
  if (token) {
    axios.defaults.headers.common.Authorization = `Bearer ${token}`;
  } else {
    delete axios.defaults.headers.common.Authorization;
  }
}

/** Bearer header map for a single authenticated request. */
function bearerHeaders(token?: string | null): Record<string, string> {
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function get<T>(
  url: string,
  params?: Record<string, unknown>,
  token?: string | null,
  signal?: AbortSignal,
): Promise<T> {
  const { data } = await axios.get<T>(url, {
    params,
    headers: bearerHeaders(token),
    signal,
  });
  return data;
}

async function patch<T>(url: string, body?: unknown): Promise<T> {
  const { data } = await axios.patch<T>(url, body ?? {});
  return data;
}

async function post<T>(
  url: string,
  body?: unknown,
  params?: Record<string, unknown>,
  signal?: AbortSignal,
  token?: string | null
): Promise<T> {
  const { data } = await axios.post<T>(url, body ?? null, {
    params,
    signal,
    headers: bearerHeaders(token),
  });
  return data;
}

async function put<T>(url: string, body?: unknown): Promise<T> {
  const { data } = await axios.put<T>(url, body ?? {});
  return data;
}

async function del<T>(url: string): Promise<T> {
  const { data } = await axios.delete<T>(url);
  return data;
}

// Multipart upload helper — the existing helpers above are JSON-only. The
// browser supplies the multipart boundary, so Content-Type must NOT be set
// manually here.
async function postFormData<T>(url: string, formData: FormData): Promise<T> {
  const { data } = await axios.post<T>(url, formData);
  return data;
}

export interface BlobDownload {
  blob: Blob;
  filename?: string;
}

// Binary download helper — preserve the server's authoritative filename as
// well as the body. PRD exports encode the snapshot SemVer in that header.
async function postBlob(url: string, body?: unknown): Promise<BlobDownload> {
  const resp = await axios.post<Blob>(url, body ?? null, { responseType: 'blob' });
  const disposition = resp.headers['content-disposition'];
  const filenameMatch = typeof disposition === 'string'
    ? disposition.match(/filename="([^"]+)"/i)
    : null;
  return {
    blob: resp.data,
    filename: filenameMatch?.[1],
  };
}

// Error bodies from binary endpoints arrive as a Blob too, so the FastAPI
// {"detail": ...} payload has to be decoded before it can be surfaced.
export async function detailFromBlobError(err: unknown): Promise<string | null> {
  if (!axios.isAxiosError(err) || !(err.response?.data instanceof Blob)) return null;
  try {
    const parsed = JSON.parse(await err.response.data.text()) as { detail?: unknown };
    return typeof parsed.detail === 'string' ? parsed.detail : null;
  } catch {
    return null;
  }
}

// Error bodies from JSON endpoints are FastAPI {"detail": ...} payloads: a
// plain string for 4xx conflicts (e.g. duplicate project name -> 409) or a
// list of issue objects for 422 validation failures, whose `msg` carries the
// readable text (Pydantic prefixes nested validator messages with
// "Value error, " — stripped here). Returns null when nothing usable exists.
export function detailFromJsonError(err: unknown): string | null {
  if (!axios.isAxiosError(err)) return null;
  const detail: unknown = (err.response?.data as { detail?: unknown } | undefined)?.detail;
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    for (const item of detail) {
      const msg = (item as { msg?: unknown })?.msg;
      if (typeof msg === 'string' && msg.trim()) {
        return msg.replace(/^Value error,\s*/i, '');
      }
    }
  }
  return null;
}

export const api = {
  // ---- PRD sections (part-level editing / locking / versioning) ------------
  // The PRD is stored as NINE editable parts; each part is independently
  // edited, locked, and versioned (append-only history).
  getPrdSections: (projectId: string, materialize = false) =>
    get<PrdSectionListPayload>(`/api/project/${projectId}/prd/sections`, { materialize }),
  // ---- PRD sections (parts) ----------------------------------------------
  // PRD-SECTION 1.x — frontend side of the nine-part document. Backend:
  // routes 2.1–2.7, service 3.1–3.7, repository 4.1–4.5.
  // NOT present in this client (verified): a per-part revert helper (PRD-SECTION 2.3) and
  // a section-version helper (PRD-SECTION 2.7) — both endpoints are UI-unreachable.
  // PRD-SECTION 1.3 — part save (PRD-SECTION 2.2): `baseContent` is the text the editor
  //               started from, which is what enables the server-side three-way merge
  //               (3.5) instead of a blind overwrite.
  updatePrdSection: (
    projectId: string,
    sectionKey: string,
    content: string,
    options?: { review_status?: string; change_summary?: string; updated_by?: string; base_content?: string; ai_generatable?: boolean }
  ) =>
    patch<PrdSectionUpdateResponse>(`/api/project/${projectId}/prd/sections/${sectionKey}`, {
      content,
      base_content: options?.base_content,
      review_status: options?.review_status,
      change_summary: options?.change_summary,
      // Omitted => backend default False => the manual save makes the part
      // HUMAN-OWNED so "Generate PRD" preserves it. Pass true to hand it back.
      ai_generatable: options?.ai_generatable,
      updated_by: options?.updated_by ?? 'user',
    }),
  // PRD-SECTION 1.4 — per-part lock (PRD-SECTION 2.4); `lockedBy` is the lock owner label.
  lockPrdSection: (projectId: string, sectionKey: string, lockedBy = 'user') =>
    post(`/api/project/${projectId}/prd/sections/${sectionKey}/lock`, { locked_by: lockedBy }),
  // PRD-SECTION 1.4 — per-part unlock (PRD-SECTION 2.5): re-enables editing and AI
  //               regeneration for that part.
  unlockPrdSection: (projectId: string, sectionKey: string, lockedBy = 'user') =>
    post(`/api/project/${projectId}/prd/sections/${sectionKey}/unlock`, { locked_by: lockedBy }),

  // ---- PRD version ledger ------------------------------------------------
  // VERSION 1.x — frontend side of the append-only ledger (VERSION 2.x/3.x/4.x).
  // VERSION 1.1 — timeline read: the whole ledger, newest first (VERSION 2.1),
  //               mapped by toVersionHistoryList into the VersionHistory panel.
  // Immutable per-project PRD snapshots. EVERY change (Generate PRD click or
  // manual part edit/revert) appends a version that ALWAYS advances; each row
  // carries change_type ('ai' | 'manual'), a change summary, and the
  // per-section change records incl. locked_preserved markers.
  getPrdVersions: (
    projectId: string,
    options?: { limit?: number; beforeVersion?: number; signal?: AbortSignal },
  ) =>
    get<PrdVersionPayload[]>(
      `/api/project/${projectId}/prd-versions`,
      { limit: options?.limit, before_version: options?.beforeVersion },
      null,
      options?.signal,
    ),
  // VERSION 1.2 — diff read (VERSION 2.2): `baseVersion` is the user-picked base from
  //               the timeline; omitted ⇒ the backend uses the immediate predecessor.
  getPrdVersionDiff: (projectId: string, versionNumber: number, baseVersion?: number) =>
    get<PrdVersionDiffPayload>(
      `/api/project/${projectId}/prd-versions/${versionNumber}/diff`,
      baseVersion ? { base_version: baseVersion } : undefined,
    ),
  // Restore the WHOLE PRD document from a ledger version. APPEND-ONLY: the
  // backend records the restored content as a NEW version; locked parts keep
  // their current content. Returns the re-stitched document + new version.
  // VERSION 1.5 — restore call (VERSION 2.5): append-only, so the response carries the
  //               NEW version number produced by the restore, not the restored one.
  restorePrdVersion: (projectId: string, versionNumber: number, restoredBy = 'user') =>
    post<PrdVersionRestorePayload>(
      `/api/project/${projectId}/prd-versions/${versionNumber}/restore`,
      null,
      { restored_by: restoredBy },
    ),

  // ---- Projects ----------------------------------------------------------
  // PROJECT flow index — frontend side of the project lifecycle. Backend
  // counterparts: routes 1.4/3.4/4.4/5.3/6.3/7.3/7.4.3/7.5.3/2.3/8.2 in
  // backend/app/routes/projects.py, repositories 1.4.1/3.4.1/4.4.2/5.3.2/6.3.1/
  // 7.3.1/7.4.4/7.5.4 in app/repositories/project.py, state 2.3.2/8.2.2 in
  // app/repositories/requirement_state.py, and the shared AUTH 7.5 ownership gate.
  // PROJECT 1.3 — API client: the sidebar list read → route PROJECT 1.4.
  listProjects: () => get<ProjectSummary[]>('/api/projects'),
  // PROJECT 3.3 — API client for the debounced sidebar search (PROJECT 3.2)
  //               → route PROJECT 3.4. Server-side search: matches project
  //               names AND conversation message content.
  searchProjects: (query: string) => get<ProjectSummary[]>('/api/projects/search', { q: query }),
  // PROJECT 4.3 — API client (PROJECT 4.2 → route PROJECT 4.4). A duplicate
  //               name answers 409 and is rendered inline by the modal.
  createProject: (name: string, description?: string) =>
    post<ProjectCreated>('/api/projects', { name, description: description?.trim() || null }),
  // PROJECT 5.2 — API client (PROJECT 5.1 → route PROJECT 5.3); the name is
  //               trimmed client-side and again in the ProjectCreate schema.
  renameProject: (projectId: string, name: string) =>
    put<ProjectSummary>(`/api/projects/${projectId}`, { name: name.trim() }),
  // PROJECT 6.2 — API client (PROJECT 6.1 → route PROJECT 6.3). Deletion
  //               cascades to every child table via the FK definitions.
  deleteProject: (projectId: string) =>
    del<ProjectDeleteResponse>(`/api/projects/${projectId}`),
  // PROJECT 7.2 — API client (PROJECT 7.1 → route PROJECT 7.3).
  toggleProjectPin: (projectId: string, isPinned: boolean) =>
    put<ProjectSummary>(`/api/projects/${projectId}/pin`, { is_pinned: isPinned }),
  // PROJECT 7.4.2 — API client for the dashboard ★ flag (PROJECT 7.4.1 → route
  //                 PROJECT 7.4.3).
  // Dashboard-only ★ flag marker (PUT /api/projects/{id}/flag). Independent
  // of pinning: flagging never affects the sidebar's pinned-first ordering.
  toggleProjectFlag: (projectId: string, isFlagged: boolean) =>
    put<ProjectSummary>(`/api/projects/${projectId}/flag`, { is_flagged: isFlagged }),
  // PROJECT 7.5.2 — API client for the dashboard workflow-status dropdown
  //                 (PROJECT 7.5.1 → route PROJECT 7.5.3).
  // Dashboard workflow status (user-editable via the status badge dropdown).
  updateProjectStatus: (projectId: string, status: ProjectStatus, comment?: string) =>
    put<ProjectSummary>(`/api/projects/${projectId}/status`, { status, comment }),
  getProjectReviewHistory: (projectId: string) =>
    get<ProjectReviewEventPayload[]>(`/api/projects/${projectId}/review-history`),
  getProjectHealth: (projectId: string) =>
    get<ProjectHealthPayload>(`/api/projects/${projectId}/health`),
  getAuditHistory: (projectId: string) =>
    get<AuditRunPayload[]>(`/api/projects/${projectId}/audit-history`),
  getAuditWaivers: (projectId: string) =>
    get<AuditWaiverPayload[]>(`/api/projects/${projectId}/waivers`),
  createAuditWaiver: (projectId: string, payload: {
    rule_id: string;
    target_requirement_id?: string | null;
    reason: string;
    compensating_control?: string;
    owner: string;
    expires_at: string;
  }) => post<AuditWaiverPayload>(`/api/projects/${projectId}/waivers`, payload),
  revokeAuditWaiver: (projectId: string, waiverId: string, reason: string) =>
    post<AuditWaiverPayload>(`/api/projects/${projectId}/waivers/${waiverId}/revoke`, { reason }),

  // ---- Requirement state --------------------------------------------------
  // PROJECT 2.2 — API client for the full project-state read that opens a
  //               project (trigger PROJECT 2.1 → route PROJECT 2.3); the payload
  //               is mapped into the store in PROJECT 2.4.
  getProjectState: (projectId: string, signal?: AbortSignal) =>
    get<RequirementStatePayload>(`/api/project/${projectId}`, undefined, null, signal),
  // PROJECT 8.1 — API client for the DIRECT state write (manual/legacy
  //               fallback; the AI write path is the CONFIRMATION flow)
  //               → route PROJECT 8.2.
  updateProjectState: (projectId: string, updates: Record<string, unknown>) =>
    put<RequirementStatePayload>(`/api/project/${projectId}`, updates),

  // ---- Pending actions ----------------------------------------------------
  // CONFIRM 2.x — client side of the human-in-the-loop write path. The draft was
  // staged by GATHERING 8.0 or the document extraction; only 2.3 actually writes.
  // CONFIRM 2.1 — read: staged actions for a project (CONFIRM 3.1, WAITING_CONFIRMATION
  //               only). Also drives CONFIRM 1.7.
  listPendingActions: (projectId: string) =>
    get<PendingActionPayload[]>(`/api/pending-actions/${projectId}`),
  listConversationMessages: (projectId: string, options?: { limit?: number; before?: string }) =>
    get<ConversationMessagePayload[]>(`/api/project/${projectId}/conversations`, options),
  appendConversationMessage: (projectId: string, message: ConversationMessageCreatePayload) =>
    post<ConversationMessagePayload>(`/api/project/${projectId}/conversations`, message),
  // CONFIRM 2.3 — THE write call of the whole product's AI path: it commits the
  //               staged draft (CONFIRM 3.3 → RequirementStateRepository).
  confirmAction: (actionId: string, projectId: string) =>
    post<ConfirmActionResponse>(`/api/confirm-action/${actionId}`, null, { project_id: projectId }),
  // CONFIRM 2.4 — Discard call (CONFIRM 3.4): deletes the action, writes nothing else.
  cancelAction: (actionId: string, projectId: string) =>
    post<ActionStatusResponse>(`/api/cancel-action/${actionId}`, null, { project_id: projectId }),
  // CONFIRM 2.2 — Impact preview read (CONFIRM 1.5 → 3.2 → 4.x): what the draft
  //               changes and which downstream artifacts are affected.
  // READ-ONLY Requirement Impact Analysis for one pending merge action:
  // what the merge changes + which downstream artifacts are impacted.
  getActionImpact: (actionId: string, projectId: string) =>
    get<ImpactAnalysisPayload>(`/api/pending-actions/${actionId}/impact`, { project_id: projectId }),

  // ---- Artifact locks -----------------------------------------------------
  lockArtifact: (projectId: string, artifactType: string, artifactId: string, lockedBy = 'user') =>
    post<ArtifactLockResponse>(
      `/api/project/${projectId}/artifacts/${artifactType}/${artifactId}/lock`,
      { locked_by: lockedBy }
    ),
  unlockArtifact: (projectId: string, artifactType: string, artifactId: string, lockedBy = 'user') =>
    post<ArtifactLockResponse>(
      `/api/project/${projectId}/artifacts/${artifactType}/${artifactId}/unlock`,
      { locked_by: lockedBy }
    ),

  // ---- Requirement records (authoritative list, incl. lock metadata) ------
  // Backed by GET /api/project/{id}/requirements — the only endpoint that
  // returns each requirement's real database UUID (needed by the lock API,
  // which rejects anything that is not a UUID) together with its lock state.
  listRequirements: (projectId: string) =>
    get<RequirementPayload[]>(`/api/project/${projectId}/requirements`),

  // ---- Multi-agent requirements workflow ----------------------------------
  // `signal` lets callers abort an in-flight generation (the Stop button) —
  // axios then rejects the promise with a CanceledError instead of waiting for
  // the LLM workflow to finish.
  // CHAT 1.3 / GATHERING entry — the single request the chat composer issues. The
  // backend decides between conversation (CHAT 2.3) and the agent workflow, which
  // is why the UI never calls POST /api/chat (CHAT 5.1): that handler exists but
  // has NO client method here (documented discrepancy, see CHAT 5.x).
  processRequirements: (request: ProcessRequirementsRequest, signal?: AbortSignal) =>
    post<ProcessRequirementsResponse>('/api/process-requirements', request, undefined, signal),
  startAgentJob: (request: ProcessRequirementsRequest) =>
    post<GenerationJobPayload<ProcessRequirementsResponse>>('/api/process-requirements/background', request),
  getGenerationJob: <T>(projectId: string, jobId: string) =>
    get<GenerationJobPayload<T>>(`/api/projects/${projectId}/generation-jobs/${jobId}`),
  cancelGenerationJob: (projectId: string, jobId: string) =>
    post<GenerationJobPayload>(`/api/projects/${projectId}/generation-jobs/${jobId}/cancel`),

  // ---- PRD template ---------------------------------------------------------
  // Official Krungsri Nimble PRD LaTeX template served from
  // backend/app/prompts/template-krungsrinimble.tex — the exact structure the
  // Architect agent fills in during generation. Normalized for the PRD panel
  // preview and compiled for exports.
  getPrdTemplate: () =>
    get<{ template_latex: string }>('/api/prd/template'),

  // ---- PRD preview normalization --------------------------------------------
  // The generated PRD body is LaTeX (Krungsri .tex template). The backend turns
  // either that LaTeX or a legacy Markdown PRD into clean GFM markdown so the
  // on-screen preview never renders raw LaTeX (POST /api/prd/convert).
  convertPrdToMarkdown: (source: string) =>
    post<ConvertedPrdMarkdownPayload>('/api/prd/convert', { latex_source: source }),

  // ---- Generation cancellation (Stop button) --------------------------------
  // Asks the backend to hard-terminate the in-flight multi-agent run for a
  // project. Complements the AbortSignal above: the signal stops the UI
  // waiting, this stops the server actually doing the work.
  cancelProcessRequirements: (projectId: string) =>
    post<{ project_id: string; cancelled: boolean }>(
      '/api/process-requirements/cancel',
      null,
      { project_id: projectId },
    ),

  // ---- Clarifications ------------------------------------------------------
  // CLARIFY 2.x — client side of the clarification flow (FLOW 10).
  // CLARIFY 2.1 — POST /api/clarification/submit (CLARIFY 3.0): answers are keyed
  //               POSITIONALLY (`q-0`, `q-1`, …) over the stored question list, which
  //               is the contract CLARIFY 3.4 relies on.
  submitClarifications: (projectId: string, answers: Record<string, string>) =>
    post<RequirementStatePayload>('/api/clarification/submit', {
      project_id: projectId,
      answers,
    }),

  // ---- Uploaded documents (knowledge base) ---------------------------------
  // DOC-UPLOAD 1.7 — client side of the knowledge base (FLOW 14): backend routes 2.1–2.4.
  //               Upload ONLY stores the converted markdown as knowledge; it never writes
  //               requirements. `processDocument` (FLOW 15) is the explicit extraction
  //               action and returns a DRAFT pending_action for user confirmation.
  // DOC-UPLOAD 1.2 — the upload call itself (DOC-UPLOAD 2.1); multipart FormData is the
  //               only request in this client that does not send JSON.
  uploadDocument: (projectId: string, file: File) => {
    const formData = new FormData();
    formData.append('file', file);
    return postFormData<UploadedDocumentPayload>(
      `/api/project/${projectId}/documents/upload`,
      formData,
    );
  },
  // DOC-UPLOAD 1.3 — list load (DOC-UPLOAD 2.2): metadata only.
  listDocuments: (projectId: string) =>
    get<UploadedDocumentPayload[]>(`/api/project/${projectId}/documents`),
  // DOC-UPLOAD 1.5 — full stored markdown for the preview (DOC-UPLOAD 2.3).
  getDocumentMarkdown: (projectId: string, documentId: string) =>
    get<DocumentMarkdownPayload>(`/api/project/${projectId}/documents/${documentId}`),
  // FLOW 15 — explicit extraction (DRAFT only); no knowledge-base write happens here.
  processDocument: (projectId: string, documentId: string, signal?: AbortSignal) =>
    post<ProcessDocumentResponse>(
      `/api/project/${projectId}/documents/${documentId}/process`,
      undefined,
      undefined,
      signal,
    ),
  cancelDocumentExtraction: (projectId: string, documentId: string) =>
    post<{ project_id: string; document_id: string; cancelled: boolean }>(
      `/api/project/${projectId}/documents/${documentId}/process/cancel`,
    ),
  // DOC-UPLOAD 1.6 — permanent removal (DOC-UPLOAD 2.4), which also discards that
  //               document's staged DRAFT server-side (2.4.2).
  deleteDocument: (projectId: string, documentId: string) =>
    del<DocumentDeleteResponse>(
      `/api/project/${projectId}/documents/${documentId}`,
    ),

  // ---- PRD file export (LaTeX -> PDF / DOCX) --------------------------------
  // The generated PRD is LaTeX following template-krungsrinimble.tex, so both
  // exports are COMPILED server-side (PDF via the same Word build, DOCX via
  // the native renderer). The posted source is only a fallback: the backend
  // resolves the LATEST stored document itself (same resolution the preview
  // uses), so both downloads always carry the newest version even if this
  // store copy briefly lagged a live sync. The returned Blob is the finished
  // file — no client-side markdown parsing is involved.
  exportPrdPdf: (projectId: string, latexSource: string, version?: number, projectName?: string) =>
    postBlob(`/api/project/${projectId}/export/pdf`, {
      latex_source: latexSource,
      version,
      project_name: projectName,
    }),
  exportPrdDocx: (projectId: string, latexSource: string, version?: number, projectName?: string) =>
    postBlob(`/api/project/${projectId}/export/docx`, {
      latex_source: latexSource,
      version,
      project_name: projectName,
    }),

  // ---- Requirement Traceability Matrix --------------------------------------
  // Derived (read-only) matrix linking Requirements <-> User Stories <-> Acceptance
  // Criteria <-> PRD sections <-> diagrams. See backend/app/traceability_service.py.
  getTraceability: (projectId: string) =>
    get<TraceabilityPayload>(`/api/project/${projectId}/traceability`),
  getDependencyGraph: (projectId: string) =>
    get<DependencyGraphPayload>(`/api/projects/${projectId}/dependency-graph`),
  planRegeneration: (projectId: string, changedArtifacts: ArtifactReferencePayload[]) =>
    post<RegenerationPlanPayload>(`/api/projects/${projectId}/regeneration/plan`, {
      changed_artifacts: changedArtifacts,
    }),
  executeRegeneration: (projectId: string, changedArtifacts: ArtifactReferencePayload[]) =>
    post<RegenerationResultPayload>(`/api/projects/${projectId}/regeneration`, {
      changed_artifacts: changedArtifacts,
      confirm: true,
    }),
  startRegenerationJob: (projectId: string, changedArtifacts: ArtifactReferencePayload[]) =>
    post<GenerationJobPayload<RegenerationResultPayload>>(`/api/projects/${projectId}/regeneration/background`, {
      changed_artifacts: changedArtifacts,
      confirm: true,
    }),

  // ---- Health --------------------------------------------------------------
  // CHAT 6.3 — Readiness probe (GET /api/health → routes/chat.py get_health_status
  //            → llm_client.check_lm_studio_health). Polled by useLmStudioHealth to
  //            drive the offline pill when the local LLM is down.
  fetchHealth: () => get<HealthPayload>('/api/health'),

  // ---- Authentication (backend/app/routes/auth.py) -------------------------
  // AUTH 1.4 — API-client layer of the LOGIN flow. Called by AUTH 1.3.1; the
  //            payload it returns is what AUTH 1.5 persists. The other AUTH
  //            entry points live in this same block:
  //              authMe       AUTH 2.2  (session restore, GET  /api/auth/me)
  //              authConfig   AUTH 3.2  (signup probe   , GET  /api/auth/config)
  //              authLogout   AUTH 4.2  (audit log      , POST /api/auth/logout)
  //              authRegister AUTH 6.2  (signup         , POST /api/auth/register)
  // Login uses the OAuth2 password flow, so the body is form-encoded
  // (username = email) rather than JSON — `post` alone cannot express that.
  // `withCredentials` is deliberately NOT set anywhere: auth travels in the
  // Authorization header, not cookies (see the CORS notes in backend/app/main.py).
  authLogin: async (email: string, password: string) => {
    const body = new URLSearchParams({ username: email, password });
    // AUTH 1.4.1 — HTTP boundary → FastAPI route AUTH 1.7, which runs the
    //              service AUTH 1.7.1 and answers with the signed JWT (AUTH 1.7.3)
    //              inside LoginResponse (AUTH 1.7.4). A 401 becomes an axios
    //              error, surfaced by AUTH 1.3.1 into AUTH 1.2's inline error.
    const { data } = await axios.post<AuthSessionPayload>('/api/auth/login', body);
    return data;
  },
  // AUTH 6.2 — API-client layer of REGISTER → route AUTH 6.3. The 201 response
  //            contains no token (AUTH 6.3.6), so AUTH 6.4 immediately re-enters
  //            the LOGIN flow.
  authRegister: (payload: AuthRegisterRequest) =>
    post<AuthRegisterResponse>('/api/auth/register', payload),
  // AUTH 3.2 — Unauthenticated capabilities probe → route AUTH 3.3. The token is
  //            deliberately NOT attached: this must work before login. Result
  //            gates the sign-up tab of AUTH 1.1.1.
  /** Unauthenticated capabilities probe (signup_enabled, token TTL). */
  authConfig: () => get<AuthConfigPayload>('/api/auth/config'),
  // AUTH 4.2 — API-client layer of LOGOUT → route AUTH 4.4, which only writes an
  //            audit log (the JWT is stateless). Token passed explicitly because
  //            this helper must not depend on the axios default of AUTH 1.5.1.
  /** Server-side logout (audit log). JWT is stateless, so also clear client state. */
  authLogout: (token: string) =>
    post<AuthMessageResponse>('/api/auth/logout', undefined, undefined, undefined, token),
  /** Permanently delete the authenticated account after password re-verification. */
  authDeleteAccount: async (password: string, confirmation: string, token: string) => {
    const { data } = await axios.delete<AuthAccountDeleteResponse>('/api/auth/account', {
      data: { password, confirmation },
      headers: bearerHeaders(token),
    });
    return data;
  },
  // AUTH 2.2 — API-client layer of SESSION RESTORE → route AUTH 2.3, where the
  //            Bearer token is re-resolved via AUTH 7.1 (verify + users-table
  //            read). A 401 here is AUTH 2.4 (clear the stale session).
  /** Resolves the token's owner straight from the database — also a validity probe. */
  authMe: (token: string) =>
    get<AuthUserPayload>('/api/auth/me', undefined, token),
};
