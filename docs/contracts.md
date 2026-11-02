<p align="center"><b>English</b> · <a href="contracts_zh.md">简体中文</a></p>

# Contracts

The five modules under `src/auction_night/contracts/` define the data shapes of one night: its
configuration, its events, its replay, the six mechanisms and the `act` tool. Engine, model
layer, show layer and viewer are written against them. The files of a season are described under
Season files.

## Freeze rule

The five contracts are frozen from version 0.1.0 on:

* A field, a value or a type may be added. Nothing may be changed, renamed or removed.
* A new field needs a default, so that data written before the change still validates.
* A new enum value is an addition. Code that reads enums must tolerate values it does not know.
* A change that breaks the rule needs a new schema version (`...@2`), not an edit.

Three tests guard the rule: the exported JSON Schemas in `schemas/` must equal a fresh export,
the canonical log of the miniature night must keep its digest, and the example
configurations must keep validating. Additions after 0.1.0 are listed in
[CHANGELOG.md](../CHANGELOG.md).

## Conventions

* **Amounts are integers** (coins). Ratios that need a fraction are integer thousandths with a
  `_milli` suffix. Events and replays contain no floats at all. Configuration may hold ratios
  such as `0.95`, with at most four decimals, and the engine must convert them exactly.
* **Canonical JSON** (`canonical_json`): sorted keys at every level, compact separators, UTF-8
  text, no NaN, floats rejected. Dict keys that are numbers (seats, lots, tiers) are written as
  strings and parsed back to integers. Models are written with their aliases.
* **Strict integers**: `120.0`, `"120"` and `true` are not amounts.
* **Text** has a length cap and, for single-line fields, no control characters. Sanitising
  (newlines, brackets, truncation) happens once at the engine boundary, before an event exists.
* **Seats** are numbered 1 to 8 and **lots** 1 to 36. A night uses seats `1..N` and lots
  `1..n_lots`.
* Models are frozen (fields cannot be reassigned) and reject unknown fields. Validation errors leave out the offending
  input value, so a pasted secret does not end up in a log.

## Mechanisms (`mechanism.py`)

A `MechanismDef` is data only: pricing (`first`, `second`, `all_pay`), clock (`none`,
`ascending_button`, `descending_levels`), value model (`private`, `common`), reserve, levels
and theta. `MECHANISMS` holds the six formats. Rule texts, payment examples, legal actions,
reference bids and settlement are all generated from these objects.

| Key | Pricing | Clock | Value | Parameters | Bid phases |
|---|---|---|---|---|---|
| `english` | second | ascending button | private | theta 4 | `english_init`, `english_revise` |
| `dutch` | first | descending levels | private | 10 levels | `dutch` |
| `sealed_first` | first | none | private | | `sealed` |
| `sealed_second` | second | none | private | | `sealed` |
| `all_pay` | all pay | none | private | no reserve | `sealed` |
| `blind_box` | second | none | common | | `sealed` |

Each definition carries the keys of its zh text templates (`rule`, `example`, `card`,
`glossary`, for example `mechanism.english.rule`) and the slots of its payment example
(`bids`, `winner_bid`, `price`, `payments`, `tier`, `grab_price`, `ladder_head`) with the
fixed example inputs. The settlement code fills the slots, so no number in a rule text is typed
by hand. `ref` is the versioned name used in events, such as `english@v1`.

## Configuration (`config.py`)

One YAML file describes one night. `load_config(path)` rejects duplicate keys and validates;
`config_hash` is the digest that enters the manifest. YAML is read with PyYAML, which is a
runtime dependency.

