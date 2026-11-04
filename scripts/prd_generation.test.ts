import assert from 'node:assert/strict';
import { test } from 'node:test';
import { hasManualPrdEditsAtRisk } from '../src/utils/prdGeneration';
import type { PrdSectionLockState } from '../src/components/types';

const section: PrdSectionLockState = {
  content_source: 'ai', ai_generatable: true, is_locked: false, review_status: 'draft',
};
const sections = [{ id: 'scope', title: 'Scope', content: 'Saved text' }];

test('generation warns only for human content that regeneration can replace', () => {
  for (const content_source of ['ai', 'template', 'human']) {
    for (const is_locked of [true, false]) {
      for (const ai_generatable of [true, false]) {
        assert.equal(hasManualPrdEditsAtRisk({ scope: {
          ...section, content_source, is_locked, ai_generatable,
        } }, sections, null, ''), content_source === 'human' && !is_locked && ai_generatable);
      }
    }
  }
  assert.equal(hasManualPrdEditsAtRisk({}, [], null, ''), false);
});

test('unsaved manual edits warn even when lock metadata is unavailable', () => {
  assert.equal(hasManualPrdEditsAtRisk({ scope: section }, sections, 'scope', 'Changed'), true);
  assert.equal(hasManualPrdEditsAtRisk({}, sections, 'scope', 'Changed'), true);
  assert.equal(hasManualPrdEditsAtRisk({ scope: section }, sections, 'scope', 'Saved text'), false);
  assert.equal(hasManualPrdEditsAtRisk({ scope: { ...section, is_locked: true } }, sections, 'scope', 'Changed'), false);
});
