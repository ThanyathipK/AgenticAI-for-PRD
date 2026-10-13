// ============================================================================
// Unit tests — every exported function that is testable without a browser DOM
// or a live backend:
//
//   src/utils/time.ts                    formatDate, formatRelativeUpdated
//   src/utils/markdown.ts                getSafeSectionContent, getSafeSectionTitle,
//                                        parsePRDToSections, stitchSectionsToPRD,
//                                        formatUserStoriesToMarkdown
//   src/utils/highlight.ts               highlightMatch
//   src/api/transforms.ts                all payload -> domain transforms
//   src/hooks/useArtifactLocks.ts        isLockableRequirementId, sortLockTargets,
//                                        toLockTargets (pure exports)
//   src/schema/parseDdl.ts               parseDdl
//   src/api/client.ts                    detailFromBlobError, detailFromJsonError
//   src/components/Toast.tsx             notify, handleError, handleWarning
//   src/components/MarkdownRenderer.tsx  renderInlineFormatting
//   src/hooks/useRequirementStore.ts     getPrdTemplateMarkdown (axios stubbed)
//
// React *hooks* (useChat, useProjects, ...) need a renderer/DOM host and are
// intentionally out of scope here.
//
// Run:  npx tsx scripts/unit.test.ts
// (same convention as scripts/requirement_locks.test.ts — node:test via tsx)
// ============================================================================
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { formatDate, formatRelativeUpdated } from '../src/utils/time';
import {
  getSafeSectionContent,
  getSafeSectionTitle,
  parsePRDToSections,
  stitchSectionsToPRD,
  formatUserStoriesToMarkdown,
} from '../src/utils/markdown';
import { highlightMatch } from '../src/utils/highlight';
import {
  DEFAULT_EPIC_FALLBACK,
  DEFAULT_FAILED_CHECKS,
  DEFAULT_PASSED_CHECKS,
  epicNameFromRequirements,
  toRequirementItem,
  toRequirementItems,
  toStructuredRequirementsFromGathered,
  toUserStories,
  toUserStory,
} from '../src/api/transforms';
import {
  isLockableRequirementId,
  sortLockTargets,
  toLockTargets,
} from '../src/hooks/useArtifactLocks';
import { parseDdl } from '../src/schema/parseDdl';
import { detailFromBlobError, detailFromJsonError } from '../src/api/client';
import { handleError, handleWarning, notify } from '../src/components/Toast';
import { renderInlineFormatting } from '../src/components/MarkdownRenderer';
import { getPrdTemplateMarkdown } from '../src/hooks/useRequirementStore';
import type { UserStory, RequirementItem } from '../src/components/types';
import type { RequirementPayload, UserStoryPayload } from '../src/api/types';

// ----------------------------------------------------------------------------
// src/utils/time.ts — all inputs are LOCAL wall-clock strings so the suite is
// deterministic in every timezone.
// ----------------------------------------------------------------------------
const NOW = new Date('2024-01-15T12:00:00');

test('formatDate: empty string for missing or invalid input', () => {
  assert.equal(formatDate(null), '');
  assert.equal(formatDate(undefined), '');
  assert.equal(formatDate('not-a-date'), '');
});

test('formatDate: renders YYYY-MM-DD with zero padding', () => {
  assert.equal(formatDate('2024-01-15'), '2024-01-15');
  assert.equal(formatDate('2024-03-05T09:07:00'), '2024-03-05');
});

test('formatRelativeUpdated: hyphen for missing or invalid input', () => {
  assert.equal(formatRelativeUpdated(null), '-');
  assert.equal(formatRelativeUpdated(undefined), '-');
  assert.equal(formatRelativeUpdated('garbage', NOW), '-');
});

test('formatRelativeUpdated: "Just now" under a minute', () => {
  assert.equal(formatRelativeUpdated('2024-01-15T12:00:00', NOW), 'Just now');
  assert.equal(formatRelativeUpdated('2024-01-15T11:59:59', NOW), 'Just now');
});

test('formatRelativeUpdated: humanized minutes', () => {
  assert.equal(formatRelativeUpdated('2024-01-15T11:55:00', NOW), '5 min ago');
  assert.equal(formatRelativeUpdated('2024-01-15T11:01:00', NOW), '59 min ago');
});

test('formatRelativeUpdated: humanized hours under a day', () => {
  assert.equal(formatRelativeUpdated('2024-01-15T11:00:00', NOW), '1 hr ago');
  assert.equal(formatRelativeUpdated('2024-01-15T06:00:00', NOW), '6 hr ago');
  assert.equal(formatRelativeUpdated('2024-01-14T13:00:00', NOW), '23 hr ago');
});

