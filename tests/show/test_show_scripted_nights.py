"""Whole nights played by the scripted policies, judged, awarded, highlighted and exported.

The mock policies of the model layer are scripted seats: ``honest`` bids by its value and promises
nothing, ``collude`` arranges in the chat to take lots in turn with a partner and sits out the
partner's turn in the bids, ``backstab`` pledges not to bid and bids all the same. Each night here
is played through the engine, read by two mock judges, and goes through the same steps a published
night goes through: the findings, the awards with the bots' list, the highlights, the replay and
the content gate. Nothing is called and nothing leaves the process.

What the nights show:

* **honest**: the judges find nothing to report. No finding of any kind, no pledge kept or broken,
  and none of the awards or highlights that need one.
* **collude**: seats 2 and 4 get a collusion card, red or suspect by the rule of two lots (a red
  card needs at least two lots in which both the chat and the bids speak). The card holds the
  statistic of every lot and the threshold, and the two keep their pledges, so the keeper award
  is theirs.
* **backstab**: seat 5 breaks its pledge in nearly every lot. Each broken pledge is a finding
  decided by program, seat 5 wins the backstabber award with that count, and the night has
  backstab highlights.
* **judges absent**: when both judges give no usable answer, only what the program decides is
  left: the broken pledges, the awards and highlights that follow from them. No red card can be
  given, because it needs two judges to agree on what was said.
* **the same night, the same bytes**: playing a night again and exporting it gives the same file,
  also in a process with another hash seed.
"""

import json
import os
import subprocess
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import pytest
from export_support import NO_GIT
from judge_support import ScriptedJudge, hybrid_night, night_config
from prize_support import LogOracle

from auction_night.contracts import NightConfig, Replay
from auction_night.contracts.events import AWARD_KEYS, JudgeVerdictData
from auction_night.engine.night import NightState
from auction_night.show.content_scan import ContentGate
from auction_night.show.factcard import FactCards, build_fact_cards
from auction_night.show.host import MockHost, run_host
from auction_night.show.judges import JudgeBackend, MockJudge
from auction_night.show.replay_export import (
    ShowParts,
    check_replay,
    export_replay,
    show_parts,
)
from auction_night.show.screening import NULL_THRESHOLD_MILLI
from auction_night.show.verdicts import MIN_RED_LOTS, Judging, run_judging
from auction_night.theory import LotTheory, analyse_night

SEATS = (1, 2, 3, 4, 5, 6)
HONEST = tuple((seat, "honest") for seat in SEATS)
COLLUDE = tuple((seat, "collude" if seat in (2, 4) else "honest") for seat in SEATS)
BACKSTAB = tuple((seat, "backstab" if seat == 5 else "honest") for seat in SEATS)
SEEDS = ("d10-1", "d10-2", "d10-3")

Policies = tuple[tuple[int, str], ...]


@dataclass(frozen=True)
class Night:
    """A scripted night with everything that follows it, judged by two mock judges."""

    config: NightConfig
    state: NightState
    cards: FactCards
    theory: list[LotTheory]
    judging: Judging
    parts: ShowParts

    @property
    def verdicts(self) -> tuple[JudgeVerdictData, ...]:
        return self.judging.verdicts

    def kinds(self) -> list[str]:
        return [v.verdict for v in self.verdicts]

    def award(self, key: str) -> tuple[list[int], int | None]:
        entry = next(a for a in self.parts.awards if a.award == key)
        return entry.winners, entry.metric

    def categories(self) -> list[str]:
        return [h.category for h in self.parts.highlights]


@cache
def played(policies: Policies, seed: str, bids: Policies = (), talk: Policies = ()) -> Night:
    """Play a night of those policies (``bids`` and ``talk`` replace the policy of a seat for that
    part of its play) and judge it with the two mock judges."""
    state = hybrid_night(
        seed, policies=dict(policies), bids=dict(bids) or None, talk=dict(talk) or None
    )
    config = night_config(dict(policies))
    cards = build_fact_cards(state, theory=(theory := analyse_night(state)))
    judging = run_judging(state, [MockJudge(0), MockJudge(1)])
    return Night(config, state, cards, theory, judging, show_parts(config, state, cards))


