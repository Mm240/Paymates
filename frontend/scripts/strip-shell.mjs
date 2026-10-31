// The artifact host wraps pages in its own <html>/<head>/<body>, so publish only the page content.
import { readFileSync, writeFileSync } from 'node:fs'
const file = new URL('../dist-artifact/index.html', import.meta.url)
let html = readFileSync(file, 'utf8')
html = html
  .replace(/<!doctype html>\s*/i, '')
  .replace(/<html[^>]*>\s*/i, '')
  .replace(/<\/html>\s*$/i, '')
  .replace(/<head>\s*/i, '')
  .replace(/<\/head>\s*/i, '')
  .replace(/<body>\s*/i, '')
  .replace(/<\/body>\s*/i, '')
  .replace(/<meta charset="UTF-8" \/>\s*/i, '')
  .replace(/<meta name="viewport"[^>]*>\s*/i, '')
writeFileSync(file, html.trim() + '\n')
console.log('artifact page ready:', html.length, 'bytes')
