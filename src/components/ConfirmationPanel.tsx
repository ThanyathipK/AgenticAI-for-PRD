import React, { useEffect, useMemo, useState } from 'react';
import { AlertTriangle, Check, GitCompare, Lock, X } from 'lucide-react';
import { api } from '../api/client';
import type {
  ConfirmActionResponse,
  ImpactAnalysisPayload,
  PendingActionPayload,
} from '../api/types';
import { handleError } from './Toast';

interface ConfirmationPanelProps {
  action: PendingActionPayload;
  /** Receives the confirm response so the caller can report the saved version. */
  onConfirm: (result?: ConfirmActionResponse) => void;
  onCancel: () => void;
}

// ----------------------------------------------------------------------------
// The merge window shows ONLY what matters — what changes and what is affected.
// `proposed_changes` shapes vary slightly between the chat MERGE flow and the
// document-extraction INSERT flow, so the fallback counters read defensively.
// ----------------------------------------------------------------------------

/** Compact shape of the staged draft — the fallback when impact analysis fails. */
interface DraftShape {
  requirements: number;
  stories: number;
  versionNumber?: number;
}

function asArray(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? (value as Record<string, unknown>[]) : [];
}

/**
 * Color-coded change-kind badge (created/updated/removed). `label` overrides the
 * default wording so a dangling reference can be flagged explicitly.
 */
function ChangeBadge({ kind, label }: { kind: string; label?: string }) {
  const styles: Record<string, string> = {
    created: 'bg-emerald-50 text-emerald-700 border-emerald-200',
    updated: 'bg-blue-50 text-blue-700 border-blue-200',
    removed: 'bg-red-50 text-red-700 border-red-200',
    unchanged: 'bg-slate-50 text-slate-500 border-slate-200',
  };
  const labels: Record<string, string> = {
    created: 'NEW',
    updated: 'UPDATED',
    removed: 'ARCHIVED',
    unchanged: 'UNCHANGED',
  };
  return (
    <span
      className={`text-[9px] font-bold uppercase tracking-wide px-1.5 py-0.5 rounded border shrink-0 ${
        styles[kind] ?? styles.unchanged
      }`}
    >
      {label ?? labels[kind] ?? kind.toUpperCase()}
    </span>
  );
}

/**
 * Merge window body: the ONLY things worth judging before Save — what changes
 * (requirements / user stories / acceptance criteria) and what is affected (PRD
 * sections and diagrams that reference the affected `REQ-`/`US-` codes).
 */
