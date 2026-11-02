"""The text of a lot card: fixed wording, every number read from the data."""

from typing import Any

import pytest
from show_support import make_lot, seat_line

from auction_night.contracts.events import DutchResultData, EnglishDropoutData, Pledge
from auction_night.contracts.replay import PledgeRecord
from auction_night.show.cards import (
    ActionLine,
    Candidate,
    GuessLine,
    MissionLine,
    PriorLine,
)
from auction_night.show.numbers_check import unlisted_numbers
from auction_night.show.render import (
    lot_headline,
    milli_text,
    render_lot,
    seats_text,
    signed,
)


def lines_of(lot: Any) -> list[str]:
    return render_lot(lot).split("\n")


def seat_text(lot: Any, seat: int) -> str:
    return next(line for line in lines_of(lot) if line.startswith(f"- {seat} 号「"))


# -------------------------------------------------------------------- small formatters


@pytest.mark.parametrize(
    ("thousandths", "text"),
    [
        (0, "0.000"),
        (7, "0.007"),
        (874, "0.874"),
        (1000, "1.000"),
        (71295, "71.295"),
        (-5, "-0.005"),
    ],
)
def test_milli_text(thousandths: int, text: str) -> None:
    assert milli_text(thousandths) == text


def test_signed_and_seat_lists() -> None:
    assert (signed(12), signed(-55), signed(0)) == ("+12", "-55", "0")
    assert seats_text([1, 3, 8]) == "1、3、8 号"
    assert seats_text([4]) == "4 号"


# ------------------------------------------------------------------------ the seat line


def test_a_seat_that_bid_shows_its_cards_and_the_theory_columns() -> None:
    line = seat_line(
        3,
        value=120,
        played=95,
        ref=100,
        excess=-5,
        regret=12,
        said="我先看看",
        plan_stars=2,
        plan_budget=60,
        alpha_milli=874,
        won=True,
        paid=95,
    )
    lot = make_lot(seats=[line], winner=3, price=95)
    text = seat_text(lot, 3)
    for part in (
        "预算余 300",
        "估值 120",
        "计划 2 星（打算最多花 60）",
        "说「我先看看」",
        "出价 95",
        "付 95（付后预算余 205）",
        "拍得",
        "教科书参考 100",
        "超额收益 -5",
        "后悔值 12",
        "α 0.874",
    ):
        assert part in text
    assert "｜".join(["- 3 号「选手3」", "预算余 300"]) in text


def test_the_word_for_the_number_follows_the_format() -> None:
    assert "出价 5" in seat_text(make_lot(fmt="sealed_second", seats=[seat_line(1, played=5)]), 1)
    assert "出手价 5" in seat_text(make_lot(fmt="dutch", seats=[seat_line(1, played=5)]), 1)
    assert "退出价 5" in seat_text(make_lot(fmt="english", seats=[seat_line(1, played=5)]), 1)


def test_a_blind_box_seat_shows_the_appraisal() -> None:
    lot = make_lot(fmt="blind_box", seats=[seat_line(1, played=60, signal=70)])
    assert "鉴定价 70" in seat_text(lot, 1) and "估值" not in seat_text(lot, 1)


def test_a_bot_is_marked_and_a_seat_without_a_plan_has_none() -> None:
    text = seat_text(make_lot(seats=[seat_line(7, role="bot", played=1)]), 7)
    assert "「选手7」（机器人）" in text and "计划" not in text


def test_an_abstention_is_not_a_zero_bid() -> None:
    action = ActionLine(phase="sealed", status="abstain", reason="timeout")
    line = seat_line(1, abstained=True, actions=(action,))
    text = seat_text(make_lot(seats=[line]), 1)
    assert "弃权（超时）" in text and "出价" not in text