test('formatRelativeUpdated: humanized days under a week (singular/plural)', () => {
  assert.equal(formatRelativeUpdated('2024-01-14T12:00:00', NOW), '1 day ago');
  assert.equal(formatRelativeUpdated('2024-01-09T12:00:00', NOW), '6 days ago');
});

test('formatRelativeUpdated: a week or older falls back to the calendar date', () => {
  assert.equal(formatRelativeUpdated('2024-01-08T12:00:00', NOW), '2024-01-08');
  assert.equal(formatRelativeUpdated('2023-12-01T00:00:00', NOW), '2023-12-01');
});

test('formatRelativeUpdated: future timestamps render the calendar date', () => {
  assert.equal(formatRelativeUpdated('2024-01-16T12:00:00', NOW), '2024-01-16');
});

// ----------------------------------------------------------------------------
// src/utils/markdown.ts
// ----------------------------------------------------------------------------
test('getSafeSectionContent: null/undefined -> empty string', () => {
  assert.equal(getSafeSectionContent(null), '');
  assert.equal(getSafeSectionContent(undefined), '');
});

test('getSafeSectionContent: strings pass through untouched', () => {
  assert.equal(getSafeSectionContent('hello'), 'hello');
  assert.equal(getSafeSectionContent(''), '');
});

test('getSafeSectionContent: unwraps {content} and {markdown} wrappers recursively', () => {
  assert.equal(getSafeSectionContent({ content: 'inner' }), 'inner');
  assert.equal(getSafeSectionContent({ markdown: 'md body' }), 'md body');
  assert.equal(getSafeSectionContent({ content: { markdown: 'deep' } }), 'deep');
});

test('getSafeSectionContent: plain objects become key/value markdown lines', () => {
  assert.equal(getSafeSectionContent({ a: 1, b: true }), '**a:** 1\n\n**b:** true');
  assert.equal(getSafeSectionContent({ nested: { x: 1 } }), '**nested:** {"x":1}');
});

test('getSafeSectionContent: other primitives via String()', () => {
  assert.equal(getSafeSectionContent(42), '42');
  assert.equal(getSafeSectionContent(true), 'true');
});

test('getSafeSectionTitle: "Untitled Section" for missing titles', () => {
  assert.equal(getSafeSectionTitle(null), 'Untitled Section');
  assert.equal(getSafeSectionTitle(undefined), 'Untitled Section');
});

test('getSafeSectionTitle: strings pass through, primitives via String()', () => {
  assert.equal(getSafeSectionTitle('Roadmap'), 'Roadmap');
  assert.equal(getSafeSectionTitle(7), '7');
});

test('getSafeSectionTitle: unwraps {title} then {name} recursively', () => {
  assert.equal(getSafeSectionTitle({ title: 'T' }), 'T');
  assert.equal(getSafeSectionTitle({ name: 'N' }), 'N');
  assert.equal(getSafeSectionTitle({ title: { name: 'deep' } }), 'deep');
  assert.equal(getSafeSectionTitle({ a: 1 }), '{"a":1}');
});

test('parsePRDToSections: empty input yields no sections', () => {
  assert.deepEqual(parsePRDToSections(''), []);
  assert.deepEqual(parsePRDToSections(null), []);
});

test('parsePRDToSections: plain text stays one title section', () => {
  const sections = parsePRDToSections('Just some intro text');
  assert.equal(sections.length, 1);
  assert.equal(sections[0].id, 'title');
  assert.equal(sections[0].title, 'Product Requirement Document');
  assert.equal(sections[0].content, 'Just some intro text');
});

test('parsePRDToSections: branded cover heading stays in the title part', () => {
  const sections = parsePRDToSections([
    'PRODUCT REQUIREMENT',
    '',
    '## Nimble by Krungsri',
    'PMO No: PMO-001',
    '### Stakeholders',
    '|Role|Name|',
  ].join('\n'));
  assert.deepEqual(sections.map((section) => section.id), ['title', 'stakeholders']);
  assert.match(sections[0].content, /Nimble by Krungsri/);
  assert.match(sections[0].content, /PMO No: PMO-001/);
});

test('parsePRDToSections: blank markdown keeps the empty title section', () => {
  const sections = parsePRDToSections('   ');
  assert.equal(sections.length, 1);
  assert.equal(sections[0].id, 'title');
  assert.equal(sections[0].content, '');
});

