import { useEffect, useState } from "react";
import {
  ChevronDown,
  Download,
  FileText,
  GitBranch,
  Network,
  Printer,
  Clock,
} from "lucide-react";

import type { WorkspaceTab } from "../hooks/useWorkspaceUi";
import { Tooltip } from "./Tooltip";

const TAB_ITEMS: ReadonlyArray<{
  id: WorkspaceTab;
  label: string;
  icon: typeof FileText;
}> = [
  { id: "prd", label: "PRD Documents", icon: FileText },
  { id: "trace", label: "Requirements", icon: GitBranch },
  { id: "flows", label: "Flows", icon: Network },
  { id: "history", label: "Versions", icon: Clock },
];

interface WorkspaceNavigationProps {
  activeTab: WorkspaceTab;
  onTabChange: (tab: WorkspaceTab) => void;
  onDownloadDocx: () => void;
  onPrintPdf: () => void;
}

export function WorkspaceNavigation({
  activeTab,
  onTabChange,
  onDownloadDocx,
  onPrintPdf,
}: WorkspaceNavigationProps) {
  // PRD-EXPORT 1.0 — Export menu UI. DOCX (1.1) and PDF (1.2) close the menu
  // before delegating to the shared coordinator 2.0; no document conversion
  // or filename construction happens in this component.
  const [exportMenuOpen, setExportMenuOpen] = useState(false);

  useEffect(() => {
    if (!exportMenuOpen) return;
    const closeOnPointer = (event: MouseEvent) => {
      const target = event.target;
      if (target instanceof Element && target.closest("[data-export-menu]"))
        return;
      setExportMenuOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setExportMenuOpen(false);
    };
    document.addEventListener("mousedown", closeOnPointer);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("mousedown", closeOnPointer);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [exportMenuOpen]);

  return (
    <div className="h-[50px] bg-white border-b border-outline shrink-0 select-none flex items-center">
      <div className="workspace-toolbar flex flex-1 min-w-0 items-center gap-1.5 px-2 sm:px-3">
        <nav
          aria-label="Workspace views"
          className="flex flex-[1_1_0%] min-w-0 items-center gap-0.5 p-[3px] h-[38px] bg-tab-pill border border-tab-pill-border rounded-[8px] overflow-hidden"
        >
          {TAB_ITEMS.map((tab) => (
            <Tooltip
              key={tab.id}
              label={tab.label}
              side="bottom"
              className="flex-[1_1_0%] min-w-0 h-full"
            >
              <button
                type="button"
                onClick={() => onTabChange(tab.id)}
                aria-label={`${tab.label} tab`}
                aria-current={activeTab === tab.id ? "page" : undefined}
                className={`flex w-full min-w-0 h-full px-1 rounded-[6px] text-[11px] leading-none font-semibold whitespace-nowrap transition-colors duration-200 items-center justify-center gap-1 overflow-hidden ${
                  activeTab === tab.id
                    ? "bg-tab-active text-white shadow-sm"
                    : "text-tab-text hover:bg-tab-hover"
                }`}
              >
                <tab.icon className="w-3.5 h-3.5 shrink-0" />
                <span className="workspace-tab-label min-w-0 truncate">
                  {tab.label}
                </span>
              </button>
            </Tooltip>
          ))}
        </nav>

        <div data-export-menu className="relative shrink-0 no-print">
          <button
            type="button"
            onClick={() => setExportMenuOpen((open) => !open)}
            aria-label="Export document"
            aria-haspopup="menu"
            aria-expanded={exportMenuOpen}
            className="workspace-export-button h-[38px] px-2.5 rounded-[8px] bg-white border border-export-border hover:bg-export-hover text-xs font-semibold text-export-text transition-colors flex items-center justify-center gap-1.5 cursor-pointer whitespace-nowrap"
          >
            <Download className="w-3.5 h-3.5 shrink-0" />
            <span className="workspace-export-label">Export</span>
            <ChevronDown
              className={`workspace-export-chevron w-3 h-3 shrink-0 transition-transform ${exportMenuOpen ? "rotate-180" : ""}`}
            />
          </button>
          {exportMenuOpen && (
            <div
              role="menu"
              className="absolute right-0 top-full mt-1.5 z-50 w-40 rounded-[10px] border border-outline bg-white p-1 shadow-xl"
            >
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  setExportMenuOpen(false);
                  onDownloadDocx();
                }}
                className="w-full rounded-[7px] px-3 py-2 text-left text-xs font-semibold text-on-surface hover:bg-tab-hover flex items-center gap-2"
              >
                <Download className="w-3.5 h-3.5 text-primary" /> DOCX
              </button>
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  setExportMenuOpen(false);
                  onPrintPdf();
                }}
                className="w-full rounded-[7px] px-3 py-2 text-left text-xs font-semibold text-on-surface hover:bg-tab-hover flex items-center gap-2"
              >
                <Printer className="w-3.5 h-3.5 text-primary" /> PDF
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
