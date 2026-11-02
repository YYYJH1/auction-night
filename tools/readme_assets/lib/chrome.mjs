// Drive a headless Chromium over the Chrome DevTools Protocol, with Node's own WebSocket.
//
// No dependencies: `spawn` starts the browser, the browser prints the address of its debugging
// socket on stderr, and the global `WebSocket` of Node 22 talks to it. The browser is the
// "headless shell" build of Chromium (CHROME_BIN), which has no window and no GPU process.
//
// Everything the browser writes (profile, shared memory, crash files) goes under `workDir`.

import { spawn } from 'node:child_process';
import { mkdir, mkdtemp, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

const LISTENING = /DevTools listening on (ws:\/\/\S+)/;

/** The browser to use: the argument, else the environment variable CHROME_BIN. */
export function chromeBin(explicit) {
  const bin = explicit ?? process.env.CHROME_BIN;
  if (!bin) {
    throw new Error(
      'CHROME_BIN is not set. Point it at a headless Chromium (the chrome-headless-shell binary).',
    );
  }
  return bin;
}

/** A directory for this run's temporary files: under `base`, else under the system's TMPDIR. */
export async function makeWorkDir(base, prefix = 'readme-assets-') {
  const parent = base ?? os.tmpdir();
  await mkdir(parent, { recursive: true });
  return mkdtemp(path.join(parent, prefix));
}

/** One WebSocket connection to the browser; commands return promises, events call handlers. */
class Connection {
  #socket;
  #next = 1;
  #pending = new Map();
  #handlers = new Map();

  constructor(socket) {
    this.#socket = socket;
    socket.addEventListener('message', (event) => this.#receive(String(event.data)));
    socket.addEventListener('close', () => {
      for (const { reject } of this.#pending.values()) reject(new Error('the browser closed the connection'));
      this.#pending.clear();
    });
  }

  static async open(url) {
    const socket = new WebSocket(url);
    await new Promise((resolve, reject) => {
      socket.addEventListener('open', resolve, { once: true });
      socket.addEventListener('error', () => reject(new Error(`cannot connect to ${url}`)), { once: true });
    });
    return new Connection(socket);
  }

  #receive(text) {
    const message = JSON.parse(text);
    if (message.id !== undefined) {
      const entry = this.#pending.get(message.id);
      if (entry === undefined) return;
      this.#pending.delete(message.id);
      if (message.error) entry.reject(new Error(`${entry.method}: ${message.error.message}`));
      else entry.resolve(message.result ?? {});
      return;
    }
    for (const handler of this.#handlers.get(message.method) ?? []) handler(message.params ?? {}, message.sessionId);
  }

  send(method, params = {}, sessionId = undefined) {
    const id = this.#next++;
    const payload = { id, method, params };
    if (sessionId !== undefined) payload.sessionId = sessionId;
    return new Promise((resolve, reject) => {
      this.#pending.set(id, { resolve, reject, method });
      this.#socket.send(JSON.stringify(payload));
    });
  }

  on(method, handler) {
    const list = this.#handlers.get(method) ?? [];
    list.push(handler);
    this.#handlers.set(method, list);
    return () => this.#handlers.set(method, (this.#handlers.get(method) ?? []).filter((h) => h !== handler));
  }

  close() {
    try {
      this.#socket.close();
    } catch {
      // already closed
    }
  }
}

/** One tab. */
export class Page {
  constructor(connection, sessionId, targetId) {
    this.connection = connection;
    this.sessionId = sessionId;
    this.targetId = targetId;
  }

  send(method, params = {}) {
    return this.connection.send(method, params, this.sessionId);
  }

  /** Run an expression in the page and return its value (promises are awaited). */
  async evaluate(expression) {
    const result = await this.send('Runtime.evaluate', {
      expression,
      awaitPromise: true,
      returnByValue: true,
    });
    if (result.exceptionDetails) {
      const detail = result.exceptionDetails.exception?.description ?? result.exceptionDetails.text;
      throw new Error(`in the page: ${detail}`);
    }
    return result.result?.value;
  }

  /** Wait until an expression is truthy in the page. */
  async waitFor(expression, { timeout = 15000, interval = 50, what = expression } = {}) {
    const deadline = Date.now() + timeout;
    for (;;) {
      if (await this.evaluate(`Boolean(${expression})`)) return;
      if (Date.now() > deadline) throw new Error(`timed out waiting for ${what}`);
      await new Promise((resolve) => setTimeout(resolve, interval));
    }
  }

  /** Open an address and wait for the load event (or, for a change of fragment only, for the change). */
  async goto(url) {
    const loaded = new Promise((resolve) => {
      const offs = [];
      const done = (_params, session) => {
        if (session !== this.sessionId) return;
        for (const off of offs) off();
        resolve();
      };
      offs.push(this.connection.on('Page.loadEventFired', done));
      offs.push(this.connection.on('Page.navigatedWithinDocument', done));
    });
    const result = await this.send('Page.navigate', { url });
    if (result.errorText) throw new Error(`cannot open ${url}: ${result.errorText}`);
    await loaded;
  }

  /** Emulate media features, for example [{name: 'prefers-color-scheme', value: 'dark'}]. */
  setMedia(features) {
    return this.send('Emulation.setEmulatedMedia', { features });
  }

  /** The size of the window in CSS pixels, and the pixel ratio of the screenshots. */
  setViewport(width, height, scale = 1) {
    return this.send('Emulation.setDeviceMetricsOverride', {
      width,
      height,
      deviceScaleFactor: scale,
      mobile: false,
    });
  }

  /** A transparent page background (for pictures with rounded corners). */
  setTransparent(on) {
    return this.send('Emulation.setDefaultBackgroundColorOverride', on ? { color: { r: 0, g: 0, b: 0, a: 0 } } : {});
  }

  /** A screenshot as a Buffer. `clip` is {x, y, width, height, scale} in CSS pixels. */
  async screenshot({ clip, format = 'png' } = {}) {
    const params = { format, fromSurface: true, captureBeyondViewport: false };
    if (clip) {
      params.clip = { scale: 1, ...clip };
      params.captureBeyondViewport = true; // the clip may reach below the window
    }
    const { data } = await this.send('Page.captureScreenshot', params);
    return Buffer.from(data, 'base64');
  }

  async close() {
    await this.connection.send('Target.closeTarget', { targetId: this.targetId }).catch(() => {});
  }
}

/** A running browser. */
export class Browser {
  constructor(child, connection, profile) {
    this.child = child;
    this.connection = connection;
    this.profile = profile;
    this.closed = false;
  }

  /**
   * Start the browser. `workDir` holds its profile and its temporary files and is not removed here.
   * @param {{bin?: string, workDir: string, args?: string[]}} options
   */
  static async launch({ bin, workDir, args = [] }) {
    const binary = chromeBin(bin);
    const profile = await mkdtemp(path.join(workDir, 'profile-'));
    const flags = [
      '--no-sandbox',
      '--disable-gpu',
      '--disable-dev-shm-usage', // shared memory goes to TMPDIR instead of the system's /dev/shm
      '--hide-scrollbars',
      '--mute-audio',
      // No persistent cookie store: on some file systems its SQLite file never finishes loading,
      // and every http request of the page waits for it. Nothing here needs a cookie or a cache.
      '--incognito',
      '--no-first-run',
      '--no-default-browser-check',
      '--disable-background-networking',
      '--disable-component-update',
      '--disable-sync',
      // Make the pictures repeatable: every stage of the compositor runs before a frame is drawn.
      '--run-all-compositor-stages-before-draw',
      '--disable-threaded-animation',
      '--disable-threaded-scrolling',
      '--disable-checker-imaging',
      '--disable-partial-raster',
      '--remote-debugging-port=0',
      `--user-data-dir=${profile}`,
      ...args,
      'about:blank',
    ];
    // detached: the browser gets its own process group, so the whole group can be ended at once.
    const child = spawn(binary, flags, {
      stdio: ['ignore', 'ignore', 'pipe'],
      detached: true,
      env: { ...process.env, TMPDIR: workDir },
    });
    let log = '';
    const endGroup = () => {
      try {
        process.kill(-child.pid, 'SIGTERM');
      } catch {
        // the group is already gone
      }
    };
    const address = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        endGroup();
        reject(new Error(`the browser did not start in time\n${log}`));
      }, 20000);
      child.stderr.on('data', (chunk) => {
        log = (log + chunk).slice(-4000);
        const found = LISTENING.exec(log);
        if (found) {
          clearTimeout(timer);
          resolve(found[1]);
        }
      });
      child.once('error', (error) => {
        clearTimeout(timer);
        reject(error);
      });
      child.once('exit', (code) => {
        clearTimeout(timer);
        reject(new Error(`the browser exited with code ${code}\n${log}`));
      });
    });
    const connection = await Connection.open(address).catch((error) => {
      endGroup();
      throw error;
    });
    const browser = new Browser(child, connection, profile);
    const stop = () => browser.kill();
    process.once('exit', stop);
    process.once('SIGINT', () => {
      stop();
      process.exit(130);
    });
    process.once('SIGTERM', () => {
      stop();
      process.exit(143);
    });
    return browser;
  }

  /** Open a tab. */
  async newPage({ width = 1280, height = 800, scale = 1, colorScheme = null, transparent = false } = {}) {
    const { targetId } = await this.connection.send('Target.createTarget', { url: 'about:blank' });
    const { sessionId } = await this.connection.send('Target.attachToTarget', { targetId, flatten: true });
    const page = new Page(this.connection, sessionId, targetId);
    await page.send('Page.enable');
    await page.send('Runtime.enable');
    await page.setViewport(width, height, scale);
    if (colorScheme) await page.setMedia([{ name: 'prefers-color-scheme', value: colorScheme }]);
    if (transparent) await page.setTransparent(true);
    return page;
  }

  /** End the browser and every process of its group. */
  kill() {
    if (this.closed) return;
    this.closed = true;
    if (this.child.pid === undefined) return;
    try {
      process.kill(-this.child.pid, 'SIGTERM');
    } catch {
      // the group is already gone
    }
  }

  async close() {
    if (this.closed) return;
    await this.connection.send('Browser.close').catch(() => {});
    this.connection.close();
    await new Promise((resolve) => {
      if (this.child.exitCode !== null) return resolve();
      const timer = setTimeout(resolve, 3000);
      this.child.once('exit', () => {
        clearTimeout(timer);
        resolve();
      });
    });
    this.kill();
    await rm(this.profile, { recursive: true, force: true });
  }
}