test('parsePRDToSections: ## and ### headings split sections and slug unknown titles', () => {
  const md = [
    'Intro paragraph',
    '',
    '## Executive Summary',
    'Short overview.',
    '### Stakeholders',
    '- PO',
    '## Deep Dive Notes',
  ].join('\n');
  const sections = parsePRDToSections(md);
  assert.deepEqual(
    sections.map((s) => s.id),
    ['title', 'exec_summary', 'stakeholders', 'deep_dive_notes'],
  );
  assert.equal(sections[0].content, 'Intro paragraph');
  assert.equal(sections[1].title, 'Executive Summary');
  assert.equal(sections[1].content, '## Executive Summary\nShort overview.');
  assert.equal(sections[2].title, 'Stakeholders');
  assert.equal(sections[3].title, 'Deep Dive Notes');
});

test('parsePRDToSections: maps every template heading to its canonical section id', () => {
  const md = [
    '## 1. Business & Strategic Overview',
    '## Technical Architecture',
    '## Scope of Requirements (User Stories)',
    '## Version History',
    '## Review & Approval',
    '## Table of Contents',
    '## Product Scope',
    '## Technical & Operational Constraints',
    '## Appendix A',
  ].join('\n');
  assert.deepEqual(parsePRDToSections(md).map((s) => s.id), [
    'title',
    'business_overview',
    'tech_arch',
    'user_stories',
    'version_history',
    'reviews',
    'contents',
    'product_scope',
    'tech_ops',
    'appendix',
  ]);
});

test('parsePRDToSections: unknown headings become slugs (stars stripped)', () => {
  const sections = parsePRDToSections('## *Roadmap* (v2)\nbody');
  assert.equal(sections.length, 2);
  assert.equal(sections[1].id, 'roadmap_v2');
});

test('parsePRDToSections: empty sections (title included) are kept as-is', () => {
  const sections = parsePRDToSections('## Empty\n## Filled\ncontent here');
  assert.deepEqual(
    sections.map((s) => s.id),
    ['title', 'empty', 'filled'],
  );
  assert.equal(sections[0].content, '');
  // A section body includes its own heading line, so a heading-only section
  // carries exactly that heading as content.
  assert.equal(sections[1].content, '## Empty');
  assert.equal(sections[2].content, '## Filled\ncontent here');
});

test('stitchSectionsToPRD: joins section bodies with blank lines', () => {
  assert.equal(
    stitchSectionsToPRD([
      { id: 'a', title: 'A', content: 'one' },
      { id: 'b', title: 'B', content: 'two' },
      { id: 'c', title: 'C', content: '' },
    ]),
    'one\n\ntwo\n\n',
  );
});

const STORY: UserStory = {
  ticket_code: 'US-001',
  story_title: 'User login',
  as_a: 'registered user',
  i_want_to: 'log in securely',
  so_that: 'my data stays private',
  acceptance_criteria: ['Shows an error on bad password', 'Redirects to dashboard'],
};

test('formatUserStoriesToMarkdown: empty list renders the placeholder', () => {
  assert.equal(
    formatUserStoriesToMarkdown([]),
    '## 3. Scope of Requirements (User Stories)\nNo user stories registered yet.',
  );
});

test('formatUserStoriesToMarkdown: renders stories with acceptance criteria', () => {
  assert.equal(
    formatUserStoriesToMarkdown([STORY]),
    '## 3. Scope of Requirements (User Stories)\n' +
      '\n### US-001: User login\n' +
      '**As a** registered user\n' +
      '**I want to** log in securely\n' +
      '**So that** my data stays private\n\n' +
      '**Acceptance Criteria:**\n' +
      '- Shows an error on bad password\n' +
      '- Redirects to dashboard\n',
  );
});

test('formatUserStoriesToMarkdown: omits the criteria block when none exist', () => {
  const md = formatUserStoriesToMarkdown([{ ...STORY, acceptance_criteria: [] }]);
  assert.ok(!md.includes('Acceptance Criteria'));
  assert.ok(md.includes('### US-001: User login'));
});

// ----------------------------------------------------------------------------
// src/utils/highlight.ts
// ----------------------------------------------------------------------------
type MarkLike = { type: string; props: { className?: string; children: unknown } };
const isMark = (node: unknown): node is MarkLike =>
  typeof node === 'object' && node !== null && (node as { type?: unknown }).type === 'mark';

test('highlightMatch: no query or no match returns the text unchanged', () => {
  assert.equal(highlightMatch('Hello world', ''), 'Hello world');
  assert.equal(highlightMatch('Hello world', '   '), 'Hello world');
  assert.equal(highlightMatch('', 'hello'), '');
  assert.equal(highlightMatch('Hello world', 'zzz'), 'Hello world');
});

