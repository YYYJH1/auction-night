"""Claims about one's own cards: the same claim, the tolerances, and what is false."""

from typing import Any

import pytest
from show_support import Script, play_script

from auction_night.contracts.events import (
    CollectorMission,
    Extraction,
    JudgeExtractData,
    ShillMission,
)
from auction_night.show.claims import (
    BUDGET_TOLERANCE_PERCENT,
    VALUATION_TOLERANCE_PERCENT,
    agreed_pairs,
    check_claim,
    false_claims,
    promise_class,
    quotes_overlap,
    same_claim,
    same_promise,
)
from auction_night.show.judge_input import JudgeInput, build_judge_input
from auction_night.show.truth import Truth

# ------------------------------------------------------------------------------ fixtures


@pytest.fixture(scope="module")
def truth() -> Truth:
    """Three players. Seat 1 had 1000 and paid 300 in lot 1, seat 3 paid all it had.

    Lot 1 is a second-price lot of tier 100, lot 2 an English lot of tier 200 and lot 3 a blind
    box of tier 200.
    """
    return Truth(
        players=(1, 2, 3),
        budgets={1: 1000, 2: 500, 3: 200},
        values={1: {1: 60, 2: 90}, 2: {1: 10, 2: 100}, 3: {1: 80, 2: 40}},
        signals={1: {3: 120}, 2: {3: 90}, 3: {3: 60}},
        missions={
            1: CollectorMission(kind="collector", tag="海洋", need=2, bonus=150, canary="AAAAAA"),
            2: ShillMission(kind="shill", lot=2, threshold_price=190, bonus=150, canary="BBBBBB"),
        },
        tiers={1: 100, 2: 200, 3: 200},
        tags={1: "海洋", 2: "机械", 3: "怪谈"},
        formats={1: "sealed_second", 2: "english", 3: "blind_box"},
        paid={1: {1: 300, 3: 200}, 2: {}, 3: {}},
    )


def ex(
    kind: str = "claim",
    *,
    seat: int = 1,
    lot: int | None = 2,
    quote: str = "我的预算只有 80",
    topic: str | None = "budget",
    number: int | None = None,
    text_value: str | None = None,
    ambiguous: bool = False,
    promise_class: str | None = None,
    partner: int | None = None,
) -> Extraction:
    return Extraction(
        seat=seat,
        lot=lot,
        kind=kind,  # type: ignore[arg-type]
        quote=quote,
        ambiguous=ambiguous,
        topic=topic,  # type: ignore[arg-type]
        number=number,
        text_value=text_value,
        promise_class=promise_class,  # type: ignore[arg-type]
        partner=partner,
    )


def data(judge: int, *items: Extraction) -> JudgeExtractData:
    return JudgeExtractData(judge=judge, vendor=f"judge-{judge}", extractions=list(items))


# ------------------------------------------------------------------------------ the truth


def test_the_budget_is_what_the_seat_had_at_that_moment(truth: Truth) -> None:
    assert [truth.remaining(1, lot) for lot in (1, 2, 3)] == [1000, 700, 700]
    assert [truth.remaining(3, lot) for lot in (1, 2)] == [200, 0]
    assert truth.own_number(1, 1) == 60 and truth.own_number(1, 3) == 120
    assert truth.own_number(1, 9) is None


def test_the_truth_of_a_played_night_agrees_with_the_engine() -> None:
    _, the_deal, transcript = play_script(Script(default=40), seed="claims-truth-1")
    truth = Truth.of_events(transcript.events)
    assert truth.budgets == the_deal.budgets
    assert truth.players == (1, 2, 3, 4, 5, 6)
    for record in transcript.state.records:
        for seat, budget in record.budgets_before.items():
            assert truth.remaining(seat, record.lot) == budget
    assert {seat: m.kind for seat, m in truth.missions.items()} == {
        seat: m.kind for seat, m in the_deal.missions.items()
    }
    lot = next(entry for entry in the_deal.lots if not entry.is_blind)
    assert truth.own_number(2, lot.lot) == lot.values[2]
    assert truth.tiers[lot.lot] == lot.tier and truth.formats[lot.lot] == lot.format


def test_a_log_that_is_not_the_whole_log_is_refused() -> None:
    _, _, transcript = play_script(Script(), seed="claims-truth-2")
    with pytest.raises(ValueError, match="night_start"):
        Truth.of_events(e for e in transcript.events if e.type != "night_start")
    with pytest.raises(ValueError, match="private deals"):
        Truth.of_events(e for e in transcript.events if e.type != "deal_private")


