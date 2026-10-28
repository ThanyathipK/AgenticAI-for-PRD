// DocumentLibrary — uploaded-document knowledge base panel.
//
// DOC-UPLOAD 1.2–1.5 — Knowledge-base UI (state lives in useDocuments 2.x):
// - Explicit extraction shows mode/chunk progress and, when ready, hands the merged
//   proposal to CONFIRM. Tracking status/transcript/draft writes happen before approval;
//   proposed requirement fields do not.
// - Per-document "Remove" action permanently deletes the stored document (and
//   any DRAFT merge-preview staged from it, application step 5.2) after a destructive-style
//   confirm.
// - Markdown preview uses the shared MarkdownRenderer.
// - Note: new documents are uploaded from the ChatPanel paperclip attach (1.1) — this
//   panel only lists/processes/removes/previews documents in the knowledge base.
import React, { useState } from 'react';
import { FileText, Loader2, Trash2 } from 'lucide-react';
import { api } from '../api/client';
import type {
  DocumentMarkdownPayload,
  UploadedDocumentPayload,
} from '../api/types';
import { parseAndRenderMarkdown } from './MarkdownRenderer';
import { handleError } from './Toast';
import { ConfirmModal } from './ConfirmModal';
import { Tooltip } from './Tooltip';
import type { UseDocumentsResult } from '../hooks/useDocuments';

interface DocumentLibraryProps {
  projectId: string | null;
  docs: UseDocumentsResult;
}

const formatBytes = (bytes: number): string => {
  if (!bytes) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  const idx = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
  return `${(bytes / Math.pow(1024, idx)).toFixed(idx === 0 ? 0 : 1)} ${units[idx]}`;
};