test('highlightMatch: wraps matches case-insensitively in a themed <mark>', () => {
  const out = highlightMatch('Hello World', 'world') as unknown[];
  assert.ok(Array.isArray(out));
  assert.equal(out.length, 3);
  assert.equal(out[0], 'Hello ');
  assert.ok(isMark(out[1]));
  assert.equal(out[1].props.children, 'World');
  assert.equal(out[2], '');
});

test('highlightMatch: match at the start of the text', () => {
  const out = highlightMatch('World hello', 'world') as unknown[];
  assert.equal(out.length, 2);
  assert.ok(isMark(out[0]));
  assert.equal(out[0].props.children, 'World');
  assert.equal(out[1], ' hello');
});

test('highlightMatch: highlights every occurrence', () => {
  const out = highlightMatch('abc abc', 'abc') as unknown[];
  assert.equal(out.filter(isMark).length, 2);
  assert.equal(out[1], ' ');
  assert.equal(out[3], '');
});

test('highlightMatch: custom highlight class is forwarded to the <mark>', () => {
  const out = highlightMatch('abc', 'a', 'custom-class') as unknown[];
  assert.ok(isMark(out[0]));
  assert.equal(out[0].props.className, 'custom-class');
});

// ----------------------------------------------------------------------------
// src/api/transforms.ts — payload -> domain transforms
// ----------------------------------------------------------------------------
test('transforms: default constants keep their documented values', () => {
  assert.equal(DEFAULT_EPIC_FALLBACK, 'Structured Requirements Draft');
  assert.deepEqual(DEFAULT_PASSED_CHECKS, [
    'Financial Regulatory Compliance',
    'Security & Data Masking',
  ]);
  assert.deepEqual(DEFAULT_FAILED_CHECKS, [
    'Idempotency & De-duplication',
    'Network Timeouts & Retry Strategies',
  ]);
});

test('toUserStory: normalizes missing acceptance criteria to []', () => {
  assert.deepEqual(
    toUserStory({ ticket_code: 'US-1', story_title: 'T', as_a: 'A', i_want_to: 'I', so_that: 'S' }),
    {
      ticket_code: 'US-1', story_title: 'T', as_a: 'A', i_want_to: 'I', so_that: 'S',
      acceptance_criteria: [],
    },
  );
  const withCriteria = toUserStory({
    ticket_code: 'US-2', story_title: 'T2', as_a: 'A', i_want_to: 'I', so_that: 'S',
    acceptance_criteria: ['c1'],
  });
  assert.deepEqual(withCriteria.acceptance_criteria, ['c1']);
});

test('toUserStories: null/undefined/non-array input -> []', () => {
  assert.deepEqual(toUserStories(undefined), []);
  assert.deepEqual(toUserStories(null), []);
  assert.deepEqual(toUserStories({} as unknown as UserStoryPayload[]), []);
  const mapped = toUserStories([
    { ticket_code: 'US-1', story_title: 'T', as_a: 'A', i_want_to: 'I', so_that: 'S' },
  ]);
  assert.equal(mapped.length, 1);
  assert.equal(mapped[0].ticket_code, 'US-1');
});

test('toRequirementItem: applies every default for sparse payloads', () => {
  assert.deepEqual(toRequirementItem({ requirement_code: 'REQ-1', title: 'T' }), {
    id: undefined,
    requirement_code: 'REQ-1',
    title: 'T',
    description: '',
    user_stories: [],
    status: 'active',
    is_locked: false,
    locked_by: undefined,
    locked_at: undefined,
    lock_reason: undefined,
  });
});

test('toRequirementItem: keeps lock metadata, status and nested stories', () => {
  const item = toRequirementItem({
    id: 'uuid-1',
    requirement_code: 'REQ-2',
    title: 'T2',
    description: 'D',
    status: 'archived',
    is_locked: true,
    locked_by: 'hpo',
    locked_at: '2024-01-01T00:00:00Z',
    user_stories: [
      { ticket_code: 'US-9', story_title: 'S', as_a: 'A', i_want_to: 'I', so_that: 'S', acceptance_criteria: ['c'] },
    ],
  });
  assert.equal(item.id, 'uuid-1');
  assert.equal(item.description, 'D');
  assert.equal(item.status, 'archived');
  assert.equal(item.is_locked, true);
  assert.equal(item.locked_by, 'hpo');
  assert.equal(item.locked_at, '2024-01-01T00:00:00Z');
  assert.deepEqual(item.user_stories[0].acceptance_criteria, ['c']);
});

test('toRequirementItems: undefined when the payload has no requirements array', () => {
  assert.equal(toRequirementItems(undefined), undefined);
  assert.equal(toRequirementItems(null), undefined);
  assert.equal(toRequirementItems('nope' as unknown as never), undefined);
  const items = toRequirementItems([{ requirement_code: 'R', title: 'T' }]);
  assert.equal(items?.length, 1);
  assert.equal(items?.[0].requirement_code, 'R');
});

