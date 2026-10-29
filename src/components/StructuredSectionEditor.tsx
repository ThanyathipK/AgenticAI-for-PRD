import { useEffect, useState, type TextareaHTMLAttributes } from 'react';

interface StructuredSectionEditorProps {
  value: string;
  onChange: (value: string) => void;
  sectionId?: string;
}

interface MarkdownTable {
  start: number;
  end: number;
  separator: number;
}

interface StructuredTextBlock {
  start: number;
  end: number;
  kind: 'heading' | 'field' | 'paragraph' | 'list-item';
  label: string;
  value: string;
  prefix?: string;
  headingLevel?: number;
}

/**
 * Keep the user's in-progress whitespace locally. The Markdown model normalizes
 * table padding, which would otherwise erase a just-typed trailing space before
 * the next character (and make Space/Enter appear broken).
 */
function EditableTextarea({ value, onChange, onFocus, onBlur, ...props }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  const externalValue = String(value ?? '');
  const [draft, setDraft] = useState(externalValue);
  const [focused, setFocused] = useState(false);

  useEffect(() => {
    if (!focused) setDraft(externalValue);
  }, [externalValue, focused]);

  return (
    <textarea
      {...props}
      value={draft}
      onFocus={(event) => {
        setFocused(true);
        onFocus?.(event);
      }}
      onBlur={(event) => {
        setFocused(false);
        onBlur?.(event);
      }}
      onChange={(event) => {
        setDraft(event.target.value);
        onChange?.(event);
      }}
    />
  );
}

const normalizeTableBoundary = (line: string) => line
  .trim()
  .replace(/^\\\|/, '|')
  .replace(/\\\|\\?$/, '|');

export const isTableLine = (line: string) => {
  const normalized = normalizeTableBoundary(line);
  return normalized.startsWith('|') && normalized.endsWith('|');
};

export const splitRow = (line: string): string[] => {
  const body = normalizeTableBoundary(line).replace(/^\|/, '').replace(/\|$/, '');
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

export const isSeparatorRow = (line: string) => {
  const cells = splitRow(line);
  // Match the preview renderer: Pandoc emits empty alignment cells for blank
  // columns (for example `|:----------------||`). Those are valid table
  // separators and must not send the whole section to the raw-text fallback.
  const isMarker = (cell: string) => /^:?-{1,}:?$/.test(cell);
  return cells.some(isMarker) && cells.every(cell => cell === '' || isMarker(cell));
};

export const findTables = (lines: string[]): MarkdownTable[] => {
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
  const normalized = populatedCells.map(cell => cleanLabel(cell).toLowerCase());
  return populatedCells.length >= 2 && (
    populatedCells.every(isBoldLabel)
    || (normalized.length === 2 && normalized[0] === 'term' && normalized[1] === 'definition')
  );
};

const isConventionalHeaderRow = (cells: string[]) => {
  const conventionalLabels = new Set([
    'role', 'name', 'version', 'date', 'author', 'description', 'signature',
    'topic', 'details', 'consideration', 'reference', 'appendix item',
    'scope in (included)', 'scope out (excluded)',
  ]);
  const labels = cells.map(cleanLabel).filter(Boolean);
  return labels.length === cells.length
    && labels.length > 0
    && labels.every(label => conventionalLabels.has(label.toLowerCase()));
};

const isDocumentControl = (line: string) =>
  /^\s*(?:<\/?div(?:\s[^>]*)?>|\\newpage|---+|___+|\*\*\*+|PRODUCT REQUIREMENT|KRUNGSRI NIMBLE CONFIDENTIAL)\s*$/i.test(line);

/**
 * Turn narrative Markdown into stable form fields without exposing its syntax.
 * Headings and document controls remain structural; only user content is editable.
 * Every editable block retains its original line prefix so saving still emits the
 * same Markdown contract consumed by section versioning and export.
 */
export const parseStructuredTextBlocks = (lines: string[]): StructuredTextBlock[] => {
  const blocks: StructuredTextBlock[] = [];
  let paragraphNumber = 0;
  let listNumber = 0;
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim() || isDocumentControl(line)) {
      index += 1;
      continue;
    }

    const heading = line.match(/^\s*(#{1,6})\s+(.+?)\s*$/);
    if (heading) {
      blocks.push({
        start: index,
        end: index,
        kind: 'heading',
        label: heading[2].replace(/\*\*/g, '').trim(),
        value: '',
        headingLevel: heading[1].length,
      });
      index += 1;
      continue;
    }

    const strongHeading = line.match(/^\s*\*\*(.+?)\*\*\\?\s*$/);
    if (strongHeading || line.trim().toLowerCase() === 'nimble by krungsri') {
      blocks.push({
        start: index,
        end: index,
        kind: 'heading',
        label: strongHeading?.[1] ?? line.trim(),
        value: '',
        headingLevel: strongHeading ? 4 : 2,
      });
      index += 1;
      continue;
    }

    const listItem = line.match(/^(\s*(?:[-+*]|\d+[.)])\s+)(.*)$/);
    if (listItem) {
      listNumber += 1;
      blocks.push({
        start: index,
        end: index,
        kind: 'list-item',
        label: `Item ${listNumber}`,
        value: listItem[2],
        prefix: listItem[1],
      });
      index += 1;
      continue;
    }

    const field = line.match(/^(\s*(?:\*\*)?[^:\n]{2,60}:(?:\*\*)?\s*)(.*)$/);
    if (field) {
      blocks.push({
        start: index,
        end: index,
        kind: 'field',
        label: field[1].replace(/\*\*/g, '').replace(/:\s*$/, '').trim(),
        value: field[2],
        prefix: field[1],
      });
      index += 1;
      continue;
    }

    const start = index;
    while (
      index + 1 < lines.length
      && lines[index + 1].trim()
      && !isDocumentControl(lines[index + 1])
      && !/^\s*#{1,6}\s+/.test(lines[index + 1])
      && !/^\s*(?:[-+*]|\d+[.)])\s+/.test(lines[index + 1])
      && !/^\s*(?:\*\*)?[^:\n]{2,60}:(?:\*\*)?\s*/.test(lines[index + 1])
    ) {
      index += 1;
    }
    paragraphNumber += 1;
    blocks.push({
      start,
      end: index,
      kind: 'paragraph',
      label: `Paragraph ${paragraphNumber}`,
      value: lines.slice(start, index + 1).join('\n'),
    });
    index += 1;
  }

  return blocks;
};