// CONFIRM 1.6 — Impact renderer (consumed by the panel body): shows the change
//              counters, per-item changes and impacted PRD sections/diagrams from
//              CONFIRM 2.2 / 4.x, with `state` distinguishing loading / ready / error
//              and `draft` providing the fallback counts when the preview is
//              unavailable. Presentational only — nothing here writes.
function ImpactAnalysisSection({
  impact,
  state,
  draft,
}: {
  impact: ImpactAnalysisPayload | null;
  state: 'loading' | 'ready' | 'error';
  draft: DraftShape;
}) {
  const summary = impact?.summary;
  const changedRequirements = impact?.requirements.filter(r => r.change_kind !== 'unchanged') ?? [];
  const changedStories = impact?.user_stories.filter(s => s.change_kind !== 'unchanged') ?? [];
  const impactedSections = impact?.impacted_prd_sections ?? [];
  const impactedDiagrams = impact?.impacted_diagrams ?? [];
  const nothingAffected = impactedSections.length === 0 && impactedDiagrams.length === 0;

  const chips: Array<{ label: string; tone: string }> = [];
  if (summary) {
    if (summary.requirements_created) chips.push({ label: `+${summary.requirements_created} requirement`, tone: 'bg-emerald-50 text-emerald-700 border-emerald-200' });
    if (summary.requirements_updated) chips.push({ label: `~${summary.requirements_updated} requirements updated`, tone: 'bg-blue-50 text-blue-700 border-blue-200' });
    if (summary.requirements_removed) chips.push({ label: `−${summary.requirements_removed} requirement archived`, tone: 'bg-red-50 text-red-700 border-red-200' });
    if (summary.stories_created) chips.push({ label: `+${summary.stories_created} story`, tone: 'bg-emerald-50 text-emerald-700 border-emerald-200' });
    if (summary.stories_updated) chips.push({ label: `~${summary.stories_updated} stories updated`, tone: 'bg-blue-50 text-blue-700 border-blue-200' });
    if (summary.stories_removed) chips.push({ label: `−${summary.stories_removed} story archived`, tone: 'bg-red-50 text-red-700 border-red-200' });
    if (summary.criteria_added) chips.push({ label: `+${summary.criteria_added} criteria`, tone: 'bg-emerald-50 text-emerald-700 border-emerald-200' });
    if (summary.criteria_removed) chips.push({ label: `−${summary.criteria_removed} criteria`, tone: 'bg-red-50 text-red-700 border-red-200' });
    if (summary.criteria_review_required) chips.push({ label: `${summary.criteria_review_required} criteria need review`, tone: 'bg-amber-50 text-amber-700 border-amber-200' });
    if (summary.sections_impacted) chips.push({ label: `${summary.sections_impacted} PRD section${summary.sections_impacted > 1 ? 's' : ''} impacted`, tone: 'bg-amber-50 text-amber-700 border-amber-200' });
    if (summary.diagrams_impacted) chips.push({ label: `${summary.diagrams_impacted} diagram${summary.diagrams_impacted > 1 ? 's' : ''} impacted`, tone: 'bg-amber-50 text-amber-700 border-amber-200' });
  }

  return (
    <div className="px-4 pt-3">
      {/* Header: what the merge does + the requirements revision it would create */}
      <div className="flex items-center gap-2 flex-wrap">
        <GitCompare className="w-4 h-4 text-primary shrink-0" />
        <span className="text-xs font-bold text-on-surface">What changes vs what is affected</span>
        {state === 'ready' && impact && (
          <span className="text-[10px] font-mono text-on-surface-variant ml-auto shrink-0">
            revision {impact.current_version} to {impact.proposed_version ?? impact.current_version}
          </span>
        )}
      </div>

      {state === 'loading' && (
        <p className="mt-2 text-[11px] text-on-surface-variant font-mono">
          Analyzing changes and affected artifacts…
        </p>
      )}

      {state === 'error' && (
        <p className="mt-2 text-[11px] text-amber-700">
          Impact analysis is unavailable. The draft holds {draft.requirements} requirement
          {draft.requirements === 1 ? '' : 's'}, {draft.stories} stor{draft.stories === 1 ? 'y' : 'ies'}
          {draft.versionNumber !== undefined ? ` (revision ${draft.versionNumber})` : ''}. Nothing is saved until you click Save.
        </p>
      )}

      {state === 'ready' && impact && (
        <div className="mt-2 rounded-xl border border-slate-200 bg-slate-50/60 p-3 space-y-3 max-h-72 overflow-y-auto custom-scrollbar">
          {/* WHAT CHANGES */}
          <div>
            <p className="text-[10px] font-bold uppercase tracking-wide text-slate-500 mb-1">What changes</p>
            {chips.length > 0 ? (
              <div className="flex items-center gap-1.5 flex-wrap">
                {chips.map((chip, i) => (
                  <span key={i} className={`text-[10px] font-bold px-2 py-0.5 rounded-full border ${chip.tone}`}>
                    {chip.label}
                  </span>
                ))}
              </div>
            ) : (
              <p className="text-[11px] text-slate-500">
                Nothing. The draft matches the stored state.
              </p>
            )}
            {(changedRequirements.length > 0 || changedStories.length > 0) && (
              <ul className="mt-1.5 space-y-1">
                {changedRequirements.map((r, i) => (
                  <li key={`req-${i}`} className="flex items-center gap-2 flex-wrap text-[11px]">
                    <ChangeBadge kind={r.change_kind} />
                    <span className="font-mono font-bold text-slate-700">{r.requirement_code}</span>
                    <span className="text-slate-600 break-words min-w-0">{r.title || '(untitled)'}</span>
                  </li>
                ))}
                {changedStories.map((s, i) => (
                  <li key={`story-${i}`} className="flex items-center gap-2 flex-wrap text-[11px]">
                    <ChangeBadge kind={s.change_kind} />
                    <span className="font-mono font-bold text-primary">{s.ticket_code}</span>
                    <span className="text-slate-600 break-words min-w-0">{s.story_title || '(untitled)'}</span>
                    {s.criteria_added > 0 && (
                      <span className="text-[10px] font-mono text-emerald-600 font-bold">+{s.criteria_added} criteria</span>
                    )}
                    {s.criteria_removed > 0 && (
                      <span className="text-[10px] font-mono text-red-600 font-bold">−{s.criteria_removed} criteria</span>
                    )}
                    {s.criteria_review_required > 0 && (
                      <span className="text-[10px] font-mono text-amber-700 font-bold">{s.criteria_review_required} need review</span>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </div>

          {/* WHAT IS AFFECTED */}
          <div>
            <p className="text-[10px] font-bold uppercase tracking-wide text-slate-500 mb-1">What is affected</p>
            {nothingAffected ? (
              <p className="text-[11px] text-slate-500">
                No PRD section or diagram references the changed codes.
              </p>
            ) : (
              <ul className="space-y-1">
                {impactedSections.map((sec, i) => (
                  <li key={`sec-${i}`} className="flex items-center gap-2 flex-wrap text-[11px]">
                    <ChangeBadge
                      kind={sec.impact_kind === 'references_removed' ? 'removed' : 'updated'}
                      label={sec.impact_kind === 'references_removed' ? 'DANGLING' : undefined}
                    />
                    <span className="text-slate-700 font-semibold break-words min-w-0">
                      {sec.title || sec.section_key}
                    </span>
                    <span className="text-[10px] font-mono text-slate-400">
                      {sec.referenced_codes.join(', ')}
                    </span>
                    {sec.is_locked && (
                      <span className="flex items-center gap-0.5 text-[10px] text-amber-700 font-mono">
                        <Lock className="w-3 h-3" /> locked
                      </span>
                    )}
                  </li>
                ))}
                {impactedDiagrams.map((d, i) => (
                  <li key={`diagram-${i}`} className="flex items-center gap-2 flex-wrap text-[11px]">
                    <ChangeBadge kind="updated" />
                    <span className="text-slate-700 font-semibold break-words min-w-0">{d.label}</span>
                    <span className="text-[10px] font-mono text-slate-400">
                      {d.referenced_codes.join(', ')}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

  // CONFIRM 1.0 — Confirmation panel = the UI of the human-in-the-loop gate. It
  //              renders ONE staged action (CONFIRM 1.7) with: Save (1.2 → 2.3),
  //              Cancel (1.4 → 2.4) and the read-only impact preview (1.5 → 1.6).
  //              Nothing on this card has been persisted yet; the header text says so.
  // CONFIRM 1.1 — Component entry: `action` is the staged draft (proposed_changes =
  //              the full merged requirement state), `onConfirm`/`onCancel` are the
  //              Dashboard callbacks that refresh project state afterwards.
export const ConfirmationPanel: React.FC<ConfirmationPanelProps> = ({ action, onConfirm, onCancel }) => {
  const [isWorking, setIsWorking] = useState<boolean>(false);
  const isArchive = action.action_type === 'ARCHIVE';

  // Compact shape of the staged draft — parsed once per action and used as the
  // fallback summary when the impact analysis endpoint is unavailable, so Save
  // is never a blind click.
  const draft = useMemo<DraftShape>(() => {
    const proposed = (action.proposed_changes ?? {}) as Record<string, unknown>;
    const requirements = asArray(proposed.requirements);
    const stories = requirements.reduce((acc, r) => acc + asArray(r.user_stories).length, 0)
      + asArray(proposed.user_stories).length;
    const versionNumber = typeof proposed.version_number === 'number' ? proposed.version_number : undefined;
    return { requirements: requirements.length, stories, versionNumber };
  }, [action.proposed_changes]);

  // CONFIRM 1.2 — Save handler: calls the write endpoint and hands the persisted
  //              state back to the parent (which drops the action from the list and
  //              re-loads project state + locks). `isWorking` guards double submits.
  const handleConfirm = async () => {
    if (isWorking) return;
    setIsWorking(true);
    try {
      // CONFIRM 2.3 — API boundary: POST /api/confirm-action/{id}?project_id=…
      //               (CONFIRM 3.3). Errors (409 locked / 404 gone / 400 empty) surface
      //               as a toast; the draft stays staged.
      const result = await api.confirmAction(action.id, action.project_id);
      onConfirm(result);
    } catch (error) {
      handleError('Failed to confirm the action.', error);
    } finally {
      setIsWorking(false);
    }
  };

  // CONFIRM 1.4 — Cancel handler: discards the staged draft (CONFIRM 2.4 → 3.4).
  //              The persisted project is untouched, so the parent only closes the card.
  const handleCancel = async () => {
    if (isWorking) return;
    setIsWorking(true);
    try {
      // CONFIRM 2.4 — API boundary: POST /api/cancel-action/{id}?project_id=…
      await api.cancelAction(action.id, action.project_id);
      onCancel();
    } catch (error) {
      handleError('Failed to cancel the action.', error);
    } finally {
      setIsWorking(false);
    }
  };

  // CONFIRM 1.5 — Impact prefetch (once per action, non-blocking): the read-only diff
  //              (CONFIRM 2.2 → 3.2 → 4.x). A failure merely switches the section to
  //              its fallback summary (draft counts) — Save/Cancel stay usable.
  // ---- Requirement Impact Analysis (READ-ONLY) -------------------------------
  // What this merge changes and which downstream artifacts are affected.
  // Fetched once per action; a failure NEVER blocks Save/Cancel.
  const [impact, setImpact] = useState<ImpactAnalysisPayload | null>(null);
  const [impactState, setImpactState] = useState<'loading' | 'ready' | 'error'>('loading');

  useEffect(() => {
    let cancelled = false;
    setImpact(null);
    setImpactState('loading');
    // CONFIRM 2.2 — API boundary: GET /api/pending-actions/{id}/impact?project_id=…
    api.getActionImpact(action.id, action.project_id)
      .then(result => {
        if (!cancelled) {
          setImpact(result);
          setImpactState('ready');
        }
      })
      .catch(err => {
        console.warn('Impact analysis unavailable.', err);
        if (!cancelled) setImpactState('error');
      });
    return () => {
      cancelled = true;
    };
  }, [action.id, action.project_id]);

  // True when the draft actually changes something (the impact result when
  // known, otherwise the draft's own counts).
  const hasChanges = impact ? impact.has_changes : draft.requirements > 0 || draft.stories > 0;

  return (
    <div className="bg-white border border-orange-300 border-l-4 rounded-[12px] my-3 overflow-hidden">
      {/* Header */}
      <div className="bg-[#fffaf6] border-b border-outline px-4 py-3 flex items-start gap-3">
        <div className="w-8 h-8 rounded-xl bg-amber-100 flex items-center justify-center shrink-0">
          <AlertTriangle className="text-amber-600 w-4 h-4" />
        </div>
        <div className="flex-1 min-w-0">
          <h3 className="font-bold text-sm text-amber-900">
            {isArchive ? 'Archive alert: confirmation required' : `Pending ${action.action_type}: Review Before Saving`}
          </h3>
          <p className="text-xs text-amber-800 mt-0.5 break-words">
            {hasChanges
              ? isArchive
                ? 'Nothing is archived yet. Confirm archive applies the changes below; Cancel keeps every item active.'
                : 'Nothing is saved yet. Below is what changes and what is affected. Save applies this revision; Cancel keeps the previous revision.'
              : `You have a pending change based on: "${action.original_user_message}"`}
          </p>
        </div>
      </div>

      {/* Merge window: what changes + what is affected (READ-ONLY) */}
      <ImpactAnalysisSection impact={impact} state={impactState} draft={draft} />

      {/* Actions */}
      <div className="flex items-center gap-2 px-4 py-3">
        <button
          onClick={handleConfirm}
          disabled={isWorking}
          className="flex h-9 w-[132px] shrink-0 items-center justify-center gap-1 bg-orange-500 hover:bg-orange-600 text-white px-3 rounded-[8px] text-xs font-semibold transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        >
          <Check size={16} /> {isArchive ? 'Confirm archive' : 'Save'}
        </button>
        <button
          onClick={handleCancel}
          disabled={isWorking}
          className="flex h-9 w-[132px] shrink-0 items-center justify-center gap-1 bg-white hover:bg-[#faf8f4] text-gray-700 px-3 rounded-[8px] border border-outline text-xs font-semibold transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        >
          <X size={16} /> Cancel
        </button>
        <span className="text-[10px] text-on-surface-variant ml-auto font-mono">
          {isWorking ? 'Working…' : isArchive ? 'Confirm archive applies it; Cancel keeps items active' : 'Save applies this revision; Cancel keeps the previous revision'}
        </span>
      </div>
    </div>
  );
};
