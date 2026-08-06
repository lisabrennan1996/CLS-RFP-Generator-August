#!/usr/bin/env node
// Bundles the frontend (index.html + styles.css + the 4 JS files + the Lilly logo) into one
// self-contained HTML file for distribution/preview outside the FastAPI static-file mount.
// The backend (rfp-webapp/backend/) is NOT part of this bundle -- the resulting file still
// needs a running API server to actually do anything; see API_BASE in app.js (?api=... query
// param or window.RFP_API_BASE) to point it at one that isn't same-origin.
//
// Usage: node build-single-file.js  (run from rfp-webapp/frontend/)
// Output: rfp-webapp/frontend/dist/index.single.html

const fs = require('fs');
const path = require('path');

const ROOT = __dirname;
const OUT_DIR = path.join(ROOT, 'dist');
const OUT_FILE = path.join(OUT_DIR, 'index.single.html');

const SCRIPT_ORDER = ['blocks-view.js', 'master-schedule.js', 'master-table.js', 'app.js'];

function read(name) {
  return fs.readFileSync(path.join(ROOT, name), 'utf8');
}

function main() {
  let html = read('index.html');
  const css = read('styles.css');
  const logoBase64 = fs.readFileSync(path.join(ROOT, 'assets', 'lilly-logo.png')).toString('base64');

  // 1. Inline the stylesheet <link> as a <style> block.
  html = html.replace(
    /<link rel="stylesheet" href="styles\.css" \/>/,
    `<style>\n${css}\n</style>`
  );

  // 2. Inline the logo as a data URI.
  html = html.replace(
    /src="assets\/lilly-logo\.png"/,
    `src="data:image/png;base64,${logoBase64}"`
  );

  // 3. Inline each <script src="..."> in the same load order as the source index.html.
  for (const name of SCRIPT_ORDER) {
    const js = read(name);
    const re = new RegExp(`<script src="${name}"></script>`);
    if (!re.test(html)) {
      throw new Error(`Expected to find <script src="${name}"> in index.html but didn't -- bundling would silently drop it.`);
    }
    // Guard against a real "</script>" substring inside the source ever breaking out of the tag.
    const safeJs = js.replace(/<\/script>/g, '<\\/script>');
    html = html.replace(re, `<script>\n${safeJs}\n</script>`);
  }

  fs.mkdirSync(OUT_DIR, { recursive: true });
  fs.writeFileSync(OUT_FILE, html, 'utf8');

  const stat = fs.statSync(OUT_FILE);
  console.log(`Wrote ${OUT_FILE} (${(stat.size / 1024).toFixed(1)} KB)`);
}

main();
