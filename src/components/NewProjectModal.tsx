import { useEffect, useRef, useState } from 'react';
import { Plus } from 'lucide-react';
import { useModalBehavior } from '../hooks/useModalBehavior';

export const STAKEHOLDER_ROLES = ['Product Owner', 'Technical Product Owner', 'Solution Architecture', 'System Analyst', 'Engineering Manager', 'Project Manager', 'Quality Assurance Lead', 'Performance QA Lead'] as const;
export const BUSINESS_TOPICS = ['Introduction & Executive Summary', 'Problem Statement', 'Business Objectives', 'Expected Benefit', 'Success Metrics', 'Target Audience & user Personas'] as const;

export interface ProjectIntake {
  pmoNo: string;
  author: string;
  stakeholders: Record<string, string>;
  businessTopics: Record<string, string>;
  gathererBrief: string;
}

interface NewProjectModalProps {
  isOpen: boolean;
  /**
   * Creates the project. Resolves to null on success (modal closes only then)
   * or to a user-facing error message — e.g. the backend's duplicate-name
   * conflict — which is rendered inline below the input.
   */
  /** When `skipIntake` is true, create an empty project with its required cover details. */
  onCreate: (name: string, intake: ProjectIntake, skipIntake?: boolean) => Promise<string | null>;
  onClose: () => void;
}

