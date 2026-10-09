// Copies public documentation from the parent docs directory to ./.docs-build and adapts
// GitHub-flavoured Markdown for Docusaurus. docs/internal is intentionally excluded:
//   1. GitHub alerts ("> [!WARNING]") -> Docusaurus admonitions (":::warning")
//   2. Links that leave docs/ (e.g. ../README.md) -> absolute GitHub URLs
// The source files in docs/ are never modified.
import { cpSync, existsSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const src = resolve(root, '..');
const out = resolve(root, '.docs-build');
const repoUrl = (process.env.DOCS_REPO_URL || 'https://github.com/SaschaSchwarzK/netbox-manager').replace(/\/$/, '');
const branch = process.env.DOCS_SOURCE_BRANCH || 'develop';

const ADMONITION = { NOTE: 'note', TIP: 'tip', IMPORTANT: 'info', WARNING: 'warning', CAUTION: 'danger' };

if (!existsSync(src)) throw new Error(`docs folder not found: ${src}`);
rmSync(out, { recursive: true, force: true });
mkdirSync(out, { recursive: true });
for (const entry of readdirSync(src)) {
  if (entry === 'website' || entry === 'internal' || entry === '.DS_Store' || entry.startsWith('._')) continue;
  cpSync(join(src, entry), join(out, entry), {
    recursive: true,
    filter: (p) => {
      const name = p.split('/').pop();
      return !(name === '.DS_Store' || name.startsWith('._'));
    },
  });
}

function convertAlerts(text) {
  const lines = text.split('\n');
  const result = [];
  let inFence = false;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (/^\s*(```|~~~)/.test(line)) inFence = !inFence;
    const m = !inFence && line.match(/^>\s*\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*$/i);
    if (!m) { result.push(line); continue; }
    const body = [];
    let j = i + 1;
    while (j < lines.length && /^>/.test(lines[j])) { body.push(lines[j].replace(/^>\s?/, '')); j++; }
    if (result.length && result[result.length - 1].trim() !== '') result.push('');
    result.push(`:::${ADMONITION[m[1].toUpperCase()]}`, ...body, ':::');
    if (j < lines.length && lines[j].trim() !== '') result.push('');
    i = j - 1;
  }
  return result.join('\n');
}

function rewriteExternalLinks(text) {
  // [label](../README.md#x) -> absolute GitHub URL (only links that climb out of docs/)
  return text.replace(/\]\(((?:\.\.\/)+)([^)\s]+)\)/g, (_all, _up, rest) => `](${repoUrl}/blob/${branch}/${rest})`);
}

function walk(dir) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p);
    else if (/\.mdx?$/.test(name)) {
      writeFileSync(p, rewriteExternalLinks(convertAlerts(readFileSync(p, 'utf8'))));
    }
  }
}
walk(out);
console.log(`Prepared docs: ${src} -> ${out}`);
