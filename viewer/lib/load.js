// Getting a replay into the page: from ?replay=<path>, from a chosen file, or from text.
//
// This is the only module that asks the network for anything, and it asks for one file from
// the same site as the page. Nothing is uploaded, nothing is stored.

import { indexReplay, MAX_FILE_CHARS, validateReplay } from './replay.js';
import { resolveReplayUrl } from './route.js';
import { STR } from './strings.js';

/** A problem with a replay, in words for the reader. `messages` is a list of lines. */
export class ReplayError extends Error {
  constructor(messages) {
    super(messages.join(' '));
    this.name = 'ReplayError';
    this.messages = messages;
  }
}

/** Parse, check and index the text of a replay file. Throws ReplayError. */
export function parseReplay(text) {
  if (typeof text !== 'string' || text.length > MAX_FILE_CHARS) throw new ReplayError([STR.err.tooLarge]);
  let raw;
  try {
    raw = JSON.parse(text);
  } catch {
    throw new ReplayError([STR.err.notJson]);
  }
  const result = validateReplay(raw);
  if (!result.ok) throw new ReplayError(result.errors);
  return indexReplay(raw);
}

/**
 * Fetch the text named by ?replay=. The path must stay on the site of the page. `fetchFn` is
 * the browser's fetch, passed in so that tests can stand in for it.
 */
export async function fetchReplayText(param, baseHref, fetchFn) {
  const target = resolveReplayUrl(param, baseHref);
  if (!target.ok) throw new ReplayError([target.reason === 'origin' ? STR.err.cross : STR.err.badPath]);
  let response;
  try {
    response = await fetchFn(target.url, {
      credentials: 'omit',
      cache: 'no-cache',
      referrerPolicy: 'no-referrer',
    });
  } catch {
    throw new ReplayError([STR.err.network]);
  }
  if (!response.ok) throw new ReplayError([STR.err.fetchFailed(response.status)]);
  const final = response.url ? new URL(response.url) : new URL(target.url);
  if (final.origin !== new URL(baseHref).origin) throw new ReplayError([STR.err.cross]);
  try {
    return await response.text();
  } catch {
    throw new ReplayError([STR.err.network]);
  }
}

/** Read the text of a file chosen with the picker. */
export async function readFileText(file) {
  if (file.size > MAX_FILE_CHARS * 4) throw new ReplayError([STR.err.tooLarge]);
  try {
    return await file.text();
  } catch {
    throw new ReplayError([STR.err.network]);
  }
}
