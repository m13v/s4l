// Build helper for Story B (double-click .mcpb install).
//
// Produces mcp/dist/pipeline.tgz: the EXACT npm tarball of the social-autoposter
// pipeline (curated `files` allowlist from the repo-root package.json), embedded
// in the .mcpb so a bare double-click install can materialize the pipeline source
// without a separate git clone. Using `npm pack` (not a hand-maintained copy list)
// guarantees the bundled scripts are byte-identical to what npm publishes; there
// is no second curation list to drift.
//
// The repo-root package.json excludes mcp/dist/pipeline.tgz from its own `files`,
// so packing never recursively embeds a prior tarball, and Story A (npm install)
// ships the source directly and never carries this tarball.

import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(__dirname, "..");
const distDir = path.join(__dirname, "dist");
const outPath = path.join(distDir, "pipeline.tgz");

fs.mkdirSync(distDir, { recursive: true });

// Pack into a temp dir, then move to dist/pipeline.tgz. --json prints the
// produced filename so we don't have to guess the version-stamped name.
const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "s4l-pack-"));
let produced;
try {
  const out = execFileSync("npm", ["pack", "--json", "--pack-destination", tmpDir], {
    cwd: repoRoot,
    encoding: "utf-8",
  });
  const meta = JSON.parse(out);
  const fname = Array.isArray(meta) && meta[0] && meta[0].filename ? meta[0].filename : null;
  if (!fname) throw new Error("npm pack --json did not report a filename");
  // npm may sanitize scoped names; resolve the actual file on disk.
  const candidate = path.join(tmpDir, path.basename(fname));
  produced = fs.existsSync(candidate)
    ? candidate
    : path.join(tmpDir, fs.readdirSync(tmpDir).find((f) => f.endsWith(".tgz")));
  if (!produced || !fs.existsSync(produced)) throw new Error("packed tarball not found in " + tmpDir);
  fs.rmSync(outPath, { force: true });
  fs.copyFileSync(produced, outPath);
} finally {
  fs.rmSync(tmpDir, { recursive: true, force: true });
}

const bytes = fs.statSync(outPath).size;
console.log(`bundled pipeline.tgz: ${outPath} (${(bytes / 1024 / 1024).toFixed(1)} MB)`);
