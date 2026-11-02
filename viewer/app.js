// The page: connects the replay, the playback state and the DOM to the browser.
//
// The decisions live in lib/ (pure functions) and ui/ (DOM built from a document argument).
// This file only wires them to the address bar, the animation clock, the keyboard and the
// controls. It reads one replay.json, from ?replay=<path> or from the file picker.
//
// There are five parts, and one of them is on show at a time (`view`):
//   lots         the lots one by one, with the stage
//   recap        the three-minute version: a plan of segments (lib/recap.js) on one clock
//   highlights   the list of highlights
//   awards       the awards, and the findings of the judges and the program
//   review       the review of the host: its recap, and its predictions with what became of them
// The lots and the recap are played by the same reducer (lib/state.js). The recap is a night
// of one unit as long as its plan, so play, pause, speed, seek and the end of the night work
// the same way, and its previous and next buttons move between segments. The lens and the
// speed are shared: both states always agree on them. A part is chosen by the address, so the
// links of the top bar, the back button and a pasted link all work the same way.

import { clear, h, setAttr, setText } from './lib/dom.js';
import { createClock } from './lib/clock.js';
import { clock } from './lib/format.js';
import { momentStart } from './lib/highlights.js';
import { fetchReplayText, parseReplay, readFileText, ReplayError } from './lib/load.js';
import { planRecap, recapAt, segmentStart } from './lib/recap.js';
import { formatHash, formatMomentHash, formatPageHash, PAGES, parseHash, replayParam } from './lib/route.js';
import { cleanLine } from './lib/safe.js';
import { initialState, reduce, SPEEDS } from './lib/state.js';
import { STR } from './lib/strings.js';
import { mountAwards } from './ui/awards.js';
import { mountHighlights } from './ui/highlights.js';
import { mountLotList } from './ui/lotlist.js';
import { mountRecapBar } from './ui/recap.js';
import { mountReview } from './ui/review.js';
import { createStage } from './ui/stage.js';
import { mountViews } from './ui/views.js';

const doc = document;
const byId = (id) => doc.getElementById(id);
const els = {
  bar: byId('topbar'),
  night: byId('night-label'),
  views: byId('views'),
  controls: byId('controls'),
  transport: byId('transport'),
  prev: byId('btn-prev'),
  play: byId('btn-play'),
  next: byId('btn-next'),
  speeds: byId('speeds'),
  scrub: byId('scrub'),
  time: byId('time'),
  lens: byId('lens'),
  file: byId('file'),
  fileLabel: byId('file-label'),
  panel: byId('panel'),
  app: byId('app'),
  lotsTitle: byId('lots-title'),
  lotList: byId('lot-list'),
  stage: byId('stage'),
  recapBar: byId('recap-bar'),
  lotStage: byId('lot-stage'),
  page: byId('page'),
};
const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const speedButtons = [];

// session: {model, stage, lotList, views, ctx, visited, recap: {plan, ctx, bar}, ui}
// state is the playback of the lots, recapState the playback of the recap.
let session = null;
let state = null;
let recapState = null;
let view = 'lots';
let revealed = false; // the spoiler gate of the highlights and of the review is open
let shownRecap = null; // {index, lens}: what the stage shows while the recap plays

const playing = () => (view === 'lots' ? state.playing : view === 'recap' ? recapState.playing : false);
// The parts that are a page in the place of the stage: nothing plays there.
const isPage = (name) => name === 'highlights' || name === 'awards' || name === 'review';

// One clock drives playback. It asks the browser for a frame, turns the time since the last
// frame into a tick, and goes on while the page is playing.
const frames = createClock({
  request: (callback) => window.requestAnimationFrame(callback),
  cancel: (id) => window.cancelAnimationFrame(id),
  onTick(seconds) {
    if (session === null || !playing()) return false;
    if (seconds > 0) dispatch({ type: 'tick', dt: seconds });
    return playing();
  },
});

// --------------------------------------------------------------------------- panels

function showPanel(kind, lines = []) {
  clear(els.panel);
  els.panel.hidden = false;
  const pick = h(doc, 'label', { class: 'btn btn-primary', for: 'file' }, STR.welcome.pick);
  if (kind === 'welcome') {
    els.panel.append(h(doc, 'h2', {}, STR.welcome.title), h(doc, 'p', {}, STR.welcome.body), pick);
  } else if (kind === 'loading') {
    els.panel.append(h(doc, 'p', { role: 'status' }, STR.loading));
  } else if (kind === 'notice') {
    els.panel.append(h(doc, 'p', { role: 'status', class: 'notice' }, lines.join(' ')));
  } else {
    els.panel.append(
      h(doc, 'h2', {}, STR.loadFailed),
      h(doc, 'ul', { role: 'alert' }, lines.map((line) => h(doc, 'li', {}, line))),
      pick,
    );
  }
}

