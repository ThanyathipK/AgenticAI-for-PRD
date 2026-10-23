import { useEffect, useRef, useState } from 'react';
import { AlertTriangle, Loader2 } from 'lucide-react';
import { useModalBehavior } from '../hooks/useModalBehavior';

interface DeleteAccountModalProps {
  isOpen: boolean;
  isWorking: boolean;
  onClose: () => void;
  onDelete: (password: string, confirmation: string) => Promise<string | null>;
}

export function DeleteAccountModal({
  isOpen,
  isWorking,
  onClose,
  onDelete,
}: DeleteAccountModalProps) {
  const [password, setPassword] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [error, setError] = useState<string | null>(null);
  const passwordRef = useRef<HTMLInputElement>(null);
  const { dialogRef } = useModalBehavior({ isOpen, onClose, dismissible: !isWorking });

  useEffect(() => {
    if (!isOpen) return;
    setPassword('');
    setConfirmation('');
    setError(null);
    const timer = setTimeout(() => passwordRef.current?.focus(), 30);
    return () => clearTimeout(timer);
  }, [isOpen]);

  if (!isOpen) return null;
  const canDelete = Boolean(password) && confirmation === 'DELETE' && !isWorking;

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!canDelete) return;
    setError(null);
    const failure = await onDelete(password, confirmation);
    if (failure) setError(failure);
    else onClose();
  };

  return (
    <div
      className="fixed inset-0 z-[80] flex items-center justify-center bg-black/45 p-4"
      onClick={() => { if (!isWorking) onClose(); }}
    >
      <div
        ref={dialogRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-labelledby="delete-account-title"
        onClick={event => event.stopPropagation()}
        className="w-full max-w-md rounded-2xl border border-red-200 bg-white p-6 shadow-2xl focus:outline-none"
      >
      <form onSubmit={submit}>
        <div className="flex items-start gap-3">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-red-50 text-red-600">
            <AlertTriangle className="h-5 w-5" />
          </div>
          <div>
            <h2 id="delete-account-title" className="text-lg font-bold text-on-surface">Delete account permanently</h2>
            <p className="mt-1 text-sm leading-relaxed text-on-surface-variant">
              This deletes your projects, requirements, uploaded documents, conversations, PRDs, and account-owned knowledge. This action cannot be undone.
            </p>
          </div>
        </div>

        <label className="mt-5 block text-xs font-semibold text-on-surface">
          Current password
          <input
            ref={passwordRef}
            type="password"
            value={password}
            onChange={event => setPassword(event.target.value)}
            autoComplete="current-password"
            disabled={isWorking}
            className="mt-1.5 w-full rounded-xl border border-outline px-3 py-2.5 text-sm outline-none focus:border-red-400 focus:ring-1 focus:ring-red-200 disabled:bg-slate-50"
          />
        </label>

        <label className="mt-4 block text-xs font-semibold text-on-surface">
          Type <span className="font-mono text-red-700">DELETE</span> to confirm
          <input
            type="text"
            value={confirmation}
            onChange={event => setConfirmation(event.target.value)}
            autoComplete="off"
            disabled={isWorking}
            className="mt-1.5 w-full rounded-xl border border-outline px-3 py-2.5 font-mono text-sm outline-none focus:border-red-400 focus:ring-1 focus:ring-red-200 disabled:bg-slate-50"
          />
        </label>

        {error && <p className="mt-3 rounded-lg bg-red-50 px-3 py-2 text-xs text-red-700" role="alert">{error}</p>}

        <div className="mt-6 flex justify-end gap-2">
          <button type="button" onClick={onClose} disabled={isWorking} className="rounded-xl border border-outline px-4 py-2 text-xs font-semibold text-on-surface-variant hover:bg-slate-50 disabled:opacity-50">
            Cancel
          </button>
          <button type="submit" disabled={!canDelete} className="flex items-center gap-1.5 rounded-xl bg-red-600 px-4 py-2 text-xs font-bold text-white hover:bg-red-700 disabled:cursor-not-allowed disabled:opacity-40">
            {isWorking && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
            {isWorking ? 'Deleting account…' : 'Delete account'}
          </button>
        </div>
      </form>
      </div>
    </div>
  );
}