| Section | Content |
|---|---|
| `night` | id, deal seed (inline or from an environment variable) and its commitment, language, mode (`plain` or `variety`), formats and lots per format, tags, tier counts, budget, Dutch levels, English theta, blind-box prior and noise, missions, chat and thought limits |
| `season` | `single` or `rotation`, planned decks with seed commitments, this night's deck and rotation index, seat limit per vendor |
| `seats` | 2 to 8 seats. `llm`, `mock` (scripted policy), `bot` (`truthful` or `economist`, with `pacing`) or `human` (at most one). A model seat may name a persona, see Modes |
| `endpoints` | `anthropic` or `openai_compatible`, base URL or the name of the variable that holds it, name of the variable that holds the key, tool choice, strict tools, rate limits, capability records |
| `show` | offline host, exactly two judges, content scan |
| `limits` | call timeout, retries, cost cap in USD, price file |

Keys never appear in a configuration. Endpoints name environment variables, and the names are
validated (`^[A-Z][A-Z0-9_]{0,63}$`), so a pasted key is rejected.

### Modes

`night.mode` is `plain` or `variety`. In `plain` mode every model seat gets the same system
prompt apart from its code name and its private cards. In `variety` mode a seat that names a
persona (`seats[].persona`) gets one more paragraph, with the name and the description of the
persona, and the economist bot may recite the textbook of the format at hand. A persona changes
the tone and never the goal. The personas are `铁算盘`, `浪里个浪` and `影帝`
(`src/auction_night/llm/personas.py`). An `llm` seat of a variety night needs a persona, and a
seat of a plain night has none. A night in variety mode is left out of the season leaderboard
with the reason `variety`, and its replay has `ranked` false.

### Deal seed

The deal seed is secret until the night is published, so it follows the same rule as the keys:

* **Committed season configurations use `seed_env`**, the name of the environment variable that
  holds the seed, together with `deal_seed_commitment`. The seed never sits in a file.
* **Inline `seed` only appears in mock or test configurations**, where there is nothing to hide.
* Exactly one of `seed` and `seed_env` is required. The JSON Schema says so with `oneOf`.

Validating a configuration never reads the environment, so a configuration with `seed_env`
validates on a machine that does not have the variable. Code that needs the seed calls
`config.night.resolve_seed()`. It returns the inline seed, or the value of the variable with
surrounding whitespace removed, and raises `SeedResolutionError` when:

* the variable is missing or empty (the message names `night.seed_env` and the variable);
* the value is not a valid seed (1 to 128 characters from `A-Z a-z 0-9 _ . : -`);
* `deal_seed_commitment` is present and the seed does not open it, or it is still a
  placeholder (the message names `night.deal_seed_commitment`).

Error messages never contain the seed. An inline seed is checked against its commitment when
the configuration is validated. `config_hash` covers the configuration as written, so with
`seed_env` it covers the variable name and never the seed.

### Seed commitment

The commitment to a deal seed is the SHA-256 digest of its UTF-8 text, written
`sha256:<hex>` (`seed_commitment`). It is published before the season. A `seed_commit` event
in phase `reveal`, the manifest and the `night` section all check that the seed opens the
commitment next to it. A seed is only as hard to guess as it is long, so published seasons use
long random seeds. The commitment covers the random draws of the environment (lots, private
cards, budgets, missions, tie-break keys).

### Capability records

A capability record says what an endpoint accepts: `supports_sampling_params`,
`supports_forced_tool_choice`, `supports_strict_tools`, `thinking_modes`, `efforts` and
`explicit_thinking_max_effort`. `capabilities` is the endpoint default and
`model_capabilities` replaces it for one model id. Without a record, the defaults of the
endpoint kind apply. The root validator checks every seat and show role against the record of
its model and rejects:

* sampling parameters (`temperature`, `top_p`, `top_k`, `seed`) anywhere on a model whose
  record does not support them, on the seat or as an endpoint default;
* `tool_choice` other than `auto` where forced choice is unsupported, and strict tools where
  they are unsupported;
* a thinking mode the model does not accept, a non-adaptive thinking mode above the permitted
  effort, missing `thinking` or `effort` where the record lists modes or efforts (they must be
  pinned explicitly), and missing `max_tokens` on an Anthropic endpoint.

The example API configuration records, for the two Claude models:

| | `claude-haiku-5-5` | `claude-sonnet-5-5` |
|---|---|---|
| Sampling parameters | none, HTTP 400 | none, HTTP 400 |
| Forced `tool_choice` | accepted, but the reply has no thinking block | HTTP 400 |
| Thinking | `adaptive`, or `disabled` up to effort `high` | `adaptive`, or `between_tools` up to effort `high` |

`adaptive` means that the request carries no thinking field. The run never forces the tool.
Re-check these records whenever a model changes.

### Price list

`load_prices(path)` reads the dated price list that `estimate` and the cost gate use
(`limits.prices_file`). An entry gives the price of one model per million tokens for input and
output, and optionally for cache reads and writes, with its `currency` (`USD` unless stated), its
`as_of` date and its `source`. A missing cache price means the input price.

A price in `CNY` is counted in dollars with the rate of the list. `exchange_rates` then has an
entry `CNY` with `units_per_usd` (1 USD = `units_per_usd` CNY, the way a rate source quotes it),
its date and its source. The cost of a call is `tokens x price / units_per_usd` in exact decimal
arithmetic, rounded up to a whole micro-dollar, so the ledger and the cost cap stay in dollars. A
list that has a price in a currency without its rate is refused when it is read, and the commands
exit with status 2: no rate is assumed. A rate may also be listed for a list in dollars. `estimate`
then prints the amounts in that currency beside the dollars, with the rate and its date.
`configs/prices.bailian.yaml` is a list in yuan.

### Line-up rules

At most `max_seats_per_vendor` (default 2) seats per vendor. The host and the content
classifier must not share a vendor with a seat, and neither may a judge, unless
`allow_judge_vendor_overlap` is set. The two judges must differ. Tiers must add up to the lots
of the night, lots must spread evenly over the tags, every tier must be divisible by the Dutch
levels, and there must be enough eligible lots and seats for the missions. A value written as
`<like-this>` is a placeholder (model ids, and the commitment of the deal seed before it is
published): `find_placeholders` lists them, and a real run must find none.

## Events (`events.py`)

Every fact is one event: `{"seq", "lot", "type", "vis", "data"}`. `Event` is a discriminated
union on `type` of one envelope class per type; `parse_event` validates a line or a mapping.
`EventBase` is the shared envelope class. `seq` runs from 1 without gaps. `lot` is null for
night-level events.

### Visibility

The `vis` field is one of exactly four strings, and each type has one level.

| Value | Who may see it |
|---|---|
| `public` | seats and audience |
| `seat:N` | seat N during the night, everyone after it |
| `audience` | never a seat; the show layer and the replay |
| `system` | internal only, never published |

A prompt for seat N is built from `public` and `seat:N` events only. `visible_to(vis, viewer)`
implements this for the viewers `seat:N`, `audience`, `god` (everything except system) and
`system`.

### Event types

| Type | Vis | Lot | Payload |
|---|---|---|---|
| `night_start` | public | no | night id, mode, public lot catalog, seat code names, budget range, mission mix |
| `seed_commit` | public | no | phase `commit` with the SHA-256 commitment, phase `reveal` with the seed |
| `deal_private` | seat:N | no | budget, valuations, blind-box signals, mission with its canary |
| `plan` | seat:N | no | priority 0 to 3 and planned budget per lot, persona line |
| `lot_open` | public | yes | format, item, mechanism ref, tier, reserve, active and sitting-out seats, talk order |
| `talk` | public | yes | sanitised text and an optional structured pledge |
| `guess` | seat:N | yes | guess of another seat's remaining budget (the truth is added at export) |
| `action` | seat:N | yes, except `plan` | phase, status, effective value, raw value if clamped, thought |
| `english_dropout` | public | yes | seat, price, remaining seats; a non-participant is announced at price 0 |
| `dutch_result` | public | yes | price ladder, level, price, buyer, or unsold |
| `hammer` | public | yes | winner, price, payments, revealed sealed bids, blind-box value, tied seats, newly out seats |
| `mission_result` | public | no | outcome of one mission, published after the last lot |
| `night_end` | public | no | status and the final scores |
| `host_line` | audience | per-lot lines only | offline commentary |
| `judge_extract` | audience | no | one judge's raw extractions and mission guesses |
| `judge_verdict` | audience | optional | finding decided by program or confirmed by both judges |
| `award` | audience | no | award, winners, metric; a vacant award has no winners |
| `cost_event` | system | optional | cost tally, or the moment the cost cap fired |
| `parse_repair` | system | optional | repair codes of one attempt to read a reply and how it ended (`ok`, `retry`, `abstain`) |