test('epicNameFromRequirements: array/object/fallback precedence', () => {
  const rows = [
    { requirement_code: 'R1', title: 'First' },
    { requirement_code: 'R2', title: 'Second' },
  ];
  assert.equal(epicNameFromRequirements(rows), 'First');
  assert.equal(epicNameFromRequirements([]), DEFAULT_EPIC_FALLBACK);
  assert.equal(epicNameFromRequirements([{ requirement_code: 'R', title: '' }]), DEFAULT_EPIC_FALLBACK);
  assert.equal(epicNameFromRequirements({ epic_name: 'Epic X' }), 'Epic X');
  assert.equal(epicNameFromRequirements({ epic_name: '' }), DEFAULT_EPIC_FALLBACK);
  assert.equal(epicNameFromRequirements(null, 'Custom'), 'Custom');
  assert.equal(epicNameFromRequirements(undefined), DEFAULT_EPIC_FALLBACK);
});

test('toStructuredRequirementsFromGathered: null payload -> empty defaults', () => {
  assert.deepEqual(toStructuredRequirementsFromGathered(null), {
    epic_name: '',
    version: 1,
    user_stories: [],
    requirements: undefined,
  });
});

test('toStructuredRequirementsFromGathered: uses the payload version or the fallback', () => {
  const gathered = { epic_name: 'E', version: 4, user_stories: [], requirements: [] };
  const result = toStructuredRequirementsFromGathered(gathered);
  assert.equal(result.version, 4);
  assert.equal(result.epic_name, 'E');
  assert.deepEqual(result.user_stories, []);
  assert.deepEqual(result.requirements, []);
  assert.equal(toStructuredRequirementsFromGathered({ epic_name: 'E' }, 9).version, 9);
});

// ----------------------------------------------------------------------------
// src/hooks/useArtifactLocks.ts — the three pure (non-hook) exports
// ----------------------------------------------------------------------------
const UUID = '123e4567-e89b-42d3-a456-426614174000';

test('isLockableRequirementId: only real UUIDs are lockable', () => {
  assert.equal(isLockableRequirementId(UUID), true);
  assert.equal(isLockableRequirementId(UUID.toUpperCase()), true);
  assert.equal(isLockableRequirementId('REQ-001'), false);
  assert.equal(isLockableRequirementId('123e4567-e89b-42d3-a456-42661417400'), false);
  assert.equal(isLockableRequirementId(''), false);
  assert.equal(isLockableRequirementId(undefined), false);
  assert.equal(isLockableRequirementId(null), false);
});

const lockRow = (code: string, status: string): RequirementItem => ({
  id: undefined,
  requirement_code: code,
  title: code,
  description: '',
  user_stories: [],
  status,
  is_locked: false,
  locked_by: undefined,
  locked_at: undefined,
});

test('sortLockTargets: active first, then archived, each by numeric code then lexicographic', () => {
  const input = [
    lockRow('REQ-10', 'archived'),
    lockRow('REQ-2', 'active'),
    lockRow('REQ-1', 'active'),
    lockRow('REQ-2', 'archived'),
    lockRow('REQ-2A', 'active'),
    lockRow('XYZ', 'active'),
  ];
  const sorted = sortLockTargets(input);
  assert.deepEqual(
    sorted.map((r) => `${r.requirement_code}:${r.status}`),
    ['REQ-1:active', 'REQ-2:active', 'REQ-2A:active', 'XYZ:active', 'REQ-2:archived', 'REQ-10:archived'],
  );
  // The input array must not be mutated.
  assert.equal(input[0].requirement_code, 'REQ-10');
});

test('sortLockTargets: empty input -> empty output', () => {
  assert.deepEqual(sortLockTargets([]), []);
});

test('toLockTargets: drops deleted rows, keeps archived, orders lock-ready', () => {
  const rows: RequirementPayload[] = [
    { requirement_code: 'REQ-3', title: 'Archived', status: 'archived' },
    { requirement_code: 'REQ-1', title: 'Active' },
    { requirement_code: 'REQ-2', title: 'Deleted', status: 'deleted' },
  ];
  assert.deepEqual(
    toLockTargets(rows).map((r) => r.requirement_code),
    ['REQ-1', 'REQ-3'],
  );
  assert.deepEqual(toLockTargets(null), []);
  assert.deepEqual(toLockTargets(undefined), []);
  assert.deepEqual(toLockTargets('nope' as unknown as RequirementPayload[]), []);
});