def without_judges(night: Night, kind: str = "other") -> Night:
    """The same night when the judges give no usable answer."""
    if kind == "unreadable":
        judges: list[JudgeBackend] = [
            MockJudge(0, fault="always", spoil="garbage"),
            MockJudge(1, fault="always", spoil="shape"),
        ]
    else:
        judges = [ScriptedJudge(0, "judge-a", [None]), ScriptedJudge(1, "judge-b", [None])]
    judging = run_judging(night.state, judges)
    parts = show_parts(night.config, night.state, night.cards, backends=judges)
    return Night(night.config, night.state, night.cards, night.theory, judging, parts)


def exported(night: Night) -> tuple[Replay, str]:
    """The night through the gate to the text of the file, with no git and no clock."""
    host = run_host(night.cards, MockHost()).block
    done = export_replay(
        night.config,
        night.state,
        host,
        gate=ContentGate.from_config(night.config),
        theory=night.theory,
        cards=night.cards,
        parts=night.parts,
        git=NO_GIT,
    )
    assert done.report.ok
    return done.replay, done.text


def pledge_verdicts(night: Night) -> list[JudgeVerdictData]:
    return [v for v in night.verdicts if v.verdict == "pledge_violation"]


def collusion(night: Night) -> list[JudgeVerdictData]:
    return [v for v in night.verdicts if v.verdict.startswith("collusion")]


def test_the_parts_of_the_replay_are_the_judging_stage_laid_out_for_the_file() -> None:
    for policies in (HONEST, COLLUDE, BACKSTAB):
        night = played(policies, SEEDS[0])
        assert night.parts.judges == night.judging.judges
        assert night.parts.verdicts == night.judging.verdicts
        assert night.parts.judge_accuracy == night.judging.accuracy
        assert night.parts.judges_absent == night.judging.run.absences == ()


# ===================================================================================== honest


@pytest.mark.parametrize("seed", SEEDS)
def test_an_honest_night_has_no_finding_of_any_kind(seed: str) -> None:
    night = played(HONEST, seed)
    assert night.verdicts == () and night.parts.verdicts == ()
    assert night.judging.pledges == () and night.judging.false_claims == ()
    assert night.judging.collusion.ring == ()
    assert not any(f.chat for f in night.judging.collusion.lots), "nobody arranged anything"
    assert not any(f.over for f in night.judging.collusion.lots), "and nobody sat out"
    assert night.parts.judges_absent == ()
    assert [j.judge for j in night.parts.judges] == [0, 1]


@pytest.mark.parametrize("seed", SEEDS)
def test_an_honest_night_gives_none_of_the_awards_or_highlights_that_need_a_word_or_a_claim(
    seed: str,
) -> None:
    night = played(HONEST, seed)
    for key in ("backstabber", "best_actor", "keeper"):
        assert night.award(key) == ([], None), key
    assert "backstab" not in night.categories() and "claim_vs_hand" not in night.categories()
    assert [a.award for a in night.parts.awards] == list(AWARD_KEYS)
    assert night.parts.highlights, "an honest night is still a night with moments in it"


@pytest.mark.parametrize("seed", SEEDS)
def test_the_judges_of_an_honest_night_still_guess_missions_and_are_scored(seed: str) -> None:
    night = played(HONEST, seed)
    rows = night.parts.judge_accuracy
    assert [(r.judge, r.vendor) for r in rows] == [(0, "mock"), (1, "mock")]
    assert all(r.targets == 6 and 0 <= r.correct <= r.guesses for r in rows)


# ==================================================================================== collusion


