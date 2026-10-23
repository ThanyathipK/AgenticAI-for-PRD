import { AlertTriangle, CheckCircle2, CircleHelp, FileText, Lock, MessageSquare, RefreshCw } from 'lucide-react';

const STEPS = ['Create a project', 'Add requirements or source files', 'Review and save changes', 'Validate requirements', 'Resolve clarifications', 'Generate and approve the PRD'] as const;

const SECTIONS = [
  { number: '1', title: 'Creating requirements', items: [
    ['Create a project', 'Start a project from the sidebar. Intake details seed the PRD and give the Gatherer initial business context.'],
    ['Describe the need in chat', 'The Gatherer structures your message into requirements, user stories, and acceptance criteria. Nothing is saved until you review the pending change and select Save.'],
    ['Add source files', 'Use the attachment control to add project documents to the knowledge base. Source filenames and sections remain visible in audit evidence. Document extraction is a separate action when offered in the document library.'],
    ['Review before saving', 'A merge preview shows what will change and which downstream PRD sections or diagrams may be affected. Save applies the proposal; Cancel leaves stored requirements unchanged.'],
  ] },
  { number: '2', title: 'Validating and resolving findings', items: [
    ['Validate Requirements', 'The Auditor checks the current saved requirements against applicable project evidence and banking controls. Results are grouped by requirement as Passed, Needs review, Blocking issue, or Conflict. Every conflict includes a clarification question that can be answered to prepare a requirement update.'],
    ['Read the evidence', 'Each finding shows its affected requirement and available source filename or section. Banking rule labels are supporting controls, not requirement identifiers.'],
    ['Answer clarifications', 'Enter the missing business detail and submit the answers. The Gatherer prepares a real requirement-change proposal from those answers.'],
    ['Apply the update', 'Review the prepared proposal and select Save to update the requirement, user story, or acceptance criteria. Then validate again; a conflict disappears only after the saved content satisfies the control.'],
  ] },
  { number: '3', title: 'Generating and reviewing the PRD', items: [
    ['Generate PRD', 'Generate PRD builds a draft from the currently saved project artifacts. The existing saved PRD remains unchanged while the draft is in preview.'],
    ['PRD clarity level', 'Clarity evaluates the complete PRD, not individual requirements. High is reserved for documents that include a named system, API, or tool; a specific business domain; actors and end-to-end flows; explicit constraints; at least two measurable targets; and at least three distinct operational control areas. Medium has core project specificity but incomplete measurement or operational coverage. Low lacks enough concrete, testable detail. A generic word such as “API” does not count as a named technology, and template guidance does not increase the level.'],
    ['Approve, regenerate, or discard', 'Approve & save PRD creates the next semantic version. Regenerate replaces the unsaved draft. Discard removes the draft without changing the saved PRD.'],
    ['Edit sections', 'PRD sections can be edited individually. A saved manual edit creates a new immutable PRD version and marks that section as human-owned.'],
    ['Lock reviewed content', 'Lock a requirement or PRD section to protect it from later AI changes. Unlock it when further editing or regeneration is required.'],
  ] },
  { number: '4', title: 'Exploring, versioning, and exporting', items: [
    ['Requirements', 'Review saved requirements, user stories, acceptance criteria, traceability, and lock state in the Requirements tab.'],
    ['Flows', 'View the generated Mermaid architecture flow associated with the current project.'],
    ['Versions', 'PRD history is append-only and uses semantic versions. The newest versions load when you open Version History; select Load older versions to retrieve earlier entries in additional groups. Open a version to inspect section-level changes. Restoring an older snapshot creates a new version and preserves currently locked sections.'],
    ['Export', 'Export the latest saved PRD as DOCX or PDF. An unsaved generation preview is not treated as the current exported document.'],
    ['Dashboard review', 'Use the Dashboard to manage project review status, inspect review history, audit health, and recorded waivers.'],
  ] },
  { number: '5', title: 'Chat trigger phrases', items: [
    ['Create something new', 'Use action words such as Add, Create, Implement, Introduce, Append, or Include. Example: “Add a requirement for beneficiary approval.”'],
    ['Update existing content', 'Use Update, Change, Modify, Adjust, Revise, Edit, Replace, Rename, Set, Increase, Decrease, Correct, Fix, Amend, or Refine. Example: “Update REQ-002 to use a 30-second timeout.”'],
    ['Archive existing content', 'Use Remove, Delete, Cancel, Drop, Discard, Retire, Archive, or “no longer need.” The system archives governed requirement artifacts instead of silently erasing their history.'],
    ['Restore archived requirements', 'Use Restore, Reopen, Reactivate, or Unarchive with a stable requirement code, for example “Restore REQ-001.” The system prepares a confirmation preview, preserves the original code and stored hierarchy, and requires validation again after saving.'],
    ['Run validation', 'Use Validate, Audit, Review, Check requirements, Run audit, or Perform validation. The Validate Requirements button runs the same dedicated Auditor workflow directly.'],
    ['Generate or export the PRD', 'Use “Generate PRD” or “Create PRD” to request document generation. Use “Export DOCX” or “Export PDF” for export-related commands, or use the Export menu in the workspace.'],
    ['Ask without changing anything', 'Questions such as “Explain REQ-002,” “What does US-003 mean?”, or “How do retries work?” are treated as read-only conversation. A question-shaped action such as “Can you update REQ-002?” is still an update request.'],
    ['Be specific', 'Include a stable code such as REQ-002 or US-003, the field or behavior to change, and the intended new value. Specific targets produce safer matching and clearer previews.'],
  ] },
  { number: '6', title: 'Account and data', items: [
    ['Delete account', 'Open Account settings from the user panel. Enter your current password and type DELETE exactly. This permanently removes your account, projects, requirements, uploaded documents, conversations, PRDs, and account-owned knowledge. The action cannot be undone.'],
  ] },
  { number: '7', title: 'Loading, switching, and live updates', items: [
    ['Switch projects safely', 'Selecting another project cancels the obsolete project load. Only information belonging to the currently selected project is applied, so a slower response from the previous project cannot replace the new workspace.'],
    ['Recent chat first', 'The workspace initially restores the five newest messages. Scroll upward to load older messages in additional groups of five. Persisted messages remain part of the project history and are not deleted by this bounded loading.'],
    ['Live updates', 'Saved changes are delivered through the live connection and refreshed automatically. Related events that arrive together are combined into one refresh to avoid repeated loading.'],
    ['Version history on demand', 'Only the current semantic-version information is needed during ordinary project loading. The version timeline loads when you open Version History, and older entries load only when requested.'],
    ['If information looks stale', 'Wait for the sync status to report that live updates are connected. You can use the visible Refresh control where provided; reconnecting or reopening the project performs a complete server refresh without changing saved data.'],
  ] },
] as const;

