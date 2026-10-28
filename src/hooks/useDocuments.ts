// useDocuments — uploaded-document (knowledge base) state + handlers.
//
// DOC-UPLOAD 1.0 — Frontend document lifecycle (backend 2.x, repository 4.x).
// Upload adds knowledge only. Explicit extraction (1.7 → 2.5) stages a pending
// merge for CONFIRM; it never applies proposed requirement changes directly.
import { useCallback, useEffect, useRef, useState } from 'react';
import type { Dispatch, SetStateAction } from 'react';
import { api } from '../api/client';
import type { ChatMessage } from '../components/types';
import type {
  PendingActionPayload,
  ProcessDocumentResponse,
  UploadedDocumentPayload,
} from '../api/types';
import { handleError, handleWarning } from '../components/Toast';

export interface DocumentExtractionProgress {
  documentId: string;
  mode: string;
  chunkIndex: number;
  chunkCount: number;
}

export interface DocumentsDeps {
  projectId: string | null;
  pendingActions: PendingActionPayload[];
  setPendingActions: Dispatch<SetStateAction<PendingActionPayload[]>>;
  setMessages: Dispatch<SetStateAction<ChatMessage[]>>;
  setSyncStatus: Dispatch<SetStateAction<string>>;
  setCurrentAgentNode: Dispatch<SetStateAction<string | null>>;
  /**
   * SSE bridge supplied by the composition root: `useProjectSync` forwards
   * `document_extraction_progress` frames here so the progress readout updates
   * LIVE (chunk X/N) during long extractions instead of hanging on the
   * optimistic placeholder set when processing starts.
   */
  registerLiveProgress?: (fn: (p: DocumentExtractionProgress | null) => void) => void;
}

export interface UseDocumentsResult {
  documents: UploadedDocumentPayload[];
  isLoadingDocuments: boolean;
  isUploading: boolean;
  processingDocId: string | null;
  deletingDocId: string | null;
  extractionProgress: DocumentExtractionProgress | null;
  handleUploadDocument: (file: File) => Promise<void>;
  handleProcessDocument: (doc: UploadedDocumentPayload) => Promise<void>;
  handleStopDocumentExtraction: () => Promise<void>;
  /** Permanently removes a document; resolves to true on success. */
  handleDeleteDocument: (doc: UploadedDocumentPayload) => Promise<boolean>;
  refreshDocuments: () => Promise<void>;
}