@pytest.mark.parametrize(
    ("reason", "word"),
    [
        ("missing", "没有回答"),
        ("parse_failed", "回答无法解析"),
        ("refusal", "拒答"),
        ("budget_aborted", "费用上限"),
        ("not_asked", "没有被询问"),
    ],
)
def test_every_abstention_reason_has_words(reason: str, word: str) -> None:
    action = ActionLine(phase="sealed", status="abstain", reason=reason)  # type: ignore[arg-type]
    text = seat_text(make_lot(seats=[seat_line(1, abstained=True, actions=(action,))]), 1)
    assert f"弃权（{word}）" in text


def test_a_clamped_bid_shows_the_raw_number() -> None:
    action = ActionLine(phase="sealed", status="clamped", value=40, raw_value=90)
    text = seat_text(make_lot(seats=[seat_line(1, played=40, actions=(action,))]), 1)
    assert "出价 40（原报 90，按预算夹取）" in text


def test_a_kept_exit_price_and_a_revision_are_told() -> None:
    kept = (
        ActionLine(phase="english_init", status="ok", value=50),
        ActionLine(phase="english_revise", status="kept_previous", value=50),
    )
    changed = (
        ActionLine(phase="english_init", status="ok", value=50),
        ActionLine(phase="english_revise", status="ok", value=70),
    )
    lot = make_lot(
        fmt="english",
        seats=[seat_line(1, played=50, actions=kept), seat_line(2, played=70, actions=changed)],
    )
    assert "退出价 50（改价失败，保留原价）" in seat_text(lot, 1)
    assert "退出价 70（初报 50，后来改价）" in seat_text(lot, 2)


def test_a_seat_that_was_out_of_budget_says_so() -> None:
    lot = make_lot(seats=[seat_line(1, played=5), seat_line(2, asked=False)])
    assert "预算用尽，没有出价资格" in seat_text(lot, 2)
    assert "预算用尽没有出价资格：2 号" in render_lot(lot)


def test_an_english_non_participant_is_told_with_what_it_declared() -> None:
    left = EnglishDropoutData(seat=2, price=0, remaining=[1], participated=False)
    action = ActionLine(phase="english_init", status="ok", value=0)
    lot = make_lot(
        fmt="english",
        seats=[seat_line(1, played=60), seat_line(2, played=0, actions=(action,))],
        dropouts=(left,),
    )
    assert "未参加" in seat_text(lot, 2)
    assert "2 号未参加" in render_lot(lot)


# ----------------------------------------------------------------------- the result


def test_a_sold_lot_and_an_unsold_lot() -> None:
    sold = make_lot(seats=[seat_line(2, won=True, paid=77)], winner=2, price=77)
    assert "结果：2 号「选手2」拍得，成交价 77。" in render_lot(sold)
    unsold = make_lot(seats=[seat_line(1)], unsold=True)
    assert "结果：流拍，谁都没付款。" in render_lot(unsold)


def test_an_all_pay_result_says_everyone_who_bid_pays() -> None:
    lot = make_lot(
        fmt="all_pay",
        seats=[seat_line(1, won=True, paid=80), seat_line(2, paid=30)],
        winner=1,
        price=80,
        payments={1: 80, 2: 30},
    )
    assert "出价最高拍得，付 80；出过价的人都要付自己的出价" in render_lot(lot)


def test_a_tie_and_a_new_drop_out_are_told() -> None:
    lot = make_lot(
        seats=[seat_line(1, won=True, paid=50), seat_line(2)],
        winner=1,
        price=50,
        tied=(1, 2),
        newly_out=(1,),
    )
    text = render_lot(lot)
    assert "平局：1、2 号同价，按抽签键决出 1 号。" in text and "预算用尽出局：1 号。" in text


@pytest.mark.parametrize(
    ("price", "phrase"),
    [(250, "多付 50"), (150, "少付 50"), (200, "正好等于真值")],
)
def test_the_blind_box_line_says_over_under_or_even(price: int, phrase: str) -> None:
    lot = make_lot(
        fmt="blind_box",
        seats=[seat_line(1, won=True, paid=price)],
        winner=1,
        price=price,
        true_value=200,
        curse_gap=price - 200,
        top_signal_error=20,
        prior=PriorLine(low=100, high=300, noise=40),
    )
    text = render_lot(lot)
    assert phrase in text and "盲盒真值 200（先验 100 到 300，鉴定噪声 ±40）" in text
    assert "最高鉴定价比真值高 20" in text


