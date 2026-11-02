"""The judges' own score: precision and recall on the missions, agreement, and the season sums."""

import json

import pytest
from judge_support import ScriptedJudge, answer, guess, mock_night

from auction_night.contracts.events import (
    CollectorMission,
    JudgeExtractData,
    MissionGuess,
    ShillMission,
)
from auction_night.show.judge_board import (
    Agreement,
    JudgeScore,
    NightBoard,
    accuracy_rows,
    agreement,
    guess_is_correct,
    night_board,
    ratio_milli,
    score_judge,
    season_board,
)
from auction_night.show.judge_input import build_judge_input
from auction_night.show.judges import MockJudge, run_judges
from auction_night.show.truth import Truth
from auction_night.show.verdicts import run_judging


def collector(tag: str) -> CollectorMission:
    return CollectorMission(kind="collector", tag=tag, need=2, bonus=150, canary="AAAAAA")


def shill(lot: int) -> ShillMission:
    return ShillMission(kind="shill", lot=lot, threshold_price=190, bonus=150, canary="BBBBBB")


@pytest.fixture(scope="module")
def truth() -> Truth:
    """Four missions among six players: seats 1 and 3 collect, seats 2 and 4 shill."""
    return Truth(
        players=(1, 2, 3, 4, 5, 6),
        budgets={},
        values={},
        signals={},
        missions={1: collector("海洋"), 2: shill(2), 3: collector("机械"), 4: shill(5)},
        tiers={},
        tags={},
        formats={},
        paid={},
    )


def judge(index: int, *guesses: MissionGuess) -> JudgeExtractData:
    return JudgeExtractData(judge=index, vendor=f"judge-{index}", mission_guesses=list(guesses))


def g(seat: int, kind: str, tag: str | None = None, lot: int | None = None) -> MissionGuess:
    return MissionGuess(seat=seat, kind=kind, tag=tag, lot=lot)  # type: ignore[arg-type]


FIRST = (
    g(1, "collector", tag="海洋"),  # right
    g(2, "shill", lot=2),  # right
    g(3, "collector", tag="海洋"),  # the right kind, the wrong tag
    g(5, "shill", lot=1),  # seat 5 holds no mission
)
SECOND = (g(1, "collector", tag="海洋"), g(4, "shill", lot=5))


# ------------------------------------------------------------------------------ rates


@pytest.mark.parametrize(
    ("part", "whole", "milli"),
    [(1, 3, 333), (2, 3, 667), (1, 2, 500), (0, 5, 0), (5, 5, 1000), (1, 8, 125), (1, 16, 63)],
)
def test_a_rate_is_whole_thousandths_rounded_half_up(part: int, whole: int, milli: int) -> None:
    assert ratio_milli(part, whole) == milli


def test_a_rate_without_a_denominator_is_none() -> None:
    assert ratio_milli(0, 0) is None and ratio_milli(3, 0) is None


# ----------------------------------------------------------------------------- one guess


def test_a_guess_is_right_when_the_kind_and_the_tag_or_lot_match(truth: Truth) -> None:
    assert guess_is_correct(g(1, "collector", tag="海洋"), truth.mission_of(1))
    assert not guess_is_correct(g(1, "collector", tag="机械"), truth.mission_of(1))
    assert not guess_is_correct(g(1, "shill", lot=2), truth.mission_of(1))  # the wrong kind
    assert guess_is_correct(g(2, "shill", lot=2), truth.mission_of(2))
    assert not guess_is_correct(g(2, "shill", lot=3), truth.mission_of(2))
    assert not guess_is_correct(g(2, "collector", tag="海洋"), truth.mission_of(2))
    assert not guess_is_correct(g(5, "shill", lot=2), truth.mission_of(5))  # holds no mission


# ----------------------------------------------------------------------------- one night


