<p align="center"><a href="contracts.md">English</a> · <b>简体中文</b></p>

# 契约

`src/auction_night/contracts/` 下的五个模块定义了一夜的数据形状：配置、事件、回放、六种机制和 `act` 工具。引擎、模型层、节目层和回放页都依据它们编写。赛季的文件在“赛季文件”一节说明。

## 冻结规则

五个契约从 0.1.0 版起冻结：

* 字段、取值或类型可以新增。任何东西都不得更改、改名或删除。
* 新字段需要有默认值，这样改动之前写出的数据仍能通过校验。
* 新的枚举值算新增。读取枚举的代码必须容忍自己不认识的取值。
* 违反这条规则的改动需要新的 schema 版本（`...@2`），不能直接修改。

三个测试守着这条规则。`schemas/` 里导出的 JSON Schema 必须与重新导出的结果相同。迷你一夜的规范日志必须保持它的摘要。示例配置必须一直能通过校验。0.1.0 之后的新增列在 [CHANGELOG_zh.md](../CHANGELOG_zh.md) 里。

## 约定

* **金额是整数**（金币）。需要用分数表示的比率，写成整数千分数，字段名带 `_milli` 后缀。事件和回放里完全没有浮点数。配置里可以写 `0.95` 这样的比率，最多四位小数。引擎必须把它们精确地转换。
* **规范 JSON**（`canonical_json`）：每一层排序的键、紧凑的分隔符、UTF-8 文本、不含 NaN、拒绝浮点数。字典里作为数字的键（座位、拍品、档位）写成字符串，读回时再解析成整数。数据模型按别名写出。
* **严格整数**：`120.0`、`"120"` 和 `true` 都不是金额。
* **文本**有长度上限，单行字段里不能有控制字符。清洗（换行、括号、截断）在引擎边界处做一次，那时事件还没有产生。
* **座位**编号为 1 到 8，**拍品**编号为 1 到 36。一夜使用座位 `1..N` 和拍品 `1..n_lots`。
* 数据模型是冻结的（字段不能重新赋值），并拒绝未知字段。校验错误里不含出错的输入值，所以粘贴进来的秘密不会落进日志。

## 机制（`mechanism.py`）

`MechanismDef` 只含数据：定价（`first`、`second`、`all_pay`）、时钟（`none`、`ascending_button`、`descending_levels`）、估值模型（`private`、`common`）、保留价、档数和 theta。`MECHANISMS` 保存六种规则。规则文字、付款例子、合法动作、参考出价和结算，全都由这些对象生成。

| 键 | 定价 | 时钟 | 估值 | 参数 | 出价阶段 |
|---|---|---|---|---|---|
| `english` | 二价 | 升价按钮 | 私人 | theta 4 | `english_init`、`english_revise` |
| `dutch` | 一价 | 逐档降价 | 私人 | 10 档 | `dutch` |
| `sealed_first` | 一价 | 无 | 私人 | | `sealed` |
| `sealed_second` | 二价 | 无 | 私人 | | `sealed` |
| `all_pay` | 全员付费 | 无 | 私人 | 无保留价 | `sealed` |
| `blind_box` | 二价 | 无 | 共同 | | `sealed` |

每个定义都带有它的中文文本模板的键（`rule`、`example`、`card`、`glossary`，例如 `mechanism.english.rule`），以及它的付款例子的各个槽位（`bids`、`winner_bid`、`price`、`payments`、`tier`、`grab_price`、`ladder_head`）和固定的示例输入。结算代码填充这些槽位，所以规则文字里的任何数字都不是手写的。`ref` 是事件里使用的带版本的名字，例如 `english@v1`。

## 配置（`config.py`）

一份 YAML 文件描述一夜。`load_config(path)` 拒绝重复的键并做校验。`config_hash` 是进入清单的摘要。YAML 用 PyYAML 读取，它是运行时依赖。

| 部分 | 内容 |
|---|---|
| `night` | 夜号、发牌种子（内联，或来自环境变量）及其承诺、语言、模式（`plain` 或 `variety`）、各规则及每种规则的拍品数、标签、各档位的数量、预算、荷式的档数、英式的 theta、盲盒的先验和噪声、任务、发言和内心独白的上限 |
| `season` | `single` 或 `rotation`、计划使用的各副牌及其种子承诺、这一夜用的那副牌和轮座序号、每家厂商的座位数上限 |
| `seats` | 2 到 8 个座位。`llm`、`mock`（脚本策略）、`bot`（`truthful` 或 `economist`，带 `pacing`）或 `human`（最多一个）。模型座位可以指定人设，见“模式” |
| `endpoints` | `anthropic` 或 `openai_compatible`、基础 URL 或保存它的变量名、保存密钥的变量名、工具选择、严格工具、速率限制、能力记录 |
| `show` | 离线主持人、恰好两位评委、内容筛查 |
| `limits` | 调用超时、重试、以美元计的费用上限、价目表文件 |

