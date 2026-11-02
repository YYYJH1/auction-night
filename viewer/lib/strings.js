// Every piece of fixed text on the page, kept here apart from the logic.

const names = (list, name) => list.map((seat) => `${seat} 号${name(seat)}`).join('、');

// What a claim about one's own cards can be about (verdict.topic). It stands out here because the
// tests of the award table read the entries of STR that are indented six spaces.
const CLAIM_TOPICS = Object.freeze({
  budget: '自己的预算',
  valuation: '自己对这件拍品的估值',
  mission_kind: '自己的秘密任务是哪一种',
  mission_tag: '自己的秘密任务要收哪个标签',
  plan: '自己的计划',
  unknown: '自己的底牌',
});

export const STR = Object.freeze({
  documentTitle: (nightId) => `${nightId} · AI 拍卖之夜`,

  welcome: Object.freeze({
    title: '打开一场回放',
    body: '这个页面只读取一个 replay.json 文件，不上传任何东西。请选择本机的文件，或者在地址后面加上 ?replay=相对路径。',
    pick: '选择回放文件',
  }),
  loading: '正在载入回放…',
  night: Object.freeze({
    season: (id) => `赛季 ${id}`,
    deck: (id) => `牌组 ${id}`,
    rotation: (n) => `第 ${n} 轮座`,
    lots: (played, total) => (played === total ? `${total} 件拍品` : `${total} 件拍品，播了 ${played} 件`),
  }),
  loadFailed: '回放没有载入',
  openAnother: '打开回放文件…',
  linkNight: (linked, actual) => `这个链接指向夜号 ${linked}，当前打开的是 ${actual}。已显示当前回放的对应一件。`,

  err: Object.freeze({
    notObject: '文件内容不是 JSON 对象。',
    noSchema: '缺少 schema 字段，这不是回放文件。',
    version: (found) => `这个查看器只读 auction-night/replay@1，文件的版本是 ${found}。`,
    missing: (path) => `缺少字段 ${path}。`,
    invalid: (path) => `字段 ${path} 不合规。`,
    notJson: '文件不是合法的 JSON。',
    tooLarge: '文件太大，查看器不打开。',
    fetchFailed: (status) => `读取回放文件失败（HTTP ${status}）。`,
    network: '读取回放文件失败。',
    badPath: '?replay= 只接受本站内的相对路径，并且要以 .json 结尾。',
    cross: '回放文件必须和页面来自同一个站点。',
  }),

  controls: Object.freeze({
    group: '播放控制',
    play: '播放',
    pause: '暂停',
    prev: '上一件',
    next: '下一件',
    prevSegment: '上一段',
    nextSegment: '下一段',
    speeds: '播放速度',
    speed: (s) => `${s}×`,
    seek: '本件进度',
  }),

  lotList: Object.freeze({
    title: '拍品',
    lot: (n) => `第 ${n} 件`,
    unplayed: '未开拍',
  }),

  stage: Object.freeze({
    lotOf: (n, total) => `第 ${n} / ${total} 件`,
    tier: (t) => `档位 ${t}`,
    reserve: (r) => `保留价 ${r}`,
    noLot: '这一件没有开拍。',
    // The line on a lot in which the cost gate skipped some seats: from which lot on, and which seats.
    capNote: (from, seats) =>
      `费用上限：这一夜从第 ${from} 件起，模型座位因为费用上限不再行动。本件没有出价的座位：${seats.join('、')} 号。`,
    seats: '座位',
    chat: '公开频道',
    chatEmpty: '这一件没有人发言。',
    silent: '（这一轮没有说话）',
    chatNote: '频道里只显示代号。型号在座位卡的小字里。',
    host: '主持人',
    sittingOut: '本件不在场',
    spoke: '正在发言',
    won: '拍得本件',
    configured: (model) => `（配置：${model}）`,
    kinds: Object.freeze({ llm: '模型', mock: '脚本', bot: '规则机器人', human: '真人', unknown: '未知' }),
    pledge: (p, nameOf) => {
      if (p.kind === 'no_bid') return `誓约：第 ${p.lot ?? '?'} 件不出价`;
      if (p.kind === 'max_bid') return `誓约：第 ${p.lot ?? '?'} 件出价不超过 ${p.amount ?? '?'}`;
      if (p.kind === 'let_win') return `誓约：让 ${p.to ?? '?'} 号${p.to ? nameOf(p.to) : ''}拿下第 ${p.lot ?? '?'} 件`;
      return '誓约';
    },
  }),

  views: Object.freeze({
    label: '看哪一部分',
    lots: '逐件观看',
    recap: '三分钟看完一夜',
    highlights: '高光时刻',
    awards: '颁奖',
    review: '主持人复盘',
    recapOff: '这场回放没有记录高光时刻，编不出三分钟版。',
  }),

  gate: Object.freeze({
    title: '这里会剧透整晚',
    body: '这一页写着整晚的结果。看完最后一件，或者看完三分钟版，它会自动展开。也可以现在就展开。',
    open: '现在展开',
  }),

  awards: Object.freeze({
    title: '颁奖',
    intro: '只有模型座位参评。同分的座位并列获奖，达不到下限的奖本场空缺。机器人单列在页尾的「机器人参考」里。',
    note: '页面只摆出回放里记录的颁奖结果，不重新计算。',
    none: '这场回放没有颁奖记录。',
    rule: '判定',
    floor: '下限',
    noFloor: '没有额外的下限，条件已经写在判定里。',
    basis: '依据',
    states: Object.freeze({
      won: '获奖',
      tied: '并列获奖',
      vacant: '本场空缺',
      held: '需要底牌镜头',
    }),
    vacantWhy: (floor) => `没有座位达到下限（${floor}）。`,
    vacantPlain: '没有座位拿到这个奖。',
    vacantNoJudges: '这一夜有评委缺席，说法没法由两位评委共同认定，所以这个奖评不了。',
    held: '这个奖的依据是选手当时没公开的信息。打开底牌镜头，才能看到结果。',
    botsTitle: '机器人参考',
    botsNote: '机器人不参评，这里只是按同样口径给出的参照。',
    botsNone: '这场回放没有机器人参考。',
    unknownName: '其他奖项',
    unknownRule: '这个查看器还不认识这个奖，只显示回放里记录的结果。',
    metricPlain: (n) => `回放记录的数字：${n}`,
    coins: (n) => `${n} 金币`,
    missionDone: '完成了秘密任务',
    missionBonus: (n) => `完成了秘密任务，奖金 ${n}`,
    items: Object.freeze({
      most_textbook: Object.freeze({
        name: '最像教科书',
        rule: '普通拍品（不含盲盒）上，每次出价与这一件教科书参考出价的差距，除以这一件的档位，再取平均。平均差距最小的座位得奖。',
        floor: '至少 6 次有效出价',
        unit: 'milli',
        metric: (n) => `平均差距 ${n} 个档位`,
      }),
      winners_curse: Object.freeze({
        name: '赢家诅咒王',
        rule: '盲盒的赢家里，成交价比开箱真值高得最多的座位得奖。',
        floor: '成交价至少比真值高出这个盲盒档位的 5%',
        unit: 'coins',
        metric: (n) => `成交价比真值高 ${n}`,
      }),
      tilt: Object.freeze({
        name: '上头奖',
        rule: '全员付费的拍品上，没拍到却照付的金币总额最多的座位得奖。',
        floor: '至少 10 金币',
        unit: 'coins',
        metric: (n) => `没拍到却付了 ${n}`,
      }),
      bargain: Object.freeze({
        name: '捡漏王',
        rule: '单件拍品上，自己的估值减去成交价最大的座位得奖。',
        floor: '至少为这一件档位的 20%',
        unit: 'coins',
        metric: (n) => `估值比成交价高 ${n}`,
      }),
      backstabber: Object.freeze({
        name: '背刺王',
        rule: '违背结构化誓约次数最多的座位得奖。是否违背由程序按誓约判定。',
        floor: '至少 1 次',
        unit: 'count',
        metric: (n) => `违背了 ${n} 条誓约`,
      }),
      best_actor: Object.freeze({
        name: '最佳演员',
        rule: '关于自己底牌的说法，被两位评委共同判为不实的条数最多的座位得奖。',
        floor: '至少 1 条',
        unit: 'count',
        metric: (n) => `${n} 条说法被判为不实`,
      }),
      keeper: Object.freeze({
        name: '守信奖',
        rule: '立过至少 2 条誓约，并且全部遵守的座位得奖。',
        floor: null,
        vacant: '没有座位立过至少 2 条誓约并且全部遵守。',
        unit: 'count',
        metric: (n) => `${n} 条誓约全部遵守`,
      }),
      mission_master: Object.freeze({
        name: '任务达人',
        rule: '完成了秘密任务的座位得奖。多人完成时，给最早完成的座位。',
        floor: null,
        vacant: '没有座位完成秘密任务。',
        unit: null,
        metric: null,
      }),
      mind_reader: Object.freeze({
        name: '读心术',
        rule: '读心猜测与对方真实剩余预算的差距，按预算范围折算后取平均。平均差距最小的座位得奖。',
        floor: '至少 8 次有效猜测',
        unit: 'milli',
        metric: (n) => `平均误差 ${n}（预算范围的倍数）`,
      }),
      consistent: Object.freeze({
        name: '言行一致',
        rule: '计划为 2 到 3 星的拍品认真出了价（出价不低于自己估值的一半），计划为 0 到 1 星的拍品没怎么出价。这两种情况占比最高的座位得奖。',
        floor: '至少 10 件拍品可判',
        unit: 'milli',
        metric: (n) => `一致率 ${n}`,
      }),
    }),
  }),

  verdicts: Object.freeze({
    title: '判定',
    intro:
      '判定是整夜结束后得出的发现。背刺由程序拿誓约去对照出价，不靠评委；不实说法，以及合谋的聊天一侧，要两位来自不同厂商的评委都认定才算。',
    lensRule:
      '哪些内容要打开底牌镜头：凡是要拿选手没公开的估值、预算、任务去对的部分。誓约是公开的，背刺的判定一直看得到，只有出价记录里没公开的数字要打开底牌镜头；不实说法和合谋整条都要打开。',
    absentTitle: '有评委缺席',
    absentOne: (number, vendor, reason) => `评委 ${number}${vendor === null ? '' : `（${vendor}）`}缺席：${reason}。`,
    absentOnly:
      '没有两位评委就没有共同认定，所以这一夜只剩程序判定：背刺。不实说法一条也判不了，合谋的聊天一侧不能成立，这一夜不会有合谋红牌，出价一侧单独成立的件最多记为疑似。',
    absentReasons: Object.freeze({
      unreadable: '回复读不懂',
      timeout: '超时',
      refusal: '拒答',
      budget_aborted: '成本闸中止',
      other: '其他原因',
    }),
    count: (n) => `${n} 条`,
    lot: (n) => `第 ${n} 件`,
    lots: (list) => `第 ${list.join('、')} 件`,
    quote: (text) => `「${text}」`,
    held: '需要底牌镜头',
    pledge: Object.freeze({
      title: '背刺',
      basis: '依据：选手在公开频道里立下的结构化誓约，和这一件拍品里记下的出价。由程序判定，不靠评委。',
      made: (lot, target) => (target === null || target === lot ? `在第 ${lot} 件立誓` : `在第 ${lot} 件立誓，说的是第 ${target} 件`),
      about: (lots) => `涉及${lots}`,
      broken: '程序判定：违背',
      record: '程序的记录',
      detailHeld: '每条的出价记录要打开底牌镜头才能看到：英式拍卖里赢家的退出价、荷式拍卖里各座位的出手价，都没有公开。',
      none: '回放里没有记录背刺。',
    }),
    claim: Object.freeze({
      title: '不实说法',
      basis:
        '依据：两位评委都从公开频道里抽到同一句关于自己底牌的话，并且都没有拿不准；再由程序拿选手当时的底牌去对：说的金额超出容差（预算的容差是 10%，估值的容差是这一件档位的 15%），或者说的任务种类、标签对不上，才算不实。',
      held: '要拿选手没公开的底牌去对，才知道一句话实不实，连这条判定存在与否都会透露底牌。打开底牌镜头，才能看到，条数也不显示。',
      none: '回放里没有记录不实说法。',
      badge: '判定：不实',
      topicLabel: '说法的主题',
      topics: CLAIM_TOPICS,
      gapTier: (pct) => `偏差：占这一件档位的 ${pct}%`,
      gapBudget: (pct) => `偏差：占预算的 ${pct}%（预算按两种理解各算一次，此时还剩的和开场发的，取偏差较小的）`,
      gapPlain: (pct) => `偏差：${pct}%（回放没有记录它占的是档位还是预算）`,
      gapNone: '任务的种类和标签只有对错，没有偏差数字。',
      gapMissing: '回放没有记录偏差。',
      rounding:
        '偏差怎么算：引擎把差距向下取整到千分之一记下，页面把它除以 10 写成百分数，最多一位小数，不再四舍五入。',
      record: '引擎的记录',
    }),
    collusion: Object.freeze({
      title: '合谋',
      rules: Object.freeze([
        '聊天一侧：两位评委都从同一个座位的同一件拍品里，抽到了约定压价、轮流拿或让位的原话，并且都没有拿不准。约定要在这一件或上一件里说出口，说过这种话的座位组成圈子。',
        '出价一侧：这一件的统计量严格大于阈值。统计量有两部分：卖方实际收到的钱，比人人都按教科书出价时少了多少；圈内没拍到的座位，平均比自己的教科书出价低了多少。两部分都以这一件的档位为单位。',
        '阈值：规则机器人在没人合谋时打出的统计量，取第 95 百分位。所以没人合谋时，大约每 100 个观察值里最多 5 个会超过它。',
        '红牌要至少两件两侧都成立，因为单独一件也可能碰巧超过阈值。只有一件两侧都成立，或者只有一侧成立的件，记为疑似，并列出它们各自成立的一侧。',
        '一夜最多一条合谋判定，两个互不相干的约定也会合成一个圈子。',
      ]),
      held: '出价一侧要拿选手的私密估值算教科书出价，所以整条判定（连是红牌还是疑似）都要打开底牌镜头才能看到。',
      none: '回放里没有记录合谋红牌或疑似。',
      redTitle: '合谋红牌',
      redLead: (lots) => `${lots}两侧都成立，至少两件，所以是红牌。`,
      suspectTitle: '合谋疑似',
      suspectLead: '没有达到红牌的条件：红牌要至少两件两侧都成立。',
      ring: '圈内座位',
      ringNone: '聊天里没有两位评委都认定的约定，只有出价一侧在说话。',
      quotes: '两位评委共同认定的原话',
      statsTitle: '每一件的统计量',
      statsNote: '单位：这一件档位的千分之几，括号里是同一个数写成百分比。负数也照实写出。',
      thresholdLine: (milli, pct) => `阈值：${milli}（${pct}%）。统计量严格大于它，这一件的出价一侧才成立。`,
      record: '引擎的说明',
      thresholdMissing: '回放没有记录阈值，没法标出哪几件超过了它。',
      statsMissing: '回放没有记录每一件的统计量。',
      over: '超过阈值',
      listedRed: '入选红牌',
      listedSuspect: '入选疑似',
      sidesBoth: (lots) => `${lots}聊天和出价两侧都成立。`,
      sidesChat: (lots) => `${lots}统计量没有超过阈值，所以只有聊天一侧成立。`,
      sidesBid: (lots) => `${lots}统计量超过阈值，出价一侧成立。`,
      sidesBidOnly: (lots) => `另有${lots}统计量也超过了阈值，但聊天一侧不成立，没有计入红牌。`,
      judgesGone: '有评委缺席，聊天一侧无法成立。',
    }),
  }),

  review: Object.freeze({
    title: '主持人复盘',
    intro: '离线主持人在整夜结束后，根据引擎算出的事实卡写的复盘，里面的数字都在事实卡上。另外还有开场押的预测和夜末的对账。',
    recapTitle: '复盘',
    recapHeld: '主持人是看完整夜之后写的，会用到选手当时不知道的信息。打开底牌镜头，才能看到复盘。',
    recapNone: '这场回放没有主持人复盘。',
    predTitle: '赛前预测与对账',
    predIntro: '主持人在开场押了几条，每条押一种拍卖规则。夜末由程序按固定的规则对账，没有可比的拍品时就无法对账。',
    predHeld: '对账要拿教科书出价和选手的估值去比，打开底牌镜头，才能看到结果。',
    predNone: '这场回放没有赛前预测。',
    results: Object.freeze({ hit: '命中', miss: '未中', open: '无法对账' }),
    openWhy: '这一夜没有可以比较的拍品，或者没有模型座位出价。',
    tally: (hit, miss, open) => `命中 ${hit} 条，未中 ${miss} 条，无法对账 ${open} 条`,
    empty: '这场回放没有主持人复盘，也没有赛前预测。',
  }),

  opening: Object.freeze({
    title: '赛前预测',
    note: (n) => `主持人开场押了 ${n} 条，整夜结束后对账，结果在「主持人复盘」里。`,
  }),

  highlights: Object.freeze({
    title: '高光时刻',
    intro: '每个高光由规则打一个 0 到 10 的趣味分，只说这个时刻有多好看，不评价谁的表现。点一条，直接跳到那件拍品的对应时刻。',
    order: '按趣味分从高到低排列，分数相同时按拍品先后。',
    filter: '按类别筛选',
    all: '全部',
    count: (shown, total) => (shown === total ? `共 ${total} 条` : `显示 ${shown} 条，共 ${total} 条`),
    score: (n) => `趣味分 ${n}/10`,
    scoreWord: '趣味分',
    lot: (n, name) => `第 ${n} 件 ${name}`,
    wholeNight: '整晚',
    jump: '跳到这一刻',
    noJump: '这一刻没有对应的拍品',
    empty: '这场回放没有记录高光时刻。',
    held: '细节需要打开底牌镜头才能看到。',
    heldCount: (n) => `另有 ${n} 条的细节需要打开底牌镜头。`,
    unknownCategory: '其他',
    categories: Object.freeze({
      winners_curse: '赢家诅咒',
      shill_stuck: '托儿砸手里',
      backstab: '背刺',
      claim_vs_hand: '说法与底牌差得多',
      plan_contradiction: '计划打脸',
      all_pay_tilt: '全员付费上头',
      bargain: '捡漏',
      mindread_miss: '读心大失败',
    }),
  }),

  recap: Object.freeze({
    title: '三分钟看完一夜',
    segment: (k, n) => `第 ${k} / ${n} 段`,
    lot: (n, name) => `第 ${n} 件 ${name}`,
    score: (n) => `趣味分 ${n}/10`,
    held: '细节需要打开底牌镜头才能看到。',
    done: '三分钟版播完了。',
    seek: '三分钟版进度',
  }),

  lens: Object.freeze({
    label: '底牌镜头',
    on: '底牌镜头已开：以下是选手当时没看到的内容，包括私密估值、预算、任务、作战计划和内心独白。',
    off: '底牌镜头已关：只显示选手当时看得到的公开信息。',
    value: (n) => `估值 ${n}`,
    appraisal: (n) => `鉴定价 ${n}`,
    budget: (budget, left) => (left === null ? `预算 ${budget}` : `预算 ${budget}，此时还剩 ${left}`),
    plan: (stars, budget) => {
      const parts = [];
      if (stars) parts.push(stars);
      if (budget !== null) parts.push(`最多花 ${budget}`);
      return parts.length === 0 ? '' : `计划 ${parts.join('，')}`;
    },
    persona: (text) => `打算表现成：${text}`,
    collector: (need, tag) => `任务（收藏家）：拍到 ${need ?? '?'} 件「${tag ?? '?'}」标签的拍品`,
    shill: (lot, threshold) =>
      `任务（托儿）：让第 ${lot ?? '?'} 件的成交价不低于 ${threshold ?? '?'}，而且买下它的不是自己`,
    missionUnknown: '任务：未知类型',
    guess: (target, name, guess, truth) =>
      `读心：猜 ${target} 号${name}还剩 ${guess ?? '?'}${truth === null ? '' : `，实际 ${truth}`}`,
    noCard: '没有这个座位的底牌记录。',
    phases: Object.freeze({
      plan: '开场独白',
      talk: '喊话时',
      english_init: '初报退出价时',
      english_revise: '改价时',
      dutch: '报出手价时',
      sealed: '写出价时',
      unknown: '独白',
    }),
    hostNote: '主持人的话是看完整夜后写的，会用到选手当时不知道的信息。',
  }),

  bids: Object.freeze({
    title: '出价',
    rule: Object.freeze({
      first: '最高出价者得，付自己的出价。',
      second: '最高出价者得，付次高的出价。',
      all_pay: '最高出价者得到拍品，所有人都付自己的出价。',
      blind: '最高出价者得，付次高的出价，落槌后才揭晓拍品的真值。',
    }),
    intro: '所有人的出价都封在信封里。',
    opening: (seat, name) => `${seat} 号${name}的信封打开了`,
    nobody: '没有人参加这一件。',
    abstain: '弃权',
    muted: '未行动',
    reasons: Object.freeze({
      missing: '没有回应',
      parse_failed: '回复无法解析',
      timeout: '超时',
      refusal: '拒答',
      budget_aborted: '成本闸中止',
      not_asked: '没有被询问',
    }),
    clamped: (raw, value) => `想出 ${raw}，预算只够 ${value}`,
    pays: (n) => `付 ${n}`,
    sold: (seat, name, price) => `${seat} 号${name}拍得，成交价 ${price}`,
    unsold: '流拍，没有人拍到。',
    allPay: (n) => `${n} 个座位付了钱，各付自己的出价。`,
    tie: (listed) => `${listed}出价相同，按平局键决定。`,
    out: (listed) => `${listed}预算用尽，出局。`,
    openBox: (value) => `开箱：真值是 ${value}`,
    over: (d) => `成交价比真值高 ${d}，赢家多付了。`,
    under: (d) => `成交价比真值低 ${d}，赢家赚到了。`,
    same: '成交价正好等于真值。',
    names,
  }),

  summary: Object.freeze({
    unknown: '这个查看器还不认识这一件的规则，没有专门的动画，只显示结果。',
  }),

  english: Object.freeze({
    rule: '价格一路往上涨，座位在自己的退出价上离场。最后留下的人赢，按倒数第二个离场的价格买下。',
    intro: (start) => (start > 0 ? `价格从保留价 ${start} 开始上涨。` : '价格从 0 开始上涨。'),
    startNote: (start) => `从保留价 ${start} 起步`,
    nobody: '没有人参加这一件。',
    scale: '价格',
    price: '当前价',
    mark: (seat, name, price) => `${seat} 号${name}在 ${price} 退出`,
    notIn: (seat, name) => `${seat} 号${name}没有参加`,
    left: (seat, name, price, remaining) =>
      `${seat} 号${name}在 ${price} 退出${remaining === null ? '' : `，场上还剩 ${remaining} 人`}`,
    chip: Object.freeze({
      in: '还在场',
      skip: '没有参加',
      won: '拍得',
      left: (price) => `${price} 退出`,
    }),
    ghostMark: (exit) => `赢家的退出价 ${exit}`,
    ghost: (seat, name, exit, price) => {
      const head = `底牌镜头：${seat} 号${name}的退出价是 ${exit}`;
      if (exit > price) return `${head}。这个数没有公开，成交价只由倒数第二个离场的人定在 ${price}。`;
      return exit === price ? `${head}，和成交价一样高。` : `${head}。`;
    },
  }),

  dutch: Object.freeze({
    rule: (n) => `价格分 ${n} 档从高往低走。每个座位在开拍前就一次报好了出手价，价格钟只是把结果慢慢放给大家看。`,
    intro: '价格钟从最高一档开始往下走。',
    fall: (n, price) => `降到第 ${n} 档，价格 ${price}`,
    stop: (n, price, seat, name) => `停在第 ${n} 档，价格 ${price}，${seat} 号${name}买下`,
    none: (n) => `${n} 档走完，没有出手价落在这些价位上`,
    nobody: '这一件没有价位可走。',
    level: (n) => `第 ${n} 档`,
    buyer: (seat) => `${seat} 号`,
    grabs: '出手价（开拍前一次报好的，只有底牌镜头看得到）',
    grab: (seat, name, grab, cell) =>
      `${seat} 号${name}：${grab}，${cell === null ? '低于最低一档，不会出手' : `落在第 ${cell + 1} 档`}`,
    abstained: (seat, name, reason) => `${seat} 号${name}：弃权${reason ? `（${reason}）` : ''}`,
    muted: (seat, name, reason) => `${seat} 号${name}：未行动${reason ? `（${reason}）` : ''}`,
  }),

  overlay: Object.freeze({
    title: '数字对照',
    note: '这些数字都只属于这一件拍品，彼此不相加，也不折算成整夜的分数。',
    glossary: '术语表',
    glossaryTitle: '术语表',
    hidden: '打开底牌镜头，可以看到各座位的参考出价、后悔值和相对教科书的盈亏。',
    seat: '座位',
    missing: '—',
    revenue: '期望成交额',
    revenueLine: (expected, actual, allPay) => {
      if (actual === null) return `${expected}，这一件流拍了`;
      return allPay ? `${expected}，实际收到 ${actual}（每个出价的人都付了钱）` : `${expected}，实际成交价 ${actual}`;
    },
    highest: '给了估值最高的人',
    highestYes: '✓ 是',
    highestNo: '✗ 否',
    curse: '成交价减真值',
    curseOver: '赢家多付了',
    curseUnder: '赢家捡了便宜',
    curseSame: '正好等于真值',
    tableCaption: '各座位的数字',
    columns: Object.freeze({
      ref: '参考出价',
      alpha: 'α',
      regret: '后悔值',
      excess: '相对教科书',
    }),
    rowLabel: (name, seat) => `${seat} 号${name}`,
  }),

  // One or two plain sentences for each term. The overlay opens this list, and a term in the
  // overlay opens it at that term. Every figure explained here belongs to one lot.
  glossary: Object.freeze([
    Object.freeze({
      id: 'ref',
      term: '参考出价',
      text: '教科书模型给出的标准出价：只看自己的估值和在场人数，按规则算出来，最多取到自己剩下的预算。它不考虑任务，只当尺子用。',
    }),
    Object.freeze({
      id: 'alpha',
      term: 'α（预算折扣）',
      text: '只有经济学家机器人才有，在 0 到 1 之间。预算撑得起后面的拍品时是 1，撑不起时按比例把出价打折。',
    }),
    Object.freeze({
      id: 'regret',
      term: '后悔值',
      text: '假如事先知道别人怎么出，这一件本来最多还能多赚多少，0 表示没有更好的出法。英式拍卖按别人最后的退出价估算。',
    }),
    Object.freeze({
      id: 'excess',
      term: '相对教科书',
      text: '这一件的实际收益，减去照参考出价本来会得到的收益。为正说明偏离教科书反而多赚了，为负说明偏离让自己吃了亏，正负都照实显示。',
    }),
    Object.freeze({
      id: 'highest',
      term: '给了估值最高的人',
      text: '这一件最后是不是落到了在场估值最高的人手里。盲盒的估值谁也不知道，所以没有这一项。',
    }),
    Object.freeze({
      id: 'revenue',
      term: '期望成交额',
      text: '同样的规则、同样的出价人数、估值随机时，这一件平均能卖多少，精确到千分之一枚（全员付费算所有人付的钱）。有保留价的拍品没有这个数，它和实际成交价并排放着。',
    }),
    Object.freeze({
      id: 'curse',
      term: '成交价减真值',
      text: '盲盒落槌后揭晓真值，用成交价减去它。为正说明赢家多付了，为负说明赢家捡了便宜。',
    }),
    Object.freeze({
      id: 'winners-curse',
      term: '赢家诅咒',
      text: '大家估的是同一个东西时，出价最高的往往是鉴定价偏高的那位，所以赢家容易多付。',
    }),
    Object.freeze({
      id: 'exit',
      term: '退出价',
      text: '英式加价里，每个座位事先写下愿意出到的最高价。价格涨到这个数，它就离场。',
    }),
    Object.freeze({
      id: 'grab',
      term: '出手价',
      text: '荷式降价里，每个座位事先写下的买入价。价格降到不高于它的第一档，它就买下。',
    }),
    Object.freeze({
      id: 'lens',
      term: '底牌镜头',
      text: '像扑克转播亮出底牌：打开时能看到选手当时不知道的私密信息，比如各人的估值、预算和任务，关掉时只剩公开信息。',
    }),
  ]),
});