function hidePanel() {
  clear(els.panel);
  els.panel.hidden = true;
}

function fail(error) {
  const lines = error instanceof ReplayError ? error.messages : [cleanLine(error?.message ?? '', 160)];
  showPanel('error', lines.filter(Boolean));
}

// --------------------------------------------------------------------------- controls

/**
 * The top bar can wrap onto a second row, and the column of lots sticks below it, so the page
 * measures the bar and tells the style sheet (--topbar-h). Without ResizeObserver the style
 * sheet keeps its own guess for a bar of one row.
 */
function followBarHeight() {
  const root = doc.documentElement;
  if (!els.bar || !root?.style || typeof window.ResizeObserver !== 'function') return;
  const measure = () => root.style.setProperty('--topbar-h', `${els.bar.offsetHeight}px`);
  new window.ResizeObserver(measure).observe(els.bar);
  measure();
}

function initControls() {
  setText(els.fileLabel, STR.openAnother);
  setText(els.lotsTitle, STR.lotList.title);
  setAttr(els.speeds, 'aria-label', STR.controls.speeds);
  setAttr(els.scrub, 'aria-label', STR.controls.seek);
  setAttr(els.controls, 'aria-label', STR.controls.group);
  setAttr(els.views, 'aria-label', STR.views.label);
  for (const speed of SPEEDS) {
    const button = h(doc, 'button', { type: 'button', class: 'btn speed', 'data-speed': String(speed) }, STR.controls.speed(speed));
    button.addEventListener('click', () => dispatch({ type: 'speed', value: speed }));
    els.speeds.appendChild(button);
    speedButtons.push({ speed, button });
  }
  els.prev.addEventListener('click', () => step(-1));
  els.next.addEventListener('click', () => step(1));
  els.play.addEventListener('click', () => dispatch({ type: 'toggle' }));
  els.lens.addEventListener('click', () => dispatch({ type: 'lens', value: !state.lens }));
  els.scrub.addEventListener('input', () => {
    const length = view === 'recap' ? session.recap.plan.length : session.ctx.durationOf(state.lot);
    dispatch({ type: 'seek', t: (Number(els.scrub.value) / 1000) * length });
  });
}

/** Previous and next: a lot in the lots, a segment in the recap. */
function step(direction) {
  if (session === null) return;
  if (view === 'lots') {
    dispatch({ type: direction > 0 ? 'next' : 'prev' });
  } else if (view === 'recap') {
    const to = segmentStart(session.recap.plan, recapState.t, direction);
    if (to !== null) dispatch({ type: 'seek', t: to });
  }
}

function syncControls() {
  const pages = isPage(view);
  els.transport.hidden = pages;
  setText(els.lens, `${state.lens ? '●' : '○'} ${STR.lens.label}`);
  setAttr(els.lens, 'aria-checked', state.lens);
  session.views.update(view, state.lot);
  if (pages) return;
  const recap = view === 'recap';
  const current = recap ? recapState : state;
  const length = recap ? session.recap.plan.length : session.ctx.durationOf(state.lot);
  setText(els.prev, `‹ ${recap ? STR.controls.prevSegment : STR.controls.prev}`);
  setText(els.next, `${recap ? STR.controls.nextSegment : STR.controls.next} ›`);
  setText(els.play, current.playing ? `⏸ ${STR.controls.pause}` : `▶ ${STR.controls.play}`);
  setAttr(els.play, 'aria-pressed', current.playing);
  for (const { speed, button } of speedButtons) setAttr(button, 'aria-pressed', speed === current.speed);
  setAttr(els.scrub, 'aria-label', recap ? STR.recap.seek : STR.controls.seek);
  const place = String(Math.round((current.t / length) * 1000));
  if (doc.activeElement !== els.scrub && els.scrub.value !== place) els.scrub.value = place;
  setText(els.time, `${clock(current.t)} / ${clock(length)}`);
}

function nightLabel(model) {
  const night = model.night;
  const parts = [night.id];
  if (night.season_id) parts.push(STR.night.season(night.season_id));
  if (night.deck_id) parts.push(STR.night.deck(night.deck_id));
  if (night.rotation !== null) parts.push(STR.night.rotation(night.rotation + 1));
  parts.push(STR.night.lots(model.lots.length, night.n_lots));
  return parts.join(' · ');
}

// --------------------------------------------------------------------------- playback

