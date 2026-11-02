"""The judges' own score: how well each guessed the secret missions, and how often they agree.

The judges are scored too, which is part of the show. The standard answer is the missions the deal
handed out (read from the log by :class:`~auction_night.show.truth.Truth`), and the judges never
see them.

One guess, one seat
-------------------
A judge guesses at most one mission per player. A guess is **correct** when the seat holds a
mission of that kind and the details match: a collector guess names the seat's tag, a shill guess
names its target lot. A guess of the right kind with the wrong tag or lot counts in
``kind_correct`` only. A guess for a seat that holds no mission is wrong.

* **precision** = ``correct / guesses``: of the missions the judge named, how many it got right;
* **recall** = ``correct / targets``: of the missions that were held, how many it got right
  (``targets`` is the number of missions of the night);
* **agreement** of the two judges: among the players that at least one of them guessed
  (``compared``), the share where both made the *same* guess (``agreed``). A seat that only one
  judge guessed counts as a disagreement.

Rates are integer thousandths, rounded half up, and ``None`` when the denominator is 0.

From a night to a season
------------------------
A night's board holds counts. A season's board **adds the counts** (guesses, correct, kind
correct, targets, compared, agreed) across the nights and computes each rate once, from the
summed counts. It never adds, averages or otherwise combines the rates of single nights, so a
season rate equals the count-weighted mean of the nightly rates and a night with more guesses
weighs more. A judge that was absent on a night has no row for it (it neither guessed nor missed),
and the season row says how many nights it counts. Rows are told apart by judge number and
vendor.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from auction_night.contracts.events import (
    CollectorMission,
    JudgeExtractData,
    Mission,
    MissionGuess,
)
from auction_night.contracts.replay import JudgeAccuracy
from auction_night.show.judges import JudgeRun
from auction_night.show.truth import Truth

__all__ = [
    "Agreement",
    "JudgeScore",
    "NightBoard",
    "SeasonBoard",
    "SeasonRow",
    "accuracy_rows",
    "agreement",
    "guess_is_correct",
    "night_board",
    "ratio_milli",
    "score_judge",
    "season_board",
]


def ratio_milli(part: int, whole: int) -> int | None:
    """``part / whole`` in integer thousandths, rounded half up; None if ``whole`` is 0."""
    if whole == 0:
        return None
    return (2000 * part + whole) // (2 * whole)


def guess_is_correct(guess: MissionGuess, mission: Mission | None) -> bool:
    """Whether a guess names the seat's mission: same kind, and the same tag or target lot."""
    if mission is None or mission.kind != guess.kind:
        return False
    if isinstance(mission, CollectorMission):
        return guess.tag == mission.tag
    return guess.lot == mission.lot


# ------------------------------------------------------------------------------ one night


@dataclass(frozen=True, slots=True)
class JudgeScore:
    """One judge's counts for one night."""

    judge: int
    vendor: str
    guesses: int
    correct: int
    kind_correct: int
    targets: int

    @property
    def precision_milli(self) -> int | None:
        """``correct / guesses`` in thousandths."""
        return ratio_milli(self.correct, self.guesses)

    @property
    def recall_milli(self) -> int | None:
        """``correct / targets`` in thousandths."""
        return ratio_milli(self.correct, self.targets)

    def accuracy(self) -> JudgeAccuracy:
        """The part of the replay that holds these counts."""
        return JudgeAccuracy(
            judge=self.judge,
            vendor=self.vendor,
            guesses=self.guesses,
            correct=self.correct,
            targets=self.targets,
        )


def score_judge(data: JudgeExtractData, truth: Truth) -> JudgeScore:
    """The counts of one judge's mission guesses against the missions of the night."""
    guesses = [g for g in data.mission_guesses if g.seat in truth.players]
    correct = sum(1 for g in guesses if guess_is_correct(g, truth.mission_of(g.seat)))
    kind_correct = sum(
        1
        for g in guesses
        if (mission := truth.mission_of(g.seat)) is not None and mission.kind == g.kind
    )
    return JudgeScore(
        judge=data.judge,
        vendor=data.vendor,
        guesses=len(guesses),
        correct=correct,
        kind_correct=kind_correct,
        targets=sum(1 for seat in truth.missions if seat in truth.players),
    )