Action statuses: `ok`, `clamped` (a bid above the remaining budget was reduced, which is
legal), `abstain` (with a reason: `missing`, `parse_failed`, `timeout`, `refusal`,
`budget_aborted`, `not_asked`) and `kept_previous` (an English revision that failed keeps the
earlier exit price). A missing action is always an abstention, never a default price.

`budget_aborted` marks an action that the cost gate skipped after the cap was reached. The seat
did not decide it. `src/auction_night/engine/muted.py` reads these actions off a log: a seat is
muted in a lot when its line or its bid there was skipped. An English revision that the gate skips
is logged as `kept_previous`, which has no reason, so it is not read as muted.

The envelope checks the visibility level of the type, that `vis` and the seat in the payload
agree, and whether the type takes a lot. The payloads check their own rules, for example that
the winner pays exactly the price, or that a clamped action has a raw value above its value.

`dumps_events` and `loads_events` read and write JSON lines. `validate_log` finds gaps in `seq`
and a missing, duplicated or misplaced start.

## The `act` tool (`act.py`)

One tool, `act`, serves every phase, so the tool definition and the cached prompt prefix never
change. Its fields are the union over phases, in this fixed order: `phase`, `say`,
`pledge_kind`, `pledge_lot`, `pledge_to`, `pledge_amount`, `guess_budget`, `exit_price`,
`grab_price`, `bid`, `plan_priorities`, `plan_budget`, `plan_persona`, `thought`.

The schema is strict-compatible: `additionalProperties` is false, every property is listed in
`required`, and a phase-dependent property is nullable. `phase` and `thought` are never null.
Length limits are not in the schema: the sanitiser truncates.

Nullability is written in two ways that admit the same values:

| Export | Form | Example |
|---|---|---|
| `act_tool_anthropic()` (default) | `anyOf` with a null branch | `{"anyOf": [{"type": "integer"}, {"type": "null"}]}` |
| `act_tool_openai()` (default) | type array | `{"type": ["integer", "null"]}` |

Both exports stay inside the keyword subset that strict schemas support: `type`, `properties`,
`required`, `additionalProperties`, `enum`, `items` and `anyOf`. They use no numeric, string
or array constraints, no references and no recursion, and every object sets
`additionalProperties` to false. A test checks this on the functions and on the committed
files. `act_input_schema` takes the form as an explicit argument, so every caller states it.

| Phase | Required (non-null) | Optional |
|---|---|---|
| `plan` | `plan_priorities`, `plan_budget`, `plan_persona`, `thought` | |
| `talk` | `say` (may be empty), `guess_budget`, `thought` | the four `pledge_*` fields |
| `english_init`, `english_revise` | `exit_price`, `thought` | |
| `dutch` | `grab_price`, `thought` | |
| `sealed` | `bid`, `thought` | |

A lot uses `talk` plus the bid phases of its format (`lot_phases`). The pledge is four flat
fields rather than one object; `extract_pledge` builds the structured `Pledge`.

* `act_tool_anthropic()` returns `{name, description, strict: true, input_schema}` with the
  `anyOf` form.
* `act_tool_openai(strict=...)` returns the chat-completions form with `parameters` and type
  arrays.