// DOC-UPLOAD 1.2 — Extraction chip maps repository 7.5 status. `extraction_pending`
//             means either processing is active or a draft awaits CONFIRM; this UI uses
//             `processingDocId` separately to show the active Extracting state.
const extractionChip = (status: string): { label: string; className: string } => {
  switch (status) {
    case 'extraction_applied':
      return { label: 'extracted', className: 'bg-emerald-100 text-emerald-800 border-emerald-300' };
    case 'extraction_pending':
      return { label: 'draft awaiting confirm', className: 'bg-amber-100 text-amber-800 border-amber-300' };
    default:
      return { label: 'knowledge only', className: 'bg-slate-100 text-slate-600 border-slate-200' };
  }
};
export const DocumentLibrary: React.FC<DocumentLibraryProps> = ({ projectId, docs }) => {
  const [previewDoc, setPreviewDoc] = useState<DocumentMarkdownPayload | null>(null);
  const [isLoadingPreview, setIsLoadingPreview] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<UploadedDocumentPayload | null>(null);

  // DOC-UPLOAD 1.3 — Preview handler: reads the FULL stored markdown (DOC-UPLOAD 3.3 → 4.3) and
  //             renders it with the shared MarkdownRenderer. This is the only way to see a
  //             document's converted content — the list response omits the body (7.2/7.6).
  const openPreview = async (doc: UploadedDocumentPayload): Promise<void> => {
    if (!projectId) return;
    setIsLoadingPreview(true);
    try {
      setPreviewDoc(await api.getDocumentMarkdown(projectId, doc.id));
    } catch (err) {
      handleError('Could not load the stored markdown preview.', err);
    } finally {
      setIsLoadingPreview(false);
    }
  };

  if (!projectId) {
    return (
      <div className="max-w-4xl mx-auto text-sm text-on-surface-variant">
        Create or select a project to view its knowledge base documents.
      </div>
    );
  }

  return (
    <div className="max-w-4xl mx-auto space-y-3 mt-3">
      {/* KNOWLEDGE BASE HEADER */}
      <section className="bg-white border border-outline rounded-[10px] p-4">
        <div className="flex items-start gap-4 flex-wrap">
          <div>
            <h3 className="font-bold text-sm text-on-surface">Knowledge Base Documents</h3>
            <p className="text-xs text-on-surface-variant mt-1">
              Documents in this project's knowledge base. Requirements are never
              written until you explicitly run an extraction and confirm its merge preview.
            </p>
          </div>
        </div>
      </section>

      {/* DOCUMENT LIST — DOC-UPLOAD 1.2: filename opens the markdown preview (1.3), the chip
          shows extraction state, and the Process button is disabled unless the document is
          `processed` (a needs-OCR `failed` row can never be extracted — matches the backend
          guard in DOC-UPLOAD 4.5). */}
      <section className="space-y-2">
        {!docs.isLoadingDocuments && docs.documents.length === 0 && (
          <p className="text-xs text-on-surface-variant px-1">No documents uploaded yet.</p>
        )}
        {docs.isLoadingDocuments && (
          <p className="text-xs text-on-surface-variant px-1">Loading documents…</p>
        )}
        {docs.documents.map(doc => {
          const chip = extractionChip(doc.extraction_status);
          const isProcessing = docs.processingDocId === doc.id;
          return (
            <article key={doc.id} className="bg-white border border-outline rounded-[10px] p-3">
              <div className="flex items-center gap-3 flex-wrap">
                <span className="w-9 h-9 rounded-xl bg-primary/10 text-primary flex items-center justify-center shrink-0">
                  <FileText className="w-4 h-4" />
                </span>
                <div className="min-w-0 flex-1">
                  <Tooltip label="Click to preview document" side="bottom" align="start" className="max-w-full">
                    <button
                      onClick={() => void openPreview(doc)}
                      className="group/preview block max-w-full cursor-pointer rounded text-left text-sm font-semibold text-on-surface transition-colors hover:text-primary focus-visible:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
                      aria-label={`Preview ${doc.original_filename}`}
                    >
                      <span className="truncate group-hover/preview:underline group-focus-visible/preview:underline">
                        {doc.original_filename}
                      </span>
                    </button>
                  </Tooltip>
                  <p className="text-[10px] font-mono text-on-surface-variant mt-0.5">
                    {doc.original_format.toUpperCase()}, {formatBytes(doc.file_size_bytes)}, ~{doc.token_count} tokens, status {doc.status}
                  </p>
                </div>
                <span className={`text-[10px] px-2 py-1 rounded-full border font-bold ${chip.className}`}>
                  {chip.label}
                </span>
                {isProcessing ? (
                  <span className="px-3 py-1.5 rounded-xl bg-primary/5 text-primary border border-primary/20 text-[11px] font-bold flex items-center gap-1.5 whitespace-nowrap">
                    <Loader2 className="w-3.5 h-3.5 animate-spin" />
                    <span>Extracting…</span>
                  </span>
                ) : (
                  <button
                    onClick={() => void docs.handleProcessDocument(doc)}
                    disabled={doc.status !== 'processed'}
                    className="px-3 py-1.5 rounded-xl bg-primary/10 text-primary border border-primary/20 hover:bg-primary/20 text-[11px] font-bold transition-all flex items-center gap-1.5 cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed whitespace-nowrap"
                    title="Extract requirements as a DRAFT merge preview (nothing is written until you confirm)"
                  >
                    <span>Extract Requirements</span>
                  </button>
                )}
                <button
                  onClick={() => setDeleteTarget(doc)}
                  className="px-3 py-1.5 rounded-xl bg-red-50 text-red-700 border border-red-200 hover:bg-red-100 text-[11px] font-bold transition-all flex items-center gap-1.5 cursor-pointer whitespace-nowrap"
                  title="Remove this document from the project's knowledge base"
                >
                  <Trash2 className="w-3.5 h-3.5" />
                  <span>Remove</span>
                </button>
              </div>

              {/* DOC-UPLOAD 1.4 — Extraction action/progress. The user-facing “nothing is
                  saved” wording means no proposed requirement fields are applied;
                  tracking status and transcript milestones are persisted separately. */}
              {isProcessing && docs.extractionProgress && (
                <div className="mt-3 text-[11px] text-primary bg-primary/5 border border-primary/20 rounded-xl px-3 py-2 font-mono">
                  Running extraction in <b>{docs.extractionProgress.mode}</b> mode
                  {docs.extractionProgress.chunkCount > 0 &&
                    `, chunk ${Math.max(1, docs.extractionProgress.chunkIndex)}/${docs.extractionProgress.chunkCount}`}
                  ; nothing is saved until you confirm the draft.
                </div>
              )}
              {doc.status === 'failed' && doc.message && (
                <div className="mt-3 text-[11px] text-red-700 bg-red-50 border border-red-200 rounded-xl px-3 py-2">
                  {doc.message}
                </div>
              )}
            </article>
          );
        })}
      </section>

      {/* MARKDOWN PREVIEW — full canonical content from the knowledge store */}
      {(isLoadingPreview || previewDoc) && (
        <section className="bg-white border border-outline rounded-[10px] p-4">
          <div className="flex flex-wrap items-center justify-between gap-2 mb-3">
            <h4 className="font-bold text-xs text-on-surface min-w-0 flex-1 break-words">
              {previewDoc?.original_filename ?? 'Loading…'}, stored markdown (~{previewDoc?.token_count ?? '…'} tokens)
            </h4>
            <button
              onClick={() => setPreviewDoc(null)}
              className="shrink-0 text-[11px] text-on-surface-variant hover:text-primary cursor-pointer"
            >
              Close
            </button>
          </div>
          {isLoadingPreview ? (
            <Loader2 className="w-5 h-5 animate-spin text-primary" />
          ) : (
            <div className="max-h-96 overflow-y-auto custom-scrollbar pr-2">
              {parseAndRenderMarkdown(previewDoc?.content_markdown ?? '')}
            </div>
          )}
        </section>
      )}

      {/* REMOVE CONFIRMATION — destructive, requires explicit confirmation.
          DOC-UPLOAD 1.5 — Remove goes through ConfirmModal; on success the local markdown
          preview is dropped too (the stored body is gone with the document). Requirements
          already extracted from it are explicitly NOT affected — stated in the modal copy,
          matching the backend contract at DOC-UPLOAD 4.4. */}
      <ConfirmModal
        isOpen={deleteTarget !== null}
        title="Remove document?"
        description={`'${deleteTarget?.original_filename ?? 'This document'}' will be permanently removed from this project's knowledge base. Any requirements you already extracted from it are NOT affected.`}
        confirmLabel="Remove"
        pendingLabel="Removing..."
        danger
        onConfirm={async () => {
          if (deleteTarget === null) return false;
          const ok = await docs.handleDeleteDocument(deleteTarget);
          // The stored markdown preview is gone with the document.
          if (ok && previewDoc && previewDoc.id === deleteTarget.id) {
            setPreviewDoc(null);
          }
          return ok;
        }}
        onClose={() => setDeleteTarget(null)}
      />
    </div>
  );
};
