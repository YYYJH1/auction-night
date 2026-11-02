// A small static file server for the capture script.
//
// It listens on 127.0.0.1 only, on a port the system picks, and serves two kinds of things:
//   * a folder (the replay viewer), at the root of the site;
//   * single files under a name of their own (the replay json), given as `mounts`.
// It reads files and nothing else: GET and HEAD only, no directory listings, and a request
// that leaves the folder is refused.

import { createServer } from 'node:http';
import { readFile, stat } from 'node:fs/promises';
import path from 'node:path';

const TYPES = new Map([
  ['.html', 'text/html; charset=utf-8'],
  ['.js', 'text/javascript; charset=utf-8'],
  ['.mjs', 'text/javascript; charset=utf-8'],
  ['.css', 'text/css; charset=utf-8'],
  ['.json', 'application/json; charset=utf-8'],
  ['.svg', 'image/svg+xml'],
  ['.png', 'image/png'],
  ['.ico', 'image/x-icon'],
  ['.txt', 'text/plain; charset=utf-8'],
]);

function typeOf(file) {
  return TYPES.get(path.extname(file).toLowerCase()) ?? 'application/octet-stream';
}

/**
 * Start the server.
 * @param {{root: string, mounts?: Record<string, string>}} options
 *   root    the folder served at "/"
 *   mounts  a map from a URL path ("/replay.json") to the file that answers it
 * @returns {Promise<{origin: string, port: number, close: () => Promise<void>}>}
 */
export async function serve({ root, mounts = {} }) {
  const base = path.resolve(root);
  const server = createServer(async (request, response) => {
    const fail = (code) => {
      response.writeHead(code, { 'content-type': 'text/plain; charset=utf-8' });
      response.end(`${code}\n`);
    };
    try {
      if (request.method !== 'GET' && request.method !== 'HEAD') return fail(405);
      const url = new URL(request.url ?? '/', 'http://127.0.0.1');
      let pathname;
      try {
        pathname = decodeURIComponent(url.pathname);
      } catch {
        return fail(400);
      }
      if (pathname.includes('\0')) return fail(400);
      let file = Object.hasOwn(mounts, pathname) ? mounts[pathname] : null;
      if (file === null) {
        const wanted = pathname.endsWith('/') ? `${pathname}index.html` : pathname;
        file = path.resolve(base, `.${wanted}`);
        if (file !== base && !file.startsWith(base + path.sep)) return fail(403);
      }
      const info = await stat(file).catch(() => null);
      if (info === null || !info.isFile()) return fail(404);
      const body = await readFile(file);
      response.writeHead(200, {
        'content-type': typeOf(file),
        'content-length': body.length,
        'cache-control': 'no-store',
      });
      response.end(request.method === 'HEAD' ? undefined : body);
    } catch {
      fail(500);
    }
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  const { port } = server.address();
  return {
    origin: `http://127.0.0.1:${port}`,
    port,
    close: () =>
      new Promise((resolve) => {
        server.close(() => resolve());
        server.closeAllConnections?.();
      }),
  };
}
