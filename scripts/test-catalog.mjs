import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { validateCatalog } from './check-catalog.mjs';

function fixture(t, change) {
  const root = mkdtempSync(join(tmpdir(), 'pcm-catalog-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const catalog = Object.fromEntries(['physics', 'chemistry', 'mathematics'].map((subject) => {
    mkdirSync(join(root, subject, '1.Introduction'), { recursive: true });
    writeFileSync(join(root, subject, '1.Introduction', '0001.jpg'), 'fixture');
    return [subject, [{ number: 1, title: 'Introduction', folderName: '1.Introduction', pageCount: 1 }]];
  }));
  change?.(catalog, root);
  writeFileSync(join(root, 'catalog.json'), JSON.stringify(catalog));
  return pathToFileURL(`${root}/`);
}

test('the tracked catalog matches every source page', () => assert.equal(Object.values(validateCatalog()).flat().length, 91));
test('accepts a complete archive', (t) => assert.ok(validateCatalog(fixture(t))));
test('rejects stale page counts', (t) => assert.throws(() => validateCatalog(fixture(t, (c) => c.physics[0].pageCount++)), /Page files/));
test('rejects missing chapters', (t) => assert.throws(() => validateCatalog(fixture(t, (c) => c.physics = [])), /Chapter folders/));
test('rejects duplicate chapter numbering', (t) => assert.throws(() => validateCatalog(fixture(t, (c) => c.physics[0].number = 2)), /Invalid physics/));
test('rejects gaps in page filenames', (t) => assert.throws(() => validateCatalog(fixture(t, (_, root) => {
  rmSync(join(root, 'physics', '1.Introduction', '0001.jpg'));
  writeFileSync(join(root, 'physics', '1.Introduction', '0002.jpg'), 'fixture');
})), /Page files/));
test('rejects unexpected subjects', (t) => assert.throws(() => validateCatalog(fixture(t, (c) => c.biology = [])), /three note subjects/));