def test_precision_is_right_over_guessed_and_recall_is_right_over_held(truth: Truth) -> None:
    score = score_judge(judge(0, *FIRST), truth)
    assert (score.guesses, score.correct, score.kind_correct, score.targets) == (4, 2, 3, 4)
    assert score.precision_milli == 500 and score.recall_milli == 500
    other = score_judge(judge(1, *SECOND), truth)
    assert (other.guesses, other.correct, other.targets) == (2, 2, 4)
    assert other.precision_milli == 1000 and other.recall_milli == 500


def test_a_judge_that_guessed_nothing_has_no_precision_but_a_recall_of_zero(truth: Truth) -> None:
    score = score_judge(judge(0), truth)
    assert score.guesses == 0 and score.precision_milli is None and score.recall_milli == 0


def test_the_score_is_the_replay_row(truth: Truth) -> None:
    row = score_judge(judge(0, *FIRST), truth).accuracy()
    assert row.model_dump() == {
        "judge": 0,
        "vendor": "judge-0",
        "guesses": 4,
        "correct": 2,
        "targets": 4,
    }


def test_agreement_is_the_same_guess_among_the_seats_that_either_judge_guessed() -> None:
    both = agreement(judge(0, *FIRST), judge(1, *SECOND))
    # seats 1, 2, 3, 4 and 5 were guessed by someone; only seat 1 got the same guess twice
    assert both == Agreement(compared=5, agreed=1) and both.rate_milli == 200
    same = agreement(judge(0, *SECOND), judge(1, *SECOND))
    assert (same.compared, same.agreed, same.rate_milli) == (2, 2, 1000)
    assert agreement(judge(0), judge(1)) == Agreement(0, 0)
    assert agreement(judge(0), judge(1)).rate_milli is None


def test_a_different_tag_for_the_same_seat_is_a_disagreement() -> None:
    a = judge(0, g(1, "collector", tag="海洋"))
    b = judge(1, g(1, "collector", tag="机械"))
    assert agreement(a, b) == Agreement(1, 0)


def test_the_night_board_has_a_row_per_present_judge_and_the_agreement(truth: Truth) -> None:
    judges = [
        ScriptedJudge(0, "judge-0", [answer(guesses=[])]),
        ScriptedJudge(1, "judge-1", [None]),
    ]
    record = build_judge_input(mock_night("board-1").events)
    run = run_judges(record, judges)
    board = night_board(run, truth)
    assert [s.judge for s in board.scores] == [0] and board.agreement is None
    assert board.absent == (1,)
    assert [r.judge for r in accuracy_rows(run, truth)] == [0]


# ------------------------------------------------------------------------------ a season


def board(*scores: JudgeScore, agreed: tuple[int, int] | None = None) -> NightBoard:
    return NightBoard(
        scores=tuple(scores), agreement=None if agreed is None else Agreement(*agreed[::-1])
    )


def score(judge: int, guesses: int, correct: int, targets: int, vendor: str = "") -> JudgeScore:
    return JudgeScore(judge, vendor or f"judge-{judge}", guesses, correct, correct, targets)


def test_a_season_adds_the_counts_and_computes_each_rate_once() -> None:
    # night 1: 1 of 1 right (precision 1000); night 2: 0 of 9 right (precision 0)
    nights = [
        board(score(0, 1, 1, 6), score(1, 3, 2, 6), agreed=(1, 1)),
        board(score(0, 9, 0, 6), score(1, 3, 1, 6), agreed=(1, 4)),
    ]
    season = season_board(nights)
    one, two = season.rows
    assert (one.guesses, one.correct, one.targets, one.nights) == (10, 1, 12, 2)
    # the pooled rate, not the mean of 1000 and 0 (which would be 500)
    assert one.precision_milli == 100 and one.recall_milli == 83
    assert (two.guesses, two.correct) == (6, 3) and two.precision_milli == 500
    assert (season.compared, season.agreed, season.nights) == (5, 2, 2)
    assert season.agreement_milli == 400  # not the mean of 1000 and 250


