// Every piece of fixed text on the home page, in both languages, kept here apart from the logic.
//
// zh and en hold the same keys, and a test compares them. How a key is used:
//   data-i18n="key"        in index.html: the text of that element
//   data-i18n-alt="key"    in index.html: the alt text of that image
//   data-href="key"        in index.html: the address of that link (the keys that start with link.)
//   the rest               page.js builds the leaderboard and the cards of the six nights with them
// A value is a string, or a function when the text carries a number, or an object of names.

export const LANGS = Object.freeze(['zh', 'en']);

/** The value of the lang attribute of the page for each language. */
export const HTML_LANG = Object.freeze({ zh: 'zh-CN', en: 'en' });

const BLOB = 'https://github.com/YYYJH1/auction-night/blob/main/';

const ZH = Object.freeze({
  'page.title': 'AI 拍卖之夜',

  'hero.title': 'AI 拍卖之夜',
  'hero.lead': '六个大模型和两个规则机器人，按六种教科书拍卖规则竞拍十二件拍品，拍完由主持人和两位评委复盘这一夜。',
  'hero.alt': '回放页播放一件拍品：座位依次发言，每句话下面挂着座位的内心独白，密封出价一个个拆开，落槌，盲盒公布真值',
  'hero.caption': '演示夜第 5 件，盲盒：赢家付 95 买下真值 84 的东西，正是赢家诅咒。',

  'start.title': '先看这个',
  'start.note': '这三项都在回放页里打开。',
  'start.lot.title': '演示夜第 5 件',
  'start.lot.body': '盲盒拍卖。座位依次发言，内心独白挂在每句话下面，密封出价一个个拆开，最后公布真值。',
  'start.lot.go': '看这一件',
  'start.recap.title': '三分钟版',
  'start.recap.body': '演示夜里的高光片段，按夜里的顺序连成一段，每段前有标题卡。',
  'start.recap.go': '三分钟看完一夜',
  'start.awards.title': '颁奖页',
  'start.awards.body': '每个奖的判定规则和背后的数字，再加上违背誓约、不实说法和合谋这几类发现。',
  'start.awards.go': '看颁奖',

  'season.title': '赛季 s1',
  'season.intro': '六个模型和两个规则机器人打了六夜。每夜用同一副牌，模型每夜往后挪一个座位，所以每个模型都把每个座位坐过一次。',
  'season.boardTitle': '积分榜',
  'season.boardNote': '按每夜平均盈余排名。盈余是拍下的东西的价值减去付出的价钱，不含任务奖金。',
  'season.oneDeck': 's1 只有一副牌，而名次区间要在不同的牌之间重抽才算得出来，所以这张榜没有名次区间，分不出哪些差距是真的。',
  'season.more': '赛季说明',
  'season.loading': '正在读取积分榜…',
  'season.failed': '积分榜没有读到。',
  'season.failedLink': '积分榜和六夜回放见赛季说明',
  'season.nightsTitle': '六夜回放',

  'board.rank': '名次',
  'board.entry': '参赛者',
  'board.nights': '夜数',
  'board.surplus': '每夜平均盈余',
  'board.abstain': '弃权率',
  'board.bot': '机器人',
  'board.names': Object.freeze({ 'bot:economist': '经济学家', 'bot:truthful': '老实人' }),

  'night.title': (n) => `第 ${n} 夜`,
  'night.go': '看回放',

  'run.title': '自己跑一夜',
  'run.body': '模拟夜不需要网络，也不需要密钥，装好 Python 3.12 和 uv，几条命令就能在自己的机器上打一夜，再用同一个回放页看。',
  'run.go': '试一试',

  'footer.repo': 'GitHub 仓库',
  'footer.license': 'MIT 许可证',

  'link.seasons': `${BLOB}docs/seasons_zh.md`,
  'link.try': `${BLOB}README_zh.md#试一试`,
});

