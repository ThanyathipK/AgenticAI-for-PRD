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
// AUTH 1.5.1 — Synchronize the JWT with the shared HTTP transport.
// Trigger: AUTH 1.5 login persistence, AUTH 2.1 restoration, or AUTH 4.3 clear.
// Input: JWT string or null.
// Writes: axios default Authorization header only.
// Branches: token → Bearer header; null → remove header.
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

// PRD-EXPORT 3.0 — Binary client boundary: preserve the server's authoritative
// filename and body. PRD exports encode snapshot SemVer in that header.
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

// PRD-EXPORT 3.0.1 — Error bodies from binary endpoints arrive as a Blob, so the FastAPI
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
  // PRD-SECTION 3.1 — List ordered parts (route 4.1), optionally materializing them.
  getPrdSections: (projectId: string, materialize = false) =>
    get<PrdSectionListPayload>(`/api/project/${projectId}/prd/sections`, { materialize }),
  // ---- PRD sections (parts) ----------------------------------------------
  // PRD-SECTION 3.0 — Client boundary for the nine-part document (routes 4.x).
  // NOT present (verified): per-part revert and section-history methods for routes
  // 4.3/4.7. Those endpoints remain API/test-only and are unreachable from the SPA.
  // PRD-SECTION 3.2 — Part save (route 4.2): `baseContent` is the text the editor
  //               started from, which is what enables the server-side three-way merge
  //               (6.5) instead of a blind overwrite.
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
  // PRD-SECTION 3.3 — Per-part lock (route 4.4); `lockedBy` is the lock owner label.
  lockPrdSection: (projectId: string, sectionKey: string, lockedBy = 'user') =>
    post(`/api/project/${projectId}/prd/sections/${sectionKey}/lock`, { locked_by: lockedBy }),
  // PRD-SECTION 3.4 — Per-part unlock (route 4.5): re-enables editing and AI
  //               regeneration for that part.
  unlockPrdSection: (projectId: string, sectionKey: string, lockedBy = 'user') =>
    post(`/api/project/${projectId}/prd/sections/${sectionKey}/unlock`, { locked_by: lockedBy }),

  // ---- PRD version ledger ------------------------------------------------
  // VERSION 3.0 — API client boundary for the PRD snapshot ledger (routes 4.x).
  // VERSION 3.1 — Paged metadata read, newest first (route 4.1). Project opening asks
  //               for one row; the Versions tab asks for 20 and uses
  //               beforeVersion to append older pages. The backend deliberately
  //               blanks generated_prd in this response; toVersionHistoryList
  //               maps only the timeline metadata.
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
  // VERSION 3.2 — Diff read (route 4.2): the panel automatically supplies the preceding
  //               loaded version. If that row is not loaded, omission makes the
  //               backend resolve the true preceding ledger row (6.8/7.2).
  getPrdVersionDiff: (projectId: string, versionNumber: number, baseVersion?: number) =>
    get<PrdVersionDiffPayload>(
      `/api/project/${projectId}/prd-versions/${versionNumber}/diff`,
      baseVersion ? { base_version: baseVersion } : undefined,
    ),
  // VERSION 3.3 — Restore request (route 4.5). The route copies target sections that
  //               exist and are currently unlocked, preserves locked/current-only
  //               sections, and normally appends a new ledger row. Ledger-record
  //               failure is caught by the route, so new_version_number can fall
  //               back to the source number even though the document was restored.
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
  // CONFIRM 2.0 — Client boundary for the human-in-the-loop gate. The pending row
  //               was staged earlier; 2.3 applies its proposed authoritative state,
  //               2.4 deletes it, and 2.2 may prune it when already expired.
  // CONFIRM 2.1 — read: staged actions for a project (CONFIRM 3.1, WAITING_CONFIRMATION
  //               only). Also drives CONFIRM 1.7.
  listPendingActions: (projectId: string) =>
    get<PendingActionPayload[]>(`/api/pending-actions/${projectId}`),
  listConversationMessages: (projectId: string, options?: { limit?: number; before?: string }) =>
    get<ConversationMessagePayload[]>(`/api/project/${projectId}/conversations`, options),
  appendConversationMessage: (projectId: string, message: ConversationMessageCreatePayload) =>
    post<ConversationMessagePayload>(`/api/project/${projectId}/conversations`, message),
  // CONFIRM 2.3 — Save call: submits the staged action to CONFIRM 3.3, whose action
  //               type selects the requirement-state or generated-PRD commit branch.
  confirmAction: (actionId: string, projectId: string) =>
    post<ConfirmActionResponse>(`/api/confirm-action/${actionId}`, null, { project_id: projectId }),
  // CONFIRM 2.4 — Discard call (CONFIRM 3.4): deletes the action without applying
  //               its proposal. Document-extraction drafts also reset source status.
  cancelAction: (actionId: string, projectId: string) =>
    post<ActionStatusResponse>(`/api/cancel-action/${actionId}`, null, { project_id: projectId }),
  // IMPACT 3.1 — GET one pending action's predicted changes and downstream references
  // from route 4.1. Successful analysis is read-only.
  getActionImpact: (actionId: string, projectId: string) =>
    get<ImpactAnalysisPayload>(`/api/pending-actions/${actionId}/impact`, { project_id: projectId }),

  // ---- Artifact locks -----------------------------------------------------
  // LOCK 3.1 — Send generic requirement/PRD-section lock mutations.
  // Trigger: LOCK 2.2/2.3 optimistic handlers.
  // Input: project/type/artifact UUID plus caller-supplied owner label.
  // Calls: LOCK 4.1 lock or LOCK 4.2 unlock route.
  // Writes: NONE locally; transport only.
  // Failure: propagates so the hook rolls back optimistic state.
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
  // CHAT 1.3 — Send a message through the product's shared processing endpoint.
  // Trigger: CHAT 1.2 useChat.handleSendMessage or an explicit agent action.
  // Input: process payload and optional AbortSignal.
  // Calls: CHAT 2.1 POST /api/process-requirements.
  // Branches: backend chooses CHAT 2.3 or the LangGraph agent path.
  // Failure: transport/cancellation errors propagate to the calling hook.
  // Note: the SPA does not call CHAT 5.1 POST /api/chat.
  processRequirements: (request: ProcessRequirementsRequest, signal?: AbortSignal) =>
    post<ProcessRequirementsResponse>('/api/process-requirements', request, undefined, signal),
  // JOB 3.1 — Create durable Architect/Auditor work through JOB 4.1.
  // Trigger: JOB 2.1 background runner.
  // Writes: NONE locally; backend persists the queued job.
  // Failure: HTTP errors propagate to the runner.
  startAgentJob: (request: ProcessRequirementsRequest) =>
    post<GenerationJobPayload<ProcessRequirementsResponse>>('/api/process-requirements/background', request),
  // JOB 3.2 — Poll project-scoped status/result through JOB 4.3.
  getGenerationJob: <T>(projectId: string, jobId: string) =>
    get<GenerationJobPayload<T>>(`/api/projects/${projectId}/generation-jobs/${jobId}`),
  // JOB 3.3 — Request durable cancellation through JOB 4.4.
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
  // JOB 3.4 — Request direct-workflow cancellation through JOB 4.5.
  // Trigger: JOB 2.2 Stop when no durable job id is known.
  cancelProcessRequirements: (projectId: string) =>
    post<{ project_id: string; cancelled: boolean }>(
      '/api/process-requirements/cancel',
      null,
      { project_id: projectId },
    ),

  // ---- Clarifications ------------------------------------------------------
  // CLARIFY 2.0 — Client boundary for clarification-answer submission.
  // CLARIFY 2.1 — POST /api/clarification/submit (CLARIFY 3.0). `answers` normally
  //               maps stable question UUIDs to text; legacy q-<index> keys are accepted.
  submitClarifications: (projectId: string, answers: Record<string, string>) =>
    post<RequirementStatePayload>('/api/clarification/submit', {
      project_id: projectId,
      answers,
    }),

  // ---- Uploaded documents (knowledge base) ---------------------------------
  // DOC-UPLOAD 3.0 — HTTP client boundary for backend routes 4.1–4.6. Upload stores
  //               converted knowledge; processDocument explicitly stages a draft.
  // DOC-UPLOAD 3.1 — Upload (DOC-UPLOAD 4.1); multipart FormData is the
  //               only request in this client that does not send JSON.
  uploadDocument: (projectId: string, file: File) => {
    const formData = new FormData();
    formData.append('file', file);
    return postFormData<UploadedDocumentPayload>(
      `/api/project/${projectId}/documents/upload`,
      formData,
    );
  },
  // DOC-UPLOAD 3.2 — List (DOC-UPLOAD 4.2): metadata only.
  listDocuments: (projectId: string) =>
    get<UploadedDocumentPayload[]>(`/api/project/${projectId}/documents`),
  // DOC-UPLOAD 3.3 — Detail (DOC-UPLOAD 4.3): full stored markdown for preview.
  getDocumentMarkdown: (projectId: string, documentId: string) =>
    get<DocumentMarkdownPayload>(`/api/project/${projectId}/documents/${documentId}`),
  // DOC-UPLOAD 3.4 — Explicit extraction (DOC-UPLOAD 4.5). It updates extraction tracking and
  //               transcript state, then returns a pending requirement proposal.
  processDocument: (projectId: string, documentId: string, signal?: AbortSignal) =>
    post<ProcessDocumentResponse>(
      `/api/project/${projectId}/documents/${documentId}/process`,
      undefined,
      undefined,
      signal,
    ),
  // DOC-UPLOAD 3.5 / JOB 3.5 — Signal extraction cancellation through JOB 4.6.
  cancelDocumentExtraction: (projectId: string, documentId: string) =>
    post<{ project_id: string; document_id: string; cancelled: boolean }>(
      `/api/project/${projectId}/documents/${documentId}/process/cancel`,
    ),
  // DOC-UPLOAD 3.6 — Permanent removal (DOC-UPLOAD 4.4), which also discards that
  //               document's staged DRAFT server-side (5.2).
  deleteDocument: (projectId: string, documentId: string) =>
    del<DocumentDeleteResponse>(
      `/api/project/${projectId}/documents/${documentId}`,
    ),

  // ---- PRD file export (LaTeX -> PDF / DOCX) --------------------------------
  // POST fallback source/filename metadata and receive completed file bytes.
  // PRD-EXPORT 3.1 — PDF Blob client → route 4.1.
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
  // PRD-EXPORT 3.2 — DOCX Blob client → route 4.2.
  exportPrdDocx: (projectId: string, latexSource: string, version?: number, projectName?: string) =>
    postBlob(`/api/project/${projectId}/export/docx`, {
      latex_source: latexSource,
      version,
      project_name: projectName,
    }),

  // ---- Requirement Traceability Matrix --------------------------------------
  // TRACEABILITY 3.1 — GET the derived, read-only matrix from route 4.1.
  // Links Requirements ↔ Stories ↔ Criteria ↔ PRD sections ↔ diagrams.
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
  // CHAT 6.3.1 — Fetch backend and inference-gateway readiness.
  // Trigger: CHAT 6.3.2 useLmStudioHealth polling/manual retry.
  // Calls: CHAT 6.2.1 GET /api/health → CHAT 6.2 gateway probe.
  // Writes: NONE; returns the structured readiness payload.
  // Failure: transport errors propagate so the hook can mark the backend offline.
  fetchHealth: (force = false) => get<HealthPayload>('/api/health', force ? { force: true } : undefined),

  // ---- Authentication (backend/app/routes/auth.py) -------------------------
  // AUTH 1.4 — Send login credentials to the authentication route.
  // Trigger: AUTH 1.3.1 useAuth.login.
  // Input: email and plaintext password encoded as OAuth2 form data.
  // Calls: AUTH 1.7 POST /api/auth/login.
  // Returns: access token plus public user for AUTH 1.5 persistence.
  // Failure: axios preserves the route's generic 401 for the modal.
  // Other AUTH API boundaries in this block:
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
  // AUTH 8.3 — Send the destructive account-deletion request.
  // Trigger: AUTH 8.2 useAuth.deleteAccount.
  // Input: password, DELETE confirmation, and explicit bearer token.
  // Calls: AUTH 8.4 DELETE /api/auth/account.
  // Writes: NONE in the browser transport layer.
  // Failure: axios preserves the route status/detail for AUTH 8.2.
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