// ----------------------------------------------------------------------------
// src/schema/parseDdl.ts
// ----------------------------------------------------------------------------
const DDL = [
  'CREATE EXTENSION IF NOT EXISTS "uuid-ossp";',
  '',
  '-- a leading comment',
  'CREATE TABLE projects (',
  "    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(), -- row identity",
  '    name VARCHAR(255) NOT NULL,',
  '    description TEXT,',
  '    owner_email VARCHAR(255) UNIQUE NOT NULL,',
  '    created_at TIMESTAMPTZ DEFAULT NOW()',
  ');',
  '',
  'CREATE TABLE stories (',
  '    id UUID PRIMARY KEY,',
  '    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,',
  "    metadata JSONB DEFAULT '{\"tier\": 1}'::jsonb",
  ');',
  '',
  'CREATE INDEX idx_stories_project ON stories(project_id, created_at);',
  'ALTER TABLE projects ADD CONSTRAINT uq_projects_name UNIQUE (name, owner_email);',
].join('\n');

test('parseDdl: empty input -> no tables', () => {
  assert.deepEqual(parseDdl(''), []);
  assert.deepEqual(parseDdl('   \n  \n'), []);
});

test('parseDdl: parses tables, columns, constraints and defaults', () => {
  const tables = parseDdl(DDL);
  assert.deepEqual(
    tables.map((t) => t.name),
    ['projects', 'stories'],
  );

  const projects = tables[0];
  assert.deepEqual(
    projects.columns.map((c) => c.name),
    ['id', 'name', 'description', 'owner_email', 'created_at'],
  );
  assert.deepEqual(projects.columns[0], {
    name: 'id',
    type: 'UUID',
    constraints: 'PRIMARY KEY',
    defaultValue: 'uuid_generate_v4()',
  });
  assert.deepEqual(projects.columns[1], { name: 'name', type: 'VARCHAR(255)', constraints: 'NOT NULL' });
  assert.deepEqual(projects.columns[2], { name: 'description', type: 'TEXT', constraints: 'NULL' });
  assert.deepEqual(projects.columns[3], {
    name: 'owner_email',
    type: 'VARCHAR(255)',
    constraints: 'UNIQUE NOT NULL',
  });
  assert.deepEqual(projects.columns[4], {
    name: 'created_at',
    type: 'TIMESTAMPTZ',
    constraints: 'NULL',
    defaultValue: 'NOW()',
  });
  assert.deepEqual(projects.relations, []);
});

test('parseDdl: collects CREATE INDEX and ALTER TABLE UNIQUE labels per table', () => {
  const tables = parseDdl(DDL);
  assert.deepEqual(tables[0].indexes, ['uq_projects_name (name, owner_email)']);
  assert.deepEqual(tables[1].indexes, ['idx_stories_project (project_id, created_at)']);
});

test('parseDdl: extracts foreign-key relations with referential actions', () => {
  const stories = parseDdl(DDL)[1];
  assert.deepEqual(stories.relations, [
    { fromColumn: 'project_id', toTable: 'projects', toColumn: 'id', onDelete: 'CASCADE' },
  ]);
});

test('parseDdl: maps unsupported ON DELETE actions to NONE', () => {
  const tables = parseDdl(
    [
      'CREATE TABLE locks (',
      '    id UUID PRIMARY KEY,',
      '    tenant_id UUID REFERENCES tenants(id) ON DELETE SET NULL,',
      '    other_id UUID REFERENCES things(id) ON DELETE RESTRICT,',
      '    weird_id UUID REFERENCES misc(id) ON DELETE NO ACTION',
      ');',
    ].join('\n'),
  );
  assert.deepEqual(
    tables[0].relations.map((r) => [r.toTable, r.onDelete]),
    [
      ['tenants', 'SET NULL'],
      ['things', 'RESTRICT'],
      ['misc', 'NONE'],
    ],
  );
});

test('parseDdl: string literals survive comment stripping', () => {
  const tables = parseDdl(DDL);
  assert.equal(tables[1].columns[2].defaultValue, "'{\"tier\": 1}'::jsonb");
});

// ----------------------------------------------------------------------------
// src/api/client.ts — error-detail decoders
// ----------------------------------------------------------------------------
test('detailFromJsonError: null for non-axios errors and axios errors without detail', () => {
  assert.equal(detailFromJsonError(new Error('plain')), null);
  assert.equal(detailFromJsonError({ isAxiosError: true, response: { data: {} } }), null);
  assert.equal(detailFromJsonError({ isAxiosError: true }), null);
  assert.equal(detailFromJsonError(undefined), null);
});

