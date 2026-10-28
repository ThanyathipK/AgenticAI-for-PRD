// VersionHistory
// Renders the immutable change ledger timeline and diff viewer.
import { useEffect, useRef, useState } from "react";
import { ChevronDown, ChevronRight, Plus, Minus, Lock, FileText, RotateCcw } from "lucide-react";
import { ProjectState } from "../hooks/useProjectState";
import { api } from "../api/client";
import { ConfirmModal } from "./ConfirmModal";
import type { PrdVersionDiffPayload, PrdVersionDiffSectionPayload } from "../api/types";
import { displayVersionAuthor, extractProjectAuthor } from "../utils/actorName";
import { formatPrdVersionLabel } from "../utils/version";
import { formatDateTime } from "../utils/time";

const VERSION_HISTORY_PAGE_SIZE = 10;

// VERSION 1.3 — Diff-line renderer: paints the add/del/context tuples returned by
//               6.7. It does not calculate or validate the comparison.
/** Renders diff lines: green for additions, red for deletions, grey for context. */
type DiffRow = {
  kind: 'pair' | 'context' | 'skip';
  before?: string;
  after?: string;
  oldLine?: number;
  newLine?: number;
  skipped?: number;
};

export function readableDiffLine(line: string): string {
  if (/^\s*\|?(?:\s*:?-{1,}:?\s*\|)+\s*$/.test(line)) return 'Table divider';
  let value = line
    .replace(/<br\s*\/?>/gi, ' · ')
    .replace(/\\([*_`])/g, '$1')
    .replace(/\*\*([^*]+)\*\*/g, '$1')
    .replace(/__([^_]+)__/g, '$1')
    .replace(/^\s{0,3}#{1,6}\s+/, '');
  if (value.trim().startsWith('|') && value.trim().endsWith('|')) {
    value = value.trim().slice(1, -1).split('|').map(cell => cell.trim()).join('  |  ');
  }
  return value;
}

export function buildDiffRows(diffLines: [string, string][]): DiffRow[] {
  const rows: DiffRow[] = [];
  let oldLine = 1;
  let newLine = 1;
  for (let index = 0; index < diffLines.length;) {
    const [tag, line] = diffLines[index];
    if (tag === 'skip') {
      const skipped = Math.max(0, Number.parseInt(line, 10) || 0);
      rows.push({ kind: 'skip', skipped });
      oldLine += skipped;
      newLine += skipped;
      index += 1;
      continue;
    }
    if (tag === 'context') {
      rows.push({ kind: 'context', before: line, oldLine, newLine });
      oldLine += 1;
      newLine += 1;
      index += 1;
      continue;
    }
    const removed: { text: string; line: number }[] = [];
    const added: { text: string; line: number }[] = [];
    while (diffLines[index]?.[0] === 'del') {
      removed.push({ text: diffLines[index][1], line: oldLine++ });
      index += 1;
    }
    while (diffLines[index]?.[0] === 'add') {
      added.push({ text: diffLines[index][1], line: newLine++ });
      index += 1;
    }
    if (removed.length === 0 && tag === 'add') {
      while (diffLines[index]?.[0] === 'add') {
        added.push({ text: diffLines[index][1], line: newLine++ });
        index += 1;
      }
    }
    const count = Math.max(removed.length, added.length);
    for (let rowIndex = 0; rowIndex < count; rowIndex += 1) {
      rows.push({
        kind: 'pair',
        before: removed[rowIndex]?.text,
        after: added[rowIndex]?.text,
        oldLine: removed[rowIndex]?.line,
        newLine: added[rowIndex]?.line,
      });
    }
  }
  return rows;
}

function ChangedWords({ before = '', after = '', side, raw }: {
  before?: string;
  after?: string;
  side: 'before' | 'after';
  raw: boolean;
}) {
  const source = raw ? (side === 'before' ? before : after) : readableDiffLine(side === 'before' ? before : after);
  const other = raw ? (side === 'before' ? after : before) : readableDiffLine(side === 'before' ? after : before);
  const otherWords = new Set(other.split(/\s+/).filter(Boolean));
  return <>{source.split(/(\s+)/).map((word, index) => {
    const changed = word.trim() !== '' && !otherWords.has(word);
    return changed ? <mark key={index} className={side === 'before' ? 'bg-red-200 text-inherit' : 'bg-emerald-200 text-inherit'}>{word}</mark> : word;
  })}</>;
}

export function DiffLines({ diffLines }: { diffLines: [string, string][] }) {
  const [showRaw, setShowRaw] = useState(false);
  if (diffLines.length === 0) {
    return <span className="text-slate-400 italic text-xs">(no line changes)</span>;
  }
  const rows = buildDiffRows(diffLines);
  return (
    <div className="text-xs leading-relaxed">
      <div className="mb-2 flex justify-end">
        <button type="button" onClick={() => setShowRaw(value => !value)} className="rounded-md border border-slate-200 bg-white px-2 py-1 text-[10px] font-semibold text-slate-600 hover:bg-slate-50">
          {showRaw ? 'Show readable diff' : 'Show raw Markdown'}
        </button>
      </div>
      <div className="space-y-1 font-mono whitespace-pre-wrap break-words">
        {rows.map((row, idx) => {
          if (row.kind === 'skip') return <div key={idx} className="py-1 text-center text-[10px] italic text-slate-400">… {row.skipped} unchanged lines …</div>;
          if (row.kind === 'context') return (
            <div key={idx} className="grid grid-cols-[2rem_1fr] rounded-md border-l-2 border-slate-200 px-2 py-1 text-slate-500">
              <span className="select-none text-right text-slate-400">{row.oldLine}</span>
              <span className="pl-3">{showRaw ? row.before : readableDiffLine(row.before ?? '')}</span>
            </div>
          );
          return (
            <div key={idx} className="overflow-hidden rounded-lg border border-slate-200 bg-white">
              {row.before !== undefined && <div className="grid grid-cols-[3.5rem_2rem_1fr] bg-red-50 px-2 py-1.5 text-red-800">
                <span className="font-sans text-[10px] font-bold uppercase">Before</span><span className="select-none text-right text-red-400">{row.oldLine}</span>
                <span className="pl-3"><ChangedWords before={row.before} after={row.after} side="before" raw={showRaw} /></span>
              </div>}
              {row.after !== undefined && <div className="grid grid-cols-[3.5rem_2rem_1fr] bg-emerald-50 px-2 py-1.5 text-emerald-800">
                <span className="font-sans text-[10px] font-bold uppercase">After</span><span className="select-none text-right text-emerald-500">{row.newLine}</span>
                <span className="pl-3"><ChangedWords before={row.before} after={row.after} side="after" raw={showRaw} /></span>
              </div>}
            </div>
          );
        })}
      </div>
    </div>
  );
}

/** One per-section diff card showing title, change kind, line counts and diff. */
// VERSION 1.4 — Per-section diff card: renders 6.8's change kind, line counts and
//               line tuples. `locked_preserved` reflects the section's CURRENT lock
//               state when the diff is requested, not necessarily its historical lock.
function SectionDiffCard({ section }: {
  section: PrdVersionDiffSectionPayload;
}) {
  const kindBorder = {
    created: 'border-emerald-300 bg-emerald-50/40',
    updated: 'border-blue-300 bg-blue-50/40',
    unchanged: 'border-slate-200 bg-slate-50/30',
    removed: 'border-red-300 bg-red-50/40',
    locked_preserved: 'border-amber-300 bg-amber-50/40',
  }[section.change_kind] ?? 'border-slate-200 bg-slate-50/30';

  const kindLabel = {
    created: 'New Section',
    updated: 'Updated',
    unchanged: 'Unchanged',
    removed: 'Removed',
    locked_preserved: 'Locked (Preserved)',
  }[section.change_kind] ?? section.change_kind;

  return (
    <div className={"rounded-xl border-2 p-4 " + kindBorder}>
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2 min-w-0">
          {section.change_kind === 'locked_preserved' ? (
            <Lock className="w-3.5 h-3.5 flex-shrink-0 text-amber-500" />
          ) : section.change_kind === 'removed' ? (
            <Minus className="w-3.5 h-3.5 flex-shrink-0 text-red-500" />
          ) : section.change_kind === 'created' ? (
            <Plus className="w-3.5 h-3.5 flex-shrink-0 text-emerald-500" />
          ) : (
            <FileText className="w-3.5 h-3.5 flex-shrink-0 text-slate-400" />
          )}
          <div className="min-w-0">
            <h5 className="font-semibold text-sm truncate">{section.title}</h5>
            <p className="text-[10px] font-mono opacity-70">{section.section_key}</p>
          </div>
        </div>
        <div className="flex items-center gap-2 flex-shrink-0">
          <span className="text-[10px] font-bold uppercase px-2 py-0.5 rounded-full bg-black/5">{kindLabel}</span>
          {section.changed && section.change_kind !== 'unchanged' && section.change_kind !== 'locked_preserved' && (
            <div className="flex items-center gap-1.5 text-[11px] font-mono">
              {section.added > 0 && <span className="text-emerald-600 font-semibold">+{section.added}</span>}
              {section.removed > 0 && <span className="text-red-600 font-semibold">-{section.removed}</span>}
              <span className="text-slate-400">lines</span>
            </div>
          )}
        </div>
      </div>
      {section.change_kind === 'created' && section.diff_lines.length > 0 && (
        <div className="mt-2"><DiffLines diffLines={section.diff_lines} /></div>
      )}
      {section.change_kind === 'updated' && section.diff_lines.length > 0 && (
        <div className="mt-2 border-t border-black/5 pt-2"><DiffLines diffLines={section.diff_lines} /></div>
      )}
      {section.change_kind === 'removed' && section.diff_lines.length > 0 && (
        <div className="mt-2"><DiffLines diffLines={section.diff_lines} /></div>
      )}
      {section.change_kind === 'locked_preserved' && (
        <div className="mt-1">
          <p className="text-xs text-amber-700">This section is currently locked. Its version differences are shown below.</p>
          {section.diff_lines.length > 0 && <div className="mt-2 border-t border-amber-200 pt-2"><DiffLines diffLines={section.diff_lines} /></div>}
        </div>
      )}
      {section.change_kind === 'unchanged' && section.diff_lines.length === 0 && (
        <p className="text-xs text-slate-400 mt-1">No changes to this section.</p>
      )}
    </div>
  );
}

function ChangeTypeBadge({ changeType }: { changeType: string | undefined }) {
  if (!changeType) return null;
  return (
    <span className={"text-[10px] font-bold px-1.5 py-0.5 rounded " + (changeType === 'ai' ? 'bg-amber-100 text-amber-700' : 'bg-blue-100 text-blue-700')}>
      {changeType === 'ai' ? 'AI' : 'Manual'}
    </span>
  );
}

export function resolveLoadedVersionSelection(
  versions: number[],
  selected: number,
  selectLatest: boolean,
): number {
  if (versions.length === 0) return selected;
  if (selectLatest) return versions[0];
  if (selected === 0 || versions.includes(selected)) return selected;
  return versions[0];
}

// VERSION 1.0 — Versions tab coordinator:
//               hook 2.1 paged timeline → automatic selection/base → client 3.2 diff
//               → 1.3/1.4 rendering → hook 2.2 confirmed restore. There is no manual
//               base picker: the loaded immediate predecessor is selected for it.
export function VersionHistory({ state }: { state: ProjectState }) {
  const {
    versionHistory,
    selectedHistVersion,
    setSelectedHistVersion,
    projectId,
    diffBaseVersion,
    setDiffBaseVersion,
    handleRestoreVersion,
    loadVersionHistory,
    prdMarkdown,
  } = state;
  const projectAuthor = extractProjectAuthor(prdMarkdown);

  // Pending restore target: the version number awaiting user confirmation via
  // the ConfirmModal (null = no restore in flight).
  const [restoreTarget, setRestoreTarget] = useState<number | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [hasOlderVersions, setHasOlderVersions] = useState(true);
  const selectionProjectRef = useRef<string | null>(null);

  // The full ledger is intentionally lazy: project opening fetches only the
  // latest SemVer row, while mounting this tab fetches the first metadata page.
  useEffect(() => {
    if (!projectId) return;
    let active = true;
    setHistoryLoading(true);
    void loadVersionHistory(projectId, { limit: VERSION_HISTORY_PAGE_SIZE }).then((count) => {
      if (!active) return;
      setHasOlderVersions(count === VERSION_HISTORY_PAGE_SIZE);
      setHistoryLoading(false);
    });
    return () => {
      active = false;
    };
  }, [projectId, loadVersionHistory]);

  // The ledger is newest-first, so its head is the current document version —
  // restoring it would be a no-op, hence no Restore button on the first entry.
  const latestVersion = versionHistory[0]?.version ?? null;

  // Select the newest row once when a project's ledger first loads. Afterwards,
  // preserve an explicit row selection (including an older version or collapsed
  // state). The previous implementation selected the head on every selection
  // change, making older rows impossible to open.
  useEffect(() => {
    if (!projectId || versionHistory.length === 0) return;
    const projectChanged = selectionProjectRef.current !== projectId;
    const next = resolveLoadedVersionSelection(
      versionHistory.map(version => version.version),
      selectedHistVersion,
      projectChanged,
    );
    selectionProjectRef.current = projectId;
    if (next !== selectedHistVersion) setSelectedHistVersion(next);
  }, [projectId, versionHistory, selectedHistVersion, setSelectedHistVersion]);

  // VERSION 1.2.1 — Choose the selected row's preceding LOADED row. If pagination
  //                 has not loaded it, null delegates predecessor lookup to 6.8.
  useEffect(() => {
    if (!projectId || selectedHistVersion <= 1) { setDiffBaseVersion(null); return; }
    // Find the version immediately before the selected one (any sort order).
    const sorted = [...versionHistory].sort((a, b) => a.version - b.version);
    const idx = sorted.findIndex(v => v.version === selectedHistVersion);
    if (idx > 0) setDiffBaseVersion(sorted[idx - 1].version);
    else setDiffBaseVersion(null);
  }, [selectedHistVersion, versionHistory, projectId, setDiffBaseVersion]);

  const [diff, setDiff] = useState<PrdVersionDiffPayload | null>(null);
  const [diffLoading, setDiffLoading] = useState(false);
  const [diffError, setDiffError] = useState<string | null>(null);

  useEffect(() => {
    if (!projectId || selectedHistVersion < 1) return;
    const base = diffBaseVersion ?? undefined;
    if (base === selectedHistVersion) {
      setDiff(null); setDiffError(null); return;
    }
    // VERSION 1.2 — The only SPA diff caller. No predecessor means 6.8 compares
    //               against an empty document (base 0). A 404 from a missing or
    //               invalid target/base is intentionally displayed as no diff.
    console.log('[Diff] Fetching diff:', projectId, 'v' + selectedHistVersion, 'vs v' + base);
    setDiffLoading(true);
    setDiffError(null);
    api.getPrdVersionDiff(projectId, selectedHistVersion, base)
      .then(result => {
        console.log('[Diff] Received:', result.sections?.length, 'sections');
        setDiff(result);
      })
      .catch(err => {
        console.warn('[Diff] Error:', err);
        // Don't show 404 as an error — it just means there's nothing to compare
        if (String(err).includes('404')) {
          setDiff(null);
          setDiffError(null);
        } else {
          setDiffError(String(err));
        }
      })
      .finally(() => setDiffLoading(false));
  }, [projectId, selectedHistVersion, diffBaseVersion]);

  return (
    <div className="space-y-3 animate-fadeIn">
      <div className="space-y-2">
          {versionHistory.map((v, idx) => (
            <section key={idx} className={`rounded-[10px] border overflow-hidden bg-white ${selectedHistVersion === v.version ? 'border-primary' : 'border-outline'}`}>
              <button
                onClick={() => setSelectedHistVersion(selectedHistVersion === v.version ? 0 : v.version)}
                className={"w-full px-4 py-3 flex items-center gap-3 text-left transition-colors " +
                  (selectedHistVersion === v.version
                    ? 'bg-[#faf8f4] text-on-surface'
                    : 'bg-white text-on-surface hover:bg-[#faf8f4]')}
              >
                <span className="min-w-0 flex-1">
                  <strong className="block text-sm font-bold">{formatPrdVersionLabel(v.semVersion, v.version)}</strong>
                  <span className="block mt-0.5 text-[11px] text-on-surface-variant font-normal">{formatDateTime(v.timestamp)}</span>
                </span>
                {selectedHistVersion === v.version
                  ? <ChevronDown className="w-4 h-4 shrink-0" />
                  : <ChevronRight className="w-4 h-4 shrink-0" />}
              </button>
              {selectedHistVersion === v.version && <div className="space-y-3 px-4 py-3 border-t border-outline">
                <div className="flex items-center gap-2">
                  <ChangeTypeBadge changeType={v.changeType} />
                  <span className="text-[11px] text-on-surface-variant">
                    Captured by {displayVersionAuthor(v.author, projectAuthor, v.changeType)}
                  </span>
                </div>
                <ul className="space-y-2 text-xs text-on-surface">
                  <li className="flex gap-2 leading-relaxed">
                    <span className="font-bold text-emerald-600">+</span>
                    <span>{v.description || 'Saved product requirement document snapshot'}</span>
                  </li>
                  {diff?.sections.filter(section => section.changed).slice(0, 4).map(section => (
                    <li key={section.section_key} className="flex gap-2 leading-relaxed">
                      <span className={`font-bold ${section.change_kind === 'removed' ? 'text-red-600' : section.change_kind === 'locked_preserved' ? 'text-amber-600' : 'text-emerald-600'}`}>
                        {section.change_kind === 'removed' ? '−' : section.change_kind === 'locked_preserved' ? '~' : '+'}
                      </span>
                      <span>{section.change_kind === 'locked_preserved' ? 'Preserved' : section.change_kind === 'removed' ? 'Removed' : 'Updated'} {section.title}</span>
                    </li>
                  ))}
                </ul>
                {diffLoading && <p className="text-xs text-on-surface-variant">Loading version changes…</p>}
                {diffError && <p className="text-xs text-red-700">Could not load version changes.</p>}
                {diff && diff.sections.length > 0 && (
                  <details className="rounded-[8px] border border-outline bg-[#faf8f4]">
                    <summary className="cursor-pointer list-none px-3 py-2 text-[11px] font-semibold text-on-surface">View detailed line changes</summary>
                    <div className="space-y-2 border-t border-outline p-3">
                      {diff.sections.filter(section => section.changed).map(section => (
                        <SectionDiffCard key={section.section_key} section={section} />
                      ))}
                    </div>
                  </details>
                )}
                {latestVersion !== null && v.version !== latestVersion && (
                  <div className="flex justify-end pt-1">
                    <button
                      onClick={() => setRestoreTarget(v.version)}
                      className="inline-flex items-center gap-1.5 rounded-[8px] bg-orange-500 px-4 py-2 text-[11px] font-semibold text-white hover:bg-orange-600 transition-colors cursor-pointer"
                      title={`Restore the PRD document to ${formatPrdVersionLabel(v.semVersion, v.version)}`}
                    >
                      <RotateCcw className="w-3 h-3" />
                      Restore {formatPrdVersionLabel(v.semVersion, v.version)}
                    </button>
                  </div>
                )}
              </div>}
            </section>
          ))}

        {versionHistory.length === 0 && (
          <div className="rounded-[10px] border border-dashed border-outline bg-white px-4 py-10 text-center text-sm text-on-surface-variant">
            No version history yet. Generate a PRD to start tracking changes.
          </div>
        )}

        {historyLoading && (
          <p className="py-2 text-center text-xs text-on-surface-variant">
            Loading version history…
          </p>
        )}
        {!historyLoading && hasOlderVersions && versionHistory.length > 0 && (
          <button
            type="button"
            className="w-full rounded-[8px] border border-outline bg-white px-3 py-2 text-xs font-semibold text-on-surface hover:bg-[#faf8f4]"
            onClick={async () => {
              if (!projectId) return;
              setHistoryLoading(true);
              const oldest = versionHistory[versionHistory.length - 1]?.version;
              const count = await loadVersionHistory(projectId, {
                append: true,
                beforeVersion: oldest,
                limit: VERSION_HISTORY_PAGE_SIZE,
              });
              setHasOlderVersions(count === VERSION_HISTORY_PAGE_SIZE);
              setHistoryLoading(false);
            }}
          >
            Load older versions
          </button>
        )}

      </div>

      <ConfirmModal
        isOpen={restoreTarget !== null}
        title={`Restore ${formatPrdVersionLabel(
          versionHistory.find(v => v.version === restoreTarget)?.semVersion,
          restoreTarget,
        )}?`}
        description={`The PRD document will be restored from snapshot ${formatPrdVersionLabel(
          versionHistory.find(v => v.version === restoreTarget)?.semVersion,
          restoreTarget,
        )}. This is APPEND-ONLY: the restored content becomes a NEW version and locked sections keep their current content.`}
        confirmLabel="Restore version"
        pendingLabel="Restoring..."
        onConfirm={async () => {
          if (restoreTarget === null) return false;
          const ok = await handleRestoreVersion(restoreTarget);
          if (ok) setRestoreTarget(null);
          return ok;
        }}
        onClose={() => setRestoreTarget(null)}
      />
    </div>
  );
}