配置里从不出现密钥。端点指定的是环境变量名，变量名会被校验（`^[A-Z][A-Z0-9_]{0,63}$`）。所以粘贴进来的密钥会被拒绝。

### 模式

`night.mode` 取 `plain`（原味模式）或 `variety`（综艺模式）。在 `plain` 模式下，每个模型座位的系统提示词，除了代号和底牌，都相同。在 `variety` 模式下，指定了人设（`seats[].persona`）的座位会多得到一段文字，写着人设的名字和描述。经济学家机器人也可以背诵手头这种规则的教科书内容。人设只改变语气，从不改变目标。人设有 `铁算盘`、`浪里个浪` 和 `影帝`（`src/auction_night/llm/personas.py`）。综艺模式一夜里的 `llm` 座位需要人设，原味模式一夜里的座位没有人设。综艺模式的一夜会被排除在赛季积分榜之外，理由记为 `variety`。它的回放里 `ranked` 为 false。

### 发牌种子

发牌种子在这一夜公布之前是秘密，所以它遵循和密钥相同的规则：

* **提交到仓库的赛季配置使用 `seed_env`**，也就是保存种子的环境变量名，并同时带有 `deal_seed_commitment`。种子从不放在文件里。
* **内联的 `seed` 只出现在模拟配置或测试配置里**，那里没有什么需要隐藏。
* `seed` 和 `seed_env` 必须恰好给出一个。JSON Schema 用 `oneOf` 表达这一点。

校验配置从不读取环境变量，所以带 `seed_env` 的配置在没有该变量的机器上也能通过校验。需要种子的代码调用 `config.night.resolve_seed()`。它返回内联的种子，或者去掉首尾空白的变量值，并在以下情况抛出 `SeedResolutionError`：

* 变量不存在或为空（报错信息会指出 `night.seed_env` 和这个变量）
* 取值不是有效的种子（由 `A-Z a-z 0-9 _ . : -` 中的字符组成，长度为 1 到 128）
* `deal_seed_commitment` 存在而种子打不开它，或者它仍是占位符（报错信息会指出 `night.deal_seed_commitment`）

错误信息从不包含种子。内联的种子在校验配置时就与它的承诺核对。`config_hash` 涵盖的是按原样写出的配置。所以用 `seed_env` 时，它涵盖变量名，从不涵盖种子。

### 种子承诺

发牌种子的承诺是其 UTF-8 文本的 SHA-256 摘要，写成 `sha256:<hex>`（`seed_commitment`）。它在赛季前公布。阶段为 `reveal` 的 `seed_commit` 事件、清单和 `night` 部分，三处都会核对种子能打开紧挨着它的那条承诺。种子有多难猜取决于它有多长，所以公布的赛季使用很长的随机种子。承诺涵盖环境的随机抽取（拍品、底牌、预算、任务、平局键）。

### 能力记录

能力记录说明一个端点接受什么：`supports_sampling_params`、`supports_forced_tool_choice`、`supports_strict_tools`、`thinking_modes`、`efforts` 和 `explicit_thinking_max_effort`。`capabilities` 是端点的默认值，`model_capabilities` 对某一个模型型号替换它。没有记录时，使用该类端点的默认值。根校验器按每个座位和节目角色所用模型的记录检查它们，并拒绝以下情况：

* 在记录不支持采样参数（`temperature`、`top_p`、`top_k`、`seed`）的模型上出现这些参数，不论写在座位上还是作为端点默认值
* 在不支持强制选择的地方使用 `auto` 以外的 `tool_choice`，以及在不支持严格工具的地方使用严格工具
* 模型不接受的思考模式、高于允许强度的非自适应思考模式、记录里列出了模式或强度却缺少 `thinking` 或 `effort`（它们必须显式指定），以及 Anthropic 端点上缺少 `max_tokens`

示例 API 配置为两个 Claude 模型记录了：