@dataclass(frozen=True, slots=True)
class Agreement:
    """How often two judges made the same guess: ``agreed`` of ``compared`` seats."""

    compared: int
    agreed: int

    @property
    def rate_milli(self) -> int | None:
        """``agreed / compared`` in thousandths."""
        return ratio_milli(self.agreed, self.compared)


def agreement(first: JudgeExtractData, second: JudgeExtractData) -> Agreement:
    """The agreement of two judges on a night (see the module text)."""
    one = {g.seat: g for g in first.mission_guesses}
    two = {g.seat: g for g in second.mission_guesses}
    seats = set(one) | set(two)
    same = sum(1 for seat in seats if one.get(seat) == two.get(seat))
    return Agreement(compared=len(seats), agreed=same)


@dataclass(frozen=True, slots=True)
class NightBoard:
    """The judges' score for one night: a row per judge that delivered, and their agreement
    (None if a judge was absent). ``absent`` lists the numbers of the judges that did not
    deliver."""

    scores: tuple[JudgeScore, ...]
    agreement: Agreement | None
    absent: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Plain integers for JSON."""
        return {
            "scores": [
                {
                    "judge": s.judge,
                    "vendor": s.vendor,
                    "guesses": s.guesses,
                    "correct": s.correct,
                    "kind_correct": s.kind_correct,
                    "targets": s.targets,
                    "precision_milli": s.precision_milli,
                    "recall_milli": s.recall_milli,
                }
                for s in self.scores
            ],
            "agreement": None
            if self.agreement is None
            else {
                "compared": self.agreement.compared,
                "agreed": self.agreement.agreed,
                "rate_milli": self.agreement.rate_milli,
            },
            "absent": list(self.absent),
        }


def night_board(run: JudgeRun, truth: Truth) -> NightBoard:
    """The score of the judges of one night."""
    extracts = run.extracts
    return NightBoard(
        scores=tuple(score_judge(data, truth) for data in extracts),
        agreement=agreement(extracts[0], extracts[1]) if run.both else None,
        absent=tuple(r.index for r in run.absent),
    )


def accuracy_rows(run: JudgeRun, truth: Truth) -> tuple[JudgeAccuracy, ...]:
    """The replay's ``judge_accuracy``: a row per judge that delivered."""
    return tuple(score.accuracy() for score in night_board(run, truth).scores)


# ----------------------------------------------------------------------------- a season


@dataclass(frozen=True, slots=True)
class SeasonRow:
    """One judge over a season: summed counts and the nights they come from."""

    judge: int
    vendor: str
    nights: int
    guesses: int
    correct: int
    kind_correct: int
    targets: int

    @property
    def precision_milli(self) -> int | None:
        """Season precision: summed ``correct`` over summed ``guesses``."""
        return ratio_milli(self.correct, self.guesses)

    @property
    def recall_milli(self) -> int | None:
        """Season recall: summed ``correct`` over summed ``targets``."""
        return ratio_milli(self.correct, self.targets)


@dataclass(frozen=True, slots=True)
class SeasonBoard:
    """The judges over a season: a row per judge, the summed agreement, the nights counted."""

    rows: tuple[SeasonRow, ...]
    compared: int
    agreed: int
    nights: int

    @property
    def agreement_milli(self) -> int | None:
        """Season agreement: summed ``agreed`` over summed ``compared``."""
        return ratio_milli(self.agreed, self.compared)


def season_board(boards: Iterable[NightBoard]) -> SeasonBoard:
    """The season board: counts added across nights, every rate computed once from the sums."""
    rows: dict[tuple[int, str], Mapping[str, int]] = {}
    compared = agreed = nights = 0
    for board in boards:
        nights += 1
        for score in board.scores:
            total = dict(
                rows.get(
                    (score.judge, score.vendor),
                    {"nights": 0, "guesses": 0, "correct": 0, "kind_correct": 0, "targets": 0},
                )
            )
            total["nights"] += 1
            total["guesses"] += score.guesses
            total["correct"] += score.correct
            total["kind_correct"] += score.kind_correct
            total["targets"] += score.targets
            rows[(score.judge, score.vendor)] = total
        if board.agreement is not None:
            compared += board.agreement.compared
            agreed += board.agreement.agreed
    return SeasonBoard(
        rows=tuple(
            SeasonRow(judge=judge, vendor=vendor, **counts)  # type: ignore[arg-type]
            for (judge, vendor), counts in sorted(rows.items())
        ),
        compared=compared,
        agreed=agreed,
        nights=nights,
    )
