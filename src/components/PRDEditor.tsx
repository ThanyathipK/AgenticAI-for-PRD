// PRDEditor — PRD Document tab extracted from the former Dashboard.tsx.
// Renders the compiled PRD as editable markdown sections with lock/unlock and
// inline section editing.
import { useRef } from 'react';
import { FileText, Pencil, Save, Lock, Unlock } from 'lucide-react';
import { ProjectState } from '../hooks/useProjectState';
import { getSafeSectionTitle, getSafeSectionContent } from '../utils/markdown';
import { parseAndRenderMarkdown } from './MarkdownRenderer';
import { StructuredSectionEditor } from './StructuredSectionEditor';

const isHeadingOnlySection = (content: string) => {
  const body = content
    .replace(/^#{2,3}\s+.*?(?:\n|$)/, '')
    .replace(/^\\newpage\s*$/gm, '')
    .trim();
  return body.length === 0;
};

export function PRDEditor({ state }: { state: ProjectState }) {
  const {
    prdMarkdown,
    sections,
    editingSectionId,
    setEditingSectionId,
    editBuffer,
    setEditBuffer,
    handleSaveSection,
    sectionLocks,
    handleToggleSectionLock,
  } = state;

  // The section text the current edit STARTED from. Sent back as
  // `base_content` so the server can three-way merge the save onto the
  // latest stored section ("last version + this edit").
  const editBaseRef = useRef<string>('');

  return (
    <div className="space-y-3 animate-fadeIn">
      {/* Styled markdown content rendering */}
      <article className="bg-white p-4 sm:p-5 rounded-[12px] border border-outline prose prose-neutral max-w-none min-w-0">
        
        {/* Header decorative accent */}
        <div className="text-[10px] font-bold tracking-[0.12em] text-[#a89b91] mb-2 no-print">KRUNGSRI NIMBLE CONFIDENTIAL</div>
        
        {!prdMarkdown ? (
          <div className="flex flex-col items-center justify-center py-20 text-center text-on-surface-variant no-print">
            <FileText className="w-12 h-12 text-primary/40 mb-4 animate-pulse" />
            <p className="font-bold text-lg text-on-surface">PRD template not loaded yet</p>
            <p className="text-xs max-w-sm mt-1">The Krungsri Nimble template could not be fetched. Enter your raw requirements in the chat or click Generate PRD to build the document.</p>
          </div>
        ) : (
          <div className="space-y-0">
            {sections.map((section, sectionIdx) => {
              const isEditing = editingSectionId === section.id;
              const safeTitle = getSafeSectionTitle(section.title);
              const safeContent = getSafeSectionContent(section.content);
              
              return (
                <div 
                  key={section.id} 
                  className={`relative group rounded-2xl border transition-all duration-200 ${
                    isEditing 
                      ? 'bg-[#faf8f4] p-3.5 sm:p-4 border-primary/30'
                      : 'border-x-0 border-t-0 border-outline bg-white px-0 py-4 rounded-none'
                  }`}
                >
                  {/* Header Area with Title & Edit button */}
                  <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2 border-b border-outline pb-2.5 mb-4">
                    <h3 className="text-xs font-bold text-on-surface uppercase tracking-[0.02em] flex items-center gap-2 min-w-0 break-words">
                      <span className="opacity-40 font-semibold text-[10px] shrink-0">#0{sectionIdx + 1}</span>
                      <span className="min-w-0">{safeTitle}</span>
                    </h3>
                    
                    <div className="ml-auto flex shrink-0 items-center justify-end gap-2">
                      {/* Lock/Unlock button for this part */}
                      {(() => {
                        const isLocked = sectionLocks[section.id]?.is_locked ?? false;
                        return (
                          <button
                            onClick={() => handleToggleSectionLock(section.id)}
                            title={
                              isLocked
                                ? 'This part is locked. Edits and AI regeneration are blocked.'
                                : 'Lock this part. The Architect will never overwrite it again.'
                            }
                            className={`w-[82px] justify-center opacity-60 hover:opacity-100 group-hover:opacity-100 transition-opacity text-[11px] px-2.5 py-1.5 rounded-xl flex items-center gap-1.5 border shadow-sm cursor-pointer z-10 font-semibold ${
                              isLocked
                                ? 'bg-amber-100 text-amber-800 border-amber-300 hover:bg-amber-200'
                                : 'bg-white hover:bg-slate-50 text-slate-700 border-slate-200'
                            }`}
                          >
                            {isLocked ? (
                              <>
                                <Unlock className="w-3.5 h-3.5 text-amber-700" />
                                <span>Unlock</span>
                              </>
                            ) : (
                              <>
                                <Lock className="w-3.5 h-3.5 text-primary" />
                                <span>Lock</span>
                              </>
                            )}
                          </button>
                        );
                      })()}

                      {!isEditing && (
                        <button
                          onClick={() => {
                            setEditingSectionId(section.id);
                            setEditBuffer(safeContent);
                            editBaseRef.current = safeContent;
                          }}
                          disabled={sectionLocks[section.id]?.is_locked ?? false}
                          className="w-[82px] justify-center opacity-60 hover:opacity-100 group-hover:opacity-100 transition-opacity bg-white hover:bg-slate-50 text-slate-700 text-[11px] px-2.5 py-1.5 rounded-xl flex items-center gap-1.5 border border-slate-200 shadow-sm cursor-pointer z-10 font-semibold disabled:opacity-30 disabled:cursor-not-allowed"
                        >
                          <Pencil className="w-3.5 h-3.5 text-primary" />
                          <span>Edit</span>
                        </button>
                      )}
                    </div>
                  </div>
{isEditing ? (
                    <div className="space-y-4">
                      <StructuredSectionEditor
                        value={editBuffer}
                        onChange={setEditBuffer}
                      />
                      <div className="flex flex-wrap items-center justify-between gap-2 pt-3 border-t border-slate-100 mt-2">
                        <p className="text-[11px] text-slate-500 italic min-w-0">
                          Saved and versioned per part. Lock an approved part and the Architect will never overwrite it.
                        </p>
                        <div className="flex items-center gap-2">
                          <button
                            onClick={() => {
                              setEditingSectionId(null);
                              setEditBuffer("");
                            }}
                            className="px-3.5 py-1.5 rounded-xl border border-slate-200 hover:bg-slate-50 text-slate-700 text-xs font-semibold shadow-sm transition-all cursor-pointer"
                          >
                            Cancel
                          </button>
                          <button
                            onClick={() => handleSaveSection(section.id, editBuffer, editBaseRef.current)}
                            className="px-4 py-1.5 rounded-xl bg-primary text-on-primary hover:brightness-110 text-xs font-semibold shadow-md transition-all flex items-center gap-1.5 cursor-pointer"
                          >
                            <Save className="w-3.5 h-3.5" />
                            <span>Save Changes</span>
                          </button>
                        </div>
                      </div>
                    </div>
                  ) : (
                    <div className="prose prose-slate max-w-none pb-4 min-w-0 break-words">
                      {isHeadingOnlySection(safeContent) ? (
                        <p className="my-3 rounded-lg border border-dashed border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-500">
                          No confirmed details yet. This section is intentionally left empty until validated information is available.
                        </p>
                      ) : parseAndRenderMarkdown(safeContent)}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </article>
    </div>
  );
}