| | `claude-haiku-5-5` | `claude-sonnet-5-5` |
|---|---|---|
| 采样参数 | 都不接受，HTTP 400 | 都不接受，HTTP 400 |
| 强制 `tool_choice` | 接受，但回复里没有 thinking 块 | HTTP 400 |
| 思考 | `adaptive`，或强度最高到 `high` 的 `disabled` | `adaptive`，或强度最高到 `high` 的 `between_tools` |

`adaptive` 表示请求里不带 thinking 字段。运行时从不强制使用该工具。每当模型有变，都要重新核对这些记录。

### 价目表

`load_prices(path)` 读取带日期的价目表，`estimate` 和成本闸都使用它（`limits.prices_file`）。每个条目给出一个模型每百万 token 的输入价和输出价，也可以给缓存读取价和缓存写入价，并带有它的 `currency`（不写就是 `USD`）、`as_of` 日期和 `source`。没有缓存价格时，缓存价格就是输入价。

`CNY` 的价格用价目表自带的汇率折成美元计算。这时 `exchange_rates` 里有一个 `CNY` 条目，带 `units_per_usd`（1 USD = `units_per_usd` CNY，与汇率来源的报价方式一致）、它的日期和它的来源。一次调用的费用是 `tokens x price / units_per_usd`，用精确的十进制算术计算，并向上取整到整数微美元。所以账本和费用上限仍以美元计。价目表里有某种货币的价格却没有它的汇率时，读取就会被拒绝，命令以状态 2 退出：不会假设任何汇率。以美元计价的价目表也可以列出汇率。这时 `estimate` 会在美元旁边打印该货币的金额，并附上汇率及其日期。`configs/prices.bailian.yaml` 是以人民币标价的价目表。

### 阵容规则

每家厂商最多占 `max_seats_per_vendor`（默认 2）个座位。主持人和内容分类器不得与任何座位同属一家厂商，评委也不得，除非设置了 `allow_judge_vendor_overlap`。两位评委必须不同。各档位的数量加起来必须等于这一夜的拍品数。拍品必须均匀分布在各个标签上。每个档位都必须能被荷式的档数整除。符合条件的拍品和座位必须足够多，才能安排任务。写成 `<like-this>` 的值是占位符（模型型号，以及发布之前的发牌种子承诺）：`find_placeholders` 会列出它们，正式运行时必须一个也找不到。

## 事件（`events.py`）

每个事实都是一个事件：`{"seq", "lot", "type", "vis", "data"}`。`Event` 是以 `type` 为判别字段的联合类型，每种类型有一个信封类。`parse_event` 校验一行文本或一个映射。`EventBase` 是共用的信封类。`seq` 从 1 开始，没有间隔。整夜级别的事件，`lot` 为 null。

### 可见性

`vis` 字段恰好是四个字符串之一，每种类型对应一个级别。

| 取值 | 谁可以看到 |
|---|---|
| `public` | 座位和观众 |
| `seat:N` | 夜里是 N 号座位，夜后是所有人 |
| `audience` | 座位永远看不到，节目层和回放可以看到 |
| `system` | 只在内部使用，从不发布 |

N 号座位的提示词只用 `public` 和 `seat:N` 事件构建。`visible_to(vis, viewer)` 为 `seat:N`、`audience`、`god`（除 system 以外的一切）和 `system` 这几种查看者实现了这一点。

### 事件类型

| 类型 | 可见性 | 拍品 | 载荷 |
|---|---|---|---|
| `night_start` | public | 否 | 夜号、模式、公开的拍品目录、座位代号、预算范围、任务构成 |
| `seed_commit` | public | 否 | 阶段 `commit` 带 SHA-256 承诺，阶段 `reveal` 带种子 |
| `deal_private` | seat:N | 否 | 预算、估值、盲盒鉴定价、任务及其 canary |
| `plan` | seat:N | 否 | 每件拍品的优先级（0 到 3）和计划预算、人设自述 |
| `lot_open` | public | 是 | 规则、物品、机制 ref、档位、保留价、在场和不在场的座位、发言顺序 |
| `talk` | public | 是 | 清洗后的文本和可选的结构化誓约 |
| `guess` | seat:N | 是 | 对另一个座位剩余预算的猜测（真值在导出时补上） |
| `action` | seat:N | 是，`plan` 除外 | 阶段、状态、生效值、被夹取时的原始值、内心独白 |
| `english_dropout` | public | 是 | 座位、价格、剩余座位，未参加的座位按价格 0 宣布 |
| `dutch_result` | public | 是 | 价格阶梯、档、价格、买家，或者流拍 |
| `hammer` | public | 是 | 赢家、价格、付款、公开的密封出价、盲盒真值、平局的座位、新出局的座位 |
| `mission_result` | public | 否 | 一个任务的结果，在最后一件拍品之后公布 |
| `night_end` | public | 否 | 状态和最终得分 |
| `host_line` | audience | 仅逐件的解说行 | 离线解说 |
| `judge_extract` | audience | 否 | 一位评委的原始抽取结果和任务猜测 |
| `judge_verdict` | audience | 可选 | 由程序判定或经两位评委共同确认的判定 |
| `award` | audience | 否 | 奖项、得主、指标，空缺的奖项没有得主 |
| `cost_event` | system | 可选 | 费用累计，或者费用上限触发的那一刻 |
| `parse_repair` | system | 可选 | 一次读取回复的尝试所用的修复代码，以及它的结局（`ok`、`retry`、`abstain`） |

