import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

export function validateCatalog(root = new URL('../', import.meta.url)) {
  const catalog = JSON.parse(readFileSync(new URL('catalog.json', root), 'utf8'));
  const subjects = ['physics', 'chemistry', 'mathematics'];
  if (Object.keys(catalog).sort().join() !== subjects.toSorted().join()) {
    throw new Error('The catalog must contain exactly the three note subjects.');
  }
  for (const subject of subjects) {
    const chapters = catalog[subject];
    const directory = new URL(`${subject}/`, root);
    const folders = readdirSync(directory, { withFileTypes: true })
      .filter((entry) => entry.isDirectory() && !entry.name.startsWith('.'))
      .map((entry) => entry.name).sort();
    if (!Array.isArray(chapters) || !chapters.length ||
        chapters.map((chapter) => chapter.folderName).sort().join('\n') !== folders.join('\n')) {
      throw new Error(`Chapter folders do not match the ${subject} catalog.`);
    }
    for (const [index, chapter] of chapters.entries()) {
      if (chapter.number !== index + 1 || !Number.isInteger(chapter.pageCount) ||
          chapter.pageCount < 1 || typeof chapter.title !== 'string' || !chapter.title.trim() ||
          !chapter.folderName.startsWith(`${chapter.number}.`)) {
        throw new Error(`Invalid ${subject} chapter ${index + 1}.`);
      }
      const pages = readdirSync(new URL(`${encodeURIComponent(chapter.folderName)}/`, directory))
        .filter((name) => /\.jpe?g$/i.test(name)).sort();
      const expected = Array.from({ length: chapter.pageCount }, (_, i) => `${String(i + 1).padStart(4, '0')}.jpg`);
      if (pages.join('\n') !== expected.join('\n')) {
        throw new Error(`Page files do not match ${subject}/${chapter.folderName}.`);
      }
    }
  }
  return catalog;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const catalog = validateCatalog();
  const chapters = Object.values(catalog).flat();
  console.log(`Catalog verified: ${chapters.length} chapters, ${chapters.reduce((sum, c) => sum + c.pageCount, 0)} pages.`);
}
