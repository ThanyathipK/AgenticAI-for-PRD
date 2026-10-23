// useWorkspaceUi — pure UI layout/tab state extracted from the former
// useProjectState god-hook: split-pane drag, sidebar collapse, tab switching,
// diagram viewport zoom, and the version-history diff selection.
import { useEffect, useRef, useState } from 'react';
import type { Dispatch, SetStateAction } from 'react';

export type WorkspaceTab = 'prd' | 'flows' | 'trace' | 'history';

/** Top-level app view: the chat/PRD workspace or the projects overview dashboard. */
export type AppView = 'workspace' | 'dashboard' | 'help';

export interface UseWorkspaceUiResult {
  splitPct: number;
  setSplitPct: Dispatch<SetStateAction<number>>;
  isDraggingSplit: boolean;
  setIsDraggingSplit: Dispatch<SetStateAction<boolean>>;
  historyCollapsed: boolean;
  setHistoryCollapsed: Dispatch<SetStateAction<boolean>>;
  splitContainerRef: React.RefObject<HTMLDivElement | null>;
  activeTab: WorkspaceTab;
  setActiveTab: (tab: WorkspaceTab) => void;
  /** Which top-level page fills the content area next to the project sidebar. */
  activeView: AppView;
  setActiveView: (view: AppView) => void;
  diagramZoom: number;
  setDiagramZoom: Dispatch<SetStateAction<number>>;
  hoverNode: string | null;
  setHoverNode: Dispatch<SetStateAction<string | null>>;
  activePacketFlow: boolean;
  setActivePacketFlow: Dispatch<SetStateAction<boolean>>;
  selectedHistVersion: number;
  setSelectedHistVersion: Dispatch<SetStateAction<number>>;
  diffBaseVersion: number | null;
  setDiffBaseVersion: Dispatch<SetStateAction<number | null>>;
}

export function useWorkspaceUi(): UseWorkspaceUiResult {
  const [splitPct, setSplitPct] = useState<number>(49);
  const [isDraggingSplit, setIsDraggingSplit] = useState<boolean>(false);
  const [historyCollapsed, setHistoryCollapsed] = useState<boolean>(true);
  const splitContainerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!isDraggingSplit) return;
    const handleMove = (e: MouseEvent) => {
      const container = splitContainerRef.current;
      if (!container) return;
      const rect = container.getBoundingClientRect();
      let pct = ((e.clientX - rect.left) / rect.width) * 100;
      pct = Math.min(70, Math.max(24, pct));
      setSplitPct(pct);
    };
    const handleUp = () => setIsDraggingSplit(false);
    document.body.classList.add('is-resizing');
    window.addEventListener('mousemove', handleMove);
    window.addEventListener('mouseup', handleUp);
    return () => {
      document.body.classList.remove('is-resizing');
      window.removeEventListener('mousemove', handleMove);
      window.removeEventListener('mouseup', handleUp);
    };
  }, [isDraggingSplit]);

  // Layout Tab Active States: 'prd' | 'flows' | 'trace' | 'history'
  const [activeTab, setActiveTab] = useState<WorkspaceTab>('prd');

  // Top-level view next to the project sidebar: workspace, dashboard, or help.
  const [activeView, setActiveView] = useState<AppView>('workspace');

  // Interactive Flowchart/Sequence Visualizer zoom/state
  const [diagramZoom, setDiagramZoom] = useState<number>(100);
  const [hoverNode, setHoverNode] = useState<string | null>(null);
  const [activePacketFlow, setActivePacketFlow] = useState<boolean>(true);

  // Selected version for Timeline Diff comparison
  const [selectedHistVersion, setSelectedHistVersion] = useState<number>(1);
  const [diffBaseVersion, setDiffBaseVersion] = useState<number | null>(null);

  return {
    splitPct,
    setSplitPct,
    isDraggingSplit,
    setIsDraggingSplit,
    historyCollapsed,
    setHistoryCollapsed,
    splitContainerRef,
    activeTab,
    setActiveTab,
    activeView,
    setActiveView,
    diagramZoom,
    setDiagramZoom,
    hoverNode,
    setHoverNode,
    activePacketFlow,
    setActivePacketFlow,
    selectedHistVersion,
    setSelectedHistVersion,
    diffBaseVersion,
    setDiffBaseVersion,
  };
}