def test_the_top_signal_can_be_below_the_true_value() -> None:
    lot = make_lot(
        fmt="blind_box",
        seats=[seat_line(1)],
        true_value=200,
        top_signal_error=-15,
        prior=PriorLine(low=100, high=300, noise=40),
        unsold=True,
    )
    assert "最高鉴定价比真值低 15" in render_lot(lot)


def test_the_allocation_line_is_per_lot_and_says_who_had_the_top_value() -> None:
    lot = make_lot(
        seats=[seat_line(1, won=True, paid=10), seat_line(2)],
        winner=1,
        price=10,
        to_highest_value=False,
        top_value=150,
        top_value_seats=(2,),
    )
    assert "估值最高的是 2 号（估值 150），这件没有给估值最高的人。" in render_lot(lot)


def test_the_english_clock_line() -> None:
    drops = (
        EnglishDropoutData(seat=3, price=20, remaining=[1, 2]),
        EnglishDropoutData(seat=2, price=40, remaining=[1]),
    )
    lot = make_lot(
        fmt="english",
        seats=[seat_line(1, won=True, played=90, paid=40), seat_line(2), seat_line(3)],
        winner=1,
        price=40,
        winner_exit=90,
        dropouts=drops,
    )
    assert "退出顺序：3 号@20，2 号@40；留到最后的 1 号退出价 90，付 40。" in render_lot(lot)


def test_the_dutch_clock_line() -> None:
    sold = DutchResultData(ladder=[200, 180, 160], level=2, price=180, buyer=4)
    lot = make_lot(fmt="dutch", seats=[seat_line(4)], winner=4, price=180, dutch=sold)
    assert "价格档：200、180、160；降到第 2 档（价格 180）时 4 号出手买下。" in render_lot(lot)
    none = DutchResultData(ladder=[200, 180], unsold=True)
    unsold = make_lot(fmt="dutch", seats=[seat_line(4)], unsold=True, dutch=none)
    assert "没有人出手，流拍。" in render_lot(unsold)


# ------------------------------------------------------- pledges, missions, candidates


def record(outcome: str, lot: int = 1) -> PledgeRecord:
    return PledgeRecord(
        seq=9,
        lot=lot,
        seat=1,
        pledge=Pledge(kind="max_bid", lot=3, amount=40),
        outcome=outcome,  # type: ignore[arg-type]
        detail="承诺第 3 件最多出 40，实际出价 41",
    )


def test_pledges_made_and_due_are_listed_apart() -> None:
    made = make_lot(lot=1, seats=[seat_line(1)], pledges_made=(record("violated"),))
    assert "誓约（本件喊话）：1 号「选手1」承诺第 3 件最多出 40。" in render_lot(made)
    assert "违约" not in render_lot(made)  # the end is not given away in the lot of the promise
    due = make_lot(lot=3, seats=[seat_line(1)], pledges_due=(record("violated"),))
    assert "誓约（涉及本件）：1 号「选手1」在第 1 件承诺第 3 件最多出 40，违约：" in render_lot(due)
    assert "实际出价 41" in render_lot(due)


@pytest.mark.parametrize(("outcome", "word"), [("kept", "遵守"), ("void", "无法检验")])
def test_the_other_pledge_outcomes_have_words(outcome: str, word: str) -> None:
    due = make_lot(lot=3, seats=[seat_line(1)], pledges_due=(record(outcome),))
    assert f"，{word}：" in render_lot(due)


def mission_text(mission: MissionLine) -> str:
    lot = make_lot(seats=[seat_line(1), seat_line(2)], missions=(mission,))
    return next(line for line in lines_of(lot) if line.startswith("任务："))


