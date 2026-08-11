#!/usr/bin/env node
/**
 * Single-file build.
 *
 * Inlines every local module and the app icon into one self-contained HTML file
 * that can be opened straight from phone storage, with no server and no
 * sibling files.
 *
 * Deliberately dependency-free: no bundler, no npm install. The module graph
 * here is small, flat, and uses only named imports of local files, so
 * concatenating in dependency order and stripping the local import/export
 * keywords is sufficient and easy to audit.
 *
 * Caveat worth stating plainly: this inlines *our* code. The model runtime and
 * its weights are still fetched from the network on first use — hundreds of
 * megabytes cannot sensibly be embedded in an HTML file.
 *
 * Usage:  node build/bundle.mjs
 * Writes: dist/prompt-forge.html
 */

import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');

/** Dependency order: a module may only reference those above it. */
const MODULE_ORDER = [
  'src/vocab.js',
  'src/eras.js',
  'src/compiler.js',
  'src/vision.js',
  'src/app.js',
];

/** Matches a local (relative) import, including ones spanning several lines. */
const LOCAL_IMPORT = /^import\s+[\s\S]*?\s+from\s+['"]\.\/[^'"]+['"];?[ \t]*$/gm;

/** Leading `export` on a declaration. Local modules use no other export form. */
const EXPORT_KEYWORD = /^export\s+(?=(?:const|let|var|function|async|class)\b)/gm;

function stripModuleSyntax(source, name) {
  const withoutImports = source.replace(LOCAL_IMPORT, '');
  const withoutExports = withoutImports.replace(EXPORT_KEYWORD, '');

  // Fail loudly rather than emit a broken bundle.
  const leftoverImport = withoutExports.match(/^import\s+[^(]/m);
  if (leftoverImport) {
    throw new Error(`${name}: unhandled import statement — ${leftoverImport[0].trim()}`);
  }
  const leftoverExport = withoutExports.match(/^export\b/m);
  if (leftoverExport) {
    throw new Error(`${name}: unhandled export statement — ${leftoverExport[0].trim()}`);
  }
  return withoutExports.trim();
}

async function main() {
  const html = await readFile(join(root, 'index.html'), 'utf8');

  const modules = [];
  for (const name of MODULE_ORDER) {
    const source = await readFile(join(root, name), 'utf8');
    modules.push(`/* ===== ${name} ===== */\n${stripModuleSyntax(source, name)}`);
  }
  const bundled = modules.join('\n\n');

  const iconDataUri = `data:image/png;base64,${(
    await readFile(join(root, 'icons/icon-192.png'))
  ).toString('base64')}`;

  let out = html;

  // A lone HTML file has no sibling manifest or icon files.
  out = out.replace(/^[ \t]*<link rel="manifest"[^>]*>\n/m, '');
  out = out.replaceAll('./icons/icon-192.png', iconDataUri);

  // Replace the module <script src> with the inlined bundle.
  const scriptTag = /^[ \t]*<script type="module" src="\.\/src\/app\.js"><\/script>[ \t]*$/m;
  if (!scriptTag.test(out)) {
    throw new Error('index.html: could not find the module script tag to replace');
  }
  out = out.replace(
    scriptTag,
    `    <script type="module">\n${bundled}\n    </script>`,
  );

  out = out.replace(
    '<title>Perchance Prompt Forge</title>',
    '<title>Perchance Prompt Forge (standalone)</title>',
  );

  // There is no sibling sw.js either, so skip registration rather than letting
  // it 404 on every load. Applied after inlining, since the guard lives in the
  // bundled JS rather than in index.html.
  const swGuard = "'serviceWorker' in navigator";
  if (!out.includes(swGuard)) {
    throw new Error('app.js: service-worker guard not found — bundle would 404 on sw.js');
  }
  out = out.replace(swGuard, 'false /* standalone build: no sibling sw.js */');

  if (/src="\.\/src\//.test(out) || /href="\.\/manifest/.test(out)) {
    throw new Error('bundle still references external local files');
  }

  await mkdir(join(root, 'dist'), { recursive: true });
  const target = join(root, 'dist/prompt-forge.html');
  await writeFile(target, out, 'utf8');

  const kb = (Buffer.byteLength(out, 'utf8') / 1024).toFixed(1);
  console.log(`wrote dist/prompt-forge.html (${kb} kB, ${MODULE_ORDER.length} modules inlined)`);
}

main().catch((err) => {
  console.error(`bundle failed: ${err.message}`);
  process.exit(1);
});