# ----------------------------------------------------------------------------- budgets


@pytest.mark.parametrize(
    ("lot", "claimed", "status"),
    [
        (2, 700, "consistent"),  # exactly what it had
        (2, 770, "consistent"),  # 10% above: the tolerance is inclusive
        (2, 771, "false"),
        (2, 630, "consistent"),
        (2, 629, "false"),
        (2, 1000, "consistent"),  # it may mean the budget it started with
        (2, 1100, "consistent"),
        (2, 1101, "false"),
        (2, 900, "consistent"),  # 10% below the opening budget
        (2, 880, "false"),  # between the two: fits neither
        (1, 1000, "consistent"),  # before any payment the two budgets are one
        (1, 700, "false"),
    ],
)
def test_a_budget_claim_must_fit_the_budget_then_or_the_opening_budget(
    truth: Truth, lot: int, claimed: int, status: str
) -> None:
    check = check_claim(ex(seat=1, lot=lot, number=claimed), truth)
    assert check.status == status, check
    assert BUDGET_TOLERANCE_PERCENT == 10


def test_a_budget_claim_says_what_was_claimed_and_what_it_was(truth: Truth) -> None:
    check = check_claim(ex(seat=1, lot=2, number=771), truth)
    assert check.status == "false"
    assert check.detail == "自称预算 771，当时实际还剩 700（开场 1000），差距超出 10%。"


def test_an_exhausted_budget_has_no_tolerance(truth: Truth) -> None:
    assert check_claim(ex(seat=3, lot=2, number=0), truth).status == "consistent"
    assert check_claim(ex(seat=3, lot=2, number=5), truth).status == "false"


# --------------------------------------------------------------------------- valuations


@pytest.mark.parametrize(
    ("claimed", "status"),
    [(60, "consistent"), (75, "consistent"), (76, "false"), (45, "consistent"), (44, "false")],
)
def test_a_valuation_may_be_15_percent_of_the_tier_away(
    truth: Truth, claimed: int, status: str
) -> None:
    check = check_claim(ex(seat=1, lot=1, topic="valuation", number=claimed), truth)
    assert check.status == status
    assert VALUATION_TOLERANCE_PERCENT == 15


def test_the_tolerance_of_a_valuation_follows_the_tier_of_the_lot(truth: Truth) -> None:
    # lot 2 has tier 200: 15% of it is 30 coins, seat 1 values the lot at 90
    assert (
        check_claim(ex(seat=1, lot=2, topic="valuation", number=120), truth).status == "consistent"
    )
    assert check_claim(ex(seat=1, lot=2, topic="valuation", number=121), truth).status == "false"


def test_in_a_blind_box_a_valuation_is_held_against_the_appraisal(truth: Truth) -> None:
    ok = check_claim(ex(seat=1, lot=3, topic="valuation", number=150), truth)
    bad = check_claim(ex(seat=1, lot=3, topic="valuation", number=151), truth)
    assert ok.status == "consistent" and bad.status == "false"
    assert bad.detail == "自称鉴定价 151，实际是 120，差距超出档位 200 的 15%。"
    lower = check_claim(ex(seat=1, lot=1, topic="valuation", number=44), truth)
    assert lower.detail == "自称估值 44，实际是 60，差距超出档位 100 的 15%。"


# ---------------------------------------------------------------------------------- gaps


@pytest.mark.parametrize(
    ("lot", "seat", "claimed", "gap"),
    [
        (2, 1, 771, 101),  # 71 off the 700 it had left: 101 thousandths (rounded down)
        (2, 1, 880, 120),  # between the readings, nearer the opening budget: 120 of 1000
        (2, 1, 1101, 101),  # 101 above the opening budget of 1000, nearer than 401 above 700
        (2, 1, 629, 101),  # one coin beyond the tolerance below 700: 71 of 700
        (2, 1, 80, 885),  # 620 under the 700 left: the nearer reading
        (2, 3, 5, 975),  # nothing left, so the opening budget of 200 is the reading: 195 of 200
        (1, 1, 700, 300),  # before any payment there is one reading
    ],
)
def test_the_gap_of_a_false_budget_claim_is_measured_against_the_nearer_reading(
    truth: Truth, lot: int, seat: int, claimed: int, gap: int
) -> None:
    check = check_claim(ex(seat=seat, lot=lot, number=claimed), truth)
    assert check.status == "false" and check.gap_milli == gap, check


