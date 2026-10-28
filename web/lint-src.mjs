import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from 'node:url';

const root = new URL("./src/", import.meta.url);
const banned = [
  { pattern: /dangerouslySetInnerHTML/, message: "unsafe HTML" },
  { pattern: /innerHTML\s*=/, message: "unsafe HTML assignment" },
  { pattern: /\beval\s*\(/, message: "eval" },
  { pattern: /document\.write\s*\(/, message: "document.write" },
  { pattern: /https?:\/\/(?!127\.0\.0\.1|localhost)/, message: "external runtime URL" },
  { pattern: /localStorage\.setItem\(\s*["']life-recorder-token["']/, message: "token in localStorage" },
];

function files(dir) {
  const found = [];
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) found.push(...files(path));
    else if (/\.(ts|tsx|mjs)$/.test(name)) found.push(path);
  }
  return found;
}

const failures = [];
for (const path of files(fileURLToPath(root))) {
  const text = readFileSync(path, "utf8");
  for (const rule of banned) {
    if (rule.pattern.test(text)) failures.push(`${path}: ${rule.message}`);
  }
}
if (failures.length) {
  console.error(failures.join("\n"));
  process.exit(1);
}
console.log(`linted ${files(fileURLToPath(root)).length} source files`);