* `validate_act(args, phase, ctx)` checks a parsed dict against the phase table and returns a
  list of `FieldError(field, code, message)` with codes `missing_required`, `wrong_type`,
  `out_of_range`, `phase_mismatch` and `inconsistent_pledge`. Fields that a phase does not use
  are ignored. A bid above the budget is not an error, because the engine clamps it. The
  parsing ladder that turns a reply into such a dict is `src/auction_night/llm/parse.py`.

The Anthropic documentation for strict tool use lists `anyOf` and null among the supported
schema features and does not mention type arrays such as `["integer", "null"]`, so the Anthropic
export uses `anyOf`. If an endpoint refuses it,
`act_tool_anthropic("type_array")` gives the other form without touching anything else. Strict
mode of OpenAI-compatible endpoints is endpoint specific, and
`act_tool_openai(nullable_style="any_of")` gives the first form there as well.

## Replay (`replay.py`)

`replay.json` v1 is one self-contained file with `"schema": "auction-night/replay@1"`.

| Part | Content |
|---|---|
| `night`, `seats`, `catalog` | public facts, who sat where with model ids (configured and returned), the lot catalog |
| `timeline` | per lot: talk, bid-phase actions, English drop-outs, Dutch result, hammer, overlay, host line, event range |
| `reveal` | budgets, valuations, signals, blind-box values, missions, plans, guesses with the truth, thoughts |
| `pledges`, `judges`, `judges_absent`, `verdicts`, `judge_accuracy` | pledge outcomes decided by program, raw judge output, the judges that gave no usable answer and why, findings, judge accuracy on the missions |
| `awards`, `bots_reference`, `highlights` | awards for model seats, the same for bots as reference, jump-to moments scored 0 to 10 |
| `scores` | surplus, mission bonus and total kept apart, plus reference statistics |
| `host`, `usage`, `manifest` | offline commentary, calls and tokens by role, config hash, prompt version, git commit, seed commitment and revealed seed, and the findings of the content gate that a person cleared |

A bid-phase action of the timeline with the status `abstain` and the reason `budget_aborted` is a
bid that the cost gate skipped. The viewer gives such a lot one line that says so, and the overlay
of the lot has the textbook bid of that seat and neither a regret nor an excess return.

The required parts are `night`, `seats`, `catalog`, `timeline`, `reveal`, `scores` and
`manifest`. The show-layer parts default to empty. Per-lot overlay numbers are descriptive and
are never summed into ratios. They are the textbook bids, the pacing factor, the regret,
whether the lot went to a highest value, the excess over the textbook bid, the exact expected
revenue in thousandths of a coin and, for a blind box, the price minus the true value. The
manifest must agree with the night on the mechanism versions
and, when it carries a revealed seed, with the commitment. The replay never contains the
canaries of the private missions.

## Season files

The files of a season are defined outside the five modules, and `schemas/` does not hold them.
Each names its format in `schema`.

| Schema id | File | Defined in |
|---|---|---|
| `auction-night/season-commitments@1` | `seasons/<season>/commitments.json`: the commitments to the deal seeds, published before the season | `src/auction_night/seeds.py` |
| `auction-night/season-seeds@1` | `seasons/<season>/seeds.json`: the deal seeds, published after the season | `src/auction_night/seeds.py` |
| `auction-night/season-seeds-private@1` | the private file of the deal seeds, which stays outside every git work tree | `src/auction_night/seeds.py` |
| `auction-night/leaderboard@1` | `leaderboard.json`: the board that `auction-night season` builds from finished nights | `src/auction_night/season.py` |

## Schemas

`schemas/` holds the exported JSON Schemas (`config`, `events`, `replay`, `prices`) and the two
tool definitions. Regenerate them after any contract change with:

```
uv run python -c "from auction_night.contracts import write_schemas; write_schemas('schemas')"
```

The exported text is deterministic, and a test compares the committed files with a fresh export.
Schemas cannot express everything: cross-field rules (a winner pays the price, visibility per
type) are enforced by the pydantic models only.