动作状态有：`ok`，`clamped`（高于剩余预算的出价被降到了剩余预算，这是合法的），`abstain`（带原因：`missing`、`parse_failed`、`timeout`、`refusal`、`budget_aborted`、`not_asked`）和 `kept_previous`（失败的英式改价保留较早的退出价）。缺失的动作永远算弃权，绝不当作默认价格。

`budget_aborted` 标记的是达到上限之后被成本闸跳过的动作。这不是座位自己的决定。`src/auction_night/engine/muted.py` 从日志里读出这些动作：座位在某件拍品里的发言或出价被跳过，就算它在那一件里被静音。被闸门跳过的英式改价记为 `kept_previous`，它没有原因，所以不会被读成静音。

信封检查这种类型的可见性级别，检查 `vis` 与载荷里的座位是否一致，也检查这种类型是否带 lot。载荷检查各自的规则，例如赢家恰好付出成交价，或者被夹取的动作的原始值高于它的值。

`dumps_events` 和 `loads_events` 读写 JSON Lines。`validate_log` 找出 `seq` 里的间隔，以及缺失、重复或错位的开头。

## `act` 工具（`act.py`）

只有一个工具 `act` 服务所有阶段，所以工具定义和被缓存的提示词前缀从不变化。它的字段是各阶段字段的并集，按这个固定顺序排列：`phase`、`say`、`pledge_kind`、`pledge_lot`、`pledge_to`、`pledge_amount`、`guess_budget`、`exit_price`、`grab_price`、`bid`、`plan_priorities`、`plan_budget`、`plan_persona`、`thought`。

这份 schema 与严格模式兼容。`additionalProperties` 为 false。每个属性都列在 `required` 里。随阶段而定的属性可以为 null。`phase` 和 `thought` 永远不为 null。长度限制不在 schema 里，由清洗器截断。

可空性有两种写法，允许的取值相同：

| 导出函数 | 写法 | 示例 |
|---|---|---|
| `act_tool_anthropic()`（默认） | 带 null 分支的 `anyOf` | `{"anyOf": [{"type": "integer"}, {"type": "null"}]}` |
| `act_tool_openai()`（默认） | 类型数组 | `{"type": ["integer", "null"]}` |

两种导出都只用严格 schema 支持的那部分关键字：`type`、`properties`、`required`、`additionalProperties`、`enum`、`items` 和 `anyOf`。它们不用数值、字符串或数组约束，不用引用，也不用递归。每个对象都把 `additionalProperties` 设为 false。有一个测试在这些函数和已提交的文件上检查这一点。`act_input_schema` 把写法作为显式参数，所以每个调用方都要写明它。

| 阶段 | 必填（非 null） | 可选 |
|---|---|---|
| `plan` | `plan_priorities`、`plan_budget`、`plan_persona`、`thought` | |
| `talk` | `say`（可以为空）、`guess_budget`、`thought` | 四个 `pledge_*` 字段 |
| `english_init`、`english_revise` | `exit_price`、`thought` | |
| `dutch` | `grab_price`、`thought` | |
| `sealed` | `bid`、`thought` | |

一件拍品使用 `talk`，加上它的规则所对应的出价阶段（`lot_phases`）。誓约是四个平铺的字段，而不是一个对象。`extract_pledge` 构建结构化的 `Pledge`。