@pytest.mark.parametrize("seed", SEEDS)
def test_seats_that_arrange_in_the_chat_and_sit_out_in_the_bids_get_a_collusion_card(
    seed: str,
) -> None:
    night = played(COLLUDE, seed)
    (verdict,) = collusion(night)
    report = night.judging.collusion
    assert report.ring == (2, 4) and report.judges_agree
    both = [f.lot for f in report.lots if f.chat and f.over]
    anything = [f.lot for f in report.lots if f.chat or f.over]
    # the rule of two lots: a red card needs at least two lots in which both sides speak
    if len(both) >= MIN_RED_LOTS:
        assert verdict.verdict == "collusion_red" and verdict.lots == both
    else:
        assert verdict.verdict == "collusion_suspect" and verdict.lots == anything
    assert verdict.verdict in ("collusion_red", "collusion_suspect")
    assert verdict.seats == [2, 4] and verdict.intent is None
    assert verdict.quotes and all("号" in q or "轮" in q for q in verdict.quotes)


@pytest.mark.parametrize("seed", SEEDS)
def test_the_card_holds_the_statistic_of_every_lot_and_the_threshold(seed: str) -> None:
    night = played(COLLUDE, seed)
    (verdict,) = collusion(night)
    assert sorted(verdict.statistic_milli) == list(range(1, 13))
    assert verdict.statistic_milli == {
        f.lot: f.statistic_milli for f in night.judging.collusion.lots
    }
    assert verdict.threshold_milli == NULL_THRESHOLD_MILLI
    if verdict.verdict == "collusion_red":
        assert all(verdict.statistic_milli[n] > NULL_THRESHOLD_MILLI for n in verdict.lots)
    assert verdict.topic is None and verdict.gap_milli is None


@pytest.mark.parametrize("seed", SEEDS)
def test_a_red_card_stands_on_events_of_the_log_and_words_that_were_said(seed: str) -> None:
    night = played(COLLUDE, seed)
    (verdict,) = collusion(night)
    by_seq = {e.seq: e for e in night.state.events}
    kinds = {by_seq[s].type for s in verdict.evidence_seqs}
    assert kinds <= {"talk", "hammer"} and "hammer" in kinds
    said = [e.data.text for e in night.state.events if e.type == "talk"]
    assert verdict.quotes and all(any(q in text for text in said) for q in verdict.quotes)


@pytest.mark.parametrize("seed", SEEDS)
def test_the_colluders_keep_their_pledges_and_share_the_keeper_award(seed: str) -> None:
    night = played(COLLUDE, seed)
    kept = Counter(p.seat for p in night.judging.pledges if p.outcome == "kept")
    assert set(kept) == {2, 4} and all(n >= 2 for n in kept.values())
    assert night.award("keeper") == (
        [seat for seat in (2, 4) if kept[seat] == max(kept.values())],
        max(kept.values()),
    ), "the one who kept the most, or both on a tie"
    assert night.award("backstabber") == ([], None)
    assert pledge_verdicts(night) == []


def test_an_arrangement_in_words_alone_is_a_suspect_and_the_night_still_exports() -> None:
    night = played(COLLUDE, "d10-2", bids=((2, "honest"), (4, "honest")))
    (verdict,) = collusion(night)
    assert verdict.verdict == "collusion_suspect" and verdict.seats == [2, 4]
    assert not any(f.over for f in night.judging.collusion.lots)
    replay, _ = exported(night)
    assert [v.verdict for v in replay.verdicts] == ["collusion_suspect"]
    assert check_replay(replay) == []


def test_sitting_out_in_silence_is_a_suspect_that_names_nobody() -> None:
    night = played(COLLUDE, "d10-1", talk=((2, "honest"), (4, "honest")))
    (verdict,) = collusion(night)
    assert verdict.verdict == "collusion_suspect" and verdict.seats == []
    assert night.judging.collusion.ring == () and verdict.quotes == []
    replay, _ = exported(night)
    assert replay.verdicts[0].statistic_milli == verdict.statistic_milli


# =================================================================================== backstab


@pytest.mark.parametrize("seed", SEEDS[:2])
def test_each_broken_pledge_is_a_finding_decided_by_program(seed: str) -> None:
    night = played(BACKSTAB, seed)
    violated = [p for p in night.judging.pledges if p.outcome == "violated"]
    found = pledge_verdicts(night)
    assert len(found) == len(violated) >= 10
    assert {tuple(v.seats) for v in found} == {(5,)}
    assert {v.intent for v in found} == {"unknown"}, "the log holds what was bid, not why"
    assert all(v.detail.startswith("承诺第 ") and "不出价" in v.detail for v in found)
    assert "collusion_red" not in night.kinds()