export function HelpGuidelines() {
  return (
    <main className="flex-1 min-w-0 min-h-0 overflow-y-auto bg-[#faf8f4] custom-scrollbar">
      <header className="sticky top-0 z-10 flex h-[50px] items-center gap-2 border-b border-outline bg-white px-5">
        <CircleHelp className="h-4 w-4 text-primary" />
        <span className="text-[13px] font-bold text-on-surface">Help &amp; Guidelines</span>
      </header>
      <div className="mx-auto max-w-5xl px-5 py-7 md:px-8 md:py-9">
        <h1 className="text-2xl font-bold text-on-surface">Help &amp; Guidelines</h1>
        <p className="mt-1.5 max-w-3xl text-sm leading-relaxed text-on-surface-variant">Learn how this workspace gathers and validates requirements, prepares governed updates, generates PRDs, and protects versioned content.</p>

        <section className="mt-6 rounded-xl border border-outline bg-white p-4 shadow-sm">
          <p className="text-[10px] font-bold uppercase tracking-[0.1em] text-on-surface-variant">Getting started</p>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            {STEPS.map((step, index) => <div key={step} className="flex items-center gap-2">
              <span className="rounded-md border border-primary/15 bg-primary/[0.06] px-2.5 py-1.5 text-[11px] font-semibold text-on-surface">{step}</span>
              {index < STEPS.length - 1 && <span className="text-xs text-on-surface-variant/50">→</span>}
            </div>)}
          </div>
          <p className="mt-3 text-xs leading-relaxed text-on-surface-variant">AI output is staged for review before it changes governed requirements or the PRD. Requirement proposals and generated PRD previews each have an explicit save or approval step.</p>
        </section>

        <div className="mt-4 space-y-3">
          {SECTIONS.map((section, index) => <details key={section.number} open={index === 0} className="group rounded-xl border border-outline bg-white shadow-sm">
            <summary className="flex cursor-pointer list-none items-center gap-3 px-4 py-3.5">
              <span className="flex h-6 w-6 items-center justify-center rounded-md bg-primary/10 text-[11px] font-bold text-primary">{section.number}</span>
              <span className="flex-1 text-sm font-bold text-on-surface">{section.title}</span>
              <span className="text-xs text-on-surface-variant transition-transform group-open:rotate-180">⌄</span>
            </summary>
            <div className="border-t border-outline px-4 py-2">
              {section.items.map(([title, description]) => <div key={title} className="border-b border-outline/70 py-3 last:border-b-0">
                <h3 className="text-xs font-bold text-on-surface">{title}</h3>
                <p className="mt-1 text-xs leading-relaxed text-on-surface-variant">{description}</p>
              </div>)}
            </div>
          </details>)}
        </div>

        <section className="mt-4 rounded-xl border border-amber-200 bg-amber-50/50 p-4">
          <div className="flex items-center gap-2 text-amber-800"><AlertTriangle className="h-4 w-4" /><h2 className="text-xs font-bold">Important safeguards</h2></div>
          <ul className="mt-2 space-y-2 text-xs leading-relaxed text-on-surface-variant">
            <li className="flex gap-2"><CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-emerald-700" />Review and save staged requirement changes before generating from them.</li>
            <li className="flex gap-2"><Lock className="mt-0.5 h-3.5 w-3.5 shrink-0 text-primary" />Locked artifacts are protected server-side, not only disabled in the interface.</li>
            <li className="flex gap-2"><RefreshCw className="mt-0.5 h-3.5 w-3.5 shrink-0 text-primary" />Revalidate after saving clarification-driven changes to obtain a current verdict.</li>
            <li className="flex gap-2"><FileText className="mt-0.5 h-3.5 w-3.5 shrink-0 text-primary" />Version restores and approved previews append history; they do not rewrite earlier snapshots.</li>
            <li className="flex gap-2"><MessageSquare className="mt-0.5 h-3.5 w-3.5 shrink-0 text-primary" />Use chat to refine saved requirements; use section editing for deliberate manual PRD changes.</li>
            <li className="flex gap-2"><RefreshCw className="mt-0.5 h-3.5 w-3.5 shrink-0 text-primary" />Bounded chat and version loading change only what is fetched first; saved history remains persistent and can be loaded when needed.</li>
          </ul>
        </section>
      </div>
    </main>
  );
}
