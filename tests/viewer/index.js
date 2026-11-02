// Entry point for `node --test tests/viewer/`. Node runs a directory argument as a module, so
// this file loads every test file of the folder into one process. Running a single file
// (`node --test tests/viewer/safe.test.js`) works as well.
import { readdirSync } from 'node:fs';

const folder = new URL('./', import.meta.url);
const files = readdirSync(folder)
  .filter((name) => name.endsWith('.test.js'))
  .sort();
for (const name of files) await import(new URL(name, folder));