export function StructuredSectionEditor({ value, onChange, sectionId }: StructuredSectionEditorProps) {
  const lines = value.split('\n');
  const tables = findTables(lines);

  const updateCell = (lineIndex: number, cellIndex: number, nextValue: string) => {
    const nextLines = [...lines];
    const cells = splitRow(nextLines[lineIndex]);
    cells[cellIndex] = storageValue(nextValue);
    nextLines[lineIndex] = `| ${cells.join(' | ')} |`;
    onChange(nextLines.join('\n'));
  };

  if (sectionId === 'appendix') {
    const normalizedLabel = (cell: string) => cleanLabel(cell).replace(/\s+/g, ' ').toLowerCase();
    const fieldDefinitions = [
      { label: 'Open Questions & Risks', key: 'open questions & risks' },
      { label: 'Assumptions', key: 'assumptions' },
      { label: 'Related Documents', key: 'related documents' },
    ];
    const fields = fieldDefinitions.map(field => ({
      ...field,
      lineIndex: lines.findIndex(line => (
        isTableLine(line) && normalizedLabel(splitRow(line)[0] ?? '') === field.key
      )),
    }));
    const glossaryIndex = lines.findIndex(line => (
      isTableLine(line) && normalizedLabel(splitRow(line)[0] ?? '') === 'glossary'
    ));
    const relatedIndex = fields.find(field => field.key === 'related documents')?.lineIndex ?? -1;
    const glossaryRows = lines
      .map((line, lineIndex) => ({ line, lineIndex }))
      .filter(({ line, lineIndex }) => {
        if (!isTableLine(line) || glossaryIndex < 0 || lineIndex <= glossaryIndex) return false;
        if (relatedIndex >= 0 && lineIndex >= relatedIndex) return false;
        const cells = splitRow(line);
        if (isSeparatorRow(line) || cells.length < 3) return false;
        const term = normalizedLabel(cells[cells.length - 2] ?? '');
        const definition = normalizedLabel(cells[cells.length - 1] ?? '');
        return !(term === 'term' && definition === 'definition');
      });

    const renderField = (field: typeof fields[number]) => {
      if (field.lineIndex < 0) return null;
      const cells = splitRow(lines[field.lineIndex]);
      const fieldValue = displayValue(cells[1] ?? '');
      return (
        <div key={field.key} className="bg-white">
          <div className="border-b border-slate-200 bg-slate-50 px-3 py-2.5 text-xs font-semibold text-slate-700">
            {field.label}
          </div>
          <label className="block bg-white">
            <span className="block border-b border-slate-100 bg-slate-50/70 px-2.5 py-1.5 text-[10px] font-semibold uppercase tracking-wide text-slate-500">
              Details
            </span>
            <EditableTextarea
              aria-label={`${field.label} details`}
              value={fieldValue}
              onChange={(event) => updateCell(field.lineIndex, 1, event.target.value)}
              rows={Math.max(2, fieldValue.split('\n').length)}
              className="min-h-[76px] w-full resize-y bg-white px-3 py-2.5 text-sm leading-relaxed text-slate-700 outline-none transition focus:bg-orange-50/30 focus:ring-2 focus:ring-inset focus:ring-orange-500/20"
              placeholder={`Enter ${field.label.toLowerCase()}`}
            />
          </label>
        </div>
      );
    };

    return (
      <div className="space-y-4">
        <section className="overflow-hidden rounded-xl border border-slate-200 bg-white">
          {renderField(fields[0])}
          {renderField(fields[1])}
          {glossaryIndex >= 0 && (
            <div className="border-y border-slate-200 bg-slate-50 px-3 py-2.5 text-xs font-semibold text-slate-700">
              Glossary
            </div>
          )}
          {glossaryRows.map(({ line, lineIndex }, entryIndex) => {
            const cells = splitRow(line);
            const termIndex = cells.length - 2;
            const definitionIndex = cells.length - 1;
            return (
              <div key={lineIndex} className="bg-white">
                <div className="border-b border-slate-200 bg-slate-50 px-3 py-2 text-xs font-bold text-slate-700">
                  Glossary entry {entryIndex + 1}
                </div>
                <div className="grid grid-cols-1 divide-y divide-slate-200 sm:grid-cols-2 sm:divide-x sm:divide-y-0">
                  {[
                    { label: 'Term', index: termIndex },
                    { label: 'Definition', index: definitionIndex },
                  ].map(column => (
                    <label key={column.label} className="min-w-0 bg-white">
                      <span className="block border-b border-slate-100 bg-slate-50/70 px-2.5 py-1.5 text-[10px] font-semibold uppercase tracking-wide text-slate-500">
                        {column.label}
                      </span>
                      <EditableTextarea
                        aria-label={`Glossary entry ${entryIndex + 1} ${column.label.toLowerCase()}`}
                        value={displayValue(cells[column.index] ?? '')}
                        onChange={(event) => updateCell(lineIndex, column.index, event.target.value)}
                        rows={2}
                        className="min-h-[76px] w-full resize-y bg-white px-3 py-2.5 text-sm leading-relaxed text-slate-700 outline-none transition focus:bg-orange-50/30 focus:ring-2 focus:ring-inset focus:ring-orange-500/20"
                      />
                    </label>
                  ))}
                </div>
              </div>
            );
          })}
          {renderField(fields[2])}
        </section>
        <p className="text-[11px] text-slate-500">
          Empty Appendix fields remain editable. Line breaks are preserved in the saved PRD and exports.
        </p>
      </div>
    );
  }

  if (tables.length === 0) {
    const blocks = parseStructuredTextBlocks(lines);
    const editableBlocks = blocks.filter(block => block.kind !== 'heading');

    const updateBlock = (block: StructuredTextBlock, nextValue: string) => {
      const nextLines = [...lines];
      const replacement = block.kind === 'paragraph'
        ? nextValue.split('\n')
        : [`${block.prefix ?? ''}${storageValue(nextValue)}`];
      nextLines.splice(block.start, block.end - block.start + 1, ...replacement);
      onChange(nextLines.join('\n'));
    };

    return (
      <div className="space-y-4">
        {blocks.map((block) => {
          if (block.kind === 'heading') {
            const headingClass = (block.headingLevel ?? 3) <= 2
              ? 'border-b border-slate-100 pb-2 text-xl font-bold text-slate-800'
              : (block.headingLevel ?? 3) === 3
                ? 'text-lg font-bold text-slate-800'
                : 'text-base font-semibold text-slate-700';
            return <div key={`${block.start}-heading`} className={headingClass}>{block.label}</div>;
          }
          if (block.kind === 'field') {
            return (
              <label
                key={`${block.start}-field`}
                className="grid overflow-hidden rounded-lg border border-slate-200 bg-white sm:grid-cols-[minmax(150px,0.32fr)_1fr]"
              >
                <span className="border-b border-slate-200 bg-slate-50 px-3 py-2.5 text-xs font-semibold text-slate-700 sm:border-b-0 sm:border-r">
                  {block.label}
                </span>
                <EditableTextarea
                  value={displayValue(block.value)}
                  onChange={(event) => updateBlock(block, event.target.value)}
                  rows={1}
                  className="min-h-[42px] w-full resize-y bg-white px-3 py-2.5 text-sm leading-relaxed text-slate-700 outline-none transition focus:bg-orange-50/30 focus:ring-2 focus:ring-inset focus:ring-orange-500/20"
                  placeholder={`Enter ${block.label.toLowerCase()}`}
                />
              </label>
            );
          }
          if (block.kind === 'list-item') {
            const marker = block.prefix?.trim() || '•';
            if (sectionId === 'contents') {
              return (
                <div key={`${block.start}-list-item`} className="flex items-center gap-3 py-1 text-sm text-slate-700">
                  <span className="min-w-5 text-right font-semibold text-slate-500">{marker}</span>
                  <span>{block.value}</span>
                </div>
              );
            }
            return (
              <label key={`${block.start}-list-item`} className="flex items-start gap-2 text-sm text-slate-700">
                <span className="min-w-5 pt-2.5 text-right font-semibold text-slate-500">{marker}</span>
                <EditableTextarea
                  aria-label={block.label}
                  value={displayValue(block.value)}
                  onChange={(event) => updateBlock(block, event.target.value)}
                  rows={1}
                  className="min-h-[40px] flex-1 resize-y rounded-md border border-transparent bg-transparent px-2 py-2 leading-relaxed outline-none transition hover:border-slate-200 hover:bg-white focus:border-orange-400 focus:bg-white focus:ring-2 focus:ring-orange-500/15"
                  placeholder="Enter list item"
                />
              </label>
            );
          }
          return (
            <EditableTextarea
              key={`${block.start}-paragraph`}
              aria-label={block.label}
              value={block.value}
              onChange={(event) => updateBlock(block, event.target.value)}
              rows={Math.max(2, block.value.split('\n').length)}
              className="min-h-[54px] w-full resize-y rounded-md border border-transparent bg-transparent px-2 py-2 text-sm leading-relaxed text-slate-700 outline-none transition hover:border-slate-200 hover:bg-white focus:border-orange-400 focus:bg-white focus:ring-2 focus:ring-orange-500/15"
              placeholder="Enter paragraph"
            />
          );
        })}
        {editableBlocks.length === 0 && (
          <p className="rounded-lg border border-dashed border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-500">
            This section has no editable content yet.
          </p>
        )}
        <p className="text-[11px] text-slate-500">
          Headings and document structure stay fixed. Edit the fields below without changing Markdown syntax.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {tables.map((table, tableIndex) => {
        const headerRowIndex = table.separator - 1;
        const rawHeaderCells = splitRow(lines[headerRowIndex]);
        const conventionalHeader = isConventionalHeaderRow(rawHeaderCells);
        const tableHeaders = rawHeaderCells.map(cleanLabel);
        const rowsAfterSeparator = Array.from(
          { length: table.end - table.separator },
          (_, offset) => table.separator + 1 + offset,
        );
        const embeddedHeaderRow = rowsAfterSeparator.find(lineIndex =>
          isEmbeddedHeaderRow(splitRow(lines[lineIndex])),
        );
        const embeddedHeaders = embeddedHeaderRow === undefined
          ? []
          : splitRow(lines[embeddedHeaderRow]).map(cleanLabel);
        const headers = tableHeaders.map((header, cellIndex) =>
          conventionalHeader
            ? header
            : embeddedHeaders[cellIndex] || header || '',
        );
        const sectionHeading = lines
          .slice(0, table.start)
          .reverse()
          .find(line => /^#{1,3}\s+/.test(line.trim()))
          ?.trim()
          .replace(/^#{1,3}\s+/, '') ?? '';
        const precedingHeading = lines
          .slice(0, table.start)
          .reverse()
          .find(line => /^#{4,6}\s+/.test(line.trim()));
        const precedingHeadingLabel = precedingHeading
          ?.trim()
          .replace(/^#{4,6}\s+/, '') ?? '';
        // Imported/Pandoc Markdown can discard the empty right-hand header of
        // the scope table. The nearby fixed heading still identifies it, so
        // keep treating it as a two-column Scope In/Scope Out editor.
        const isScopeComparison = (
          headers[0]?.toLowerCase().startsWith('scope in')
          && (
            headers[1]?.toLowerCase().startsWith('scope out')
            || precedingHeadingLabel.toLowerCase().includes('scope definition')
          )
        ) || precedingHeadingLabel.toLowerCase().includes('scope definition');
        if (isScopeComparison) {
          headers[0] = 'Scope In (Included)';
          headers[1] = 'Scope Out (Excluded)';
        }
        const hasFixedFirstColumn =
          !isScopeComparison && (
            sectionId === 'title'
            || headers[0]?.toLowerCase() === 'appendix item'
            || sectionHeading === 'Version History'
            || sectionHeading === 'Reviews'
            || sectionHeading.includes('Appendix')
            || sectionHeading.includes('Product Scope & Functional Requirements')
          );
        const tableTitle = precedingHeading
          ? precedingHeadingLabel
          : tables.length === 1 ? 'Section fields' : `Table ${tableIndex + 1}`;
        // Several official-template tables place the first editable field row
        // immediately before the Markdown separator. Only true column-label
        // rows (Role/Name, Version/Date/...) should be omitted from the form.
        const dataRows = [
          ...(conventionalHeader || rawHeaderCells.every(cell => !cell.trim()) ? [] : [headerRowIndex]),
          ...rowsAfterSeparator,
        ].filter(lineIndex => (
          lineIndex !== embeddedHeaderRow
          && (
            splitRow(lines[lineIndex]).some(cell => cleanLabel(cell).length > 0)
            // An empty Scope In/Scope Out row is still the editable value row.
            // Dropping it made a blank project show headings with no boxes.
            || (isScopeComparison && lineIndex > table.separator)
          )
        ));
        return (
          <section key={`${table.start}-${table.end}`} className="overflow-hidden rounded-xl border border-slate-200 bg-white">
            {tables.length > 1 && (
              <div className="border-b border-slate-200 bg-slate-50 px-3 py-2 text-[11px] font-bold uppercase tracking-wide text-slate-600">
                {tableTitle}
              </div>
            )}
            <div className="divide-y divide-slate-200">
              {dataRows.map((lineIndex, rowIndex) => {
                const parsedCells = splitRow(lines[lineIndex]);
                const isTemplateLeadRow = lineIndex === headerRowIndex && !conventionalHeader;
                const rowLabel = cleanLabel(parsedCells[0] || '').toLowerCase();
                const isSingleValueTemplateRow = [
                  'user flow & wireframes',
                  'wireframes',
                  'scope definition (in vs. out)',
                  'open questions & risks',
                  'assumptions',
                  'related documents',
                ].includes(rowLabel);
                // Pandoc can collapse an empty merged template row to only its
                // label cell (not even an empty value cell remains). Supply the
                // editable value slot in the UI; updateCell will materialize it
                // back into the Markdown row on the first keystroke.
                const minimumCellCount = isScopeComparison ? 2 : isSingleValueTemplateRow ? 2 : 0;
                const cells = minimumCellCount > parsedCells.length
                  ? [...parsedCells, ...Array(minimumCellCount - parsedCells.length).fill('')]
                  : parsedCells;
                const isStructuralLabelRow = rowLabel === 'glossary';
                if (isSingleValueTemplateRow && !isScopeComparison) {
                  const fieldLabel = cleanLabel(cells[0]) || `Field ${rowIndex + 1}`;
                  const fieldValue = displayValue(cells[1] ?? '');
                  return (
                    <div key={lineIndex} className="bg-white">
                      <div className="border-b border-slate-200 bg-slate-50 px-3 py-2.5 text-xs font-semibold text-slate-700">
                        {fieldLabel}
                      </div>
                      <label className="block bg-white">
                        <span className="block border-b border-slate-100 bg-slate-50/70 px-2.5 py-1.5 text-[10px] font-semibold uppercase tracking-wide text-slate-500">
                          Details
                        </span>
                        <EditableTextarea
                          aria-label={`${fieldLabel} details`}
                          value={fieldValue}
                          onChange={(event) => updateCell(lineIndex, 1, event.target.value)}
                          rows={Math.max(2, fieldValue.split('\n').length)}
                          className="min-h-[58px] w-full resize-y bg-white px-3 py-2.5 text-sm leading-relaxed text-slate-700 outline-none transition focus:bg-orange-50/30 focus:ring-2 focus:ring-inset focus:ring-orange-500/20"
                          placeholder={`Enter ${fieldLabel.toLowerCase()}`}
                        />
                      </label>
                    </div>
                  );
                }
                if (cells.length === 2 && !isScopeComparison) {
                  return (
                    <div key={lineIndex} className="grid grid-cols-1 bg-white sm:grid-cols-[minmax(170px,0.38fr)_1fr]">
                      <div className="border-b border-slate-200 bg-slate-50 px-3 py-2.5 text-xs font-semibold text-slate-700 sm:border-b-0 sm:border-r">
                        {cleanLabel(cells[0]) || headers[0] || `Field ${rowIndex + 1}`}
                      </div>
                      <EditableTextarea
                        value={displayValue(cells[1])}
                        onChange={(event) => updateCell(lineIndex, 1, event.target.value)}
                        rows={Math.max(1, displayValue(cells[1]).split('\n').length)}
                        aria-label={cleanLabel(cells[0]) || headers[1] || `Value ${rowIndex + 1}`}
                        className="min-h-[42px] w-full resize-y bg-white px-3 py-2.5 text-sm leading-relaxed text-slate-700 outline-none transition focus:bg-orange-50/30 focus:ring-2 focus:ring-inset focus:ring-orange-500/20"
                        placeholder={`Enter ${cleanLabel(cells[0]) || 'value'}`}
                      />
                    </div>
                  );
                }
                const recordTitle = cleanLabel(cells[0] || '')
                  || (sectionId === 'product_scope' ? `Requirement ${rowIndex + 1}` : `Entry ${rowIndex + 1}`);
                return (
                  <div key={lineIndex} className="bg-white">
                    {hasFixedFirstColumn && (
                      <div className="border-b border-slate-200 bg-slate-50 px-3 py-2 text-xs font-bold text-slate-700">
                        {recordTitle}
                      </div>
                    )}
                    <div className="grid grid-cols-1 divide-y divide-slate-200 sm:grid-cols-2 sm:divide-x sm:divide-y-0">
                    {cells.map((cell, cellIndex) => {
                      const columnLabel = headers[cellIndex] || `Column ${cellIndex + 1}`;
                      if (cellIndex === 0 && hasFixedFirstColumn) {
                        return null;
                      }
                      if (
                        isTemplateLeadRow
                        && cellIndex > 0
                        && !cell.trim()
                        && !(isSingleValueTemplateRow && cellIndex === 1)
                      ) {
                        return null;
                      }
                      if (
                        (sectionId === 'product_scope' || sectionId === 'appendix')
                        && !isScopeComparison
                        && rowLabel
                        && cellIndex > 0
                        && !cell.trim()
                        && !(isSingleValueTemplateRow && cellIndex === 1)
                      ) {
                        return null;
                      }
                      if (
                        (isSingleValueTemplateRow && cellIndex > 1)
                        || (isStructuralLabelRow && cellIndex > 0)
                      ) {
                        return null;
                      }
                      const inlineField = cell.match(/^([^:]{2,40}:)\s*(.*)$/);
                      const editorLabel = inlineField
                        ? cleanLabel(inlineField[1]).replace(/:$/, '')
                        : isSingleValueTemplateRow && cellIndex === 1
                          ? 'Details'
                          : columnLabel;
                      const editorValue = inlineField ? inlineField[2] : cell;
                      return (
                        <label key={cellIndex} className="min-w-0 bg-white">
                          <span className="block border-b border-slate-100 bg-slate-50/70 px-2.5 py-1.5 text-[10px] font-semibold uppercase tracking-wide text-slate-500">{editorLabel}</span>
                          <EditableTextarea
                            aria-label={editorLabel}
                            value={displayValue(editorValue)}
                            onChange={(event) => updateCell(
                              lineIndex,
                              cellIndex,
                              inlineField
                                ? `${inlineField[1]} ${event.target.value}`
                                : event.target.value,
                            )}
                            rows={2}
                            className="min-h-[58px] w-full resize-y bg-white px-2.5 py-2 text-sm font-normal normal-case tracking-normal text-slate-700 outline-none transition focus:bg-orange-50/30 focus:ring-2 focus:ring-inset focus:ring-orange-500/20"
                          />
                        </label>
                      );
                    })}
                    </div>
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
