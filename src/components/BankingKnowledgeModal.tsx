import { useEffect, useState } from 'react';
import { BookOpenCheck, CheckCircle2, FileUp, Loader2, ShieldCheck, Trash2, X } from 'lucide-react';
import { api } from '../api/client';
import type { BankingKnowledgeDocumentPayload } from '../api/types';
import { handleError, notify } from './Toast';

interface BankingKnowledgeModalProps {
  open: boolean;
  onClose: () => void;
}

export function BankingKnowledgeModal({ open, onClose }: BankingKnowledgeModalProps) {
  const [documents, setDocuments] = useState<BankingKnowledgeDocumentPayload[]>([]);
  const [loading, setLoading] = useState(false);
  const [workingId, setWorkingId] = useState<string | null>(null);
  const [file, setFile] = useState<File | null>(null);

  const load = async () => {
    setLoading(true);
    try {
      setDocuments(await api.listBankingKnowledge());
    } catch (error) {
      handleError('Could not load reusable banking knowledge.', error);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (open) void load();
  }, [open]);

  if (!open) return null;

  const upload = async () => {
    if (!file) return;
    setWorkingId('upload');
    try {
      const created = await api.uploadBankingKnowledge(file, {
        title: (file.name.replace(/\.[^.]+$/, '') || file.name).slice(0, 255),
        document_type: 'reference',
        jurisdiction: 'global',
        tags: '',
      });
      setDocuments([created]);
      setFile(null);
      notify('Shared knowledge replaced. Approve it before audits can retrieve it.', 'success');
    } catch (error) {
      handleError('Could not upload the banking knowledge document.', error);
    } finally {
      setWorkingId(null);
    }
  };

  const clearKnowledge = async () => {
    if (!window.confirm('Clear the shared banking knowledge file for all of your projects?')) return;
    setWorkingId('clear');
    try {
      await api.clearBankingKnowledge();
      setDocuments([]);
      setFile(null);
      notify('Shared banking knowledge cleared.', 'success');
    } catch (error) {
      handleError('Could not clear shared banking knowledge.', error);
    } finally {
      setWorkingId(null);
    }
  };

  const changeStatus = async (document: BankingKnowledgeDocumentPayload, action: 'approve' | 'retire') => {
    setWorkingId(document.id);
    try {
      const updated = action === 'approve'
        ? await api.approveBankingKnowledge(document.id)
        : await api.retireBankingKnowledge(document.id);
      setDocuments((current) => current.map((item) => item.id === updated.id ? updated : item));
      notify(action === 'approve' ? 'Knowledge approved for Banking RAG.' : 'Knowledge retired from Banking RAG.', 'success');
    } catch (error) {
      handleError(`Could not ${action} the banking knowledge document.`, error);
    } finally {
      setWorkingId(null);
    }
  };

  return (
    <div className="fixed inset-0 z-[70] flex items-center justify-center bg-black/45 p-4" role="dialog" aria-modal="true">
      <div className="flex max-h-[88vh] w-full max-w-3xl flex-col rounded-2xl border border-outline bg-surface shadow-xl">
        <div className="flex items-start justify-between border-b border-outline px-5 py-4">
          <div>
            <h2 className="flex items-center gap-2 font-bold text-on-surface"><BookOpenCheck className="h-4.5 w-4.5 text-primary" /> Banking Knowledge Base</h2>
            <p className="mt-1 text-xs text-on-surface-variant">One approved file is reused across your projects. Uploading another file replaces it.</p>
          </div>
          <button type="button" aria-label="Close banking knowledge" onClick={onClose}><X className="h-4 w-4" /></button>
        </div>

        <div className="overflow-y-auto p-5 space-y-5">
          <div className="rounded-xl border border-outline bg-slate-50 p-4 space-y-3">
            <div className="flex items-center gap-2 text-sm font-bold text-on-surface"><FileUp className="h-4 w-4 text-primary" /> Shared knowledge file</div>
            <p className="text-xs text-on-surface-variant">Select one reference file. A new upload replaces the current file.</p>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <input
                id="banking-knowledge-file"
                type="file"
                accept=".pdf,.docx,.md,.txt"
                onChange={(event) => {
                  const next = event.target.files?.[0] ?? null;
                  setFile(next);
                }}
                className="sr-only"
              />
              <div className="flex min-w-0 flex-1 items-center gap-2">
                <label
                  htmlFor="banking-knowledge-file"
                  className="inline-flex shrink-0 cursor-pointer items-center gap-1.5 rounded-xl border border-primary bg-white px-3 py-2 text-xs font-bold text-primary hover:bg-primary/5"
                >
                  <FileUp className="h-3.5 w-3.5" /> Select file
                </label>
                <span className="truncate text-xs text-on-surface-variant">
                  {file ? file.name : 'No file selected · PDF, DOCX, Markdown, or TXT'}
                </span>
              </div>
              <div className="flex flex-wrap gap-2">
                <button type="button" disabled={documents.length === 0 || workingId !== null} onClick={() => void clearKnowledge()} className="inline-flex items-center gap-1.5 rounded-xl border border-red-200 px-3 py-2 text-xs font-bold text-red-700 disabled:opacity-50">
                  {workingId === 'clear' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Trash2 className="h-3.5 w-3.5" />} Clear
                </button>
                <button type="button" disabled={!file || workingId !== null} onClick={() => void upload()} className="inline-flex items-center gap-1.5 rounded-xl bg-primary px-4 py-2 text-xs font-bold text-on-primary disabled:opacity-50">
                  {workingId === 'upload' && <Loader2 className="h-3.5 w-3.5 animate-spin" />} {documents.length ? 'Replace file' : 'Upload file'}
                </button>
              </div>
            </div>
          </div>

          <div>
            <h3 className="text-sm font-bold text-on-surface">Current shared file</h3>
            {loading ? (
              <div className="flex items-center gap-2 py-6 text-sm text-on-surface-variant"><Loader2 className="h-4 w-4 animate-spin" /> Loading…</div>
            ) : documents.length === 0 ? (
              <p className="py-6 text-sm text-on-surface-variant">No shared banking knowledge file yet.</p>
            ) : (
              <div className="mt-2 space-y-2">
                {documents.map((document) => (
                  <div key={document.id} className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-outline p-3">
                    <div className="min-w-0">
                      <p className="truncate text-sm font-semibold text-on-surface">{document.title}</p>
                      <div className="mt-1 flex flex-wrap gap-1">
                        <span className={`rounded-full px-2 py-0.5 text-[10px] font-bold uppercase ${document.status === 'approved' ? 'bg-emerald-50 text-emerald-700' : document.status === 'retired' ? 'bg-slate-100 text-slate-600' : 'bg-amber-50 text-amber-700'}`}>{document.status}</span>
                      </div>
                    </div>
                    <div className="flex gap-2">
                      {document.status !== 'approved' && (
                        <button type="button" disabled={workingId === document.id} onClick={() => void changeStatus(document, 'approve')} className="inline-flex items-center gap-1 rounded-lg border border-emerald-200 px-2.5 py-1.5 text-xs font-semibold text-emerald-700 disabled:opacity-50"><CheckCircle2 className="h-3.5 w-3.5" /> Approve</button>
                      )}
                      {document.status === 'approved' && (
                        <button type="button" disabled={workingId === document.id} onClick={() => void changeStatus(document, 'retire')} className="inline-flex items-center gap-1 rounded-lg border border-outline px-2.5 py-1.5 text-xs font-semibold text-on-surface-variant disabled:opacity-50"><ShieldCheck className="h-3.5 w-3.5" /> Retire</button>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