@pytest.mark.parametrize("seed", SEEDS[:2])
def test_the_backstabber_wins_its_award_with_the_number_of_pledges_it_broke(seed: str) -> None:
    night = played(BACKSTAB, seed)
    assert night.award("backstabber") == ([5], len(pledge_verdicts(night)))
    assert night.award("keeper") == ([], None) and night.award("best_actor") == ([], None)


@pytest.mark.parametrize("seed", SEEDS[:2])
def test_the_night_has_backstab_highlights_about_the_seat_that_broke_its_word(seed: str) -> None:
    night = played(BACKSTAB, seed)
    lights = [h for h in night.parts.highlights if h.category == "backstab"]
    assert lights, "a night of broken pledges has backstab moments"
    broken_in = {lot for v in pledge_verdicts(night) for lot in v.lots}
    for light in lights:
        assert light.seats == [5] and light.lot in broken_in and light.basis == "public"
        assert "答应" in light.text and light.first_seq <= light.last_seq
        assert light.score >= 4


@pytest.mark.parametrize("seed", SEEDS[:2])
def test_the_awards_of_every_scripted_night_are_what_the_log_says(seed: str) -> None:
    """An oracle that reads nothing but the event log and the deal agrees with the show layer."""
    for policies in (HONEST, COLLUDE, BACKSTAB):
        night = played(policies, seed)
        expected = LogOracle(night.state).awards("player", list(night.verdicts))
        got = {a.award: (a.winners, a.metric) for a in night.parts.awards}
        assert got == expected, policies
        bots = LogOracle(night.state).awards("bot", list(night.verdicts))
        assert {a.award: (a.winners, a.metric) for a in night.parts.bots_reference} == {
            key: bots[key] for key in ("most_textbook", "winners_curse", "tilt", "bargain")
        }


# ================================================================================= judges absent


@pytest.mark.parametrize("kind", ["other", "unreadable"])
def test_with_both_judges_absent_a_backstab_night_keeps_only_the_findings_of_the_program(
    kind: str,
) -> None:
    full = played(BACKSTAB, SEEDS[0])
    night = without_judges(full, kind)
    assert not night.judging.run.both and night.parts.judges == ()
    assert night.parts.judge_accuracy == ()
    assert [(a.judge, a.reason) for a in night.parts.judges_absent] == [(0, kind), (1, kind)]
    assert night.kinds() == ["pledge_violation"] * len(pledge_verdicts(full))
    assert list(night.verdicts) == pledge_verdicts(full), "the same findings, judges or not"
    assert night.award("backstabber") == full.award("backstabber")
    assert night.award("best_actor") == ([], None)
    assert [h for h in night.parts.highlights if h.category == "backstab"] == [
        h for h in full.parts.highlights if h.category == "backstab"
    ]


def test_with_both_judges_absent_a_colluding_night_cannot_get_a_red_card() -> None:
    full = played(COLLUDE, SEEDS[0])
    assert full.kinds() == ["collusion_red"]
    night = without_judges(full)
    assert "collusion_red" not in night.kinds(), "a red card needs two judges to agree on words"
    for verdict in collusion(night):
        assert verdict.verdict == "collusion_suspect" and verdict.seats == [], "bids alone"
        assert verdict.quotes == []
    assert night.judging.collusion.ring == () and not night.judging.collusion.judges_agree
    assert night.award("keeper") == full.award("keeper"), "pledges are the program's to judge"


def test_with_one_judge_absent_the_other_is_still_listed_and_nothing_needs_both() -> None:
    full = played(BACKSTAB, SEEDS[0])
    judges: list[JudgeBackend] = [MockJudge(0), ScriptedJudge(1, "judge-b", [None])]
    judging = run_judging(full.state, judges)
    parts = show_parts(full.config, full.state, full.cards, backends=judges)
    assert [j.judge for j in parts.judges] == [0]
    assert [(a.judge, a.vendor, a.reason) for a in parts.judges_absent] == [(1, "judge-b", "other")]
    assert [r.judge for r in parts.judge_accuracy] == [0]
    assert list(judging.verdicts) == pledge_verdicts(full)


