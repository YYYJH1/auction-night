"""The replay fixtures validate with the replay model and agree with the rules.

There are three files. ``english_dutch.json`` and ``sealed_four_lots.json`` are written by hand.
``mock_night.json`` is real output: the mock night of ``configs/night_mock.yaml`` exported by the
code that exports a published night (``tools/gen_mock_replay.py`` writes it and a test regenerates
it). The viewer tests (``tests/viewer``) load the same files, so these checks keep them honest:
prices follow the pricing rule of each format, budgets are never overspent, the scores are the
ledger of the night, the sequence numbers fit together, the per-lot overlay numbers are the ones
the theory module gives for the data in the file, and the awards, the highlights and the findings
of the judges follow their rules and point at moments that exist.

The real file differs from the hand-written ones in what it holds, and the checks say so where
it matters. It is canonical JSON, which writes every field: a value that is absent in a
hand-written file is ``null`` here, so "is there a ``true_value``" is asked as "is it not null".
It has real ties, and real tie keys: the deal seed is revealed in its manifest, so the checks
that depend on ties rebuild the keys from the deal. It carries every part of the show layer: the
judges, the findings, the awards with the bots' reference, the highlights and the statistic
columns of the scores. The show-layer parts of the real file are rebuilt from the facts in the
file, by the rules and not by the code that made them, and must be the file's, one for one. The
hand-written files were composed by hand before those rules existed, so their awards are checked
against the rules (they are short enough that the awards that need many observations are vacant)
and their highlights and findings for happening, not for the exact score or wording.
"""

import copy
import dataclasses
import json
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from difflib import SequenceMatcher
from fractions import Fraction
from pathlib import Path
from typing import Any, get_args

import pytest

from auction_night.contracts import FORMAT_KEYS, MECHANISMS, Replay, canonical_json
from auction_night.contracts.config import BlindConfig, config_hash, load_config
from auction_night.contracts.events import AWARD_KEYS, HammerData
from auction_night.contracts.mechanism import make_mechanism
from auction_night.contracts.replay import REPLAY_SCHEMA_ID, HighlightCategory
from auction_night.engine.deal import blind_bounds, deal
from auction_night.engine.formats import dutch_ladder
from auction_night.show.content_scan import scan_prompt_digest
from auction_night.show.host import host_templates_digest
from auction_night.show.judges import judge_templates_digest
from auction_night.show.screening import NULL_THRESHOLD_MILLI
from auction_night.theory.counterfactual import LotSetup, excess_return
from auction_night.theory.exact import expected_revenue
from auction_night.theory.regret import lot_regrets

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "replays"
VIEWER = REPO / "viewer"
PATHS = sorted(FIXTURES.glob("*.json"))
HAND_WRITTEN = ("english_dutch.json", "sealed_four_lots.json")
SAMPLE = "mock_night.json"
BID_PHASES = ("sealed", "english_init", "english_revise", "dutch")


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def by_name(name: str) -> dict[str, Any]:
    return load(FIXTURES / name)


def talk_lines(raw: dict[str, Any]) -> Iterator[str]:
    for lot in raw["timeline"]:
        for talk in lot["talk"]:
            yield talk["text"]


# ------------------------------------------------------------------------------- validation


def test_there_are_two_hand_written_fixtures_and_the_real_sample() -> None:
    assert [p.name for p in PATHS] == sorted([*HAND_WRITTEN, SAMPLE])


@pytest.mark.parametrize("path", PATHS, ids=lambda p: p.name)
def test_fixture_validates_with_the_replay_model(path: Path) -> None:
    replay = Replay.model_validate(load(path))
    assert replay.schema_id == REPLAY_SCHEMA_ID == "auction-night/replay@1"
    assert [lot.lot for lot in replay.timeline] == list(range(1, replay.night.n_lots + 1))


@pytest.mark.parametrize("path", PATHS, ids=lambda p: p.name)
def test_fixture_survives_a_canonical_round_trip(path: Path) -> None:
    replay = Replay.model_validate(load(path))
    text = canonical_json(replay)
    assert Replay.model_validate_json(text) == replay
    assert "canary" not in text