def test_collector_lines() -> None:
    base = {"seat": 1, "kind": "collector", "tag": "海洋", "need": 2, "tag_lots_left": 2}
    advanced = MissionLine(outcome="advanced", won_so_far=1, **base)  # type: ignore[arg-type]
    done = MissionLine(outcome="done", won_so_far=2, **base)  # type: ignore[arg-type]
    missed = MissionLine(outcome="missed", won_so_far=0, **base)  # type: ignore[arg-type]
    assert "收藏家 1 号「选手1」（海洋，要 2 件，已有 1 件），本件到手，之后同标签还有 2 件。" in (
        mission_text(advanced)
    )
    assert "本件到手，任务完成。" in mission_text(done)
    assert "本件没拿到，之后同标签还有 2 件。" in mission_text(missed)


@pytest.mark.parametrize(
    ("outcome", "phrase"),
    [
        ("stuck", "自己买下，任务失败"),
        ("reached", "买家 2 号，任务完成"),
        ("short", "没到门槛，任务失败"),
        ("unsold", "这件流拍，任务失败"),
    ],
)
def test_shill_lines(outcome: str, phrase: str) -> None:
    mission = MissionLine(
        seat=1,
        kind="shill",
        outcome=outcome,  # type: ignore[arg-type]
        target_lot=1,
        threshold=190,
        price=None if outcome == "unsold" else 170,
        buyer=2,
    )
    text = mission_text(mission)
    assert "托儿 1 号「选手1」（目标第 1 件，门槛 190）" in text and phrase in text


def test_candidates_are_listed_with_their_category_names() -> None:
    candidate = Candidate(
        category="winners_curse", lot=1, seats=(1,), facts={"gap": 5}, reason="多付 5。"
    )
    lot = make_lot(seats=[seat_line(1)], candidates=(candidate,))
    assert "高光候选（赢家诅咒）：多付 5。" in render_lot(lot)


def test_mind_reading_lines() -> None:
    line = seat_line(2, guess=GuessLine(target=1, guess=100, truth=250, abs_error=150))
    lot = make_lot(seats=[seat_line(1), line])
    assert "读心：2 号猜 1 号预算余 100，实际 250，差 150。" in render_lot(lot)


def test_the_expected_revenue_is_a_per_lot_decimal() -> None:
    lot = make_lot(seats=[seat_line(1), seat_line(2)], expected_revenue_milli=71295)
    assert "教科书：在场 2 人，本件期望成交额 71.295。" in render_lot(lot)


# -------------------------------------------------------------------------- headline


def test_the_headline_of_a_sold_lot_and_of_an_unsold_one() -> None:
    sold = make_lot(lot=4, seats=[seat_line(2, won=True, paid=77)], winner=2, price=77)
    assert lot_headline(sold) == "第 4 件 拍品4（密封一价）：2 号选手2拍得，成交价 77。"
    unsold = make_lot(lot=5, seats=[seat_line(1)], unsold=True)
    assert lot_headline(unsold) == "第 5 件 拍品5（密封一价）：流拍，谁都没付款。"


def test_the_headline_adds_the_true_value_and_the_first_candidate() -> None:
    candidate = Candidate(category="winners_curse", lot=1, seats=(1,), facts={}, reason="多付 50。")
    lot = make_lot(
        fmt="blind_box",
        seats=[seat_line(1, won=True, paid=250)],
        winner=1,
        price=250,
        true_value=200,
        candidates=(candidate, candidate),
    )
    assert lot_headline(lot).endswith("成交价 250。真值 200。多付 50。")
    assert lot_headline(lot).count("多付 50") == 1


def test_a_headline_never_has_a_number_its_card_lacks() -> None:
    from auction_night.show.numbers_check import number_keys

    lot = make_lot(
        fmt="blind_box",
        seats=[seat_line(1, won=True, paid=250)],
        winner=1,
        price=250,
        true_value=200,
        curse_gap=50,
        prior=PriorLine(low=100, high=300, noise=40),
    )
    assert unlisted_numbers(lot_headline(lot), number_keys(render_lot(lot))) == []