// NewProjectModal — styled replacement for the former native prompt() dialog,
// following the app's design language: rounded-2xl card on a blurred overlay,
// outline-border inputs, primary CTA.
export function NewProjectModal({ isOpen, onCreate, onClose }: NewProjectModalProps) {
  const [name, setName] = useState<string>('');
  const [pmoNo, setPmoNo] = useState<string>('');
  const [author, setAuthor] = useState<string>('');
  const [stakeholders, setStakeholders] = useState<Record<string, string>>({});
  const [businessTopics, setBusinessTopics] = useState<Record<string, string>>({});
  const [isCreating, setIsCreating] = useState<boolean>(false);
  // Inline validation error (e.g. "name already exists" from the backend's
  // 409 duplicate check) — cleared as soon as the user edits the input.
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Shared modal behavior: Escape-anywhere-to-close, focus trap, focus restore
  // on close, and background scroll lock. Escape stays disabled while the
  // create request is in flight (same guard as the overlay click below).
  const { dialogRef } = useModalBehavior({
    isOpen,
    onClose: () => {
      if (!isCreating) onClose();
    },
  });

  // Fresh, focused input every time the modal opens.
  useEffect(() => {
    if (!isOpen) return;
    setName('');
    setPmoNo('');
    setAuthor('');
    setStakeholders({});
    setBusinessTopics({});
    setIsCreating(false);
    setError(null);
    const timer = setTimeout(() => inputRef.current?.focus(), 30);
    return () => clearTimeout(timer);
  }, [isOpen]);

  if (!isOpen) return null;

  const handleSubmit = async (skipIntake = false) => {
    const trimmed = name.trim();
    if (!trimmed || isCreating) return;
    if (!skipIntake && (!pmoNo.trim() || !author.trim())) {
      setError('PMO No. and Author are required.');
      return;
    }
    setIsCreating(true);
    setError(null);
    try {
      const gathererBrief = [
        `Initial project intake for ${trimmed}.`,
        pmoNo.trim() && `PMO No.: ${pmoNo.trim()}`,
        author.trim() && `Author: ${author.trim()}`,
        ...STAKEHOLDER_ROLES.map(role => stakeholders[role]?.trim() && `${role}: ${stakeholders[role].trim()}`),
        ...BUSINESS_TOPICS.map(topic => businessTopics[topic]?.trim() && `${topic}: ${businessTopics[topic].trim()}`),
        'Create the initial requirements from this intake. Use the stated stakeholders as actors and do not invent scope that is not supported by this brief.',
      ].filter(Boolean).join('\n\n');
      const failure = await onCreate(
        trimmed,
        { pmoNo: pmoNo.trim(), author: author.trim(), stakeholders, businessTopics, gathererBrief },
        skipIntake,
      );
      if (failure === null) {
        onClose();
      } else {
        setError(failure);
      }
    } finally {
      setIsCreating(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/20"
      onClick={() => { if (!isCreating) onClose(); }}
    >
      <div
        ref={dialogRef}
        tabIndex={-1}
        className="w-[700px] max-w-[calc(100vw-1.5rem)] max-h-[calc(100vh-2rem)] overflow-y-auto bg-white border border-outline rounded-[16px] shadow-[0_12px_28px_rgba(43,36,32,0.14)] p-6 focus:outline-none"
        role="dialog"
        aria-modal="true"
        aria-label="Create new project"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2.5 mb-5">
          <div className="min-w-0 flex-1">
            <h3 className="text-[18px] font-bold text-on-surface">New Project</h3>
            <p className="mt-1 text-xs text-on-surface-variant">Add an intake for a first requirements draft, or create an empty project with its name only.</p>
          </div>
        </div>

        <label className="block text-xs font-semibold text-on-surface">
          Project Name <span className="font-normal text-red-600">(Required)</span>
          <input
            ref={inputRef}
            type="text"
            value={name}
            onChange={(e) => {
              setName(e.target.value);
              if (error) setError(null);
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') handleSubmit();
            }}
            placeholder="Example: Smart Budget Planner"
            maxLength={50}
            required
            aria-required="true"
            aria-invalid={!!error}
            className={`mt-1.5 w-full h-[42px] bg-[#faf9f7] border rounded-[10px] px-3 text-sm font-normal text-on-surface placeholder:font-normal placeholder:text-on-surface-variant/35 focus:outline-none transition-colors ${
              error
                ? 'border-red-400 focus:border-red-500'
                : 'border-outline focus:border-orange-500'
            }`}
          />
        </label>
        <div className="mt-4 grid grid-cols-1 sm:grid-cols-2 gap-3">
          <label className="text-xs font-semibold text-on-surface">
            PMO No. <span className="font-normal text-red-600">(Required)</span>
            <input value={pmoNo} onChange={(e) => { setPmoNo(e.target.value); if (error) setError(null); }} placeholder="Example: PMO-2026-001" maxLength={80} required aria-required="true" className="mt-1.5 w-full h-[42px] bg-[#faf9f7] border border-outline rounded-[10px] px-3 text-sm font-normal placeholder:font-normal focus:outline-none focus:border-orange-500" />
          </label>
          <label className="text-xs font-semibold text-on-surface">
            Author <span className="font-normal text-red-600">(Required)</span>
            <input value={author} onChange={(e) => { setAuthor(e.target.value); if (error) setError(null); }} placeholder="Example: Author name" maxLength={200} required aria-required="true" className="mt-1.5 w-full h-[42px] bg-[#faf9f7] border border-outline rounded-[10px] px-3 text-sm font-normal placeholder:font-normal focus:outline-none focus:border-orange-500" />
          </label>
        </div>
        <fieldset className="mt-4"><legend className="text-xs font-bold text-on-surface">Stakeholders by role</legend><div className="mt-2 grid grid-cols-1 sm:grid-cols-2 gap-3">{STAKEHOLDER_ROLES.map(role => <label key={role} className="text-xs font-semibold text-on-surface">{role}<input value={stakeholders[role] ?? ''} onChange={(e) => setStakeholders(prev => ({ ...prev, [role]: e.target.value }))} placeholder="Name or team" maxLength={200} className="mt-1.5 w-full h-[38px] bg-[#faf9f7] border border-outline rounded-[10px] px-3 text-sm font-normal placeholder:font-normal focus:outline-none focus:border-orange-500" /></label>)}</div></fieldset>
        <fieldset className="mt-4"><legend className="text-xs font-bold text-on-surface">1. Business &amp; Strategic Overview</legend><div className="mt-2 space-y-3">{BUSINESS_TOPICS.map(topic => <label key={topic} className="block text-xs font-semibold text-on-surface">{topic}<textarea value={businessTopics[topic] ?? ''} onChange={(e) => setBusinessTopics(prev => ({ ...prev, [topic]: e.target.value }))} placeholder={`Describe ${topic.toLowerCase()}`} rows={2} maxLength={2000} className="mt-1.5 w-full resize-y bg-[#faf9f7] border border-outline rounded-[10px] px-3 py-2 text-sm font-normal placeholder:font-normal focus:outline-none focus:border-orange-500" /></label>)}</div></fieldset>
        {error && (
          <p role="alert" className="mt-2 text-[11px] font-medium text-red-600 leading-snug">
            {error}
          </p>
        )}

        <div className="flex items-center justify-end gap-2 mt-6">
          <button
            onClick={() => { if (!isCreating) onClose(); }}
            disabled={isCreating}
            className="px-4 py-2 rounded-[10px] bg-white border border-outline text-on-surface-variant hover:bg-[#faf8f4] transition-colors text-xs font-semibold cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
          >
            Cancel
          </button>
          <button
            onClick={() => handleSubmit(true)}
            disabled={!name.trim() || isCreating}
            className="px-4 py-2 rounded-[10px] bg-white border border-primary text-primary hover:bg-primary/5 transition-colors text-xs font-semibold cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {isCreating ? 'Creating...' : 'Skip & Create Empty'}
          </button>
          <button
            onClick={() => handleSubmit()}
            disabled={!name.trim() || !pmoNo.trim() || !author.trim() || !businessTopics['Introduction & Executive Summary']?.trim() || isCreating}
            className="px-4 py-2 rounded-[10px] bg-primary text-on-primary hover:brightness-110 active:scale-[0.99] transition-all text-xs font-bold flex items-center gap-1.5 cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:brightness-100"
          >
            <Plus className="w-3.5 h-3.5" />
            {isCreating ? 'Creating...' : 'Create & Start Gatherer'}
          </button>
        </div>
      </div>
    </div>
  );
}