@pytest.mark.parametrize("path", PATHS, ids=lambda p: p.name)
def test_fixture_holds_no_float_and_no_control_character(path: Path) -> None:
    def refuse(text: str) -> float:
        raise AssertionError(f"float in {path.name}: {text}")

    raw = path.read_text(encoding="utf-8")
    json.loads(raw, parse_float=refuse)
    assert not re.search(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", raw)


def test_the_two_nights_cover_all_six_formats() -> None:
    sealed = Replay.model_validate(by_name("sealed_four_lots.json"))
    clocks = Replay.model_validate(by_name("english_dutch.json"))
    assert [lot.format for lot in sealed.timeline] == [
        "sealed_first", "sealed_second", "all_pay", "blind_box"
    ]  # fmt: skip
    assert [lot.format for lot in clocks.timeline] == ["english", "dutch"]
    covered = {lot.format for night in (sealed, clocks) for lot in night.timeline}
    assert covered == set(FORMAT_KEYS)


def test_the_real_sample_is_a_whole_night_of_eight_seats() -> None:
    sample = Replay.model_validate(by_name(SAMPLE))
    assert {lot.format for lot in sample.timeline} == set(FORMAT_KEYS)
    assert [lot.lot for lot in sample.timeline] == list(range(1, 13))
    assert [seat.kind for seat in sample.seats] == ["mock"] * 6 + ["bot"] * 2
    assert sample.night.status == "completed"
    assert sample.manifest.revealed_seed is not None, "the sample reveals its deal seed"
    raw = by_name(SAMPLE)
    assert tie_orders(raw) == ("deal",), "so the checks that depend on ties are exact"
    assert any(lot["hammer"]["tie_broken"] for lot in raw["timeline"]), "and it has a real tie"


# ------------------------------------------------------------------------- hostile text


def test_the_sealed_night_carries_the_hostile_chat_lines() -> None:
    lines = list(talk_lines(by_name("sealed_four_lots.json")))
    joined = "\n".join(lines)
    for needle in ("<img src=x onerror=alert(1)>", "<script>", "javascript:", "[link=", "[31m"):
        assert needle in joined, needle
    assert max(len(line) for line in lines) > 1000, "a very long line"
    assert all(len(line) <= 2000 for line in lines), "still valid under the contract limit"


def test_bots_and_players_speak_every_lot() -> None:
    for name in (*HAND_WRITTEN, SAMPLE):
        raw = by_name(name)
        seats = {seat["seat"] for seat in raw["seats"]}
        for lot in raw["timeline"]:
            assert {t["seat"] for t in lot["talk"]} == seats
            assert [t["seat"] for t in lot["talk"]] == lot["talk_order"]


# ------------------------------------------------------------------------------- the ledger


def settled(lot: dict[str, Any], reserve: int) -> None:
    """The recorded winner, price and payments follow the rule of the format."""
    hammer = lot["hammer"]
    fmt = lot["format"]
    winner, price = hammer["winner"], hammer["price"]
    if fmt in ("sealed_first", "sealed_second", "all_pay", "blind_box"):
        bids = {int(k): v for k, v in hammer["bids"].items()}
        top = max(bids.values())
        assert bids[winner] == top, "the winner has a highest bid"
        others = max((b for s, b in bids.items() if s != winner), default=0)
        if fmt == "sealed_first":
            assert price == top
            assert hammer["payments"] == {str(winner): price}
        elif fmt == "all_pay":
            assert price == top
            assert hammer["payments"] == {str(s): b for s, b in bids.items() if b >= 1}
        else:
            assert price == max(others, reserve)
            assert hammer["payments"] == {str(winner): price}
        return
    if fmt == "english":
        exits = [d for d in lot["dropouts"] if d.get("participated", True)]
        assert exits[-1]["remaining"] == [winner]
        assert price == max(exits[-1]["price"], reserve)
        assert [d["price"] for d in exits] == sorted(d["price"] for d in exits)
    else:
        dutch = lot["dutch"]
        assert dutch["buyer"] == winner and dutch["price"] == price
        assert dutch["ladder"][dutch["level"] - 1] == price
        assert dutch["ladder"] == sorted(dutch["ladder"], reverse=True)
    assert hammer["payments"] == {str(winner): price}


@pytest.mark.parametrize("path", PATHS, ids=lambda p: p.name)
def test_every_lot_is_settled_by_its_pricing_rule(path: Path) -> None:
    raw = load(path)
    catalog = {entry["lot"]: entry for entry in raw["catalog"]}
    for lot in raw["timeline"]:
        entry = catalog[lot["lot"]]
        assert entry["format"] == lot["format"] and entry["mechanism"] == lot["mechanism"]
        settled(lot, entry["reserve"])
        blind = lot["format"] == "blind_box"
        assert (lot["hammer"].get("true_value") is not None) == blind
        if blind:
            assert str(lot["lot"]) in raw["reveal"]["true_values"]
            assert lot["hammer"]["true_value"] == raw["reveal"]["true_values"][str(lot["lot"])]


@pytest.mark.parametrize("path", PATHS, ids=lambda p: p.name)
def test_budgets_are_never_overspent_and_scores_are_the_ledger(path: Path) -> None:
    raw = load(path)
    reveal = raw["reveal"]
    remaining = {int(s): b for s, b in reveal["budgets"].items()}
    spent = dict.fromkeys(remaining, 0)
    surplus = dict.fromkeys(remaining, 0)
    won: dict[int, list[int]] = {seat: [] for seat in remaining}
    for lot in raw["timeline"]:
        number = lot["lot"]
        hammer = lot["hammer"]
        for seat, bid in (hammer.get("bids") or {}).items():
            assert bid <= remaining[int(seat)], f"lot {number}: seat {seat} bids above its budget"
        for guess in (g for g in reveal["guesses"] if g["lot"] == number):
            assert guess["truth"] == remaining[guess["target"]], "the truth is the budget left"
            assert guess["abs_error"] == abs(guess["guess"] - guess["truth"])
        for seat, amount in hammer["payments"].items():
            assert amount <= remaining[int(seat)], f"lot {number}: seat {seat} overspends"
            remaining[int(seat)] -= amount
            spent[int(seat)] += amount
            surplus[int(seat)] -= amount
        if hammer["winner"] is not None:
            winner = hammer["winner"]
            blind = hammer.get("true_value")
            worth = blind if blind is not None else reveal["values"][str(winner)][str(number)]
            surplus[winner] += worth
            won[winner].append(number)
    bonus = {m["seat"]: m["bonus"] for m in reveal["missions"]}
    for score in raw["scores"]:
        seat = score["seat"]
        assert score["spent"] == spent[seat]
        assert score["surplus"] == surplus[seat]
        assert score["mission_bonus"] == bonus.get(seat, 0)
        assert score["total"] == score["surplus"] + score["mission_bonus"]
        assert score["lots_won"] == won[seat]
    bots = pool_of(raw, ("bot",))
    totals = {s["seat"]: s["total"] for s in raw["scores"] if s["seat"] not in bots}
    for score in raw["scores"]:
        if score["seat"] in bots:
            assert score.get("rank_tonight") is None, "a bot is not ranked"
        else:  # competition ranking: equal totals share a rank and the next rank is skipped
            beaten_by = sum(1 for total in totals.values() if total > score["total"])
            assert score["rank_tonight"] == 1 + beaten_by, score["seat"]


@pytest.mark.parametrize("path", PATHS, ids=lambda p: p.name)
def test_sealed_actions_match_the_revealed_bids(path: Path) -> None:
    raw = load(path)
    for lot in raw["timeline"]:
        bids = lot["hammer"].get("bids")
        if bids is None:
            continue
        for action in lot["actions"]:
            assert action["phase"] == "sealed"
            expected = 0 if action["status"] == "abstain" else action["value"]
            assert bids[str(action["seat"])] == expected
            if action["status"] == "clamped":
                assert action["raw_value"] > action["value"]


# ------------------------------------------------------------------------ sequence numbers


@pytest.mark.parametrize("path", PATHS, ids=lambda p: p.name)
def test_sequence_numbers_fit_together(path: Path) -> None:
    raw = load(path)
    lots = raw["timeline"]
    for before, after in zip(lots, lots[1:], strict=False):
        assert before["last_seq"] < after["first_seq"]
    ranges = {lot["lot"]: (lot["first_seq"], lot["last_seq"]) for lot in lots}
    action_by_seq: dict[int, tuple[int, str, int]] = {}
    for lot in lots:
        for action in lot["actions"]:
            assert ranges[lot["lot"]][0] < action["seq"] < ranges[lot["lot"]][1]
            action_by_seq[action["seq"]] = (action["seat"], action["phase"], lot["lot"])
    seqs = [t["seq"] for t in raw["reveal"]["thoughts"]]
    assert seqs == sorted(set(seqs)), "thoughts are unique and in event order"
    for thought in raw["reveal"]["thoughts"]:
        if thought["phase"] == "plan":
            assert thought.get("lot") is None and thought["seq"] < lots[0]["first_seq"]
            continue
        first, last = ranges[thought["lot"]]
        assert first < thought["seq"] < last
        if thought["phase"] in BID_PHASES:
            assert action_by_seq[thought["seq"]] == (
                thought["seat"], thought["phase"], thought["lot"]
            )  # fmt: skip
    for highlight in raw.get("highlights", []):
        assert highlight["first_seq"] <= highlight["last_seq"]
        assert highlight["last_seq"] <= lots[-1]["last_seq"]


@pytest.mark.parametrize("path", PATHS, ids=lambda p: p.name)
def test_plans_missions_and_hole_cards_cover_the_players(path: Path) -> None:
    raw = load(path)
    n_lots = raw["night"]["n_lots"]
    players = pool_of(raw, ("llm", "mock", "human"))
    reveal = raw["reveal"]
    assert {p["seat"] for p in reveal["plans"]} == players
    held = {m["seat"] for m in reveal["missions"]}
    assert (
        held <= players
        and len(reveal["missions"]) == raw["night"]["collectors"] + raw["night"]["shills"]
    )
    for plan in reveal["plans"]:
        assert len(plan["priorities"]) == len(plan["budget_plan"]) == n_lots
    assert set(reveal["budgets"]) == {str(s["seat"]) for s in raw["seats"]}
    for seat, values in reveal["values"].items():
        blind = {str(e["lot"]) for e in raw["catalog"] if e["format"] == "blind_box"}
        assert set(values) == {str(e["lot"]) for e in raw["catalog"]} - blind, seat
        if blind:
            assert set(reveal["signals"][seat]) == blind


# ------------------------------------------------------------------------ overlay numbers
#
# The per-lot numbers of the overlay are not free: they are what the theory module returns for
# the deal, the bids and the result that the file records. The checks below rebuild each lot from
# the file and compare. The night uses the default blind-box prior.
#
# Ties need care. The hand-written files do not say how ties were broken, so for them every
# number is checked under two tie orders (by seat number, and in reverse), and a number that
# holds for both does not depend on the tie keys. The real sample has real ties (lot 11 is
# decided between two equal top bids), and neither seat order reproduces them. Its manifest
# reveals the deal seed, so the checks rebuild the tie keys of the deal itself and are exact. A
# tie the hammer records is also ranked as the hammer says wherever the keys are not known.
#
# The hand-written Dutch lot does not keep the grab prices of the two bots, so for a seat
# without a recorded action they follow their policies: the honest bot grabs at its value, the
# economist at its textbook bid scaled by alpha. The real sample records every action of every
# seat, and those are used as they are.


def english_tie_key(lot: dict[str, Any], winner: int | None, asked: list[int]) -> dict[int, int]:
    """Tie ranks of an English lot: the winner, then the last seat to leave, and so on back."""
    order = [] if winner is None else [winner]
    order += [d["seat"] for d in reversed(lot["dropouts"]) if d.get("participated", True)]
    order += [d["seat"] for d in lot["dropouts"] if not d.get("participated", True)]
    order += [seat for seat in asked if seat not in order]
    return {seat: rank for rank, seat in enumerate(order)}


def deal_tie_keys(raw: dict[str, Any]) -> dict[int, dict[int, int]] | None:
    """The tie key of every seat in every lot, from the deal behind the file; None when the file
    does not say which deal it is. The file must be a night of the mock configuration and reveal
    its seed."""
    manifest = raw["manifest"]
    config = load_config(REPO / "configs" / "night_mock.yaml")
    seed = manifest.get("revealed_seed")
    if seed is None or manifest["config_hash"] != config_hash(config):
        return None
    return {lot.lot: dict(lot.tie_key) for lot in deal(config, seed=seed).lots}


def tie_orders(raw: dict[str, Any]) -> tuple[str, ...]:
    """The tie orders to check a file under: the deal's own, or the two bracketing seat orders."""
    return ("deal",) if deal_tie_keys(raw) is not None else ("seats", "reversed")


def ranked_ties(asked: list[int], hammer: HammerData, order: str) -> dict[int, int]:
    """Tie ranks by seat number (or the reverse), the winner of a recorded tie first."""
    seats = list(reversed(asked) if order == "reversed" else asked)
    if hammer.tie_broken and hammer.winner is not None:
        seats = [hammer.winner, *(seat for seat in seats if seat != hammer.winner)]
    return {seat: rank for rank, seat in enumerate(seats)}


def lot_setups(raw: dict[str, Any], *, order: str) -> Iterator[tuple[dict[str, Any], LotSetup]]:
    """Replay the ledger of the night and give every lot as a setup of the theory module.

    ``order`` is one of :func:`tie_orders`: ``deal`` takes the tie keys of the deal, ``seats`` and
    ``reversed`` rank ties by seat number or by the reverse order.
    """
    reveal = raw["reveal"]
    left = {int(seat): budget for seat, budget in reveal["budgets"].items()}
    values = {int(s): {int(k): v for k, v in d.items()} for s, d in reveal["values"].items()}
    true_values = {int(k): v for k, v in reveal.get("true_values", {}).items()}
    policy = {seat["seat"]: seat.get("policy") for seat in raw["seats"]}
    catalog = {entry["lot"]: entry for entry in raw["catalog"]}
    dealt = deal_tie_keys(raw) if order == "deal" else None
    for lot in raw["timeline"]:
        number, entry = lot["lot"], catalog[lot["lot"]]
        mech = make_mechanism(lot["format"], reserve=entry["reserve"])
        hammer = HammerData.model_validate(lot["hammer"])
        true_value = true_values.get(number)
        worth = {s: true_value if mech.value_model == "common" else values[s][number] for s in left}
        ref = {int(s): v for s, v in lot["overlay"]["ref_bids"].items()}
        alpha = {int(s): v for s, v in lot["overlay"].get("alpha_milli", {}).items()}
        if mech.clock == "none":
            asked = [seat for seat in sorted(left) if left[seat] >= 1]
            bids = {seat: hammer.bids[seat] for seat in asked}
        elif mech.clock == "ascending_button":
            asked = sorted(lot["active"])
            bids = dict.fromkeys(asked, 0)
            bids |= {d["seat"]: d["price"] for d in lot["dropouts"] if d.get("participated", True)}
            if hammer.winner is not None:
                exits = [a for a in lot["actions"] if a["seat"] == hammer.winner and a.get("value")]
                bids[hammer.winner] = max(exits, key=lambda a: a["seq"])["value"]
        else:
            ladder = dutch_ladder(entry["tier"], mech.levels, entry["reserve"])
            asked = sorted(seat for seat in left if left[seat] >= ladder[-1])
            bids = dict.fromkeys(asked, 0)
            grabs = {a["seat"]: a["value"] or 0 for a in lot["actions"] if a["phase"] == "dutch"}
            bids |= grabs
            for seat in asked:
                if seat in grabs:
                    continue
                if policy.get(seat) == "truthful":
                    bids[seat] = worth[seat]
                elif policy.get(seat) == "economist":
                    bids[seat] = ref[seat] * alpha.get(seat, 1000) // 1000
        if mech.clock == "ascending_button":
            ties = english_tie_key(lot, hammer.winner, asked)
        elif dealt is not None:
            ties = dealt[number]
        else:
            ties = ranked_ties(asked, hammer, order)
        setup = LotSetup(
            mech=mech,
            tier=entry["tier"],
            budgets=dict(left),
            tie_key={seat: ties.get(seat, 100 + seat) for seat in left},
            values=worth,
            true_value=true_value,
            bids=bids,
            hammer=hammer,
        )
        yield lot, setup
        for seat, amount in hammer.payments.items():
            left[seat] -= amount


@pytest.mark.parametrize("path", PATHS, ids=lambda p: p.name)
def test_the_rebuilt_lots_settle_exactly_as_recorded(path: Path) -> None:
    raw = load(path)
    for order in tie_orders(raw):
        for lot, setup in lot_setups(raw, order=order):
            replayed = setup.settle(setup.bids)
            assert (replayed.winner, replayed.price) == (setup.hammer.winner, setup.hammer.price)
            assert replayed.payments == setup.hammer.payments, f"lot {lot['lot']}"


@pytest.mark.parametrize("path", PATHS, ids=lambda p: p.name)
def test_curse_gap_is_the_price_minus_the_true_value(path: Path) -> None:
    raw = load(path)
    for lot in raw["timeline"]:
        overlay, hammer = lot["overlay"], lot["hammer"]
        if lot["format"] == "blind_box" and hammer["winner"] is not None:
            true_value = raw["reveal"]["true_values"][str(lot["lot"])]
            assert overlay["curse_gap"] == hammer["price"] - true_value
        else:
            assert overlay.get("curse_gap") is None, "only a sold blind box has a curse gap"


def test_the_sealed_night_shows_a_blind_box_that_cost_the_winner_money() -> None:
    lot = by_name("sealed_four_lots.json")["timeline"][3]
    assert lot["overlay"]["curse_gap"] == 14 > 0
    assert any(
        value < 0 for fixture in PATHS for lot in load(fixture)["timeline"]
        for value in lot["overlay"]["excess"].values()
    ), "excess is negative somewhere, and the viewer must show the sign"  # fmt: skip
    assert any(
        value > 0 for fixture in PATHS for lot in load(fixture)["timeline"]
        for value in lot["overlay"]["excess"].values()
    ), "and positive somewhere"  # fmt: skip


@pytest.mark.parametrize("path", PATHS, ids=lambda p: p.name)
def test_expected_revenue_is_the_exact_expectation_for_the_bidders_of_the_lot(path: Path) -> None:
    raw = load(path)
    catalog = {entry["lot"]: entry for entry in raw["catalog"]}
    for lot in raw["timeline"]:
        entry, overlay = catalog[lot["lot"]], lot["overlay"]
        mech = make_mechanism(lot["format"], reserve=entry["reserve"])
        exact = expected_revenue(mech, entry["tier"], len(lot["active"]), BlindConfig())
        if exact is None:
            assert entry["reserve"] > 0
            assert overlay.get("expected_revenue_milli") is None, "no exact figure with a reserve"
            continue
        milli = overlay["expected_revenue_milli"]
        assert isinstance(milli, int) and not isinstance(milli, bool)
        assert milli == math.floor(exact * 1000), f"lot {lot['lot']}"


@pytest.mark.parametrize("path", PATHS, ids=lambda p: p.name)
def test_a_textbook_bid_never_exceeds_the_budget_the_seat_had_left(path: Path) -> None:
    raw = load(path)
    for lot, setup in lot_setups(raw, order=tie_orders(raw)[0]):
        for seat, ref in lot["overlay"]["ref_bids"].items():
            assert ref <= setup.budgets[int(seat)], f"lot {lot['lot']} seat {seat}"


@pytest.mark.parametrize("path", PATHS, ids=lambda p: p.name)
def test_excess_is_the_earning_minus_the_earning_at_the_textbook_bid(path: Path) -> None:
    raw = load(path)
    for order in tie_orders(raw):
        for lot, setup in lot_setups(raw, order=order):
            ref = {int(s): v for s, v in lot["overlay"]["ref_bids"].items()}
            expected = {
                int(seat): value for seat, value in lot["overlay"].get("excess", {}).items()
            }
            assert set(expected) == set(ref), f"lot {lot['lot']}: a figure for every textbook bid"
            for seat, value in expected.items():
                assert value == excess_return(setup, seat, ref[seat]), (lot["lot"], seat)
                if setup.bids[seat] == ref[seat]:
                    assert value == 0, "playing the textbook bid leaves nothing to compare"


@pytest.mark.parametrize("path", PATHS, ids=lambda p: p.name)
def test_regret_is_what_the_engine_finds_for_the_order_of_ties(path: Path) -> None:
    raw = load(path)
    orders = tie_orders(raw)
    found = {
        order: [lot_regrets(setup) for _, setup in lot_setups(raw, order=order)] for order in orders
    }
    for index, lot in enumerate(raw["timeline"]):
        for seat, value in lot["overlay"]["regret"].items():
            options = {found[order][index].get(int(seat)) for order in orders}
            assert value in options, f"lot {lot['lot']} seat {seat}: {value} not in {options}"


@pytest.mark.parametrize("path", PATHS, ids=lambda p: p.name)
def test_the_highest_value_flag_follows_the_values(path: Path) -> None:
    raw = load(path)
    for lot, setup in lot_setups(raw, order=tie_orders(raw)[0]):
        flag = lot["overlay"].get("to_highest_value")
        winner = setup.hammer.winner
        if lot["format"] == "blind_box" or winner is None:
            assert flag is None
        else:
            assert flag is (setup.values[winner] >= max(setup.values[s] for s in setup.bids))


# ---------------------------------------------------------------- awards and highlights
#
# The award lists are not free either. They follow the rules of the awards: only model seats
# compete, the bots are listed apart as a reference, seats with the same figure win together,
# and an award that nobody reaches the lower limit for is vacant. The checks below rebuild each
# award from the facts that the same file holds (payments, pledges, verdicts, missions, bids,
# plans and guesses) and compare. That includes the three awards that ask for the most
# observations: six valid bids for the textbook, eight valid guesses for the mind reader and ten
# judgable lots for consistency. The two hand-written nights are too short to reach those limits,
# so for them the rebuilt awards are vacant; the real sample is a night of twelve lots and does
# reach them.
#
# Every seat's figures (valid bids and their mean distance to the textbook bid, valid guesses and
# their mean error, judgable lots and the share in which the plan was followed) are rebuilt too,
# and the sample's ``scores[].stats`` columns must be exactly these: the leaderboard columns and
# the awards come from one pass in the show layer, so they can never disagree.
#
# A replay carries the awards and the highlights or none of them. Both the awards and the
# highlights say what they rest on (``basis``), and the real sample must agree with the tables
# that the replay page holds back by.

MODEL_KINDS = ("llm", "mock")
BOT_AWARDS = ("most_textbook", "winners_curse", "tilt", "bargain")
MIN_VALID_BIDS, MIN_VALID_GUESSES, MIN_JUDGED_LOTS, MIN_TILT = 6, 8, 10, 10
AWARD_UNITS = {
    "most_textbook": "milli", "winners_curse": "coins", "tilt": "coins", "bargain": "coins",
    "backstabber": "count", "best_actor": "count", "keeper": "count",
    "mind_reader": "milli", "consistent": "milli",
}  # fmt: skip
HIGH_PLAN_STARS = 2  # a plan of two stars or more means "bid seriously"

Result = tuple[list[int], int | None]


def pool_of(raw: dict[str, Any], kinds: tuple[str, ...]) -> set[int]:
    return {seat["seat"] for seat in raw["seats"] if seat["kind"] in kinds}


def tiers_of(raw: dict[str, Any]) -> dict[int, int]:
    return {entry["lot"]: entry["tier"] for entry in raw["catalog"]}


def top(figures: dict[int, int], *, lowest: bool = False) -> Result:
    """The seats with the greatest (or least) figure, all of them on a tie, and that figure."""
    if not figures:
        return [], None
    best = min(figures.values()) if lowest else max(figures.values())
    return sorted(seat for seat, figure in figures.items() if figure == best), best


def curse_award(raw: dict[str, Any], pool: set[int]) -> Result:
    """Blind boxes: the price above the true value, at least a twentieth of the tier."""
    tiers, figures = tiers_of(raw), {}
    for lot in raw["timeline"]:
        hammer = lot["hammer"]
        if lot["format"] != "blind_box" or hammer["winner"] not in pool:
            continue
        gap = hammer["price"] - hammer["true_value"]
        if gap * 20 >= tiers[lot["lot"]]:
            seat = hammer["winner"]
            figures[seat] = max(figures.get(seat, gap), gap)
    return top(figures)


def tilt_award(raw: dict[str, Any], pool: set[int]) -> Result:
    """All-pay lots: the coins a seat paid without winning, at least ten in all."""
    lost = dict.fromkeys(pool, 0)
    for lot in raw["timeline"]:
        if lot["format"] != "all_pay":
            continue
        for key, amount in lot["hammer"]["payments"].items():
            if int(key) in pool and int(key) != lot["hammer"]["winner"]:
                lost[int(key)] += amount
    return top({seat: total for seat, total in lost.items() if total >= MIN_TILT})


def bargain_award(raw: dict[str, Any], pool: set[int]) -> Result:
    """The best single lot: worth minus price, at least a fifth of the tier."""
    tiers, values, figures = tiers_of(raw), raw["reveal"]["values"], {}
    for lot in raw["timeline"]:
        hammer, seat = lot["hammer"], lot["hammer"]["winner"]
        if seat not in pool:
            continue
        blind = lot["format"] == "blind_box"
        worth = hammer["true_value"] if blind else values[str(seat)][str(lot["lot"])]
        gain = worth - hammer["payments"][str(seat)]
        if gain * 5 >= tiers[lot["lot"]]:
            figures[seat] = max(figures.get(seat, gain), gain)
    return top(figures)


def backstab_award(raw: dict[str, Any], pool: set[int]) -> Result:
    """Pledges broken, decided by program; at least one."""
    broken = [p["seat"] for p in raw["pledges"] if p["outcome"] == "violated" and p["seat"] in pool]
    return top({seat: broken.count(seat) for seat in set(broken)})


def actor_award(raw: dict[str, Any], pool: set[int]) -> Result:
    """Claims about one's own cards that both judges found untrue; at least one."""
    lies = [
        seat
        for verdict in raw.get("verdicts", [])
        if verdict["verdict"] == "false_claim"
        for seat in verdict["seats"]
        if seat in pool
    ]
    return top({seat: lies.count(seat) for seat in set(lies)})


def keeper_award(raw: dict[str, Any], pool: set[int]) -> Result:
    """Seats that made two pledges or more and kept all of them: the one with most pledges."""
    outcomes: dict[int, list[str]] = {}
    for pledge in raw["pledges"]:
        if pledge["seat"] in pool and pledge["outcome"] != "void":
            outcomes.setdefault(pledge["seat"], []).append(pledge["outcome"])
    return top({s: len(o) for s, o in outcomes.items() if len(o) >= 2 and set(o) == {"kept"}})


def mission_award(raw: dict[str, Any], pool: set[int]) -> Result:
    """The earliest finished secret mission, by the lot on which it was finished. No figure."""
    done = {}
    for mission in raw["reveal"]["missions"]:
        if mission["seat"] in pool and mission["completed"]:
            lots = sorted(mission["won_lots"])
            done[mission["seat"]] = (
                mission["target_lot"] if mission["kind"] == "shill" else lots[mission["need"] - 1]
            )
    if not done:
        return [], None
    first = min(done.values())
    return sorted(seat for seat, lot in done.items() if lot == first), None


# ------------------------------------------------------------ the figures of every seat


@dataclass(frozen=True)
class Play:
    """One decided lot of a file as the rules read it.

    ``budgets`` are the budgets before the lot, ``bids`` the number each asked seat played with
    (the bid, the final English exit price, the Dutch grab price; 0 for an abstention), and
    ``abstained`` the asked seats whose every action in the lot is an abstention.
    """

    lot: dict[str, Any]
    tier: int
    asked: tuple[int, ...]
    budgets: dict[int, int]
    bids: dict[int, int]
    abstained: frozenset[int]
    setup: LotSetup


def plays(raw: dict[str, Any]) -> list[Play]:
    """Every lot of the file, with what the rules of the awards need (see :class:`Play`)."""
    tiers, found = tiers_of(raw), []
    for lot, setup in lot_setups(raw, order=tie_orders(raw)[0]):
        statuses: dict[int, set[str]] = {}
        for action in lot["actions"]:
            statuses.setdefault(action["seat"], set()).add(action["status"])
        asked = tuple(sorted(lot["active"]))
        found.append(
            Play(
                lot=lot,
                tier=tiers[lot["lot"]],
                asked=asked,
                budgets=dict(setup.budgets),
                bids=dict(setup.bids),
                abstained=frozenset(s for s in asked if statuses.get(s) == {"abstain"}),
                setup=setup,
            )
        )
    return found


@dataclass
class Columns:
    """The reference columns of one seat (the names are those of ``SeatStats``)."""

    textbook_bids: int = 0
    textbook_deviation_milli: int | None = None
    valid_guesses: int = 0
    mind_error_milli: int | None = None
    judgable_lots: int = 0
    consistency_milli: int | None = None

    def as_stats(self) -> dict[str, int | None]:
        return dataclasses.asdict(self)


def columns(raw: dict[str, Any]) -> dict[int, Columns]:
    """The columns of every seat, from the bids, the guesses, the plans and the hole cards.

    * a valid bid (textbook): an ordinary lot (not a blind box) in which the seat was asked, did
      not abstain and has a textbook bid; its distance is ``|bid - textbook| / tier``;
    * a valid guess: a mind-reading guess in the file; its error is ``|guess - truth|`` and the
      mean is taken in budget spans (the width of the public range, at least 1);
    * a judgable lot: the seat has a plan, was asked, did not abstain, has a positive number of
      its own (its value, or its appraisal in a blind box) and has at least half of it in budget;
      the seat followed its plan there when it planned two stars or more and bid at least half
      of the number, or planned fewer and bid less than that.

    Every ratio is in whole thousandths, rounded down.
    """
    night, reveal = raw["night"], raw["reveal"]
    span = max(1, night["budget_hi"] - night["budget_lo"])
    seats = [seat["seat"] for seat in raw["seats"]]
    plans = {p["seat"]: p["priorities"] for p in reveal["plans"]}
    distance = {seat: Fraction(0) for seat in seats}
    bids, judged, followed = Counter[int](), Counter[int](), Counter[int]()
    for play in plays(raw):
        number, blind = play.lot["lot"], play.lot["format"] == "blind_box"
        for seat in play.asked:
            if seat in play.abstained or seat not in play.bids:
                continue
            ref = play.lot["overlay"]["ref_bids"].get(str(seat))
            if not blind and ref is not None:
                distance[seat] += Fraction(abs(play.bids[seat] - ref), play.tier)
                bids[seat] += 1
            own = (reveal["signals"] if blind else reveal["values"]).get(str(seat), {})
            own_number = own.get(str(number))
            affordable = own_number is not None and 2 * play.budgets[seat] >= own_number
            if seat in plans and own_number and affordable:
                judged[seat] += 1
                serious = 2 * play.bids[seat] >= own_number
                followed[seat] += int((plans[seat][number - 1] >= HIGH_PLAN_STARS) == serious)
    errors: dict[int, list[int]] = {}
    for guess in reveal["guesses"]:
        errors.setdefault(guess["seat"], []).append(abs(guess["guess"] - guess["truth"]))
    found = {}
    for seat in seats:
        mine = errors.get(seat, [])
        found[seat] = Columns(
            textbook_bids=bids[seat],
            textbook_deviation_milli=(
                distance[seat].numerator * 1000 // (distance[seat].denominator * bids[seat])
                if bids[seat]
                else None
            ),
            valid_guesses=len(mine),
            mind_error_milli=sum(mine) * 1000 // (len(mine) * span) if mine else None,
            judgable_lots=judged[seat],
            consistency_milli=followed[seat] * 1000 // judged[seat] if judged[seat] else None,
        )
    return found


def textbook_award(raw: dict[str, Any], pool: set[int]) -> Result:
    """The least mean distance to the textbook bid, at least six valid bids."""
    cols = columns(raw)
    return top(
        {
            seat: cols[seat].textbook_deviation_milli
            for seat in pool
            if cols[seat].textbook_bids >= MIN_VALID_BIDS
            and cols[seat].textbook_deviation_milli is not None
        },
        lowest=True,
    )


def mind_award(raw: dict[str, Any], pool: set[int]) -> Result:
    """The least mean error in budget spans, at least eight valid guesses."""
    cols = columns(raw)
    return top(
        {
            seat: cols[seat].mind_error_milli
            for seat in pool
            if cols[seat].valid_guesses >= MIN_VALID_GUESSES
            and cols[seat].mind_error_milli is not None
        },
        lowest=True,
    )


def consistent_award(raw: dict[str, Any], pool: set[int]) -> Result:
    """The highest share of judgable lots in which the plan was followed, at least ten lots."""
    cols = columns(raw)
    return top(
        {
            seat: cols[seat].consistency_milli
            for seat in pool
            if cols[seat].judgable_lots >= MIN_JUDGED_LOTS
            and cols[seat].consistency_milli is not None
        }
    )


def expected_awards(raw: dict[str, Any], pool: set[int]) -> dict[str, Result]:
    """Every award for the seats of `pool`, with the figure that decided it."""
    return {
        "most_textbook": textbook_award(raw, pool),
        "winners_curse": curse_award(raw, pool),
        "tilt": tilt_award(raw, pool),
        "bargain": bargain_award(raw, pool),
        "backstabber": backstab_award(raw, pool),
        "best_actor": actor_award(raw, pool),
        "keeper": keeper_award(raw, pool),
        "mission_master": mission_award(raw, pool),
        "mind_reader": mind_award(raw, pool),
        "consistent": consistent_award(raw, pool),
    }


CARRIERS = [path for path in PATHS if load(path)["awards"]]
BY_HAND = [path for path in PATHS if path.name in HAND_WRITTEN]
REAL = [path for path in PATHS if path.name == SAMPLE]


def viewer_basis(file: str) -> dict[str, str]:
    """The ``BASIS`` table of ``viewer/lib/awards.js`` or ``viewer/lib/highlights.js``."""
    source = (VIEWER / "lib" / file).read_text(encoding="utf-8")
    block = re.search(r"const BASIS = Object\.freeze\(\{(.*?)\}\);", source, flags=re.S)
    assert block, f"BASIS not found in {file}"
    return dict(re.findall(r"(\w+): '(public|private)'", block.group(1)))


@pytest.mark.parametrize("path", PATHS, ids=lambda p: p.name)
def test_a_replay_carries_the_awards_and_the_highlights_or_none_of_them(path: Path) -> None:
    raw = load(path)
    if raw["awards"]:
        assert raw["bots_reference"] and raw["highlights"], "the show-layer parts come together"
    else:
        assert raw["bots_reference"] == [] and raw["highlights"] == []


def test_the_hand_written_nights_carry_the_awards_so_that_the_award_checks_run() -> None:
    assert [path.name for path in CARRIERS if path.name in HAND_WRITTEN] == sorted(HAND_WRITTEN)


def test_the_real_sample_carries_the_awards_the_highlights_and_the_findings() -> None:
    assert [path.name for path in CARRIERS] == sorted([*HAND_WRITTEN, SAMPLE])
    raw = by_name(SAMPLE)
    assert len(raw["awards"]) == 10 and len(raw["bots_reference"]) == 4
    assert raw["highlights"] and raw["verdicts"] and len(raw["judges"]) == 2


@pytest.mark.parametrize("path", CARRIERS, ids=lambda p: p.name)
def test_awards_are_the_award_rules_applied_to_the_file(path: Path) -> None:
    raw = load(path)
    expected = expected_awards(raw, pool_of(raw, MODEL_KINDS))
    assert sorted(entry["award"] for entry in raw["awards"]) == sorted(AWARD_KEYS)
    for entry in raw["awards"]:
        key = entry["award"]
        winners, figure = expected[key]
        assert entry.get("winners", []) == winners, key
        assert entry.get("vacant", False) is (not winners), key
        assert entry.get("metric") == figure, key
        unit = AWARD_UNITS[key] if winners and figure is not None else "none"
        assert entry.get("metric_unit", "none") == unit, key


@pytest.mark.parametrize("path", CARRIERS, ids=lambda p: p.name)
def test_only_model_seats_win_awards_and_the_bots_are_listed_apart(path: Path) -> None:
    raw = load(path)
    models, bots = pool_of(raw, MODEL_KINDS), pool_of(raw, ("bot",))
    assert models and bots and not models & bots
    for entry in raw["awards"]:
        assert set(entry.get("winners", [])) <= models, entry["award"]
    assert [entry["award"] for entry in raw["bots_reference"]] == list(BOT_AWARDS)
    expected = expected_awards(raw, bots)
    for entry in raw["bots_reference"]:
        winners, figure = expected[entry["award"]]
        assert entry.get("winners", []) == winners, entry["award"]
        assert entry.get("metric") == figure, entry["award"]
        assert set(winners) <= bots, entry["award"]
        assert entry.get("vacant", False) is (not winners), entry["award"]


@pytest.mark.parametrize("path", BY_HAND, ids=lambda p: p.name)
def test_every_hand_written_night_has_a_vacant_award_and_a_tie(path: Path) -> None:
    awards = load(path)["awards"]
    assert any(entry.get("vacant") for entry in awards), "an award nobody reached the limit for"
    assert any(len(entry.get("winners", [])) >= 2 for entry in awards), "seats that win together"


def test_the_nights_show_a_bot_reference_that_is_given_and_one_that_is_vacant() -> None:
    entries = [e for p in PATHS for e in load(p)["bots_reference"]]
    assert any(e.get("vacant") for e in entries) and any(e.get("winners") for e in entries)


@pytest.mark.parametrize("path", BY_HAND, ids=lambda p: p.name)
def test_the_hand_written_nights_are_too_short_for_the_three_awards_that_need_many_observations(
    path: Path,
) -> None:
    """So their rebuilt awards are vacant, and the check above is not hiding a missing rule."""
    raw = load(path)
    for cols in columns(raw).values():
        assert cols.textbook_bids < MIN_VALID_BIDS
        assert cols.valid_guesses < MIN_VALID_GUESSES
        assert cols.judgable_lots < MIN_JUDGED_LOTS


def test_the_real_sample_reaches_the_limits_of_the_three_awards_and_they_have_winners() -> None:
    raw = by_name(SAMPLE)
    models = pool_of(raw, MODEL_KINDS)
    cols = columns(raw)
    assert max(cols[s].textbook_bids for s in models) >= MIN_VALID_BIDS
    assert max(cols[s].valid_guesses for s in models) >= MIN_VALID_GUESSES
    assert max(cols[s].judgable_lots for s in models) >= MIN_JUDGED_LOTS
    awards = {entry["award"]: entry for entry in raw["awards"]}
    for key in ("most_textbook", "mind_reader", "consistent"):
        assert awards[key].get("winners") and not awards[key].get("vacant", False), key
        assert awards[key]["metric_unit"] == "milli"


@pytest.mark.parametrize("path", REAL, ids=lambda p: p.name)
def test_the_columns_of_the_scores_are_the_figures_the_awards_rank(path: Path) -> None:
    raw = load(path)
    cols = columns(raw)
    for score in raw["scores"]:
        stats = score["stats"]
        mine = cols[score["seat"]].as_stats()
        assert {name: stats[name] for name in mine} == mine, f"seat {score['seat']}"
    assert any(c.mind_error_milli is not None for c in cols.values())
    assert any(c.consistency_milli is not None for c in cols.values())
    assert any(c.textbook_deviation_milli is not None for c in cols.values())


@pytest.mark.parametrize("path", CARRIERS, ids=lambda p: p.name)
def test_every_award_says_what_it_rests_on_and_the_page_holds_back_by_the_same_table(
    path: Path,
) -> None:
    raw = load(path)
    table = viewer_basis("awards.js")
    assert set(table) == set(AWARD_KEYS)
    for entry in [*raw["awards"], *raw["bots_reference"]]:
        if path.name in HAND_WRITTEN:
            assert entry.get("basis") in (None, table[entry["award"]]), entry["award"]
        else:
            assert entry["basis"] == table[entry["award"]], entry["award"]
    if path.name not in HAND_WRITTEN:
        assert {e["basis"] for e in raw["awards"]} == {"public", "private"}


# A highlight is a moment of one lot. These checks say what must be true of the file for each
# category, and that the moment (the range of event numbers) lies inside the lot.


SEQUENCE_KEYS = ("seq", "first_seq", "last_seq", "evidence_seqs", "talk_seqs", "hammer_seq")


def integers_in(value: Any) -> Iterator[int]:
    """The whole numbers of a value, in numbers and in digits of text, without event numbers."""
    if isinstance(value, bool):
        return
    if isinstance(value, int):
        yield value
    elif isinstance(value, str):
        yield from (int(digits) for digits in re.findall(r"\d+", value))
    elif isinstance(value, list):
        for item in value:
            yield from integers_in(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key not in SEQUENCE_KEYS:
                yield from integers_in(key)
                yield from integers_in(item)


def differences(raw: dict[str, Any]) -> set[int]:
    """Numbers a sentence may state that are the difference of two numbers of the file: what a
    winner's value was above or below the price, how far a guess was from the budget."""
    values, found = raw["reveal"]["values"], set()
    for lot in raw["timeline"]:
        hammer = lot["hammer"]
        if hammer["winner"] is None:
            continue
        worth = hammer.get("true_value") or values[str(hammer["winner"])][str(lot["lot"])]
        found.add(worth - hammer["price"])
    found |= {abs(g["guess"] - g["truth"]) for g in raw["reveal"]["guesses"]}
    return found | {abs(n) for n in found}


def assert_highlight_happened(raw: dict[str, Any], light: dict[str, Any]) -> None:
    lot = next(entry for entry in raw["timeline"] if entry["lot"] == light["lot"])
    hammer, seats, reveal = lot["hammer"], set(light["seats"]), raw["reveal"]
    number = lot["lot"]
    match light["category"]:
        case "winners_curse":
            assert lot["format"] == "blind_box" and hammer["winner"] in seats
            assert hammer["price"] > hammer["true_value"]
        case "shill_stuck":
            assert any(
                m["seat"] in seats and m["kind"] == "shill" and m["target_lot"] == number
                and m["buyer"] == m["seat"] and not m["completed"]
                for m in reveal["missions"]
            )  # fmt: skip
        case "backstab":
            assert any(
                p["pledge"]["lot"] == number and p["seat"] in seats and p["outcome"] == "violated"
                for p in raw["pledges"]
            )
        case "claim_vs_hand":
            assert any(
                v["verdict"] == "false_claim" and number in v["lots"] and seats & set(v["seats"])
                for v in raw.get("verdicts", [])
            )
        case "plan_contradiction":
            (seat,) = seats
            plan = next(p for p in reveal["plans"] if p["seat"] == seat)
            stars = plan["priorities"][number - 1]
            if hammer["winner"] == seat:
                assert stars <= 1, "a lot that the seat won although it planned little for it"
            else:
                assert stars == 3 and seat in lot["active"], (
                    "or one it planned much for and did not bid"
                )
                mine = [a for a in lot["actions"] if a["seat"] == seat]
                assert all(a["status"] == "abstain" or a["value"] == 0 for a in mine) or not mine
        case "all_pay_tilt":
            assert lot["format"] == "all_pay"
            assert all(str(s) in hammer["payments"] and s != hammer["winner"] for s in seats)
        case "bargain":
            (seat,) = seats
            assert hammer["winner"] == seat
            blind = lot["format"] == "blind_box"
            worth = hammer["true_value"] if blind else reveal["values"][str(seat)][str(number)]
            assert worth - hammer["payments"][str(seat)] > 0
        case "mindread_miss":
            worst = max(g["abs_error"] for g in reveal["guesses"])
            assert any(
                g["lot"] == number and g["seat"] in seats and g["abs_error"] <= worst
                for g in reveal["guesses"]
            )
        case other:
            raise AssertionError(f"no check for the category {other}")


@pytest.mark.parametrize("path", CARRIERS, ids=lambda p: p.name)
def test_highlights_point_at_a_lot_and_a_moment_that_exist(path: Path) -> None:
    raw = load(path)
    lots = {lot["lot"]: lot for lot in raw["timeline"]}
    seats = {seat["seat"] for seat in raw["seats"]}
    lights = raw["highlights"]
    assert [light["id"] for light in lights] == list(range(1, len(lights) + 1))
    assert len({light["lot"] for light in lights}) >= 2, "the highlights are spread over lots"
    parts = ("catalog", "timeline", "reveal", "scores", "pledges", "verdicts", "awards")
    facts = set(integers_in({key: raw[key] for key in parts})) | differences(raw)
    for light in lights:
        lot = lots[light["lot"]]
        assert lot["first_seq"] <= light["first_seq"] <= light["last_seq"] <= lot["last_seq"]
        assert light["seats"] and set(light["seats"]) <= seats
        assert 0 <= light["score"] <= 10
        assert set(integers_in(light["text"])) <= facts, f"a number of highlight {light['id']}"
        assert_highlight_happened(raw, light)


def test_the_nights_show_every_category_and_a_tie_in_the_score() -> None:
    lights = [light for path in PATHS for light in load(path)["highlights"]]
    assert {light["category"] for light in lights} == set(get_args(HighlightCategory))
    for path in BY_HAND:
        scores = [light["score"] for light in load(path)["highlights"]]
        assert len(scores) != len(set(scores)), f"{path.name}: two highlights with the same score"


@pytest.mark.parametrize("path", CARRIERS, ids=lambda p: p.name)
def test_every_highlight_says_what_it_rests_on_and_the_page_holds_back_by_the_same_table(
    path: Path,
) -> None:
    raw = load(path)
    table = viewer_basis("highlights.js")
    assert set(table) == set(get_args(HighlightCategory))
    for light in raw["highlights"]:
        if path.name in HAND_WRITTEN:
            assert light.get("basis") in (None, table[light["category"]]), light["id"]
        else:
            assert light["basis"] == table[light["category"]], light["id"]
    if path.name not in HAND_WRITTEN:
        assert {light["basis"] for light in raw["highlights"]} == {"public", "private"}


# ----------------------------------------------------------- the highlights, rebuilt
#
# For the real file the highlights are rebuilt from the facts in the file by the rules of the
# show layer (base score, rungs of the amount in thousandths of the tier or the budget range,
# twists; the three best of each category; the order of the night; the place in the log) and must
# be the file's highlights, one for one. The hand-written files have scores that were chosen by
# hand, so they are only checked for happening (above).

BASE = {
    "winners_curse": 4, "shill_stuck": 8, "backstab": 4, "claim_vs_hand": 5,
    "plan_contradiction": 3, "all_pay_tilt": 3, "bargain": 3, "mindread_miss": 3,
}  # fmt: skip
CURSE_RUNGS, TILT_RUNGS, BARGAIN_RUNGS = (50, 100, 200), (250, 500, 1000), (350, 500, 750)
PLAN_RUNGS, MIND_RUNGS, CLAIM_RUNGS = (250, 500), (750, 1000, 1500), (250, 500, 1000)
PER_CATEGORY = 3
COUNTED = ("ok", "clamped", "kept_previous")


def rungs(amount: int, unit: int, ladder: tuple[int, ...]) -> int:
    """How many thresholds (thousandths of the unit) the amount reaches."""
    return sum(1 for rung in ladder if amount * 1000 >= rung * max(1, unit))


@dataclass(frozen=True)
class Found:
    """A moment found in a file: where it is, who it is about and what it scores."""

    category: str
    lot: int
    seat: int
    score: int
    first_seq: int
    last_seq: int

    @property
    def order(self) -> tuple[int, int, int, int, int]:
        return (self.lot, self.first_seq, self.last_seq, ORDER.index(self.category), self.seat)


ORDER = list(get_args(HighlightCategory))


def positive_numbers(lot: dict[str, Any], seat: int) -> list[int]:
    return [
        a["value"]
        for a in lot["actions"]
        if a["seat"] == seat and a["status"] in COUNTED and (a.get("value") or 0) > 0
    ]


def intent_of(record: dict[str, Any], verdicts: list[dict[str, Any]]) -> str:
    """The intent behind a broken pledge: its own record, else a verdict about it."""
    if record["intent"] != "unknown":
        return record["intent"]
    for verdict in verdicts:
        if verdict["verdict"] != "pledge_violation" or record["seat"] not in verdict["seats"]:
            continue
        about = (
            record["seq"] in verdict["evidence_seqs"]
            if verdict["evidence_seqs"]
            else record["pledge"]["lot"] in verdict["lots"]
        )
        if about and verdict.get("intent") is not None:
            return verdict["intent"]
    return "unknown"


def moments_of(raw: dict[str, Any]) -> list[Found]:
    """Every moment the rules find in the file, scored and placed."""
    night, reveal = raw["night"], raw["reveal"]
    span = night["budget_hi"] - night["budget_lo"]
    config = load_config(REPO / "configs" / "night_mock.yaml")
    miss = max(1, config.night.budget.c // 2)  # how far a guess must be off to be a miss
    lots = {lot["lot"]: lot for lot in raw["timeline"]}
    plans = {p["seat"]: p for p in reveal["plans"]}
    verdicts = raw["verdicts"]
    found: list[Found] = []

    def place(lot: int, first: int | None, last: int | None) -> tuple[int, int]:
        lo, hi = lots[lot]["first_seq"], lots[lot]["last_seq"]
        begin = lo if first is None else min(max(first, lo), hi)
        return begin, hi if last is None else min(max(last, begin), hi)

    def first_bid(lot: int, seat: int) -> int:
        return min(a["seq"] for a in lots[lot]["actions"] if a["seat"] == seat)

    def add(category: str, lot: int, seat: int, score: int, first: int | None, last: int | None):
        begin, end = place(lot, first, last)
        found.append(Found(category, lot, seat, max(0, min(10, score)), begin, end))

    for play in plays(raw):
        lot, tier = play.lot, play.tier
        number, hammer = lot["lot"], lot["hammer"]
        winner, price = hammer["winner"], hammer["price"]
        values = reveal["values"]
        # a blind box that sold for more than its true value
        if winner is not None and hammer["true_value"] is not None and price > hammer["true_value"]:
            gap = price - hammer["true_value"]
            high = blind_bounds(tier, config.night.blind).high
            score = BASE["winners_curse"] + rungs(gap, tier, CURSE_RUNGS) + 2 * int(price > high)
            add("winners_curse", number, winner, score, first_bid(number, winner), None)
        # a shill that bought its own target lot
        for mission in reveal["missions"]:
            bought_its_own = mission["kind"] == "shill" and mission["target_lot"] == number
            if bought_its_own and winner == mission["seat"]:
                own = values.get(str(winner), {}).get(str(number))
                score = BASE["shill_stuck"] + int(price >= mission["threshold_price"])
                score += int(own is not None and price > own)
                add("shill_stuck", number, winner, score, None, None)
        # a pledge about this lot that was broken
        for record in raw["pledges"]:
            if record["pledge"]["lot"] != number or record["outcome"] != "violated":
                continue
            seat, pledge = record["seat"], record["pledge"]
            top_bid = max(positive_numbers(lot, seat), default=0)
            if pledge["kind"] == "let_win":
                size = price
            else:
                size = top_bid - (pledge["amount"] or 0) if pledge["kind"] == "max_bid" else top_bid
            score = BASE["backstab"] + 2 * int(winner == seat)
            score += 2 * int(intent_of(record, verdicts) == "deliberate")
            score += int(size * 1000 >= 250 * tier)
            add("backstab", number, seat, score, record["seq"], None)
        # a plan that the seat contradicted
        planned = []
        for seat in sorted(plans):
            stars = plans[seat]["priorities"][number - 1]
            budget = plans[seat]["budget_plan"][number - 1]
            asked = seat in play.asked
            nothing = asked and (seat in play.abstained or play.bids.get(seat) == 0)
            if stars <= 1 and winner == seat:
                score = BASE["plan_contradiction"] + (2 if stars == 0 else 1)
                score += rungs(price, tier, PLAN_RUNGS) + int(price > budget)
            elif stars == 3 and nothing:
                score = BASE["plan_contradiction"] + int(
                    seat not in play.abstained and winner not in (None, seat) and price <= budget
                )
            else:
                continue
            planned.append((seat, score))
        for seat, score in planned[:2]:
            add("plan_contradiction", number, seat, score, lot["talk_seqs"][str(seat)], None)
        # the loser of an all-pay lot who paid the most for nothing
        if lot["format"] == "all_pay":
            losers = [(int(s), paid) for s, paid in hammer["payments"].items() if int(s) != winner]
            losers = [(seat, paid) for seat, paid in losers if paid > 0]
            if losers:
                seat, paid = max(losers, key=lambda pair: (pair[1], -pair[0]))
                if paid * 10 >= tier:
                    score = BASE["all_pay_tilt"] + rungs(paid, tier, TILT_RUNGS)
                    behind = price - paid
                    score += int(behind >= 0 and behind * 1000 <= 50 * tier)
                    add("all_pay_tilt", number, seat, score, first_bid(number, seat), None)
        # a winner of a private-value lot who paid far less than it was worth
        if winner is not None and lot["format"] != "blind_box":
            gain = values[str(winner)][str(number)] - price
            if gain * 5 >= tier:
                score = BASE["bargain"] + rungs(gain, tier, BARGAIN_RUNGS)
                add("bargain", number, winner, score, first_bid(number, winner), None)
        # the worst miss of the lot in a mind-reading guess
        worst = None
        for guess in sorted(reveal["guesses"], key=lambda g: g["seat"]):
            error = abs(guess["guess"] - guess["truth"])
            if guess["lot"] == number and error >= miss and (worst is None or error > worst[1]):
                worst = (guess, error)
        if worst is not None:
            guess, error = worst
            score = BASE["mindread_miss"] + rungs(error, span, MIND_RUNGS)
            score += int(
                guess["guess"] * 3 <= guess["truth"] or guess["truth"] * 3 <= guess["guess"]
            )
            at = lot["talk_seqs"][str(guess["seat"])] + 1  # the reading comes right after the line
            add("mindread_miss", number, guess["seat"], score, at, at)
    # a claim that both judges found and the hole cards contradict
    for verdict in verdicts:
        if verdict["verdict"] != "false_claim":
            continue
        number, evidence = min(verdict["lots"]), sorted(verdict["evidence_seqs"])
        won = lots[number]["hammer"]["winner"] in verdict["seats"]
        score = BASE["claim_vs_hand"] + 2 * int(verdict.get("intent") == "deliberate") + int(won)
        score += rungs(verdict["gap_milli"] or 0, 1000, CLAIM_RUNGS)
        for seat in sorted(set(verdict["seats"]))[:1]:
            add("claim_vs_hand", number, seat, score, evidence[0], evidence[-1])
    return found


def chosen_highlights(raw: dict[str, Any]) -> list[Found]:
    """The best three of each category, in the order of the night."""
    moments = moments_of(raw)
    kept: list[Found] = []
    for category in ORDER:
        mine = sorted(
            (m for m in moments if m.category == category), key=lambda m: (-m.score, m.order)
        )
        kept.extend(mine[:PER_CATEGORY])
    return sorted(kept, key=lambda m: m.order)


@pytest.mark.parametrize("path", REAL, ids=lambda p: p.name)
def test_the_highlights_are_the_rules_applied_to_the_file(path: Path) -> None:
    raw = load(path)
    expected = chosen_highlights(raw)
    got = [
        (h["id"], h["category"], h["lot"], h["seats"], h["score"], h["first_seq"], h["last_seq"])
        for h in raw["highlights"]
    ]
    want = [
        (n, m.category, m.lot, [m.seat], m.score, m.first_seq, m.last_seq)
        for n, m in enumerate(expected, start=1)
    ]
    assert got == want


@pytest.mark.parametrize("path", REAL, ids=lambda p: p.name)
def test_the_real_highlights_cover_what_the_sample_can_show(path: Path) -> None:
    raw = load(path)
    categories = {light["category"] for light in raw["highlights"]}
    assert {
        "backstab",
        "bargain",
        "plan_contradiction",
        "all_pay_tilt",
        "mindread_miss",
    } <= categories
    assert "winners_curse" in categories
    assert len({light["score"] for light in raw["highlights"]}) >= 4, "the scores differ"
    per_category = Counter(light["category"] for light in raw["highlights"])
    assert max(per_category.values()) <= PER_CATEGORY


@pytest.mark.parametrize("path", REAL, ids=lambda p: p.name)
def test_a_highlight_names_the_seat_it_is_about(path: Path) -> None:
    raw = load(path)
    names = {seat["seat"]: seat["codename"] for seat in raw["seats"]}
    for light in raw["highlights"]:
        (seat,) = light["seats"]
        assert f"{seat} 号{names[seat]}" in light["text"], light["id"]


def positive_bids(lot: dict[str, Any], seat: int) -> list[int]:
    """The positive numbers of a seat in a lot: bids, exit prices and grab prices that counted.

    A kept English exit price counts and an abstention does not. A pledge is tested on these.
    """
    counted = ("ok", "clamped", "kept_previous")
    return [
        a["value"]
        for a in lot["actions"]
        if a["seat"] == seat and a["status"] in counted and (a.get("value") or 0) > 0
    ]


@pytest.mark.parametrize("path", PATHS, ids=lambda p: p.name)
def test_pledges_agree_with_the_bids_of_the_file(path: Path) -> None:
    raw = load(path)
    lots = {lot["lot"]: lot for lot in raw["timeline"]}
    for record in raw["pledges"]:
        made, pledge = lots[record["lot"]], record["pledge"]
        assert made["first_seq"] < record["seq"] < made["last_seq"]
        spoken = [t for t in made["talk"] if t["seat"] == record["seat"]]
        assert [t.get("pledge") for t in spoken] == [pledge], "spoken by that seat in that lot"
        if made.get("talk_seqs"):
            assert made["talk_seqs"][str(record["seat"])] == record["seq"], "at that very line"
        # The pledge is about its own lot, which need not be the lot it was made in.
        target = lots[pledge["lot"]]
        if record["seat"] not in target["active"]:
            assert record["outcome"] == "void", "a seat that was not asked cannot break a pledge"
            continue
        bids = positive_bids(target, record["seat"])
        if pledge["kind"] == "max_bid":
            broke = max(bids, default=0) > pledge["amount"]
        elif pledge["kind"] == "no_bid":
            broke = bool(bids)
        else:
            assert pledge["kind"] == "let_win"
            broke = target["hammer"]["winner"] == record["seat"]
        assert record["outcome"] == ("violated" if broke else "kept"), record


@pytest.mark.parametrize("path", PATHS, ids=lambda p: p.name)
def test_the_verdicts_of_a_file_are_about_lots_and_words_that_are_there(path: Path) -> None:
    raw = load(path)
    lots = {lot["lot"]: lot for lot in raw["timeline"]}
    for verdict in raw.get("verdicts", []):
        assert verdict["lots"] == sorted(set(verdict["lots"])) and set(verdict["lots"]) <= set(lots)
        assert set(verdict["seats"]) <= {seat["seat"] for seat in raw["seats"]}
        collusion = verdict["verdict"].startswith("collusion")
        # a collusion verdict also cites the lines of the lot before a lot it lists
        about = list(lots) if collusion else verdict["lots"]
        ranges = [(lots[n]["first_seq"], lots[n]["last_seq"]) for n in about]
        assert all(any(a <= seq <= b for a, b in ranges) for seq in verdict["evidence_seqs"])
        said = " ".join(t["text"] for n in about for t in lots[n]["talk"])
        assert all(quote in said for quote in verdict["quotes"]), verdict
        if verdict["verdict"] == "pledge_violation":
            (seat,) = verdict["seats"]
            assert any(
                r["seat"] == seat and r["outcome"] == "violated" and r["lot"] in verdict["lots"]
                for r in raw["pledges"]
            )
        elif verdict["verdict"] == "false_claim":
            (seat,), (number,) = verdict["seats"], verdict["lots"]
            for judge in raw["judges"]:
                assert any(
                    e["seat"] == seat and e["lot"] == number and e["kind"] == "claim"
                    and not e["ambiguous"] and e["quote"] in verdict["quotes"]
                    for e in judge["extractions"]
                ), "both judges found the claim, and neither was unsure"  # fmt: skip
        else:
            assert verdict["verdict"] in ("collusion_red", "collusion_suspect")


@pytest.mark.parametrize("path", PATHS, ids=lambda p: p.name)
def test_the_extractions_of_the_judges_are_words_that_were_said(path: Path) -> None:
    raw = load(path)
    lots = {lot["lot"]: lot for lot in raw["timeline"]}
    seats = {seat["seat"] for seat in raw["seats"]}
    players = pool_of(raw, MODEL_KINDS)
    for judge in raw.get("judges", []):
        for item in judge["extractions"]:
            said = [t["text"] for t in lots[item["lot"]]["talk"] if t["seat"] == item["seat"]]
            assert any(item["quote"] in text for text in said), item
            if item["kind"] == "claim":
                assert item.get("promise_class") is None and item.get("partner") is None
            else:
                partner = item.get("partner")
                assert partner is None or (partner in seats and partner != item["seat"])
                assert partner is None or item.get("promise_class") == "collusion"
                assert item.get("text_value") is None or path.name in HAND_WRITTEN
        for guess in judge["mission_guesses"]:
            assert guess["seat"] in players
            assert (guess["kind"] == "collector") == (guess.get("tag") is not None)
            assert (guess["kind"] == "shill") == (guess.get("lot") is not None)


@pytest.mark.parametrize("path", PATHS, ids=lambda p: p.name)
def test_the_accuracy_of_the_judges_is_their_guesses_held_against_the_missions(path: Path) -> None:
    raw = load(path)
    players = pool_of(raw, MODEL_KINDS)
    held = {m["seat"]: m for m in raw["reveal"]["missions"] if m["seat"] in players}
    rows = {row["judge"]: row for row in raw.get("judge_accuracy", [])}
    assert set(rows) == {j["judge"] for j in raw.get("judges", [])}
    for judge in raw.get("judges", []):
        guesses = [g for g in judge["mission_guesses"] if g["seat"] in players]
        right = 0
        for g in guesses:
            mission = held.get(g["seat"])
            if mission is None or mission["kind"] != g["kind"]:
                continue
            right += int(
                g.get("tag") == mission["tag"]
                if g["kind"] == "collector"
                else g.get("lot") == mission["target_lot"]
            )
        row = rows[judge["judge"]]
        assert (row["vendor"], row["guesses"], row["correct"], row["targets"]) == (
            judge["vendor"], len(guesses), right, len(held)
        )  # fmt: skip


@pytest.mark.parametrize("path", PATHS, ids=lambda p: p.name)
def test_the_judges_that_did_not_deliver_are_absent_and_not_present(path: Path) -> None:
    raw = load(path)
    gave = {j["judge"] for j in raw.get("judges", [])}
    absent = raw.get("judges_absent", [])
    assert not gave & {a["judge"] for a in absent}
    assert all(
        a["reason"] in ("unreadable", "timeout", "refusal", "budget_aborted", "other")
        for a in absent
    )
    assert gave | {a["judge"] for a in absent} <= {0, 1, 2, 3}


# ------------------------------------------------------- the findings, rebuilt for the real file
#
# For the real sample the findings are rebuilt from the file by the rules of the show layer and
# must be the file's verdicts, one for one:
#
#   pledge_violation  one for each broken pledge, in the order the pledges were made. It names the
#                     seat, the lot the pledge was made in and the lot it was about, the line and
#                     the hammer as evidence, and the words of the line. The intent is unknown.
#   false_claim       a claim that both judges found, neither unsure, and that the hole cards
#                     contradict: a budget more than 10% away from what the seat had left and from
#                     what it began with, a value more than 15% of the tier away, a mission kind
#                     or tag that is not its own. It says how far off it was (``gap_milli``).
#   collusion         two sides. The chat: both judges found a promise of the class
#                     ``collusion`` from the same seat in the same lot, neither unsure, with
#                     quotes that overlap; the seats that made one are the ring, and a lot is
#                     covered by the lot of the promise and the one after it. The bids: the
#                     statistic S = D + A of the lot is strictly above the threshold, where D is
#                     what the all-textbook lot would have taken in less what the lot took in, and
#                     A how far below their textbook bid the ring members who were asked and did
#                     not win stayed, both in thousandths of the tier. A lot with both sides is
#                     red, with one it is suspect. Two red lots make a red card; otherwise any lot
#                     with a side makes a suspect. The verdict holds S of every lot and the
#                     threshold.

THRESHOLD = NULL_THRESHOLD_MILLI  # published: the 95th percentile of S for bots, no ring
MIN_RED = 2  # lots with both sides that a red card needs
TOLERANCE = {"budget": 10, "valuation": 15}  # percent of the budget, percent of the tier
MISSION_WORDS = {"collector", "shill", "none"}
MIN_OVERLAP = 6  # characters of a run that two quotes share


def compact(text: str) -> str:
    """Text without white space, punctuation and symbols, in one case."""
    return "".join(
        char
        for char in text.casefold()
        if not char.isspace() and unicodedata.category(char)[0] not in "PSZ"
    )


def overlap(first: str, second: str) -> bool:
    """Two quotes cover the same words: one holds the other, or they share a run of at least six
    characters that is at least half of the shorter."""
    a, b = compact(first), compact(second)
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    run = SequenceMatcher(None, a, b, autojunk=False).find_longest_match(0, len(a), 0, len(b))
    return run.size >= MIN_OVERLAP and 2 * run.size >= min(len(a), len(b))


def same_item(a: dict[str, Any], b: dict[str, Any], kind: str) -> bool:
    if a["kind"] != kind or b["kind"] != kind or a["ambiguous"] or b["ambiguous"]:
        return False
    if a["lot"] is None or (a["seat"], a["lot"]) != (b["seat"], b["lot"]):
        return False
    if kind == "promise":
        same_class = (a.get("promise_class") or "other") == (b.get("promise_class") or "other")
        return same_class and overlap(a["quote"], b["quote"])
    if a.get("topic") is None or a["topic"] != b.get("topic"):
        return False
    numbers = [x["number"] for x in (a, b) if x.get("number") is not None]
    texts = [(x.get("text_value") or "").strip().casefold() for x in (a, b)]
    texts = [t for t in texts if t]
    if len(set(numbers)) > 1 or len(set(texts)) > 1:
        return False
    return len(numbers) == 2 or len(texts) == 2 or overlap(a["quote"], b["quote"])


def agreed(raw: dict[str, Any], kind: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """The extractions of the two judges that pair up, in the order of the first judge's."""
    if len(raw.get("judges", [])) < 2 or raw.get("judges_absent"):
        return []
    first, second = raw["judges"][0]["extractions"], raw["judges"][1]["extractions"]
    pairs, used = [], set()
    for a in first:
        for index, b in enumerate(second):
            if index not in used and same_item(a, b, kind):
                used.add(index)
                pairs.append((a, b))
                break
    return pairs


def pledge_findings(raw: dict[str, Any]) -> list[dict[str, Any]]:
    lots = {lot["lot"]: lot for lot in raw["timeline"]}
    found = []
    for record in sorted(raw["pledges"], key=lambda r: r["seq"]):
        if record["outcome"] != "violated":
            continue
        target = lots[record["pledge"]["lot"]]
        line = next(t for t in lots[record["lot"]]["talk"] if t["seat"] == record["seat"])
        found.append(
            {
                "verdict": "pledge_violation",
                "seats": [record["seat"]],
                "lots": sorted({record["lot"], record["pledge"]["lot"]}),
                "evidence_seqs": sorted({record["seq"], target["hammer_seq"]}),
                "quotes": [line["text"]] if line["text"] else [],
                "intent": "unknown",
                "detail": record["detail"],
            }
        )
    return found


def claim_findings(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """The claims both judges found that the hole cards contradict, in lot, seat, topic order."""
    reveal, tiers = raw["reveal"], tiers_of(raw)
    left = {int(s): b for s, b in reveal["budgets"].items()}
    before: dict[int, dict[int, int]] = {}
    for lot in raw["timeline"]:
        before[lot["lot"]] = dict(left)
        for seat, amount in lot["hammer"]["payments"].items():
            left[int(seat)] -= amount
    missions = {m["seat"]: m for m in reveal["missions"]}
    players = pool_of(raw, MODEL_KINDS)
    found: dict[tuple[int, int, str, Any], dict[str, Any]] = {}
    for a, b in agreed(raw, "claim"):
        seat, number, topic = a["seat"], a["lot"], a["topic"]
        said = a["number"] if a.get("number") is not None else b.get("number")
        word = (a.get("text_value") or b.get("text_value") or "").strip()
        if seat not in players or topic not in (
            "budget",
            "valuation",
            "mission_kind",
            "mission_tag",
        ):
            continue
        gap: int | None = None
        if topic == "budget" and said is not None:
            readings = {before[number][seat], reveal["budgets"][str(seat)]}
            if any(abs(said - r) * 100 <= 10 * r for r in readings):
                continue
            gap = min(abs(said - r) * 1000 // max(1, r) for r in readings)
        elif topic == "valuation" and said is not None:
            blind = str(number) in reveal.get("true_values", {})
            table = reveal["signals"] if blind else reveal["values"]
            own = table[str(seat)][str(number)]
            if abs(said - own) * 100 <= 15 * tiers[number]:
                continue
            gap = abs(said - own) * 1000 // tiers[number]
        elif topic == "mission_kind" and word.casefold() in MISSION_WORDS:
            mine = missions.get(seat)
            if word.casefold() == (mine["kind"] if mine else "none"):
                continue
        elif topic == "mission_tag" and word:
            mine = missions.get(seat)
            if mine is not None and mine["kind"] == "collector" and mine["tag"] == word:
                continue
        else:
            continue
        key = (number, seat, topic, said if said is not None else word.casefold())
        lot = next(t for t in raw["timeline"] if t["lot"] == number)
        found[key] = {
            "verdict": "false_claim",
            "seats": [seat],
            "lots": [number],
            "evidence_seqs": sorted({lot["talk_seqs"][str(seat)], lot["hammer_seq"]}),
            "quotes": list(dict.fromkeys((a["quote"], b["quote"]))),
            "topic": topic,
            "gap_milli": gap,
            "intent": "unknown",
        }
    return [found[key] for key in sorted(found, key=lambda k: (k[0], k[1], k[2], str(k[3])))]


def statistic_of(play: Play, ring: list[int]) -> int:
    """S = D + A for one lot and a ring (see the comment above), in thousandths of the tier."""
    ref = {int(s): v for s, v in play.lot["overlay"]["ref_bids"].items()}
    counterfactual = sum(play.setup.settle(ref).payments.values())
    observed = sum(play.lot["hammer"]["payments"].values())
    discount = (counterfactual - observed) * 1000 // play.tier
    winner = play.lot["hammer"]["winner"]
    cover = sorted(s for s in set(ring) if s in ref and s != winner)
    short = sum(max(0, ref[s] - play.bids.get(s, 0)) for s in cover)
    return discount + (short * 1000 // (play.tier * len(cover)) if cover else 0)


def collusion_finding(raw: dict[str, Any]) -> dict[str, Any] | None:
    players = pool_of(raw, MODEL_KINDS)
    quotes = sorted(
        (a["lot"], a["seat"], a["quote"])
        for a, _ in agreed(raw, "promise")
        if (a.get("promise_class") or "other") == "collusion" and a["seat"] in players
    )
    ring = sorted({seat for _, seat, _ in quotes})
    spoken = {lot for lot, _, _ in quotes}
    statistic, status = {}, {}
    for play in plays(raw):
        number = play.lot["lot"]
        statistic[number] = statistic_of(play, ring)
        chat = number in spoken or number - 1 in spoken
        over = statistic[number] > THRESHOLD
        if chat or over:
            status[number] = "red" if chat and over else "suspect"
    red = [n for n, s in status.items() if s == "red"]
    if not status:
        return None
    lots = red if len(red) >= MIN_RED else sorted(status)
    lines = {t["lot"]: t for t in raw["timeline"]}
    shown = [(lot, seat, quote) for lot, seat, quote in quotes if lot in lots or lot + 1 in lots]
    seqs = {lines[lot]["talk_seqs"][str(seat)] for lot, seat, _ in shown}
    seqs |= {lines[n]["hammer_seq"] for n in lots}
    return {
        "verdict": "collusion_red" if len(red) >= MIN_RED else "collusion_suspect",
        "seats": ring,
        "lots": lots,
        "evidence_seqs": sorted(seqs),
        "quotes": list(dict.fromkeys(quote for _, _, quote in shown))[:6],
        "intent": None,
        "statistic_milli": {str(n): s for n, s in sorted(statistic.items())},
        "threshold_milli": THRESHOLD,
    }


def comparable(verdict: dict[str, Any]) -> dict[str, Any]:
    """A verdict as the rules decide it: the fields that are not words of the show."""
    keys = (
        "verdict", "seats", "lots", "evidence_seqs", "quotes", "intent", "topic", "gap_milli",
        "statistic_milli", "threshold_milli",
    )  # fmt: skip
    return {key: verdict.get(key) for key in keys}


def with_defaults(verdict: dict[str, Any]) -> dict[str, Any]:
    """The fields a finding does not have are null (the file is canonical JSON)."""
    return {
        "topic": None, "gap_milli": None, "statistic_milli": {}, "threshold_milli": None,
        "intent": None, **verdict,
    }  # fmt: skip


@pytest.mark.parametrize("path", REAL, ids=lambda p: p.name)
def test_the_verdicts_are_the_rules_applied_to_the_file(path: Path) -> None:
    raw = load(path)
    expected = [*pledge_findings(raw), *claim_findings(raw)]
    collusion = collusion_finding(raw)
    if collusion is not None:
        expected.append(collusion)
    got = [comparable(v) for v in raw["verdicts"]]
    assert got == [comparable(with_defaults(v)) for v in expected]


@pytest.mark.parametrize("path", REAL, ids=lambda p: p.name)
def test_the_real_sample_has_a_red_card_and_broken_pledges_and_no_claims(path: Path) -> None:
    raw = load(path)
    kinds = Counter(v["verdict"] for v in raw["verdicts"])
    assert kinds["pledge_violation"] >= 10 and kinds["collusion_red"] == 1
    assert kinds["false_claim"] == 0 and kinds["collusion_suspect"] == 0
    red = next(v for v in raw["verdicts"] if v["verdict"] == "collusion_red")
    assert red["seats"] == [2, 4] and len(red["lots"]) >= MIN_RED
    assert sorted(map(int, red["statistic_milli"])) == list(range(1, raw["night"]["n_lots"] + 1))
    assert all(red["statistic_milli"][str(n)] > THRESHOLD for n in red["lots"])
    assert red["threshold_milli"] == THRESHOLD
    assert f"阈值 {THRESHOLD}" in red["detail"]
    assert not [c for j in raw["judges"] for c in j["extractions"] if c["kind"] == "claim"]
    assert {v["verdict"] for v in raw["verdicts"] if v["seats"] == [5]} == {"pledge_violation"}


@pytest.mark.parametrize("path", REAL, ids=lambda p: p.name)
def test_the_partners_the_judges_name_are_the_other_colluder(path: Path) -> None:
    raw = load(path)
    named = {
        (item["seat"], item["partner"])
        for judge in raw["judges"]
        for item in judge["extractions"]
        if item.get("partner") is not None
    }
    assert named == {(2, 4), (4, 2)}


# ------------------------------------------------------------------ the fields added later
#
# ``talk_seqs`` and ``hammer_seq`` of a lot, the ``basis`` of awards and highlights (checked
# against the tables of the page above), ``topic`` and ``gap_milli`` of a false claim and
# ``statistic_milli`` and ``threshold_milli`` of a collusion verdict (rebuilt above),
# ``promise_class`` and ``partner`` of a promise (checked with the judges above), the three
# template digests of the manifest and the list of absent judges. The hand-written files predate
# them and simply do not have them, so a check that needs a field runs where the field is.


@pytest.mark.parametrize("path", PATHS, ids=lambda p: p.name)
def test_the_line_of_each_seat_and_the_hammer_are_events_inside_the_lot(path: Path) -> None:
    raw = load(path)
    for lot in raw["timeline"]:
        seqs = lot.get("talk_seqs") or {}
        if seqs:
            assert set(seqs) == {str(t["seat"]) for t in lot["talk"]}
            in_order = [seqs[str(seat)] for seat in lot["talk_order"]]
            assert in_order == sorted(set(in_order)), "all different, in speaking order"
            assert lot["first_seq"] < in_order[0] and in_order[-1] < lot["last_seq"]
        if lot.get("hammer_seq") is not None:
            assert lot["hammer_seq"] == lot["last_seq"]


@pytest.mark.parametrize("path", REAL, ids=lambda p: p.name)
def test_the_real_file_names_the_line_of_every_seat_and_the_hammer_of_every_lot(path: Path) -> None:
    raw = load(path)
    for lot in raw["timeline"]:
        assert set(lot["talk_seqs"]) == {str(s) for s in lot["talk_order"]}
        assert lot["hammer_seq"] == lot["last_seq"], lot["lot"]
    # each line is followed by the reading of a model seat and by its monologue, in that order,
    # and a bot has neither: the monologue of the talk phase sits one or two events after the line
    by_key = {
        (t["lot"], t["seat"]): t["seq"] for t in raw["reveal"]["thoughts"] if t["phase"] == "talk"
    }
    readers = {(g["lot"], g["seat"]) for g in raw["reveal"]["guesses"]}
    for lot in raw["timeline"]:
        for seat, line_seq in lot["talk_seqs"].items():
            key = (lot["lot"], int(seat))
            if key in by_key:
                assert by_key[key] - line_seq == (2 if key in readers else 1), key


@pytest.mark.parametrize("path", REAL, ids=lambda p: p.name)
def test_a_pledge_is_made_at_the_line_that_the_timeline_names(path: Path) -> None:
    raw = load(path)
    lots = {lot["lot"]: lot for lot in raw["timeline"]}
    assert raw["pledges"]
    for record in raw["pledges"]:
        assert lots[record["lot"]]["talk_seqs"][str(record["seat"])] == record["seq"]


@pytest.mark.parametrize("path", REAL, ids=lambda p: p.name)
def test_the_manifest_names_the_four_sets_of_templates_in_force(path: Path) -> None:
    manifest = load(path)["manifest"]
    names = ("prompt_hash", "host_prompt_hash", "judge_prompt_hash", "scan_prompt_hash")
    hashes = [manifest[name] for name in names]
    assert all(re.fullmatch(r"sha256:[0-9a-f]{64}", h) for h in hashes)
    assert len(set(hashes)) == 4, "the seats, the host, the judges and the classifier differ"
    assert manifest["judge_prompt_hash"] == judge_templates_digest("zh")
    assert manifest["host_prompt_hash"] == host_templates_digest("zh")
    assert manifest["scan_prompt_hash"] == scan_prompt_digest()


@pytest.mark.parametrize("path", PATHS, ids=lambda p: p.name)
def test_a_false_claim_says_what_it_was_about_and_how_far_off_it_was(path: Path) -> None:
    raw = load(path)
    for verdict in raw.get("verdicts", []):
        topic, gap = verdict.get("topic"), verdict.get("gap_milli")
        if verdict["verdict"] != "false_claim":
            assert topic is None and gap is None, "only a false claim has a topic and a gap"
            continue
        assert topic in (None, "budget", "valuation", "mission_kind", "mission_tag")
        if gap is not None:
            floor = TOLERANCE[topic] * 10  # the tolerance is in percent, the gap in thousandths
            assert topic in TOLERANCE and gap >= floor, "a false amount is past the tolerance"
        if topic in ("mission_kind", "mission_tag"):
            assert gap is None, "a mission has no gap"


@pytest.mark.parametrize("path", PATHS, ids=lambda p: p.name)
def test_a_collusion_verdict_shows_the_statistic_of_the_lots_and_the_threshold(path: Path) -> None:
    raw = load(path)
    n_lots = raw["night"]["n_lots"]
    for verdict in raw.get("verdicts", []):
        if verdict["verdict"].startswith("collusion"):
            numbers = {int(lot) for lot in verdict["statistic_milli"]}
            assert numbers == set(range(1, n_lots + 1)) and verdict["threshold_milli"] is not None
            assert set(verdict["lots"]) <= numbers
            assert verdict["topic"] is None and verdict["gap_milli"] is None
        else:
            assert not verdict.get("statistic_milli") and verdict.get("threshold_milli") is None


@pytest.mark.parametrize("path", BY_HAND, ids=lambda p: p.name)
def test_the_gap_of_a_hand_written_claim_is_the_claim_held_against_the_hole_card(
    path: Path,
) -> None:
    raw = load(path)
    tiers = tiers_of(raw)
    checked = 0
    for verdict in raw.get("verdicts", []):
        if verdict["verdict"] != "false_claim" or verdict.get("gap_milli") is None:
            continue
        (seat,), (number,) = verdict["seats"], verdict["lots"]
        assert verdict["topic"] == "valuation" and str(number) not in raw["reveal"]["true_values"]
        said = {
            e["number"]
            for judge in raw["judges"]
            for e in judge["extractions"]
            if (e["seat"], e["lot"], e["kind"]) == (seat, number, "claim") and e.get("number")
        }
        assert len(said) == 1, "both judges quote the same number"
        own = raw["reveal"]["values"][str(seat)][str(number)]
        gap = abs(said.pop() - own) * 1000 // tiers[number]
        assert verdict["gap_milli"] == gap
        assert gap * 100 > TOLERANCE["valuation"] * 1000, "past the tolerance of the claim"
        checked += 1
    assert checked == (1 if path.name == "sealed_four_lots.json" else 0)


@pytest.mark.parametrize("path", PATHS, ids=lambda p: p.name)
def test_a_night_with_an_absent_judge_keeps_only_the_findings_of_the_program(path: Path) -> None:
    raw = load(path)
    absent = raw.get("judges_absent", [])
    kinds = {v["verdict"] for v in raw.get("verdicts", [])}
    if absent:
        # without two judges nothing is agreed: no claim is checked and no red card can stand
        assert not kinds & {"false_claim", "collusion_red"}
        assert len(raw.get("judges", [])) + len(absent) <= 2
    assert path.name != "english_dutch.json" or len(absent) == 2, "the night with no judges at all"


def test_the_checks_of_the_gap_notice_a_gap_that_is_not_the_claim_held_against_the_hole_card() -> (
    None
):
    raw = by_name(SAMPLE)
    lot = next(entry for entry in raw["timeline"] if entry["format"] == "sealed_first")
    number, seat = lot["lot"], 3
    tier = tiers_of(raw)[number]
    own = raw["reveal"]["values"][str(seat)][str(number)]
    line = next(t for t in lot["talk"] if t["seat"] == seat)["text"]

    def with_claim(claimed: int, judges: tuple[int, ...] = (0, 1)) -> dict[str, Any]:
        item = {
            "seat": seat, "lot": number, "kind": "claim", "quote": line, "ambiguous": False,
            "topic": "valuation", "number": claimed, "text_value": None,
            "promise_class": None, "partner": None,
        }  # fmt: skip
        made = copy.deepcopy(raw)
        for index in judges:
            made["judges"][index]["extractions"].append(dict(item))
        return made

    assert claim_findings(with_claim(own + 5)) == [], "inside the tolerance of 15% of the tier"
    (found,) = claim_findings(with_claim(own + tier))
    assert (found["topic"], found["seats"], found["lots"]) == ("valuation", [seat], [number])
    assert found["gap_milli"] == 1000, "a whole tier off"
    assert found["quotes"] == [line] and found["intent"] == "unknown"
    (smaller,) = claim_findings(with_claim(own + 20 * tier // 100))
    assert smaller["gap_milli"] == 200
    assert claim_findings(with_claim(own + tier, judges=(0,))) == [], "one judge is no finding"


# ------------------------------------------------------------------- viewer tables


def test_viewer_format_titles_follow_the_mechanism_definitions() -> None:
    source = (VIEWER / "lib" / "format.js").read_text(encoding="utf-8")
    block = re.search(r"FORMAT_TITLES\s*=\s*Object\.freeze\(\{(.*?)\}\)", source, re.S)
    assert block, "FORMAT_TITLES not found in viewer/lib/format.js"
    titles = dict(re.findall(r"^\s*(\w+):\s*'([^']+)'", block.group(1), re.M))
    assert titles == {key: MECHANISMS[key].title_zh for key in FORMAT_KEYS}


def js_keys(source: str, name: str) -> list[str]:
    """The strings of ``export const NAME = Object.freeze([...])`` in a viewer module."""
    block = re.search(rf"{name}\s*=\s*Object\.freeze\(\[(.*?)\]\)", source, re.S)
    assert block, f"{name} not found"
    return re.findall(r"'([a-z_]+)'", block.group(1))


def test_viewer_award_keys_and_highlight_categories_follow_the_contract() -> None:
    awards = (VIEWER / "lib" / "awards.js").read_text(encoding="utf-8")
    assert js_keys(awards, "AWARD_KEYS") == list(AWARD_KEYS)
    highlights = (VIEWER / "lib" / "highlights.js").read_text(encoding="utf-8")
    assert js_keys(highlights, "CATEGORY_KEYS") == list(get_args(HighlightCategory))