const EN = Object.freeze({
  'page.title': 'AI Auction Night',

  'hero.title': 'AI Auction Night',
  'hero.lead':
    'Six language models and two rule-based bots bid for twelve lots under six textbook auction rules, and afterwards a host and two judges review the night.',
  'hero.alt':
    'The replay page playing one lot: the seats talk, each with a private thought under the line, the sealed bids open one by one, the hammer falls and the blind box shows its true value',
  'hero.caption': "Lot 5 of the demo night, a blind box: the winner paid 95 for an item worth 84, the winner's curse.",

  'start.title': 'Start here',
  'start.note': 'These open in the replay page, which is in Chinese.',
  'start.lot.title': 'Lot 5 of the demo night',
  'start.lot.body':
    'A blind box. The seats talk, each line with a private thought under it, the sealed bids open one by one, and the true value comes last.',
  'start.lot.go': 'Watch the lot',
  'start.recap.title': 'The three-minute version',
  'start.recap.body': 'The best moments of the demo night in the order they happened, each with a title card.',
  'start.recap.go': 'Play it',
  'start.awards.title': 'The awards',
  'start.awards.body':
    'The rule of each award and the figure behind it, then the findings: broken pledges, false claims and collusion.',
  'start.awards.go': 'See the awards',

  'season.title': 'Season s1',
  'season.intro':
    'Six models and two rule-based bots played six nights on one deck. The models moved one seat on each night, so every model sat at every seat once.',
  'season.boardTitle': 'Leaderboard',
  'season.boardNote':
    'Ranked by mean surplus per night, which is the value of the lots won minus the price paid. The mission bonus is not included.',
  'season.oneDeck':
    'Rank intervals are drawn over decks, and s1 has one deck, so the board has no rank intervals and does not say which differences can be told apart.',
  'season.more': 'Season notes',
  'season.loading': 'Loading the leaderboard…',
  'season.failed': 'The leaderboard could not be loaded.',
  'season.failedLink': 'See the season notes for the leaderboard and the six replays',
  'season.nightsTitle': 'The six nights',

  'board.rank': 'Rank',
  'board.entry': 'Entry',
  'board.nights': 'Nights',
  'board.surplus': 'Mean surplus',
  'board.abstain': 'Abstentions',
  'board.bot': 'bot',
  'board.names': Object.freeze({ 'bot:economist': 'Economist', 'bot:truthful': 'Truthful' }),

  'night.title': (n) => `Night ${n}`,
  'night.go': 'Watch the replay',

  'run.title': 'Run a night yourself',
  'run.body':
    'A mock night needs no network and no key, so with Python 3.12 and uv installed, a few commands play a night on your own machine, and the same replay page shows it.',
  'run.go': 'Try it',

  'footer.repo': 'GitHub repository',
  'footer.license': 'MIT license',

  'link.seasons': `${BLOB}docs/seasons.md`,
  'link.try': `${BLOB}README.md#try-it`,
});

export const STR = Object.freeze({ zh: ZH, en: EN });

/**
 * The text for a key in a language. A function value is called with the rest of the arguments.
 * A key that does not exist is a mistake in the page and throws, so that "undefined" never
 * reaches the screen.
 */
export function textOf(lang, key, ...args) {
  const table = STR[lang];
  if (table === undefined || !Object.hasOwn(table, key)) throw new RangeError(`no text for ${lang}/${key}`);
  const value = table[key];
  return typeof value === 'function' ? value(...args) : value;
}

/**
 * The language to open the page in: the one the reader chose before, else the first language of
 * the browser's list that the page has, else English.
 */
export function pickLanguage({ stored = null, languages = [] } = {}) {
  if (LANGS.includes(stored)) return stored;
  for (const tag of languages) {
    const base = String(tag).toLowerCase().split(/[-_]/)[0];
    if (LANGS.includes(base)) return base;
  }
  return 'en';
}

/** The name to show for a row of the leaderboard: a name from the table, else its own label. */
export function nameOf(lang, id, label) {
  const names = STR[lang]['board.names'];
  return Object.hasOwn(names, id) ? names[id] : label;
}