* `act_tool_anthropic()` 返回 `{name, description, strict: true, input_schema}`，用 `anyOf` 写法。
* `act_tool_openai(strict=...)` 返回 chat-completions 的格式，带 `parameters` 和类型数组。
* `validate_act(args, phase, ctx)` 按阶段表检查一个已解析的字典，返回 `FieldError(field, code, message)` 的列表，其中 code 取 `missing_required`、`wrong_type`、`out_of_range`、`phase_mismatch` 和 `inconsistent_pledge`。阶段用不到的字段会被忽略。高于预算的出价不算错误，因为引擎会把它夹取到预算。把回复变成这种字典的解析阶梯在 `src/auction_night/llm/parse.py`。

Anthropic 关于严格工具调用的文档，在受支持的 schema 特性里列出了 `anyOf` 和 null。文档没有提到 `["integer", "null"]` 这样的类型数组。所以 Anthropic 的导出使用 `anyOf`。如果某个端点拒绝它，`act_tool_anthropic("type_array")` 给出另一种写法，其余一概不动。兼容 OpenAI 的端点的严格模式因端点而异，`act_tool_openai(nullable_style="any_of")` 也能在那里给出第一种写法。

## 回放（`replay.py`）

`replay.json` 第 1 版是一个自包含的文件，带有 `"schema": "auction-night/replay@1"`。

| 部分 | 内容 |
|---|---|
| `night`、`seats`、`catalog` | 公开的事实，谁坐哪个座位以及模型型号（配置的和接口返回的），拍品目录 |
| `timeline` | 每件拍品：发言、出价阶段的动作、英式的退出、荷式的结果、落槌、数字对照、主持人解说、事件范围 |
| `reveal` | 预算、估值、鉴定价、盲盒真值、任务、计划、附真值的猜测、内心独白 |
| `pledges`、`judges`、`judges_absent`、`verdicts`、`judge_accuracy` | 由程序判定的誓约结果、评委的原始输出、没有给出可用回答的评委及原因、判定、评委在任务上的准确率 |
| `awards`、`bots_reference`、`highlights` | 模型座位的奖项、机器人的同类数据（仅作参照）、打 0 到 10 分的可跳转时刻 |
| `scores` | 分开记录的盈余、任务奖金和总分，以及参考统计 |
| `host`、`usage`、`manifest` | 离线解说、按角色统计的调用次数和 token 数、配置哈希、提示词版本、git 提交、种子承诺和揭晓的种子，以及由人放行的内容闸门命中项 |

时间线里状态为 `abstain`、原因为 `budget_aborted` 的出价阶段动作，是被成本闸跳过的出价。回放页会给这样的拍品加一行说明。这件拍品的数字对照里有该座位的教科书出价，没有后悔值，也没有超额收益。

必需的部分是 `night`、`seats`、`catalog`、`timeline`、`reveal`、`scores` 和 `manifest`。节目层的部分默认为空。每件拍品的数字对照只是描述性的，从不加总成比率。它们包括教科书出价、预算折扣系数、后悔值、这件拍品是否给了估值最高的人、超出教科书出价的部分、精确到千分之一枚金币的期望成交额，以及盲盒的成交价减去真值。清单必须与这一夜在机制版本上一致，带有揭晓的种子时，还必须与承诺一致。回放里从不含私密任务的 canary。

## 赛季文件

赛季的文件定义在这五个模块之外，`schemas/` 里没有它们。每个文件在 `schema` 里写明自己的格式。

| Schema ID | 文件 | 定义位置 |
|---|---|---|
| `auction-night/season-commitments@1` | `seasons/<season>/commitments.json`：发牌种子的承诺，在赛季前公布 | `src/auction_night/seeds.py` |
| `auction-night/season-seeds@1` | `seasons/<season>/seeds.json`：发牌种子，在赛季后公布 | `src/auction_night/seeds.py` |
| `auction-night/season-seeds-private@1` | 发牌种子的私密文件，始终留在所有 git 工作树之外 | `src/auction_night/seeds.py` |
| `auction-night/leaderboard@1` | `leaderboard.json`：`auction-night season` 从已打完的夜里建出的积分榜 | `src/auction_night/season.py` |

## Schema

`schemas/` 里是导出的 JSON Schema（`config`、`events`、`replay`、`prices`）和两份工具定义。任何契约变化之后，用下面的命令重新生成它们：

```
uv run python -c "from auction_night.contracts import write_schemas; write_schemas('schemas')"
```

导出的文本是确定的。有一个测试把已提交的文件与重新导出的结果比较。Schema 不能表达所有规则：跨字段的规则（赢家付出成交价，每种类型的可见性）只由 pydantic 模型来执行。
