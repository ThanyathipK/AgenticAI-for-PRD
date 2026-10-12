import { useCallback, useEffect, useId, useState } from 'react';
import { AlertTriangle, Download, FileCode, ImageDown, LoaderCircle, Network } from 'lucide-react';
import { ProjectState } from '../hooks/useProjectState';
import { Tooltip } from './Tooltip';

let mermaidPromise: ReturnType<typeof importMermaid> | null = null;

async function importMermaid() {
  const { default: mermaid } = await import('mermaid');
  mermaid.initialize({
    startOnLoad: false,
    securityLevel: 'strict',
    theme: 'base',
    themeVariables: {
      primaryColor: '#ffffff',
      primaryTextColor: '#171717',
      primaryBorderColor: '#8a6a50',
      lineColor: '#8a6a50',
      secondaryColor: '#ece7dc',
      tertiaryColor: '#f7f5f0',
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
    },
    flowchart: { htmlLabels: false, curve: 'basis', useMaxWidth: true },
  });
  return mermaid;
}

function loadMermaid() {
  mermaidPromise ??= importMermaid();
  return mermaidPromise;
}

interface MermaidDiagramProps {
  source: string;
  zoom: number;
  onError: (message: string | null) => void;
  onRendered: (svg: string) => void;
}

function MermaidDiagram({ source, zoom, onError, onRendered }: MermaidDiagramProps) {
  const reactId = useId();
  const [svg, setSvg] = useState('');

  useEffect(() => {
    let active = true;
    setSvg('');
    onRendered('');
    onError(null);

    const render = async () => {
      try {
        const mermaid = await loadMermaid();
        const id = `project-flow-${reactId.replace(/[^a-zA-Z0-9_-]/g, '')}`;
        const result = await mermaid.render(id, source);
        if (active) {
          setSvg(result.svg);
          onRendered(result.svg);
        }
      } catch (error) {
        if (!active) return;
        onError(error instanceof Error ? error.message : 'The Mermaid source could not be rendered.');
      }
    };

    void render();
    return () => {
      active = false;
    };
  }, [onError, onRendered, reactId, source]);

  if (!svg) {
    return (
      <div className="flex min-h-72 items-center justify-center gap-2 text-sm text-on-surface-variant">
        <LoaderCircle className="h-5 w-5 animate-spin text-primary" />
        Rendering flowchart…
      </div>
    );
  }

  return (
    <div
      className="min-w-full transition-transform duration-300 [&_svg]:mx-auto [&_svg]:h-auto [&_svg]:max-w-none"
      style={{ transform: `scale(${zoom / 100})`, transformOrigin: 'top center' }}
      // Mermaid sanitizes labels in strict security mode. This SVG is the
      // library's intended browser output and never includes raw user HTML.
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  );
}