/** Send an action to the part on show. The lens and the speed belong to every part. */
function dispatch(action) {
  if (session === null) return;
  if (action.type === 'lens' || action.type === 'speed') {
    state = reduce(state, action, session.ctx).state;
    recapState = reduce(recapState, action, session.recap.ctx).state;
    if (view === 'lots') refresh(null);
    else if (view === 'recap') refreshRecap(false);
    else renderPage();
    if (action.type === 'speed') syncControls();
    return;
  }
  if (view === 'lots') dispatchLots(action);
  else if (view === 'recap') dispatchRecap(action);
}

function dispatchLots(action) {
  const before = state;
  const result = reduce(state, action, session.ctx);
  state = result.state;
  for (const effect of result.effects) {
    if (effect.kind === 'push') history.pushState(null, '', effect.hash);
    else history.replaceState(null, '', effect.hash);
  }
  refresh(before);
  noteEnd();
  if (state.playing) frames.start();
}

function dispatchRecap(action) {
  // The recap has one address. Its reducer's own history effects are dropped.
  recapState = reduce(recapState, action, session.recap.ctx).state;
  refreshRecap(false);
  noteEnd();
  if (recapState.playing) frames.start();
}

/** Reaching the end of the last lot, or of the recap, opens the highlights and the review. */
function noteEnd() {
  if (revealed) return;
  const last = session.model.lotNos.at(-1);
  const lotsOver = view === 'lots' && !state.playing && state.lot === last && state.t >= session.ctx.durationOf(last);
  const recapOver = view === 'recap' && !recapState.playing && recapState.t >= session.recap.plan.length;
  if (lotsOver || recapOver) revealed = true;
}

function refresh(before) {
  const { stage, lotList, visited } = session;
  if (before === null || before.lot !== state.lot || before.lens !== state.lens) {
    stage.render(state.lot, state.lens, { opening: true });
    visited.add(state.lot);
    lotList.update(state.lot, visited);
  }
  stage.update(state.t);
  syncControls();
}

function refreshRecap(force) {
  const { stage, lotList, visited, recap } = session;
  const at = recapAt(recap.plan, recapState.t);
  if (at.segment === null) return;
  if (force || shownRecap === null || shownRecap.index !== at.index || shownRecap.lens !== recapState.lens) {
    shownRecap = { index: at.index, lens: recapState.lens };
    stage.render(at.segment.lot, recapState.lens);
    recap.bar.show(at.segment, recapState.lens);
    visited.add(at.segment.lot);
    lotList.update(at.segment.lot, visited);
  }
  stage.update(at.lotT);
  recap.bar.update(at);
  syncControls();
}

// ------------------------------------------------------------------------------- parts

function renderPage() {
  const { model, ui } = session;
  clear(els.page);
  // The highlights and the review tell how the night ended, so they stand behind a gate until it
  // has been watched to its end or the reader opens it.
  const onReveal = () => {
    revealed = true;
    renderPage();
    els.page.focus?.({ preventScroll: true }); // the button that was pressed is gone
  };
  if (view === 'awards') {
    mountAwards(doc, els.page, model, { lens: state.lens });
  } else if (view === 'highlights') {
    mountHighlights(doc, els.page, model, { lens: state.lens, revealed, ui: ui.highlights, onReveal });
  } else if (view === 'review') {
    mountReview(doc, els.page, model, { lens: state.lens, revealed, onReveal });
  }
  syncControls();
}

/**
 * Show a part. A part that is not on show does not run: the clock only asks for frames while
 * the lots or the recap are on show, and the play state of the lots is kept for the way back.
 * The recap always starts from its first segment. The recap of a night with nothing to play
 * falls back to the lots.
 */
function setView(next) {
  if (session === null) return;
  let target = next;
  if (target === 'recap' && session.recap.plan.segments.length === 0) target = 'lots';
  const fellBack = target !== next;
  if (target === view) {
    if (fellBack) history.replaceState(null, '', formatHash(session.model.night.id, state.lot));
    return;
  }
  frames.stop();
  view = target;
  const onPage = isPage(view);
  els.recapBar.hidden = view !== 'recap';
  els.lotStage.hidden = onPage;
  els.page.hidden = !onPage;
  if (view === 'lots') {
    refresh(null);
    if (fellBack) history.replaceState(null, '', formatHash(session.model.night.id, state.lot));
    if (state.playing) frames.start();
  } else if (view === 'recap') {
    recapState = { lot: 1, t: 0, playing: !reduceMotion, speed: state.speed, lens: state.lens };
    shownRecap = null;
    refreshRecap(true);
    if (recapState.playing) frames.start();
  } else {
    session.lotList.update(null, session.visited);
    renderPage();
    els.page.focus?.({ preventScroll: true });
  }
}