test('detailFromJsonError: surfaces a plain-string FastAPI detail', () => {
  const err = { isAxiosError: true, response: { data: { detail: 'Project name already exists' } } };
  assert.equal(detailFromJsonError(err), 'Project name already exists');
});

test('detailFromJsonError: whitespace-only string details are unusable', () => {
  const err = { isAxiosError: true, response: { data: { detail: '   ' } } };
  assert.equal(detailFromJsonError(err), null);
});

test('detailFromJsonError: 422 issue lists use the first readable msg (prefix stripped)', () => {
  const err = {
    isAxiosError: true,
    response: {
      data: {
        detail: [
          { msg: 'Field required', loc: ['body', 'name'] },
          { msg: 'Value error, epic name is too long' },
        ],
      },
    },
  };
  assert.equal(detailFromJsonError(err), 'Field required');

  const prefixed = {
    isAxiosError: true,
    response: { data: { detail: [{ msg: 'value error,  bad input' }] } },
  };
  assert.equal(detailFromJsonError(prefixed), 'bad input');

  const noMsgs = {
    isAxiosError: true,
    response: { data: { detail: [{}, { msg: '   ' }] } },
  };
  assert.equal(detailFromJsonError(noMsgs), null);
});

test('detailFromBlobError: null for non-axios errors and non-Blob bodies', async () => {
  assert.equal(await detailFromBlobError(new Error('plain')), null);
  assert.equal(await detailFromBlobError({ isAxiosError: true, response: { data: 'json' } }), null);
  assert.equal(await detailFromBlobError({ isAxiosError: true }), null);
  assert.equal(await detailFromBlobError(undefined), null);
});

test('detailFromBlobError: decodes a Blob body carrying {"detail": "..."}', async () => {
  const blob = new Blob([JSON.stringify({ detail: 'PDF engine unavailable' })], {
    type: 'application/json',
  });
  const err = { isAxiosError: true, response: { data: blob } };
  assert.equal(await detailFromBlobError(err), 'PDF engine unavailable');
});

test('detailFromBlobError: null when the Blob JSON has no string detail or is invalid', async () => {
  const noDetail = { isAxiosError: true, response: { data: new Blob(['{"error": 1}']) } };
  assert.equal(await detailFromBlobError(noDetail), null);
  const invalid = { isAxiosError: true, response: { data: new Blob(['not json']) } };
  assert.equal(await detailFromBlobError(invalid), null);
});

import { api } from '../src/api/client';

// ----------------------------------------------------------------------------
// src/components/Toast.tsx — module-level toast store helpers
// (The <ToastHost /> component itself needs a DOM; the three triggers do not.)
// ----------------------------------------------------------------------------
interface ScheduledCall {
  fn: unknown;
  ms: number | undefined;
}

/** Runs `fn` with console + setTimeout patched so tests stay silent and timer-free. */
async function withPatchedEnv(fn: (env: { scheduled: ScheduledCall[] }) => void | Promise<void>) {
  const scheduled: ScheduledCall[] = [];
  const originalTimeout = globalThis.setTimeout;
  const originalError = console.error;
  const originalWarn = console.warn;
  (globalThis as { setTimeout: unknown }).setTimeout = ((innerFn: unknown, ms?: number) => {
    scheduled.push({ fn: innerFn, ms });
    return 0 as unknown as ReturnType<typeof setTimeout>;
  }) as typeof setTimeout;
  console.error = () => {};
  console.warn = () => {};
  try {
    await fn({ scheduled });
  } finally {
    globalThis.setTimeout = originalTimeout;
    console.error = originalError;
    console.warn = originalWarn;
  }
}

test('notify: schedules auto-dismiss with the default 5s, never for duration 0', async () => {
  await withPatchedEnv(({ scheduled }) => {
    notify('Saved', 'success');
    notify('No timer', 'info', 0);
    assert.equal(scheduled.length, 1);
    assert.equal(scheduled[0].ms, 5000);
    assert.equal(notify('Again', 'warning', 10), undefined);
    assert.equal(scheduled.length, 2);
  });
});

test('handleError: logs context + error via console.error and raises an error toast', async () => {
  await withPatchedEnv(({ scheduled }) => {
    const logged: unknown[][] = [];
    console.error = (...args: unknown[]) => logged.push(args);
    const err = new Error('boom');
    handleError('Save failed', err);
    handleError('No error object');
    assert.deepEqual(logged, [['Save failed', err], ['No error object', undefined]]);
    // Both calls notified with the default duration -> two dismissal timers.
    assert.equal(scheduled.length, 2);
  });
});