@pytest.mark.parametrize("kind", ["other", "unreadable"])
def test_a_night_without_judges_still_exports_and_says_who_was_absent(kind: str) -> None:
    night = without_judges(played(BACKSTAB, SEEDS[0]), kind)
    replay, text = exported(night)
    assert replay.judges == [] and [a.reason for a in replay.judges_absent] == [kind, kind]
    assert Replay.model_validate_json(text) == replay and check_replay(replay) == []
    assert {v.verdict for v in replay.verdicts} == {"pledge_violation"}


# ======================================================================== the file of a night


@pytest.mark.parametrize(
    "policies", [HONEST, COLLUDE, BACKSTAB], ids=["honest", "collude", "backstab"]
)
def test_a_scripted_night_exports_a_valid_replay_with_every_part(policies: Policies) -> None:
    night = played(policies, SEEDS[0])
    replay, text = exported(night)
    assert Replay.model_validate_json(text) == replay and check_replay(replay) == []
    assert [e.judge for e in replay.judges] == [0, 1] and replay.judges_absent == []
    assert [a.award for a in replay.awards] == list(AWARD_KEYS)
    assert [a.award for a in replay.bots_reference] == [
        "most_textbook", "winners_curse", "tilt", "bargain"
    ]  # fmt: skip
    assert all(a.basis is not None for a in (*replay.awards, *replay.bots_reference))
    assert all(h.basis is not None for h in replay.highlights)
    assert all(lot.talk_seqs and lot.hammer_seq == lot.last_seq for lot in replay.timeline)
    assert replay.verdicts == list(night.verdicts)
    assert any(s.stats.mind_error_milli is not None for s in replay.scores)


@pytest.mark.parametrize(
    "policies", [HONEST, COLLUDE, BACKSTAB], ids=["honest", "collude", "backstab"]
)
def test_the_same_night_exports_the_same_bytes_when_it_is_played_again(policies: Policies) -> None:
    first = played(policies, SEEDS[0])
    again = played.__wrapped__(policies, SEEDS[0])  # a night played from the start, not the cache
    assert again is not first
    assert exported(first)[1] == exported(again)[1]
    assert exported(first)[1] == exported(first)[1]


DIGEST_SCRIPT = """
import sys
sys.path[:0] = sys.argv[1:4]
from test_show_scripted_nights import COLLUDE, SEEDS, exported, played

print(exported(played(COLLUDE, SEEDS[0]))[1], end="")
"""


def test_the_bytes_of_a_night_do_not_depend_on_the_hash_seed_of_the_process() -> None:
    tests = Path(__file__).resolve().parents[1]
    outputs = set()
    for seed in ("1", "31337"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "CUDA_VISIBLE_DEVICES": ""}
        done = subprocess.run(
            [sys.executable, "-c", DIGEST_SCRIPT]
            + [str(tests / part) for part in ("engine", "night", "show")],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        outputs.add(done.stdout)
    assert len(outputs) == 1
    (text,) = outputs
    assert text == exported(played(COLLUDE, SEEDS[0]))[1]
    assert json.loads(text)["schema"] == "auction-night/replay@1"


def test_a_scripted_night_is_a_fixed_function_of_its_seed_and_policies() -> None:
    texts = {exported(played(BACKSTAB, seed))[1] for seed in SEEDS[:2]}
    assert len(texts) == 2, "another deal, another file"


def policies_of(night: Night) -> Sequence[str]:
    return [seat.mock_policy or "" for seat in night.config.seats if seat.kind == "mock"]


def test_the_scripted_nights_are_the_ones_they_say_they_are() -> None:
    assert policies_of(played(HONEST, SEEDS[0])) == ["honest"] * 6
    assert policies_of(played(COLLUDE, SEEDS[0])) == [
        "honest", "collude", "honest", "collude", "honest", "honest"
    ]  # fmt: skip
    assert policies_of(played(BACKSTAB, SEEDS[0])) == [
        "honest", "honest", "honest", "honest", "backstab", "honest"
    ]  # fmt: skip
