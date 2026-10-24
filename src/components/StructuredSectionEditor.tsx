import { Table2 } from 'lucide-react';

interface StructuredSectionEditorProps {
  value: string;
  onChange: (value: string) => void;
}

interface MarkdownTable {
  start: number;
  end: number;
  separator: number;
}

const isTableLine = (line: string) => line.trim().startsWith('|') && line.trim().endsWith('|');

const splitRow = (line: string): string[] => {
  const body = line.trim().replace(/^\|/, '').replace(/\|$/, '');
  const cells: string[] = [];
  let current = '';
  let escaped = false;
  for (const character of body) {
    if (character === '|' && !escaped) {
      cells.push(current.trim());
      current = '';
      continue;
    }
    current += character;
    escaped = character === '\\' && !escaped;
    if (character !== '\\') escaped = false;
  }
  cells.push(current.trim());
  return cells;
};

const isSeparatorRow = (line: string) => {
  const cells = splitRow(line);
  return cells.length > 0 && cells.every(cell => /^:?-{3,}:?$/.test(cell));
};

const findTables = (lines: string[]): MarkdownTable[] => {
  const tables: MarkdownTable[] = [];
  let index = 0;
  while (index < lines.length) {
    if (!isTableLine(lines[index])) {
      index += 1;
      continue;
    }
    const start = index;
    while (index < lines.length && isTableLine(lines[index])) index += 1;
    const end = index - 1;
    const separator = lines.findIndex((line, lineIndex) =>
      lineIndex >= start && lineIndex <= end && isSeparatorRow(line));
    if (separator > start) tables.push({ start, end, separator });
  }
  return tables;
};

const cleanLabel = (value: string) => value
  .replace(/\*\*/g, '')
  .replace(/<br\s*\/?\s*>/gi, ' ')
  .replace(/\\newline/g, ' ')
  .trim();

const displayValue = (value: string) => value
  .replace(/<br\s*\/?\s*>/gi, '\n')
  .replace(/\\newline\s*/g, '\n')
  .replace(/\\\|/g, '|')
  .trim();

const storageValue = (value: string) => value
  .replace(/\|/g, '\\|')
  .replace(/\r?\n/g, '<br>');

const isBoldLabel = (value: string) => /^\*\*[^*]+\*\*$/.test(value.trim());

const isEmbeddedHeaderRow = (cells: string[]) => {
  const populatedCells = cells.filter(cell => cell.trim());
  return populatedCells.length >= 2 && populatedCells.every(isBoldLabel);
};