export function ArchitectureFlows({ state }: { state: ProjectState }) {
  const {
    mermaidDiagram,
    diagramZoom,
    setDiagramZoom,
    isLoading,
    isProcessing,
    projectId,
    projects,
  } = state;
  const [renderError, setRenderError] = useState<string | null>(null);
  const [renderedSvg, setRenderedSvg] = useState('');
  const handleRenderError = useCallback((message: string | null) => setRenderError(message), []);
  const handleRendered = useCallback((svg: string) => setRenderedSvg(svg), []);
  const source = mermaidDiagram.trim();
  const isWaitingForFirstDiagram = !source && (isLoading || isProcessing);
  const projectName = projects.find((project) => project.id === projectId)?.name || 'project';
  const safeProjectName = projectName
    .trim()
    .replace(/[^a-zA-Z0-9._-]+/g, '_')
    .replace(/^_+|_+$/g, '') || 'project';

  const downloadBlob = (blob: Blob, filename: string) => {
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  };

  const handleDownloadMarkdown = () => {
    if (!source) return;
    const markdown = `# ${projectName} — Project Flowchart\n\n\`\`\`mermaid\n${source}\n\`\`\`\n`;
    downloadBlob(
      new Blob([markdown], { type: 'text/markdown;charset=utf-8' }),
      `${safeProjectName}_flowchart.md`,
    );
  };

  const handleDownloadDiagram = () => {
    if (!renderedSvg) return;
    downloadBlob(
      new Blob([renderedSvg], { type: 'image/svg+xml;charset=utf-8' }),
      `${safeProjectName}_flowchart.svg`,
    );
  };

  return (
    <div className="space-y-3 animate-fadeIn">
      <div className="relative overflow-hidden rounded-[12px] border border-outline bg-white p-4">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-x-4 gap-y-2 border-b border-black/5 pb-3">
          <div className="flex min-w-0 items-center gap-2">
            <Network className="h-5 w-5 shrink-0 text-primary" />
            <div className="min-w-0">
              <h3 className="text-sm font-bold text-on-surface">Project Flowchart</h3>
            </div>
          </div>

          {source && (
            <div className="flex shrink-0 items-center gap-2">
              {renderedSvg && (
                <Tooltip label="Download rendered diagram as SVG" side="bottom">
                  <button
                    type="button"
                    onClick={handleDownloadDiagram}
                    className="inline-flex cursor-pointer items-center gap-1.5 rounded-lg border border-outline bg-white px-2.5 py-1.5 text-xs font-semibold text-on-surface transition-colors hover:border-primary/40 hover:text-primary"
                    aria-label="Download diagram as SVG"
                  >
                    <ImageDown className="h-3.5 w-3.5" />
                    Diagram
                  </button>
                </Tooltip>
              )}
              <Tooltip label="Download Mermaid as Markdown" side="bottom">
                <button
                  type="button"
                  onClick={handleDownloadMarkdown}
                  className="inline-flex cursor-pointer items-center gap-1.5 rounded-lg border border-outline bg-white px-2.5 py-1.5 text-xs font-semibold text-on-surface transition-colors hover:border-primary/40 hover:text-primary"
                  aria-label="Download diagram as Markdown"
                >
                  <Download className="h-3.5 w-3.5" />
                  Markdown
                </button>
              </Tooltip>
              {!renderError && (
                <div className="flex rounded-xl bg-black/5 p-1">
                  <Tooltip label="Zoom out" side="bottom">
                    <button
                      onClick={() => setDiagramZoom((previous) => Math.max(70, previous - 15))}
                      aria-label="Zoom out"
                      className="cursor-pointer rounded px-2.5 py-1 text-xs font-semibold transition-all hover:bg-white"
                    >
                      −
                    </button>
                  </Tooltip>
                  <span className="flex items-center px-3 font-mono text-xs font-bold">{diagramZoom}%</span>
                  <Tooltip label="Zoom in" side="bottom">
                    <button
                      onClick={() => setDiagramZoom((previous) => Math.min(150, previous + 15))}
                      aria-label="Zoom in"
                      className="cursor-pointer rounded px-2.5 py-1 text-xs font-semibold transition-all hover:bg-white"
                    >
                      +
                    </button>
                  </Tooltip>
                </div>
              )}
            </div>
          )}
        </div>

        <div className="min-h-72 w-full overflow-auto rounded-[10px] border border-outline bg-[#f8f6f2] p-4 custom-scrollbar sm:p-5">
          {isWaitingForFirstDiagram ? (
            <div className="flex min-h-72 flex-col items-center justify-center gap-2 text-center">
              <LoaderCircle className="h-7 w-7 animate-spin text-primary" />
              <span className="text-sm font-bold text-on-surface">
                {isLoading ? 'Loading project flowchart…' : 'Generating project flowchart…'}
              </span>
              <p className="max-w-sm text-xs text-on-surface-variant">
                The Architect is building the Mermaid flow from this project's requirements and user stories.
              </p>
            </div>
          ) : renderError ? (
            <div className="flex min-h-72 flex-col items-center justify-center gap-2 text-center">
              <AlertTriangle className="h-7 w-7 text-red-600" />
              <span className="text-sm font-bold text-on-surface">Flowchart could not be rendered</span>
              <p className="max-w-lg text-xs text-on-surface-variant">
                The saved Mermaid source is invalid. Generate the PRD again to refresh the project flowchart.
              </p>
              <p className="max-w-lg break-words font-mono text-[10px] text-red-700">{renderError}</p>
            </div>
          ) : source ? (
            <MermaidDiagram
              source={source}
              zoom={diagramZoom}
              onError={handleRenderError}
              onRendered={handleRendered}
            />
          ) : (
            <div className="flex min-h-72 flex-col items-center justify-center gap-2 text-center">
              <FileCode className="h-7 w-7 text-primary/40" />
              <span className="text-sm font-bold text-on-surface">No flowchart generated yet</span>
              <p className="max-w-sm text-xs text-on-surface-variant">
                Add and validate requirements, then use Generate PRD. The Architect will create and save a Mermaid flowchart for this project.
              </p>
            </div>
          )}
        </div>
      </div>

      {source && (
        <details className="rounded-[12px] border border-outline bg-white p-4">
          <summary className="flex cursor-pointer list-none items-center gap-2 text-sm font-bold text-on-surface">
            <FileCode className="h-4.5 w-4.5 text-primary" />
            Mermaid source
            <span className="rounded border border-primary/20 bg-primary/10 px-2 py-0.5 font-mono text-[10px] font-bold uppercase text-primary">
              Saved with project
            </span>
          </summary>
          <pre className="mt-3 overflow-x-auto rounded-[10px] border border-[#5b21b6] bg-[#2f343c] p-4 font-mono text-xs text-[#6fd08c]">
            <code>{source}</code>
          </pre>
        </details>
      )}
    </div>
  );
}