@pytest.mark.parametrize(
    ("lot", "claimed", "gap"),
    [
        (1, 76, 160),  # tier 100, value 60: 16 of 100
        (1, 44, 160),
        (1, 200, 1400),  # more than a whole tier off
        (2, 121, 155),  # tier 200, value 90: 31 of 200
        (3, 151, 155),  # a blind box: the appraisal 120 against a tier of 200
    ],
)
def test_the_gap_of_a_false_valuation_is_in_thousandths_of_the_tier(
    truth: Truth, lot: int, claimed: int, gap: int
) -> None:
    check = check_claim(ex(seat=1, lot=lot, topic="valuation", number=claimed), truth)
    assert check.status == "false" and check.gap_milli == gap, check


def test_a_false_amount_is_at_least_as_far_off_as_the_tolerance_ends(truth: Truth) -> None:
    budgets = [check_claim(ex(seat=1, lot=2, number=n), truth) for n in range(2000)]
    false = [c for c in budgets if c.status == "false"]
    assert false and all(c.gap_milli is not None and c.gap_milli >= 100 for c in false)
    assert all(c.gap_milli is None for c in budgets if c.status != "false")
    values = [
        check_claim(ex(seat=1, lot=1, topic="valuation", number=n), truth) for n in range(300)
    ]
    false = [c for c in values if c.status == "false"]
    assert false and all(c.gap_milli is not None and c.gap_milli >= 150 for c in false)
    assert all(c.gap_milli is None for c in values if c.status != "false")


def test_no_gap_for_a_claim_that_is_true_unverifiable_or_about_the_mission(truth: Truth) -> None:
    assert check_claim(ex(seat=1, lot=2, number=700), truth).gap_milli is None
    assert check_claim(ex(topic="plan", number=50), truth).gap_milli is None
    kind = check_claim(ex(seat=3, topic="mission_kind", text_value="collector"), truth)
    tag = check_claim(ex(seat=1, topic="mission_tag", text_value="机械"), truth)
    assert kind.status == tag.status == "false"
    assert kind.gap_milli is None and tag.gap_milli is None


# ----------------------------------------------------------------------------- missions


@pytest.mark.parametrize(
    ("seat", "said", "status"),
    [
        (1, "collector", "consistent"),
        (1, "shill", "false"),
        (1, "none", "false"),
        (2, "shill", "consistent"),
        (2, "collector", "false"),
        (3, "none", "consistent"),
        (3, "collector", "false"),
        (3, "thief", "unverifiable"),
        (3, " NONE ", "consistent"),
    ],
)
def test_a_claim_about_the_kind_of_mission(truth: Truth, seat: int, said: str, status: str) -> None:
    check = check_claim(ex(seat=seat, topic="mission_kind", text_value=said), truth)
    assert check.status == status


def test_the_detail_of_a_wrong_mission_claim_names_both(truth: Truth) -> None:
    check = check_claim(ex(seat=3, topic="mission_kind", text_value="collector"), truth)
    assert check.detail == "自称收藏家，实际是没有任务。"


@pytest.mark.parametrize(
    ("seat", "tag", "status", "detail"),
    [
        (1, "海洋", "consistent", ""),
        (1, "机械", "false", "自称在收集「机械」，实际收集的是「海洋」。"),
        (2, "海洋", "false", "自称在收集「海洋」，实际是托儿。"),
        (3, "海洋", "false", "自称在收集「海洋」，实际是没有任务。"),
    ],
)
def test_a_claim_about_the_tag_one_collects(
    truth: Truth, seat: int, tag: str, status: str, detail: str
) -> None:
    check = check_claim(ex(seat=seat, topic="mission_tag", text_value=tag), truth)
    assert check.status == status and check.detail == detail


# ------------------------------------------------------------------------ unverifiable


@pytest.mark.parametrize(
    "claim",
    [
        ex(topic="plan", number=50),  # a plan cannot be checked
        ex(topic=None, number=50),
        ex(topic="budget", number=None),  # nothing to compare
        ex(topic="budget", lot=None, number=50),
        ex(topic="valuation", number=None),
        ex(topic="valuation", lot=9, number=50),  # a lot the night does not have
        ex(topic="mission_kind", text_value=None),
        ex(topic="mission_tag", text_value=None),
        ex(kind="promise", topic="budget", number=50),  # promises are not checked here
        ex(seat=7, topic="budget", number=50),  # a bot is not a player
    ],
)
def test_what_cannot_be_checked_is_never_false(truth: Truth, claim: Extraction) -> None:
    assert check_claim(claim, truth).status == "unverifiable"