/** Follow the address: a part, a lot, or a highlight. */
function followAddress() {
  const link = parseHash(window.location.hash);
  if (link === null || session === null) return;
  if (PAGES.includes(link.view)) {
    setView(link.view);
    return;
  }
  if (link.view === 'lot' || link.view === 'moment') {
    setView('lots');
    dispatch({ type: 'hash', hash: window.location.hash });
  }
}

/** The lot and time of a highlight on the clock of its lot, or null when it cannot be played. */
function momentOf(model, stage, id) {
  const light = model.highlight(id);
  if (light === null || light.lot === null) return null;
  const lot = model.lot(light.lot);
  const script = stage.scriptOf(light.lot);
  if (lot === null || script === null) return null;
  return { lot: light.lot, t: momentStart(lot, script, light) };
}

// --------------------------------------------------------------------------- starting

function start(model, { honourHash }) {
  frames.stop();
  const nightId = model.night.id;
  const stage = createStage(doc, els.lotStage, model, { reduceMotion });
  clear(els.lotList);
  const lotList = mountLotList(doc, els.lotList, model);
  const plan = planRecap(model, stage.scriptOf);
  const ctx = {
    nightId,
    lotNos: model.lotNos,
    durationOf: stage.durationOf,
    momentOf: (id) => momentOf(model, stage, id),
    reducedMotion: reduceMotion,
  };
  const recapCtx = { nightId, lotNos: [1], durationOf: () => plan.length };
  clear(els.views);
  clear(els.recapBar);
  clear(els.page);
  session = {
    model,
    stage,
    lotList,
    views: mountViews(doc, els.views, model, { recapAvailable: plan.segments.length > 0 }),
    ctx,
    visited: new Set(),
    recap: { plan, ctx: recapCtx, bar: mountRecapBar(doc, els.recapBar, model, plan) },
    ui: { highlights: { filter: null } },
  };

  const link = honourHash ? parseHash(window.location.hash) : null;
  const moment = link?.moment == null ? null : momentOf(model, stage, link.moment);
  state = initialState(ctx, { lot: moment?.lot ?? link?.lot ?? null, at: moment?.t ?? 0, reducedMotion: reduceMotion });
  recapState = { lot: 1, t: 0, playing: false, speed: 1, lens: state.lens };
  const opening = PAGES.includes(link?.view) && (link.view !== 'recap' || plan.segments.length > 0) ? link.view : 'lots';
  view = 'lots';
  revealed = false;
  shownRecap = null;
  els.recapBar.hidden = true;
  els.lotStage.hidden = false;
  els.page.hidden = true;
  doc.title = STR.documentTitle(nightId);
  setText(els.night, nightLabel(model));
  els.app.hidden = false;
  els.controls.hidden = false;
  els.views.hidden = false;
  // The address is made canonical: the part that opens, the moment that was asked for, or the lot.
  let canonical = formatHash(nightId, state.lot);
  if (opening !== 'lots') canonical = formatPageHash(nightId, opening);
  else if (moment !== null) canonical = formatMomentHash(nightId, link.moment);
  history.replaceState(null, '', canonical);
  if (link !== null && link.night !== nightId) {
    showPanel('notice', [STR.linkNight(cleanLine(link.night, 40), nightId)]);
  } else {
    hidePanel();
  }
  refresh(null);
  if (state.playing) frames.start();
  if (opening !== 'lots') setView(opening);
}

async function loadFile(file) {
  // A deep link opened before any replay is honoured by the first file. Later files start at lot 1.
  const first = session === null;
  showPanel('loading');
  try {
    start(parseReplay(await readFileText(file)), { honourHash: first });
  } catch (error) {
    fail(error);
  }
}

async function boot() {
  initControls();
  followBarHeight();
  els.file.addEventListener('change', () => {
    const file = els.file.files?.[0];
    if (file) loadFile(file).finally(() => (els.file.value = ''));
  });
  window.addEventListener('hashchange', followAddress);
  doc.addEventListener('keydown', (event) => {
    if (session === null || event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return;
    const tag = event.target?.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
    if (view !== 'lots' && view !== 'recap') return;
    if (event.key === ' ' && tag !== 'BUTTON' && tag !== 'A') {
      event.preventDefault();
      dispatch({ type: 'toggle' });
    } else if (event.key === 'ArrowRight') {
      step(1);
    } else if (event.key === 'ArrowLeft') {
      step(-1);
    }
  });
  const param = replayParam(window.location.search);
  if (param === null) {
    showPanel('welcome');
    return;
  }
  showPanel('loading');
  try {
    const text = await fetchReplayText(param, window.location.href, window.fetch.bind(window));
    start(parseReplay(text), { honourHash: true });
  } catch (error) {
    fail(error);
  }
}

boot();