export function StructuredSectionEditor({ value, onChange }: StructuredSectionEditorProps) {
  const lines = value.split('\n');
  const tables = findTables(lines);

  if (tables.length === 0) {
    return (
      <div className="space-y-2">
        <label className="text-xs font-semibold text-slate-700">Section content</label>
        <textarea
          value={value}
          onChange={(event) => onChange(event.target.value)}
          rows={10}
          className="w-full text-sm font-sans text-slate-800 bg-white border border-outline rounded-[10px] p-4 focus:ring-2 focus:ring-orange-500/20 focus:border-orange-500 outline-none transition-all resize-y custom-scrollbar min-h-[180px]"
          placeholder="Enter section content"
        />
      </div>
    );
  }

  const updateCell = (lineIndex: number, cellIndex: number, nextValue: string) => {
    const nextLines = [...lines];
    const cells = splitRow(nextLines[lineIndex]);
    cells[cellIndex] = storageValue(nextValue);
    nextLines[lineIndex] = `| ${cells.join(' | ')} |`;
    onChange(nextLines.join('\n'));
  };

  return (
    <div className="space-y-4">
      {tables.map((table, tableIndex) => {
        const tableHeaders = splitRow(lines[table.separator - 1]).map(cleanLabel);
        const allDataRows = Array.from(
          { length: table.end - table.separator },
          (_, offset) => table.separator + 1 + offset,
        );
        const embeddedHeaderRow = allDataRows.find(lineIndex =>
          isEmbeddedHeaderRow(splitRow(lines[lineIndex])),
        );
        const embeddedHeaders = embeddedHeaderRow === undefined
          ? []
          : splitRow(lines[embeddedHeaderRow]).map(cleanLabel);
        const headers = tableHeaders.map((header, cellIndex) =>
          header || embeddedHeaders[cellIndex] || '',
        );
        const isScopeComparison =
          headers[0]?.toLowerCase().startsWith('scope in')
          && headers[1]?.toLowerCase().startsWith('scope out');
        const sectionHeading = lines
          .slice(0, table.start)
          .reverse()
          .find(line => /^#{1,3}\s+/.test(line.trim()))
          ?.trim()
          .replace(/^#{1,3}\s+/, '') ?? '';
        const hasFixedFirstColumn =
          !isScopeComparison && (
            headers[0]?.toLowerCase() === 'appendix item'
            || sectionHeading === 'Version History'
            || sectionHeading === 'Reviews'
            || sectionHeading.includes('Product Scope & Functional Requirements')
          );
        const precedingHeading = lines
          .slice(0, table.start)
          .reverse()
          .find(line => /^#{4,6}\s+/.test(line.trim()));
        const tableTitle = precedingHeading
          ? precedingHeading.trim().replace(/^#{4,6}\s+/, '')
          : tables.length === 1 ? 'Section fields' : `Table ${tableIndex + 1}`;
        const dataRows = allDataRows.filter(lineIndex => lineIndex !== embeddedHeaderRow);
        return (
          <section key={`${table.start}-${table.end}`} className="overflow-hidden rounded-xl border border-slate-200 bg-white">
            <div className="flex items-center gap-2 border-b border-slate-200 bg-slate-50 px-3.5 py-2.5">
              <Table2 className="h-4 w-4 text-primary" />
              <span className="text-xs font-bold text-slate-700">
                {tableTitle}
              </span>
            </div>
            <div className="divide-y divide-slate-100">
              {dataRows.map((lineIndex, rowIndex) => {
                const cells = splitRow(lines[lineIndex]);
                if (cells.length === 2 && !isScopeComparison) {
                  return (
                    <div key={lineIndex} className="grid grid-cols-1 gap-2 p-3 sm:grid-cols-[minmax(170px,0.38fr)_1fr] sm:items-start">
                      <div className="pt-2 text-xs font-semibold text-slate-700">
                        {cleanLabel(cells[0]) || headers[0] || `Field ${rowIndex + 1}`}
                      </div>
                      <textarea
                        value={displayValue(cells[1])}
                        onChange={(event) => updateCell(lineIndex, 1, event.target.value)}
                        rows={Math.max(1, displayValue(cells[1]).split('\n').length)}
                        aria-label={cleanLabel(cells[0]) || headers[1] || `Value ${rowIndex + 1}`}
                        className="min-h-[38px] w-full resize-y rounded-lg border border-slate-200 bg-[#fcfbf9] px-3 py-2 text-sm text-slate-800 outline-none transition focus:border-orange-500 focus:ring-2 focus:ring-orange-500/15"
                        placeholder={`Enter ${cleanLabel(cells[0]) || 'value'}`}
                      />
                    </div>
                  );
                }
                return (
                  <div key={lineIndex} className="grid gap-2 p-3" style={{ gridTemplateColumns: `repeat(${Math.max(cells.length, 1)}, minmax(130px, 1fr))` }}>
                    {cells.map((cell, cellIndex) => {
                      const columnLabel = headers[cellIndex] || `Column ${cellIndex + 1}`;
                      if (cellIndex === 0 && hasFixedFirstColumn) {
                        return (
                          <div key={cellIndex} className="min-w-0 text-[10px] font-semibold uppercase tracking-wide text-slate-500">
                            {columnLabel}
                            <div className="mt-1.5 min-h-[58px] w-full px-2.5 py-2 text-sm font-semibold normal-case tracking-normal text-slate-800">
                              {displayValue(cell)}
                            </div>
                          </div>
                        );
                      }
                      return (
                        <label key={cellIndex} className="min-w-0 text-[10px] font-semibold uppercase tracking-wide text-slate-500">
                          {columnLabel}
                          <textarea
                            value={displayValue(cell)}
                            onChange={(event) => updateCell(lineIndex, cellIndex, event.target.value)}
                            rows={2}
                            className="mt-1.5 min-h-[58px] w-full resize-y rounded-lg border border-slate-200 bg-[#fcfbf9] px-2.5 py-2 text-sm font-normal normal-case tracking-normal text-slate-800 outline-none transition focus:border-orange-500 focus:ring-2 focus:ring-orange-500/15"
                          />
                        </label>
                      );
                    })}
                  </div>
                );
              })}
            </div>
          </section>
        );
      })}
      <p className="text-[11px] text-slate-500">
        The document layout and table headings stay fixed. Edit the field values; line breaks are preserved in the saved PRD.
      </p>
    </div>
  );
}
