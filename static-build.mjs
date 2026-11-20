import { cpSync, mkdirSync, rmSync } from "node:fs";

import { validateCatalog } from "./scripts/check-catalog.mjs";

validateCatalog();

const output = new URL("./dist/", import.meta.url);
rmSync(output, { recursive: true, force: true });
mkdirSync(output, { recursive: true });

for (const path of [
  "assets",
  "catalog.json",
  "chemistry",
  "mathematics",
  "physics",
  "chemistry.html",
  "favicon.ico",
  "gallery.html",
  "index.html",
  "mathematics.html",
  "physics.html",
  "privacy.html",
  "robots.txt",
  "script.js",
  "sitemap.xml",
  "style.css",
  "terms.html",
]) {
  cpSync(new URL(path, import.meta.url), new URL(path, output), {
    recursive: true,
  });
}
