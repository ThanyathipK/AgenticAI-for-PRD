import { readFile, stat } from "node:fs/promises";
import path from "node:path";

const outputDirectory = process.env.BUNDLE_DIR || "dist";
const manifestPath = path.join(outputDirectory, ".vite", "manifest.json");
const manifest = JSON.parse(await readFile(manifestPath, "utf8"));
const entry = Object.values(manifest).find((item) => item.isEntry);

if (!entry?.file) {
  throw new Error(`Vite manifest has no application entry: ${manifestPath}`);
}

const entryPath = path.join(outputDirectory, entry.file);
const { size } = await stat(entryPath);
const maximumBytes = 500 * 1024;

if (size > maximumBytes) {
  throw new Error(
    `Initial JavaScript entry is ${(size / 1024).toFixed(1)} KiB; budget is 500 KiB.`,
  );
}

console.log(
  `Initial JavaScript entry: ${(size / 1024).toFixed(1)} KiB / 500 KiB budget.`,
);