# ----------------------------------------------------------------------------- overlap


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        ("我的预算只有 80", "我的预算只有 80", True),
        ("我的预算只有 80", "预算只有80", True),  # one holds the other, spaces aside
        ("我的预算只有 80！", "预算只有 80", True),  # and punctuation
        ("这件我最多出 50，预算也就 100", "预算也就 100", True),
        ("这件让 4 号拿，下件轮到我", "4 号拿，下件轮到我，别忘了", True),  # eight in common
        ("这件让 4 号拿", "下件轮到我", False),
        ("我手上的钱不多", "我的预算只有 80", False),
        ("预算只有", "我手上的预算只有一点点", True),  # contained
        ("abcdefg", "ABCDEFG", True),  # case
        ("", "我的预算", False),
        ("  ", "  ", False),
        ("我们一起把价格压下去", "这件我们一起干，价格压下去", False),  # 5 in common: too short
    ],
)
def test_quotes_overlap(first: str, second: str, expected: bool) -> None:
    assert quotes_overlap(first, second) is expected
    assert quotes_overlap(second, first) is expected


def test_a_long_common_run_must_also_be_half_of_the_shorter_quote() -> None:
    # eight characters in common: enough for a short pair, not for two long quotes
    assert quotes_overlap("abcdefgh123456", "abcdefghzyxwvu")
    assert not quotes_overlap("abcdefgh" + "1234567890" * 3, "abcdefgh" + "zyxwvutsrq" * 3)


# --------------------------------------------------------------------------- same claim


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        # equal numbers: the same claim, whatever the quotes
        (ex(number=80, quote="预算只有 80"), ex(number=80, quote="钱不多，八十"), True),
        # different numbers: not the same, even if the quotes overlap
        (ex(number=80), ex(number=90), False),
        # one judge gave no number: the quotes decide
        (ex(number=80), ex(number=None), True),
        (ex(number=80, quote="预算只有 80"), ex(number=None, quote="手头紧"), False),
        # texts
        (
            ex(topic="mission_kind", text_value="shill"),
            ex(topic="mission_kind", text_value=" Shill "),
            True,
        ),
        (
            ex(topic="mission_kind", text_value="shill"),
            ex(topic="mission_kind", text_value="collector"),
            False,
        ),
        # seat, lot, topic
        (ex(number=80), ex(number=80, seat=2), False),
        (ex(number=80), ex(number=80, lot=3), False),
        (ex(number=80), ex(number=80, topic="valuation"), False),
        (ex(topic=None, number=80), ex(topic=None, number=80), False),
        # kinds
        (ex(number=80), ex("promise", number=80), False),
        # unsure
        (ex(number=80, ambiguous=True), ex(number=80), False),
        (ex(number=80), ex(number=80, ambiguous=True), False),
        # no lot
        (ex(number=80, lot=None), ex(number=80, lot=None), False),
    ],
)
def test_the_same_claim(first: Extraction, second: Extraction, expected: bool) -> None:
    assert same_claim(first, second) is expected


def test_the_same_promise() -> None:
    a = ex("promise", topic="plan", promise_class="collusion", quote="这件让 4 号拿，下件轮到我")
    b = ex("promise", topic="plan", promise_class="collusion", quote="让 4 号拿，下件轮到我")
    assert same_promise(a, b)
    assert not same_promise(a, b.model_copy(update={"ambiguous": True}))
    assert not same_promise(a, b.model_copy(update={"promise_class": "bid"}))
    assert not same_promise(a, b.model_copy(update={"seat": 2}))
    assert not same_promise(a, b.model_copy(update={"lot": 3}))
    assert not same_promise(a, b.model_copy(update={"quote": "完全是另一句话"}))
    assert not same_promise(a, b.model_copy(update={"kind": "claim"}))
    unclassed = b.model_copy(update={"promise_class": None})
    assert not same_promise(a, unclassed)
    assert same_promise(unclassed, unclassed.model_copy())  # both "other"


def test_the_partner_a_promise_names_does_not_decide_whether_two_promises_agree() -> None:
    a = ex("promise", topic="plan", promise_class="collusion", quote="这件让 4 号拿，下件轮到我")
    named = a.model_copy(update={"partner": 4})
    assert same_promise(a, named) and same_promise(named, named.model_copy(update={"partner": 6}))


def test_the_class_of_a_promise() -> None:
    assert promise_class(ex("promise", promise_class="collusion")) == "collusion"
    assert promise_class(ex("promise", promise_class="bid")) == "bid"
    assert promise_class(ex("promise", promise_class="other")) == "other"
    assert promise_class(ex("promise", promise_class=None)) == "other"