def test_a_judge_that_was_absent_one_night_counts_only_the_nights_it_delivered() -> None:
    nights = [
        board(score(0, 2, 1, 6), score(1, 2, 2, 6), agreed=(2, 2)),
        NightBoard(scores=(score(0, 4, 2, 6),), agreement=None, absent=(1,)),
    ]
    season = season_board(nights)
    zero, one = season.rows
    assert (zero.nights, zero.guesses, zero.correct, zero.targets) == (2, 6, 3, 12)
    assert (one.nights, one.guesses, one.correct, one.targets) == (1, 2, 2, 6)
    assert season.nights == 2 and (season.compared, season.agreed) == (2, 2)


def test_a_judge_is_told_apart_by_its_number_and_vendor() -> None:
    nights = [board(score(0, 1, 1, 6, "a")), board(score(0, 1, 0, 6, "b"))]
    rows = season_board(nights).rows
    assert [(r.judge, r.vendor, r.nights) for r in rows] == [(0, "a", 1), (0, "b", 1)]


def test_an_empty_season_is_empty() -> None:
    empty = season_board([])
    assert empty.rows == () and empty.nights == 0 and empty.agreement_milli is None


def test_the_night_board_is_plain_numbers_for_json(truth: Truth) -> None:
    text = json.dumps(
        board(score_judge(judge(0, *FIRST), truth), agreed=(3, 5)).to_dict(), ensure_ascii=False
    )
    data = json.loads(text, parse_float=lambda raw: pytest.fail(f"a float: {raw}"))
    assert data["scores"][0]["precision_milli"] == 500
    assert data["agreement"] == {"compared": 5, "agreed": 3, "rate_milli": 600}
    assert data["absent"] == []


# ---------------------------------------------------------------------------- played nights


def test_the_judges_of_a_played_night_are_scored_against_its_missions() -> None:
    state = mock_night("board-2")
    judging = run_judging(state, [MockJudge(0), MockJudge(1)])
    truth = Truth.of_events(state.events)
    assert len(judging.accuracy) == 2
    for row in judging.accuracy:
        assert row.targets == len(truth.missions) == 6
        assert row.correct <= row.guesses
        assert 0 <= row.guesses <= 6
    # the two mock judges read the record alike, so they agree on every seat they guessed
    board_ = night_board(judging.run, truth)
    assert board_.agreement is not None and board_.agreement.rate_milli in (None, 1000)
    assert judging.accuracy[0].model_dump() == {**judging.accuracy[1].model_dump(), "judge": 0}


def test_a_perfect_judge_and_a_blind_judge_on_a_played_night() -> None:
    state = mock_night("board-3")
    truth = Truth.of_events(state.events)
    right = [
        guess(seat, m.kind, tag=getattr(m, "tag", None), lot=getattr(m, "lot", None))
        for seat, m in sorted(truth.missions.items())
    ]
    judges = [
        ScriptedJudge(0, "perfect", [answer(guesses=right)]),
        ScriptedJudge(1, "silent", [answer()]),
    ]
    judging = run_judging(state, judges)
    perfect, silent = judging.accuracy
    assert (perfect.guesses, perfect.correct, perfect.targets) == (6, 6, 6)
    assert (silent.guesses, silent.correct, silent.targets) == (0, 0, 6)


def test_a_season_of_played_nights_adds_the_boards_of_the_nights() -> None:
    judgings = [
        run_judging(mock_night(f"board-season-{k}"), [MockJudge(0), MockJudge(1)]) for k in range(3)
    ]
    for judging in judgings:
        assert tuple(s.accuracy() for s in judging.board.scores) == judging.accuracy
    season = season_board(j.board for j in judgings)
    assert season.nights == 3 and [(r.judge, r.vendor, r.nights) for r in season.rows] == [
        (0, "mock", 3),
        (1, "mock", 3),
    ]
    for row in season.rows:
        assert row.guesses == sum(j.accuracy[row.judge].guesses for j in judgings)
        assert row.correct == sum(j.accuracy[row.judge].correct for j in judgings)
        assert row.targets == 18
    assert season.compared == sum(j.board.agreement.compared for j in judgings)  # type: ignore[union-attr]