export function useDocuments(deps: DocumentsDeps): UseDocumentsResult {
  // DOC-UPLOAD 1.4 — Documents state rendered by DocumentLibrary. Note that backend
  //               `extraction_pending` covers both a running extraction and a staged
  //               draft; the row's processing state distinguishes the live run locally.
  const [documents, setDocuments] = useState<UploadedDocumentPayload[]>([]);
  const [isLoadingDocuments, setIsLoadingDocuments] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const [processingDocId, setProcessingDocId] = useState<string | null>(null);
  const [deletingDocId, setDeletingDocId] = useState<string | null>(null);
  const [extractionProgress, setExtractionProgress] = useState<DocumentExtractionProgress | null>(null);
  const prevPendingCount = useRef<number>(0);
  const uploadInFlightRef = useRef(false);
  const extractionAbortRef = useRef<AbortController | null>(null);

  // DOC-UPLOAD 1.3 — Knowledge-base list load (DOC-UPLOAD 2.2). Runs on project switch and
  //             is the reconciliation point after upload / extraction / delete.
  const refreshDocuments = useCallback(async () => {
    if (!deps.projectId) {
      setDocuments([]);
      return;
    }
    setIsLoadingDocuments(true);
    try {
      const docs = await api.listDocuments(deps.projectId);
      setDocuments(docs);
    } catch (err) {
      handleError('Failed to load uploaded documents.', err);
    } finally {
      setIsLoadingDocuments(false);
    }
  }, [deps.projectId]);

  // Reload the knowledge list when switching projects.
  useEffect(() => {
    void refreshDocuments();
  }, [refreshDocuments]);

  // DOC-UPLOAD 1.7.1 — SSE live-progress bridge. `useProjectState` wires a stable setter here that
  // `useProjectSync`'s SSE handler drives with document_extraction_progress
  // frames, so the DocumentLibrary's "chunk X/N" readout advances in real time
  // (the old optimist placeholder stayed at 0/N for the whole run).
  useEffect(() => {
    if (!deps.registerLiveProgress) return;
    deps.registerLiveProgress(setExtractionProgress);
    return () => deps.registerLiveProgress?.(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deps.registerLiveProgress]);

  // DOC-UPLOAD 1.8 — Draft-resolution refresh: when the pending list SHRINKS (the extraction
  //             draft was confirmed or cancelled — CONFIRM/CANCELLATION flows), re-read the
  //             documents so each row's extraction_status chip stays truthful.
  useEffect(() => {
    const before = prevPendingCount.current;
    prevPendingCount.current = deps.pendingActions.length;
    if (deps.pendingActions.length < before) {
      void refreshDocuments();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deps.pendingActions.length]);

  // DOC-UPLOAD 1.2 — Upload handler. POSTs the file (DOC-UPLOAD 2.1), then branches on the
  //             returned status: `failed` (e.g. the 2.1.5 needs-OCR record) surfaces the
  //             server message as an amber notice and the row stays visible with status
  //             'failed'. On success the backend appends one simple upload confirmation
  //             to the durable chat transcript, so SSE refresh restores it after reload.
  const handleUploadDocument = useCallback(async (file: File) => {
    if (!deps.projectId || uploadInFlightRef.current) return;
    uploadInFlightRef.current = true;
    setIsUploading(true);
    try {
      const uploaded = await api.uploadDocument(deps.projectId, file);
      if (uploaded.status === 'processed') {
        // The completed upload persists the confirmation before returning.
        // Read that authoritative row instead of creating a second local copy.
        try {
          const history = await api.listConversationMessages(deps.projectId);
          const expectedContent = `File uploaded: ${uploaded.original_filename}`;
          const persisted = [...history].reverse().find(message =>
            message.intent === 'DOCUMENT_CONVERSION_PROCESSED' &&
            (message.content || message.message) === expectedContent,
          );
          if (persisted) {
            const createdAt = new Date(persisted.created_at);
            deps.setMessages(prev => prev.some(message => message.id === persisted.id)
              ? prev
              : [...prev, {
                  id: persisted.id,
                  role: persisted.role || 'assistant',
                  content: persisted.content || persisted.message || expectedContent,
                  timestamp: Number.isNaN(createdAt.getTime())
                    ? persisted.timestamp || ''
                    : createdAt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
                }]);
          }
        } catch (err) {
          handleWarning('The upload finished, but its chat confirmation could not be refreshed.', err);
        }
      }
      await refreshDocuments();
    } catch (err) {
      handleError('Document upload failed.', err);
    } finally {
      uploadInFlightRef.current = false;
      setIsUploading(false);
    }
    // The dependency object is reconstructed; these members are stable inputs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deps.projectId, deps.setMessages, refreshDocuments]);

  // DOC-UPLOAD 1.7 — Explicit extraction handler: start backend 2.5, display SSE
  //               progress, and mirror its pending-action id into CONFIRM UI state.
  //               The stored knowledge document remains available throughout.
  const handleProcessDocument = useCallback(async (doc: UploadedDocumentPayload) => {
    if (!deps.projectId || processingDocId) return;
    // The backend persists DOCUMENT_REQUIREMENTS_REQUEST before starting the
    // long-running model call and SSE refreshes that canonical transcript row.
    // Do not add an optimistic local copy here or the same request appears twice.
    deps.setSyncStatus(`Gathering requirements from ${doc.original_filename}…`);
    deps.setCurrentAgentNode('gatherer_node');
    setProcessingDocId(doc.id);
    const controller = new AbortController();
    extractionAbortRef.current = controller;
    // Optimistic progress placeholder until the first chunk event lands.
    setExtractionProgress({
      documentId: doc.id,
      mode: doc.token_count <= 0 ? 'full' : 'chunked',
      chunkIndex: 0,
      chunkCount: 0,
    });
    try {
      const res: ProcessDocumentResponse = await api.processDocument(
        deps.projectId,
        doc.id,
        controller.signal,
      );
      if (res.status === 'cancelled') {
        deps.setSyncStatus('Document extraction stopped. No changes were written.');
        return;
      }
      if (res.pending_action_id) {
        // Surface the DRAFT through CONFIRM. The pending row/status/transcript are
        // already durable; only its proposed requirement changes await approval.
        const draft: PendingActionPayload = {
          id: res.pending_action_id,
          project_id: deps.projectId,
          action_type: 'INSERT_CHUNKED_REQUIREMENTS',
          original_user_message:
            `Extract requirements from uploaded document '${doc.original_filename}'.`,
        };
        deps.setPendingActions(prev =>
          prev.some(a => a.id === draft.id) ? prev : [...prev, draft],
        );
        // The backend persists the canonical completion message before publishing
        // document_extraction_draft. The SSE refresh owns transcript updates so a
        // fast refresh racing this response cannot create a duplicate message.
        deps.setSyncStatus('Document requirements gathered. Review the merge preview.');
      }
    } catch (err) {
      if (controller.signal.aborted) {
        deps.setSyncStatus('Document extraction stopped. No changes were written.');
        return;
      }
      handleError('Document extraction failed. No changes were written.', err);
      // Extraction failures are also persisted and delivered through the SSE
      // transcript refresh. The toast above still covers pre-extraction HTTP
      // validation failures for which no durable chat entry is appropriate.
      deps.setSyncStatus('Document extraction failed. No changes were written.');
    } finally {
      if (extractionAbortRef.current === controller) extractionAbortRef.current = null;
      deps.setCurrentAgentNode(null);
      setProcessingDocId(null);
      setExtractionProgress(null);
      await refreshDocuments();
    }
  }, [deps, processingDocId, refreshDocuments]);

  // DOC-UPLOAD 1.7.2 — Stop handler: signal backend 2.6, then abort this browser
  //                 wait. The process route owns status reset and cancellation log.
  const handleStopDocumentExtraction = useCallback(async () => {
    if (!deps.projectId || !processingDocId) return;
    deps.setSyncStatus('Stopping document extraction…');
    try {
      await api.cancelDocumentExtraction(deps.projectId, processingDocId);
      extractionAbortRef.current?.abort();
      deps.setSyncStatus('Document extraction stopped. No changes were written.');
    } catch (err) {
      handleError('Could not stop document extraction.', err);
      deps.setSyncStatus('Document extraction is still running.');
    }
  }, [deps, processingDocId]);

  // DOC-UPLOAD 1.6 — Remove handler (DOC-UPLOAD 2.4). Currently staged drafts from
  //             this document are discarded server-side (2.4.2); the pending
  //             panel evicts it on the next SSE sync / project refresh — this only
  //             re-reads the knowledge list. Returns a boolean so the ConfirmModal can
  //             stay open on failure.
  const handleDeleteDocument = useCallback(async (doc: UploadedDocumentPayload) => {
    if (!deps.projectId || deletingDocId) return false;
    setDeletingDocId(doc.id);
    try {
      await api.deleteDocument(deps.projectId, doc.id);
      // The deleted doc's currently staged pending actions were discarded server-side; the
      // next SSE sync / project refresh will evict it from the pending panel.
      await refreshDocuments();
      return true;
    } catch (err) {
      handleError('Failed to remove the document.', err);
      return false;
    } finally {
      setDeletingDocId(null);
    }
  }, [deps.projectId, deletingDocId, refreshDocuments]);

  return {
    documents,
    isLoadingDocuments,
    isUploading,
    processingDocId,
    deletingDocId,
    extractionProgress,
    handleUploadDocument,
    handleProcessDocument,
    handleStopDocumentExtraction,
    handleDeleteDocument,
    refreshDocuments,
  };
}