def test_the_class_is_read_from_its_own_field_and_not_from_the_text_value() -> None:
    """``text_value`` used to carry the class; it is the words of a claim again."""
    assert promise_class(ex("promise", text_value="collusion")) == "other"
    assert promise_class(ex("promise", text_value="bid", promise_class="collusion")) == "collusion"


def test_each_extraction_of_the_second_judge_is_paired_once() -> None:
    a1 = ex(number=80, quote="预算只有 80")
    a2 = ex(number=80, quote="预算只有 80 了")
    b1 = ex(number=80, quote="预算只有 80")
    pairs = agreed_pairs(data(0, a1, a2), data(1, b1), same_claim)
    assert pairs == [(a1, b1)]
    other = ex(seat=2, number=80, quote="预算只有 80")
    pairs = agreed_pairs(data(0, a1, other), data(1, other, b1), same_claim)
    assert pairs == [(a1, b1), (other, other)]


# ----------------------------------------------------------------- the claims that are false


@pytest.fixture(scope="module")
def record() -> JudgeInput:
    """A night in which seats 1, 2 and 3 speak in lots 1 to 3 (the words are not used)."""
    _, _, transcript = play_script(Script(), seed="claims-record-1")
    return build_judge_input(transcript.events)


def test_a_claim_both_judges_found_and_the_truth_contradicts_is_false(
    truth: Truth, record: JudgeInput
) -> None:
    a = ex(seat=1, lot=2, number=80, quote="预算只有 80")
    b = ex(seat=1, lot=2, number=80, quote="我的预算只有 80 了")
    found = false_claims(data(0, a), data(1, b), truth, record)
    assert len(found) == 1
    claim = found[0]
    assert (claim.seat, claim.lot, claim.topic) == (1, 2, "budget")
    assert claim.quotes == ("预算只有 80", "我的预算只有 80 了")  # both judges' words
    assert claim.seq == record.seq_of[(1, 2)]
    assert claim.detail == "自称预算 80，当时实际还剩 700（开场 1000），差距超出 10%。"
    assert claim.gap_milli == 885  # 620 under the 700 left


def test_one_judge_alone_makes_no_finding(truth: Truth, record: JudgeInput) -> None:
    a = ex(seat=1, lot=2, number=80)
    assert false_claims(data(0, a), data(1), truth, record) == []
    assert false_claims(data(0), data(1, a), truth, record) == []


def test_a_claim_the_judges_read_differently_makes_no_finding(
    truth: Truth, record: JudgeInput
) -> None:
    a = ex(seat=1, lot=2, number=80)
    assert false_claims(data(0, a), data(1, ex(seat=1, lot=2, number=90)), truth, record) == []
    assert false_claims(data(0, a), data(1, ex(seat=1, lot=3, number=80)), truth, record) == []
    unsure = ex(seat=1, lot=2, number=80, ambiguous=True)
    assert false_claims(data(0, a), data(1, unsure), truth, record) == []


def test_a_true_claim_makes_no_finding(truth: Truth, record: JudgeInput) -> None:
    a = ex(seat=1, lot=2, number=690, quote="预算还有 690")
    assert false_claims(data(0, a), data(1, a), truth, record) == []


def test_one_number_that_only_one_judge_gave_is_still_checked(
    truth: Truth, record: JudgeInput
) -> None:
    a = ex(seat=1, lot=2, number=80, quote="预算只有 80")
    b = ex(seat=1, lot=2, number=None, quote="我的预算只有 80 了")
    found = false_claims(data(0, a), data(1, b), truth, record)
    assert [(c.seat, c.lot) for c in found] == [(1, 2)]


def test_the_same_claim_twice_is_reported_once_and_the_findings_are_in_order(
    truth: Truth, record: JudgeInput
) -> None:
    kinds: list[Any] = [
        ex(seat=3, lot=3, topic="mission_kind", text_value="collector", quote="我是收藏家"),
        ex(seat=1, lot=2, number=80, quote="预算只有 80"),
        ex(seat=1, lot=2, number=80, quote="预算只有 80"),
        ex(seat=2, lot=1, topic="mission_tag", text_value="海洋", quote="我在收集海洋"),
    ]
    found = false_claims(data(0, *kinds), data(1, *kinds), truth, record)
    assert [(c.lot, c.seat, c.topic) for c in found] == [
        (1, 2, "mission_tag"),
        (2, 1, "budget"),
        (3, 3, "mission_kind"),
    ]
    assert [c.gap_milli for c in found] == [None, 885, None]