test('handleWarning: logs context + error via console.warn and raises a warning toast', async () => {
  await withPatchedEnv(({ scheduled }) => {
    const logged: unknown[][] = [];
    console.warn = (...args: unknown[]) => logged.push(args);
    const err = 'stale state';
    handleWarning('Fallback used', err);
    assert.deepEqual(logged, [['Fallback used', err]]);
    assert.equal(scheduled.length, 1);
  });
});

// ----------------------------------------------------------------------------
// src/components/MarkdownRenderer.tsx — renderInlineFormatting
// ----------------------------------------------------------------------------
type El = { type: string; props: { className?: string; children: unknown } };
const isEl = (node: unknown, type: string): node is El =>
  typeof node === 'object' && node !== null && (node as El).type === type;

test('renderInlineFormatting: empty text renders nothing', () => {
  assert.equal(renderInlineFormatting(''), '');
});

test('renderInlineFormatting: plain text without query is a single text node', () => {
  assert.deepEqual(renderInlineFormatting('plain text'), ['plain text']);
});

test('renderInlineFormatting: paired **bold** becomes <strong>, unmatched stays literal', () => {
  const out = renderInlineFormatting('**bold** tail') as unknown[];
  assert.equal(out.length, 2);
  assert.ok(isEl(out[0], 'strong'));
  assert.equal(out[0].props.children, 'bold');
  assert.equal(out[1], ' tail');

  assert.deepEqual(renderInlineFormatting('5 ** 3'), ['5 ** 3']);
});

test('renderInlineFormatting: `code` spans become styled <code> chips', () => {
  const out = renderInlineFormatting('a `x` b') as unknown[];
  assert.equal(out.length, 3);
  assert.equal(out[0], 'a ');
  assert.ok(isEl(out[1], 'code'));
  assert.equal(out[1].props.children, 'x');
  assert.ok(String(out[1].props.className).includes('font-mono'));
  assert.equal(out[2], ' b');
});

test('renderInlineFormatting: bold and code compose on one line', () => {
  const out = renderInlineFormatting('Use **bold** and `code` now') as unknown[];
  assert.deepEqual(
    out.map((n) =>
      typeof n === 'string' ? n : isEl(n, 'strong') ? '<strong>' : isEl(n, 'code') ? '<code>' : '?',
    ),
    ['Use ', '<strong>', ' and ', '<code>', ' now'],
  );
});

test('renderInlineFormatting: highlight query is forwarded into bold segments', () => {
  const out = renderInlineFormatting('**World**', 'world', 'hl-class') as unknown[];
  assert.ok(isEl(out[0], 'strong'));
  const children = out[0].props.children as unknown[];
  assert.ok(Array.isArray(children));
  // The match starts at index 0, so the <mark> leads and a trailing '' follows.
  assert.ok(isMark(children[0]));
  assert.equal(children[0].props.children, 'World');
  assert.equal(children[0].props.className, 'hl-class');
  assert.equal(children[1], '');
});

// ----------------------------------------------------------------------------
// src/hooks/useRequirementStore.ts — getPrdTemplateMarkdown (api-level stub).
// The store keeps a process-wide template cache, so the SUCCESS path runs on a
// fresh module instance (query-busted dynamic import) while the FAILURE path
// runs on the main instance, whose cache the failure poisons to ''.
// ----------------------------------------------------------------------------
test('getPrdTemplateMarkdown: fetches once, maps template_latex, then caches', async () => {
  let calls = 0;
  api.getPrdTemplate = (async () => {
    calls += 1;
    return { template_latex: '# PRD Template' };
  }) as never;

  // Query-busted dynamic import: keeping the specifier in a variable means
  // tsc cannot statically resolve it (so --noEmit passes), while tsx/node
  // still treat the query string as part of the module key and hand back a
  // fresh instance alongside the regular top-level import of the same module.
  const freshSpecifier = '../src/hooks/useRequirementStore?fresh=1';
  const fresh = (await import(freshSpecifier)) as {
    getPrdTemplateMarkdown: () => Promise<string>;
  };
  assert.equal(await fresh.getPrdTemplateMarkdown(), '# PRD Template');
  assert.equal(await fresh.getPrdTemplateMarkdown(), '# PRD Template');
  assert.equal(calls, 1, 'the second call must be served from the module cache');
});

test('getPrdTemplateMarkdown: an unreachable backend degrades to an empty template', async () => {
  let calls = 0;
  api.getPrdTemplate = (async () => {
    calls += 1;
    throw new Error('backend down');
  }) as never;

  // First call hits the (stubbed) backend and swallows the error.
  assert.equal(await getPrdTemplateMarkdown(), '');
  // The empty template is cached, so no further fetches happen.
  assert.equal(await getPrdTemplateMarkdown(), '');
  assert.equal(calls, 1);
});
