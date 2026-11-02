"""Chinese rule texts and worked payment examples, generated from the mechanism definitions.

Nothing in a rule text is typed twice. The parameters it mentions (the English ``theta``, the
number of Dutch levels) are read from the mechanism definition, and every number in a payment
example comes from running the settlement functions on the fixed example inputs of the
definition. The tests parse the numbers back out of the text and compare them with a fresh run
of the settlement, so a text that drifts from the code fails.

Four texts exist per format, named by the keys of ``MechanismDef.text_keys``:

* ``rule``      what a bidder is told. It states the rules only and never what to bid.
* ``example``   one worked example, ``例：...``. Rule and example are meant to be joined.
* ``card``      the one-line card shown to the audience.
* ``glossary``  the textbook result for the audience. It is not part of any prompt.

``mechanism.common.rule`` holds the conventions shared by all formats.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from auction_night.contracts.mechanism import (
    COMMON_RULE_KEY,
    FORMAT_KEYS,
    MECHANISMS,
    FormatKey,
    MechanismDef,
    make_mechanism,
)
from auction_night.engine.formats import Answers, LotStep, play_lot

# The example bidders are rich enough for any example price: the examples are about prices,
# not about budgets.
_EXAMPLE_BUDGET: Final = 10_000
_EXAMPLE_TIER: Final = 200
_EXAMPLE_HIDDEN_VALUE: Final = 100
_ZH_COUNT: Final = {2: "两", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八"}


@dataclass(frozen=True, slots=True)
class PaymentExample:
    """The filled slots of one payment example, in the order the definition declares them.

    Lists are tuples. ``bids`` is the example input; every other slot is a result of the
    settlement run on that input.
    """

    key: FormatKey
    slots: dict[str, Any]


def _run_example(mech: MechanismDef) -> dict[str, Any]:
    """Run the settlement on the fixed example input and collect every slot we can fill."""
    spec = mech.example
    plain = make_mechanism(mech.key, theta=mech.theta, levels=mech.levels)  # reserve 0
    if mech.clock == "descending_levels":
        if spec.tier is None or spec.grab_price is None:
            raise ValueError("a Dutch example needs a tier and a grab price")
        grab = spec.grab_price
        outcome = play_lot(
            plain,
            tier=spec.tier,
            budgets={1: _EXAMPLE_BUDGET},
            tie_key={1: 0},
            answer=lambda step: {1: grab},
        )
        result = outcome.dutch
        if result is None or result.level is None:
            raise ValueError("the Dutch example does not sell")
        return {
            "tier": spec.tier,
            "grab_price": grab,
            "price": outcome.hammer.price,
            "ladder_head": tuple(result.ladder[: result.level]),
        }

    if spec.bids is None:
        raise ValueError(f"the example of {mech.key} needs bids")
    bids = tuple(spec.bids)
    seats = range(1, len(bids) + 1)
    offers = dict(zip(seats, bids, strict=True))

    def answer(step: LotStep) -> Answers:
        # Only the first round carries the example bids; revision rounds keep the exit prices.
        return offers if step.phase in ("sealed", "english_init") else {}

    outcome = play_lot(
        plain,
        tier=_EXAMPLE_TIER,
        budgets=dict.fromkeys(seats, _EXAMPLE_BUDGET),
        tie_key={seat: seat - 1 for seat in seats},
        answer=answer,
        true_value=_EXAMPLE_HIDDEN_VALUE if mech.value_model == "common" else None,
    )
    hammer = outcome.hammer
    if hammer.winner is None:
        raise ValueError(f"the example of {mech.key} does not sell")
    winner_bid = hammer.bids[hammer.winner] if hammer.bids else outcome.winner_exit
    return {
        "bids": bids,
        "winner_bid": winner_bid,
        "price": hammer.price,
        "payments": tuple(hammer.payments.get(seat, 0) for seat in seats),
    }


def payment_example(mech: MechanismDef) -> PaymentExample:
    """Fill the example slots the definition declares by running the settlement."""
    filled = _run_example(mech)
    missing = [slot for slot in mech.example.slots if slot not in filled]
    if missing:
        raise ValueError(f"cannot fill the example slots {missing} of {mech.key}")
    return PaymentExample(mech.key, {slot: filled[slot] for slot in mech.example.slots})


def _join(numbers: Sequence[int]) -> str:
    return "、".join(str(number) for number in numbers)


def _slot(example: PaymentExample, name: str) -> Any:
    if name not in example.slots:
        raise ValueError(f"the example of {example.key} does not declare the slot {name!r}")
    return example.slots[name]


# ---------------------------------------------------------------------------- rule texts


def _second_price_body() -> str:
    return "每人写一个出价，最高者赢，付第二高的出价，不低于保留价。"


def _step_text(levels: int) -> str:
    return "一成" if levels == 10 else f"1/{levels}"


def _english_body(mech: MechanismDef) -> str:
    theta = mech.theta
    if theta is None:
        raise ValueError("an English mechanism needs theta")
    return (
        "开拍时每人报一个退出价，价格从保留价起往上涨，涨到谁的退出价谁就退出，退出价当场公开。"
        "退出价低于 1 或低于保留价，算不参加。"
        f"在场的人超过 {theta} 个时，你的退出价会被自动执行。"
        f"剩下 2 到 {theta} 人时，每有人退出，系统会问剩下的人要不要改退出价："
        "新的退出价不能低于当前价，低于当前价就是当场退出，没有回答则保留原来的退出价。"
        "最后留下的人赢，付最后一位退出者的退出价，不低于保留价。"
        "剩下的人退出价相同时，按种子随机选一人，按这个价买下。"
    )


def _dutch_body(mech: MechanismDef) -> str:
    levels = mech.levels
    if levels is None:
        raise ValueError("a Dutch mechanism needs levels")
    return (
        f"价格从档位开始，每档降档位的{_step_text(levels)}，共 {levels} 档，保留价以下的档位跳过。"
        "开拍前你报一个出手价：价格降到出手价或更低的第一档时，你自动出手。"
        "价格高于你剩余预算的档位，你不会出手；剩余预算买不起最低一档的人不会被询问，直接算不出手。"
        "最先出手的人按那一档的价格买下，同一档几人同时出手就按种子随机选一人；"
        "到最低一档仍没人出手就流拍。"
    )


def _sealed_body(mech: MechanismDef) -> str:
    if mech.value_model == "common":
        return (
            "拍品对所有人价值相同，真值在落槌后公布。"
            "每人拿到一个鉴定价，它只是真值的估计，不一定准。"
            f"出价规则同密封二价：{_second_price_body()}"
        )
    if mech.pricing == "first":
        return "每人写一个出价，最高者赢，付自己写的价。"
    if mech.pricing == "second":
        return _second_price_body()
    return "每人写一个出价，最高者赢；所有人都付自己写的价，赢没赢都要付。这个规则没有保留价。"


def rule_text(mech: MechanismDef) -> str:
    """The rule of one format as a bidder is told, without the example."""
    if mech.clock == "ascending_button":
        body = _english_body(mech)
    elif mech.clock == "descending_levels":
        body = _dutch_body(mech)
    else:
        body = _sealed_body(mech)
    return f"{mech.title_zh}：{body}"


def rule_with_example(mech: MechanismDef) -> str:
    """The rule followed by its worked example: the paragraph a prompt shows for one format."""
    return f"{rule_text(mech)}{example_text(mech)}"


def common_rule_text() -> str:
    """The conventions shared by all formats."""
    return (
        "通用：出价 0 表示不参加。平局按种子随机。出价超过剩余预算时按剩余预算计，这是合法的。"
        "没有人出正价，或最高出价低于保留价，拍品流拍，谁都不付款；"
        "全员付费没有保留价，全员出 0 才流拍。密封类拍品落槌后公布所有人的出价。"
    )


def example_text(mech: MechanismDef, example: PaymentExample | None = None) -> str:
    """The worked example, ``例：...``, with every number taken from the settlement."""
    ex = example or payment_example(mech)
    price = _slot(ex, "price") if "price" in ex.slots else None
    if mech.clock == "descending_levels":
        return (
            f"例：档位 {_slot(ex, 'tier')}，你报出手价 {_slot(ex, 'grab_price')}，"
            f"价格依次是 {_join(_slot(ex, 'ladder_head'))}……，"
            f"没人更早出手的话，你在 {price} 这一档买下，付 {price}。"
        )
    bids = _slot(ex, "bids")
    winner_bid = _slot(ex, "winner_bid")
    if mech.clock == "ascending_button":
        return f"例：退出价 {_join(bids)}，报 {winner_bid} 的人赢，付 {price}。"
    if mech.pricing == "all_pay":
        count = _ZH_COUNT.get(len(bids), str(len(bids)))
        payments = _join(_slot(ex, "payments"))
        return f"例：出价 {_join(bids)}，出 {winner_bid} 的人赢，{count}人分别付 {payments}。"
    return f"例：出价 {_join(bids)}，出 {winner_bid} 的人赢，付 {price}。"


_CARD_SUMMARY: Final = {
    "english": "价格逐步上涨，退出价最高的人留到最后，付最后一位退出者的退出价。",
    "dutch": "价格从档位逐档下降，最先出手的人按那一档的价格买下。",
    "sealed_first": "同时写下出价，最高者赢，付自己的出价。",
    "sealed_second": "同时写下出价，最高者赢，付第二高的出价。",
    "all_pay": "同时写下出价，最高者赢，但所有人都要付自己的出价。",
    "blind_box": (
        "拍品对所有人价值相同，大家只有带噪声的鉴定价；"
        "同时写下出价，最高者赢，付第二高的出价，落槌后公布真值。"
    ),
}


def card_text(mech: MechanismDef, example: PaymentExample | None = None) -> str:
    """The audience card: the format in one line plus the generated example."""
    summary = _CARD_SUMMARY.get(mech.key)
    if summary is None:
        raise ValueError(f"no card summary for {mech.key}")
    return f"{mech.title_zh}：{summary}{example_text(mech, example)}"


_TEXTBOOK_PRIVATE: Final = "教科书模型：独立私有估值，风险中性，不计预算约束。"
_TEXTBOOK_COMMON: Final = "教科书模型：共同价值，各人只有带噪声的鉴定价，风险中性，不计预算约束。"
_UNIFORM: Final = "n 人的估值在 0 到 T 之间均匀分布时，"
_GLOSSARY_BODY: Final = {
    "english": (
        "与密封二价等价：如实退出，即退出价等于自己的估值，是占优策略，成交价是次高估值。"
        f"{_UNIFORM}期望成交价是 (n−1)/(n+1)·T。"
    ),
    "dutch": (
        "与密封一价等价，因为价格下降时没有新信息：均衡出手价取一价的均衡出价 (n−1)/n·v，"
        "再向下取到不高于它的最近一档。档位是离散的，所以期望成交价略低于连续情形。"
    ),
    "sealed_first": (
        f"{_UNIFORM}对称均衡出价是 (n−1)/n·v，也就是把估值打 (n−1)/n 折。"
        "期望成交价与二价相同，是 (n−1)/(n+1)·T。"
    ),
    "sealed_second": (
        f"如实出价是占优策略，赢家付次高出价。{_UNIFORM}期望成交价是 (n−1)/(n+1)·T。"
    ),
    "all_pay": (
        f"{_UNIFORM}对称均衡出价是 (n−1)/n · vⁿ / Tⁿ⁻¹。"
        "所有人付款的总和，期望与前几种规则相同，是 (n−1)/(n+1)·T，但输家也要付。"
    ),
    "blind_box": (
        "共同价值拍卖，会出现赢家诅咒。设鉴定价的噪声幅度为 ε，鉴定价最高者平均高估真值 "
        "ε(n−1)/(n+1)。二价下对称均衡出价是 b(s) = s − ε(n−2)/n，"
        "照报鉴定价的赢家平均多付 ε(n−3)/(n+1)。以上是内点近似。"
    ),
}


def glossary_text(mech: MechanismDef) -> str:
    """The textbook result of the format, for the audience glossary."""
    body = _GLOSSARY_BODY.get(mech.key)
    if body is None:
        raise ValueError(f"no glossary entry for {mech.key}")
    model = _TEXTBOOK_COMMON if mech.value_model == "common" else _TEXTBOOK_PRIVATE
    return f"{mech.title_zh}：{model}{body}"


# ----------------------------------------------------------------------------- by key


def _mechanisms(mechanisms: Mapping[FormatKey, MechanismDef] | None) -> Mapping[Any, MechanismDef]:
    return MECHANISMS if mechanisms is None else mechanisms


def render(key: str, mechanisms: Mapping[FormatKey, MechanismDef] | None = None) -> str:
    """The text with the given key, for example ``mechanism.english.example``.

    ``mechanisms`` defaults to the registry; pass a night's own definitions (with its theta,
    levels and reserve) to render the texts of that night.
    """
    if key == COMMON_RULE_KEY:
        return common_rule_text()
    for mech in _mechanisms(mechanisms).values():
        keys = mech.text_keys
        if key == keys.rule:
            return rule_text(mech)
        if key == keys.example:
            return example_text(mech)
        if key == keys.card:
            return card_text(mech)
        if key == keys.glossary:
            return glossary_text(mech)
    raise KeyError(key)


def all_texts(mechanisms: Mapping[FormatKey, MechanismDef] | None = None) -> dict[str, str]:
    """Every text key of the given definitions, rendered."""
    mechs = _mechanisms(mechanisms)
    keys = [COMMON_RULE_KEY]
    for mech in mechs.values():
        tk = mech.text_keys
        keys += [tk.rule, tk.example, tk.card, tk.glossary]
    return {key: render(key, mechanisms) for key in keys}


def rules_section(
    formats: Sequence[FormatKey] | None = None,
    mechanisms: Mapping[FormatKey, MechanismDef] | None = None,
) -> str:
    """The block of rules for a prompt: one line per format (rule and example), then the
    common conventions. ``formats`` defaults to all six in registry order."""
    mechs = _mechanisms(mechanisms)
    chosen = FORMAT_KEYS if formats is None else tuple(formats)
    lines = [f"- {rule_with_example(mechs[key])}" for key in chosen]
    lines.append(f"- {common_rule_text()}")
    return "\n".join(lines)
